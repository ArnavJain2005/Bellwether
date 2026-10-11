"""Fetch 2026 US midterm election markets from Polymarket and Kalshi.

Saves active markets to data/venue_markets/output/venue_markets_<date>.json
and data/venue_markets/output/venue_markets_latest.json.

Standard library only (Python 3.11+).
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import random
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

USER_AGENT = "bellwether/0.1 (research)"
MIN_REQUEST_INTERVAL = 0.3  # at most 4 requests per second per venue


class FetchError(Exception):
    """Raised when venue market fetching or pagination fails."""


US_STATES = [
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana",
    "maine", "maryland", "massachusetts", "michigan", "minnesota",
    "mississippi", "missouri", "montana", "nebraska", "nevada",
    "new hampshire", "new jersey", "new mexico", "new york",
    "north carolina", "north dakota", "ohio", "oklahoma", "oregon",
    "pennsylvania", "rhode island", "south carolina", "south dakota",
    "tennessee", "texas", "utah", "vermont", "virginia", "washington",
    "west virginia", "wisconsin", "wyoming",
]

# Longer state names must be tested before substrings (e.g. "west virginia" before "virginia")
US_STATES_BY_LENGTH = sorted(US_STATES, key=len, reverse=True)

STATE_ABBRS = {
    "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas",
    "CA": "california", "CO": "colorado", "CT": "connecticut", "DE": "delaware",
    "FL": "florida", "GA": "georgia", "HI": "hawaii", "ID": "idaho",
    "IL": "illinois", "IN": "indiana", "IA": "iowa", "KS": "kansas",
    "KY": "kentucky", "LA": "louisiana", "ME": "maine", "MD": "maryland",
    "MA": "massachusetts", "MI": "michigan", "MN": "minnesota", "MS": "mississippi",
    "MO": "missouri", "MT": "montana", "NE": "nebraska", "NV": "nevada",
    "NH": "new hampshire", "NJ": "new jersey", "NM": "new mexico", "NY": "new york",
    "NC": "north carolina", "ND": "north dakota", "OH": "ohio", "OK": "oklahoma",
    "OR": "oregon", "PA": "pennsylvania", "RI": "rhode island", "SC": "south carolina",
    "SD": "south dakota", "TN": "tennessee", "TX": "texas", "UT": "utah",
    "VT": "vermont", "VA": "virginia", "WA": "washington", "WV": "west virginia",
    "WI": "wisconsin", "WY": "wyoming",
}


def _safe_parse_json(text: str | None, default: Any = None) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return default


def _safe_float(val: Any) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _to_cents(dollars_val: Any, native_cents: Any = None) -> int | None:
    if native_cents is not None:
        try:
            return int(native_cents)
        except (ValueError, TypeError):
            pass
    if dollars_val is not None:
        try:
            return round(float(dollars_val) * 100)
        except (ValueError, TypeError):
            pass
    return None


class RateLimiter:
    """Enforces sequential requests at most `1 / interval` per second."""

    def __init__(self, interval: float = MIN_REQUEST_INTERVAL) -> None:
        self.interval = interval
        self.last_request_time = 0.0

    def wait(self) -> None:
        now = time.time()
        elapsed = now - self.last_request_time
        if elapsed < self.interval:
            time.sleep(self.interval - elapsed)
        self.last_request_time = time.time()


# =====================================================================
# State & Race Classification
# =====================================================================

def extract_states_from_text(text: str) -> list[str]:
    """Find all US states mentioned as whole words in text, handling multi-word states first."""
    # Replace hyphens with spaces to properly handle slugs and hyphenated text (e.g. 'west-virginia')
    masked = text.lower().replace("-", " ")
    found: list[str] = []
    for state in US_STATES_BY_LENGTH:
        pattern = rf"\b{re.escape(state)}\b"
        if re.search(pattern, masked):
            found.append(state)
            # Mask out the matched state so substrings like 'virginia' inside 'west virginia' cannot re-match
            masked = re.sub(pattern, " ", masked)
    return found


def extract_race_state(venue: str, market: dict[str, Any], race_type: str) -> str | None:
    """Extract US state associated with a race, rejecting multi-state comparisons."""
    if race_type not in ("senate", "governor"):
        return None

    ticker = (market.get("market_ticker") or market.get("ticker") or "").upper()
    series = (market.get("series_ticker") or "").upper()

    if venue == "kalshi":
        # Check anchored, validated symbol patterns with proper boundaries
        for sym in (series, ticker):
            for kind, pattern in _KALSHI_STATE_CONTEST_RES:
                if kind != race_type:
                    continue
                match = pattern.match(sym)
                if match and match.group("st") in STATE_ABBRS:
                    return STATE_ABBRS[match.group("st")]

    # Text extraction for Polymarket / Kalshi title fallback
    if venue == "polymarket":
        text = f"{market.get('question', '')} {market.get('slug', '')} {market.get('event_title', '')}"
    else:
        # Market subtitles are candidate/outcome labels, even when a label is
        # exactly a state name. Parent-event context is retained in event_title.
        text = f"{market.get('title', '')} {market.get('event_title', '')}"

    states = extract_states_from_text(text)
    # If comparison between 2 or more states (e.g. 'Florida or South Carolina'), return None
    if len(states) == 1:
        return states[0]
    if len(states) > 1:
        return None

    # Check for state full names
    upper_text = text.upper()
    for full in STATE_ABBRS.values():
        if re.search(rf"\b{full.upper()}\b", upper_text):
            return full

    if venue == "kalshi" and _KALSHI_SUFFIX_STATE_SERIES_RE.match(series):
        # Check ticker suffix for hyphen-delimited state code: e.g. -TX or -AK
        # Allow suffix-derived states only through explicitly recognized, anchored series formats.
        suffix_match = re.search(r"[-_](?P<st>[A-Z]{2})(?:[-_].*)?$", ticker)
        if suffix_match and suffix_match.group("st") in STATE_ABBRS:
            return STATE_ABBRS[suffix_match.group("st")]

    return None


def classify_race(venue: str, market: dict[str, Any], event_title: str | None = None) -> str:
    """Classify a market as 'senate', 'house', 'governor', 'control', or 'other'."""
    ev_t = (event_title or market.get("event_title") or "").lower()
    if venue == "polymarket":
        q = (market.get("question") or "").lower()
        slug = (market.get("slug") or market.get("market_slug") or "").lower()
        text = f"{q} {slug} {ev_t}"
        if _LT_GOV_RE.search(text):
            return "other"
        if _is_state_legislative_contest(text):
            return "other"
        states = extract_states_from_text(text)
        if len(states) >= 2 or "closer" in text:
            return "other"
        if any(k in text for k in ["how many seats", "seat count", "total seats", "balance of power"]):
            return "other"
        if any(k in text for k in ["control the senate", "control the house", "senate control", "house control", "chamber control"]):
            return "control"
        if "governor" in text or "gubernatorial" in text:
            return "governor"
        if "senate" in text:
            return "senate"
        if "house" in text:
            return "house"
        return "other"

    # Kalshi
    ticker = (market.get("market_ticker") or market.get("ticker") or "").upper()
    series = (market.get("series_ticker") or "").upper()
    title = (market.get("title") or "").lower()
    subtitle = (market.get("subtitle") or market.get("yes_sub_title") or "").lower()
    text = f"{ticker} {series} {title} {ev_t}"

    # Valid structured state contest evidence takes precedence over broad wording
    structured = _kalshi_structured_contest(series, ticker)
    if structured in ("senate", "governor", "house"):
        return structured

    if _LT_GOV_RE.search(f"{text} {subtitle}") or "LTGOV" in ticker or "LTGOV" in series or "LIEUTENANTGOV" in series:
        return "other"
    if "STATESUPERMAJ" in series or "STATESUPERMAJ" in ticker or _is_state_legislative_contest(text, structured=structured):
        return "other"

    if structured == "control" or any(k in series for k in ["CONTROLS", "CONTROLH"]):
        return "control"

    has_state = bool(
        extract_race_state("kalshi", market, "senate")
        or extract_race_state("kalshi", market, "governor")
        or extract_states_from_text(text)
    )

    if not has_state:
        if any(k in text for k in ["control the senate", "control the house", "senate control", "house control", "chamber control"]):
            return "control"
        if any(k in title for k in ["win the senate", "win the house"]):
            return "control"

    if series.startswith("GOVPARTY") or "governor" in text or "gubernatorial" in text:
        return "governor"
    if series.startswith("SENATE") or "senate" in text:
        return "senate"
    if series.startswith("HOUSE") or "house" in text or re.search(r"\b[a-z]{2}-\d{2}\b", title) or re.search(r"\b[a-z]{2}-\d{2}\b", subtitle):
        return "house"

    saved_type = market.get("race_type")
    if not ev_t and saved_type in ("senate", "house", "governor", "control") and "event_title" not in market:
        return saved_type

    return "other"


# =====================================================================
# Market Eligibility Verification
# =====================================================================

# Contest-evidence rules shared by both venues. See README "Eligibility evidence rules".
CONTEST_YEAR = 2026

# State names that are also countries/cities: ambiguous on their own unless election office named.
AMBIGUOUS_STATE_NAMES = frozenset({"georgia", "washington"})

_FOREIGN_TERMS = (
    "argentina", "argentine", "australia", "australian", "austria", "austrian", "belgium", "belgian",
    "brazil", "brazilian", "britain", "british", "bundestag", "canada", "canadian", "chile", "chilean",
    "china", "chinese", "colombia", "colombian", "denmark", "danish", "duma", "egypt", "egyptian",
    "england", "english", "eu", "european union", "european", "finland", "finnish", "france", "french",
    "georgian", "germany", "german", "greece", "greek", "hungary", "hungarian", "india", "indian",
    "indonesia", "indonesian", "iran", "iranian", "ireland", "irish", "israel", "israeli", "italy",
    "italian", "japan", "japanese", "kenya", "knesset", "korea", "korean", "mexico", "mexican",
    "netherlands", "dutch", "nigeria", "nigerian", "norway", "norwegian", "pakistan", "parliament",
    "parliamentary", "peru", "peruvian", "philippines", "philippine", "poland", "polish", "portugal",
    "portuguese", "prime minister", "reichstag", "russia", "russian", "scotland", "scottish", "spain",
    "spanish", "sweden", "swedish", "switzerland", "swiss", "taiwan", "tbilisi", "turkey", "turkish",
    "uk", "united kingdom", "ukraine", "ukrainian", "venezuela", "venezuelan", "wales", "welsh",
    "south africa", "south african",
)
_FOREIGN_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(t) for t in sorted(_FOREIGN_TERMS, key=len, reverse=True)) + r")\b"
)
_UNRELATED_RE = re.compile(
    r"\b(?:steps? down|resign(?:s|ed|ation)?|appoint(?:s|ed|ment)?|confirmed as|confirmation of"
    r"|nominated|nomination to|impeach(?:ed|ment)?|retire(?:s|d|ment)?|cabinet|secretary of"
    r"|reconciliation bill|federal reserve board of)\b"
)
_LT_GOV_RE = re.compile(
    r"\b(?:lieutenant\s+governor|lieutenant\s+gov|lt\s+governor|lt\s+gov|ltgov|lieutenantgov)\b",
    re.IGNORECASE,
)
_EXPLICIT_STATE_LEGISLATURE_RE = re.compile(
    r"\b(?:state\s+senate|state\s+house|state\s+assembly|state\s+legislature|statehouse|legislature"
    r"|general\s+assembly|house\s+of\s+delegates|state\s+supermajority|statesupermaj)\b",
    re.IGNORECASE,
)
_STATE_SEATS_RE = re.compile(
    r"\b(?:" + "|".join(US_STATES) + r")\s+(?:senate|house(?:\s+of\s+representatives)?)\s+seats\b",
    re.IGNORECASE,
)
_EXPLICIT_FEDERAL_BODY_RE = re.compile(
    r"\b(?:(?:u\.s\.?|us|united\s+states|federal)\s+(?:senate|house(?:\s+of\s+representatives)?|congress(?:ional)?)"
    r"|congressional\s+elections?|in\s+congress)\b",
    re.IGNORECASE,
)


def _is_state_legislative_contest(text: str, structured: str | None = None) -> bool:
    """Return True if contest text refers to a state legislature contest rather than a federal one."""
    if _EXPLICIT_STATE_LEGISLATURE_RE.search(text):
        return True
    if _STATE_SEATS_RE.search(text):
        return not (structured in ("senate", "house") or bool(_EXPLICIT_FEDERAL_BODY_RE.search(text)))
    return False


_US_WORDING_RE = re.compile(r"\b(?:us|usa|united states|midterms?)\b")
_OFFICE_RE = re.compile(r"\b(?:senate|house|governor|governors|gubernatorial|congress)\b")
_CONTEST_RE = re.compile(
    r"\b(?:win|wins|won|winner|winners|elect|elected|election|elections|race|races|seat|seats"
    r"|control|controls|controlled|majority|midterm|midterms|flip|flips|balance of power)\b"
)
_YEAR_RE = re.compile(r"\b(20[23]\d)\b")

# Narrow, state-validated Kalshi structured series/ticker patterns
_KALSHI_CONTROL_RE = re.compile(r"^(?:KX)?CONTROL[SH](?:[-_](?P<yr>20[23]\d|[23]\d))?(?:[-_].*)?$")
_KALSHI_GENERIC_RE = re.compile(r"^(?:KX)?(?:SENATE(?:PARTY)?|HOUSE(?:PARTY)?|GOV(?:PARTY)?)(?:[-_](?P<yr>20[23]\d|[23]\d))?(?:[-_].*)?$")
_KALSHI_STATE_CONTEST_RES = (
    ("senate", re.compile(r"^(?:KX)?SENATE(?:PARTY[-_]?)?(?P<st>[A-Z]{2})(?:[-_](?P<yr>20[23]\d|[23]\d))?(?:[-_].*)?$")),
    ("senate", re.compile(r"^(?:KX)?(?P<st>[A-Z]{2})SENATE(?:PARTY)?(?:[-_](?P<yr>20[23]\d|[23]\d))?(?:[-_].*)?$")),
    ("governor", re.compile(r"^(?:KX)?GOV(?:PARTY)?[-_]?(?P<st>[A-Z]{2})(?:[-_](?P<yr>20[23]\d|[23]\d))?(?:[-_].*)?$")),
    ("governor", re.compile(r"^(?:KX)?(?P<st>[A-Z]{2})GOV(?:PARTY)?(?:[-_](?P<yr>20[23]\d|[23]\d))?(?:[-_].*)?$")),
    ("house", re.compile(r"^(?:KX)?HOUSE(?:PARTY)?[-_]?(?P<st>[A-Z]{2})[-_]?(?:\d+|AL)(?:[-_](?P<yr>20[23]\d|[23]\d))?(?:[-_].*)?$")),
    ("house", re.compile(r"^(?:KX)?(?P<st>[A-Z]{2})HOUSE(?:PARTY)?[-_]?(?:\d+|AL)?(?:[-_](?P<yr>20[23]\d|[23]\d))?(?:[-_].*)?$")),
)
_KALSHI_SUFFIX_STATE_SERIES_RE = re.compile(
    r"^(?:KX)?(?:SENATEWITHIN\d*|CLOSEST(?:SENATE|GOVERNOR))$"
)


def _normalize_contest_text(text: str | None) -> str:
    """Lowercase, fold 'U.S.' spellings to 'us', drop punctuation and the 'White House'."""
    t = (text or "").lower()
    t = re.sub(r"\bu\.s\.a\.?", " usa ", t)
    t = re.sub(r"\bu\.s\b\.?", " us ", t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\bwhite house\b", " ", t)


def _kalshi_structured_contest(series: str, ticker: str) -> str | None:
    """Return 'control'/'senate'/'governor'/'house' for narrowly recognised U.S. Kalshi symbols."""
    for symbol in (series, ticker):
        s = (symbol or "").upper()
        if _KALSHI_CONTROL_RE.match(s):
            return "control"
        for kind, pattern in _KALSHI_STATE_CONTEST_RES:
            match = pattern.match(s)
            if match and match.group("st") in STATE_ABBRS:
                return kind
    return None


def _kalshi_symbol_years(*symbols: str) -> set[int]:
    """Cycle years encoded in recognized structured Kalshi tickers.

    A year is only extracted from a valid cycle-year position following a recognized prefix.
    """
    years: set[int] = set()
    for symbol in symbols:
        s = (symbol or "").upper()

        match = _KALSHI_CONTROL_RE.match(s)
        if match:
            yr = match.group("yr")
            if yr:
                years.add(int(yr) if len(yr) == 4 else 2000 + int(yr))
            continue

        match = _KALSHI_GENERIC_RE.match(s)
        if match:
            yr = match.group("yr")
            if yr:
                years.add(int(yr) if len(yr) == 4 else 2000 + int(yr))
            continue

        for kind, pattern in _KALSHI_STATE_CONTEST_RES:
            match = pattern.match(s)
            if match and match.group("st") in STATE_ABBRS:
                yr = match.group("yr")
                if yr:
                    years.add(int(yr) if len(yr) == 4 else 2000 + int(yr))
                break
    return years


def _evaluate_election_contest(
    identity_text: str,
    symbol_years: set[int] | None = None,
    structured: str | None = None,
    is_tag_102289: bool = False,
) -> tuple[bool, str]:
    """Decide from primary contest identity whether a market is a 2026 U.S. election contest.

    ``identity_text`` must hold only primary identity (question/title, slug, event title);
    description/rules text must never be passed in. Returns ``(True, "verified_2026")`` or
    ``(False, reason)``.
    """
    text = _normalize_contest_text(identity_text)

    if _UNRELATED_RE.search(text):
        return False, "unrelated_political_event"

    if _LT_GOV_RE.search(text):
        return False, "lieutenant_governor_contest"

    states = extract_states_from_text(text)
    explicit_us = bool(_US_WORDING_RE.search(text))

    masked = text
    for state in states:  # so 'new mexico' is not read as 'mexico'
        masked = re.sub(rf"\b{re.escape(state)}\b", " ", masked)
    has_foreign = bool(_FOREIGN_RE.search(masked))

    # Georgia and Washington count as US states whenever text names a Senate, governor or House race,
    # unless a foreign term is present.
    has_race_office = bool(_OFFICE_RE.search(text) and _CONTEST_RE.search(text))
    if has_race_office and not has_foreign:
        firm_states = list(states)
    else:
        firm_states = [s for s in states if s not in AMBIGUOUS_STATE_NAMES]

    if has_foreign:
        has_us_context = explicit_us or bool(firm_states) or structured is not None
        return False, "conflicting_jurisdiction" if has_us_context else "foreign_election"

    if _is_state_legislative_contest(text, structured=structured):
        return False, "state_legislative_contest"
    if len(states) >= 2:
        return False, "conflicting_state_context"

    years = {int(y) for y in _YEAR_RE.findall(text)} | (symbol_years or set())
    if years - {CONTEST_YEAR}:
        return False, "non_2026_primary_contest"

    if CONTEST_YEAR not in years:
        return False, "lacks_2026_contest_year"

    # Election check
    if structured is None and not (_OFFICE_RE.search(text) and _CONTEST_RE.search(text)):
        return False, "lacks_election_evidence"

    # Jurisdiction check
    if explicit_us or firm_states or (structured is not None) or is_tag_102289:
        return True, "verified_2026"
    if states:
        return False, "ambiguous_jurisdiction"
    return False, "lacks_us_jurisdiction"


def is_2026_polymarket_market(
    item: dict[str, Any],
    parent_event: dict[str, Any] | None = None,
    is_tag_102289: bool = False,
) -> tuple[bool, str]:
    """Verify whether a Polymarket record is a 2026 U.S. Senate/House/governor/control contest."""
    closed_val = item.get("closed")
    if closed_val is not False or type(closed_val) is not bool:
        return False, "market_closed"

    active_val = item.get("active")
    if active_val is not True or type(active_val) is not bool:
        return False, "market_inactive"

    ev = parent_event or (item.get("events")[0] if isinstance(item.get("events"), list) and item.get("events") else {})
    ev_title = ev.get("title") or item.get("event_title") or ""
    ev_slug = ev.get("slug") or item.get("event_slug") or ""
    q = item.get("question") or ""
    slug = item.get("slug") or item.get("market_slug") or ""
    identity = f"{q} {slug} {ev_title} {ev_slug}".strip()
    return _evaluate_election_contest(identity, is_tag_102289=is_tag_102289)


def is_2026_kalshi_market(
    m: dict[str, Any], ev: dict[str, Any], is_primary_match: bool = False
) -> tuple[bool, str | None]:
    """Verify whether a Kalshi market is a 2026 U.S. Senate/House/governor/control contest."""
    m_status = (m.get("status") or "").lower()
    if m_status not in ("open", "active"):
        return False, "market_inactive"

    m_ticker = (m.get("ticker") or "").upper()
    m_series = (m.get("series_ticker") or ev.get("series_ticker") or "").upper()
    event_ticker = (ev.get("event_ticker") or "").upper()
    m_title = m.get("title") or ev.get("title") or ""
    ev_title = ev.get("title") or ""
    ev_subtitle = ev.get("sub_title") or ""
    cand_subtitle = m.get("yes_sub_title") or m.get("subtitle") or ""

    if "STATESUPERMAJ" in m_series or "STATESUPERMAJ" in m_ticker:
        return False, "state_legislative_contest"

    # Targeted exclusion for Lieutenant Governor across all subtitle sources
    all_subtitles = f"{cand_subtitle} {ev_subtitle}".strip()
    if (
        "LTGOV" in m_series
        or "LTGOV" in m_ticker
        or "LIEUTENANTGOV" in m_series
        or "LIEUTENANTGOV" in m_ticker
        or bool(_LT_GOV_RE.search(all_subtitles))
    ):
        return False, "lieutenant_governor_contest"

    structured = _kalshi_structured_contest(m_series, m_ticker)
    symbol_years = _kalshi_symbol_years(m_ticker, m_series, event_ticker)

    # Candidate/outcome labels never establish jurisdiction or the cycle year.
    # Event subtitles describe the contest and remain part of its identity.
    contest_identity = f"{m_title} {ev_subtitle} {ev_title}".strip()

    eligible, reason = _evaluate_election_contest(
        contest_identity,
        symbol_years=symbol_years,
        structured=structured,
        is_tag_102289=False,
    )
    return (True, None) if eligible else (False, reason)


# =====================================================================
# Polymarket Fetcher
# =====================================================================

_PLACEHOLDER_VALUES = frozenset({"none", "null", "nan", "undefined"})


def _clean_token_field(value: Any) -> str | None:
    """Return a stripped nonempty, non-placeholder string, else None."""
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned or cleaned.lower() in _PLACEHOLDER_VALUES:
        return None
    return cleaned


def _parse_binary_tokens(tokens: list[Any]) -> tuple[list[str], list[str], list[float | None]] | None:
    """Parse exactly two outcome-token records into (labels, token_ids, prices) in API order."""
    if len(tokens) != 2:
        return None
    outcomes: list[str] = []
    token_ids: list[str] = []
    prices: list[float | None] = []
    for record in tokens:
        if not isinstance(record, dict):
            return None
        outcome = _clean_token_field(record.get("outcome"))
        token_id = _clean_token_field(record.get("token_id"))
        if outcome is None or token_id is None:
            return None
        outcomes.append(outcome)
        token_ids.append(token_id)
        prices.append(_safe_float(record.get("price")))
    if len({o.casefold() for o in outcomes}) != 2 or len(set(token_ids)) != 2:
        return None
    return outcomes, token_ids, prices


def _parse_polymarket_tokens(item: dict[str, Any]) -> tuple[list[str], list[str], list[float | None]] | str:
    """Parse and validate exactly two outcome-token records for a Polymarket market.

    Supports both Gamma API schema (clobTokenIds, outcomes, outcomePrices) and CLOB tokens list.
    Returns (outcomes, clob_token_ids, prices) on success, or an exclusion reason string:
    'invalid_tokens', 'non_binary_tokens', or 'invalid_token_mapping'.
    """
    if "tokens" in item and item["tokens"] is not None:
        tokens = item["tokens"]
        if not isinstance(tokens, list):
            return "invalid_tokens"
        if len(tokens) != 2:
            return "non_binary_tokens"
        parsed = _parse_binary_tokens(tokens)
        if parsed is None:
            return "invalid_token_mapping"
        return parsed

    outcomes_raw = item.get("outcomes")
    tokens_raw = item.get("clobTokenIds") if item.get("clobTokenIds") is not None else item.get("clob_token_ids")

    if outcomes_raw is None or tokens_raw is None:
        return "invalid_tokens"

    outcomes = _safe_parse_json(outcomes_raw, default=None) if isinstance(outcomes_raw, str) else outcomes_raw
    clob_tokens = _safe_parse_json(tokens_raw, default=None) if isinstance(tokens_raw, str) else tokens_raw

    if not isinstance(outcomes, list) or not isinstance(clob_tokens, list):
        return "invalid_tokens"

    if len(outcomes) != 2 or len(clob_tokens) != 2:
        return "non_binary_tokens"

    clean_outcomes: list[str] = []
    clean_tokens: list[str] = []
    for o, t in zip(outcomes, clob_tokens):
        co = _clean_token_field(o)
        ct = _clean_token_field(t)
        if co is None or ct is None:
            return "invalid_token_mapping"
        clean_outcomes.append(co)
        clean_tokens.append(ct)

    if len({o.casefold() for o in clean_outcomes}) != 2 or len(set(clean_tokens)) != 2:
        return "invalid_token_mapping"

    prices_raw = item.get("outcomePrices") if item.get("outcomePrices") is not None else item.get("outcome_prices")
    parsed_prices = _safe_parse_json(prices_raw, default=[]) if isinstance(prices_raw, str) else (prices_raw or [])
    if isinstance(parsed_prices, list):
        prices = [_safe_float(p) for p in parsed_prices]
        while len(prices) < 2:
            prices.append(None)
        prices = prices[:2]
    else:
        prices = [None, None]

    return clean_outcomes, clean_tokens, prices


def fetch_polymarket_markets(
    rate_limiter: RateLimiter,
    base_url: str = "https://gamma-api.polymarket.com/events/keyset",
    opener: Any = None,
    exclusion_samples: dict[str, list[str]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Fetch active 2026 midterm markets from Polymarket Gamma API."""
    print("Fetching Polymarket markets...", flush=True)
    markets_by_id: dict[str, dict[str, Any]] = {}
    excluded_counts: dict[str, int] = {}
    if exclusion_samples is None:
        exclusion_samples = {}

    seen_cursors: set[str] = set()
    seen_page_identities: set[tuple[tuple[str, tuple[str, ...]], ...]] = set()
    next_cursor = ""

    limit = 100
    tag_id = 102289

    while True:
        if next_cursor in seen_cursors:
            raise FetchError(f"Polymarket pagination loop detected: repeated cursor {next_cursor}")
        if next_cursor:
            seen_cursors.add(next_cursor)

        params: dict[str, Any] = {
            "tag_id": tag_id,
            "closed": "false",
            "limit": limit,
        }
        if next_cursor:
            params["after_cursor"] = next_cursor

        url = f"{base_url}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

        data = None
        for attempt in range(5):
            rate_limiter.wait()
            try:
                if opener is not None:
                    resp_obj = opener.open(req)
                    body_bytes = resp_obj.read()
                else:
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        body_bytes = resp.read()
                data = json.loads(body_bytes.decode("utf-8"))
                break
            except urllib.error.HTTPError as e:
                if attempt < 4:
                    time.sleep((2 ** attempt) + random.uniform(0, 1))
                else:
                    raise FetchError(f"Polymarket HTTP {e.code} at cursor {next_cursor}: {e}") from e
            except Exception as e:
                if attempt < 4:
                    time.sleep((2 ** attempt) + random.uniform(0, 1))
                else:
                    raise FetchError(f"Polymarket request failed at cursor {next_cursor}: {e}") from e

        if not isinstance(data, dict):
            raise FetchError(f"Polymarket malformed response at cursor {next_cursor}: expected dict")

        events = data.get("events")
        if not isinstance(events, list):
            raise FetchError(f"Polymarket malformed response at cursor {next_cursor}: expected 'events' list")

        if events:
            page_event_signatures: list[tuple[str, tuple[str, ...]]] = []
            for ev in events:
                if not isinstance(ev, dict):
                    raise FetchError(f"Polymarket malformed response at cursor {next_cursor}: event must be a dict, got {type(ev).__name__}")
                ev_raw_id = ev.get("id")
                if ev_raw_id is None:
                    raise FetchError(f"Polymarket malformed response at cursor {next_cursor}: event missing stable id")
                ev_id = str(ev_raw_id).strip()
                if not ev_id:
                    raise FetchError(f"Polymarket malformed response at cursor {next_cursor}: event missing stable id")

                if "markets" not in ev:
                    raise FetchError(f"Polymarket malformed response at cursor {next_cursor}: event missing 'markets' list")
                if not isinstance(ev["markets"], list):
                    raise FetchError(f"Polymarket malformed response at cursor {next_cursor}: 'markets' must be a list, got {type(ev['markets']).__name__}")

                nested_market_ids: list[str] = []
                for item in ev["markets"]:
                    if not isinstance(item, dict):
                        raise FetchError(f"Polymarket malformed response at cursor {next_cursor}: market item must be a dict")
                    m_raw_id = item.get("id") or item.get("conditionId") or item.get("condition_id")
                    if m_raw_id is None:
                        raise FetchError(f"Polymarket malformed response at cursor {next_cursor}: market missing stable id")
                    m_id = str(m_raw_id).strip()
                    if not m_id:
                        raise FetchError(f"Polymarket malformed response at cursor {next_cursor}: market missing stable id")
                    nested_market_ids.append(m_id)

                page_event_signatures.append((ev_id, tuple(sorted(nested_market_ids))))

            page_identity = tuple(sorted(page_event_signatures))
            if page_identity in seen_page_identities:
                raise FetchError(f"Polymarket whole-page replay detected at cursor {next_cursor}")
            seen_page_identities.add(page_identity)

        for ev in events:
            ev_id = str(ev.get("id")) if ev.get("id") is not None else None
            ev_title = ev.get("title")
            ev_slug = ev.get("slug")

            nested_markets = ev["markets"]

            for item in nested_markets:
                m_id = str(item.get("id") or item.get("conditionId") or item.get("condition_id") or "")
                q_text = item.get("question") or item.get("slug") or m_id
                if not m_id:
                    excluded_counts["missing_id"] = excluded_counts.get("missing_id", 0) + 1
                    if len(exclusion_samples.setdefault("missing_id", [])) < 20:
                        exclusion_samples["missing_id"].append(q_text)
                    continue

                if m_id in markets_by_id:
                    continue

                is_eligible, reason = is_2026_polymarket_market(item, parent_event=ev, is_tag_102289=True)
                if not is_eligible:
                    excluded_counts[reason] = excluded_counts.get(reason, 0) + 1
                    if len(exclusion_samples.setdefault(reason, [])) < 20:
                        exclusion_samples[reason].append(q_text)
                    continue

                parsed = _parse_polymarket_tokens(item)
                if isinstance(parsed, str):
                    excluded_counts[parsed] = excluded_counts.get(parsed, 0) + 1
                    if len(exclusion_samples.setdefault(parsed, [])) < 20:
                        exclusion_samples[parsed].append(q_text)
                    continue
                outcomes, clob_tokens, prices = parsed

                outcome_token_map = dict(zip(outcomes, clob_tokens))

                best_bid = _safe_float(item.get("bestBid") if item.get("bestBid") is not None else item.get("best_bid"))
                best_ask = _safe_float(item.get("bestAsk") if item.get("bestAsk") is not None else item.get("best_ask"))
                last_trade_price = _safe_float(item.get("lastTradePrice") if item.get("lastTradePrice") is not None else item.get("last_trade_price"))

                snapshot_prices = {
                    "is_snapshot": True,
                    "outcome_prices": prices,
                    "best_bid": best_bid,
                    "best_ask": best_ask,
                    "last_trade_price": last_trade_price,
                }

                market_record = {
                    "venue": "polymarket",
                    "market_id": m_id,
                    "question": item.get("question"),
                    "slug": item.get("slug") or item.get("market_slug"),
                    "outcomes": outcomes,
                    "clob_token_ids": clob_tokens,
                    "outcome_token_map": outcome_token_map,
                    "snapshot_prices": snapshot_prices,
                    "end_date": item.get("endDate") or item.get("end_date_iso") or item.get("end_date"),
                    "resolution_description": item.get("description"),
                    "event_id": ev_id,
                    "event_title": ev_title,
                    "event_slug": ev_slug,
                    "race_type": "",
                }
                market_record["race_type"] = classify_race("polymarket", market_record)
                markets_by_id[m_id] = market_record

        if "next_cursor" not in data:
            break
        returned_cursor = data["next_cursor"]
        if not isinstance(returned_cursor, str) or not returned_cursor.strip():
            raise FetchError(f"Polymarket malformed response: next_cursor must be a nonempty string, got {type(returned_cursor).__name__ if not isinstance(returned_cursor, str) else 'empty string'}")
        next_cursor = returned_cursor

    return list(markets_by_id.values()), excluded_counts


