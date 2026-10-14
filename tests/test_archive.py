"""Tests de extracción segura de tar.gz (archive.py)."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from setup_architect.archive import ArchiveError, safe_extract


def _make_tar(entries: list[tarfile.TarInfo | tuple[str, bytes]]) -> bytes:
    """Construye un tar.gz en memoria a partir de entradas simples.

    Cada entrada es un `(nombre, contenido)` para un archivo regular, o
    un `tarfile.TarInfo` ya armado (para symlinks, hardlinks, etc.).
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for entry in entries:
            if isinstance(entry, tarfile.TarInfo):
                tar.addfile(entry)
            else:
                name, content = entry
                info = tarfile.TarInfo(name=name)
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def tar_with(name: str, content: bytes = b"contenido") -> bytes:
    """Un tar.gz con un único miembro regular en `name`."""
    return _make_tar([(name, content)])


def tar_with_symlink(name: str = "link", target: str = "/etc/passwd") -> bytes:
    info = tarfile.TarInfo(name=name)
    info.type = tarfile.SYMTYPE
    info.linkname = target
    return _make_tar([info])


def tar_with_hardlink(name: str = "hard", target: str = "real") -> bytes:
    info = tarfile.TarInfo(name=name)
    info.type = tarfile.LNKTYPE
    info.linkname = target
    return _make_tar([info])


def tar_with_device(name: str = "dev0") -> bytes:
    info = tarfile.TarInfo(name=name)
    info.type = tarfile.CHRTYPE
    return _make_tar([info])


def tar_with_fifo(name: str = "fifo0") -> bytes:
    info = tarfile.TarInfo(name=name)
    info.type = tarfile.FIFOTYPE
    return _make_tar([info])


# --- casos básicos ------------------------------------------------------


def test_escaping_member_rejected(tmp_path: Path) -> None:
    with pytest.raises(ArchiveError):
        safe_extract(tar_with("../evil"), tmp_path)


def test_symlink_member_rejected(tmp_path: Path) -> None:
    with pytest.raises(ArchiveError):
        safe_extract(tar_with_symlink(), tmp_path)


# --- casos borde --------------------------------------------------------


def test_absolute_path_member_rejected(tmp_path: Path) -> None:
    with pytest.raises(ArchiveError):
        safe_extract(tar_with("/etc/passwd"), tmp_path)


def test_hardlink_member_rejected(tmp_path: Path) -> None:
    with pytest.raises(ArchiveError):
        safe_extract(tar_with_hardlink(), tmp_path)


def test_device_member_rejected(tmp_path: Path) -> None:
    with pytest.raises(ArchiveError):
        safe_extract(tar_with_device(), tmp_path)


def test_fifo_member_rejected(tmp_path: Path) -> None:
    with pytest.raises(ArchiveError):
        safe_extract(tar_with_fifo(), tmp_path)


def test_corrupt_tar_rejected(tmp_path: Path) -> None:
    with pytest.raises(ArchiveError):
        safe_extract(b"esto no es un tar.gz valido", tmp_path)


def test_empty_bytes_rejected(tmp_path: Path) -> None:
    with pytest.raises(ArchiveError):
        safe_extract(b"", tmp_path)


def test_valid_archive_extracts_files(tmp_path: Path) -> None:
    archive = _make_tar(
        [("repo-abc123/SKILL.md", b"hola"), ("repo-abc123/sub/dato.txt", b"x")]
    )
    safe_extract(archive, tmp_path)
    assert (tmp_path / "repo-abc123" / "SKILL.md").read_text(encoding="utf-8") == "hola"
    assert (tmp_path / "repo-abc123" / "sub" / "dato.txt").read_text(
        encoding="utf-8"
    ) == "x"


def test_nothing_written_when_one_member_is_invalid(tmp_path: Path) -> None:
    archive = _make_tar([("ok.txt", b"ok"), ("../evil.txt", b"malo")])
    with pytest.raises(ArchiveError):
        safe_extract(archive, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_nothing_written_when_last_member_is_symlink(tmp_path: Path) -> None:
    good = tarfile.TarInfo(name="ok.txt")
    good.size = 2
    link = tarfile.TarInfo(name="bad_link")
    link.type = tarfile.SYMTYPE
    link.linkname = "/etc/passwd"

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.addfile(good, io.BytesIO(b"ok"))
        tar.addfile(link)

    with pytest.raises(ArchiveError):
        safe_extract(buf.getvalue(), tmp_path)
    assert list(tmp_path.iterdir()) == []


# --- sin escrituras parciales si la extracción falla a mitad ------------


def test_nothing_written_when_extraction_fails_midway(tmp_path: Path) -> None:
    # `r/a` es un archivo y después `r/a/b` lo exige como directorio: cada
    # miembro es válido por separado, así que el fallo llega con `r/a` ya
    # escrito. Aun así no debe quedar nada en `dest`.
    archive = _make_tar([("r/a", b"archivo"), ("r/a/b", b"imposible")])
    with pytest.raises(ArchiveError):
        safe_extract(archive, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_missing_dest_not_created_when_extraction_fails(tmp_path: Path) -> None:
    dest = tmp_path / "nuevo"
    archive = _make_tar([("r/a", b"archivo"), ("r/a/b", b"imposible")])
    with pytest.raises(ArchiveError):
        safe_extract(archive, dest)
    assert not dest.exists()


def test_existing_content_untouched_when_extraction_fails(tmp_path: Path) -> None:
    (tmp_path / "previo.txt").write_text("intacto", encoding="utf-8")
    archive = _make_tar([("r/a", b"archivo"), ("r/a/b", b"imposible")])
    with pytest.raises(ArchiveError):
        safe_extract(archive, tmp_path)
    assert [p.name for p in tmp_path.iterdir()] == ["previo.txt"]
    assert (tmp_path / "previo.txt").read_text(encoding="utf-8") == "intacto"


def test_extract_creates_missing_dest(tmp_path: Path) -> None:
    dest = tmp_path / "nuevo"
    safe_extract(_make_tar([("r/SKILL.md", b"hola")]), dest)
    assert (dest / "r" / "SKILL.md").read_bytes() == b"hola"
    assert [p.name for p in dest.iterdir()] == ["r"]


def test_extract_refuses_to_overwrite_existing_entry(tmp_path: Path) -> None:
    (tmp_path / "r").mkdir()
    (tmp_path / "r" / "mio.txt").write_text("mio", encoding="utf-8")
    with pytest.raises(ArchiveError):
        safe_extract(_make_tar([("r/SKILL.md", b"hola")]), tmp_path)
    assert [p.name for p in (tmp_path / "r").iterdir()] == ["mio.txt"]
    assert [p.name for p in tmp_path.iterdir()] == ["r"]
