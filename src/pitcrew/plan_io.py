"""Carga y validación del plan JSON de Pitcrew.

La validación es manual con stdlib (`json` y un mapa `op -> dataclass`),
sin librerías de esquemas. `load_plan` nunca deja escapar `KeyError` ni
`TypeError`: cualquier problema de forma, tipo o valor se traduce a
`PlanError`.

`parse_item` se expone por separado porque `pitcrew verify` necesita
parsear items sueltos (`{"items": [PlanItem...]}`, sin `schema_version`
ni `environment`) con la misma lista cerrada de operaciones.

`card_to_dict`/`card_from_dict` serializan `TrustCard` a y desde un
`dict` listo para JSON: el renderizador y el aplicador leen
`cards.json` a través de ellas.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pitcrew.models import (
    AddMcp,
    Candidate,
    CopySkill,
    Flag,
    Level,
    ManualStep,
    Operation,
    Plan,
    PlanItem,
    ResearchedSource,
    SetEnv,
    TrustCard,
    WriteFile,
)


class PlanError(Exception):
    """Error al cargar o validar un plan."""


# --- helpers de validación de tipos -----------------------------------

def _expect_dict(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PlanError(f"se esperaba un objeto en {where}")
    return value


def _expect_list(value: Any, where: str) -> list[Any]:
    if not isinstance(value, list):
        raise PlanError(f"se esperaba una lista en {where}")
    return value


def _check_known_fields(data: dict[str, Any], known: set[str], where: str) -> None:
    extra = set(data) - known
    if extra:
        raise PlanError(
            f"campo desconocido en {where}: {', '.join(sorted(extra))}"
        )


def _require(data: dict[str, Any], name: str, where: str) -> Any:
    if name not in data:
        raise PlanError(f"falta el campo '{name}' en {where}")
    return data[name]


def _as_str(value: Any, field: str, where: str) -> str:
    if not isinstance(value, str):
        raise PlanError(f"tipo inválido en {where}.{field}: se esperaba str")
    return value


def _as_optional_str(value: Any, field: str, where: str) -> str | None:
    if value is None:
        return None
    return _as_str(value, field, where)


def _as_int(value: Any, field: str, where: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise PlanError(f"tipo inválido en {where}.{field}: se esperaba int")
    return value


def _as_literal(value: Any, options: tuple[str, ...], field: str, where: str) -> str:
    text = _as_str(value, field, where)
    if text not in options:
        opciones = ", ".join(options)
        raise PlanError(
            f"valor inválido en {where}.{field}: {text!r} (opciones: {opciones})"
        )
    return text


def _as_str_list(value: Any, field: str, where: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise PlanError(f"tipo inválido en {where}.{field}: se esperaba list[str]")
    return list(value)


def _as_optional_str_list(value: Any, field: str, where: str) -> list[str] | None:
    if value is None:
        return None
    return _as_str_list(value, field, where)


# --- especificaciones declarativas por dataclass -----------------------
# Cada campo mapea a un conversor (value, field, where) -> valor validado.

_CANDIDATE_SPEC: dict[str, Any] = {
    "name": _as_str,
    "kind": lambda v, f, w: _as_literal(v, ("skill", "mcp", "cli", "config", "site"), f, w),
    "source_url": _as_str,
    "ref": _as_optional_str,
    "description": _as_str,
    "why_fit": _as_str,
    "stage": _as_str,
}

_WRITE_FILE_SPEC: dict[str, Any] = {
    "path": _as_str,
    "content": _as_str,
}

_COPY_SKILL_SPEC: dict[str, Any] = {
    "repo": _as_str,
    "sha": _as_str,
    "subdir": _as_str,
    "dest_name": _as_str,
}

_ADD_MCP_SPEC: dict[str, Any] = {
    "name": _as_str,
    "scope": lambda v, f, w: _as_literal(v, ("user", "project"), f, w),
    "transport": lambda v, f, w: _as_literal(v, ("stdio", "http"), f, w),
    "command": _as_optional_str_list,
    "url": _as_optional_str,
    "env_names": _as_str_list,
}

_SET_ENV_SPEC: dict[str, Any] = {
    "name": _as_str,
}

_MANUAL_STEP_SPEC: dict[str, Any] = {
    "title": _as_str,
    "steps": _as_str_list,
}

# Lista cerrada de operaciones permitidas: op (snake_case) -> (dataclass, spec).
_OPERATIONS: dict[str, tuple[type, dict[str, Any]]] = {
    "write_file": (WriteFile, _WRITE_FILE_SPEC),
    "copy_skill": (CopySkill, _COPY_SKILL_SPEC),
    "add_mcp": (AddMcp, _ADD_MCP_SPEC),
    "set_env": (SetEnv, _SET_ENV_SPEC),
    "manual_step": (ManualStep, _MANUAL_STEP_SPEC),
}


def _build(cls: type, data: dict[str, Any], spec: dict[str, Any], where: str) -> Any:
    _check_known_fields(data, set(spec), where)
    kwargs = {}
    for field, convert in spec.items():
        value = _require(data, field, where)
        kwargs[field] = convert(value, field, where)
    return cls(**kwargs)


def _parse_candidate(data: Any, where: str) -> Candidate:
    data = _expect_dict(data, where)
    return _build(Candidate, data, _CANDIDATE_SPEC, where)


def _parse_operation(data: Any, where: str) -> Operation:
    data = _expect_dict(data, where)
    if "op" not in data:
        raise PlanError(f"falta el campo 'op' en {where}")
    op = data["op"]
    if not isinstance(op, str):
        raise PlanError(f"tipo inválido en {where}.op: se esperaba str")
    entry = _OPERATIONS.get(op)
    if entry is None:
        raise PlanError(f"operación no permitida: {op}")
    cls, spec = entry
    rest = {k: v for k, v in data.items() if k != "op"}
    return _build(cls, rest, spec, where)


_PLAN_ITEM_FIELDS = {"candidate", "operations"}


def parse_item(data: Any, where: str = "item") -> PlanItem:
    """Parsea un `PlanItem` suelto: `{"candidate": ..., "operations": [...]}`.

    Reutilizable fuera de `load_plan` (p. ej. para verificar items sin el
    envoltorio `schema_version`/`environment` de un plan completo).
    """
    data = _expect_dict(data, where)
    _check_known_fields(data, _PLAN_ITEM_FIELDS, where)
    candidate = _parse_candidate(_require(data, "candidate", where), f"{where}.candidate")
    operations_data = _expect_list(_require(data, "operations", where), f"{where}.operations")
    operations = [
        _parse_operation(op_data, f"{where}.operations[{i}]")
        for i, op_data in enumerate(operations_data)
    ]
    return PlanItem(candidate=candidate, operations=operations)


_PLAN_FIELDS = {"schema_version", "environment", "items", "researched"}

_RESEARCHED_SPEC: dict[str, Any] = {
    "name": _as_str,
    "source_url": _as_str,
    "description": _as_str,
}


def _parse_researched(data: Any, where: str) -> ResearchedSource:
    data = _expect_dict(data, where)
    return _build(ResearchedSource, data, _RESEARCHED_SPEC, where)


def load_plan(data: bytes) -> Plan:
    """Carga y valida un plan completo desde JSON crudo.

    Rechaza con `PlanError`: JSON inválido, operaciones fuera de la lista
    cerrada, campos desconocidos o faltantes en cualquier nivel, tipos
    incorrectos y valores fuera de los `Literal` declarados.

    `researched` es obligatorio (puede ser una lista vacía): un plan que
    lo omite no se carga, para que no pueda saltarse en silencio la
    comparación del `CLAUDE.md` contra lo investigado.
    """
    try:
        raw = json.loads(data)
    except (ValueError, RecursionError) as exc:
        # ValueError cubre JSONDecodeError y UnicodeDecodeError (bytes no
        # UTF-8); RecursionError, el anidamiento excesivo.
        raise PlanError(f"JSON inválido: {exc}") from exc

    raw = _expect_dict(raw, "plan")
    _check_known_fields(raw, _PLAN_FIELDS, "plan")

    schema_version = _as_int(_require(raw, "schema_version", "plan"), "schema_version", "plan")
    environment = _as_literal(
        _require(raw, "environment", "plan"),
        ("global", "project", "chat"),
        "environment",
        "plan",
    )
    items_data = _expect_list(_require(raw, "items", "plan"), "plan.items")
    items = [parse_item(item, f"items[{i}]") for i, item in enumerate(items_data)]
    researched_data = _expect_list(_require(raw, "researched", "plan"), "plan.researched")
    researched = [
        _parse_researched(entry, f"researched[{i}]")
        for i, entry in enumerate(researched_data)
    ]

    return Plan(
        schema_version=schema_version,
        environment=environment,
        items=items,
        researched=researched,
    )


def plan_hash(data: bytes) -> str:
    """Hash sha256 (hex) del contenido crudo del plan."""
    return hashlib.sha256(data).hexdigest()


# --- serialización de TrustCard ----------------------------------------


def card_to_dict(card: TrustCard) -> dict[str, Any]:
    """Serializa `card` a un `dict` listo para `json.dumps`.

    `level` se serializa como su valor (`"Verificado"`, no `Level.VERIFICADO`)
    y cada bandera como un objeto con sus tres campos.
    """
    return {
        "candidate": card.candidate,
        "level": card.level.value,
        "reasons": list(card.reasons),
        "flags": [
            {"code": flag.code, "detail": flag.detail, "veto": flag.veto}
            for flag in card.flags
        ],
        "pinned_ref": card.pinned_ref,
        "popularity": card.popularity,
    }


def _as_bool(value: Any, field: str, where: str) -> bool:
    if not isinstance(value, bool):
        raise PlanError(f"tipo inválido en {where}.{field}: se esperaba bool")
    return value


def _as_level(value: Any, field: str, where: str) -> Level:
    text = _as_str(value, field, where)
    try:
        return Level(text)
    except ValueError:
        opciones = ", ".join(level.value for level in Level)
        raise PlanError(
            f"valor inválido en {where}.{field}: {text!r} (opciones: {opciones})"
        ) from None


_FLAG_SPEC: dict[str, Any] = {"code": _as_str, "detail": _as_str, "veto": _as_bool}


def card_from_dict(d: dict[str, Any], where: str = "ficha") -> TrustCard:
    """Reconstruye una `TrustCard` desde el `dict` de `card_to_dict`.

    Valida campos faltantes o desconocidos, tipos y `level`; cualquier
    problema se traduce a `PlanError`.
    """
    data = _expect_dict(d, where)
    _check_known_fields(
        data,
        {"candidate", "level", "reasons", "flags", "pinned_ref", "popularity"},
        where,
    )
    flags_data = _expect_list(_require(data, "flags", where), f"{where}.flags")
    flags = [
        _build(Flag, _expect_dict(f, f"{where}.flags[{i}]"), _FLAG_SPEC, f"{where}.flags[{i}]")
        for i, f in enumerate(flags_data)
    ]
    return TrustCard(
        candidate=_as_str(_require(data, "candidate", where), "candidate", where),
        level=_as_level(_require(data, "level", where), "level", where),
        reasons=_as_str_list(_require(data, "reasons", where), "reasons", where),
        flags=flags,
        pinned_ref=_as_optional_str(_require(data, "pinned_ref", where), "pinned_ref", where),
        popularity=_as_int(_require(data, "popularity", where), "popularity", where),
    )
