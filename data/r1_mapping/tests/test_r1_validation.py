"""Validator behaviour on small synthetic worlds: one test per failure class."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from data.r1_mapping.sig_inventory import audit_inventory
from data.r1_mapping.tests.synthetic import (
    DATE,
    MARKETS,
    World,
    make_world,
    sig_market,
    two_pages,
    write_pages,
)


@pytest.fixture
def world(tmp_path: Path) -> World:
    return make_world(tmp_path)


def where_of(world: World, code: str) -> list[str]:
    return [f.where for f in world.validate().findings if f.code == code]


# --- baseline ---------------------------------------------------------------------------

def test_fresh_skeleton_is_structurally_valid_and_all_unresolved(world: World) -> None:
    assert world.validate().findings == []
    assert {r["venues"][v]["decision"] for r in world.mapping["records"]
            for v in ("polymarket", "kalshi")} == {"unresolved"}


def test_true_match_with_full_evidence_passes(world: World) -> None:
    world.true_match("258", "polymarket", "630692")
    world.verify_match("258", "kalshi", "SENATEGA-26-D")
    assert world.validate().findings == []


def test_verified_match_fails_closed_without_the_venue_snapshot(world: World) -> None:
    world.true_match()
    assert "venue_snapshot_unavailable" in world.codes(with_snapshot=False)


# --- inventory and pagination -----------------------------------------------------------

def test_broken_cursor_chain_and_presented_complete(tmp_path: Path) -> None:
    markets = [sig_market(*m) for m in MARKETS]
    pages, _ = two_pages(markets)
    source = write_pages(tmp_path, pages, [None, "not-the-next-cursor"])
    codes = {f.code for f in audit_inventory(source, tmp_path).findings}
    assert {"inventory_cursor_chain_broken", "inventory_incomplete_presented_as_complete"} <= codes


def test_stopped_walk_is_truncated(tmp_path: Path) -> None:
    markets = [sig_market(*m) for m in MARKETS]
    pages, cursors = two_pages(markets)
    source = write_pages(tmp_path, pages[:1], cursors[:1])
    audit = audit_inventory(source, tmp_path)
    codes = {f.code for f in audit.findings}
    assert audit.complete is False
    assert {"inventory_truncated", "inventory_count_mismatch"} <= codes


def test_duplicate_market_across_pages_and_total_mismatch(tmp_path: Path) -> None:
    markets = [sig_market(*m) for m in MARKETS]
    pages, cursors = two_pages(markets + [copy.deepcopy(markets[0])])
    source = write_pages(tmp_path, pages, cursors)
    codes = {f.code for f in audit_inventory(source, tmp_path).findings}
    assert {"inventory_duplicate_market_id", "inventory_count_mismatch"} <= codes


def test_page_bytes_changed_after_declaration(tmp_path: Path, world: World) -> None:
    page = world.root / world.sources["sig_inventory"]["run_dir"] / \
        world.sources["sig_inventory"]["pages"][0]["file"]
    page.write_bytes(page.read_bytes().replace(b"Georgia", b"Georgie"))
    assert "inventory_page_hash_mismatch" in world.codes()


def test_no_option_exchange_is_an_invalid_yes_no_association(tmp_path: Path) -> None:
    markets = [sig_market(*m) for m in MARKETS]
    markets[1]["exchanges"][0]["option"] = "NO"
    pages, cursors = two_pages(markets)
    source = write_pages(tmp_path, pages, cursors)
    codes = {f.code for f in audit_inventory(source, tmp_path).findings}
    assert "invalid_yes_no_association" in codes


def test_mapping_record_with_no_option(world: World) -> None:
    world.record("258")["sig"]["option"] = "NO"
    assert {"invalid_yes_no_association", "sig_record_mismatch"} <= world.codes()


# --- classifications vs inventory ------------------------------------------------------

def test_duplicate_missing_and_extra_classifications(world: World) -> None:
    records = world.mapping["records"]
    records.insert(2, copy.deepcopy(records[1]))
    world.mapping["records"] = [r for r in records if r["sig"]["market_id"] != "318"]
    extra = copy.deepcopy(world.record("354"))
    extra["sig"].update({"market_id": "999", "exchange_id": "9999"})
    world.mapping["records"].append(extra)
    codes = world.codes()
    assert {"duplicate_sig_contract", "missing_sig_contract", "extra_sig_contract"} <= codes
    assert any("sig=318" in w for w in where_of(world, "missing_sig_contract"))


def test_records_must_stay_in_stable_order(world: World) -> None:
    world.mapping["records"].reverse()
    assert "records_not_sorted" in world.codes()


def test_missing_evidence_entry_is_reported(world: World) -> None:
    world.evidence["entries"] = world.evidence["entries"][1:]
    assert "missing_sig_contract" in world.codes()


# --- race identity and groups -----------------------------------------------------------

def test_contradictory_district_is_rejected(world: World) -> None:
    world.record("318")["identity"]["proposed"]["district"] = "01"
    assert {"identity_contradicts_sig_title", "race_key_inconsistent"} <= world.codes()


def test_chamber_control_cannot_be_grouped_as_a_state_race(world: World) -> None:
    proposed = world.record("153")["identity"]["proposed"]
    proposed["race_key"] = "senate:GA"
    world.record("153")["groups"]["proposals"] = ["chamber:house", "party-outcome:D"]
    codes = world.codes()
    assert {"race_key_inconsistent", "group_proposals_inconsistent"} <= codes


def test_special_and_regular_contracts_for_one_seat_are_not_one_race(world: World) -> None:
    world.verify_identity("258", "race-ga", election_type="special")
    world.verify_identity("259", "race-ga", election_type="regular")
    codes = world.codes()
    assert "special_election_grouping" in codes


def test_sibling_contracts_need_one_race_id(world: World) -> None:
    world.verify_identity("258", "race-ga-1")
    world.verify_identity("259", "race-ga-2")
    assert "inconsistent_sibling_race_assignment" in world.codes()
    world.verify_identity("259", "race-ga-1")
    assert world.codes() == set()


def test_verified_identity_needs_reviewed_evidence(world: World) -> None:
    world.verify_identity("258", "race-ga")
    world.verify_identity("259", "race-ga")
    world.record("258")["identity"]["authoritative"]["reviewed_by"] = "Claude Code"
    assert "missing_race_identity_evidence" in world.codes()


def test_group_approval_while_oq_c4_open(world: World) -> None:
    world.record("258")["groups"]["approved"] = ["chamber:senate"]
    assert "group_assigned_while_oq_c4_open" in world.codes()


# --- resolution evidence ----------------------------------------------------------------

def test_verified_match_without_info_tab_text(world: World) -> None:
    world.verify_identity("258", "race-ga")
    world.verify_identity("259", "race-ga")
    world.verify_match("258", "polymarket", "630692")
    assert "verified_without_sig_resolution" in world.codes()


def test_captured_text_that_changed_or_is_missing(world: World) -> None:
    world.capture_info_tab("258", "original text\n")
    (world.root / "markets/sig_info_tab/258.md").write_text("edited\n", encoding="utf-8")
    world.evidence_entry("259").update({"status": "captured_unreviewed",
                                        "text_path": "markets/sig_info_tab/259.md"})
    codes = world.codes()
    assert {"evidence_text_hash_mismatch", "evidence_text_missing",
            "evidence_provenance_missing"} <= codes


def test_unreviewed_capture_does_not_support_a_match(world: World) -> None:
    world.true_match()
    world.capture_info_tab("258", "Resolves YES if the Democratic Party nominee wins.\n",
                           reviewed=False)
    assert "verified_without_sig_resolution" in world.codes()


# --- equivalence criteria ---------------------------------------------------------------

def _set_criterion(decision: dict, criterion: str, status: str, note: str | None = None) -> None:
    decision["equivalence"][criterion].update({"status": status, "note": note})


def test_swearing_in_versus_election_win(world: World) -> None:
    decision = world.true_match("258", "kalshi", "SENATEGA-26-D")
    _set_criterion(decision, "proposition", "different")
    assert "settlement_condition_mismatch" in world.codes()


def test_special_versus_regular_election(world: World) -> None:
    decision = world.true_match()
    _set_criterion(decision, "election_type", "different")
    assert "election_type_mismatch" in world.codes()


def test_party_versus_named_candidate(world: World) -> None:
    decision = world.true_match()
    _set_criterion(decision, "outcome_subject", "different")
    assert "outcome_subject_mismatch" in world.codes()


def test_named_candidate_venue_record_cannot_be_verified_for_a_party(world: World) -> None:
    world.capture_info_tab("276", "Resolves YES if the Independent Party candidate wins.\n")
    world.verify_identity("276", "race-mt")
    world.verify_identity("274", "race-mt")
    world.verify_match("276", "kalshi", "SENATEMT-26-IND")
    findings = [f for f in world.validate().findings if f.code == "venue_record_discrepancy"]
    assert findings and "named_candidate_or_non_party_subject" in findings[0].message


@pytest.mark.parametrize("criterion", ["resolution_source", "timing"])
def test_divergent_source_or_timing_must_be_explained(world: World, criterion: str) -> None:
    decision = world.true_match()
    _set_criterion(decision, criterion, "different")
    assert "unexplained_timing_or_source_difference" in world.codes()
    _set_criterion(decision, criterion, "different_explained", None)
    assert "unexplained_timing_or_source_difference" in world.codes()
    _set_criterion(decision, criterion, "different_explained",
                   "Venue closes earlier; both settle on the certified winner.")
    assert world.codes() == set()


def test_missing_criterion_or_evidence(world: World) -> None:
    decision = world.true_match()
    del decision["equivalence"]["jurisdiction"]
    decision["equivalence"]["timing"]["venue_evidence"] = ""
    assert "verified_without_equivalence_review" in world.codes()


# --- polarity and venue evidence -------------------------------------------------------

def test_reversed_polymarket_polarity(world: World) -> None:
    world.true_match()
    world.record("258")["venues"]["polymarket"]["match"]["outcome_label"] = "No"
    codes = world.codes()
    assert {"reversed_polarity", "token_polarity_mismatch"} <= codes


def test_complement_token_is_rejected(world: World) -> None:
    world.true_match()
    world.record("258")["venues"]["polymarket"]["match"]["token_id"] = "6306922"
    assert "token_polarity_mismatch" in world.codes()


def test_reversed_kalshi_side(world: World) -> None:
    world.true_match("258", "kalshi", "SENATEGA-26-D")
    world.record("258")["venues"]["kalshi"]["match"]["side"] = "no"
    assert "reversed_polarity" in world.codes()


def test_other_party_question_is_contradictory(world: World) -> None:
    world.true_match("258", "polymarket", "630693")
    assert "contradictory_party_outcome" in world.codes()


def test_close_but_wrong_district(world: World) -> None:
    world.capture_info_tab("318", "Resolves YES if the Democratic nominee wins AL-02.\n")
    world.verify_identity("318", "race-al-02")
    world.verify_match("318", "polymarket", "3390043")
    assert "jurisdiction_mismatch" in world.codes()


def test_inconsistent_venue_metadata_is_never_matched(world: World) -> None:
    # SENATELA-26-D: Louisiana ticker, Kentucky title and rules.
    world.capture_info_tab("258", "Resolves YES if the Democratic nominee wins.\n")
    world.verify_identity("258", "race-ga")
    world.verify_identity("259", "race-ga")
    world.verify_match("258", "kalshi", "SENATELA-26-D")
    codes = world.codes()
    assert {"venue_record_discrepancy", "jurisdiction_mismatch"} <= codes


def test_match_fields_must_equal_the_snapshot(world: World) -> None:
    world.true_match()
    world.record("258")["venues"]["polymarket"]["match"]["question"] = "Edited question?"
    assert "venue_evidence_mismatch" in world.codes()


def test_unknown_venue_record(world: World) -> None:
    world.true_match()
    world.record("258")["venues"]["polymarket"]["match"]["market_id"] = "000"
    assert "venue_record_not_found" in world.codes()


# --- conflicting assignments ------------------------------------------------------------

def test_many_to_one_venue_outcome(world: World) -> None:
    world.true_match("258", "polymarket", "630692")
    world.capture_info_tab("259", "Resolves YES if the Republican nominee wins.\n")
    world.verify_match("259", "polymarket", "630692")
    codes = world.codes()
    assert {"many_to_one_venue_outcome", "contradictory_party_outcome"} <= codes


def test_documented_shared_outcome_exception_suppresses_only_many_to_one(world: World) -> None:
    world.true_match("258", "polymarket", "630692")
    world.capture_info_tab("259", "Resolves YES if the Republican nominee wins.\n")
    world.verify_match("259", "polymarket", "630692")
    exception = {"id": "EX-1", "reason": "reviewed shared outcome", "reviewed_by": "Arnav"}
    world.record("258")["venues"]["polymarket"]["shared_outcome_exception"] = exception
    assert "many_to_one_venue_outcome" in world.codes()
    world.record("259")["venues"]["polymarket"]["shared_outcome_exception"] = dict(exception)
    codes = world.codes()
    assert "many_to_one_venue_outcome" not in codes
    assert "contradictory_party_outcome" in codes


def test_unknown_decision_and_missing_venue(world: World) -> None:
    world.record("258")["venues"]["polymarket"]["decision"] = "probably_match"
    del world.record("259")["venues"]["kalshi"]
    assert {"invalid_decision", "missing_venue_decision"} <= world.codes()


def test_unresolved_needs_a_defined_reason(world: World) -> None:
    decision = world.record("258")["venues"]["kalshi"]
    decision["reason_codes"] = ["mystery"]
    assert "unresolved_without_reason" in world.codes()
    decision["reason"] = "Kalshi lists two GA Senate events; awaiting Arnav."
    assert world.codes() == set()


# --- review --------------------------------------------------------------------------

def test_agent_cannot_approve(world: World) -> None:
    decision = world.true_match()
    decision["review"]["reviewer"] = "Claude Code"
    assert "decision_not_reviewed" in world.codes()


def test_low_confidence_or_open_discrepancy_blocks_verification(world: World) -> None:
    decision = world.true_match()
    decision["confidence"] = "low"
    decision["discrepancies"] = [{"code": "close_time", "detail": "x", "status": "open"}]
    assert {"verified_low_confidence", "verified_with_open_discrepancy"} <= world.codes()


def test_numeric_confidence_is_not_policy(world: World) -> None:
    world.record("258")["venues"]["kalshi"]["confidence"] = 0.93
    assert "invalid_confidence" in world.codes()


def test_changed_decision_needs_history(world: World) -> None:
    world.true_match()
    world.record("258")["history"] = world.record("258")["history"][:1]
    assert "history_missing" in world.codes()


# --- no-match scope ---------------------------------------------------------------------

def _no_match(world: World, mid: str, sources: list[dict]) -> dict:
    decision = world.record(mid)["venues"]["kalshi"]
    decision.update({
        "decision": "no_match_in_reviewed_scope",
        "searched_scope": {"sources": sources,
                           "coverage_limits": "Kalshi listing as of the review date only."},
        "review": {"status": "approved", "reviewer": "Arnav", "reviewed_at": DATE},
    })
    decision.pop("reason_codes", None)
    world.record(mid)["history"].append({"at": DATE, "by": "Arnav", "change": "no_match"})
    return decision


def test_absence_from_incomplete_snapshot_is_not_a_no_match(world: World) -> None:
    _no_match(world, "354", [{"kind": "frozen_snapshot", "ref": "venue-synthetic",
                              "complete_for_scope": True}])
    assert "no_match_from_incomplete_snapshot" in world.codes()
    _no_match(world, "354", [{"kind": "frozen_snapshot", "ref": "venue-synthetic",
                              "complete_for_scope": False}])
    assert "no_match_from_incomplete_snapshot" in world.codes()


def test_scoped_no_match_with_a_reviewed_complete_listing(world: World) -> None:
    _no_match(world, "354", [
        {"kind": "frozen_snapshot", "ref": "venue-synthetic", "complete_for_scope": False},
        {"kind": "venue_listing_review", "ref": "Kalshi Senate series list, FL",
         "complete_for_scope": True, "evidence": "reviewed every open FL Senate event",
         "reviewed_by": "Arnav", "reviewed_at": DATE},
    ])
    assert world.codes() == set()


def test_no_match_without_scope_or_review(world: World) -> None:
    decision = _no_match(world, "354", [])
    decision["review"] = {"status": "pending"}
    assert {"no_match_scope_missing", "decision_not_reviewed"} <= world.codes()


# --- fair value --------------------------------------------------------------------------

def test_mapping_edit_enabling_fair_value_on_unresolved_record(world: World) -> None:
    world.record("258")["venues"]["polymarket"]["fair_value_eligible"] = True
    assert "fair_value_enabled_without_evidence" in world.codes()


def test_fair_value_needs_an_approved_confidence_rule(world: World) -> None:
    decision = world.true_match()
    decision["fair_value_eligible"] = True
    assert "fair_value_enabled_without_evidence" in world.codes()
    world.sources["policies"]["fair_value_confidence_rule"] = {
        "status": "approved", "approved_by": "Arnav", "approved_at": DATE,
        "eligible_confidence": ["high"]}
    assert world.codes() == set()
    decision["confidence"] = "medium"
    assert "fair_value_enabled_without_evidence" in world.codes()


def test_rule_approved_by_an_agent_is_not_approved(world: World) -> None:
    world.sources["policies"]["fair_value_confidence_rule"] = {
        "status": "approved", "approved_by": "Claude Code", "approved_at": DATE,
        "eligible_confidence": ["high"]}
    assert "unapproved_policy" in world.codes()


def test_fair_value_flag_must_be_boolean(world: World) -> None:
    world.record("258")["venues"]["kalshi"]["fair_value_eligible"] = "true"
    assert "invalid_fair_value_flag" in world.codes()


def test_snapshot_cannot_claim_complete_coverage_without_evidence(world: World) -> None:
    world.sources["venue_snapshots"][0]["coverage"]["kalshi"]["complete"] = True
    assert "snapshot_coverage_unsupported" in world.codes()
