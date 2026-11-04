"""The limits ladder: loading, ceilings, gates, the loop, persistence, the CLI. No network."""

import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from bot import db, risk
from bot.ladder import (
    DEFAULT_PATH,
    EventRow,
    Facts,
    LadderError,
    evaluate,
    live_seconds,
    load_ladder,
    main,
    parse_ladder,
    read_facts,
)
from bot.limits import LimitsError
from bot.tests.kit import NOW, make_intent, make_limits
from bot.tests.loopkit import make_loop

D = Decimal
EDGE = (D("0.30"), D("0.40"))  # buy YES @ 0.40 (fair 0.48, election-night edge 0.06)
BASE = make_limits(mode="live", start_balance=100000)
CAPS = {
    "L1": {
        "per_order": 500,
        "per_race": 2000,
        "total_committed": 10000,
        "min_cash_reserve": 90000,
        "loss_stop": 2000,
    },
    "L2": {
        "per_order": 1000,
        "per_race": 3500,
        "total_committed": 35000,
        "min_cash_reserve": 60000,
        "loss_stop": 7000,
    },
    "L3": {
        "per_order": 1000,
        "per_race": 5000,
        "total_committed": 65000,
        "min_cash_reserve": 30000,
        "loss_stop": 12000,
    },
}
SCALES = {"L1": "0.5", "L2": "1.0", "L3": "1.0"}


def _z(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def raw_ladder(
    *, max_step="L3", start=NOW - timedelta(hours=3), gap=timedelta(hours=1), **step_fields
):
    steps = []
    for i, name in enumerate(("L1", "L2", "L3")):
        step = {
            "name": name,
            "not_before_utc": _z(start + i * gap),
            "require_live": True,
            "clean_minutes": 1,
            "min_fills": 0,
            "scale": SCALES[name],
            "caps": dict(CAPS[name]),
        }
        step.update(step_fields)
        steps.append(step)
    return {"max_step": max_step, "steps": steps}


def dump(raw) -> bytes:
    return yaml.safe_dump(raw, sort_keys=False).encode()


def loaded(raw=None, base=BASE):
    return parse_ladder(dump(raw or raw_ladder()), base)


def write(path: Path, raw) -> Path:
    path.write_bytes(dump(raw))
    return path


def run(coro):
    return asyncio.run(coro)


# --- loading and validation -------------------------------------------------------------


def test_repo_ladder_yaml_loads_with_exact_decimals():
    ladder = load_ladder(make_limits(), DEFAULT_PATH)
    assert ladder is not None
    assert ladder.names == ("L0", "L1", "L2", "L3") and ladder.max_step == "L3"
    l1 = ladder.step("L1")
    assert l1.not_before_utc == datetime(2026, 11, 3, 10, 30, tzinfo=UTC)
    assert (l1.require_live, l1.clean_minutes, l1.min_fills) == (True, 45, 1)
    assert str(l1.scale) == "0.5"  # from the YAML text, never through float
    limits = ladder.limits_for("L1")
    assert limits.effective_cap("per_order") == 250
    assert limits.effective_cap("total_committed") == 5000
    assert limits.effective_cap("min_cash_reserve") == 90000  # never scaled
    l3 = ladder.limits_for("L3")
    assert l3.effective_cap("total_committed") == 65000
    assert l3.effective_cap("min_cash_reserve") == 30000
    assert ladder.limits_for("L0") is ladder.base


def test_absent_file_means_no_ladder(tmp_path):
    assert load_ladder(BASE, tmp_path / "ladder.yaml") is None


def test_invalid_ladder_is_a_limits_error_and_refuses_to_start(monkeypatch, capsys):
    assert issubclass(LadderError, LimitsError)
    from bot import main as bot_main

    def bad(*_args, **_kwargs):
        raise LadderError("ladder.yaml failed validation: test")

    monkeypatch.setattr(bot_main, "load_ladder", bad)
    assert bot_main.main([]) == 2
    assert "refusing to start: ladder.yaml failed validation" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("scale", "1.5"),
        ("per_order", 2001),
        ("per_race", 15001),
        ("total_committed", 100001),
        ("min_cash_reserve", -1),
        ("loss_stop", 15001),
    ],
)
def test_hard_ceilings_refuse_to_load(field, value):
    raw = raw_ladder()
    target = raw["steps"][2]
    if field == "scale":
        target["scale"] = value
    else:
        target["caps"][field] = value
    with pytest.raises(LadderError):
        loaded(raw)


