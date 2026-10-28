"""Offline validator for the committed R1 mapping files.

Structural failures (this module's findings) are kept apart from legitimate ``unresolved``
decisions and from explicitly scoped ``no_match_in_reviewed_scope`` decisions, which are
counted by the report, not raised as errors.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from data.r1_mapping.candidates import analyse_kalshi, analyse_polymarket
from data.r1_mapping.common import (
    EVIDENCE_FORMAT,
    INFO_TAB_DIR,
    MAPPING_FORMAT,
    REPO_ROOT,
    SOURCES_FORMAT,
    VENUES,
    Finding,
    sha256_text_lf,
)
from data.r1_mapping.identity import (
    CYCLES,
    ELECTION_TYPES,
    proposed_group_ids,
    proposed_identity,
    race_key,
)
from data.r1_mapping.sig_inventory import InventoryAudit

DECISIONS = ("verified_match", "no_match_in_reviewed_scope", "unresolved")
REVIEW_STATUSES = ("pending", "approved", "rejected")
CONFIDENCE_LEVELS = ("high", "medium", "low")
EVIDENCE_STATUSES = ("missing", "captured_unreviewed", "reviewed")
DISCREPANCY_STATUSES = ("open", "resolved")

# Equivalence criteria a reviewer must record for a verified match. STRICT criteria must
# be "same"; EXPLAINABLE criteria may differ only with a written explanation.
STRICT_CRITERIA = (
    "event_and_office",
    "jurisdiction",
    "election_cycle_and_term",
    "election_type",
    "outcome_subject",
    "outcome_polarity",
    "proposition",
    "settlement_condition",
)
EXPLAINABLE_CRITERIA = ("resolution_source", "timing")
CRITERIA = STRICT_CRITERIA + EXPLAINABLE_CRITERIA
CRITERION_STATUSES = ("same", "different_explained", "different", "unknown")

MATCH_FIELDS = {
    "polymarket": ("market_id", "event_id", "question", "outcome_label", "token_id",
                   "end_date", "rules_sha256"),
    "kalshi": ("market_ticker", "event_ticker", "series_ticker", "title", "side",
               "close_time", "rules_sha256"),
}
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?Z?)?$")


@dataclass
class ValidationResult:
    findings: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.findings

    def add(self, code: str, where: str, message: str) -> None:
        self.findings.append(Finding(code, where, message))


def _is_date(value: Any) -> bool:
    return isinstance(value, str) and bool(_DATE_RE.match(value))


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_all(
    sources: dict[str, Any],
    mapping: dict[str, Any],
    evidence: dict[str, Any],
    inventory: InventoryAudit,
    venue_snapshots: dict[str, dict[str, Any]] | None = None,
    root: Path = REPO_ROOT,
) -> ValidationResult:
    """Validate every committed R1 file against the audited SIG inventory.

    ``venue_snapshots`` maps a declared snapshot id to its loaded JSON. Each one must
    already have been hash-checked by the caller (see ``cli.load_venue_snapshots``).
    """
    result = ValidationResult()
    result.findings.extend(inventory.findings)
    _check_sources(sources, result)
    evidence_by_id = _check_evidence(evidence, sources, inventory, result, root)
    _check_mapping(mapping, sources, inventory, evidence_by_id, venue_snapshots or {}, result)
    result.findings.sort()
    return result


def authorized_reviewers(sources: dict[str, Any]) -> set[str]:
    reviewers = (sources.get("policies") or {}).get("authorized_reviewers") or []
    return {r for r in reviewers if _nonempty(r)}


def snapshot_ids(sources: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {s.get("id"): s for s in sources.get("venue_snapshots") or [] if s.get("id")}


def _check_sources(sources: dict[str, Any], result: ValidationResult) -> None:
    where = "sources"
    if sources.get("format") != SOURCES_FORMAT:
        result.add("wrong_format", where, f"expected format {SOURCES_FORMAT}")
    ids = [s.get("id") for s in sources.get("venue_snapshots") or []]
    if len(ids) != len(set(ids)) or not all(ids):
        result.add("duplicate_snapshot_id", where, "venue snapshot ids must be unique and set")
    for snap in sources.get("venue_snapshots") or []:
        swhere = f"sources:venue_snapshots:{snap.get('id')}"
        if not re.fullmatch(r"[0-9a-f]{64}", str(snap.get("sha256") or "")):
            result.add("snapshot_hash_missing", swhere, "sha256 must be a 64-hex digest")
        for venue in VENUES:
            cov = (snap.get("coverage") or {}).get(venue)
            if not isinstance(cov, dict) or not isinstance(cov.get("complete"), bool):
                result.add("snapshot_coverage_undeclared", swhere,
                           f"coverage.{venue}.complete must be declared true or false")
            elif cov["complete"] and not _nonempty(cov.get("evidence")):
                result.add("snapshot_coverage_unsupported", swhere,
                           f"{venue} coverage claimed complete without evidence")
    policies = sources.get("policies") or {}
    rule = policies.get("fair_value_confidence_rule") or {}
    if rule.get("status") == "approved":
        if not (rule.get("approved_by") in authorized_reviewers(sources)
                and _is_date(rule.get("approved_at"))):
            result.add("unapproved_policy", "sources:policies:fair_value_confidence_rule",
                       "an approved rule needs an authorized approver and a date")
        if not set(rule.get("eligible_confidence") or []) <= set(CONFIDENCE_LEVELS):
            result.add("unapproved_policy", "sources:policies:fair_value_confidence_rule",
                       "eligible_confidence must use high/medium/low")
    oq = policies.get("oq_c4_related_market_groups") or {}
    if oq.get("status") == "decided" and not (
        oq.get("decided_by") in authorized_reviewers(sources) and _is_date(oq.get("decided_at"))
    ):
        result.add("unapproved_policy", "sources:policies:oq_c4_related_market_groups",
                   "a decided OQ-C4 needs an authorized decider and a date")


def _check_evidence(
    evidence: dict[str, Any],
    sources: dict[str, Any],
    inventory: InventoryAudit,
    result: ValidationResult,
    root: Path,
) -> dict[str, dict[str, Any]]:
    where = "evidence"
    if evidence.get("format") != EVIDENCE_FORMAT:
        result.add("wrong_format", where, f"expected format {EVIDENCE_FORMAT}")
    if evidence.get("inventory_id") != inventory.source_id:
        result.add("inventory_id_mismatch", where, "evidence file names another SIG inventory")
    reviewers = authorized_reviewers(sources)
    by_id: dict[str, dict[str, Any]] = {}
    inv = inventory.by_market_id
    for i, entry in enumerate(evidence.get("entries") or []):
        mid = str(entry.get("sig_market_id"))
        ewhere = f"evidence:entries[{i}] sig={mid}"
        if mid in by_id:
            result.add("duplicate_sig_contract", ewhere, "second evidence entry for this market")
            continue
        by_id[mid] = entry
        contract = inv.get(mid)
        if contract is None:
            result.add("extra_sig_contract", ewhere, "market is not in the selected inventory")
            continue
        if entry.get("sig_exchange_id") != contract["exchange_id"]:
            result.add("sig_record_mismatch", ewhere, "exchange id differs from the inventory")
        status = entry.get("status")
        if status not in EVIDENCE_STATUSES:
            result.add("invalid_evidence_status", ewhere, f"status {status!r}")
            continue
        if status == "missing":
            if entry.get("text_path") or entry.get("text_sha256"):
                result.add("evidence_status_inconsistent", ewhere,
                           "status missing but a text file is referenced")
            continue
        path = entry.get("text_path")
        if not (isinstance(path, str) and path.startswith(INFO_TAB_DIR + "/")
                and path.endswith(".md")):
            result.add("evidence_text_missing", ewhere,
                       f"{status} evidence needs text_path under {INFO_TAB_DIR}/")
        elif not (root / path).is_file():
            result.add("evidence_text_missing", ewhere, f"{path} does not exist")
        elif sha256_text_lf((root / path).read_text(encoding="utf-8")) != entry.get("text_sha256"):
            result.add("evidence_text_hash_mismatch", ewhere, f"{path} differs from text_sha256")
        for key in ("source", "captured_by"):
            if not _nonempty(entry.get(key)):
                result.add("evidence_provenance_missing", ewhere, f"{key} is required")
        if not _is_date(entry.get("captured_at")):
            result.add("evidence_provenance_missing", ewhere, "captured_at must be a date")
        if status == "reviewed" and not (
            entry.get("reviewed_by") in reviewers and _is_date(entry.get("reviewed_at"))
        ):
            result.add("evidence_review_missing", ewhere,
                       "reviewed evidence needs an authorized reviewer and a date")
    for mid in sorted(set(inv) - set(by_id), key=int):
        result.add("missing_sig_contract", f"evidence sig={mid}",
                   "inventory market has no resolution-evidence entry")
    return by_id


def _check_mapping(
    mapping: dict[str, Any],
    sources: dict[str, Any],
    inventory: InventoryAudit,
    evidence_by_id: dict[str, dict[str, Any]],
    snapshots: dict[str, dict[str, Any]],
    result: ValidationResult,
) -> None:
    if mapping.get("format") != MAPPING_FORMAT:
        result.add("wrong_format", "mapping", f"expected format {MAPPING_FORMAT}")
    if mapping.get("inventory_id") != inventory.source_id:
        result.add("inventory_id_mismatch", "mapping", "mapping names another SIG inventory")
    inv = inventory.by_market_id
    records = mapping.get("records") or []
    seen: dict[str, str] = {}
    seen_exchange: dict[str, str] = {}
    order: list[int] = []
    valid_records: list[tuple[str, dict[str, Any]]] = []
    for i, record in enumerate(records):
        sig = record.get("sig") or {}
        mid = str(sig.get("market_id"))
        where = f"mapping:records[{i}] sig={mid}"
        if mid in seen:
            result.add("duplicate_sig_contract", where, f"also classified at {seen[mid]}")
            continue
        seen[mid] = where
        exchange = str(sig.get("exchange_id"))
        if exchange in seen_exchange:
            result.add("duplicate_sig_contract", where,
                       f"exchange {exchange} also classified at {seen_exchange[exchange]}")
        seen_exchange[exchange] = where
        order.append(int(mid) if mid.isdigit() else -1)
        contract = inv.get(mid)
        if contract is None:
            result.add("extra_sig_contract", where, "market is not in the selected inventory")
            continue
        for key, inv_key in (("exchange_id", "exchange_id"), ("option", "option"),
                             ("title", "title")):
            if sig.get(key) != contract[inv_key]:
                result.add("sig_record_mismatch", where,
                           f"{key} {sig.get(key)!r} differs from inventory "
                           f"{contract[inv_key]!r}")
        if sig.get("option") != "YES":
            result.add("invalid_yes_no_association", where,
                       "a SIG contract is the market's YES exchange; option must be 'YES'")
        valid_records.append((where, record))
    if order != sorted(order):
        result.add("records_not_sorted", "mapping",
                   "records must be ordered by numeric SIG market id for stable diffs")
    for mid in sorted(set(inv) - set(seen), key=int):
        result.add("missing_sig_contract", f"mapping sig={mid}",
                   "inventory market has no classification record")

    for where, record in valid_records:
        _check_identity(where, record, sources, result)
        _check_groups(where, record, sources, result)
        for venue in VENUES:
            _check_decision(where, venue, record, sources, evidence_by_id, snapshots, result,
                            mapping.get("reason_code_definitions"))
        _check_history(where, record, result)
    _check_siblings(valid_records, result)
    _check_venue_usage(valid_records, result)


def _check_identity(where: str, record: dict[str, Any], sources: dict[str, Any],
                    result: ValidationResult) -> None:
    identity = record.get("identity") or {}
    title = (record.get("sig") or {}).get("title")
    expected = proposed_identity(title)
    proposed = identity.get("proposed")
    status = identity.get("status")
    if status not in ("unresolved", "verified"):
        result.add("invalid_identity_status", where, f"identity.status {status!r}")
    if expected is None:
        if proposed is not None:
            result.add("identity_contradicts_sig_title", where,
                       "title has no recognised shape, so no proposal may be recorded")
        if status != "unresolved":
            result.add("missing_race_identity_evidence", where,
                       "an unparsed title cannot carry a verified identity")
        return
    if not isinstance(proposed, dict):
        result.add("missing_race_identity_evidence", where, "identity.proposed is missing")
        return
    for key in ("basis", "shape", "office", "state", "district", "party"):
        if proposed.get(key) != expected[key]:
            result.add("identity_contradicts_sig_title", where,
                       f"proposed.{key} {proposed.get(key)!r} but the SIG title gives "
                       f"{expected[key]!r}")
    if proposed.get("election_type") not in ELECTION_TYPES:
        result.add("invalid_identity_field", where, "proposed.election_type is not allowed")
    if proposed.get("cycle") not in CYCLES:
        result.add("invalid_identity_field", where, "proposed.cycle is not allowed")
    canonical = race_key(proposed.get("office"), proposed.get("state"),
                         proposed.get("district"), proposed.get("election_type") or "")
    if proposed.get("race_key") != canonical:
        result.add("race_key_inconsistent", where,
                   f"race_key {proposed.get('race_key')!r}, fields give {canonical!r}")
    if status == "verified":
        auth = identity.get("authoritative")
        if not isinstance(auth, dict):
            result.add("missing_race_identity_evidence", where,
                       "verified identity needs an authoritative block")
            return
        for key in ("race_id", "source"):
            if not _nonempty(auth.get(key)):
                result.add("missing_race_identity_evidence", where, f"authoritative.{key}")
        if not (auth.get("reviewed_by") in authorized_reviewers(sources)
                and _is_date(auth.get("reviewed_at"))):
            result.add("missing_race_identity_evidence", where,
                       "authoritative identity needs an authorized reviewer and a date")
        for key in ("office", "state", "district", "election_type", "cycle"):
            if key in auth and auth[key] != proposed.get(key):
                result.add("identity_fields_contradictory", where,
                           f"authoritative.{key} {auth[key]!r} vs proposed "
                           f"{proposed.get(key)!r}")
        if proposed.get("election_type") == "unverified" or proposed.get("cycle") != "2026":
            result.add("missing_race_identity_evidence", where,
                       "a verified identity must settle election_type and cycle")
    elif identity.get("authoritative") is not None:
        result.add("identity_status_inconsistent", where,
                   "authoritative data present but identity.status is not verified")


def _check_groups(where: str, record: dict[str, Any], sources: dict[str, Any],
                  result: ValidationResult) -> None:
    groups = record.get("groups") or {}
    proposed = (record.get("identity") or {}).get("proposed")
    expected = proposed_group_ids(proposed if isinstance(proposed, dict) else None)
    if groups.get("proposals") != expected:
        result.add("group_proposals_inconsistent", where,
                   f"proposals {groups.get('proposals')} but identity gives {expected}")
    oq = ((sources.get("policies") or {}).get("oq_c4_related_market_groups") or {})
    if groups.get("approved") and oq.get("status") != "decided":
        result.add("group_assigned_while_oq_c4_open", where,
                   "approved groups require OQ-C4 to be decided first")
    if groups.get("status") != ("decided" if oq.get("status") == "decided" else
                                "unresolved_oq_c4"):
        result.add("group_status_inconsistent", where,
                   "groups.status must be unresolved_oq_c4 while OQ-C4 is open")


def _check_decision(
    where: str,
    venue: str,
    record: dict[str, Any],
    sources: dict[str, Any],
    evidence_by_id: dict[str, dict[str, Any]],
    snapshots: dict[str, dict[str, Any]],
    result: ValidationResult,
    reason_definitions: dict[str, str] | None = None,
) -> None:
    vwhere = f"{where} venue={venue}"
    reason_definitions = reason_definitions or {}
    decision = (record.get("venues") or {}).get(venue)
    if not isinstance(decision, dict):
        result.add("missing_venue_decision", vwhere, "no decision recorded for this venue")
        return
    kind = decision.get("decision")
    if kind not in DECISIONS:
        result.add("invalid_decision", vwhere, f"decision {kind!r}")
        return
    declared = snapshot_ids(sources)
    snap_id = decision.get("venue_snapshot")
    if snap_id not in declared:
        result.add("unknown_venue_snapshot", vwhere, f"venue_snapshot {snap_id!r} not declared")
    review = decision.get("review") or {}
    if review.get("status") not in REVIEW_STATUSES:
        result.add("invalid_review", vwhere, f"review.status {review.get('status')!r}")
    approved = (review.get("status") == "approved"
                and review.get("reviewer") in authorized_reviewers(sources)
                and _is_date(review.get("reviewed_at")))
    confidence = decision.get("confidence")
    if confidence not in (None, *CONFIDENCE_LEVELS):
        result.add("invalid_confidence", vwhere,
                   f"confidence {confidence!r}; numbers are not a policy, use high/medium/low")
    discrepancies = decision.get("discrepancies") or []
    open_discrepancies = []
    for j, d in enumerate(discrepancies):
        if d.get("status") not in DISCREPANCY_STATUSES or not _nonempty(d.get("detail")):
            result.add("invalid_discrepancy", f"{vwhere} discrepancy[{j}]",
                       "needs detail and status open/resolved")
        if d.get("status") == "open":
            open_discrepancies.append(d)
        if d.get("status") == "resolved" and not _nonempty(d.get("resolution")):
            result.add("invalid_discrepancy", f"{vwhere} discrepancy[{j}]",
                       "a resolved discrepancy must say how")

    if kind == "unresolved":
        if decision.get("match") is not None:
            result.add("decision_fields_inconsistent", vwhere,
                       "an unresolved decision must not carry a match")
        codes = decision.get("reason_codes") or []
        undefined = [c for c in codes if not _nonempty(reason_definitions.get(c))]
        if not codes or (undefined and not _nonempty(decision.get("reason"))):
            result.add("unresolved_without_reason", vwhere,
                       "unresolved needs reason_codes, each defined in "
                       "reason_code_definitions or explained in a reason")
    elif kind == "no_match_in_reviewed_scope":
        _check_no_match(vwhere, venue, decision, declared, approved, result)
    else:
        _check_verified(vwhere, venue, record, decision, evidence_by_id, snapshots,
                        approved, open_discrepancies, result)

    eligible = decision.get("fair_value_eligible")
    if not isinstance(eligible, bool):
        result.add("invalid_fair_value_flag", vwhere, "fair_value_eligible must be true/false")
    elif eligible:
        _check_fair_value(vwhere, record, decision, sources, evidence_by_id,
                          open_discrepancies, result)


def _check_no_match(vwhere: str, venue: str, decision: dict[str, Any],
                    declared: dict[str, dict[str, Any]], approved: bool,
                    result: ValidationResult) -> None:
    if decision.get("match") is not None:
        result.add("decision_fields_inconsistent", vwhere, "no_match must not carry a match")
    if not approved:
        result.add("decision_not_reviewed", vwhere,
                   "no_match needs an approved review by an authorized reviewer")
    scope = decision.get("searched_scope") or {}
    sources_searched = scope.get("sources") or []
    if not sources_searched or not _nonempty(scope.get("coverage_limits")):
        result.add("no_match_scope_missing", vwhere,
                   "list the sources searched and acknowledge coverage limits")
        return
    complete_source = False
    for s in sources_searched:
        if not (_nonempty(s.get("ref")) and _nonempty(s.get("kind"))):
            result.add("no_match_scope_missing", vwhere, "each searched source needs kind/ref")
            continue
        if s.get("kind") == "frozen_snapshot":
            snap = declared.get(s.get("ref")) or {}
            snap_complete = ((snap.get("coverage") or {}).get(venue) or {}).get("complete")
            if s.get("complete_for_scope") and not snap_complete:
                result.add("no_match_from_incomplete_snapshot", vwhere,
                           f"snapshot {s.get('ref')!r} is not complete for {venue}; absence "
                           "from it is unresolved, not a no-match")
                continue
            complete_source = complete_source or bool(s.get("complete_for_scope"))
        elif s.get("complete_for_scope"):
            if not (_nonempty(s.get("reviewed_by")) and _is_date(s.get("reviewed_at"))
                    and _nonempty(s.get("evidence"))):
                result.add("no_match_scope_missing", vwhere,
                           "a complete-listing source needs evidence, reviewer and date")
                continue
            complete_source = True
    if not complete_source:
        result.add("no_match_from_incomplete_snapshot", vwhere,
                   "no searched source is complete for this scope; record unresolved instead")


def _check_verified(
    vwhere: str,
    venue: str,
    record: dict[str, Any],
    decision: dict[str, Any],
    evidence_by_id: dict[str, dict[str, Any]],
    snapshots: dict[str, dict[str, Any]],
    approved: bool,
    open_discrepancies: list[dict[str, Any]],
    result: ValidationResult,
) -> None:
    mid = str((record.get("sig") or {}).get("market_id"))
    if not approved:
        result.add("decision_not_reviewed", vwhere,
                   "verified_match needs an approved review by an authorized reviewer")
    if (evidence_by_id.get(mid) or {}).get("status") != "reviewed":
        result.add("verified_without_sig_resolution", vwhere,
                   "SIG Info-tab resolution text is not captured and reviewed")
    if decision.get("confidence") not in ("high", "medium"):
        result.add("verified_low_confidence", vwhere,
                   "verified_match needs high or medium confidence")
    if open_discrepancies:
        result.add("verified_with_open_discrepancy", vwhere,
                   f"{len(open_discrepancies)} open discrepancy(ies)")
    match = decision.get("match")
    if not isinstance(match, dict):
        result.add("verified_without_venue_evidence", vwhere, "match block is missing")
        return
    for key in MATCH_FIELDS[venue]:
        if not _nonempty(match.get(key)):
            result.add("verified_without_venue_evidence", vwhere, f"match.{key} is required")
    if venue == "polymarket" and match.get("outcome_label") != "Yes":
        result.add("reversed_polarity", vwhere,
                   f"outcome_label {match.get('outcome_label')!r}; the SIG YES contract maps "
                   "only to the venue's Yes outcome of a same-party question")
    if venue == "kalshi" and match.get("side") != "yes":
        result.add("reversed_polarity", vwhere,
                   f"side {match.get('side')!r}; the SIG YES contract maps only to Kalshi YES")
    _check_equivalence(vwhere, decision.get("equivalence"), result)
    snap = snapshots.get(decision.get("venue_snapshot"))
    if snap is None:
        result.add("venue_snapshot_unavailable", vwhere,
                   "the declared venue snapshot was not supplied, so the match cannot be "
                   "re-checked offline")
        return
    _check_against_snapshot(vwhere, venue, record, match, snap, result)


def _check_equivalence(vwhere: str, equivalence: Any, result: ValidationResult) -> None:
    if not isinstance(equivalence, dict):
        result.add("verified_without_equivalence_review", vwhere, "equivalence is missing")
        return
    for criterion in CRITERIA:
        entry = equivalence.get(criterion)
        cwhere = f"{vwhere} criterion={criterion}"
        if not isinstance(entry, dict) or entry.get("status") not in CRITERION_STATUSES:
            result.add("verified_without_equivalence_review", cwhere,
                       "criterion missing or status invalid")
            continue
        if not (_nonempty(entry.get("sig_evidence")) and _nonempty(entry.get("venue_evidence"))):
            result.add("verified_without_equivalence_review", cwhere,
                       "both sig_evidence and venue_evidence are required")
        status = entry["status"]
        if criterion in STRICT_CRITERIA and status != "same":
            code = {
                "proposition": "settlement_condition_mismatch",
                "settlement_condition": "settlement_condition_mismatch",
                "outcome_polarity": "reversed_polarity",
                "outcome_subject": "outcome_subject_mismatch",
                "election_type": "election_type_mismatch",
                "jurisdiction": "jurisdiction_mismatch",
            }.get(criterion, "equivalence_mismatch")
            result.add(code, cwhere, f"must be 'same' for a verified match, got {status!r}")
        if criterion in EXPLAINABLE_CRITERIA:
            if status in ("different", "unknown"):
                result.add("unexplained_timing_or_source_difference", cwhere,
                           f"{status!r}; explain the difference against both rules or fail")
            if status == "different_explained" and not _nonempty(entry.get("note")):
                result.add("unexplained_timing_or_source_difference", cwhere,
                           "different_explained needs a note")


def _check_against_snapshot(vwhere: str, venue: str, record: dict[str, Any],
                            match: dict[str, Any], snap: dict[str, Any],
                            result: ValidationResult) -> None:
    proposed = (record.get("identity") or {}).get("proposed") or {}
    if venue == "polymarket":
        rows = [r for r in snap.get("polymarket") or []
                if str(r.get("market_id")) == str(match.get("market_id"))]
        if len(rows) != 1:
            result.add("venue_record_not_found", vwhere,
                       f"market {match.get('market_id')} not in the snapshot exactly once")
            return
        row = rows[0]
        token_map = row.get("outcome_token_map") or {}
        if token_map.get(match.get("outcome_label")) != match.get("token_id"):
            result.add("token_polarity_mismatch", vwhere,
                       "token_id is not the snapshot's token for outcome_label")
        feats = analyse_polymarket(row)
        pairs = (("question", "question"), ("event_id", "event_id"), ("end_date", "end_date"),
                 ("rules_sha256", "rules_sha256"))
    else:
        rows = [r for r in snap.get("kalshi") or []
                if r.get("market_ticker") == match.get("market_ticker")]
        if len(rows) != 1:
            result.add("venue_record_not_found", vwhere,
                       f"ticker {match.get('market_ticker')} not in the snapshot exactly once")
            return
        feats = analyse_kalshi(rows[0])
        pairs = (("title", "title"), ("event_ticker", "event_ticker"),
                 ("series_ticker", "series_ticker"), ("close_time", "close_time"),
                 ("rules_sha256", "rules_sha256"))
    for key, entry_key in pairs:
        if match.get(key) != feats["entry"].get(entry_key):
            result.add("venue_evidence_mismatch", vwhere,
                       f"match.{key} differs from the snapshot record")
    if feats["party"] != proposed.get("party"):
        result.add("contradictory_party_outcome", vwhere,
                   f"venue outcome party {feats['party']!r}, SIG {proposed.get('party')!r}")
    if feats["state"] != proposed.get("state"):
        result.add("jurisdiction_mismatch", vwhere,
                   f"venue state {feats['state']!r}, SIG {proposed.get('state')!r}")
    if proposed.get("office") == "house" and feats["district"] != proposed.get("district"):
        result.add("jurisdiction_mismatch", vwhere,
                   f"venue district {feats['district']!r}, SIG {proposed.get('district')!r}")
    if feats["issues"]:
        result.add("venue_record_discrepancy", vwhere,
                   f"venue record has unresolved issues: {', '.join(sorted(feats['issues']))}")


def _check_fair_value(vwhere: str, record: dict[str, Any], decision: dict[str, Any],
                      sources: dict[str, Any], evidence_by_id: dict[str, dict[str, Any]],
                      open_discrepancies: list[dict[str, Any]],
                      result: ValidationResult) -> None:
    problems = []
    if decision.get("decision") != "verified_match":
        problems.append(f"decision is {decision.get('decision')}")
    if (record.get("identity") or {}).get("status") != "verified":
        problems.append("race identity is not verified")
    mid = str((record.get("sig") or {}).get("market_id"))
    if (evidence_by_id.get(mid) or {}).get("status") != "reviewed":
        problems.append("SIG Info-tab text is not reviewed")
    rule = (sources.get("policies") or {}).get("fair_value_confidence_rule") or {}
    if rule.get("status") != "approved":
        problems.append("no approved confidence rule")
    elif decision.get("confidence") not in (rule.get("eligible_confidence") or []):
        problems.append(f"confidence {decision.get('confidence')!r} not eligible under the rule")
    if open_discrepancies:
        problems.append("open discrepancies")
    if problems:
        result.add("fair_value_enabled_without_evidence", vwhere, "; ".join(problems))


def _check_history(where: str, record: dict[str, Any], result: ValidationResult) -> None:
    history = record.get("history")
    if not isinstance(history, list) or not history:
        result.add("history_missing", where, "every record needs a change history")
        return
    dates = []
    for k, entry in enumerate(history):
        if not (_is_date(entry.get("at")) and _nonempty(entry.get("by"))
                and _nonempty(entry.get("change"))):
            result.add("history_invalid", f"{where} history[{k}]", "needs at, by and change")
        dates.append(str(entry.get("at")))
    if dates != sorted(dates):
        result.add("history_invalid", where, "history entries must be in date order")
    reviewed = any(
        ((record.get("venues") or {}).get(v) or {}).get("decision") != "unresolved"
        for v in VENUES
    )
    if reviewed and len(history) < 2:
        result.add("history_missing", where, "a changed decision needs its own history entry")


def _check_siblings(records: list[tuple[str, dict[str, Any]]], result: ValidationResult) -> None:
    by_key: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    by_race_id: dict[str, set[str]] = defaultdict(set)
    seat_types: dict[str, set[str]] = defaultdict(set)
    for where, record in records:
        identity = record.get("identity") or {}
        proposed = identity.get("proposed")
        if not isinstance(proposed, dict):
            continue
        by_key[str(proposed.get("race_key"))].append((where, record))
        seat = race_key(proposed.get("office"), proposed.get("state"), proposed.get("district"))
        seat_types[seat].add(str(proposed.get("election_type")))
        auth = identity.get("authoritative") or {}
        if identity.get("status") == "verified" and _nonempty(auth.get("race_id")):
            by_race_id[auth["race_id"]].add(str(proposed.get("race_key")))
    for key, members in sorted(by_key.items()):
        parties = [(m[1]["identity"]["proposed"].get("party"), m[0]) for m in members]
        seen_parties: dict[str, str] = {}
        for party, where in parties:
            if party in seen_parties:
                result.add("conflicting_sibling_contracts", where,
                           f"race {key} already has a {party} contract at {seen_parties[party]}")
            seen_parties[party] = where
        types = {m[1]["identity"]["proposed"].get("election_type") for m in members}
        if len(types) > 1:
            result.add("inconsistent_sibling_race_assignment", f"race={key}",
                       f"siblings disagree on election_type: {sorted(map(str, types))}")
        statuses = {m[1]["identity"].get("status") for m in members}
        race_ids = {(m[1]["identity"].get("authoritative") or {}).get("race_id")
                    for m in members}
        if "verified" in statuses and (len(statuses) > 1 or len(race_ids) > 1):
            result.add("inconsistent_sibling_race_assignment", f"race={key}",
                       "siblings must share one verified authoritative race id")
    for seat, types in sorted(seat_types.items()):
        if "special" in types and len(types) > 1:
            result.add("special_election_grouping", f"seat={seat}",
                       f"contracts for this seat disagree on election type {sorted(types)}; "
                       "a special and a regular election are different races")
    for race_id, keys in sorted(by_race_id.items()):
        if len(keys) > 1:
            result.add("inconsistent_sibling_race_assignment", f"race_id={race_id}",
                       f"one authoritative race id spans proposed races {sorted(keys)}")


def _check_venue_usage(records: list[tuple[str, dict[str, Any]]],
                       result: ValidationResult) -> None:
    usage: dict[tuple[str, str, str], list[tuple[str, Any]]] = defaultdict(list)
    for where, record in records:
        for venue in VENUES:
            decision = (record.get("venues") or {}).get(venue) or {}
            if decision.get("decision") != "verified_match":
                continue
            match = decision.get("match") or {}
            if venue == "polymarket":
                key = (venue, str(match.get("market_id")), str(match.get("token_id")))
            else:
                key = (venue, str(match.get("market_ticker")), str(match.get("side")))
            usage[key].append((f"{where} venue={venue}",
                               decision.get("shared_outcome_exception")))
    for key, users in sorted(usage.items()):
        if len(users) < 2:
            continue
        exceptions = [u[1] for u in users]
        documented = all(
            isinstance(e, dict) and _nonempty(e.get("id")) and _nonempty(e.get("reason"))
            and _nonempty(e.get("reviewed_by")) for e in exceptions
        ) and len({e["id"] for e in exceptions}) == 1
        if not documented:
            for where, _ in users:
                result.add("many_to_one_venue_outcome", where,
                           f"{key[0]} outcome {key[1]}/{key[2]} is mapped by {len(users)} SIG "
                           "contracts without one documented, reviewed exception")
