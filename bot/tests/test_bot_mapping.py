from pathlib import Path

import pytest
import yaml

from bot.mapping import MAP_PATH, MappingError, load_map, parse_map


def _con(party="D", ex="10", ticker="K1", token=None, approved=True):
    return {"party": party, "sig_market_id": "1", "sig_exchange_id": ex, "sig_title": "t",
            "kalshi_ticker": ticker, "polymarket_token_id": token, "approved": approved}


def _race(key="senate:PA", cons=None, exclude=False):
    return {"race_key": key, "exclude": exclude, "note": "", "contracts": cons or [_con()]}


def test_only_approved_in_non_excluded_races():
    out = parse_map({"races": [
        _race(cons=[_con("D"), _con("R", ex="11", ticker="K2", approved=False)]),
        _race("senate:OH", exclude=True, cons=[_con("D", ex="12", ticker="K3")])]})
    assert [(c.race_key, c.party) for c in out] == [("senate:PA", "D")]


@pytest.mark.parametrize("bad, text", [
    (lambda r: r["contracts"][0].update(sig_exchange_id="abc"), "senate:PA D"),
    (lambda r: r["contracts"][0].update(sig_market_id=5), "numeric"),
    (lambda r: r["contracts"][0].update(polymarket_token_id="x1"), "token"),
    (lambda r: r["contracts"][0].update(approved="yes"), "approved"),
    (lambda r: r["contracts"].append(_con("D", ex="11", ticker="K2")), "more than one D"),
    (lambda r: r["contracts"][0].update(party="X"), "party"),
])
def test_validation_failures(bad, text):
    race = _race()
    bad(race)
    with pytest.raises(MappingError, match=text):
        parse_map({"races": [race]})


@pytest.mark.parametrize("field, a, b", [
    ("sig_exchange_id", "10", "10"), ("kalshi_ticker", "K1", "K1"),
    ("polymarket_token_id", "77", "77")])
def test_duplicates_refused(field, a, b):
    c1 = _con("D", ex="10", ticker="K1")
    c2 = _con("R", ex="11", ticker="K2")
    c1[field], c2[field] = a, b
    with pytest.raises(MappingError, match="already used"):
        parse_map({"races": [_race(cons=[c1, c2])]})


def test_duplicate_race_key_and_bad_top_level():
    with pytest.raises(MappingError, match="twice"):
        parse_map({"races": [_race(), _race()]})
    with pytest.raises(MappingError):
        parse_map({"nope": []})


def test_load_map_reads_yaml(tmp_path: Path):
    p = tmp_path / "m.yaml"
    p.write_text(yaml.safe_dump({"races": [_race()]}), encoding="utf-8")
    assert len(load_map(p)) == 1


def test_committed_map_is_valid_and_valid_if_fully_approved():
    approved = load_map(MAP_PATH)  # raises on any validation failure
    assert all(not c.race_key.startswith("house:") for c in approved)
    data = yaml.safe_load(MAP_PATH.read_text(encoding="utf-8"))
    for race in data["races"]:
        race["exclude"] = False
        for c in race["contracts"]:
            c["approved"] = True
    assert len(parse_map(data)) == sum(len(r["contracts"]) for r in data["races"])
