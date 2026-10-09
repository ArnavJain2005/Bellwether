"""S1-T4-A-F1 regressions: credential-safe writes, bounded reads, stop after any 429.

Every response is synthetic (httpx.MockTransport); conftest.py blocks the real network.
The "credentials" are dummy sentinels, never real keys.
"""

from __future__ import annotations

import asyncio
import gzip
import io
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from recorder_kit import (
    BUILD,
    EXCHANGE_BOOK,
    MARKET_BOOK,
    MARKETS,
    PRICES,
    TID,
    TOURNAMENT_PATH,
    all_bytes,
    capture,
    config,
    fixed_now,
    plan,
    prices_route,
    read_manifest,
    sig_router,
    step,
)
from sig_testkit import (
    BASE_URL,
    SENTINEL_KEY,
    TOURNAMENT,
    FakeClock,
    Router,
    fixture,
    json_response,
    raw_response,
)

from engine.config import ApiKey
from engine.sig_client import record_fixtures as rf

REQUIRED = [
    "--tournament-slug", TOURNAMENT.slug,
    "--tournament-id", TID,
    "--exchange-id", "36",
    "--market-id", "26",
]  # fmt: skip

# SENTINEL_KEY with its first character as a JSON \u escape: no literal key bytes.
def escaped(key: str) -> str:
    """`key` with its first character written as a JSON \\u escape."""
    return f"\\u{ord(key[0]):04x}{key[1:]}"


ESCAPED_KEY = escaped(SENTINEL_KEY)
assert SENTINEL_KEY.encode() not in ESCAPED_KEY.encode()
assert json.loads(f'"{ESCAPED_KEY}"') == SENTINEL_KEY


def body_with(name: str, extra: str) -> bytes:
    """A synthetic fixture body with `extra` spliced in as raw JSON members."""
    text = json.dumps(fixture(name))
    return ("{" + extra + ", " + text[1:]).encode()


def leak_forms(key: str = SENTINEL_KEY) -> list[bytes]:
    return [key.encode(), escaped(key).encode(), key.encode("utf-16-le")]


def assert_nothing_leaked(run_dir: Path, *texts: str, key: str = SENTINEL_KEY) -> None:
    written = all_bytes(run_dir)
    for form in leak_forms(key):
        assert form not in written
    for text in texts:
        assert key not in text
        assert leak_forms(key)[1].decode() not in text


def json_200(content: bytes) -> httpx.Response:
    return raw_response(200, content, {"content-type": "application/json"})


# --- 1. credential-safe persistence -----------------------------------------------------


