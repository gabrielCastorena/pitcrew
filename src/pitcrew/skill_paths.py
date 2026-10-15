"""Qué es "estar bajo `.claude/skills/`": una sola definición.

La comparten la rúbrica del verificador (`verify/rubric.py`) y las vistas
(`render/layers.py`). Vive en un módulo neutro, que no importa a ninguno
de los dos, para que `verify/` y `render/` no dependan uno del otro.
"""

from __future__ import annotations


def in_skills_dir(parts: tuple[str, ...]) -> bool:
    """`parts` contiene los componentes consecutivos `.claude`, `skills`."""
    return any(a == ".claude" and b == "skills" for a, b in zip(parts, parts[1:]))
