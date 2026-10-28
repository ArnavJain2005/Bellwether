"""Errors raised by the SIG client.

Every message and attribute is built from trusted pieces only:
- the HTTP method and the request path, which the client builds from configuration and
  validated arguments (no query string, no headers);
- the HTTP status and numeric Retry-After;
- SIG's error `code`, but only when it is one of the documented codes below;
- fixed, client-written descriptions.

Nothing taken from a response body is copied into an error or a log line: not SIG's
free-form `message`, not unknown codes, not echoed tournament ids or slugs, not item ids.
Any of those could contain a credential or other text we must not re-emit. Request
objects, headers and response bodies are never attached to these exceptions.
"""

from __future__ import annotations

# The codes in the "Error Responses" table of #/info/description, plus
# TERMS_NOT_ACKNOWLEDGED from the order endpoints' 403 schema.
KNOWN_ERROR_CODES = frozenset(
    {
        "MISSING_API_KEY",
        "INVALID_API_KEY",
        "API_KEY_REVOKED",
        "API_KEY_EXPIRED",
        "UNAUTHORIZED",
        "INSUFFICIENT_SCOPES",
        "ACCOUNT_BANNED",
        "RESIDENCE_UPDATE_REQUIRED",
        "ADMIN_REQUIRED",
        "FORBIDDEN",
        "RATE_LIMITED",
        "INVALID_JSON",
        "INVALID_ID",
        "INVALID_CURSOR",
        "VALIDATION_ERROR",
        "NOT_FOUND",
        "CONFLICT",
        "REQUEST_IN_FLIGHT",
        "RELATIONSHIP_VIOLATION",
        "DATABASE_ERROR",
        "INTERNAL_ERROR",
        "ORDER_STATUS_UNKNOWN",
        "TX_CONFLICT",
        "SERVICE_UNAVAILABLE",
        "TERMS_NOT_ACKNOWLEDGED",
    }
)


def known_code(code: object) -> str | None:
    """SIG's error code if it is a documented one, else None (never echoed)."""
    if isinstance(code, str) and code in KNOWN_ERROR_CODES:
        return code
    return None


class SigClientError(Exception):
    """Base class for every SIG client error."""


class SigArgumentError(SigClientError, ValueError):
    """A client method was called with arguments outside the documented limits."""


class SigClientClosedError(SigClientError):
    """The client was used after `aclose()`."""


class SigApiError(SigClientError):
    """SIG answered with a non-success HTTP status.

    `code` is a documented SIG code or None. `unrecognized_code` is True when the body
    carried some other code; its value is deliberately not kept. `detail` is a fixed
    client-written description of the body's shape, never SIG's text.

    `retry_after` is the server's numeric Retry-After in seconds, never rounded down, or
    None. `retry_after_oversized` is True when the header was a valid number too large
    to hold safely (above MAX_COOLDOWN_SECONDS); `retry_after` is then None.
    """

    def __init__(
        self,
        *,
        method: str,
        path: str,
        status: int,
        code: str | None,
        unrecognized_code: bool,
        detail: str,
        retry_after: float | None = None,
        retry_after_oversized: bool = False,
    ) -> None:
        self.method = method
        self.path = path
        self.status = status
        self.code = code
        self.unrecognized_code = unrecognized_code
        self.detail = detail
        self.retry_after = retry_after
        self.retry_after_oversized = retry_after_oversized
        shown = code or ("<unrecognized code>" if unrecognized_code else "-")
        super().__init__(f"{method} {path} -> HTTP {status} {shown}: {detail}")


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
    """The request failed below HTTP (connect, timeout, protocol).

    Only the httpx exception's class name is kept, never its text.
    """

    def __init__(self, *, method: str, path: str, kind: str) -> None:
        self.method = method
        self.path = path
        self.kind = kind
        super().__init__(f"{method} {path} -> transport error {kind}")


class SigRetryExhaustedError(SigClientError):
    """A read kept failing transiently until the attempt limit was reached."""

    def __init__(self, *, attempts: int, last_error: SigApiError | SigTransportError) -> None:
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(f"attempt limit reached after {attempts} attempt(s); last: {last_error}")


class SigDeadlineExceededError(SigClientError):
    """A logical read's overall deadline passed, or would pass before the next step.

    `phase` is where it ran out: "caller admission" (a caller's own per-attempt gate,
    such as the lifecycle sweep's), "rate admission", "request" (sending, waiting for and
    reading the response) or "retry wait". `last_error` is the previous attempt's
    failure, if any. No response that arrives after the deadline is returned.
    """

    def __init__(
        self,
        *,
        method: str,
        path: str,
        phase: str,
        attempts: int,
        last_error: SigApiError | SigTransportError | None = None,
    ) -> None:
        self.method = method
        self.path = path
        self.phase = phase
        self.attempts = attempts
        self.last_error = last_error
        text = (
            f"{method} {path} -> read deadline exceeded during {phase} after {attempts} attempt(s)"
        )
        if last_error is not None:
            text += f"; last: {last_error}"
        super().__init__(text)


class SigRateBudgetError(SigClientError):
    """RateLimiter.acquire: the deadline has passed, or the wait would end after it."""


class SigResponseError(SigClientError):
    """A success response that is not valid JSON or does not match the documented shape.

    `reason` is client-written and names fields, never their values.
    """

    def __init__(self, *, method: str, path: str, reason: str) -> None:
        self.method = method
        self.path = path
        self.reason = reason
        super().__init__(f"{method} {path} -> malformed response: {reason}")


class SigContextMismatchError(SigResponseError):
    """SIG reported a different tournament than the one configured."""


class SigPaginationError(SigResponseError):
    """Pagination could not be completed; no partial collection is returned."""
