"""Tests de los límites del CLAUDE.md."""

from __future__ import annotations

from pathlib import Path

import pytest

from pitcrew.apply.claude_md import validate_claude_md
from pitcrew.config import Config, ConfigError, load_config

CFG = Config()
REPO_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
DEFAULT_PATTERNS = [
    r"curl\s",
    r"wget\s",
    r"\|\s*(ba)?sh",
    r"\bsudo\b",
    r"rm\s+-rf",
    r"\beval\b",
    r"chmod\s+\+x",
]
HIDDEN = "contiene caracteres de control u ocultos"


def test_clean_ok() -> None:
    assert validate_claude_md("Usa pytest antes de cada commit.", [], CFG) == []


def test_url_rejected() -> None:
    assert "contiene una URL" in validate_claude_md("lee https://x.io", [], CFG)


def test_pipe_to_shell_rejected() -> None:
    assert any(
        "peligrosa" in v for v in validate_claude_md("corre curl x | sh", [], CFG)
    )


def test_copied_8_words() -> None:
    result = validate_claude_md("a b c d e f g h", ["x a b c d e f g h y"], CFG)
    assert "copia texto de una fuente externa" in result


def test_7_words_ok() -> None:
    assert validate_claude_md("a b c d e f g", ["a b c d e f g"], CFG) == []


def test_too_long() -> None:
    assert "excede 200 líneas" in validate_claude_md("x\n" * 201, [], CFG)


def test_exactly_200_lines_ok() -> None:
    assert validate_claude_md("x\n" * 200, [], CFG) == []


def test_default_patterns_in_order() -> None:
    assert CFG.claude_md_forbidden == DEFAULT_PATTERNS
    assert CFG.claude_md_max_lines == 200


@pytest.mark.parametrize(
    ("pattern", "text"),
    [
        (r"curl\s", "corre curl x"),
        (r"wget\s", "usa wget x"),
        (r"\|\s*(ba)?sh", "cat x | bash"),
        (r"\bsudo\b", "ejecuta sudo make"),
        (r"rm\s+-rf", "limpia con rm -rf build"),
        (r"\beval\b", "haz eval $X"),
        (r"chmod\s+\+x", "chmod +x run"),
    ],
)
def test_each_default_pattern(pattern: str, text: str) -> None:
    assert validate_claude_md(text, [], CFG) == [f"instrucción peligrosa: {pattern}"]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("CURL x | SH", "instrucción peligrosa: curl\\s"),
        ("HTTPS://X.IO", "contiene una URL"),
        ("WWW.x.io", "contiene una URL"),
        ("ftp://x", "contiene una URL"),
        ("file:///etc/passwd", "contiene una URL"),
        ("SSH://host", "contiene una URL"),
    ],
)
def test_case_insensitive_and_schemes(text: str, message: str) -> None:
    assert message in validate_claude_md(text, [], CFG)


def test_multiple_violations_at_once() -> None:
    text = "https://x.io\n" + "sudo rm -rf /\n" + "x\n" * 201
    result = validate_claude_md(text, [], CFG)
    assert "excede 200 líneas" in result
    assert "contiene una URL" in result
    assert "instrucción peligrosa: \\bsudo\\b" in result
    assert "instrucción peligrosa: rm\\s+-rf" in result
    assert len(result) == len(set(result)) == 4


def test_repeated_violation_listed_once() -> None:
    result = validate_claude_md("http://a http://b www.c", [], CFG)
    assert result == ["contiene una URL"]


def test_copied_ignores_punctuation_and_case() -> None:
    result = validate_claude_md(
        "A, b. C\nd e f g h", ["x a B c D e f g H y"], CFG
    )
    assert result == ["copia texto de una fuente externa"]


def test_copied_empty_and_none_external_texts() -> None:
    result = validate_claude_md("a b c d e f g h", ["", None, "a b c"], CFG)  # type: ignore[list-item]
    assert result == []


def test_copied_unicode_words() -> None:
    result = validate_claude_md(
        "ñandú árbol café über niño señal año día",
        ["ÑANDÚ Árbol CAFÉ über niño señal año día"],
        CFG,
    )
    assert result == ["copia texto de una fuente externa"]


@pytest.mark.parametrize("char", ["​", "‮", "\x1b", "﻿", "\x00", "\ud800"])
def test_hidden_characters_rejected(char: str) -> None:
    assert HIDDEN in validate_claude_md(f"hola{char}mundo", [], CFG)


