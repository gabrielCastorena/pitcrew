"""Aprobación por la TTY y resumen del plan que se aprueba.

El plan lo arma un LLM y el aplicador lo puede lanzar ese mismo LLM, así
que la aprobación no se lee de stdin: `open_tty` abre la terminal del
usuario (`/dev/tty`) y exige que lo sea de verdad. `ask` solo da por
bueno un sí explícito; EOF, un error o cualquier otra cosa es un no. Las
respuestas se leen del descriptor, sin búfer en el proceso.

`build_summary` genera lo que el usuario aprueba a partir del plan ya
cargado y de las fichas recién calculadas, nunca de lo que diga el plan
sobre sí mismo. Todo texto externo pasa por `safe_text`, para que no
pueda fabricar líneas ni secuencias de escape en esa pantalla.
"""

from __future__ import annotations

import difflib
import io
import os
import posixpath
import shlex
import stat
import termios
import unicodedata
from pathlib import Path
from typing import TextIO

from setup_architect.apply.paths import PathError, resolve_target
from setup_architect.models import (
    AddMcp,
    CopySkill,
    Level,
    ManualStep,
    Operation,
    Plan,
    SetEnv,
    TrustCard,
    WriteFile,
)
from setup_architect.plan_io import plan_hash
from setup_architect.render.text import safe_text

_YES = frozenset({"s", "si", "sí", "y", "yes"})
_LEVEL_NOTE = {
    Level.REVISAR: " - se confirmará aparte",
    Level.BLOQUEADO: " - no se instalará",
}
# Sangría fija de cada línea de contenido: ninguna puede empezar en la
# columna de una cabecera del resumen.
_CONTENT_PREFIX = "         | "
# Tope de una respuesta; el modo canónico del kernel ya limita la línea.
_MAX_ANSWER_BYTES = 4096


# Lo que se le dice al usuario de un item cuya fuente no es de GitHub.
UNSUPPORTED_SOURCE_NOTICE = (
    "Nada de este item se verificó: la fuente no es un repo de GitHub soportado."
)


# Lo que dice el resumen de un plan con `researched` vacío.
NO_RESEARCHED_NOTICE = (
    "    El plan no declara ninguna fuente investigada: el CLAUDE.md no se comparó "
    "contra ningún texto externo fuera de los items."
)


# Lo que se dice de cada `manual_step` en el resumen: no se instala nada.
MANUAL_PENDING_NOTE = "queda pendiente: el aplicador solo imprime la guía"


def is_unsupported_source(card: TrustCard) -> bool:
    """La ficha salió por la ruta "fuente no soportada": no se verificó nada.

    Se decide por la bandera que pone la rúbrica, nunca por el texto de
    una razón.
    """
    return any(flag.code == "unsupported_source" for flag in card.flags)


def is_unverified(card: TrustCard) -> bool:
    """La ficha dice que no se pudo verificar: no se instala ni se pregunta."""
    return any(flag.code == "unverified" for flag in card.flags)


class NoTTYError(Exception):
    """No hay una terminal interactiva de la que leer la aprobación."""


class SecretError(Exception):
    """No se obtuvo un valor para el secreto. Nunca cita el valor."""


def open_tty(path: str) -> TextIO:
    """Abre `path` para leer y escribir; lanza `NoTTYError` si no es una TTY.

    Nunca crea el archivo. Si no existe, no se puede abrir o no es una
    terminal, cierra lo abierto y lanza `NoTTYError`.
    """
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
    except (OSError, ValueError, TypeError) as exc:
        raise NoTTYError("no se pudo abrir la terminal") from exc
    if not os.isatty(fd):
        os.close(fd)
        raise NoTTYError("no es una terminal interactiva")
    # Una TTY no admite seek: se envuelve el descriptor sin búfer binario.
    return io.TextIOWrapper(
        io.FileIO(fd, "r+"), encoding="utf-8", errors="backslashreplace"
    )


def _read_answer(fd: int) -> str | None:
    """Lee una línea directamente del descriptor, byte a byte.

    Sin búfer propio: lo que llegue después del salto de línea se queda en
    la cola del kernel, donde la siguiente pregunta lo descarta. Devuelve
    `None` ante EOF antes del salto de línea o una línea demasiado larga.
    """
    data = bytearray()
    while len(data) <= _MAX_ANSWER_BYTES:
        byte = os.read(fd, 1)
        if not byte:
            return None
        if byte == b"\n":
            return data.decode("utf-8", errors="replace")
        data += byte
    return None


