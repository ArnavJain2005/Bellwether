"""The limits ladder: config/ladder.yaml (Arnav-only file; this module never writes it).

    python -m bot.ladder check     validate ladder.yaml, print each step's effective caps
    python -m bot.ladder status    read bot/state/bellwether.db read-only, print every gate

A ladder is a list of steps L1..Ln, each with a scale and caps. Step L0 is risk.yaml as
loaded at startup. The bot climbs one step at a time, only at the start of a loop, and
only when the next step's earliest time has passed AND every health gate passes. A
clock alone never raises the caps. The ladder replaces scale and caps only: the mode,
edges, strategies, NO-order settings and the mapping always come from risk.yaml.

No ladder.yaml at startup = no ladder: the bot behaves exactly as before. An invalid one
refuses to start (LadderError is a LimitsError). While running, ladder.yaml is re-read
every loop: max_step and LADDER_HOLD take effect without a restart. A re-read that fails
validation (or a deleted file) keeps the current step, writes one error event and stops
climbing until the file is valid again. The active step's caps are the ones it was
entered with; an edit to the current step's caps applies the next time a step is
entered. The only automatic downward move is to max_step, when the current step is above
it.

Gates for climbing from the current step to the next (each failure has a reason):
  1. schedule:  now >= next.not_before_utc, and next is at or below max_step.
  2. live:      require_live -> the bot is live ("waiting: dry run" otherwise).
  3. not held:  no PAUSED, STOP or LADDER_HOLD file in bot/state/.
  4. clean:     no error-level event (other than the ladder's own), no 'mismatch' and no
                'order_unknown' event within the last clean_minutes or since the current
                step began, whichever is longer.
  5. no unknown orders in the journal.
  6. account:   the last successful account cycle (snapshot row) is under 3 minutes old
                and its balance was read.
  7. live time: live, unpaused time in the events table, over all runs, >= clean_minutes.
  8. fills:     >= min_fills journal orders (not the probe) are 'filled' or 'partial'.
"""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

from bot import db
from bot.limits import Caps, Limits, LimitsError, _DecimalLoader, load_limits

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "ladder.yaml"
HOLD_FILE = "LADDER_HOLD"
L0 = "L0"
ACCOUNT_FRESH = timedelta(minutes=3)
IST = timezone(timedelta(hours=5, minutes=30), "IST")

# Hard ceilings, checked at load: any breach refuses to start.
MAX_SCALE = Decimal(1)
MAX_PER_ORDER = Decimal(1500)
MAX_PER_RACE = Decimal(6000)
MAX_TOTAL_COMMITTED = Decimal(70000)
MIN_CASH_RESERVE_FLOOR = Decimal(25000)
MAX_LOSS_STOP = Decimal(15000)
MAX_RESERVE_DROP = Decimal(30000)  # per step, against the step before (L1 against L0)

# Events that keep the 'clean' gate shut, besides any error-level event.
DIRTY_KINDS = frozenset({"mismatch", "order_unknown"})


class LadderError(LimitsError):
    """config/ladder.yaml is malformed, out of range or above a hard ceiling."""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Step(_Frozen):
    name: str
    not_before_utc: datetime
    require_live: StrictBool
    clean_minutes: StrictInt = Field(ge=1, le=1440)
    min_fills: StrictInt = Field(ge=0, le=1000)
    scale: Decimal = Field(gt=0, le=MAX_SCALE)
    caps: Caps

    @field_validator("not_before_utc")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("not_before_utc must carry a UTC offset (end in Z)")
        if value.utcoffset() != timedelta(0):
            raise ValueError("not_before_utc must be UTC (end in Z)")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _ceilings(self) -> Step:
        caps = self.caps
        breaches = [
            f"per_order {caps.per_order} > {MAX_PER_ORDER}"
            if caps.per_order > MAX_PER_ORDER
            else "",
            f"per_race {caps.per_race} > {MAX_PER_RACE}" if caps.per_race > MAX_PER_RACE else "",
            f"total_committed {caps.total_committed} > {MAX_TOTAL_COMMITTED}"
            if caps.total_committed > MAX_TOTAL_COMMITTED
            else "",
            f"min_cash_reserve {caps.min_cash_reserve} < {MIN_CASH_RESERVE_FLOOR}"
            if caps.min_cash_reserve < MIN_CASH_RESERVE_FLOOR
            else "",
            f"loss_stop {caps.loss_stop} > {MAX_LOSS_STOP}"
            if caps.loss_stop > MAX_LOSS_STOP
            else "",
        ]
        breaches = [b for b in breaches if b]
        if breaches:
            raise ValueError(f"step {self.name} breaks a hard ceiling: {'; '.join(breaches)}")
        return self


