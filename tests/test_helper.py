import io
import json

import pytest

from vpn_manager.catalog import load_catalog
from vpn_manager.helper_main import SENTINELA_SENHA, helper_main
from vpn_manager.profile_store import Paths
from vpn_manager.protocol import VERSAO

PERFIL_REQ = {
    "id": "vpn-exemplo",
    "nome": "Rede A",
    "proposito": "Serviços internos",
    "gateway": {"host": "vpn.exemplo.com", "porta": 443},
    "usuario": "usuario",
    "senha": "s3nha",
    "trusted_cert": "a" * 64,
    "redes": ["10.0.0.0/24"],
    "checks": [{"host": "10.0.0.10", "porta": 443, "rotulo": "Serviço web"}],
}


def paths_de_teste(tmp_path):
    return Paths(
        conf_dir=tmp_path / "etc/openfortivpn",
        ip_up_dir=tmp_path / "etc/ppp/ip-up.d",
        catalog=tmp_path / "etc/vpn-manager/profiles.toml",
        undo_dir=tmp_path / "var/lib/vpn-manager/undo",
    )


def chamar(payload, paths, unidade_ativa=False):
    """Chama o helper direto, sem subprocess e sem root."""
    saida = io.StringIO()
    codigo = helper_main(
        io.StringIO(json.dumps(payload)),
        saida,
        paths=paths,
        unit_ativa=lambda _pid: unidade_ativa,
    )
    return codigo, json.loads(saida.getvalue())


class TestCreate:
    def test_cria_o_perfil(self, tmp_path):
        p = paths_de_teste(tmp_path)
        codigo, resp = chamar(
            {"versao": VERSAO, "op": "create", "perfil": PERFIL_REQ}, p
        )
        assert codigo == 0
        assert resp["ok"] is True
        assert [x.id for x in load_catalog(p.catalog)] == ["vpn-exemplo"]

    def test_recusa_id_ja_existente(self, tmp_path):
        p = paths_de_teste(tmp_path)
        chamar({"versao": VERSAO, "op": "create", "perfil": PERFIL_REQ}, p)
        codigo, resp = chamar(
            {"versao": VERSAO, "op": "create", "perfil": PERFIL_REQ}, p
        )
        assert codigo == 1
        assert resp["ok"] is False
        assert "existe" in resp["detalhe"].lower()

    def test_recusa_colisao_com_conf_manual_nao_catalogado(self, tmp_path):
        """Um .conf que existe mas não está no catálogo é perfil manual.
        Sobrescrever apagaria a configuração de alguém — é erro, não
        oportunidade de sobrescrita."""
        p = paths_de_teste(tmp_path)
        p.conf_dir.mkdir(parents=True)
        (p.conf_dir / "vpn-exemplo.conf").write_text("host = manual\n")

        codigo, resp = chamar(
            {"versao": VERSAO, "op": "create", "perfil": PERFIL_REQ}, p
        )
        assert codigo == 1
        assert (p.conf_dir / "vpn-exemplo.conf").read_text() == "host = manual\n"


