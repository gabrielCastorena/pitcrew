"""Escaneo de directorios de skill para detectar patrones peligrosos.

`scan_skill_dir` se usa en el verificador para avisar antes de aprobar
una skill, y se reutiliza en el aplicador justo antes de copiarla: es la
última barrera de seguridad antes de tocar disco, así que ante la duda
debe detectar (preferimos falsos positivos a falsos negativos).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from pitcrew.forbidden import forbidden_component
from pitcrew.models import Flag

# Tolerantes a espacios variables y opciones intermedias (p. ej. `sudo`,
# flags de curl/wget) entre el comando de descarga y el intérprete al que
# se tubería el resultado.
_DANGEROUS_PIPE = re.compile(
    r"\b(?:curl|wget)\b[^\n|]*\|[^\n]*?\b(?:sh|bash)\b",
    re.IGNORECASE,
)
_REMOTE_EVAL_CURL = re.compile(
    r"\beval\b\s*[\"']\$\(\s*curl\b",
    re.IGNORECASE,
)
_REMOTE_EVAL_BASE64 = re.compile(
    r"\bbase64\b[^\n]*?-d\b[^\n]*?\|[^\n]*?\b(?:sh|bash)\b",
    re.IGNORECASE,
)
_FRONTMATTER_FENCE = "---"
# Lo que el `\s` de JavaScript reconoce y puede seguir a la valla sin que
# deje de serlo: Claude Code lee el frontmatter con `/^---\s*\n.../`. Lista
# explícita y no `str.isspace()`, que no incluye U+FEFF. (Los saltos de
# línea no hacen falta: el texto ya llega partido en líneas.)
_JS_WHITESPACE = frozenset(
    " \t\x0b\x0c\u00a0\u1680\u2028\u2029\u202f\u205f\u3000\ufeff"
    + "".join(chr(code) for code in range(0x2000, 0x200B))
)
# Única forma reconocible de una línea de primer nivel del frontmatter que
# no sea vacía ni comentario: una clave simple, sin comillas.
_SIMPLE_KEY = re.compile(r"([A-Za-z][A-Za-z0-9_-]*)[ \t]*:")
# Claude Code ejecuta en el shell, ANTES de enviar la skill al modelo y sin
# pedir permiso, lo que un `.md` de la skill ponga como `` !`comando` `` (solo
# si el `!` va al inicio de una línea o justo después de espacio en blanco) o
# en un bloque cuya valla empieza por ```!. Ante la duda se detecta: no se
# intenta saber si está dentro de otro bloque de código, y el "espacio en
# blanco" es el de Python más U+FEFF (el `\s` de JavaScript). La valla se
# busca en CUALQUIER posición de la línea, pegada a cualquier carácter
# anterior (`texto```!`, `>```!`): no se sabe si el reconocedor de Claude
# Code exige que empiece la línea. Son tres o más acentos graves seguidos,
# con espacios o tabs opcionales, de un `!`.
_AFTER_SPACE = r"(?:^|(?<=[\s\ufeff]))"
_SHELL_INLINE = re.compile(_AFTER_SPACE + r"!`", re.MULTILINE)
_SHELL_FENCE = re.compile(r"`{3,}[ \t]*!")
# Claves del frontmatter que pre-aprueban herramientas para el turno que
# invoca la skill.
_ALLOWED_TOOLS_KEYS = frozenset({"allowed-tools", "allowed_tools"})
_VETO_HOOKS = "declara hooks en su frontmatter"
_VETO_UNPARSEABLE = "frontmatter no analizable"
_SCRIPT_SUFFIXES = {".sh", ".py", ".js"}


def scan_skill_dir(path: Path) -> list[Flag]:
    """Recorre `path` recursivamente y devuelve las banderas de sus archivos.

    No sigue symlinks (ni de directorio ni de archivo). Lee cada archivo
    como texto sin importar su extensión -- un patrón peligroso puede
    estar en un `SKILL.md` y no solo en un script -- con
    `errors="replace"`, así que un archivo no decodificable como UTF-8
    nunca hace fallar el escaneo. En los `.md` (extensión sin distinguir
    mayúsculas) busca además lo que Claude Code interpreta al invocar la
    skill: hooks en el frontmatter e inyección de shell (vetos), y
    `allowed-tools` (bandera `allowed_tools`, que no veta). Emite a lo más una bandera por código
    por archivo, con la ruta relativa a `path` en `detail`.
    """
    flags: list[Flag] = []
    for file_path in _iter_files(path):
        relative = file_path.relative_to(path)
        content = file_path.read_text(encoding="utf-8", errors="replace")
        flags.extend(_scan_file(file_path, relative, content))
    return flags


def _iter_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for current_dir, dir_names, file_names in os.walk(root, followlinks=False):
        current = Path(current_dir)
        # os.walk con followlinks=False no desciende en symlinks de
        # directorio, pero igual los listaría en dir_names; los quitamos
        # para que ni aparezcan como vacíos.
        dir_names[:] = [d for d in dir_names if not (current / d).is_symlink()]
        for file_name in sorted(file_names):
            file_path = current / file_name
            if file_path.is_symlink():
                continue
            files.append(file_path)
    return files


def content_vetoes(text: str, *, markdown: bool = False) -> list[str]:
    """Códigos de los vetos de contenido que `scan_skill_dir` daría a `text`.

    Son los que dependen solo del texto de un archivo (`dangerous_pipe`,
    `remote_eval`) y, si el archivo es un `.md` (`markdown=True`), también
    `skill_shell_exec`: una inyección de shell que Claude Code ejecutaría
    al invocar la skill. El aplicador los aplica también a lo que un
    `write_file` escribiría dentro de `.claude/skills/`.
    """
    codes: list[str] = []
    if _DANGEROUS_PIPE.search(text):
        codes.append("dangerous_pipe")
    if _REMOTE_EVAL_CURL.search(text) or _REMOTE_EVAL_BASE64.search(text):
        codes.append("remote_eval")
    if markdown and (_SHELL_INLINE.search(text) or _SHELL_FENCE.search(text)):
        codes.append("skill_shell_exec")
    return codes


def _scan_file(file_path: Path, relative: Path, content: str) -> list[Flag]:
    detail = str(relative)
    is_markdown = file_path.suffix.lower() == ".md"
    flags = [
        Flag(code=code, detail=detail, veto=True)
        for code in content_vetoes(content, markdown=is_markdown)
    ]

    # Mismos nombres que el aplicador se niega a escribir o instalar.
    if forbidden_component(relative.parts) is not None:
        flags.append(Flag(code="settings_or_hooks", detail=detail, veto=True))
    elif is_markdown:
        reason = frontmatter_veto(content)
        if reason is not None:
            flags.append(
                Flag(code="settings_or_hooks", detail=f"{reason}: {detail}", veto=True)
            )

    if is_markdown and frontmatter_allows_tools(content):
        # No veta: la rúbrica limita el nivel a `Revisar`.
        flags.append(Flag(code="allowed_tools", detail=detail, veto=False))

    if file_path.suffix in _SCRIPT_SUFFIXES or os.access(file_path, os.X_OK):
        flags.append(Flag(code="bundled_script", detail=detail, veto=False))

    return flags


def frontmatter_veto(text: str) -> str | None:
    """Por qué el frontmatter YAML de `text` es un veto, o `None` si no lo es.

    Claude Code admite `hooks:` en el frontmatter de un `SKILL.md`, y esos
    hooks ejecutan comandos de shell: una skill que los declare equivale a
    escribir hooks en `settings.json`. El frontmatter es el bloque entre la
    primera línea `---` y la siguiente línea `---`.

    No hay parser YAML (solo stdlib), así que la regla es fail closed: cada
    línea de primer nivel (sin sangría) del bloque debe ser vacía, un
    comentario (`#...`) o una clave simple sin comillas (`clave:`). Las
    líneas sangradas con espacios son continuación del valor de la clave
    anterior y no se inspeccionan. Devuelve:

    - "declara hooks en su frontmatter" si una clave simple es `hooks`, sin
      distinguir mayúsculas;
    - "frontmatter no analizable" ante cualquier otra línea de primer nivel
      (clave entre comillas, etiqueta, clave compleja, estilo flow, lista,
      ancla o alias, texto suelto), una línea sangrada sin clave previa (el
      mapa entero podría estar sangrado), una línea que empieza por tab, un
      bloque que nunca se cierra o una primera línea `---` seguida de algún
      carácter no imprimible que no sea espacio en blanco de JavaScript;
    - `None` si no hay frontmatter o todas sus líneas son reconocibles.
    """
    # La valla (de apertura o de cierre) es `---` seguido solo de espacio en
    # blanco de JavaScript, tras un BOM inicial opcional.
    lines = text.lstrip("\ufeff").splitlines()
    start = 0
    while start < len(lines) and not lines[start].strip():
        start += 1
    if start >= len(lines):
        return None
    opening = lines[start]
    if not _is_fence(opening):
        # Una primera línea corriente (`----`, `--- título`) no abre un
        # frontmatter, tampoco para Claude Code. Pero con un carácter
        # invisible o de formato pegado a la valla no se sabe qué leería.
        if opening.startswith(_FRONTMATTER_FENCE) and not opening.isprintable():
            return _VETO_UNPARSEABLE
        return None

    seen_key = False
    for line in lines[start + 1 :]:
        if _is_fence(line):
            return None
        if not line.strip():
            continue
        if line.startswith(" "):
            if not seen_key:
                return _VETO_UNPARSEABLE
            continue
        if line.startswith("#"):
            continue
        key = _SIMPLE_KEY.match(line)
        if key is None:
            return _VETO_UNPARSEABLE
        if key.group(1).lower() == "hooks":
            return _VETO_HOOKS
        seen_key = True
    return _VETO_UNPARSEABLE


def frontmatter_allows_tools(text: str) -> bool:
    """El frontmatter de `text` tiene una clave `allowed-tools` de primer nivel.

    Esa clave pre-aprueba herramientas para el turno que invoca la skill:
    Claude las usa sin pedir permiso. Se busca en las mismas líneas que
    mira `frontmatter_veto` (claves simples sin sangría, entre la valla de
    apertura y la de cierre, o hasta el final si el bloque no se cierra),
    sin distinguir mayúsculas y aceptando también `allowed_tools`. Una
    clave escrita de otra forma (entre comillas, en estilo flow) ya es un
    veto de `frontmatter_veto`.
    """
    lines = text.lstrip("\ufeff").splitlines()
    start = 0
    while start < len(lines) and not lines[start].strip():
        start += 1
    if start >= len(lines) or not _is_fence(lines[start]):
        return False
    for line in lines[start + 1 :]:
        if _is_fence(line):
            return False
        key = _SIMPLE_KEY.match(line)
        if key is not None and key.group(1).lower() in _ALLOWED_TOOLS_KEYS:
            return True
    return False


def _is_fence(line: str) -> bool:
    return line.startswith(_FRONTMATTER_FENCE) and all(
        ch in _JS_WHITESPACE for ch in line[len(_FRONTMATTER_FENCE) :]
    )
