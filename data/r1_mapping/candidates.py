"""Reproducible candidate discovery against a frozen S1-T5 venue output.

Candidate discovery only *surfaces* venue contracts for a reviewer. A suggestion is never
evidence of settlement equivalence and is never written into a decision: promotion to
``verified_match`` is a human edit of ``markets/r1_mapping.json`` that the validator
checks against the evidence rules. The S1-T5 ``race_type`` label is not used (the frozen
output misclassifies state races as ``control``); features are read from each record's
own question, tickers and rules text. ``snapshot_prices`` are ignored entirely.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from typing import Any

from data.r1_mapping.common import CANDIDATES_FORMAT, sha256_text_lf
from data.r1_mapping.identity import STATE_CODE_BY_NAME, proposed_identity
from data.venue_markets.fetch_venue_markets import STATE_ABBRS

GENERATOR_VERSION = "r1-candidates/1"
NEAR_MISS_LIMIT = 3

_STATE_NAMES_BY_LENGTH = sorted(STATE_CODE_BY_NAME, key=len, reverse=True)
_STATE_NAME_RE = re.compile(
    r"\b(" + "|".join(re.escape(n) for n in _STATE_NAMES_BY_LENGTH) + r")\b", re.IGNORECASE
)
_DISTRICT_RE = re.compile(r"\b(?P<st>[A-Z]{2})-(?P<d>\d{1,2}|AL)\b")
_YEAR_RE = re.compile(r"\b(20[2-3]\d)\b")
_SENTENCE_END_RE = re.compile(r"(?<!\bU\.S\.)(?<!\bU\.S\.A\.)(?<=[.!?])\s+")

_MARGIN_RE = re.compile(
    r"%|\bmargin\b|\bpopular vote\b|\bvote share\b|\bpercent|\bby \d|\bseats?\b|\bturnout\b",
    re.IGNORECASE,
)
_SUB_JURISDICTION_RE = re.compile(r"\bcounty\b|\bprecinct|\bcity\b|\bparish\b", re.IGNORECASE)
_PRIMARY_RE = re.compile(r"\bprimar(?:y|ies)\b|\bnominee\b|\bnominat", re.IGNORECASE)
_LT_GOV_RE = re.compile(r"\blieutenant\b|\blt\.? gov", re.IGNORECASE)
_SPECIAL_RE = re.compile(r"\bspecial\b", re.IGNORECASE)
_RUNOFF_RE = re.compile(r"\brun-?offs?\b", re.IGNORECASE)

# Polymarket question shapes recognised as a party (not candidate) outcome. Everything
# else is a near miss for a reviewer to look at, never a suggestion.
_PM_PARTY_SHAPES = (
    ("statewide_party_win", re.compile(
        r"^Will the (?P<party>Democrats|Republicans) win the (?P<state>[A-Za-z ]+?) "
        r"(?P<office>Senate|governor|Governor) race in (?P<year>\d{4})\?$")),
    ("statewide_party_candidate_win", re.compile(
        r"^Will (?:the|a) (?P<party>Democratic|Republican) Party candidate win the "
        r"(?P<year>\d{4}) (?P<state>[A-Za-z ]+?) "
        r"(?P<office>Senate|Governor|governor|[Gg]ubernatorial) election\?$")),
    ("house_party_candidate_win", re.compile(
        r"^Will (?:the|a) (?P<party>Democratic|Republican) Party candidate win the "
        r"(?P<year>\d{4}) (?P<st>[A-Z]{2})-(?P<d>\d{2}|AL) House election\?$")),
    ("house_party_seat_win", re.compile(
        r"^Will the (?P<party>Democratic|Republican) Party win the "
        r"(?P<st>[A-Z]{2})-(?P<d>\d{2}|AL) House seat\?$")),
    ("chamber_control", re.compile(
        r"^Will the (?P<party>Democratic|Republican) Party control the (?P<chamber>Senate|House) "
        r"after the (?P<year>\d{4}) Midterm elections\?$")),
)

# Kalshi decisive first sentences, as recorded in the frozen output.
_K_TAIL = r", then the market resolves to Yes\.$"
_K_PARTY = r"(?P<party>Democratic|Republican)(?P<dfl> \(DFL\))?"
_K_SHAPES = (
    ("senate_sworn_in", "sworn_in", re.compile(
        rf"^If a representative of the {_K_PARTY} party is sworn in as a Senator of "
        rf"(?P<state>[A-Za-z ]+?) for the term beginning in (?P<term>\d{{4}}){_K_TAIL}")),
    ("governor_inaugurated", "inaugurated", re.compile(
        rf"^If a representative of the {_K_PARTY} party is inaugurated as the governor of "
        rf"(?P<state>[A-Za-z ]+?) pursuant to the (?P<year>\d{{4}}) election{_K_TAIL}")),
    ("house_member_sworn_in", "sworn_in", re.compile(
        rf"^If the House member sworn in for (?P<st>[A-Z]{{2}})-(?P<d>\d{{1,2}}|AL) for the term "
        rf"beginning in (?P<term>\d{{4}}) is a member of the {_K_PARTY} [Pp]arty{_K_TAIL}")),
    ("chamber_control", "chamber_control", re.compile(
        rf"^If the (?P<party>Democratic|Republican) Party has won control of the "
        rf"(?:U\.S\. )?(?P<chamber>Senate|House) in (?P<year>\d{{4}}){_K_TAIL}")),
)
# A named person as the subject: "If Seth Bodnar (as an Independent) is sworn in ...",
# "If a representative of the David Hatley party is inaugurated ...".
_K_NAMED_RE = re.compile(
    r"^If (?:a representative of the )?(?!(?:Democratic|Republican)\b)[A-Z][a-z]+ [A-Z][a-z]+"
)
_K_SUFFIX_PARTY = {"D": "D", "DEM": "D", "R": "R", "REP": "R", "GOP": "R", "I": "I", "IND": "I"}
_K_SERIES_SHAPES = (
    ("senate", re.compile(r"^SENATE(?P<st>[A-Z]{2})$")),
    ("governor", re.compile(r"^(?:KX)?GOVPARTY(?P<st>[A-Z]{2})$")),
    ("house", re.compile(r"^(?:KX)?HOUSE(?P<st>[A-Z]{2})(?P<d>\d{1,2}|AL)$")),
    ("house_generic", re.compile(r"^KXHOUSERACE$")),
    ("senate_control", re.compile(r"^CONTROLS$")),
    ("house_control", re.compile(r"^CONTROLH$")),
)
_KXHOUSERACE_EVENT_RE = re.compile(r"^KXHOUSERACE-(?P<st>[A-Z]{2})(?P<d>\d{2}|AL)-26$")

_PARTY_WORDS = (
    ("D", re.compile(r"\bDemocrat(?:s|ic|ics)?\b", re.IGNORECASE)),
    ("R", re.compile(r"\bRepublicans?\b|\bGOP\b", re.IGNORECASE)),
    ("I", re.compile(r"\bIndependents?\b", re.IGNORECASE)),
)


def _norm_district(d: str | None) -> str | None:
    if d is None:
        return None
    return "AL" if d.upper() == "AL" else f"{int(d):02d}"


def first_sentence(text: str | None) -> str:
    if not text:
        return ""
    line = text.strip().split("\n", 1)[0].strip()
    return _SENTENCE_END_RE.split(line, maxsplit=1)[0].strip()


def _states_in(text: str) -> set[str]:
    found = {STATE_CODE_BY_NAME[m.lower()] for m in _STATE_NAME_RE.findall(text or "")}
    for m in _DISTRICT_RE.finditer(text or ""):
        if m["st"] in STATE_ABBRS:
            found.add(m["st"])
    return found


def _parties_in(text: str) -> set[str]:
    return {code for code, pattern in _PARTY_WORDS if pattern.search(text or "")}


def _office_of(text: str) -> str | None:
    lowered = (text or "").lower()
    if _LT_GOV_RE.search(lowered):
        return "lt_governor"
    if re.search(r"\bgovernor|gubernatorial|governorship", lowered):
        return "governor"
    if "senate" in lowered:
        return "senate"
    if re.search(r"\bhouse\b|congressional district", lowered):
        return "house"
    return None


def analyse_polymarket(record: dict[str, Any]) -> dict[str, Any]:
    """Identity features of one Polymarket record, from its own question and rules."""
    question = record.get("question") or ""
    rules = record.get("resolution_description") or ""
    feats: dict[str, Any] = {
        "venue": "polymarket", "id": str(record.get("market_id")), "shape": None,
        "party": None, "office": None, "state": None, "district": None, "year": None,
        "issues": [], "signals": [],
    }
    for shape, pattern in _PM_PARTY_SHAPES:
        m = pattern.match(question)
        if not m:
            continue
        feats["shape"] = shape
        feats["party"] = "D" if m["party"].startswith("Democrat") else "R"
        groups = m.groupdict()
        feats["year"] = groups.get("year")
        if shape == "chamber_control":
            feats["office"] = f"{m['chamber'].lower()}_control"
        elif shape.startswith("statewide"):
            feats["office"] = "senate" if m["office"] == "Senate" else "governor"
            feats["state"] = STATE_CODE_BY_NAME.get(m["state"].lower())
        else:
            feats["office"] = "house"
            feats["state"] = m["st"]
            feats["district"] = _norm_district(m["d"])
        break
    if feats["shape"] is None:
        districts = {(m["st"], _norm_district(m["d"])) for m in _DISTRICT_RE.finditer(question)}
        states = _states_in(question)
        feats["office"] = _office_of(question)
        if len(districts) == 1:
            feats["state"], feats["district"] = next(iter(districts))
            feats["office"] = feats["office"] or "house"
        elif len(states) == 1:
            feats["state"] = next(iter(states))
        elif len(states) > 1:
            feats["issues"].append("multiple_jurisdictions")
        parties = _parties_in(question)
        feats["party"] = next(iter(parties)) if len(parties) == 1 else None
        if len(parties) > 1:
            feats["issues"].append("multiple_party_outcomes")
        if _LT_GOV_RE.search(question):
            feats["issues"].append("different_office")
        if _SUB_JURISDICTION_RE.search(question):
            feats["issues"].append("sub_jurisdiction_scope")
        if _MARGIN_RE.search(question):
            feats["issues"].append("margin_vote_share_or_seat_count")
        if _PRIMARY_RE.search(question):
            feats["issues"].append("primary_or_nomination")
        if not parties:
            feats["issues"].append("named_candidate_or_non_party_subject")
        feats["issues"].append("unrecognised_question_shape")
    years = {int(y) for y in _YEAR_RE.findall(question)}
    if years - {2026}:
        feats["issues"].append("other_election_cycle")
    token_map = record.get("outcome_token_map") or {}
    if not isinstance(token_map.get("Yes"), str) or set(token_map) != {"Yes", "No"}:
        feats["issues"].append("not_binary_yes_no")
    feats["yes_token_id"] = token_map.get("Yes")
    lead = first_sentence(rules)
    if _SPECIAL_RE.search(rules) or _SPECIAL_RE.search(question):
        feats["signals"].append("venue_text_mentions_special_election")
    if _RUNOFF_RE.search(lead):
        feats["signals"].append("venue_rules_include_runoffs")
    if _SUB_JURISDICTION_RE.search(lead) and "sub_jurisdiction_scope" not in feats["issues"]:
        feats["signals"].append("venue_rules_mention_county_or_city")
    if feats["shape"] in ("house_party_candidate_win", "statewide_party_candidate_win"):
        feats["signals"].append("venue_subject_is_party_candidate")
    feats["proposition"] = (
        "chamber_control" if feats["shape"] == "chamber_control" else "election_winner"
    ) if feats["shape"] else None
    feats["entry"] = {
        "market_id": str(record.get("market_id")),
        "question": question,
        "event_id": record.get("event_id"),
        "event_title": record.get("event_title"),
        "end_date": record.get("end_date"),
        "outcome_label": "Yes" if feats["yes_token_id"] else None,
        "token_id": feats["yes_token_id"],
        "rules_sha256": sha256_text_lf(rules),
        "rules_lead": lead,
    }
    return feats


def analyse_kalshi(record: dict[str, Any]) -> dict[str, Any]:
    """Identity features of one Kalshi record, from tickers, title and rules."""
    ticker = record.get("market_ticker") or ""
    event = record.get("event_ticker") or ""
    series = record.get("series_ticker") or ""
    title = record.get("title") or ""
    rules = record.get("rules_text") or ""
    lead = first_sentence(rules)
    feats: dict[str, Any] = {
        "venue": "kalshi", "id": ticker, "shape": None, "party": None, "office": None,
        "state": None, "district": None, "year": None, "issues": [], "signals": [],
        "proposition": None,
    }
    ticker_state = ticker_district = None
    for office, pattern in _K_SERIES_SHAPES:
        m = pattern.match(series)
        if not m:
            continue
        feats["office"] = "house" if office == "house_generic" else office
        groups = m.groupdict()
        ticker_state = groups.get("st")
        ticker_district = _norm_district(groups.get("d"))
        if office == "house_generic":
            em = _KXHOUSERACE_EVENT_RE.match(event)
            if em:
                ticker_state, ticker_district = em["st"], _norm_district(em["d"])
        break
    suffix = ticker[len(event) + 1:] if event and ticker.startswith(event + "-") else ""
    suffix_party = _K_SUFFIX_PARTY.get(suffix.upper())

    rules_state = rules_district = rules_party = None
    for shape, proposition, pattern in _K_SHAPES:
        m = pattern.match(lead)
        if not m:
            continue
        feats["shape"] = shape
        feats["proposition"] = proposition
        groups = m.groupdict()
        rules_party = "D" if groups["party"] == "Democratic" else "R"
        if groups.get("dfl"):
            feats["signals"].append("venue_party_label_dfl")
        if groups.get("state"):
            rules_state = STATE_CODE_BY_NAME.get(groups["state"].lower())
        if groups.get("st"):
            rules_state, rules_district = groups["st"], _norm_district(groups["d"])
        if shape == "chamber_control":
            feats["office"] = f"{groups['chamber'].lower()}_control"
        feats["year"] = groups.get("year") or groups.get("term")
        break

    feats["state"] = rules_state or ticker_state
    feats["district"] = rules_district or ticker_district
    feats["party"] = rules_party or suffix_party
    if feats["office"] is None:
        feats["office"] = _office_of(f"{title} {record.get('event_title') or ''}")
        title_states = _states_in(title)
        if len(title_states) == 1:
            feats["state"] = next(iter(title_states))
        parties = _parties_in(title)
        feats["party"] = next(iter(parties)) if len(parties) == 1 else None
    feats["other_states"] = sorted(
        ({ticker_state, rules_state} | _states_in(title)) - {None, feats["state"]}
    )

    if feats["shape"] is None:
        if _MARGIN_RE.search(f"{title} {lead}"):
            feats["issues"].append("margin_vote_share_or_seat_count")
        if _K_NAMED_RE.match(lead) or (suffix and suffix_party is None):
            feats["issues"].append("named_candidate_or_non_party_subject")
        if _LT_GOV_RE.search(f"{title} {series}"):
            feats["issues"].append("different_office")
        feats["issues"].append("unrecognised_rules_shape")
    else:
        if ticker_state and rules_state and ticker_state != rules_state:
            feats["issues"].append("ticker_state_conflicts_with_rules")
        if ticker_district and rules_district and ticker_district != rules_district:
            feats["issues"].append("ticker_district_conflicts_with_rules")
        if suffix_party is None:
            feats["issues"].append("named_candidate_or_non_party_subject")
        elif rules_party and suffix_party != rules_party:
            feats["issues"].append("ticker_party_conflicts_with_rules")
        if "dfl" in " ".join(feats["signals"]) and feats["state"] != "MN":
            feats["issues"].append("dfl_label_outside_minnesota")
        title_states = _states_in(title)
        if title_states and feats["state"] and feats["state"] not in title_states:
            feats["issues"].append("title_state_conflicts_with_rules")
    feats["entry"] = {
        "market_ticker": ticker,
        "event_ticker": event,
        "series_ticker": series,
        "title": title,
        "subtitle": record.get("subtitle"),
        "close_time": record.get("close_time"),
        "side": "yes",
        "rules_sha256": sha256_text_lf(rules),
        "rules_lead": lead,
    }
    return feats


def _relevant(sig: dict[str, Any], feats: dict[str, Any]) -> bool:
    """Whether a venue record is worth showing for this SIG contract at all."""
    office = sig["office"]
    if office in ("senate_control", "house_control"):
        if feats["office"] == office:
            return True
        return (feats["state"] is None and not feats.get("other_states")
                and feats["office"] == office.split("_")[0])
    states = {feats["state"], *feats.get("other_states", [])}
    if sig["state"] not in states:
        return False
    if office == "house":
        return feats["office"] == "house" or feats["district"] is not None
    return feats["office"] == office or (office == "governor" and feats["office"] == "lt_governor")


def _compare(sig: dict[str, Any], feats: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Rejection reasons and reviewer signals for one SIG contract / venue record pair."""
    reasons = list(feats["issues"])
    signals = list(feats["signals"])
    office = sig["office"]
    if feats["office"] not in (office, None) and "different_office" not in reasons:
        reasons.append("different_office")
    if feats["state"] != sig["state"]:
        reasons.append("different_state")
    if office == "house" and feats["district"] != sig["district"]:
        reasons.append("different_district")
    if feats["party"] is None:
        if "named_candidate_or_non_party_subject" not in reasons:
            reasons.append("no_party_outcome")
    elif feats["party"] != sig["party"]:
        reasons.append("other_party_outcome")
    if feats["year"] and feats["year"] not in ("2026", "2027") and \
            "other_election_cycle" not in reasons:
        reasons.append("other_election_cycle")
    if feats["proposition"] == "sworn_in":
        signals.append("venue_condition_is_swearing_in")
    elif feats["proposition"] == "inaugurated":
        signals.append("venue_condition_is_inauguration")
    return sorted(set(reasons)), sorted(set(signals))