class TestRead:
    def test_devolve_campos_gerenciados(self, tmp_path):
        p = paths_de_teste(tmp_path)
        chamar({"versao": VERSAO, "op": "create", "perfil": PERFIL_REQ}, p)

        _, resp = chamar({"versao": VERSAO, "op": "read", "id": "vpn-exemplo"}, p)
        assert resp["perfil"]["gateway"]["host"] == "vpn.exemplo.com"
        assert resp["perfil"]["usuario"] == "usuario"
        assert resp["gerenciado"] is True

    def test_nunca_devolve_a_senha(self, tmp_path):
        """A senha não volta ao processo sem privilégio. Este é o teste que
        justifica o helper existir com verbo read."""
        p = paths_de_teste(tmp_path)
        chamar({"versao": VERSAO, "op": "create", "perfil": PERFIL_REQ}, p)

        saida = io.StringIO()
        helper_main(
            io.StringIO(json.dumps({"versao": VERSAO, "op": "read", "id": "vpn-exemplo"})),
            saida,
            paths=p,
            unit_ativa=lambda _p: False,
        )
        bruto = saida.getvalue()
        assert "s3nha" not in bruto
        assert json.loads(bruto)["perfil"]["senha"] == SENTINELA_SENHA

    def test_devolve_linhas_desconhecidas_verbatim(self, tmp_path):
        p = paths_de_teste(tmp_path)
        p.conf_dir.mkdir(parents=True)
        (p.conf_dir / "vpn-exemplo.conf").write_text(
            "host = vpn.exemplo.com\nport = 443\nhalf-internet-routes = 1\n"
        )
        _, resp = chamar({"versao": VERSAO, "op": "read", "id": "vpn-exemplo"}, p)
        assert "half-internet-routes = 1" in resp["preservar"]
        assert resp["gerenciado"] is False

    def test_id_inexistente_e_erro(self, tmp_path):
        codigo, resp = chamar(
            {"versao": VERSAO, "op": "read", "id": "vpn-exemplo"}, paths_de_teste(tmp_path)
        )
        assert codigo == 1
        assert resp["ok"] is False


class TestUpdate:
    def test_preserva_a_senha_quando_vem_a_sentinela(self, tmp_path):
        p = paths_de_teste(tmp_path)
        chamar({"versao": VERSAO, "op": "create", "perfil": PERFIL_REQ}, p)

        editado = dict(PERFIL_REQ, nome="Rede A editada", senha=SENTINELA_SENHA)
        codigo, _ = chamar({"versao": VERSAO, "op": "update", "perfil": editado}, p)

        assert codigo == 0
        assert "password = s3nha" in (p.conf_dir / "vpn-exemplo.conf").read_text()
        assert load_catalog(p.catalog)[0].name == "Rede A editada"

    def test_troca_a_senha_quando_vem_uma_nova(self, tmp_path):
        p = paths_de_teste(tmp_path)
        chamar({"versao": VERSAO, "op": "create", "perfil": PERFIL_REQ}, p)

        chamar(
            {"versao": VERSAO, "op": "update", "perfil": dict(PERFIL_REQ, senha="outra")}, p
        )
        assert "password = outra" in (p.conf_dir / "vpn-exemplo.conf").read_text()

    def test_recusa_perfil_inexistente(self, tmp_path):
        codigo, resp = chamar(
            {"versao": VERSAO, "op": "update", "perfil": PERFIL_REQ}, paths_de_teste(tmp_path)
        )
        assert codigo == 1


class TestDelete:
    def test_remove_o_perfil(self, tmp_path):
        p = paths_de_teste(tmp_path)
        chamar({"versao": VERSAO, "op": "create", "perfil": PERFIL_REQ}, p)

        codigo, _ = chamar({"versao": VERSAO, "op": "delete", "id": "vpn-exemplo"}, p)

        assert codigo == 0
        assert not (p.conf_dir / "vpn-exemplo.conf").exists()

    def test_recusa_apagar_perfil_conectado(self, tmp_path):
        """O helper checa o estado ele mesmo — não confia no que o chamador
        diz ter visto na tela."""
        p = paths_de_teste(tmp_path)
        chamar({"versao": VERSAO, "op": "create", "perfil": PERFIL_REQ}, p)

        codigo, resp = chamar(
            {"versao": VERSAO, "op": "delete", "id": "vpn-exemplo"}, p, unidade_ativa=True
        )

        assert codigo == 1
        assert "ativa" in resp["detalhe"].lower() or "conect" in resp["detalhe"].lower()
        assert (p.conf_dir / "vpn-exemplo.conf").exists()


