"""CLI `sarch`.

`main` arma un `argparse.ArgumentParser` con subparsers y despacha a
una función `_cmd_<subcomando>` por cada uno.

`main` nunca deja escapar una excepción no controlada por entrada del
usuario: cualquier problema de E/S, JSON, plan o configuración se
traduce a un mensaje en español por stderr y al código de salida 2.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import webbrowser
from pathlib import Path
from typing import Any, NoReturn

from setup_architect.apply.runner import run_apply, run_audit, run_revert
from setup_architect.config import Config, ConfigError, load_config
from setup_architect.models import Flag, Level, PlanItem, TrustCard
from setup_architect.plan_io import (
    PlanError,
    card_from_dict,
    card_to_dict,
    load_plan,
    parse_item,
)
from setup_architect.render.html import render_html
from setup_architect.render.text import render_text
from setup_architect.verify.pipeline import Deps, default_deps, verify_item

_DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
_LEVEL_ORDER = [Level.VERIFICADO, Level.CONFIABLE, Level.REVISAR, Level.BLOQUEADO]


class _UsageError(Exception):
    """Argumentos de línea de comandos inválidos."""


class _Parser(argparse.ArgumentParser):
    """`ArgumentParser` que no termina el proceso ante un uso incorrecto.

    argparse llama a `sys.exit(2)` con un mensaje en inglés; aquí se
    convierte en `_UsageError` para que `main` pueda devolver 2 en español.
    """

    def error(self, message: str) -> NoReturn:
        raise _UsageError(message)


def main(argv: list[str] | None = None, deps: Deps | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except _UsageError:
        print(
            "sarch: argumentos inválidos. "
            "Uso: sarch verify ITEMS [-o SALIDA] [--config DIR]; "
            "sarch render PLAN --cards CARDS [--format text|html] [-o SALIDA] [--open]; "
            "sarch apply PLAN [--project DIR]; "
            "sarch revert RUN_ID [--project DIR]; "
            "sarch audit [--project DIR] [--config DIR]; "
            "usa --help para más información.",
            file=sys.stderr,
        )
        return 2
    except SystemExit as exc:
        # `--help` termina con SystemExit(0) tras imprimir la ayuda.
        return exc.code if isinstance(exc.code, int) else 0

    if args.command == "render":
        return _cmd_render(args)
    if args.command in ("apply", "revert", "audit"):
        return _cmd_applier(args, deps)
    return _cmd_verify(args, deps)


def _build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="sarch")
    subparsers = parser.add_subparsers(dest="command", required=True)

    verify_parser = subparsers.add_parser(
        "verify", help="verifica las herramientas de una lista de items"
    )
    verify_parser.add_argument("items_path", metavar="ITEMS")
    verify_parser.add_argument("-o", "--output", dest="output", default=None)
    verify_parser.add_argument(
        "--config", dest="config", default=str(_DEFAULT_CONFIG_DIR)
    )

    render_parser = subparsers.add_parser(
        "render", help="muestra el plan y las fichas de confianza"
    )
    render_parser.add_argument("plan_path", metavar="PLAN")
    render_parser.add_argument("--cards", dest="cards", required=True)
    render_parser.add_argument(
        "--format", dest="format", choices=["text", "html"], default="text"
    )
    render_parser.add_argument("--open", dest="open", action="store_true")
    render_parser.add_argument("-o", "--output", dest="output", default=None)

    apply_parser = subparsers.add_parser(
        "apply", help="re-verifica el plan, pide aprobación por la terminal y lo ejecuta"
    )
    apply_parser.add_argument("plan_path", metavar="PLAN")
    apply_parser.add_argument("--project", dest="project", default=None)
    # Sin `--config`: el plan no la trae y cambiaría los dueños oficiales y
    # los umbrales sin que el resumen que se aprueba lo diga.

    revert_parser = subparsers.add_parser("revert", help="deshace una corrida de apply")
    revert_parser.add_argument("run_id", metavar="RUN_ID")
    revert_parser.add_argument("--project", dest="project", default=None)

    audit_parser = subparsers.add_parser(
        "audit", help="vuelve a consultar lo instalado y señala lo obsoleto"
    )
    audit_parser.add_argument("--project", dest="project", default=None)
    audit_parser.add_argument("--config", dest="config", default=str(_DEFAULT_CONFIG_DIR))

    return parser


def _cmd_applier(args: argparse.Namespace, deps: Deps | None) -> int:
    """`apply`, `revert` y `audit`: la lógica vive en `apply/runner.py`.

    `apply` usa siempre la configuración por defecto del paquete.
    """
    project = Path(args.project) if args.project is not None else None
    config: Config | None = None
    if args.command != "revert":
        config_dir = Path(args.config) if args.command == "audit" else _DEFAULT_CONFIG_DIR
        try:
            config = load_config(config_dir)
        except ConfigError as exc:
            print(f"configuración inválida: {exc}", file=sys.stderr)
            return 2
    if deps is None:
        deps = default_deps()
    if args.command == "revert":
        return run_revert(args.run_id, deps, project)
    assert config is not None
    if args.command == "audit":
        return run_audit(deps, config, project)
    return run_apply(Path(args.plan_path), deps, config, project)


def _verify_contained(item: PlanItem, deps: Deps, config: Config) -> TrustCard:
    """Verifica un item; cualquier excepción inesperada produce una ficha `Bloqueado`.

    Es la red de seguridad (falla cerrado): captura `Exception`, no
    `BaseException`, para que Ctrl+C siga interrumpiendo. La ficha lleva solo
    el nombre de la clase de la excepción, nunca su mensaje ni el traceback,
    porque pueden contener texto venido de internet.
    """
    try:
        return verify_item(item, deps, config)
    except Exception as exc:
        kind = type(exc).__name__
        print(
            f"aviso: error interno al verificar '{item.candidate.name}': {kind}",
            file=sys.stderr,
        )
        reason = f"error interno al verificar: {kind}"
        return TrustCard(
            candidate=item.candidate.name,
            level=Level.BLOQUEADO,
            reasons=[reason],
            flags=[Flag(code="internal_error", detail=reason, veto=True)],
            pinned_ref=None,
            popularity=0,
        )


def _cmd_verify(args: argparse.Namespace, deps: Deps | None) -> int:
    try:
        raw = Path(args.items_path).read_bytes()
    except OSError as exc:
        print(f"no se pudo leer '{args.items_path}': {exc}", file=sys.stderr)
        return 2

    try:
        data = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        # ValueError cubre json.JSONDecodeError (JSON mal formado) y
        # UnicodeDecodeError (bytes que no son UTF-8 válido, que json.loads
        # intenta decodificar antes de parsear); RecursionError aparece con
        # JSON válido pero anidado más allá del límite de recursión.
        print(f"JSON inválido en '{args.items_path}': {exc}", file=sys.stderr)
        return 2

    if not isinstance(data, dict) or set(data) != {"items"}:
        print(
            "entrada inválida: se esperaba un objeto con una única clave 'items'",
            file=sys.stderr,
        )
        return 2

    items_raw = data["items"]
    if not isinstance(items_raw, list):
        print("entrada inválida: 'items' debe ser una lista", file=sys.stderr)
        return 2

    try:
        items = [parse_item(item, f"items[{i}]") for i, item in enumerate(items_raw)]
    except PlanError as exc:
        print(f"items inválidos: {exc}", file=sys.stderr)
        return 2

    try:
        config = load_config(Path(args.config))
    except ConfigError as exc:
        print(f"configuración inválida: {exc}", file=sys.stderr)
        return 2

    if deps is None:
        if not os.environ.get("GITHUB_TOKEN"):
            print(
                "aviso: no hay GITHUB_TOKEN en el entorno; "
                "la API de GitHub limitará las consultas",
                file=sys.stderr,
            )
        deps = default_deps()

    cards = [_verify_contained(item, deps, config) for item in items]
    cards.sort(key=lambda card: (_LEVEL_ORDER.index(card.level), -card.popularity))

    payload: list[dict[str, Any]] = [card_to_dict(card) for card in cards]
    output_text = json.dumps(payload, ensure_ascii=True, indent=2)

    if args.output:
        try:
            Path(args.output).write_text(output_text + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"no se pudo escribir '{args.output}': {exc}", file=sys.stderr)
            return 2
    else:
        print(output_text)

    return 0


def _cmd_render(args: argparse.Namespace) -> int:
    try:
        plan_raw = Path(args.plan_path).read_bytes()
        cards_raw = Path(args.cards).read_bytes()
    except (OSError, ValueError) as exc:
        print(f"no se pudo leer el archivo: {exc}", file=sys.stderr)
        return 2

    try:
        plan = load_plan(plan_raw)
    except PlanError as exc:
        print(f"plan inválido en '{args.plan_path}': {exc}", file=sys.stderr)
        return 2

    try:
        cards_data = json.loads(cards_raw)
    except (ValueError, RecursionError) as exc:
        print(f"JSON inválido en '{args.cards}': {exc}", file=sys.stderr)
        return 2
    if not isinstance(cards_data, list):
        print("fichas inválidas: se esperaba una lista de fichas", file=sys.stderr)
        return 2
    try:
        cards = [card_from_dict(c, f"fichas[{i}]") for i, c in enumerate(cards_data)]
    except PlanError as exc:
        print(f"fichas inválidas en '{args.cards}': {exc}", file=sys.stderr)
        return 2

    open_view = args.open
    if open_view and args.format != "html":
        print("aviso: --open solo aplica a --format html; se ignora", file=sys.stderr)
        open_view = False

    text = render_html(plan, cards) if args.format == "html" else render_text(plan, cards)

    if open_view:
        return _write_and_open(text, args.output)

    if args.output:
        try:
            Path(args.output).write_text(text, encoding="utf-8")
        except (OSError, ValueError) as exc:
            print(f"no se pudo escribir '{args.output}': {exc}", file=sys.stderr)
            return 2
    else:
        try:
            sys.stdout.write(text)
        except UnicodeEncodeError:
            # Terminal con codificación limitada: degradar en vez de reventar.
            encoding = sys.stdout.encoding or "ascii"
            sys.stdout.write(text.encode(encoding, "backslashreplace").decode(encoding))

    return 0


def _write_and_open(text: str, output: str | None) -> int:
    """Escribe el HTML (en `output` o en un temporal) y lo abre en el navegador."""
    try:
        if output:
            path = Path(output)
            path.write_text(text, encoding="utf-8")
        else:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", suffix=".html", delete=False
            ) as tmp:
                tmp.write(text)
            path = Path(tmp.name)
    except (OSError, ValueError) as exc:
        target = output or "el archivo temporal"
        print(f"no se pudo escribir '{target}': {exc}", file=sys.stderr)
        return 2
    try:
        opened = webbrowser.open(path.resolve().as_uri())
    except (OSError, ValueError, webbrowser.Error) as exc:
        print(f"no se pudo abrir la vista HTML: {exc}", file=sys.stderr)
        return 2
    if not opened:
        print(
            f"no se pudo abrir el navegador; el HTML quedó en '{path}'",
            file=sys.stderr,
        )
        return 2
    if not output:
        print(f"vista HTML en '{path}'", file=sys.stderr)
    return 0
