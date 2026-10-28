"""SIG contract identity proposals, race keys and OQ-C4 group proposals.

Everything here is a *proposal* read from the SIG market title. A title is not an
authoritative race ID, stage ID or resolution rule, so a proposal never establishes a
race on its own: ``identity.status`` stays ``unresolved`` until a reviewed source supplies
the race identity (see ``markets/README.md``).
"""

from __future__ import annotations

import re
from typing import Any

from data.venue_markets.fetch_venue_markets import STATE_ABBRS

STATE_CODE_BY_NAME = {name: code for code, name in STATE_ABBRS.items()}
PARTY_CODES = {"Democratic": "D", "Republican": "R", "Independent": "I"}

OFFICES = ("senate", "governor", "house", "senate_control", "house_control")
ELECTION_TYPES = ("unverified", "regular", "special")
CYCLES = ("unverified", "2026")

_PARTY = r"(?P<party>Democratic|Republican|Independent)"
TITLE_SHAPES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("chamber", re.compile(rf"^Will the {_PARTY} Party win the U\.S\. (?P<chamber>House|Senate)\?$")),
    ("house_district", re.compile(
        rf"^Will the {_PARTY} Party win the (?P<st>[A-Z]{{2}})-(?P<dist>\d{{2}}|AL) House race\?$")),
    ("statewide", re.compile(
        rf"^Will the {_PARTY} Party win the (?P<state>[A-Z][A-Za-z ]+?) (?P<office>Senate|Governor)\?$")),
)

PROPOSAL_BASIS = "sig_title_grammar"


def parse_sig_title(title: Any) -> dict[str, Any] | None:
    """Read office, state, district and party from a SIG title, or ``None``.

    Only the three shapes observed in the recorded inventory are accepted, and the whole
    title must match. Anything else is left unparsed (identity unresolved).
    """
    if not isinstance(title, str):
        return None
    for shape, pattern in TITLE_SHAPES:
        m = pattern.match(title)
        if not m:
            continue
        party = PARTY_CODES[m["party"]]
        if shape == "chamber":
            office = "senate_control" if m["chamber"] == "Senate" else "house_control"
            return {"shape": shape, "office": office, "state": None, "district": None,
                    "party": party}
        if shape == "house_district":
            if m["st"] not in STATE_ABBRS or m["dist"] in ("00",):
                return None
            return {"shape": shape, "office": "house", "state": m["st"],
                    "district": m["dist"], "party": party}
        state = STATE_CODE_BY_NAME.get(m["state"].lower())
        if state is None:
            return None
        return {"shape": shape, "office": m["office"].lower(), "state": state,
                "district": None, "party": party}
    return None


def race_key(office: str, state: str | None, district: str | None,
             election_type: str = "unverified") -> str:
    """Canonical race key for a contract's identity fields.

    A special election never shares a key with the regular race for the same seat, and
    chamber-control contracts never share a key with any state race.
    """
    if office in ("senate_control", "house_control"):
        return office
    if office == "house":
        base = f"house:{state}-{district}"
    else:
        base = f"{office}:{state}"
    return f"{base}:special" if election_type == "special" else base


def proposed_identity(title: Any) -> dict[str, Any] | None:
    parsed = parse_sig_title(title)
    if parsed is None:
        return None
    return {
        "basis": PROPOSAL_BASIS,
        "shape": parsed["shape"],
        "office": parsed["office"],
        "state": parsed["state"],
        "district": parsed["district"],
        "party": parsed["party"],
        "election_type": "unverified",
        "cycle": "unverified",
        "race_key": race_key(parsed["office"], parsed["state"], parsed["district"]),
    }


# OQ-C4 candidates from the PRD, as proposals only. Membership is computed from identity
# fields so a mistaken grouping (for example House control in the Senate group) is caught.
GROUP_PROPOSALS: tuple[dict[str, str], ...] = (
    {"id": "chamber:senate",
     "basis": "PRD OQ-C4 candidate: Senate control with all Senate races",
     "rule": "office in {senate, senate_control}"},
    {"id": "chamber:house",
     "basis": "PRD OQ-C4 candidate: House control with all House races",
     "rule": "office in {house, house_control}"},
    {"id": "state:<ST>",
     "basis": "PRD OQ-C4 candidate: races in the same state",
     "rule": "one group per state code; chamber-control contracts have no state"},
    {"id": "party-outcome:<P>",
     "basis": ("Proxy for the PRD OQ-C4 'same lean' candidate (a uniform polling miss): "
               "contracts paying on the same party. Race lean itself is not in reviewed "
               "inputs, so this is not the lean grouping."),
     "rule": "one group per contract party"},
)


def proposed_group_ids(identity: dict[str, Any] | None) -> list[str]:
    """Every OQ-C4 group a contract would join under each candidate definition.

    A contract can appear in several proposals; nothing here picks one definition.
    """
    if identity is None:
        return []
    office = identity.get("office")
    groups: list[str] = []
    if office in ("senate", "senate_control"):
        groups.append("chamber:senate")
    if office in ("house", "house_control"):
        groups.append("chamber:house")
    if identity.get("state"):
        groups.append(f"state:{identity['state']}")
    if identity.get("party"):
        groups.append(f"party-outcome:{identity['party']}")
    return groups
