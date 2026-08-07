from dataclasses import dataclass
from enum import StrEnum


class State(StrEnum):
    INACTIVE = "inativo"
    ACTIVE = "ativo"
    PARTIAL = "parcial"
    EXTERNAL = "externo"
    FAILED = "falhou"
    UNCONFIGURED = "nao_configurado"
    CONNECTING = "conectando"


@dataclass(frozen=True)
class Check:
    host: str
    port: int
    label: str


@dataclass(frozen=True)
class Profile:
    id: str
    name: str
    purpose: str
    networks: tuple[str, ...]
    checks: tuple[Check, ...]