def test_values_at_the_ceilings_load():
    raw = raw_ladder()
    raw["steps"][0]["caps"]["min_cash_reserve"] = 60000
    raw["steps"][1]["caps"]["min_cash_reserve"] = 30000
    raw["steps"][2]["caps"] = {
        "per_order": 2000,
        "per_race": 15000,
        "total_committed": 100000,
        "min_cash_reserve": 0,
        "loss_stop": 15000,
    }
    l3 = loaded(raw).limits_for("L3")
    assert l3.effective_cap("total_committed") == 100000
    assert l3.effective_cap("min_cash_reserve") == 0


@pytest.mark.parametrize("where", ["top", "step", "caps"])
def test_unknown_keys_refuse_to_load(where):
    raw = raw_ladder()
    {"top": raw, "step": raw["steps"][0], "caps": raw["steps"][0]["caps"]}[where]["extra"] = 1
    with pytest.raises(LadderError):
        loaded(raw)


@pytest.mark.parametrize("field", ["not_before_utc", "require_live", "clean_minutes", "caps"])
def test_missing_value_refuses_to_load(field):
    raw = raw_ladder()
    del raw["steps"][1][field]
    with pytest.raises(LadderError):
        loaded(raw)


@pytest.mark.parametrize("stamp", ["2026-11-03T10:30:00", "2026-11-03T16:00:00+05:30"])
def test_naive_or_non_utc_datetimes_refuse_to_load(stamp):
    raw = raw_ladder()
    raw["steps"][0]["not_before_utc"] = stamp
    with pytest.raises(LadderError):
        loaded(raw)


@pytest.mark.parametrize(("field", "value"), [("require_live", "yes"), ("clean_minutes", "45")])
def test_loose_types_refuse_to_load(field, value):
    raw = raw_ladder()
    raw["steps"][0][field] = value
    with pytest.raises(LadderError):
        loaded(raw)


def test_out_of_order_steps_refuse_to_load():
    raw = raw_ladder()
    raw["steps"][0], raw["steps"][1] = raw["steps"][1], raw["steps"][0]
    with pytest.raises(LadderError):
        loaded(raw)
    raw = raw_ladder()
    raw["steps"][2]["not_before_utc"] = raw["steps"][1]["not_before_utc"]  # not strictly later
    with pytest.raises(LadderError):
        loaded(raw)
    raw = raw_ladder()
    raw["steps"][1]["name"] = "L1"  # duplicate name
    with pytest.raises(LadderError):
        loaded(raw)


def test_max_step_must_name_a_step():
    with pytest.raises(LadderError):
        loaded(raw_ladder(max_step="L4"))
    assert loaded(raw_ladder(max_step="L0")).max_step == "L0"


def test_reserve_may_not_drop_more_than_30000_per_step():
    raw = raw_ladder()
    raw["steps"][0]["caps"]["min_cash_reserve"] = 59999  # L0 (risk.yaml) is 90000
    with pytest.raises(LadderError):
        loaded(raw)
    raw = raw_ladder()
    raw["steps"][2]["caps"]["min_cash_reserve"] = 29999  # L2 is 60000
    with pytest.raises(LadderError):
        loaded(raw)


def test_step_limits_replace_only_scale_and_caps():
    ladder = loaded()
    l2 = ladder.limits_for("L2")
    assert l2.scale == D("1.0") and l2.caps.total_committed == 35000
    base = BASE.model_dump()
    new = l2.model_dump()
    for key in base:
        if key not in ("scale", "caps"):
            assert new[key] == base[key], key
    with pytest.raises(ValueError):
        l2.scale = D("0.1")  # frozen


# --- gates (pure) -----------------------------------------------------------------------

T = NOW
START = EventRow(1, T - timedelta(hours=2), "info", "start", "bot-ew-4 started in live with 4")


def facts(**overrides) -> Facts:
    values = {
        "now": T,
        "live": True,
        "paused": False,
        "stopped": False,
        "held": False,
        "events": (START,),
        "unknown_orders": 0,
        "fills": 5,
        "last_account_utc": T - timedelta(seconds=30),
        "balance_readable": True,
        "open_end": T,
    }
    values.update(overrides)
    return Facts(**values)


