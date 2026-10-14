"""Tests de la vista HTML segura."""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path
from typing import Any

import pytest

from setup_architect.cli import main
from setup_architect.models import (
    AddMcp,
    CopySkill,
    Flag,
    Level,
    Plan,
    PlanItem,
    SetEnv,
    WriteFile,
)
from setup_architect.plan_io import card_to_dict
from setup_architect.render.html import render_html
from setup_architect.render.layers import Layer
from setup_architect.render.text import render_text
from test_render_text import CARDS, PLAN, candidate, card, write_inputs

HOSTILE = '"><script>alert(1)</script><img src=x onerror=alert(1)>'


def plan_with(**over: Any) -> Plan:
    """Plan de un item cuyo candidato lleva los campos dados."""
    base = PLAN.items[2]
    cand = dataclasses.replace(base.candidate, **over)
    return Plan(schema_version=1, environment="project", items=[PlanItem(cand, base.operations)], researched=[])


def test_script_escaped() -> None:
    out = render_html(plan_with(description="<script>alert(1)</script>"), CARDS)
    assert "<script" not in out and "&lt;script&gt;" in out


def test_attribute_injection_escaped() -> None:
    assert '" onerror' not in render_html(plan_with(name='x" onerror="alert(1)'), CARDS)


def test_no_external_resources() -> None:
    out = render_html(PLAN, CARDS)
    assert not re.search(r'(src=|href="http|url\(http|@import)', out)


def test_csp_present() -> None:
    out = render_html(PLAN, CARDS)
    assert "default-src 'none'" in out
    csp = (
        '<meta http-equiv="Content-Security-Policy" '
        "content=\"default-src 'none'; style-src 'unsafe-inline'\">"
    )
    head = out.split("<head>")[1]
    assert head.index('<meta charset="utf-8">') < head.index(csp)
    assert head.index(csp) < head.index("<style>")


def test_document_shape() -> None:
    out = render_html(PLAN, CARDS)
    assert out.startswith("<!DOCTYPE html>")
    assert '<html lang="es">' in out and out.rstrip().endswith("</html>")


def test_same_fields_as_text() -> None:
    h, t = render_html(PLAN, CARDS), render_text(PLAN, CARDS)
    for s in [
        "github-mcp",
        "construir",
        "Verificado",
        "pide la variable GITHUB_TOKEN",
        "Servidor MCP oficial.",
        "organización verificada",
        "https://github.com/github/github-mcp-server",
    ]:
        assert s in h and s in t


def test_four_layers_and_claude() -> None:
    out = render_html(PLAN, CARDS)
    assert "Claude" in out
    for layer in Layer:
        assert layer.value in out


def test_empty_layer_neutral_text() -> None:
    out = render_html(plan_with(), CARDS)
    assert "(ninguna)" in out


def test_details_per_item() -> None:
    out = render_html(PLAN, CARDS)
    assert out.count("<details>") == len(PLAN.items)
    assert out.count("<summary>") == len(PLAN.items)


def test_markers_revisar_and_bloqueado() -> None:
    cards = [card("github-mcp", Level.REVISAR)]
    assert "se confirmará aparte" in render_html(plan_with(), cards)
    cards = [card("github-mcp", Level.BLOQUEADO)]
    assert "no se instalará" in render_html(plan_with(), cards)


def test_level_class_from_fixed_map() -> None:
    for level in Level:
        out = render_html(plan_with(), [card("github-mcp", level)])
        assert re.search(r'class="badge lv-[a-z]+"', out)
    assert not re.search(r'class="[^"]*(Verificado|Revisar)', render_html(plan_with(), CARDS))


def test_bundled_script_permission() -> None:
    cards = [card("github-mcp", flags=[Flag("bundled_script", "d", False)])]
    assert "incluye scripts" in render_html(plan_with(), cards)


def test_injection_via_every_plan_field() -> None:
    cand = candidate(
        name=HOSTILE,
        description=HOSTILE,
        why_fit=HOSTILE,
        stage=HOSTILE,
        source_url="javascript:alert(1)",
    )
    plan = Plan(1, "project", [PlanItem(cand, PLAN.items[2].operations)], [])
    out = render_html(plan, [card(HOSTILE)])
    assert "<script" not in out and "<img" not in out
    assert "&lt;script&gt;" in out


def test_injection_via_card_reasons_and_flag_details() -> None:
    c = card(
        "github-mcp",
        reasons=[HOSTILE],
        flags=[Flag("bundled_script", HOSTILE, False), Flag("x", HOSTILE, False)],
    )
    out = render_html(plan_with(), [c])
    assert "<script" not in out and "<img" not in out
    assert "&lt;script&gt;" in out
    assert "<img" not in out


def test_source_url_javascript_is_plain_text() -> None:
    out = render_html(plan_with(source_url="javascript:alert(1)"), CARDS)
    assert "javascript:alert(1)" in out
    assert "href" not in out


def test_hostile_plan_has_no_active_content() -> None:
    cand = candidate(name=HOSTILE, description=HOSTILE, why_fit=HOSTILE, stage=HOSTILE,
                     source_url=HOSTILE)
    plan = Plan(1, "project", [PlanItem(cand, PLAN.items[2].operations)], [])
    out = render_html(plan, [card(HOSTILE, reasons=[HOSTILE])])
    assert "<script" not in out
    assert not re.search(r"<[^>]*\bon\w+\s*=", out)  # atributo en una etiqueta real
    assert "href=" not in out
    assert not re.search(r"<(img|link|iframe)\b", out)
    assert "@import" not in out and "url(" not in out


