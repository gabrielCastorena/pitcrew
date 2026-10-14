"""Pipeline del verificador: encadena fuente GitHub, extracción de tar.gz,
escaneo de skills y rúbrica en `verify_item`.

`Deps` reúne todo lo que el verificador (y el aplicador) necesita del mundo exterior -- red, reloj, subprocesos, tty
y HOME -- para que los tests puedan sustituirlo por completo y ningún
test real toque la red ni el `~/.claude` de la máquina.
"""

from __future__ import annotations

import http.client
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from setup_architect.archive import ArchiveError, safe_extract
from setup_architect.config import Config
from setup_architect.models import (
    Candidate,
    CopySkill,
    Flag,
    Level,
    PlanItem,
    RepoFacts,
    TrustCard,
)
from setup_architect.verify.github import (
    Fetcher,
    FetchError,
    VerificationError,
    fetch_repo_facts,
    http_fetcher,
    parse_repo_url,
    sha_in_default_branch,
)
from setup_architect.verify.rubric import evaluate
from setup_architect.verify.skill_scan import scan_skill_dir

ArchiveFetcher = Callable[[str, str, str], bytes]

_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
_ARCHIVE_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class Deps:
    """Dependencias inyectables del verificador y, luego, del aplicador."""

    fetch: Fetcher
    fetch_archive: ArchiveFetcher
    now: Callable[[], datetime]
    run_cmd: Callable[[list[str]], int]
    tty_path: str
    home: Path


def default_deps() -> Deps:
    """Construye las `Deps` reales (red, reloj y tty del sistema).

    Ningún test llama a esto contra la red: solo se prueba que el
    objeto se construye con los tipos correctos.
    """
    return Deps(
        fetch=http_fetcher(os.environ.get("GITHUB_TOKEN")),
        fetch_archive=_codeload_fetcher(),
        now=lambda: datetime.now(timezone.utc),
        run_cmd=_run_quiet,
        tty_path="/dev/tty",
        home=Path.home(),
    )


def _run_quiet(argv: list[str]) -> int:
    """Ejecuta `argv` sin heredar la E/S y devuelve su código de salida.

    La salida de `claude mcp get` puede incluir valores de entorno de un
    servidor existente: no debe llegar al stdout de `sarch`, que lee el LLM
    que lo lanzó.
    """
    return subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode


def _codeload_fetcher() -> ArchiveFetcher:
    def fetch_archive(owner: str, name: str, sha: str) -> bytes:
        url = f"https://codeload.github.com/{owner}/{name}/tar.gz/{sha}"
        request = urllib.request.Request(url)
        try:
            with urllib.request.urlopen(
                request, timeout=_ARCHIVE_TIMEOUT_SECONDS
            ) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            raise FetchError(exc.code) from exc
        except urllib.error.URLError as exc:
            raise FetchError(0) from exc
        except TimeoutError as exc:
            raise FetchError(0) from exc
        except (OSError, http.client.HTTPException) as exc:
            # Igual que en github.http_fetcher: una conexión que se cae a
            # mitad de response.read() no debe escapar sin envolver.
            raise FetchError(0) from exc

    return fetch_archive


def verify_item(item: PlanItem, deps: Deps, config: Config) -> TrustCard:
    """Verifica un `PlanItem` completo y devuelve su `TrustCard`.

    Encadena `parse_repo_url` (fuente no soportada -> `facts=None`),
    `fetch_repo_facts` (`VerificationError` -> `error`) y, por cada
    `CopySkill` en sus operaciones, la descarga del tar.gz en el SHA
    exacto de la operación y el escaneo de su `subdir`. Usa `deps.now()`
    como reloj para `evaluate`.

    Procesa TODAS las operaciones antes de decidir: un veto detectado en
    una `CopySkill` (p. ej. `repo_mismatch`) nunca se descarta solo porque
    otra operación, o `fetch_repo_facts`, falló después con un `error`.
    `Revisar` es confirmable por el usuario y `Bloqueado` no, así que un
    `error` nunca puede "lavar" un veto ya detectado -- ni siquiera cuando
    el propio `fetch_repo_facts` es el que falla, porque `repo_mismatch`
    solo compara texto (`op.repo` contra la URL) y no necesita `RepoFacts`.
    """
    candidate = item.candidate
    now = deps.now()

    parsed = parse_repo_url(candidate.source_url)
    if parsed is None:
        # Una fuente que no es GitHub no puede respaldar un copy_skill: la
        # operación instalaría código de un repo que nadie verificó.
        mismatch_flags = [
            Flag(
                code="repo_mismatch",
                detail=(
                    f"la fuente {candidate.source_url} no es un repo de GitHub, "
                    f"pero la operación instalaría {op.repo}"
                ),
                veto=True,
            )
            for op in item.operations
            if isinstance(op, CopySkill)
        ]
        if mismatch_flags:
            return _blocked_without_facts(candidate, mismatch_flags)
        return evaluate(candidate, item.operations, None, [], config, now)

    owner, name = parsed

    facts = None
    fetch_error: str | None = None
    try:
        facts = fetch_repo_facts(owner, name, deps.fetch)
    except VerificationError as exc:
        fetch_error = str(exc)

    extra_flags: list[Flag] = []
    op_error: str | None = None
    for op in item.operations:
        if not isinstance(op, CopySkill):
            continue
        flags, error = _verify_copy_skill(op, owner, name, facts, deps)
        extra_flags.extend(flags)
        if error is not None and op_error is None:
            op_error = error

    # Con `facts`, los vetos que calcula `evaluate` desde RepoFacts
    # (typosquat, estrellas infladas, fork casi idéntico) y los de las
    # operaciones se evalúan SIEMPRE antes que cualquier error: un error de
    # operación o de red nunca puede degradar un Bloqueado a un Revisar
    # confirmable.
    facts_card: TrustCard | None = None
    if facts is not None:
        facts_card = evaluate(candidate, item.operations, facts, extra_flags, config, now)
        if facts_card.level == Level.BLOQUEADO:
            return facts_card
    elif any(flag.veto for flag in extra_flags):
        # `evaluate` nunca devuelve Bloqueado con facts=None (su regla 2
        # decide "fuente no soportada" antes de llegar a los vetos), así
        # que este caso -- un veto de operación cuando además falló
        # fetch_repo_facts -- se construye directamente aquí.
        return _blocked_without_facts(candidate, extra_flags)

    error = fetch_error if fetch_error is not None else op_error
    if error is not None:
        return evaluate(candidate, item.operations, None, [], config, now, error=error)

    assert facts_card is not None  # facts is None implica fetch_error, ya devuelto
    return facts_card


