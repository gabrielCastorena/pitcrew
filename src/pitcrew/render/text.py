"""Vista de texto del plan para la terminal.

Todo texto que viene de internet (nombres, descripciones, razones, URLs)
pasa por `safe_text` antes de salir: nada puede inyectar secuencias de
escape ni reventar la escritura con surrogates sueltos.
"""

from __future__ import annotations

import unicodedata

from pitcrew.models import Level, Plan, TrustCard
from pitcrew.render.layers import Layer, RenderItem, group_by_layer, permissions_for

_ALLOWED_CONTROL = {"\t"}
_NAMED_ESCAPES = {"\n": "\\n", "\r": "\\r"}
# Control, formato (p. ej. bidi), surrogates y separadores de línea/párrafo.
_ESCAPED_CATEGORIES = {"Cc", "Cf", "Cs", "Zl", "Zp"}


def safe_text(text: str) -> str:
    """Escapa de forma visible caracteres de control y surrogates sueltos.

    `\\n` y `\\r` también se escapan, para que un valor externo nunca añada una
    línea a la salida; solo se conserva `\\t`. El resultado siempre es
    codificable en UTF-8.
    """
    out: list[str] = []
    for ch in text:
        if ch in _NAMED_ESCAPES:
            out.append(_NAMED_ESCAPES[ch])
        elif ch not in _ALLOWED_CONTROL and unicodedata.category(ch) in _ESCAPED_CATEGORIES:
            code = ord(ch)
            if code < 0x100:
                out.append(f"\\x{code:02x}")
            elif code <= 0xFFFF:
                out.append(f"\\u{code:04x}")
            else:
                out.append(f"\\U{code:08x}")
        else:
            out.append(ch)
    return "".join(out)


_LEVEL_NOTE = {
    Level.BLOQUEADO: "no se instalará",
    Level.REVISAR: "se confirmará aparte",
}


def _render_item(ri: RenderItem) -> list[str]:
    cand, card = ri.item.candidate, ri.card
    level = card.level.value
    note = _LEVEL_NOTE.get(card.level)
    header = f"* {safe_text(cand.name)}  [{level}]" + (f" - {note}" if note else "")
    lines = [
        header,
        f"    Qué es: {safe_text(cand.description)}",
        f"    Momento: {safe_text(cand.stage)}",
        f"    Para qué: {safe_text(cand.why_fit)}",
        f"    Nivel: {level}",
    ]
    lines += [f"      - {safe_text(reason)}" for reason in card.reasons]
    perms = permissions_for(ri)
    if perms:
        lines.append("    Permisos:")
        lines += [f"      - {safe_text(perm)}" for perm in perms]
    else:
        lines.append("    Permisos: ninguno")
    lines.append(f"    Fuente: {safe_text(cand.source_url)}")
    return lines


def render_text(plan: Plan, cards: list[TrustCard]) -> str:
    groups = group_by_layer(plan, cards)
    lines = [f"Plan ({safe_text(plan.environment)}): {len(plan.items)} herramientas", ""]
    for layer in Layer:
        lines.append(f"== {layer.value} ==")
        items = groups[layer]
        if not items:
            lines.append("  (ninguna)")
        for ri in items:
            lines += _render_item(ri)
            lines.append("")
        if not items:
            lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"
