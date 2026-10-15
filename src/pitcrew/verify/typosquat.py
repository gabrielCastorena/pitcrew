"""Detección de typosquatting: nombres casi idénticos a repos conocidos.

`detect_typosquat` compara el nombre de un candidato contra una lista de
repos conocidos y populares del ecosistema (`config/known_names.txt`,
cargado por `config.py`). Un nombre casi idéntico pero de
otro dueño es la señal clásica de typosquatting: alguien publica
`servres` esperando que lo confundan con `servers`.
"""

from __future__ import annotations

from pitcrew.models import Flag


def levenshtein(a: str, b: str) -> int:
    """Distancia de edición (inserción, borrado, sustitución) entre `a` y `b`."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)

    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, start=1):
        current = [i] + [0] * len(b)
        for j, char_b in enumerate(b, start=1):
            cost = 0 if char_a == char_b else 1
            current[j] = min(
                previous[j] + 1,  # borrar de a
                current[j - 1] + 1,  # insertar en a
                previous[j - 1] + cost,  # sustituir
            )
        previous = current
    return previous[-1]


def detect_typosquat(
    owner: str,
    name: str,
    known: list[tuple[str, str]],
    max_distance: int,
) -> Flag | None:
    """Compara `owner/name` contra repos conocidos; veta si se le parece demasiado.

    Para cada `(k_owner, k_name)` conocido, si el dueño coincide (en
    minúsculas) se ignora esa entrada: es el mismo repo o el mismo autor,
    legítimo. En otro caso, si la distancia de Levenshtein entre `name` y
    `k_name` (ambos en minúsculas) es menor o igual a `max_distance`
    -- incluido 0, el nombre idéntico de otro dueño -- se devuelve una
    bandera de veto `typosquat` que nombra al repo conocido al que se
    parece. Si ningún conocido se parece lo suficiente, devuelve `None`.
    """
    lower_owner = owner.lower()
    lower_name = name.lower()
    for k_owner, k_name in known:
        if lower_owner == k_owner.lower():
            continue
        distance = levenshtein(lower_name, k_name.lower())
        if distance <= max_distance:
            return Flag(
                code="typosquat",
                detail=(
                    f"'{name}' se parece a '{k_name}' del repo conocido "
                    f"{k_owner}/{k_name} (distancia {distance})"
                ),
                veto=True,
            )
    return None
