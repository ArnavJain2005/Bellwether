"""The loop against a fake SIG server and fake Kalshi, on one fake clock. No network."""

import asyncio
import json
from decimal import Decimal

import httpx

from bot import db
from bot.main import Bot
from bot.tests.kit import make_limits
from bot.tests.loopkit import make_loop

D = Decimal
EDGE = (D("0.30"), D("0.40"))  # ask 0.40 <= fair 0.48 - edge 0.06 -> buy YES @ 0.40


def run(coro):
    return asyncio.run(coro)


async def loops(rig, n, seconds=10.0):
    for _ in range(n):
        assert await rig.bot.iteration()
        await rig.time.sleep(seconds)


def with_edges(rig, count):
    ids = list(rig.bot.contracts)[:count]
    for exchange_id in ids:
        rig.sig.quotes[exchange_id] = EDGE
    return ids


# --- dry run ----------------------------------------------------------------------------


def test_dry_run_sends_nothing(tmp_path):
    rig = make_loop(tmp_path, races=3)
    with_edges(rig, 6)

    async def go():
        await rig.bot.startup()
        await loops(rig, 12)
        (rig.state_dir / "STOP").write_text("x")
        assert not await rig.bot.iteration()

    run(go())
    assert rig.sig.writes() == []  # no order, no cancel, ever
    would = rig.decisions("would_order")
    assert would and all("buy YES" in r["reason"] for r in would)
    assert would[0]["sig_ask"] == "0.40" and would[0]["party"] in ("D", "R")
    assert rig.orders() == []
    assert rig.heartbeat()["mode"] == "dry_run"


def test_live_flag_without_live_mode_is_a_dry_run(tmp_path):
    rig = make_loop(tmp_path, limits=make_limits(mode="dry_run"), live=True)
    assert not rig.bot.live and rig.bot.mode == "dry_run"


def test_at_most_five_orders_per_loop(tmp_path):
    rig = make_loop(tmp_path, races=5)
    with_edges(rig, 10)

    async def go():
        await rig.bot.startup()
        await rig.bot.iteration()

    run(go())
    assert len(rig.decisions("would_order")) == 5
    assert any("5 orders this loop" in r["reason"] for r in rig.decisions("skip"))


def test_skip_rows_at_most_once_per_contract_per_minute(tmp_path):
    rig = make_loop(tmp_path, races=1)

    async def go():
        await rig.bot.startup()
        await loops(rig, 6)  # 60 s

    run(go())
    per_subject = {}
    for row in rig.decisions("skip"):
        key = (row["strategy"], row["sig_exchange_id"])
        per_subject[key] = per_subject.get(key, 0) + 1
    assert per_subject and all(n == 1 for n in per_subject.values())
    rows = rig.decisions("skip")
    assert all(r["reason"] for r in rows)


# --- live orders ------------------------------------------------------------------------


def test_live_reference_order_reaches_sig_with_context(tmp_path):
    rig = make_loop(tmp_path, live=True)
    (exchange_id,) = with_edges(rig, 1)

    async def go():
        await rig.bot.startup()
        await rig.bot.iteration()

    run(go())
    posts = [s for s in rig.sig.writes() if s.path == "/orders"]
    assert len(posts) == 1
    body = json.loads(posts[0].content, parse_float=Decimal)
    assert (body["exchangeId"], body["side"], body["price"]) == (exchange_id, "yes", D("0.4"))
    # per_order 50 at scale 0.10 -> floor(50 / 0.40) = 125 shares
    assert body["quantity"] == 125
    order = rig.decisions("order")[0]
    assert order["sig_ask"] == "0.40" and order["fair"] == "0.48" and order["party"] == "D"
    assert "-> buy YES 125 @ 0.400" in order["reason"]
    assert rig.orders()[0]["status"] == "resting"


def test_depth_caps_the_size(tmp_path):
    rig = make_loop(tmp_path, live=True)
    (exchange_id,) = with_edges(rig, 1)
    rig.sig.books[exchange_id] = ([], [(D("0.40"), D("7.5")), (D("0.41"), D(500))])

    async def go():
        await rig.bot.startup()
        await rig.bot.iteration()

    run(go())
    assert rig.orders()[0]["quantity"] == 7


