import errno
import json
import socket
import subprocess
from dataclasses import dataclass
from typing import Iterable

from .models import Check


ERROS_TRADUZIDOS = {
    errno.ECONNREFUSED: "conexão recusada",
    errno.EHOSTUNREACH: "host inalcançável",
    errno.ENETUNREACH: "rede inalcançável",
}


@dataclass(frozen=True)
class Route:
    dst: str
    dev: str


@dataclass(frozen=True)
class CheckResult:
    check: Check
    ok: bool
    error: str | None


class RouteReadError(Exception):
    """`ip route show` falhou ou devolveu algo ilegível.

    Item 1 (Critical) da rodada de fechamento pré-merge, Vetor C: antes desta
    exceção, `read_routes` devolvia `()` tanto para "comando falhou/estourou
    timeout" quanto para "não há rota nenhuma" — os dois casos eram o mesmo
    valor de retorno. `status_of` (vpnctl.py) usava esse `()` para calcular
    `missing_networks`, e como "nenhuma rota" é indistinguível de "todas as
    rotas ausentes" quando a interface é conhecida, um `ip route` lento ou
    quebrado virava, de verdade, o estado `parcial` — com o botão Reconectar
    (`systemctl restart`) sobre um túnel são. Levantar aqui obriga quem chama
    a decidir explicitamente o que fazer com uma leitura degradada, em vez de
    herdar por acidente o mesmo formato de "nada encontrado"."""


def read_routes(run=subprocess.run) -> tuple[Route, ...]:
    proc = run(
        ["ip", "-j", "route", "show"],
        capture_output=True, text=True, timeout=5,
    )
    if proc.returncode != 0:
        raise RouteReadError(f"'ip -j route show' devolveu código {proc.returncode}")
    try:
        entries = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as e:
        raise RouteReadError(f"saída de 'ip -j route show' não é JSON válido: {e}") from e
    return tuple(
        Route(dst=e["dst"], dev=e["dev"])
        for e in entries
        if "dst" in e and "dev" in e
    )


def missing_networks(
    networks: Iterable[str], iface: str | None, routes: Iterable[Route]
) -> tuple[str, ...]:
    """Uma rede só conta como presente se a rota existir E apontar para `iface`.

    Rota presente via outra interface é tratada como ausente: foi assim que o
    `ip route replace` de uma instância duplicada mascarou a falta de alcance.
    """
    if iface is None:
        return tuple(networks)
    present = {r.dst for r in routes if r.dev == iface}
    return tuple(n for n in networks if n not in present)


def run_check(check: Check, timeout: float = 3.0) -> CheckResult:
    try:
        with socket.create_connection((check.host, check.port), timeout=timeout):
            return CheckResult(check=check, ok=True, error=None)
    except TimeoutError:
        return CheckResult(check=check, ok=False, error="tempo esgotado")
    except OSError as e:
        error_msg = ERROS_TRADUZIDOS.get(e.errno) or e.strerror or str(e)
        return CheckResult(check=check, ok=False, error=error_msg)
