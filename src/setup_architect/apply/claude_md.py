"""Límites del `CLAUDE.md`: validador puro que el aplicador ejecuta antes de
mostrar y de escribir el contenido.

Claude Code carga el `CLAUDE.md` en cada sesión futura, así que una
instrucción colada desde un README malicioso sería una inyección
persistente. Política: ante la duda, es violación (fail closed).
"""

from __future__ import annotations

import re
import unicodedata

from setup_architect.config import Config

_URL = re.compile(r"https?://|www\.|[a-z][a-z0-9+.\-]*://", re.IGNORECASE)
_WORD = re.compile(r"\w+")
_WINDOW = 8
_HIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Cs"})
_IMPORT_VIOLATION = "importa un archivo con @ruta"
# Una valla de bloque de código: hasta tres espacios de sangría, tres o más
# acentos graves o `~`, y el resto de la línea.
_FENCE = re.compile(r"( {0,3})(`{3,}|~{3,})(.*)")
# Algo que otro lector de Markdown podría tomar por una valla: la misma
# secuencia tras cualquier sangría o tras marcas de cita o de lista.
_FENCE_LIKE = re.compile(r"[ \t>*+\-0-9.)]*(?:`{3,}|~{3,})")
_BACKTICKS = re.compile(r"`+")


def validate_claude_md(
    text: str, external_texts: list[str], config: Config
) -> list[str]:
    """Devuelve las violaciones (en español) de `text`; vacía si es válido."""
    violations: list[str] = []

    if len(text.splitlines()) > config.claude_md_max_lines:
        violations.append(f"excede {config.claude_md_max_lines} líneas")
    if _URL.search(text):
        violations.append("contiene una URL")
    for pattern in config.claude_md_forbidden:
        if _matches(pattern, text):
            violations.append(f"instrucción peligrosa: {pattern}")
    if _copies_external(text, external_texts):
        violations.append("copia texto de una fuente externa")
    if _has_hidden_chars(text):
        violations.append("contiene caracteres de control u ocultos")
    if _imports_file(text):
        violations.append(_IMPORT_VIOLATION)
    return violations


def _matches(pattern: str, text: str) -> bool:
    try:
        return re.search(pattern, text, re.IGNORECASE) is not None
    except re.error:
        return True  # fail closed: un patrón que no compila no puede dar por bueno el texto


def _has_hidden_chars(text: str) -> bool:
    # `\r` solo es válido como parte de `\r\n`.
    for char in text.replace("\r\n", "\n"):
        if char in "\n\t":
            continue
        if unicodedata.category(char) in _HIDDEN_CATEGORIES:
            return True
    return False


