"""SIG Super Market API client.

Read-only REST (S1-T2-B) and the realtime market-data feed (S1-T3-A). Realtime token
minting is the only write; there is no order placement or cancellation here.
"""

from engine.sig_client.rate_limiter import RateLimiter, RequestKind, classify_method
from engine.sig_client.realtime import RealtimeFeedPolicy, RealtimeMarketFeed, RealtimeTransport
from engine.sig_client.realtime_token import RealtimeTokenClient
from engine.sig_client.rest import ReadRetryPolicy, SigRestClient

__all__ = [
    "RateLimiter",
    "ReadRetryPolicy",
    "RealtimeFeedPolicy",
    "RealtimeMarketFeed",
    "RealtimeTokenClient",
    "RealtimeTransport",
    "RequestKind",
    "SigRestClient",
    "classify_method",
]
