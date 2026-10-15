"""Nombres que ninguna skill ni `write_file` puede traer.

Única definición de "componente prohibido", compartida por el escáner del
verificador (`verify/skill_scan.py`) y por el aplicador (`apply/paths.py`,
`apply/ops.py`): así lo que el verificador aprueba y lo que el aplicador
acepta instalar no pueden discrepar.
"""

from __future__ import annotations

from collections.abc import Iterable

_FORBIDDEN_NAMES = frozenset({"settings.json", "settings.local.json", ".mcp.json"})
_FORBIDDEN_SEGMENT = "hooks"


def forbidden_component(parts: Iterable[str]) -> str | None:
    """El primer componente de `parts` con un nombre prohibido, o `None`.

    Prohibidos, sin distinguir mayúsculas y en cualquier posición de la
    ruta: `settings.json`, `settings.local.json`, `.mcp.json` y el segmento
    `hooks`. Todos configuran o ejecutan comandos de shell en Claude Code.
    """
    for part in parts:
        lowered = part.lower()
        if lowered in _FORBIDDEN_NAMES or lowered == _FORBIDDEN_SEGMENT:
            return part
    return None