def _question(tty: TextIO, prompt: str, echo: bool) -> str | None:
    """Hace una pregunta por la TTY y devuelve la línea contestada, o `None`.

    Mientras dura la pregunta la terminal queda en modo canónico (una
    lectura entrega una sola línea), con o sin eco; `TCSAFLUSH` descarta la
    entrada pendiente, así que lo tecleado o inyectado antes de ver la
    pregunta no la contesta. Los atributos originales se restauran siempre.
    Lanza `OSError`, `ValueError` o `termios.error` si `tty` no es una
    terminal utilizable.
    """
    fd = tty.fileno()
    original = termios.tcgetattr(fd)
    asking = list(original)
    asking[0] |= termios.ICRNL  # Enter termina la línea aunque estuviera en crudo
    asking[3] |= termios.ICANON
    if echo:
        asking[3] |= termios.ECHO
    else:
        asking[3] &= ~termios.ECHO
    try:
        termios.tcsetattr(fd, termios.TCSAFLUSH, asking)
        tty.write(prompt)
        tty.flush()
        return _read_answer(fd)
    finally:
        termios.tcsetattr(fd, termios.TCSAFLUSH, original)


def ask(tty: TextIO, prompt: str) -> bool:
    """Pregunta por la TTY y lee UNA línea de ella.

    Solo `s`, `si`, `sí`, `y` o `yes` (sin importar mayúsculas ni los
    espacios alrededor), en una línea terminada con Enter, dan `True`. EOF,
    una línea demasiado larga, un error o cualquier otra respuesta dan
    `False`; también si `tty` no es una terminal.

    La respuesta se lee del descriptor, sin búfer, con la terminal en modo
    canónico y tras descartar la entrada pendiente: ni lo tecleado antes de
    ver la pregunta ni lo que sobre de la respuesta anterior la contestan.
    """
    try:
        line = _question(tty, f"{prompt} [s/N]: ", echo=True)
    except (OSError, ValueError, termios.error):
        return False
    if line is None:
        return False
    return unicodedata.normalize("NFC", line.strip().lower()) in _YES


def ask_secret(tty: TextIO, name: str) -> str:
    """Pide por la TTY el valor del secreto `name`, sin eco.

    Se lee igual que en `ask` (del descriptor, en modo canónico, tras
    descartar la entrada pendiente), con el eco apagado mientras dura la
    pregunta. Lanza `SecretError` -- nunca con el valor en el mensaje -- si
    `tty` no es una terminal, si la lectura falla, ante EOF, una línea
    demasiado larga o un valor vacío.
    """
    try:
        value = _question(tty, f"Valor para {safe_text(name)} (no se mostrará): ", echo=False)
        # El Enter del usuario no se mostró.
        tty.write("\n")
        tty.flush()
    except (OSError, ValueError, termios.error) as exc:
        raise SecretError(
            f"no se pudo leer el valor de la terminal ({type(exc).__name__})"
        ) from None
    if value is None:
        raise SecretError("la terminal se cerró o el valor es demasiado largo")
    if value == "":
        raise SecretError("valor vacío")
    return value


def describe_op(op: Operation) -> str:
    """Una línea que describe `op` solo con campos del plan, ya saneada.

    Es también lo que queda en el manifiesto: del contenido de un archivo
    y de los secretos no cita nada más que la ruta o el nombre.
    """
    if isinstance(op, WriteFile):
        text = f"write_file {op.path}"
    elif isinstance(op, CopySkill):
        text = f"copy_skill {op.repo}@{op.sha} ({op.subdir}) -> .claude/skills/{op.dest_name}"
    elif isinstance(op, AddMcp):
        target = op.url if op.transport == "http" else shlex.join(op.command or [])
        text = f"add_mcp {op.name} (scope {op.scope}, {op.transport}): {target}"
        if op.env_names and op.transport == "http":
            # `claude mcp add --transport http` no recibe variables: las
            # del plan no se piden ni llegan al servidor.
            text += (
                "; variables (informativas; no se piden ni se pasan): "
                f"{', '.join(op.env_names)}"
            )
        elif op.env_names:
            text += f"; variables: {', '.join(op.env_names)}"
    elif isinstance(op, SetEnv):
        text = f"set_env {op.name}"
    elif isinstance(op, ManualStep):
        text = f"manual_step {op.title}"
    else:
        text = "operación no soportada"
    return safe_text(text)


