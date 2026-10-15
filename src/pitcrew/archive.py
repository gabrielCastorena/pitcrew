"""Extracción segura de archivos `tar.gz`.

`safe_extract` es una barrera de seguridad que se usa dos veces: el
verificador la llama para inspeccionar una skill antes de aprobarla, y
el aplicador la vuelve a llamar justo antes de
copiarla a `.claude/skills/`. Por eso valida TODOS los miembros antes
de escribir ninguno y usa además el filtro `data` de `tarfile` como
segunda capa. La extracción ocurre en un directorio temporal dentro de
`dest` que solo se vuelca a `dest` cuando terminó completa: si un solo
miembro es inválido o la extracción falla a mitad, nada queda en `dest`.
"""

from __future__ import annotations

import io
import os
import shutil
import tarfile
import tempfile
from pathlib import Path

_ALLOWED_TYPES = frozenset({tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE})


class ArchiveError(Exception):
    """Error al extraer un `tar.gz`: miembro inválido o archivo corrupto."""


def safe_extract(archive: bytes, dest: Path) -> None:
    """Extrae `archive` (bytes de un `tar.gz`) dentro de `dest`.

    Rechaza con `ArchiveError`, sin escribir nada en `dest`: rutas
    absolutas, miembros con `..` que escapan de `dest`, symlinks,
    hardlinks y dispositivos/FIFOs. Un `tar.gz` corrupto o cuyos bytes
    no son gzip también cuenta como `ArchiveError`, igual que una
    extracción que falla a mitad (p. ej. un miembro que es archivo y
    después se usa como directorio) o que pisaría una entrada que ya
    existe en `dest`. En todos esos casos `dest` queda como estaba.
    """
    try:
        tar = tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz")
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise ArchiveError(f"archivo tar.gz inválido: {exc}") from exc

    with tar:
        try:
            members = tar.getmembers()
        except (tarfile.TarError, OSError, EOFError) as exc:
            raise ArchiveError(f"archivo tar.gz inválido: {exc}") from exc

        for member in members:
            _validate_member(member, dest)

        created_dest = not dest.exists()
        try:
            dest.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix=".pitcrew-extract-", dir=dest))
        except OSError as exc:
            raise ArchiveError(
                f"no se pudo preparar el destino: {exc.strerror}"
            ) from exc

        try:
            try:
                tar.extractall(staging, members=members, filter="data")
            except tarfile.FilterError as exc:
                raise ArchiveError(f"miembro rechazado por el filtro: {exc}") from exc
            except (tarfile.TarError, OSError, EOFError) as exc:
                raise ArchiveError(f"no se pudo extraer el archivo: {exc}") from exc
            _publish(staging, dest)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            if created_dest:
                _rmdir_quietly(dest)
            raise
        shutil.rmtree(staging, ignore_errors=True)


def _publish(staging: Path, dest: Path) -> None:
    """Mueve lo extraído de `staging` a `dest`, sin pisar nada existente."""
    try:
        entries = sorted(staging.iterdir())
        for entry in entries:
            if os.path.lexists(dest / entry.name):
                raise ArchiveError(f"el destino ya contiene {entry.name!r}")
        moved: list[Path] = []
        try:
            for entry in entries:
                target = dest / entry.name
                os.rename(entry, target)
                moved.append(target)
        except OSError:
            for target in moved:
                _remove_quietly(target)
            raise
    except OSError as exc:
        raise ArchiveError(
            f"no se pudo mover lo extraído al destino: {exc.strerror}"
        ) from exc


def _remove_quietly(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            path.unlink()
        except OSError:
            pass


def _rmdir_quietly(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        pass


def _validate_member(member: tarfile.TarInfo, dest: Path) -> None:
    if member.issym() or member.islnk():
        raise ArchiveError(f"miembro de tipo enlace rechazado: {member.name}")
    if member.isdev():
        raise ArchiveError(
            f"miembro de tipo dispositivo/FIFO rechazado: {member.name}"
        )
    if member.type not in _ALLOWED_TYPES:
        raise ArchiveError(f"tipo de miembro no permitido: {member.name}")

    target = (dest / member.name).resolve()
    try:
        target.relative_to(dest.resolve())
    except ValueError:
        raise ArchiveError(
            f"ruta fuera del destino rechazada: {member.name}"
        ) from None