# =====================================================================
# Kalshi Fetcher
# =====================================================================

class _KalshiTransport:
    """Own verified, reusable HTTPS connections for one fetch; never retry here."""

    def __init__(self) -> None:
        self._context = ssl.create_default_context()
        self._connections: dict[str, http.client.HTTPSConnection] = {}

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _discard(self, host: str) -> None:
        connection = self._connections.pop(host, None)
        if connection is not None:
            connection.close()

    def close(self) -> None:
        for host in list(self._connections):
            self._discard(host)

    def request(self, host: str, path: str, timeout: float = 25.0) -> tuple[int, bytes]:
        connection = self._connections.get(host)
        if connection is None:
            connection = http.client.HTTPSConnection(host, 443, context=self._context, timeout=timeout)
            self._connections[host] = connection
        try:
            connection.request("GET", path, headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            })
            response = connection.getresponse()
            # Drain error responses too: http.client requires this before reuse.
            body = response.read()
            if response.will_close:
                self._discard(host)
            return response.status, body
        except BaseException:
            # No poisoned/stale socket can be reused, even on interruption.
            self._discard(host)
            raise


def _kalshi_request(
    host: str,
    path: str,
    timeout: float = 25.0,
) -> tuple[int, bytes]:

    """One-shot diagnostic helper; production fetches own a reusable transport."""
    with _KalshiTransport() as transport:
        return transport.request(host, path, timeout)


