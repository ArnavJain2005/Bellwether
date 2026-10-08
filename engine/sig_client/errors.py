"""Errors raised by the SIG client.

Every message is built from sanitized pieces: the HTTP method, the request path (no
query string, no headers), the status, SIG's stable error `code`, and a truncated copy of
SIG's free-form `message` with the API key redacted. Request objects, headers and
response bodies are never attached to these exceptions.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

_MAX_TEXT = 200
_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def sanitize_text(text: str, secrets: Iterable[str] = ()) -> str:
    """Redact secrets, drop control characters and truncate free-form text."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<redacted>")
    text = _CONTROL.sub(" ", text)
    if len(text) > _MAX_TEXT:
        text = text[:_MAX_TEXT] + "..."
    return text


def sanitize_code(code: object) -> str | None:
    """Keep SIG's error code only if it looks like one (e.g. RATE_LIMITED)."""
    if isinstance(code, str) and _CODE_PATTERN.match(code):
        return code
    return None


class SigClientError(Exception):
    """Base class for every SIG client error."""


class SigArgumentError(SigClientError, ValueError):
    """A client method was called with arguments outside the documented limits."""


class SigClientClosedError(SigClientError):
    """The client was used after `aclose()`."""


class SigApiError(SigClientError):
    """SIG answered with a non-success HTTP status."""

    def __init__(
        self,
        *,
        method: str,
        path: str,
        status: int,
        code: str | None,
        message: str,
        retry_after: float | None = None,
    ) -> None:
        self.method = method
        self.path = path
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after
        super().__init__(f"{method} {path} -> HTTP {status} {code or '-'}: {message}")


class SigBadRequestError(SigApiError):
    """400: validation, bad id or bad cursor. Never retried."""


class SigAuthError(SigApiError):
    """401 or 403: key, scope, membership or account problem. Never retried."""


class SigNotFoundError(SigApiError):
    """404. Never retried."""


class SigConflictError(SigApiError):
    """409. Never retried by the read client."""


class SigRateLimitedError(SigApiError):
    """429 RATE_LIMITED."""


class SigServiceUnavailableError(SigApiError):
    """503: TX_CONFLICT or SERVICE_UNAVAILABLE."""


class SigServerError(SigApiError):
    """Any other 5xx. Not documented as transient for reads, so never retried."""


class SigUnexpectedStatusError(SigApiError):
    """Any other status, including redirects, which are never followed."""


class SigTransportError(SigClientError):
    """The request failed below HTTP (connect, timeout, protocol)."""

    def __init__(self, *, method: str, path: str, kind: str) -> None:
        self.method = method
        self.path = path
        self.kind = kind
        super().__init__(f"{method} {path} -> transport error {kind}")


class SigRetryExhaustedError(SigClientError):
    """A read failed transiently and could not be retried within the policy."""

    def __init__(
        self, *, reason: str, attempts: int, last_error: SigApiError | SigTransportError
    ) -> None:
        self.reason = reason
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(f"{reason} after {attempts} attempt(s); last error: {last_error}")


class SigRateBudgetError(SigClientError):
    """Waiting for local rate budget would exceed the caller's deadline."""


class SigResponseError(SigClientError):
    """A success response that is not valid JSON or does not match the documented shape."""

    def __init__(self, *, method: str, path: str, reason: str) -> None:
        self.method = method
        self.path = path
        self.reason = reason
        super().__init__(f"{method} {path} -> malformed response: {reason}")


class SigContextMismatchError(SigResponseError):
    """SIG reported a different tournament than the one configured."""


class SigPaginationError(SigResponseError):
    """Pagination could not be completed; no partial collection is returned."""