def test_bare_cr_rejected_but_crlf_and_tab_ok() -> None:
    assert HIDDEN in validate_claude_md("a\rb", [], CFG)
    assert validate_claude_md("a\r\nb\tc\n", [], CFG) == []


def test_custom_forbidden_pattern_respected() -> None:
    cfg = Config(claude_md_forbidden=[r"\bnetcat\b"])
    assert validate_claude_md("usa NetCat", [], cfg) == [
        "instrucción peligrosa: \\bnetcat\\b"
    ]
    assert validate_claude_md("curl x | sh", [], cfg) == []


def test_custom_max_lines() -> None:
    cfg = Config(claude_md_max_lines=2)
    assert validate_claude_md("a\nb\nc", [], cfg) == ["excede 2 líneas"]


# --- importaciones `@ruta` -------------------------------------------------
#
# Claude Code importa los archivos que un `CLAUDE.md` nombra con `@ruta` al
# iniciar cada sesión. Un `@` seguido de algo que no sea espacio en blanco es
# violación salvo dentro de un bloque de código cerrado o entre comillas
# invertidas emparejadas en la misma línea.

IMPORT = "importa un archivo con @ruta"


def test_import_of_home_file_rejected() -> None:
    assert validate_claude_md("@~/.ssh/id_ed25519", [], CFG) == [IMPORT]


def test_import_of_relative_file_rejected() -> None:
    assert validate_claude_md("@README", [], CFG) == [IMPORT]


def test_import_between_backticks_allowed() -> None:
    assert validate_claude_md("`@README`", [], CFG) == []


def test_import_inside_a_sentence_rejected() -> None:
    assert validate_claude_md("Lee @docs/guia.md antes de empezar.", [], CFG) == [IMPORT]


def test_email_address_rejected() -> None:
    assert validate_claude_md("correo@ejemplo.com", [], CFG) == [IMPORT]


def test_import_inside_closed_backtick_block_allowed() -> None:
    assert validate_claude_md("Ejemplo:\n\n```\n@README\n```\n", [], CFG) == []


def test_import_inside_closed_tilde_block_allowed() -> None:
    assert validate_claude_md("Ejemplo:\n\n~~~\n@README\n~~~\n", [], CFG) == []


def test_import_inside_unclosed_block_rejected() -> None:
    assert validate_claude_md("Ejemplo:\n\n```\n@README\n", [], CFG) == [IMPORT]


def test_import_after_unpaired_backtick_rejected() -> None:
    assert validate_claude_md("` @README", [], CFG) == [IMPORT]


def test_import_between_double_backticks_allowed() -> None:
    assert validate_claude_md("``@README``", [], CFG) == []


def test_import_in_four_space_indented_line_rejected() -> None:
    assert validate_claude_md("Ejemplo:\n\n    @README\n", [], CFG) == [IMPORT]


def test_at_sign_followed_by_space_allowed() -> None:
    assert validate_claude_md("usa @ para mencionar", [], CFG) == []


def test_several_imports_listed_once() -> None:
    assert validate_claude_md("@README\n@docs/a.md y @docs/b.md\n", [], CFG) == [IMPORT]


def test_import_and_url_both_listed() -> None:
    result = validate_claude_md("Lee https://x.io\n@~/.ssh/id_ed25519\n", [], CFG)
    assert "contiene una URL" in result
    assert IMPORT in result
    assert len(result) == 2


