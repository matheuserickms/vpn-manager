import pytest

from vpn_manager.editor_form import (
    Form,
    erros_do_form,
    form_de_leitura,
    form_vazio,
    parse_checks,
    parse_redes,
    perfil_do_form,
    senha_para_envio,
)
from vpn_manager.helper_main import SENTINELA_SENHA

FORM_OK = Form(
    id="vpn-exemplo",
    nome="Rede A",
    proposito="Serviços internos",
    gateway_host="vpn.exemplo.com",
    gateway_porta="443",
    usuario="usuario",
    senha="s3nha",
    trusted_cert="",
    redes_texto="10.0.0.0/24\n10.0.1.0/24",
    checks_texto="10.0.0.10:443 Serviço web",
)


class TestParseRedes:
    def test_uma_por_linha(self):
        assert parse_redes("10.0.0.0/24\n10.0.1.0/24") == ["10.0.0.0/24", "10.0.1.0/24"]

    def test_ignora_linhas_vazias_e_espacos(self):
        assert parse_redes("  10.0.0.0/24  \n\n\n  ") == ["10.0.0.0/24"]

    def test_texto_vazio_vira_lista_vazia(self):
        assert parse_redes("") == []
        assert parse_redes("   \n  ") == []


class TestParseChecks:
    def test_host_porta_e_rotulo(self):
        assert parse_checks("10.0.0.10:443 Serviço web") == [
            {"host": "10.0.0.10", "porta": 443, "rotulo": "Serviço web"}
        ]

    def test_varias_linhas(self):
        texto = "10.0.0.10:443 Web\n10.0.0.11:22 SSH"
        assert len(parse_checks(texto)) == 2

    def test_rotulo_opcional_usa_host_e_porta(self):
        assert parse_checks("10.0.0.10:443")[0]["rotulo"] == "10.0.0.10:443"

    def test_linha_sem_porta_e_erro_de_forma(self):
        with pytest.raises(ValueError):
            parse_checks("10.0.0.10 sem porta")

    def test_porta_nao_numerica_e_erro_de_forma(self):
        with pytest.raises(ValueError):
            parse_checks("10.0.0.10:abc Web")


class TestErrosDoForm:
    def test_form_valido_nao_tem_erro(self):
        assert erros_do_form(FORM_OK, criando=True) == {}

    def test_aponta_o_campo_de_cada_erro(self):
        """O diálogo marca o campo; um erro sem campo viraria toast genérico."""
        erros = erros_do_form(FORM_OK._replace(id="VPN INVÁLIDO"), criando=True)
        assert "id" in erros

    def test_campos_obrigatorios_vazios(self):
        erros = erros_do_form(form_vazio(), criando=True)
        for campo in ("id", "nome", "gateway_host", "usuario"):
            assert campo in erros

    def test_senha_obrigatoria_ao_criar(self):
        assert "senha" in erros_do_form(FORM_OK._replace(senha=""), criando=True)

    def test_senha_opcional_ao_editar(self):
        """Campo vazio na edição significa 'manter a atual'."""
        assert "senha" not in erros_do_form(FORM_OK._replace(senha=""), criando=False)

    def test_rede_invalida_aponta_o_campo_de_redes(self):
        erros = erros_do_form(FORM_OK._replace(redes_texto="nao-e-rede"), criando=True)
        assert "redes_texto" in erros

    def test_check_malformado_aponta_o_campo_de_checks(self):
        erros = erros_do_form(FORM_OK._replace(checks_texto="10.0.0.10 sem porta"), criando=True)
        assert "checks_texto" in erros

    def test_porta_fora_da_faixa(self):
        assert "gateway_porta" in erros_do_form(
            FORM_OK._replace(gateway_porta="99999"), criando=True
        )

    def test_trusted_cert_precisa_ser_hex_de_64(self):
        assert "trusted_cert" in erros_do_form(
            FORM_OK._replace(trusted_cert="xyz"), criando=True
        )

    def test_trusted_cert_vazio_e_valido(self):
        assert "trusted_cert" not in erros_do_form(FORM_OK, criando=True)


class TestPerfilDoForm:
    def test_monta_o_formato_do_protocolo(self):
        p = perfil_do_form(FORM_OK)
        assert p["gateway"] == {"host": "vpn.exemplo.com", "porta": 443}
        assert p["usuario"] == "usuario"
        assert p["redes"] == ["10.0.0.0/24", "10.0.1.0/24"]

    def test_nao_inclui_a_senha(self):
        """A senha vai separada, para não passear por dicionários que podem
        acabar logados ou serializados."""
        assert "senha" not in perfil_do_form(FORM_OK)