def test_a_literal_credential_in_an_unknown_field_is_never_written(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    body = body_with("tournament", f'"debugEcho": "{SENTINEL_KEY}"')
    router = sig_router({TOURNAMENT_PATH: json_200(body)})
    out, err = io.StringIO(), io.StringIO()
    code = rf.main(
        [*REQUIRED, "--output-root", str(tmp_path / "c"), "--capture"],
        environ={"SUSQ_API_KEY": SENTINEL_KEY},
        transport=httpx.MockTransport(router),
        base_url=BASE_URL,
        stdout=out,
        stderr=err,
        run_id="leak",
        build=BUILD,
    )
    run_dir = tmp_path / "c" / "leak"
    manifest = read_manifest(run_dir)
    assert code == 1
    assert manifest["run"]["status"] == "aborted"
    assert manifest["run"]["stop_reason"] == rf.CREDENTIAL_REASON
    assert manifest["credential_detected"] is True
    tournament = step(manifest, "tournament")
    assert tournament["outcome"] == "credential_detected"
    assert tournament["client_validation_passed"] is False
    assert tournament["attempts"][0]["body"] == {
        "recorded": False,
        "omitted": "held the active credential; not written",
    }
    assert router.paths() == ["/tournaments/alpha-tournament"]  # nothing more was sent
    assert list((run_dir / "bodies").iterdir()) == []
    assert_nothing_leaked(run_dir, out.getvalue(), err.getvalue(), caplog.text)
    assert rf.load_replayable(run_dir) == []


@pytest.mark.parametrize(
    ("route", "fixture_name", "step_name", "extra"),
    [
        # JSON-escaped value of an unknown field.
        (EXCHANGE_BOOK, "exchange_orderbook", "exchange-orderbook-36", f'"n": "{ESCAPED_KEY}"'),
        # The credential as a JSON field name, escaped.
        (MARKET_BOOK, "market_orderbook", "market-orderbook-26", f'"{ESCAPED_KEY}": 1'),
        # Escaped solidus, and the key nested deep inside an unknown structure.
        (
            EXCHANGE_BOOK,
            "exchange_orderbook",
            "exchange-orderbook-36",
            f'"x": [{{"y": ["\\/", "{ESCAPED_KEY}"]}}]',
        ),
    ],
)
def test_escaped_credentials_in_values_and_field_names_are_caught(
    route: str,
    fixture_name: str,
    step_name: str,
    extra: str,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level(logging.DEBUG)
    router = sig_router({route: json_200(body_with(fixture_name, extra))})
    result, manifest, _ = capture(tmp_path, router)
    caught = step(manifest, step_name)
    assert caught["outcome"] == "credential_detected"
    assert result.status == "aborted" and result.exit_code == 1
    assert step(manifest, "tournament")["outcome"] == "passed"  # earlier evidence kept
    assert "orders" in manifest["not_run"]
    printed = capsys.readouterr()
    assert_nothing_leaked(result.run_dir, printed.out, printed.err, caplog.text)
    assert caught["name"] not in {r.step for r in rf.load_replayable(result.run_dir)}


def test_a_utf16_body_holding_the_credential_is_caught(tmp_path: Path) -> None:
    document = fixture("tournament")
    document["debugEcho"] = SENTINEL_KEY
    content = json.dumps(document).encode("utf-16-le")
    result, manifest, _ = capture(tmp_path, sig_router({TOURNAMENT_PATH: json_200(content)}))
    assert step(manifest, "tournament")["outcome"] == "credential_detected"
    assert result.exit_code == 1
    assert_nothing_leaked(result.run_dir)


def test_an_unsafe_cursor_is_never_sent_or_written(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    page = json.dumps(fixture("tournament_markets_page1")).replace(
        '"cursor-page-2"', f'"{ESCAPED_KEY}"'
    )
    router = sig_router({MARKETS: json_200(page.encode())})
    two_pages = plan(tmp_path, markets_max_pages=2)
    result, manifest, _ = capture(tmp_path, router, capture_plan=two_pages)
    assert router.count("/tournaments/alpha-tournament/markets") == 1
    assert step(manifest, "tournament-markets-p1")["outcome"] == "credential_detected"
    assert result.status == "aborted"
    assert_nothing_leaked(result.run_dir, caplog.text)


def test_the_cursor_check_stands_on_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Even if the body checks were bypassed, the cursor is checked before it is sent."""
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(rf._Recorder, "_credential_check", lambda self, step: False)
    monkeypatch.setattr(
        rf._Recorder, "_store_bodies", lambda self, step: self._discard_bodies(step, "test")
    )
    page = fixture("tournament_markets_page1")
    page["pagination"]["nextCursor"] = SENTINEL_KEY
    router = sig_router({MARKETS: json_response(200, page)})
    two_pages = plan(tmp_path, markets_max_pages=2)
    result, manifest, _ = capture(tmp_path, router, capture_plan=two_pages)
    assert router.count("/tournaments/alpha-tournament/markets") == 1
    markets = manifest["collections"]["tournament_markets"]
    assert markets["note"] == "the next cursor held the active credential: not sent"
    assert manifest["run"]["stop_reason"] == rf.CREDENTIAL_REASON
    assert result.exit_code == 1
    assert_nothing_leaked(result.run_dir, caplog.text)


def test_the_request_hook_refuses_a_url_holding_the_credential(tmp_path: Path) -> None:
    guard = rf._CredentialGuard(ApiKey(SENTINEL_KEY))
    observer = rf._Observer(config(), rf.planned_reads(plan(tmp_path)), fixed_now, guard)
    observer.current = rf._Step(1, "orders-p2", "r", "/orders", {}, "t")
    request = httpx.Request(
        "GET", f"{BASE_URL}/orders", params={"tournamentId": TID, "cursor": SENTINEL_KEY}
    )
    with pytest.raises(rf.RecorderSafetyError) as info:
        asyncio.run(observer.on_request(request))
    assert SENTINEL_KEY not in str(info.value)
    assert observer.current.attempts == []


def test_a_credential_in_kept_header_metadata_is_withheld(tmp_path: Path) -> None:
    key = "dummy/sentinel-media"  # test placeholder shaped like a media type
    response = raw_response(200, json.dumps(fixture("tournament")).encode(), {"content-type": key})
    result, manifest, _ = capture(tmp_path, sig_router({TOURNAMENT_PATH: response}), api_key=key)
    tournament = step(manifest, "tournament")
    assert tournament["outcome"] == "credential_detected"
    assert tournament["attempts"][0]["headers"] == {"content-type": rf.WITHHELD}
    assert result.exit_code == 1
    assert_nothing_leaked(result.run_dir, key=key)


def test_manifest_strings_matching_the_credential_are_withheld_and_fail_the_run(
    tmp_path: Path,
) -> None:
    # A key equal to text that only the manifest's own wording contains: every entry
    # check passes, so this exercises the last line of defence alone.
    key = "client-written"
    result, manifest, _ = capture(tmp_path, api_key=key)
    assert manifest["withheld_fields"] > 0
    assert rf.WITHHELD in manifest["transformations"]
    assert key.encode() not in all_bytes(result.run_dir)
    assert result.status == "partial" and result.exit_code == 1
    assert "withheld" in manifest["run"]["stop_reason"]


def test_the_guard_never_shows_the_key() -> None:
    guard = rf._CredentialGuard(ApiKey(SENTINEL_KEY))
    assert SENTINEL_KEY not in repr(guard) + str(guard)
    assert not hasattr(guard, "__dict__")
    assert guard.in_body(f'{{"a": "{ESCAPED_KEY}"}}'.encode())
    assert not guard.in_body(b'{"a": "harmless"}')
    assert not guard.in_body(b"not json")


# --- 2. bounded reading -----------------------------------------------------------------


class ChunkStream(httpx.AsyncByteStream):
    """Yields `chunk` up to `count` times, lazily; records reads and closing."""

    def __init__(self, chunk: bytes, count: int, hang_after: int | None = None) -> None:
        self.chunk = chunk
        self.count = count
        self.hang_after = hang_after
        self.yielded = 0
        self.closed = False
        self.started = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for _ in range(self.count):
            if self.hang_after is not None and self.yielded == self.hang_after:
                self.started.set()
                await asyncio.Event().wait()
            self.yielded += 1
            yield self.chunk

    async def aclose(self) -> None:
        self.closed = True


def streamed(status: int, stream: ChunkStream, headers: dict[str, str] | None = None) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers=headers or {}, stream=stream)

    return handler


def test_a_chunked_oversized_body_is_cut_off_closed_and_never_passed(tmp_path: Path) -> None:
    stream = ChunkStream(b"x" * 1000, count=10_000)
    handler = streamed(200, stream, {"content-type": "application/json"})
    router = sig_router({EXCHANGE_BOOK: handler})
    clock = FakeClock()
    result = asyncio.run(
        rf.record(
            plan(tmp_path),
            config(),
            run_id="big",
            transport=httpx.MockTransport(router),
            clock=clock,
            sleep=clock.sleep,
            now=fixed_now,
            build=BUILD,
            max_body_bytes=4096,
        )
    )
    manifest = read_manifest(result.run_dir)
    assert stream.yielded == 5  # 5 000 bytes: stopped at the first chunk over 4 096
    assert stream.closed
    book = step(manifest, "exchange-orderbook-36")
    assert book["outcome"] == "body_too_large"
    assert book["client_validation_passed"] is False
    assert book["attempts"][0]["outcome"] == "body_too_large"
    assert book["attempts"][0]["http_status"] == 200
    assert book["attempts"][0]["body"] is None
    assert router.count("/exchanges/36/orderbook") == 1  # not retried
    assert step(manifest, "orders-p1")["outcome"] == "passed"  # the run carried on
    assert manifest["limits"]["max_body_bytes"] == 4096
    assert result.status == "partial" and result.exit_code == 1
    assert "exchange-orderbook-36" not in {r.step for r in rf.load_replayable(result.run_dir)}
    assert not list(result.run_dir.rglob("*.tmp"))


def run_small(tmp_path: Path, router: Router, run_id: str) -> tuple[rf.RunResult, dict[str, Any]]:
    clock = FakeClock()
    result = asyncio.run(
        rf.record(
            plan(tmp_path),
            config(),
            run_id=run_id,
            transport=httpx.MockTransport(router),
            clock=clock,
            sleep=clock.sleep,
            now=fixed_now,
            build=BUILD,
            max_body_bytes=4096,
        )
    )
    return result, read_manifest(result.run_dir)


def test_the_limit_applies_to_the_decoded_size(tmp_path: Path) -> None:
    bomb = gzip.compress(b"0" * 200_000)
    assert len(bomb) < 4096
    router = sig_router(
        {
            EXCHANGE_BOOK: raw_response(
                200, bomb, {"content-type": "application/json", "content-encoding": "gzip"}
            )
        }
    )
    result, manifest = run_small(tmp_path, router, "gzip")
    assert step(manifest, "exchange-orderbook-36")["outcome"] == "body_too_large"
    assert result.exit_code == 1


def test_an_oversized_error_body_is_bounded_too(tmp_path: Path) -> None:
    stream = ChunkStream(b"e" * 1000, count=10_000)
    result, manifest = run_small(
        tmp_path, sig_router({MARKET_BOOK: streamed(404, stream)}), "error-body"
    )
    assert stream.yielded == 5 and stream.closed
    book = step(manifest, "market-orderbook-26")
    assert book["outcome"] == "body_too_large"
    assert book["attempts"][0]["http_status"] == 404
    assert book["attempts"][0]["error_body"] is None
    assert result.exit_code == 1


def test_a_body_within_the_limit_is_kept_byte_for_byte(tmp_path: Path) -> None:
    content = json.dumps(fixture("exchange_orderbook")).encode()
    stream = ChunkStream(content, count=1)
    result, manifest = run_small(
        tmp_path,
        sig_router({EXCHANGE_BOOK: streamed(200, stream, {"content-type": "application/json"})}),
        "small",
    )
    attempt = step(manifest, "exchange-orderbook-36")["attempts"][0]
    assert (result.run_dir / attempt["body"]["file"]).read_bytes() == content
    assert stream.closed
    assert result.status == "succeeded"


def test_an_interrupted_stream_is_closed(tmp_path: Path) -> None:
    stream = ChunkStream(b"{", count=3, hang_after=1)

    async def scenario() -> None:
        router = sig_router({EXCHANGE_BOOK: streamed(200, stream)})
        clock = FakeClock()
        task = asyncio.create_task(
            rf.record(
                plan(tmp_path),
                config(),
                run_id="hang",
                transport=httpx.MockTransport(router),
                clock=clock,
                sleep=clock.sleep,
                now=fixed_now,
                build=BUILD,
            )
        )
        await stream.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert stream.closed
    manifest = read_manifest(tmp_path / "captures" / "hang")
    assert manifest["run"]["status"] == "interrupted"
    assert step(manifest, "exchange-orderbook-36")["outcome"] == "interrupted"


@pytest.mark.parametrize("limit", [0, rf.MAX_BODY_BYTES + 1, True])
def test_the_body_limit_can_only_be_lowered(limit: Any, tmp_path: Path) -> None:
    with pytest.raises(rf.RecorderArgumentError):
        asyncio.run(
            rf.record(
                plan(tmp_path),
                config(),
                transport=httpx.MockTransport(sig_router()),
                build=BUILD,
                max_body_bytes=limit,
            )
        )
    assert not (tmp_path / "captures").exists()


# --- 3. stop after the first 429 --------------------------------------------------------


def test_no_request_follows_a_429_even_when_the_retry_would_succeed(tmp_path: Path) -> None:
    limited = json_response(429, fixture("error_envelopes")["RATE_LIMITED"], {"Retry-After": "1"})
    ok = prices_route(httpx.Request("GET", f"{BASE_URL}/exchanges/prices?ids=36"))
    router = sig_router({PRICES: [limited, ok]})
    clock = FakeClock()
    result, manifest, _ = capture(tmp_path, router, clock=clock)
    assert router.count("/exchanges/prices") == 1  # the available 200 was never requested
    assert router.paths()[-1] == "/exchanges/prices"
    prices = step(manifest, "exchange-prices")
    assert prices["outcome"] == "rate_limited"
    assert prices["error"]["class"] == "RecorderRateLimitStop"
    assert [a["http_status"] for a in prices["attempts"]] == [429]
    assert manifest["refused_after_429"] == 1
    assert manifest["run"]["stop_reason"] == rf.RATE_LIMITED_REASON
    assert result.status == "aborted" and result.exit_code == 1
    # The client's own handling still ran first: it honoured Retry-After before retrying.
    assert max(clock.sleeps) >= 1.0


def test_cancellation_during_the_client_retry_wait_still_propagates(tmp_path: Path) -> None:
    limited = json_response(429, fixture("error_envelopes")["RATE_LIMITED"], {"Retry-After": "1"})

    class BlockingClock(FakeClock):
        """Fake time that blocks forever in the client's retry wait after the 429."""

        def __init__(self, router: Router) -> None:
            super().__init__()
            self.router = router
            self.waiting = asyncio.Event()

        async def sleep(self, seconds: float) -> None:
            if self.router.count("/exchanges/prices"):
                self.waiting.set()
                await asyncio.Event().wait()
            await super().sleep(seconds)

    async def scenario() -> Router:
        router = sig_router({PRICES: limited})
        clock = BlockingClock(router)
        task = asyncio.create_task(
            rf.record(
                plan(tmp_path),
                config(),
                run_id="wait",
                transport=httpx.MockTransport(router),
                clock=clock,
                sleep=clock.sleep,
                now=fixed_now,
                build=BUILD,
            )
        )
        await clock.waiting.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return router

    router = asyncio.run(scenario())
    assert router.count("/exchanges/prices") == 1
    manifest = read_manifest(tmp_path / "captures" / "wait")
    assert manifest["run"]["status"] == "interrupted"
    assert step(manifest, "exchange-prices")["outcome"] == "interrupted"


def test_preview_and_help_stay_credential_free(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("credential guard built outside capture")

    monkeypatch.setattr(rf, "_CredentialGuard", refuse)
    monkeypatch.setattr(rf, "load_sig_config", refuse)
    assert rf.main([*REQUIRED, "--output-root", "unused"], environ={}) == 0
    with pytest.raises(SystemExit):
        rf.main(["--help"], environ={})
    assert "PREVIEW ONLY" in capsys.readouterr().out


def test_a_real_capture_would_bound_httpx_default_transport() -> None:
    """Without an injected transport, the bound wraps the transport httpx would build."""
    recorder = rf._Recorder(
        rf.CapturePlan(tournament=TOURNAMENT, exchange_ids=("36",), market_ids=("26",)),
        config(),
        run_id="unused",
        provenance="recorded",
        transport=None,
        limiter=rf.RateLimiter(),
        clock=lambda: 0.0,
        sleep=FakeClock().sleep,
        now=fixed_now,
        build=BUILD,
    )
    bounded = recorder._client._http._transport
    assert isinstance(bounded, rf._BoundedTransport)
    assert type(bounded._inner) is httpx.AsyncHTTPTransport
    assert bounded._max_bytes == rf.MAX_BODY_BYTES
    asyncio.run(recorder._client.aclose())  # nothing was sent; conftest blocks the network
