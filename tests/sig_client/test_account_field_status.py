"""A2-A: the numeric-field status table (design 4.1 R5, 5.9), provenance invariants and
the static boundaries of the account-read code (design T46, A2 part)."""

from __future__ import annotations

import ast
import dataclasses
import json
import re
import types
import typing
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from engine.sig_client import account_reads, rest, wire
from engine.sig_client.account_reads import (
    NUMERIC_FIELD_STATUS,
    CoverageState,
    FieldState,
    NumericFieldStatus,
    ReadProvenance,
    ScopeEvidence,
    VerificationUse,
    numeric_field_status,
)
from engine.sig_client.wire import (
    FillsCoverage,
    Order,
    OrderCoverage,
    OrderFill,
    OrderFillsCoverage,
    OrderFillsPage,
    PositionLot,
    PositionsSummary,
    Tournament,
    TournamentFill,
    TournamentPnl,
    TournamentPosition,
    TournamentSettlementEntry,
)

ROOT = Path(__file__).resolve().parents[2]
SPEC = json.loads((ROOT / "docs" / "api" / "openapi.json").read_text(encoding="utf-8"))
PRD = (ROOT / "docs" / "PRD.md").read_text(encoding="utf-8")
DESIGN = (ROOT / "docs" / "design" / "account-state-contracts.md").read_text(encoding="utf-8")
ACCOUNT_MODELS: tuple[type[BaseModel], ...] = (
    Tournament,
    TournamentPosition,
    PositionLot,
    PositionsSummary,
    Order,
    OrderCoverage,
    # A2-B
    TournamentFill,
    FillsCoverage,
    TournamentPnl,
    TournamentSettlementEntry,
    OrderFillsPage,
    OrderFill,
    OrderFillsCoverage,
)


def is_numeric(annotation: Any) -> bool:
    if annotation is bool:
        return False
    if annotation in (int, Decimal):
        return True
    origin = typing.get_origin(annotation)
    if origin is typing.Annotated:
        return is_numeric(typing.get_args(annotation)[0])
    if origin in (typing.Union, types.UnionType):
        return any(is_numeric(arg) for arg in typing.get_args(annotation))
    return False


def resolve(pointer: str) -> Any:
    node: Any = SPEC
    assert pointer.startswith("#/")
    for part in pointer[2:].split("/"):
        node = node[part.replace("~1", "/").replace("~0", "~")]
    return node


def test_every_numeric_field_of_the_a2_models_has_a_status() -> None:
    numeric = {
        (model.__name__, name)
        for model in ACCOUNT_MODELS
        for name, info in model.model_fields.items()
        if is_numeric(info.annotation)
    }
    assert numeric == set(NUMERIC_FIELD_STATUS)  # nothing missing, nothing stale


@pytest.mark.parametrize(
    "entry", list(NUMERIC_FIELD_STATUS.values()), ids=lambda e: f"{e.model}.{e.field}"
)
def test_every_pointer_resolves_to_a_numeric_schema(entry: NumericFieldStatus) -> None:
    schema = resolve(entry.pointer)
    kinds = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
    assert {"number", "integer"} & set(kinds)
    assert entry.pointer.endswith(f"/{entry.field}")


@pytest.mark.parametrize(
    "entry", list(NUMERIC_FIELD_STATUS.values()), ids=lambda e: f"{e.model}.{e.field}"
)
def test_every_question_id_exists(entry: NumericFieldStatus) -> None:
    for question in entry.questions:
        assert re.fullmatch(r"OQ-[A-Z]+[0-9]+|A1-N[0-9]+", question)
        assert question in (PRD if question.startswith("OQ-") else DESIGN), question


