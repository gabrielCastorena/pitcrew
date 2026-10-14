"""Rúbrica de confianza: decide el nivel de un candidato sin usar LLM.

`evaluate` combina los hechos crudos del repo (`RepoFacts`), las
banderas de typosquatting y de escaneo de skills, y las operaciones
que el candidato instalaría, en una `TrustCard` con uno de los cuatro
niveles de `Level`. Es el corazón del verificador: determinista, sin
red y sin LLM, para que ningún texto de internet pueda influir en el
resultado. Un veto siempre gana; un error o un dato faltante nunca
puede subir el nivel.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import PurePosixPath

from setup_architect.config import Config
from setup_architect.models import (
    AddMcp,
    Candidate,
    Flag,
    Level,
    Operation,
    RepoFacts,
    TrustCard,
    WriteFile,
)
from setup_architect.skill_paths import in_skills_dir
from setup_architect.verify.typosquat import detect_typosquat

_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
_UNSUPPORTED_SOURCE_DETAIL = "la fuente no es un repo de GitHub soportado"


def evaluate(
    candidate: Candidate,
    ops: list[Operation],
    facts: RepoFacts | None,
    skill_flags: list[Flag],
    config: Config,
    now: datetime,
    error: str | None = None,
) -> TrustCard:
    """Evalúa a `candidate` y devuelve su `TrustCard`.

    El algoritmo sigue este orden; la primera regla que aplica decide:

    1. Si hay `error`, el nivel es `Revisar` ("no se pudo verificar: {error}")
       y la ficha lleva la bandera `unverified`: el aplicador no instala
       lo que no se pudo verificar, ni con confirmación.
    2. Si `facts` es `None`, el nivel es `Revisar` ("fuente no soportada")
       y la ficha lleva la bandera `unsupported_source`: nada del item se
       verificó. Se suman la razón y la bandera de cada `AddMcp` (regla 6)
       y de cada `WriteFile` (regla 7).
    3. Se juntan las banderas (typosquat, estrellas infladas, fork casi
       idéntico y `skill_flags`); cualquier veto da `Bloqueado`.
    4. Se calculan `official`, `healthy` y `high_risk`.
    5. `official` y `healthy` dan `Verificado`. Si no es oficial,
       `healthy` sin `high_risk` y con el fork ya comparado da
       `Confiable`. Todo lo demás da `Revisar`.
    6. Un item con algún `AddMcp` nunca pasa de `Revisar`: se verifica el
       repo de GitHub, no el paquete, la imagen ni la URL que el MCP
       ejecutaría. Lleva una razón y una bandera `unverified_mcp` por cada
       `AddMcp`.
    7. Un item con algún `WriteFile` tampoco pasa de `Revisar`, sea cual
       sea la ruta (un `CLAUDE.md` o un archivo bajo `.claude/skills/`) y
       la fuente que declare, por el mismo motivo: el nivel saldría del
       repo de la fuente, no del contenido que escribe el plan. Lleva una
       razón y una bandera `unverified_content` (con la ruta) por cada uno.
    8. Una bandera `allowed_tools` en `skill_flags` (la skill pre-aprueba
       herramientas, que Claude usaría sin pedir permiso) limita el nivel a
       `Revisar`, con una razón por bandera. Aplica a oficiales y a terceros.

    Las reglas 6 a 8 nunca le ganan a un veto.

    `pinned_ref` es el `ref` del candidato si es un SHA de 40 hex; si
    no, `facts.head_sha`. `popularity` es `facts.stars` (0 si no hay
    `facts`) y nunca entra en los pasos 1 a 5.
    """
    if error is not None:
        return TrustCard(
            candidate=candidate.name,
            level=Level.REVISAR,
            reasons=[f"no se pudo verificar: {error}"],
            flags=[Flag(code="unverified", detail=error, veto=False)],
            pinned_ref=None,
            popularity=0,
        )

    if facts is None:
        unverified = _unverified_mcp(ops)
        written = _written_content(ops)
        return TrustCard(
            candidate=candidate.name,
            level=Level.REVISAR,
            reasons=[
                "fuente no soportada",
                *unverified,
                *(reason for _, reason in written),
            ],
            flags=[
                Flag(code="unsupported_source", detail=_UNSUPPORTED_SOURCE_DETAIL, veto=False),
                *(Flag(code="unverified_mcp", detail=detail, veto=False) for detail in unverified),
                *(Flag(code="unverified_content", detail=path, veto=False) for path, _ in written),
            ],
            pinned_ref=None,
            popularity=0,
        )

    flags = _collect_flags(facts, skill_flags, config, now)
    unverified_mcp = _unverified_mcp(ops)
    flags.extend(Flag(code="unverified_mcp", detail=detail, veto=False) for detail in unverified_mcp)
    written = _written_content(ops)
    flags.extend(Flag(code="unverified_content", detail=path, veto=False) for path, _ in written)
    vetoes = [flag for flag in flags if flag.veto]
    pinned_ref = _pinned_ref(candidate, facts)

    if vetoes:
        return TrustCard(
            candidate=candidate.name,
            level=Level.BLOQUEADO,
            reasons=[f"{flag.code}: {flag.detail}" for flag in vetoes],
            flags=flags,
            pinned_ref=pinned_ref,
            popularity=facts.stars,
        )

    official = facts.owner.lower() in config.official_owners
    inactive_days = (now - facts.pushed_at).days
    healthy = inactive_days <= config.max_inactive_days
    high_risk = _is_high_risk(ops, skill_flags)
    unresolved_fork = facts.is_fork and facts.ahead_by is None

    if official and healthy:
        level = Level.VERIFICADO
        reasons = [f"publicador oficial: {facts.owner}", "actividad reciente"]
    elif not official and healthy and not high_risk and not unresolved_fork:
        level = Level.CONFIABLE
        reasons = ["actividad reciente", "bajo riesgo"]
    else:
        level = Level.REVISAR
        reasons = []
        if not healthy:
            reasons.append(
                f"inactivo: {inactive_days} días desde el último push "
                f"(máximo {config.max_inactive_days})"
            )
        if high_risk:
            reasons.append(
                "riesgo alto: script incluido o conexión MCP por stdio "
                "o con variables de entorno"
            )
        if unresolved_fork:
            reasons.append("fork sin comparar contra su repo padre")

    # Lo que el plan instala y la fuente no respalda, y las herramientas que
    # una skill se pre-aprueba: nunca pasa de Revisar.
    capped = [
        *unverified_mcp,
        *(reason for _, reason in written),
        *(
            f"la skill pre-aprueba herramientas sin pedir permiso (allowed-tools): {flag.detail}"
            for flag in skill_flags
            if flag.code == "allowed_tools"
        ),
    ]
    if capped:
        if level != Level.REVISAR:
            level = Level.REVISAR
            reasons = []
        reasons.extend(capped)

    return TrustCard(
        candidate=candidate.name,
        level=level,
        reasons=reasons,
        flags=flags,
        pinned_ref=pinned_ref,
        popularity=facts.stars,
    )


def _collect_flags(
    facts: RepoFacts,
    skill_flags: list[Flag],
    config: Config,
    now: datetime,
) -> list[Flag]:
    flags: list[Flag] = []

    typosquat_flag = detect_typosquat(
        facts.owner, facts.name, config.known_names, config.typosquat_max_distance
    )
    if typosquat_flag is not None:
        flags.append(typosquat_flag)

    age_days = (now - facts.created_at).days
    if age_days < config.new_repo_days and facts.stars >= config.new_repo_star_veto:
        flags.append(
            Flag(
                code="inflated_stars",
                detail=(
                    f"repo de {age_days} días con {facts.stars} estrellas "
                    f"(umbral {config.new_repo_star_veto} para repos nuevos)"
                ),
                veto=True,
            )
        )

    if (
        facts.is_fork
        and facts.ahead_by is not None
        and facts.ahead_by < config.fork_min_ahead
    ):
        flags.append(
            Flag(
                code="near_identical_fork",
                detail=(
                    f"fork con solo {facts.ahead_by} commits propios "
                    f"(mínimo {config.fork_min_ahead})"
                ),
                veto=True,
            )
        )

    flags.extend(skill_flags)
    return flags


def _unverified_mcp(ops: list[Operation]) -> list[str]:
    """Qué instalaría cada `AddMcp` que el verificador no comprueba."""
    details: list[str] = []
    for op in ops:
        if not isinstance(op, AddMcp):
            continue
        if op.transport == "http":
            details.append(f"la URL del MCP no se verifica contra la fuente: {op.url}")
        else:
            command = " ".join(str(arg) for arg in op.command or [])
            details.append(f"el paquete del MCP no se verifica contra la fuente: {command}")
    return details


def _written_content(ops: list[Operation]) -> list[tuple[str, str]]:
    """`(ruta, razón)` de cada `WriteFile`: contenido que nadie verificó.

    Todo `WriteFile` cuenta, sea cual sea su ruta. La ruta solo decide el
    texto de la razón: bajo `.claude/skills/` (misma definición que las
    vistas, `in_skills_dir`, aplicada a la ruta tal como viene y también
    normalizada, porque el aplicador escribe en la ruta normalizada) se
    nombra ese directorio. Una ruta que no es texto cuenta también.
    """
    written: list[tuple[str, str]] = []
    for op in ops:
        if not isinstance(op, WriteFile):
            continue
        if not isinstance(op.path, str):
            path = repr(op.path)
            in_skills = False
        else:
            path = op.path
            in_skills = in_skills_dir(PurePosixPath(path).parts) or in_skills_dir(
                PurePosixPath(os.path.normpath(path)).parts
            )
        if in_skills:
            reason = f"el contenido escrito bajo .claude/skills/ no se verifica contra la fuente: {path}"
        else:
            reason = f"el contenido que escribe el plan no se verifica contra la fuente: {path}"
        written.append((path, reason))
    return written


def _is_high_risk(ops: list[Operation], skill_flags: list[Flag]) -> bool:
    if any(flag.code == "bundled_script" for flag in skill_flags):
        return True
    for op in ops:
        if isinstance(op, AddMcp) and (op.transport == "stdio" or op.env_names):
            return True
    return False


def _pinned_ref(candidate: Candidate, facts: RepoFacts) -> str:
    ref = candidate.ref
    if ref is not None and _SHA_RE.fullmatch(ref):
        return ref
    return facts.head_sha
