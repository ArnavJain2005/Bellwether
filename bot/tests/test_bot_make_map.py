import pytest

from bot import make_map
from bot.make_map import build_map, party_from_title, render_review


def _row(t):
    return {"market_ticker": t, "title": f"K {t}", "rules_lead": "kr", "signals": []}


def _poly(token, label="Yes"):
    return {"token_id": token, "outcome_label": label, "question": "Q", "rules_lead": "pr"}


def _c(mid, key, party, kalshi, poly=(), near=()):
    word = {"D": "Democratic", "R": "Republican", "I": "Independent"}[party]
    return {"sig_market_id": mid, "sig_exchange_id": str(int(mid) + 1000),
            "sig_title": f"Will the {word} Party win the X?", "proposed_race_key": key,
            "kalshi": {"suggested": [_row(t) for t in kalshi],
                       "near_misses": [{"id": n, "reasons": ["ticker_state_conflicts_with_rules"]}
                                       for n in near]},
            "polymarket": {"suggested": list(poly), "near_misses": []}}


def _build(contracts):
    return {r["race_key"]: r for r in build_map({"contracts": contracts})}


def test_party_from_title():
    assert party_from_title("Will the Independent Party win the Utah Senate?") == "I"
    assert party_from_title("Will the Republican Party win the U.S. House?") == "R"


def test_single_kalshi_suggestion_rule_and_defaults():
    races = _build([
        _c("1", "senate:PA", "D", ["KD"], [_poly("11")]),
        _c("2", "senate:PA", "R", ["KR1", "KR2"]),
        _c("3", "senate:OH", "D", []),
    ])
    assert list(races) == ["senate:PA"]
    pa = races["senate:PA"]
    assert [c["kalshi_ticker"] for c in pa["contracts"]] == ["KD"]
    assert pa["exclude"] is False and "1 of 2" in pa["note"]
    assert all(c["approved"] is False for c in pa["contracts"])


def test_polymarket_token_only_for_single_yes():
    races = _build([
        _c("1", "senate:PA", "D", ["KD"], [_poly("11")]),
        _c("2", "senate:PA", "R", ["KR"], [_poly("21"), _poly("22")]),
        _c("3", "governor:PA", "D", ["GD"], [_poly("31", "No")]),
    ])
    assert [c["polymarket_token_id"] for c in races["senate:PA"]["contracts"]] == ["11", None]
    assert races["governor:PA"]["contracts"][0]["polymarket_token_id"] is None


def test_exclusions():
    races = _build([
        _c("1", "senate:GA", "D", ["A"]), _c("2", "senate:GA", "R", ["B"]),
        _c("3", "house:ME-02", "D", ["C"]), _c("4", "house:ME-02", "R", ["D"]),
        _c("5", "senate:MT", "D", ["E"]), _c("6", "senate:MT", "R", ["F"]),
        _c("7", "senate:MT", "I", ["G"]),
        _c("8", "senate:KY", "D", ["H"], near=["BAD"]), _c("9", "senate:KY", "R", ["I"]),
        _c("10", "senate:TX", "D", ["J"]), _c("11", "senate:TX", "R", ["K"]),
    ])
    assert sorted(k for k, r in races.items() if r["exclude"]) == [
        "house:ME-02", "senate:GA", "senate:KY", "senate:MT"]
    assert "BAD" in races["senate:KY"]["note"]
    assert races["senate:TX"]["exclude"] is False


def test_review_order_and_content():
    races = build_map({"contracts": [
        _c("1", "house:AZ-01", "D", ["H"]), _c("2", "house_control", "D", ["HC"]),
        _c("3", "governor:AZ", "D", ["G"]), _c("4", "senate:AZ", "D", ["S"])]})
    text = render_review(races)
    order = [text.index(f"## {k}") for k in ("senate:AZ", "governor:AZ", "house_control",
                                             "house:AZ-01")]
    assert order == sorted(order)
    assert "approve? set approved: true in bot/map.yaml" in text


def test_regeneration_preserves_existing_manual_approvals(tmp_path, monkeypatch):
    map_path = tmp_path / "map.yaml"
    review_path = tmp_path / "map_review.md"
    original = "races:\\n- race_key: senate:PA\\n  contracts:\\n  - approved: true\\n"
    map_path.write_text(original.replace("\\n", "\n"), encoding="utf-8")
    review_path.write_text("reviewed by Arnav", encoding="utf-8")
    monkeypatch.setattr(make_map, "MAP_PATH", map_path)
    monkeypatch.setattr(make_map, "REVIEW_PATH", review_path)
    monkeypatch.setattr(make_map, "CANDIDATES_PATH", tmp_path / "absent.json")

    with pytest.raises(ValueError, match="manual approvals"):
        make_map.main()

    assert map_path.read_text(encoding="utf-8") == original.replace("\\n", "\n")
    assert review_path.read_text(encoding="utf-8") == "reviewed by Arnav"