def _sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
    return (len(item["reasons"]), item["reasons"], item["id"])


def generate_candidates(
    contracts: list[dict[str, Any]],
    snapshot: dict[str, Any],
    snapshot_meta: dict[str, Any],
    inventory_id: str,
) -> dict[str, Any]:
    """Candidate report for every SIG contract, deterministic for a given input."""
    analysed = {
        "polymarket": [analyse_polymarket(r) for r in snapshot.get("polymarket") or []],
        "kalshi": [analyse_kalshi(r) for r in snapshot.get("kalshi") or []],
    }
    out_contracts = []
    for contract in contracts:
        identity = proposed_identity(contract.get("title"))
        row: dict[str, Any] = {
            "sig_market_id": contract["market_id"],
            "sig_exchange_id": contract["exchange_id"],
            "sig_title": contract.get("title"),
            "proposed_race_key": identity["race_key"] if identity else None,
        }
        for venue in ("polymarket", "kalshi"):
            if identity is None:
                row[venue] = {"suggested": [], "near_misses": [], "near_miss_total": 0,
                              "near_miss_reason_counts": {},
                              "note": "SIG title unparsed; no discovery attempted"}
                continue
            suggested, near = [], []
            for feats in analysed[venue]:
                if not _relevant(identity, feats):
                    continue
                reasons, signals = _compare(identity, feats)
                item = {"id": feats["id"], "reasons": reasons, "signals": signals,
                        "entry": feats["entry"]}
                (near if reasons else suggested).append(item)
            suggested.sort(key=_sort_key)
            near.sort(key=_sort_key)
            reason_counts = Counter(r for item in near for r in item["reasons"])
            row[venue] = {
                "suggested": [
                    {**s["entry"], "signals": s["signals"]} for s in suggested
                ],
                "near_misses": [
                    {"id": n["id"], "reasons": n["reasons"],
                     "label": n["entry"].get("question") or n["entry"].get("title")}
                    for n in near[:NEAR_MISS_LIMIT]
                ],
                "near_miss_total": len(near),
                "near_miss_reason_counts": dict(sorted(reason_counts.items())),
            }
        out_contracts.append(row)
    usage: dict[tuple[str, str], list[str]] = defaultdict(list)
    for row in out_contracts:
        for venue in ("polymarket", "kalshi"):
            for s in row[venue]["suggested"]:
                usage[(venue, s.get("token_id") or s.get("market_ticker"))].append(
                    row["sig_market_id"])
    shared = {f"{venue}:{key}": ids for (venue, key), ids in sorted(usage.items())
              if len(ids) > 1}
    return {
        "format": CANDIDATES_FORMAT,
        "generator": GENERATOR_VERSION,
        "notice": ("Algorithmic suggestions only. A suggestion is not evidence of settlement "
                   "equivalence, does not change any decision in r1_mapping.json and must "
                   "never feed fair value. Prices in the venue output are ignored."),
        "generated_from": {
            "sig_inventory": inventory_id,
            "venue_snapshot": snapshot_meta.get("id"),
            "venue_snapshot_sha256": snapshot_meta.get("sha256"),
            "venue_snapshot_fetched_at": snapshot.get("fetched_at"),
        },
        "suggestions_shared_by_several_sig_contracts": shared,
        "contracts": out_contracts,
    }


