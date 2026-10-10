"""S1-T3-B-B recorder: opt-in market detail and trade-tape capture.

Every response is synthetic and served through httpx.MockTransport; conftest.py blocks
the real network, so every run here is labelled "synthetic". The "credential" is a
dummy sentinel, never a real key.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from recorder_kit import (
    BUILD,
    MARKET_DETAIL,
    TID,
    TRADES,
    all_bytes,
    capture,
    config,
    fixed_now,
    plan,
    read_manifest,
    sig_router,
    step,
)
from sig_testkit import (
    API,
    BASE_URL,
    SENTINEL_KEY,
    TOURNAMENT,
    FakeClock,
    Router,
    fixture,
    json_response,
    raw_response,
    run,
)

from engine.config import ApiKey
from engine.sig_client import record_fixtures as rf
from engine.sig_client.rate_limiter import RateLimiter, RequestKind
from engine.sig_client.rest import SigRestClient

FROM = datetime(2026, 4, 3, tzinfo=UTC)
TO = datetime(2026, 4, 4, tzinfo=UTC)
FROM_TEXT = "2026-04-03T00:00:00.000Z"
TO_TEXT = "2026-04-04T00:00:00.000Z"
WINDOW = {"tournamentId": TID, "limit": "10", "from": FROM_TEXT, "to": TO_TEXT}
ERROR_TEXT_SENTINEL = "DUMMY_ERROR_TEXT_SENTINEL"  # test placeholder
OMIT = object()

REQUIRED = [
    "--tournament-slug", TOURNAMENT.slug,
    "--tournament-id", TID,
    "--exchange-id", "36",
    "--market-id", "26",
]  # fmt: skip
TRADE_ARGS = ["--include-trades", "--trades-from", FROM_TEXT, "--trades-to", TO_TEXT]


def escaped(text: str) -> str:
    """`text` with its first character written as a JSON \\u escape."""
    return f"\\u{ord(text[0]):04x}{text[1:]}"


ESCAPED_KEY = escaped(SENTINEL_KEY)


def leak_forms() -> list[bytes]:
    return [
        SENTINEL_KEY.encode(),
        ESCAPED_KEY.encode(),
        SENTINEL_KEY.encode("utf-16-le"),
        SENTINEL_KEY.encode("utf-16-be"),
    ]


def trade(trade_id: int, created: str) -> dict[str, Any]:
    return {
        "id": str(trade_id),
        "createdAt": created,
        "price": 0.42,
        "size": 1,
        "side": "YES",
        "volume": 0.42,
    }


def trades_page(
    trades: list[dict[str, Any]],
    next_cursor: str | None = None,
    *,
    coverage: Any = OMIT,
    market_id: str = "26",
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "exchangeId": "36",
        "marketId": market_id,
        "from": FROM_TEXT,
        "to": TO_TEXT,
        "data": trades,
        "pagination": {"limit": 10, "hasMore": next_cursor is not None, "nextCursor": next_cursor},
    }
    if coverage is not OMIT:
        body["coverage"] = coverage
    return body


def three_pages() -> dict[str | None, dict[str, Any]]:
    """A synthetic three-page tape, keyed by the cursor that requests each page."""
    return {
        None: trades_page(
            [trade(30, "2026-04-03T18:00:00.000Z"), trade(29, "2026-04-03T17:00:00.000Z")],
            "t-2",
            coverage={"complete": True, "projectedThroughSequence": 900},
        ),
        "t-2": trades_page([trade(28, "2026-04-03T16:00:00.000Z")], "t-3"),
        "t-3": trades_page(
            [trade(27, "2026-04-03T15:00:00.000Z")],
            coverage={"complete": True, "projectedThroughSequence": None},
        ),
    }


def chained(pages: Mapping[str | None, Any]) -> Callable[[httpx.Request], httpx.Response]:
    """Serves `pages` by cursor; fails the test if the planned window was not sent."""

    def route(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        assert (params["from"], params["to"]) == (FROM_TEXT, TO_TEXT)
        page = pages[params.get("cursor")]
        if isinstance(page, httpx.Response):
            return page
        return json_response(200, page)

    return route


def trades_plan(tmp_path: Path, **overrides: Any) -> rf.CapturePlan:
    values: dict[str, Any] = {"include_trades": True, "trades_from": FROM, "trades_to": TO}
    values.update(overrides)
    return plan(tmp_path, **values)


def sent(router: Router, path: str) -> list[dict[str, str]]:
    return [dict(r.url.params) for r in router.requests if r.url.path == path]


def assert_no_trace(run_dir: Path, step_name: str) -> None:
    manifest = read_manifest(run_dir)
    unsafe = step(manifest, step_name)
    assert unsafe["outcome"] == "credential_detected"
    assert unsafe["client_validation_passed"] is False
    for attempt in unsafe["attempts"]:
        assert attempt["body"] == {
            "recorded": False,
            "omitted": "held the active credential; not written",
        }
    assert manifest["run"]["status"] == "aborted"
    assert manifest["run"]["stop_reason"] == rf.CREDENTIAL_REASON
    assert manifest["credential_detected"] is True
    assert not [p for p in (run_dir / "bodies").iterdir() if step_name in p.name]
    assert not list(run_dir.rglob("*.tmp"))
    written = all_bytes(run_dir)
    for form in leak_forms():
        assert form not in written


# --- defaults stay as they were ---------------------------------------------------------


def test_new_reads_are_off_by_default(tmp_path: Path) -> None:
    default = plan(tmp_path)
    assert default.include_market_detail is False and default.include_trades is False
    assert [r.name for r in rf.planned_reads(default)] == [
        "tournament",
        "tournament-markets",
        "exchange-prices",
        "exchange-orderbook-36",
        "market-orderbook-26",
        "orders",
    ]
    result, manifest, router = capture(tmp_path)
    assert result.status == "succeeded"
    assert "/markets/26" not in router.paths()
    assert not any(path.endswith("/trades") for path in router.paths())
    assert manifest["selection"]["include_market_detail"] is False
    assert manifest["selection"]["include_trades"] is False
    assert manifest["selection"]["trades_from"] is None
    assert set(manifest["collections"]) == {"tournament_markets", "orders"}


def test_cli_defaults_plan_no_new_reads() -> None:
    args = rf.build_parser().parse_args(REQUIRED)
    built = rf.plan_from_args(args)
    assert built.include_market_detail is False and built.include_trades is False
    assert built.trades_from is None and built.trades_to is None


def test_opt_in_reads_come_after_every_default_read(tmp_path: Path) -> None:
    both = trades_plan(tmp_path, include_market_detail=True)
    names = [r.name for r in rf.planned_reads(both)]
    assert names[:6] == [r.name for r in rf.planned_reads(plan(tmp_path))]
    assert names[6:] == ["market-detail-26", "exchange-trades-36"]


# --- market detail ----------------------------------------------------------------------


def test_market_detail_is_read_once_per_selected_market_with_explicit_scope(
    tmp_path: Path,
) -> None:
    exact = json.dumps(fixture("market"), indent=1).encode()
    router = sig_router(
        {MARKET_DETAIL: raw_response(200, exact, {"content-type": "application/json"})}
    )
    result, manifest, router = capture(
        tmp_path, router, capture_plan=plan(tmp_path, include_market_detail=True)
    )
    assert result.status == "succeeded" and result.exit_code == 0
    assert sent(router, MARKET_DETAIL) == [{"tournamentId": TID}]
    detail = step(manifest, "market-detail-26")
    assert detail["route"] == "GET /markets/{id}"
    assert detail["outcome"] == "passed" and detail["client_validation_passed"]
    attempt = detail["attempts"][0]
    assert attempt["params"] == {"tournamentId": TID} and attempt["omitted_params"] == 0
    assert (result.run_dir / attempt["body"]["file"]).read_bytes() == exact
    assert manifest["selection"]["include_market_detail"] is True


def test_market_detail_for_another_tournament_stops_the_run(tmp_path: Path) -> None:
    body = fixture("market")
    body["contexts"][0]["tournament"]["slug"] = "other-tournament"
    router = sig_router({MARKET_DETAIL: json_response(200, body)})
    result, manifest, _ = capture(
        tmp_path, router, capture_plan=trades_plan(tmp_path, include_market_detail=True)
    )
    assert step(manifest, "market-detail-26")["outcome"] == "context_mismatch"
    assert result.status == "aborted"
    assert "exchange-trades-36" in manifest["not_run"]
    assert "/exchanges/36/trades" not in router.paths()


def test_a_market_detail_that_is_not_found_does_not_stop_the_run(tmp_path: Path) -> None:
    router = sig_router(
        {
            MARKET_DETAIL: json_response(
                404, {"error": {"code": "NOT_FOUND", "message": ERROR_TEXT_SENTINEL}}
            ),
            TRADES: chained(three_pages()),
        }
    )
    result, manifest, _ = capture(
        tmp_path, router, capture_plan=trades_plan(tmp_path, include_market_detail=True)
    )
    detail = step(manifest, "market-detail-26")
    assert detail["outcome"] == "http_error"
    assert detail["attempts"][0]["error_body"]["shape"] == "error envelope"
    assert step(manifest, "exchange-trades-36-p1")["outcome"] == "passed"
    assert result.status == "partial" and result.exit_code == 1
    assert ERROR_TEXT_SENTINEL.encode() not in all_bytes(result.run_dir)


# --- the trade tape: window, cursor chain, truncation, coverage -------------------------


def test_trades_follow_the_exact_cursor_chain_over_a_fixed_window(tmp_path: Path) -> None:
    router = sig_router({TRADES: chained(three_pages())})
    result, manifest, router = capture(
        tmp_path, router, capture_plan=trades_plan(tmp_path, trades_max_pages=3)
    )
    assert result.status == "succeeded" and result.exit_code == 0
    assert sent(router, TRADES) == [
        WINDOW,
        {**WINDOW, "cursor": "t-2"},
        {**WINDOW, "cursor": "t-3"},
    ]
    for number, cursor in ((1, None), (2, "t-2"), (3, "t-3")):
        page = step(manifest, f"exchange-trades-36-p{number}")
        assert page["route"] == "GET /exchanges/{id}/trades"
        assert page["outcome"] == "passed"
        expected = WINDOW if cursor is None else {**WINDOW, "cursor": cursor}
        assert page["params"] == expected
        assert page["attempts"][0]["params"] == expected  # from/to are allowlisted
        assert page["attempts"][0]["omitted_params"] == 0
    tape = manifest["collections"]["exchange_trades_36"]
    assert tape["complete"] is True and tape["truncated"] is False
    assert tape["pages_fetched"] == 3 and tape["note"] is None
    assert tape["cursor_chain"] == [None, "t-2", "t-3"]
    assert tape["last_next_cursor"] is None
    assert tape["projection_freshness"] == "unknown"
    assert tape["projection_note"] == rf.PROJECTION_NOTE
    assert manifest["selection"]["trades_from"] == FROM_TEXT
    assert manifest["selection"]["trades_to"] == TO_TEXT


def test_a_page_cap_stop_is_truncated_and_never_complete(tmp_path: Path) -> None:
    router = sig_router({TRADES: chained(three_pages())})
    result, manifest, router = capture(
        tmp_path, router, capture_plan=trades_plan(tmp_path, trades_max_pages=2)
    )
    assert len(sent(router, TRADES)) == 2
    tape = manifest["collections"]["exchange_trades_36"]
    assert tape["complete"] is False and tape["truncated"] is True
    assert "not the full collection" in tape["note"]
    assert tape["cursor_chain"] == [None, "t-2"]
    assert tape["last_next_cursor"] == "t-3"  # the cursor that was not followed
    assert result.status == "succeeded"  # a capped sample by design, labelled as such
    summary = rf.render_summary(result)
    assert "Collection exchange_trades_36: 2 page(s), NOT complete" in summary


def test_one_trades_page_by_default(tmp_path: Path) -> None:
    router = sig_router({TRADES: chained(three_pages())})
    _, manifest, router = capture(tmp_path, router, capture_plan=trades_plan(tmp_path))
    assert len(sent(router, TRADES)) == 1
    tape = manifest["collections"]["exchange_trades_36"]
    assert tape["truncated"] is True and tape["complete"] is False
    assert tape["max_pages"] == 1


def test_coverage_is_kept_per_page_and_never_read_as_freshness(tmp_path: Path) -> None:
    router = sig_router({TRADES: chained(three_pages())})
    result, manifest, _ = capture(
        tmp_path, router, capture_plan=trades_plan(tmp_path, trades_max_pages=3)
    )
    per_page = [
        {"present": True, "complete": True, "projectedThroughSequence": 900},
        {"present": False},
        {"present": True, "complete": True, "projectedThroughSequence": None},
    ]
    assert manifest["collections"]["exchange_trades_36"]["coverage_per_page"] == per_page
    for number, coverage in enumerate(per_page, 1):
        assert step(manifest, f"exchange-trades-36-p{number}")["page"]["coverage"] == coverage
    summary = rf.render_summary(result)
    assert "exchange_trades_36: 3 page(s), complete (pagination only; projection" in summary
    assert "projection freshness unknown" in summary


def test_explicit_null_coverage_still_fails_client_validation(tmp_path: Path) -> None:
    pages = three_pages()
    pages[None] = trades_page([trade(30, "2026-04-03T18:00:00.000Z")], "t-2", coverage=None)
    result, manifest, _ = capture(
        tmp_path, sig_router({TRADES: chained(pages)}), capture_plan=trades_plan(tmp_path)
    )
    first = step(manifest, "exchange-trades-36-p1")
    assert first["outcome"] == "validation_failed"
    assert first["client_validation_passed"] is False
    tape = manifest["collections"]["exchange_trades_36"]
    assert tape["complete"] is False and tape["pages_fetched"] == 0
    assert tape["note"] == "page 1 failed: the collection is incomplete"
    assert tape["cursor_chain"] == [] and tape["coverage_per_page"] == []
    assert result.status == "partial"
    assert "exchange-trades-36-p1" not in {r.step for r in rf.load_replayable(result.run_dir)}


def test_an_echoed_window_other_than_the_planned_one_fails_validation(tmp_path: Path) -> None:
    body = trades_page([trade(30, "2026-04-03T18:00:00.000Z")])
    body["to"] = "2026-04-05T00:00:00.000Z"
    result, manifest, _ = capture(
        tmp_path,
        sig_router({TRADES: json_response(200, body)}),
        capture_plan=trades_plan(tmp_path),
    )
    assert step(manifest, "exchange-trades-36-p1")["outcome"] == "validation_failed"
    assert result.status == "partial"


@pytest.mark.parametrize(
    ("second", "anomaly"),
    [
        (
            trades_page([trade(29, "2026-04-03T16:00:00.000Z")], "t-3"),
            "a trade id appeared more than once in the tape",
        ),
        (
            trades_page([trade(28, "2026-04-03T16:00:00.000Z")], "t-3", market_id="99"),
            "a trades page reports a different market",
        ),
        (
            trades_page([trade(28, "2026-04-03T19:00:00.000Z")], "t-3"),
            "a trades page holds trades newer than the previous page's",
        ),
        (
            trades_page([trade(28, "2026-04-03T16:00:00.000Z")], "t-2"),
            "SIG repeated a pagination cursor",
        ),
    ],
)
def test_inconsistent_pages_end_the_walk_as_an_anomaly(
    second: dict[str, Any], anomaly: str, tmp_path: Path
) -> None:
    pages = three_pages()
    pages["t-2"] = second
    router = sig_router({TRADES: chained(pages)})
    result, manifest, router = capture(
        tmp_path, router, capture_plan=trades_plan(tmp_path, trades_max_pages=3)
    )
    assert len(sent(router, TRADES)) == 2
    tape = manifest["collections"]["exchange_trades_36"]
    assert tape["note"] == anomaly
    assert tape["complete"] is False and tape["truncated"] is False
    assert tape["last_next_cursor"] is None
    assert manifest["anomalies"] == [f"exchange_trades_36 page 2: {anomaly}"]
    assert result.status == "partial" and result.exit_code == 1


def test_every_selected_exchange_gets_its_own_tape(tmp_path: Path) -> None:
    def other(request: httpx.Request) -> httpx.Response:
        body = trades_page([trade(1, "2026-04-03T10:00:00.000Z")], market_id="27")
        body["exchangeId"] = "37"
        return json_response(200, body)

    def book(request: httpx.Request) -> httpx.Response:
        body = fixture("exchange_orderbook")
        body["exchangeId"] = request.url.path.split("/")[-2]
        return json_response(200, body)

    router = sig_router(
        {
            TRADES: chained(three_pages()),
            f"{API}/exchanges/37/trades": other,
            f"{API}/exchanges/37/orderbook": book,
        }
    )
    result, manifest, _ = capture(
        tmp_path, router, capture_plan=trades_plan(tmp_path, exchange_ids=("36", "37"))
    )
    assert set(manifest["collections"]) >= {"exchange_trades_36", "exchange_trades_37"}
    assert manifest["collections"]["exchange_trades_37"]["complete"] is True
    assert step(manifest, "exchange-trades-37-p1")["params"] == WINDOW
    assert result.status == "succeeded"


# --- the window is explicit, fixed and checked ------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"trades_from": FROM}, "a trades window needs include_trades"),
        ({"trades_to": TO}, "a trades window needs include_trades"),
        ({"include_trades": True}, "needs both trades_from and trades_to"),
        ({"include_trades": True, "trades_from": FROM}, "needs both"),
        ({"include_trades": True, "trades_from": FROM, "trades_to": FROM}, "earlier than"),
        ({"include_trades": True, "trades_from": TO, "trades_to": FROM}, "earlier than"),
        (
            {"include_trades": True, "trades_from": FROM.replace(tzinfo=None), "trades_to": TO},
            "timezone-aware",
        ),
        (
            {"include_trades": True, "trades_from": FROM, "trades_to": "2026-04-04"},
            "timezone-aware",
        ),
        (
            {
                "include_trades": True,
                "trades_from": FROM + timedelta(microseconds=1),
                "trades_to": TO,
            },
            "whole number of milliseconds",
        ),
        ({"include_trades": 1}, "must be a bool"),
        ({"include_market_detail": "yes"}, "must be a bool"),
        ({"trades_page_limit": 51}, "trades page limit"),
        ({"trades_page_limit": 0}, "trades page limit"),
        ({"trades_max_pages": 4}, "trades max pages"),
        ({"trades_max_pages": True}, "trades max pages"),
    ],
)
def test_invalid_trade_options_are_rejected(
    overrides: dict[str, Any], message: str, tmp_path: Path
) -> None:
    with pytest.raises(rf.RecorderArgumentError, match=message):
        plan(tmp_path, **overrides)


def test_a_window_in_another_offset_is_normalised_to_utc(tmp_path: Path) -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    shifted = trades_plan(
        tmp_path, trades_from=FROM.astimezone(ist), trades_to=TO.astimezone(ist)
    )
    assert shifted.trades_from == FROM and shifted.trades_from.tzinfo is UTC
    read = rf.planned_reads(shifted)[-1]
    assert read.params == WINDOW
    _, _, router = capture(
        tmp_path, sig_router({TRADES: chained(three_pages())}), capture_plan=shifted
    )
    assert sent(router, TRADES) == [WINDOW]  # exactly what the plan and preview show


def test_a_window_ending_after_the_capture_starts_is_refused_before_anything(
    tmp_path: Path,
) -> None:
    future = trades_plan(tmp_path, trades_to=fixed_now() + timedelta(milliseconds=1))
    router = sig_router()
    with pytest.raises(rf.RecorderArgumentError, match="must not end after"):
        run(
            rf.record(
                future,
                config(),
                run_id="future",
                transport=httpx.MockTransport(router),
                now=fixed_now,
                build=BUILD,
            )
        )
    assert router.requests == []
    assert not (tmp_path / "captures").exists()


def test_a_window_ending_exactly_at_the_capture_start_is_allowed(tmp_path: Path) -> None:
    now_text = "2026-10-09T12:00:00.000Z"

    def empty(request: httpx.Request) -> httpx.Response:
        body = trades_page([])
        body["from"], body["to"] = request.url.params["from"], request.url.params["to"]
        return json_response(200, body)

    edge = trades_plan(tmp_path, trades_to=fixed_now())
    result, _, router = capture(tmp_path, sig_router({TRADES: empty}), capture_plan=edge)
    assert sent(router, TRADES)[0]["to"] == now_text
    assert result.status == "succeeded"


@pytest.mark.parametrize(
    "text",
    [
        "2026-04-03",  # a date alone
        "2026-04-03T00:00:00",  # no offset
        "2026-04-03T00:00:00.000",  # no offset
        "2026-04-03 00:00:00Z",  # not the T separator
        "20260403T000000Z",  # basic format
        "2026-13-03T00:00:00Z",  # no 13th month
        "2026-04-03T00:00:00.0000001Z",  # more than microseconds
        "2026-04-03T00:00:00.000001Z",  # finer than a millisecond
        "DUMMY-SENTINEL-VALUE",
    ],
)
def test_command_line_instants_are_strict_and_never_echoed(text: str) -> None:
    with pytest.raises(rf.RecorderArgumentError) as info:
        rf.parse_instant_arg("--trades-from", text)
    assert text not in str(info.value)


def test_command_line_instants_accept_z_and_offsets() -> None:
    assert rf.parse_instant_arg("x", "2026-04-03T00:00:00Z") == FROM
    assert rf.parse_instant_arg("x", "2026-04-03T05:30:00.000+05:30") == FROM
    assert rf.parse_instant_arg("x", "2026-04-03T00:00:00.5Z") == FROM.replace(
        microsecond=500_000
    )


# --- the request hook pins the window ---------------------------------------------------


def trades_observer(tmp_path: Path) -> rf._Observer:
    guard = rf._CredentialGuard(ApiKey(SENTINEL_KEY))
    observer = rf._Observer(
        config(), rf.planned_reads(trades_plan(tmp_path)), fixed_now, guard
    )
    observer.current = rf._Step(1, "exchange-trades-36-p1", "r", "/exchanges/36/trades", {}, "t")
    return observer


@pytest.mark.parametrize(
    "params",
    [
        [("tournamentId", TID), ("limit", "10"), ("from", FROM_TEXT)],  # no to: "now"
        [("tournamentId", TID), ("limit", "10"), ("to", TO_TEXT)],  # no from
        [("tournamentId", TID), ("limit", "10"), ("from", FROM_TEXT), ("to", FROM_TEXT)],
        [("tournamentId", TID), ("limit", "50"), ("from", FROM_TEXT), ("to", TO_TEXT)],
        [("tournamentId", TID), ("from", FROM_TEXT), ("to", TO_TEXT)],  # SIG's default limit
        [
            ("tournamentId", TID),
            ("limit", "10"),
            ("from", FROM_TEXT),
            ("from", FROM_TEXT),
            ("to", TO_TEXT),
        ],
        [("limit", "10"), ("from", FROM_TEXT), ("to", TO_TEXT)],  # unscoped
    ],
)
def test_the_request_hook_refuses_trades_outside_the_planned_window(
    params: list[tuple[str, str]], tmp_path: Path
) -> None:
    observer = trades_observer(tmp_path)
    request = httpx.Request("GET", f"{BASE_URL}/exchanges/36/trades", params=params)
    with pytest.raises(rf.RecorderSafetyError):
        run(observer.on_request(request))
    assert observer.current is not None and observer.current.attempts == []
    assert observer.refused == 1


def test_the_request_hook_accepts_exactly_the_planned_window(tmp_path: Path) -> None:
    observer = trades_observer(tmp_path)
    request = httpx.Request(
        "GET", f"{BASE_URL}/exchanges/36/trades", params={**WINDOW, "cursor": "t-2"}
    )
    run(observer.on_request(request))
    assert observer.current is not None
    assert observer.current.attempts[0].params == {**WINDOW, "cursor": "t-2"}


def test_new_routes_are_refused_unless_planned(tmp_path: Path) -> None:
    guard = rf._CredentialGuard(ApiKey(SENTINEL_KEY))
    observer = rf._Observer(config(), rf.planned_reads(plan(tmp_path)), fixed_now, guard)
    observer.current = rf._Step(1, "x", "r", "/p", {}, "t")
    for url in (
        f"{BASE_URL}/markets/26?tournamentId={TID}",
        f"{BASE_URL}/exchanges/36/trades?tournamentId={TID}&limit=10",
    ):
        with pytest.raises(rf.RecorderSafetyError):
            run(observer.on_request(httpx.Request("GET", url)))
    assert observer.refused == 2


def test_market_detail_needs_exactly_the_configured_tournament(tmp_path: Path) -> None:
    guard = rf._CredentialGuard(ApiKey(SENTINEL_KEY))
    reads = rf.planned_reads(plan(tmp_path, include_market_detail=True))
    observer = rf._Observer(config(), reads, fixed_now, guard)
    observer.current = rf._Step(1, "market-detail-26", "r", "/markets/26", {}, "t")
    for url in (f"{BASE_URL}/markets/26", f"{BASE_URL}/markets/27?tournamentId={TID}"):
        with pytest.raises(rf.RecorderSafetyError):
            run(observer.on_request(httpx.Request("GET", url)))


# --- unsafe bodies on the new routes ----------------------------------------------------


def splice(body: Mapping[str, Any], members: str) -> bytes:
    """`body` as JSON with `members` (raw JSON) placed before its own fields."""
    return ("{" + members + ", " + json.dumps(body)[1:]).encode()


def json_200(content: bytes) -> httpx.Response:
    return raw_response(200, content, {"content-type": "application/json"})


UNSAFE_MEMBERS = [
    f'"debugEcho": "{SENTINEL_KEY}"',  # literal
    f'"debugEcho": "{ESCAPED_KEY}"',  # \u escape
    f'"{ESCAPED_KEY}": 1',  # member name
    f'"id": "{ESCAPED_KEY}"',  # overwritten by a later duplicate "id"
    f'"nested": [{{"deep": "{ESCAPED_KEY}"}}]',
]


@pytest.mark.parametrize("members", UNSAFE_MEMBERS)
def test_an_unsafe_market_detail_body_leaves_no_trace(
    members: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    router = sig_router(
        {MARKET_DETAIL: json_200(splice(fixture("market"), members)), TRADES: chained({})}
    )
    result, _, router = capture(
        tmp_path, router, capture_plan=trades_plan(tmp_path, include_market_detail=True)
    )
    assert_no_trace(result.run_dir, "market-detail-26")
    assert "/exchanges/36/trades" not in router.paths()  # the run stopped
    assert result.exit_code == 1
    assert SENTINEL_KEY not in caplog.text


@pytest.mark.parametrize("members", [*UNSAFE_MEMBERS, '"marketId": "' + ESCAPED_KEY + '"'])
def test_an_unsafe_trades_body_leaves_no_trace(
    members: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    pages = three_pages()
    pages["t-2"] = json_200(splice(pages["t-2"], members))
    router = sig_router({TRADES: chained(pages)})
    result, manifest, router = capture(
        tmp_path, router, capture_plan=trades_plan(tmp_path, trades_max_pages=3)
    )
    assert_no_trace(result.run_dir, "exchange-trades-36-p2")
    assert len(sent(router, TRADES)) == 2  # page 3 was never requested
    tape = manifest["collections"]["exchange_trades_36"]
    assert tape["complete"] is False
    assert tape["note"] == "page 2 failed and the run stopped: incomplete"
    assert tape["cursor_chain"] == [None]  # only pages that were read
    assert step(manifest, "exchange-trades-36-p1")["attempts"][0]["body"]["recorded"]
    assert SENTINEL_KEY not in caplog.text


def test_a_utf16_trades_body_holding_the_credential_is_caught(tmp_path: Path) -> None:
    body = trades_page([trade(30, "2026-04-03T18:00:00.000Z")])
    body["debugEcho"] = SENTINEL_KEY
    router = sig_router({TRADES: json_200(json.dumps(body).encode("utf-16"))})
    result, _, _ = capture(tmp_path, router, capture_plan=trades_plan(tmp_path))
    assert_no_trace(result.run_dir, "exchange-trades-36-p1")


def test_an_unsafe_trades_cursor_is_never_sent_or_written(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    pages = three_pages()
    pages[None]["pagination"]["nextCursor"] = SENTINEL_KEY
    router = sig_router({TRADES: chained(pages)})
    result, _, router = capture(
        tmp_path, router, capture_plan=trades_plan(tmp_path, trades_max_pages=3)
    )
    assert len(sent(router, TRADES)) == 1
    assert_no_trace(result.run_dir, "exchange-trades-36-p1")
    assert SENTINEL_KEY not in caplog.text


def test_the_trades_cursor_check_stands_on_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Even if the body checks were bypassed, the cursor is checked before it is sent."""
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(rf._Recorder, "_credential_check", lambda self, step: False)
    monkeypatch.setattr(
        rf._Recorder, "_store_bodies", lambda self, step: self._discard_bodies(step, "test")
    )
    pages = three_pages()
    pages[None]["pagination"]["nextCursor"] = SENTINEL_KEY
    router = sig_router({TRADES: chained(pages)})
    result, manifest, router = capture(
        tmp_path, router, capture_plan=trades_plan(tmp_path, trades_max_pages=3)
    )
    assert len(sent(router, TRADES)) == 1
    tape = manifest["collections"]["exchange_trades_36"]
    assert tape["note"] == "the next cursor held the active credential: not sent"
    assert tape["last_next_cursor"] is None
    assert manifest["run"]["stop_reason"] == rf.CREDENTIAL_REASON
    assert result.exit_code == 1
    for form in leak_forms():
        assert form not in all_bytes(result.run_dir)
    assert SENTINEL_KEY not in caplog.text


