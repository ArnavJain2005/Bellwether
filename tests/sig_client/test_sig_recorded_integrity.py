"""Integrity and provenance checks for every recorded-derived fixture directory.

These run over each tests/fixtures/sig/recorded/<run>/ directory. Endpoint replays live
in test_sig_recorded_replay*.py, one file per capture.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from recorded_kit import (
    CREATOR,
    REDACTED,
    RUNS,
    THUMBNAIL_SEGMENT,
    TOURNAMENT,
    UNMODIFIED,
    effective_manifest,
    raw_body,
    sha256,
    source_provenance,
)

runs = pytest.mark.parametrize("run_dir", RUNS, ids=[r.name for r in RUNS])
MARKETS_PAGE = re.compile(r"tournament-markets-p[0-9]+")


def test_recorded_runs_are_the_reviewed_ones() -> None:
    assert [r.name for r in RUNS] == ["20261009T122244Z-fa008a", "20261009T190027Z-fb88c9"]


@runs
def test_fixture_bytes_match_the_effective_manifest(run_dir: Path) -> None:
    manifest = effective_manifest(run_dir)
    assert manifest["format"] == "bellwether.sig-fixture/1"
    files = sorted(p.relative_to(run_dir).as_posix() for p in (run_dir / "bodies").iterdir())
    assert files == sorted(f["file"] for f in manifest["fixtures"])
    for f in manifest["fixtures"]:
        data = (run_dir / f["file"]).read_bytes()
        assert sha256(data) == f["sha256"], f["name"]
        assert len(data) == f["bytes"], f["name"]


@runs
def test_source_provenance_is_sanitized_and_labelled(run_dir: Path) -> None:
    manifest = effective_manifest(run_dir)
    data, source = source_provenance(run_dir)
    assert sha256(data) == manifest["source_capture"]["provenance_sha256"]
    assert source["format"] == "bellwether.sig-source-provenance/1"
    assert source["label"] == manifest["source_capture"]["provenance_label"]
    assert "sanitized" in source["label"] and "NOT the byte-exact" in source["label"]
    assert not (run_dir / "source-capture-manifest.json").exists()
    assert (
        source["provenance"]["kind"] == "recorded" == manifest["source_capture"]["provenance_kind"]
    )
    assert source["run"]["id"] == manifest["source_capture"]["run_id"] == run_dir.name
    assert source["run"]["status"] == "succeeded"
    assert source["code"]["git_dirty"] is False
    assert source["credential_detected"] is False and source["withheld_fields"] == 0
    assert source["refused_requests"] == 0 and source["anomalies"] == []
    assert source["not_run"] == []
    assert source["target"]["tournament"] == {"slug": TOURNAMENT.slug, "id": TOURNAMENT.id}
    steps = {s["index"]: s for s in source["steps"]}
    assert sorted(f["source"]["capture_step"] for f in manifest["fixtures"]) == sorted(steps)
    for f in manifest["fixtures"]:
        step = steps[f["source"]["capture_step"]]
        (attempt,) = step["attempts"]
        # Each fixture names the recorded request and body it came from...
        assert step["name"] == f["name"] and step["outcome"] == "passed"
        assert step["client_validation_passed"] is True
        assert attempt["body"]["file"] == f["source"]["body_file"]
        assert attempt["path"] == f["request"]["path"]
        assert attempt["params"] == f["request"]["params"]
        assert attempt["http_status"] == f["response"]["http_status"]
        # ...but only unmodified fixtures keep the original hash, and equal it.
        if f["label"] == UNMODIFIED:
            assert f["redactions"] == []
            assert f["sha256"] == f["source"]["sha256"] == attempt["body"]["sha256"]
            assert f["bytes"] == f["source"]["bytes"] == attempt["body"]["bytes"]
        else:
            assert f["label"] == REDACTED and f["redactions"]
            assert "sha256" not in f["source"] and "bytes" not in f["source"]
            assert "sha256" not in attempt["body"] and "bytes" not in attempt["body"]
            assert attempt["body"]["omitted"] == ["sha256", "bytes"]


@runs
def test_every_committed_hash_is_accounted_for(run_dir: Path) -> None:
    # No original hash of a redacted body, and no hash of the capture manifest, may
    # appear: either would let anyone verify guesses of what was redacted.
    manifest = effective_manifest(run_dir)
    _, source = source_provenance(run_dir)
    allowed = {f["sha256"] for f in manifest["fixtures"]}
    allowed |= {manifest["source_capture"]["provenance_sha256"], source["spec"]["sha256"]}
    found: set[str] = set()
    for path in run_dir.rglob("*.json"):
        found |= set(re.findall(r"\b[0-9a-f]{64}\b", path.read_text(encoding="utf-8")))
    assert found <= allowed
    assert "manifest_sha256" not in json.dumps(manifest)


def resolve(body: Any, path: str) -> Any:
    node = body
    for part in path.removeprefix("$.").replace("[", ".").split("."):
        node = node[int(part[:-1])] if part.endswith("]") else node[part]
    return node


@runs
def test_redactions_hold_their_documented_placeholders(run_dir: Path) -> None:
    for f in effective_manifest(run_dir)["fixtures"]:
        redacted = f["name"] == "tournament" or MARKETS_PAGE.fullmatch(f["name"])
        assert f["label"] == (REDACTED if redacted else UNMODIFIED), f["name"]
        body = raw_body(run_dir, f["name"])
        for redaction in f["redactions"]:
            expected = json.loads(redaction["replacement_json"], parse_float=Decimal)
            assert resolve(body, redaction["path"]) == expected
        paths = sorted(r["path"] for r in f["redactions"])
        if f["name"] == "tournament":
            assert paths == ["$.isPendingEnrolment", "$.myBalance"]
            assert (str(body["myBalance"]), body["isPendingEnrolment"]) == ("12345.67", True)
        elif MARKETS_PAGE.fullmatch(f["name"]):
            assert paths == sorted(
                f"$.data[{i}].{field}"
                for i in range(len(body["data"]))
                for field in ("creator.id", "creator.username", "thumbnailUrl")
            )


@runs
def test_markets_pages_keep_thumbnail_shape_with_placeholders(run_dir: Path) -> None:
    shape = re.compile(
        r"https://r2\.thesuper\.market/market-thumbnails/"
        + re.escape(THUMBNAIL_SEGMENT)
        + r"/[0-9a-f-]{36}\.webp"
    )
    names = [f["name"] for f in effective_manifest(run_dir)["fixtures"]]
    pages = [name for name in names if MARKETS_PAGE.fullmatch(name)]
    assert pages
    for name in pages:
        for market in raw_body(run_dir, name)["data"]:
            assert shape.fullmatch(market["thumbnailUrl"])
            assert (market["creator"]["id"], market["creator"]["username"]) == CREATOR
    observed = effective_manifest(run_dir)["observed_extra_fields"]
    assert [(o["fixture"], o["path"]) for o in observed] == [
        (name, "$.data[*].thumbnailUrl") for name in pages
    ]


@runs
def test_fixture_directory_disables_line_ending_conversion(run_dir: Path) -> None:
    # Hashes cover exact bytes, so a CRLF checkout must not rewrite them.
    assert (run_dir / ".gitattributes").read_bytes().splitlines()[-1] == b"* -text"
    for path in run_dir.rglob("*.json"):
        assert b"\r" not in path.read_bytes(), path.name