class TestSenhaParaEnvio:
    def test_criando_manda_o_que_foi_digitado(self):
        assert senha_para_envio(FORM_OK, criando=True) == "s3nha"

    def test_editando_com_campo_vazio_manda_none(self):
        assert senha_para_envio(FORM_OK._replace(senha=""), criando=False) is None

    def test_editando_com_senha_nova_manda_a_nova(self):
        assert senha_para_envio(FORM_OK._replace(senha="outra"), criando=False) == "outra"


class TestFormDeLeitura:
    def test_preenche_a_partir_da_resposta_do_helper(self):
        resposta = {
            "perfil": {
                "id": "vpn-exemplo",
                "nome": "Rede A",
                "proposito": "Serviços",
                "gateway": {"host": "vpn.exemplo.com", "porta": 443},
                "usuario": "usuario",
                "senha": SENTINELA_SENHA,
                "trusted_cert": "",
                "redes": ["10.0.0.0/24"],
                "checks": [{"host": "10.0.0.10", "porta": 443, "rotulo": "Web"}],
            },
            "preservar": [],
            "gerenciado": True,
        }
        f = form_de_leitura(resposta)
        assert f.id == "vpn-exemplo"
        assert f.gateway_porta == "443"
        assert f.redes_texto == "10.0.0.0/24"
        assert f.checks_texto == "10.0.0.10:443 Web"

    def test_campo_de_senha_nasce_vazio_nunca_com_a_sentinela(self):
        """Mostrar '__mantida__' num campo de senha faria o usuário achar que
        aquilo é a senha e apagá-la sem querer."""
        resposta = {
            "perfil": {
                "id": "vpn-exemplo",
                "nome": "n",
                "proposito": "p",
                "gateway": {"host": "h.exemplo.com", "porta": 443},
                "usuario": "u",
                "senha": SENTINELA_SENHA,
                "trusted_cert": "",
                "redes": [],
                "checks": [],
            },
            "preservar": [],
            "gerenciado": True,
        }
        assert form_de_leitura(resposta).senha == ""


# --------------------------------------------------------------------------
# 4.2 / 4.3 — o que a interface oferece em cada estado
# --------------------------------------------------------------------------

from vpn_manager.editor_form import (  # noqa: E402
    oferecer_reconectar,
    pode_editar,
    pode_remover,
)
from vpn_manager.models import State  # noqa: E402


class TestPodeEditar:
    @pytest.mark.parametrize(
        "estado",
        [State.ACTIVE, State.INACTIVE, State.PARTIAL, State.FAILED, State.UNCONFIGURED],
    )
    def test_permite_na_maioria_dos_estados(self, estado):
        assert pode_editar(estado) is True

    def test_bloqueia_perfil_externo(self):
        """Externo é um openfortivpn rodando fora do systemd, com config que
        o app não sabe de onde veio. Reescrever os artefatos por baixo dele
        deixaria o processo vivo com configuração que não corresponde mais ao
        disco."""
        assert pode_editar(State.EXTERNAL) is False


class TestPodeRemover:
    @pytest.mark.parametrize("estado", [State.INACTIVE, State.FAILED, State.UNCONFIGURED])
    def test_permite_quando_nao_ha_tunel(self, estado):
        assert pode_remover(estado) is True

    @pytest.mark.parametrize(
        "estado", [State.ACTIVE, State.PARTIAL, State.CONNECTING, State.EXTERNAL]
    )
    def test_bloqueia_com_tunel_no_ar(self, estado):
        """Apagar o .conf de um túnel ativo deixa a conexão órfã: ela continua
        de pé, mas sem nada em disco que a explique. O helper recusa de todo
        jeito; a interface não deve nem oferecer."""
        assert pode_remover(estado) is False


class TestOferecerReconectar:
    @pytest.mark.parametrize("estado", [State.ACTIVE, State.PARTIAL])
    def test_oferece_quando_o_tunel_esta_no_ar(self, estado):
        """Salvar reescreve o .conf, mas o túnel de pé continua com a
        configuração antiga em memória. Sem reconectar, a edição não tem
        efeito — e não avisar isso faz o usuário achar que não funcionou."""
        assert oferecer_reconectar(estado) is True

    @pytest.mark.parametrize(
        "estado", [State.INACTIVE, State.FAILED, State.UNCONFIGURED, State.EXTERNAL]
    )
    def test_nao_oferece_sem_tunel_proprio(self, estado):
        assert oferecer_reconectar(estado) is False
