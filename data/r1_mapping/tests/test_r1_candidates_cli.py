"""Candidate discovery and command-line behaviour (synthetic inputs, no network)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from data.r1_mapping.candidates import (
    analyse_kalshi,
    analyse_polymarket,
    dump_candidates,
    first_sentence,
    generate_candidates,
)
from data.r1_mapping.cli import main
from data.r1_mapping.identity import parse_sig_title, proposed_group_ids, race_key
from data.r1_mapping.tests.synthetic import SNAPSHOT_ID, World, make_world, pm


@pytest.fixture
def world(tmp_path: Path) -> World:
    return make_world(tmp_path)


def candidates_for(world: World) -> dict[str, dict]:
    inv = world.inventory()
    meta = world.sources["venue_snapshots"][0]
    report = generate_candidates(inv.contracts, world.snapshot, meta, inv.source_id)
    return {c["sig_market_id"]: c for c in report["contracts"]}


def test_title_grammar_and_race_keys() -> None:
    assert parse_sig_title("Will the Democratic Party win the AL-02 House race?") == {
        "shape": "house_district", "office": "house", "state": "AL", "district": "02",
        "party": "D"}
    assert parse_sig_title("Will the Republican Party win the West Virginia Senate?")[
        "state"] == "WV"
    assert parse_sig_title("Will the Independent Party win the U.S. Senate?")["office"] == \
        "senate_control"
    assert parse_sig_title("Will Jon Ossoff win the Georgia Senate?") is None
    assert parse_sig_title("Will the Democratic Party win the Atlantis Senate?") is None
    assert race_key("senate", "FL", None, "special") == "senate:FL:special"
    assert race_key("senate_control", None, None, "special") == "senate_control"
    assert proposed_group_ids({"office": "house_control", "state": None, "party": "R"}) == [
        "chamber:house", "party-outcome:R"]


def test_party_winner_suggestions_and_signals(world: World) -> None:
    cands = candidates_for(world)
    ga = cands["258"]
    assert [s["market_id"] for s in ga["polymarket"]["suggested"]] == ["630692"]
    assert ga["polymarket"]["suggested"][0]["token_id"] == "6306921"
    assert ga["polymarket"]["suggested"][0]["outcome_label"] == "Yes"
    assert "venue_rules_include_runoffs" in ga["polymarket"]["suggested"][0]["signals"]
    assert [s["market_ticker"] for s in ga["kalshi"]["suggested"]] == ["SENATEGA-26-D"]
    assert "venue_condition_is_swearing_in" in ga["kalshi"]["suggested"][0]["signals"]
    near = {n["id"]: n["reasons"] for n in ga["polymarket"]["near_misses"]}
    assert near["630693"] == ["other_party_outcome"]
    assert "margin_vote_share_or_seat_count" in near["3343192"]


def test_special_election_wording_is_flagged_not_hidden(world: World) -> None:
    fl = candidates_for(world)["354"]
    assert "venue_text_mentions_special_election" in fl["polymarket"]["suggested"][0]["signals"]
    assert fl["kalshi"]["suggested"] == []


def test_close_but_wrong_district_is_a_near_miss(world: World) -> None:
    al = candidates_for(world)["318"]
    assert al["polymarket"]["suggested"] == []
    assert al["polymarket"]["near_misses"][0]["id"] == "3390043"
    assert al["polymarket"]["near_misses"][0]["reasons"] == ["different_district"]


def test_named_candidate_is_never_suggested_for_a_party(world: World) -> None:
    mt = candidates_for(world)["276"]
    assert mt["kalshi"]["suggested"] == []
    reasons = {n["id"]: n["reasons"] for n in mt["kalshi"]["near_misses"]}
    assert "named_candidate_or_non_party_subject" in reasons["SENATEMT-26-IND"]


def test_inconsistent_ticker_metadata_is_flagged() -> None:
    feats = analyse_kalshi({
        "market_ticker": "SENATELA-26-D", "event_ticker": "SENATELA-26",
        "series_ticker": "SENATELA", "title": "Will Democratics win the Senate race in Kentucky?",
        "rules_text": "If a representative of the Democratic party is sworn in as a Senator of "
                      "Kentucky for the term beginning in 2027, then the market resolves to Yes.",
    })
    assert feats["state"] == "KY"
    assert "ticker_state_conflicts_with_rules" in feats["issues"]


def test_dfl_label_only_counts_in_minnesota() -> None:
    def senate(st: str, state: str) -> dict:
        return {"market_ticker": f"SENATE{st}-26-D", "event_ticker": f"SENATE{st}-26",
                "series_ticker": f"SENATE{st}", "title": f"Will Democrats win in {state}?",
                "rules_text": "If a representative of the Democratic (DFL) party is sworn in as "
                              f"a Senator of {state} for the term beginning in 2027, then the "
                              "market resolves to Yes."}
    assert analyse_kalshi(senate("MN", "Minnesota"))["issues"] == []
    assert "dfl_label_outside_minnesota" in analyse_kalshi(senate("IA", "Iowa"))["issues"]


def test_chamber_control_sentence_is_not_split_at_us(world: World) -> None:
    assert first_sentence("If X has won control of the U.S. Senate in 2026, then Y. More.") == \
        "If X has won control of the U.S. Senate in 2026, then Y."
    control = candidates_for(world)["153"]
    assert [s["market_ticker"] for s in control["kalshi"]["suggested"]] == ["CONTROLS-2026-D"]
    assert [s["market_id"] for s in control["polymarket"]["suggested"]] == ["562793"]


def test_county_and_non_binary_records_are_not_suggested() -> None:
    county = analyse_polymarket(pm("1", "Will the Democrats win Cobb County in the Georgia "
                                   "Senate race in 2026?", "County winner."))
    assert "sub_jurisdiction_scope" in county["issues"]
    odd = pm("2", "Will the Democrats win the Georgia Senate race in 2026?", "x")
    odd["outcome_token_map"] = {"Democrat": "a", "Republican": "b"}
    assert "not_binary_yes_no" in analyse_polymarket(odd)["issues"]


def test_candidate_output_is_deterministic_and_valid_json(world: World) -> None:
    inv = world.inventory()
    meta = world.sources["venue_snapshots"][0]
    first = dump_candidates(generate_candidates(inv.contracts, world.snapshot, meta, "x"))
    reversed_snapshot = {k: list(reversed(v)) if isinstance(v, list) else v
                         for k, v in world.snapshot.items()}
    second = dump_candidates(generate_candidates(inv.contracts, reversed_snapshot, meta, "x"))
    assert first == second
    assert json.loads(first)["generated_from"]["venue_snapshot"] == SNAPSHOT_ID


# --- command line ------------------------------------------------------------------------

def test_cli_init_never_overwrites(world: World, capsys: pytest.CaptureFixture) -> None:
    world.write_repo_files()
    assert main(["init", "--snapshot-id", SNAPSHOT_ID], root=world.root) == 2
    assert "refusing to overwrite" in capsys.readouterr().err


def test_cli_init_validate_report_round_trip(world: World,
                                            capsys: pytest.CaptureFixture) -> None:
    world.write_repo_files()
    for name in ("r1_mapping.json", "sig_resolution_evidence.json"):
        (world.root / "markets" / name).unlink()
    assert main(["init", "--snapshot-id", SNAPSHOT_ID, "--date", "2026-10-28"],
                root=world.root) == 0
    assert main(["validate"], root=world.root) == 0
    assert main(["report"], root=world.root) == 0
    assert main(["report", "--check"], root=world.root) == 0
    report = (world.root / "markets" / "R1_REPORT.md").read_text(encoding="utf-8")
    assert "Not G-1 completion" in report
    assert "| 258 | 947 | senate:GA |" in report


def test_cli_validate_points_at_the_bad_record(world: World,
                                              capsys: pytest.CaptureFixture) -> None:
    world.record("258")["venues"]["kalshi"]["fair_value_eligible"] = True
    world.write_repo_files()
    assert main(["validate"], root=world.root) == 1
    out = capsys.readouterr().out
    assert "ERROR [mapping:records[1] sig=258 venue=kalshi] fair_value_enabled_without_evidence" \
        in out


def test_cli_candidates_refuses_an_undeclared_snapshot(world: World, tmp_path: Path,
                                                      capsys: pytest.CaptureFixture) -> None:
    world.write_repo_files()
    other = tmp_path / "other.json"
    other.write_text(json.dumps(world.snapshot) + " ", encoding="utf-8")
    assert main(["candidates", "--venue-snapshot", str(other)], root=world.root) == 2
    assert "not a declared venue snapshot" in capsys.readouterr().err
    declared = world.root / world.sources["venue_snapshots"][0]["file"]
    assert main(["candidates", "--venue-snapshot", str(declared)], root=world.root) == 0
    assert (world.root / "markets" / "r1_candidates.json").is_file()


def test_cli_report_check_detects_stale_report(world: World) -> None:
    world.write_repo_files()
    assert main(["report"], root=world.root) == 0
    world.record("258")["venues"]["kalshi"]["review"]["status"] = "rejected"
    world.write_repo_files()
    assert main(["report", "--check"], root=world.root) == 1
