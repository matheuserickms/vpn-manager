import tomllib
from pathlib import Path

from .models import Check, Profile

DEFAULT_PATH = Path("/etc/vpn-manager/profiles.toml")


class CatalogError(Exception):
    pass


def _require(table: dict, key: str, where: str):
    if key not in table:
        raise CatalogError(f"{where}: campo obrigatório '{key}' ausente")
    return table[key]


def load_catalog(path: Path = DEFAULT_PATH) -> tuple[Profile, ...]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise CatalogError(f"catálogo não encontrado: {path}") from None
    except OSError as e:
        raise CatalogError(f"não foi possível ler {path}: {e}") from None

    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise CatalogError(f"TOML inválido em {path}: {e}") from None

    profiles = []
    for i, entry in enumerate(data.get("profile", [])):
        where = f"perfil #{i + 1}"
        pid = _require(entry, "id", where)
        checks = []
        for j, c in enumerate(entry.get("checks", [])):
            cw = f"{where}, check #{j + 1}"
            checks.append(
                Check(
                    host=_require(c, "host", cw),
                    port=int(_require(c, "porta", cw)),
                    label=_require(c, "rotulo", cw),
                )
            )
        profiles.append(
            Profile(
                id=pid,
                name=_require(entry, "nome", where),
                purpose=_require(entry, "proposito", where),
                networks=tuple(entry.get("redes", [])),
                checks=tuple(checks),
            )
        )

    if not profiles:
        raise CatalogError(f"nenhum perfil definido em {path}")
    return tuple(profiles)
