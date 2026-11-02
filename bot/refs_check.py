"""Print reference prices for every approved contract. Arnav runs this.

    python -m bot.refs_check

Calls only the public Kalshi and Polymarket endpoints, never SIG, and uses no keys.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import yaml

from bot.mapping import MAP_PATH, load_map
from bot.refs import RefCollector, RefQuote, RefSettings, fair_value

RISK_PATH = Path(__file__).resolve().parent.parent / "config" / "risk.yaml"


def _px(q: RefQuote | None) -> str:
    if q is None:
        return "-/-"
    f = lambda d: "-" if d is None else f"{d:.3f}"
    if q.bid is None and q.ask is None and q.mid is not None:
        return f"mid {q.mid:.3f}"
    return f"{f(q.bid)}/{f(q.ask)}" + ("" if q.status == "active" else f" [{q.status}]")


def _line(race: str, party: str, k: RefQuote | None, p: RefQuote | None,
          fair: Decimal | None, reason: str) -> str:
    fv = "   -  " if fair is None else f"{fair:.4f}"
    return f"{race:<16} {party}  K {_px(k):<22} P {_px(p):<22} fair {fv}  {reason}"


async def run(map_path: Path, risk_path: Path) -> int:
    contracts = load_map(map_path)
    if not contracts:
        print("no approved contracts in the map")
        return 1
    risk = yaml.safe_load(risk_path.read_text(encoding="utf-8"))
    settings = RefSettings.from_mapping(risk["strategies"]["reference"])
    async with RefCollector() as collector:
        snap = await collector.fetch_all(
            [c.kalshi_ticker for c in contracts],
            [c.polymarket_token_id for c in contracts if c.polymarket_token_id])
    now = datetime.now(UTC)
    for c in contracts:
        k = snap.kalshi.get(c.kalshi_ticker)
        p = snap.polymarket.get(c.polymarket_token_id) if c.polymarket_token_id else None
        fair, reason = fair_value(k, p, settings, now)
        print(_line(c.race_key, c.party, k, p, fair, reason))
    for venue, err in snap.errors.items():
        print(f"ERROR {venue}: {err}", file=sys.stderr)
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--map", type=Path, default=MAP_PATH)
    ap.add_argument("--config", type=Path, default=RISK_PATH)
    args = ap.parse_args()
    sys.exit(asyncio.run(run(args.map, args.config)))


if __name__ == "__main__":
    main()
