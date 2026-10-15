"""Configuración del verificador: umbrales de la rúbrica y listas de confianza.

`Config` reúne los cinco umbrales de `verify/rubric.py` junto con los
dueños de GitHub que cuentan como oficiales y los nombres conocidos que
usa `verify/typosquat.py`. `load_config` los carga desde un directorio
como `config/`: los umbrales vienen de `thresholds.toml` (con
`tomllib`, sin dependencias externas) y las dos listas, de
`official_owners.txt` y `known_names.txt`, con el mismo formato: una
entrada por línea, líneas vacías y comentarios `#` ignorados.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_THRESHOLD_FIELDS = frozenset(
    {
        "max_inactive_days",
        "new_repo_days",
        "new_repo_star_veto",
        "typosquat_max_distance",
        "fork_min_ahead",
    }
)
_CLAUDE_MD_FIELDS = frozenset({"claude_md_max_lines", "claude_md_forbidden"})

DEFAULT_CLAUDE_MD_FORBIDDEN = (
    r"curl\s",
    r"wget\s",
    r"\|\s*(ba)?sh",
    r"\bsudo\b",
    r"rm\s+-rf",
    r"\beval\b",
    r"chmod\s+\+x",
)


class ConfigError(Exception):
    """Error al cargar la configuración: archivo faltante o umbral inválido."""


@dataclass(frozen=True, kw_only=True)
class Config:
    """Umbrales de la rúbrica y listas de confianza.

    `official_owners` y `known_names` arrancan vacíos porque van después
    de campos con default: un dataclass posicional no lo permitiría, así
    que todo el dataclass es `kw_only`.
    """

    max_inactive_days: int = 365
    new_repo_days: int = 30
    new_repo_star_veto: int = 500
    typosquat_max_distance: int = 2
    fork_min_ahead: int = 5
    claude_md_max_lines: int = 200
    claude_md_forbidden: list[str] = field(
        default_factory=lambda: list(DEFAULT_CLAUDE_MD_FORBIDDEN)
    )
    official_owners: frozenset[str] = frozenset()
    known_names: list[tuple[str, str]] = field(default_factory=list)


def load_config(directory: Path) -> Config:
    """Carga `Config` desde `directory`.

    Lee `thresholds.toml` (claves con los mismos nombres que los campos
    de umbral de `Config`; las ausentes usan su default; una clave
    desconocida o de tipo distinto a entero lanza `ConfigError`),
    `official_owners.txt` (una entrada por línea, comparada en
    minúsculas) y `known_names.txt` (una entrada `owner/name` por
    línea). Si falta el directorio o alguno de los tres archivos, o si
    una entrada de `known_names.txt` no tiene la forma `owner/name`,
    lanza `ConfigError` con un mensaje en español.
    """
    if not directory.is_dir():
        raise ConfigError(f"no existe el directorio de configuración: {directory}")

    thresholds_path = directory / "thresholds.toml"
    official_owners_path = directory / "official_owners.txt"
    known_names_path = directory / "known_names.txt"

    for path in (thresholds_path, official_owners_path, known_names_path):
        if not path.is_file():
            raise ConfigError(f"falta el archivo de configuración: {path}")

    thresholds = _load_thresholds(thresholds_path)
    official_owners = frozenset(
        owner.lower() for owner in _read_entries(official_owners_path)
    )
    known_names = _read_known_names(known_names_path)

    return Config(
        official_owners=official_owners,
        known_names=known_names,
        **thresholds,
    )


def _load_thresholds(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"thresholds.toml no es TOML válido: {exc}") from exc

    unknown = sorted(set(raw) - _THRESHOLD_FIELDS - _CLAUDE_MD_FIELDS)
    if unknown:
        raise ConfigError(
            f"umbral desconocido en thresholds.toml: {', '.join(unknown)}"
        )

    if "claude_md_forbidden" in raw:
        _check_forbidden(raw["claude_md_forbidden"])

    for key, value in raw.items():
        if key == "claude_md_forbidden":
            continue
        if not isinstance(value, int) or isinstance(value, bool):
            raise ConfigError(f"el umbral '{key}' debe ser un entero")
        if key == "claude_md_max_lines" and value < 1:
            raise ConfigError("'claude_md_max_lines' debe ser un entero positivo")

    return raw


def _check_forbidden(value: object) -> None:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError("'claude_md_forbidden' debe ser una lista de cadenas")
    for pattern in value:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ConfigError(
                f"expresión regular inválida en 'claude_md_forbidden': "
                f"'{pattern}' ({exc})"
            ) from exc


def _read_entries(path: Path) -> list[str]:
    entries: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        entries.append(line)
    return entries


def _read_known_names(path: Path) -> list[tuple[str, str]]:
    known: list[tuple[str, str]] = []
    for entry in _read_entries(path):
        parts = entry.split("/")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise ConfigError(
                f"entrada inválida en known_names.txt: '{entry}' (se espera owner/name)"
            )
        known.append((parts[0], parts[1]))
    return known
