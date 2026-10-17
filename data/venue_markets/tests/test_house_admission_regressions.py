"""Regression tests for S1-T5-F6: bounded House party-winner admission.

Two narrow admission paths are covered:

1. Polymarket House party-winner markets whose identity text omits the election year.
   They are admitted only when the market question, slug, parent event and the COMPLETE
   opening resolution clause agree on one district and parse under the supported grammar.
2. Kalshi generic ``KXHOUSERACE-<ST><DD>-26-<D|R>`` markets. They are admitted only when the
   explicit market/event/series relationships hold and the COMPLETE decisive
   ``rules_primary`` sentence parses under the supported grammar.

Fixture provenance labels used below:

- RECORDED: field values copied from the cached Gamma / Kalshi responses of the
  October 11-12 S1-T5-F5 audit (Polymarket events 190872, 90270, 726881; Kalshi events
  KXHOUSERACE-AL02-26, -CA06-26, -DEAL-26, -WV02-26). Only the eligibility-relevant fields
  are kept.
- RECORDED TEMPLATE: the recorded wording with only the district substituted. Every one of
  the 92 cached Polymarket party contracts and 697 of the 706 cached Kalshi contracts use
  exactly this wording.
- SYNTHETIC: constructed counterexamples or controls. They are NOT claims that the venues
  publish such contracts. The ten ``TestReviewProbeRegressions`` cases are synthetic
  mutations of the recorded AL-02 records taken from the S1-T5-F6-C1 reviewer probe.
"""

from __future__ import annotations

from typing import Any

import pytest

import data.venue_markets.fetch_venue_markets as fvm
from data.venue_markets.fetch_venue_markets import (
    classify_race,
    is_2026_kalshi_market,
    is_2026_polymarket_market,
)

# ---------------------------------------------------------------------
# RECORDED wording
# ---------------------------------------------------------------------

# RECORDED: Polymarket market 1280801 (event 190872), including the zero-width space that
# precedes the second paragraph in the cached response.
RECORDED_PM_DESCRIPTION_TEMPLATE = (
    "This market will resolve according to the party of the candidate who wins the {district} "
    "congressional district seat in the U.S. House of Representatives in the 2026 midterm "
    "elections. The midterm elections will take place on November 3, 2026.\n\n"
    "\u200bA candidate's party will be determined by their ballot-listed or otherwise "
    "identifiable affiliation with that party at the time all of the 2026 House elections are "
    "conclusively called by this market's resolution sources. A candidate without a "
    "ballot-listed affiliation to either the Democrat or Republican parties will be considered "
    "a member of one of these parties based on the party with which they most recently "
    "expressed their intent to caucus at the time all of the House elections are conclusively "
    "called by this market's resolution sources.\n\n"
    "This market will resolve based on the result of the election as indicated by a consensus "
    "of credible reporting. If there is ambiguity, this market will resolve based solely on the "
    "official results as reported by the United States government, specifically the Federal "
    "Election Commission (https://www.fec.gov/)."
)

# RECORDED: Kalshi KXHOUSERACE rules (697 of 706 cached contracts use this sentence).
RECORDED_KALSHI_RULES_TEMPLATE = (
    "If the House member sworn in for {district} for the term beginning in 2027 is a member of "
    "the {party} Party, then the market resolves to Yes."
)
RECORDED_KALSHI_RULES_SECONDARY = (
    "This market is eligible for accelerated determination after a consensus of media "
    "organizations project the winner. See full rules for details."
)

# The 46 SIG House races (historical SIG market CSV) and the 17 that lacked direct
# matches in S1-T5-F4.
SIG_HOUSE_DISTRICTS_46 = [
    "AL-02", "AZ-01", "AZ-06", "CA-22", "CA-48", "CO-03", "CO-04", "CO-08", "FL-07", "FL-09",
    "FL-13", "FL-16", "FL-20", "FL-22", "IA-01", "IA-02", "IA-03", "KY-04", "ME-02", "MI-07",
    "MI-10", "MN-01", "MN-02", "MN-05", "MT-01", "NE-02", "NH-01", "NJ-07", "NY-13", "NY-17",
    "PA-01", "PA-07", "PA-08", "PA-10", "SC-01", "TN-05", "TX-15", "TX-23", "TX-28", "VA-01",
    "VA-02", "VA-05", "VA-07", "WA-03", "WI-01", "WI-03",
]
DISTRICTS_17 = [
    ("AL", "02"), ("CA", "48"), ("CO", "04"), ("FL", "07"),
    ("FL", "09"), ("FL", "16"), ("FL", "20"), ("FL", "22"),
    ("IA", "02"), ("KY", "04"), ("MN", "01"), ("MN", "05"),
    ("NY", "13"), ("SC", "01"), ("TN", "05"), ("TX", "23"),
    ("VA", "05"),
]

PARTIES = [
    ("Democratic", "D"),
    ("Republican", "R"),
]


def _pm_market(district: str, party: str, **overrides: Any) -> dict[str, Any]:
    """RECORDED TEMPLATE Polymarket party-winner market for ``district`` (e.g. 'AL-02')."""
    market = {
        "id": f"pm-{district}-{party}",
        "question": f"Will the {party} Party win the {district} House seat?",
        "slug": f"will-the-{party.lower()}-party-win-the-{district.lower()}-house-seat",
        "active": True,
        "closed": False,
        "description": RECORDED_PM_DESCRIPTION_TEMPLATE.format(district=district),
    }
    market.update(overrides)
    return market


def _pm_event(district: str, **overrides: Any) -> dict[str, Any]:
    event = {
        "id": f"ev-{district}",
        "title": f"{district} House Election Winner",
        "slug": f"{district.lower()}-house-election-winner",
    }
    event.update(overrides)
    return event


def _pm_opening(district: str = "AL-02") -> str:
    """Opening clause of the recorded description, without its final period."""
    return RECORDED_PM_DESCRIPTION_TEMPLATE.format(district=district).split(". The midterm")[0]


def _k_event(district_code: str = "AL02", **overrides: Any) -> dict[str, Any]:
    """RECORDED shape of a KXHOUSERACE event (e.g. KXHOUSERACE-AL02-26)."""
    dash = f"{district_code[:2]}-{district_code[2:]}"
    event = {
        "event_ticker": f"KXHOUSERACE-{district_code}-26",
        "series_ticker": "KXHOUSERACE",
        "title": f"{dash} House winner?",
        "sub_title": dash,
    }
    event.update(overrides)
    return event


def _k_market(district_code: str = "AL02", party_code: str = "D", **overrides: Any) -> dict[str, Any]:
    """RECORDED shape of a KXHOUSERACE party market; market.series_ticker is null upstream."""
    party = "Democratic" if party_code == "D" else "Republican"
    dash = f"{district_code[:2]}-{district_code[2:]}"
    market = {
        "ticker": f"KXHOUSERACE-{district_code}-26-{party_code}",
        "event_ticker": f"KXHOUSERACE-{district_code}-26",
        "series_ticker": None,
        "title": f"Will {party} win the House race for {dash}?",
        "subtitle": f"{party} party:: {party} party",
        "status": "active",
        "rules_primary": RECORDED_KALSHI_RULES_TEMPLATE.format(district=dash, party=party),
        "rules_secondary": RECORDED_KALSHI_RULES_SECONDARY,
    }
    market.update(overrides)
    return market


