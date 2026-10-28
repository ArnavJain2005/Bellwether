"""Checks on the committed R1 files and the committed recorded SIG fixtures."""

from __future__ import annotations

from collections import Counter

import pytest

from data.r1_mapping.cli import render_report, run_validation
from data.r1_mapping.common import REPO_ROOT, VENUES, load_json, sha256_file
from data.r1_mapping.identity import parse_sig_title
from data.r1_mapping.report import G1_DISCLAIMER
from data.r1_mapping.sig_inventory import audit_inventory

SOURCES = load_json(REPO_ROOT / "markets" / "r1_sources.json")


def test_committed_inventory_is_a_complete_cursor_walk() -> None:
    audit = audit_inventory(SOURCES["sig_inventory"], REPO_ROOT)
    assert audit.findings == []
    assert audit.complete is True
    assert [p["items"] for p in audit.pages] == [100, 100, 37]
    assert [p["has_more"] for p in audit.pages] == [True, True, False]
    assert audit.pages[0]["request_cursor"] is None
    assert audit.pages[1]["request_cursor"] == audit.pages[0]["next_cursor"]
    assert audit.pages[2]["request_cursor"] == audit.pages[1]["next_cursor"]
    assert audit.pages[2]["next_cursor"] is None
    assert audit.reported_total == 237
    assert len(audit.contracts) == 237
    assert len({c["exchange_id"] for c in audit.contracts}) == 237
    assert {c["option"] for c in audit.contracts} == {"YES"}
    assert Counter(c["status"] for c in audit.contracts) == {"open": 237}


def test_committed_titles_give_117_proposed_race_keys_not_race_ids() -> None:
    audit = audit_inventory(SOURCES["sig_inventory"], REPO_ROOT)
    parsed = [parse_sig_title(c["title"]) for c in audit.contracts]
    assert all(parsed)
    keys = Counter((p["office"], p["state"], p["district"]) for p in parsed)
    assert len(keys) == 117
    assert Counter(k[0] for k in keys) == {
        "house": 46, "governor": 35, "senate": 34, "senate_control": 1, "house_control": 1}
    assert Counter(keys.values()) == {2: 114, 3: 3}
    independents = sorted(c["title"] for c, p in zip(audit.contracts, parsed, strict=True)
                          if p["party"] == "I")
    assert independents == [
        "Will the Independent Party win the Montana Senate?",
        "Will the Independent Party win the Nebraska Senate?",
        "Will the Independent Party win the Rhode Island Governor?",
    ]


def test_truncated_ten_market_capture_is_rejected_as_complete() -> None:
    run = REPO_ROOT / "tests/fixtures/sig/recorded/20261009T122244Z-fa008a"
    page = "bodies/02-tournament-markets-p1-a1.json"
    source = {
        "id": "sig-fa008a", "run_dir": "tests/fixtures/sig/recorded/20261009T122244Z-fa008a",
        "fixture_manifest_sha256": sha256_file(run / "fixtures.json"),
        "tournament_slug": "midterm-elections",
        "pages": [{"file": page, "sha256": sha256_file(run / page)}],
        "declared_complete": True,
    }
    audit = audit_inventory(source, REPO_ROOT)
    codes = {f.code for f in audit.findings}
    assert audit.complete is False
    assert len(audit.contracts) == 10
    assert {"inventory_truncated", "inventory_count_mismatch",
            "inventory_incomplete_presented_as_complete"} <= codes


def test_committed_mapping_validates_and_claims_nothing() -> None:
    result, ctx = run_validation(REPO_ROOT, [])
    assert result.findings == []
    records = ctx["mapping"]["records"]
    assert len(records) == 237
    for record in records:
        assert record["identity"]["status"] == "unresolved"
        assert record["identity"]["authoritative"] is None
        assert record["groups"]["approved"] == []
        for venue in VENUES:
            decision = record["venues"][venue]
            assert decision["decision"] == "unresolved"
            assert decision["fair_value_eligible"] is False
            assert decision["review"]["status"] == "pending"
    assert {e["status"] for e in ctx["evidence"]["entries"]} == {"missing"}
    policies = ctx["sources"]["policies"]
    assert policies["fair_value_confidence_rule"]["status"] == "not_approved"
    assert policies["oq_c4_related_market_groups"]["status"] == "open"


def test_committed_candidates_point_at_the_declared_snapshot() -> None:
    candidates = load_json(REPO_ROOT / "markets" / "r1_candidates.json")
    snap = SOURCES["venue_snapshots"][0]
    assert candidates["generated_from"]["venue_snapshot"] == snap["id"]
    assert candidates["generated_from"]["venue_snapshot_sha256"] == snap["sha256"]
    ids = [c["sig_market_id"] for c in candidates["contracts"]]
    assert ids == sorted(ids, key=int) and len(ids) == 237


def test_committed_candidates_regenerate_identically_when_snapshot_present() -> None:
    from data.r1_mapping.candidates import dump_candidates, generate_candidates

    snap = SOURCES["venue_snapshots"][0]
    path = REPO_ROOT / snap["file"]
    if not path.is_file() or sha256_file(path) != snap["sha256"]:
        pytest.skip("frozen venue output is git-ignored and not present in this checkout")
    audit = audit_inventory(SOURCES["sig_inventory"], REPO_ROOT)
    regenerated = dump_candidates(
        generate_candidates(audit.contracts, load_json(path), snap, audit.source_id))
    committed = (REPO_ROOT / "markets" / "r1_candidates.json").read_text(encoding="utf-8")
    assert regenerated == committed


def test_committed_report_is_current_and_never_claims_g1() -> None:
    text = render_report(REPO_ROOT)
    committed = (REPO_ROOT / "markets" / "R1_REPORT.md").read_text(encoding="utf-8")
    assert text == committed
    assert G1_DISCLAIMER in text
    lowered = text.lower()
    assert "g-1 complete" not in lowered and "passes g-1" not in lowered