def build_summary(
    plan: Plan,
    plan_bytes: bytes,
    cards: list[TrustCard],
    home: Path,
    project: Path | None,
) -> str:
    """El texto que el usuario aprueba; empieza con `Hash del plan: {hash}`.

    Fichas e items se emparejan por posición (lanza `ValueError` si no hay
    una ficha por item). Por item muestra nombre, nivel de la ficha,
    razones y operaciones; marca `Revisar` con "se confirmará aparte" y lo
    que no se instalará (`Bloqueado` o sin verificar), y dice de forma
    explícita cuándo nada del item se verificó (fuente no soportada). Un
    `WriteFile` muestra su contenido completo si
    el archivo es nuevo, o un `difflib.unified_diff` contra el actual si ya
    existe; cada línea lleva una sangría fija y pasa por `safe_text`, que
    deja intacto un `CLAUDE.md` que ya pasó `validate_claude_md`. No
    modifica nada en disco.

    Termina con las fuentes investigadas que declara el plan
    (`plan.researched`): cuántas son y, de cada una, nombre, fuente y
    descripción, saneados. Si no declara ninguna, lo dice de forma
    explícita.

    Cada `manual_step` de un item instalable se marca como pendiente: el
    aplicador solo imprime su guía (la de un item `Bloqueado` o sin
    verificar no se imprime, así que no se marca ni se cuenta). Si hay alguno, un aviso con el recuento y los
    títulos va justo después del contenido de cada `CLAUDE.md` que escribe
    (o, si no escribe ninguno, antes de las fuentes investigadas), para
    cruzarlo a la vista con lo que ese `CLAUDE.md` dice que está instalado.
    El aplicador no comprueba qué dice el texto: eso lo juzga el usuario.
    """
    if len(cards) != len(plan.items):
        raise ValueError("se esperaba una ficha por cada item del plan")

    where = f"proyecto {project}" if project is not None else f"HOME {home}"
    lines = [
        f"Hash del plan: {plan_hash(plan_bytes)}",
        f"Entorno: {safe_text(plan.environment)}",
        f"Destino: {safe_text(where)}",
        f"Herramientas: {len(plan.items)}",
    ]
    pending = _pending_lines(plan, cards)
    pending_shown = False
    for number, (item, card) in enumerate(zip(plan.items, cards), start=1):
        cand = item.candidate
        lines.append("")
        note = _LEVEL_NOTE.get(card.level, "")
        if is_unverified(card) and card.level != Level.BLOQUEADO:
            note = " - no se instalará (no se pudo verificar)"
        lines.append(f"[{number}] {safe_text(cand.name)}  [{card.level.value}]{note}")
        lines.append(f"    Fuente: {safe_text(cand.source_url)}")
        lines.append(f"    Qué es: {safe_text(cand.description)}")
        if is_unsupported_source(card):
            lines.append(f"    {UNSUPPORTED_SOURCE_NOTICE}")
        if card.reasons:
            lines.append("    Razones:")
            lines += [f"      - {safe_text(reason)}" for reason in card.reasons]
        if not item.operations:
            lines.append("    Operaciones: ninguna")
            continue
        lines.append("    Operaciones:")
        for position, op in enumerate(item.operations, start=1):
            prints = isinstance(op, ManualStep) and _prints_guide(card)
            pending_note = f" — {MANUAL_PENDING_NOTE}" if prints else ""
            lines.append(f"      {position}. {describe_op(op)}{pending_note}")
            lines += _op_details(op, home, project)
            if pending and isinstance(op, WriteFile) and _is_claude_md(op.path):
                lines += pending
                pending_shown = True
    if pending and not pending_shown:
        lines.append("")
        lines += pending
    lines.append("")
    lines += _researched_lines(plan)
    return "\n".join(lines) + "\n"