def fetch_kalshi_markets(
    rate_limiter: RateLimiter,
    primary_host: str = "api.elections.kalshi.com",
    fallback_host: str = "external-api.kalshi.com",
    requester: Any = None,
    exclusion_samples: dict[str, list[str]] | None = None,
) -> tuple[str, list[dict[str, Any]], dict[str, int]]:
    """Fetch active 2026 midterm markets from Kalshi Trade API v2."""
    if requester is not None:
        return _fetch_kalshi_markets(rate_limiter, primary_host, fallback_host, requester, exclusion_samples)
    with _KalshiTransport() as transport:
        return _fetch_kalshi_markets(rate_limiter, primary_host, fallback_host, transport.request, exclusion_samples)


def _fetch_kalshi_markets(
    rate_limiter: RateLimiter,
    primary_host: str,
    fallback_host: str,
    requester: Any,
    exclusion_samples: dict[str, list[str]] | None,
) -> tuple[str, list[dict[str, Any]], dict[str, int]]:
    """Traverse pages with the existing bounded retries and host fallback."""
    print("Fetching Kalshi markets...", flush=True)
    active_host = primary_host
    actual_serving_host = primary_host

    markets_by_ticker: dict[str, dict[str, Any]] = {}
    excluded_counts: dict[str, int] = {}
    seen_cursors: set[str] = set()

    cursor = ""
    page = 0
    limit = 200

    while True:
        # Detect pagination cursor loop
        if cursor and cursor in seen_cursors:
            raise FetchError(f"Kalshi pagination loop detected: repeated cursor {cursor}")
        if cursor:
            seen_cursors.add(cursor)

        query_params: dict[str, Any] = {
            "status": "open",
            "limit": limit,
            "with_nested_markets": "true",
        }
        if cursor:
            query_params["cursor"] = cursor
        path = f"/trade-api/v2/events?{urllib.parse.urlencode(query_params)}"

        page_data: dict[str, Any] | None = None
        for attempt in range(5):  # 1 initial + up to 4 retries
            rate_limiter.wait()
            try:
                status, body = requester(active_host, path)

                if status == 200:
                    page_data = json.loads(body.decode("utf-8"))
                    actual_serving_host = active_host
                    break
                print(f"  Kalshi HTTP {status} from {active_host} on attempt {attempt + 1}", flush=True)
            except (http.client.HTTPException, ssl.SSLError, OSError, TimeoutError) as e:
                # Connection error: fall back immediately if on primary host
                if active_host == primary_host:
                    print(f"  Connection error on {primary_host} ({e}); falling back to {fallback_host}", flush=True)
                    active_host = fallback_host
                    actual_serving_host = fallback_host
                    # Immediate attempt on fallback host with rate limiter
                    rate_limiter.wait()
                    try:
                        status, body = requester(active_host, path)
                        if status == 200:
                            page_data = json.loads(body.decode("utf-8"))
                            break
                    except (http.client.HTTPException, ssl.SSLError, OSError, TimeoutError) as fb_err:
                        e = fb_err
                if attempt == 4:
                    raise FetchError(f"Exhausted 4 retries on {active_host} for page {page + 1}: {e}") from e

                backoff = 1.0 * (1.8 ** attempt) + random.uniform(0.2, 0.5)
                print(f"  Kalshi connection reset ({e}); retrying in {backoff:.2f}s (retry {attempt + 1}/4)...", flush=True)
                time.sleep(backoff)

        if page_data is None:
            raise FetchError(f"Failed to retrieve Kalshi page {page + 1} after retries")
        if not isinstance(page_data, dict):
            raise FetchError(f"Kalshi malformed response on page {page + 1}: expected JSON object")

        events = page_data.get("events")
        if not isinstance(events, list):
            raise FetchError(f"Kalshi malformed response on page {page + 1}: expected 'events' list")

        if "cursor" not in page_data:
            raise FetchError(f"Kalshi malformed response on page {page + 1}: missing 'cursor'")
        new_cursor = page_data["cursor"]
        if not isinstance(new_cursor, str):
            raise FetchError(
                f"Kalshi malformed response on page {page + 1}: "
                f"'cursor' must be a string, got {type(new_cursor).__name__}"
            )
        page += 1

        for ev in events:
            series_ticker = ev.get("series_ticker") or ""
            ev_title = ev.get("title") or ""
            ev_subtitle = ev.get("sub_title") or ""
            ev_ticker = ev.get("event_ticker") or ""

            full_ev_title = (
                f"{ev_title} - {ev_subtitle}".strip(" -")
                if ev_subtitle and ev_subtitle.lower() not in ev_title.lower()
                else ev_title
            )

            for m in ev.get("markets") or []:
                m_ticker = m.get("ticker") or ""
                if not m_ticker:
                    excluded_counts["missing_market_ticker"] = excluded_counts.get("missing_market_ticker", 0) + 1
                    continue

                m_series = m.get("series_ticker") or series_ticker
                m_title = m.get("title") or ev_title

                is_eligible, filter_note = is_2026_kalshi_market(m, ev)
                if not is_eligible:
                    reason = filter_note or "ineligible"
                    excluded_counts[reason] = excluded_counts.get(reason, 0) + 1
                    if exclusion_samples is not None:
                        t = m.get("title") or ev_title or m_ticker
                        if t and len(exclusion_samples.setdefault(reason, [])) < 20 and t not in exclusion_samples[reason]:
                            exclusion_samples[reason].append(t)
                    continue

                rules_primary = m.get("rules_primary") or ""
                rules_secondary = m.get("rules_secondary") or ""
                rules_text = f"{rules_primary}\n{rules_secondary}".strip()

                yes_bid_dollars = m.get("yes_bid_dollars")
                yes_ask_dollars = m.get("yes_ask_dollars")
                last_price_dollars = m.get("last_price_dollars")
                yes_bid = m.get("yes_bid")
                yes_ask = m.get("yes_ask")
                last_price = m.get("last_price")

                snapshot_prices = {
                    "is_snapshot": True,
                    "yes_bid_dollars": yes_bid_dollars,
                    "yes_ask_dollars": yes_ask_dollars,
                    "last_price_dollars": last_price_dollars,
                    "yes_bid": yes_bid,
                    "yes_ask": yes_ask,
                    "last_price": last_price,
                    "yes_bid_cents": _to_cents(yes_bid_dollars, yes_bid),
                    "yes_ask_cents": _to_cents(yes_ask_dollars, yes_ask),
                    "last_price_cents": _to_cents(last_price_dollars, last_price),
                }

                subtitle = m.get("yes_sub_title") or m.get("subtitle") or ev_subtitle

                market_record = {
                    "venue": "kalshi",
                    "market_ticker": m_ticker,
                    "event_ticker": ev_ticker,
                    "series_ticker": m_series,
                    "title": m_title,
                    "subtitle": subtitle,
                    "event_title": full_ev_title,
                    "snapshot_prices": snapshot_prices,
                    "close_time": m.get("close_time"),
                    "rules_text": rules_text,
                    "filter_note": filter_note,
                    "race_type": "",
                }
                market_record["race_type"] = classify_race("kalshi", market_record)
                markets_by_ticker[m_ticker] = market_record

        if page % 5 == 0 or not new_cursor:
            print(f"  Kalshi page {page}: total kept markets so far: {len(markets_by_ticker)}", flush=True)

        if not new_cursor:
            break
        cursor = new_cursor

    print(f"Kalshi complete. Served by {actual_serving_host}. Total markets: {len(markets_by_ticker)}", flush=True)
    return actual_serving_host, list(markets_by_ticker.values()), excluded_counts