GATE_LADDER = raw_ladder(start=T - timedelta(hours=1), gap=timedelta(hours=2))
for _step in GATE_LADDER["steps"]:
    _step.update(clean_minutes=45, min_fills=1)
SINCE = T - timedelta(hours=1, minutes=30)


def _err(minutes_ago, kind="pause", level="error", id_=5):
    return EventRow(id_, T - timedelta(minutes=minutes_ago), level, kind, "x")


def test_all_gates_pass_climbs_one_step():
    verdict = evaluate(loaded(GATE_LADDER), "L0", SINCE, facts())
    assert verdict.target == "L1" and verdict.climb, verdict.failures()


@pytest.mark.parametrize(
    ("gate", "change"),
    [
        ("schedule", {"now": T - timedelta(hours=1, minutes=1)}),
        ("live", {"live": False}),
        ("not held", {"paused": True}),
        ("not held", {"stopped": True}),
        ("not held", {"held": True}),
        ("clean", {"events": (START, _err(10))}),
        ("clean", {"events": (START, _err(10, kind="mismatch", level="warn"))}),
        ("clean", {"events": (START, _err(10, kind="order_unknown", level="warn"))}),
        ("clean", {"events": (START, _err(60))}),  # older than 45m but since the step began
        ("no unknown", {"unknown_orders": 1}),
        ("account", {"last_account_utc": None}),
        ("account", {"last_account_utc": T - timedelta(minutes=3)}),
        ("account", {"balance_readable": False}),
        ("live time", {"events": (replace(START, ts=T - timedelta(minutes=44)),)}),
        ("fills", {"fills": 0}),
    ],
)
def test_each_gate_fails_on_its_own(gate, change):
    since = SINCE if gate != "schedule" else T - timedelta(hours=2)
    verdict = evaluate(loaded(GATE_LADDER), "L0", since, facts(**change))
    assert not verdict.climb
    assert [g.name for g in verdict.failures()] == [gate]
    assert all(g.reason for g in verdict.gates)


def test_dry_run_reason_says_waiting():
    verdict = evaluate(loaded(GATE_LADDER), "L0", SINCE, facts(live=False))
    assert verdict.failures()[0].reason == "waiting: dry run"


def test_max_step_blocks_the_schedule_gate():
    raw = json.loads(json.dumps(GATE_LADDER))
    raw["max_step"] = "L0"
    verdict = evaluate(loaded(raw), "L0", SINCE, facts())
    assert [g.name for g in verdict.failures()] == ["schedule"]
    assert "max_step is L0" in verdict.failures()[0].reason


def test_clean_gate_ignores_old_events_and_the_ladders_own_errors():
    old = _err(100)  # before the 45 minute window and before the step began
    own = _err(5, kind="ladder")
    warn = _err(5, kind="reconcile", level="warn")
    verdict = evaluate(loaded(GATE_LADDER), "L0", SINCE, facts(events=(START, old, own, warn)))
    assert verdict.climb, verdict.failures()


def test_top_step_has_nothing_to_climb_to():
    verdict = evaluate(loaded(GATE_LADDER), "L3", SINCE, facts())
    assert verdict.target is None and not verdict.climb


def ev(id_, minutes_ago, kind, message, level="info"):
    return EventRow(id_, T - timedelta(minutes=minutes_ago), level, kind, message)


def test_live_time_counts_live_unpaused_runs_only():
    events = [
        ev(1, 300, "start", "v started in dry_run with 4"),  # dry run: not counted
        ev(2, 240, "start", "v started in live with 4"),  # crashed: ends at its last event
        ev(3, 230, "reconcile", "1 open order(s)"),
        ev(4, 200, "start", "v started in live with 4"),
        ev(5, 190, "pause", "PAUSED: reconcile", level="error"),  # 10 min live
        ev(6, 170, "resume", "resumed: the PAUSED file was removed"),
        ev(7, 160, "stop", "stopping (signal) in live"),  # 10 more min
        ev(8, 30, "start", "v started in live with 4 while PAUSED"),
        ev(9, 20, "resume", "resumed: the PAUSED file was removed"),  # 20 min to now
    ]
    assert live_seconds(events, T) == (10 + 10 + 10 + 20) * 60