def _blocked_without_facts(candidate: Candidate, flags: list[Flag]) -> TrustCard:
    """`Bloqueado` directo cuando un veto de operación decide sin `RepoFacts`.

    Mismo formato que la rama de vetos de `evaluate` (un `reason` por cada
    veto, todas las banderas, sin `pinned_ref` ni popularidad porque no hay
    `RepoFacts` reales de los que sacarlos).
    """
    vetoes = [flag for flag in flags if flag.veto]
    return TrustCard(
        candidate=candidate.name,
        level=Level.BLOQUEADO,
        reasons=[f"{flag.code}: {flag.detail}" for flag in vetoes],
        flags=flags,
        pinned_ref=None,
        popularity=0,
    )


def _verify_copy_skill(
    op: CopySkill, owner: str, name: str, facts: RepoFacts | None, deps: Deps
) -> tuple[list[Flag], str | None]:
    """Verifica una operación `copy_skill` contra el repo ya verificado.

    Devuelve `(flags, None)` si pudo decidir (incluidos los vetos
    `repo_mismatch` y `unsafe_archive`), o `([], mensaje)` si no se pudo
    verificar en absoluto (SHA con forma inválida, descarga fallida o
    subdirectorio ausente).
    """
    if op.repo.lower() != f"{owner}/{name}".lower():
        return (
            [
                Flag(
                    code="repo_mismatch",
                    detail=(
                        f"se verificó {owner}/{name}, pero la operación "
                        f"instalaría {op.repo}"
                    ),
                    veto=True,
                )
            ],
            None,
        )

    if not _SHA_RE.fullmatch(op.sha):
        return ([], f"sha inválido en copy_skill: {op.sha!r}")

    if facts is not None:
        # GitHub sirve por el repo padre commits que solo existen en un
        # fork: el sha debe ser alcanzable desde la rama por defecto.
        try:
            in_repo = sha_in_default_branch(owner, name, op.sha, facts.head_sha, deps.fetch)
        except VerificationError as exc:
            return ([], f"no se pudo comprobar que el sha pertenece a {owner}/{name}: {exc}")
        if not in_repo:
            return (
                [
                    Flag(
                        code="sha_not_in_repo",
                        detail=(
                            f"el commit {op.sha} no pertenece a la rama por defecto "
                            f"de {owner}/{name}"
                        ),
                        veto=True,
                    )
                ],
                None,
            )

    try:
        archive = deps.fetch_archive(owner, name, op.sha)
    except FetchError as exc:
        return ([], f"no se pudo descargar el archivo de {owner}/{name}: {exc}")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        try:
            safe_extract(archive, tmp_path)
        except ArchiveError as exc:
            return (
                [Flag(code="unsafe_archive", detail=str(exc), veto=True)],
                None,
            )

        root = _find_root_dir(tmp_path)
        if root is None:
            return ([], "el archivo descargado no tiene un directorio raíz único")

        try:
            skill_dir = (root / Path(op.subdir)).resolve()
            inside = skill_dir.is_relative_to(root.resolve())
        except ValueError:
            inside = False
        if not inside:
            return (
                [
                    Flag(
                        code="unsafe_archive",
                        detail=f"subdir inválido: {op.subdir!r}",
                        veto=True,
                    )
                ],
                None,
            )

        if not skill_dir.is_dir():
            return ([], f"no existe el subdirectorio {op.subdir!r} en {op.repo}")

        return (scan_skill_dir(skill_dir), None)


def _find_root_dir(tmp_path: Path) -> Path | None:
    """El único directorio de primer nivel de un tarball extraído de GitHub."""
    dirs = [entry for entry in tmp_path.iterdir() if entry.is_dir()]
    if len(dirs) == 1:
        return dirs[0]
    return None
