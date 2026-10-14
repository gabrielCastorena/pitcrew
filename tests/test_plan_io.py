"""Tests de carga y validación del plan (modelos y plan_io)."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from setup_architect.models import AddMcp, Flag, Level, PlanItem, ResearchedSource, TrustCard
from setup_architect.plan_io import (
    PlanError,
    card_from_dict,
    card_to_dict,
    load_plan,
    parse_item,
    plan_hash,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "plan_minimal.json"


def _load_fixture_dict() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _encode(data: dict[str, Any]) -> bytes:
    return json.dumps(data).encode("utf-8")


def plan_with_op(op: str) -> bytes:
    """Plan válido salvo que su única operación usa `op`."""
    data = _load_fixture_dict()
    data["items"][0]["operations"][0]["op"] = op
    return _encode(data)


def plan_with_extra_field() -> bytes:
    """Plan válido con un campo extra no declarado en el nivel superior."""
    data = _load_fixture_dict()
    data["campo_extra"] = "no debería estar aquí"
    return _encode(data)


# --- casos básicos ------------------------------------------------------

def test_loads_minimal_plan() -> None:
    p = load_plan(FIXTURE_PATH.read_bytes())
    assert p.environment == "project"
    assert isinstance(p.items[0].operations[0], AddMcp)


def test_unknown_op_rejected() -> None:
    with pytest.raises(PlanError, match="operación no permitida: run_shell"):
        load_plan(plan_with_op("run_shell"))


def test_unknown_field_rejected() -> None:
    with pytest.raises(PlanError, match="campo desconocido"):
        load_plan(plan_with_extra_field())


def test_plan_hash_is_sha256() -> None:
    assert plan_hash(b"x") == hashlib.sha256(b"x").hexdigest()


# --- casos borde --------------------------------------------------------

def test_invalid_json_rejected() -> None:
    with pytest.raises(PlanError, match="JSON inválido"):
        load_plan(b"{esto no es json valido")


def test_root_not_object_rejected() -> None:
    with pytest.raises(PlanError, match="se esperaba un objeto"):
        load_plan(b"[]")


def test_missing_field_rejected() -> None:
    data = _load_fixture_dict()
    del data["environment"]
    with pytest.raises(PlanError, match="falta el campo 'environment'"):
        load_plan(_encode(data))


def test_wrong_type_rejected() -> None:
    data = _load_fixture_dict()
    data["schema_version"] = "uno"
    with pytest.raises(PlanError, match="tipo inválido"):
        load_plan(_encode(data))


def test_invalid_environment_literal_rejected() -> None:
    data = _load_fixture_dict()
    data["environment"] = "nube"
    with pytest.raises(PlanError, match="valor inválido"):
        load_plan(_encode(data))


def test_invalid_candidate_kind_literal_rejected() -> None:
    data = _load_fixture_dict()
    data["items"][0]["candidate"]["kind"] = "framework"
    with pytest.raises(PlanError, match="valor inválido"):
        load_plan(_encode(data))


def test_unknown_field_in_candidate_rejected() -> None:
    data = _load_fixture_dict()
    data["items"][0]["candidate"]["extra"] = "sobra"
    with pytest.raises(PlanError, match="campo desconocido"):
        load_plan(_encode(data))


def test_unknown_field_in_operation_rejected() -> None:
    data = _load_fixture_dict()
    data["items"][0]["operations"][0]["extra"] = "sobra"
    with pytest.raises(PlanError, match="campo desconocido"):
        load_plan(_encode(data))


def test_missing_field_in_operation_rejected() -> None:
    data = _load_fixture_dict()
    del data["items"][0]["operations"][0]["env_names"]
    with pytest.raises(PlanError, match="falta el campo 'env_names'"):
        load_plan(_encode(data))


def test_operation_op_not_string_rejected_with_type_message() -> None:
    # `op` presente pero con tipo incorrecto: es "tipo inválido", no "falta el campo".
    data = _load_fixture_dict()
    data["items"][0]["operations"][0]["op"] = 123
    with pytest.raises(PlanError, match="tipo inválido"):
        load_plan(_encode(data))


def test_operation_missing_op_rejected_with_missing_field_message() -> None:
    # `op` ausente del todo: aquí sí es "falta el campo".
    data = _load_fixture_dict()
    del data["items"][0]["operations"][0]["op"]
    with pytest.raises(PlanError, match="falta el campo 'op'"):
        load_plan(_encode(data))


# --- parse_item debe ser reutilizable sin schema_version/environment ----

def test_parse_item_reusable_without_plan_wrapper() -> None:
    data = _load_fixture_dict()
    item = parse_item(data["items"][0])
    assert isinstance(item, PlanItem)
    assert isinstance(item.operations[0], AddMcp)


def test_parse_item_rejects_unknown_op_like_load_plan() -> None:
    data = _load_fixture_dict()
    item_data = data["items"][0]
    item_data["operations"][0]["op"] = "run_shell"
    with pytest.raises(PlanError, match="operación no permitida: run_shell"):
        parse_item(item_data)


# --- card_to_dict / card_from_dict (serialización de cards.json) ------------

CARD = TrustCard(
    candidate="demo",
    level=Level.BLOQUEADO,
    reasons=["dangerous_pipe: SKILL.md"],
    flags=[Flag(code="dangerous_pipe", detail="SKILL.md", veto=True)],
    pinned_ref="a" * 40,
    popularity=42,
)


def test_card_to_dict_serializes_level_as_value() -> None:
    d = card_to_dict(CARD)
    assert d["level"] == "Bloqueado"


def test_card_to_dict_serializes_flags_as_objects() -> None:
    d = card_to_dict(CARD)
    assert d["flags"] == [{"code": "dangerous_pipe", "detail": "SKILL.md", "veto": True}]


def test_card_to_dict_is_json_serializable() -> None:
    assert json.loads(json.dumps(card_to_dict(CARD))) == card_to_dict(CARD)


def test_card_from_dict_round_trips() -> None:
    assert card_from_dict(card_to_dict(CARD)) == CARD


def test_card_from_dict_level_is_level_enum() -> None:
    card = card_from_dict(card_to_dict(CARD))
    assert card.level is Level.BLOQUEADO


def test_card_to_dict_pinned_ref_none() -> None:
    card = TrustCard(
        candidate="demo", level=Level.REVISAR, reasons=[], flags=[], pinned_ref=None, popularity=0
    )
    d = card_to_dict(card)
    assert d["pinned_ref"] is None
    assert card_from_dict(d) == card


# --- card_from_dict valida -------------


def _card_dict() -> dict[str, Any]:
    return card_to_dict(CARD)


def test_card_from_dict_missing_field_is_plan_error() -> None:
    d = _card_dict()
    del d["reasons"]
    with pytest.raises(PlanError):
        card_from_dict(d)


def test_card_from_dict_unknown_field_is_plan_error() -> None:
    d = _card_dict()
    d["extra"] = 1
    with pytest.raises(PlanError):
        card_from_dict(d)


def test_card_from_dict_not_a_dict_is_plan_error() -> None:
    with pytest.raises(PlanError):
        card_from_dict([])  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "field,value",
    [
        ("candidate", 1),
        ("reasons", "texto"),
        ("reasons", [1]),
        ("flags", {}),
        ("flags", [1]),
        ("pinned_ref", 3),
        ("popularity", "x"),
        ("popularity", True),
    ],
)
def test_card_from_dict_wrong_type_is_plan_error(field: str, value: Any) -> None:
    d = _card_dict()
    d[field] = value
    with pytest.raises(PlanError):
        card_from_dict(d)


def test_card_from_dict_flag_errors_are_plan_error() -> None:
    d = _card_dict()
    d["flags"] = [{"code": "a", "detail": "b"}]
    with pytest.raises(PlanError):
        card_from_dict(d)
    d["flags"] = [{"code": "a", "detail": "b", "veto": "si"}]
    with pytest.raises(PlanError):
        card_from_dict(d)
    d["flags"] = [{"code": "a", "detail": "b", "veto": True, "x": 1}]
    with pytest.raises(PlanError):
        card_from_dict(d)


@pytest.mark.parametrize("level", ["verificado", "Otro", 1, None, ["Revisar"]])
def test_card_from_dict_bad_level_is_plan_error(level: Any) -> None:
    d = _card_dict()
    d["level"] = level
    with pytest.raises(PlanError):
        card_from_dict(d)


def test_load_plan_non_utf8_bytes_is_plan_error() -> None:
    with pytest.raises(PlanError):
        load_plan(b"\xff\xfe{")


def test_load_plan_deep_nesting_is_plan_error() -> None:
    with pytest.raises(PlanError):
        load_plan(b"[" * 100000 + b"]" * 100000)


# === el plan lleva lo investigado (`researched`) ===============================

RESEARCHED = [
    {
        "name": "github-mcp",
        "source_url": "https://github.com/github/github-mcp-server",
        "description": "Servidor MCP oficial de GitHub para repos, issues y PRs.",
    },
    {
        "name": "descartado",
        "source_url": "https://example.com/blog/patron",
        "description": "Texto de una fuente que no entró al plan.",
    },
]


def _plan_with_researched(researched: Any) -> bytes:
    data = _load_fixture_dict()
    data["researched"] = researched
    return _encode(data)


def test_load_plan_reads_researched_sources() -> None:
    plan = load_plan(_plan_with_researched(RESEARCHED))
    assert plan.researched == [
        ResearchedSource(
            name="github-mcp",
            source_url="https://github.com/github/github-mcp-server",
            description="Servidor MCP oficial de GitHub para repos, issues y PRs.",
        ),
        ResearchedSource(
            name="descartado",
            source_url="https://example.com/blog/patron",
            description="Texto de una fuente que no entró al plan.",
        ),
    ]
    assert plan.schema_version == 1 and len(plan.items) == 1


def test_load_plan_accepts_an_empty_researched_list() -> None:
    assert load_plan(_plan_with_researched([])).researched == []


def test_researched_source_is_frozen() -> None:
    source = ResearchedSource(name="a", source_url="b", description="c")
    with pytest.raises(dataclasses.FrozenInstanceError):
        source.description = "otra"  # type: ignore[misc]


def test_plan_without_researched_is_rejected() -> None:
    # Fail closed: un plan que omite el campo no se salta la comprobación en silencio.
    data = _load_fixture_dict()
    data.pop("researched", None)
    with pytest.raises(PlanError) as excinfo:
        load_plan(_encode(data))
    assert str(excinfo.value) == "falta el campo 'researched' en plan"


@pytest.mark.parametrize("researched", [None, {}, "texto", 3])
def test_researched_that_is_not_a_list_is_rejected(researched: Any) -> None:
    with pytest.raises(PlanError, match="se esperaba una lista en plan.researched"):
        load_plan(_plan_with_researched(researched))


def test_unknown_field_in_researched_entry_is_rejected() -> None:
    entry = {**RESEARCHED[0], "why_fit": "sobra"}
    with pytest.raises(PlanError) as excinfo:
        load_plan(_plan_with_researched([RESEARCHED[1], entry]))
    assert str(excinfo.value) == "campo desconocido en researched[1]: why_fit"


@pytest.mark.parametrize("field", ["name", "source_url", "description"])
def test_missing_field_in_researched_entry_is_rejected(field: str) -> None:
    entry = {k: v for k, v in RESEARCHED[0].items() if k != field}
    with pytest.raises(PlanError) as excinfo:
        load_plan(_plan_with_researched([entry]))
    assert str(excinfo.value) == f"falta el campo '{field}' en researched[0]"


@pytest.mark.parametrize("field", ["name", "source_url", "description"])
@pytest.mark.parametrize("value", [None, 1, ["texto"], {"a": "b"}, True])
def test_wrong_type_in_researched_entry_is_rejected(field: str, value: Any) -> None:
    entry = {**RESEARCHED[0], field: value}
    with pytest.raises(PlanError) as excinfo:
        load_plan(_plan_with_researched([entry]))
    assert str(excinfo.value) == f"tipo inválido en researched[0].{field}: se esperaba str"


@pytest.mark.parametrize("entry", ["texto", None, 1, ["a", "b", "c"]])
def test_researched_entry_that_is_not_an_object_is_rejected(entry: Any) -> None:
    with pytest.raises(PlanError, match=r"se esperaba un objeto en researched\[0\]"):
        load_plan(_plan_with_researched([entry]))


def test_researched_changes_the_plan_hash() -> None:
    # El campo es parte del archivo: lo que se aprueba lo incluye.
    assert plan_hash(_plan_with_researched(RESEARCHED)) != plan_hash(_plan_with_researched([]))


def test_minimal_fixture_declares_researched() -> None:
    assert "researched" in _load_fixture_dict()