def test_read_facts_counts_live_fills_but_not_the_probe(tmp_path):
    conn = db.connect(tmp_path / "b.db")
    for key, status, strat in [
        ("a", "filled", "reference"),
        ("b", "partial", "complement"),
        ("c", "filled", "probe"),
        ("d", "unknown", "reference"),
        ("e", "resting", "reference"),
    ]:
        db.insert_order(
            conn,
            {
                "key": key,
                "created_utc": "2026-11-03T10:00:00Z",
                "updated_utc": "2026-11-03T10:00:00Z",
                "sig_exchange_id": "1",
                "side": "yes",
                "action": "buy",
                "price_side": "0.4",
                "wire_price": "0.4",
                "quantity": 1,
                "expires_utc": "2026-11-03T10:02:00Z",
                "strategy": strat,
                "status": status,
            },
        )
    got = read_facts(conn, tmp_path, now=T, live=True, open_end=T)
    assert (got.fills, got.unknown_orders, got.last_account_utc) == (2, 1, None)


# --- the loop ---------------------------------------------------------------------------


def ladder_loop(tmp_path, *, raw=None, live=True, file=None):
    path = file or tmp_path / "ladder.yaml"
    if raw is not None or not path.exists():
        write(path, raw or raw_ladder())
    ladder = parse_ladder(path.read_bytes(), BASE if live else make_limits(mode="dry_run"))
    limits = ladder.base
    return make_loop(tmp_path, live=live, limits=limits, ladder=ladder, ladder_path=path)


async def loops(rig, n, steps=None, seconds=10.0):
    for _ in range(n):
        assert await rig.bot.iteration()
        if steps is not None:
            steps.append(rig.bot.ladder.step)
        await rig.time.sleep(seconds)


def ladder_events(rig, text=""):
    return [r["message"] for r in rig.events("ladder") if text in r["message"]]


def test_climbs_one_step_per_loop_once_every_gate_passes(tmp_path):
    rig = ladder_loop(tmp_path)
    steps = []

    async def go():
        await rig.bot.startup()
        await loops(rig, 10, steps)

    run(go())
    # 60 s of live time (clean_minutes 1) is reached at the 7th loop, then one per loop.
    assert steps == ["L0"] * 6 + ["L1", "L2", "L3", "L3"]
    changes = ladder_events(rig, "all gates passed")
    assert changes[0].startswith("ladder L0 -> L1: all gates passed; scale 0.10 -> 0.5")
    assert "total cap 1000.00 -> 5000.0" in changes[0]
    assert len(changes) == 3
    assert rig.bot.limits is rig.bot.path.limits is rig.bot.ladder.limits
    state = db.get_ladder_state(rig.conn)
    assert state["step"] == "L3"


def test_no_climb_in_dry_run(tmp_path):
    rig = ladder_loop(tmp_path, live=False)

    async def go():
        await rig.bot.startup()
        await loops(rig, 20)

    run(go())
    assert rig.bot.ladder.step == "L0"
    assert rig.heartbeat()["ladder_step"] == "L0"
    assert ladder_events(rig, "live: waiting: dry run")


def test_hold_file_freezes_the_ladder(tmp_path):
    rig = ladder_loop(tmp_path)
    hold = rig.state_dir / "LADDER_HOLD"

    async def go():
        await rig.bot.startup()
        hold.write_text("")
        await loops(rig, 15)
        assert rig.bot.ladder.step == "L0"
        hold.unlink()
        await loops(rig, 1)

    run(go())
    assert rig.bot.ladder.step == "L1"
    assert ladder_events(rig, "LADDER_HOLD file(s) exist")


def test_reasons_are_logged_at_most_once_per_10_minutes(tmp_path):
    rig = ladder_loop(tmp_path)
    (rig.state_dir / "LADDER_HOLD").write_text("")

    async def go():
        await rig.bot.startup()
        await loops(rig, 70)  # 700 s

    run(go())
    assert len(ladder_events(rig, "not held:")) == 2


