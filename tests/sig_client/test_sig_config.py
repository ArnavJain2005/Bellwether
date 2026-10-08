import pytest
from sig_testkit import SENTINEL_KEY, TOURNAMENT

from engine.config import (
    API_KEY_ENV_VAR,
    DEFAULT_TIMEOUT,
    MIDTERM_ELECTIONS,
    SIG_BASE_URL,
    ApiKey,
    ConfigError,
    SigConfig,
    TournamentRef,
    load_sig_config,
)


def test_load_reads_only_the_mapping_it_is_given() -> None:
    config = load_sig_config({API_KEY_ENV_VAR: SENTINEL_KEY}, tournament=TOURNAMENT)
    assert config.api_key.reveal() == SENTINEL_KEY
    assert config.tournament == TOURNAMENT
    assert config.base_url == SIG_BASE_URL
    assert config.timeout == DEFAULT_TIMEOUT


def test_tournament_is_required() -> None:
    with pytest.raises(TypeError):
        load_sig_config({API_KEY_ENV_VAR: SENTINEL_KEY})  # type: ignore[call-arg]


@pytest.mark.parametrize("environ", [{}, {API_KEY_ENV_VAR: ""}])
def test_missing_key_is_reported_by_name(environ: dict[str, str]) -> None:
    with pytest.raises(ConfigError, match=API_KEY_ENV_VAR):
        load_sig_config(environ, tournament=TOURNAMENT)


@pytest.mark.parametrize("bad", ["has space", "tab\tkey", "new\nline", "café"])
def test_key_with_unsafe_characters_is_rejected_without_echoing_it(bad: str) -> None:
    with pytest.raises(ConfigError) as info:
        load_sig_config({API_KEY_ENV_VAR: bad}, tournament=TOURNAMENT)
    assert bad not in str(info.value)


def test_api_key_never_shows_in_repr_or_str() -> None:
    key = ApiKey(SENTINEL_KEY)
    config = SigConfig(api_key=key, tournament=TOURNAMENT)
    for text in (repr(key), str(key), f"{key}", repr(config), str(config)):
        assert SENTINEL_KEY not in text


@pytest.mark.parametrize(
    "url",
    [
        "http://www.thesuper.market/api/v1",
        "https://user:pass@www.thesuper.market/api/v1",
        "https://www.thesuper.market/api/v1?x=1",
        "https://www.thesuper.market/api/v1#frag",
        "/api/v1",
    ],
)
def test_base_url_must_be_plain_https(url: str) -> None:
    with pytest.raises(ConfigError):
        SigConfig(api_key=ApiKey(SENTINEL_KEY), tournament=TOURNAMENT, base_url=url)


def test_base_url_trailing_slash_is_removed() -> None:
    config = SigConfig(
        api_key=ApiKey(SENTINEL_KEY), tournament=TOURNAMENT, base_url=SIG_BASE_URL + "/"
    )
    assert config.base_url == SIG_BASE_URL


@pytest.mark.parametrize(
    ("slug", "tid"),
    [
        ("", TOURNAMENT.id),
        ("a/b", TOURNAMENT.id),
        ("alpha", "not-a-uuid"),
        ("alpha", TOURNAMENT.id.upper()),
    ],
)
def test_tournament_ref_validation(slug: str, tid: str) -> None:
    with pytest.raises(ConfigError):
        TournamentRef(slug=slug, id=tid)


def test_midterm_constant_matches_claude_md() -> None:
    assert MIDTERM_ELECTIONS.slug == "midterm-elections"
    assert MIDTERM_ELECTIONS.id == "bda92870-621e-47b0-bc3c-3602c5c26f55"
