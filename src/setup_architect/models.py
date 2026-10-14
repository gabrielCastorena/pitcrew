"""Modelos de datos del plan de setup-architect.

Estas dataclasses son el contrato entre todas las piezas del sistema:
el verificador produce `RepoFacts`/`TrustCard`, la skill orquestadora
arma el `Plan`, el renderizador lo muestra y el aplicador ejecuta sus
`Operation`. Ningún módulo debe redefinir estas formas.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal


class Level(StrEnum):
    """Nivel de confianza asignado a un candidato por el verificador."""

    VERIFICADO = "Verificado"
    CONFIABLE = "Confiable"
    REVISAR = "Revisar"
    BLOQUEADO = "Bloqueado"


@dataclass(frozen=True)
class Candidate:
    """Una herramienta propuesta por los subagentes investigadores."""

    name: str
    kind: Literal["skill", "mcp", "cli", "config", "site"]
    source_url: str
    ref: str | None
    description: str
    why_fit: str
    stage: str


@dataclass(frozen=True)
class RepoFacts:
    """Hechos crudos de un repositorio, obtenidos de la API de GitHub."""

    owner: str
    name: str
    created_at: datetime
    pushed_at: datetime
    stars: int
    forks: int
    open_issues: int
    is_fork: bool
    parent: str | None
    ahead_by: int | None
    owner_verified: bool
    head_sha: str
    description: str


@dataclass(frozen=True)
class Flag:
    """Una señal (buena o mala) que el verificador detectó en un candidato."""

    code: str
    detail: str
    veto: bool


@dataclass(frozen=True)
class TrustCard:
    """Resultado del verificador para un candidato: nivel y por qué."""

    candidate: str
    level: Level
    reasons: list[str]
    flags: list[Flag]
    pinned_ref: str | None
    popularity: int


@dataclass(frozen=True)
class WriteFile:
    """Escribe contenido en una ruta de la lista cerrada permitida."""

    path: str
    content: str


@dataclass(frozen=True)
class CopySkill:
    """Copia una skill verificada desde un repo a `.claude/skills/`."""

    repo: str
    sha: str
    subdir: str
    dest_name: str


@dataclass(frozen=True)
class AddMcp:
    """Registra un servidor MCP (única vía para tocar esa config)."""

    name: str
    scope: Literal["user", "project"]
    transport: Literal["stdio", "http"]
    command: list[str] | None
    url: str | None
    env_names: list[str]


@dataclass(frozen=True)
class SetEnv:
    """Define una variable de entorno para un secreto."""

    name: str


@dataclass(frozen=True)
class ManualStep:
    """Guía manual para lo que no se puede automatizar."""

    title: str
    steps: list[str]


type Operation = WriteFile | CopySkill | AddMcp | SetEnv | ManualStep


@dataclass(frozen=True)
class PlanItem:
    """Un candidato aprobado junto con las operaciones para instalarlo."""

    candidate: Candidate
    operations: list[Operation]


@dataclass(frozen=True)
class ResearchedSource:
    """Una fuente que se investigó, haya entrado o no al plan.

    Su `description` es texto externo: el aplicador compara el `CLAUDE.md`
    contra ella y la muestra en el resumen que se aprueba.
    """

    name: str
    source_url: str
    description: str


@dataclass(frozen=True)
class Plan:
    """El plan completo: fuente de verdad para texto, HTML y aplicador.

    `researched` declara todo lo investigado, también lo descartado. No
    tiene valor por defecto: quien construye un plan lo declara siempre.
    """

    schema_version: int
    environment: Literal["global", "project", "chat"]
    items: list[PlanItem]
    researched: list[ResearchedSource]