def _k_rules(condition: str) -> str:
    return f"If {condition}, then the market resolves to Yes."


# =====================================================================
# Recorded upstream excerpts
# =====================================================================


class TestRecordedPolymarketExcerpts:
    """RECORDED Polymarket records and the recorded wording across the 46 SIG House races."""

    @pytest.mark.parametrize("market_id,party", [("1280800", "Republican"), ("1280801", "Democratic")])
    def test_recorded_al02_event_190872_admitted(self, market_id: str, party: str) -> None:
        item = _pm_market("AL-02", party, id=market_id)
        event = {"id": "190872", "title": "AL-02 House Election Winner", "slug": "al-02-house-election-winner"}
        assert is_2026_polymarket_market(item, parent_event=event, is_tag_102289=True) == (True, "verified_2026")

    @pytest.mark.parametrize("market_id,party", [("703685", "Republican"), ("703686", "Democratic")])
    def test_recorded_ca22_event_90270_admitted(self, market_id: str, party: str) -> None:
        item = _pm_market("CA-22", party, id=market_id)
        event = {
            "id": "90270",
            "title": "CA-22 House Election Winner",
            "slug": "which-party-will-win-the-house-race-for-the-ca-22-seat",
        }
        assert is_2026_polymarket_market(item, parent_event=event, is_tag_102289=True) == (True, "verified_2026")

    @pytest.mark.parametrize(
        "market_id,party,slug",
        [
            ("3006482", "Republican", "will-the-republican-party-win-the-me-01-house-seat-20260720160014015"),
            ("3006483", "Democratic", "will-the-democratic-party-win-the-me-01-house-seat-20260720160014016"),
        ],
    )
    def test_recorded_me02_event_726881_conflicting_slug_rejected(
        self, market_id: str, party: str, slug: str
    ) -> None:
        """Recorded ME-02 markets keep their recorded me-01 slugs and stay excluded."""
        item = _pm_market("ME-02", party, id=market_id, slug=slug)
        event = {
            "id": "726881",
            "title": "ME-02 House Election Winner",
            "slug": "me-02-house-election-winner-20260720160226434",
        }
        assert is_2026_polymarket_market(item, parent_event=event, is_tag_102289=True) == (
            False,
            "mismatched_district",
        )

    @pytest.mark.parametrize("district", SIG_HOUSE_DISTRICTS_46)
    @pytest.mark.parametrize("party", ["Democratic", "Republican"])
    def test_recorded_template_admitted_for_all_46_sig_races(self, district: str, party: str) -> None:
        eligible, reason = is_2026_polymarket_market(
            _pm_market(district, party), parent_event=_pm_event(district), is_tag_102289=True
        )
        assert (eligible, reason) == (True, "verified_2026")

    def test_recorded_clause_fields_are_extracted(self) -> None:
        evidence, reason, background = fvm._parse_polymarket_house_clause(
            RECORDED_PM_DESCRIPTION_TEMPLATE.format(district="AL-02")
        )
        assert reason is None
        assert evidence == fvm.HouseResolutionEvidence(
            venue="polymarket",
            shape="polymarket_party_of_winner",
            subject="candidate_who_wins_district_seat",
            office="us_house",
            state="AL",
            district="2",
            party=None,
            polarity="affirmative",
            payout="party_of_winner",
            election_year=2026,
            term_year=None,
        )
        # Five recorded boilerplate sentences follow the opening clause.
        assert len(background) == 5


class TestRecordedKalshiExcerpts:
    """RECORDED Kalshi KXHOUSERACE records."""

    @pytest.mark.parametrize("party_code", ["D", "R"])
    # OR01 and DEAL pin R3 false rejections: R3 read the "OR" state code as the word "or" and
    # rejected every at-large district as a mismatch.
    @pytest.mark.parametrize("district_code", ["AL02", "WV02", "OR01", "DEAL"])
    def test_recorded_party_markets_admitted(self, district_code: str, party_code: str) -> None:
        assert is_2026_kalshi_market(_k_market(district_code, party_code), _k_event(district_code)) == (True, None)

    @pytest.mark.parametrize("party", ["Democratic", "Republican"])
    def test_recorded_ca06_lowercase_party_wording_admitted(self, party: str) -> None:
        """RECORDED: CA-06, IL-04, MN-05 and NE-01 rules spell 'party' in lower case."""
        market = _k_market(
            "CA06",
            party[0],
            rules_primary=(
                "If the House member sworn in for CA-06 for the term beginning in 2027 is a member of "
                f"the {party} party, then the market resolves to Yes."
            ),
        )
        assert is_2026_kalshi_market(market, _k_event("CA06")) == (True, None)

    def test_recorded_named_candidate_market_rejected(self) -> None:
        """RECORDED: KXHOUSERACE-CA06-26-KKIL is an independent candidate's contract."""
        market = _k_market(
            "CA06",
            "D",
            ticker="KXHOUSERACE-CA06-26-KKIL",
            title="Will Kevin Kiley win the House race for CA-06?",
            subtitle="Kevin Kiley:: Independent",
            rules_primary=(
                "If the House member sworn in for CA-06 for the term beginning in 2027 is Kevin Kiley "
                "(as an independent), then the market resolves to Yes."
            ),
        )
        assert is_2026_kalshi_market(market, _k_event("CA06")) == (False, "arbitrary_ticker_suffix")

    @pytest.mark.parametrize("st,dist", DISTRICTS_17)
    @pytest.mark.parametrize("party_code", ["D", "R"])
    def test_recorded_template_admitted_for_17_previously_missing(self, st: str, dist: str, party_code: str) -> None:
        assert is_2026_kalshi_market(_k_market(f"{st}{dist}", party_code), _k_event(f"{st}{dist}")) == (True, None)

    def test_recorded_rules_fields_are_extracted(self) -> None:
        evidence, reason, background = fvm._parse_kalshi_house_rules(
            RECORDED_KALSHI_RULES_TEMPLATE.format(district="AL-02", party="Republican")
        )
        assert reason is None
        assert background == []
        assert evidence == fvm.HouseResolutionEvidence(
            venue="kalshi",
            shape="kalshi_sworn_member_party",
            subject="house_member_sworn_in",
            office="us_house",
            state="AL",
            district="2",
            party="R",
            polarity="affirmative",
            payout="yes",
            election_year=None,
            term_year=2027,
        )


# =====================================================================
# Reviewer probe regressions (SYNTHETIC mutations of recorded AL-02 records)
# =====================================================================


