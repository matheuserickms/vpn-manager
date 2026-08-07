import json
import socket
import threading
import time

import pytest

from vpn_manager.models import Check
from vpn_manager.probe import Route, RouteReadError, read_routes, missing_networks, run_check


class FakeRun:
    """Substitui subprocess.run devolvendo uma saída fixa de `ip -j route`."""

    def __init__(self, payload, returncode=0):
        self.payload = payload
        self.returncode = returncode

    def __call__(self, *args, **kwargs):
        class R:
            pass
        r = R()
        r.returncode = self.returncode
        r.stdout = json.dumps(self.payload) if isinstance(self.payload, list) else self.payload
        return r


def test_read_routes_parseia_saida_do_ip():
    routes = read_routes(run=FakeRun([
        {"dst": "default", "gateway": "192.168.0.1", "dev": "wlp0s20f3"},
        {"dst": "10.0.0.0/24", "dev": "ppp1"},
        {"dst": "10.0.1.0/24", "dev": "ppp2"},
    ]))
    assert Route(dst="10.0.0.0/24", dev="ppp1") in routes
    assert Route(dst="default", dev="wlp0s20f3") in routes


def test_read_routes_returncode_falha():
    """Item 1 (Critical), Vetor C da rodada de fechamento: `read_routes`
    precisa DISTINGUIR "comando falhou" de "nenhuma rota" — as duas coisas
    não podem devolver o mesmo `()`, porque `status_of` usava esse valor para
    calcular rotas ausentes, e um `ip route` que só falhou virava `parcial`
    de verdade. Falha agora é uma exceção, não um valor de retorno igual ao
    de "vazio de verdade"."""
    with pytest.raises(RouteReadError):
        read_routes(run=FakeRun([], returncode=1))


def test_read_routes_json_invalido():
    """Mesma distinção do teste acima, para JSON ilegível."""

    def broken_run(*args, **kwargs):
        class R:
            returncode = 0
            stdout = "{ invalid json"
        return R()

    with pytest.raises(RouteReadError):
        read_routes(run=broken_run)


def test_read_routes_filtra_entradas_incompletas():
    """read_routes retorna () para entradas sem dst ou dev."""
    routes = read_routes(run=FakeRun([
        {"dst": "10.0.0.0/8"},  # falta dev
        {"dev": "eth0"},  # falta dst
        {"other": "field"},  # não tem dst nem dev
    ]))
    assert routes == ()


def test_rede_presente_na_interface_certa_nao_falta():
    routes = [Route(dst="10.0.0.0/24", dev="ppp1")]
    assert missing_networks(["10.0.0.0/24"], "ppp1", routes) == ()


def test_rede_na_interface_errada_conta_como_ausente():
    """O bug original: a duplicata deixou a rota presente, mas no túnel errado."""
    routes = [Route(dst="10.0.1.0/24", dev="ppp2")]
    assert missing_networks(["10.0.1.0/24"], "ppp1", routes) == ("10.0.1.0/24",)


def test_sem_interface_todas_as_redes_faltam():
    routes = [Route(dst="10.0.1.0/24", dev="ppp2")]
    assert missing_networks(["10.0.1.0/24"], None, routes) == ("10.0.1.0/24",)


def test_lista_todas_as_redes_ausentes():
    routes = [Route(dst="10.0.2.0/24", dev="ppp2")]
    faltando = missing_networks(
        ["10.0.2.0/24", "10.0.3.0/24", "10.0.1.0/24"], "ppp2", routes
    )
    assert faltando == ("10.0.3.0/24", "10.0.1.0/24")


def test_run_check_porta_aberta():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    threading.Thread(target=lambda: srv.accept(), daemon=True).start()

    r = run_check(Check(host="127.0.0.1", port=port, label="teste"), timeout=2.0)
    assert r.ok is True
    assert r.error is None
    srv.close()


def test_run_check_porta_fechada():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.close()

    r = run_check(Check(host="127.0.0.1", port=port, label="teste"), timeout=2.0)
    assert r.ok is False
    assert r.error


def test_run_check_conexao_recusada():
    """Verifica que porta fechada devolve mensagem traduzida."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.close()

    r = run_check(Check(host="127.0.0.1", port=port, label="teste"), timeout=2.0)
    assert r.ok is False
    assert r.error == "conexão recusada"


def test_run_check_host_inalcancavel_respeita_timeout():
    # 203.0.113.0/24 é TEST-NET-3 (RFC 5737): nunca roteável.
    timeout = 1.0
    start = time.monotonic()
    r = run_check(Check(host="203.0.113.1", port=9, label="teste"), timeout=timeout)
    elapsed = time.monotonic() - start
    assert r.ok is False
    assert r.error == "tempo esgotado"
    assert elapsed >= 0.9 * timeout  # pelo menos 90% do timeout
