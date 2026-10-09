"""S1-T4-A-F2 regressions: a credential hidden behind duplicate JSON member names.

json.loads keeps only the last value of a repeated name. The credential check must
still see every member, including values and nested objects that a later duplicate
overwrites. Every body here is synthetic; the "credential" is a dummy sentinel.
"""

from __future__ import annotations

import io
import json
import logging
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from recorder_kit import (
    BUILD,
    EXCHANGE_BOOK,
    TID,
    TOURNAMENT_PATH,
    all_bytes,
    capture,
    read_manifest,
    sig_router,
    step,
)
from sig_testkit import BASE_URL, SENTINEL_KEY, TOURNAMENT, FakeClock, fixture, raw_response

from engine.config import ApiKey
from engine.sig_client import record_fixtures as rf

REQUIRED = [
    "--tournament-slug", TOURNAMENT.slug,
    "--tournament-id", TID,
    "--exchange-id", "36",
    "--market-id", "26",
]  # fmt: skip


def escaped(text: str) -> str:
    """`text` with its first character written as a JSON \\u escape."""
    return f"\\u{ord(text[0]):04x}{text[1:]}"


ESCAPED_KEY = escaped(SENTINEL_KEY)


def leak_forms() -> list[bytes]:
    forms = [SENTINEL_KEY.encode(), ESCAPED_KEY.encode()]
    forms += [SENTINEL_KEY.encode(c) for c in ("utf-16-le", "utf-16-be", "utf-32-le")]
    return forms + [SENTINEL_KEY.encode("utf-32-be")]


def spliced(name: str, members: str) -> str:
    """A synthetic fixture body with `members` (raw JSON) placed before its own fields."""
    return "{" + members + ", " + json.dumps(fixture(name))[1:]


def assert_unsafe_body_left_no_trace(
    run_dir: Path, step_name: str, *texts: str
) -> dict[str, object]:
    manifest = read_manifest(run_dir)
    unsafe = step(manifest, step_name)
    assert unsafe["outcome"] == "credential_detected"
    assert unsafe["client_validation_passed"] is False
    for attempt in unsafe["attempts"]:
        assert attempt["body"] == {
            "recorded": False,
            "omitted": "held the active credential; not written",
        }  # no file, no hash, no size
    assert manifest["run"]["status"] == "aborted"
    assert manifest["run"]["stop_reason"] == rf.CREDENTIAL_REASON
    assert manifest["credential_detected"] is True
    assert not [p for p in (run_dir / "bodies").iterdir() if step_name in p.name]
    assert not list(run_dir.rglob("*.tmp"))
    written = all_bytes(run_dir)
    for form in leak_forms():
        assert form not in written
    for text in texts:
        assert SENTINEL_KEY not in text and ESCAPED_KEY not in text
    assert step_name not in {r.step for r in rf.load_replayable(run_dir)}
    return manifest


# Each body hides the key only in a member that a later duplicate overwrites.
DUPLICATE_SHAPES = {
    "earlier duplicate value": f'"debug": "{ESCAPED_KEY}", "debug": "safe"',
    "nested object overwritten": f'"meta": {{"inner": {{"x": "{ESCAPED_KEY}"}}}}, "meta": {{}}',
    "list overwritten": f'"tags": ["ok", ["{ESCAPED_KEY}"]], "tags": []',
    "name escaped differently": f'"debug": "{ESCAPED_KEY}", "d\\u0065bug": "safe"',
    "fully escaped duplicate name": (
        f'"debug": "{ESCAPED_KEY}", "\\u0064\\u0065\\u0062\\u0075\\u0067": "safe"'
    ),
    "duplicate overwrites a known field": (
        f'"currencyName": "{ESCAPED_KEY}", "currencyName": "coins"'
    ),
}


@pytest.mark.parametrize("shape", sorted(DUPLICATE_SHAPES))
def test_the_guard_checks_members_that_duplicates_overwrite(shape: str) -> None:
    body = spliced("tournament", DUPLICATE_SHAPES[shape]).encode()
    assert SENTINEL_KEY.encode() not in body  # invisible to the raw byte scan
    assert SENTINEL_KEY not in json.dumps(json.loads(body))  # gone after parsing
    assert rf._CredentialGuard(ApiKey(SENTINEL_KEY)).in_body(body)


