"""Manifiesto de lo instalado y reversión de una corrida.

El aplicador registra cada paso ANTES de ejecutarlo (`begin`, estado
`pending`) y después (`finish`, `done` o `failed`). Si el proceso muere a
mitad de un paso, este queda en `pending` y `revert` lo deshace igual que
a los `done`.

Formato en disco (JSON, UTF-8):

    {"version": 1,
     "records": [{"run_id": str, "index": int, "candidate": str,
                  "source_url": str, "op_kind": str, "summary": str,
                  "level": "Verificado" | "Confiable" | "Revisar" | "Bloqueado",
                  "ref": str | null,
                  "status": "pending" | "done" | "failed" | "skipped" | "reverted",
                  "undo": {"kind": ..., "target": str, "backup": str | null,
                           "scope": str | null} | null,
                  "at": str}]}

Cada registro lleva exactamente esos campos. No hay ninguno para valores
de secretos: del secreto solo queda el nombre de la variable.

Cada escritura es atómica (temporal en el mismo directorio, `fsync`,
`os.replace`, `fsync` del directorio): el archivo siempre es JSON válido.
Un manifiesto que no se puede leer nunca se repara ni se sobrescribe.

`revert` borra archivos y ejecuta `claude mcp remove` a partir de un
archivo que cualquiera pudo editar, así que NO confía en él: valida cada
`Undo` y, ante cualquier duda, no borra, no escribe ni ejecuta nada, deja
el paso con su estado y explica por qué. Solo borra o restaura dentro del
árbol que posee el manifiesto: el `{ancla}` de
`{ancla}/.claude/pitcrew/manifest.json`.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import secrets
import shutil
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Literal

from pitcrew.apply.ops import Undo
from pitcrew.forbidden import forbidden_component
from pitcrew.models import Level

_VERSION = 1
_STATUSES = ("pending", "done", "failed", "skipped", "reverted")
_REVERTIBLE = ("done", "pending")
_UNDO_KINDS = ("restore_file", "delete_path", "mcp_remove", "env_remove", "none")
_UNDO_FIELDS = ("kind", "target", "backup", "scope")
_SCOPES = ("user", "project")
_SKILLS = (".claude", "skills")
_CLAUDE_MDS = (("CLAUDE.md",), (".claude", "CLAUDE.md"))
_MANIFEST_TAIL = (".claude", "pitcrew", "manifest.json")
_MCP_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class ManifestError(Exception):
    """El manifiesto no se pudo leer, validar o escribir. Mensaje en español."""


@dataclass(frozen=True)
class StepRecord:
    """Un paso de una corrida del aplicador. Solo nombres, nunca valores."""

    run_id: str
    index: int
    candidate: str
    source_url: str
    op_kind: str
    summary: str
    level: Level
    ref: str | None
    status: Literal["pending", "done", "failed", "skipped", "reverted"]
    undo: Undo | None
    at: str


_RECORD_FIELDS = tuple(field.name for field in dataclasses.fields(StepRecord))


def manifest_path(home: Path, project: Path | None) -> Path:
    """Dónde vive el manifiesto: en el proyecto si lo hay; si no, en el HOME."""
    base = project if project is not None else home
    return base / ".claude" / "pitcrew" / "manifest.json"


class Manifest:
    """Registro en disco de los pasos de cada corrida."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def records(self) -> list[StepRecord]:
        """Todos los registros; `[]` si el archivo no existe.

        Lanza `ManifestError` si el archivo no es exactamente un manifiesto
        válido: nunca devuelve una lectura parcial.
        """
        try:
            fd = os.open(str(self.path), os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise self._error(f"no se pudo abrir ({_reason(exc)})") from exc
        try:
            with os.fdopen(fd, "rb") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise self._error("no es un archivo regular")
                raw = handle.read()
        except OSError as exc:
            raise self._error(f"no se pudo leer ({_reason(exc)})") from exc

        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError) as exc:
            raise self._error("no es JSON válido") from exc
        if not isinstance(data, dict) or set(data) != {"version", "records"}:
            raise self._error("no tiene la forma esperada")
        if type(data["version"]) is not int or data["version"] != _VERSION:
            raise self._error("es de una versión desconocida")
        if not isinstance(data["records"], list):
            raise self._error("no tiene la forma esperada")

        records: list[StepRecord] = []
        seen: set[tuple[str, int]] = set()
        for position, item in enumerate(data["records"]):
            try:
                record = _record_from_json(item)
            except ValueError as exc:
                raise self._error(f"registro {position} inválido: {exc}") from exc
            if (record.run_id, record.index) in seen:
                raise self._error(f"registro {position} inválido: paso repetido")
            seen.add((record.run_id, record.index))
            records.append(record)
        return records

    def begin(self, rec: StepRecord) -> None:
        """Agrega `rec` (lo normal: `pending`, antes de ejecutar el paso)."""
        try:
            record = _record_from_json(_record_to_json(rec))
        except ValueError as exc:
            raise ManifestError(f"registro inválido: {exc}") from exc
        records = self.records()
        if any(r.run_id == record.run_id and r.index == record.index for r in records):
            raise ManifestError(
                f"el paso {record.index} de la corrida {record.run_id!r} ya está en el manifiesto"
            )
        self._write([*records, record])

    def finish(self, run_id: str, index: int, status: str) -> None:
        """Cambia el estado de un paso ya registrado."""
        if not isinstance(status, str) or status not in _STATUSES:
            raise ManifestError(f"estado inválido: {status!r}")
        records = self.records()
        for position, record in enumerate(records):
            if record.run_id == run_id and record.index == index:
                records[position] = dataclasses.replace(record, status=status)  # type: ignore[arg-type]
                self._write(records)
                return
        raise ManifestError(f"el paso {index!r} de la corrida {run_id!r} no está en el manifiesto")

    def _write(self, records: list[StepRecord]) -> None:
        document = {"version": _VERSION, "records": [_record_to_json(r) for r in records]}
        try:
            data = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        except ValueError as exc:
            raise self._error("no se pudo codificar como UTF-8") from exc
        try:
            os.makedirs(self.path.parent, mode=0o700, exist_ok=True)
            _atomic_write(self.path, data, 0o600)
        except OSError as exc:
            raise ManifestError(
                f"no se pudo escribir el manifiesto {str(self.path)!r} ({_reason(exc)})"
            ) from exc

    def _error(self, detail: str) -> ManifestError:
        return ManifestError(f"el manifiesto {str(self.path)!r} {detail}; no se modificó")