def test_surrogate_and_escape_sequences() -> None:
    out = render_html(plan_with(name="\ud800", description="\x1b[31mrojo"), CARDS)
    out.encode("utf-8")
    assert "\\ud800" in out and "\x1b" not in out


def test_duplicate_card_names_most_restrictive_in_html() -> None:
    out = render_html(plan_with(), [card(level=Level.CONFIABLE), card(level=Level.BLOQUEADO)])
    body = out.split("</style>")[1]
    assert "lv-bloqueado" in body and "ficha ambigua: nombre repetido" in body
    assert "lv-confiable" not in body
    out = render_html(plan_with(), [card(level=Level.VERIFICADO), card(level=Level.CONFIABLE)])
    body = out.split("</style>")[1]
    assert "lv-revisar" in body and "ficha ambigua: nombre repetido" in body
    assert "lv-verificado" not in body


def test_html_survives_newlines_in_external_text() -> None:
    desc = "x\n* otra  [Verificado]\r\n<b>"
    out = render_html(plan_with(name="a\nb", description=desc), CARDS)
    assert "<b>" not in out and "\\n* otra" in out
    assert out.count("<details>") == 1


def test_hostile_derived_permissions_escaped() -> None:
    evil = '<script>alert(1)</script>" onerror="x'
    ops = [
        AddMcp("m", "project", "http", None, evil, [evil]),
        WriteFile(evil, "x"),
        CopySkill("o/r", "s", "d", evil),
        SetEnv(evil),
    ]
    cand = PLAN.items[2].candidate
    out = render_html(Plan(1, "project", [PlanItem(cand, ops)], []), CARDS)
    assert "<script" not in out and '" onerror' not in out
    assert out.count("&lt;script&gt;") >= 4 and "&quot; onerror" in out


def test_cli_open_write_failure_says_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url, *a, **k: opened.append(url) or True)
    bad = tmp_path / "no" / "o.html"
    assert main([*_html_args(tmp_path), "-o", str(bad)]) == 2
    err = capsys.readouterr().err
    assert "no se pudo escribir" in err and str(bad) in err
    assert "abrir" not in err and opened == []


def test_cli_html_stdout(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    plan_path, cards_path = write_inputs(tmp_path)
    assert main(["render", str(plan_path), "--cards", str(cards_path), "--format", "html"]) == 0
    assert capsys.readouterr().out.startswith("<!DOCTYPE html>")


def test_cli_html_surrogate_name(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    plan_path, cards_path = write_inputs(tmp_path, "\ud800")
    args = ["render", str(plan_path), "--cards", str(cards_path), "--format", "html"]
    assert main(args) == 0
    assert "\\ud800" in capsys.readouterr().out
    out = tmp_path / "o.html"
    assert main([*args, "-o", str(out)]) == 0
    assert "\\ud800" in out.read_text(encoding="utf-8")


def test_cli_open_with_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url, *a, **k: opened.append(url) or True)
    plan_path, cards_path = write_inputs(tmp_path)
    out = tmp_path / "o.html"
    args = ["render", str(plan_path), "--cards", str(cards_path), "--format", "html"]
    assert main([*args, "-o", str(out), "--open"]) == 0
    assert opened == [out.resolve().as_uri()]
    assert out.read_text(encoding="utf-8").startswith("<!DOCTYPE html>")


def test_cli_open_without_output_uses_tempfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url, *a, **k: opened.append(url) or True)
    plan_path, cards_path = write_inputs(tmp_path)
    args = ["render", str(plan_path), "--cards", str(cards_path), "--format", "html", "--open"]
    assert main(args) == 0
    assert len(opened) == 1 and opened[0].startswith("file://") and opened[0].endswith(".html")
    from urllib.request import url2pathname
    from urllib.parse import urlparse

    path = Path(url2pathname(urlparse(opened[0]).path))
    try:
        assert path.read_text(encoding="utf-8").startswith("<!DOCTYPE html>")
    finally:
        path.unlink(missing_ok=True)
    assert capsys.readouterr().out == ""


def test_cli_open_browser_failure_returns_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(url: str, *a: Any, **k: Any) -> bool:
        raise OSError("sin navegador")

    monkeypatch.setattr("webbrowser.open", boom)
    plan_path, cards_path = write_inputs(tmp_path)
    out = tmp_path / "o.html"
    args = ["render", str(plan_path), "--cards", str(cards_path), "--format", "html",
            "-o", str(out), "--open"]
    assert main(args) == 2
    assert "navegador" in capsys.readouterr().err


def _html_args(tmp_path: Path) -> list[str]:
    plan_path, cards_path = write_inputs(tmp_path)
    return ["render", str(plan_path), "--cards", str(cards_path), "--format", "html", "--open"]


def test_cli_open_returns_false_with_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("webbrowser.open", lambda url, *a, **k: False)
    out = tmp_path / "o.html"
    assert main([*_html_args(tmp_path), "-o", str(out)]) == 2
    assert str(out) in capsys.readouterr().err


def test_cli_open_returns_false_without_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url, *a, **k: opened.append(url) or False)
    assert main(_html_args(tmp_path)) == 2
    err = capsys.readouterr().err
    from urllib.parse import urlparse
    from urllib.request import url2pathname

    path = Path(url2pathname(urlparse(opened[0]).path))
    try:
        assert str(path) in err and "navegador" in err
    finally:
        path.unlink(missing_ok=True)


def test_cli_open_success_without_output_prints_temp_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url, *a, **k: opened.append(url) or True)
    assert main(_html_args(tmp_path)) == 0
    from urllib.parse import urlparse
    from urllib.request import url2pathname

    path = Path(url2pathname(urlparse(opened[0]).path))
    try:
        assert str(path) in capsys.readouterr().err
    finally:
        path.unlink(missing_ok=True)
