"""Tests de detección de typosquatting (verify/typosquat.py)."""

from __future__ import annotations

from setup_architect.verify.typosquat import detect_typosquat, levenshtein

KNOWN: list[tuple[str, str]] = [
    ("modelcontextprotocol", "servers"),
    ("github", "github-mcp-server"),
    ("anthropics", "skills"),
    ("anthropics", "claude-code"),
]


# --- casos básicos ------------------------------------------------------


def test_typosquat_flagged() -> None:
    assert detect_typosquat(
        "randomdev", "servres", [("modelcontextprotocol", "servers")], 2
    ).veto


def test_same_owner_not_flagged() -> None:
    assert detect_typosquat("modelcontextprotocol", "servers", KNOWN, 2) is None


def test_unrelated_not_flagged() -> None:
    assert detect_typosquat("x", "totally-different", KNOWN, 2) is None


# --- levenshtein: casos base ----------------------------------------------


def test_levenshtein_identical_strings_is_zero() -> None:
    assert levenshtein("servers", "servers") == 0


def test_levenshtein_empty_string_is_length_of_other() -> None:
    assert levenshtein("", "abc") == 3
    assert levenshtein("abc", "") == 3


def test_levenshtein_single_substitution() -> None:
    assert levenshtein("servers", "servres") == 2


def test_levenshtein_insertion() -> None:
    assert levenshtein("skill", "skills") == 1


# --- detect_typosquat: casos borde ----------------------------------------


def test_exact_name_different_owner_is_flagged_at_distance_zero() -> None:
    flag = detect_typosquat("evil", "servers", KNOWN, 0)
    assert flag is not None
    assert flag.code == "typosquat"
    assert flag.veto is True


def test_case_insensitive_owner_match_is_not_flagged() -> None:
    assert detect_typosquat("ModelContextProtocol", "servers", KNOWN, 2) is None


def test_case_insensitive_name_distance() -> None:
    assert detect_typosquat("evil", "SERVERS", KNOWN, 0) is not None


def test_distance_over_max_is_not_flagged() -> None:
    assert detect_typosquat("evil", "serverssss", KNOWN, 1) is None


def test_detail_names_the_known_repo() -> None:
    flag = detect_typosquat("evil", "servers", KNOWN, 0)
    assert flag is not None
    assert "servers" in flag.detail
