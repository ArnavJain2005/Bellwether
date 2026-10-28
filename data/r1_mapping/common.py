"""Shared constants, file helpers and the finding type for the R1 workflow."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
MARKETS_DIR = REPO_ROOT / "markets"
SOURCES_PATH = MARKETS_DIR / "r1_sources.json"
MAPPING_PATH = MARKETS_DIR / "r1_mapping.json"
EVIDENCE_PATH = MARKETS_DIR / "sig_resolution_evidence.json"
CANDIDATES_PATH = MARKETS_DIR / "r1_candidates.json"
REPORT_PATH = MARKETS_DIR / "R1_REPORT.md"
INFO_TAB_DIR = "markets/sig_info_tab"

VENUES = ("polymarket", "kalshi")

SOURCES_FORMAT = "bellwether.r1-sources/1"
MAPPING_FORMAT = "bellwether.r1-mapping/1"
EVIDENCE_FORMAT = "bellwether.r1-sig-resolution-evidence/1"
CANDIDATES_FORMAT = "bellwether.r1-candidates/1"


@dataclass(frozen=True, order=True)
class Finding:
    """One validation finding. ``where`` points at the specific record or file."""

    code: str
    where: str
    message: str

    def render(self) -> str:
        return f"ERROR [{self.where}] {self.code}: {self.message}"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def sha256_text_lf(text: str) -> str:
    """Hash of UTF-8 text with CRLF/CR normalised to LF, so checkouts agree."""
    normalised = text.replace("\r\n", "\n").replace("\r", "\n")
    return sha256_bytes(normalised.encode("utf-8"))


def load_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_wire_json(path: Path) -> Any:
    """Load a SIG wire body keeping every JSON number exact (CLAUDE.md wire rule)."""
    with open(path, encoding="utf-8") as f:
        return json.load(f, parse_float=Decimal, parse_int=Decimal)


def dump_json(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def _one(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


def dump_layout(obj: Any, expand: tuple[str, ...], path: str = "", indent: int = 0) -> str:
    """JSON with only the listed paths expanded; everything else stays on one line.

    Paths use ``/`` between keys and ``[]`` for list items, for example
    ``records[]/venues``. One reviewed field per line keeps diffs small and readable.
    """
    pad = "  " * indent
    inner = "  " * (indent + 1)
    if isinstance(obj, dict) and (path == "" or path in expand) and obj:
        parts = []
        for key, value in obj.items():
            child = f"{path}/{key}" if path else key
            parts.append(f"{inner}{_one(key)}: {dump_layout(value, expand, child, indent + 1)}")
        return "{\n" + ",\n".join(parts) + "\n" + pad + "}"
    if isinstance(obj, list) and path in expand and obj:
        child = f"{path}[]"
        parts = [f"{inner}{dump_layout(v, expand, child, indent + 1)}" for v in obj]
        return "[\n" + ",\n".join(parts) + "\n" + pad + "]"
    return _one(obj)


MAPPING_LAYOUT = ("reason_code_definitions", "group_proposals", "records", "records[]",
                  "records[]/venues", "records[]/history")
EVIDENCE_LAYOUT = ("entries",)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def rel(path: Path, root: Path = REPO_ROOT) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()
