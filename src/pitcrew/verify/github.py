"""Fuente GitHub del verificador.

Obtiene `RepoFacts` crudos de un repositorio a partir de la API de
GitHub, a través de un `Fetcher` inyectable: en producción golpea
`https://api.github.com` por HTTP (`http_fetcher`), en los tests
responde desde fixtures grabadas. Así el verificador nunca toca la red
en pruebas.
"""

from __future__ import annotations

import http.client
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Callable

from pitcrew.models import RepoFacts

_API_BASE = "https://api.github.com"
_TIMEOUT_SECONDS = 10

Fetcher = Callable[[str], dict]

_OWNER_RE = re.compile(r"[A-Za-z0-9-]{1,39}")
_REPO_RE = re.compile(r"[A-Za-z0-9._-]{1,100}")


class VerificationError(Exception):
    """Error al verificar un repositorio: red caída, HTTP o forma inesperada."""


class FetchError(Exception):
    """Error de red o HTTP al pedir una ruta de la API de GitHub."""

    def __init__(self, status: int) -> None:
        super().__init__(f"error HTTP {status}")
        self.status = status


def parse_repo_url(url: str) -> tuple[str, str] | None:
    """Extrae `(owner, name)` de una URL de GitHub, o `None` si no aplica.

    Acepta `https://github.com/o/n`, con `.git`, con barra final o con
    subrutas (p. ej. `/tree/...`). Cualquier host que no sea exactamente
    `github.com` (incluidos subdominios como `gist.github.com` o
    imitaciones como `github.com.evil.io`) devuelve `None`, igual que una
    URL sin owner/name. Owner y repo deben ser nombres válidos de GitHub
    (owner `[A-Za-z0-9-]{1,39}`, repo `[A-Za-z0-9._-]{1,100}` tras quitar
    `.git`); si no, también devuelve `None` (fuente no soportada).
    """
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    if parsed.hostname is None or parsed.hostname.lower() != "github.com":
        return None
    parts = [segment for segment in parsed.path.split("/") if segment]
    if len(parts) < 2:
        return None
    owner, name = parts[0], parts[1]
    if name.endswith(".git"):
        name = name[: -len(".git")]
    if _OWNER_RE.fullmatch(owner) is None or _REPO_RE.fullmatch(name) is None:
        return None
    return (owner, name)


# --- helpers de acceso tipado a JSON crudo ------------------------------
# Cualquier forma inesperada (clave faltante, tipo incorrecto, fecha
# inválida) se traduce a KeyError/ValueError, que fetch_repo_facts
# convierte en VerificationError sin dejar escapar el error crudo.


def _str(data: dict[str, Any], key: str) -> str:
    value = data[key]
    if not isinstance(value, str):
        raise ValueError(f"se esperaba str en '{key}'")
    return value


def _optional_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"se esperaba str o null en '{key}'")
    return value


def _dict(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data[key]
    if not isinstance(value, dict):
        raise ValueError(f"se esperaba un objeto en '{key}'")
    return value


def _int(data: dict[str, Any], key: str) -> int:
    value = data[key]
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"se esperaba int en '{key}'")
    return value


def _datetime(data: dict[str, Any], key: str) -> datetime:
    value = _str(data, key)
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"fecha inválida en '{key}': {value!r}") from exc


def _q(segment: str) -> str:
    """Escapa un segmento de ruta de la API (sin dejar `/` ni `#` sin escapar)."""
    return urllib.parse.quote(segment, safe="")


