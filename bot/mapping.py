"""Load bot/map.yaml: approved contracts in non-excluded races, validated."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

MAP_PATH = Path(__file__).resolve().parent / "map.yaml"
PARTIES = ("D", "R", "I")


class MappingError(ValueError):
    """map.yaml is invalid. The message names the race."""


@dataclass(frozen=True)
class MappedContract:
    race_key: str
    party: str
    sig_market_id: str
    sig_exchange_id: str
    sig_title: str
    kalshi_ticker: str
    polymarket_token_id: str | None


def _numeric(value: Any) -> bool:
    return isinstance(value, str) and value.isascii() and value.isdigit()


def parse_map(data: Any) -> list[MappedContract]:
    if not isinstance(data, dict) or not isinstance(data.get("races"), list):
        raise MappingError("map: top level must be a mapping with a 'races' list")
    out: list[MappedContract] = []
    seen: dict[tuple[str, str], str] = {}
    race_keys: set[str] = set()
    for race in data["races"]:
        key = race.get("race_key") if isinstance(race, dict) else None
        if not isinstance(key, str) or not key:
            raise MappingError(f"map: race without a race_key: {race!r}")
        if key in race_keys:
            raise MappingError(f"{key}: race appears twice")
        race_keys.add(key)
        if not isinstance(race.get("exclude"), bool):
            raise MappingError(f"{key}: 'exclude' must be true or false")
        parties: set[str] = set()
        for con in race.get("contracts") or []:
            party = con.get("party")
            if party not in PARTIES:
                raise MappingError(f"{key}: party must be one of D, R, I, got {party!r}")
            if party in parties:
                raise MappingError(f"{key}: more than one {party} contract")
            parties.add(party)
            for field in ("sig_market_id", "sig_exchange_id"):
                if not _numeric(con.get(field)):
                    raise MappingError(f"{key} {party}: {field} must be a numeric string")
            ticker = con.get("kalshi_ticker")
            if not isinstance(ticker, str) or not ticker:
                raise MappingError(f"{key} {party}: kalshi_ticker missing")
            token = con.get("polymarket_token_id")
            if token is not None and not _numeric(token):
                raise MappingError(f"{key} {party}: polymarket_token_id must be numeric or null")
            if not isinstance(con.get("approved"), bool):
                raise MappingError(f"{key} {party}: 'approved' must be true or false")
            if race["exclude"] or not con["approved"]:
                continue
            for kind, value in (("exchange", con["sig_exchange_id"]), ("kalshi ticker", ticker),
                                ("polymarket token", token)):
                if value is None:
                    continue
                if (kind, value) in seen:
                    raise MappingError(
                        f"{key} {party}: {kind} {value} already used by {seen[(kind, value)]}")
                seen[(kind, value)] = f"{key} {party}"
            out.append(MappedContract(key, party, con["sig_market_id"], con["sig_exchange_id"],
                                      str(con.get("sig_title", "")), ticker, token))
    return out


def load_map(path: Path = MAP_PATH) -> list[MappedContract]:
    return parse_map(yaml.safe_load(path.read_text(encoding="utf-8")))