# =====================================================================
# Validation & Atomic Output
# =====================================================================

def _safe_print(msg: str = "", file: Any = None, **kwargs: Any) -> None:
    """Print message to target stream without failing on character encoding limitations."""
    target = file if file is not None else sys.stdout
    enc = getattr(target, "encoding", None) or "utf-8"
    try:
        msg.encode(enc)
        safe_msg = msg
    except UnicodeEncodeError:
        safe_msg = msg.encode(enc, errors="backslashreplace").decode(enc)
    print(safe_msg, file=target, **kwargs)


def validate_dataset(data: dict[str, Any]) -> list[str]:
    """Inspect dataset and return a list of failure descriptions, or empty list if valid."""
    errors: list[str] = []
    polymarket_markets = data.get("polymarket", [])
    kalshi_markets = data.get("kalshi", [])

    if len(polymarket_markets) < 100:
        errors.append(f"Polymarket has fewer than 100 markets ({len(polymarket_markets)})")
    if len(kalshi_markets) < 100:
        errors.append(f"Kalshi has fewer than 100 markets ({len(kalshi_markets)})")

    # ID integrity
    poly_ids = [m.get("market_id") for m in polymarket_markets]
    if any(not m_id for m_id in poly_ids):
        errors.append("Polymarket has markets missing market_id")
    if len(poly_ids) != len(set(filter(None, poly_ids))):
        errors.append("Polymarket has duplicate market_ids")

    kalshi_ids = [m.get("market_ticker") for m in kalshi_markets]
    if any(not t for t in kalshi_ids):
        errors.append("Kalshi has markets missing market_ticker")
    if len(kalshi_ids) != len(set(filter(None, kalshi_ids))):
        errors.append("Kalshi has duplicate market_tickers")

    # Chamber control checks
    poly_has_senate_ctrl = any("senate" in (m.get("question") or "").lower() and classify_race("polymarket", m) == "control" for m in polymarket_markets)
    poly_has_house_ctrl = any("house" in (m.get("question") or "").lower() and classify_race("polymarket", m) == "control" for m in polymarket_markets)
    kalshi_has_senate_ctrl = any(("CONTROLS" in (m.get("series_ticker") or "") or "senate" in (m.get("title") or "").lower()) and classify_race("kalshi", m) == "control" for m in kalshi_markets)
    kalshi_has_house_ctrl = any(("CONTROLH" in (m.get("series_ticker") or "") or "house" in (m.get("title") or "").lower()) and classify_race("kalshi", m) == "control" for m in kalshi_markets)

    if not poly_has_senate_ctrl:
        errors.append("Polymarket missing Senate Control market")
    if not poly_has_house_ctrl:
        errors.append("Polymarket missing House Control market")
    if not kalshi_has_senate_ctrl:
        errors.append("Kalshi missing Senate Control market")
    if not kalshi_has_house_ctrl:
        errors.append("Kalshi missing House Control market")

    # Strengthened validation: Ineligible contest leaks
    bad_poly_ltgov = [
        m.get("market_id") for m in polymarket_markets
        if _LT_GOV_RE.search(f"{m.get('question', '')} {m.get('slug', '')} {m.get('event_title', '')}")
    ]
    if bad_poly_ltgov:
        errors.append(f"Polymarket contains {len(bad_poly_ltgov)} Lieutenant Governor contracts: {bad_poly_ltgov[:5]}")

    bad_kalshi_ltgov = [
        m.get("market_ticker") for m in kalshi_markets
        if "LTGOV" in (m.get("market_ticker") or "") or "LTGOV" in (m.get("series_ticker") or "")
        or "LIEUTENANTGOV" in (m.get("market_ticker") or "") or "LIEUTENANTGOV" in (m.get("series_ticker") or "")
        or _LT_GOV_RE.search(f"{m.get('title', '')} {m.get('subtitle', '')} {m.get('event_title', '')}")
    ]
    if bad_kalshi_ltgov:
        errors.append(f"Kalshi contains {len(bad_kalshi_ltgov)} Lieutenant Governor contracts: {bad_kalshi_ltgov[:5]}")

    bad_kalshi_stateleg = [
        m.get("market_ticker") for m in kalshi_markets
        if "STATESUPERMAJ" in (m.get("series_ticker") or "") or "STATESUPERMAJ" in (m.get("market_ticker") or "")
        or _is_state_legislative_contest(
            f"{m.get('title', '')} {m.get('subtitle', '')} {m.get('event_title', '')}",
            structured=_kalshi_structured_contest(m.get("series_ticker") or "", m.get("market_ticker") or ""),
        )
    ]
    if bad_kalshi_stateleg:
        errors.append(f"Kalshi contains {len(bad_kalshi_stateleg)} state-legislature contracts: {bad_kalshi_stateleg[:5]}")

    bad_kalshi_ctrl = [
        m.get("market_ticker") for m in kalshi_markets
        if m.get("race_type") == "control"
        and _kalshi_structured_contest(m.get("series_ticker") or "", m.get("market_ticker") or "") in ("senate", "house", "governor")
    ]
    if bad_kalshi_ctrl:
        errors.append(f"Kalshi contains {len(bad_kalshi_ctrl)} state-specific contests misclassified as control: {bad_kalshi_ctrl[:5]}")

    bad_within5_wi = []
    for m in kalshi_markets:
        s_ticker = m.get("series_ticker") or ""
        m_ticker = m.get("market_ticker") or ""
        if ("WITHIN" in s_ticker or "WITHIN" in m_ticker) and extract_race_state("kalshi", m, "senate") == "wisconsin":
            text_corpus = f"{m.get('title', '')} {m.get('subtitle', '')} {m.get('event_title', '')}".lower()
            has_wi_suffix = bool(re.search(r"[-_]WI(?:[-_].*)?$", m_ticker))
            has_wi_text = "wisconsin" in text_corpus
            if not (has_wi_suffix or has_wi_text):
                bad_within5_wi.append(m_ticker)
    if bad_within5_wi:
        errors.append(f"Kalshi contains {len(bad_within5_wi)} WITHIN contracts falsely extracted as Wisconsin: {bad_within5_wi[:5]}")

    return errors