def dump_candidates(report: dict[str, Any]) -> str:
    """Stable, diff-friendly JSON: structure indented, each venue entry on one line."""

    def one(obj: Any) -> str:
        return json.dumps(obj, ensure_ascii=False, sort_keys=False)

    lines = ["{"]
    head = [k for k in report if k != "contracts"]
    for key in head:
        lines.append(f"  {one(key)}: {json.dumps(report[key], ensure_ascii=False)},")
    lines.append('  "contracts": [')
    for ci, row in enumerate(report["contracts"]):
        lines.append("    {")
        keys = list(row)
        for ki, key in enumerate(keys):
            comma = "," if ki < len(keys) - 1 else ""
            value = row[key]
            if key not in ("polymarket", "kalshi"):
                lines.append(f"      {one(key)}: {one(value)}{comma}")
                continue
            lines.append(f"      {one(key)}: {{")
            vkeys = list(value)
            for vi, vkey in enumerate(vkeys):
                vcomma = "," if vi < len(vkeys) - 1 else ""
                vval = value[vkey]
                if isinstance(vval, list) and vval:
                    lines.append(f"        {one(vkey)}: [")
                    for ei, entry in enumerate(vval):
                        ecomma = "," if ei < len(vval) - 1 else ""
                        lines.append(f"          {one(entry)}{ecomma}")
                    lines.append(f"        ]{vcomma}")
                else:
                    lines.append(f"        {one(vkey)}: {one(vval)}{vcomma}")
            lines.append(f"      }}{comma}")
        lines.append("    }" + ("," if ci < len(report["contracts"]) - 1 else ""))
    lines.append("  ]")
    lines.append("}")
    return "\n".join(lines) + "\n"