class Ladder(_Frozen):
    max_step: str
    steps: tuple[Step, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _ordered(self) -> Ladder:
        for index, step in enumerate(self.steps):
            if step.name != f"L{index + 1}":
                raise ValueError(
                    f"step {index + 1} is named {step.name!r}: steps must be L1, L2, ... in order"
                )
            if index and step.not_before_utc <= self.steps[index - 1].not_before_utc:
                raise ValueError(
                    f"{step.name}: not_before_utc must be later than {self.steps[index - 1].name}'s"
                )
        if self.max_step not in self.names:
            raise ValueError(f"max_step {self.max_step!r} is not a step ({', '.join(self.names)})")
        return self

    @property
    def names(self) -> tuple[str, ...]:
        return (L0, *(s.name for s in self.steps))


@dataclass(frozen=True)
class LoadedLadder:
    """A validated ladder.yaml, its file hash, and risk.yaml as loaded at startup (L0)."""

    ladder: Ladder
    sha256: str
    base: Limits

    @property
    def names(self) -> tuple[str, ...]:
        return self.ladder.names

    @property
    def max_step(self) -> str:
        return self.ladder.max_step

    def index(self, name: str) -> int:
        return self.names.index(name)

    def step(self, name: str) -> Step | None:
        """The step's settings; None for L0 (risk.yaml)."""
        return None if name == L0 else self.ladder.steps[self.index(name) - 1]

    def limits_for(self, name: str) -> Limits:
        """risk.yaml's Limits with only scale and caps replaced by the step's."""
        step = self.step(name)
        return self.base if step is None else self.base.with_step(step.scale, step.caps)


def parse_ladder(data: bytes, base: Limits, source: str = "ladder.yaml") -> LoadedLadder:
    """Validate ladder.yaml's bytes against the hard ceilings and risk.yaml (L0)."""
    sha = hashlib.sha256(data).hexdigest()
    try:
        raw = yaml.load(data.decode("utf-8"), Loader=_DecimalLoader)  # SafeLoader subclass
    except LimitsError:
        raise LadderError(f"{source}: a number is not a finite decimal") from None
    except (yaml.YAMLError, UnicodeDecodeError):
        raise LadderError(f"{source} is not valid YAML") from None
    if not isinstance(raw, dict):
        raise LadderError(f"{source} must hold a mapping")
    try:
        ladder = Ladder.model_validate(raw)
    except ValueError as exc:
        raise LadderError(f"{source} failed validation: {exc}") from None
    previous, previous_name = base.caps.min_cash_reserve, f"{L0} (risk.yaml)"
    for step in ladder.steps:
        if step.caps.min_cash_reserve < previous - MAX_RESERVE_DROP:
            raise LadderError(
                f"{source}: {step.name} min_cash_reserve {step.caps.min_cash_reserve} is more "
                f"than {MAX_RESERVE_DROP} below {previous_name}'s {previous}"
            )
        previous, previous_name = step.caps.min_cash_reserve, step.name
    return LoadedLadder(ladder=ladder, sha256=sha, base=base)


def load_ladder(base: Limits, path: Path = DEFAULT_PATH) -> LoadedLadder | None:
    """None when ladder.yaml is absent (no ladder); LadderError when it is invalid."""
    path = Path(path)
    if not path.exists():
        return None
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise LadderError(f"cannot read {path}: {type(exc).__name__}") from None
    return parse_ladder(data, base, str(path))


# --- facts and gates (pure) ------------------------------------------------------------


@dataclass(frozen=True)
class EventRow:
    id: int
    ts: datetime | None  # None when the stored stamp cannot be read
    level: str
    kind: str
    message: str


@dataclass(frozen=True)
class Facts:
    """Everything the gates need, read once per evaluation (see read_facts)."""

    now: datetime
    live: bool
    paused: bool
    stopped: bool
    held: bool
    events: tuple[EventRow, ...]
    unknown_orders: int
    fills: int
    last_account_utc: datetime | None
    balance_readable: bool
    open_end: datetime  # where a live run with no stop event ends: now for the running bot


@dataclass(frozen=True)
class Gate:
    name: str
    ok: bool
    reason: str


@dataclass(frozen=True)
class Verdict:
    current: str
    target: str | None  # the next step, or None at the top
    gates: tuple[Gate, ...]

    @property
    def climb(self) -> bool:
        return self.target is not None and all(g.ok for g in self.gates)

    def failures(self) -> list[Gate]:
        return [g for g in self.gates if not g.ok]


def span(delta: timedelta) -> str:
    """'1h05m', '12m30s', '45s'."""
    seconds = max(0, int(delta.total_seconds()))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def stamp(moment: datetime) -> str:
    return db.utc_stamp(moment)


def _is_start(ev: EventRow) -> bool:
    return ev.kind == "start" and ev.level == "info" and " started in " in ev.message


def _is_stop(ev: EventRow) -> bool:
    return ev.kind == "stop" and ev.message.startswith("stopping (")


def _is_pause(ev: EventRow) -> bool:
    return ev.message.startswith("PAUSED")


def live_seconds(events: Iterable[EventRow], open_end: datetime) -> float:
    """Live, unpaused seconds over every run recorded in the events table.

    A run starts at an info 'start' event ("... started in live ...", " while PAUSED" when
    the PAUSED file existed then) and ends at its 'stopping (...)' event; a run with no
    stop event ends at its last event (a crash: counted short, never long), the latest
    run at `open_end`. Within a live run, time from a 'PAUSED...' event to the next
    'resume' event does not count. Events are taken in id order; a stamp earlier than
    the one before it is read as the one before it.
    """
    total = 0.0
    in_run = live = paused = False
    mark = last = None

    def close(end: datetime) -> None:
        nonlocal total
        if live and not paused and mark is not None:
            total += max(0.0, (end - mark).total_seconds())

    for ev in sorted(events, key=lambda e: e.id):
        if ev.ts is None:
            continue
        if _is_start(ev):
            if in_run and last is not None:
                close(last)
            in_run, live = True, " started in live" in ev.message
            paused = " while PAUSED" in ev.message
            mark = last = ev.ts
            continue
        if not in_run or mark is None:
            continue
        ts = max(ev.ts, last or ev.ts)
        last = ts
        if _is_stop(ev):
            close(ts)
            in_run = False
        elif ev.kind == "resume":
            if paused:
                paused = False
                mark = ts
        elif _is_pause(ev):
            if not paused:
                close(ts)
                paused = True
                mark = ts
    if in_run and mark is not None:
        close(max(open_end, last or open_end))
    return total


def _clean_gate(step: Step, since: datetime, facts: Facts) -> Gate:
    window = max(timedelta(minutes=step.clean_minutes), facts.now - since)
    start = facts.now - window
    dirty = [
        ev
        for ev in facts.events
        if (ev.level == "error" and ev.kind != "ladder") or ev.kind in DIRTY_KINDS
        if ev.ts is None or ev.ts >= start
    ]
    if dirty:
        latest = dirty[-1]
        when = stamp(latest.ts) if latest.ts else "an unreadable time"
        return Gate(
            "clean",
            False,
            f"{len(dirty)} error/mismatch/unknown event(s) in the last {span(window)}; latest "
            f"{latest.level} '{latest.kind}' at {when}: {latest.message[:120]}",
        )
    return Gate("clean", True, f"no error, mismatch or unknown-order events in {span(window)}")


def evaluate(loaded: LoadedLadder, current: str, since: datetime, facts: Facts) -> Verdict:
    """Whether the bot may climb from `current` to the next step, gate by gate."""
    index = loaded.index(current)
    if index + 1 >= len(loaded.names):
        return Verdict(current, None, (Gate("schedule", False, f"{current} is the top step"),))
    target = loaded.names[index + 1]
    step = loaded.step(target)
    assert step is not None
    gates: list[Gate] = []

    # 1. schedule and max_step
    problems = []
    if facts.now < step.not_before_utc:
        problems.append(
            f"not before {stamp(step.not_before_utc)} ({span(step.not_before_utc - facts.now)} "
            "to go)"
        )
    if loaded.index(target) > loaded.index(loaded.max_step):
        problems.append(f"max_step is {loaded.max_step}")
    gates.append(
        Gate("schedule", not problems, "; ".join(problems) or f"{target} is open and allowed")
    )

    # 2. live
    if step.require_live and not facts.live:
        gates.append(Gate("live", False, "waiting: dry run"))
    else:
        gates.append(Gate("live", True, "live" if facts.live else "live not required"))

    # 3. not paused, stopped or held
    files = [
        name
        for name, present in (
            ("PAUSED", facts.paused),
            ("STOP", facts.stopped),
            (HOLD_FILE, facts.held),
        )
        if present
    ]
    gates.append(
        Gate(
            "not held",
            not files,
            f"{', '.join(files)} file(s) exist in bot/state"
            if files
            else "no PAUSED, STOP or hold file",
        )
    )

    # 4. clean window
    gates.append(_clean_gate(step, since, facts))

    # 5. unknown orders
    gates.append(
        Gate(
            "no unknown",
            facts.unknown_orders == 0,
            f"{facts.unknown_orders} order(s) have status 'unknown'"
            if facts.unknown_orders
            else "no order has status 'unknown'",
        )
    )

    # 6. account cycle fresh, balance readable
    if facts.last_account_utc is None:
        gates.append(Gate("account", False, "no successful account cycle recorded"))
    else:
        age = facts.now - facts.last_account_utc
        if age >= ACCOUNT_FRESH:
            gates.append(Gate("account", False, f"last successful account cycle {span(age)} ago"))
        elif not facts.balance_readable:
            gates.append(Gate("account", False, "the last account cycle has no balance"))
        else:
            gates.append(Gate("account", True, f"account cycle {span(age)} ago, balance read"))

    # 7. live time
    seconds = live_seconds(facts.events, facts.open_end)
    need = step.clean_minutes * 60
    gates.append(
        Gate(
            "live time",
            seconds >= need,
            f"live and unpaused for {span(timedelta(seconds=seconds))} of the "
            f"{step.clean_minutes}m needed",
        )
    )

    # 8. fills
    gates.append(
        Gate(
            "fills",
            facts.fills >= step.min_fills,
            f"{facts.fills} filled/partial live order(s), {step.min_fills} needed",
        )
    )
    return Verdict(current, target, tuple(gates))


def caps_text(limits: Limits) -> str:
    return (
        f"scale {limits.scale}; per-order {limits.effective_cap('per_order')}; "
        f"per-race {limits.effective_cap('per_race')}; "
        f"total {limits.effective_cap('total_committed')}; "
        f"reserve {limits.effective_cap('min_cash_reserve')}; "
        f"loss stop {limits.effective_cap('loss_stop')}"
    )


def change_text(old: Limits, new: Limits) -> str:
    """'scale 0.5 -> 1.0; total cap 5000.0 -> 35000; ...' (effective values)."""
    parts = [f"scale {old.scale} -> {new.scale}"]
    for label, name in (
        ("total cap", "total_committed"),
        ("per-race cap", "per_race"),
        ("per-order cap", "per_order"),
        ("min cash reserve", "min_cash_reserve"),
        ("loss stop", "loss_stop"),
    ):
        parts.append(f"{label} {old.effective_cap(name)} -> {new.effective_cap(name)}")
    return "; ".join(parts)


# --- reading facts (I/O) ---------------------------------------------------------------


def _parse_ts(text: Any) -> datetime | None:
    try:
        value = datetime.fromisoformat(str(text))
    except ValueError:
        return None
    return value if value.tzinfo is not None else None


def read_facts(
    conn: sqlite3.Connection,
    state_dir: Path,
    *,
    now: datetime,
    live: bool,
    open_end: datetime,
) -> Facts:
    events = tuple(
        EventRow(int(r[0]), _parse_ts(r[1]), str(r[2]), str(r[3]), str(r[4]))
        for r in conn.execute("SELECT id, ts_utc, level, kind, message FROM events ORDER BY id")
    )
    unknown = conn.execute("SELECT COUNT(*) FROM orders WHERE status = 'unknown'").fetchone()[0]
    fills = conn.execute(
        "SELECT COUNT(*) FROM orders WHERE status IN ('filled', 'partial') AND strategy != 'probe'"
    ).fetchone()[0]
    snap = conn.execute("SELECT ts_utc, balance FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
    state_dir = Path(state_dir)
    return Facts(
        now=now,
        live=live,
        paused=(state_dir / "PAUSED").exists(),
        stopped=(state_dir / "STOP").exists(),
        held=(state_dir / HOLD_FILE).exists(),
        events=events,
        unknown_orders=int(unknown),
        fills=int(fills),
        last_account_utc=_parse_ts(snap[0]) if snap else None,
        balance_readable=bool(snap and snap[1] is not None),
        open_end=open_end,
    )


# --- the running controller ------------------------------------------------------------

EventFn = Callable[..., None]  # (level, message, *, throttle=None)


class LadderController:
    """The bot's ladder: current step, persistence, re-reads, gates. bot/main.py owns it.

    `tick` is called at the start of a loop, before any strategy runs; it returns the
    new active Limits when the step changed, else None.
    """

    def __init__(
        self,
        loaded: LoadedLadder,
        *,
        conn: sqlite3.Connection,
        state_dir: Path,
        live: bool,
        event: EventFn,
        path: Path = DEFAULT_PATH,
    ) -> None:
        self.loaded = loaded
        self.conn = conn
        self.state_dir = Path(state_dir)
        self.live = live
        self.path = Path(path)
        self._event = event
        self.step = L0
        self.since: datetime | None = None
        self.limits = loaded.base
        self._bad: str | None = None  # the invalid content already reported
        self._changed_reported: str | None = None

    def start(self, now: datetime) -> Limits:
        """Resume the stored step if ladder.yaml is unchanged and it is <= max_step."""
        row = db.get_ladder_state(self.conn)
        loaded = self.loaded
        why = None
        if row is None:
            why = "no stored step"
        elif row["ladder_sha256"] != loaded.sha256:
            why = f"ladder.yaml changed since the stored step {row['step']}"
        elif row["step"] not in loaded.names:
            why = f"stored step {row['step']!r} is not in ladder.yaml"
        elif loaded.index(row["step"]) > loaded.index(loaded.max_step):
            why = f"stored step {row['step']} is above max_step {loaded.max_step}"
        since = _parse_ts(row["since_utc"]) if row is not None else None
        if why is None and since is None:
            why = "stored since_utc is unreadable"
        if why is None:
            assert row is not None and since is not None
            self._set(row["step"], since, persist=False)
            self._event(
                "info",
                f"ladder resumed at {self.step} since {stamp(since)} (ladder.yaml unchanged; "
                f"max_step {loaded.max_step}): {caps_text(self.limits)}",
            )
        else:
            self._set(L0, now)
            self._event(
                "info",
                f"ladder starting at {L0} (risk.yaml limits): {why}; max_step "
                f"{loaded.max_step}: {caps_text(self.limits)}",
            )
        return self.limits

    def _set(self, step: str, since: datetime, *, persist: bool = True) -> None:
        self.step, self.since = step, since
        self.limits = self.loaded.limits_for(step)
        if persist:
            db.set_ladder_state(
                self.conn, step=step, since_utc=stamp(since), sha=self.loaded.sha256
            )

    def _reread(self) -> LoadedLadder | None:
        """The file as it is now, or None (reported once per bad content) when invalid."""
        try:
            data = self.path.read_bytes()
        except FileNotFoundError:
            data, error = None, "ladder.yaml is missing"
        except OSError as exc:
            data, error = None, f"cannot read ladder.yaml: {type(exc).__name__}"
        else:
            error = None
        loaded = None
        if data is not None:
            try:
                loaded = parse_ladder(data, self.loaded.base)
            except LadderError as exc:
                error = str(exc)
        if loaded is not None and self.step not in loaded.names:
            error, loaded = f"the current step {self.step} is no longer in ladder.yaml", None
        if loaded is None:
            key = hashlib.sha256(data).hexdigest() if data is not None else "missing"
            if self._bad != key:
                self._bad = key
                self._event(
                    "error",
                    f"ladder.yaml re-read failed, holding at {self.step}; no climbing until "
                    f"it is valid: {error}"[:500],
                )
            return None
        if self._bad is not None:
            self._bad = None
            self._event("info", f"ladder.yaml is valid again; max_step {loaded.max_step}")
        return loaded

    def tick(self, now: datetime) -> Limits | None:
        loaded = self._reread()
        if loaded is None:
            return None
        entered, now_defined = self.loaded.step(self.step), loaded.step(self.step)
        if (
            entered is not None
            and now_defined is not None
            and (entered.scale, entered.caps) != (now_defined.scale, now_defined.caps)
            and self._changed_reported != loaded.sha256
        ):
            self._changed_reported = loaded.sha256
            self._event(
                "warn",
                f"ladder.yaml changed {self.step}'s scale or caps; the active caps stay as "
                f"entered until the next step change: {caps_text(self.limits)}",
            )
        # self.limits keeps the caps this step was entered with; max_step, the gates and
        # the steps entered from now on use the new file.
        old_limits = self.limits
        self.loaded = loaded
        if loaded.index(self.step) > loaded.index(loaded.max_step):
            old = self.step
            self._set(loaded.max_step, now)
            self._event(
                "info",
                f"ladder {old} -> {self.step}: max_step lowered to {loaded.max_step}; "
                f"{change_text(old_limits, self.limits)}",
            )
            return self.limits
        if loaded.index(self.step) + 1 >= len(loaded.names):
            return None  # the top step: nothing to climb to
        assert self.since is not None
        facts = read_facts(self.conn, self.state_dir, now=now, live=self.live, open_end=now)
        verdict = evaluate(loaded, self.step, self.since, facts)
        if verdict.climb:
            assert verdict.target is not None
            old = self.step
            self._set(verdict.target, now)
            self._event(
                "info",
                f"ladder {old} -> {self.step}: all gates passed; "
                f"{change_text(old_limits, self.limits)}",
            )
            return self.limits
        for gate in verdict.failures():
            self._event(
                "info",
                f"ladder holding at {self.step} (next {verdict.target}): {gate.name}: "
                f"{gate.reason}"[:500],
                throttle=f"ladder:{gate.name}",
            )
        return None


# --- command line (read-only; never places orders) -------------------------------------


def _ist(moment: datetime) -> str:
    return moment.astimezone(IST).strftime("%b %d %H:%M IST")


def _check(base: Limits, loaded: LoadedLadder, out: Callable[[str], None]) -> None:
    out(f"ladder.yaml OK (sha256 {loaded.sha256[:12]}); max_step {loaded.max_step}")
    for name in loaded.names:
        step = loaded.step(name)
        limits = loaded.limits_for(name)
        if step is None:
            head = f"{name}  risk.yaml as loaded at startup"
        else:
            head = (
                f"{name}  not before {stamp(step.not_before_utc)} ({_ist(step.not_before_utc)}); "
                f"require_live {str(step.require_live).lower()}; clean {step.clean_minutes}m; "
                f"min_fills {step.min_fills}"
            )
        above = loaded.index(name) > loaded.index(loaded.max_step)
        out(head + ("  [above max_step]" if above else ""))
        out(f"    effective: {caps_text(limits)}")


def _status(
    base: Limits,
    loaded: LoadedLadder,
    db_path: Path,
    state_dir: Path,
    now: datetime,
    out: Callable[[str], None],
) -> int:
    if not Path(db_path).exists():
        out(f"no database at {db_path}")
        return 1
    conn = sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        has_state = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'ladder_state'"
        ).fetchone()
        row = db.get_ladder_state(conn) if has_state else None
        beat = conn.execute("SELECT ts_utc, mode FROM heartbeat WHERE id = 1").fetchone()
        live = bool(beat and beat["mode"] == "live")
        open_end = (_parse_ts(beat["ts_utc"]) if beat else None) or now
        facts = read_facts(conn, state_dir, now=now, live=live, open_end=min(open_end, now))
    finally:
        conn.close()
    if row is None:
        out("no ladder_state row: the bot has not run with a ladder yet (it starts at L0)")
        current, since = L0, None
    else:
        current, since = row["step"], _parse_ts(row["since_utc"])
        note = ""
        if row["ladder_sha256"] != loaded.sha256:
            note = " (ladder.yaml has changed since: the next restart starts at L0)"
        out(
            f"current step {current} since {row['since_utc']}"
            + (f" ({span(now - since)} ago)" if since else "")
            + note
        )
    if beat is not None:
        out(f"heartbeat {beat['ts_utc']} ({span(now - open_end)} ago), mode {beat['mode']}")
    if current not in loaded.names:
        out(f"{current} is not in ladder.yaml")
        return 1
    out(f"active caps: {caps_text(loaded.limits_for(current))}")
    if loaded.index(current) > loaded.index(loaded.max_step):
        out(f"{current} is above max_step {loaded.max_step}: the bot steps down at its next loop")
        return 0
    verdict = evaluate(loaded, current, since or now, facts)
    if verdict.target is None:
        out(f"{current} is the top step")
        return 0
    step = loaded.step(verdict.target)
    assert step is not None
    remaining = step.not_before_utc - now
    out(
        f"next step {verdict.target} not before {stamp(step.not_before_utc)} "
        f"({_ist(step.not_before_utc)}): "
        + (f"{span(remaining)} to go" if remaining > timedelta(0) else "time reached")
    )
    out(f"    would be: {caps_text(loaded.limits_for(verdict.target))}")
    for gate in verdict.gates:
        out(f"  {'PASS' if gate.ok else 'FAIL'}  {gate.name:<10} {gate.reason}")
    out("climbs at the next loop" if verdict.climb else "holding")
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    risk_path: Path | None = None,
    ladder_path: Path = DEFAULT_PATH,
    db_path: Path = db.DB_PATH,
    state_dir: Path = db.STATE_DIR,
    now: datetime | None = None,
    out: Callable[[str], None] = print,
) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m bot.ladder", description=__doc__.split("\n")[0]
    )
    parser.add_argument("command", choices=("check", "status"))
    args = parser.parse_args(argv)
    try:
        base = load_limits(risk_path) if risk_path is not None else load_limits()
        loaded = load_ladder(base, ladder_path)
    except LimitsError as exc:
        out(f"invalid: {exc}")
        return 2
    if loaded is None:
        out(f"no ladder: {ladder_path} is absent; the bot uses risk.yaml only")
        if args.command == "check":
            out(f"L0  effective: {caps_text(base)}")
        return 0
    if args.command == "check":
        _check(base, loaded, out)
        return 0
    return _status(base, loaded, db_path, state_dir, now or datetime.now(UTC), out)


if __name__ == "__main__":
    sys.exit(main())
