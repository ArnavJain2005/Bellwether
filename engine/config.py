"""Engine configuration. Secrets are read here and nowhere else.

The SIG API key comes from the `SUSQ_API_KEY` environment variable (the name listed in
`.env.example`). `load_sig_config` takes the environment mapping as an argument rather
than reading `os.environ` itself, so tests pass dummy values and nothing reads the real
key by accident. At runtime the caller passes `os.environ`.

The tournament is always explicit: there is no default, because SIG silently falls back
to an organization's default trading context when `tournamentId` is omitted.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

import httpx

API_KEY_ENV_VAR = "SUSQ_API_KEY"
SIG_BASE_URL = "https://www.thesuper.market/api/v1"

# Per-phase HTTP timeouts for one attempt. They do not bound a whole read; the read
# retry policy's overall deadline does (engine/sig_client/rest.py).
DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0)

# Every pattern is applied with fullmatch(): `$` with match() would accept a trailing "\n".
_SLUG_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
# Visible ASCII only: anything else could break or inject into the Authorization header.
_API_KEY_PATTERN = re.compile(r"[\x21-\x7e]+")
_CANONICAL_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_URL_PATTERN = re.compile(r"[\x21-\x7e]+")


class ConfigError(ValueError):
    """Invalid or missing configuration. Messages never include secret values."""


class ApiKey:
    """An API key that never appears in repr, str, logs or exception text.

    Call `reveal()` only where the key is placed into the request header.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not isinstance(value, str) or not _API_KEY_PATTERN.fullmatch(value):
            raise ConfigError("API key must be non-empty visible ASCII without whitespace")
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "ApiKey(<redacted>)"

    __str__ = __repr__


@dataclass(frozen=True)
class TournamentRef:
    """A tournament's slug and UUID. Both are checked against GET /tournaments/{slug}."""

    slug: str
    id: str

    def __post_init__(self) -> None:
        # Invalid values are not echoed: a misplaced secret must not reach the message.
        if not isinstance(self.slug, str) or not _SLUG_PATTERN.fullmatch(self.slug):
            raise ConfigError("tournament slug must be ASCII letters, digits, '-' or '_'")
        if not isinstance(self.id, str) or not _CANONICAL_UUID.fullmatch(self.id):
            raise ConfigError("tournament id must be a canonical lowercase UUID")


# From CLAUDE.md (Platform facts). Pass it explicitly; nothing uses it by default.
MIDTERM_ELECTIONS = TournamentRef(
    slug="midterm-elections", id="bda92870-621e-47b0-bc3c-3602c5c26f55"
)


def _check_base_url(base_url: str) -> str:
    if not isinstance(base_url, str) or not _URL_PATTERN.fullmatch(base_url):
        raise ConfigError("SIG base URL must be visible ASCII without whitespace")
    url = httpx.URL(base_url)
    if url.scheme != "https" or not url.host:
        raise ConfigError("SIG base URL must be an absolute https URL")
    if url.userinfo or url.query or url.fragment:
        raise ConfigError("SIG base URL must not contain credentials, a query or a fragment")
    return base_url.rstrip("/")


def _check_timeout(timeout: object) -> None:
    """Every httpx phase timeout must be set, finite and positive."""
    if not isinstance(timeout, httpx.Timeout):
        raise ConfigError("timeout must be an httpx.Timeout")
    for phase in ("connect", "read", "write", "pool"):
        value = getattr(timeout, phase)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ConfigError(f"timeout.{phase} must be a finite number of seconds above 0")


@dataclass(frozen=True)
class SigConfig:
    """SIG client settings.

    `timeout` bounds each httpx phase (connect, read, write, pool) of one attempt. It
    does not bound a whole logical read; the client's ReadRetryPolicy deadline does.
    """

    api_key: ApiKey
    tournament: TournamentRef
    base_url: str = SIG_BASE_URL
    timeout: httpx.Timeout = field(default_factory=lambda: DEFAULT_TIMEOUT)

    def __post_init__(self) -> None:
        if not isinstance(self.api_key, ApiKey):
            raise ConfigError("api_key must be an ApiKey")
        if not isinstance(self.tournament, TournamentRef):
            raise ConfigError("tournament must be a TournamentRef")
        object.__setattr__(self, "base_url", _check_base_url(self.base_url))
        _check_timeout(self.timeout)


def load_sig_config(
    environ: Mapping[str, str],
    *,
    tournament: TournamentRef,
    base_url: str = SIG_BASE_URL,
    timeout: httpx.Timeout = DEFAULT_TIMEOUT,
) -> SigConfig:
    """Build the SIG client config. Pass `os.environ` at runtime, a dict in tests."""
    raw = environ.get(API_KEY_ENV_VAR)
    if not raw:
        raise ConfigError(f"{API_KEY_ENV_VAR} is not set")
    return SigConfig(api_key=ApiKey(raw), tournament=tournament, base_url=base_url, timeout=timeout)