def _imports_file(text: str) -> bool:
    """`text` tiene un `@` seguido de algo que no es espacio, fuera de código.

    Claude Code importa los archivos que un `CLAUDE.md` nombra con `@ruta` al
    iniciar cada sesión (los de `~/.claude/CLAUDE.md`, sin pedir aprobación),
    salvo dentro de bloques de código y de comillas invertidas. No importa
    qué precede al `@`: `correo@ejemplo.com` también cuenta. Un bloque
    sangrado con cuatro espacios no es "código" para esta regla.

    Hay tres formas en las que no se sabe si Claude Code vería código donde
    lo ve este validador; con cualquiera de ellas el texto entero es ambiguo
    y ni las vallas ni las comillas invertidas protegen nada:

    - un `<` en cualquier parte: una etiqueta o un comentario HTML pueden
      llevar acentos graves o vallas que para Markdown no lo son;
    - una primera línea no vacía que empieza por `---`: si se quita como
      frontmatter antes de analizar, las vallas de dentro desaparecen;
    - un cierre de valla seguido de algo que no sea un espacio (un
      tabulador): si no cierra, el bloque sigue hasta la valla siguiente.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    fenced = None if _ambiguous_markup(text, lines) else _fenced_lines(lines)
    if fenced is None:
        return any(_has_import(line, set()) for line in lines)
    spans_ok = True
    for index, line in enumerate(lines):
        if index in fenced or not line.strip(" \t"):
            spans_ok = True  # un tramo en línea no cruza de un párrafo a otro
            continue
        protected, spans_ok = _inline_code(line, spans_ok)
        if _has_import(line, protected):
            return True
    return False


def _has_import(line: str, protected: set[int]) -> bool:
    return any(
        char == "@" and position not in protected and line[position + 1] not in " \t"
        for position, char in enumerate(line[:-1])
    )


def _ambiguous_markup(text: str, lines: list[str]) -> bool:
    if "<" in text:
        return True
    first = next((line for line in lines if line.strip()), "")
    return first.lstrip().startswith("---")


def _fenced_lines(lines: list[str]) -> set[int] | None:
    """Índices de las líneas de los bloques de código cerrados, vallas incluidas.

    `None` si algún cierre lleva detrás algo que no sea un espacio: el texto
    es ambiguo y nada queda protegido (ver `_imports_file`).

    Un bloque va de una valla de apertura a su valla de cierre: el mismo
    carácter, al menos la misma longitud y nada más que espacios detrás. Un
    bloque que nunca se cierra no protege nada.

    Qué líneas son de un bloque depende de emparejar bien las vallas, y un
    desajuste con el lector de Claude Code dejaría un `@ruta` a la vista
    dentro de lo que aquí parece un bloque. Por eso, ante una valla ambigua
    se deja de proteger desde el bloque abierto (o desde esa línea) hasta el
    final del texto. Es ambigua:

    - la que va tras un tab, cuatro o más espacios o marcas de cita o de
      lista (`- ```` `, `> ```` `): dentro de una lista o una cita abriría o
      cerraría otro bloque;
    - dentro de un bloque, una línea con menos sangría que su apertura: si
      el bloque estaba en un item de lista, esa línea ya está fuera;
    - dentro de un bloque, un cierre seguido solo de más acentos graves o
      `~`: hay lectores que lo aceptan como cierre.
    """
    fenced: set[int] = set()
    opened: tuple[str, int, int, int] | None = None  # carácter, longitud, sangría, índice
    for index, line in enumerate(lines):
        if opened is not None and line.strip(" \t"):
            if len(line) - len(line.lstrip(" ")) < opened[2]:
                break
        if _FENCE_LIKE.match(line) is None:
            continue
        fence = _FENCE.match(line)
        if fence is None:
            break
        indent, run, rest = fence.groups()
        if opened is None:
            # Con un acento grave detrás no es una valla, es texto corriente.
            if run[0] != "`" or "`" not in rest:
                opened = (run[0], len(run), len(indent), index)
            continue
        char, length, _, start = opened
        if run[0] != char or len(run) < length:
            continue  # contenido del bloque
        if not rest.strip(" \t"):
            if rest.strip(" "):
                return None
            fenced.update(range(start, index + 1))
            opened = None
        elif not rest.strip("`~ \t"):
            break
    return fenced


def _inline_code(line: str, spans_ok: bool) -> tuple[set[int], bool]:
    """Posiciones de `line` dentro de un tramo entre comillas invertidas.

    Un tramo empieza en una secuencia de acentos graves y acaba en la
    siguiente de la misma longitud, en la misma línea. Devuelve también si
    las líneas siguientes del párrafo pueden seguir confiando en sus tramos:
    una secuencia sin pareja en su línea, o precedida de una barra invertida
    (un acento grave escapado no abre nada), podría emparejarse con otra de
    una línea posterior y descolocar todos los tramos que le siguen, así que
    desde ella no se protege nada hasta la siguiente línea en blanco.
    """
    protected: set[int] = set()
    if not spans_ok:
        return protected, False
    runs = [match.span() for match in _BACKTICKS.finditer(line)]
    current = 0
    while current < len(runs):
        start, end = runs[current]
        closing = next(
            (
                later
                for later in range(current + 1, len(runs))
                if runs[later][1] - runs[later][0] == end - start
            ),
            None,
        )
        if closing is None or line[max(start - 1, 0) : start] == "\\":
            return protected, False
        protected.update(range(start, runs[closing][1]))
        current = closing + 1
    return protected, True


def _grams(text: str) -> set[tuple[str, ...]]:
    words = _WORD.findall(text.lower())
    return {tuple(words[i : i + _WINDOW]) for i in range(len(words) - _WINDOW + 1)}


def _copies_external(text: str, external_texts: list[str]) -> bool:
    own = _grams(text)
    if not own:
        return False
    return any(external and not own.isdisjoint(_grams(external)) for external in external_texts)