@pytest.mark.parametrize(
    ("model", "field", "questions"),
    [
        (Tournament, "myBalance", {"OQ-C3", "OQ-W11"}),
        (TournamentPosition, "avgCost", {"OQ-W5"}),
        (TournamentPosition, "currentPrice", {"OQ-W5", "OQ-D1"}),
        (TournamentPosition, "marketValue", {"OQ-W5"}),
        (TournamentPosition, "quantity", {"OQ-W5", "OQ-W8"}),
        (PositionLot, "entryPrice", {"OQ-W5"}),
        (Order, "priceLimit", {"OQ-W6"}),
        (OrderCoverage, "projectedThroughSequence", {"A1-N1"}),
    ],
)
def test_the_design_5_9_open_questions_travel_with_the_fields(
    model: type, field: str, questions: set[str]
) -> None:
    entry = numeric_field_status(model, field)
    assert entry.state is FieldState.UNRESOLVED
    assert questions <= set(entry.questions)


def test_documented_fields_carry_nothing_open() -> None:
    for model, field in [(Order, "quantity"), (Tournament, "memberCount")]:
        entry = numeric_field_status(model, field)
        assert entry.state is FieldState.DOCUMENTED and entry.questions == ()


def test_the_table_is_read_only_and_self_checking() -> None:
    with pytest.raises(TypeError):
        NUMERIC_FIELD_STATUS[("Order", "x")] = NUMERIC_FIELD_STATUS[("Order", "id")]  # type: ignore[index]
    with pytest.raises(KeyError):
        numeric_field_status(Order, "side")
    with pytest.raises(ValueError, match="names its questions"):
        NumericFieldStatus("M", "f", "#/x", FieldState.UNRESOLVED, "", "open", ())
    with pytest.raises(ValueError, match="nothing open"):
        NumericFieldStatus("M", "f", "#/x", FieldState.DOCUMENTED, "", "", ("OQ-W5",))


# --- ReadProvenance invariants --------------------------------------------------------------


def provenance(**changes: Any) -> ReadProvenance:
    base: dict[str, Any] = {
        "route": "/orders",
        "path": "/orders",
        "params": (),
        "cursor_sent": False,
        "scope": ScopeEvidence.TOURNAMENT_ID_SENT,
        "attempts": 1,
        "http_status": 200,
        "started_mono": 1.0,
        "finished_mono": 2.0,
        "finished_utc": datetime(2026, 10, 13, tzinfo=UTC),
        "page_index": 0,
        "coverage_state": CoverageState.OMITTED,
        "coverage": None,
        "verification": VerificationUse.CACHED,
        "verification_read": None,
    }
    return ReadProvenance(**(base | changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"attempts": 0},
        {"attempts": True},
        {"finished_mono": 0.5},
        {"started_mono": float("nan")},
        {"page_index": -1},
        {"coverage_state": CoverageState.PRESENT},  # present but nothing kept
        {"coverage": OrderCoverage(complete=True, projectedThroughSequence=1)},  # omitted but kept
        {"verification": VerificationUse.PERFORMED},  # performed but no record
        {"finished_utc": datetime(2026, 10, 13)},  # noqa: DTZ001 - naive on purpose
    ],
)
def test_provenance_rejects_inconsistent_records(changes: dict[str, Any]) -> None:
    with pytest.raises((ValueError, TypeError)):
        provenance(**changes)


def test_provenance_is_immutable() -> None:
    record = provenance()
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.attempts = 2  # type: ignore[misc]


def test_scope_evidence_cannot_express_a_default_context() -> None:
    assert {kind.name for kind in ScopeEvidence} == {
        "ECHO",
        "VERIFIED_SLUG_PATH",
        "TOURNAMENT_ID_SENT",
    }


# --- static boundaries (design T46, A2 part) --------------------------------------------------


@pytest.mark.parametrize("module", [account_reads, rest, wire])
def test_account_read_modules_never_import_shared(module: types.ModuleType) -> None:
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))  # type: ignore[arg-type]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not any(alias.name.split(".")[0] == "shared" for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] != "shared"


def test_account_reads_module_does_no_float_conversion_or_rounding() -> None:
    source = Path(account_reads.__file__).read_text(encoding="utf-8")  # type: ignore[arg-type]
    calls = {
        node.func.id
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert not calls & {"float", "round", "int"}


def test_no_account_read_requests_a_default_context_path() -> None:
    source = Path(rest.__file__).read_text(encoding="utf-8")  # type: ignore[arg-type]
    for default_path in ('"/account"', '"/portfolio/', 'f"/portfolio/'):
        assert default_path not in source