@pytest.mark.parametrize("target", ["market-detail-26", "exchange-trades-36-p1"])
def test_an_oversized_body_on_a_new_route_is_bounded(target: str, tmp_path: Path) -> None:
    routes: dict[str, Any] = {TRADES: chained(three_pages())}
    if target.startswith("market"):
        market = fixture("market")
        market["padding"] = "x" * 8000
        routes[MARKET_DETAIL] = json_response(200, market)
    else:
        padded = trades_page([trade(30, "2026-04-03T18:00:00.000Z")])
        padded["padding"] = "x" * 8000
        routes[TRADES] = json_response(200, padded)
    router = sig_router(routes)
    clock = FakeClock()
    result = run(
        rf.record(
            trades_plan(tmp_path, include_market_detail=True),
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
    big = step(manifest, target)
    assert big["outcome"] == "body_too_large"
    assert big["client_validation_passed"] is False
    assert big["attempts"][0]["body"] is None
    assert result.status == "partial" and result.exit_code == 1
    assert target not in {r.step for r in rf.load_replayable(result.run_dir)}
    assert not [p for p in (result.run_dir / "bodies").iterdir() if target in p.name]
    if target.startswith("exchange"):
        tape = manifest["collections"]["exchange_trades_36"]
        assert tape["complete"] is False and tape["pages_fetched"] == 0


def test_a_non_json_trades_body_is_not_stored(tmp_path: Path) -> None:
    router = sig_router({TRADES: json_200(b"<html>DUMMY_ERROR_TEXT_SENTINEL</html>")})
    result, manifest, _ = capture(tmp_path, router, capture_plan=trades_plan(tmp_path))
    page = step(manifest, "exchange-trades-36-p1")
    assert page["outcome"] == "validation_failed"
    assert page["attempts"][0]["body"]["recorded"] is False
    assert "not valid JSON" in page["attempts"][0]["body"]["omitted"]
    assert ERROR_TEXT_SENTINEL.encode() not in all_bytes(result.run_dir)


def test_a_trades_error_body_keeps_only_its_shape_and_code(tmp_path: Path) -> None:
    router = sig_router(
        {
            TRADES: json_response(
                400, {"error": {"code": "BAD_REQUEST", "message": ERROR_TEXT_SENTINEL}}
            )
        }
    )
    result, manifest, _ = capture(tmp_path, router, capture_plan=trades_plan(tmp_path))
    page = step(manifest, "exchange-trades-36-p1")
    assert page["outcome"] == "http_error"
    assert page["attempts"][0]["body"] is None
    assert set(page["attempts"][0]["error_body"]) == {"shape", "code", "unrecognized_code"}
    assert ERROR_TEXT_SENTINEL.encode() not in all_bytes(result.run_dir)


# --- 429 and limiter blocks on the new routes -------------------------------------------


def test_a_429_on_a_trades_page_stops_everything(tmp_path: Path) -> None:
    pages: dict[str | None, Any] = three_pages()
    pages["t-2"] = json_response(
        429, fixture("error_envelopes")["RATE_LIMITED"], {"Retry-After": "1"}
    )
    router = sig_router({TRADES: chained(pages)})
    result, manifest, router = capture(
        tmp_path,
        router,
        capture_plan=trades_plan(tmp_path, exchange_ids=("36", "37"), trades_max_pages=3),
    )
    # One 429 and no retry, no page 3 and no second exchange's tape.
    assert len(sent(router, TRADES)) == 2
    assert "/exchanges/37/trades" not in router.paths()
    assert step(manifest, "exchange-trades-36-p2")["outcome"] == "rate_limited"
    assert manifest["refused_after_429"] == 1
    assert result.status == "aborted"
    assert manifest["run"]["stop_reason"] == rf.RATE_LIMITED_REASON
    tape = manifest["collections"]["exchange_trades_36"]
    assert tape["complete"] is False and tape["truncated"] is False
    assert tape["note"] == "page 2 failed and the run stopped: incomplete"
    assert "exchange-trades-37" in manifest["not_run"]


def test_an_oversized_retry_after_on_trades_blocks_and_is_never_cleared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def never(self: RateLimiter, kind: RequestKind) -> None:
        raise AssertionError("the recorder cleared a limiter block")

    monkeypatch.setattr(RateLimiter, "clear_block", never)
    clock = FakeClock()
    limiter = RateLimiter(clock=clock, sleep=clock.sleep)
    pages: dict[str | None, Any] = three_pages()
    pages["t-2"] = json_response(
        429, fixture("error_envelopes")["RATE_LIMITED"], {"Retry-After": "99999999999"}
    )
    result, manifest, router = capture(
        tmp_path,
        sig_router({TRADES: chained(pages)}),
        capture_plan=trades_plan(tmp_path, trades_max_pages=3),
        clock=clock,
        limiter=limiter,
    )
    assert limiter.is_blocked(RequestKind.READ)
    assert step(manifest, "exchange-trades-36-p2")["outcome"] == "blocked"
    assert manifest["run"]["stop_reason"] == rf.BLOCKED_REASON
    assert len(sent(router, TRADES)) == 2
    assert result.status == "aborted"


def test_a_429_on_market_detail_stops_before_any_trades(tmp_path: Path) -> None:
    limited = json_response(429, fixture("error_envelopes")["RATE_LIMITED"], {"Retry-After": "1"})
    router = sig_router({MARKET_DETAIL: limited, TRADES: chained(three_pages())})
    result, manifest, router = capture(
        tmp_path, router, capture_plan=trades_plan(tmp_path, include_market_detail=True)
    )
    assert router.count("/markets/26") == 1
    assert "/exchanges/36/trades" not in router.paths()
    assert step(manifest, "market-detail-26")["outcome"] == "rate_limited"
    assert result.status == "aborted"


# --- replay -----------------------------------------------------------------------------


def test_recorded_market_detail_and_trades_replay_through_the_client(tmp_path: Path) -> None:
    router = sig_router({TRADES: chained(three_pages())})
    both = trades_plan(tmp_path, include_market_detail=True, trades_max_pages=3)
    result, _, _ = capture(tmp_path, router, capture_plan=both)
    replay = {r.step: r for r in rf.load_replayable(result.run_dir)}
    assert {"market-detail-26", *(f"exchange-trades-36-p{n}" for n in (1, 2, 3))} <= set(replay)
    served = {(API + r.path, tuple(sorted(r.params.items()))): r for r in replay.values()}

    def from_capture(request: httpx.Request) -> httpx.Response:
        recorded = served[(request.url.path, tuple(sorted(request.url.params.items())))]
        return raw_response(
            recorded.status, recorded.body, {"content-type": recorded.content_type or ""}
        )

    async def read_all(transport: httpx.MockTransport) -> list[Any]:
        clock = FakeClock()
        async with SigRestClient(
            config(),
            httpx.AsyncClient(transport=transport),
            owns_http=True,
            clock=clock,
            sleep=clock.sleep,
        ) as client:
            return [
                await client.get_market("26"),
                await client.list_all_exchange_trades(
                    "36", since=FROM, until=TO, page_limit=10, max_pages=3
                ),
            ]

    original = run(read_all(httpx.MockTransport(router)))
    replayed = run(read_all(httpx.MockTransport(from_capture)))
    assert replayed == original
    tape = replayed[1]
    assert [t.id for t in tape.trades] == ["30", "29", "28", "27"]
    assert tape.page_coverage[1] is None  # omitted on page 2, as recorded


# --- command line -----------------------------------------------------------------------


def test_preview_shows_the_opt_in_reads_and_the_fixed_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("credentials or an HTTP client were touched")

    monkeypatch.setattr(rf, "load_sig_config", refuse)
    monkeypatch.setattr(rf.httpx, "AsyncClient", refuse)
    monkeypatch.setattr(rf, "record", refuse)
    root = tmp_path / "captures"
    out = io.StringIO()
    args = [
        *REQUIRED,
        "--include-market-detail",
        *TRADE_ARGS,
        "--trades-max-pages", "3",
        "--output-root", str(root),
    ]  # fmt: skip
    assert rf.main(args, environ={}, stdout=out) == 0
    assert not root.exists()
    text = out.getvalue()
    assert "PREVIEW ONLY" in text
    assert f"GET /markets/26?tournamentId={TID}\n" in text
    assert (
        f"GET /exchanges/36/trades?tournamentId={TID}&limit=10&from={FROM_TEXT}&to={TO_TEXT}"
        "  (up to 3 pages"
    ) in text
    assert f"from {FROM_TEXT} (inclusive) to {TO_TEXT} (exclusive)" in text
    assert "projection has caught up" in text
    assert "Logical reads: at most 10." in text  # 6 default + 1 detail + 3 trades pages


def test_preview_marks_a_single_trades_page(tmp_path: Path) -> None:
    out = io.StringIO()
    assert rf.main([*REQUIRED, *TRADE_ARGS], environ={}, stdout=out) == 0
    line = next(x for x in out.getvalue().splitlines() if "/trades?" in x)
    assert line.endswith("(1 page only; a longer collection is recorded as truncated)")


@pytest.mark.parametrize(
    "extra",
    [
        ["--trades-from", FROM_TEXT, "--trades-to", TO_TEXT],  # no --include-trades
        ["--include-trades"],
        ["--include-trades", "--trades-from", FROM_TEXT],
        ["--include-trades", "--trades-from", "DUMMY-SENTINEL-VALUE", "--trades-to", TO_TEXT],
        ["--include-trades", "--trades-from", FROM_TEXT, "--trades-to", "2026-04-04"],
        ["--include-trades", "--trades-from", TO_TEXT, "--trades-to", FROM_TEXT],
        [*TRADE_ARGS, "--trades-page-limit", "51"],
        [*TRADE_ARGS, "--trades-max-pages", "4"],
    ],
)
def test_invalid_trade_arguments_stop_before_anything_runs(
    extra: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("credentials or an HTTP client were touched")

    monkeypatch.setattr(rf, "load_sig_config", refuse)
    monkeypatch.setattr(rf, "record", refuse)
    with pytest.raises(SystemExit) as info:
        rf.main([*REQUIRED, *extra, "--capture"], environ={})
    assert info.value.code == 2
    assert "DUMMY-SENTINEL-VALUE" not in capsys.readouterr().err


def test_capture_with_opt_ins_end_to_end_through_main(tmp_path: Path) -> None:
    router = sig_router({TRADES: chained(three_pages())})
    clock = FakeClock()
    out = io.StringIO()
    args = [
        *REQUIRED,
        "--include-market-detail",
        *TRADE_ARGS,
        "--trades-max-pages", "2",
        "--output-root", str(tmp_path / "c"),
        "--capture",
    ]  # fmt: skip
    code = rf.main(
        args,
        environ={"SUSQ_API_KEY": SENTINEL_KEY},
        transport=httpx.MockTransport(router),
        base_url=BASE_URL,
        stdout=out,
        run_id="main-run",
        build=BUILD,
        clock=clock,
        sleep=clock.sleep,
        now=fixed_now,
    )
    assert code == 0
    text = out.getvalue()
    assert "market-detail-26" in text and "exchange-trades-36-p2" in text
    assert "Collection exchange_trades_36: 2 page(s), NOT complete" in text
    assert SENTINEL_KEY not in text
    manifest = read_manifest(tmp_path / "c" / "main-run")
    assert manifest["provenance"]["kind"] == "synthetic"
    assert manifest["collections"]["exchange_trades_36"]["truncated"] is True


def test_a_future_window_through_main_creates_nothing(tmp_path: Path) -> None:
    router = sig_router()
    err = io.StringIO()
    args = [
        *REQUIRED,
        "--include-trades",
        "--trades-from", FROM_TEXT,
        "--trades-to", "2026-10-09T12:00:00.001Z",
        "--output-root", str(tmp_path / "c"),
        "--capture",
    ]  # fmt: skip
    code = rf.main(
        args,
        environ={"SUSQ_API_KEY": SENTINEL_KEY},
        transport=httpx.MockTransport(router),
        base_url=BASE_URL,
        stderr=err,
        now=fixed_now,
    )
    assert code == 1
    assert "must not end after the capture starts" in err.getvalue()
    assert router.requests == []
    assert not (tmp_path / "c").exists()


def test_interrupting_a_trades_walk_leaves_an_interrupted_manifest(tmp_path: Path) -> None:
    async def hang(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("cursor") == "t-2":
            await asyncio.Event().wait()
        return json_response(200, three_pages()[None])

    async def scenario() -> rf.RunResult:
        clock = FakeClock()
        task = asyncio.create_task(
            rf.record(
                trades_plan(tmp_path, trades_max_pages=3),
                config(),
                run_id="cancel",
                transport=httpx.MockTransport(sig_router({TRADES: hang})),
                clock=clock,
                sleep=clock.sleep,
                now=fixed_now,
                build=BUILD,
            )
        )
        for _ in range(500):
            await asyncio.sleep(0)
            run_dir = tmp_path / "captures" / "cancel"
            if (run_dir / rf.MANIFEST_NAME).exists() and "exchange-trades-36-p1" in (
                run_dir / rf.MANIFEST_NAME
            ).read_text(encoding="utf-8"):
                break
        for _ in range(50):
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return rf.RunResult("interrupted", 130, tmp_path / "captures" / "cancel", None)

    result = asyncio.run(scenario())
    manifest = read_manifest(result.run_dir)
    assert manifest["run"]["status"] == "interrupted"
    assert step(manifest, "exchange-trades-36-p2")["outcome"] == "interrupted"
    assert manifest["collections"]["exchange_trades_36"]["complete"] is False