def test_complement_checks_both_legs_before_sending_either(tmp_path):
    # per_race 5 x 0.10 = 0.50: each leg alone fits (0.40, 0.50), both together do not.
    rig = make_loop(tmp_path, races=1, live=True, limits=make_limits(mode="live", per_race=5, start_balance=100000))
    d, r = list(rig.bot.contracts)
    rig.sig.quotes[d] = (D("0.35"), D("0.40"))
    rig.sig.quotes[r] = (D("0.45"), D("0.50"))
    rig.kalshi.quotes = {"K0-D": ("0.39", "0.41"), "K0-R": ("0.49", "0.51")}  # no ref edge

    async def go():
        await rig.bot.startup()
        await rig.bot.iteration()

    run(go())
    assert [s for s in rig.sig.writes() if s.path == "/orders"] == []
    skips = [x for x in rig.decisions("skip") if x["strategy"] == "complement"]
    assert any("risk refuses every leg together" in x["reason"] for x in skips)


def test_complement_sends_both_legs_with_equal_quantity(tmp_path):
    rig = make_loop(tmp_path, races=1, live=True)
    d, r = list(rig.bot.contracts)
    rig.sig.quotes[d] = (D("0.35"), D("0.40"))
    rig.sig.quotes[r] = (D("0.45"), D("0.50"))
    rig.kalshi.quotes = {"K0-D": ("0.39", "0.41"), "K0-R": ("0.49", "0.51")}

    async def go():
        await rig.bot.startup()
        await rig.bot.iteration()

    run(go())
    orders = rig.orders()
    assert [o["sig_exchange_id"] for o in orders] == [d, r]
    assert orders[0]["quantity"] == orders[1]["quantity"] > 0
    assert all(o["strategy"] == "complement" for o in orders)


def test_complement_second_leg_refused_is_not_chased(tmp_path):
    rig = make_loop(tmp_path, races=1, live=True)
    d, r = list(rig.bot.contracts)
    rig.sig.quotes[d] = (D("0.35"), D("0.40"))
    rig.sig.quotes[r] = (D("0.45"), D("0.50"))
    rig.kalshi.quotes = {"K0-D": ("0.39", "0.41"), "K0-R": ("0.49", "0.51")}
    calls = []
    default = rig.sig.resting

    def reply(body):
        calls.append(body["exchangeId"])
        if len(calls) == 2:
            return httpx.Response(400, json={"error": {"code": "VALIDATION_ERROR", "message": "x"}})
        return default(body)

    rig.sig.order_reply = reply

    async def go():
        await rig.bot.startup()
        await loops(rig, 4)

    run(go())
    assert calls == [d, r]  # no retry of the refused leg, no chase on later loops
    assert any("not chasing" in e["message"] for e in rig.events("order_rejected"))


# --- STOP and signals -------------------------------------------------------------------


def test_stop_file_cancels_all_then_exits(tmp_path):
    rig = make_loop(tmp_path, live=True)
    with_edges(rig, 1)

    async def go():
        await rig.bot.startup()
        await rig.bot.iteration()
        (rig.state_dir / "STOP").write_text("x")
        return await rig.bot.iteration()

    assert run(go()) is False
    last = rig.sig.writes()[-1]
    assert (last.method, last.path) == ("POST", "/orders/cancel-all")
    assert json.loads(last.content) == {"tournamentId": "bda92870-621e-47b0-bc3c-3602c5c26f55"}
    assert rig.heartbeat()["stop_seen"] == 1
    assert any("stop file" in e["message"] for e in rig.events("stop"))


def test_run_returns_zero_on_stop(tmp_path):
    rig = make_loop(tmp_path)
    (rig.state_dir / "STOP").write_text("x")
    assert run(rig.bot.run()) == 0


def test_signal_cancels_all_then_exits(tmp_path):
    rig = make_loop(tmp_path, live=True)

    async def go():
        await rig.bot.startup()
        rig.bot.stopping.set()
        return await rig.bot.iteration()

    assert run(go()) is False
    assert rig.sig.writes()[-1].path == "/orders/cancel-all"
    assert any("signal" in e["message"] for e in rig.events("stop"))


def test_live_startup_cancels_all(tmp_path):
    rig = make_loop(tmp_path, live=True)
    run(rig.bot.startup())
    assert [s.path for s in rig.sig.writes()] == ["/orders/cancel-all"]
    assert rig.events("start")


def test_live_startup_requires_loss_stop_baseline_before_any_sig_call(tmp_path):
    rig = make_loop(tmp_path, live=True, limits=make_limits(mode="live", start_balance=None))
    assert run(rig.bot.run()) == 2
    assert rig.sig.seen == []
    assert rig.orders() == []
    assert "live mode requires start_balance" in rig.events("stop")[0]["message"]


