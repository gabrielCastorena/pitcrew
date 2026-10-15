"""Tests del manifiesto y de la reversión (apply/manifest.py).

Todo corre sobre un HOME y un proyecto dentro de `tmp_path`; `run_cmd` y
`secret` son dobles de prueba, así que nada toca la red, el `claude` real
ni el `~/.claude` de la máquina.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import stat
from pathlib import Path

import pytest

from pitcrew.apply.manifest import (
    Manifest,
    ManifestError,
    StepRecord,
    manifest_path,
    revert,
)
from pitcrew.apply.ops import ApplyContext, Undo, execute, prepare
from pitcrew.models import AddMcp, Level, SetEnv, WriteFile

RUN = "run-1"
SECRET = "s3cr3t"


# --- helpers ------------------------------------------------------------


def rec(
    index: int,
    undo: Undo | None = Undo("none", "", None, None),
    status: str = "pending",
    run_id: str = RUN,
) -> StepRecord:
    return StepRecord(
        run_id=run_id,
        index=index,
        candidate="acme/tool",
        source_url="https://github.com/acme/tool",
        op_kind="write_file",
        summary="escribe CLAUDE.md",
        level=Level.VERIFICADO,
        ref="0123456789abcdef0123456789abcdef01234567",
        status=status,  # type: ignore[arg-type]
        undo=undo,
        at="2026-10-07T12:00:00+00:00",
    )


class World:
    """HOME y proyecto temporales con su manifiesto y un `run_cmd` espiado."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path
        self.home = tmp_path / "home"
        self.proj = self.home / "proyecto"
        self.proj.mkdir(parents=True)
        self.path = manifest_path(self.home, self.proj)
        self.state = self.path.parent
        self.backups = self.state / "backups" / RUN
        self.env = self.state / "env.sh"
        self.skills = self.proj / ".claude" / "skills"
        self.claude_md = self.proj / "CLAUDE.md"
        self.m = Manifest(self.path)
        self.calls: list[list[str]] = []
        self.exit_code = 0

    def run_cmd(self, argv: list[str]) -> int:
        self.calls.append(argv)
        return self.exit_code

    def backup(self, name: str, content: str) -> Path:
        self.backups.mkdir(parents=True, exist_ok=True)
        path = self.backups / name
        path.write_text(content, encoding="utf-8")
        return path

    def restore(self, target: Path, content: str = "original", name: str = "CLAUDE.md.bak") -> Undo:
        return Undo("restore_file", str(target), str(self.backup(name, content)), None)

    def revert(self, run_id: str = RUN) -> list[str]:
        return revert(self.m, run_id, self.run_cmd, self.env)

    def status(self, index: int = 0) -> str:
        return next(r.status for r in self.m.records() if r.index == index)

    def ctx(self) -> ApplyContext:
        return ApplyContext(
            home=self.home,
            project=self.proj,
            run_cmd=self.run_cmd,
            backups_dir=self.backups,
            env_file=self.env,
            archives={},
            secret=lambda name: SECRET,
            manual=[],
        )


@pytest.fixture
def w(tmp_path: Path) -> World:
    return World(tmp_path)


def write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def tree(root: Path) -> dict[str, bytes | None]:
    """Foto del árbol: ruta relativa -> contenido (None para directorios y enlaces)."""
    snapshot: dict[str, bytes | None] = {}
    for current, dirs, files in os.walk(root, followlinks=False):
        for name in (*dirs, *files):
            full = Path(current, name)
            regular = stat.S_ISREG(os.lstat(full).st_mode)
            snapshot[str(full.relative_to(root))] = full.read_bytes() if regular else None
    return snapshot


# --- casos básicos ------------------------------------------------------


def test_crash_leaves_pending(w: World) -> None:
    w.m.begin(rec(0))
    assert Manifest(w.path).records()[0].status == "pending"


def test_revert_pending_restores_file(w: World) -> None:
    w.m.begin(rec(0, undo=w.restore(w.claude_md)))
    write(w.claude_md, "roto")
    w.revert()
    assert w.claude_md.read_text(encoding="utf-8") == "original"
    assert w.status() == "reverted"


def test_revert_mcp_calls_remove(w: World) -> None:
    w.m.begin(rec(0, undo=Undo("mcp_remove", "srv", None, "project")))
    w.m.finish(RUN, 0, "done")
    messages = w.revert()
    assert w.calls[0] == ["claude", "mcp", "remove", "--scope", "project", "srv"]
    assert len(w.calls) == 1
    assert len(messages) == 1
    assert w.status() == "reverted"


def test_revert_idempotent(w: World) -> None:
    w.m.begin(rec(0, undo=w.restore(w.claude_md)))
    w.m.begin(rec(1, undo=Undo("mcp_remove", "srv", None, "user")))
    w.m.begin(rec(2, undo=Undo("delete_path", str(w.skills / "x" / "SKILL.md"), None, None)))
    write(w.claude_md, "roto")
    assert len(w.revert()) == 3
    calls = list(w.calls)
    assert w.revert() == []
    assert w.calls == calls
    assert w.claude_md.read_text(encoding="utf-8") == "original"


def test_no_secret_in_manifest(w: World) -> None:
    ctx = w.ctx()
    operations = [
        AddMcp(
            name="srv",
            scope="user",
            transport="stdio",
            command=["npx", "-y", "paquete@1.2.3"],
            url=None,
            env_names=["TOKEN"],
        ),
        SetEnv(name="TOKEN"),
    ]
    for index, op in enumerate(operations):
        undo = prepare(op, ctx)
        w.m.begin(dataclasses.replace(rec(index, undo=undo), op_kind=type(op).__name__))
        execute(op, ctx)
        w.m.finish(RUN, index, "done")

    # El secreto sí pasó por el flujo real: está en el argv y en el archivo
    # de entorno. En el manifiesto solo queda el nombre de la variable.
    assert f"TOKEN={SECRET}" in w.calls[0]
    assert SECRET in w.env.read_text(encoding="utf-8")
    text = w.path.read_text(encoding="utf-8")
    assert SECRET not in text
    assert "TOKEN" in text

    messages = w.revert()
    assert len(messages) == 2
    assert all(SECRET not in message for message in messages)
    assert SECRET not in w.path.read_text(encoding="utf-8")
    assert SECRET not in w.env.read_text(encoding="utf-8")
    assert w.calls[-1] == ["claude", "mcp", "remove", "--scope", "user", "srv"]