class TestFronteiraDeSeguranca:
    """O helper roda como root. Tudo que chega pelo stdin é hostil até prova
    em contrário — inclusive o que já passou pelo parse do protocolo."""

    @pytest.mark.parametrize(
        "id_malicioso",
        ["../../../etc/cron.d/x", "vpn/../../etc/passwd", "vpn.exemplo", "VPN", ""],
    )
    def test_revalida_o_id_mesmo_vindo_do_protocolo(self, tmp_path, id_malicioso):
        p = paths_de_teste(tmp_path)
        mau = dict(PERFIL_REQ, id=id_malicioso)
        codigo, resp = chamar({"versao": VERSAO, "op": "create", "perfil": mau}, p)

        assert codigo == 1
        assert resp["ok"] is False
        assert not (tmp_path / "etc/cron.d").exists()

    def test_rejeita_rede_com_injecao_de_shell(self, tmp_path):
        p = paths_de_teste(tmp_path)
        mau = dict(PERFIL_REQ, redes=["10.0.0.0/24; rm -rf /"])
        codigo, resp = chamar({"versao": VERSAO, "op": "create", "perfil": mau}, p)

        assert codigo == 1
        assert not p.catalog.exists()

    def test_rejeita_request_malformado_sem_tocar_em_disco(self, tmp_path):
        p = paths_de_teste(tmp_path)
        saida = io.StringIO()
        codigo = helper_main(
            io.StringIO("{ nao e json"), saida, paths=p, unit_ativa=lambda _p: False
        )
        assert codigo == 1
        assert json.loads(saida.getvalue())["ok"] is False
        assert not p.conf_dir.exists()

    def test_erro_interno_vira_resposta_estruturada_nao_traceback(self, tmp_path):
        """Um traceback no stdout quebraria o parser do cliente e poderia
        vazar caminho ou conteúdo. Toda saída é JSON."""
        p = paths_de_teste(tmp_path)
        mau = dict(PERFIL_REQ, gateway={"host": "vpn.exemplo.com", "porta": 99999})
        _, resp = chamar({"versao": VERSAO, "op": "create", "perfil": mau}, p)
        assert resp["ok"] is False
        assert "erro" in resp


CONF_MANUAL = """\
host = vpn.exemplo.com
port = 443
username = usuario
password = senha-antiga
set-routes = 0
pppd-ipparam = vpn-exemplo
half-internet-routes = 1
persistent = 10
"""