class TestReviewProbeRegressions:
    """The ten S1-T5-F6-C1 reviewer probes. All failed against the R3 source."""

    @pytest.mark.parametrize(
        "condition,reason",
        [
            # Named person's affiliation is not the House winner.
            ("Jane Doe is Democratic in 2026", "unsupported_resolution_subject"),
            # No decisive district; the title's district must not be borrowed.
            (
                "the House member sworn in for the term beginning in 2027 is a member of the Democratic Party",
                "missing_resolution_district",
            ),
            # Different office despite a House-winner title.
            (
                "the mayor of AL-02 for the term beginning in 2027 is a member of the Democratic Party",
                "unsupported_resolution_subject",
            ),
            # Unconsumed restriction inside the decisive condition.
            (
                (
                    "the House member sworn in for AL-02 for the term beginning in 2027 is a member of the "
                    "Democratic Party unless turnout exceeds 50%"
                ),
                "unconsumed_resolution_condition",
            ),
            # Unconsumed losing condition inside the decisive condition.
            (
                (
                    "the House member sworn in for AL-02 for the term beginning in 2027 is a member of the "
                    "Democratic Party but loses the seat"
                ),
                "unconsumed_resolution_condition",
            ),
        ],
        ids=[
            "kalshi_named_person_affiliation",
            "kalshi_no_decisive_district",
            "kalshi_different_office",
            "kalshi_unconsumed_unless_restriction",
            "kalshi_unconsumed_losing_condition",
        ],
    )
    def test_kalshi_mutations_rejected(self, condition: str, reason: str) -> None:
        market = _k_market(rules_primary=_k_rules(condition))
        assert is_2026_kalshi_market(market, _k_event()) == (False, reason)

    @pytest.mark.parametrize(
        "description,reason",
        [
            (
                RECORDED_PM_DESCRIPTION_TEMPLATE.format(district="AL-02").replace(
                    "will resolve according", "will not resolve according"
                ),
                "contradictory_party_winner_rules",
            ),
            (
                RECORDED_PM_DESCRIPTION_TEMPLATE.format(district="AL-02").replace(
                    "will resolve according", "will resolve to No according"
                ),
                "contradictory_party_winner_rules",
            ),
            (
                (
                    "This market will resolve to Yes if Jane Doe wins the AL-02 congressional district seat in "
                    "the U.S. House of Representatives in the 2026 midterm elections, regardless of the party "
                    "of the candidate who wins the AL-02 seat."
                ),
                "not_party_winner_proposition",
            ),
            (
                _pm_opening() + ", excluding results from every county except Mobile County.",
                "sub_district_resolution_scope",
            ),
        ],
        ids=[
            "polymarket_negated_resolution",
            "polymarket_opposite_payout",
            "polymarket_candidate_with_distractor",
            "polymarket_county_coverage_negated",
        ],
    )
    def test_polymarket_mutations_rejected(self, description: str, reason: str) -> None:
        item = _pm_market("AL-02", "Democratic", description=description)
        event = {"id": "190872", "title": "AL-02 House Election Winner", "slug": "al-02-house-election-winner"}
        assert is_2026_polymarket_market(item, parent_event=event, is_tag_102289=True) == (False, reason)

    def test_kalshi_valid_west_virginia_full_name_admitted(self) -> None:
        """SYNTHETIC control: West Virginia must not also be read as Virginia."""
        market = _k_market(
            "WV02",
            "D",
            rules_primary=_k_rules(
                "the House member sworn in for West Virginia\u2019s 2nd congressional district for the term "
                "beginning in 2027 is a member of the Democratic Party"
            ),
        )
        assert is_2026_kalshi_market(market, _k_event("WV02")) == (True, None)


# =====================================================================
# Background acceptance (SYNTHETIC sentences appended to recorded contracts)
# =====================================================================

# Resolution-changing sentences that contain no word an exclusion list would anticipate.
UNLISTED_RESOLUTION_CHANGES = [
    "The preceding statement is inverted.",
    "The affirmative outcome occurs upon a Republican victory.",
]


class TestBackgroundAcceptance:
    """Only reviewed background shapes are accepted; any other sentence is rejected."""

    @pytest.mark.parametrize("sentence", UNLISTED_RESOLUTION_CHANGES)
    @pytest.mark.parametrize("field", ["rules_primary", "rules_secondary"])
    def test_kalshi_unlisted_resolution_change_rejected(self, field: str, sentence: str) -> None:
        market = _k_market("AL02", "D")
        market[field] = f"{market[field]} {sentence}"
        assert is_2026_kalshi_market(market, _k_event()) == (False, "unsupported_background_sentence")

    @pytest.mark.parametrize("sentence", UNLISTED_RESOLUTION_CHANGES)
    @pytest.mark.parametrize("position", ["after_opening_clause", "after_recorded_boilerplate"])
    def test_polymarket_unlisted_resolution_change_rejected(self, position: str, sentence: str) -> None:
        recorded = RECORDED_PM_DESCRIPTION_TEMPLATE.format(district="AL-02")
        if position == "after_opening_clause":
            description = recorded.replace("elections. The midterm", f"elections. {sentence} The midterm", 1)
        else:
            description = f"{recorded} {sentence}"
        item = _pm_market("AL-02", "Democratic", id="1280801", description=description)
        event = {"id": "190872", "title": "AL-02 House Election Winner", "slug": "al-02-house-election-winner"}
        assert is_2026_polymarket_market(item, parent_event=event, is_tag_102289=True) == (
            False,
            "unsupported_background_sentence",
        )


# =====================================================================
# Grammar: district references
# =====================================================================


class TestDistrictGrammar:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("AL-02", ("AL", "2")),
            ("AL-2", ("AL", "2")),
            ("CA-48", ("CA", "48")),
            ("DE-AL", ("DE", "AL")),
            ("West Virginia's 2nd congressional district", ("WV", "2")),
            ("West Virginia\u2019s 2nd congressional district", ("WV", "2")),
            ("Virginia's 5th congressional district", ("VA", "5")),
            ("California's 22nd congressional district", ("CA", "22")),
            ("New York's 13th congressional district", ("NY", "13")),
            ("Delaware's at-large congressional district", ("DE", "AL")),
        ],
    )
    def test_supported_forms(self, text: str, expected: tuple[str, str]) -> None:
        assert fvm._parse_house_district(text) == expected

    @pytest.mark.parametrize(
        "text",
        [
            "the second district of California",  # unsupported phrase (R3 README overstated it)
            "California's second congressional district",  # word ordinals are not implemented
            "California's 2th congressional district",  # wrong ordinal suffix
            "California's 2nd district",  # 'congressional' is required
            "California district 2",
            "AL-00",
            "AL-02 or CA-22",
            "XX-02",  # not a state
            "al-02",  # clause codes are upper case
            "AL-02 seat",
            "",
        ],
    )
    def test_unsupported_forms_fail_closed(self, text: str) -> None:
        assert fvm._parse_house_district(text) is None

    def test_scan_prefers_longest_state_name(self) -> None:
        assert fvm._scan_district_references("West Virginia's 2nd congressional district") == [("WV", "2")]

    def test_scan_finds_every_reference(self) -> None:
        assert fvm._scan_district_references(
            "the AL-02 congressional district seat and California's 2nd congressional district seat"
        ) == [("AL", "2"), ("CA", "2")]


# =====================================================================
# Grammar: Kalshi decisive sentence
# =====================================================================