# --- (de)serialización ----------------------------------------------------


def _record_to_json(rec: StepRecord) -> dict[str, object]:
    data: dict[str, object] = {name: getattr(rec, name) for name in _RECORD_FIELDS}
    if isinstance(rec.level, Level):
        data["level"] = rec.level.value
    if isinstance(rec.undo, Undo):
        data["undo"] = {name: getattr(rec.undo, name) for name in _UNDO_FIELDS}
    return data


def _is_text(value: object) -> bool:
    """Una cadena que se puede escribir como UTF-8 (sin sustitutos sueltos)."""
    if not isinstance(value, str):
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _record_from_json(item: object) -> StepRecord:
    """Valida un registro campo por campo. Lanza `ValueError` (en español)."""
    if not isinstance(item, dict) or set(item) != set(_RECORD_FIELDS):
        raise ValueError("faltan campos o hay campos desconocidos")
    for name in ("run_id", "candidate", "source_url", "op_kind", "summary", "at"):
        if not _is_text(item[name]):
            raise ValueError(f"{name} no es texto UTF-8")
    index = item["index"]
    if type(index) is not int or index < 0:
        raise ValueError("index no es un entero válido")
    if item["ref"] is not None and not _is_text(item["ref"]):
        raise ValueError("ref no es texto UTF-8")
    if not isinstance(item["status"], str) or item["status"] not in _STATUSES:
        raise ValueError("estado desconocido")
    if not isinstance(item["level"], str) or item["level"] not in [level.value for level in Level]:
        raise ValueError("nivel desconocido")
    return StepRecord(
        run_id=item["run_id"],
        index=index,
        candidate=item["candidate"],
        source_url=item["source_url"],
        op_kind=item["op_kind"],
        summary=item["summary"],
        level=Level(item["level"]),
        ref=item["ref"],
        status=item["status"],
        undo=_undo_from_json(item["undo"]),
        at=item["at"],
    )