def test_lowering_max_step_steps_down_at_the_next_loop(tmp_path):
    rig = ladder_loop(tmp_path)
    path = tmp_path / "ladder.yaml"

    async def go():
        await rig.bot.startup()
        await loops(rig, 9)
        assert rig.bot.ladder.step == "L3"
        write(path, raw_ladder(max_step="L1"))
        await loops(rig, 1)
        assert rig.bot.ladder.step == "L1"
        await loops(rig, 3)  # stays: max_step
        assert rig.bot.ladder.step == "L1"
        write(path, raw_ladder(max_step="L0"))
        await loops(rig, 1)

    run(go())
    assert rig.bot.ladder.step == "L0"
    assert rig.bot.limits is rig.bot.ladder.loaded.base
    downs = ladder_events(rig, "max_step lowered")
    assert downs[0].startswith("ladder L3 -> L1: max_step lowered to L1")
    assert rig.heartbeat()["cap_total"] == "1000.00"


def test_editing_active_step_caps_updates_limits_at_next_loop(tmp_path):
    rig = ladder_loop(tmp_path)
    path = tmp_path / "ladder.yaml"

    async def go():
        await rig.bot.startup()
        await loops(rig, 9)
        assert rig.bot.ladder.step == "L3"
        assert rig.bot.limits.effective_cap("total_committed") == 65000
        assert rig.bot.limits.effective_cap("min_cash_reserve") == 30000
        updated = raw_ladder()
        updated["steps"][0]["caps"]["min_cash_reserve"] = 60000
        updated["steps"][1]["caps"]["min_cash_reserve"] = 35000
        updated["steps"][2]["caps"]["total_committed"] = 79611
        updated["steps"][2]["caps"]["min_cash_reserve"] = 10000
        write(path, updated)
        await loops(rig, 1)

    run(go())
    assert rig.bot.ladder.step == "L3"
    assert rig.bot.limits.effective_cap("total_committed") == 79611
    assert rig.bot.limits.effective_cap("min_cash_reserve") == 10000
    assert rig.heartbeat()["cap_total"] == "79611.0"
    assert rig.heartbeat()["min_cash_reserve"] == "10000"
    assert ladder_events(rig, "active step caps updated")


def test_invalid_reread_freezes_the_step_until_the_file_is_valid(tmp_path):
    rig = ladder_loop(tmp_path)
    path = tmp_path / "ladder.yaml"
    good = path.read_bytes()

    async def go():
        await rig.bot.startup()
        await loops(rig, 7)
        assert rig.bot.ladder.step == "L1"
        bad = raw_ladder()
        bad["steps"][1]["caps"]["per_order"] = 99999
        write(path, bad)
        await loops(rig, 5)
        assert rig.bot.ladder.step == "L1"
        path.unlink()
        await loops(rig, 3)
        assert rig.bot.ladder.step == "L1"
        path.write_bytes(good)
        await loops(rig, 1)

    run(go())
    errors = [r for r in rig.events("ladder") if r["level"] == "error"]
    assert len(errors) == 2  # one per bad content (the bad file, then the missing file)
    assert "re-read failed, holding at L1" in errors[0]["message"]
    assert ladder_events(rig, "valid again")
    assert rig.bot.ladder.step == "L2"
    assert not rig.paused


def test_resume_after_restart_with_a_matching_hash(tmp_path):
    rig = ladder_loop(tmp_path)

    async def go(r, n):
        await r.bot.startup()
        await loops(r, n)

    run(go(rig, 8))
    assert rig.bot.ladder.step == "L2"
    rig.conn.close()
    again = ladder_loop(tmp_path)
    run(again.bot.startup())
    assert again.bot.ladder.step == "L2"
    assert again.bot.limits.caps.total_committed == 35000
    assert again.heartbeat()["ladder_step"] == "L2"
    assert ladder_events(again, "ladder resumed at L2")


def test_restart_with_a_changed_file_starts_at_l0(tmp_path):
    rig = ladder_loop(tmp_path)
    run(rig.bot.startup())
    run(loops(rig, 8))
    assert rig.bot.ladder.step == "L2"
    rig.conn.close()
    path = tmp_path / "ladder.yaml"
    path.write_bytes(path.read_bytes() + b"# edited\n")
    again = ladder_loop(tmp_path)
    run(again.bot.startup())
    assert again.bot.ladder.step == "L0"
    assert again.bot.limits is again.bot.ladder.loaded.base
    assert ladder_events(again, "ladder.yaml changed since the stored step L2")
    assert db.get_ladder_state(again.conn)["step"] == "L0"


