"""Small synthetic R1 world: recorded-style SIG pages, sources, venue snapshot.

All values are invented placeholders shaped like the committed fixtures and the S1-T5
output schema. Nothing here was produced by calling SIG, Polymarket or Kalshi.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from data.r1_mapping.candidates import analyse_kalshi, analyse_polymarket
from data.r1_mapping.cli import build_skeletons
from data.r1_mapping.common import (
    EVIDENCE_LAYOUT,
    MAPPING_LAYOUT,
    dump_layout,
    sha256_bytes,
    sha256_text_lf,
)
from data.r1_mapping.identity import race_key
from data.r1_mapping.sig_inventory import audit_inventory
from data.r1_mapping.validate import CRITERIA, ValidationResult, validate_all

RUN_DIR = "fixtures/sig/recorded/synthetic-run"
SNAPSHOT_ID = "venue-synthetic"
DATE = "2026-10-28"

MARKETS = [
    ("153", "842", "Will the Democratic Party win the U.S. Senate?", "Freeform"),
    ("258", "947", "Will the Democratic Party win the Georgia Senate?", "Election Outcome"),
    ("259", "948", "Will the Republican Party win the Georgia Senate?", "Election Outcome"),
    ("274", "963", "Will the Democratic Party win the Montana Senate?", "Election Outcome"),
    ("276", "965", "Will the Independent Party win the Montana Senate?", "Election Outcome"),
    ("318", "1007", "Will the Democratic Party win the AL-02 House race?", "Election Outcome"),
    ("354", "1043", "Will the Democratic Party win the Florida Senate?", "Election Outcome"),
]

PM_RULES_GA = (
    "This market will resolve according to the winner of the 2026 midterm Georgia U.S. "
    "Senate election, inclusive of any run-offs.\n\nA candidate shall be considered to "
    "represent a party in the event that he or she is the nominee of the party in question."
)


def sig_market(mid: str, eid: str, title: str, category: str, option: str = "YES"
               ) -> dict[str, Any]:
    return {
        "id": mid, "title": title, "thumbnailUrl": "https://example.invalid/x.webp",
        "status": "open", "createdAt": "2026-08-24T20:41:36.184Z",
        "settlementDate": "2026-11-04T17:00:00.000Z", "settledWith": None, "settledOn": None,
        "categories": [category], "isComposite": False, "isMultiOutcome": False,
        "exchanges": [{"id": eid, "option": option, "latestPrice": 0.5, "initialPrice": 0.5}],
        "creator": {"id": "11111111-1111-4111-8111-111111111111",
                    "username": "redacted-creator-1"},
    }


def pm(market_id: str, question: str, rules: str, event_id: str = "1",
       yes: str | None = None, no: str | None = None) -> dict[str, Any]:
    yes = yes or f"{market_id}1"
    no = no or f"{market_id}2"
    return {
        "venue": "polymarket", "market_id": market_id, "question": question,
        "slug": question.lower().replace(" ", "-"), "outcomes": ["Yes", "No"],
        "clob_token_ids": [yes, no], "outcome_token_map": {"Yes": yes, "No": no},
        "snapshot_prices": {"is_snapshot": True}, "end_date": "2026-11-04T04:59:00Z",
        "resolution_description": rules, "event_id": event_id,
        "event_title": "Synthetic event", "event_slug": "synthetic-event", "race_type": "senate",
    }


def kalshi(ticker: str, event: str, series: str, title: str, rules: str,
           subtitle: str = "") -> dict[str, Any]:
    return {
        "venue": "kalshi", "market_ticker": ticker, "event_ticker": event,
        "series_ticker": series, "title": title, "subtitle": subtitle,
        "snapshot_prices": {"is_snapshot": True}, "close_time": "2027-11-03T15:00:00Z",
        "rules_text": rules, "filter_note": None, "race_type": "senate",
    }


def k_senate(st: str, state: str, party: str, suffix: str) -> dict[str, Any]:
    return kalshi(
        f"SENATE{st}-26-{suffix}", f"SENATE{st}-26", f"SENATE{st}",
        f"Will {party}s win the Senate race in {state}?",
        f"If a representative of the {party} party is sworn in as a Senator of {state} for the "
        "term beginning in 2027, then the market resolves to Yes.\nThis market is eligible "
        "for accelerated determination.",
    )


def snapshot() -> dict[str, Any]:
    return {
        "fetched_at": "2026-10-11T18:37:01+00:00",
        "polymarket": [
            pm("630692", "Will the Democrats win the Georgia Senate race in 2026?", PM_RULES_GA),
            pm("630693", "Will the Republicans win the Georgia Senate race in 2026?",
               PM_RULES_GA),
            pm("3390043", "Will the Democratic Party candidate win the 2026 AL-01 House "
               "election?", "This market will resolve according to the party of the winner."),
            pm("631044", "Will the Democrats win the Florida Senate race in 2026?",
               "This market will resolve according to the winner of the 2026 Florida U.S. "
               "Senate special election, inclusive of any run-offs."),
            pm("562793", "Will the Democratic Party control the Senate after the 2026 Midterm "
               "elections?", "This market will resolve according to the party that wins "
               "control of the United States Senate in the 2026 United States midterm election."),
            pm("3343192", "Will the Democratic Party candidate win the 2026 Georgia Senate "
               "election by 3% or more?", "Margin market."),
        ],
        "kalshi": [
            k_senate("GA", "Georgia", "Democratic", "D"),
            k_senate("GA", "Georgia", "Republican", "R"),
            k_senate("MT", "Montana", "Democratic", "D"),
            kalshi("SENATEMT-26-IND", "SENATEMT-26", "SENATEMT",
                   "Will Independents win the Senate race in Montana?",
                   "If Seth Bodnar (as an Independent) is sworn in as a Senator of Montana for "
                   "the term beginning in 2027, then the market resolves to Yes."),
            kalshi("SENATELA-26-D", "SENATELA-26", "SENATELA",
                   "Will Democratics win the Senate race in Kentucky?",
                   "If a representative of the Democratic party is sworn in as a Senator of "
                   "Kentucky for the term beginning in 2027, then the market resolves to Yes."),
            kalshi("CONTROLS-2026-D", "CONTROLS-2026", "CONTROLS",
                   "Will Democrats win the U.S. Senate in 2026?",
                   "If the Democratic Party has won control of the U.S. Senate in 2026, then "
                   "the market resolves to Yes. This market may be determined early."),
        ],
    }


def write_json(path: Path, obj: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(obj, indent=1).encode("utf-8")
    path.write_bytes(data)
    return sha256_bytes(data)


def write_pages(root: Path, pages: list[dict[str, Any]], cursors: list[str | None]
                ) -> dict[str, Any]:
    """Write recorded-style pages plus fixtures.json; return the sources block."""
    run = root / RUN_DIR
    entries, declared = [], []
    for i, (body, cursor) in enumerate(zip(pages, cursors, strict=True)):
        name = f"bodies/{i + 2:02d}-tournament-markets-p{i + 1}-a1.json"
        digest = write_json(run / name, body)
        params = {"limit": "100", "status": "any"}
        if cursor is not None:
            params["cursor"] = cursor
        entries.append({"name": f"p{i + 1}", "file": name, "sha256": digest,
                        "request": {"method": "GET", "route": "GET /tournaments/{slug}/markets",
                                    "path": "/tournaments/midterm-elections/markets",
                                    "params": params}})
        declared.append({"name": f"p{i + 1}", "file": name, "sha256": digest})
    manifest_hash = write_json(run / "fixtures.json", {"fixtures": entries})
    return {"id": "sig-synthetic", "run_dir": RUN_DIR, "fixture_manifest_sha256": manifest_hash,
            "tournament_slug": "midterm-elections", "pages": declared,
            "declared_complete": True}


def two_pages(markets: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str | None]]:
    half = len(markets) // 2
    total = len(markets)
    p1 = {"data": markets[:half],
          "pagination": {"total": total, "limit": 100, "hasMore": True, "nextCursor": "c2"}}
    p2 = {"data": markets[half:],
          "pagination": {"total": total, "limit": 100, "hasMore": False, "nextCursor": None}}
    return [p1, p2], [None, "c2"]


@dataclass
class World:
    root: Path
    sources: dict[str, Any]
    mapping: dict[str, Any]
    evidence: dict[str, Any]
    snapshot: dict[str, Any]

    def inventory(self):
        return audit_inventory(self.sources["sig_inventory"], self.root)

    def validate(self, with_snapshot: bool = True) -> ValidationResult:
        snaps = {SNAPSHOT_ID: self.snapshot} if with_snapshot else {}
        return validate_all(self.sources, self.mapping, self.evidence, self.inventory(), snaps,
                            self.root)

    def codes(self, with_snapshot: bool = True) -> set[str]:
        return {f.code for f in self.validate(with_snapshot).findings}

    def record(self, mid: str) -> dict[str, Any]:
        return next(r for r in self.mapping["records"] if r["sig"]["market_id"] == mid)

    def evidence_entry(self, mid: str) -> dict[str, Any]:
        return next(e for e in self.evidence["entries"] if e["sig_market_id"] == mid)

    def write_repo_files(self) -> None:
        markets = self.root / "markets"
        write_json(markets / "r1_sources.json", self.sources)
        (markets / "r1_mapping.json").write_text(dump_layout(self.mapping, MAPPING_LAYOUT))
        (markets / "sig_resolution_evidence.json").write_text(
            dump_layout(self.evidence, EVIDENCE_LAYOUT))

    # --- edits a reviewer would make -------------------------------------------------

    def capture_info_tab(self, mid: str, text: str, reviewed: bool = True) -> None:
        rel_path = f"markets/sig_info_tab/{mid}.md"
        path = self.root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        self.evidence_entry(mid).update({
            "status": "reviewed" if reviewed else "captured_unreviewed",
            "text_path": rel_path, "text_sha256": sha256_text_lf(text),
            "source": f"SIG Info tab, market {mid}", "captured_by": "Arnav",
            "captured_at": DATE,
            "reviewed_by": "Arnav" if reviewed else None,
            "reviewed_at": DATE if reviewed else None,
        })

    def verify_identity(self, mid: str, race_id: str, election_type: str = "regular") -> None:
        identity = self.record(mid)["identity"]
        identity["proposed"]["election_type"] = election_type
        identity["proposed"]["cycle"] = "2026"
        p = identity["proposed"]
        p["race_key"] = race_key(p["office"], p["state"], p["district"], election_type)
        identity["status"] = "verified"
        identity["authoritative"] = {"race_id": race_id, "source": "reviewed SIG race export",
                                     "reviewed_by": "Arnav", "reviewed_at": DATE}

    def verify_match(self, mid: str, venue: str, venue_id: str,
                     outcome: str = "Yes") -> dict[str, Any]:
        """Turn the decision into a fully evidenced verified match."""
        if venue == "polymarket":
            row = next(r for r in self.snapshot["polymarket"] if r["market_id"] == venue_id)
            entry = analyse_polymarket(row)["entry"]
            match = {k: entry[k] for k in ("market_id", "event_id", "question", "end_date",
                                           "rules_sha256")}
            match.update({"outcome_label": outcome,
                          "token_id": row["outcome_token_map"][outcome]})
        else:
            row = next(r for r in self.snapshot["kalshi"] if r["market_ticker"] == venue_id)
            entry = analyse_kalshi(row)["entry"]
            match = {k: entry[k] for k in ("market_ticker", "event_ticker", "series_ticker",
                                           "title", "close_time", "rules_sha256")}
            match["side"] = "yes" if outcome == "Yes" else "no"
        decision = self.record(mid)["venues"][venue]
        decision.update({
            "decision": "verified_match",
            "match": match,
            "confidence": "high",
            "equivalence": {
                c: {"status": "same", "sig_evidence": "SIG Info tab says ...",
                    "venue_evidence": "venue rules say ...", "note": None}
                for c in CRITERIA
            },
            "review": {"status": "approved", "reviewer": "Arnav", "reviewed_at": DATE},
        })
        decision.pop("reason_codes", None)
        self.record(mid)["history"].append({"at": DATE, "by": "Arnav", "change": "verified",
                                            "detail": f"{venue} {venue_id}"})
        return decision

    def true_match(self, mid: str = "258", venue: str = "polymarket",
                   venue_id: str = "630692") -> dict[str, Any]:
        self.capture_info_tab(mid, "Resolves YES if the Democratic Party nominee wins.\n")
        self.verify_identity(mid, "race-ga-senate")
        sibling = {"258": "259", "259": "258"}.get(mid)
        if sibling:
            self.verify_identity(sibling, "race-ga-senate")
        return self.verify_match(mid, venue, venue_id)


def make_world(root: Path, markets: list[dict[str, Any]] | None = None) -> World:
    markets = markets or [sig_market(*m) for m in MARKETS]
    pages, cursors = two_pages(markets)
    sig_source = write_pages(root, pages, cursors)
    snap = snapshot()
    snap_hash = write_json(root / "data/venue_markets/output/synthetic.json", snap)
    sources = {
        "format": "bellwether.r1-sources/1",
        "sig_inventory": sig_source,
        "venue_snapshots": [{
            "id": SNAPSHOT_ID, "file": "data/venue_markets/output/synthetic.json",
            "sha256": snap_hash, "fetched_at": snap["fetched_at"],
            "s1t5_validation": {"result": "fail"},
            "coverage": {"polymarket": {"complete": False, "basis": "tag-filtered"},
                         "kalshi": {"complete": False, "basis": "selected series"}},
        }],
        "policies": {
            "authorized_reviewers": ["Arnav"],
            "oq_c4_related_market_groups": {"status": "open"},
            "fair_value_confidence_rule": {"status": "not_approved", "eligible_confidence": []},
        },
    }
    inventory = audit_inventory(sig_source, root)
    assert not inventory.findings, inventory.findings
    mapping, evidence = build_skeletons(sources, inventory, SNAPSHOT_ID, DATE)
    return World(root, sources, copy.deepcopy(mapping), copy.deepcopy(evidence), snap)
