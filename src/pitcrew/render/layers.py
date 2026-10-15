"""Capas y permisos del plan, compartidos por todas las vistas.

Este módulo no sabe nada de formatos: agrupa los items del plan en las
cuatro capas alrededor de Claude y deriva en español los permisos que
cada item pide. La vista de texto y la vista HTML lo reutilizan.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import PurePosixPath

from pitcrew.models import (
    AddMcp,
    CopySkill,
    Flag,
    Level,
    ManualStep,
    Operation,
    Plan,
    PlanItem,
    SetEnv,
    TrustCard,
    WriteFile,
)
from pitcrew.skill_paths import in_skills_dir

MISSING_CARD_REASON = "sin ficha del verificador"
BUNDLED_SCRIPT_CODE = "bundled_script"
AMBIGUOUS_CARD_REASON = "ficha ambigua: nombre repetido"
# De más a menos restrictivo: ante ambigüedad gana el primero.
_RESTRICTION_ORDER = [Level.BLOQUEADO, Level.REVISAR, Level.CONFIABLE, Level.VERIFICADO]


class Layer(StrEnum):
    """Capa alrededor de Claude donde vive una herramienta."""

    CONTEXTO = "Contexto"
    CAPACIDADES = "Capacidades"
    CONEXIONES = "Conexiones"
    EXTERNAS = "Herramientas externas"


@dataclass(frozen=True)
class RenderItem:
    """Un item del plan junto con su ficha de confianza."""

    item: PlanItem
    card: TrustCard


def layer_of(op: Operation) -> Layer:
    match op:
        case WriteFile(path=path):
            parts = PurePosixPath(path).parts
            if parts and parts[-1] == "CLAUDE.md":
                return Layer.CONTEXTO
            if in_skills_dir(parts):
                return Layer.CAPACIDADES
            return Layer.CONTEXTO
        case CopySkill():
            return Layer.CAPACIDADES
        case AddMcp() | SetEnv():
            return Layer.CONEXIONES
        case ManualStep():
            return Layer.EXTERNAS


def _layer_of_item(item: PlanItem) -> Layer:
    return layer_of(item.operations[0]) if item.operations else Layer.EXTERNAS


def _missing_card(name: str) -> TrustCard:
    return TrustCard(
        candidate=name,
        level=Level.REVISAR,
        reasons=[MISSING_CARD_REASON],
        flags=[],
        pinned_ref=None,
        popularity=0,
    )


def _ambiguous(card: TrustCard) -> TrustCard:
    """Copia de la ficha marcada como ambigua; nunca queda aprobable en bloque."""
    level = card.level if card.level in (Level.BLOQUEADO, Level.REVISAR) else Level.REVISAR
    return replace(card, level=level, reasons=[*card.reasons, AMBIGUOUS_CARD_REASON])


def _pick_card(name: str, candidates: list[TrustCard], repeated_item: bool) -> TrustCard:
    if not candidates:
        card = _missing_card(name)
    else:
        card = min(candidates, key=lambda c: _RESTRICTION_ORDER.index(c.level))
    if len(candidates) > 1 or repeated_item:
        return _ambiguous(card)
    return card


def group_by_layer(plan: Plan, cards: list[TrustCard]) -> dict[Layer, list[RenderItem]]:
    """Agrupa los items por capa (las cuatro, en orden) conservando el orden del plan.

    Con nombres repetidos (fichas o items) gana la ficha más restrictiva y
    nunca queda mejor que `Revisar`.
    """
    by_name: dict[str, list[TrustCard]] = {}
    for card in cards:
        by_name.setdefault(card.candidate, []).append(card)
    item_counts: dict[str, int] = {}
    for item in plan.items:
        item_counts[item.candidate.name] = item_counts.get(item.candidate.name, 0) + 1
    groups: dict[Layer, list[RenderItem]] = {layer: [] for layer in Layer}
    for item in plan.items:
        name = item.candidate.name
        card = _pick_card(name, by_name.get(name, []), item_counts[name] > 1)
        groups[_layer_of_item(item)].append(RenderItem(item, card))
    return groups


def _permissions_of_op(op: Operation) -> list[str]:
    match op:
        case SetEnv(name=name):
            return [f"pide la variable {name}"]
        case AddMcp(transport=transport, url=url, env_names=env_names):
            perms = [f"pide la variable {n}" for n in env_names]
            if transport == "stdio":
                perms.append("ejecuta código local (stdio)")
            else:
                perms.append(f"conecta a {url if url is not None else '(URL no indicada)'}")
            return perms
        case WriteFile(path=path):
            return [f"escribe {path}"]
        case CopySkill(dest_name=dest_name):
            return [f"escribe .claude/skills/{dest_name}"]
        case ManualStep():
            return []


def permissions_of(ops: list[Operation]) -> list[str]:
    """Permisos que piden las operaciones, sin duplicados y en orden de aparición."""
    perms: list[str] = []
    for op in ops:
        for perm in _permissions_of_op(op):
            if perm not in perms:
                perms.append(perm)
    return perms


def _has_bundled_script(flags: list[Flag]) -> bool:
    return any(flag.code == BUNDLED_SCRIPT_CODE for flag in flags)


def permissions_for(render_item: RenderItem) -> list[str]:
    """Permisos de un item: los de sus operaciones más `incluye scripts` si la ficha lo marca."""
    perms = permissions_of(render_item.item.operations)
    if _has_bundled_script(render_item.card.flags) and "incluye scripts" not in perms:
        perms.append("incluye scripts")
    return perms