class TestAssume:
    """Item 5.1: adotar um .conf escrito à mão, sem destruir o que a
    interface não entende."""

    def _com_conf_manual(self, tmp_path, script=True):
        p = paths_de_teste(tmp_path)
        p.conf_dir.mkdir(parents=True)
        (p.conf_dir / "vpn-exemplo.conf").write_text(CONF_MANUAL)
        if script:
            p.ip_up_dir.mkdir(parents=True)
            antigo = p.ip_up_dir / "51manual"
            # Script realista: o guard por ipparam é como ele sabe a qual
            # túnel pertence — é por ele que a detecção o encontra.
            antigo.write_text(
                '#!/bin/sh\n[ "$6" = "vpn-exemplo" ] || exit 0\n'
                "ip route add 10.0.0.0/24 dev $1\n"
            )
            antigo.chmod(0o755)
        return p

    def test_assume_gera_os_artefatos_gerenciados(self, tmp_path):
        p = self._com_conf_manual(tmp_path)
        codigo, resp = chamar(
            {"versao": VERSAO, "op": "assume", "perfil": PERFIL_REQ, "confirmar": "51manual"}, p
        )
        assert codigo == 0, resp
        assert (p.ip_up_dir / "50vpnmgr-vpn-exemplo").exists()
        assert [x.id for x in load_catalog(p.catalog)] == ["vpn-exemplo"]

    def test_preserva_a_senha_do_conf_manual(self, tmp_path):
        """Assumir não pode exigir que o usuário lembre a senha que já está
        no arquivo."""
        p = self._com_conf_manual(tmp_path)
        req = dict(PERFIL_REQ, senha=SENTINELA_SENHA)
        chamar({"versao": VERSAO, "op": "assume", "perfil": req, "confirmar": "51manual"}, p)
        assert "password = senha-antiga" in (p.conf_dir / "vpn-exemplo.conf").read_text()

    def test_preserva_diretivas_que_a_interface_nao_conhece(self, tmp_path):
        p = self._com_conf_manual(tmp_path)
        chamar(
            {"versao": VERSAO, "op": "assume", "perfil": PERFIL_REQ, "confirmar": "51manual"}, p
        )
        texto = (p.conf_dir / "vpn-exemplo.conf").read_text()
        assert "half-internet-routes = 1" in texto
        assert "persistent = 10" in texto

    def test_move_o_script_antigo_para_o_undo(self, tmp_path):
        """Decisão D3: dois scripts injetando rota para o mesmo túnel é
        exatamente o tipo de duplicata que o projeto existe para evitar."""
        p = self._com_conf_manual(tmp_path)
        chamar(
            {"versao": VERSAO, "op": "assume", "perfil": PERFIL_REQ, "confirmar": "51manual"}, p
        )
        assert not (p.ip_up_dir / "51manual").exists()
        assert list(p.undo_dir.rglob("51manual"))

    def test_sem_confirmacao_nao_move_o_script_antigo(self, tmp_path):
        """Remover algo escrito à mão exige confirmação nominal."""
        p = self._com_conf_manual(tmp_path)
        codigo, resp = chamar(
            {"versao": VERSAO, "op": "assume", "perfil": PERFIL_REQ}, p
        )
        assert codigo == 1
        assert (p.ip_up_dir / "51manual").exists()

    def test_recusa_perfil_conectado(self, tmp_path):
        """O ipparam antigo continua na memória do processo vivo; somado a
        `persistent`, o redial voltaria sem as rotas novas."""
        p = self._com_conf_manual(tmp_path)
        codigo, resp = chamar(
            {"versao": VERSAO, "op": "assume", "perfil": PERFIL_REQ, "confirmar": "51manual"},
            p,
            unidade_ativa=True,
        )
        assert codigo == 1
        assert "desconect" in resp["detalhe"].lower() or "ativa" in resp["detalhe"].lower()

    def test_recusa_perfil_que_nao_existe(self, tmp_path):
        codigo, _ = chamar(
            {"versao": VERSAO, "op": "assume", "perfil": PERFIL_REQ, "confirmar": "x"},
            paths_de_teste(tmp_path),
        )
        assert codigo == 1

    def test_conf_ja_gerenciado_nao_precisa_ser_assumido(self, tmp_path):
        p = paths_de_teste(tmp_path)
        chamar({"versao": VERSAO, "op": "create", "perfil": PERFIL_REQ}, p)
        codigo, resp = chamar(
            {"versao": VERSAO, "op": "assume", "perfil": PERFIL_REQ, "confirmar": "x"}, p
        )
        assert codigo == 1
        assert "gerenciado" in resp["detalhe"].lower()


    def test_script_que_nao_menciona_o_perfil_nao_e_detectado(self, tmp_path):
        """Limite conhecido: a detecção é por menção ao ipparam. Um script
        que instala rota sem citar o perfil passa despercebido e continua
        rodando ao lado do gerado — o `assume` não tem como adivinhar."""
        p = self._com_conf_manual(tmp_path, script=False)
        p.ip_up_dir.mkdir(parents=True, exist_ok=True)
        anonimo = p.ip_up_dir / "51anonimo"
        anonimo.write_text("#!/bin/sh\nip route add 10.0.0.0/24 dev $1\n")

        codigo, _ = chamar(
            {"versao": VERSAO, "op": "assume", "perfil": PERFIL_REQ}, p
        )

        assert codigo == 0
        assert anonimo.exists()
