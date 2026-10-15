"""Tests de la lista cerrada de rutas del aplicador (apply/paths.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from pitcrew.apply.paths import PathError, resolve_target


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


@pytest.fixture
def proj(home: Path) -> Path:
    # Como en la vida real, el proyecto vive dentro del HOME.
    path = home / "proyecto"
    path.mkdir()
    return path


def _snapshot(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


# --- casos básicos ------------------------------------------------------


@pytest.mark.parametrize(
    "p",
    [
        "../x",
        "/etc/passwd",
        ".claude/settings.json",
        ".claude/settings.local.json",
        ".mcp.json",
        ".claude/skills/a/hooks/x.sh",
        "src/main.py",
    ],
)
def test_rejected_paths(p: str, home: Path, proj: Path) -> None:
    before = _snapshot(home)
    with pytest.raises(PathError):
        resolve_target(p, home, proj)
    assert _snapshot(home) == before


def test_symlink_rejected(home: Path, proj: Path, tmp_path: Path) -> None:
    outside = tmp_path / "fuera"
    outside.mkdir()
    (proj / ".claude").mkdir()
    (proj / ".claude" / "skills").symlink_to(outside)
    with pytest.raises(PathError):
        resolve_target(".claude/skills/a/SKILL.md", home, proj)
    assert list(outside.iterdir()) == []


# --- rutas permitidas ---------------------------------------------------


def test_project_claude_md_allowed(home: Path, proj: Path) -> None:
    assert resolve_target("CLAUDE.md", home, proj) == proj / "CLAUDE.md"


def test_home_claude_md_allowed_without_project(home: Path) -> None:
    assert resolve_target("~/.claude/CLAUDE.md", home, None) == home / ".claude" / "CLAUDE.md"


def test_project_skill_file_allowed(home: Path, proj: Path) -> None:
    expected = proj / ".claude" / "skills" / "a" / "SKILL.md"
    assert resolve_target(".claude/skills/a/SKILL.md", home, proj) == expected


def test_home_skill_file_allowed_without_project(home: Path) -> None:
    expected = home / ".claude" / "skills" / "a" / "refs" / "x.md"
    assert resolve_target("~/.claude/skills/a/refs/x.md", home, None) == expected


def test_absolute_path_inside_allowed_area(home: Path, proj: Path) -> None:
    assert resolve_target(str(proj / "CLAUDE.md"), home, proj) == proj / "CLAUDE.md"
    target = home / ".claude" / "skills" / "a" / "SKILL.md"
    assert resolve_target(str(target), home, None) == target


def test_inner_dotdot_is_normalized(home: Path, proj: Path) -> None:
    expected = proj / ".claude" / "skills" / "b" / "SKILL.md"
    assert resolve_target(".claude/skills/a/../b/SKILL.md", home, proj) == expected


def test_result_is_absolute_and_nothing_is_created(home: Path, proj: Path) -> None:
    target = resolve_target(".claude/skills/a/SKILL.md", home, proj)
    assert target.is_absolute()
    assert _snapshot(home) == ["proyecto"]


def test_existing_real_directories_are_fine(home: Path, proj: Path) -> None:
    (proj / ".claude" / "skills" / "a").mkdir(parents=True)
    (proj / ".claude" / "skills" / "a" / "SKILL.md").write_text("x", encoding="utf-8")
    expected = proj / ".claude" / "skills" / "a" / "SKILL.md"
    assert resolve_target(".claude/skills/a/SKILL.md", home, proj) == expected


# --- rechazos: forma de la ruta -----------------------------------------


@pytest.mark.parametrize(
    "p",
    [
        "",
        "CLAUDE.md\x00",
        ".claude/skills/a\x00/SKILL.md",
        "~root/.claude/CLAUDE.md",
        "~otro/.claude/skills/a/SKILL.md",
        "~",
        "~/",
        "~/CLAUDE.md",  # solo {home}/.claude/CLAUDE.md, no {home}/CLAUDE.md
        ".claude/CLAUDE.md",  # en el proyecto solo {project}/CLAUDE.md
        "docs/CLAUDE.md",
        "CLAUDE.md/x",
        "claude.md",
        ".claude/skills",  # el propio directorio no es un destino
        ".claude/skills/",
        ".claude/skills/.",
        ".claude/skills/a/..",
        ".claude/skills/a/",  # termina en /: no nombra un archivo
        ".claude/skills/../settings.json",
        ".claude/skills/../../../x",
        ".claude/skills/a/../../../CLAUDE.md/../../x",
        "~/.claude/skills/../../.bashrc",
        "~/.ssh/authorized_keys",
        "~/.claude/skills/../../../etc/passwd",
        "//etc/passwd",
        ".claude/Skills/a/SKILL.md",
        ".Claude/skills/a/SKILL.md",
    ],
)
def test_rejected_shapes(p: str, home: Path, proj: Path) -> None:
    before = _snapshot(home)
    with pytest.raises(PathError):
        resolve_target(p, home, proj)
    assert _snapshot(home) == before


@pytest.mark.parametrize("p", [None, 5, b"CLAUDE.md", Path("CLAUDE.md")])
def test_non_string_path_rejected(p: object, home: Path, proj: Path) -> None:
    with pytest.raises(PathError):
        resolve_target(p, home, proj)  # type: ignore[arg-type]


@pytest.mark.parametrize("p", ["CLAUDE.md", ".claude/skills/a/SKILL.md", "./CLAUDE.md"])
def test_relative_path_without_project_rejected(p: str, home: Path) -> None:
    with pytest.raises(PathError):
        resolve_target(p, home, None)


def test_absolute_path_escaping_with_dotdot_rejected(home: Path, proj: Path) -> None:
    with pytest.raises(PathError):
        resolve_target(str(proj / ".claude" / "skills" / ".." / ".." / "x"), home, proj)
    with pytest.raises(PathError):
        resolve_target(str(proj) + "/../../../etc/passwd", home, proj)


def test_absolute_path_to_other_project_rejected(home: Path, proj: Path) -> None:
    other = home / "otro"
    other.mkdir()
    with pytest.raises(PathError):
        resolve_target(str(other / "CLAUDE.md"), home, proj)
    with pytest.raises(PathError):
        resolve_target(str(other / ".claude" / "skills" / "a" / "SKILL.md"), home, proj)


def test_sibling_with_same_prefix_rejected(home: Path, proj: Path) -> None:
    # `{project}-malo` empieza igual que `{project}` pero no está dentro.
    with pytest.raises(PathError):
        resolve_target(str(proj) + "-malo/CLAUDE.md", home, proj)


# --- rechazos: nombres prohibidos ---------------------------------------


@pytest.mark.parametrize(
    "p",
    [
        ".claude/skills/a/settings.json",
        ".claude/skills/a/settings.local.json",
        ".claude/skills/a/.mcp.json",
        ".claude/skills/settings.json/SKILL.md",
        ".claude/skills/hooks/SKILL.md",
        ".claude/skills/a/hooks",
        ".claude/skills/a/b/hooks/c/x.sh",
        "~/.claude/skills/a/hooks/x.sh",
        "~/.claude/settings.json",
        "~/.claude/hooks/x.sh",
        # otra capitalización: en un sistema de archivos que no distingue
        # mayúsculas son el mismo archivo.
        ".claude/Settings.json",
        ".claude/SETTINGS.JSON",
        ".claude/skills/a/SETTINGS.JSON",
        ".claude/skills/a/Settings.Local.Json",
        ".claude/skills/a/.MCP.json",
        ".MCP.JSON",
        ".claude/skills/a/Hooks/x.sh",
        ".claude/skills/a/HOOKS/x.sh",
        ".claude/skills/HOOKS",
    ],
)
def test_forbidden_names_rejected(p: str, home: Path, proj: Path) -> None:
    before = _snapshot(home)
    with pytest.raises(PathError):
        resolve_target(p, home, proj)
    assert _snapshot(home) == before


def test_hooks_as_substring_is_allowed(home: Path, proj: Path) -> None:
    # Solo el segmento exacto `hooks` está prohibido.
    expected = proj / ".claude" / "skills" / "git-hooks-guide" / "SKILL.md"
    assert resolve_target(".claude/skills/git-hooks-guide/SKILL.md", home, proj) == expected


def test_forbidden_name_in_base_does_not_reject(tmp_path: Path) -> None:
    # Los nombres prohibidos se miran en la parte de la ruta que controla
    # el plan, no en dónde vive el proyecto.
    home = tmp_path / "home"
    project = home / "hooks" / "proyecto"
    project.mkdir(parents=True)
    assert resolve_target("CLAUDE.md", home, project) == project / "CLAUDE.md"


# --- rechazos: symlinks -------------------------------------------------


def test_symlink_in_intermediate_component_rejected(
    home: Path, proj: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "fuera"
    outside.mkdir()
    (proj / ".claude" / "skills").mkdir(parents=True)
    (proj / ".claude" / "skills" / "a").symlink_to(outside)
    with pytest.raises(PathError):
        resolve_target(".claude/skills/a/refs/SKILL.md", home, proj)
    assert list(outside.iterdir()) == []


def test_symlink_dot_claude_rejected(home: Path, proj: Path, tmp_path: Path) -> None:
    outside = tmp_path / "fuera"
    outside.mkdir()
    (proj / ".claude").symlink_to(outside)
    with pytest.raises(PathError):
        resolve_target(".claude/skills/a/SKILL.md", home, proj)


def test_symlink_base_project_rejected(home: Path, tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = home / "enlace"
    link.symlink_to(real)
    with pytest.raises(PathError):
        resolve_target("CLAUDE.md", home, link)
    with pytest.raises(PathError):
        resolve_target(".claude/skills/a/SKILL.md", home, link)
    assert list(real.iterdir()) == []


def test_symlink_base_home_rejected(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "home"
    link.symlink_to(real)
    with pytest.raises(PathError):
        resolve_target("~/.claude/CLAUDE.md", link, None)
    assert list(real.iterdir()) == []


def test_symlink_target_file_rejected(home: Path, proj: Path, tmp_path: Path) -> None:
    victim = tmp_path / "victima.txt"
    victim.write_text("intacto", encoding="utf-8")
    (proj / "CLAUDE.md").symlink_to(victim)
    with pytest.raises(PathError):
        resolve_target("CLAUDE.md", home, proj)
    assert victim.read_text(encoding="utf-8") == "intacto"


def test_dangling_symlink_target_rejected(home: Path, proj: Path, tmp_path: Path) -> None:
    (proj / "CLAUDE.md").symlink_to(tmp_path / "no-existe")
    with pytest.raises(PathError):
        resolve_target("CLAUDE.md", home, proj)
    assert not (tmp_path / "no-existe").exists()


def test_dotdot_through_symlink_uses_lexical_target(
    home: Path, proj: Path, tmp_path: Path
) -> None:
    # `a` es un symlink: el sistema resolvería `a/..` fuera del proyecto.
    # La ruta se normaliza léxicamente, así que el destino real es el que
    # se comprueba y `a` no participa.
    outside = tmp_path / "fuera" / "dentro"
    outside.mkdir(parents=True)
    (proj / ".claude" / "skills").mkdir(parents=True)
    (proj / ".claude" / "skills" / "a").symlink_to(outside)
    target = resolve_target(".claude/skills/a/../b/SKILL.md", home, proj)
    assert target == proj / ".claude" / "skills" / "b" / "SKILL.md"


def test_file_used_as_directory_rejected(home: Path, proj: Path) -> None:
    (proj / ".claude" / "skills").mkdir(parents=True)
    (proj / ".claude" / "skills" / "a").write_text("soy un archivo", encoding="utf-8")
    with pytest.raises(PathError):
        resolve_target(".claude/skills/a/SKILL.md", home, proj)


def test_error_messages_are_spanish_and_name_the_path(home: Path, proj: Path) -> None:
    with pytest.raises(PathError, match="fuera de las rutas permitidas"):
        resolve_target("src/main.py", home, proj)
    with pytest.raises(PathError, match="prohibido"):
        resolve_target(".claude/settings.json", home, proj)
    (proj / "CLAUDE.md").symlink_to(home)
    with pytest.raises(PathError, match="enlace simbólico"):
        resolve_target("CLAUDE.md", home, proj)


# --- caracteres de control en la ruta -----------------------------------


@pytest.mark.parametrize(
    "p",
    [
        ".claude/skills/a\n/SKILL.md",
        ".claude/skills/a/SKILL.md\n",
        "CLAUDE.md\n",
        "\nCLAUDE.md",
        ".claude/skills/a\r/SKILL.md",
        ".claude/skills/a/\tSKILL.md",
        ".claude/skills/a/\x1b[2JSKILL.md",
        ".claude/skills/a/\x7fSKILL.md",
        ".claude/skills/a/\x85SKILL.md",  # control C1
        ".claude/skills/a/\x01",
        "~/.claude/skills/a\n/SKILL.md",
    ],
)
def test_control_characters_rejected(p: str, home: Path, proj: Path) -> None:
    before = _snapshot(home)
    with pytest.raises(PathError, match="carácter de control"):
        resolve_target(p, home, proj)
    assert _snapshot(home) == before
    with pytest.raises(PathError):
        resolve_target(str(proj / ".claude" / "skills") + "/" + p.rsplit("/", 1)[-1] + "\n", home, proj)


def test_non_ascii_names_still_allowed(home: Path, proj: Path) -> None:
    expected = proj / ".claude" / "skills" / "guía" / "notas ñ.md"
    assert resolve_target(".claude/skills/guía/notas ñ.md", home, proj) == expected


def test_forbidden_component_is_public() -> None:
    from pitcrew.apply.paths import forbidden_component

    assert forbidden_component(("a", "HOOKS", "x.sh")) == "HOOKS"
    assert forbidden_component(("a", ".MCP.json")) == ".MCP.json"
    assert forbidden_component(("a", "Settings.Local.JSON")) == "Settings.Local.JSON"
    assert forbidden_component(("a", "git-hooks", "SKILL.md")) is None
    assert forbidden_component(()) is None


# --- con proyecto solo se admiten destinos del proyecto -------------------


@pytest.mark.parametrize(
    "p",
    [
        "~/.claude/CLAUDE.md",
        "~/.claude/skills/a/SKILL.md",
        "~/.claude/skills/a/refs/x.md",
        "{home}/.claude/CLAUDE.md",
        "{home}/.claude/skills/a/SKILL.md",
    ],
)
def test_home_target_with_project_rejected(p: str, home: Path, proj: Path) -> None:
    # El manifiesto de una corrida con proyecto vive en el proyecto y
    # `revert` solo deshace dentro de ese árbol.
    before = _snapshot(home)
    with pytest.raises(PathError, match="sin --project"):
        resolve_target(p.replace("{home}", str(home)), home, proj)
    assert _snapshot(home) == before


def test_home_targets_still_allowed_without_project(home: Path) -> None:
    assert resolve_target("~/.claude/CLAUDE.md", home, None) == home / ".claude" / "CLAUDE.md"
    target = home / ".claude" / "skills" / "a" / "SKILL.md"
    assert resolve_target("~/.claude/skills/a/SKILL.md", home, None) == target


def test_project_targets_inside_home_still_allowed(home: Path, proj: Path) -> None:
    # El proyecto vive dentro del HOME: eso no lo convierte en destino global.
    assert resolve_target("CLAUDE.md", home, proj) == proj / "CLAUDE.md"
    assert resolve_target(str(proj / "CLAUDE.md"), home, proj) == proj / "CLAUDE.md"
    expected = proj / ".claude" / "skills" / "a" / "SKILL.md"
    assert resolve_target(str(expected), home, proj) == expected
