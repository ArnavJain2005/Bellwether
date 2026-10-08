"""Engine configuration. Secrets are read here and nowhere else.

The SIG API key comes from the `SUSQ_API_KEY` environment variable (the name listed in
`.env.example`). `load_sig_config` takes the environment mapping as an argument rather
than reading `os.environ` itself, so tests pass dummy values and nothing reads the real
key by accident. At runtime the caller passes `os.environ`.

The tournament is always explicit: there is no default, because SIG silently falls back
to an organization's default trading context when `tournamentId` is omitted.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field

import httpx

API_KEY_ENV_VAR = "SUSQ_API_KEY"
SIG_BASE_URL = "https://www.thesuper.market/api/v1"

# Per-attempt HTTP timeouts. The read retry policy adds an overall budget on top.
DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0)

_SLUG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
# Visible ASCII only: anything else could break or inject into the Authorization header.
_API_KEY_PATTERN = re.compile(r"^[\x21-\x7e]+$")


class ConfigError(ValueError):
    """Invalid or missing configuration. Messages never include secret values."""


class ApiKey:
    """An API key that never appears in repr, str, logs or exception text.

    Call `reveal()` only where the key is placed into the request header.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not isinstance(value, str) or not _API_KEY_PATTERN.match(value):
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
        if not _SLUG_PATTERN.match(self.slug):
            raise ConfigError(f"invalid tournament slug {self.slug!r}")
        try:
            parsed = uuid.UUID(self.id)
        except (ValueError, AttributeError, TypeError):
            raise ConfigError(f"tournament id {self.id!r} is not a UUID") from None
        if str(parsed) != self.id:
            raise ConfigError(f"tournament id {self.id!r} is not in canonical lowercase form")


# From CLAUDE.md (Platform facts). Pass it explicitly; nothing uses it by default.
MIDTERM_ELECTIONS = TournamentRef(
    slug="midterm-elections", id="bda92870-621e-47b0-bc3c-3602c5c26f55"
)


def _check_base_url(base_url: str) -> str:
    url = httpx.URL(base_url)
    if url.scheme != "https" or not url.host:
        raise ConfigError("SIG base URL must be an absolute https URL")
    if url.userinfo or url.query or url.fragment:
        raise ConfigError("SIG base URL must not contain credentials, a query or a fragment")
    return base_url.rstrip("/")


@dataclass(frozen=True)
class SigConfig:
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
