"""Load and validate config/risk.yaml (Arnav-only file; this module never writes it).

Every key is required and nothing has a default, so a missing or out-of-range value
refuses to start. YAML floats are read as exact Decimals from their source text
("0.10" -> Decimal("0.10")), never through float.

Scale: `effective_cap` multiplies per_order, per_race, total_committed and loss_stop by
`scale`. min_cash_reserve is NOT scaled: scaling it down would loosen the reserve, so
the unscaled (tighter) value is used until Arnav says otherwise.

BOT-ROTATE (all optional; absent = the behaviour before it):
- target_deployment: total committed may reach at most this fraction of equity
  (balance + total committed). 1.0 adds no limit beyond cash and the caps.
- max_race_fraction: one race's committed cost may reach at most this fraction of
  equity (balance + total committed), on top of caps.per_race.
- rotation: take-profit and edge-gone exits, reinvest ranking and the re-entry
  cooldown (bot/strategy.py, bot/main.py). Exits still need config/exits.yaml enabled.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from engine.config import MIDTERM_ELECTIONS

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "risk.yaml"

SCALED_CAPS = frozenset({"per_order", "per_race", "total_committed", "loss_stop"})


class LimitsError(ValueError):
    """config/risk.yaml is missing, malformed or out of range."""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Caps(_Frozen):
    per_order: Decimal = Field(gt=0)
    per_race: Decimal = Field(gt=0)
    total_committed: Decimal = Field(gt=0)
    min_cash_reserve: Decimal = Field(ge=0)
    loss_stop: Decimal = Field(gt=0)


class ComplementSettings(_Frozen):
    enabled: bool
    max_sum_ask: Decimal = Field(gt=0, lt=1)
    min_sum_bid: Decimal = Field(gt=1, lt=2)


class ReferenceSettings(_Frozen):
    enabled: bool
    edge: Decimal = Field(gt=0, lt=1)
    edge_election_night: Decimal = Field(gt=0, lt=1)
    election_night_from_utc: datetime
    max_ref_spread: Decimal = Field(gt=0, lt=1)
    max_ref_age_s: int = Field(gt=0, le=3600)
    max_venue_disagreement: Decimal = Field(gt=0, lt=1)

    @field_validator("election_night_from_utc")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("election_night_from_utc must carry a UTC offset (end in Z)")
        return value


class RotationSettings(_Frozen):
    enabled: bool
    take_profit_pct: Decimal = Field(gt=0, lt=1)
    edge_gone: bool
    reinvest_min_free_fraction: Decimal = Field(ge=0, lt=1)
    reentry_cooldown_minutes: int = Field(ge=0, le=1440)


class Strategies(_Frozen):
    complement: ComplementSettings
    reference: ReferenceSettings


class Limits(_Frozen):
    mode: Literal["dry_run", "live"]
    tournament_id: str
    scale: Decimal = Field(gt=0, le=1)
    caps: Caps
    start_balance: Decimal | None = Field(gt=0)
    preexisting_committed: Decimal = Field(ge=0)
    # Bellwether's write budget is 25/min (CLAUDE.md); never above it.
    max_writes_per_min: int = Field(ge=1, le=25)
    order_expiry_seconds: int = Field(ge=10, le=3600)
    no_orders_enabled: bool
    no_price_convention: Literal["unknown", "yes_price", "no_price"]
    loop_seconds: int = Field(ge=1, le=300)
    strategies: Strategies
    target_deployment: Decimal | None = Field(default=None, gt=0, le=1)
    max_race_fraction: Decimal | None = Field(default=None, gt=0, le=1)
    rotation: RotationSettings | None = None

    @property
    def rotating(self) -> bool:
        return self.rotation is not None and self.rotation.enabled

    @field_validator("tournament_id")
    @classmethod
    def _configured_tournament(cls, value: str) -> str:
        if value != MIDTERM_ELECTIONS.id:
            raise ValueError("tournament_id must be the midterm-elections tournament id")
        return value

    def effective_cap(self, name: str) -> Decimal:
        """cap x scale for the scaled caps; min_cash_reserve is returned unscaled."""
        value = getattr(self.caps, name)
        return value * self.scale if name in SCALED_CAPS else value

    def with_step(self, scale: Decimal, caps: Caps) -> Limits:
        """A new frozen Limits with only scale and caps replaced (bot/ladder.py steps).

        The step's scale and caps are validated by bot/ladder.py before this is called.
        """
        return self.model_copy(update={"scale": scale, "caps": caps})


class _DecimalLoader(yaml.SafeLoader):
    """SafeLoader that reads floats as exact Decimals from their source text."""


def _decimal(loader: yaml.SafeLoader, node: yaml.ScalarNode) -> Decimal:
    text = loader.construct_scalar(node)
    try:
        value = Decimal(str(text).replace("_", ""))
    except InvalidOperation:
        raise LimitsError("a number in risk.yaml is not a finite decimal") from None
    if not value.is_finite():
        raise LimitsError("a number in risk.yaml is not finite")
    return value


_DecimalLoader.add_constructor("tag:yaml.org,2002:float", _decimal)


def load_limits(path: Path = DEFAULT_PATH) -> Limits:
    """Read and validate risk.yaml, or raise LimitsError (the bot must not start)."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise LimitsError(f"cannot read {path}: {type(exc).__name__}") from None
    try:
        raw = yaml.load(text, Loader=_DecimalLoader)  # SafeLoader subclass
    except yaml.YAMLError:
        raise LimitsError(f"{path} is not valid YAML") from None
    if not isinstance(raw, dict):
        raise LimitsError(f"{path} must hold a mapping")
    try:
        return Limits.model_validate(raw)
    except ValueError as exc:
        raise LimitsError(f"{path} failed validation: {exc}") from None
