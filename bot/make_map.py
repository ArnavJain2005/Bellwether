"""Draft bot/map.yaml and bot/map_review.md from markets/r1_candidates.json.

    python -m bot.make_map

Every contract is written with approved: false; Arnav approves by hand. Candidate
suggestions are algorithmic, never evidence (see markets/README.md).
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CANDIDATES_PATH = ROOT / "markets" / "r1_candidates.json"
MAP_PATH = ROOT / "bot" / "map.yaml"
REVIEW_PATH = ROOT / "bot" / "map_review.md"

# Runoff / ranked-choice risk: the race may not settle on election night.
RISKY_STATES = frozenset({"GA", "LA", "AK", "ME"})
PARTY_ORDER = {"D": 0, "R": 1, "I": 2}
_PARTY_RE = re.compile(r"\b(Democratic|Republican|Independent)\b")
_PARTY_LETTER = {"Democratic": "D", "Republican": "R", "Independent": "I"}


def party_from_title(title: str) -> str:
    m = _PARTY_RE.search(title)
    if not m:
        raise ValueError(f"cannot read party from SIG title: {title!r}")
    return _PARTY_LETTER[m.group(1)]


def race_state(race_key: str) -> str | None:
    """'senate:GA' -> 'GA', 'house:ME-02' -> 'ME', 'house_control' -> None."""
    if ":" not in race_key:
        return None
    return race_key.split(":", 1)[1][:2]


def _race_order(race_key: str) -> tuple[int, str]:
    office = race_key.split(":", 1)[0]
    rank = {"senate": 0, "governor": 1, "senate_control": 2, "house_control": 2, "house": 3}
    return rank.get(office, 4), race_key


def _conflict_rows(contract: dict[str, Any]) -> list[str]:
    """Kalshi rows (suggested or near miss) that r1_candidates flags with a conflict."""
    found: list[str] = []
    k = contract["kalshi"]
    for row in k["suggested"]:
        flags = [s for s in row.get("signals", []) if "conflict" in s]
        if flags:
            found.append(f"{row['market_ticker']} ({', '.join(flags)})")
    for row in k["near_misses"]:
        flags = [s for s in row.get("reasons", []) if "conflict" in s]
        if flags:
            found.append(f"{row['id']} ({', '.join(flags)})")
    return found


def build_map(candidates: dict[str, Any]) -> list[dict[str, Any]]:
    """Return race records (map.yaml order). Each has a private '_review' list."""
    by_race: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for c in candidates["contracts"]:
        by_race[c["proposed_race_key"]].append(c)

    races: list[dict[str, Any]] = []
    for race_key in sorted(by_race, key=_race_order):
        group = by_race[race_key]
        notes: list[str] = []
        if len(group) >= 3:
            notes.append(f"{len(group)} SIG contracts in this race")
        state = race_state(race_key)
        if state in RISKY_STATES:
            notes.append(f"{state}: runoff / ranked-choice risk")
        conflicts = sorted({row for c in group for row in _conflict_rows(c)})
        if conflicts:
            notes.append("Kalshi metadata conflict: " + "; ".join(conflicts))

        contracts: list[dict[str, Any]] = []
        review: list[dict[str, Any]] = []
        for c in sorted(group, key=lambda c: PARTY_ORDER[party_from_title(c["sig_title"])]):
            ks = c["kalshi"]["suggested"]
            if len(ks) != 1:
                continue
            ps = c["polymarket"]["suggested"]
            token = None
            if len(ps) == 1 and ps[0].get("outcome_label") == "Yes":
                token = ps[0]["token_id"]
            contracts.append({
                "party": party_from_title(c["sig_title"]),
                "sig_market_id": str(c["sig_market_id"]),
                "sig_exchange_id": str(c["sig_exchange_id"]),
                "sig_title": c["sig_title"],
                "kalshi_ticker": ks[0]["market_ticker"],
                "polymarket_token_id": token,
                "approved": False,
            })
            review.append({"sig_title": c["sig_title"], "kalshi": ks[0],
                           "polymarket": ps[0] if len(ps) == 1 else None,
                           "polymarket_count": len(ps)})
        if not contracts:
            continue
        if len(contracts) < len(group):
            notes.append(f"only {len(contracts)} of {len(group)} SIG contracts mapped")
        exclude = len(group) >= 3 or state in RISKY_STATES or bool(conflicts)
        races.append({"race_key": race_key, "exclude": exclude, "note": "; ".join(notes),
                      "contracts": contracts, "_review": review})
    return races


def render_review(races: list[dict[str, Any]]) -> str:
    out = ["# Race map review", "",
           "Approve a contract by setting `approved: true` in bot/map.yaml.", ""]
    for r in races:
        flag = " (EXCLUDED)" if r["exclude"] else ""
        out.append(f"## {r['race_key']}{flag}")
        if r["note"]:
            out.append(f"_{r['note']}_")
        for con, rev in zip(r["contracts"], r["_review"], strict=True):
            k, p = rev["kalshi"], rev["polymarket"]
            out += ["", f"**{con['party']}** SIG: {rev['sig_title']}",
                    f"- Kalshi `{k['market_ticker']}`: {k['title']}",
                    f"  - rules: {k['rules_lead']}"]
            if p:
                out += [f"- Polymarket: {p['question']}", f"  - rules: {p['rules_lead']}"]
            else:
                out.append(f"- Polymarket: none usable ({rev['polymarket_count']} suggestions)")
            out.append("- approve? set approved: true in bot/map.yaml")
        out.append("")
    return "\n".join(out)


def _refuse_to_erase_approvals() -> None:
    """Regeneration must not reset Arnav's manually approved contracts."""
    if not MAP_PATH.exists():
        return
    try:
        existing = yaml.safe_load(MAP_PATH.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError("existing bot/map.yaml is invalid; refusing to overwrite it") from exc
    if not isinstance(existing, dict) or not isinstance(existing.get("races"), list):
        raise ValueError("existing bot/map.yaml is invalid; refusing to overwrite it")
    for race in existing["races"]:
        if not isinstance(race, dict) or not isinstance(race.get("contracts"), list):
            raise ValueError("existing bot/map.yaml is invalid; refusing to overwrite it")
        for contract in race["contracts"]:
            if not isinstance(contract, dict) or not isinstance(contract.get("approved"), bool):
                raise ValueError("existing bot/map.yaml is invalid; refusing to overwrite it")
            if contract["approved"]:
                raise ValueError(
                    "bot/map.yaml contains manual approvals; refusing to regenerate and erase them"
                )


def main() -> None:
    _refuse_to_erase_approvals()
    candidates = json.loads(CANDIDATES_PATH.read_text(encoding="utf-8"))
    races = build_map(candidates)
    public = [{k: v for k, v in r.items() if k != "_review"} for r in races]
    MAP_PATH.write_text(yaml.safe_dump({"races": public}, sort_keys=False, width=100,
                                       allow_unicode=True), encoding="utf-8")
    REVIEW_PATH.write_text(render_review(races), encoding="utf-8")
    n = sum(len(r["contracts"]) for r in races)
    print(f"wrote {len(races)} races, {n} contracts "
          f"({sum(r['exclude'] for r in races)} races excluded)")


if __name__ == "__main__":
    main()
