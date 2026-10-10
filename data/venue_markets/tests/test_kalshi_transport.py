"""Offline transport regressions: real fetch logic, fake HTTPS connections."""

from __future__ import annotations

import contextlib
import http.client
import io
import json
import ssl
import sys
import tempfile
import unittest
import urllib.parse
from collections import deque
from pathlib import Path
from unittest.mock import Mock, patch

from data.venue_markets import fetch_venue_markets as fvm

PRIMARY = "api.elections.kalshi.com"
FALLBACK = "external-api.kalshi.com"
CANARY = b'{"previous": "good output"}\n'


def page(cursor: str = "") -> bytes:
    return json.dumps({"events": [], "cursor": cursor}).encode()


class FakeResponse:
    def __init__(self, connection: FakeConnection, step: dict) -> None:
        self.connection = connection
        self.step = step
        self.status = step.get("status", 200)
        self.will_close = step.get("will_close", False)
        self.consumed = False

    def read(self) -> bytes:
        self.connection.factory.log.append(("read", self.connection.number))
        if self.step.get("failure") == "read":
            raise ConnectionResetError("reset while reading")
        if self.step.get("failure") == "truncated":
            raise http.client.IncompleteRead(b'{"events":', 100)
        self.consumed = True
        return self.step.get("body", page())


class FakeConnection:
    def __init__(self, factory: FakeFactory, host: str, context: ssl.SSLContext, timeout: float) -> None:
        self.factory = factory
        self.host = host
        self.context = context
        self.timeout = timeout
        self.number = len(factory.connections)
        self.closed = False
        self.response: FakeResponse | None = None
        self.step: dict = {}

    def request(self, method: str, path: str, headers: dict) -> None:
        assert not self.closed, "request on discarded connection"
        assert self.response is None or self.response.consumed, "response must be drained before reuse"
        assert method == "GET"
        assert headers["User-Agent"] == fvm.USER_AGENT
        assert headers["Accept"] == "application/json"
        self.factory.request_headers.append(headers)
        assert self.factory.steps, "unexpected extra request/retry"
        self.step = self.factory.steps.popleft()
        assert self.host == self.step.get("host", PRIMARY)
        parsed = urllib.parse.urlparse(path)
        assert parsed.path == "/trade-api/v2/events"
        query = urllib.parse.parse_qs(parsed.query)
        assert query["status"] == ["open"]
        assert query["limit"] == ["200"]
        assert query["with_nested_markets"] == ["true"]
        cursor = query.get("cursor", [""])[0]
        assert cursor == self.step.get("cursor", ""), "retry must retain the exact cursor"
        self.factory.log.append(("request", self.number, self.host, cursor))
        if self.step.get("failure") == "request":
            raise ConnectionResetError("TLS handshake or idle connection reset")

    def getresponse(self) -> FakeResponse:
        self.factory.log.append(("headers", self.number))
        if self.step.get("failure") == "headers":
            raise http.client.RemoteDisconnected("reset before response headers")
        self.response = FakeResponse(self, self.step)
        return self.response

    def close(self) -> None:
        assert not self.closed, "each discarded connection must be closed once"
        self.closed = True
        self.factory.log.append(("close", self.number))


class FakeFactory:
    def __init__(self, steps: list[dict]) -> None:
        self.steps = deque(steps)
        self.connections: list[FakeConnection] = []
        self.log: list[tuple] = []
        self.request_headers: list[dict] = []

    def __call__(self, host: str, port: int, *, context: ssl.SSLContext, timeout: float) -> FakeConnection:
        assert port == 443
        assert context.check_hostname
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert timeout == 25.0
        connection = FakeConnection(self, host, context, timeout)
        self.connections.append(connection)
        self.log.append(("create", connection.number, host))
        return connection


class KalshiTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        for target in ("socket.create_connection", "socket.getaddrinfo", "socket.socket.connect"):
            self.enterContext(patch(target, side_effect=AssertionError("real network access forbidden")))

    def install(self, steps: list[dict]) -> FakeFactory:
        factory = FakeFactory(steps)
        stack = self.enterContext(contextlib.ExitStack())
        stack.enter_context(patch.object(fvm.http.client, "HTTPSConnection", factory))
        self.context_factory = stack.enter_context(patch.object(fvm.ssl, "create_default_context", wraps=ssl.create_default_context))
        stack.enter_context(patch.object(fvm.time, "sleep"))
        self.limiter = Mock(spec=fvm.RateLimiter)
        self.limiter.wait.side_effect = lambda: factory.log.append(("wait",))
        return factory

    def fetch(self, factory: FakeFactory) -> tuple:
        with contextlib.redirect_stdout(io.StringIO()):
            result = fvm.fetch_kalshi_markets(self.limiter)
        self.assertFalse(factory.steps, "all scripted requests must occur")
        self.assertTrue(all(connection.closed for connection in factory.connections))
        requests = [i for i, event in enumerate(factory.log) if event[0] == "request"]
        self.assertEqual(self.limiter.wait.call_count, len(requests))
        for index in requests:
            # Creating a connection may follow wait(), but no request can skip it.
            prior = [event[0] for event in factory.log[:index] if event[0] in {"wait", "request"}]
            self.assertEqual(prior[-1], "wait")
        return result

    def test_multiple_pages_reuse_one_verified_connection(self) -> None:
        factory = self.install([{"body": page("next")}, {"cursor": "next"}])
        host, markets, exclusions = self.fetch(factory)
        self.assertEqual((host, markets, exclusions), (PRIMARY, [], {}))
        self.assertEqual(len(factory.connections), 1)
        self.assertEqual(self.context_factory.call_count, 1)
        self.assertTrue(all("Connection" not in headers for headers in factory.request_headers))

    def test_full_body_is_read_before_next_request(self) -> None:
        factory = self.install([{"body": page("next")}, {"cursor": "next"}])
        self.fetch(factory)
        self.assertEqual([event[0] for event in factory.log], [
            "wait", "create", "request", "headers", "read",
            "wait", "request", "headers", "read", "close",
        ])

    def test_server_close_reconnects_on_next_page(self) -> None:
        factory = self.install([{"body": page("next"), "will_close": True}, {"cursor": "next"}])
        self.fetch(factory)
        self.assertEqual(len(factory.connections), 2)
        self.assertLess(factory.log.index(("close", 0)), factory.log.index(("create", 1, PRIMARY)))
        self.assertEqual(self.context_factory.call_count, 1)

    def failure_on_later_page(self, phase: str) -> None:
        cursor = "opaque+/=cursor"
        factory = self.install([
            {"body": page(cursor)}, {"cursor": cursor, "failure": phase},
            {"host": FALLBACK, "cursor": cursor},
        ])
        host, _markets, _exclusions = self.fetch(factory)
        self.assertEqual(host, FALLBACK)
        self.assertEqual(len(factory.connections), 2)
        self.assertIs(factory.connections[0].context, factory.connections[1].context)
        self.assertLess(factory.log.index(("close", 0)), factory.log.index(("create", 1, FALLBACK)))

    def test_request_failure_discards_connection_and_retains_cursor(self) -> None:
        self.failure_on_later_page("request")

    def test_header_failure_discards_connection_and_retains_cursor(self) -> None:
        self.failure_on_later_page("headers")

    def test_body_failure_discards_connection_and_retains_cursor(self) -> None:
        self.failure_on_later_page("read")

    def test_truncated_body_discards_connection_and_retains_cursor(self) -> None:
        self.failure_on_later_page("truncated")

    def test_fallback_idle_reset_retries_same_page_on_new_connection(self) -> None:
        factory = self.install([
            {"failure": "request"}, {"host": FALLBACK, "body": page("next")},
            {"host": FALLBACK, "cursor": "next", "failure": "request"},
            {"host": FALLBACK, "cursor": "next"},
        ])
        host, _markets, _exclusions = self.fetch(factory)
        self.assertEqual(host, FALLBACK)
        self.assertEqual(len(factory.connections), 3)
        self.assertEqual([event[3] for event in factory.log if event[0] == "request"], ["", "", "next", "next"])

    def test_http_error_body_is_drained_and_connection_reused_for_retry(self) -> None:
        factory = self.install([
            {"body": page("next")}, {"cursor": "next", "status": 503, "body": b"busy"},
            {"cursor": "next"},
        ])
        self.fetch(factory)
        self.assertEqual(len(factory.connections), 1)
        self.assertEqual(self.limiter.wait.call_count, 3)

    def malformed_page(self, body: bytes) -> None:
        factory = self.install([{"body": page("next")}, {"cursor": "next", "body": body}])
        with self.assertRaises((ValueError, fvm.FetchError)), contextlib.redirect_stdout(io.StringIO()):
            fvm.fetch_kalshi_markets(self.limiter)
        self.assertFalse(factory.steps)
        self.assertEqual(len(factory.connections), 1)
        self.assertTrue(factory.connections[0].closed)

    def test_json_failure_closes_owned_transport(self) -> None:
        self.malformed_page(b"{not-json")

    def test_schema_failure_closes_owned_transport(self) -> None:
        self.malformed_page(b'{"events": null, "cursor": ""}')

    def test_cursor_failure_closes_owned_transport(self) -> None:
        self.malformed_page(b'{"events": [], "cursor": null}')

    def test_exhausted_later_page_retries_preserve_latest_in_main(self) -> None:
        factory = self.install([
            {"body": page("next")}, {"cursor": "next", "failure": "request"},
            *[{"host": FALLBACK, "cursor": "next", "failure": "request"} for _ in range(5)],
        ])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "output"
            output.mkdir()
            latest = output / "venue_markets_latest.json"
            latest.write_bytes(CANARY)
            err = io.StringIO()
            with (
                patch.object(fvm, "__file__", str(root / "fetch_venue_markets.py")),
                patch.object(sys, "argv", ["fetch_venue_markets.py"]),
                patch.object(fvm, "RateLimiter", return_value=self.limiter),
                patch.object(fvm, "fetch_polymarket_markets", return_value=([], {})),
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err),
                self.assertRaises(SystemExit) as caught,
            ):
                fvm.main()
            self.assertEqual(caught.exception.code, 1)
            self.assertIn("Exhausted 4 retries", err.getvalue())
            self.assertEqual(latest.read_bytes(), CANARY)
            self.assertEqual(list(output.iterdir()), [latest])
        self.assertFalse(factory.steps)
        self.assertEqual(self.limiter.wait.call_count, 7)
        self.assertEqual(len(factory.connections), 6)
        self.assertTrue(all(connection.closed for connection in factory.connections))

    def test_contexts_are_scoped_to_each_fetch(self) -> None:
        factory = self.install([{}, {}])
        with contextlib.redirect_stdout(io.StringIO()):
            fvm.fetch_kalshi_markets(self.limiter)
            fvm.fetch_kalshi_markets(self.limiter)
        self.assertEqual(len(factory.connections), 2)
        self.assertIsNot(factory.connections[0].context, factory.connections[1].context)
        self.assertTrue(all(connection.closed for connection in factory.connections))

    def test_injected_requester_does_not_create_owned_connections(self) -> None:
        factory = self.install([])
        requester = Mock(return_value=(200, page()))
        with contextlib.redirect_stdout(io.StringIO()):
            fvm.fetch_kalshi_markets(self.limiter, requester=requester)
        requester.assert_called_once()
        self.assertFalse(factory.connections)
        self.context_factory.assert_not_called()

    def test_interrupted_wait_closes_previous_healthy_connection(self) -> None:
        factory = self.install([{"body": page("next")}])
        waits = 0

        def interrupted_wait() -> None:
            nonlocal waits
            waits += 1
            if waits == 2:
                raise KeyboardInterrupt
            factory.log.append(("wait",))

        self.limiter.wait.side_effect = interrupted_wait
        with self.assertRaises(KeyboardInterrupt), contextlib.redirect_stdout(io.StringIO()):
            fvm.fetch_kalshi_markets(self.limiter)
        self.assertFalse(factory.steps)
        self.assertEqual(len(factory.connections), 1)
        self.assertTrue(factory.connections[0].closed)

    def test_one_shot_helper_closes_success_and_does_not_reuse_across_calls(self) -> None:
        factory = self.install([{}, {}])
        path = "/trade-api/v2/events?status=open&limit=200&with_nested_markets=true"
        self.assertEqual(fvm._kalshi_request(PRIMARY, path), (200, page()))
        self.assertTrue(factory.connections[0].closed)
        self.assertEqual(fvm._kalshi_request(PRIMARY, path, timeout=25.0), (200, page()))
        self.assertEqual(len(factory.connections), 2)
        self.assertTrue(factory.connections[1].closed)

    def test_one_shot_helper_propagates_error_without_hidden_retry(self) -> None:
        factory = self.install([{"failure": "request"}])
        path = "/trade-api/v2/events?status=open&limit=200&with_nested_markets=true"
        with self.assertRaises(ConnectionResetError):
            fvm._kalshi_request(PRIMARY, path)
        self.assertFalse(factory.steps)
        self.assertEqual(len(factory.connections), 1)
        self.assertTrue(factory.connections[0].closed)


if __name__ == "__main__":
    unittest.main()