def test_valid_json_after_each_step(w: World) -> None:
    w.m.begin(rec(0))
    json.loads(w.path.read_text(encoding="utf-8"))
    w.m.finish(RUN, 0, "done")
    json.loads(w.path.read_text(encoding="utf-8"))
    assert w.status() == "done"


# --- manifest_path ------------------------------------------------------


def test_manifest_path_with_project(tmp_path: Path) -> None:
    home, proj = tmp_path / "h", tmp_path / "p"
    assert manifest_path(home, proj) == proj / ".claude" / "pitcrew" / "manifest.json"


def test_manifest_path_without_project(tmp_path: Path) -> None:
    home = tmp_path / "h"
    assert manifest_path(home, None) == home / ".claude" / "pitcrew" / "manifest.json"


# --- Manifest -----------------------------------------------------------


def test_records_empty_when_missing(w: World) -> None:
    assert w.m.records() == []
    assert not w.path.exists()


def test_roundtrip_preserves_record(w: World) -> None:
    first = rec(0, undo=Undo("restore_file", "/a/CLAUDE.md", "/b/backups/x.bak", None))
    second = dataclasses.replace(rec(1, undo=None), level=Level.REVISAR, ref=None)
    w.m.begin(first)
    w.m.begin(second)
    assert Manifest(w.path).records() == [first, second]
    assert isinstance(Manifest(w.path).records()[1].level, Level)


