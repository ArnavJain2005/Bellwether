"""Bounded, read-only SIG fixture recorder (S1-T4-A).

    python -m engine.sig_client.record_fixtures --help

Without `--capture` the command only prints the planned reads and output paths. It reads
no credentials, creates no files and sends no requests. With `--capture` it records one
small, bounded sample of the six GET routes SigRestClient implements, through that
client, into a new local run directory. RECORDER.md (next to this file) describes the
capture format and the review required before any capture becomes a committed fixture.

How responses are observed. rest.py is not modified. The recorder builds the httpx
client with the same settings as SigRestClient.create (per-phase timeout,
follow_redirects=False, trust_env=False) and adds two httpx event hooks:
- The request hook runs before anything is sent. It refuses every request that is not a
  GET to one of the planned paths under the configured base URL, and every scoped read
  whose `tournamentId` is not the configured tournament. It reads the method and URL
  only, never headers.
- The response hook reads the body. httpx would read it next anyway, inside the same
  `send()` call, so the read stays under the client's read deadline and the client then
  decodes the same cached bytes. The hook keeps the status, allowlisted headers and (for
  2xx only) the decoded body bytes in memory. Bodies are written after the client call
  returns or fails, and only then is the step's validation outcome known.
Request building, retries, deadlines, rate admission and validation are the client's
own; the recorder adds no HTTP or retry logic.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import functools
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urlencode

import httpx

from engine.config import (
    API_KEY_ENV_VAR,
    SIG_BASE_URL,
    ConfigError,
    SigConfig,
    TournamentRef,
    load_sig_config,
)
from engine.sig_client.errors import (
    SigApiError,
    SigBadRequestError,
    SigClientError,
    SigContextMismatchError,
    SigDeadlineExceededError,
    SigNotFoundError,
    SigRateLimitedError,
    SigResponseError,
    SigRetryExhaustedError,
    SigTransportError,
    known_code,
)
from engine.sig_client.rate_limiter import Clock, RateLimiter, RequestKind, Sleep
from engine.sig_client.rest import (
    MARKET_STATUSES,
    MARKETS_PAGE_MAX,
    ORDER_STATUSES,
    ReadRetryPolicy,
    SigRestClient,
)

CAPTURE_FORMAT = "bellwether.sig-capture/1"
RECORDER_VERSION = "1"
DEFAULT_OUTPUT_ROOT = Path("sig_captures")
MANIFEST_NAME = "manifest.json"
BODIES_DIR = "bodies"

# Recorder bounds: deliberately far below the API's documented maximums.
MAX_SELECTED_EXCHANGES = 10
MAX_SELECTED_MARKETS = 10
MAX_PAGES = 3
DEFAULT_PAGES = 1
DEFAULT_MARKETS_LIMIT = 10
ORDERS_LIMIT_MAX = 50
DEFAULT_ORDERS_LIMIT = 10
DEPTH_MAX = 20
DEFAULT_DEPTH = 5
# The recorder runs in its own process and cannot share the bot's limiter, so it takes a
# small read budget of its own: 15 + the bot's 80 stays under SIG's 100 per minute.
# A burst of 1 spaces reads evenly (one every 4 s). With a full burst, the 16th quick
# read would wait about a minute for the rolling window, longer than the client's read
# deadline, and a large plan would fail part-way.
RECORDER_READ_LIMIT = 15
RECORDER_READ_BURST = 1
MAX_BODY_BYTES = 8 * 1024 * 1024
# Error bodies larger than this are not even parsed for their code.
MAX_ERROR_BODY_INSPECT = 64 * 1024

BLOCKED_REASON = (
    "the rate limiter has blocked read admission (SIG sent a Retry-After too long to "
    "hold); recording stopped. The recorder never clears a block: wait out SIG's limit "
    "and start a new run."
)
RATE_LIMITED_REASON = (
    "SIG rate-limited the account (HTTP 429); recording stopped to protect the read "
    "budget shared with the trading bot."
)

_NUMERIC_ID = re.compile(r"[0-9]{1,20}")
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_MEDIA_TYPE = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,63}")
_DELTA_SECONDS = re.compile(r"[0-9]{1,15}(\.[0-9]{1,9})?")
_CURSOR_VALUE = re.compile(r"[\x21-\x7e]{1,512}")
_PARAM_VALUE = re.compile(r"[A-Za-z0-9,_-]{1,2200}")
_SAFE_PARAM_NAMES = frozenset(
    {"cursor", "depth", "exchangeId", "ids", "limit", "marketId", "status", "tournamentId"}
)

Now = Callable[[], datetime]


class RecorderArgumentError(ValueError):
    """Invalid recorder arguments. Messages never echo the rejected value."""


class RecorderOutputError(RuntimeError):
    """The output directory cannot be used (for example, it already exists)."""


class RecorderSafetyError(RuntimeError):
    """The request hook refused a request that is outside the recording plan."""


def utc_now() -> datetime:
    return datetime.now(UTC)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# --- plan -------------------------------------------------------------------------------


def _check_ids(name: str, values: Sequence[str], cap: int) -> tuple[str, ...]:
    if isinstance(values, str) or not isinstance(values, Sequence):
        raise RecorderArgumentError(f"{name} must be a sequence of id strings")
    ids = tuple(values)
    if not 1 <= len(ids) <= cap:
        raise RecorderArgumentError(f"select 1 to {cap} {name}")
    for value in ids:
        if not isinstance(value, str) or not _NUMERIC_ID.fullmatch(value):
            raise RecorderArgumentError(f"every {name} entry must be 1-20 ASCII digits")
    if len(set(ids)) != len(ids):
        raise RecorderArgumentError(f"{name} must not contain duplicates")
    return ids


def _check_int(name: str, value: object, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise RecorderArgumentError(f"{name} must be an integer from {low} to {high}")
    return value


def _check_choice(name: str, value: object, choices: frozenset[str]) -> str:
    if value not in choices:
        raise RecorderArgumentError(f"{name} must be one of {sorted(choices)}")
    return value  # type: ignore[return-value]


@dataclass(frozen=True)
class CapturePlan:
    """What one run reads. Every field is validated before any request is possible."""

    tournament: TournamentRef
    exchange_ids: tuple[str, ...]
    market_ids: tuple[str, ...]
    output_root: Path = DEFAULT_OUTPUT_ROOT
    markets_status: str = "any"
    markets_page_limit: int = DEFAULT_MARKETS_LIMIT
    markets_max_pages: int = DEFAULT_PAGES
    include_orders: bool = True
    orders_status: str = "open"
    orders_page_limit: int = DEFAULT_ORDERS_LIMIT
    orders_max_pages: int = DEFAULT_PAGES
    depth: int = DEFAULT_DEPTH

    def __post_init__(self) -> None:
        if not isinstance(self.tournament, TournamentRef):
            raise RecorderArgumentError("tournament must be a TournamentRef")
        set_ = functools.partial(object.__setattr__, self)
        set_("exchange_ids", _check_ids("exchange ids", self.exchange_ids, MAX_SELECTED_EXCHANGES))
        set_("market_ids", _check_ids("market ids", self.market_ids, MAX_SELECTED_MARKETS))
        if not isinstance(self.output_root, Path):
            raise RecorderArgumentError("output_root must be a Path")
        _check_choice("markets status", self.markets_status, MARKET_STATUSES)
        _check_int("markets page limit", self.markets_page_limit, 1, MARKETS_PAGE_MAX)
        _check_int("markets max pages", self.markets_max_pages, 1, MAX_PAGES)
        if not isinstance(self.include_orders, bool):
            raise RecorderArgumentError("include_orders must be a bool")
        _check_choice("orders status", self.orders_status, ORDER_STATUSES)
        _check_int("orders page limit", self.orders_page_limit, 1, ORDERS_LIMIT_MAX)
        _check_int("orders max pages", self.orders_max_pages, 1, MAX_PAGES)
        _check_int("depth", self.depth, 1, DEPTH_MAX)


@dataclass(frozen=True)
class PlannedRead:
    """One planned GET. A paginated read may run up to `max_pages` pages."""

    name: str
    route: str
    path: str
    params: Mapping[str, str]
    max_pages: int = 1

    def as_manifest(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "route": self.route,
            "path": self.path,
            "params": dict(self.params),
            "max_pages": self.max_pages,
        }


def planned_reads(plan: CapturePlan) -> list[PlannedRead]:
    """The reads one run makes, in order, with the query parameters the client sends."""
    slug = plan.tournament.slug
    tid = plan.tournament.id
    depth = str(plan.depth)
    reads = [
        PlannedRead("tournament", "GET /tournaments/{slug}", f"/tournaments/{slug}", {}),
        PlannedRead(
            "tournament-markets",
            "GET /tournaments/{slug}/markets",
            f"/tournaments/{slug}/markets",
            {"limit": str(plan.markets_page_limit), "status": plan.markets_status},
            max_pages=plan.markets_max_pages,
        ),
        PlannedRead(
            "exchange-prices",
            "GET /exchanges/prices",
            "/exchanges/prices",
            {"ids": ",".join(plan.exchange_ids), "tournamentId": tid},
        ),
    ]
    reads += [
        PlannedRead(
            f"exchange-orderbook-{exchange_id}",
            "GET /exchanges/{id}/orderbook",
            f"/exchanges/{exchange_id}/orderbook",
            {"depth": depth, "tournamentId": tid},
        )
        for exchange_id in plan.exchange_ids
    ]
    reads += [
        PlannedRead(
            f"market-orderbook-{market_id}",
            "GET /markets/{id}/orderbook",
            f"/markets/{market_id}/orderbook",
            {"depth": depth, "tournamentId": tid},
        )
        for market_id in plan.market_ids
    ]
    if plan.include_orders:
        reads.append(
            PlannedRead(
                "orders",
                "GET /orders",
                "/orders",
                {
                    "status": plan.orders_status,
                    "limit": str(plan.orders_page_limit),
                    "tournamentId": tid,
                },
                max_pages=plan.orders_max_pages,
            )
        )
    return reads


def render_preview(plan: CapturePlan, *, base_url: str = SIG_BASE_URL) -> str:
    reads = planned_reads(plan)
    # The tournament read is also the verification; later reads reuse it.
    max_reads = sum(read.max_pages for read in reads)
    attempts = max_reads * ReadRetryPolicy().max_attempts
    root = plan.output_root.resolve()
    lines = [
        "SIG fixture recorder: PREVIEW ONLY. No credentials were read, no files were",
        "created and no requests were sent.",
        "",
        f"Tournament: {plan.tournament.slug} / {plan.tournament.id}",
        f"Base URL:   {base_url}",
        "Planned GET requests, in order:",
    ]
    for number, read in enumerate(reads, 1):
        query = f"?{urlencode(dict(read.params))}" if read.params else ""
        line = f"  {number:2d}. GET {read.path}{query}"
        if read.max_pages > 1:
            line += f"  (up to {read.max_pages} pages; later pages add cursor=<nextCursor>)"
        elif read.name in ("tournament-markets", "orders"):
            line += "  (1 page only; a longer collection is recorded as truncated)"
        lines.append(line)
    if not plan.include_orders:
        lines.append("  (GET /orders skipped: --skip-orders)")
    lines += [
        "",
        (
            f"Logical reads: at most {max_reads}. HTTP attempts: at most {attempts} "
            f"({ReadRetryPolicy().max_attempts} per read, client retry policy)."
        ),
        (
            f"Recorder read budget: {RECORDER_READ_LIMIT} reads/minute on its own limiter, "
            f"one every {60 / RECORDER_READ_LIMIT:g} s."
        ),
        "",
        "Output (created only with --capture):",
        f"  root:          {root}",
        f"  run directory: {root / '<UTC time>-<random>'}  (new per run; never reused)",
        f"  files:         {MANIFEST_NAME}, .gitignore (ignores the whole run),",
        f"                 {BODIES_DIR}/<NN>-<step>-a<attempt>.json (2xx bodies, byte-exact)",
        "",
        f"To record, add --capture. It reads {API_KEY_ENV_VAR} from the environment.",
        "Captures are local and unreviewed; see engine/sig_client/RECORDER.md.",
    ]
    return "\n".join(lines)


# --- observation ------------------------------------------------------------------------


def _safe_params(url: httpx.URL) -> tuple[dict[str, str], int]:
    """Allowlisted query parameters, and how many others were left out."""
    kept: dict[str, str] = {}
    omitted = 0
    for name, value in url.params.multi_items():
        pattern = _CURSOR_VALUE if name == "cursor" else _PARAM_VALUE
        if name in _SAFE_PARAM_NAMES and name not in kept and pattern.fullmatch(value):
            kept[name] = value
        else:
            omitted += 1
    return kept, omitted


def _safe_headers(headers: httpx.Headers) -> dict[str, str]:
    """Only the media type and a delta-seconds Retry-After. Everything else is dropped."""
    kept: dict[str, str] = {}
    content_type = headers.get("content-type")
    if content_type is not None:
        media = content_type.split(";", 1)[0].strip().lower()
        kept["content-type"] = media if _MEDIA_TYPE.fullmatch(media) else "<omitted>"
    retry_after = headers.get("retry-after")
    if retry_after is not None:
        text = retry_after.strip()
        kept["retry-after"] = text if _DELTA_SECONDS.fullmatch(text) else "<omitted>"
    return kept


def _error_body_summary(content: bytes) -> dict[str, Any]:
    """A non-2xx body's shape and documented SIG code only; its text is never kept."""
    if len(content) > MAX_ERROR_BODY_INSPECT:
        return {"shape": "large error body (not inspected)"}
    try:
        body = json.loads(content)
    except (ValueError, RecursionError):
        return {"shape": "non-JSON error body"}
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        raw = body["error"].get("code")
        code = known_code(raw)
        return {
            "shape": "error envelope",
            "code": code,
            "unrecognized_code": code is None and raw is not None,
        }
    return {"shape": "error body without an error object"}