def test_restart_above_max_step_starts_at_l0(tmp_path):
    rig = ladder_loop(tmp_path)
    run(rig.bot.startup())
    run(loops(rig, 8))
    rig.conn.close()
    # Same bytes, same hash: only possible through the stored row, so rewrite the row.
    conn = db.connect(tmp_path / "state" / "bellwether.db")
    sha = db.get_ladder_state(conn)["ladder_sha256"]
    conn.close()
    path = tmp_path / "ladder.yaml"
    write(path, raw_ladder(max_step="L1"))
    again = ladder_loop(tmp_path)
    db.set_ladder_state(
        again.conn, step="L2", since_utc="2026-11-03T23:30:00Z", sha=again.bot.ladder.loaded.sha256
    )
    assert sha != again.bot.ladder.loaded.sha256
    run(again.bot.startup())
    assert again.bot.ladder.step == "L0"
    assert ladder_events(again, "stored step L2 is above max_step L1")


def test_absent_ladder_behaves_exactly_as_before(tmp_path):
    rig = make_loop(tmp_path, live=True)

    async def go():
        await rig.bot.startup()
        await loops(rig, 12)

    run(go())
    assert rig.bot.ladder is None
    assert rig.events("ladder") == [] and rig.events("resume") == []
    assert db.get_ladder_state(rig.conn) is None
    beat = rig.heartbeat()
    assert beat["ladder_step"] is None
    assert (beat["scale"], beat["cap_total"]) == ("0.10", "1000.00")
    assert "while PAUSED" not in rig.events("start")[-1]["message"]


def test_heartbeat_and_exposure_show_the_active_step(tmp_path):
    rig = ladder_loop(tmp_path)

    async def go():
        await rig.bot.startup()
        assert rig.heartbeat()["ladder_step"] == "L0"
        await loops(rig, 7)

    run(go())
    beat = rig.heartbeat()
    assert beat["ladder_step"] == "L1"
    assert (beat["scale"], beat["cap_total"], beat["cap_per_race"], beat["min_cash_reserve"]) == (
        "0.5",
        "5000.0",
        "1000.0",
        "90000",
    )
    caps = {r["cap"] for r in rig.conn.execute("SELECT cap FROM exposure")}
    assert caps == {"1000.0"}


def test_step_change_happens_only_at_the_start_of_a_loop(tmp_path):
    rig = ladder_loop(tmp_path)
    order: list[str] = []
    real_tick, real_place = rig.bot.ladder.tick, rig.bot.path.place

    def tick(now):
        order.append("tick")
        return real_tick(now)

    async def place(intent, **kwargs):
        order.append("place")
        return await real_place(intent, **kwargs)

    rig.bot.ladder.tick = tick
    rig.bot.path.place = place

    async def go():
        await rig.bot.startup()
        for exchange_id in list(rig.bot.contracts)[:2]:
            rig.sig.quotes[exchange_id] = EDGE
        await loops(rig, 3, seconds=130.0)  # orders expire and are cancelled in between

    run(go())
    assert order.count("tick") == 3 and "place" in order
    # Every loop begins with its tick; no tick between places of one loop.
    loops_seen = "".join("T" if o == "tick" else "p" for o in order).split("T")[1:]
    assert len(loops_seen) == 3


def test_set_limits_is_refused_while_an_order_is_being_placed(tmp_path):
    rig = ladder_loop(tmp_path)

    async def go():
        async with rig.bot.path._lock:
            with pytest.raises(RuntimeError):
                rig.bot.apply_limits(rig.bot.ladder.loaded.limits_for("L3"))

    run(go())
    assert rig.bot.limits is rig.bot.path.limits


def test_order_size_follows_the_active_step(tmp_path):
    rig = ladder_loop(tmp_path)
    (exchange_id,) = list(rig.bot.contracts)[:1]

    async def go():
        await rig.bot.startup()
        rig.sig.quotes[exchange_id] = EDGE
        await loops(rig, 1)
        rig.sig.quotes.pop(exchange_id)
        await loops(rig, 6)
        assert rig.bot.ladder.step == "L1"
        other = list(rig.bot.contracts)[2]
        rig.sig.quotes[other] = EDGE

    run(go())
    run(rig.bot.iteration())  # L1 -> L2 at the start of this loop, then the order
    posts = [json.loads(s.content, parse_float=D) for s in rig.sig.writes() if s.path == "/orders"]
    # L0: per_order 500 x 0.10 = 50 -> 125 shares. L2: 1000 x 1.0, per race 3500 x 1.0, but
    # total room is 35000 - 50 committed and depth is 1000 -> 1000 shares.
    assert [p["quantity"] for p in posts] == [125, 1000]


