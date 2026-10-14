"""Lista cerrada de rutas en las que el aplicador puede escribir.

`resolve_target` es la barrera que impide que un `write_file` del plan
(armado por un LLM a partir de texto de internet) toque algo que no sea
un `CLAUDE.md` o un archivo de skill. Trabaja sobre el texto de la ruta:
la normaliza léxicamente, la compara con la lista cerrada y solo después
mira el disco, con `lstat`, para rechazar cualquier componente que sea un
enlace simbólico. Nunca usa `resolve()`, que seguiría el enlace que se
quiere detectar. Ante cualquier duda, rechaza.
"""

from __future__ import annotations

import os
import stat
import unicodedata
from pathlib import Path, PurePosixPath

from setup_architect.forbidden import forbidden_component

_SKILLS = (".claude", "skills")


class PathError(Exception):
    """La ruta pedida no está en la lista cerrada o no es segura."""


def resolve_target(path: str, home: Path, project: Path | None) -> Path:
    """Devuelve la ruta absoluta de `path` si el aplicador puede escribirla.

    `~/` se resuelve contra `home` y una ruta relativa contra `project`.
    Con proyecto solo se aceptan `{project}/CLAUDE.md` y archivos bajo
    `{project}/.claude/skills/`; sin proyecto, `{home}/.claude/CLAUDE.md` y
    archivos bajo `{home}/.claude/skills/`. Una corrida con proyecto guarda
    su manifiesto en el proyecto y `revert` solo deshace dentro de ese
    árbol, así que un destino de HOME con proyecto se rechaza: el setup
    global se aplica en una corrida aparte, sin `--project`.
    Lanza `PathError` para todo lo demás: ruta vacía, con byte nulo o con
    cualquier otro carácter de control (saltos de línea incluidos),
    `~usuario/`, ruta relativa sin proyecto, `..` que escapa, los nombres
    `settings.json`, `settings.local.json` y `.mcp.json`, cualquier
    segmento `hooks` (sin distinguir mayúsculas) y cualquier componente
    existente, de la base al destino, que sea un enlace simbólico.

    No crea ni modifica nada en disco.
    """
    if not isinstance(path, str) or path == "":
        raise PathError("ruta vacía o inválida")
    if "\x00" in path:
        raise PathError("la ruta contiene un byte nulo")
    if any(unicodedata.category(ch) == "Cc" for ch in path):
        raise PathError(f"la ruta contiene un carácter de control: {path!r}")
    if path.endswith("/"):
        raise PathError(f"la ruta no nombra un archivo: {path!r}")

    home_abs = os.path.abspath(home)
    project_abs = os.path.abspath(project) if project is not None else None

    if path == "~" or path.startswith("~/"):
        joined = home_abs + "/" + path[2:]
    elif path.startswith("~"):
        raise PathError(f"no se admite el HOME de otro usuario: {path!r}")
    elif path.startswith("/"):
        joined = path
    elif project_abs is None:
        raise PathError(f"ruta relativa sin proyecto: {path!r}")
    else:
        joined = project_abs + "/" + path

    # Normalización léxica: tras ella no queda ningún `..`, así que un
    # `..` que escapaba deja la ruta fuera de toda base y se rechaza abajo.
    candidate = PurePosixPath(os.path.normpath(joined)).parts

    # Una sola base por corrida: el proyecto si lo hay; si no, el HOME.
    is_project = project_abs is not None
    base = project_abs if project_abs is not None else home_abs
    base_parts = PurePosixPath(base).parts
    home_parts = PurePosixPath(home_abs).parts

    forbidden: str | None = None
    in_home = False
    if candidate[: len(base_parts)] == base_parts:
        relative = candidate[len(base_parts) :]
        forbidden = forbidden_component(relative)
        if forbidden is None and _is_allowed(relative, is_project):
            _reject_symlinks(base, relative, path)
            return Path(base).joinpath(*relative)
    if is_project and candidate[: len(home_parts)] == home_parts:
        relative = candidate[len(home_parts) :]
        bad = forbidden_component(relative)
        if bad is not None:
            forbidden = forbidden or bad
        else:
            in_home = _is_allowed(relative, False)

    if forbidden is not None:
        raise PathError(f"nombre prohibido en la ruta ({forbidden!r}): {path!r}")
    if in_home:
        raise PathError(
            f"destino del HOME en una corrida con proyecto: {path!r}; el setup "
            "global se aplica en una corrida aparte, sin --project"
        )
    raise PathError(f"ruta fuera de las rutas permitidas: {path!r}")


def _is_allowed(relative: tuple[str, ...], is_project: bool) -> bool:
    claude_md = ("CLAUDE.md",) if is_project else (".claude", "CLAUDE.md")
    if relative == claude_md:
        return True
    # Al menos un componente por debajo de `skills`: el propio directorio
    # no es un destino válido.
    return relative[:2] == _SKILLS and len(relative) > 2


def _reject_symlinks(base: str, relative: tuple[str, ...], path: str) -> None:
    """Recorre de la base al destino; ningún componente puede ser symlink."""
    current = base
    for part in ("", *relative):
        if part:
            current = current + "/" + part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            # Lo que sigue tampoco existe: no hay nada más que comprobar.
            return
        except OSError as exc:
            raise PathError(
                f"no se pudo comprobar la ruta {path!r}: {exc.strerror}"
            ) from exc
        if stat.S_ISLNK(info.st_mode):
            raise PathError(
                f"la ruta pasa por un enlace simbólico ({current}): {path!r}"
            )
