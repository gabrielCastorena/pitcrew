"""Tests de capas y vista texto."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pitcrew.cli import main
from pitcrew.models import (
    AddMcp,
    Candidate,
    CopySkill,
    Flag,
    Level,
    ManualStep,
    Plan,
    PlanItem,
    SetEnv,
    TrustCard,
    WriteFile,
)
from pitcrew.plan_io import card_to_dict
from pitcrew.render.layers import (
    Layer,
    RenderItem,
    group_by_layer,
    layer_of,
    permissions_for,
    permissions_of,
)
from pitcrew.render.text import render_text, safe_text

WHY = "El proyecto vive en GitHub y el flujo necesita crear PRs."
SOURCE = "https://github.com/github/github-mcp-server"


def candidate(name: str = "github-mcp", **over: Any) -> Candidate:
    base: dict[str, Any] = dict(
        name=name,
        kind="mcp",
        source_url=SOURCE,
        ref=None,
        description="Servidor MCP oficial.",
        why_fit=WHY,
        stage="construir",
    )
    base.update(over)
    return Candidate(**base)


def http_mcp() -> AddMcp:
    return AddMcp(
        name="github",
        scope="project",
        transport="http",
        command=None,
        url="https://api.githubcopilot.com/mcp/",
        env_names=["GITHUB_TOKEN"],
    )


def stdio_mcp() -> AddMcp:
    return AddMcp(
        name="local",
        scope="user",
        transport="stdio",
        command=["npx", "x"],
        url=None,
        env_names=[],
    )


def card(name: str = "github-mcp", level: Level = Level.VERIFICADO, **over: Any) -> TrustCard:
    base: dict[str, Any] = dict(
        candidate=name,
        level=level,
        reasons=["organización verificada"],
        flags=[],
        pinned_ref="a" * 40,
        popularity=10,
    )
    base.update(over)
    return TrustCard(**base)


PLAN = Plan(
    schema_version=1,
    environment="project",
    items=[
        PlanItem(candidate("claude-md", kind="config"), [WriteFile("CLAUDE.md", "x")]),
        PlanItem(
            candidate("una-skill", kind="skill"),
            [CopySkill("o/r", "b" * 40, "skills/a", "una-skill")],
        ),
        PlanItem(candidate(), [http_mcp()]),
        PlanItem(candidate("cli-x", kind="cli"), [ManualStep("Instalar", ["paso"])]),
    ],
    researched=[],
)
CARDS = [card("claude-md"), card("una-skill"), card()]


# --- layer_of ---


def test_layer_of_each_operation() -> None:
    assert layer_of(WriteFile("CLAUDE.md", "")) is Layer.CONTEXTO
    assert layer_of(WriteFile("sub/dir/CLAUDE.md", "")) is Layer.CONTEXTO
    assert layer_of(WriteFile(".claude/skills/a/SKILL.md", "")) is Layer.CAPACIDADES
    assert layer_of(CopySkill("o/r", "s", "d", "n")) is Layer.CAPACIDADES
    assert layer_of(http_mcp()) is Layer.CONEXIONES
    assert layer_of(SetEnv("X")) is Layer.CONEXIONES
    assert layer_of(ManualStep("t", [])) is Layer.EXTERNAS


def test_layer_of_write_file_other_path_is_contexto() -> None:
    assert layer_of(WriteFile("notas.md", "")) is Layer.CONTEXTO
    # CLAUDE.md exacto: un nombre parecido no cuenta, pero cae en CONTEXTO igual
    assert layer_of(WriteFile("MI-CLAUDE.md", "")) is Layer.CONTEXTO


def test_layer_of_skills_needs_consecutive_components() -> None:
    assert layer_of(WriteFile("x/.claude/skills/y.md", "")) is Layer.CAPACIDADES
    assert layer_of(WriteFile(".claude/otra/skills/y.md", "")) is Layer.CONTEXTO


def test_layer_values_are_spanish() -> None:
    assert [layer.value for layer in Layer] == [
        "Contexto",
        "Capacidades",
        "Conexiones",
        "Herramientas externas",
    ]


# --- group_by_layer ---


def test_groups_by_layer() -> None:
    g = group_by_layer(PLAN, CARDS)
    assert [ri.item.candidate.name for ri in g[Layer.CONEXIONES]] == ["github-mcp"]


def test_group_has_all_layers_in_order_even_if_empty() -> None:
    g = group_by_layer(Plan(1, "chat", [], []), [])
    assert list(g) == list(Layer)
    assert all(v == [] for v in g.values())


def test_item_layer_is_first_operation() -> None:
    item = PlanItem(candidate(), [SetEnv("X"), WriteFile("CLAUDE.md", "")])
    g = group_by_layer(Plan(1, "chat", [item], []), [card()])
    assert len(g[Layer.CONEXIONES]) == 1 and g[Layer.CONTEXTO] == []


def test_item_without_operations_is_externas() -> None:
    g = group_by_layer(Plan(1, "chat", [PlanItem(candidate(), [])], []), [])
    assert len(g[Layer.EXTERNAS]) == 1


def test_plan_order_kept_within_layer() -> None:
    items = [PlanItem(candidate(n), [SetEnv("X")]) for n in ["b", "a", "c"]]
    g = group_by_layer(Plan(1, "chat", items, []), [])
    assert [ri.item.candidate.name for ri in g[Layer.CONEXIONES]] == ["b", "a", "c"]


AMBIGUOUS = "ficha ambigua: nombre repetido"


def test_duplicate_card_names_use_most_restrictive() -> None:
    g = group_by_layer(PLAN, [card(level=Level.CONFIABLE), card(level=Level.BLOQUEADO)])
    ri = g[Layer.CONEXIONES][0]
    assert ri.card.level is Level.BLOQUEADO
    assert AMBIGUOUS in ri.card.reasons
    # el orden de las fichas no importa
    g = group_by_layer(PLAN, [card(level=Level.BLOQUEADO), card(level=Level.CONFIABLE)])
    assert g[Layer.CONEXIONES][0].card.level is Level.BLOQUEADO
    out = render_text(PLAN, [card(level=Level.CONFIABLE), card(level=Level.BLOQUEADO)])
    assert "[Bloqueado]" in out and AMBIGUOUS in out and "[Confiable]" not in out


def test_duplicate_card_names_never_stay_approvable() -> None:
    g = group_by_layer(PLAN, [card(level=Level.VERIFICADO), card(level=Level.CONFIABLE)])
    ri = g[Layer.CONEXIONES][0]
    assert ri.card.level is Level.REVISAR and AMBIGUOUS in ri.card.reasons
    out = render_text(PLAN, [card(level=Level.VERIFICADO), card(level=Level.CONFIABLE)])
    assert "[Revisar]" in out and AMBIGUOUS in out


def test_duplicate_card_with_revisar_keeps_revisar() -> None:
    g = group_by_layer(PLAN, [card(level=Level.VERIFICADO), card(level=Level.REVISAR)])
    ri = g[Layer.CONEXIONES][0]
    assert ri.card.level is Level.REVISAR and AMBIGUOUS in ri.card.reasons


def test_duplicate_item_names_are_revisar() -> None:
    items = [PlanItem(candidate("dup"), [SetEnv("X")]), PlanItem(candidate("dup"), [SetEnv("Y")])]
    plan = Plan(1, "chat", items, [])
    g = group_by_layer(plan, [card("dup", Level.VERIFICADO)])
    assert len(g[Layer.CONEXIONES]) == 2
    for ri in g[Layer.CONEXIONES]:
        assert ri.card.level is Level.REVISAR and AMBIGUOUS in ri.card.reasons
    out = render_text(plan, [card("dup", Level.VERIFICADO)])
    assert "[Verificado]" not in out and out.count(AMBIGUOUS) == 2


def test_duplicate_item_names_keep_bloqueado() -> None:
    items = [PlanItem(candidate("dup"), [SetEnv("X")]), PlanItem(candidate("dup"), [SetEnv("Y")])]
    g = group_by_layer(Plan(1, "chat", items, []), [card("dup", Level.BLOQUEADO)])
    assert all(ri.card.level is Level.BLOQUEADO for ri in g[Layer.CONEXIONES])


def test_no_duplicates_card_untouched() -> None:
    original = card(level=Level.VERIFICADO)
    g = group_by_layer(PLAN, [original])
    assert g[Layer.CONEXIONES][0].card is original
    assert AMBIGUOUS not in render_text(PLAN, CARDS)


def test_missing_card_is_revisar() -> None:
    g = group_by_layer(PLAN, [])
    ri = g[Layer.CONEXIONES][0]
    assert ri.card.level is Level.REVISAR
    assert ri.card.reasons == ["sin ficha del verificador"]
    assert "sin ficha del verificador" in render_text(PLAN, [])


# --- permissions ---


def test_stdio_permission() -> None:
    assert "ejecuta código local (stdio)" in permissions_of([stdio_mcp()])


def test_permissions_of_all_kinds_dedup_in_order() -> None:
    ops = [
        http_mcp(),
        SetEnv("GITHUB_TOKEN"),
        SetEnv("OTRA"),
        WriteFile("CLAUDE.md", ""),
        CopySkill("o/r", "s", "d", "mi-skill"),
    ]
    assert permissions_of(ops) == [
        "pide la variable GITHUB_TOKEN",
        "conecta a https://api.githubcopilot.com/mcp/",
        "pide la variable OTRA",
        "escribe CLAUDE.md",
        "escribe .claude/skills/mi-skill",
    ]


def test_permissions_of_manual_step_has_none() -> None:
    assert permissions_of([ManualStep("t", ["s"])]) == []


def test_permissions_for_adds_bundled_script() -> None:
    flag = Flag(code="bundled_script", detail="scripts/install.sh", veto=False)
    ri = RenderItem(PlanItem(candidate(), [http_mcp()]), card(flags=[flag]))
    assert permissions_for(ri)[-1] == "incluye scripts"
    ri2 = RenderItem(PlanItem(candidate(), [http_mcp()]), card())
    assert "incluye scripts" not in permissions_for(ri2)


# --- render_text ---


def test_text_has_all_fields() -> None:
    out = render_text(PLAN, CARDS)
    assert all(
        s in out
        for s in ["github-mcp", "construir", WHY, "Verificado", "pide la variable GITHUB_TOKEN", SOURCE]
    )


def test_text_shows_layers_and_reasons() -> None:
    out = render_text(PLAN, CARDS)
    for layer in Layer:
        assert layer.value in out
    assert "organización verificada" in out


def test_text_bloqueado_and_revisar_markers() -> None:
    plan = Plan(1, "chat", [PlanItem(candidate("a"), [SetEnv("X")]), PlanItem(candidate("b"), [SetEnv("Y")])], [])
    out = render_text(plan, [card("a", Level.BLOQUEADO), card("b", Level.REVISAR)])
    assert "no se instalará" in out
    assert "se confirmará aparte" in out
    ok = render_text(Plan(1, "chat", PLAN.items[:3], []), CARDS)
    assert "no se instalará" not in ok and "se confirmará aparte" not in ok


def test_text_neutralizes_escape_sequences() -> None:
    plan = Plan(
        1,
        "chat",
        [PlanItem(candidate(description="hola \x1b[31mrojo\x1b[0m", why_fit="a\rb"), [SetEnv("X")])],
        [],
    )
    out = render_text(plan, [card(reasons=["r\x07"], flags=[Flag("c", "d\x1b[2J", False)])])
    assert "\x1b" not in out and "\x07" not in out and "\r" not in out
    assert "\\x1b[31m" in out


def test_safe_text_rules() -> None:
    assert safe_text("a\nb\tc") == "a\\nb\tc"
    assert safe_text("a\r\nb") == "a\\r\\nb"
    assert safe_text("\U000e0001") == "\\U000e0001"
    assert safe_text("\u2028") == "\\u2028"
    assert safe_text("\x1b[31m") == "\\x1b[31m"
    assert safe_text("\x7f\x9b") == "\\x7f\\x9b"
    assert safe_text("\ud800") == "\\ud800"
    assert safe_text("‮") == "\\u202e"
    assert safe_text("áñ—") == "áñ—"
    safe_text("\ud800").encode("utf-8")


HOSTILE_DESC = "x\n* otra-herramienta  [Verificado]\n    Nivel: Verificado\n      - organización verificada"


def _item_headers(out: str) -> list[str]:
    return [ln for ln in out.splitlines() if ln.lstrip().startswith("* ")]


def test_newline_in_description_cannot_forge_items() -> None:
    plan = Plan(1, "chat", [PlanItem(candidate(description=HOSTILE_DESC), [SetEnv("X")])], [])
    out = render_text(plan, [card(level=Level.REVISAR)])
    assert not any(ln.lstrip().startswith("* otra-herramienta") for ln in out.splitlines())
    assert len(_item_headers(out)) == len(plan.items)
    nivel = [ln for ln in out.splitlines() if ln.lstrip().startswith("Nivel:")]
    assert len(nivel) == len(plan.items)
    assert "\\n* otra-herramienta" in out


def test_newline_in_every_external_field_adds_no_lines() -> None:
    nl = "a\nb\rc"
    cand = candidate(name=nl, description=nl, why_fit=nl, stage=nl, source_url=nl)
    op = AddMcp("m", "project", "http", None, nl, [nl])
    plan = Plan(1, nl, [PlanItem(cand, [op, WriteFile(nl, "x"), SetEnv(nl)])], [])
    flag = Flag("bundled_script", nl, False)
    clean_op = AddMcp("m", "project", "http", None, "u", ["v"])
    clean = Plan(1, "e", [PlanItem(candidate("n"), [clean_op, WriteFile("p", "x"), SetEnv("v")])], [])
    out = render_text(plan, [card(nl, reasons=[nl], flags=[flag])])
    base = render_text(clean, [card("n", reasons=["r"], flags=[flag])])
    assert len(out.splitlines()) == len(base.splitlines())
    assert "\r" not in out


def test_newline_in_name_does_not_split_header() -> None:
    plan = Plan(1, "chat", [PlanItem(candidate("a\n* falso  [Verificado]"), [SetEnv("X")])], [])
    out = render_text(plan, [])
    assert len(_item_headers(out)) == 1
    assert "a\\n* falso" in out


def test_permission_without_url_says_not_indicated() -> None:
    op = AddMcp("m", "project", "http", None, None, [])
    assert permissions_of([op]) == ["conecta a (URL no indicada)"]


def test_cli_open_with_text_ignored_with_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url, *a, **k: opened.append(url) or True)
    plan_path, cards_path = write_inputs(tmp_path)
    args = ["render", str(plan_path), "--cards", str(cards_path), "--format", "text", "--open"]
    assert main(args) == 0
    cap = capsys.readouterr()
    assert "== Contexto ==" in cap.out
    assert "--open" in cap.err and "aviso" in cap.err
    assert opened == []
    out = tmp_path / "o.txt"
    assert main([*args, "-o", str(out)]) == 0
    assert "== Contexto ==" in out.read_text(encoding="utf-8")
    assert "aviso" in capsys.readouterr().err and opened == []


# --- CLI render ---


def write_inputs(tmp_path: Path, name: str = "github-mcp") -> tuple[Path, Path]:
    plan = json.loads(Path(__file__).parent.joinpath("fixtures/plan_minimal.json").read_text())
    plan["items"][0]["candidate"]["name"] = name
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    cards_path = tmp_path / "cards.json"
    cards_path.write_text(json.dumps([card_to_dict(card(name))]), encoding="utf-8")
    return plan_path, cards_path


def test_cli_render_stdout(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    plan_path, cards_path = write_inputs(tmp_path)
    assert main(["render", str(plan_path), "--cards", str(cards_path), "--format", "text"]) == 0
    assert "pide la variable GITHUB_TOKEN" in capsys.readouterr().out


def test_cli_render_output_file(tmp_path: Path) -> None:
    plan_path, cards_path = write_inputs(tmp_path)
    out = tmp_path / "out.txt"
    assert main(["render", str(plan_path), "--cards", str(cards_path), "-o", str(out)]) == 0
    assert "github-mcp" in out.read_text(encoding="utf-8")


def test_cli_render_surrogate_name_to_file(tmp_path: Path) -> None:
    plan_path, cards_path = write_inputs(tmp_path, "\ud800")
    out = tmp_path / "out.txt"
    assert main(["render", str(plan_path), "--cards", str(cards_path), "-o", str(out)]) == 0
    assert "\\ud800" in out.read_text(encoding="utf-8")


def test_cli_render_surrogate_name_to_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan_path, cards_path = write_inputs(tmp_path, "\ud800")
    assert main(["render", str(plan_path), "--cards", str(cards_path)]) == 0
    assert "\\ud800" in capsys.readouterr().out


def run_bad(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], plan: bytes, cards: bytes, *extra: str
) -> str:
    plan_path = tmp_path / "p.json"
    cards_path = tmp_path / "c.json"
    plan_path.write_bytes(plan)
    cards_path.write_bytes(cards)
    code = main(["render", str(plan_path), "--cards", str(cards_path), *extra])
    assert code == 2
    return capsys.readouterr().err


def good_plan() -> bytes:
    return Path(__file__).parent.joinpath("fixtures/plan_minimal.json").read_bytes()


@pytest.mark.parametrize(
    "plan,cards",
    [
        (b"no es json", b"[]"),
        (b"\xff\xfe", b"[]"),
        (b"[" * 100000, b"[]"),
        (b'{"schema_version": 1}', b"[]"),
        (None, b"no es json"),
        (None, b"\xff\xfe"),
        (None, b"[" * 100000),
        (None, b"{}"),
        (None, b'[{"candidate": "x"}]'),
        (None, b'[{"candidate":"x","level":"Raro","reasons":[],"flags":[],"pinned_ref":null,"popularity":0}]'),
    ],
)
def test_cli_render_bad_input_returns_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], plan: bytes | None, cards: bytes
) -> None:
    err = run_bad(tmp_path, capsys, plan if plan is not None else good_plan(), cards)
    assert err.strip()


def test_cli_render_unreadable_files(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    plan_path, cards_path = write_inputs(tmp_path)
    assert main(["render", str(tmp_path / "no.json"), "--cards", str(cards_path)]) == 2
    assert main(["render", str(plan_path), "--cards", str(tmp_path / "no.json")]) == 2
    assert main(["render", "a\x00b", "--cards", str(cards_path)]) == 2
    capsys.readouterr()


def test_cli_render_output_unwritable(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    plan_path, cards_path = write_inputs(tmp_path)
    bad = tmp_path / "no" / "out.txt"
    assert main(["render", str(plan_path), "--cards", str(cards_path), "-o", str(bad)]) == 2
    assert main(["render", str(plan_path), "--cards", str(cards_path), "-o", "a\x00b"]) == 2
    capsys.readouterr()


def test_cli_render_rejects_other_formats_and_missing_args(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan_path, cards_path = write_inputs(tmp_path)
    assert main(["render", str(plan_path), "--cards", str(cards_path), "--format", "pdf"]) == 2
    assert main(["render", str(plan_path)]) == 2
    capsys.readouterr()