def test_dry_run_allows_unset_loss_stop_baseline(tmp_path):
    rig = make_loop(tmp_path, live=False, limits=make_limits(mode="live", start_balance=None))
    run(rig.bot.startup())
    assert rig.sig.writes() == []
    assert "loss stop is disabled" in rig.events("start")[0]["message"]


def test_empty_map_refuses_to_start(tmp_path):
    rig = make_loop(tmp_path)
    rig.bot.contracts.clear()
    assert run(rig.bot.run()) == 2
    assert rig.sig.seen == []
    assert "no approved contracts" in rig.events("stop")[0]["message"]


def test_startup_marks_an_interrupted_send_unknown(tmp_path):
    rig = make_loop(tmp_path)
    db.insert_order(
        rig.conn,
        {
            "key": "k1",
            "created_utc": "2026-11-03T23:00:00Z",
            "updated_utc": "2026-11-03T23:00:00Z",
            "race_key": "senate:S0",
            "sig_market_id": "500",
            "sig_exchange_id": "1000",
            "side": "yes",
            "action": "buy",
            "price_side": "0.40",
            "wire_price": "0.4",
            "quantity": 1,
            "expires_utc": "2026-11-03T23:02:00Z",
            "strategy": "reference",
            "status": "sent",
        },
    )
    run(rig.bot.startup())
    assert rig.orders()[0]["status"] == "unknown"
    assert rig.paused


# --- reconcile, stale orders, markets ---------------------------------------------------


def test_unexplained_open_order_pauses_with_reconcile(tmp_path):
    rig = make_loop(tmp_path, live=True)

    async def go():
        await rig.bot.startup()
        rig.sig.open_orders.append(
            {
                "id": 99,
                "exchangeId": "1000",
                "side": "yes",
                "action": "buy",
                "quantity": 10,
                "priceLimit": D("0.30"),
                "open": True,
                "createdAt": "2026-11-03T23:00:00.000Z",
                "expirationDate": None,
            }
        )
        await loops(rig, 7)  # past the 60 s account cycle

    run(go())
    assert rig.paused
    assert "reconcile" in (rig.state_dir / "PAUSED").read_text()
    assert rig.events("reconcile")[-1]["level"] == "error"
    assert D(rig.heartbeat()["committed"]) == D(3)  # the foreign order counts at its limit


def test_foreign_open_order_in_dry_run_warns_without_pausing(tmp_path):
    rig = make_loop(tmp_path)
    rig.sig.open_orders.append(
        {
            "id": 99,
            "exchangeId": "1000",
            "side": "no",
            "action": "buy",
            "quantity": 10,
            "priceLimit": D("0.30"),
            "open": True,
            "createdAt": "2026-11-03T23:00:00.000Z",
            "expirationDate": None,
        }
    )
    run(rig.bot.startup())
    assert not rig.paused
    assert rig.bot.external_committed == D(7)  # a NO order counts at max(p, 1 - p)


def test_stale_own_order_is_cancelled(tmp_path):
    rig = make_loop(tmp_path, live=True)
    with_edges(rig, 1)

    async def go():
        await rig.bot.startup()
        await rig.bot.iteration()  # places order 7001, which SIG keeps open
        rig.sig.quotes.clear()  # no new edge
        await loops(rig, 20)  # the 180 s account cycle sees it older than 120 + 30 s

    run(go())
    deletes = [s for s in rig.sig.writes() if s.method == "DELETE"]
    assert [s.path for s in deletes] == ["/orders/7001"]
    assert rig.orders()[0]["status"] in ("cancelled", "expired")
    assert not rig.paused


def test_closed_market_is_dropped(tmp_path):
    rig = make_loop(tmp_path, races=2)
    closed = rig.bot.contracts["1001"]
    rig.sig.market_status[closed.sig_market_id] = "closed"

    async def go():
        await rig.bot.startup()
        await loops(rig, 2)

    run(go())
    assert "1001" not in rig.bot.contracts
    assert "is closed: dropped" in rig.events("market_closed")[0]["message"]
    last_prices = [s for s in rig.sig.reads() if s.path == "/exchanges/prices"][-1]
    assert "1001" not in last_prices.query["ids"][0].split(",")


# --- failures ---------------------------------------------------------------------------


