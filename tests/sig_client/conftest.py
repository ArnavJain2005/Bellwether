"""Block real network use for every SIG client test.

Tests use httpx.MockTransport. If any code path falls back to httpx's real transports,
or tries to resolve a host name, the test fails instead of reaching the network.
"""

from __future__ import annotations

import socket
from typing import Any

import httpx
import pytest
from sig_testkit import RealNetworkUsed


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise RealNetworkUsed("a SIG client test tried to use the real network")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