def publish_output(dated_file: Path, latest_file: Path, output_payload: dict[str, Any]) -> None:
    """Atomically write and replace output files through temporary files."""
    dated_file.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(output_payload, indent=2)

    tmp_dated = dated_file.with_suffix(".tmp")
    tmp_latest = latest_file.with_suffix(".tmp")

    try:
        with open(tmp_dated, "w", encoding="utf-8") as f:
            f.write(serialized)
        with open(tmp_latest, "w", encoding="utf-8") as f:
            f.write(serialized)

        # Atomic replacement
        os.replace(tmp_dated, dated_file)
        os.replace(tmp_latest, latest_file)
    finally:
        if tmp_dated.exists():
            tmp_dated.unlink()
        if tmp_latest.exists():
            tmp_latest.unlink()


def run_validation(latest_path: Path) -> int:
    """Validate and summarize the latest output file."""
    if not latest_path.exists():
        _safe_print(f"Error: {latest_path} does not exist. Run fetch first.", file=sys.stderr)
        return 1

    with open(latest_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    fetched_at = data.get("fetched_at")
    kalshi_host = data.get("kalshi_host")
    polymarket_markets = data.get("polymarket", [])
    kalshi_markets = data.get("kalshi", [])

    _safe_print("=" * 70)
    _safe_print("VENUE MARKETS VALIDATION REPORT")
    _safe_print("=" * 70)
    _safe_print(f"Fetched At:   {fetched_at}")
    _safe_print(f"Kalshi Host:  {kalshi_host}")
    _safe_print(f"Total Counts: Polymarket: {len(polymarket_markets)}, Kalshi: {len(kalshi_markets)}")
    _safe_print("-" * 70)

    # 1. Duplicate & Missing IDs
    poly_ids = [m.get("market_id") for m in polymarket_markets]
    poly_missing = sum(1 for m_id in poly_ids if not m_id)
    poly_dupes = len(poly_ids) - len(set(filter(None, poly_ids)))

    kalshi_ids = [m.get("market_ticker") for m in kalshi_markets]
    kalshi_missing = sum(1 for t in kalshi_ids if not t)
    kalshi_dupes = len(kalshi_ids) - len(set(filter(None, kalshi_ids)))

    _safe_print("ID INTEGRITY:")
    _safe_print(f"  Polymarket missing IDs: {poly_missing}, duplicates: {poly_dupes}")
    _safe_print(f"  Kalshi missing IDs:     {kalshi_missing}, duplicates: {kalshi_dupes}")
    _safe_print("-" * 70)

    # 2. Race Type Counts
    poly_types: dict[str, int] = {}
    for m in polymarket_markets:
        rtype = classify_race("polymarket", m)
        poly_types[rtype] = poly_types.get(rtype, 0) + 1

    kalshi_types: dict[str, int] = {}
    for m in kalshi_markets:
        rtype = classify_race("kalshi", m)
        kalshi_types[rtype] = kalshi_types.get(rtype, 0) + 1

    _safe_print("MARKET COUNTS BY RACE TYPE:")
    for rtype in ["senate", "house", "governor", "control", "other"]:
        _safe_print(f"  {rtype.capitalize():<10} -> Polymarket: {poly_types.get(rtype, 0):<5} | Kalshi: {kalshi_types.get(rtype, 0):<5}")
    _safe_print("-" * 70)

    # 3. Chamber Control Presence
    poly_has_senate_ctrl = any("senate" in (m.get("question") or "").lower() and classify_race("polymarket", m) == "control" for m in polymarket_markets)
    poly_has_house_ctrl = any("house" in (m.get("question") or "").lower() and classify_race("polymarket", m) == "control" for m in polymarket_markets)
    kalshi_has_senate_ctrl = any(("CONTROLS" in (m.get("series_ticker") or "") or "senate" in (m.get("title") or "").lower()) and classify_race("kalshi", m) == "control" for m in kalshi_markets)
    kalshi_has_house_ctrl = any(("CONTROLH" in (m.get("series_ticker") or "") or "house" in (m.get("title") or "").lower()) and classify_race("kalshi", m) == "control" for m in kalshi_markets)

    _safe_print("CHAMBER CONTROL MARKETS:")
    _safe_print(f"  Polymarket Senate Control Present: {'YES' if poly_has_senate_ctrl else 'NO'}")
    _safe_print(f"  Polymarket House Control Present:  {'YES' if poly_has_house_ctrl else 'NO'}")
    _safe_print(f"  Kalshi Senate Control Present:     {'YES' if kalshi_has_senate_ctrl else 'NO'}")
    _safe_print(f"  Kalshi House Control Present:      {'YES' if kalshi_has_house_ctrl else 'NO'}")
    _safe_print("-" * 70)

    # 4. Distinct State Coverage
    poly_senate_states = {
        extract_race_state("polymarket", m, "senate")
        for m in polymarket_markets
        if classify_race("polymarket", m) == "senate"
    } - {None}

    poly_gov_states = {
        extract_race_state("polymarket", m, "governor")
        for m in polymarket_markets
        if classify_race("polymarket", m) == "governor"
    } - {None}

    kalshi_senate_states = {
        extract_race_state("kalshi", m, "senate")
        for m in kalshi_markets
        if classify_race("kalshi", m) == "senate"
    } - {None}

    kalshi_gov_states = {
        extract_race_state("kalshi", m, "governor")
        for m in kalshi_markets
        if classify_race("kalshi", m) == "governor"
    } - {None}

    _safe_print("DISTINCT STATE COVERAGE:")
    _safe_print(f"  Polymarket States with Senate Markets:   {len(poly_senate_states)}")
    _safe_print(f"  Polymarket States with Governor Markets: {len(poly_gov_states)}")
    _safe_print(f"  Kalshi States with Senate Markets:       {len(kalshi_senate_states)}")
    _safe_print(f"  Kalshi States with Governor Markets:     {len(kalshi_gov_states)}")
    _safe_print("  (Note: Expected 35 Senate races (33 regular + Ohio and Florida specials) and 36 governor races)")
    _safe_print("-" * 70)

    exclusion_samples = data.get("exclusion_samples", {})
    if exclusion_samples:
        _safe_print("EXCLUSION SAMPLES (up to 20 per reason):")
        for venue_name in ("polymarket", "kalshi"):
            venue_dict = exclusion_samples.get(venue_name, {})
            if not venue_dict:
                continue
            _safe_print(f"  Venue: {venue_name.capitalize()}")
            for reason, samples in sorted(venue_dict.items()):
                _safe_print(f"    [{reason}] ({len(samples)} sample{'s' if len(samples) != 1 else ''}):")
                for s in samples[:20]:
                    _safe_print(f"      - {s}")
        _safe_print("=" * 70)
    else:
        _safe_print("=" * 70)

    errors = validate_dataset(data)
    if errors:
        _safe_print("VALIDATION FAILURES DETECTED:", file=sys.stderr)
        for err in errors:
            _safe_print(f"  - {err}", file=sys.stderr)
        return 1

    _safe_print("VALIDATION SUCCESSFUL: All checks passed.")
    return 0


# =====================================================================
# Main CLI
# =====================================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch 2026 US midterm election markets from Polymarket and Kalshi.")
    parser.add_argument("--validate", action="store_true", help="Validate existing latest output without re-fetching.")
    args = parser.parse_args()

    base_dir = Path(__file__).resolve().parent
    output_dir = base_dir / "output"
    latest_file = output_dir / "venue_markets_latest.json"

    if args.validate:
        sys.exit(run_validation(latest_file))

    today_str = datetime.now(UTC).strftime("%Y-%m-%d")
    dated_file = output_dir / f"venue_markets_{today_str}.json"

    rate_limiter = RateLimiter(MIN_REQUEST_INTERVAL)
    poly_samples: dict[str, list[str]] = {}
    kalshi_samples: dict[str, list[str]] = {}
    try:
        polymarket_markets, poly_excluded = fetch_polymarket_markets(rate_limiter, exclusion_samples=poly_samples)
        kalshi_host, kalshi_markets, kalshi_excluded = fetch_kalshi_markets(rate_limiter, exclusion_samples=kalshi_samples)
    except Exception as e:  # noqa: BLE001
        _safe_print(f"\nFETCH FAILED: {e}", file=sys.stderr, flush=True)
        _safe_print("Previous latest output preserved byte-for-byte; no output updated.", file=sys.stderr, flush=True)
        sys.exit(1)

    _safe_print(f"Found {len(polymarket_markets)} eligible markets from Polymarket (exclusions: {poly_excluded})", flush=True)
    _safe_print(f"Found {len(kalshi_markets)} eligible markets from Kalshi (exclusions: {kalshi_excluded})", flush=True)

    output_payload = {
        "fetched_at": datetime.now(UTC).isoformat(),
        "kalshi_host": kalshi_host,
        "polymarket_count": len(polymarket_markets),
        "kalshi_count": len(kalshi_markets),
        "exclusion_samples": {
            "polymarket": poly_samples,
            "kalshi": kalshi_samples,
        },
        "polymarket": polymarket_markets,
        "kalshi": kalshi_markets,
    }

    errors = validate_dataset(output_payload)
    if errors:
        _safe_print("\nVALIDATION FAILED ON NEW DATASET:", file=sys.stderr, flush=True)
        for err in errors:
            _safe_print(f"  - {err}", file=sys.stderr, flush=True)
        _safe_print("Previous latest output preserved byte-for-byte; no output updated.", file=sys.stderr, flush=True)
        sys.exit(1)

    publish_output(dated_file, latest_file, output_payload)
    _safe_print(f"Saved {len(polymarket_markets) + len(kalshi_markets)} markets to {dated_file}", flush=True)
    _safe_print(f"Atomically updated {latest_file}", flush=True)

    _safe_print("\nRunning validation on published dataset...", flush=True)
    sys.exit(run_validation(latest_file))


if __name__ == "__main__":
    main()