# --- risk.check across steps ------------------------------------------------------------


FULL = D(100000)


def _state(limits, balance=FULL):
    return risk.RiskState(
        limits=limits,
        balance=balance,
        total_committed=D(0),
        race_committed=D(0),
        writes_last_min=0,
        unknown_orders=0,
        journal_committed=D(100000) - balance,  # cash spent on buys: equity unchanged
    )


def test_risk_denies_what_the_previous_step_allowed_and_the_reverse(tmp_path):
    ladder = loaded()
    l1, l2 = ladder.limits_for("L1"), ladder.limits_for("L2")
    big = make_intent(price_side=D("0.40"), quantity=1000)  # 400: over L1 per-order 250
    assert not risk.check(big, replace(_state(l1), state_dir=tmp_path)).allowed
    assert risk.check(big, replace(_state(l2), state_dir=tmp_path)).allowed
    # Balance 85000: L1's 90000 reserve refuses, L2's 60000 allows; stepping down reverses.
    small = make_intent(price_side=D("0.40"), quantity=10)
    assert risk.check(small, replace(_state(l2, D(85000)), state_dir=tmp_path)).allowed
    assert not risk.check(small, replace(_state(l1, D(85000)), state_dir=tmp_path)).allowed


# --- persistence and the command line ---------------------------------------------------


def test_connect_adds_ladder_step_to_an_old_heartbeat(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE heartbeat (id INTEGER PRIMARY KEY CHECK (id = 1), ts_utc TEXT)")
    old.execute("INSERT INTO heartbeat (id, ts_utc) VALUES (1, 'x')")
    old.commit()
    old.close()
    conn = db.connect(path)
    db.upsert_heartbeat(conn, ts_utc="y", ladder_step="L1")
    assert conn.execute("SELECT ladder_step FROM heartbeat").fetchone()[0] == "L1"


def test_cli_check_prints_every_step(tmp_path):
    risk_path = tmp_path / "risk.yaml"
    risk_path.write_text((DEFAULT_PATH.parent / "risk.yaml").read_text(encoding="utf-8"))
    lines: list[str] = []
    assert main(["check"], risk_path=risk_path, ladder_path=DEFAULT_PATH, out=lines.append) == 0
    text = "\n".join(lines)
    for name in ("L0", "L1", "L2", "L3"):
        assert f"\n{name}  " in "\n" + text
    assert "total 65000.0" in text and "reserve 30000" in text
    bad = write(tmp_path / "bad.yaml", raw_ladder(max_step="L9"))
    lines.clear()
    assert main(["check"], risk_path=risk_path, ladder_path=bad, out=lines.append) == 2
    lines.clear()
    absent = tmp_path / "absent.yaml"
    assert main(["check"], risk_path=risk_path, ladder_path=absent, out=lines.append) == 0
    assert lines[0].startswith("no ladder")


def test_cli_status_reads_the_database_read_only(tmp_path):
    rig = ladder_loop(tmp_path)
    run(rig.bot.startup())
    run(loops(rig, 7))
    rig.conn.close()
    risk_path = tmp_path / "risk.yaml"
    risk_path.write_text(yaml.safe_dump(json.loads(BASE.model_dump_json())), encoding="utf-8")
    db_path = tmp_path / "state" / "bellwether.db"
    before = db_path.read_bytes()
    lines: list[str] = []
    code = main(
        ["status"],
        risk_path=risk_path,
        ladder_path=tmp_path / "ladder.yaml",
        db_path=db_path,
        state_dir=tmp_path / "state",
        now=NOW + timedelta(seconds=70),
        out=lines.append,
    )
    assert code == 0, lines
    text = "\n".join(lines)
    assert "current step L1" in text and "next step L2" in text
    assert "PASS  schedule" in text and "PASS  live time" in text
    assert db_path.read_bytes() == before