def test_exception_pauses_while_heartbeat_continues(tmp_path):
    rig = make_loop(tmp_path)

    async def boom(*args, **kwargs):
        raise RuntimeError("venue exploded")

    async def go():
        await rig.bot.startup()
        rig.bot.refs.fetch_all = boom
        assert await rig.bot.iteration()
        first = rig.heartbeat()["ts_utc"]
        await rig.time.sleep(10)
        assert await rig.bot.iteration()
        return first

    first = run(go())
    assert rig.paused
    errors = [e for e in rig.events("pause") if e["level"] == "error"]
    assert errors and "RuntimeError" in errors[0]["message"]
    assert rig.heartbeat()["ts_utc"] > first
    assert rig.heartbeat()["paused"] == 1
    assert rig.sig.writes() == []


def test_paused_bot_spends_no_reads_on_orders(tmp_path):
    rig = make_loop(tmp_path, live=True)
    with_edges(rig, 2)

    async def go():
        await rig.bot.startup()
        (rig.state_dir / "PAUSED").write_text("manual")
        await loops(rig, 3)

    run(go())
    assert not [s for s in rig.sig.reads() if s.path.endswith("/orderbook")]
    assert [s.path for s in rig.sig.writes()] == ["/orders/cancel-all"]


def test_sig_price_failure_skips_trading_but_keeps_the_heartbeat(tmp_path):
    rig = make_loop(tmp_path)
    with_edges(rig, 1)
    default = rig.sig.handler

    def failing(request):
        if request.url.path.endswith("/exchanges/prices"):
            return httpx.Response(403, json={"error": {"code": "FORBIDDEN", "message": "x"}})
        return default(request)

    rig.bot.rest._http._transport = httpx.MockTransport(failing)

    async def go():
        await rig.bot.startup()
        await loops(rig, 2)

    run(go())
    assert rig.decisions("would_order") == []
    assert rig.events("ref_stale")
    assert not rig.paused
    assert rig.heartbeat() is not None


# --- read budget ------------------------------------------------------------------------


def test_reads_stay_within_80_per_minute_with_120_exchanges(tmp_path):
    rig = make_loop(tmp_path, races=60)
    with_edges(rig, 120)  # every exchange has an edge: maximum orderbook reads

    async def go():
        await rig.bot.startup()
        start = rig.time.t
        iterations = 0
        while rig.time.t - start < 300:
            await rig.bot.iteration()
            iterations += 1
            await rig.time.sleep(10)
        return start, iterations

    start, iterations = run(go())
    reads = [s.at for s in rig.sig.reads()]
    assert len([s for s in rig.sig.reads() if s.path.endswith("/orderbook")]) > 50
    for t in reads:
        window = [x for x in reads if t <= x < t + 60]
        assert len(window) <= 80, f"{len(window)} reads in the minute from {t - start:.0f}s"
    assert iterations >= 29  # the loop kept its pace; the limiter never had to stall it
    assert rig.sig.writes() == []
    assert isinstance(rig.bot, Bot)


# --- one order per exchange per loop, and the loss stop on simulated buys ----------------


def test_exchange_with_an_order_this_loop_is_skipped_by_later_evaluations(tmp_path):
    rig = make_loop(tmp_path, races=1)
    d, r = list(rig.bot.contracts)
    rig.sig.quotes[d] = (D("0.35"), D("0.40"))  # complement (0.40 + 0.50) and reference on D
    rig.sig.quotes[r] = (D("0.45"), D("0.50"))
    rig.kalshi.quotes = {"K0-R": ("0.49", "0.51")}  # D keeps fair 0.48: reference edge on D

    async def go():
        await rig.bot.startup()
        await rig.bot.iteration()

    run(go())
    would = rig.decisions("would_order")
    assert [(w["strategy"], w["sig_exchange_id"]) for w in would] == [
        ("complement", d),
        ("complement", r),
    ]
    skipped = [s for s in rig.decisions("skip") if s["sig_exchange_id"] == d]
    assert any(
        s["strategy"] == "reference" and "exchange already has an order this loop" in s["reason"]
        for s in skipped
    )


def test_simulated_buys_do_not_trip_the_loss_stop(tmp_path):
    # 5 dry-run orders of 50 = 250 > the 200 loss stop: cash-only would trip it.
    rig = make_loop(tmp_path, races=5, limits=make_limits(mode="dry_run", start_balance=100000))
    with_edges(rig, 10)
    rig.sig.balance = D(99900)  # 100 below start: 2 more simulated orders reach the stop

    async def go():
        await rig.bot.startup()
        await rig.bot.iteration()

    run(go())
    assert len(rig.decisions("would_order")) == 5
    assert not [x for x in rig.conn.execute("SELECT reason FROM decisions") if "loss stop" in x[0]]
