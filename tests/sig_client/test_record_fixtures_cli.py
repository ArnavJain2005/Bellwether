"""S1-T4-A recorder: import, help and preview need no credentials, files or network."""

from __future__ import annotations

import io
import os
import subprocess
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
from recorder_kit import BUILD, config, fixed_now, sig_router
from sig_testkit import BASE_URL, SENTINEL_KEY, TOURNAMENT, FakeClock

from engine.sig_client import record_fixtures as rf
from engine.sig_client.rest import SigRestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
TID = TOURNAMENT.id
REQUIRED = [
    "--tournament-slug", TOURNAMENT.slug,
    "--tournament-id", TID,
    "--exchange-id", "36",
    "--market-id", "26",
]  # fmt: skip


class NoEnvironment(Mapping[str, str]):
    """An environment that fails the test on any read."""

    def __getitem__(self, key: str) -> str:
        raise AssertionError("the environment was read")

    def __iter__(self) -> Iterator[str]:
        raise AssertionError("the environment was read")

    def __len__(self) -> int:
        raise AssertionError("the environment was read")


@pytest.fixture
def no_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    """Credential loading or building an HTTP client fails the test."""

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("credentials or an HTTP client were touched")

    monkeypatch.setattr(rf, "load_sig_config", refuse)
    monkeypatch.setattr(rf.httpx, "AsyncClient", refuse)
    monkeypatch.setattr(rf, "record", refuse)


def test_preview_reads_no_credentials_creates_nothing_sends_nothing(
    tmp_path: Path, no_side_effects: None
) -> None:
    root = tmp_path / "captures"
    out = io.StringIO()
    code = rf.main(
        [*REQUIRED, "--output-root", str(root)], environ=NoEnvironment(), stdout=out
    )
    assert code == 0
    assert not root.exists()
    text = out.getvalue()
    assert "PREVIEW ONLY" in text
    for path in (
        "GET /tournaments/alpha-tournament\n",
        "GET /tournaments/alpha-tournament/markets?limit=10&status=any",
        f"GET /exchanges/prices?ids=36&tournamentId={TID}",
        f"GET /exchanges/36/orderbook?depth=5&tournamentId={TID}",
        f"GET /markets/26/orderbook?depth=5&tournamentId={TID}",
        f"GET /orders?status=open&limit=10&tournamentId={TID}",
    ):
        assert path in text
    assert str(root.resolve()) in text
    assert "Logical reads: at most 6." in text


def test_preview_counts_pages_and_skipped_orders(tmp_path: Path, no_side_effects: None) -> None:
    out = io.StringIO()
    args = [*REQUIRED, "--markets-max-pages", "3", "--skip-orders", "--output-root", "x"]
    assert rf.main(args, environ=NoEnvironment(), stdout=out) == 0
    text = out.getvalue()
    assert "up to 3 pages" in text
    assert "GET /orders?" not in text
    assert "Logical reads: at most 7." in text  # tournament, 3 pages, prices, 2 books


