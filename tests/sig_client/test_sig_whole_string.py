"""F1 area 4: whole-string validation (no trailing-newline loophole)."""

import copy
from typing import Any

import pytest
from sig_testkit import (
    API,
    SENTINEL_KEY,
    TOURNAMENT,
    Router,
    fixture,
    json_response,
    make_client,
    run,
)

from engine.config import (
    API_KEY_ENV_VAR,
    ApiKey,
    ConfigError,
    SigConfig,
    TournamentRef,
    load_sig_config,
)
from engine.sig_client.errors import SigArgumentError, SigResponseError

TID = TOURNAMENT.id


@pytest.mark.parametrize("suffix", ["\n", "\r\n", "\r", "\x00", "\t", " ", "\x7f"])
def test_api_key_with_trailing_control_or_space_is_rejected_silently(suffix: str) -> None:
    for value in (SENTINEL_KEY + suffix, suffix + SENTINEL_KEY):
        with pytest.raises(ConfigError) as info:
            ApiKey(value)
        assert SENTINEL_KEY not in str(info.value)
        with pytest.raises(ConfigError) as info:
            load_sig_config({API_KEY_ENV_VAR: value}, tournament=TOURNAMENT)
        assert SENTINEL_KEY not in str(info.value)


@pytest.mark.parametrize(
    ("slug", "tid"),
    [
        ("alpha-tournament\n", TID),
        ("alpha-tournament\r", TID),
        ("alpha-tournament", TID + "\n"),
        ("alpha-tournament", "{" + TID + "}"),
        ("alpha-tournament", "urn:uuid:" + TID),
        ("alpha-tournament", TID.replace("-", "")),
    ],
)
def test_tournament_ref_requires_whole_string_matches(slug: str, tid: str) -> None:
    with pytest.raises(ConfigError) as info:
        TournamentRef(slug=slug, id=tid)
    assert tid not in str(info.value) and slug not in str(info.value)


@pytest.mark.parametrize("url", ["https://sig.invalid/api/v1\n", "https://sig.invalid/api v1"])
def test_base_url_requires_a_whole_string_match(url: str) -> None:
    with pytest.raises(ConfigError):
        SigConfig(api_key=ApiKey(SENTINEL_KEY), tournament=TOURNAMENT, base_url=url)


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.get_exchange_orderbook("36\n"),
        lambda c: c.get_market_orderbook("26\n"),
        lambda c: c.get_exchange_prices(["36", "37\n"]),
        lambda c: c.list_orders_page(exchange_id="36\n"),
        lambda c: c.list_orders_page(market_id="26\n"),
        lambda c: c.list_orders_page(exchange_id="x"),
        lambda c: c.list_all_orders(exchange_id="36\n"),
        lambda c: c.list_all_orders(market_id="\n"),
    ],
)
def test_ids_with_trailing_newlines_send_nothing(call: Any) -> None:
    client, router, _ = make_client()

    async def scenario() -> None:
        await call(client)

    with pytest.raises(SigArgumentError):
        run(scenario())
    assert router.requests == []  # not even the tournament verification


def test_valid_order_filters_still_work() -> None:
    client, router, _ = make_client()
    run(client.list_orders_page(exchange_id="36", market_id="26"))
    request = router.requests[-1]
    assert request.url.params["exchangeId"] == "36" and request.url.params["marketId"] == "26"


@pytest.mark.parametrize(
    "reported",
    [TID + "\n", "{" + TID + "}", "urn:uuid:" + TID, TID.replace("-", ""), " " + TID],
)
def test_echoed_tournament_uuid_must_be_canonical(reported: str) -> None:
    body = fixture("tournament") | {"id": reported}
    client, _, _ = make_client(
        Router({f"{API}/tournaments/alpha-tournament": json_response(200, body)})
    )
    with pytest.raises(SigResponseError, match="canonical UUID"):
        run(client.get_tournament())


@pytest.mark.parametrize("reported", [TID + "\n", "{" + TID + "}", TID.replace("-", "")])
def test_echoed_context_uuid_must_be_canonical(reported: str) -> None:
    body = copy.deepcopy(fixture("market_orderbook"))
    body["contexts"][0]["tournament"]["id"] = reported
    client, _, _ = make_client(Router({f"{API}/markets/26/orderbook": json_response(200, body)}))
    with pytest.raises(SigResponseError, match="canonical UUID"):
        run(client.get_market_orderbook("26"))