class TestKalshiRulesGrammar:
    def test_synthetic_party_wins_race_shape_admitted(self) -> None:
        """SYNTHETIC control: complete affirmative 2026 House-party winner clause."""
        market = _k_market(rules_primary=_k_rules("the Democratic Party wins the AL-02 House race in the 2026 election"))
        assert is_2026_kalshi_market(market, _k_event()) == (True, None)
        evidence, reason, _ = fvm._parse_kalshi_house_rules(market["rules_primary"])
        assert reason is None
        assert (evidence.shape, evidence.subject, evidence.party, evidence.election_year) == (
            "kalshi_party_wins_race",
            "district_house_race_winner",
            "D",
            2026,
        )

    @pytest.mark.parametrize(
        "rules,reason",
        [
            ("", "insufficient_resolution_evidence"),
            ("The market resolves to Yes if the Democratic Party wins AL-02.", "insufficient_resolution_evidence"),
            (
                RECORDED_KALSHI_RULES_TEMPLATE.format(district="AL-02", party="Democratic").replace(
                    "beginning in 2027", "beginning in 2029"
                ),
                "non_2026_primary_contest",
            ),
            (
                _k_rules(
                    "the House member sworn in for the second district of Alabama for the term beginning in "
                    "2027 is a member of the Democratic Party"
                ),
                "unsupported_district_reference",
            ),
            (
                _k_rules(
                    "the House member sworn in for AL-02 or AL-03 for the term beginning in 2027 is a member "
                    "of the Democratic Party"
                ),
                "mismatched_district",
            ),
            (
                _k_rules(
                    "the House member sworn in for AL-02 for the term beginning in 2027 is a member of the "
                    "Green Party"
                ),
                "contradictory_party_winner_rules",
            ),
            (
                _k_rules("the House member sworn in for AL-02 for the term beginning in 2027 is Democratic"),
                "not_party_winner_proposition",
            ),
            (
                _k_rules(
                    "the House member sworn in for AL-02 for the term beginning in 2027 is never a member of "
                    "the Democratic Party"
                ),
                "contradictory_party_winner_rules",
            ),
            (
                _k_rules(
                    "the House member sworn in for AL-02 for the term beginning in 2027 is a member of the "
                    "Democratic Party and turnout exceeds 50%"
                ),
                "contradictory_party_winner_rules",
            ),
            (
                _k_rules("the Democratic Party wins the AL-02 House race in the 2026 election by 5 points"),
                "unconsumed_resolution_condition",
            ),
            (
                _k_rules("the Democratic Party wins the AL-02 House race in the 2026 special election"),
                "unsupported_resolution_clause",
            ),
        ],
        ids=[
            "empty",
            "unsupported_post_condition_frame",
            "wrong_term_year",
            "unsupported_district_phrase",
            "two_districts",
            "third_party",
            "affiliation_without_membership_phrase",
            "never_negation",
            "and_combination",
            "party_wins_with_margin",
            "party_wins_special_election",
        ],
    )
    def test_unsupported_or_contradictory_rules_rejected(self, rules: str, reason: str) -> None:
        market = _k_market(rules_primary=rules)
        assert is_2026_kalshi_market(market, _k_event()) == (False, reason)

    @pytest.mark.parametrize(
        "extra",
        [
            "This market resolves to No if turnout exceeds 50%.",
            "Only votes cast in Mobile County are counted.",
            "The seat was redrawn after the 2030 census.",
            "The incumbent previously represented AL-03.",
            "The incumbent previously served in Georgia.",
            # Variants of the reviewed party-mention shape are not that shape.
            "The Republican Party also nominated a candidate in this race, who is favoured.",
            "The Green Party also nominated a candidate in this race.",
        ],
    )
    def test_unknown_background_in_rules_primary_rejected(self, extra: str) -> None:
        """SYNTHETIC: every sentence after the decisive one must be a supported background shape."""
        market = _k_market()
        market["rules_primary"] = f"{market['rules_primary']} {extra}"
        assert is_2026_kalshi_market(market, _k_event()) == (False, "unsupported_background_sentence")

    def test_unknown_rules_secondary_rejected(self) -> None:
        market = _k_market(rules_secondary="The market resolves to No if the winner changes parties.")
        assert is_2026_kalshi_market(market, _k_event()) == (False, "unsupported_background_sentence")

    def test_recorded_rules_secondary_with_one_sentence_changed_rejected(self) -> None:
        """Recorded boilerplate is accepted sentence by sentence, never by resemblance."""
        market = _k_market(rules_secondary=RECORDED_KALSHI_RULES_SECONDARY.replace("project the winner", "project a winner"))
        assert is_2026_kalshi_market(market, _k_event()) == (False, "unsupported_background_sentence")

    def test_absent_rules_secondary_accepted(self) -> None:
        market = _k_market()
        del market["rules_secondary"]
        assert is_2026_kalshi_market(market, _k_event()) == (True, None)

    def test_title_is_corroboration_not_decisive(self) -> None:
        """Title and parent may corroborate the rules district but never replace it."""
        market = _k_market(rules_primary=_k_rules(
            "the House member sworn in for AL-03 for the term beginning in 2027 is a member of the "
            "Democratic Party"
        ))
        assert is_2026_kalshi_market(market, _k_event()) == (False, "mismatched_district")

    def test_parent_subtitle_district_conflict_rejected(self) -> None:
        assert is_2026_kalshi_market(_k_market(), _k_event(sub_title="AL-03")) == (False, "mismatched_district")

    def test_explicit_market_series_ticker_still_accepted(self) -> None:
        assert is_2026_kalshi_market(_k_market(series_ticker="KXHOUSERACE"), _k_event()) == (True, None)


# =====================================================================
# Grammar: Polymarket opening clause and identity
# =====================================================================


