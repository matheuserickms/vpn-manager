import json

import pytest

from vpn_manager.helper_main import SENTINELA_SENHA
from vpn_manager.profile_client import (
    HELPER,
    ClientError,
    assume,
    create,
    delete,
    read,
    update,
)
from vpn_manager.protocol import VERSAO

PERFIL = {
    "id": "vpn-exemplo",
    "nome": "Rede A",
    "proposito": "Serviços internos",
    "gateway": {"host": "vpn.exemplo.com", "porta": 443},
    "usuario": "usuario",
    "trusted_cert": "",
    "redes": ["10.0.0.0/24"],
    "checks": [],
}


class Recorder:
    """Substitui subprocess.run, guardando como foi chamado."""

    def __init__(self, stdout='{"ok": true}', returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr
        self.calls = []
        self.inputs = []

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        self.inputs.append(kwargs.get("input"))

        class R:
            pass

        r = R()
        r.returncode = self.returncode
        r.stdout = self.stdout
        r.stderr = self.stderr
        return r


class TestSenhaNaoVazaNaLinhaDeComando:
    """`/proc/<pid>/cmdline` é legível por qualquer usuário da máquina. Uma
    senha em argv fica exposta enquanto o processo viver."""

    def test_senha_nao_aparece_em_nenhum_argumento(self):
        run = Recorder()
        create(PERFIL, senha="SEGREDO", run=run)

        for args in run.calls:
            for arg in args:
                assert "SEGREDO" not in arg

    def test_senha_viaja_pelo_stdin(self):
        run = Recorder()
        create(PERFIL, senha="SEGREDO", run=run)

        enviado = json.loads(run.inputs[0])
        assert enviado["perfil"]["senha"] == "SEGREDO"

    def test_nao_usa_arquivo_temporario(self):
        """Arquivo temporário com senha sobrevive a um crash."""
        run = Recorder()
        create(PERFIL, senha="SEGREDO", run=run)
        for args in run.calls:
            assert not any(a.startswith("/tmp") for a in args)


class TestChamada:
    def test_invoca_o_helper_por_pkexec(self):
        run = Recorder()
        create(PERFIL, senha="x", run=run)
        assert run.calls[0][0] == "pkexec"
        assert run.calls[0][1] == HELPER

    def test_manda_a_versao_do_protocolo(self):
        run = Recorder()
        create(PERFIL, senha="x", run=run)
        assert json.loads(run.inputs[0])["versao"] == VERSAO

    def test_read_nao_manda_perfil_nem_senha(self):
        run = Recorder(stdout='{"ok": true, "perfil": {}, "preservar": [], "gerenciado": true}')
        read("vpn-exemplo", run=run)
        enviado = json.loads(run.inputs[0])
        assert enviado == {"versao": VERSAO, "op": "read", "id": "vpn-exemplo"}

    def test_update_manda_a_sentinela_quando_a_senha_nao_muda(self):
        run = Recorder()
        update(PERFIL, senha=None, run=run)
        assert json.loads(run.inputs[0])["perfil"]["senha"] == SENTINELA_SENHA

    def test_delete_manda_so_o_id(self):
        run = Recorder()
        delete("vpn-exemplo", run=run)
        assert json.loads(run.inputs[0]) == {
            "versao": VERSAO,
            "op": "delete",
            "id": "vpn-exemplo",
        }


class TestTraducaoDeErros:
    def test_erro_estruturado_do_helper_vira_excecao_com_campo(self):
        run = Recorder(
            stdout=json.dumps(
                {"ok": False, "erro": "rede_invalida", "detalhe": "não é rede", "campo": "redes[0]"}
            ),
            returncode=1,
        )
        with pytest.raises(ClientError) as e:
            create(PERFIL, senha="x", run=run)
        assert e.value.campo == "redes[0]"
        assert e.value.codigo == "rede_invalida"

    def test_autorizacao_negada_tem_mensagem_propria(self):
        """pkexec sai com 126 quando o usuário cancela ou não autoriza."""
        run = Recorder(stdout="", returncode=126, stderr="Not authorized")
        with pytest.raises(ClientError) as e:
            create(PERFIL, senha="x", run=run)
        assert "autoriza" in str(e.value).lower() or "cancel" in str(e.value).lower()

    def test_saida_ilegivel_nao_estoura_json_decode_cru(self):
        """Se o helper morrer antes de escrever, o cliente precisa dizer algo
        útil em vez de propagar JSONDecodeError."""
        run = Recorder(stdout="Killed", returncode=137)
        with pytest.raises(ClientError):
            create(PERFIL, senha="x", run=run)

    def test_helper_ausente_e_erro_claro(self):
        def run_que_falha(*a, **k):
            raise FileNotFoundError(HELPER)

        with pytest.raises(ClientError, match="instal"):
            create(PERFIL, senha="x", run=run_que_falha)


class TestRespostaDeSucesso:
    def test_read_devolve_o_corpo_do_helper(self):
        corpo = {
            "ok": True,
            "perfil": dict(PERFIL, senha=SENTINELA_SENHA),
            "preservar": ["half-internet-routes = 1"],
            "gerenciado": True,
        }
        run = Recorder(stdout=json.dumps(corpo))
        r = read("vpn-exemplo", run=run)
        assert r["perfil"]["id"] == "vpn-exemplo"
        assert r["preservar"] == ["half-internet-routes = 1"]


class TestAssume:
    def test_manda_o_verbo_assume(self):
        run = Recorder()
        assume(PERFIL, senha="x", run=run)
        assert json.loads(run.inputs[0])["op"] == "assume"

    def test_sem_confirmar_nao_manda_o_campo(self):
        run = Recorder()
        assume(PERFIL, senha="x", run=run)
        assert "confirmar" not in json.loads(run.inputs[0])

    def test_com_confirmar_manda_o_nome_do_script(self):
        run = Recorder()
        assume(PERFIL, senha="x", confirmar="51manual", run=run)
        assert json.loads(run.inputs[0])["confirmar"] == "51manual"

    def test_senha_none_vira_sentinela(self):
        """Assumir normalmente reaproveita a senha do arquivo."""
        run = Recorder()
        assume(PERFIL, senha=None, run=run)
        assert json.loads(run.inputs[0])["perfil"]["senha"] == SENTINELA_SENHA