def _reject_constant(name: str) -> None:
    raise ValueError(f"non-finite JSON constant {name}")


def _is_client_json(content: bytes) -> bool:
    """Decodes under the client's own rules (exact Decimal numbers, no NaN/Infinity)."""
    try:
        json.loads(content, parse_float=Decimal, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        return False
    return True


@dataclass
class _Attempt:
    number: int
    path: str
    params: dict[str, str]
    omitted_params: int
    started_at: str
    http_status: int | None = None
    body_read: bool = False
    headers: dict[str, str] = field(default_factory=dict)
    error_body: dict[str, Any] | None = None
    body_info: dict[str, Any] | None = None
    # Decoded 2xx content, in memory only until the step finishes.
    pending_body: bytes | None = field(default=None, repr=False)

    def as_manifest(self) -> dict[str, Any]:
        return {
            "attempt": self.number,
            "path": self.path,
            "params": self.params,
            "omitted_params": self.omitted_params,
            "started_at": self.started_at,
            "outcome": (
                "no_response"
                if self.http_status is None
                else "response" if self.body_read else "body_not_read"
            ),
            "http_status": self.http_status,
            "headers": self.headers,
            "body": self.body_info,
            "error_body": self.error_body,
        }


@dataclass
class _Step:
    index: int
    name: str
    route: str
    path: str
    params: dict[str, str]
    started_at: str
    finished_at: str | None = None
    outcome: str = "running"
    client_validation_passed: bool = False
    error: dict[str, Any] | None = None
    page: dict[str, Any] | None = None
    attempts: list[_Attempt] = field(default_factory=list)

    def as_manifest(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "name": self.name,
            "route": self.route,
            "path": self.path,
            "params": self.params,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "outcome": self.outcome,
            "client_validation_passed": self.client_validation_passed,
            "error": self.error,
            "page": self.page,
            "attempts": [attempt.as_manifest() for attempt in self.attempts],
        }


class _Observer:
    """httpx event hooks. Never reads request headers; never raises for planned reads."""

    def __init__(self, config: SigConfig, reads: Sequence[PlannedRead], now: Now) -> None:
        base = httpx.URL(config.base_url)
        self._origin = (base.scheme, base.host, base.port)
        self._prefix = base.path.rstrip("/")
        self._tournament_id = config.tournament.id
        self._paths = {read.path: "tournamentId" in read.params for read in reads}
        self._now = now
        self.current: _Step | None = None
        self.refused = 0

    def _refuse(self, reason: str) -> RecorderSafetyError:
        self.refused += 1
        return RecorderSafetyError(reason)

    async def on_request(self, request: httpx.Request) -> None:
        url = request.url
        if (url.scheme, url.host, url.port) != self._origin:
            raise self._refuse("request is not for the configured SIG base URL")
        if not url.path.startswith(self._prefix + "/"):
            raise self._refuse("request is outside the configured API base path")
        path = url.path[len(self._prefix) :]
        if request.method != "GET":
            raise self._refuse("the recorder sends GET requests only")
        if path not in self._paths:
            raise self._refuse("request path is not one of the planned reads")
        if self._paths[path] and url.params.get_list("tournamentId") != [self._tournament_id]:
            raise self._refuse("scoped read without exactly the configured tournamentId")
        step = self.current
        if step is None:
            raise self._refuse("request outside a recording step")
        params, omitted = _safe_params(url)
        step.attempts.append(
            _Attempt(
                number=len(step.attempts) + 1,
                path=path,
                params=params,
                omitted_params=omitted,
                started_at=_stamp(self._now()),
            )
        )

    async def on_response(self, response: httpx.Response) -> None:
        step = self.current
        if step is None or not step.attempts:
            return  # unreachable: on_request refused the request
        attempt = step.attempts[-1]
        attempt.http_status = response.status_code
        attempt.headers = _safe_headers(response.headers)
        await response.aread()
        attempt.body_read = True
        if 200 <= response.status_code < 300:
            attempt.pending_body = response.content
        else:
            attempt.error_body = _error_body_summary(response.content)


# --- failure classification -------------------------------------------------------------


@dataclass(frozen=True)
class _Failure:
    outcome: str
    error: dict[str, Any]
    stop_reason: str | None


def _api_error_info(error: SigApiError) -> dict[str, Any]:
    # Built from the client's sanitised attributes only (see errors.py).
    return {
        "class": type(error).__name__,
        "http_status": error.status,
        "code": error.code,
        "unrecognized_code": error.unrecognized_code,
        "detail": error.detail,
        "retry_after_s": error.retry_after,
        "retry_after_oversized": error.retry_after_oversized,
    }


def _classify(exc: SigClientError | RecorderSafetyError, limiter: RateLimiter) -> _Failure:
    """Turn a client error, or a refused request, into a step outcome."""
    if isinstance(exc, SigContextMismatchError):
        return _Failure(
            "context_mismatch",
            {"class": type(exc).__name__, "reason": exc.reason},
            "SIG reported a different tournament than configured; recording stopped.",
        )
    if isinstance(exc, SigResponseError):
        # reason is client-written: field locations and error types, never values.
        return _Failure(
            "validation_failed", {"class": type(exc).__name__, "reason": exc.reason}, None
        )
    if isinstance(exc, RecorderSafetyError):
        return _Failure(
            "safety_refusal",
            {"class": type(exc).__name__, "reason": str(exc)},
            "the recorder refused a request outside its plan; recording stopped.",
        )
    error: dict[str, Any] = {"class": type(exc).__name__}
    root: Exception = exc
    if isinstance(exc, (SigRetryExhaustedError, SigDeadlineExceededError)):
        error["attempts"] = exc.attempts
        if isinstance(exc, SigDeadlineExceededError):
            error["phase"] = exc.phase
        if exc.last_error is not None:
            root = exc.last_error
    if isinstance(root, SigApiError):
        error["http"] = _api_error_info(root)
    elif isinstance(root, SigTransportError):
        error["transport_error"] = root.kind

    if limiter.is_blocked(RequestKind.READ):
        return _Failure("blocked", error, BLOCKED_REASON)
    if isinstance(root, SigRateLimitedError):
        return _Failure("rate_limited", error, RATE_LIMITED_REASON)
    if isinstance(exc, (SigNotFoundError, SigBadRequestError)):
        # Specific to the selected id or parameters: record it and carry on.
        return _Failure("http_error", error, None)
    if isinstance(exc, SigApiError):
        return _Failure(
            "http_error", error, f"SIG answered HTTP {exc.status}; recording stopped."
        )
    if isinstance(exc, SigRetryExhaustedError):
        return _Failure("retry_exhausted", error, "transient failures persisted; stopped.")
    if isinstance(exc, SigDeadlineExceededError):
        return _Failure("deadline_exceeded", error, "a read ran out of time; stopped.")
    if isinstance(exc, SigTransportError):
        return _Failure("transport_error", error, "the connection to SIG failed; stopped.")
    return _Failure("client_error", error, "the SIG client failed; recording stopped.")


class _StopRun(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# --- files ------------------------------------------------------------------------------


def _atomic_write(path: Path, data: bytes) -> None:
    """Write to a temporary sibling, fsync, then rename. Never leaves a partial file."""
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        with open(tmp, "xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _git(root: Path, *args: str) -> str | None:
    try:
        done = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def describe_build() -> dict[str, Any]:
    """Code and spec identification for the manifest. Called only when capturing."""
    root = _repo_root()
    commit = _git(root, "rev-parse", "HEAD")
    status = _git(root, "status", "--porcelain", "--untracked-files=no")
    spec_path = root / "docs" / "api" / "openapi.json"
    spec: dict[str, Any] = {"path": "docs/api/openapi.json", "sha256": None, "version": None}
    try:
        raw = spec_path.read_bytes()
    except OSError:
        pass
    else:
        spec["sha256"] = hashlib.sha256(raw).hexdigest()
        with contextlib.suppress(ValueError, AttributeError):
            spec["version"] = json.loads(raw).get("info", {}).get("version")
    return {
        "code": {
            "recorder_version": RECORDER_VERSION,
            "git_commit": commit,
            "git_dirty": None if status is None else bool(status),
        },
        "spec": spec,
    }


# --- recording --------------------------------------------------------------------------

_TRANSFORMATIONS = [
    (
        "2xx response bodies are stored byte-for-byte after HTTP content decoding (for "
        "example gzip); they are never parsed and re-serialised, so numbers, nulls and "
        "key order are exactly as SIG sent them."
    ),
    (
        "2xx bodies that are not valid JSON under the client's decoding rules, or larger "
        f"than {MAX_BODY_BYTES} bytes, are not stored; their size and SHA-256 are."
    ),
    (
        "non-2xx response bodies are never stored: only the HTTP status, the body's shape "
        "and a documented SIG error code (if any) are kept."
    ),
    (
        "response headers other than the media type and a delta-seconds Retry-After are "
        "omitted (no Set-Cookie, no Location, no server or tracing headers)."
    ),
    (
        "request headers are never read or recorded: no Authorization header, API key or "
        "cookie, and no complete request objects."
    ),
    "query parameters outside an allowlist are omitted and only counted.",
    (
        "error and exception text is never recorded: only client error classes, "
        "documented codes, fixed client-written descriptions and client-written "
        "validation summaries."
    ),
]
_REVIEW_NOTES = [
    "Not sanitised. Removing credentials does not make a capture safe to publish.",
    (
        "The tournament body carries the account's balance (myBalance) and orders bodies "
        "carry the account's own orders. Review them before any capture becomes a "
        "committed fixture."
    ),
    "A committed fixture must keep this manifest's provenance and be reviewed by Arnav.",
]


@dataclass(frozen=True)
class RunResult:
    status: str
    exit_code: int
    run_dir: Path
    stop_reason: str | None


class _Recorder:
    def __init__(
        self,
        plan: CapturePlan,
        config: SigConfig,
        *,
        run_id: str,
        provenance: str,
        transport: httpx.AsyncBaseTransport | None,
        limiter: RateLimiter,
        clock: Clock,
        sleep: Sleep,
        now: Now,
        build: Mapping[str, Any],
    ) -> None:
        self._plan = plan
        self._config = config
        self._reads = planned_reads(plan)
        self._run_dir = plan.output_root / run_id
        self._run_id = run_id
        self._provenance = provenance
        self._limiter = limiter
        self._now = now
        self._build = build
        self._observer = _Observer(config, self._reads, now)
        # Same settings as SigRestClient.create, plus the observation hooks.
        http = httpx.AsyncClient(
            transport=transport,
            timeout=config.timeout,
            follow_redirects=False,
            trust_env=False,
            event_hooks={
                "request": [self._observer.on_request],
                "response": [self._observer.on_response],
            },
        )
        self._client = SigRestClient(
            config, http, owns_http=True, limiter=limiter, clock=clock, sleep=sleep
        )
        self._steps: list[_Step] = []
        self._collections: dict[str, dict[str, Any]] = {}
        self._anomalies: list[str] = []
        self._status = "in_progress"
        self._stop_reason: str | None = None
        self._started_at = _stamp(now())
        self._finished_at: str | None = None

    # -- files --

    def _prepare_directory(self) -> None:
        self._plan.output_root.mkdir(parents=True, exist_ok=True)
        try:
            self._run_dir.mkdir()
        except FileExistsError:
            raise RecorderOutputError(
                f"run directory already exists; refusing to reuse it: {self._run_dir}"
            ) from None
        _atomic_write(
            self._run_dir / ".gitignore",
            b"# Local SIG capture: not reviewed. Never commit it as is (see RECORDER.md).\n*\n",
        )
        (self._run_dir / BODIES_DIR).mkdir()

    def _manifest(self) -> dict[str, Any]:
        plan = self._plan
        ran = {step.name for step in self._steps}
        not_run = [
            read.name
            for read in self._reads
            if read.name not in ran and f"{read.name}-p1" not in ran
        ]
        synthetic = self._provenance != "recorded"
        return {
            "format": CAPTURE_FORMAT,
            "provenance": {
                "kind": self._provenance,
                "detail": (
                    "produced against an injected test transport: NOT a SIG recording"
                    if synthetic
                    else "genuine responses recorded from SIG by this recorder; unreviewed"
                ),
            },
            "run": {
                "id": self._run_id,
                "status": self._status,
                "stop_reason": self._stop_reason,
                "started_at": self._started_at,
                "finished_at": self._finished_at,
            },
            **dict(self._build),
            "target": {
                "base_url": self._config.base_url,
                "tournament": {"slug": plan.tournament.slug, "id": plan.tournament.id},
            },
            "selection": {
                "exchange_ids": list(plan.exchange_ids),
                "market_ids": list(plan.market_ids),
                "markets_status": plan.markets_status,
                "markets_page_limit": plan.markets_page_limit,
                "markets_max_pages": plan.markets_max_pages,
                "include_orders": plan.include_orders,
                "orders_status": plan.orders_status,
                "orders_page_limit": plan.orders_page_limit,
                "orders_max_pages": plan.orders_max_pages,
                "depth": plan.depth,
            },
            "limits": {
                "recorder_read_limit_per_minute": RECORDER_READ_LIMIT,
                "recorder_read_burst": RECORDER_READ_BURST,
                "max_body_bytes": MAX_BODY_BYTES,
            },
            "plan": [read.as_manifest() for read in self._reads],
            "steps": [step.as_manifest() for step in self._steps],
            "not_run": not_run,
            "collections": self._collections,
            "anomalies": self._anomalies,
            "refused_requests": self._observer.refused,
            "transformations": _TRANSFORMATIONS,
            "review": {
                "sanitized": False,
                "required_before_commit": True,
                "notes": _REVIEW_NOTES,
            },
        }

    def _write_manifest(self) -> None:
        data = json.dumps(self._manifest(), indent=2, ensure_ascii=False, allow_nan=False)
        _atomic_write(self._run_dir / MANIFEST_NAME, (data + "\n").encode("utf-8"))

    def _store_bodies(self, step: _Step) -> None:
        for attempt in step.attempts:
            content = attempt.pending_body
            if content is None:
                continue
            info: dict[str, Any] = {
                "recorded": False,
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            if len(content) > MAX_BODY_BYTES:
                info["omitted"] = "larger than the recorder's size cap"
            elif not _is_client_json(content):
                info["omitted"] = "not valid JSON under the client's rules (may be any text)"
            else:
                relative = f"{BODIES_DIR}/{step.index:02d}-{step.name}-a{attempt.number}.json"
                target = self._run_dir / relative
                if target.exists():
                    raise RecorderOutputError("a body file already exists; refusing to replace")
                _atomic_write(target, content)
                info.update(recorded=True, file=relative)
            attempt.body_info = info
            # Cleared only now: if the write failed, _discard_bodies records why.
            attempt.pending_body = None

    @staticmethod
    def _discard_bodies(step: _Step, reason: str) -> None:
        for attempt in step.attempts:
            if attempt.pending_body is not None:
                attempt.body_info = {"recorded": False, "omitted": reason}
                attempt.pending_body = None

    # -- steps --

    def _check_blocked(self) -> None:
        if self._limiter.is_blocked(RequestKind.READ):
            raise _StopRun(BLOCKED_REASON)

    async def _step(
        self,
        name: str,
        read: PlannedRead,
        params: Mapping[str, str],
        call: Callable[[], Awaitable[Any]],
        describe: Callable[[Any], dict[str, Any]] | None = None,
    ) -> tuple[Any, _Failure | None]:
        self._check_blocked()
        step = _Step(
            index=len(self._steps) + 1,
            name=name,
            route=read.route,
            path=read.path,
            params=dict(params),
            started_at=_stamp(self._now()),
        )
        self._steps.append(step)
        self._observer.current = step
        result: Any = None
        failure: _Failure | None = None
        try:
            try:
                result = await call()
            finally:
                self._observer.current = None
        except (asyncio.CancelledError, KeyboardInterrupt):
            step.outcome = "interrupted"
            step.finished_at = _stamp(self._now())
            self._discard_bodies(step, "the run was interrupted before this read finished")
            raise
        except (SigClientError, RecorderSafetyError) as exc:
            # Anything else is a recorder bug: it ends the run with status "error".
            failure = _classify(exc, self._limiter)
            step.outcome = failure.outcome
            step.error = failure.error
        else:
            step.outcome = "passed"
            step.client_validation_passed = True
            if describe is not None:
                step.page = describe(result)
        step.finished_at = _stamp(self._now())
        try:
            self._store_bodies(step)
        except BaseException:
            # The step's evidence is incomplete, so it never counts as passed.
            step.outcome = "recorder_error"
            self._discard_bodies(step, "the recorder failed while writing the body")
            raise
        self._write_manifest()
        if failure is not None and failure.stop_reason is not None:
            raise _StopRun(failure.stop_reason)
        return result, failure

    async def _paginate(
        self,
        key: str,
        read: PlannedRead,
        fetch: Callable[[str | None], Awaitable[Any]],
    ) -> None:
        summary: dict[str, Any] = {
            "max_pages": read.max_pages,
            "pages_fetched": 0,
            "complete": False,
            "truncated": False,
            "note": None,
        }
        self._collections[key] = summary
        seen: set[str] = set()
        cursor: str | None = None
        for number in range(1, read.max_pages + 1):
            params = dict(read.params)
            if cursor is not None:
                params["cursor"] = cursor
            try:
                page, _ = await self._step(
                    f"{read.name}-p{number}",
                    read,
                    params,
                    functools.partial(fetch, cursor),
                    describe=lambda page: {
                        "items": len(page.data),
                        "has_more": page.pagination.hasMore,
                        "next_cursor_present": page.pagination.nextCursor is not None,
                    },
                )
            except _StopRun:
                summary["note"] = f"page {number} failed and the run stopped: incomplete"
                raise
            if page is None:
                summary["note"] = f"page {number} failed: the collection is incomplete"
                return
            summary["pages_fetched"] = number
            has_more = page.pagination.hasMore
            next_cursor = page.pagination.nextCursor
            anomaly = None
            if not has_more and next_cursor is not None:
                anomaly = "hasMore is false but nextCursor is set"
            elif has_more and next_cursor is None:
                anomaly = "hasMore is true but nextCursor is null"
            elif has_more and next_cursor in seen:
                anomaly = "SIG repeated a pagination cursor"
            if anomaly is not None:
                summary["note"] = anomaly
                self._anomalies.append(f"{key} page {number}: {anomaly}")
                return
            if not has_more:
                summary["complete"] = True
                return
            seen.add(next_cursor)
            cursor = next_cursor
        summary["truncated"] = True
        summary["note"] = (
            "stopped at the page cap while hasMore was true: a sample, not the full collection"
        )

    async def _run_reads(self) -> None:
        reads = {read.name: read for read in self._reads}
        client = self._client
        plan = self._plan

        _, failure = await self._step(
            "tournament", reads["tournament"], {}, client.get_tournament
        )
        if failure is not None:
            raise _StopRun("tournament verification failed; no scoped read can be trusted.")

        await self._paginate(
            "tournament_markets",
            reads["tournament-markets"],
            lambda cursor: client.list_tournament_markets_page(
                limit=plan.markets_page_limit, cursor=cursor, status=plan.markets_status
            ),
        )
        prices = reads["exchange-prices"]
        await self._step(
            prices.name,
            prices,
            prices.params,
            functools.partial(client.get_exchange_prices, list(plan.exchange_ids)),
        )
        for exchange_id in plan.exchange_ids:
            read = reads[f"exchange-orderbook-{exchange_id}"]
            await self._step(
                read.name,
                read,
                read.params,
                functools.partial(client.get_exchange_orderbook, exchange_id, depth=plan.depth),
            )
        for market_id in plan.market_ids:
            read = reads[f"market-orderbook-{market_id}"]
            await self._step(
                read.name,
                read,
                read.params,
                functools.partial(client.get_market_orderbook, market_id, depth=plan.depth),
            )
        if plan.include_orders:
            await self._paginate(
                "orders",
                reads["orders"],
                lambda cursor: client.list_orders_page(
                    status=plan.orders_status, limit=plan.orders_page_limit, cursor=cursor
                ),
            )

    def _final_status(self) -> str:
        failed = any(step.outcome != "passed" for step in self._steps)
        incomplete = self._manifest()["not_run"]
        return "partial" if failed or incomplete or self._anomalies else "succeeded"

    def _finish(self, status: str, stop_reason: str | None) -> None:
        self._status = status
        self._stop_reason = stop_reason
        self._finished_at = _stamp(self._now())
        for step in self._steps:
            if step.outcome == "running":
                step.outcome = "recorder_error"
                self._discard_bodies(step, "the recorder failed before this read finished")
        self._write_manifest()

    async def run(self) -> RunResult:
        try:
            self._prepare_directory()
            self._write_manifest()
        except BaseException:
            await self._client.aclose()
            raise
        try:
            await self._run_reads()
        except _StopRun as stop:
            status, reason = "aborted", stop.reason
        except (asyncio.CancelledError, KeyboardInterrupt):
            # A failed manifest write must not replace the cancellation; the manifest on
            # disk then still says "in_progress", which is never mistaken for success.
            with contextlib.suppress(OSError):
                self._finish("interrupted", "cancelled (for example Ctrl+C) before the end")
            raise
        except Exception as exc:  # noqa: BLE001 - becomes status "error" in the manifest
            # Only the class name: exception text could hold response data or paths.
            status, reason = "error", f"recorder error: {type(exc).__name__}"
        else:
            status, reason = self._final_status(), None
        finally:
            await self._client.aclose()
        self._finish(status, reason)
        return RunResult(status, 0 if status == "succeeded" else 1, self._run_dir, reason)


def _new_run_id(now: Now) -> str:
    return f"{now().astimezone(UTC):%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}"


async def record(
    plan: CapturePlan,
    config: SigConfig,
    *,
    run_id: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    limiter: RateLimiter | None = None,
    clock: Clock = time.monotonic,
    sleep: Sleep = asyncio.sleep,
    now: Now = utc_now,
    build: Mapping[str, Any] | None = None,
) -> RunResult:
    """Record one bounded sample into a new run directory under `plan.output_root`.

    With `transport` left as None the run talks to `config.base_url` and is labelled a
    genuine recording. Any injected transport makes the run "synthetic", whatever it
    serves, so a test can never produce a capture labelled as recorded.
    """
    if plan.tournament != config.tournament:
        raise RecorderArgumentError("the plan and the config name different tournaments")
    if run_id is None:
        run_id = _new_run_id(now)
    elif not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise RecorderArgumentError("run id must be 1-64 letters, digits, '.', '_' or '-'")
    if limiter is None:
        limiter = RateLimiter(
            read_limit=RECORDER_READ_LIMIT,
            read_burst=RECORDER_READ_BURST,
            write_limit=1,
            clock=clock,
            sleep=sleep,
        )
    recorder = _Recorder(
        plan,
        config,
        run_id=run_id,
        provenance="recorded" if transport is None else "synthetic",
        transport=transport,
        limiter=limiter,
        clock=clock,
        sleep=sleep,
        now=now,
        build=describe_build() if build is None else build,
    )
    return await recorder.run()


# --- replay -----------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplayResponse:
    """A recorded 2xx body that the client accepted, ready to serve to a mock transport."""

    provenance: str
    step: str
    path: str
    params: Mapping[str, str]
    status: int
    content_type: str | None
    body: bytes


def load_replayable(run_dir: Path) -> list[ReplayResponse]:
    """Every step that passed client validation, with its exact body bytes.

    Failed steps (including validation failures kept as local evidence) are left out.
    Each body is checked against its recorded SHA-256 and must lie inside `run_dir`.
    """
    root = run_dir.resolve()
    manifest = json.loads((root / MANIFEST_NAME).read_bytes())
    if manifest.get("format") != CAPTURE_FORMAT:
        raise ValueError("not a Bellwether SIG capture manifest")
    provenance = manifest["provenance"]["kind"]
    responses = []
    for step in manifest["steps"]:
        if step["outcome"] != "passed" or not step["client_validation_passed"]:
            continue
        attempt = step["attempts"][-1]
        body = attempt["body"]
        if not body or not body.get("recorded"):
            raise ValueError(f"step {step['name']} passed but has no recorded body")
        path = (root / body["file"]).resolve()
        if not path.is_relative_to(root):
            raise ValueError(f"step {step['name']} body lies outside the run directory")
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != body["sha256"]:
            raise ValueError(f"step {step['name']} body does not match its SHA-256")
        responses.append(
            ReplayResponse(
                provenance=provenance,
                step=step["name"],
                path=attempt["path"],
                params=attempt["params"],
                status=attempt["http_status"],
                content_type=attempt["headers"].get("content-type"),
                body=content,
            )
        )
    return responses


# --- command line -----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m engine.sig_client.record_fixtures",
        description=(
            "Record a small, bounded, read-only sample of SIG responses for review. "
            "Without --capture this only previews the plan: no credentials, no files, "
            "no requests."
        ),
    )
    parser.add_argument("--tournament-slug", required=True, help="e.g. midterm-elections")
    parser.add_argument("--tournament-id", required=True, help="the tournament's UUID")
    parser.add_argument(
        "--exchange-id",
        action="append",
        required=True,
        metavar="ID",
        help=f"exchange to sample (prices and orderbook); repeat, 1-{MAX_SELECTED_EXCHANGES}",
    )
    parser.add_argument(
        "--market-id",
        action="append",
        required=True,
        metavar="ID",
        help=f"market whose orderbook to sample; repeat, 1-{MAX_SELECTED_MARKETS}",
    )
    parser.add_argument("--markets-status", default="any", choices=sorted(MARKET_STATUSES))
    parser.add_argument(
        "--markets-page-limit", type=int, default=DEFAULT_MARKETS_LIMIT,
        help=f"markets per page, 1-{MARKETS_PAGE_MAX} (default {DEFAULT_MARKETS_LIMIT})",
    )
    parser.add_argument(
        "--markets-max-pages", type=int, default=DEFAULT_PAGES,
        help=f"markets pages, 1-{MAX_PAGES} (default {DEFAULT_PAGES})",
    )
    parser.add_argument("--orders-status", default="open", choices=sorted(ORDER_STATUSES))
    parser.add_argument(
        "--orders-page-limit", type=int, default=DEFAULT_ORDERS_LIMIT,
        help=f"orders per page, 1-{ORDERS_LIMIT_MAX} (default {DEFAULT_ORDERS_LIMIT})",
    )
    parser.add_argument(
        "--orders-max-pages", type=int, default=DEFAULT_PAGES,
        help=f"orders pages, 1-{MAX_PAGES} (default {DEFAULT_PAGES})",
    )
    parser.add_argument("--skip-orders", action="store_true", help="do not read GET /orders")
    parser.add_argument(
        "--depth", type=int, default=DEFAULT_DEPTH,
        help=f"orderbook levels per side, 1-{DEPTH_MAX} (default {DEFAULT_DEPTH})",
    )
    parser.add_argument(
        "--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT,
        help=f"parent of the per-run directory (default {DEFAULT_OUTPUT_ROOT}/)",
    )
    parser.add_argument(
        "--capture",
        action="store_true",
        help=f"actually record (reads {API_KEY_ENV_VAR}; sends GET requests to SIG)",
    )
    return parser


def plan_from_args(args: argparse.Namespace) -> CapturePlan:
    return CapturePlan(
        tournament=TournamentRef(slug=args.tournament_slug, id=args.tournament_id),
        exchange_ids=tuple(args.exchange_id),
        market_ids=tuple(args.market_id),
        output_root=args.output_root,
        markets_status=args.markets_status,
        markets_page_limit=args.markets_page_limit,
        markets_max_pages=args.markets_max_pages,
        include_orders=not args.skip_orders,
        orders_status=args.orders_status,
        orders_page_limit=args.orders_page_limit,
        orders_max_pages=args.orders_max_pages,
        depth=args.depth,
    )


def render_summary(result: RunResult) -> str:
    manifest = json.loads((result.run_dir / MANIFEST_NAME).read_bytes())
    lines = [
        f"SIG capture finished: status={result.status} (exit {result.exit_code})",
        f"Run directory: {result.run_dir.resolve()}",
        (
            f"Provenance:    {manifest['provenance']['kind']} "
            "(not reviewed, not sanitised; see engine/sig_client/RECORDER.md)"
        ),
        "Steps:",
    ]
    for step in manifest["steps"]:
        lines.append(f"  {step['index']:02d} {step['name']:<32} {step['outcome']}")
    for name in manifest["not_run"]:
        lines.append(f"  -- {name:<32} not run")
    for key, summary in manifest["collections"].items():
        state = "complete" if summary["complete"] else "NOT complete"
        note = f": {summary['note']}" if summary["note"] else ""
        lines.append(f"Collection {key}: {summary['pages_fetched']} page(s), {state}{note}")
    if result.stop_reason:
        lines.append(f"Stopped: {result.stop_reason}")
    return "\n".join(lines)


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    base_url: str = SIG_BASE_URL,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    **record_options: Any,
) -> int:
    """CLI entry point. Tests pass `environ`, `transport` and `base_url`; nothing else
    reads the process environment, and only `--capture` reads it at all."""
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        plan = plan_from_args(args)
    except (RecorderArgumentError, ConfigError) as exc:
        parser.error(str(exc))
    if not args.capture:
        print(render_preview(plan, base_url=base_url), file=out)
        return 0
    try:
        config = load_sig_config(
            os.environ if environ is None else environ,
            tournament=plan.tournament,
            base_url=base_url,
        )
    except ConfigError as exc:
        print(f"error: {exc}", file=err)
        return 2
    try:
        result = asyncio.run(record(plan, config, transport=transport, **record_options))
    except KeyboardInterrupt:
        print("interrupted: the run's manifest is marked 'interrupted'", file=err)
        return 130
    except (RecorderOutputError, RecorderArgumentError) as exc:
        print(f"error: {exc}", file=err)
        return 1
    except OSError as exc:
        # Only the class: the message may hold a path or other local detail.
        print(f"error: could not write the capture ({type(exc).__name__})", file=err)
        return 1
    print(render_summary(result), file=out)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