class TestPolymarketClauseGrammar:
    @pytest.mark.parametrize(
        "description,reason",
        [
            (
                RECORDED_PM_DESCRIPTION_TEMPLATE.format(district="AL-02").replace(
                    "will resolve according", "will resolve to Yes according"
                ),
                "contradictory_party_winner_rules",
            ),
            (_pm_opening().replace("2026 midterm", "2026 primary") + ".", "non_2026_primary_contest"),
            (_pm_opening().replace("2026 midterm", "2026 general") + ".", "unsupported_resolution_clause"),
            (_pm_opening().replace("2026 midterm", "2024 midterm") + ".", "non_2026_primary_contest"),
            (
                _pm_opening().replace("U.S. House of Representatives", "Texas House of Representatives") + ".",
                "unsupported_resolution_clause",
            ),
            (_pm_opening() + ", unless the race is called after December 1.", "unconsumed_resolution_condition"),
            (_pm_opening() + ", based solely on votes cast in Mobile County.", "sub_district_resolution_scope"),
            (_pm_opening() + " in 2028.", "non_2026_primary_contest"),
            (
                _pm_opening().replace("wins the AL-02", "wins the second district of Alabama") + ".",
                "unsupported_resolution_clause",
            ),
        ],
        ids=[
            "yes_payout_inserted",
            "primary_kind",
            "general_kind_not_supported",
            "wrong_year",
            "state_house_office",
            "unconsumed_condition",
            "county_only",
            "trailing_year",
            "unsupported_district_phrase",
        ],
    )
    def test_unsupported_or_contradictory_clauses_rejected(self, description: str, reason: str) -> None:
        item = _pm_market("AL-02", "Democratic", description=description)
        assert is_2026_polymarket_market(item, parent_event=_pm_event("AL-02")) == (False, reason)

    def test_named_state_ordinal_clause_admitted(self) -> None:
        """SYNTHETIC control: supported full-name district form inside the opening clause."""
        description = _pm_opening().replace("the AL-02 congressional district", "Alabama's 2nd congressional district")
        item = _pm_market("AL-02", "Democratic", description=description + ".")
        assert is_2026_polymarket_market(item, parent_event=_pm_event("AL-02")) == (True, "verified_2026")

    @pytest.mark.parametrize(
        "extra",
        [
            "This market will resolve to No if the winner changes parties before taking office.",
            "Coverage notes also apply to AL-03.",
            # The Kalshi party-mention shape is reviewed for Kalshi only.
            "The Republican Party also nominated a candidate in this race.",
        ],
    )
    def test_unknown_background_sentence_rejected(self, extra: str) -> None:
        description = f"{RECORDED_PM_DESCRIPTION_TEMPLATE.format(district='AL-02')} {extra}"
        item = _pm_market("AL-02", "Democratic", description=description)
        assert is_2026_polymarket_market(item, parent_event=_pm_event("AL-02")) == (
            False,
            "unsupported_background_sentence",
        )

    def test_shortened_party_determination_rule_rejected(self) -> None:
        """SYNTHETIC: R3's shortened party-determination sentence is a different rule, not neutral.

        It drops "or otherwise identifiable affiliation ... at the time all of the 2026 House
        elections are conclusively called" and the caucus rule for unaffiliated winners, so it
        can change which party a winner counts for. It is therefore not accepted as background.
        """
        description = (
            _pm_opening() + ". The midterm elections will take place on November 3, 2026.\n\n"
            "A candidate's party will be determined by their ballot-listed affiliation."
        )
        item = _pm_market("AL-02", "Democratic", description=description)
        assert is_2026_polymarket_market(item, parent_event=_pm_event("AL-02")) == (
            False,
            "unsupported_background_sentence",
        )

    def test_recorded_boilerplate_subset_accepted(self) -> None:
        """Each recorded boilerplate sentence is accepted on its own; none is required."""
        description = _pm_opening() + ". The midterm elections will take place on November 3, 2026."
        item = _pm_market("AL-02", "Democratic", description=description)
        assert is_2026_polymarket_market(item, parent_event=_pm_event("AL-02")) == (True, "verified_2026")

    def test_parent_description_is_not_borrowed(self) -> None:
        """The market's own clause is decisive; a parent event description cannot supply it."""
        item = _pm_market("AL-02", "Democratic", description="")
        event = _pm_event("AL-02", description=RECORDED_PM_DESCRIPTION_TEMPLATE.format(district="AL-02"))
        assert is_2026_polymarket_market(item, parent_event=event) == (False, "lacks_2026_contest_year")

    def test_slug_party_contradiction_rejected(self) -> None:
        item = _pm_market("AL-02", "Democratic", slug="will-the-republican-party-win-the-al-02-house-seat")
        assert is_2026_polymarket_market(item, parent_event=_pm_event("AL-02")) == (
            False,
            "contradictory_party_winner_rules",
        )

    def test_unsupported_slug_rejected(self) -> None:
        item = _pm_market("AL-02", "Democratic", slug="al-02-dem-market")
        assert is_2026_polymarket_market(item, parent_event=_pm_event("AL-02")) == (
            False,
            "unsupported_market_identity",
        )

    def test_parent_primary_event_rejected(self) -> None:
        event = _pm_event("ME-02", title="ME-02 Democratic Primary Winner", slug="me-02-democratic-primary-winner")
        assert is_2026_polymarket_market(_pm_market("ME-02", "Democratic"), parent_event=event) == (
            False,
            "non_2026_primary_contest",
        )

    def test_non_house_records_keep_baseline_reason(self) -> None:
        """The fallback does not relabel unrelated records that lack a cycle year."""
        item = {
            "id": "pm-senate",
            "question": "Will the Democratic Party win the Alabama Senate race?",
            "slug": "will-the-democratic-party-win-the-alabama-senate-race",
            "active": True,
            "closed": False,
            "description": "This market will resolve according to the party of the winning candidate.",
        }
        assert is_2026_polymarket_market(item, is_tag_102289=True) == (False, "lacks_2026_contest_year")


# =====================================================================
# Retained S1-T5-F6 / R1 / R2 / R3 cases
# =====================================================================
#
# Corrections relative to R3 (each keeps the original intent):
# - Kalshi fixtures titled "Will <Party> win the AL-02 House race?" now use the recorded title
#   "Will <Party> win the House race for AL-02?". The old word order is not a supported title
#   shape; with it, the negative cases would have been rejected by the title rather than by
#   the rules defect they target.
# - The R3 Kalshi background controls said "<Party> Party is mentioned only as background".
#   Background is now accepted only in supported shapes, and that meta-sentence is not one, so
#   the controls use the reviewed shape "The <Party> Party also nominated a candidate in this
#   race." The intent (a background party mention neither creates nor erases a contradiction)
#   is unchanged.
# - The R3 Polymarket 17-district control used a shortened party-determination sentence. It is
#   a different rule, not neutral background, so the control now uses the recorded description
#   and a separate test pins the shortened sentence's rejection.
# - Reason assertions that R3 left open (``in (...)``) or that named the R3 fallback reason
#   now name the informative reason produced by the parser.
# - Fixtures now come from the shared builders above; the malformed, unused R3
#   ``UNMATCHED_17_DISTRICTS`` list was removed.


