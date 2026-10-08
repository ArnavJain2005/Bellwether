"""SIG Super Market API client. This slice is read-only REST (S1-T2-B)."""

from engine.sig_client.rate_limiter import RateLimiter, RequestKind, classify_method
from engine.sig_client.rest import ReadRetryPolicy, SigRestClient

__all__ = ["RateLimiter", "ReadRetryPolicy", "RequestKind", "SigRestClient", "classify_method"]
