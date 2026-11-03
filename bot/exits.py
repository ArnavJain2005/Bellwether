"""Reduce-only exits: config/exits.yaml (Arnav-only file; this module never writes it).

An exit closes shares the bot already holds when SIG pays more for them than they are
worth: for a holding on side S with fair value f_S and SIG's best bid for that side b_S,
exit when b_S >= f_S + exit_edge (bot/strategy.py, evaluate_exit).

Mechanism (Step 0 of BOT-EW-5): a complement BUY in the same exchange, never a SELL.
POST /orders documents that "an opposite-signed BUY nets against the existing position:
it reduces or crosses the position at FIFO lot cost", and CLAUDE.md says bot/ never
sells. `mechanism: auto` therefore means complement_buy; `mechanism: sell` fails
validation. The exit probe (bot/exit_probe.py) shows the netting with 1 share before
Arnav sets `enabled: true`.

No exits.yaml = no exits: the bot behaves exactly as before. The file is re-read every
loop, so `enabled` and the other values take effect without a restart. A file that
fails validation means exits stay off and one error event is written (until the file is
valid again or removed); buys carry on unaffected.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Literal

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

from bot.limits import LimitsError, _DecimalLoader

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "exits.yaml"
MAX_EXIT_EDGE = Decimal("0.2")
COMPLEMENT_BUY = "complement_buy"


class ExitsError(LimitsError):
    """config/exits.yaml is malformed or out of range."""


class _ExitsLoader(_DecimalLoader):
    """Exact Decimals, and only true/false are booleans.

    YAML 1.1 also reads yes/no/on/off as booleans, which would turn `sides: [yes, no]`
    into [True, False]. Here they stay strings, so `sides` reads as written and
    `enabled: yes` fails the strict bool check instead of passing.
    """


_ExitsLoader.yaml_implicit_resolvers = {
    first: [(tag, regexp) for tag, regexp in resolvers if tag != "tag:yaml.org,2002:bool"]
    for first, resolvers in _DecimalLoader.yaml_implicit_resolvers.items()
}
_ExitsLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)


class ExitSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: StrictBool
    mechanism: Literal["auto", "complement_buy", "sell"]
    exit_edge: Decimal = Field(gt=0, lt=MAX_EXIT_EDGE)
    exit_edge_election_night: Decimal = Field(gt=0, lt=MAX_EXIT_EDGE)
    max_exit_quantity_fraction: Decimal = Field(gt=0, le=1)
    reentry_block_minutes: StrictInt = Field(ge=0, le=1440)
    sides: tuple[Literal["yes", "no"], ...] = Field(min_length=1, max_length=2)

    @field_validator("exit_edge", "exit_edge_election_night", "max_exit_quantity_fraction")
    @classmethod
    def _exact(cls, value: Decimal) -> Decimal:
        if not isinstance(value, Decimal) or not value.is_finite():
            raise ValueError("must be an exact decimal")
        return value

    @field_validator("sides")
    @classmethod
    def _unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("sides must not repeat")
        return value

    @model_validator(mode="after")
    def _no_sell(self) -> ExitSettings:
        if self.mechanism == "sell":
            raise ValueError(
                "mechanism 'sell' is not built: CLAUDE.md says bot/ never sells; "
                "use auto or complement_buy"
            )
        return self

    @property
    def resolved_mechanism(self) -> str:
        """What `auto` means (Step 0): the complement buy."""
        return COMPLEMENT_BUY


def parse_exits(data: bytes, source: str = "exits.yaml") -> ExitSettings:
    """Validate exits.yaml's bytes, or raise ExitsError."""
    try:
        raw = yaml.load(data.decode("utf-8"), Loader=_ExitsLoader)  # SafeLoader subclass
    except LimitsError:
        raise ExitsError(f"{source}: a number is not a finite decimal") from None
    except (yaml.YAMLError, UnicodeDecodeError):
        raise ExitsError(f"{source} is not valid YAML") from None
    if not isinstance(raw, dict):
        raise ExitsError(f"{source} must hold a mapping")
    # Edges and the fraction must be YAML numbers (0.03, 1), never strings or booleans
    # that pydantic would coerce.
    for name in ("exit_edge", "exit_edge_election_night", "max_exit_quantity_fraction"):
        value = raw.get(name)
        if name in raw and (isinstance(value, bool) or not isinstance(value, Decimal | int)):
            raise ExitsError(f"{source}: {name} must be a number such as 0.03")
        if isinstance(value, int) and not isinstance(value, bool):
            raw[name] = Decimal(value)
    try:
        return ExitSettings.model_validate(raw)
    except ValueError as exc:
        raise ExitsError(f"{source} failed validation: {exc}") from None


def load_exits(path: Path = DEFAULT_PATH) -> ExitSettings | None:
    """None when exits.yaml is absent (no exits); ExitsError when it is invalid."""
    path = Path(path)
    if not path.exists():
        return None
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ExitsError(f"cannot read {path}: {type(exc).__name__}") from None
    return parse_exits(data, str(path))


@dataclass
class ExitsConfig:
    """Re-reads exits.yaml every loop. `settings` is None whenever exits are off."""

    path: Path = DEFAULT_PATH
    settings: ExitSettings | None = None
    last_reentry_minutes: int | None = None  # kept when the file goes away or breaks
    _bad_sha: str | None = None

    def tick(self, error: Callable[[str], None]) -> ExitSettings | None:
        """Re-read the file. An invalid one turns exits off and calls `error` once."""
        path = Path(self.path)
        try:
            data = path.read_bytes() if path.exists() else None
        except OSError as exc:
            data, why = b"", f"cannot read {path}: {type(exc).__name__}"
        else:
            why = ""
        if data is None:
            self.settings, self._bad_sha = None, None
            return None
        sha = hashlib.sha256(data).hexdigest()
        try:
            if why:
                raise ExitsError(why)
            settings = parse_exits(data, str(path))
        except ExitsError as exc:
            self.settings = None
            if sha != self._bad_sha:
                self._bad_sha = sha
                error(f"exits off: {exc}"[:500])
            return None
        self.settings, self._bad_sha = settings, None
        self.last_reentry_minutes = settings.reentry_block_minutes
        return settings

    def reentry_minutes(self) -> int | None:
        """The re-entry block in force: the current file's, else the last valid one's."""
        if self.settings is not None:
            return self.settings.reentry_block_minutes
        return self.last_reentry_minutes