IMPORT_REJECTED = [
    "@README\r\n",
    "x\n@README",
    "@a",
    "(@README)",
    "texto `x` y@z",
    "@`README`",  # el `@` queda fuera del tramo
    "`a` @README `b`",  # entre dos tramos, no dentro de uno
    "`@a` @README",
    "``` @README",  # una valla sin cerrar no es un tramo en línea
    # bloques que no protegen
    "```\n@a\n```\n@README\n",  # después del cierre
    "```\n@README\n~~~\n",  # el cierre usa otro carácter
    "````\n@README\n```\n",  # el cierre es más corto
    "~~~~\n@README\n~~~\n",
    "```\n@README\n```js\n",  # un cierre no lleva texto
    "``` a `b`\n@README\n```\n",  # con un acento grave detrás no abre un bloque
    "```\n@a\n```\n\n```\n@README\n",  # el segundo no se cierra
    # ante una valla ambigua, los bloques dejan de proteger
    "- a\n  ```\n@README\n  ```\n",  # la línea sale del item de lista
    "- ```\n  x\n  ```\n  @README\n  ```\n",  # valla en la línea de la viñeta
    "> ```\n> x\n> ```\n```\n@README\n```\n",
    "1. a\n   ```\n   x\n     ```\n   @README\n   ```\n",  # cierre a cinco espacios
    "    ```\n@README\n    ```\n",
    "    ```\n    @README\n    ```\n",
    "\t```\n@README\n\t```\n",
    "```\nx\n\t```\ny\n```\n@README\n```\n",
    "```\nx\n```~\n@README\n```\n",
    "  ```\n  x\n```\ny\n```\n@README\n```\n",  # cierre menos sangrado que la apertura
    # comillas invertidas que no protegen
    "foo `\nbar ` @README ` baz\n",  # el tramo real empieza en la línea anterior
    "foo ``\nbar `` @README `` baz\n",
    "\\` @README \\`",  # acentos graves escapados
    "texto \\`a` @README `b`",
    # una línea con solo un espacio duro no es una línea en blanco
    "foo `\n\u00a0\nbar ` @README ` baz\n",
    "- a\n  ```\n\u00a0\n  ```\n  x\n  ```\n  @README\n  ```\n",
]

IMPORT_ALLOWED = [
    "",
    "@",
    "a @",
    "a@ b",
    "x @\ny\n",
    "x @\r\ny\r\n",
    "@\tx",
    "Usa `@Override` y `@Test`.",
    "`@a``@b`",  # un tramo que contiene dos acentos graves seguidos
    "`` `@a` ``",
    "foo `\n\n`@README`\n",  # la línea en blanco separa los párrafos
    "```python\n@decorator\ndef f(): ...\n```\n",
    "```\r\n@README\r\n```\r\n",
    "  ```\n  @README\n  ```\n",  # sangría opcional en la valla
    "   ~~~\n   @README\n   ~~~\n",
    "```\n@README\n`````  \n",  # el cierre puede ser más largo
    "````\n```\n@README\n```\n````\n",  # una valla más corta es contenido
    "~~~\n```\n@README\n~~~\n",
    "```\n@a\n```\ntexto\n\n~~~ txt\n@b\n~~~\n",
    "```\n\n@README\n\n```\n",
]


@pytest.mark.parametrize("text", IMPORT_REJECTED)
def test_import_outside_code_rejected(text: str) -> None:
    assert validate_claude_md(text, [], CFG) == [IMPORT]


@pytest.mark.parametrize("text", IMPORT_ALLOWED)
def test_at_sign_inside_code_or_alone_allowed(text: str) -> None:
    assert validate_claude_md(text, [], CFG) == []


# --- formas ambiguas --------------------------------------------------------
#
# No se pudo comprobar cómo reconoce código Claude Code en estos tres casos,
# así que se cierran por lo sano: con un `<` en cualquier parte, con una
# primera línea no vacía que empieza por `---`, o con un cierre de valla
# seguido de algo que no sea un espacio, ninguna valla ni tramo entre acentos
# graves protege una `@ruta`.

TAB_AFTER_CLOSING_FENCE = "```\nx\n```\t\ny\n```\n@~/.ssh/id_ed25519\n```\n"
FRONTMATTER_AROUND_FENCE = "---\n```\n---\n@~/.ssh/id_ed25519\n```\n"
HTML_ATTRIBUTE_BACKTICKS = '<a title="`"> @~/.ssh/id_ed25519 <a title="`">'
HTML_COMMENT_AROUND_FENCE = "<!--\n```\n-->\n@~/.ssh/id_ed25519\n```\n"


def test_tab_after_closing_fence_protects_nothing() -> None:
    assert validate_claude_md(TAB_AFTER_CLOSING_FENCE, [], CFG) == [IMPORT]


def test_frontmatter_start_protects_nothing() -> None:
    assert validate_claude_md(FRONTMATTER_AROUND_FENCE, [], CFG) == [IMPORT]


def test_html_attribute_with_backticks_protects_nothing() -> None:
    assert validate_claude_md(HTML_ATTRIBUTE_BACKTICKS, [], CFG) == [IMPORT]