def _undo_from_json(item: object) -> Undo | None:
    if item is None:
        return None
    if not isinstance(item, dict) or set(item) != set(_UNDO_FIELDS):
        raise ValueError("undo no tiene la forma esperada")
    if not isinstance(item["kind"], str) or item["kind"] not in _UNDO_KINDS:
        raise ValueError("undo de tipo desconocido")
    if not _is_text(item["target"]):
        raise ValueError("undo.target no es texto UTF-8")
    for name in ("backup", "scope"):
        if item[name] is not None and not _is_text(item[name]):
            raise ValueError(f"undo.{name} no es texto UTF-8")
    return Undo(item["kind"], item["target"], item["backup"], item["scope"])


# --- escritura atómica ----------------------------------------------------


def _reason(exc: OSError) -> str:
    return exc.strerror or type(exc).__name__


def _atomic_write(path: Path, data: bytes, mode: int | None) -> None:
    """Escribe `data` en un hermano temporal de `path` y lo renombra encima.

    `path` nunca queda a medias y, si fuera un symlink, se reemplaza el
    enlace en vez de escribir a través de él. Si algo falla, `path` queda
    como estaba y el temporal se borra. Lanza `OSError`.
    """
    temp = path.with_name(f".{path.name}.pitcrew-{secrets.token_hex(8)}.tmp")
    fd = os.open(
        str(temp),
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600 if mode is not None else 0o666,
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            if mode is not None:
                os.fchmod(handle.fileno(), mode)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
    dir_fd = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


# --- reversión ------------------------------------------------------------


class _Refused(Exception):
    """El paso no se deshizo; el texto (en español) va al usuario."""


def revert(
    m: Manifest,
    run_id: str,
    run_cmd: Callable[[list[str]], int],
    env_file: Path,
) -> list[str]:
    """Deshace los pasos `done` y `pending` de `run_id`, del último al primero.

    Cada `Undo` se valida antes de actuar. Si es válido se aplica de forma
    idempotente (lo que ya no existe se ignora) y el paso se marca
    `reverted` en el acto, para que una reversión interrumpida se pueda
    repetir. Si no lo es, o si falla (también con una excepción
    inesperada), no se marca y se sigue con los demás.

    `restore_file` y `delete_path` solo actúan dentro del árbol que posee
    el manifiesto: si `m.path` es `{ancla}/.claude/pitcrew/manifest.json`,
    sobre `{ancla}/CLAUDE.md`, `{ancla}/.claude/CLAUDE.md` y lo que haya
    bajo `{ancla}/.claude/skills/`, sin pasar por ningún enlace simbólico
    desde el ancla. Un manifiesto en otra ruta no deshace pasos de archivos.

    Riesgo residual aceptado: `mcp_remove` y `env_remove` no se pueden
    anclar a un árbol; solo se valida la forma del nombre, así que un
    manifiesto manipulado puede quitar otro servidor MCP u otra variable
    de `env_file`.

    Devuelve un mensaje en español por paso deshecho o negado (los pasos
    manuales, que no tienen nada que deshacer, no dan mensaje). Lanza
    `ManifestError` si el manifiesto no se puede leer o actualizar.
    """
    steps = [r for r in m.records() if r.run_id == run_id and r.status in _REVERTIBLE]
    steps.sort(key=lambda record: record.index, reverse=True)
    anchor = _anchor(m.path)

    messages: list[str] = []
    for step in steps:
        label = f"paso {step.index} ({step.candidate!r})"
        try:
            done = _apply_undo(step.undo, run_cmd, Path(env_file), anchor)
        except _Refused as exc:
            messages.append(f"{label}: no se revirtió: {exc}")
            continue
        except Exception as exc:
            # Un paso que falla de forma inesperada no bloquea a los demás.
            # Sin el texto de la excepción: no se sabe qué cita.
            messages.append(
                f"{label}: no se revirtió: error inesperado ({type(exc).__name__})"
            )
            continue
        if done is not None:
            messages.append(f"{label}: {done}")
        try:
            m.finish(step.run_id, step.index, "reverted")
        except ManifestError as exc:
            # Lo ya deshecho no se pierde con el error.
            raise ManifestError(
                f"{exc}. El paso {step.index} se deshizo pero no se pudo marcar "
                "como revertido. Hecho hasta el fallo: " + " | ".join(messages)
            ) from exc
    return messages


def _apply_undo(
    undo: Undo | None,
    run_cmd: Callable[[list[str]], int],
    env_file: Path,
    anchor: tuple[str, ...] | None,
) -> str | None:
    if undo is None:
        raise _Refused("el manifiesto no dice cómo deshacerlo")
    if undo.kind == "none":
        return None
    if undo.kind == "restore_file":
        return _undo_restore_file(undo, anchor)
    if undo.kind == "delete_path":
        return _undo_delete_path(undo, anchor)
    if undo.kind == "mcp_remove":
        return _undo_mcp_remove(undo, run_cmd)
    if undo.kind == "env_remove":
        return _undo_env_remove(undo, env_file)
    raise _Refused(f"tipo de reversión desconocido: {undo.kind!r}")


def _normalized_parts(text: object, what: str) -> tuple[str, ...]:
    """Componentes de una ruta absoluta ya normalizada; si no lo es, niega."""
    if not isinstance(text, str) or text == "":
        raise _Refused(f"{what} no es una ruta")
    # Cc: caracteres de control. Cs: sustitutos sueltos, que el sistema de
    # archivos no puede codificar.
    if any(unicodedata.category(ch) in ("Cc", "Cs") for ch in text):
        raise _Refused(f"{what} contiene un carácter no admitido: {text!r}")
    # normpath conserva un `//` inicial: se descarta aparte.
    if not text.startswith("/") or "//" in text or os.path.normpath(text) != text:
        raise _Refused(f"{what} no es una ruta absoluta normalizada: {text!r}")
    return PurePosixPath(text).parts


def _first_symlink(parts: tuple[str, ...], start: int) -> str | None:
    """El primer enlace simbólico entre `parts[:start + 1]` y la ruta entera."""
    for end in range(start + 1, len(parts) + 1):
        current = os.path.join(*parts[:end])
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise _Refused(f"no se pudo comprobar {current!r} ({_reason(exc)})") from exc
        if stat.S_ISLNK(info.st_mode):
            return current
    return None


def _anchor(path: Path) -> tuple[str, ...] | None:
    """Componentes del árbol que posee el manifiesto, o `None` si no hay.

    Solo vale un manifiesto en `{ancla}/.claude/pitcrew/manifest.json`,
    dado como ruta absoluta y normalizada.
    """
    try:
        parts = _normalized_parts(str(path), "el manifiesto")
    except _Refused:
        return None
    if len(parts) <= len(_MANIFEST_TAIL) or parts[-len(_MANIFEST_TAIL) :] != _MANIFEST_TAIL:
        return None
    return parts[: -len(_MANIFEST_TAIL)]


def _require_anchor(anchor: tuple[str, ...] | None) -> tuple[str, ...]:
    if anchor is None:
        raise _Refused(
            "el manifiesto no está en una ruta absoluta de la forma "
            "{base}/.claude/pitcrew/manifest.json; no se tocan archivos"
        )
    return anchor


def _check_target(target: object, anchor: tuple[str, ...] | None) -> int | None:
    """Valida el destino de `restore_file` o `delete_path`.

    Solo se admite `{ancla}/CLAUDE.md`, `{ancla}/.claude/CLAUDE.md` o algo
    por debajo de `{ancla}/.claude/skills/`, sin nombres prohibidos bajo el
    ancla y sin enlaces simbólicos desde el ancla (incluida) hasta el
    destino. Devuelve el número de componentes de `.claude/skills` si el
    destino está debajo, o `None` si es un `CLAUDE.md`.
    """
    anchor = _require_anchor(anchor)
    parts = _normalized_parts(target, "el destino")
    if parts[: len(anchor)] != anchor:
        raise _Refused(f"el destino está fuera del árbol del manifiesto: {target!r}")
    relative = parts[len(anchor) :]
    bad = forbidden_component(relative)
    if bad is not None:
        raise _Refused(f"el destino tiene un nombre prohibido ({bad!r}): {target!r}")

    if relative[:2] == _SKILLS and len(relative) > 2:
        skills_depth: int | None = len(anchor) + 2
    elif relative in _CLAUDE_MDS:
        skills_depth = None
    else:
        raise _Refused(
            "el destino no es el CLAUDE.md del árbol del manifiesto ni está "
            f"dentro de su .claude/skills: {target!r}"
        )

    link = _first_symlink(parts, len(anchor) - 1)
    if link is not None:
        raise _Refused(f"el destino pasa por un enlace simbólico ({link!r}): {target!r}")
    return skills_depth


def _lstat_mode(path: str) -> int | None:
    try:
        return os.lstat(path).st_mode
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _Refused(f"no se pudo comprobar {path!r} ({_reason(exc)})") from exc


def _undo_restore_file(undo: Undo, anchor: tuple[str, ...] | None) -> str:
    target = undo.target
    _check_target(target, anchor)

    backup_parts = _normalized_parts(undo.backup, "el respaldo")
    # {directorio del manifiesto}/backups
    base_parts = (*_require_anchor(anchor), *_MANIFEST_TAIL[:-1], "backups")
    if len(backup_parts) <= len(base_parts) or backup_parts[: len(base_parts)] != base_parts:
        raise _Refused(f"el respaldo no está en el directorio de respaldos: {undo.backup!r}")
    # Sin enlaces desde el directorio del manifiesto hasta el respaldo.
    link = _first_symlink(backup_parts, len(base_parts) - 2)
    if link is not None:
        raise _Refused(f"el respaldo pasa por un enlace simbólico ({link!r})")
    backup = os.path.join(*backup_parts)
    backup_mode = _lstat_mode(backup)
    if backup_mode is None:
        raise _Refused(f"falta el respaldo {backup!r}; no se puede restaurar {target!r}")
    if not stat.S_ISREG(backup_mode):
        raise _Refused(f"el respaldo {backup!r} no es un archivo regular")

    target_mode = _lstat_mode(target)
    if target_mode is not None and not stat.S_ISREG(target_mode):
        raise _Refused(f"el destino {target!r} existe y no es un archivo regular")

    try:
        fd = os.open(backup, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise _Refused(f"el respaldo {backup!r} no es un archivo regular")
            data = handle.read()
    except OSError as exc:
        raise _Refused(f"no se pudo leer el respaldo {backup!r} ({_reason(exc)})") from exc
    try:
        # El aplicador conserva el modo al pisar un archivo: el actual es el original.
        _atomic_write(Path(target), data, None if target_mode is None else stat.S_IMODE(target_mode))
    except OSError as exc:
        raise _Refused(f"no se pudo restaurar {target!r} ({_reason(exc)})") from exc
    return f"se restauró {target!r} desde su respaldo"


def _undo_delete_path(undo: Undo, anchor: tuple[str, ...] | None) -> str:
    target = undo.target
    skills_depth = _check_target(target, anchor)
    mode = _lstat_mode(target)
    try:
        if mode is None:
            message = f"{target!r} ya no existía; nada que borrar"
        elif stat.S_ISREG(mode):
            os.unlink(target)
            message = f"se borró {target!r}"
        elif stat.S_ISDIR(mode) and skills_depth is not None:
            shutil.rmtree(target)  # no sigue enlaces simbólicos
            message = f"se borró el directorio {target!r}"
        else:
            raise _Refused(f"{target!r} no es un archivo regular ni el directorio de una skill")
    except FileNotFoundError:
        message = f"{target!r} ya no existía; nada que borrar"
    except OSError as exc:
        raise _Refused(f"no se pudo borrar {target!r} ({_reason(exc)})") from exc

    if skills_depth is not None:
        _remove_empty_parents(PurePosixPath(target).parts, skills_depth)
    return message


def _remove_empty_parents(parts: tuple[str, ...], skills_depth: int) -> None:
    """Borra los directorios padre vacíos, sin llegar a `.claude/skills`."""
    for end in range(len(parts) - 1, skills_depth, -1):
        try:
            os.rmdir(os.path.join(*parts[:end]))  # falla si no está vacío
        except FileNotFoundError:
            continue
        except OSError:
            return


def _undo_mcp_remove(undo: Undo, run_cmd: Callable[[list[str]], int]) -> str:
    name, scope = undo.target, undo.scope
    # fullmatch y no match: `$` aceptaría un salto de línea final.
    if not isinstance(name, str) or _MCP_NAME_RE.fullmatch(name) is None:
        raise _Refused(f"nombre de servidor MCP inválido: {name!r}")
    if scope not in _SCOPES:
        raise _Refused(f"scope inválido para el servidor MCP {name!r}: {scope!r}")
    try:
        code = run_cmd(["claude", "mcp", "remove", "--scope", scope, name])
    except Exception as exc:
        # Sin el texto de la excepción: no se sabe qué cita.
        raise _Refused(
            f"no se pudo ejecutar `claude mcp remove` para el servidor {name!r} "
            f"({type(exc).__name__})"
        ) from None
    if type(code) is not int:
        raise _Refused(f"`claude mcp remove` no devolvió un código de salida para {name!r}")
    if code != 0:
        return (
            f"el servidor MCP {name!r} ya no estaba registrado "
            f"(`claude mcp remove` salió con código {code}); se ignora"
        )
    return f"se quitó el servidor MCP {name!r} (scope {scope})"


def _undo_env_remove(undo: Undo, env_file: Path) -> str:
    name = undo.target
    if not isinstance(name, str) or _ENV_NAME_RE.fullmatch(name) is None:
        raise _Refused(f"nombre de variable de entorno inválido: {name!r}")
    shown = str(env_file)
    try:
        fd = os.open(shown, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return f"{shown!r} ya no existe; la variable {name} no estaba definida"
    except OSError as exc:
        # ELOOP: el archivo es un enlace simbólico y no se sigue.
        raise _Refused(f"no se pudo abrir {shown!r} sin seguir enlaces ({_reason(exc)})") from exc
    try:
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise _Refused(f"{shown!r} no es un archivo regular")
            content = handle.read()
    except OSError as exc:
        raise _Refused(f"no se pudo leer {shown!r} ({_reason(exc)})") from exc

    # La misma forma de línea que escribe y busca `SetEnv`.
    definition = re.compile(rb"[ \t]*export[ \t]+" + re.escape(name.encode("ascii")) + b"=")
    lines = content.split(b"\n")
    kept = [line for line in lines if definition.match(line) is None]
    if len(kept) == len(lines):
        return f"la variable {name} ya no estaba definida en {shown!r}"
    try:
        _atomic_write(env_file, b"\n".join(kept), 0o600)
    except OSError as exc:
        raise _Refused(f"no se pudo reescribir {shown!r} ({_reason(exc)})") from exc
    return f"se quitó la variable {name} de {shown!r}"
