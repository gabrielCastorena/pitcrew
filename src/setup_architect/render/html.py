"""Vista HTML del plan: un solo archivo, sin JavaScript ni recursos externos.

Claude va en la celda central de una rejilla de 3x3 y las cuatro capas
alrededor. El contenido viene de internet, así que este módulo es una barrera
contra XSS: todo texto dinámico pasa por `safe_text` y luego por
`html.escape(quote=True)`; la clase del nivel sale de un mapa fijo; la fuente
se muestra como texto, nunca como enlace.
"""

from __future__ import annotations

import html

from setup_architect.models import Level, Plan, TrustCard
from setup_architect.render.layers import Layer, RenderItem, group_by_layer, permissions_for
from setup_architect.render.text import safe_text

CSP = "default-src 'none'; style-src 'unsafe-inline'"
EMPTY_LAYER = "(ninguna)"

_LEVEL_CLASS = {
    Level.VERIFICADO: "lv-verificado",
    Level.CONFIABLE: "lv-confiable",
    Level.REVISAR: "lv-revisar",
    Level.BLOQUEADO: "lv-bloqueado",
}
_LEVEL_NOTE = {
    Level.BLOQUEADO: "no se instalará",
    Level.REVISAR: "se confirmará aparte",
}
# Celda de la rejilla por capa (clase fija, nunca derivada de datos).
_LAYER_CELL = {
    Layer.CONTEXTO: "top",
    Layer.CAPACIDADES: "left",
    Layer.CONEXIONES: "right",
    Layer.EXTERNAS: "bottom",
}

_CSS = """
:root{color-scheme:light dark;--bg:#f6f7f9;--fg:#1b1f24;--card:#fff;--line:#c9d0d9;--muted:#4a5562;
--ok-bg:#0b6b3a;--trust-bg:#0a5a8a;--rev-bg:#8a5a00;--blk-bg:#a31621;--badge-fg:#fff}
@media (prefers-color-scheme:dark){:root{--bg:#14171b;--fg:#e8ebef;--card:#1d2127;--line:#3a424d;
--muted:#aab4c0;--ok-bg:#2fa866;--trust-bg:#4aa3de;--rev-bg:#e0a030;--blk-bg:#ef6670;--badge-fg:#10141a}}
body{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif}
h1{font-size:1.1rem;margin:0 0 12px}
.grid{display:grid;grid-template-columns:1fr 1fr 1fr;grid-template-areas:". top ." "left center right" ". bottom .";gap:12px}
.top{grid-area:top}.left{grid-area:left}.right{grid-area:right}.bottom{grid-area:bottom}
.center{grid-area:center;display:flex;align-items:center;justify-content:center;font-size:1.6rem;font-weight:700;border:2px solid var(--line);border-radius:16px;background:var(--card);min-height:80px}
.layer{border:1px solid var(--line);border-radius:12px;background:var(--card);padding:10px;min-width:0}
.layer h2{font-size:1rem;margin:0 0 8px}
.empty{color:var(--muted)}
details{border-top:1px solid var(--line);padding:6px 0;overflow-wrap:anywhere}
summary{cursor:pointer}
.badge{display:inline-block;margin-left:6px;padding:1px 8px;border-radius:10px;font-size:.8rem;color:var(--badge-fg)}
.lv-verificado{background:var(--ok-bg)}.lv-confiable{background:var(--trust-bg)}
.lv-revisar{background:var(--rev-bg)}.lv-bloqueado{background:var(--blk-bg)}
dl{margin:6px 0 0}dt{font-weight:600;margin-top:4px}dd{margin:0 0 0 12px}
ul{margin:0;padding-left:20px}
@media (max-width:700px){.grid{grid-template-columns:1fr;grid-template-areas:"center" "top" "left" "right" "bottom"}}
"""


def _e(text: str) -> str:
    return html.escape(safe_text(text), quote=True)


def _list(items: list[str]) -> str:
    return "<ul>" + "".join(f"<li>{_e(i)}</li>" for i in items) + "</ul>"


def _render_item(ri: RenderItem) -> str:
    cand, card = ri.item.candidate, ri.card
    level = card.level
    note = _LEVEL_NOTE.get(level)
    summary = (
        f"<summary>{_e(cand.name)}"
        f'<span class="badge {_LEVEL_CLASS[level]}">{_e(level.value)}</span>'
        + (f" - {_e(note)}" if note else "")
        + "</summary>"
    )
    perms = permissions_for(ri)
    rows = [
        ("Qué es", f"<dd>{_e(cand.description)}</dd>"),
        ("Momento", f"<dd>{_e(cand.stage)}</dd>"),
        ("Para qué", f"<dd>{_e(cand.why_fit)}</dd>"),
        ("Nivel", f"<dd>{_e(level.value)}{_list(card.reasons)}</dd>"),
        ("Permisos", f"<dd>{_list(perms) if perms else 'ninguno'}</dd>"),
        ("Fuente", f"<dd>{_e(cand.source_url)}</dd>"),
    ]
    body = "".join(f"<dt>{label}</dt>{dd}" for label, dd in rows)
    return f"<details>{summary}<dl>{body}</dl></details>"


def _render_layer(layer: Layer, items: list[RenderItem]) -> str:
    inner = "".join(_render_item(ri) for ri in items) or f'<p class="empty">{EMPTY_LAYER}</p>'
    return f'<section class="layer {_LAYER_CELL[layer]}"><h2>{_e(layer.value)}</h2>{inner}</section>'


def render_html(plan: Plan, cards: list[TrustCard]) -> str:
    groups = group_by_layer(plan, cards)
    title = f"Plan ({_e(plan.environment)}): {len(plan.items)} herramientas"
    layers = "".join(_render_layer(layer, groups[layer]) for layer in Layer)
    return (
        "<!DOCTYPE html>\n"
        '<html lang="es">\n<head>\n'
        '<meta charset="utf-8">\n'
        f'<meta http-equiv="Content-Security-Policy" content="{CSP}">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>Plan de setup</title>\n"
        f"<style>{_CSS}</style>\n"
        "</head>\n<body>\n"
        f"<h1>{title}</h1>\n"
        f'<div class="grid">{layers}<div class="center">Claude</div></div>\n'
        "</body>\n</html>\n"
    )
