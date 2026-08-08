import json

import pytest

from vpn_manager.protocol import (
    VERSAO,
    LIMITE_BYTES,
    MAX_ITENS,
    ProtocolError,
    parse_request,
    perfil_interno,
    resposta_erro,
    resposta_ok,
)

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


def req(**over):
    base = {"versao": VERSAO, "op": "create", "perfil": dict(PERFIL_REQ)}
    base.update(over)
    return json.dumps(base)


class TestParseRequest:
    def test_aceita_request_bem_formado(self):
        r = parse_request(req())
        assert r["op"] == "create"
        assert r["perfil"]["id"] == "vpn-exemplo"

    @pytest.mark.parametrize("op", ["create", "read", "update", "delete", "assume"])
    def test_aceita_os_cinco_verbos(self, op):
        assert parse_request(req(op=op))["op"] == op

    def test_rejeita_verbo_desconhecido(self):
        with pytest.raises(ProtocolError):
            parse_request(req(op="exec"))

    def test_rejeita_versao_diferente(self):
        """Fail-closed: uma versão futura pode dar outro significado aos
        campos. Recusar é melhor que interpretar com as regras erradas."""
        with pytest.raises(ProtocolError):
            parse_request(req(versao=VERSAO + 1))

    def test_rejeita_versao_ausente(self):
        with pytest.raises(ProtocolError):
            parse_request(json.dumps({"op": "read", "perfil": {"id": "vpn-exemplo"}}))

    def test_rejeita_chave_desconhecida_no_topo(self):
        with pytest.raises(ProtocolError):
            parse_request(req(caminho="/etc/shadow"))

    def test_rejeita_chave_desconhecida_no_perfil(self):
        """O protocolo não tem parâmetro de caminho. Um campo extra que uma
        versão futura interprete não pode passar despercebido agora."""
        mau = dict(PERFIL_REQ, destino="/etc/cron.d/x")
        with pytest.raises(ProtocolError):
            parse_request(req(perfil=mau))

    def test_rejeita_json_invalido(self):
        with pytest.raises(ProtocolError):
            parse_request("{ nao e json")

    def test_rejeita_request_que_nao_e_objeto(self):
        with pytest.raises(ProtocolError):
            parse_request("[1, 2, 3]")

    def test_rejeita_acima_do_limite_de_bytes(self):
        with pytest.raises(ProtocolError):
            parse_request("x" * (LIMITE_BYTES + 1))

    def test_rejeita_redes_demais(self):
        mau = dict(PERFIL_REQ, redes=[f"10.0.{i}.0/24" for i in range(MAX_ITENS + 1)])
        with pytest.raises(ProtocolError):
            parse_request(req(perfil=mau))

    def test_rejeita_checks_demais(self):
        c = {"host": "10.0.0.10", "porta": 443, "rotulo": "x"}
        mau = dict(PERFIL_REQ, checks=[c] * (MAX_ITENS + 1))
        with pytest.raises(ProtocolError, match="check"):
            parse_request(req(perfil=mau))

    def test_rejeita_perfil_que_nao_e_objeto(self):
        with pytest.raises(ProtocolError):
            parse_request(req(perfil=[1, 2, 3]))

    def test_read_e_delete_dispensam_perfil_completo(self):
        r = parse_request(json.dumps({"versao": VERSAO, "op": "read", "id": "vpn-exemplo"}))
        assert r["id"] == "vpn-exemplo"


class TestPerfilInterno:
    """O protocolo usa gateway aninhado; o profile_store usa campos planos."""

    def test_converte_gateway_aninhado_em_campos_planos(self):
        interno = perfil_interno(PERFIL_REQ)
        assert interno["gateway_host"] == "vpn.exemplo.com"
        assert interno["gateway_porta"] == 443
        assert interno["username"] == "usuario"

    def test_nao_carrega_a_senha_para_dentro_do_perfil(self):
        """A senha é argumento separado de apply_profile; deixá-la no dict
        arriscaria vazar para o catálogo, que é 644."""
        assert "senha" not in perfil_interno(PERFIL_REQ)


class TestRespostas:
    def test_resposta_ok_e_json_de_uma_linha(self):
        texto = resposta_ok({"id": "vpn-exemplo"})
        assert "\n" not in texto.strip()
        assert json.loads(texto) == {"ok": True, "id": "vpn-exemplo"}

    def test_resposta_erro_e_estruturada_para_marcar_o_campo(self):
        """O formulário precisa saber QUAL campo recusar, não só mostrar
        um toast genérico."""
        d = json.loads(resposta_erro("rede_invalida", "não é rede", campo="redes[2]"))
        assert d == {
            "ok": False,
            "erro": "rede_invalida",
            "detalhe": "não é rede",
            "campo": "redes[2]",
        }

    def test_resposta_erro_sem_campo_omite_a_chave(self):
        assert "campo" not in json.loads(resposta_erro("io", "disco cheio"))