@pytest.mark.parametrize("shape", sorted(DUPLICATE_SHAPES))
def test_a_duplicate_hidden_credential_aborts_the_run_through_main(
    shape: str,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    body = spliced("tournament", DUPLICATE_SHAPES[shape]).encode()
    router = sig_router(
        {TOURNAMENT_PATH: raw_response(200, body, {"content-type": "application/json"})}
    )
    out, err = io.StringIO(), io.StringIO()
    clock = FakeClock()
    code = rf.main(
        [*REQUIRED, "--output-root", str(tmp_path / "c"), "--capture"],
        environ={"SUSQ_API_KEY": SENTINEL_KEY},
        transport=httpx.MockTransport(router),
        base_url=BASE_URL,
        stdout=out,
        stderr=err,
        run_id="dup",
        build=BUILD,
        clock=clock,
        sleep=clock.sleep,
    )
    assert code == 1
    assert router.paths() == ["/tournaments/alpha-tournament"]  # no subsequent request
    manifest = assert_unsafe_body_left_no_trace(
        tmp_path / "c" / "dup", "tournament", out.getvalue(), err.getvalue(), caplog.text
    )
    assert len(manifest["not_run"]) == 5


def test_a_nested_object_overwritten_later_in_the_run_stops_it(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    members = f'"debug": {{"trace": ["{ESCAPED_KEY}"]}}, "debug": null'
    body = spliced("exchange_orderbook", members).encode()
    router = sig_router(
        {EXCHANGE_BOOK: raw_response(200, body, {"content-type": "application/json"})}
    )
    result, _, _ = capture(tmp_path, router)
    assert result.exit_code == 1
    assert router.paths()[-1] == "/exchanges/36/orderbook"  # nothing sent after it
    printed = capsys.readouterr()
    manifest = assert_unsafe_body_left_no_trace(
        result.run_dir, "exchange-orderbook-36", printed.out, printed.err, caplog.text
    )
    assert step(manifest, "tournament")["outcome"] == "passed"  # earlier evidence kept
    assert manifest["not_run"] == ["market-orderbook-26", "orders"]


@pytest.mark.parametrize(
    "encoding", ["utf-16", "utf-16-le", "utf-16-be", "utf-32", "utf-32-le", "utf-32-be"]
)
def test_utf16_and_utf32_duplicates_are_caught(
    encoding: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    text = spliced("tournament", f'"debug": "{SENTINEL_KEY}", "debug": "safe"')
    body = text.encode(encoding)
    assert SENTINEL_KEY.encode() not in body  # the raw ASCII scan cannot see it
    assert json.loads(body)["debug"] == "safe"  # the client would accept this body
    router = sig_router(
        {TOURNAMENT_PATH: raw_response(200, body, {"content-type": "application/json"})}
    )
    result, _, _ = capture(tmp_path, router)
    assert result.status == "aborted" and result.exit_code == 1
    assert router.paths() == ["/tournaments/alpha-tournament"]
    assert_unsafe_body_left_no_trace(result.run_dir, "tournament", caplog.text)


def test_harmless_duplicates_are_stored_byte_for_byte_and_replay(tmp_path: Path) -> None:
    """Documented policy: duplicate names are not rejected. The body is kept exactly as
    received, and the client keeps the last value, as on any other read."""
    members = '"debug": "a", "d\\u0065bug": "b", "myBalance": 1, "nested": {"x": 1}, "nested": 2'
    body = spliced("tournament", members).encode()
    router = sig_router(
        {TOURNAMENT_PATH: raw_response(200, body, {"content-type": "application/json"})}
    )
    result, manifest, _ = capture(tmp_path, router)
    assert result.status == "succeeded" and result.exit_code == 0
    tournament = step(manifest, "tournament")
    assert tournament["outcome"] == "passed"
    stored = (result.run_dir / tournament["attempts"][0]["body"]["file"]).read_bytes()
    assert stored == body
    replay = {r.step: r for r in rf.load_replayable(result.run_dir)}
    assert replay["tournament"].body == body
    decoded = json.loads(replay["tournament"].body, parse_float=Decimal)
    assert decoded["myBalance"] == Decimal("847.5")  # the fixture's later value wins
    assert decoded["debug"] == "b"
