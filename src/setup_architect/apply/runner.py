"""Corrida del aplicador: `sarch apply`, `sarch revert` y `sarch audit`.

`run_apply` es donde un plan armado por un LLM a partir de texto de
internet se convierte en algo instalado, así que no confía en él: lee sus
bytes una sola vez, vuelve a verificar cada herramienta, valida todo lo
que se puede validar sin efectos, muestra un resumen generado por el
propio aplicador y pide la aprobación por la TTY. Hasta ese sí no se
escribe ni se ejecuta nada, y ante cualquier duda o error la corrida se
aborta (fail closed).

Durante la ejecución cada paso se registra en el manifiesto antes
(`pending`) y después (`done` o `failed`), para que `sarch revert` pueda
deshacer la corrida aunque el proceso muera a la mitad.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import os
import re
import shlex
import stat
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TextIO

from setup_architect.apply import approval
from setup_architect.apply.claude_md import validate_claude_md
from setup_architect.apply.manifest import (
    Manifest,
    ManifestError,
    StepRecord,
    manifest_path,
    revert,
)
from setup_architect.apply.ops import (
    ApplyContext,
    OpError,
    Undo,
    check_write_file,
    execute,
    prepare,
)
from setup_architect.config import Config
from setup_architect.models import (
    AddMcp,
    CopySkill,
    Level,
    ManualStep,
    Operation,
    Plan,
    PlanItem,
    SetEnv,
    TrustCard,
    WriteFile,
)
from setup_architect.plan_io import PlanError, load_plan, plan_hash
from setup_architect.render.text import safe_text
from setup_architect.verify.github import (
    FetchError,
    VerificationError,
    fetch_repo_facts,
    parse_repo_url,
)
from setup_architect.verify.pipeline import Deps, verify_item

_SHA_RE = re.compile(r"[0-9a-fA-F]{40}")
_BLOCK_LEVELS = (Level.VERIFICADO, Level.CONFIABLE)
_REVERTIBLE = ("done", "pending")
_NO_UNDO = Undo("none", "", None, None)
_OP_KINDS: dict[type, str] = {
    WriteFile: "write_file",
    CopySkill: "copy_skill",
    AddMcp: "add_mcp",
    SetEnv: "set_env",
    ManualStep: "manual_step",
}


class _Abort(Exception):
    """La corrida no puede seguir; el texto (en español, ya saneado) va al usuario."""


class _Console:
    """Dónde se le habla al usuario: stderr hasta abrir la TTY; después, la TTY."""

    def __init__(self) -> None:
        self.tty: TextIO | None = None

    def say(self, text: str = "") -> None:
        if self.tty is not None:
            try:
                self.tty.write(text + "\n")
                self.tty.flush()
                return
            except (OSError, ValueError):
                self.tty = None  # la terminal se cerró: queda stderr
        print(text, file=sys.stderr)


class _Secrets:
    """Pide cada secreto por la TTY una sola vez por corrida."""

    def __init__(self, console: _Console) -> None:
        self._console = console
        self._values: dict[str, str] = {}

    def __call__(self, name: str) -> str:
        if name not in self._values:
            if self._console.tty is None:
                raise approval.NoTTYError("no hay terminal para pedir el secreto")
            self._values[name] = approval.ask_secret(self._console.tty, name)
        return self._values[name]


@dataclass
class _Run:
    """Estado de una corrida ya aprobada."""

    run_id: str
    plan: Plan
    cards: list[TrustCard]
    deps: Deps
    project: Path | None
    manifest: Manifest
    ctx: ApplyContext
    console: _Console
    tty: TextIO
    next_index: int = 0
    # [resumen, estado] de cada paso registrado, en orden.
    log: list[list[str]] = field(default_factory=list)
    # (nombre, motivo) de cada item que no se instaló, ya saneados.
    not_installed: list[tuple[str, str]] = field(default_factory=list)
    # Si esta corrida escribió un `CLAUDE.md`.
    wrote_claude_md: bool = False


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _env_file(home: Path) -> Path:
    return home / ".config" / "setup-architect" / "secrets.env"


def _revert_command(run_id: str, project: Path | None) -> str:
    command = f"sarch revert {run_id}"
    if project is not None:
        command += f" --project {shlex.quote(str(project))}"
    return safe_text(command)


def _kind(exc: BaseException) -> str:
    return type(exc).__name__


def _installable(card: TrustCard) -> bool:
    """Lo `Bloqueado` y lo que no se pudo verificar nunca se ejecuta ni se pregunta."""
    return card.level in (*_BLOCK_LEVELS, Level.REVISAR) and not approval.is_unverified(card)


def _same_directory(a: str | Path, b: str | Path) -> bool:
    return os.path.realpath(a) == os.path.realpath(b)


# --- sarch apply ------------------------------------------------------------


def run_apply(plan_path: Path, deps: Deps, config: Config, project: Path | None) -> int:
    """Verifica, pide aprobación por la TTY y ejecuta el plan. Devuelve 0 o 1.

    Antes de la aprobación no se escribe ni se ejecuta nada del setup, y
    cualquier error (también una excepción inesperada, de la que solo se
    nombra el tipo) aborta la corrida con código 1. `KeyboardInterrupt`
    nunca se captura: a mitad de un paso lo deja `pending` en el
    manifiesto, y `sarch revert` lo deshace.

    Termine bien o mal, lo último que se dice es qué items no se
    instalaron y por qué (`Bloqueado`, sin verificar o rechazados).
    """
    console = _Console()
    with contextlib.ExitStack() as stack:
        try:
            run = _prepare_run(plan_path, deps, config, project, console, stack)
        except _Abort as exc:
            console.say(str(exc))
            return 1
        except Exception as exc:
            console.say(
                f"error inesperado antes de ejecutar ({_kind(exc)}); no se ejecutó nada"
            )
            return 1
        try:
            try:
                code = _execute_run(run)
            except Exception as exc:
                # Algo falló fuera de un paso (p. ej. al anotar en el manifiesto).
                code = _crash(run, exc)
            _say_not_installed(run)
            return code
        except Exception as exc:
            # Ni siquiera se pudo informar. Lo ya hecho quedó registrado.
            print(
                f"error inesperado durante la ejecución ({_kind(exc)}); "
                f"para deshacer lo hecho: {_revert_command(run.run_id, run.project)}",
                file=sys.stderr,
            )
            return 1


def _prepare_run(
    plan_path: Path,
    deps: Deps,
    config: Config,
    project: Path | None,
    console: _Console,
    stack: contextlib.ExitStack,
) -> _Run:
    """Pasos 1 a 7: todo lo que ocurre antes de ejecutar. Sin efectos en el setup."""
    home = _absolute(deps.home)
    if project is not None:
        project = _check_project(project)
    # Antes de leer o crear nada del manifiesto.
    _check_manifest_dir(project if project is not None else home)

    # 1. Los bytes del plan se leen UNA vez; el archivo no se vuelve a abrir.
    try:
        plan_bytes = Path(plan_path).read_bytes()
    except (OSError, ValueError) as exc:
        raise _Abort(f"no se pudo leer el plan ({_kind(exc)})") from exc
    try:
        plan = load_plan(plan_bytes)
    except PlanError as exc:
        raise _Abort(f"plan inválido: {safe_text(str(exc))}") from exc
    digest = plan_hash(plan_bytes)

    # 2. Fichas frescas, emparejadas con los items por posición. Se anota
    # el SHA-256 de cada archivo que el verificador descarga y escanea.
    verified: dict[tuple[str, str, str], set[str]] = {}

    def fetch_and_record(owner: str, name: str, sha: str) -> bytes:
        archive = deps.fetch_archive(owner, name, sha)
        if isinstance(archive, bytes):
            verified.setdefault(_archive_key(owner, name, sha), set()).add(
                hashlib.sha256(archive).hexdigest()
            )
        return archive

    verify_deps = dataclasses.replace(deps, fetch_archive=fetch_and_record)
    cards = [verify_item(item, verify_deps, config) for item in plan.items]

    # 3. Límites del CLAUDE.md.
    _check_claude_md(plan, config, console)

    # 4. Pre-validación sin efectos.
    run_id = uuid.uuid4().hex
    manifest_file = _absolute(manifest_path(home, project))
    manifest = Manifest(manifest_file)
    try:
        manifest.records()
    except ManifestError as exc:
        raise _Abort(safe_text(str(exc))) from exc
    ctx = ApplyContext(
        home=home,
        project=project,
        run_cmd=deps.run_cmd,
        backups_dir=manifest_file.parent / "backups" / run_id,
        env_file=_env_file(home),
        archives={},
        secret=_Secrets(console),
        manual=[],
    )
    sources = _prevalidate(plan, cards, ctx)

    # 5. La aprobación solo se lee de una terminal de verdad.
    try:
        tty = stack.enter_context(approval.open_tty(deps.tty_path))
    except approval.NoTTYError as exc:
        raise _Abort("la aprobación requiere una terminal interactiva") from exc
    console.tty = tty

    # 6. Resumen generado aquí, y la pregunta.
    console.say(approval.build_summary(plan, plan_bytes, cards, home, project))
    if not approval.ask(tty, f"¿Aprobar este plan? (hash {digest[:12]})"):
        raise _Abort("plan no aprobado; no se ejecutó nada")

    # 7. Descarga de las skills que se van a copiar. Se vuelve a descargar
    # (no se reutiliza lo verificado) y solo vale si trae exactamente los
    # bytes que el verificador escaneó: los niveles se calcularon con esos.
    for sha, (owner, name) in sources.items():
        try:
            archive = deps.fetch_archive(owner, name, sha)
        except Exception as exc:
            raise _Abort(
                f"no se pudo descargar {owner}/{name}@{sha} ({_kind(exc)}); "
                "no se ejecutó nada"
            ) from exc
        if not isinstance(archive, bytes):
            raise _Abort(f"descarga inválida de {owner}/{name}@{sha}; no se ejecutó nada")
        # Un solo digest registrado e igual al de ahora. Si el verificador
        # no lo descargó, o vio dos contenidos distintos, no hay con qué
        # darlo por verificado.
        if verified.get(_archive_key(owner, name, sha)) != {hashlib.sha256(archive).hexdigest()}:
            raise _Abort(
                f"el archivo de {owner}/{name}@{sha[:12]} cambió desde la "
                "verificación; no se ejecutó nada"
            )
        ctx.archives[sha] = archive

    return _Run(run_id, plan, cards, deps, project, manifest, ctx, console, tty)


def _archive_key(owner: str, name: str, sha: str) -> tuple[str, str, str]:
    # GitHub no distingue mayúsculas en owner/name, y la URL de la fuente y
    # el `repo` de la operación pueden diferir en eso.
    return (owner.lower(), name.lower(), sha)


def _check_project(project: Path) -> Path:
    project = _absolute(project)
    shown = safe_text(str(project))
    try:
        mode = os.lstat(project).st_mode
    except OSError as exc:
        raise _Abort(f"el proyecto {shown} no existe o no se puede leer") from exc
    if stat.S_ISLNK(mode):
        raise _Abort(f"el proyecto {shown} es un enlace simbólico; indica su ruta real")
    if not stat.S_ISDIR(mode):
        raise _Abort(f"el proyecto {shown} no es un directorio")
    return project


def _check_manifest_dir(base: Path) -> None:
    """Ningún componente del directorio del manifiesto puede ser un enlace.

    Recorre con `lstat` lo que ya exista de `base` (incluida),
    `{base}/.claude`, `{base}/.claude/setup-architect` y su `backups/`: el
    manifiesto y los respaldos se escribirían a través del enlace, fuera
    del árbol que `revert` acepta deshacer. No crea nada.
    """
    current = base
    for part in ("", ".claude", "setup-architect", "backups"):
        if part:
            current = current / part
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            return  # lo que sigue tampoco existe
        except (OSError, ValueError) as exc:
            raise _Abort(
                f"no se pudo comprobar {safe_text(str(current))} ({_kind(exc)}); "
                "no se ejecutó nada"
            ) from exc
        if stat.S_ISLNK(mode):
            raise _Abort(
                f"el directorio del manifiesto pasa por un enlace simbólico "
                f"({safe_text(str(current))}); no se ejecutó nada"
            )


def _is_claude_md(path: str) -> bool:
    # Sobre la ruta normalizada: `CLAUDE.md/.` también es un CLAUDE.md. Sin
    # distinguir mayúsculas: hay sistemas de archivos que no lo hacen.
    return PurePosixPath(os.path.normpath(path)).name.lower() == "claude.md"


def _check_claude_md(plan: Plan, config: Config, console: _Console) -> None:
    """Valida cada `CLAUDE.md` del plan; aborta si alguno rompe sus límites.

    El texto externo contra el que se compara son las `description` de los
    items y las de todo lo que el plan declara como investigado
    (`researched`), entre o no en el plan. De lo que el plan no declara,
    el aplicador no sabe nada.
    """
    external = [item.candidate.description for item in plan.items]
    external += [source.description for source in plan.researched]
    rejected = False
    for item in plan.items:
        for op in item.operations:
            if not isinstance(op, WriteFile) or not _is_claude_md(op.path):
                continue
            for violation in validate_claude_md(op.content, external, config):
                rejected = True
                console.say(
                    f"CLAUDE.md rechazado ({safe_text(op.path)}): {safe_text(violation)}"
                )
    if rejected:
        raise _Abort("el CLAUDE.md del plan rompe sus límites; no se ejecutó nada")


def _prevalidate(
    plan: Plan, cards: list[TrustCard], ctx: ApplyContext
) -> dict[str, tuple[str, str]]:
    """Comprueba, sin efectos, lo que se va a ejecutar.

    Devuelve qué descargar para los `CopySkill`: `sha -> (owner, name)`.
    Lanza `_Abort` ante la primera operación que no se podría ejecutar.
    """
    if len(cards) != len(plan.items):
        raise _Abort("la verificación no dio una ficha por cada item del plan")
    sources: dict[str, tuple[str, str]] = {}
    for item, card in zip(plan.items, cards):
        for text in (item.candidate.name, item.candidate.source_url):
            try:
                text.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise _Abort(
                    f"el candidato {safe_text(item.candidate.name)} tiene texto que "
                    "no es UTF-8 válido"
                ) from exc
        if not _installable(card):
            continue  # nunca se ejecuta
        for op in item.operations:
            try:
                _prevalidate_op(op, ctx, sources)
            except OpError as exc:
                raise _Abort(
                    f"{safe_text(item.candidate.name)}: {approval.describe_op(op)}: "
                    f"{safe_text(str(exc))}; no se ejecutó nada"
                ) from exc
    return sources


def _prevalidate_op(
    op: Operation, ctx: ApplyContext, sources: dict[str, tuple[str, str]]
) -> None:
    if isinstance(op, WriteFile):
        check_write_file(op, ctx)  # ruta, frontmatter y vetos de contenido
    elif isinstance(op, CopySkill):
        parsed = parse_repo_url(f"https://github.com/{op.repo}")
        if parsed is None or f"{parsed[0]}/{parsed[1]}" != op.repo:
            raise OpError(f"repo inválido en copy_skill: {op.repo!r}")
        if _SHA_RE.fullmatch(op.sha) is None:
            raise OpError(f"copy_skill exige un commit SHA de 40 hex, no {op.sha!r}")
        # Los archivos descargados se guardan por SHA: un mismo SHA no
        # puede venir de dos repos.
        known = sources.setdefault(op.sha, parsed)
        if (known[0].lower(), known[1].lower()) != (parsed[0].lower(), parsed[1].lower()):
            raise OpError(f"el commit {op.sha} aparece en copy_skill de dos repos distintos")
    elif isinstance(op, AddMcp):
        if op.scope == "project":
            # `claude mcp add --scope project` escribe el `.mcp.json` del
            # directorio de trabajo.
            if ctx.project is None:
                raise OpError(f"el servidor MCP {op.name!r} pide scope project y no hay proyecto")
            if not _same_directory(os.getcwd(), ctx.project):
                raise OpError(
                    f"el servidor MCP {op.name!r} pide scope project: ejecuta sarch "
                    "desde el directorio del proyecto"
                )
        prepare(op, ctx)  # sin efectos para add_mcp: solo valida
    elif isinstance(op, (SetEnv, ManualStep)):
        prepare(op, ctx)  # sin efectos: solo valida (y lee env_file)
    else:
        raise OpError("operación no soportada")


# --- ejecución --------------------------------------------------------------


def _execute_run(run: _Run) -> int:
    """Paso 8: primero lo aprobado en bloque, después cada `Revisar`.

    Lo que nunca se instala (`Bloqueado` o sin verificar) se anota
    `skipped` antes de empezar, para que conste pase lo que pase después.
    """
    say = run.console.say
    say(f"Corrida {run.run_id}. Ejecutando lo aprobado.")
    pairs = list(zip(run.plan.items, run.cards))
    for item, card in pairs:
        if _installable(card):
            continue
        name = safe_text(item.candidate.name)
        # Antes de anotar: debe constar aunque el manifiesto falle.
        run.not_installed.append((name, _not_installed_reason(card)))
        _skip(run, item, card)
        if card.level == Level.BLOQUEADO:
            say(f"  {name}: Bloqueado, no se instaló")
        else:
            say(f"  {name}: no se pudo verificar; vuelve a intentarlo más tarde")
    installable = [pair for pair in pairs if _installable(pair[1])]
    queue = [pair for pair in installable if pair[1].level in _BLOCK_LEVELS]
    queue += [pair for pair in installable if pair[1].level == Level.REVISAR]

    for position, (item, card) in enumerate(queue):
        name = safe_text(item.candidate.name)
        if card.level == Level.REVISAR:
            say()
            say(f"{name} quedó en Revisar:")
            for reason in card.reasons:
                say(f"  - {safe_text(reason)}")
            question = f"¿Instalar {name} de todos modos?"
            if approval.is_unsupported_source(card):
                question = f"{approval.UNSUPPORTED_SOURCE_NOTICE} {question}"
            if not approval.ask(run.tty, question):
                run.not_installed.append((name, "rechazado al confirmar"))
                _skip(run, item, card)
                say(f"  {name}: omitido")
                continue
        for op_position, op in enumerate(item.operations):
            error = _run_step(run, item, card, op)
            if error is None:
                say(f"  hecho: {name}: {approval.describe_op(op)}")
                if isinstance(op, WriteFile) and _is_claude_md(op.path):
                    run.wrote_claude_md = True
                continue
            pending = [op, *item.operations[op_position + 1 :]]
            for later, _ in queue[position + 1 :]:
                pending += later.operations
            return _fail(run, f"{name}: {approval.describe_op(op)}: {error}", pending)

    if run.ctx.manual:
        say()
        say("Pasos manuales (hazlos tú):")
        for step in run.ctx.manual:
            say(f"  * {safe_text(step.title)}")
            for line in step.steps:
                say(f"      - {safe_text(line)}")
    say()
    say(
        f"Corrida {run.run_id} terminada. "
        f"Para revertirla: {_revert_command(run.run_id, run.project)}"
    )
    return 0


def _not_installed_reason(card: TrustCard) -> str:
    """Por qué no se instala un item que ni se ejecuta ni se pregunta."""
    if card.level == Level.BLOQUEADO:
        reasons = "; ".join(safe_text(reason) for reason in card.reasons)
        return f"Bloqueado: {reasons}" if reasons else "Bloqueado"
    details = [flag.detail for flag in card.flags if flag.code == "unverified"]
    detail = safe_text("; ".join(details))
    return f"no se pudo verificar: {detail}; vuelve a intentarlo más tarde"


def _say_not_installed(run: _Run) -> None:
    """Sección final: cada item que no se instaló, con su motivo.

    Si la corrida escribió un `CLAUDE.md`, avisa de que puede describir
    como instaladas esas piezas: se redactó con el plan completo.
    """
    if not run.not_installed:
        return
    say = run.console.say
    say()
    say("No se instaló:")
    for name, reason in run.not_installed:
        say(f"  - {name}: {reason}")
    if run.wrote_claude_md:
        names = ", ".join(name for name, _ in run.not_installed)
        say()
        say(
            "Aviso: El CLAUDE.md que se escribió puede mencionarlas como instaladas: "
            f"{names}. Se redactó con el plan completo; conviene quitarlas a mano."
        )


def _record(run: _Run, item: PlanItem, card: TrustCard, op: Operation, status: str, undo: Undo) -> StepRecord:
    return StepRecord(
        run_id=run.run_id,
        index=run.next_index,
        candidate=item.candidate.name,
        source_url=item.candidate.source_url,
        op_kind=_OP_KINDS.get(type(op), "desconocida"),
        summary=approval.describe_op(op),
        level=card.level,
        ref=card.pinned_ref,
        status=status,  # type: ignore[arg-type]
        undo=undo,
        at=run.deps.now().isoformat(),
    )


def _begin(run: _Run, record: StepRecord) -> None:
    run.manifest.begin(record)
    run.next_index += 1
    run.log.append([record.summary, record.status])


def _skip(run: _Run, item: PlanItem, card: TrustCard) -> None:
    for op in item.operations:
        _begin(run, _record(run, item, card, op, "skipped", _NO_UNDO))


def _refuse_existing_mcp(op: AddMcp, run: _Run) -> None:
    """Falla si ya hay un servidor con ese nombre: no es nuestro para quitarlo.

    Se decide solo por el código de salida de `claude mcp get`, sin leer su
    salida: 0 significa "existe un servidor con ese nombre" y nada más (el
    comando devuelve 0 aunque ese servidor no conecte); cualquier otro
    entero, "no existe". Si no hay un código entero (el comando no se pudo
    lanzar o `run_cmd` devolvió otra cosa) no se sabe, y el paso falla.
    """
    try:
        code = run.deps.run_cmd(["claude", "mcp", "get", op.name])
    except Exception as exc:
        raise OpError(
            f"no se pudo comprobar si ya existe el servidor MCP {op.name!r} ({_kind(exc)})"
        ) from None
    if type(code) is not int:
        raise OpError(f"no se pudo comprobar si ya existe el servidor MCP {op.name!r}")
    if code == 0:
        raise OpError(f"ya existe un servidor MCP con ese nombre: {op.name!r}")


def _problem(exc: Exception) -> str:
    """Texto para el usuario: el de nuestros errores; de los demás, solo el tipo."""
    if isinstance(exc, (OpError, ManifestError)):
        return safe_text(str(exc))
    return f"error inesperado ({_kind(exc)})"


def _run_step(run: _Run, item: PlanItem, card: TrustCard, op: Operation) -> str | None:
    """Ejecuta un paso. Devuelve `None` si quedó `done`, o por qué falló."""
    try:
        if isinstance(op, AddMcp):
            _refuse_existing_mcp(op, run)
        undo = prepare(op, run.ctx)
        record = _record(run, item, card, op, "pending", undo)
        _begin(run, record)
    except Exception as exc:
        return _problem(exc)  # el paso no quedó registrado

    try:
        execute(op, run.ctx)
    except Exception as exc:
        problem = _problem(exc)
        try:
            run.manifest.finish(run.run_id, record.index, "failed")
            run.log[-1][1] = "failed"
        except ManifestError:
            problem += "; además no se pudo registrar el fallo en el manifiesto"
        return problem

    try:
        run.manifest.finish(run.run_id, record.index, "done")
    except Exception as exc:
        return (
            f"se ejecutó, pero no se pudo registrar como hecho ({_problem(exc)}); "
            "queda pendiente y revertir lo deshace"
        )
    run.log[-1][1] = "done"
    return None


def _fail(run: _Run, problem: str, pending: list[Operation]) -> int:
    say = run.console.say
    say()
    say(f"Falló {problem}")
    _say_log(run)
    say("Pasos que no se ejecutaron:")
    for op in pending:
        say(f"  - {approval.describe_op(op)}")
    return _offer_revert(run)


def _crash(run: _Run, exc: Exception) -> int:
    """Un error fuera de un paso: informa y, si hay algo hecho, ofrece revertir."""
    say = run.console.say
    command = _revert_command(run.run_id, run.project)
    say()
    say(f"La corrida se interrumpió: {_problem(exc)}")
    _say_log(run)
    try:
        left = [
            record
            for record in run.manifest.records()
            if record.run_id == run.run_id and record.status in _REVERTIBLE
        ]
    except ManifestError as error:
        say(f"{safe_text(str(error))}. Cuando el manifiesto se pueda leer: {command}")
        return 1
    if not left:
        say("No quedó nada que revertir.")
        return 1
    return _offer_revert(run)


def _say_log(run: _Run) -> None:
    say = run.console.say
    say("La corrida se detuvo. Pasos registrados:")
    for summary, status in run.log:
        say(f"  - [{status}] {summary}")
    if not run.log:
        say("  (ninguno)")


def _offer_revert(run: _Run) -> int:
    say = run.console.say
    command = _revert_command(run.run_id, run.project)
    if not approval.ask(run.tty, "¿Revertir esta corrida?"):
        say(f"No se revirtió. Para hacerlo después: {command}")
        return 1
    try:
        messages = revert(run.manifest, run.run_id, run.deps.run_cmd, run.ctx.env_file)
    except Exception as exc:
        say(f"no se pudo revertir: {_problem(exc)}. Para reintentarlo: {command}")
        return 1
    for message in messages:
        say(f"  {safe_text(message)}")
    if not messages:
        say("  no había nada que revertir")
    return 1


# --- sarch revert -----------------------------------------------------------


def run_revert(run_id: str, deps: Deps, project: Path | None) -> int:
    """Deshace la corrida `run_id`. Devuelve 1 si algo quedó sin deshacer.

    `claude mcp remove --scope project` actúa sobre el `.mcp.json` del
    directorio de trabajo: si la corrida tiene un MCP de alcance proyecto
    por deshacer y no se está en el proyecto, no se toca nada.
    """
    home = _absolute(deps.home)
    project = _absolute(project) if project is not None else None
    manifest = Manifest(_absolute(manifest_path(home, project)))
    try:
        steps = [record for record in manifest.records() if record.run_id == run_id]
        if not steps:
            print(f"no hay pasos de la corrida {safe_text(run_id)} en el manifiesto")
            return 0
        project_mcp = any(
            step.status in _REVERTIBLE
            and step.undo is not None
            and step.undo.kind == "mcp_remove"
            and step.undo.scope == "project"
            for step in steps
        )
        if project_mcp and (project is None or not _same_directory(os.getcwd(), project)):
            print(
                "la corrida registró un servidor MCP de alcance proyecto: ejecuta "
                "sarch revert desde el directorio del proyecto (con --project); "
                "no se revirtió nada",
                file=sys.stderr,
            )
            return 1
        messages = revert(manifest, run_id, deps.run_cmd, _env_file(home))
        left = [
            record
            for record in manifest.records()
            if record.run_id == run_id and record.status in _REVERTIBLE
        ]
    except (ManifestError, ValueError, OSError) as exc:
        # ValueError: una ruta de proyecto con un byte nulo.
        print(safe_text(str(exc)), file=sys.stderr)
        return 1
    for message in messages:
        print(safe_text(message))
    if not messages:
        print("no había nada que revertir")
    return 1 if left else 0


# --- sarch audit ------------------------------------------------------------


def run_audit(deps: Deps, config: Config, project: Path | None) -> int:
    """Vuelve a consultar lo instalado (pasos `done`). De solo lectura."""
    home = _absolute(deps.home)
    project = _absolute(project) if project is not None else None
    try:
        records = Manifest(_absolute(manifest_path(home, project))).records()
    except (ManifestError, ValueError) as exc:
        # ValueError: una ruta de proyecto con un byte nulo.
        print(safe_text(str(exc)), file=sys.stderr)
        return 1

    # El último paso `done` de cada candidato trae la versión vigente.
    installed: dict[tuple[str, str], StepRecord] = {}
    for record in records:
        if record.status == "done":
            installed[(record.candidate, record.source_url)] = record
    if not installed:
        print("no hay nada instalado que auditar")
        return 0
    for record in installed.values():
        findings = "; ".join(_audit_one(record, deps, config))
        print(
            f"- {safe_text(record.candidate)} ({safe_text(record.source_url)}): {findings}"
        )
    return 0


def _audit_one(record: StepRecord, deps: Deps, config: Config) -> list[str]:
    parsed = parse_repo_url(record.source_url)
    if parsed is None:
        return ["la fuente no es de GitHub; no se puede auditar"]
    try:
        facts = fetch_repo_facts(parsed[0], parsed[1], deps.fetch)
    except VerificationError as exc:
        cause = exc.__cause__
        if isinstance(cause, FetchError) and cause.status == 404:
            return ["el repo ya no existe"]
        return ["no se pudo consultar"]
    except Exception as exc:
        return [f"no se pudo consultar ({_kind(exc)})"]

    findings: list[str] = []
    if record.ref is not None and facts.head_sha != record.ref:
        findings.append(
            f"hay una versión nueva (instalado {safe_text(record.ref[:12])}, "
            f"actual {safe_text(facts.head_sha[:12])})"
        )
    inactive_days = (deps.now() - facts.pushed_at).days
    if inactive_days > config.max_inactive_days:
        findings.append(
            f"inactivo: {inactive_days} días desde el último push "
            f"(máximo {config.max_inactive_days})"
        )
    return findings or ["al día"]
