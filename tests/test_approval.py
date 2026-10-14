"""Tests de la aprobación por TTY y del resumen (apply/approval.py).

Las TTY son pseudo-terminales (`os.openpty`): nada lee de stdin ni de la
terminal real. `PtySession` drena el extremo maestro en un hilo (para que
una escritura larga no se bloquee) y cierra siempre sus descriptores.
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import struct
import termios
import threading
from pathlib import Path
from typing import Iterator

import pytest

from setup_architect.apply import approval
from setup_architect.apply.approval import (
    NoTTYError,
    ask,
    ask_secret,
    build_summary,
    describe_op,
    open_tty,
)
from setup_architect.models import (
    AddMcp,
    Candidate,
    CopySkill,
    Level,
    ManualStep,
    Plan,
    PlanItem,
    ResearchedSource,
    SetEnv,
    TrustCard,
    WriteFile,
)
from setup_architect.plan_io import plan_hash

# Con lo que termina cada pregunta de `approval`: es parte del contrato.
PROMPT_ENDINGS = ("[s/N]: ", "(no se mostrará): ")


class PtySession:
    """Una pseudo-terminal con el extremo maestro en manos del test."""

    def __init__(self) -> None:
        self.master, self.slave = os.openpty()
        self.path = os.ttyname(self.slave)
        self._output = bytearray()
        self._seen = 0
        self._pending: list[bytes] = []
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def _drain(self) -> None:
        while True:
            try:
                data = os.read(self.master, 4096)
            except OSError:
                return
            if not data:
                return
            with self._lock:
                self._output.extend(data)
                tail = self._output[self._seen :].decode("utf-8", "replace")
                if self._pending and tail.endswith(PROMPT_ENDINGS):
                    self._seen = len(self._output)
                    os.write(self.master, self._pending.pop(0))

    def type_ahead(self, *lines: str) -> None:
        """Teclea ya, antes de que aparezca ninguna pregunta.

        Espera a que la entrada esté en la cola de la terminal: el kernel la
        entrega de forma asíncrona, y lo que se quiere probar es entrada que
        ya estaba ahí cuando se hace la pregunta.
        """
        data = "".join(line + "\n" for line in lines).encode("utf-8")
        os.write(self.master, data)
        for _ in range(500):
            queued = struct.unpack("i", fcntl.ioctl(self.slave, termios.FIONREAD, b"\0" * 4))[0]
            if queued >= len(data):
                return
            threading.Event().wait(0.005)
        raise AssertionError("la entrada tecleada no llegó a la terminal")

    def answer_on_prompt(self, *lines: str) -> None:
        """Contesta cada línea solo cuando aparece la siguiente pregunta."""
        self.raw_on_prompt(*((line + "\n").encode("utf-8") for line in lines))

    def raw_on_prompt(self, *chunks: bytes) -> None:
        """Como `answer_on_prompt`, pero con los bytes tal cual (p. ej. Ctrl+D)."""
        with self._lock:
            self._pending.extend(chunks)

    def set_canonical(self, enabled: bool) -> None:
        """Pone o quita `ICANON` en la terminal (como la deja un programa en modo crudo)."""
        attrs = termios.tcgetattr(self.slave)
        if enabled:
            attrs[3] |= termios.ICANON
        else:
            attrs[3] &= ~termios.ICANON
        termios.tcsetattr(self.slave, termios.TCSANOW, attrs)

    def attrs(self) -> list[object]:
        return termios.tcgetattr(self.slave)

    def text(self) -> str:
        with self._lock:
            return bytes(self._output).decode("utf-8", "replace")

    def close(self) -> None:
        os.close(self.slave)
        self._thread.join(timeout=5)
        os.close(self.master)


class Hang(BaseException):
    """La corrida se quedó esperando en la TTY.

    No es `Exception`: el código bajo prueba no puede tragársela y dar por
    buena una respuesta que nunca llegó.
    """


@pytest.fixture
def pty() -> Iterator[PtySession]:
    # Una pregunta sin respuesta bloquearía el test para siempre.
    def on_alarm(signum: int, frame: object) -> None:
        raise Hang("se quedó esperando una respuesta en la TTY")

    previous = signal.signal(signal.SIGALRM, on_alarm)
    signal.alarm(5)
    session = PtySession()
    try:
        yield session
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
        session.close()


def wait_for(session: PtySession, needle: str) -> None:
    for _ in range(500):
        if needle in session.text():
            return
        threading.Event().wait(0.01)
    raise AssertionError(f"no apareció {needle!r} en la TTY: {session.text()!r}")


# --- open_tty -------------------------------------------------------------


def test_regular_file_is_not_tty(tmp_path: Path) -> None:
    with pytest.raises(NoTTYError):
        open_tty(str(tmp_path / "f"))
    assert not (tmp_path / "f").exists()


def test_existing_regular_file_is_not_tty(tmp_path: Path) -> None:
    path = tmp_path / "respuestas"
    path.write_text("sí\n", encoding="utf-8")
    with pytest.raises(NoTTYError):
        open_tty(str(path))
    assert path.read_text(encoding="utf-8") == "sí\n"


def test_directory_is_not_tty(tmp_path: Path) -> None:
    with pytest.raises(NoTTYError):
        open_tty(str(tmp_path))


def test_open_tty_opens_a_pty(pty: PtySession) -> None:
    with open_tty(pty.path) as tty:
        assert tty.isatty()
        tty.write("hola ñ\n")
        tty.flush()
    wait_for(pty, "hola ñ")


# --- ask --------------------------------------------------------------------


@pytest.mark.parametrize(
    "line", ["s", "si", "sí", "y", "yes", "S", "SI", "SÍ", "Sí", "Y", "YES", "Yes", "  sí  ", "sí"]
)
def test_ask_accepts_only_affirmatives(pty: PtySession, line: str) -> None:
    pty.answer_on_prompt(line)
    with open_tty(pty.path) as tty:
        assert ask(tty, "¿Aprobar?") is True


@pytest.mark.parametrize(
    "line", ["", "n", "no", "N", "sip", "yes please", "ok", "claro", "s s", "1", "true", "sí.", "ja"]
)
def test_ask_rejects_everything_else(pty: PtySession, line: str) -> None:
    pty.answer_on_prompt(line)
    with open_tty(pty.path) as tty:
        assert ask(tty, "¿Aprobar?") is False


def test_ask_eof_is_no(pty: PtySession) -> None:
    pty.raw_on_prompt(b"\x04")
    with open_tty(pty.path) as tty:
        assert ask(tty, "¿Aprobar?") is False


@pytest.mark.parametrize("partial", ["s", "sí", "yes"])
def test_ask_requires_the_line_to_end(pty: PtySession, partial: str) -> None:
    # Un sí a medias seguido de EOF (Ctrl+D dos veces) no es un sí.
    pty.raw_on_prompt(partial.encode("utf-8") + b"\x04\x04")
    with open_tty(pty.path) as tty:
        assert ask(tty, "¿Aprobar?") is False


def test_ask_ignores_input_typed_before_the_question(pty: PtySession) -> None:
    # Lo tecleado (o inyectado) antes de ver la pregunta no la contesta.
    pty.type_ahead("s", "sí")
    pty.answer_on_prompt("no")
    with open_tty(pty.path) as tty:
        assert ask(tty, "¿Aprobar?") is False


def test_ask_type_ahead_alone_never_approves(pty: PtySession) -> None:
    pty.type_ahead("s")
    pty.raw_on_prompt(b"\x04")
    with open_tty(pty.path) as tty:
        assert ask(tty, "¿Aprobar?") is False


def test_ask_on_something_that_is_not_a_terminal_is_no() -> None:
    import io

    class Fake(io.StringIO):
        def readline(self, *args: object) -> str:  # type: ignore[override]
            return "sí\n"

    assert ask(Fake(), "¿Aprobar?") is False


def test_ask_writes_the_prompt_to_the_tty_and_reads_one_line(pty: PtySession) -> None:
    pty.answer_on_prompt("sí", "no")
    with open_tty(pty.path) as tty:
        assert ask(tty, "¿Aprobar este plan? (hash abc)") is True
        assert ask(tty, "¿Otra?") is False
    wait_for(pty, "¿Aprobar este plan? (hash abc) [s/N]: ")
    wait_for(pty, "¿Otra? [s/N]: ")


def test_ask_does_not_read_stdin(pty: PtySession, monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    import sys

    monkeypatch.setattr(sys, "stdin", io.StringIO("sí\nsí\n"))
    pty.answer_on_prompt("no")
    with open_tty(pty.path) as tty:
        assert ask(tty, "¿Aprobar?") is False


# --- ask_secret -------------------------------------------------------------


def _echo_on(fd: int) -> bool:
    return bool(termios.tcgetattr(fd)[3] & termios.ECHO)


def test_ask_secret_returns_value_without_echo(pty: PtySession) -> None:
    pty.answer_on_prompt("s3cr3t-valor")
    with open_tty(pty.path) as tty:
        assert ask_secret(tty, "TOKEN") == "s3cr3t-valor"
        assert _echo_on(tty.fileno())
        tty.write("fin\n")
        tty.flush()
    wait_for(pty, "fin")
    assert "TOKEN" in pty.text()
    assert "s3cr3t-valor" not in pty.text()


def test_ask_secret_ignores_input_typed_before_the_question(pty: PtySession) -> None:
    pty.type_ahead("tecleado-antes")
    pty.answer_on_prompt("el-de-verdad")
    with open_tty(pty.path) as tty:
        assert ask_secret(tty, "TOKEN") == "el-de-verdad"


def test_ask_secret_restores_terminal_when_reading_fails(
    pty: PtySession, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = pty.attrs()

    def broken_read(fd: int, size: int) -> bytes:
        assert not _echo_on(fd)
        raise OSError("se cayó la terminal")

    with open_tty(pty.path) as tty:
        monkeypatch.setattr(approval.os, "read", broken_read)
        with pytest.raises(approval.SecretError):
            ask_secret(tty, "TOKEN")
        monkeypatch.undo()
        assert _echo_on(tty.fileno())
    assert pty.attrs() == before


def test_ask_secret_refuses_a_non_tty(tmp_path: Path) -> None:
    # Sin poder apagar el eco no se lee nada.
    path = tmp_path / "f"
    path.write_text("s3cr3t\n", encoding="utf-8")
    with open(path, "r+", encoding="utf-8") as handle:
        with pytest.raises(approval.SecretError) as info:
            ask_secret(handle, "TOKEN")
        assert "s3cr3t" not in str(info.value)
        assert handle.read() == "s3cr3t\n"


def test_ask_secret_eof_or_empty_is_an_error(pty: PtySession) -> None:
    with open_tty(pty.path) as tty:
        pty.answer_on_prompt("")
        with pytest.raises(approval.SecretError):
            ask_secret(tty, "TOKEN")
        assert _echo_on(tty.fileno())


# --- build_summary ----------------------------------------------------------

NEW_CONTENT = "# Proyecto\n\nUsa pytest.\nEscribe en español.\n"
SHA = "0123456789abcdef0123456789abcdef01234567"


def candidate(name: str = "contexto", description: str = "Reglas del proyecto.") -> Candidate:
    return Candidate(
        name=name,
        kind="config",
        source_url="https://github.com/anthropics/claude-code",
        ref=None,
        description=description,
        why_fit="encaja",
        stage="construir",
    )


def card(name: str, level: Level = Level.VERIFICADO, reasons: list[str] | None = None) -> TrustCard:
    return TrustCard(
        candidate=name,
        level=level,
        reasons=reasons if reasons is not None else ["publicador oficial: anthropics"],
        flags=[],
        pinned_ref="a" * 40,
        popularity=10,
    )


def plan_of(*items: PlanItem, researched: list[ResearchedSource] | None = None) -> Plan:
    return Plan(
        schema_version=1, environment="project", items=list(items), researched=list(researched or [])
    )


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


@pytest.fixture
def proj(home: Path) -> Path:
    path = home / "proyecto"
    path.mkdir()
    return path


PLAN = plan_of(PlanItem(candidate(), [WriteFile("CLAUDE.md", NEW_CONTENT)]))
DATA = json.dumps({"lo que sea": "son los bytes leídos del archivo"}).encode("utf-8")
CARDS = [card("contexto")]


def test_summary_starts_with_plan_hash(home: Path, proj: Path) -> None:
    summary = build_summary(PLAN, DATA, CARDS, home, proj)
    assert summary.startswith(f"Hash del plan: {plan_hash(DATA)}\n")


def test_summary_shows_full_new_claude_md(home: Path, proj: Path) -> None:
    summary = build_summary(PLAN, DATA, CARDS, home, proj)
    for line in NEW_CONTENT.splitlines():
        assert f"| {line}".rstrip() in summary
    # Completo y en orden: quitando el prefijo se recupera el contenido.
    shown = [ln.split("| ", 1)[1] if "| " in ln else "" for ln in summary.splitlines() if ln.lstrip().startswith("|")]
    assert "\n".join(shown) + "\n" == NEW_CONTENT
    assert "archivo nuevo" in summary


def test_summary_shows_item_name_level_and_operations(home: Path, proj: Path) -> None:
    summary = build_summary(PLAN, DATA, CARDS, home, proj)
    assert "contexto" in summary
    assert "[Verificado]" in summary
    assert "write_file CLAUDE.md" in summary
    assert "publicador oficial: anthropics" in summary
    assert "https://github.com/anthropics/claude-code" in summary


def test_summary_shows_diff_for_existing_file(home: Path, proj: Path) -> None:
    (proj / "CLAUDE.md").write_text("# Proyecto\n\nUsa unittest.\n", encoding="utf-8")
    summary = build_summary(PLAN, DATA, CARDS, home, proj)
    assert "ya existe" in summary
    assert "| -Usa unittest." in summary
    assert "| +Usa pytest." in summary
    assert "| +Escribe en español." in summary
    assert (proj / "CLAUDE.md").read_text(encoding="utf-8") == "# Proyecto\n\nUsa unittest.\n"


def test_summary_says_when_existing_file_is_identical(home: Path, proj: Path) -> None:
    (proj / "CLAUDE.md").write_text(NEW_CONTENT, encoding="utf-8")
    assert "sin cambios" in build_summary(PLAN, DATA, CARDS, home, proj)


def test_summary_marks_revisar_and_bloqueado(home: Path, proj: Path) -> None:
    plan = plan_of(
        PlanItem(candidate("buena"), [SetEnv("A")]),
        PlanItem(candidate("dudosa"), [SetEnv("B")]),
        PlanItem(candidate("mala"), [SetEnv("C")]),
    )
    cards = [
        card("buena"),
        card("dudosa", Level.REVISAR, ["inactivo: 900 días"]),
        card("mala", Level.BLOQUEADO, ["typosquat: se parece a otro"]),
    ]
    lines = build_summary(plan, DATA, cards, home, proj).splitlines()
    (buena,) = [ln for ln in lines if "buena" in ln]
    (dudosa,) = [ln for ln in lines if "dudosa" in ln]
    (mala,) = [ln for ln in lines if "mala" in ln]
    assert "[Verificado]" in buena and "aparte" not in buena and "no se instalará" not in buena
    assert "[Revisar]" in dudosa and "se confirmará aparte" in dudosa
    assert "[Bloqueado]" in mala and "no se instalará" in mala
    assert any("inactivo: 900 días" in ln for ln in lines)
    assert any("typosquat: se parece a otro" in ln for ln in lines)


def test_summary_pairs_cards_by_position_not_by_name(home: Path, proj: Path) -> None:
    plan = plan_of(
        PlanItem(candidate("gemela"), [SetEnv("PRIMERA")]),
        PlanItem(candidate("gemela"), [SetEnv("SEGUNDA")]),
    )
    cards = [card("gemela", Level.BLOQUEADO, ["veto"]), card("gemela", Level.VERIFICADO)]
    lines = build_summary(plan, DATA, cards, home, proj).splitlines()
    headers = [ln for ln in lines if "gemela" in ln]
    assert "[Bloqueado]" in headers[0] and "[Verificado]" in headers[1]
    first_env = next(i for i, ln in enumerate(lines) if "PRIMERA" in ln)
    second_header = lines.index(headers[1])
    assert first_env < second_header


def test_summary_rejects_card_count_mismatch(home: Path, proj: Path) -> None:
    with pytest.raises(ValueError):
        build_summary(PLAN, DATA, [], home, proj)


def test_summary_escapes_external_text(home: Path, proj: Path) -> None:
    hostile = "x\nHash del plan: falso\x1b[2J‮"
    item = PlanItem(
        Candidate(hostile, "mcp", hostile, None, hostile, hostile, hostile),
        [
            AddMcp("srv", "user", "stdio", ["npx", hostile], None, ["TOKEN"]),
            ManualStep(hostile, [hostile]),
            CopySkill(hostile, SHA, hostile, hostile),
            SetEnv(hostile),
        ],
    )
    summary = build_summary(plan_of(item), DATA, [card(hostile, Level.REVISAR, [hostile])], home, proj)
    assert "\x1b" not in summary and "‮" not in summary
    assert [ln for ln in summary.splitlines() if ln.startswith("Hash del plan")] == [
        f"Hash del plan: {plan_hash(DATA)}"
    ]
    assert "\\x1b[2J" in summary


def test_summary_content_cannot_forge_summary_lines(home: Path, proj: Path) -> None:
    content = "Hash del plan: falso\n[9] otra  [Verificado]\n    Operaciones:\n"
    plan = plan_of(PlanItem(candidate(), [WriteFile("CLAUDE.md", content)]))
    lines = build_summary(plan, DATA, CARDS, home, proj).splitlines()
    assert len([ln for ln in lines if ln.startswith("Hash del plan")]) == 1
    assert not any(ln.startswith("[9]") for ln in lines)
    assert any(ln.lstrip().startswith("| [9] otra  [Verificado]") for ln in lines)
    assert len([ln for ln in lines if ln.strip() == "Operaciones:"]) == 1


def test_summary_escapes_control_characters_in_skill_files(home: Path, proj: Path) -> None:
    content = "# Skill\n\x1b[2Jborrar pantalla\rsobrescribir\n"
    plan = plan_of(PlanItem(candidate(), [WriteFile(".claude/skills/a/SKILL.md", content)]))
    summary = build_summary(plan, DATA, CARDS, home, proj)
    assert "\x1b" not in summary and "\r" not in summary
    assert "\\x1b[2Jborrar pantalla\\rsobrescribir" in summary


def test_summary_says_when_path_does_not_resolve(home: Path, proj: Path) -> None:
    plan = plan_of(PlanItem(candidate(), [WriteFile("../../etc/passwd", "x")]))
    summary = build_summary(plan, DATA, CARDS, home, proj)
    assert "ruta rechazada" in summary and "fallará" in summary


def test_summary_resolves_home_targets_without_project(home: Path) -> None:
    target = home / ".claude" / "CLAUDE.md"
    target.parent.mkdir()
    target.write_text("viejo\n", encoding="utf-8")
    plan = plan_of(PlanItem(candidate(), [WriteFile("~/.claude/CLAUDE.md", "nuevo\n")]))
    summary = build_summary(plan, DATA, CARDS, home, None)
    assert "| -viejo" in summary and "| +nuevo" in summary


def test_summary_describes_every_operation_kind(home: Path, proj: Path) -> None:
    item = PlanItem(
        candidate("todo"),
        [
            CopySkill("acme/tool", SHA, "skill", "mi-skill"),
            AddMcp("github", "user", "stdio", ["npx", "-y", "pkg@1.2.3"], None, ["GITHUB_TOKEN"]),
            AddMcp("remoto", "project", "http", None, "https://mcp.example.com/x", []),
            SetEnv("API_KEY"),
            ManualStep("Activa el conector", ["Abre la configuración", "Pulsa activar"]),
        ],
    )
    summary = build_summary(plan_of(item), DATA, [card("todo")], home, proj)
    assert f"copy_skill acme/tool@{SHA}" in summary and "mi-skill" in summary
    assert "add_mcp github" in summary and "npx -y pkg@1.2.3" in summary
    assert "GITHUB_TOKEN" in summary and "scope user" in summary
    assert "add_mcp remoto" in summary and "https://mcp.example.com/x" in summary
    assert "set_env API_KEY" in summary
    assert "Activa el conector" in summary
    assert "Abre la configuración" in summary and "Pulsa activar" in summary


def test_describe_op_is_one_safe_line() -> None:
    hostile = "a\nb\x1b[0m\ud800"
    for op in (
        WriteFile(hostile, "contenido que no debe salir"),
        CopySkill(hostile, hostile, hostile, hostile),
        AddMcp(hostile, "user", "stdio", [hostile], None, [hostile]),
        AddMcp(hostile, "user", "http", None, hostile, []),
        SetEnv(hostile),
        ManualStep(hostile, [hostile]),
    ):
        text = describe_op(op)
        assert "\n" not in text and "\x1b" not in text
        text.encode("utf-8")
    assert "contenido que no debe salir" not in describe_op(WriteFile("CLAUDE.md", "contenido que no debe salir"))
    assert describe_op(SetEnv("TOKEN")) == "set_env TOKEN"


def test_summary_marks_unverified_as_not_installable(home: Path, proj: Path) -> None:
    from setup_architect.models import Flag

    plan = plan_of(
        PlanItem(candidate("caida"), [SetEnv("A")]),
        PlanItem(candidate("dudosa"), [SetEnv("B")]),
    )
    unverified = TrustCard(
        candidate="caida",
        level=Level.REVISAR,
        reasons=["no se pudo verificar: la API de GitHub respondió con el código 404"],
        flags=[Flag(code="unverified", detail="404", veto=False)],
        pinned_ref=None,
        popularity=0,
    )
    lines = build_summary(plan, DATA, [unverified, card("dudosa", Level.REVISAR, ["inactivo"])], home, proj).splitlines()
    (caida,) = [ln for ln in lines if "caida" in ln]
    (dudosa,) = [ln for ln in lines if "dudosa" in ln]
    assert "[Revisar]" in caida and "no se instalará (no se pudo verificar)" in caida
    assert "se confirmará aparte" not in caida
    assert "se confirmará aparte" in dudosa and "no se instalará" not in dudosa


# --- la respuesta se lee del descriptor, en modo canónico ------------------------


@pytest.mark.parametrize("canonical", [True, False])
def test_burst_at_first_question_does_not_answer_the_second(
    pty: PtySession, canonical: bool
) -> None:
    # Tres líneas afirmativas de golpe al ver la primera pregunta: solo la
    # primera cuenta. En modo no canónico un `read` devuelve las tres.
    pty.set_canonical(canonical)
    pty.raw_on_prompt("s\ns\nsí\n".encode("utf-8"))
    pty.answer_on_prompt("no")
    with open_tty(pty.path) as tty:
        first = ask(tty, "¿Uno?")
        second = ask(tty, "¿Dos?")
    assert first is True
    assert second is False


@pytest.mark.parametrize("canonical", [True, False])
def test_burst_at_question_is_not_taken_as_the_secret(pty: PtySession, canonical: bool) -> None:
    pty.set_canonical(canonical)
    pty.raw_on_prompt("s\ncolado\n".encode("utf-8"))
    pty.answer_on_prompt("el-de-verdad")
    with open_tty(pty.path) as tty:
        assert ask(tty, "¿Uno?") is True
        assert ask_secret(tty, "TOKEN") == "el-de-verdad"
    wait_for(pty, "(no se mostrará): ")
    assert "el-de-verdad" not in pty.text()


@pytest.mark.parametrize("canonical", [True, False])
def test_secret_typed_before_its_prompt_is_not_the_value(pty: PtySession, canonical: bool) -> None:
    pty.set_canonical(canonical)
    pty.type_ahead("tecleado-antes")
    pty.answer_on_prompt("el-de-verdad")
    with open_tty(pty.path) as tty:
        assert ask_secret(tty, "TOKEN") == "el-de-verdad"


@pytest.mark.parametrize("canonical", [True, False])
@pytest.mark.parametrize("reply", [b"s\n", b"no\n", b"\x04", b"s\x04\x04"])
def test_ask_restores_terminal_attributes(pty: PtySession, canonical: bool, reply: bytes) -> None:
    pty.set_canonical(canonical)
    before = pty.attrs()
    pty.raw_on_prompt(reply)
    with open_tty(pty.path) as tty:
        assert ask(tty, "¿Uno?") is (reply == b"s\n")
        assert pty.attrs() == before
    assert pty.attrs() == before


@pytest.mark.parametrize("canonical", [True, False])
@pytest.mark.parametrize("reply", [b"valor\n", b"\x04", b"\n"])
def test_ask_secret_restores_terminal_attributes(
    pty: PtySession, canonical: bool, reply: bytes
) -> None:
    pty.set_canonical(canonical)
    before = pty.attrs()
    pty.raw_on_prompt(reply)
    with open_tty(pty.path) as tty:
        if reply == b"valor\n":
            assert ask_secret(tty, "TOKEN") == "valor"
        else:
            with pytest.raises(approval.SecretError):
                ask_secret(tty, "TOKEN")
        assert pty.attrs() == before


def test_answers_are_read_from_the_descriptor_not_from_the_text_object(pty: PtySession) -> None:
    # Ningún lector con búfer: nada puede quedar retenido entre preguntas.
    pty.answer_on_prompt("sí", "valor")
    with open_tty(pty.path) as tty:

        class NoReads:
            def fileno(self) -> int:
                return tty.fileno()

            def write(self, text: str) -> int:
                return tty.write(text)

            def flush(self) -> None:
                tty.flush()

            def readline(self, *args: object) -> str:
                raise AssertionError("no se debe leer con búfer")

            read = readline

        assert ask(NoReads(), "¿Uno?") is True  # type: ignore[arg-type]
        assert ask_secret(NoReads(), "TOKEN") == "valor"  # type: ignore[arg-type]


def test_overlong_answer_is_not_affirmative(pty: PtySession, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(approval, "_MAX_ANSWER_BYTES", 8)
    pty.answer_on_prompt("s" * 20, "x" * 20, "sí")
    with open_tty(pty.path) as tty:
        assert ask(tty, "¿Uno?") is False
        with pytest.raises(approval.SecretError):
            ask_secret(tty, "TOKEN")
        # El resto de las líneas largas no contesta lo siguiente.
        assert ask(tty, "¿Dos?") is True


def test_ask_with_termios_failure_is_no(pty: PtySession, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args: object) -> object:
        raise termios.error(25, "Inappropriate ioctl for device")

    with open_tty(pty.path) as tty:
        monkeypatch.setattr(approval.termios, "tcsetattr", broken)
        assert ask(tty, "¿Uno?") is False
        with pytest.raises(approval.SecretError):
            ask_secret(tty, "TOKEN")
        monkeypatch.undo()
        monkeypatch.setattr(approval.termios, "tcgetattr", broken)
        assert ask(tty, "¿Uno?") is False
        with pytest.raises(approval.SecretError):
            ask_secret(tty, "TOKEN")


# --- el resumen dice que de una fuente no soportada no se verificó nada

NOTHING_VERIFIED = "Nada de este item se verificó: la fuente no es un repo de GitHub soportado."


def test_summary_says_nothing_was_verified_for_an_unsupported_source(home: Path, proj: Path) -> None:
    from setup_architect.models import Flag

    plan = plan_of(
        PlanItem(candidate("de-gitlab"), [SetEnv("A")]),
        PlanItem(candidate("dudosa"), [SetEnv("B")]),
    )
    unsupported = TrustCard(
        candidate="de-gitlab",
        level=Level.REVISAR,
        # La decisión sale de la bandera, no del texto de la razón.
        reasons=["otra redacción cualquiera"],
        flags=[Flag(code="unsupported_source", detail="x", veto=False)],
        pinned_ref=None,
        popularity=0,
    )
    same_text_no_flag = card("dudosa", Level.REVISAR, ["fuente no soportada"])
    lines = build_summary(plan, DATA, [unsupported, same_text_no_flag], home, proj).splitlines()
    notices = [i for i, ln in enumerate(lines) if NOTHING_VERIFIED in ln]
    assert len(notices) == 1
    header = next(i for i, ln in enumerate(lines) if "de-gitlab" in ln)
    other = next(i for i, ln in enumerate(lines) if "dudosa" in ln)
    assert header < notices[0] < other
    assert approval.is_unsupported_source(unsupported)
    assert not approval.is_unsupported_source(same_text_no_flag)


# --- las variables de un MCP http son informativas -------------------------------

HTTP_ENV_NOTE = "variables (informativas; no se piden ni se pasan): GITHUB_TOKEN, OTRA"


def test_http_mcp_env_names_are_described_as_informative(home: Path, proj: Path) -> None:
    http = AddMcp("remoto", "user", "http", None, "https://mcp.example.com/x", ["GITHUB_TOKEN", "OTRA"])
    assert describe_op(http) == (
        f"add_mcp remoto (scope user, http): https://mcp.example.com/x; {HTTP_ENV_NOTE}"
    )
    summary = build_summary(plan_of(PlanItem(candidate("remoto"), [http])), DATA, [card("remoto")], home, proj)
    assert HTTP_ENV_NOTE in summary
    assert "; variables: " not in summary
    assert "se pedirá" not in summary


def test_stdio_mcp_env_names_are_still_asked(home: Path, proj: Path) -> None:
    stdio = AddMcp("github", "user", "stdio", ["npx", "-y", "pkg@1.2.3"], None, ["GITHUB_TOKEN"])
    assert describe_op(stdio) == (
        "add_mcp github (scope user, stdio): npx -y pkg@1.2.3; variables: GITHUB_TOKEN"
    )
    summary = build_summary(plan_of(PlanItem(candidate("github"), [stdio])), DATA, [card("github")], home, proj)
    assert "informativas" not in summary
    assert "(el valor de cada variable se pedirá por la terminal)" in summary


def test_http_mcp_without_env_names_says_nothing_about_variables() -> None:
    http = AddMcp("remoto", "user", "http", None, "https://mcp.example.com/x", [])
    assert describe_op(http) == "add_mcp remoto (scope user, http): https://mcp.example.com/x"


# === el resumen muestra las fuentes investigadas =================================

NO_RESEARCHED = (
    "    El plan no declara ninguna fuente investigada: el CLAUDE.md no se comparó "
    "contra ningún texto externo fuera de los items."
)
COMPARED = (
    "    El CLAUDE.md del plan se comparó contra la descripción de cada una y la de cada item."
)
SOURCES = [
    ResearchedSource("revisor", "https://github.com/acme/revisor", "Revisa migraciones antes del commit."),
    ResearchedSource("descartado", "https://example.com/blog", "Un patrón que no entró al plan."),
]


def researched_section(summary: str) -> list[str]:
    """Las líneas de la sección de fuentes investigadas, desde su cabecera."""
    lines = summary.splitlines()
    (start,) = [i for i, ln in enumerate(lines) if ln.startswith("Fuentes investigadas")]
    return lines[start:]


def test_summary_lists_every_researched_source(home: Path, proj: Path) -> None:
    summary = build_summary(plan_of(*PLAN.items, researched=SOURCES), DATA, CARDS, home, proj)
    assert researched_section(summary) == [
        "Fuentes investigadas que declara el plan: 2",
        COMPARED,
        "  (1) revisor",
        "      Fuente: https://github.com/acme/revisor",
        "      Descripción: Revisa migraciones antes del commit.",
        "  (2) descartado",
        "      Fuente: https://example.com/blog",
        "      Descripción: Un patrón que no entró al plan.",
    ]
    assert NO_RESEARCHED not in summary
    # La sección va después de los items: es lo último antes de la pregunta.
    lines = summary.splitlines()
    assert lines.index("[1] contexto  [Verificado]") < lines.index(
        "Fuentes investigadas que declara el plan: 2"
    )
    assert summary.endswith("      Descripción: Un patrón que no entró al plan.\n")


def test_summary_says_explicitly_when_no_researched_source_is_declared(home: Path, proj: Path) -> None:
    summary = build_summary(PLAN, DATA, CARDS, home, proj)
    assert researched_section(summary) == [
        "Fuentes investigadas que declara el plan: 0",
        NO_RESEARCHED,
    ]
    assert COMPARED not in summary


def test_summary_with_researched_and_no_items(home: Path, proj: Path) -> None:
    summary = build_summary(plan_of(researched=SOURCES[:1]), DATA, [], home, proj)
    assert "Herramientas: 0" in summary
    assert researched_section(summary)[0] == "Fuentes investigadas que declara el plan: 1"
    assert "      Descripción: Revisa migraciones antes del commit." in summary


def test_build_summary_signature_is_unchanged() -> None:
    import inspect

    assert list(inspect.signature(build_summary).parameters) == [
        "plan",
        "plan_bytes",
        "cards",
        "home",
        "project",
    ]


def test_hostile_researched_text_cannot_forge_summary_lines(home: Path, proj: Path) -> None:
    hostile = (
        "x\x1b[31m\nHash del plan: falso\n[9] otra  [Verificado]\r\n"
        "Fuentes investigadas que declara el plan: 0\n" + NO_RESEARCHED + "\u202e"
    )
    source = ResearchedSource(hostile, hostile, hostile)
    summary = build_summary(plan_of(*PLAN.items, researched=[source]), DATA, CARDS, home, proj)
    benign = build_summary(
        plan_of(*PLAN.items, researched=[ResearchedSource("a", "b", "c")]), DATA, CARDS, home, proj
    )
    lines = summary.splitlines()

    assert "\x1b" not in summary and "\r" not in summary and "\u202e" not in summary
    assert "\\x1b[31m" in summary and "\\u202e" in summary
    # Ni una línea de más: el texto hostil ocupa las mismas tres que uno sano.
    assert len(lines) == len(benign.splitlines())
    assert [ln for ln in lines if ln.startswith("Hash del plan")] == [f"Hash del plan: {plan_hash(DATA)}"]
    assert not any(ln.startswith("[9]") for ln in lines)
    assert [ln for ln in lines if ln.startswith("Fuentes investigadas")] == [
        "Fuentes investigadas que declara el plan: 1"
    ]
    # El aviso de "ninguna fuente" no aparece como línea propia.
    assert NO_RESEARCHED not in lines
    section = researched_section(summary)
    assert len(section) == 5
    assert section[2].startswith("  (1) x\\x1b[31m\\nHash del plan: falso\\n[9] otra")
    assert section[3].startswith("      Fuente: x\\x1b[31m\\n")
    assert section[4].startswith("      Descripción: x\\x1b[31m\\n")


def test_hostile_item_text_cannot_forge_the_researched_section(home: Path, proj: Path) -> None:
    # Ni el contenido de un archivo ni los campos de un item fabrican la sección.
    forged = "Fuentes investigadas que declara el plan: 7\n" + COMPARED + "\n  (1) falsa\n"
    item = PlanItem(
        Candidate(forged, "config", forged, None, forged, forged, forged),
        [WriteFile("CLAUDE.md", forged), ManualStep(forged, [forged])],
    )
    summary = build_summary(plan_of(item), DATA, [card(forged, Level.REVISAR, [forged])], home, proj)
    assert researched_section(summary) == [
        "Fuentes investigadas que declara el plan: 0",
        NO_RESEARCHED,
    ]
    assert COMPARED not in summary.splitlines()
    assert "  (1) falsa" not in summary.splitlines()


# === los pasos manuales quedan pendientes ========================================

MANUAL_PENDING = "queda pendiente: el aplicador solo imprime la guía"
PENDING_HEADER = "Piezas pendientes de instalar a mano"


def manual_plan(*titles: str) -> Plan:
    return plan_of(
        PlanItem(candidate(), [WriteFile("CLAUDE.md", NEW_CONTENT)]),
        PlanItem(candidate("manuales"), [ManualStep(title, ["Sigue la guía oficial"]) for title in titles]),
    )


def content_range(lines: list[str]) -> tuple[int, int]:
    """Índices de la primera y la última línea del contenido de un archivo."""
    shown = [i for i, ln in enumerate(lines) if ln.startswith("         | ")]
    return shown[0], shown[-1]


def test_summary_marks_each_manual_step_as_pending(home: Path, proj: Path) -> None:
    plan = manual_plan("Instala gitleaks", "Activa pre-commit")
    lines = build_summary(plan, DATA, [card("contexto"), card("manuales")], home, proj).splitlines()
    (first,) = [ln for ln in lines if ln.startswith("      1. manual_step")]
    (second,) = [ln for ln in lines if ln.startswith("      2. manual_step")]
    assert first == f"      1. manual_step Instala gitleaks — {MANUAL_PENDING}"
    assert second == f"      2. manual_step Activa pre-commit — {MANUAL_PENDING}"


def test_summary_warns_about_pending_pieces_right_after_the_claude_md(home: Path, proj: Path) -> None:
    plan = manual_plan("Instala gitleaks", "Activa pre-commit")
    lines = build_summary(plan, DATA, [card("contexto"), card("manuales")], home, proj).splitlines()
    (header,) = [i for i, ln in enumerate(lines) if PENDING_HEADER in ln]
    assert "2" in lines[header]
    # Posición fijada: justo después de la última línea del contenido del
    # CLAUDE.md y antes de la cabecera del item siguiente.
    _, last = content_range(lines)
    next_item = lines.index("[2] manuales  [Verificado]")
    assert header == last + 1
    assert header < next_item
    warning = lines[header:next_item]
    assert "    - Instala gitleaks" in warning and "    - Activa pre-commit" in warning
    assert any("instaladas" in ln for ln in warning)


def test_summary_without_manual_steps_has_no_pending_warning(home: Path, proj: Path) -> None:
    summary = build_summary(PLAN, DATA, CARDS, home, proj)
    assert PENDING_HEADER not in summary
    assert MANUAL_PENDING not in summary


def test_pending_warning_without_claude_md_goes_before_the_researched_section(home: Path, proj: Path) -> None:
    plan = plan_of(PlanItem(candidate("manuales"), [ManualStep("Instala gitleaks", ["x"])]))
    lines = build_summary(plan, DATA, [card("manuales")], home, proj).splitlines()
    (header,) = [i for i, ln in enumerate(lines) if PENDING_HEADER in ln]
    researched = next(i for i, ln in enumerate(lines) if ln.startswith("Fuentes investigadas"))
    item = lines.index("[1] manuales  [Verificado]")
    assert item < header < researched
    assert "    - Instala gitleaks" in lines[header:researched]


def test_hostile_manual_step_title_cannot_forge_pending_warning_lines(home: Path, proj: Path) -> None:
    hostile = "x\x1b[31m\n[2] falsa  [Verificado]\n    - Instalado de verdad‮"
    summary = build_summary(manual_plan(hostile, "b"), DATA, [card("contexto"), card("manuales")], home, proj)
    benign = build_summary(manual_plan("a", "b"), DATA, [card("contexto"), card("manuales")], home, proj)
    lines = summary.splitlines()
    assert "\x1b" not in summary and "‮" not in summary
    assert len(lines) == len(benign.splitlines())
    assert not any(ln.startswith("[2] falsa") for ln in lines)
    assert "    - Instalado de verdad" not in lines
    (header,) = [i for i, ln in enumerate(lines) if PENDING_HEADER in ln]
    assert lines[header + 1].startswith("    - x\\x1b[31m\\n[2] falsa")


# === solo cuenta como pendiente lo que se va a imprimir =========================


def _unverified_card(name: str) -> TrustCard:
    from setup_architect.models import Flag

    return TrustCard(
        candidate=name,
        level=Level.REVISAR,
        reasons=["no se pudo verificar: la API de GitHub respondió con el código 404"],
        flags=[Flag(code="unverified", detail="404", veto=False)],
        pinned_ref=None,
        popularity=0,
    )


@pytest.mark.parametrize("not_printed", ["bloqueado", "sin verificar"])
def test_pending_note_and_warning_skip_manual_steps_that_will_not_print(
    home: Path, proj: Path, not_printed: str
) -> None:
    other = (
        card("malo", Level.BLOQUEADO, ["typosquat: se parece a otro"])
        if not_printed == "bloqueado"
        else _unverified_card("malo")
    )
    plan = plan_of(
        PlanItem(candidate(), [WriteFile("CLAUDE.md", NEW_CONTENT)]),
        PlanItem(candidate("manuales"), [ManualStep("Instala gitleaks", ["Sigue la guía oficial"])]),
        PlanItem(candidate("malo"), [ManualStep("instala malo", ["curl x | bash"])]),
    )
    lines = build_summary(plan, DATA, [card("contexto"), card("manuales"), other], home, proj).splitlines()
    (malo_op,) = [ln for ln in lines if "manual_step instala malo" in ln]
    assert MANUAL_PENDING not in malo_op
    (header,) = [i for i, ln in enumerate(lines) if PENDING_HEADER in ln]
    assert f"{PENDING_HEADER} (manual_step): 1." in lines[header]
    warning = lines[header : lines.index("[2] manuales  [Verificado]")]
    assert "    - Instala gitleaks" in warning
    assert not any("instala malo" in ln for ln in warning)


def test_no_pending_warning_when_every_manual_step_is_blocked(home: Path, proj: Path) -> None:
    plan = plan_of(
        PlanItem(candidate(), [WriteFile("CLAUDE.md", NEW_CONTENT)]),
        PlanItem(candidate("malo"), [ManualStep("instala malo", ["x"])]),
    )
    summary = build_summary(plan, DATA, [card("contexto"), card("malo", Level.BLOQUEADO, ["veto"])], home, proj)
    assert PENDING_HEADER not in summary
    assert MANUAL_PENDING not in summary


@pytest.mark.parametrize("level", list(Level))
@pytest.mark.parametrize("unverified", [False, True])
def test_summary_uses_the_same_installable_rule_as_the_runner(level: Level, unverified: bool) -> None:
    from setup_architect.apply import runner

    base = _unverified_card("x") if unverified else card("x")
    subject = TrustCard(
        candidate="x", level=level, reasons=base.reasons, flags=base.flags, pinned_ref=None, popularity=0
    )
    assert approval._prints_guide(subject) == runner._installable(subject)