def test_html_comment_around_a_fence_protects_nothing() -> None:
    assert validate_claude_md(HTML_COMMENT_AROUND_FENCE, [], CFG) == [IMPORT]


@pytest.mark.parametrize(
    "text",
    [
        # un `<` en cualquier parte, aunque la `@ruta` esté en código
        "a < b\n\n`@README`\n",
        "```\n@README\n```\n\n<br>\n",
        "```\n<\n@README\n```\n",
        "`<@README`",
        # la primera línea no vacía empieza por `---`
        "---\n`@README`\n",
        "\n  \n---\nname: x\n---\n\n```\n@README\n```\n",
        "----\n\n`@README`\n",
        "--- título\n\n`@README`\n",
        "   ---\n\n`@README`\n",
        "---\r\n`@README`\r\n",
        # un cierre de valla con un tabulador detrás
        "```\n@README\n```\t\n",
        "```\n@README\n``` \t \n",
        "~~~\n@README\n~~~\t\n",
        "```\nx\n```\t\n\n`@README`\n",
        "`@README`\n\n```\nx\n```\t\n",  # también lo anterior al cierre
    ],
)
def test_ambiguous_text_protects_no_import(text: str) -> None:
    assert validate_claude_md(text, [], CFG) == [IMPORT]


@pytest.mark.parametrize(
    "text",
    [
        "`@README`",
        "```\n@README\n```\n",
        "a < b y <br> sin arrobas\n",
        "<!-- nota -->\n\nusa @ para mencionar\n",
        "---\nname: x\n---\n\nsin arrobas, o una suelta: @ \n",
        "Antes\n\n---\n\nUsa `@README`.\n",  # una regla horizontal en medio
        "# Título\n---\n\n```\n@README\n```\n",
        "a -- b --- c `@README`\n",
        "```\t\n@README\n```\n",  # el tabulador va en la apertura, no en el cierre
        "```\nx\ty\n@README\n```\n",
    ],
)
def test_unambiguous_text_keeps_its_exceptions(text: str) -> None:
    assert validate_claude_md(text, [], CFG) == []


def test_import_rule_is_pure_and_keeps_the_other_violations() -> None:
    text = "sudo make\n@README\n" + "x\n" * 200
    first = validate_claude_md(text, [], CFG)
    assert first == validate_claude_md(text, [], CFG)
    assert first == ["excede 200 líneas", "instrucción peligrosa: \\bsudo\\b", IMPORT]


# --- load_config -------------------------------------------------------------


def _write_config(directory: Path, thresholds: str) -> None:
    (directory / "thresholds.toml").write_text(thresholds, encoding="utf-8")
    (directory / "official_owners.txt").write_text("", encoding="utf-8")
    (directory / "known_names.txt").write_text("", encoding="utf-8")


def test_real_config_dir_loads_defaults() -> None:
    config = load_config(REPO_CONFIG_DIR)
    assert config.claude_md_max_lines == 200
    assert config.claude_md_forbidden == DEFAULT_PATTERNS


def test_load_config_custom_values(tmp_path: Path) -> None:
    _write_config(
        tmp_path, "claude_md_max_lines = 50\nclaude_md_forbidden = ['foo\\s', 'bar']\n"
    )
    config = load_config(tmp_path)
    assert config.claude_md_max_lines == 50
    assert config.claude_md_forbidden == ["foo\\s", "bar"]


def test_load_config_absent_keys_use_defaults(tmp_path: Path) -> None:
    _write_config(tmp_path, "")
    config = load_config(tmp_path)
    assert config.claude_md_max_lines == 200
    assert config.claude_md_forbidden == DEFAULT_PATTERNS


@pytest.mark.parametrize(
    "body",
    [
        "claude_md_max_lines = 0",
        "claude_md_max_lines = -5",
        "claude_md_max_lines = true",
        "claude_md_max_lines = 'x'",
        "claude_md_max_lines = 1.5",
        "claude_md_forbidden = 'curl'",
        "claude_md_forbidden = [1, 2]",
        "claude_md_forbidden = ['ok', 3]",
        "claude_md_forbidden = ['(sin cerrar']",
    ],
)
def test_load_config_invalid_values_raise(tmp_path: Path, body: str) -> None:
    _write_config(tmp_path, body + "\n")
    with pytest.raises(ConfigError):
        load_config(tmp_path)