def _is_claude_md(path: str) -> bool:
    # El mismo criterio que usa el aplicador para validar un `CLAUDE.md`.
    return posixpath.basename(posixpath.normpath(path)).lower() == "claude.md"


def _prints_guide(card: TrustCard) -> bool:
    """Si el aplicador llegará a imprimir la guía de los `manual_step` del item.

    El mismo criterio que `runner._installable`: lo `Bloqueado` y lo que no
    se pudo verificar nunca se ejecuta, así que su guía no se imprime.
    """
    return card.level != Level.BLOQUEADO and not is_unverified(card)


def _pending_lines(plan: Plan, cards: list[TrustCard]) -> list[str]:
    """El aviso de piezas pendientes de instalar a mano, o nada si no hay.

    Solo cuenta los `manual_step` cuya guía se imprimirá.
    """
    titles = [
        op.title
        for item, card in zip(plan.items, cards)
        if _prints_guide(card)
        for op in item.operations
        if isinstance(op, ManualStep)
    ]
    if not titles:
        return []
    return [
        f"    Piezas pendientes de instalar a mano (manual_step): {len(titles)}. "
        "El aplicador solo imprime su guía; no instala nada.",
        *(f"    - {safe_text(title)}" for title in titles),
        "    Hasta que las instales no existen: si el CLAUDE.md las describe como "
        "instaladas o activas, Claude confiará en algo que no está.",
    ]


def _researched_lines(plan: Plan) -> list[str]:
    """La sección de fuentes investigadas: lo que el plan declara, saneado.

    Son los textos externos, además de los de los items, contra los que se
    comparó el `CLAUDE.md`. El aplicador solo conoce los que el plan
    declara, así que el usuario los ve aquí antes de aprobar.
    """
    lines = [f"Fuentes investigadas que declara el plan: {len(plan.researched)}"]
    if not plan.researched:
        lines.append(NO_RESEARCHED_NOTICE)
        return lines
    lines.append(
        "    El CLAUDE.md del plan se comparó contra la descripción de cada una "
        "y la de cada item."
    )
    for number, source in enumerate(plan.researched, start=1):
        lines.append(f"  ({number}) {safe_text(source.name)}")
        lines.append(f"      Fuente: {safe_text(source.source_url)}")
        lines.append(f"      Descripción: {safe_text(source.description)}")
    return lines


def _op_details(op: Operation, home: Path, project: Path | None) -> list[str]:
    if isinstance(op, WriteFile):
        return _write_file_details(op, home, project)
    if isinstance(op, SetEnv):
        return ["         (el valor se pedirá por la terminal; no se muestra ni se registra)"]
    if isinstance(op, AddMcp) and op.transport == "stdio" and op.env_names:
        return ["         (el valor de cada variable se pedirá por la terminal)"]
    if isinstance(op, ManualStep):
        return [f"         - {safe_text(step)}" for step in op.steps]
    return []


def _content_lines(lines: list[str]) -> list[str]:
    return [_CONTENT_PREFIX + safe_text(line) for line in lines]


def _split(content: str) -> list[str]:
    lines = content.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _write_file_details(op: WriteFile, home: Path, project: Path | None) -> list[str]:
    note = "         "
    try:
        target = resolve_target(op.path, home, project)
    except PathError as exc:
        return [f"{note}(ruta rechazada: {safe_text(str(exc))}; la operación fallará)"]

    try:
        fd = os.open(str(target), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        new = _split(op.content)
        return [
            f"{note}(archivo nuevo en {safe_text(str(target))}, {len(new)} líneas)",
            *_content_lines(new),
        ]
    except OSError:
        return [f"{note}(el destino existe y no se pudo leer; la operación fallará)"]
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            return [f"{note}(el destino existe y no es un archivo regular; la operación fallará)"]
        current = handle.read().decode("utf-8", errors="replace")

    diff = list(
        difflib.unified_diff(
            _split(current), _split(op.content), "actual", "nuevo", lineterm=""
        )
    )
    shown = safe_text(str(target))
    if not diff and current == op.content:
        return [f"{note}({shown} ya existe, sin cambios)"]
    if not diff:
        return [f"{note}({shown} ya existe; solo cambia el salto de línea final)"]
    return [
        f"{note}({shown} ya existe; se respalda y se reemplaza; cambios:)",
        *_content_lines(diff),
    ]