def test_level_stored_as_value(w: World) -> None:
    w.m.begin(rec(0))
    data = json.loads(w.path.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert data["records"][0]["level"] == "Verificado"
    assert data["records"][0]["undo"] == {"kind": "none", "target": "", "backup": None, "scope": None}


def test_begin_duplicate_rejected(w: World) -> None:
    w.m.begin(rec(0))
    before = w.path.read_bytes()
    with pytest.raises(ManifestError):
        w.m.begin(rec(0))
    assert w.path.read_bytes() == before
    w.m.begin(rec(0, run_id="run-2"))  # otro run_id sí
    assert len(w.m.records()) == 2


def test_begin_rejects_invalid_record(w: World) -> None:
    with pytest.raises(ManifestError):
        w.m.begin(rec(0, status="inventado"))
    with pytest.raises(ManifestError):
        w.m.begin(rec(1, undo=Undo("rm_rf", "/", None, None)))  # type: ignore[arg-type]
    with pytest.raises(ManifestError):
        w.m.begin(dataclasses.replace(rec(2), level="Dudoso"))  # type: ignore[arg-type]
    assert not w.path.exists()


def test_finish_unknown_step_rejected(w: World) -> None:
    w.m.begin(rec(0))
    before = w.path.read_bytes()
    with pytest.raises(ManifestError):
        w.m.finish(RUN, 1, "done")
    with pytest.raises(ManifestError):
        w.m.finish("otro", 0, "done")
    assert w.path.read_bytes() == before


def test_finish_invalid_status_rejected(w: World) -> None:
    w.m.begin(rec(0))
    before = w.path.read_bytes()
    with pytest.raises(ManifestError):
        w.m.finish(RUN, 0, "hecho")
    assert w.path.read_bytes() == before


def test_finish_only_changes_that_step(w: World) -> None:
    w.m.begin(rec(0))
    w.m.begin(rec(1))
    w.m.begin(rec(0, run_id="run-2"))
    w.m.finish(RUN, 1, "failed")
    assert [(r.run_id, r.index, r.status) for r in w.m.records()] == [
        (RUN, 0, "pending"),
        (RUN, 1, "failed"),
        ("run-2", 0, "pending"),
    ]


def _valid_record() -> dict[str, object]:
    return {
        "run_id": RUN,
        "index": 0,
        "candidate": "acme/tool",
        "source_url": "https://github.com/acme/tool",
        "op_kind": "write_file",
        "summary": "s",
        "level": "Verificado",
        "ref": None,
        "status": "pending",
        "undo": {"kind": "none", "target": "", "backup": None, "scope": None},
        "at": "2026-10-07T12:00:00+00:00",
    }


def _doc(**changes: object) -> str:
    record = _valid_record()
    record.update(changes)
    return json.dumps({"version": 1, "records": [record]})


def _without(field: str) -> str:
    record = _valid_record()
    del record[field]
    return json.dumps({"version": 1, "records": [record]})


CORRUPT = {
    "vacio": "",
    "json_invalido": '{"version": 1, "records": [',
    "no_utf8": b"\xff\xfe",
    "lista": json.dumps([_valid_record()]),
    "sin_version": json.dumps({"records": []}),
    "version_desconocida": json.dumps({"version": 2, "records": []}),
    "clave_extra": json.dumps({"version": 1, "records": [], "extra": 1}),
    "records_no_lista": json.dumps({"version": 1, "records": {}}),
    "registro_no_objeto": json.dumps({"version": 1, "records": ["x"]}),
    "campo_faltante": _without("status"),
    "sin_undo": _without("undo"),
    "campo_desconocido": _doc(valor="x"),
    "status_invalido": _doc(status="hecho"),
    "level_invalido": _doc(level="Dudoso"),
    "kind_invalido": _doc(undo={"kind": "rm_rf", "target": "/", "backup": None, "scope": None}),
    "undo_campo_extra": _doc(undo={"kind": "none", "target": "", "backup": None, "scope": None, "x": 1}),
    "undo_campo_faltante": _doc(undo={"kind": "none", "target": ""}),
    "undo_target_no_texto": _doc(undo={"kind": "none", "target": 3, "backup": None, "scope": None}),
    "undo_no_objeto": _doc(undo="delete_path"),
    "index_bool": _doc(index=True),
    "index_texto": _doc(index="0"),
    "index_negativo": _doc(index=-1),
    "run_id_no_texto": _doc(run_id=7),
    "ref_no_texto": _doc(ref=7),
    "duplicado": json.dumps({"version": 1, "records": [_valid_record(), _valid_record()]}),
}


@pytest.mark.parametrize("content", CORRUPT.values(), ids=CORRUPT.keys())
def test_corrupt_manifest_is_never_repaired(w: World, content: str | bytes) -> None:
    raw = content if isinstance(content, bytes) else content.encode("utf-8")
    w.state.mkdir(parents=True)
    w.path.write_bytes(raw)

    with pytest.raises(ManifestError):
        w.m.records()
    with pytest.raises(ManifestError):
        w.m.begin(rec(5))
    with pytest.raises(ManifestError):
        w.m.finish(RUN, 0, "done")
    with pytest.raises(ManifestError):
        w.revert()
    assert w.path.read_bytes() == raw
    assert os.listdir(w.state) == ["manifest.json"]


def test_manifest_symlink_rejected(w: World) -> None:
    real = write(w.root / "otro.json", json.dumps({"version": 1, "records": []}))
    w.state.mkdir(parents=True)
    w.path.symlink_to(real)
    with pytest.raises(ManifestError):
        w.m.records()
    with pytest.raises(ManifestError):
        w.m.begin(rec(0))
    assert w.path.is_symlink()
    assert json.loads(real.read_text(encoding="utf-8")) == {"version": 1, "records": []}


def test_atomic_write_failure_keeps_previous(w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    w.m.begin(rec(0))
    before = w.path.read_bytes()

    def boom(src: object, dst: object, **kwargs: object) -> None:
        raise OSError(28, "No space left on device")

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", boom)
        with pytest.raises(ManifestError):
            w.m.begin(rec(1))
        with pytest.raises(ManifestError):
            w.m.finish(RUN, 0, "done")

    assert w.path.read_bytes() == before
    assert os.listdir(w.state) == ["manifest.json"]
    assert [r.status for r in w.m.records()] == ["pending"]


def test_atomic_write_uses_fsync_and_sibling_temp(w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    synced: list[int] = []
    replaced: list[tuple[str, str]] = []
    real_fsync, real_replace = os.fsync, os.replace

    def spy_fsync(fd: int) -> None:
        synced.append(fd)
        real_fsync(fd)

    def spy_replace(src: object, dst: object, **kwargs: object) -> None:
        replaced.append((os.fspath(src), os.fspath(dst)))  # type: ignore[arg-type]
        real_replace(src, dst, **kwargs)  # type: ignore[arg-type]

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", spy_fsync)
        patch.setattr(os, "replace", spy_replace)
        w.m.begin(rec(0))

    assert len(replaced) == 1
    source, dest = replaced[0]
    assert dest == str(w.path)
    assert os.path.dirname(source) == str(w.state)
    assert len(synced) >= 2  # el archivo y su directorio


def test_permissions(w: World) -> None:
    old = os.umask(0o022)
    try:
        w.m.begin(rec(0))
        w.m.finish(RUN, 0, "done")
    finally:
        os.umask(old)
    assert mode(w.state) == 0o700
    assert mode(w.path) == 0o600


# --- revert: qué pasos y en qué orden -----------------------------------


def test_revert_unknown_run_is_empty(w: World) -> None:
    assert w.revert() == []
    w.m.begin(rec(0, undo=Undo("mcp_remove", "srv", None, "user")))
    assert w.revert("otro-run") == []
    assert w.calls == []
    assert w.status() == "pending"


def test_crash_mid_install_is_reverted(w: World) -> None:
    """El proceso muere entre `begin` y `finish`."""
    write(w.claude_md, "original\n")
    ctx = w.ctx()
    first = WriteFile(path="CLAUDE.md", content="instalado a medias\n")
    second = WriteFile(path=".claude/skills/nueva/SKILL.md", content="# nueva\n")

    for index, op in enumerate([first, second]):
        w.m.begin(rec(index, undo=prepare(op, ctx)))
        execute(op, ctx)
        if index == 0:
            w.m.finish(RUN, index, "done")
    # Aquí muere el proceso: el segundo paso escribió y nunca llegó a `finish`.
    del w.m

    survivor = Manifest(w.path)
    assert [r.status for r in survivor.records()] == ["done", "pending"]
    messages = revert(survivor, RUN, w.run_cmd, w.env)

    assert len(messages) == 2
    assert w.claude_md.read_text(encoding="utf-8") == "original\n"
    assert not (w.skills / "nueva").exists()
    assert w.skills.is_dir()
    assert [r.status for r in survivor.records()] == ["reverted", "reverted"]


def test_crash_before_effect_is_reverted(w: World) -> None:
    """El proceso muere después de `begin` y antes de escribir nada."""
    ctx = w.ctx()
    undo = prepare(WriteFile(path=".claude/skills/nueva/SKILL.md", content="x"), ctx)
    w.m.begin(rec(0, undo=undo))

    survivor = Manifest(w.path)
    assert len(revert(survivor, RUN, w.run_cmd, w.env)) == 1
    assert survivor.records()[0].status == "reverted"


def test_revert_in_reverse_order(w: World) -> None:
    w.m.begin(rec(0, undo=w.restore(w.claude_md, "original", "CLAUDE.md.0.bak")))
    w.m.finish(RUN, 0, "done")
    w.m.begin(rec(1, undo=w.restore(w.claude_md, "v1", "CLAUDE.md.1.bak")))
    w.m.finish(RUN, 1, "done")
    write(w.claude_md, "v2")

    messages = w.revert()

    assert w.claude_md.read_text(encoding="utf-8") == "original"
    assert "paso 1" in messages[0] and "paso 0" in messages[1]


def test_revert_skips_failed_skipped_reverted_and_other_runs(w: World) -> None:
    for index, status in enumerate(["failed", "skipped", "reverted"]):
        w.m.begin(rec(index, undo=Undo("mcp_remove", f"srv{index}", None, "user")))
        w.m.finish(RUN, index, status)
    w.m.begin(rec(0, undo=Undo("mcp_remove", "ajeno", None, "user"), run_id="run-2"))
    w.m.finish("run-2", 0, "done")

    assert w.revert() == []
    assert w.calls == []
    assert [r.status for r in w.m.records()] == ["failed", "skipped", "reverted", "done"]


def test_revert_marks_each_step_immediately(w: World) -> None:
    """Una reversión interrumpida deja marcado lo ya deshecho."""
    w.m.begin(rec(0, undo=Undo("mcp_remove", "uno", None, "user")))
    w.m.begin(rec(1, undo=Undo("mcp_remove", "dos", None, "user")))
    seen: list[list[str]] = []

    def dying(argv: list[str]) -> int:
        seen.append([r.status for r in Manifest(w.path).records()])
        if len(seen) == 2:
            raise KeyboardInterrupt
        return 0

    with pytest.raises(KeyboardInterrupt):
        revert(w.m, RUN, dying, w.env)
    assert seen == [["pending", "pending"], ["pending", "reverted"]]

    w.revert()
    assert [call[-1] for call in w.calls] == ["uno"]
    assert [r.status for r in w.m.records()] == ["reverted", "reverted"]


def test_revert_none_is_silent(w: World) -> None:
    w.m.begin(rec(0, undo=Undo("none", "", None, None)))
    w.m.finish(RUN, 0, "done")
    assert w.revert() == []
    assert w.status() == "reverted"


def test_revert_without_undo_is_refused(w: World) -> None:
    w.m.begin(rec(0, undo=None))
    messages = w.revert()
    assert len(messages) == 1
    assert w.status() == "pending"


# --- revert: restore_file -----------------------------------------------


def test_restore_keeps_target_mode_and_leaves_no_temp(w: World) -> None:
    write(w.claude_md, "roto")
    os.chmod(w.claude_md, 0o640)
    w.m.begin(rec(0, undo=w.restore(w.claude_md)))
    w.revert()
    assert w.claude_md.read_text(encoding="utf-8") == "original"
    assert mode(w.claude_md) == 0o640
    assert sorted(os.listdir(w.proj)) == [".claude", "CLAUDE.md"]


def test_restore_recreates_deleted_target(w: World) -> None:
    w.m.begin(rec(0, undo=w.restore(w.claude_md)))
    assert not w.claude_md.exists()
    w.revert()
    assert w.claude_md.read_text(encoding="utf-8") == "original"


def test_restore_skill_file(w: World) -> None:
    target = write(w.skills / "x" / "SKILL.md", "roto")
    w.m.begin(rec(0, undo=w.restore(target, name="SKILL.md.bak")))
    w.revert()
    assert target.read_text(encoding="utf-8") == "original"


def test_restore_missing_backup_not_reverted(w: World) -> None:
    write(w.claude_md, "roto")
    undo = w.restore(w.claude_md)
    os.unlink(undo.backup)  # type: ignore[arg-type]
    w.m.begin(rec(0, undo=undo))
    messages = w.revert()
    assert len(messages) == 1 and "respaldo" in messages[0]
    assert w.claude_md.read_text(encoding="utf-8") == "roto"
    assert w.status() == "pending"


def test_restore_backup_outside_backups_refused(w: World) -> None:
    write(w.claude_md, "actual")
    evil = write(w.root / "malo.txt", "contenido del atacante")
    inside_state = write(w.state / "otro.txt", "tampoco")
    escaping = f"{w.state}/backups/../../../../malo.txt"
    w.backup("x.bak", "x")
    for index, backup in enumerate(
        [str(evil), str(inside_state), escaping, str(w.state / "backups"), "backups/x.bak", ""]
    ):
        w.m.begin(rec(index, undo=Undo("restore_file", str(w.claude_md), backup, None)))
    w.m.begin(rec(9, undo=Undo("restore_file", str(w.claude_md), None, None)))

    messages = w.revert()

    assert len(messages) == 7
    assert w.claude_md.read_text(encoding="utf-8") == "actual"
    assert all(r.status == "pending" for r in w.m.records())


def test_restore_backup_symlink_refused(w: World) -> None:
    write(w.claude_md, "actual")
    evil = write(w.root / "malo.txt", "contenido del atacante")
    w.backups.mkdir(parents=True)
    link = w.backups / "CLAUDE.md.bak"
    link.symlink_to(evil)
    linked_dir = w.state / "backups" / "enlace"
    linked_dir.symlink_to(w.root)
    w.m.begin(rec(0, undo=Undo("restore_file", str(w.claude_md), str(link), None)))
    w.m.begin(rec(1, undo=Undo("restore_file", str(w.claude_md), str(linked_dir / "malo.txt"), None)))

    assert len(w.revert()) == 2
    assert w.claude_md.read_text(encoding="utf-8") == "actual"
    assert [r.status for r in w.m.records()] == ["pending", "pending"]


def test_restore_target_outside_allowed_refused(w: World) -> None:
    bashrc = write(w.home / ".bashrc", "mío")
    settings = write(w.proj / ".claude" / "settings.json", "{}")
    hook = write(w.skills / "x" / "hooks" / "run.sh", "mío")
    backup = w.backup("x.bak", "contenido del atacante")
    targets = [bashrc, settings, hook, w.proj / ".claude" / "skills", w.proj / "claude.md"]
    for index, target in enumerate(targets):
        w.m.begin(rec(index, undo=Undo("restore_file", str(target), str(backup), None)))
    before = tree(w.home)

    messages = w.revert()

    assert len(messages) == len(targets)
    after = tree(w.home)
    after.pop(os.path.relpath(w.path, w.home))
    before.pop(os.path.relpath(w.path, w.home))
    assert after == before
    assert all(r.status == "pending" for r in w.m.records())


def test_restore_target_symlink_refused(w: World) -> None:
    victim = write(w.root / "victima.txt", "mío")
    w.claude_md.symlink_to(victim)
    w.m.begin(rec(0, undo=w.restore(w.claude_md)))
    assert len(w.revert()) == 1
    assert victim.read_text(encoding="utf-8") == "mío"
    assert w.claude_md.is_symlink()
    assert w.status() == "pending"


def test_restore_target_directory_refused(w: World) -> None:
    w.claude_md.mkdir()
    w.m.begin(rec(0, undo=w.restore(w.claude_md)))
    assert len(w.revert()) == 1
    assert w.claude_md.is_dir()
    assert w.status() == "pending"


# --- revert: delete_path ------------------------------------------------


def test_delete_new_claude_md(w: World) -> None:
    write(w.claude_md, "nuevo")
    other = write(w.proj / "README.md", "mío")
    w.m.begin(rec(0, undo=Undo("delete_path", str(w.claude_md), None, None)))
    assert len(w.revert()) == 1
    assert not w.claude_md.exists()
    assert other.exists() and w.proj.is_dir()
    assert w.status() == "reverted"


def test_delete_skill_directory(w: World) -> None:
    skill = w.skills / "mi-skill"
    write(skill / "SKILL.md", "# skill")
    write(skill / "refs" / "a.md", "a")
    w.m.begin(rec(0, undo=Undo("delete_path", str(skill), None, None)))
    w.m.finish(RUN, 0, "done")

    w.revert()

    assert not skill.exists()
    assert w.skills.is_dir()  # `.claude/skills` nunca se borra, ni vacío
    assert w.status() == "reverted"


def test_delete_cleans_empty_parents_up_to_skills(w: World) -> None:
    target = write(w.skills / "a" / "b" / "SKILL.md", "x")
    w.m.begin(rec(0, undo=Undo("delete_path", str(target), None, None)))
    w.revert()
    assert not (w.skills / "a").exists()
    assert w.skills.is_dir()
    assert (w.proj / ".claude").is_dir()


def test_delete_keeps_non_empty_parents(w: World) -> None:
    target = write(w.skills / "a" / "b" / "SKILL.md", "x")
    sibling = write(w.skills / "a" / "otro.md", "mío")
    w.m.begin(rec(0, undo=Undo("delete_path", str(target), None, None)))
    w.revert()
    assert not (w.skills / "a" / "b").exists()
    assert sibling.read_text(encoding="utf-8") == "mío"


def test_delete_missing_target_is_ignored(w: World) -> None:
    w.skills.mkdir(parents=True)
    w.m.begin(rec(0, undo=Undo("delete_path", str(w.skills / "nunca" / "SKILL.md"), None, None)))
    messages = w.revert()
    assert len(messages) == 1
    assert w.status() == "reverted"
    assert w.skills.is_dir()


def test_delete_does_not_follow_symlinks_inside_skill(w: World) -> None:
    victim = write(w.root / "fuera" / "mio.txt", "mío")
    skill = w.skills / "mi-skill"
    write(skill / "SKILL.md", "x")
    (skill / "enlace").symlink_to(victim.parent)
    w.m.begin(rec(0, undo=Undo("delete_path", str(skill), None, None)))
    w.revert()
    assert not skill.exists()
    assert victim.read_text(encoding="utf-8") == "mío"


def _tampered_targets(w: World) -> dict[str, str]:
    victim = write(w.root / "victima.txt", "mío")
    write(w.home / "datos" / "a.txt", "mío")
    write(w.proj / ".claude" / "settings.json", "{}")
    write(w.proj / ".claude" / "agents" / "a.md", "mío")
    write(w.skills / "x" / "hooks" / "run.sh", "mío")
    write(w.skills / "x" / "SKILL.md", "mío")
    write(w.proj / "claude.md", "mío")
    write(w.proj / "notas" / "CLAUDE.md.bak", "mío")
    return {
        "fuera": str(victim),
        "directorio_ajeno": str(w.home / "datos"),
        "home": str(w.home),
        "raiz": "/",
        "punto_punto": f"{w.skills}/x/../../../../../victima.txt",
        "punto_punto_dentro": f"{w.skills}/y/../x/SKILL.md",
        "punto": f"{w.skills}/./x/SKILL.md",
        "doble_barra": f"{w.skills}//x/SKILL.md",
        "doble_barra_inicial": f"/{w.skills}/x/SKILL.md",
        "barra_final": f"{w.skills}/x/",
        "relativa": ".claude/skills/x/SKILL.md",
        "vacia": "",
        "control": f"{w.skills}/x/SKILL.md\n",
        "nulo": f"{w.skills}/x/SKILL.md\x00",
        "hooks": str(w.skills / "x" / "hooks" / "run.sh"),
        "hooks_mayusculas": str(w.skills / "x" / "HOOKS"),
        "settings": str(w.proj / ".claude" / "settings.json"),
        "skills_mismo": str(w.skills),
        "dot_claude": str(w.proj / ".claude"),
        "agents": str(w.proj / ".claude" / "agents" / "a.md"),
        "claude_md_minusculas": str(w.proj / "claude.md"),
        "claude_md_sufijo": str(w.proj / "notas" / "CLAUDE.md.bak"),
    }


def test_tampered_delete_path_deletes_nothing(w: World) -> None:
    targets = _tampered_targets(w)
    for index, target in enumerate(targets.values()):
        w.m.begin(rec(index, undo=Undo("delete_path", target, None, None)))
        if index % 2:
            w.m.finish(RUN, index, "done")
    before = tree(w.root)
    statuses = [r.status for r in w.m.records()]

    messages = w.revert()

    assert len(messages) == len(targets)
    assert tree(w.root) == before  # ni el manifiesto cambió
    assert [r.status for r in w.m.records()] == statuses
    assert "reverted" not in statuses


def test_tampered_delete_etc_passwd(w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    w.m.begin(rec(0, undo=Undo("delete_path", "/etc/passwd", None, None)))
    w.m.begin(rec(1, undo=Undo("restore_file", "/etc/passwd", str(w.backup("p.bak", "x")), None)))
    touched: list[object] = []

    def spy(*args: object, **kwargs: object) -> None:
        touched.append(args)

    with monkeypatch.context() as patch:
        for module, name in [
            (os, "unlink"), (os, "remove"), (os, "rmdir"), (os, "replace"),
            (os, "rename"), (shutil, "rmtree"),
        ]:
            patch.setattr(module, name, spy)
        messages = w.revert()

    assert touched == []
    assert len(messages) == 2
    assert [r.status for r in w.m.records()] == ["pending", "pending"]


def test_delete_target_symlink_refused(w: World) -> None:
    victim = write(w.root / "fuera" / "mio.txt", "mío")
    w.skills.mkdir(parents=True)
    link = w.skills / "enlace"
    link.symlink_to(victim.parent)
    w.m.begin(rec(0, undo=Undo("delete_path", str(link), None, None)))
    w.m.begin(rec(1, undo=Undo("delete_path", str(link / "mio.txt"), None, None)))

    assert len(w.revert()) == 2
    assert victim.read_text(encoding="utf-8") == "mío"
    assert link.is_symlink()
    assert [r.status for r in w.m.records()] == ["pending", "pending"]


def test_delete_through_symlinked_skills_dir_refused(w: World) -> None:
    outside = w.root / "fuera"
    victim = write(outside / "x" / "SKILL.md", "mío")
    (w.proj / ".claude").mkdir()
    w.skills.symlink_to(outside)
    w.m.begin(rec(0, undo=Undo("delete_path", str(w.skills / "x"), None, None)))

    assert len(w.revert()) == 1
    assert victim.read_text(encoding="utf-8") == "mío"
    assert w.status() == "pending"


# --- revert: mcp_remove -------------------------------------------------


@pytest.mark.parametrize(
    ("name", "scope"),
    [
        ("-rf", "user"),
        ("--scope", "user"),
        ("", "user"),
        ("a b", "user"),
        ("srv\n", "user"),
        ("a" * 65, "user"),
        ("ñandú", "user"),
        ("srv", "local"),
        ("srv", None),
        ("srv", "--all"),
    ],
)
def test_mcp_remove_invalid_never_runs(w: World, name: str, scope: str | None) -> None:
    w.m.begin(rec(0, undo=Undo("mcp_remove", name, None, scope)))
    w.m.finish(RUN, 0, "done")
    messages = w.revert()
    assert w.calls == []
    assert len(messages) == 1
    assert w.status() == "done"


def test_mcp_remove_nonzero_means_already_gone(w: World) -> None:
    w.exit_code = 1
    w.m.begin(rec(0, undo=Undo("mcp_remove", "srv", None, "user")))
    messages = w.revert()
    assert len(w.calls) == 1
    assert len(messages) == 1 and "ya no" in messages[0]
    assert w.status() == "reverted"


def test_mcp_remove_exception_not_reverted(w: World) -> None:
    w.m.begin(rec(0, undo=Undo("mcp_remove", "srv", None, "user")))

    def failing(argv: list[str]) -> int:
        raise RuntimeError("texto-interno-de-la-excepcion")

    messages = revert(w.m, RUN, failing, w.env)
    assert len(messages) == 1
    assert "texto-interno-de-la-excepcion" not in messages[0]
    assert w.status() == "pending"


# --- revert: env_remove -------------------------------------------------


def test_env_remove_deletes_only_that_line(w: World) -> None:
    w.state.mkdir(parents=True)
    w.env.write_text(
        "# mis variables\n"
        "export OTRA='a'\n"
        "export TOKEN='s3cr3t'\n"
        "export TOKEN_2='b'\n"
        "echo export TOKEN='no es una definición'\n",
        encoding="utf-8",
    )
    os.chmod(w.env, 0o644)
    w.m.begin(rec(0, undo=Undo("env_remove", "TOKEN", None, None)))

    messages = w.revert()

    assert w.env.read_text(encoding="utf-8") == (
        "# mis variables\n"
        "export OTRA='a'\n"
        "export TOKEN_2='b'\n"
        "echo export TOKEN='no es una definición'\n"
    )
    assert mode(w.env) == 0o600
    assert sorted(os.listdir(w.state)) == ["env.sh", "manifest.json"]
    assert len(messages) == 1 and SECRET not in messages[0]
    assert w.status() == "reverted"


def test_env_remove_undoes_set_env_exactly(w: World) -> None:
    w.state.mkdir(parents=True)
    w.env.write_bytes(b"export OTRA='a'")  # sin salto de línea final
    ctx = w.ctx()
    op = SetEnv(name="TOKEN")
    w.m.begin(rec(0, undo=prepare(op, ctx)))
    execute(op, ctx)
    assert SECRET in w.env.read_text(encoding="utf-8")

    w.revert()

    assert w.env.read_bytes() == b"export OTRA='a'\n"
    assert mode(w.env) == 0o600


def test_env_remove_missing_file_or_line_is_ignored(w: World) -> None:
    w.m.begin(rec(0, undo=Undo("env_remove", "TOKEN", None, None)))
    assert len(w.revert()) == 1
    assert not w.env.exists()
    assert w.status() == "reverted"

    w.env.write_text("export OTRA='a'\n", encoding="utf-8")
    w.m.begin(rec(1, undo=Undo("env_remove", "TOKEN", None, None)))
    assert len(w.revert()) == 1
    assert w.env.read_text(encoding="utf-8") == "export OTRA='a'\n"
    assert w.status(1) == "reverted"


def test_env_remove_symlink_refused(w: World) -> None:
    real = write(w.root / "real.sh", "export TOKEN='s3cr3t'\n")
    w.state.mkdir(parents=True)
    w.env.symlink_to(real)
    w.m.begin(rec(0, undo=Undo("env_remove", "TOKEN", None, None)))

    messages = w.revert()

    assert len(messages) == 1 and SECRET not in messages[0]
    assert real.read_text(encoding="utf-8") == "export TOKEN='s3cr3t'\n"
    assert w.env.is_symlink()
    assert w.status() == "pending"


@pytest.mark.parametrize("name", ["", "1TOKEN", "TOKEN=x", "A B", ".*", "TOKEN\n", "ñ"])
def test_env_remove_invalid_name_refused(w: World, name: str) -> None:
    w.state.mkdir(parents=True)
    content = "export TOKEN='s3cr3t'\nexport OTRA='a'\n"
    w.env.write_text(content, encoding="utf-8")
    w.m.begin(rec(0, undo=Undo("env_remove", name, None, None)))

    messages = w.revert()

    assert len(messages) == 1 and SECRET not in messages[0]
    assert w.env.read_text(encoding="utf-8") == content
    assert w.status() == "pending"


# --- revert: solo dentro del árbol que posee el manifiesto ---------------


def test_planted_manifest_cannot_touch_another_tree(tmp_path: Path) -> None:
    """Un repo clonado trae manifiesto y respaldos; los destinos están en el HOME."""
    home = tmp_path / "home"
    cloned = tmp_path / "clonado"
    write(home / ".claude" / "skills" / "mia" / "SKILL.md", "mía")
    write(home / "otro" / "CLAUDE.md", "mío")
    path = manifest_path(home, cloned)
    backup = write(path.parent / "backups" / "x.bak", "INYECCION")
    m = Manifest(path)
    undos = [
        Undo("delete_path", str(home / ".claude" / "skills" / "mia"), None, None),
        Undo("delete_path", str(home / "otro" / "CLAUDE.md"), None, None),
        Undo("restore_file", str(home / ".claude" / "CLAUDE.md"), str(backup), None),
        Undo("restore_file", str(home / ".claude" / "skills" / "nueva.md"), str(backup), None),
    ]
    for index, undo in enumerate(undos):
        m.begin(rec(index, undo=undo))
        m.finish(RUN, index, "done")
    before = tree(home)

    messages = revert(m, RUN, lambda argv: 0, tmp_path / "env.sh")

    assert tree(home) == before
    assert len(messages) == 4 and all("no se revirtió" in text for text in messages)
    assert [r.status for r in m.records()] == ["done"] * 4


def test_symlinked_ancestor_inside_anchor_refused(w: World) -> None:
    victim = w.root / "victima"
    md = write(victim / "CLAUDE.md", "mío")
    skill = write(victim / ".claude" / "skills" / "s" / "SKILL.md", "mío")
    (w.proj / "enlace").symlink_to(victim)
    w.m.begin(rec(0, undo=Undo("delete_path", str(w.proj / "enlace" / "CLAUDE.md"), None, None)))
    w.m.begin(rec(1, undo=Undo("delete_path", str(w.proj / "enlace" / ".claude" / "skills" / "s"), None, None)))

    assert len(w.revert()) == 2
    assert md.exists() and skill.exists()
    assert [r.status for r in w.m.records()] == ["pending", "pending"]


def test_symlinked_anchor_refused(tmp_path: Path) -> None:
    victim = tmp_path / "victima"
    md = write(victim / "CLAUDE.md", "mío")
    skill = write(victim / ".claude" / "skills" / "s" / "SKILL.md", "mío")
    link = tmp_path / "enlace"
    link.symlink_to(victim)
    m = Manifest(manifest_path(tmp_path, link))
    backup = write(victim / ".claude" / "pitcrew" / "backups" / "x.bak", "INYECCION")
    m.begin(rec(0, undo=Undo("delete_path", str(link / "CLAUDE.md"), None, None)))
    m.begin(rec(1, undo=Undo("delete_path", str(link / ".claude" / "skills" / "s"), None, None)))
    m.begin(rec(2, undo=Undo("restore_file", str(link / "CLAUDE.md"),
                             str(link / ".claude" / "pitcrew" / "backups" / "x.bak"), None)))

    assert len(revert(m, RUN, lambda argv: 0, tmp_path / "env.sh")) == 3
    assert md.read_text(encoding="utf-8") == "mío" and skill.exists() and backup.exists()
    assert [r.status for r in m.records()] == ["pending"] * 3


@pytest.mark.parametrize(
    "relative",
    [
        "manifest.json",
        ".claude/manifest.json",
        ".claude/otro/manifest.json",
        ".claude/pitcrew/otro.json",
        "claude/pitcrew/manifest.json",
        ".claude/pitcrew/sub/manifest.json",
    ],
)
def test_manifest_outside_expected_location_refuses_file_steps(tmp_path: Path, relative: str) -> None:
    base = tmp_path / "proyecto"
    path = base / relative
    md = write(base / "CLAUDE.md", "actual")
    skill = write(base / ".claude" / "skills" / "s" / "SKILL.md", "mía")
    md_here = write(path.parent / "CLAUDE.md", "actual")
    backup = write(path.parent / "backups" / "x.bak", "INYECCION")
    m = Manifest(path)
    undos = [
        Undo("restore_file", str(md), str(backup), None),
        Undo("delete_path", str(skill.parent), None, None),
        Undo("delete_path", str(md_here), None, None),
        Undo("mcp_remove", "srv", None, "user"),
    ]
    for index, undo in enumerate(undos):
        m.begin(rec(index, undo=undo))
    calls: list[list[str]] = []

    messages = revert(m, RUN, lambda argv: calls.append(argv) or 0, tmp_path / "env.sh")

    assert len(messages) == 4
    assert md.read_text(encoding="utf-8") == "actual" and skill.exists() and md_here.exists()
    # Lo que no es de archivos no depende del ancla.
    assert [r.status for r in m.records()] == ["pending", "pending", "pending", "reverted"]
    assert len(calls) == 1


def test_relative_manifest_path_refuses_file_steps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    md = write(tmp_path / "CLAUDE.md", "nuevo")
    m = Manifest(Path(".claude/pitcrew/manifest.json"))
    m.begin(rec(0, undo=Undo("delete_path", str(md), None, None)))
    assert len(revert(m, RUN, lambda argv: 0, tmp_path / "env.sh")) == 1
    assert md.exists()
    assert m.records()[0].status == "pending"


def test_three_valid_targets_are_reverted(tmp_path: Path) -> None:
    home = tmp_path / "home"
    path = manifest_path(home, None)
    m = Manifest(path)
    root_md = write(home / "CLAUDE.md", "nuevo")
    global_md = write(home / ".claude" / "CLAUDE.md", "roto")
    skill = write(home / ".claude" / "skills" / "s" / "SKILL.md", "x")
    backup = write(path.parent / "backups" / RUN / "CLAUDE.md.bak", "original")
    m.begin(rec(0, undo=Undo("delete_path", str(root_md), None, None)))
    m.begin(rec(1, undo=Undo("restore_file", str(global_md), str(backup), None)))
    m.begin(rec(2, undo=Undo("delete_path", str(skill.parent), None, None)))

    messages = revert(m, RUN, lambda argv: 0, tmp_path / "env.sh")

    assert len(messages) == 3 and not any("no se revirtió" in text for text in messages)
    assert not root_md.exists() and not skill.parent.exists()
    assert global_md.read_text(encoding="utf-8") == "original"
    assert [r.status for r in m.records()] == ["reverted"] * 3


def test_anchor_under_directory_named_hooks(tmp_path: Path) -> None:
    proj = tmp_path / "hooks" / "settings.json" / "proyecto"
    path = manifest_path(tmp_path, proj)
    m = Manifest(path)
    md = write(proj / "CLAUDE.md", "roto")
    skill = write(proj / ".claude" / "skills" / "s" / "SKILL.md", "x")
    backup = write(path.parent / "backups" / "CLAUDE.md.bak", "original")
    m.begin(rec(0, undo=Undo("restore_file", str(md), str(backup), None)))
    m.begin(rec(1, undo=Undo("delete_path", str(skill.parent), None, None)))

    revert(m, RUN, lambda argv: 0, tmp_path / "env.sh")

    assert md.read_text(encoding="utf-8") == "original"
    assert not skill.parent.exists()
    assert [r.status for r in m.records()] == ["reverted", "reverted"]


def test_nested_claude_md_and_nested_skills_refused(w: World) -> None:
    sub_md = write(w.proj / "sub" / "CLAUDE.md", "mío")
    sub_skill = write(w.proj / "sub" / ".claude" / "skills" / "s" / "SKILL.md", "mío")
    deep_md = write(w.proj / ".claude" / "agents" / "CLAUDE.md", "mío")
    home_md = write(w.home / "CLAUDE.md", "mío")  # por encima del ancla
    backup = w.backup("x.bak", "INYECCION")
    w.m.begin(rec(0, undo=Undo("delete_path", str(sub_md), None, None)))
    w.m.begin(rec(1, undo=Undo("delete_path", str(sub_skill.parent), None, None)))
    w.m.begin(rec(2, undo=Undo("restore_file", str(deep_md), str(backup), None)))
    w.m.begin(rec(3, undo=Undo("delete_path", str(home_md), None, None)))
    before = tree(w.home)

    assert len(w.revert()) == 4
    assert tree(w.home) == before
    assert all(r.status == "pending" for r in w.m.records())


# --- cadenas no codificables y robustez del bucle de revert --------------

SURROGATE = "\ud800"


def _doc_with_surrogate(field: str) -> str:
    record = _valid_record()
    undo = {"kind": "delete_path", "target": "/x/.claude/skills/s", "backup": None, "scope": None}
    if field.startswith("undo."):
        undo[field[5:]] = "/x/.claude/skills/" + SURROGATE
    else:
        record[field] = "a" + SURROGATE
    record["undo"] = undo
    other = _valid_record()
    other.update(index=1, undo={"kind": "mcp_remove", "target": "srv", "backup": None, "scope": "user"})
    return json.dumps({"version": 1, "records": [record, other]})  # ensure_ascii: queda como \ud800


SURROGATE_FIELDS = [
    "run_id", "candidate", "source_url", "op_kind", "summary", "ref", "at",
    "undo.target", "undo.backup", "undo.scope",
]


@pytest.mark.parametrize("field", SURROGATE_FIELDS)
def test_unencodable_string_in_manifest_is_rejected(w: World, field: str) -> None:
    raw = _doc_with_surrogate(field).encode("ascii")
    w.state.mkdir(parents=True)
    w.path.write_bytes(raw)

    with pytest.raises(ManifestError):
        w.m.records()
    with pytest.raises(ManifestError):
        w.revert()
    with pytest.raises(ManifestError):
        w.m.finish(RUN, 1, "reverted")
    assert w.path.read_bytes() == raw
    assert w.calls == []


@pytest.mark.parametrize("field", SURROGATE_FIELDS)
def test_begin_rejects_unencodable_string(w: World, field: str) -> None:
    record = rec(0, undo=Undo("delete_path", "/x/.claude/skills/s", "b", "user"))
    if field.startswith("undo."):
        record = dataclasses.replace(
            record, undo=dataclasses.replace(record.undo, **{field[5:]: "x" + SURROGATE})  # type: ignore[arg-type]
        )
    else:
        record = dataclasses.replace(record, **{field: "x" + SURROGATE})
    with pytest.raises(ManifestError):
        w.m.begin(record)
    assert not w.path.exists()


class FakeManifest:
    """Manifiesto en memoria: entrega registros que `records()` real vetaría."""

    def __init__(self, path: Path, records: list[StepRecord]) -> None:
        self.path = path
        self._records = records
        self.finished: list[tuple[int, str]] = []

    def records(self) -> list[StepRecord]:
        return list(self._records)

    def finish(self, run_id: str, index: int, status: str) -> None:
        self.finished.append((index, status))


def test_surrogate_target_is_refused_and_other_steps_continue(w: World) -> None:
    fake = FakeManifest(
        w.path,
        [
            rec(0, undo=Undo("mcp_remove", "srv", None, "user"), status="done"),
            rec(1, undo=Undo("delete_path", f"{w.skills}/{SURROGATE}", None, None), status="done"),
            rec(2, undo=Undo("restore_file", str(w.claude_md), f"{w.state}/backups/{SURROGATE}", None)),
        ],
    )
    messages = revert(fake, RUN, w.run_cmd, w.env)  # type: ignore[arg-type]

    assert len(messages) == 3
    assert "no se revirtió" in messages[0] and "no se revirtió" in messages[1]
    assert fake.finished == [(0, "reverted")]
    assert w.calls == [["claude", "mcp", "remove", "--scope", "user", "srv"]]


def test_unexpected_exception_refuses_only_that_step(w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    skill = write(w.skills / "s" / "SKILL.md", "x").parent
    w.m.begin(rec(0, undo=Undo("mcp_remove", "srv", None, "user")))
    w.m.begin(rec(1, undo=Undo("delete_path", str(skill), None, None)))

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("texto-interno-de-la-excepcion")

    with monkeypatch.context() as patch:
        patch.setattr(shutil, "rmtree", boom)
        messages = w.revert()

    assert len(messages) == 2
    assert "RuntimeError" in messages[0] and "no se revirtió" in messages[0]
    assert "texto-interno-de-la-excepcion" not in messages[0]
    assert skill.is_dir()
    assert [r.status for r in w.m.records()] == ["reverted", "pending"]
    assert len(w.calls) == 1


def test_manifest_failure_mid_revert_reports_what_was_undone(w: World) -> None:
    w.m.begin(rec(0, undo=Undo("mcp_remove", "uno", None, "user")))
    w.m.begin(rec(1, undo=Undo("mcp_remove", "dos", None, "user")))

    def corrupting(argv: list[str]) -> int:
        w.calls.append(argv)
        if argv[-1] == "uno":
            w.path.write_text("{ roto", encoding="utf-8")
        return 0

    with pytest.raises(ManifestError) as info:
        revert(w.m, RUN, corrupting, w.env)

    text = str(info.value)
    assert "paso 1" in text and "'dos'" in text  # deshecho y marcado
    assert "paso 0" in text and "'uno'" in text  # deshecho, no se pudo marcar
    assert [call[-1] for call in w.calls] == ["dos", "uno"]