def test_help_has_no_side_effects(
    no_side_effects: None, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as info:
        rf.main(["--help"], environ=NoEnvironment())
    assert info.value.code == 0
    assert "--capture" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("extra", "replace"),
    [
        ([f"--exchange-id={i}" for i in range(100, 110)], None),  # 11 exchanges
        (["--exchange-id", "36"], None),  # duplicate
        ([f"--market-id={i}" for i in range(100, 110)], None),  # 11 markets
        ([], ("--exchange-id", "36", "--exchange-id", "DUMMY-SENTINEL-VALUE")),
        ([], ("--market-id", "26", "--market-id", "1.5")),
        (["--depth", "21"], None),
        (["--depth", "0"], None),
        (["--markets-page-limit", "101"], None),
        (["--markets-max-pages", "4"], None),
        (["--orders-page-limit", "51"], None),
        (["--orders-max-pages", "0"], None),
        (["--orders-status", "everything"], None),
        ([], ("--tournament-id", TID, "--tournament-id", "DUMMY-SENTINEL-VALUE")),
        ([], ("--tournament-slug", TOURNAMENT.slug, "--tournament-slug", "bad slug")),
    ],
)
def test_invalid_arguments_are_rejected_before_anything_runs(
    extra: list[str],
    replace: tuple[str, str, str, str] | None,
    no_side_effects: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = list(REQUIRED)
    if replace is not None:
        flag, old, new_flag, new = replace
        args[args.index(old)] = new
        assert flag == new_flag
    with pytest.raises(SystemExit) as info:
        rf.main([*args, *extra, "--capture"], environ=NoEnvironment())
    assert info.value.code == 2
    assert "DUMMY-SENTINEL-VALUE" not in capsys.readouterr().err  # never echoed


@pytest.mark.parametrize("missing", ["--tournament-slug", "--tournament-id", "--market-id"])
def test_tournament_and_selection_are_required(missing: str, no_side_effects: None) -> None:
    args = list(REQUIRED)
    at = args.index(missing)
    del args[at : at + 2]
    with pytest.raises(SystemExit) as info:
        rf.main(args, environ=NoEnvironment())
    assert info.value.code == 2


def test_capture_without_a_key_stops_before_any_file_or_request(tmp_path: Path) -> None:
    router = sig_router()
    err = io.StringIO()
    code = rf.main(
        [*REQUIRED, "--output-root", str(tmp_path / "c"), "--capture"],
        environ={},
        transport=httpx.MockTransport(router),
        base_url=BASE_URL,
        stderr=err,
    )
    assert code == 2
    assert "SUSQ_API_KEY is not set" in err.getvalue()
    assert router.requests == []
    assert not (tmp_path / "c").exists()


def test_capture_end_to_end_through_main(tmp_path: Path) -> None:
    router = sig_router()
    clock = FakeClock()
    out = io.StringIO()
    code = rf.main(
        [*REQUIRED, "--output-root", str(tmp_path / "c"), "--capture"],
        environ={"SUSQ_API_KEY": SENTINEL_KEY},
        transport=httpx.MockTransport(router),
        base_url=BASE_URL,
        stdout=out,
        run_id="main-run",
        build=BUILD,
        clock=clock,
        sleep=clock.sleep,
    )
    assert code == 0
    assert len(clock.sleeps) == 5  # the default recorder limiter paced six reads
    text = out.getvalue()
    assert "status=succeeded" in text
    assert "Provenance:    synthetic" in text
    assert "Collection tournament_markets: 1 page(s), NOT complete" in text
    assert SENTINEL_KEY not in text
    assert (tmp_path / "c" / "main-run" / "manifest.json").is_file()


def test_keyboard_interrupt_exits_130(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def interrupted(*args: Any, **kwargs: Any) -> Any:
        raise KeyboardInterrupt

    monkeypatch.setattr(rf, "record", interrupted)
    code = rf.main(
        [*REQUIRED, "--output-root", str(tmp_path), "--capture"],
        environ={"SUSQ_API_KEY": SENTINEL_KEY},
        base_url=BASE_URL,
    )
    assert code == 130
    assert "interrupted" in capsys.readouterr().err


def test_recorder_http_client_matches_sig_rest_client_create() -> None:
    """The hooks are the only difference from SigRestClient.create's httpx client."""
    reference = SigRestClient.create(config())._http
    recorder = rf._Recorder(
        rf.CapturePlan(tournament=TOURNAMENT, exchange_ids=("36",), market_ids=("26",)),
        config(),
        run_id="unused",
        provenance="synthetic",
        transport=httpx.MockTransport(sig_router()),
        limiter=rf.RateLimiter(),
        clock=lambda: 0.0,
        sleep=lambda seconds: None,  # type: ignore[arg-type, return-value]
        now=fixed_now,
        build=BUILD,
    )
    http = recorder._client._http
    assert http.timeout == reference.timeout
    assert http.follow_redirects is reference.follow_redirects is False
    assert http.trust_env is reference.trust_env is False
    assert http.auth is None and "authorization" not in http.headers
    assert recorder._client._limiter is recorder._limiter
    assert [len(http.event_hooks[k]) for k in ("request", "response")] == [1, 1]
    assert reference.event_hooks == {"request": [], "response": []}


# --- the real entry point, in a subprocess with the network blocked ---------------------

_GUARD = """
import socket, sys
def refuse(*args, **kwargs):
    sys.stderr.write("REAL NETWORK USED\\n")
    sys.exit(97)
socket.getaddrinfo = refuse
socket.create_connection = refuse
socket.socket.connect = refuse
"""


def _guarded_python(code: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    # A minimal environment without SUSQ_API_KEY: only these non-secret names are read.
    names = (
        "PATH", "SYSTEMROOT", "SystemRoot", "WINDIR", "TEMP", "TMP", "HOME", "LANG",
        "APPDATA", "LOCALAPPDATA", "USERPROFILE",  # Windows user site-packages
    )  # fmt: skip
    env = {name: os.environ[name] for name in names if name in os.environ}
    env["PYTHONPATH"] = str(REPO_ROOT)
    return subprocess.run(
        [sys.executable, "-c", _GUARD + code],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_import_alone_needs_no_key_and_no_network(tmp_path: Path) -> None:
    done = _guarded_python(
        "import engine.sig_client.record_fixtures\nprint('imported')\n", tmp_path
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "imported"


@pytest.mark.parametrize("args", [["--help"], REQUIRED])
def test_module_entry_point_help_and_preview(args: list[str], tmp_path: Path) -> None:
    code = (
        "import runpy, sys\n"
        f"sys.argv = ['record_fixtures', *{args!r}]\n"
        "runpy.run_module('engine.sig_client.record_fixtures', run_name='__main__')\n"
    )
    done = _guarded_python(code, tmp_path)
    assert done.returncode == 0, done.stderr
    assert "REAL NETWORK USED" not in done.stderr
    assert ("--capture" if args == ["--help"] else "PREVIEW ONLY") in done.stdout
    assert list(tmp_path.iterdir()) == []  # nothing created