class TestPolymarketHouseAdmission:
    """Polymarket primary resolution clause evidence."""

    @pytest.mark.parametrize("st,dist", DISTRICTS_17)
    @pytest.mark.parametrize("party_name,party_code", PARTIES)
    def test_unmatched_17_districts_admitted_with_resolution_clause(
        self, st: str, dist: str, party_name: str, party_code: str
    ) -> None:
        """RECORDED TEMPLATE description with R3's question-form parent slug.

        R3 used a shortened background sentence here ("A candidate's party will be determined by
        their ballot-listed affiliation."). That sentence is a different party-determination rule,
        so it is no longer accepted; this positive control now uses the recorded description, and
        ``test_shortened_party_determination_rule_rejected`` pins the rejection of R3's wording.
        """
        item = {
            "id": f"poly-{st}{dist}-{party_code}",
            "question": f"Will the {party_name} Party win the {st}-{dist} House seat?",
            "slug": f"will-the-{party_name.lower()}-party-win-the-{st.lower()}-{dist.lower()}-house-seat",
            "active": True,
            "closed": False,
            "description": RECORDED_PM_DESCRIPTION_TEMPLATE.format(district=f"{st}-{dist}"),
        }
        parent_event = {
            "id": f"ev-{st}{dist}",
            "title": f"{st}-{dist} House Election Winner",
            "slug": f"which-party-will-win-the-house-race-for-the-{st.lower()}-{dist.lower()}-seat",
        }

        eligible, reason = is_2026_polymarket_market(item, parent_event=parent_event)
        assert eligible is True, f"Failed for {st}-{dist} {party_name}: {reason}"
        assert reason == "verified_2026"

        market_record = {
            "venue": "polymarket",
            "question": item["question"],
            "slug": item["slug"],
            "event_title": parent_event["title"],
        }
        assert classify_race("polymarket", market_record) == "house"

    def test_synthetic_me02_consistent_control_admitted(self) -> None:
        """SYNTHETIC positive control: ME-02 with consistent question, slug, event and clause."""
        item = _pm_market("ME-02", "Democratic", id="poly-synthetic-me02-d")
        parent_event = {
            "id": "726881",
            "title": "ME-02 House Election Winner",
            "slug": "me-02-house-election-winner-20260720160226434",
        }
        assert is_2026_polymarket_market(item, parent_event=parent_event) == (True, "verified_2026")

    def test_conflicting_identity_year_is_not_overridden(self) -> None:
        """Explicit non-2026 year in question/slug is never overridden by a 2026 description."""
        item = {
            "id": "poly-conflicting-year",
            "question": "Will the Republican Party win the CA-22 House seat in 2024?",
            "slug": "will-the-republican-party-win-the-ca-22-house-seat-in-2024",
            "active": True,
            "closed": False,
            "description": _pm_opening("CA-22") + ".",
        }
        assert is_2026_polymarket_market(item) == (False, "non_2026_primary_contest")

    def test_incidental_2026_reference_in_description_rejected(self) -> None:
        """A 2026 mention incidental to background text cannot supply the cycle."""
        item = _pm_market(
            "CA-22",
            "Republican",
            description="This market resolves according to the 2024 election. In 2026, the census figures will be reviewed.",
        )
        assert is_2026_polymarket_market(item) == (False, "unsupported_resolution_clause")

    def test_mismatched_district_between_identity_and_clause_rejected(self) -> None:
        item = _pm_market("AL-02", "Democratic", description=_pm_opening("AL-03") + ".")
        assert is_2026_polymarket_market(item) == (False, "mismatched_district")

    def test_candidate_only_contract_rejected(self) -> None:
        item = {
            "id": "poly-cand-only",
            "question": "Will David Valadao win the CA-22 House seat?",
            "slug": "will-david-valadao-win-the-ca-22-house-seat",
            "active": True,
            "closed": False,
            "description": (
                "This market will resolve to Yes if David Valadao wins the CA-22 congressional "
                "district seat in the U.S. House of Representatives in the 2026 midterm elections."
            ),
        }
        assert is_2026_polymarket_market(item) == (False, "not_party_winner_proposition")

    def test_margin_spread_threshold_rejected(self) -> None:
        item = {
            "id": "poly-margin",
            "question": "Will the Republican Party candidate win the 2026 FL-16 House election by 9%-12%?",
            "slug": "fl-16-margin-9-12",
            "active": True,
            "closed": False,
            "description": "Resolves based on margin of victory.",
        }
        eligible, _reason = is_2026_polymarket_market(item)
        assert eligible is False

    def test_inactive_and_closed_markets_rejected(self) -> None:
        base = _pm_market("AL-02", "Democratic")
        assert is_2026_polymarket_market({**base, "active": True, "closed": True}) == (False, "market_closed")
        assert is_2026_polymarket_market({**base, "active": False, "closed": False}) == (False, "market_inactive")

    def test_synthetic_candidate_only_question_with_party_clause_rejected(self) -> None:
        """SYNTHETIC case A: candidate-only question paired with generic party-winner clause."""
        item = _pm_market(
            "AL-02",
            "Democratic",
            question="Will Democratic candidate Jane Doe win the AL-02 House seat?",
            slug="will-democratic-candidate-jane-doe-win-the-al-02-house-seat",
        )
        assert is_2026_polymarket_market(item, parent_event=_pm_event("AL-02")) == (
            False,
            "not_party_winner_proposition",
        )

    def test_synthetic_multiple_district_question_rejected(self) -> None:
        """SYNTHETIC case B: multiple-district question with rules covering only AL-02."""
        item = _pm_market(
            "AL-02",
            "Democratic",
            question="Will the Democratic Party win the AL-02 and CA-22 House seats?",
            slug="will-the-democratic-party-win-the-al-02-and-ca-22-house-seats",
        )
        assert is_2026_polymarket_market(item) == (False, "mismatched_district")

    def test_synthetic_incidental_year_in_resolution_clause_rejected(self) -> None:
        """SYNTHETIC case C: incidental year in the opening clause."""
        item = _pm_market(
            "AL-02",
            "Democratic",
            description=(
                "This market will resolve according to the party of the candidate who wins the AL-02 "
                "congressional district seat in the U.S. House of Representatives, under this policy updated in 2026."
            ),
        )
        assert is_2026_polymarket_market(item) == (False, "unsupported_resolution_clause")

    def test_synthetic_district_substring_mismatch_rejected(self) -> None:
        """SYNTHETIC case D: AL-02 question with a 12th district clause."""
        description = _pm_opening().replace(
            "the AL-02 congressional district", "Alabama\u2019s 12th congressional district"
        )
        item = _pm_market("AL-02", "Democratic", description=description + ".")
        assert is_2026_polymarket_market(item) == (False, "mismatched_district")

    def test_synthetic_poly_county_only_resolution_rejected(self) -> None:
        """SYNTHETIC case E: county-only resolution scope."""
        item = _pm_market(
            "AL-02", "Democratic", description=_pm_opening() + ", based solely on votes cast in Mobile County."
        )
        assert is_2026_polymarket_market(item, parent_event=_pm_event("AL-02")) == (
            False,
            "sub_district_resolution_scope",
        )

    def test_synthetic_poly_foreign_office_clause_rejected(self) -> None:
        """SYNTHETIC case F: Canadian Parliament clause."""
        item = _pm_market(
            "AL-02",
            "Democratic",
            description=(
                "This market will resolve according to the party of the candidate who wins the AL-02 "
                "House seat in the Canadian Parliament in the 2026 general election."
            ),
        )
        assert is_2026_polymarket_market(item, parent_event=_pm_event("AL-02")) == (False, "lacks_us_jurisdiction")

    def test_synthetic_poly_same_district_party_combination_rejected(self) -> None:
        """SYNTHETIC case G: same-district party combination question."""
        item = _pm_market(
            "AL-02",
            "Democratic",
            question="Will the Democratic Party win the AL-02 House seat or the Republican Party win the AL-02 House seat?",
            slug="will-the-democratic-party-win-the-al-02-house-seat-or-the-republican-party",
        )
        assert is_2026_polymarket_market(item, parent_event=_pm_event("AL-02")) == (
            False,
            "not_party_winner_proposition",
        )

    def test_synthetic_poly_district_combination_named_state_rejected(self) -> None:
        """SYNTHETIC case H: district combination in named-state wording."""
        item = _pm_market(
            "AL-02",
            "Democratic",
            description=_pm_opening().replace(
                "the AL-02 congressional district seat",
                "the AL-02 congressional district seat and California's 2nd congressional district seat",
            )
            + ".",
        )
        assert is_2026_polymarket_market(item, parent_event=_pm_event("AL-02")) == (False, "mismatched_district")

    @pytest.mark.parametrize("party", ["Democratic", "Republican"])
    def test_synthetic_poly_district_wide_county_aggregation_admitted(self, party: str) -> None:
        """SYNTHETIC case I: certified district-wide county aggregation is supported."""
        item = _pm_market(
            "AL-02", party, description=_pm_opening() + ", using certified totals from every county in the district."
        )
        assert is_2026_polymarket_market(item, parent_event=_pm_event("AL-02")) == (True, "verified_2026")

    def test_synthetic_poly_ambiguous_clause_shape_rejected(self) -> None:
        """SYNTHETIC: resolution sentence without the supported party-of-winner proposition."""
        item = _pm_market(
            "AL-02",
            "Democratic",
            description=(
                "This market will resolve based on announcements made regarding the AL-02 congressional "
                "district seat in the U.S. House in 2026."
            ),
        )
        assert is_2026_polymarket_market(item, parent_event=_pm_event("AL-02")) == (
            False,
            "unsupported_resolution_clause",
        )

    def test_description_without_resolution_sentence_keeps_baseline_reason(self) -> None:
        item = _pm_market("AL-02", "Democratic", description="Background on the AL-02 race in 2026.")
        assert is_2026_polymarket_market(item) == (False, "lacks_2026_contest_year")