def fetch_repo_facts(owner: str, name: str, fetch: Fetcher) -> RepoFacts:
    """Construye `RepoFacts` para `owner/name` usando `fetch` por ruta.

    Llama a `/repos/{owner}/{name}` para los datos del repo, a
    `/repos/{owner}/{name}/commits/{default_branch}` para `head_sha`, a
    `/orgs/{owner}` solo si el dueño es una organización (un 404 ahí
    significa `owner_verified=False`) y, solo si el repo es un fork, a
    `/repos/{owner}/{name}/compare/{parent_owner}:{parent_branch}...{owner}:{branch}`
    para `ahead_by`. La base se califica con el dueño del padre porque un
    fork puede tener un branch por defecto con el mismo nombre que el del
    padre: sin calificar, se compararía el branch contra sí mismo y
    `ahead_by` daría 0 siempre.

    Lanza `VerificationError` si `fetch` falla (el mensaje incluye el
    código de estado) o si alguna respuesta tiene forma inesperada.
    """
    try:
        repo = fetch(f"/repos/{_q(owner)}/{_q(name)}")
        default_branch = _str(repo, "default_branch")

        commit = fetch(f"/repos/{_q(owner)}/{_q(name)}/commits/{_q(default_branch)}")
        head_sha = _str(commit, "sha")

        # Los nombres canónicos salen de la respuesta, no de los argumentos:
        # un repo transferido o renombrado (GitHub redirige) no puede pasar
        # por oficial gracias a su URL vieja.
        owner_info = _dict(repo, "owner")
        canonical_owner = _str(owner_info, "login")
        canonical_name = _str(repo, "name")
        owner_verified = False
        if _str(owner_info, "type") == "Organization":
            try:
                org = fetch(f"/orgs/{_q(canonical_owner)}")
            except FetchError as exc:
                if exc.status != 404:
                    raise
            else:
                if not isinstance(org, dict):
                    raise ValueError("se esperaba un objeto en la respuesta de /orgs")
                owner_verified = bool(org.get("is_verified", False))

        is_fork = bool(repo["fork"])
        parent: str | None = None
        ahead_by: int | None = None
        if is_fork:
            parent_info = _dict(repo, "parent")
            parent = _str(parent_info, "full_name")
            parent_branch = _str(parent_info, "default_branch")
            parent_owner = _str(_dict(parent_info, "owner"), "login")
            compare = fetch(
                f"/repos/{_q(owner)}/{_q(name)}/compare/"
                f"{_q(parent_owner)}:{_q(parent_branch)}"
                f"...{_q(canonical_owner)}:{_q(default_branch)}"
            )
            ahead_by = _int(compare, "ahead_by")

        return RepoFacts(
            owner=canonical_owner,
            name=canonical_name,
            created_at=_datetime(repo, "created_at"),
            pushed_at=_datetime(repo, "pushed_at"),
            stars=_int(repo, "stargazers_count"),
            forks=_int(repo, "forks_count"),
            open_issues=_int(repo, "open_issues_count"),
            is_fork=is_fork,
            parent=parent,
            ahead_by=ahead_by,
            owner_verified=owner_verified,
            head_sha=head_sha,
            description=_optional_str(repo, "description"),
        )
    except FetchError as exc:
        raise VerificationError(
            f"la API de GitHub respondió con el código {exc.status}"
        ) from exc
    except (KeyError, TypeError, ValueError) as exc:
        raise VerificationError(
            f"respuesta de GitHub con forma inesperada: {exc}"
        ) from exc


def sha_in_default_branch(
    owner: str, name: str, sha: str, head_sha: str, fetch: Fetcher
) -> bool:
    """Indica si `sha` es la cabeza de la rama por defecto o un ancestro suyo.

    GitHub sirve por el repo padre commits que solo existen en un fork, así
    que "el sha existe en owner/name" no prueba pertenencia. Aquí solo
    cuenta lo alcanzable desde la rama por defecto (`head_sha`): si `sha`
    es la cabeza no se llama a la API; si no, se compara `sha...head_sha`
    y solo `ahead` o `identical` (es decir, `behind_by == 0`) es `True`.
    `behind` y `diverged` son `False`. Tags y otras ramas no cuentan.

    Lanza `VerificationError` si la API falla o la respuesta es inesperada.
    """
    if sha == head_sha:
        return True
    try:
        compare = fetch(
            f"/repos/{_q(owner)}/{_q(name)}/compare/{_q(sha)}...{_q(head_sha)}"
        )
        if not isinstance(compare, dict):
            raise ValueError("se esperaba un objeto en la respuesta de compare")
        status = _str(compare, "status")
    except FetchError as exc:
        raise VerificationError(
            f"la API de GitHub respondió con el código {exc.status}"
        ) from exc
    except (KeyError, TypeError, ValueError) as exc:
        raise VerificationError(
            f"respuesta de GitHub con forma inesperada: {exc}"
        ) from exc
    if status in ("ahead", "identical"):
        return True
    if status in ("behind", "diverged"):
        return False
    raise VerificationError(f"estado de comparación desconocido: {status!r}")


def http_fetcher(token: str | None) -> Fetcher:
    """Construye un `Fetcher` real contra `https://api.github.com`.

    Agrega `Authorization: Bearer <token>` solo si hay token y usa un
    timeout explícito. Convierte cualquier error de red, de HTTP o de
    JSON inválido en `FetchError`, con estado `0` cuando no hay código
    HTTP (red caída, DNS, timeout).
    """

    def fetch(path: str) -> dict:
        headers = {"Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(f"{_API_BASE}{path}", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise FetchError(exc.code) from exc
        except urllib.error.URLError as exc:
            raise FetchError(0) from exc
        except TimeoutError as exc:
            raise FetchError(0) from exc
        except (OSError, http.client.HTTPException) as exc:
            # Una conexión que se cae a mitad de response.read() (p. ej.
            # ConnectionResetError u otro OSError que no sea ya un
            # URLError/TimeoutError) o una respuesta HTTP mal formada
            # (http.client.HTTPException, como IncompleteRead) tampoco
            # debe escapar sin envolver: un fallo de red siempre es
            # FetchError, nunca una excepción cruda.
            raise FetchError(0) from exc
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise FetchError(0) from exc

    return fetch
