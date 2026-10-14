"""Operaciones tipadas del aplicador: cómo se prepara y se ejecuta cada una.

Este módulo es el único que escribe en disco y registra servidores MCP.
El plan que ejecuta lo arma un LLM a partir de texto de internet, así que
cada campo de cada operación se trata como entrada hostil: se valida su
forma antes de usarlo y, ante cualquier duda, error o dato inesperado, se
lanza `OpError` sin escribir ni ejecutar nada.

`prepare` calcula cómo deshacer una operación (y respalda el archivo que
se va a pisar) y `execute` la realiza. Los dos validan lo mismo y cada
uno resuelve las rutas por su cuenta: `execute` nunca confía en lo que
`prepare` vio. El valor de un secreto jamás aparece en un `OpError` ni
en un `Undo`.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import stat
import tempfile
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Literal

from setup_architect.apply.paths import PathError, resolve_target
from setup_architect.archive import ArchiveError, safe_extract
from setup_architect.forbidden import forbidden_component
from setup_architect.models import (
    AddMcp,
    CopySkill,
    ManualStep,
    Operation,
    SetEnv,
    WriteFile,
)
from setup_architect.verify.skill_scan import (
    content_vetoes,
    frontmatter_veto,
    scan_skill_dir,
)

_SHA_RE = re.compile(r"[0-9a-fA-F]{40}")
_DEST_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_MCP_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_PACKAGE_NAME_RE = re.compile(r"(?:@[A-Za-z0-9][A-Za-z0-9._-]*/)?[A-Za-z0-9][A-Za-z0-9._-]*")
# Versión exacta, semver estricto: MAYOR.MENOR.PARCHE sin ceros a la
# izquierda, con prerelease (`-rc.1`) y build (`+build`) opcionales. `1` o
# `1.2` son rangos en npm, y lo que no es semver se resuelve como dist-tag,
# que el dueño del paquete puede reapuntar. `[0-9]` y no `\d`, que acepta
# dígitos Unicode.
_NUM = r"(?:0|[1-9][0-9]*)"
_PRE = rf"(?:{_NUM}|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
_SEMVER = (
    rf"{_NUM}\.{_NUM}\.{_NUM}"
    rf"(?:-{_PRE}(?:\.{_PRE})*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
_SEMVER_RE = re.compile(_SEMVER)
# Los lanzadores de Python admiten además la post-release de PyPI.
_PYPI_VERSION_RE = re.compile(rf"{_SEMVER}|{_NUM}\.{_NUM}\.{_NUM}\.post[0-9]+")
# Topes de node-semver: más allá de ellos una cadena no es versión ni rango
# y npm la trataría como dist-tag.
_MAX_VERSION_LENGTH = 256
_MAX_VERSION_NUMBER = 9007199254740991  # Number.MAX_SAFE_INTEGER
_VERSION_RE: dict[str, re.Pattern[str]] = {
    "npx": _SEMVER_RE,
    "bunx": _SEMVER_RE,
    "uvx": _PYPI_VERSION_RE,
    "pipx": _PYPI_VERSION_RE,
}
_DOCKER_IMAGE_RE = re.compile(r"[a-z0-9][a-z0-9._/:-]*@sha256:[0-9a-f]{64}")
# Opciones de `docker run` admitidas antes de la imagen: lista cerrada.
_DOCKER_FLAGS = frozenset({"-i", "--interactive", "--rm", "--init"})
_DOCKER_ENV_OPTIONS = frozenset({"-e", "--env"})

_SCOPES = ("user", "project")
# Opciones que pueden ir antes del paquete sin cambiar qué se instala ni
# ejecutar otra cosa. `--from` de uvx va seguida del paquete, que es
# entonces el primer argumento que no empieza por `-`.
_RUNNER_OPTIONS: dict[str, frozenset[str]] = {
    "npx": frozenset({"-y", "--yes"}),
    "bunx": frozenset({"-y", "--yes"}),
    "uvx": frozenset({"--from"}),
    "pipx": frozenset(),
}


class OpError(Exception):
    """Una operación no se pudo validar o ejecutar. Mensaje en español."""


@dataclass(frozen=True)
class Undo:
    """Cómo deshacer una operación; es lo que guarda el manifiesto."""

    kind: Literal["restore_file", "delete_path", "mcp_remove", "env_remove", "none"]
    target: str
    backup: str | None
    scope: str | None


@dataclass(frozen=True)
class ApplyContext:
    """Todo lo que una operación necesita del mundo exterior."""

    home: Path
    project: Path | None
    run_cmd: Callable[[list[str]], int]
    backups_dir: Path
    env_file: Path
    archives: dict[str, bytes]
    secret: Callable[[str], str]
    manual: list[ManualStep]


def prepare(op: Operation, ctx: ApplyContext) -> Undo:
    """Valida `op` y devuelve cómo deshacerla, sin realizarla.

    Su único efecto es respaldar en `ctx.backups_dir` el archivo que un
    `WriteFile` va a pisar. Valida lo mismo que `execute`, salvo lo que
    depende del valor de un secreto (que aquí no se pide), para fallar
    antes de que el paso quede registrado.
    """
    if isinstance(op, WriteFile):
        target, _ = _check_write_file(op, ctx)
        if not os.path.lexists(target):
            return Undo("delete_path", str(target), None, None)
        return Undo("restore_file", str(target), str(_backup(target, ctx)), None)
    if isinstance(op, CopySkill):
        with _staged_skill(op, ctx) as (_, dest):
            return Undo("delete_path", str(dest), None, None)
    if isinstance(op, AddMcp):
        _check_add_mcp(op, ctx)
        return Undo("mcp_remove", op.name, None, op.scope)
    if isinstance(op, SetEnv):
        _check_set_env(op, ctx)
        return Undo("env_remove", op.name, None, None)
    if isinstance(op, ManualStep):
        return Undo("none", "", None, None)
    raise OpError("operación no soportada")


def execute(op: Operation, ctx: ApplyContext) -> None:
    """Realiza `op`. Lanza `OpError` si no es válida o si algo falla."""
    if isinstance(op, WriteFile):
        _execute_write_file(op, ctx)
    elif isinstance(op, CopySkill):
        _execute_copy_skill(op, ctx)
    elif isinstance(op, AddMcp):
        _execute_add_mcp(op, ctx)
    elif isinstance(op, SetEnv):
        _execute_set_env(op, ctx)
    elif isinstance(op, ManualStep):
        ctx.manual.append(op)
    else:
        raise OpError("operación no soportada")


# --- utilidades comunes -------------------------------------------------


def _matches(pattern: re.Pattern[str], value: object) -> bool:
    # fullmatch y no match: `$` aceptaría un salto de línea final.
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _resolve(path: str, ctx: ApplyContext) -> Path:
    try:
        return resolve_target(path, ctx.home, ctx.project)
    except PathError as exc:
        raise OpError(f"ruta rechazada: {exc}") from exc


def _os_error(what: str, exc: OSError) -> OpError:
    return OpError(f"{what}: {exc.strerror or type(exc).__name__}")


# --- WriteFile ----------------------------------------------------------


def check_write_file(op: WriteFile, ctx: ApplyContext) -> None:
    """Valida un `WriteFile` como lo harán `prepare` y `execute`, sin efectos.

    Solo mira el disco (`lstat`); no respalda ni escribe nada. Sirve para
    rechazar la operación antes de pedir la aprobación.
    """
    if not isinstance(op, WriteFile):
        raise OpError("operación no soportada")
    _check_write_file(op, ctx)


def _check_write_file(op: WriteFile, ctx: ApplyContext) -> tuple[Path, bytes]:
    target = _resolve(op.path, ctx)
    if not isinstance(op.content, str):
        raise OpError(f"contenido inválido para {op.path!r}")
    try:
        data = op.content.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise OpError(f"el contenido de {op.path!r} no es texto UTF-8 válido") from exc
    is_skill_file = _is_skill_file(target, ctx)
    veto = frontmatter_veto(op.content) if is_skill_file else None
    if veto is not None:
        # Los hooks del frontmatter de una skill ejecutan comandos de shell:
        # escribirlos sería lo mismo que escribir hooks en settings.json. Un
        # frontmatter que no se puede analizar se trata igual.
        raise OpError(f"contenido rechazado para {op.path!r}: {veto}; una skill no puede traer hooks")
    # Como el escáner: en un `.md` se busca además la inyección de shell que
    # Claude Code ejecutaría al invocar la skill.
    is_markdown = target.suffix.lower() == ".md"
    vetoes = content_vetoes(op.content, markdown=is_markdown) if is_skill_file else []
    if vetoes:
        # Lo mismo que vetaría el escáner si la skill llegara por copy_skill.
        raise OpError(
            f"contenido rechazado para {op.path!r}: patrones vetados ({', '.join(vetoes)})"
        )
    try:
        info = os.lstat(target)
    except FileNotFoundError:
        return target, data
    except OSError as exc:
        raise _os_error(f"no se pudo comprobar {op.path!r}", exc) from exc
    if not stat.S_ISREG(info.st_mode):
        raise OpError(f"el destino {op.path!r} existe y no es un archivo regular")
    return target, data


def _is_skill_file(target: Path, ctx: ApplyContext) -> bool:
    """`resolve_target` solo admite dos `CLAUDE.md`; lo demás es de una skill."""
    claude_mds = [os.path.join(os.path.abspath(ctx.home), ".claude", "CLAUDE.md")]
    if ctx.project is not None:
        claude_mds.append(os.path.join(os.path.abspath(ctx.project), "CLAUDE.md"))
    return str(target) not in claude_mds


def _backup(target: Path, ctx: ApplyContext) -> Path:
    """Copia `target` a un archivo nuevo, de nombre único, en `backups_dir`."""
    try:
        source_fd = os.open(str(target), os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(source_fd, "rb") as source:
            data = source.read()
        os.makedirs(ctx.backups_dir, mode=0o700, exist_ok=True)
        backup_fd, backup_name = tempfile.mkstemp(
            dir=ctx.backups_dir, prefix=f"{target.name}.", suffix=".bak"
        )
        with os.fdopen(backup_fd, "wb") as backup:
            backup.write(data)
    except OSError as exc:
        raise _os_error(f"no se pudo respaldar {str(target)!r}", exc) from exc
    return Path(backup_name)


def _execute_write_file(op: WriteFile, ctx: ApplyContext) -> None:
    target, data = _check_write_file(op, ctx)
    try:
        os.makedirs(target.parent, exist_ok=True)
    except OSError as exc:
        raise _os_error(f"no se pudo crear el directorio de {op.path!r}", exc) from exc
    # Con los directorios ya creados, se vuelve a comprobar la ruta entera.
    target, data = _check_write_file(op, ctx)

    # Se escribe en un archivo hermano y se renombra encima: el destino
    # nunca queda a medias y, si fuera un symlink, se reemplaza el enlace
    # en vez de escribir a través de él.
    temp = target.with_name(f".{target.name}.sarch-{secrets.token_hex(8)}.tmp")
    try:
        try:
            mode = stat.S_IMODE(os.lstat(target).st_mode)
        except FileNotFoundError:
            mode = None
        fd = os.open(
            str(temp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o666
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                if mode is not None:
                    os.fchmod(handle.fileno(), mode)
                handle.write(data)
            os.replace(temp, target)
        except BaseException:
            _unlink_quietly(temp)
            raise
    except OSError as exc:
        raise _os_error(f"no se pudo escribir {op.path!r}", exc) from exc


def _unlink_quietly(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


# --- CopySkill ----------------------------------------------------------


@contextmanager
def _staged_skill(op: CopySkill, ctx: ApplyContext) -> Iterator[tuple[Path, Path]]:
    """Valida `op` por completo y entrega `(origen verificado, destino)`.

    El tar.gz se extrae en un directorio temporal, nunca en
    `.claude/skills/`, y ahí se vuelve a escanear. El origen solo existe
    dentro del bloque `with`.
    """
    if not _matches(_DEST_NAME_RE, op.dest_name) or op.dest_name.lower() == "hooks":
        raise OpError(f"nombre de destino inválido para la skill: {op.dest_name!r}")
    if not _matches(_SHA_RE, op.sha):
        raise OpError(f"copy_skill exige un commit SHA de 40 hex, no {op.sha!r}")
    archive = ctx.archives.get(op.sha)
    if not isinstance(archive, bytes):
        raise OpError(f"no hay un archivo descargado para el commit {op.sha}")

    base = ctx.project if ctx.project is not None else ctx.home
    dest = _resolve(str(Path(os.path.abspath(base), ".claude", "skills", op.dest_name)), ctx)
    if os.path.lexists(dest):
        raise OpError(f"el destino de la skill ya existe: {dest}")

    with tempfile.TemporaryDirectory(prefix="sarch-skill-") as tmp:
        staging = Path(tmp) / "extract"
        try:
            safe_extract(archive, staging)
        except ArchiveError as exc:
            raise OpError(f"archivo de la skill rechazado: {exc}") from exc
        source = _skill_source(staging, op)
        vetoes = [flag for flag in scan_skill_dir(source) if flag.veto]
        if vetoes:
            detail = "; ".join(f"{flag.code}: {flag.detail}" for flag in vetoes)
            raise OpError(f"la skill {op.dest_name!r} tiene patrones vetados ({detail})")
        _reject_unsafe_tree(source)
        yield source, dest


def _skill_source(staging: Path, op: CopySkill) -> Path:
    """El `subdir` de la skill dentro del único directorio raíz del tarball.

    Misma lógica que `verify.pipeline._verify_copy_skill`, para instalar
    exactamente el directorio que el verificador escaneó.
    """
    try:
        roots = [entry for entry in staging.iterdir() if entry.is_dir()]
    except OSError as exc:
        raise _os_error("no se pudo leer el archivo extraído", exc) from exc
    if len(roots) != 1:
        raise OpError("el archivo descargado no tiene un directorio raíz único")
    root = roots[0].resolve()

    if not isinstance(op.subdir, str):
        raise OpError(f"subdir inválido: {op.subdir!r}")
    try:
        source = (root / Path(op.subdir)).resolve()
        inside = source.is_relative_to(root)
    except (ValueError, OSError):
        inside = False
    if not inside:
        raise OpError(f"subdir inválido: {op.subdir!r}")
    if not source.is_dir():
        raise OpError(f"no existe el subdirectorio {op.subdir!r} en {op.repo!r}")
    return source


def _reject_unsafe_tree(source: Path) -> None:
    """Segunda barrera sobre el árbol que se va a instalar.

    Solo directorios y archivos regulares (ni symlinks ni nada más), y
    ningún nombre que `write_file` tenga prohibido. `scan_skill_dir` ya
    veta esos nombres con la misma definición, pero solo recorre archivos:
    aquí se rechaza también un directorio prohibido vacío.
    """
    for current, dir_names, file_names in os.walk(source, followlinks=False):
        for name in (*dir_names, *file_names):
            full = os.path.join(current, name)
            relative = os.path.relpath(full, source)
            mode = os.lstat(full).st_mode
            if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise OpError(
                    f"la skill contiene un enlace o archivo especial: {relative!r}"
                )
            if forbidden_component((name,)) is not None:
                raise OpError(f"la skill contiene un nombre prohibido: {relative!r}")


def _execute_copy_skill(op: CopySkill, ctx: ApplyContext) -> None:
    with _staged_skill(op, ctx) as (source, dest):
        try:
            os.makedirs(dest.parent, exist_ok=True)
            # Con los directorios ya creados, se vuelve a comprobar la ruta.
            _resolve(str(dest), ctx)
            # mkdir falla si el destino apareció mientras tanto: así nunca
            # se toca (ni se borra abajo) un directorio que no creamos.
            os.mkdir(dest)
        except FileExistsError as exc:
            raise OpError(f"el destino de la skill ya existe: {dest}") from exc
        except OSError as exc:
            raise _os_error(f"no se pudo crear {dest}", exc) from exc
        try:
            shutil.copytree(source, dest, symlinks=True, dirs_exist_ok=True)
        except BaseException as exc:
            shutil.rmtree(dest, ignore_errors=True)
            if isinstance(exc, (OSError, shutil.Error)):
                raise OpError(f"no se pudo copiar la skill a {dest}") from exc
            raise


# --- AddMcp -------------------------------------------------------------


def _check_add_mcp(op: AddMcp, ctx: ApplyContext) -> None:
    if not _matches(_MCP_NAME_RE, op.name):
        raise OpError(f"nombre de servidor MCP inválido: {op.name!r}")
    if op.scope not in _SCOPES:
        raise OpError(f"scope inválido para el servidor MCP {op.name!r}: {op.scope!r}")
    if op.scope == "project" and ctx.project is None:
        raise OpError(f"el servidor MCP {op.name!r} pide scope project y no hay proyecto")
    if not isinstance(op.env_names, list):
        raise OpError(f"env_names inválido para el servidor MCP {op.name!r}")
    for env_name in op.env_names:
        if not _matches(_ENV_NAME_RE, env_name):
            raise OpError(f"nombre de variable de entorno inválido: {env_name!r}")

    if op.transport == "stdio":
        if op.env_names and op.scope == "project":
            # `claude mcp add --scope project -e NOMBRE=valor` guarda el
            # valor en el `.mcp.json` del proyecto: el secreto iría al repo.
            raise OpError(
                f"el servidor MCP {op.name!r} lleva secretos y pide alcance "
                "project: el valor quedaría escrito en el .mcp.json del repo; "
                "usa el alcance user"
            )
        _check_command(op)
    elif op.transport == "http":
        _check_url(op)
    else:
        raise OpError(f"transporte inválido para el servidor MCP {op.name!r}: {op.transport!r}")


def _check_command(op: AddMcp) -> None:
    command = op.command
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(arg, str) and "\x00" not in arg for arg in command)
        or command[0] == ""
    ):
        raise OpError(f"comando inválido para el servidor MCP {op.name!r}")

    program = command[0]
    if program in _RUNNER_OPTIONS:
        _check_pinned_package(program, command[1:])
    elif program == "docker":
        _check_docker_image(command[1:])
    else:
        # Comparación exacta: `/usr/bin/npx` o `./npx` no son `npx`.
        raise OpError(f"comando no fijable: {program!r}")


def _check_docker_image(args: list[str]) -> None:
    """Exige `run [opciones permitidas] imagen@sha256:<digest> [args...]`.

    El digest debe llevarlo la imagen, no un argumento cualquiera: por eso
    solo se admite una lista cerrada de opciones antes de ella, y el primer
    argumento que no sea una de esas opciones tiene que ser la imagen
    fijada. Lo que sigue a la imagen son argumentos del contenedor.
    """
    if not args or args[0] != "run":
        raise OpError(
            "docker solo se admite como `docker run imagen@sha256:digest`, "
            "con la imagen fijada por digest"
        )
    index = 1
    while index < len(args):
        arg = args[index]
        if arg in _DOCKER_FLAGS:
            index += 1
        elif arg in _DOCKER_ENV_OPTIONS:
            # Solo el nombre: `-e X=Y` metería un valor del plan.
            if index + 1 >= len(args) or not _matches(_ENV_NAME_RE, args[index + 1]):
                raise OpError(
                    f"docker: {arg} debe ir seguido solo del nombre de una variable; "
                    "no se puede comprobar el digest de la imagen"
                )
            index += 2
        elif _matches(_DOCKER_IMAGE_RE, arg):
            return
        elif arg.startswith("-"):
            raise OpError(
                f"opción {arg!r} no admitida antes de la imagen de docker: "
                "no se puede comprobar el digest de la imagen"
            )
        else:
            raise OpError(
                f"la imagen de docker {arg!r} no fija su versión: debe terminar "
                "en @sha256: seguido del digest de 64 hex"
            )
    raise OpError("docker run no indica una imagen fijada por digest")


def _check_pinned_package(program: str, args: list[str]) -> None:
    """Exige `nombre@versión` exacta en el paquete que lanza `program`."""
    if program == "pipx":
        if not args or args[0] != "run":
            raise OpError(
                "pipx solo se admite como `pipx run paquete@versión`, con la versión fijada"
            )
        args = args[1:]

    package: str | None = None
    for arg in args:
        if not arg.startswith("-"):
            package = arg
            break
        if arg not in _RUNNER_OPTIONS[program]:
            raise OpError(
                f"opción {arg!r} no admitida antes del paquete de {program}: "
                "no se puede comprobar qué versión se instalaría"
            )
    if package is None:
        raise OpError(f"{program} no indica un paquete con versión fijada")

    # El `@` de la versión es el último: `@scope/pkg@1.2.3`.
    name, separator, version = package.rpartition("@")
    pinned = (
        separator == "@"
        and _matches(_PACKAGE_NAME_RE, name)
        and _is_exact_version(program, version)
    )
    if not pinned:
        raise OpError(
            f"el paquete {package!r} no fija una versión exacta "
            "(se espera nombre@MAYOR.MENOR.PARCHE; nunca un rango ni latest)"
        )


def _is_exact_version(program: str, version: str) -> bool:
    if len(version) > _MAX_VERSION_LENGTH or not _matches(_VERSION_RE[program], version):
        return False
    core = re.match(r"([0-9]+)\.([0-9]+)\.([0-9]+)", version)
    return core is not None and all(
        int(number) <= _MAX_VERSION_NUMBER for number in core.groups()
    )


def _check_url(op: AddMcp) -> None:
    url = op.url
    valid = (
        isinstance(url, str)
        and (url.startswith("https://") or url.startswith("http://"))
        and url not in ("https://", "http://")
        and not any(ch.isspace() or unicodedata.category(ch).startswith("C") for ch in url)
    )
    if not valid:
        raise OpError(f"URL inválida para el servidor MCP {op.name!r}")


def _execute_add_mcp(op: AddMcp, ctx: ApplyContext) -> None:
    _check_add_mcp(op, ctx)
    if op.transport == "http":
        # Los env_names de un MCP http son informativos: no van al comando.
        argv = ["claude", "mcp", "add", "--transport", "http", "--scope", op.scope, op.name, op.url]
    else:
        env_args: list[str] = []
        for env_name in op.env_names:
            value = _secret(env_name, ctx)
            if "\x00" in value:
                raise OpError(f"el valor de {env_name} contiene un byte nulo")
            env_args += ["-e", f"{env_name}={value}"]
        # El nombre va antes de los `-e`: la opción es variádica y, con el
        # nombre detrás, lo tomaría como otro valor de entorno.
        argv = ["claude", "mcp", "add", "--scope", op.scope, op.name, *env_args, "--", *op.command]

    # El argv lleva secretos (`-e NOMBRE=valor`): nunca entra en un mensaje,
    # ni siquiera a través de la excepción original.
    try:
        code = ctx.run_cmd(argv)
    except Exception as exc:
        # Cualquier excepción: TimeoutExpired o CalledProcessError también
        # citan el argv en su texto.
        raise OpError(
            f"no se pudo ejecutar `claude mcp add` para el servidor {op.name!r} "
            f"({type(exc).__name__})"
        ) from None
    # Solo el entero 0 es éxito: `False`, `None` o `0.0` no lo son.
    if type(code) is not int or code != 0:
        shown = code if type(code) is int else "desconocido"
        raise OpError(
            f"`claude mcp add` falló para el servidor {op.name!r} (código {shown})"
        )


def _secret(name: str, ctx: ApplyContext) -> str:
    try:
        value = ctx.secret(name)
    except Exception as exc:
        # Sin el texto de la excepción: podría citar el valor.
        raise OpError(
            f"no se pudo obtener el secreto {name} ({type(exc).__name__})"
        ) from None
    if not isinstance(value, str):
        raise OpError(f"el valor del secreto {name} no es texto")
    return value


# --- SetEnv -------------------------------------------------------------


def _check_set_env(op: SetEnv, ctx: ApplyContext) -> None:
    """Valida el nombre y que `env_file`, si existe, no lo defina ya."""
    if not _matches(_ENV_NAME_RE, op.name):
        raise OpError(f"nombre de variable de entorno inválido: {op.name!r}")
    try:
        fd = os.open(str(ctx.env_file), os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise _os_error(f"no se pudo leer {ctx.env_file}", exc) from exc
    with os.fdopen(fd, "rb") as handle:
        _check_env_content(handle.fileno(), handle.read(), op.name, ctx)


def _check_env_content(fd: int, content: bytes, name: str, ctx: ApplyContext) -> None:
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        raise OpError(f"{ctx.env_file} no es un archivo regular")
    text = content.decode("utf-8", errors="replace")
    if re.search(rf"^[ \t]*export[ \t]+{re.escape(name)}=", text, re.MULTILINE):
        raise OpError(f"la variable {name} ya está definida en {ctx.env_file}")


def _execute_set_env(op: SetEnv, ctx: ApplyContext) -> None:
    _check_set_env(op, ctx)
    value = _secret(op.name, ctx)
    if any(ch in value for ch in "\n\r\x00"):
        raise OpError(
            f"el valor de {op.name} contiene un salto de línea o un byte nulo"
        )
    quoted = value.replace("'", "'\\''")
    try:
        line = f"export {op.name}='{quoted}'\n".encode("utf-8")
    except UnicodeEncodeError:
        raise OpError(f"el valor de {op.name} no es texto UTF-8 válido") from None

    try:
        os.makedirs(ctx.env_file.parent, mode=0o700, exist_ok=True)
        # 0600 desde la creación: el secreto nunca vive en un archivo más
        # abierto, ni por un instante. O_NOFOLLOW: nunca a través de un enlace.
        fd = os.open(
            str(ctx.env_file),
            os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(fd, "a+b") as handle:
            handle.seek(0)
            content = handle.read()
            _check_env_content(handle.fileno(), content, op.name, ctx)
            if stat.S_IMODE(os.fstat(handle.fileno()).st_mode) != 0o600:
                os.fchmod(handle.fileno(), 0o600)
            if content and not content.endswith(b"\n"):
                line = b"\n" + line
            handle.write(line)
    except OSError as exc:
        raise _os_error(f"no se pudo escribir {ctx.env_file}", exc) from exc