class TestKalshiGenericHouseAdmission:
    """Kalshi generic KXHOUSERACE series corroboration."""

    @pytest.mark.parametrize("st,dist", DISTRICTS_17)
    @pytest.mark.parametrize("party_name,party_code", PARTIES)
    def test_unmatched_17_districts_admitted_with_kxhouserace(
        self, st: str, dist: str, party_name: str, party_code: str
    ) -> None:
        """SYNTHETIC: explicit market series_ticker, unpadded title district, '- In 2026' parent."""
        series_ticker = "KXHOUSERACE"
        event_ticker = f"KXHOUSERACE-{st}{dist}-26"
        market_ticker = f"KXHOUSERACE-{st}{dist}-26-{party_code}"
        dist_num = str(int(dist))

        m = {
            "ticker": market_ticker,
            "event_ticker": event_ticker,
            "series_ticker": series_ticker,
            "title": f"Will {party_name} win the House race for {st}-{dist_num}?",
            "subtitle": f"{party_name} Party",
            "status": "open",
            "rules_primary": (
                f"If the House member sworn in for {st}-{dist} for the term beginning in 2027 "
                f"is a member of the {party_name} party, then the market resolves to Yes."
            ),
        }
        ev = {
            "event_ticker": event_ticker,
            "series_ticker": series_ticker,
            "title": f"{st}-{dist} House winner? - In 2026",
            "sub_title": f"{st}-{dist}",
        }

        eligible, reason = is_2026_kalshi_market(m, ev)
        assert eligible is True, f"Failed for {st}-{dist} {party_name}: {reason}"
        assert reason is None

        market_record = {
            "venue": "kalshi",
            "market_ticker": market_ticker,
            "event_ticker": event_ticker,
            "series_ticker": series_ticker,
            "title": m["title"],
            "event_title": ev["title"],
        }
        assert classify_race("kalshi", market_record) == "house"

    def test_district_26_with_cycle_year_admitted(self) -> None:
        """FL-26 with explicit cycle segment -26. Complete clause replaces an R1 truncated fixture."""
        m = _k_market(
            "FL26",
            "D",
            series_ticker="KXHOUSERACE",
            status="open",
            rules_primary=(
                "If the House member sworn in for FL-26 for the term beginning in 2027 "
                "is a member of the Democratic party, then the market resolves to Yes."
            ),
        )
        ev = _k_event("FL26", title="FL-26 House winner? - In 2026")
        assert is_2026_kalshi_market(m, ev) == (True, None)

    def test_district_26_without_cycle_year_rejected(self) -> None:
        """District number 26 without a cycle segment is never read as 2026."""
        m = {
            "ticker": "KXHOUSERACE-FL26-D",
            "event_ticker": "KXHOUSERACE-FL26",
            "series_ticker": "KXHOUSERACE",
            "title": "Will Democratic win the House race for FL-26?",
            "status": "open",
        }
        ev = {"event_ticker": "KXHOUSERACE-FL26", "series_ticker": "KXHOUSERACE", "title": "FL-26 House race"}
        assert is_2026_kalshi_market(m, ev) == (False, "lacks_2026_contest_year")

    def test_conflicting_kalshi_cycle_year_rejected(self) -> None:
        m = {
            "ticker": "KXHOUSERACE-AL02-24-D",
            "event_ticker": "KXHOUSERACE-AL02-24",
            "series_ticker": "KXHOUSERACE",
            "title": "Will Democratic win the House race for AL-2?",
            "status": "open",
        }
        ev = {
            "event_ticker": "KXHOUSERACE-AL02-24",
            "series_ticker": "KXHOUSERACE",
            "title": "AL-02 House winner? - In 2024",
        }
        assert is_2026_kalshi_market(m, ev) == (False, "non_2026_primary_contest")

    def test_mismatched_district_between_ticker_and_title_rejected(self) -> None:
        m = _k_market(title="Will Democratic win the House race for CA-22?")
        ev = _k_event(title="CA-22 House winner? - In 2026")
        assert is_2026_kalshi_market(m, ev) == (False, "mismatched_district")

    def test_arbitrary_ticker_suffix_rejected(self) -> None:
        m = _k_market(ticker="KXHOUSERACE-AL02-26-TURNOUT", title="Turnout in AL-02")
        assert is_2026_kalshi_market(m, _k_event(title="AL-02 Turnout")) == (False, "arbitrary_ticker_suffix")

    def test_candidate_vote_share_series_preserved_in_baseline_ingestion(self) -> None:
        """KXVOTEGENERAL vote-share contracts stay in baseline reference-data ingestion.

        Reference-data ingestion, direct party mapping and fair-value eligibility are distinct
        gates; this bounded repair does not change the baseline path for these records.
        """
        m = {
            "ticker": "KXVOTEGENERAL-KXHOUSERACE-NY25-26VMCI-26",
            "event_ticker": "KXVOTEGENERAL-KXHOUSERACE-NY25-26VMCI",
            "series_ticker": "KXVOTEGENERAL",
            "title": "Will Virginia McIntyre receive at least 26% of the popular vote in the 2026 NY-25 House election?",
            "status": "open",
        }
        ev = {
            "event_ticker": "KXVOTEGENERAL-KXHOUSERACE-NY25-26VMCI",
            "series_ticker": "KXVOTEGENERAL",
            "title": "Virginia McIntyre vote share",
        }
        eligible, _reason = is_2026_kalshi_market(m, ev)
        assert eligible is True

    def test_kxhouserace_candidate_only_contract_rejected(self) -> None:
        m = _k_market(
            ticker="KXHOUSERACE-AL02-26-JDOE", title="Will John Doe win the House race for AL-2?", subtitle="John Doe"
        )
        assert is_2026_kalshi_market(m, _k_event()) == (False, "arbitrary_ticker_suffix")

    def test_parent_subtitle_in_2028_rejected(self) -> None:
        assert is_2026_kalshi_market(_k_market(), _k_event(sub_title="In 2028")) == (False, "non_2026_primary_contest")

    def test_synthetic_market_event_ticker_mismatch_rejected(self) -> None:
        """SYNTHETIC case E: market.event_ticker identifies another district."""
        m = _k_market(event_ticker="KXHOUSERACE-CA22-26")
        assert is_2026_kalshi_market(m, _k_event()) == (False, "mismatched_market_event_relationship")

    def test_synthetic_market_conflicting_series_ticker_rejected(self) -> None:
        """SYNTHETIC case F: explicit market.series_ticker names another series."""
        m = _k_market(series_ticker="KXOTHER")
        assert is_2026_kalshi_market(m, _k_event()) == (False, "mismatched_market_event_relationship")

    def test_synthetic_parent_event_conflicting_series_ticker_rejected(self) -> None:
        """SYNTHETIC case G: parent event.series_ticker names another series."""
        assert is_2026_kalshi_market(_k_market(), _k_event(series_ticker="KXOTHER")) == (
            False,
            "mismatched_market_event_relationship",
        )

    def test_synthetic_party_winner_rules_contradiction_rejected(self) -> None:
        """SYNTHETIC case H: D market whose rules name the Republican Party."""
        m = _k_market(rules_primary=RECORDED_KALSHI_RULES_TEMPLATE.format(district="AL-02", party="Republican"))
        assert is_2026_kalshi_market(m, _k_event()) == (False, "contradictory_party_winner_rules")

    def test_synthetic_rules_cycle_year_contradiction_rejected(self) -> None:
        """SYNTHETIC case I: rules reference a 2028 election."""
        m = _k_market(rules_primary=_k_rules(
            "the House member sworn in for AL-02 for the term beginning in 2027 following the 2028 election "
            "is a member of the Democratic party"
        ))
        assert is_2026_kalshi_market(m, _k_event()) == (False, "non_2026_primary_contest")

    def test_synthetic_candidate_named_title_with_party_rules_rejected(self) -> None:
        """SYNTHETIC case J: named-candidate title with generic party rules and D suffix."""
        m = _k_market(title="Will Democratic candidate Jane Doe win the House race for AL-2?", subtitle="Jane Doe")
        assert is_2026_kalshi_market(m, _k_event()) == (False, "not_party_winner_proposition")

    def test_synthetic_kalshi_election_year_vs_term_year_rejected(self) -> None:
        """SYNTHETIC: the party-wins shape naming a 2027 election."""
        m = _k_market(rules_primary=_k_rules("the Democratic Party wins the AL-02 House seat in the 2027 election"))
        assert is_2026_kalshi_market(m, _k_event()) == (False, "non_2026_primary_contest")

    @pytest.mark.parametrize("ticker_party,rules_party", [("D", "Republican"), ("R", "Democratic")])
    def test_synthetic_kalshi_resolving_party_vs_background_party_rejected(
        self, ticker_party: str, rules_party: str
    ) -> None:
        """SYNTHETIC: a background mention of the ticker's party does not erase a contradiction."""
        ticker_party_name = "Democratic" if ticker_party == "D" else "Republican"
        m = _k_market(
            "AL02",
            ticker_party,
            rules_primary=(
                RECORDED_KALSHI_RULES_TEMPLATE.format(district="AL-02", party=rules_party)
                + f" The {ticker_party_name} Party also nominated a candidate in this race."
            ),
        )
        assert is_2026_kalshi_market(m, _k_event()) == (False, "contradictory_party_winner_rules")

    @pytest.mark.parametrize(
        "party_code,party,other", [("D", "Democratic", "Republican"), ("R", "Republican", "Democratic")]
    )
    def test_synthetic_kalshi_background_party_mention_admitted(self, party_code: str, party: str, other: str) -> None:
        """SYNTHETIC control: a neutral background mention of the other party does not block admission."""
        m = _k_market(
            "AL02",
            party_code,
            rules_primary=(
                RECORDED_KALSHI_RULES_TEMPLATE.format(district="AL-02", party=party)
                + f" The {other} Party also nominated a candidate in this race."
            ),
        )
        assert is_2026_kalshi_market(m, _k_event()) == (True, None)

    @pytest.mark.parametrize(
        "title,rules",
        [
            (
                "Will Democratic win the House primary for AL-02?",
                _k_rules("the Democratic Party wins the AL-02 House race in the 2026 election"),
            ),
            (
                "Will Democratic win the House race for AL-02?",
                _k_rules("the Democratic Party wins the AL-02 House primary in the 2026 election"),
            ),
            (
                "Will Democratic win the House primary for AL-02?",
                _k_rules("the Democratic Party wins the AL-02 House primary in the 2026 election"),
            ),
        ],
        ids=["title_only", "rules_only", "title_and_rules"],
    )
    def test_synthetic_kalshi_primary_contest_rejected(self, title: str, rules: str) -> None:
        m = _k_market(title=title, rules_primary=rules)
        assert is_2026_kalshi_market(m, _k_event()) == (False, "non_2026_primary_contest")

    def test_synthetic_kalshi_missing_explicit_relationships_rejected(self) -> None:
        """SYNTHETIC case D: relationships must be explicit; market.series_ticker may be absent."""
        m_missing = _k_market()
        del m_missing["event_ticker"]
        del m_missing["series_ticker"]
        ev_missing = _k_event()
        del ev_missing["series_ticker"]
        assert is_2026_kalshi_market(m_missing, ev_missing) == (False, "mismatched_market_event_relationship")

        m_ctrl = _k_market()
        del m_ctrl["series_ticker"]
        assert is_2026_kalshi_market(m_ctrl, _k_event()) == (True, None)

    def test_synthetic_kalshi_wrong_payout_direction_rejected(self) -> None:
        rules = RECORDED_KALSHI_RULES_TEMPLATE.format(district="AL-02", party="Democratic")
        m = _k_market(rules_primary=rules.replace("resolves to Yes", "resolves to No"))
        assert is_2026_kalshi_market(m, _k_event()) == (False, "contradictory_party_winner_rules")

    @pytest.mark.parametrize("party_code,party", [("D", "Democratic"), ("R", "Republican")])
    def test_synthetic_kalshi_negated_party_condition_rejected(self, party_code: str, party: str) -> None:
        m = _k_market(
            "AL02",
            party_code,
            rules_primary=_k_rules(
                f"the House member sworn in for AL-02 for the term beginning in 2027 is not a member of the {party} Party"
            ),
        )
        assert is_2026_kalshi_market(m, _k_event()) == (False, "contradictory_party_winner_rules")

    def test_synthetic_kalshi_named_candidate_condition_rejected(self) -> None:
        m = _k_market(
            rules_primary=_k_rules("the House member sworn in for AL-02 for the term beginning in 2027 is Jane Doe")
        )
        assert is_2026_kalshi_market(m, _k_event()) == (False, "not_party_winner_proposition")

    def test_synthetic_kalshi_party_combination_condition_rejected(self) -> None:
        m = _k_market(rules_primary=_k_rules(
            "the House member sworn in for AL-02 for the term beginning in 2027 is a member of the Democratic "
            "Party or the Republican Party"
        ))
        assert is_2026_kalshi_market(m, _k_event()) == (False, "contradictory_party_winner_rules")

    def test_synthetic_kalshi_conflicting_election_and_term_rejected(self) -> None:
        """SYNTHETIC: a valid 2027 term does not excuse an unrelated 2027 election."""
        m = _k_market(rules_primary=_k_rules(
            "the House member sworn in for AL-02 for the term beginning in 2027 is a member of the Democratic "
            "Party after the election held in 2027"
        ))
        assert is_2026_kalshi_market(m, _k_event()) == (False, "non_2026_primary_contest")

    def test_synthetic_kalshi_named_state_district_mismatch_rejected(self) -> None:
        m = _k_market(rules_primary=_k_rules(
            "the House member sworn in for California's 2nd congressional district for the term beginning in "
            "2027 is a member of the Democratic Party"
        ))
        assert is_2026_kalshi_market(m, _k_event()) == (False, "mismatched_district")

    def test_synthetic_kalshi_ambiguous_rules_shape_rejected(self) -> None:
        m = _k_market(
            rules_primary="This contract covers announcements made regarding the AL-02 congressional district in 2026."
        )
        assert is_2026_kalshi_market(m, _k_event()) == (False, "insufficient_resolution_evidence")

    def test_missing_rules_primary_rejected(self) -> None:
        m = _k_market()
        del m["rules_primary"]
        assert is_2026_kalshi_market(m, _k_event()) == (False, "insufficient_resolution_evidence")
