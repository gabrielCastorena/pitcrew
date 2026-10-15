"""Tests de la fuente GitHub del verificador (verify/github.py)."""

from __future__ import annotations

import json
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from pitcrew.verify.github import (
    FetchError,
    Fetcher,
    VerificationError,
    fetch_repo_facts,
    http_fetcher,
    parse_repo_url,
    sha_in_default_branch,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "github"


def _read_fixture(filename: str) -> dict[str, Any]:
    return json.loads((FIXTURES_DIR / filename).read_text(encoding="utf-8"))


_BASE_RESPONSES: dict[str, dict[str, Any]] = {
    "/repos/acme/tool": _read_fixture("repo_acme_tool.json"),
    "/repos/acme/tool/commits/main": _read_fixture("commit_acme_tool.json"),
    "/orgs/acme": _read_fixture("org_acme.json"),
    "/repos/x/fork": _read_fixture("repo_x_fork.json"),
    "/repos/x/fork/commits/main": _read_fixture("commit_x_fork.json"),
    "/repos/x/fork/compare/upstream:main...x:main": _read_fixture("compare_x_fork.json"),
}


def fixture_fetcher(overrides: dict[str, dict[str, Any] | None] | None = None) -> Fetcher:
    """Fetcher de pruebas: ruta de API -> fixture, con overrides opcionales.

    Un override en `None` borra esa ruta (simula una respuesta ausente).
    """
    responses = dict(_BASE_RESPONSES)
    if overrides:
        for path, value in overrides.items():
            if value is None:
                responses.pop(path, None)
            else:
                responses[path] = value

    def fetch(path: str) -> dict:
        if path not in responses:
            raise AssertionError(f"ruta sin fixture: {path}")
        return responses[path]

    return fetch


def failing_fetcher(status: int) -> Fetcher:
    """Fetcher de pruebas que siempre falla con el código HTTP dado."""

    def fetch(path: str) -> dict:
        raise FetchError(status)

    return fetch


# --- casos básicos ------------------------------------------------------


def test_parse_github_url() -> None:
    assert parse_repo_url("https://github.com/a/b.git") == ("a", "b")


def test_parse_malformed_url_is_none() -> None:
    assert parse_repo_url("https://[x/a/b") is None


def test_parse_non_github_is_none() -> None:
    assert parse_repo_url("https://gitlab.com/a/b") is None


def test_facts_from_fixture() -> None:
    f = fetch_repo_facts("acme", "tool", fixture_fetcher())
    assert f.stars == 1200 and f.head_sha == "a" * 40


def test_fork_sets_ahead_by() -> None:
    f = fetch_repo_facts("x", "fork", fixture_fetcher())
    assert f.is_fork and f.ahead_by == 2


def test_http_error_raises() -> None:
    with pytest.raises(VerificationError, match="403"):
        fetch_repo_facts("a", "b", failing_fetcher(403))


# --- parse_repo_url: casos borde -----------------------------------------


def test_parse_github_url_with_trailing_slash() -> None:
    assert parse_repo_url("https://github.com/a/b/") == ("a", "b")


def test_parse_github_url_with_subpath() -> None:
    assert parse_repo_url("https://github.com/a/b/tree/main") == ("a", "b")


def test_parse_gist_subdomain_is_none() -> None:
    assert parse_repo_url("https://gist.github.com/a/b") is None


def test_parse_lookalike_host_is_none() -> None:
    assert parse_repo_url("https://github.com.evil.io/a/b") is None


def test_parse_url_without_name_is_none() -> None:
    assert parse_repo_url("https://github.com/a") is None


# --- fetch_repo_facts: campos derivados y casos borde --------------------


def test_facts_from_fixture_full_fields() -> None:
    f = fetch_repo_facts("acme", "tool", fixture_fetcher())
    assert f.owner == "acme"
    assert f.name == "tool"
    assert f.is_fork is False
    assert f.parent is None
    assert f.ahead_by is None
    assert f.owner_verified is True
    assert f.forks == 87
    assert f.open_issues == 5
    assert f.description == "Una herramienta de ejemplo para pruebas"


def test_fork_parent_full_name() -> None:
    f = fetch_repo_facts("x", "fork", fixture_fetcher())
    assert f.parent == "upstream/original"


def test_fork_compare_qualifies_base_with_parent_owner() -> None:
    # x/fork y su padre (upstream/original) tienen el mismo nombre de branch
    # por defecto ("main"); si la base del compare no se califica con el
    # dueño del padre, se compara x/fork:main contra sí mismo y ahead_by
    # da 0 siempre, vetando forks legítimos.
    requested: list[str] = []
    base_fetch = fixture_fetcher()

    def recording_fetch(path: str) -> dict:
        requested.append(path)
        return base_fetch(path)

    fetch_repo_facts("x", "fork", recording_fetch)

    assert "/repos/x/fork/compare/upstream:main...x:main" in requested


def test_owner_not_organization_skips_verification() -> None:
    # x/fork tiene dueño de tipo "User": nunca se debería llamar /orgs/x.
    f = fetch_repo_facts("x", "fork", fixture_fetcher())
    assert f.owner_verified is False


def test_null_description_becomes_empty_string() -> None:
    f = fetch_repo_facts("x", "fork", fixture_fetcher())
    assert f.description == ""


def test_org_404_means_owner_not_verified() -> None:
    fetch = fixture_fetcher({"/orgs/acme": None})

    def failing_org_fetch(path: str) -> dict:
        if path == "/orgs/acme":
            raise FetchError(404)
        return fetch(path)

    f = fetch_repo_facts("acme", "tool", failing_org_fetch)
    assert f.owner_verified is False


def test_malformed_response_missing_key_raises_verification_error() -> None:
    repo = dict(_read_fixture("repo_acme_tool.json"))
    del repo["stargazers_count"]
    fetch = fixture_fetcher({"/repos/acme/tool": repo})
    with pytest.raises(VerificationError):
        fetch_repo_facts("acme", "tool", fetch)


def test_invalid_date_raises_verification_error() -> None:
    repo = dict(_read_fixture("repo_acme_tool.json"))
    repo["created_at"] = "esto-no-es-una-fecha"
    fetch = fixture_fetcher({"/repos/acme/tool": repo})
    with pytest.raises(VerificationError):
        fetch_repo_facts("acme", "tool", fetch)


def test_non_404_org_error_propagates_as_verification_error() -> None:
    def fetch(path: str) -> dict:
        if path == "/orgs/acme":
            raise FetchError(503)
        return fixture_fetcher()(path)

    with pytest.raises(VerificationError, match="503"):
        fetch_repo_facts("acme", "tool", fetch)



def test_facts_owner_and_name_are_canonical_from_response() -> None:
    # Un repo transferido/renombrado responde con su dueño y
    # nombre actuales; RepoFacts debe reflejarlos, no los argumentos.
    repo = dict(_read_fixture("repo_acme_tool.json"))
    repo["owner"] = {"login": "tercero", "type": "User"}
    repo["name"] = "renombrado"
    fetch = fixture_fetcher({"/repos/acme/tool": repo})
    f = fetch_repo_facts("acme", "tool", fetch)
    assert f.owner == "tercero"
    assert f.name == "renombrado"


def test_default_branch_with_hash_is_percent_encoded() -> None:
    # Una rama con '#' truncaría la ruta si no se escapa.
    repo = dict(_read_fixture("repo_acme_tool.json"))
    repo["default_branch"] = "rel#1"
    requested: list[str] = []
    base = fixture_fetcher(
        {"/repos/acme/tool": repo, "/repos/acme/tool/commits/rel%231": _read_fixture("commit_acme_tool.json")}
    )

    def recording(path: str) -> dict:
        requested.append(path)
        return base(path)

    fetch_repo_facts("acme", "tool", recording)
    assert "/repos/acme/tool/commits/rel%231" in requested


def test_fork_compare_segments_are_percent_encoded() -> None:
    fork = dict(_read_fixture("repo_x_fork.json"))
    fork["default_branch"] = "rel#1"
    fork["parent"] = {
        "full_name": "upstream/original",
        "owner": {"login": "upstream"},
        "default_branch": "p#2",
    }
    requested: list[str] = []
    base = fixture_fetcher(
        {
            "/repos/x/fork": fork,
            "/repos/x/fork/commits/rel%231": _read_fixture("commit_x_fork.json"),
            "/repos/x/fork/compare/upstream:p%232...x:rel%231": _read_fixture("compare_x_fork.json"),
        }
    )

    def recording(path: str) -> dict:
        requested.append(path)
        return base(path)

    fetch_repo_facts("x", "fork", recording)
    assert "/repos/x/fork/compare/upstream:p%232...x:rel%231" in requested


def test_org_response_not_an_object_raises_verification_error() -> None:
    # /orgs/{o} devuelve una lista en vez de un objeto.
    fetch = fixture_fetcher({"/orgs/acme": ["no", "es", "objeto"]})  # type: ignore[dict-item]
    with pytest.raises(VerificationError):
        fetch_repo_facts("acme", "tool", fetch)


# --- sha_in_default_branch ----------------------------------------------

_HEAD = "a" * 40
_OTHER = "b" * 40


def _compare_fetch(response: Any, requested: list[str] | None = None) -> Fetcher:
    def fetch(path: str) -> dict:
        if requested is not None:
            requested.append(path)
        if isinstance(response, Exception):
            raise response
        return response

    return fetch


def test_sha_equal_to_head_is_true_without_api_call() -> None:
    requested: list[str] = []
    assert sha_in_default_branch("acme", "tool", _HEAD, _HEAD, _compare_fetch({}, requested))
    assert requested == []


@pytest.mark.parametrize("status", ["ahead", "identical"])
def test_sha_ancestor_of_head_is_true(status: str) -> None:
    requested: list[str] = []
    fetch = _compare_fetch({"status": status}, requested)
    assert sha_in_default_branch("acme", "tool", _OTHER, _HEAD, fetch) is True
    assert requested == [f"/repos/acme/tool/compare/{_OTHER}...{_HEAD}"]


@pytest.mark.parametrize("status", ["behind", "diverged"])
def test_sha_not_ancestor_is_false(status: str) -> None:
    fetch = _compare_fetch({"status": status})
    assert sha_in_default_branch("acme", "tool", _OTHER, _HEAD, fetch) is False


def test_sha_compare_fetch_error_raises_verification_error() -> None:
    with pytest.raises(VerificationError, match="403"):
        sha_in_default_branch("acme", "tool", _OTHER, _HEAD, _compare_fetch(FetchError(403)))


@pytest.mark.parametrize("response", [{}, {"status": 3}, {"status": "raro"}, ["ahead"]])
def test_sha_compare_malformed_response_raises_verification_error(response: Any) -> None:
    with pytest.raises(VerificationError):
        sha_in_default_branch("acme", "tool", _OTHER, _HEAD, _compare_fetch(response))


# --- http_fetcher: construcción de la petición y mapeo de errores -------
# Estos tests no tocan la red: monkeypatchean urllib.request.urlopen.


class _FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def read(self) -> bytes:
        return self._payload


class _BreakingResponse:
    """Como `_FakeResponse`, pero `read()` falla a mitad de la respuesta."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def __enter__(self) -> "_BreakingResponse":
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def read(self) -> bytes:
        raise self._exc


def _http_error(code: int, msg: str) -> urllib.error.HTTPError:
    """Construye un `HTTPError` de prueba sin dejar un `ResourceWarning`.

    `HTTPError` hereda de `tempfile._TemporaryFileWrapper` (vía
    `urllib.response.addinfourl`) incluso con `fp=None`: internamente abre
    un `io.BytesIO()` que, si no se cierra explícitamente, dispara un
    `ResourceWarning` en su `__del__` cuando el recolector de basura lo
    atrapa -- lo que `pytest -W error` convierte en un fallo intermitente
    de CUALQUIER otro test que se ejecute después (el GC no es inmediato).
    `.close()` no afecta `.code`/`.msg`, que ya quedaron fijados.
    """
    error = urllib.error.HTTPError("https://example.com", code, msg, {}, None)
    error.close()
    return error


def test_http_fetcher_builds_url_and_timeout_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(request: Any, timeout: float) -> _FakeResponse:
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["headers"] = dict(request.header_items())
        return _FakeResponse(b"{}")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    fetch = http_fetcher(None)
    fetch("/repos/acme/tool")

    assert captured["url"] == "https://api.github.com/repos/acme/tool"
    assert captured["timeout"] is not None
    assert "Authorization" not in captured["headers"]


def test_http_fetcher_adds_bearer_authorization_with_token(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(request: Any, timeout: float) -> _FakeResponse:
        captured["headers"] = dict(request.header_items())
        return _FakeResponse(b"{}")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    fetch = http_fetcher("secret-token")
    fetch("/repos/acme/tool")

    assert captured["headers"]["Authorization"] == "Bearer secret-token"


def test_http_fetcher_returns_parsed_json(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request: Any, timeout: float) -> _FakeResponse:
        return _FakeResponse(b'{"ok": true}')

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    fetch = http_fetcher(None)
    assert fetch("/repos/acme/tool") == {"ok": True}


def test_http_fetcher_wraps_http_error_with_status(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request: Any, timeout: float) -> _FakeResponse:
        raise _http_error(403, "Forbidden")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    fetch = http_fetcher(None)
    with pytest.raises(FetchError) as excinfo:
        fetch("/repos/acme/tool")
    assert excinfo.value.status == 403


def test_http_fetcher_wraps_url_error_as_status_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request: Any, timeout: float) -> _FakeResponse:
        raise urllib.error.URLError("red caída")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    fetch = http_fetcher(None)
    with pytest.raises(FetchError) as excinfo:
        fetch("/repos/acme/tool")
    assert excinfo.value.status == 0


def test_http_fetcher_wraps_timeout_as_status_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request: Any, timeout: float) -> _FakeResponse:
        raise TimeoutError("se agotó el tiempo")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    fetch = http_fetcher(None)
    with pytest.raises(FetchError) as excinfo:
        fetch("/repos/acme/tool")
    assert excinfo.value.status == 0


def test_http_fetcher_wraps_connection_reset_as_status_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Una conexión que se cae a mitad de response.read() no es un
    # HTTPError/URLError/TimeoutError: es un OSError (o una subclase de
    # http.client.HTTPException) que antes escapaba sin envolver.
    def fake_urlopen(request: Any, timeout: float) -> _BreakingResponse:
        return _BreakingResponse(ConnectionResetError("conexión perdida"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    fetch = http_fetcher(None)
    with pytest.raises(FetchError) as excinfo:
        fetch("/repos/acme/tool")
    assert excinfo.value.status == 0


def test_http_fetcher_wraps_incomplete_read_as_status_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import http.client

    def fake_urlopen(request: Any, timeout: float) -> _BreakingResponse:
        return _BreakingResponse(http.client.IncompleteRead(b""))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    fetch = http_fetcher(None)
    with pytest.raises(FetchError) as excinfo:
        fetch("/repos/acme/tool")
    assert excinfo.value.status == 0


def test_http_fetcher_wraps_invalid_json_as_status_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request: Any, timeout: float) -> _FakeResponse:
        return _FakeResponse(b"esto no es json")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    fetch = http_fetcher(None)
    with pytest.raises(FetchError) as excinfo:
        fetch("/repos/acme/tool")
    assert excinfo.value.status == 0


# --- parse_repo_url valida owner y repo -----------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/é/x",
        "https://github.com/" + "a" * 40 + "/x",
        "https://github.com/a_b/x",
        "https://github.com/a/b c",
        "https://github.com/a/b%20c",
        "https://github.com/a/" + "b" * 101,
        "https://github.com/a/.git",
    ],
)
def test_parse_invalid_owner_or_repo_is_none(url: str) -> None:
    assert parse_repo_url(url) is None


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/a/b.git", ("a", "b")),
        ("https://github.com/my-org/my-repo", ("my-org", "my-repo")),
        ("https://github.com/a/my.repo_name-1", ("a", "my.repo_name-1")),
        ("https://github.com/" + "a" * 39 + "/x", ("a" * 39, "x")),
        ("https://github.com/a/" + "b" * 100, ("a", "b" * 100)),
    ],
)
def test_parse_valid_owner_and_repo(url: str, expected: tuple[str, str]) -> None:
    assert parse_repo_url(url) == expected
