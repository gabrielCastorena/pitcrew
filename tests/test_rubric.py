"""Tests de la rúbrica de confianza (config.py y verify/rubric.py)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pitcrew.config import Config, ConfigError, load_config
from pitcrew.models import (
    AddMcp,
    Candidate,
    CopySkill,
    Flag,
    Level,
    Operation,
    RepoFacts,
    SetEnv,
    TrustCard,
    WriteFile,
)
from pitcrew.verify.rubric import evaluate

NOW = datetime(2026, 10, 5, tzinfo=UTC)

CONFIG = Config(
    official_owners=frozenset({"anthropics", "modelcontextprotocol", "github"}),
    known_names=[("modelcontextprotocol", "servers"), ("anthropics", "skills")],
)

CANDIDATE = Candidate(
    name="demo",
    kind="mcp",
    source_url="https://github.com/acme/tool",
    ref=None,
    description="una herramienta de ejemplo",
    why_fit="encaja con el proyecto",
    stage="construir",
)


def facts(
    *,
    owner: str = "acme",
    name: str = "tool",
    stars: int = 100,
    is_fork: bool = False,
    ahead_by: int | None = None,
    pushed_days_ago: int = 10,
    created_days_ago: int = 1000,
) -> RepoFacts:
    return RepoFacts(
        owner=owner,
        name=name,
        created_at=NOW - timedelta(days=created_days_ago),
        pushed_at=NOW - timedelta(days=pushed_days_ago),
        stars=stars,
        forks=1,
        open_issues=0,
        is_fork=is_fork,
        parent=f"{owner}/upstream" if is_fork else None,
        ahead_by=ahead_by,
        owner_verified=True,
        head_sha="a" * 40,
        description="",
    )


def official_facts(**kwargs: object) -> RepoFacts:
    kwargs.setdefault("owner", "github")
    kwargs.setdefault("name", "github-mcp-server")
    return facts(**kwargs)  # type: ignore[arg-type]


def third_party(**kwargs: object) -> RepoFacts:
    return facts(**kwargs)  # type: ignore[arg-type]


def veto(code: str) -> Flag:
    return Flag(code=code, detail="detalle de prueba", veto=True)


def ev(
    repo_facts: RepoFacts | None,
    *,
    ops: list[Operation] | None = None,
    skill_flags: list[Flag] | None = None,
    config: Config = CONFIG,
    now: datetime = NOW,
    error: str | None = None,
) -> TrustCard:
    return evaluate(
        CANDIDATE,
        ops if ops is not None else [],
        repo_facts,
        skill_flags if skill_flags is not None else [],
        config,
        now,
        error=error,
    )


# --- casos básicos ------------------------------------------------------


def test_official_healthy_verificado() -> None:
    assert ev(official_facts()).level == Level.VERIFICADO


def test_typosquat_bloqueado() -> None:
    assert ev(facts(owner="randomdev", name="servres")).level == Level.BLOQUEADO


def test_curl_bash_bloqueado() -> None:
    assert (
        ev(third_party(), skill_flags=[veto("dangerous_pipe")]).level
        == Level.BLOQUEADO
    )


def test_inactive_revisar() -> None:
    assert ev(third_party(pushed_days_ago=400)).level == Level.REVISAR


def test_veto_beats_everything() -> None:
    assert (
        ev(official_facts(stars=90000), skill_flags=[veto("remote_eval")]).level
        == Level.BLOQUEADO
    )


def test_third_party_healthy_confiable() -> None:
    assert ev(third_party()).level == Level.CONFIABLE


def test_error_is_revisar() -> None:
    c = ev(None, error="403")
    assert c.level == Level.REVISAR and "no se pudo verificar" in c.reasons[0]


def test_unsupported_source_revisar() -> None:
    assert "fuente no soportada" in ev(None).reasons[0]


def test_popularity_never_changes_level() -> None:
    assert ev(third_party(stars=0)).level == ev(third_party(stars=100000)).level


# --- banderas de la rúbrica: estrellas infladas y fork casi idéntico -------


def test_inflated_stars_vetoed() -> None:
    card = ev(third_party(created_days_ago=5, stars=1000))
    assert card.level == Level.BLOQUEADO
    assert any(flag.code == "inflated_stars" for flag in card.flags)


def test_new_repo_with_few_stars_not_vetoed() -> None:
    card = ev(third_party(created_days_ago=5, stars=10))
    assert card.level == Level.CONFIABLE


def test_old_repo_with_many_stars_not_vetoed() -> None:
    card = ev(third_party(created_days_ago=1000, stars=100000))
    assert card.level == Level.CONFIABLE


def test_near_identical_fork_vetoed() -> None:
    card = ev(third_party(is_fork=True, ahead_by=2))
    assert card.level == Level.BLOQUEADO
    assert any(flag.code == "near_identical_fork" for flag in card.flags)


def test_fork_with_enough_ahead_not_vetoed() -> None:
    card = ev(third_party(is_fork=True, ahead_by=5))
    assert card.level == Level.CONFIABLE
    assert all(flag.code != "near_identical_fork" for flag in card.flags)


# --- fork sin comparar (ahead_by is None): no es veto, no puede ser Confiable


def test_fork_ahead_by_none_is_revisar_for_third_party() -> None:
    card = ev(third_party(is_fork=True, ahead_by=None))
    assert card.level == Level.REVISAR
    assert all(flag.code != "near_identical_fork" for flag in card.flags)
    assert any("fork" in reason for reason in card.reasons)


def test_fork_ahead_by_none_still_verificado_if_official_and_healthy() -> None:
    card = ev(official_facts(is_fork=True, ahead_by=None))
    assert card.level == Level.VERIFICADO


# --- high_risk: stdio, env_names y bundled_script bajan a Revisar en terceros


def test_high_risk_stdio_mcp_is_revisar() -> None:
    ops: list[Operation] = [
        AddMcp(
            name="x",
            scope="project",
            transport="stdio",
            command=["x"],
            url=None,
            env_names=[],
        )
    ]
    assert ev(third_party(), ops=ops).level == Level.REVISAR


def test_high_risk_env_names_is_revisar() -> None:
    ops: list[Operation] = [
        AddMcp(
            name="x",
            scope="project",
            transport="http",
            command=None,
            url="https://x.example.com",
            env_names=["TOKEN"],
        )
    ]
    assert ev(third_party(), ops=ops).level == Level.REVISAR


def test_high_risk_bundled_script_is_revisar() -> None:
    flag = Flag(code="bundled_script", detail="run.sh", veto=False)
    card = ev(third_party(), skill_flags=[flag])
    assert card.level == Level.REVISAR
    assert flag in card.flags


def test_http_mcp_without_env_names_is_revisar_as_unverified_mcp() -> None:
    # No es `high_risk`, pero la URL de un MCP no se verifica contra la
    # fuente.
    ops: list[Operation] = [
        AddMcp(
            name="x",
            scope="project",
            transport="http",
            command=None,
            url="https://x.example.com",
            env_names=[],
        )
    ]
    card = ev(third_party(), ops=ops)
    assert card.level == Level.REVISAR
    assert not any("riesgo alto" in reason for reason in card.reasons)


def test_official_with_stdio_mcp_is_no_longer_verificado() -> None:
    # `high_risk` no aplica a oficiales, pero el paquete de un MCP no se
    # verifica contra la fuente.
    ops: list[Operation] = [
        AddMcp(
            name="x",
            scope="project",
            transport="stdio",
            command=["x"],
            url=None,
            env_names=[],
        )
    ]
    card = ev(official_facts(), ops=ops)
    assert card.level == Level.REVISAR
    assert not any("riesgo alto" in reason for reason in card.reasons)


# --- pinned_ref: SHA de 40 hex del candidato, o head_sha si no --------------


def test_pinned_ref_uses_candidate_sha_when_valid() -> None:
    candidate = Candidate(
        name="demo",
        kind="mcp",
        source_url="https://github.com/acme/tool",
        ref="b" * 40,
        description="",
        why_fit="",
        stage="construir",
    )
    card = evaluate(candidate, [], third_party(), [], CONFIG, NOW)
    assert card.pinned_ref == "b" * 40


def test_pinned_ref_falls_back_to_head_sha_when_ref_is_not_a_sha() -> None:
    candidate = Candidate(
        name="demo",
        kind="mcp",
        source_url="https://github.com/acme/tool",
        ref="main",
        description="",
        why_fit="",
        stage="construir",
    )
    repo_facts = third_party()
    card = evaluate(candidate, [], repo_facts, [], CONFIG, NOW)
    assert card.pinned_ref == repo_facts.head_sha


def test_pinned_ref_falls_back_to_head_sha_when_ref_is_none() -> None:
    card = ev(third_party())
    assert card.pinned_ref == third_party().head_sha


def test_pinned_ref_none_when_facts_is_none() -> None:
    assert ev(None).pinned_ref is None


# --- popularity y reasons: casos adicionales --------------------------------


def test_popularity_is_facts_stars() -> None:
    assert ev(third_party(stars=42)).popularity == 42


def test_reasons_not_empty_for_verificado() -> None:
    assert ev(official_facts()).reasons


def test_reasons_not_empty_for_confiable() -> None:
    assert ev(third_party()).reasons


def test_bloqueado_has_one_reason_per_veto() -> None:
    card = ev(
        third_party(), skill_flags=[veto("dangerous_pipe"), veto("remote_eval")]
    )
    assert len(card.reasons) == 2


def test_non_veto_skill_flag_kept_in_card_flags() -> None:
    flag = Flag(code="bundled_script", detail="run.sh", veto=False)
    card = ev(third_party(), skill_flags=[flag])
    assert flag in card.flags


# --- load_config -------------------------------------------------------------

REPO_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def test_load_config_from_repo_config_dir() -> None:
    config = load_config(REPO_CONFIG_DIR)
    assert config.max_inactive_days == 365
    assert config.new_repo_days == 30
    assert config.new_repo_star_veto == 500
    assert config.typosquat_max_distance == 2
    assert config.fork_min_ahead == 5
    assert config.official_owners == frozenset(
        {"anthropics", "modelcontextprotocol", "github"}
    )
    assert ("modelcontextprotocol", "servers") in config.known_names


def test_load_config_missing_directory_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(tmp_path / "no-existe")


def test_load_config_missing_thresholds_file_raises(tmp_path: Path) -> None:
    (tmp_path / "official_owners.txt").write_text("", encoding="utf-8")
    (tmp_path / "known_names.txt").write_text("", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_load_config_missing_official_owners_file_raises(tmp_path: Path) -> None:
    (tmp_path / "thresholds.toml").write_text("", encoding="utf-8")
    (tmp_path / "known_names.txt").write_text("", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_load_config_missing_known_names_file_raises(tmp_path: Path) -> None:
    (tmp_path / "thresholds.toml").write_text("", encoding="utf-8")
    (tmp_path / "official_owners.txt").write_text("", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_load_config_partial_thresholds_use_defaults(tmp_path: Path) -> None:
    (tmp_path / "thresholds.toml").write_text(
        "max_inactive_days = 10\n", encoding="utf-8"
    )
    (tmp_path / "official_owners.txt").write_text("acme\n", encoding="utf-8")
    (tmp_path / "known_names.txt").write_text("acme/tool\n", encoding="utf-8")
    config = load_config(tmp_path)
    assert config.max_inactive_days == 10
    assert config.new_repo_days == 30
    assert config.new_repo_star_veto == 500


def test_load_config_unknown_threshold_key_raises(tmp_path: Path) -> None:
    (tmp_path / "thresholds.toml").write_text("unknown_key = 1\n", encoding="utf-8")
    (tmp_path / "official_owners.txt").write_text("", encoding="utf-8")
    (tmp_path / "known_names.txt").write_text("", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_load_config_non_integer_threshold_raises(tmp_path: Path) -> None:
    (tmp_path / "thresholds.toml").write_text(
        'max_inactive_days = "diez"\n', encoding="utf-8"
    )
    (tmp_path / "official_owners.txt").write_text("", encoding="utf-8")
    (tmp_path / "known_names.txt").write_text("", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_load_config_boolean_threshold_raises(tmp_path: Path) -> None:
    (tmp_path / "thresholds.toml").write_text(
        "max_inactive_days = true\n", encoding="utf-8"
    )
    (tmp_path / "official_owners.txt").write_text("", encoding="utf-8")
    (tmp_path / "known_names.txt").write_text("", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_load_config_comments_and_blank_lines_ignored(tmp_path: Path) -> None:
    (tmp_path / "thresholds.toml").write_text("", encoding="utf-8")
    (tmp_path / "official_owners.txt").write_text(
        "# comentario\n\nacme\n", encoding="utf-8"
    )
    (tmp_path / "known_names.txt").write_text(
        "# comentario\n\nacme/tool\n", encoding="utf-8"
    )
    config = load_config(tmp_path)
    assert config.official_owners == frozenset({"acme"})
    assert config.known_names == [("acme", "tool")]


def test_load_config_official_owners_lowercased(tmp_path: Path) -> None:
    (tmp_path / "thresholds.toml").write_text("", encoding="utf-8")
    (tmp_path / "official_owners.txt").write_text("ACME\n", encoding="utf-8")
    (tmp_path / "known_names.txt").write_text("", encoding="utf-8")
    config = load_config(tmp_path)
    assert "acme" in config.official_owners


def test_load_config_invalid_known_name_entry_raises(tmp_path: Path) -> None:
    (tmp_path / "thresholds.toml").write_text("", encoding="utf-8")
    (tmp_path / "official_owners.txt").write_text("", encoding="utf-8")
    (tmp_path / "known_names.txt").write_text("no-tiene-slash\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(tmp_path)


# --- un AddMcp nunca es Verificado ni Confiable -----------------------------


UNSUPPORTED_SOURCE_FLAG = Flag(
    code="unsupported_source", detail="la fuente no es un repo de GitHub soportado", veto=False
)


def _stdio(*command: str) -> AddMcp:
    return AddMcp("srv", "user", "stdio", list(command), None, [])


def _http(url: str = "https://mcp.example.com/x") -> AddMcp:
    return AddMcp("srv", "user", "http", None, url, [])


def test_official_with_stdio_mcp_is_revisar_unverified_mcp() -> None:
    card = ev(official_facts(), ops=[_stdio("npx", "-y", "some-other-package@1.0.0")])
    detail = "el paquete del MCP no se verifica contra la fuente: npx -y some-other-package@1.0.0"
    assert card.level == Level.REVISAR
    assert card.reasons == [detail]
    assert Flag(code="unverified_mcp", detail=detail, veto=False) in card.flags


def test_official_with_http_mcp_is_revisar_unverified_mcp() -> None:
    card = ev(official_facts(), ops=[_http()])
    detail = "la URL del MCP no se verifica contra la fuente: https://mcp.example.com/x"
    assert card.level == Level.REVISAR
    assert card.reasons == [detail]
    assert Flag(code="unverified_mcp", detail=detail, veto=False) in card.flags


def test_third_party_with_http_mcp_without_env_is_revisar() -> None:
    card = ev(third_party(), ops=[_http()])
    assert card.level == Level.REVISAR
    assert [flag.code for flag in card.flags] == ["unverified_mcp"]


def test_one_reason_and_flag_per_add_mcp() -> None:
    card = ev(official_facts(), ops=[_stdio("npx", "a@1.0.0"), _http("https://b.example")])
    assert card.level == Level.REVISAR
    assert card.reasons == [
        "el paquete del MCP no se verifica contra la fuente: npx a@1.0.0",
        "la URL del MCP no se verifica contra la fuente: https://b.example",
    ]
    assert [flag.detail for flag in card.flags if flag.code == "unverified_mcp"] == card.reasons


def test_official_without_add_mcp_is_still_verificado() -> None:
    # Con un `copy_skill`; un `write_file` nunca es Verificado.
    card = ev(official_facts(), ops=[CopySkill("github/github-mcp-server", "a" * 40, "skill", "x")])
    assert card.level == Level.VERIFICADO
    assert not any(flag.code == "unverified_mcp" for flag in card.flags)


def test_typosquat_with_add_mcp_is_still_bloqueado() -> None:
    card = ev(third_party(owner="evil", name="servres"), ops=[_http()])
    assert card.level == Level.BLOQUEADO
    assert all(reason.startswith("typosquat") for reason in card.reasons)


def test_unverified_mcp_keeps_other_revisar_reasons() -> None:
    card = ev(third_party(pushed_days_ago=900), ops=[_stdio("npx", "a@1.0.0")])
    assert card.level == Level.REVISAR
    assert any("inactivo" in reason for reason in card.reasons)
    assert any("riesgo alto" in reason for reason in card.reasons)
    assert "el paquete del MCP no se verifica contra la fuente: npx a@1.0.0" in card.reasons


def test_unsupported_source_with_add_mcp_names_what_is_not_verified() -> None:
    card = ev(None, ops=[_http(), _stdio("npx", "a@1.0.0")])
    details = [
        "la URL del MCP no se verifica contra la fuente: https://mcp.example.com/x",
        "el paquete del MCP no se verifica contra la fuente: npx a@1.0.0",
    ]
    assert card.level == Level.REVISAR
    assert card.reasons == ["fuente no soportada", *details]
    assert card.flags == [
        UNSUPPORTED_SOURCE_FLAG,
        *(Flag(code="unverified_mcp", detail=d, veto=False) for d in details),
    ]
    assert card.pinned_ref is None and card.popularity == 0


# --- lo no verificado lleva la bandera `unverified` --------------------------


def test_error_carries_unverified_flag() -> None:
    card = ev(None, error="la API de GitHub respondió con el código 404")
    assert card.level == Level.REVISAR
    assert card.reasons == ["no se pudo verificar: la API de GitHub respondió con el código 404"]
    assert card.flags == [
        Flag(code="unverified", detail="la API de GitHub respondió con el código 404", veto=False)
    ]


def test_unsupported_source_has_no_unverified_flag() -> None:
    # Lleva `unsupported_source`, pero no `unverified`.
    assert ev(None).flags == [UNSUPPORTED_SOURCE_FLAG]


# --- la ruta "fuente no soportada" lleva la bandera `unsupported_source`


def test_unsupported_source_carries_unsupported_source_flag() -> None:
    card = ev(None)
    assert card.level == Level.REVISAR
    assert card.reasons == ["fuente no soportada"]
    assert [(flag.code, flag.veto) for flag in card.flags] == [("unsupported_source", False)]


def test_only_the_unsupported_source_route_carries_the_flag() -> None:
    for card in (
        ev(None, error="403"),
        ev(third_party()),
        ev(third_party(pushed_days_ago=900)),
        ev(official_facts(), ops=[_http()]),
        ev(third_party(owner="evil", name="servres")),
    ):
        assert not any(flag.code == "unsupported_source" for flag in card.flags)


# --- un write_file bajo .claude/skills/ nunca pasa de Revisar -------------------

SKILL_PATH = ".claude/skills/x/SKILL.md"
SKILL_REASON = f"el contenido escrito bajo .claude/skills/ no se verifica contra la fuente: {SKILL_PATH}"
SKILL_FLAG = Flag(code="unverified_content", detail=SKILL_PATH, veto=False)
CLAUDE_MD_REASON = "el contenido que escribe el plan no se verifica contra la fuente: CLAUDE.md"
CLAUDE_MD_FLAG = Flag(code="unverified_content", detail="CLAUDE.md", veto=False)


def test_official_with_skill_write_file_is_revisar_unverified_content() -> None:
    card = ev(official_facts(), ops=[WriteFile(SKILL_PATH, "# x\n")])
    assert card.level == Level.REVISAR
    assert card.reasons == [SKILL_REASON]
    assert card.flags == [SKILL_FLAG]


def test_official_healthy_source_with_claude_md_write_file_is_revisar() -> None:
    # El nivel saldría del repo de la fuente, no del contenido, que lo
    # redacta el plan.
    facts_ = official_facts()
    assert ev(facts_).level == Level.VERIFICADO  # la fuente, sola, es oficial y sana
    card = ev(facts_, ops=[WriteFile("CLAUDE.md", "# x\n")])
    assert card.level == Level.REVISAR
    assert card.reasons == [CLAUDE_MD_REASON]
    assert card.flags == [CLAUDE_MD_FLAG]
    assert not any(".claude/skills" in reason for reason in card.reasons)


def test_third_party_with_skill_write_file_is_revisar() -> None:
    card = ev(third_party(), ops=[WriteFile(SKILL_PATH, "# x\n")])
    assert card.level == Level.REVISAR
    assert card.reasons == [SKILL_REASON]
    assert card.flags == [SKILL_FLAG]


def test_third_party_healthy_source_with_claude_md_write_file_is_revisar() -> None:
    path = "~/.claude/CLAUDE.md"
    assert ev(third_party()).level == Level.CONFIABLE
    card = ev(third_party(), ops=[WriteFile(path, "# x\n")])
    assert card.level == Level.REVISAR
    assert card.reasons == [f"el contenido que escribe el plan no se verifica contra la fuente: {path}"]
    assert card.flags == [Flag(code="unverified_content", detail=path, veto=False)]


def test_one_reason_and_flag_per_write_file() -> None:
    other = ".claude/skills/y/notas.txt"
    ops: list[Operation] = [
        WriteFile(SKILL_PATH, "# x\n"),
        WriteFile("CLAUDE.md", "# c\n"),
        WriteFile(other, "y\n"),
    ]
    card = ev(official_facts(), ops=ops)
    assert card.level == Level.REVISAR
    # Una razón por operación, en el orden del plan, también la del
    # `CLAUDE.md`.
    assert card.reasons == [
        SKILL_REASON,
        CLAUDE_MD_REASON,
        f"el contenido escrito bajo .claude/skills/ no se verifica contra la fuente: {other}",
    ]
    assert card.flags == [
        SKILL_FLAG,
        CLAUDE_MD_FLAG,
        Flag(code="unverified_content", detail=other, veto=False),
    ]


@pytest.mark.parametrize(
    "path",
    [
        "~/.claude/skills/x/SKILL.md",
        "/home/u/proyecto/.claude/skills/x/SKILL.md",
        "./.claude/skills/x/SKILL.md",
        ".claude/./skills/x/SKILL.md",
        ".claude//skills/x/SKILL.md",
        # La ruta que se escribe es la normalizada: tras quitar el `..`
        # queda bajo .claude/skills/.
        ".claude/otra/../skills/x/SKILL.md",
        "x/../.claude/skills/x/SKILL.md",
        ".claude/skills/x/CLAUDE.md",
    ],
)
def test_skill_write_file_is_revisar_whatever_the_path_spelling(path: str) -> None:
    card = ev(official_facts(), ops=[WriteFile(path, "# x\n")])
    assert card.level == Level.REVISAR
    assert card.flags == [Flag(code="unverified_content", detail=path, veto=False)]


@pytest.mark.parametrize("path", ["CLAUDE.md", "./CLAUDE.md", "~/.claude/CLAUDE.md", "docs/skills/x.md", ".claude/skillsx/a.md"])
def test_write_file_outside_skills_dir_is_revisar_with_the_generic_reason(path: str) -> None:
    # La regla no depende de la ruta: solo cambia el texto de la razón.
    card = ev(official_facts(), ops=[WriteFile(path, "# x\n")])
    assert card.level == Level.REVISAR
    assert card.reasons == [f"el contenido que escribe el plan no se verifica contra la fuente: {path}"]
    assert card.flags == [Flag(code="unverified_content", detail=path, veto=False)]


def test_skill_write_file_keeps_other_revisar_reasons() -> None:
    card = ev(third_party(pushed_days_ago=900), ops=[WriteFile(SKILL_PATH, "# x\n"), _http()])
    assert card.level == Level.REVISAR
    assert any("inactivo" in reason for reason in card.reasons)
    assert SKILL_REASON in card.reasons
    assert "la URL del MCP no se verifica contra la fuente: https://mcp.example.com/x" in card.reasons


def test_unsupported_source_with_skill_write_file_names_it() -> None:
    card = ev(None, ops=[WriteFile(SKILL_PATH, "# x\n")])
    assert card.level == Level.REVISAR
    assert card.reasons == ["fuente no soportada", SKILL_REASON]
    assert card.flags == [UNSUPPORTED_SOURCE_FLAG, SKILL_FLAG]


def test_veto_beats_skill_write_file() -> None:
    card = ev(third_party(owner="evil", name="servres"), ops=[WriteFile(SKILL_PATH, "# x\n")])
    assert card.level == Level.BLOQUEADO
    assert all(reason.startswith("typosquat") for reason in card.reasons)
    assert SKILL_FLAG in card.flags


def test_rubric_and_render_share_one_skills_dir_definition() -> None:
    from pitcrew import skill_paths
    from pitcrew.render import layers
    from pitcrew.verify import rubric

    assert layers.in_skills_dir is skill_paths.in_skills_dir
    assert rubric.in_skills_dir is skill_paths.in_skills_dir
    assert skill_paths.in_skills_dir(("a", ".claude", "skills", "x"))
    assert not skill_paths.in_skills_dir((".claude", "x", "skills"))
    assert not skill_paths.in_skills_dir(("skills", ".claude"))


def test_rubric_does_not_depend_on_render_nor_apply() -> None:
    import subprocess
    import sys

    code = (
        "import sys; import pitcrew.verify.rubric; "
        "sys.exit(any(m.startswith(('pitcrew.render', 'pitcrew.apply')) "
        "for m in sys.modules))"
    )
    src = Path(__file__).parent.parent / "src"
    result = subprocess.run([sys.executable, "-c", code], env={"PYTHONPATH": str(src)})
    assert result.returncode == 0


# --- `allowed-tools` limita el nivel a Revisar -----------------------------------

TOOLS_FLAG = Flag(code="allowed_tools", detail="SKILL.md", veto=False)
TOOLS_REASON = "la skill pre-aprueba herramientas sin pedir permiso (allowed-tools): SKILL.md"


def test_official_with_allowed_tools_flag_is_revisar() -> None:
    card = ev(official_facts(), skill_flags=[TOOLS_FLAG])
    assert card.level == Level.REVISAR
    assert card.reasons == [TOOLS_REASON]
    assert card.flags == [TOOLS_FLAG]


def test_third_party_with_allowed_tools_flag_is_revisar() -> None:
    card = ev(third_party(), skill_flags=[TOOLS_FLAG])
    assert card.level == Level.REVISAR
    assert card.reasons == [TOOLS_REASON]
    assert not any("riesgo alto" in reason for reason in card.reasons)


def test_one_reason_per_allowed_tools_flag() -> None:
    other = Flag(code="allowed_tools", detail="refs/otro.md", veto=False)
    card = ev(official_facts(), skill_flags=[TOOLS_FLAG, other])
    assert card.level == Level.REVISAR
    assert card.reasons == [
        TOOLS_REASON,
        "la skill pre-aprueba herramientas sin pedir permiso (allowed-tools): refs/otro.md",
    ]


def test_allowed_tools_keeps_other_revisar_reasons() -> None:
    script = Flag(code="bundled_script", detail="run.sh", veto=False)
    card = ev(third_party(pushed_days_ago=900), skill_flags=[script, TOOLS_FLAG])
    assert card.level == Level.REVISAR
    assert any("inactivo" in reason for reason in card.reasons)
    assert any("riesgo alto" in reason for reason in card.reasons)
    assert TOOLS_REASON in card.reasons


def test_veto_beats_allowed_tools() -> None:
    shell = Flag(code="skill_shell_exec", detail="SKILL.md", veto=True)
    card = ev(official_facts(), skill_flags=[TOOLS_FLAG, shell])
    assert card.level == Level.BLOQUEADO
    assert card.reasons == ["skill_shell_exec: SKILL.md"]
    assert TOOLS_FLAG in card.flags


# === todo `write_file` queda como máximo en Revisar ===============================


@pytest.mark.parametrize("path", ["CLAUDE.md", "~/.claude/CLAUDE.md", SKILL_PATH, "docs/notas.md"])
@pytest.mark.parametrize("official", [True, False])
def test_any_write_file_caps_a_healthy_source_at_revisar(path: str, official: bool) -> None:
    repo_facts = official_facts() if official else third_party()
    assert ev(repo_facts).level in (Level.VERIFICADO, Level.CONFIABLE)
    card = ev(repo_facts, ops=[WriteFile(path, "# x\n")])
    assert card.level == Level.REVISAR
    assert len(card.reasons) == 1 and card.reasons[0].endswith(f"no se verifica contra la fuente: {path}")
    assert card.flags == [Flag(code="unverified_content", detail=path, veto=False)]


def test_unsupported_source_with_claude_md_write_file_names_it() -> None:
    # Como lo arma la skill: el item del `CLAUDE.md` no tiene fuente.
    card = ev(None, ops=[WriteFile("CLAUDE.md", "# x\n")])
    assert card.level == Level.REVISAR
    assert card.reasons == ["fuente no soportada", CLAUDE_MD_REASON]
    assert card.flags == [UNSUPPORTED_SOURCE_FLAG, CLAUDE_MD_FLAG]
    assert card.pinned_ref is None and card.popularity == 0


def test_veto_beats_claude_md_write_file() -> None:
    card = ev(third_party(owner="evil", name="servres"), ops=[WriteFile("CLAUDE.md", "# x\n")])
    assert card.level == Level.BLOQUEADO
    assert all(reason.startswith("typosquat") for reason in card.reasons)
    assert CLAUDE_MD_FLAG in card.flags and not CLAUDE_MD_FLAG.veto


def test_claude_md_write_file_keeps_other_revisar_reasons() -> None:
    card = ev(third_party(pushed_days_ago=900), ops=[WriteFile("CLAUDE.md", "# x\n"), _http()])
    assert card.level == Level.REVISAR
    assert any("inactivo" in reason for reason in card.reasons)
    assert CLAUDE_MD_REASON in card.reasons
    assert "la URL del MCP no se verifica contra la fuente: https://mcp.example.com/x" in card.reasons


def test_error_route_is_unchanged_by_a_write_file() -> None:
    card = ev(None, ops=[WriteFile("CLAUDE.md", "# x\n")], error="403")
    assert card.level == Level.REVISAR
    assert card.reasons == ["no se pudo verificar: 403"]
    assert card.flags == [Flag(code="unverified", detail="403", veto=False)]


def test_items_without_write_file_keep_their_level() -> None:
    # Lo que sigue pudiendo ir en el bloque: sin `write_file` ni `add_mcp`.
    copy = CopySkill("github/github-mcp-server", "a" * 40, "skill", "x")
    for ops in ([], [copy], [SetEnv("TOKEN")]):
        assert ev(official_facts(), ops=ops).level == Level.VERIFICADO
        assert ev(third_party(), ops=ops).level == Level.CONFIABLE
        assert ev(official_facts(), ops=ops).flags == []
