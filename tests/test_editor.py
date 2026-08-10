"""Testes do diálogo de edição.

Não testam desenho — testam o que o diálogo DECIDE: quando chama o helper,
com quais dados, e o que faz quando ele recusa.
"""

import gi_stub

gi_stub.instalar()

import pytest  # noqa: E402

from vpn_manager.editor import (  # noqa: E402
    DialogoRemocao,
    EditorPerfil,
    confirmacao_valida,
)
from vpn_manager.editor_form import Form  # noqa: E402
from vpn_manager.profile_client import ClientError  # noqa: E402

FORM_OK = Form(
    id="vpn-exemplo",
    nome="Rede A",
    proposito="Serviços",
    gateway_host="vpn.exemplo.com",
    gateway_porta="443",
    usuario="usuario",
    senha="s3nha",
    trusted_cert="",
    redes_texto="10.0.0.0/24",
    checks_texto="",
)


class ClienteFalso:
    def __init__(self, erro=None):
        self.erro = erro
        self.chamadas = []

    def create(self, perfil, *, senha):
        self.chamadas.append(("create", perfil, senha))
        if self.erro:
            raise self.erro
        return {"ok": True}

    def update(self, perfil, *, senha):
        self.chamadas.append(("update", perfil, senha))
        if self.erro:
            raise self.erro
        return {"ok": True}


class TestSalvar:
    def test_criando_chama_create_com_a_senha_digitada(self):
        cliente = ClienteFalso()
        ed = EditorPerfil(pai=None, cliente=cliente, form=FORM_OK, criando=True)

        ed.salvar()

        verbo, perfil, senha = cliente.chamadas[0]
        assert verbo == "create"
        assert perfil["id"] == "vpn-exemplo"
        assert senha == "s3nha"

    def test_editando_com_senha_vazia_manda_none(self):
        cliente = ClienteFalso()
        ed = EditorPerfil(
            pai=None, cliente=cliente, form=FORM_OK._replace(senha=""), criando=False
        )

        ed.salvar()

        _, _, senha = cliente.chamadas[0]
        assert senha is None

    def test_form_invalido_nao_chama_o_helper(self):
        """Não acordar o prompt do polkit para um formulário que já se sabe
        recusado."""
        cliente = ClienteFalso()
        ed = EditorPerfil(
            pai=None, cliente=cliente, form=FORM_OK._replace(id="INVÁLIDO"), criando=True
        )

        ed.salvar()

        assert cliente.chamadas == []
        assert "id" in ed.erros_visiveis

    def test_erro_do_helper_com_campo_marca_o_campo(self):
        cliente = ClienteFalso(erro=ClientError("rede ruim", codigo="validacao", campo="redes"))
        ed = EditorPerfil(pai=None, cliente=cliente, form=FORM_OK, criando=True)

        ed.salvar()

        assert "redes_texto" in ed.erros_visiveis or "redes" in ed.erros_visiveis

    def test_erro_sem_campo_vira_mensagem_geral(self):
        cliente = ClienteFalso(erro=ClientError("autorização negada"))
        ed = EditorPerfil(pai=None, cliente=cliente, form=FORM_OK, criando=True)

        ed.salvar()

        assert ed.mensagem_geral
        assert "autoriza" in ed.mensagem_geral.lower()

    def test_sucesso_fecha_o_dialogo_e_avisa_quem_chamou(self):
        cliente = ClienteFalso()
        avisos = []
        ed = EditorPerfil(
            pai=None, cliente=cliente, form=FORM_OK, criando=True,
            ao_salvar=lambda pid, reconectar=False: avisos.append(pid),
        )

        ed.salvar()

        assert avisos == ["vpn-exemplo"]

    def test_falha_nao_avisa_quem_chamou(self):
        cliente = ClienteFalso(erro=ClientError("nao deu"))
        avisos = []
        ed = EditorPerfil(
            pai=None, cliente=cliente, form=FORM_OK, criando=True,
            ao_salvar=lambda pid, reconectar=False: avisos.append(pid),
        )

        ed.salvar()

        assert avisos == []


class TestIdImutavel:
    def test_editando_nao_deixa_trocar_o_id(self):
        """Renomear está fora de escopo: mudaria o nome da unit, do .conf e do
        script, e um perfil conectado ficaria órfão."""
        ed = EditorPerfil(pai=None, cliente=ClienteFalso(), form=FORM_OK, criando=False)
        assert ed.id_editavel is False

    def test_criando_deixa_definir_o_id(self):
        ed = EditorPerfil(pai=None, cliente=ClienteFalso(), form=FORM_OK, criando=True)
        assert ed.id_editavel is True


class TestReconectarAposSalvar:
    """Item 4.3: com o túnel no ar, salvar reescreve o .conf mas a conexão
    viva segue com a configuração antiga em memória."""

    def test_avisa_quem_chamou_que_precisa_reconectar(self):
        cliente = ClienteFalso()
        recebidos = []
        ed = EditorPerfil(
            pai=None, cliente=cliente, form=FORM_OK, criando=False,
            reconectar_apos_salvar=True,
            ao_salvar=lambda pid, reconectar=False: recebidos.append((pid, reconectar)),
        )

        ed.salvar()

        assert recebidos == [("vpn-exemplo", True)]

    def test_sem_tunel_no_ar_nao_pede_reconexao(self):
        cliente = ClienteFalso()
        recebidos = []
        ed = EditorPerfil(
            pai=None, cliente=cliente, form=FORM_OK, criando=False,
            reconectar_apos_salvar=False,
            ao_salvar=lambda pid, reconectar=False: recebidos.append((pid, reconectar)),
        )

        ed.salvar()

        assert recebidos == [("vpn-exemplo", False)]

    def test_criando_nunca_pede_reconexao(self):
        cliente = ClienteFalso()
        recebidos = []
        ed = EditorPerfil(
            pai=None, cliente=cliente, form=FORM_OK, criando=True,
            ao_salvar=lambda pid, reconectar=False: recebidos.append((pid, reconectar)),
        )

        ed.salvar()

        assert recebidos == [("vpn-exemplo", False)]


class TestConfirmacaoDeRemocao:
    """Decisão D5: remover exige digitar o id."""

    def test_id_exato_confirma(self):
        assert confirmacao_valida("vpn-exemplo", "vpn-exemplo") is True

    def test_id_errado_nao_confirma(self):
        assert confirmacao_valida("vpn-outro", "vpn-exemplo") is False

    def test_ignora_espaco_em_volta(self):
        assert confirmacao_valida("  vpn-exemplo  ", "vpn-exemplo") is True

    def test_vazio_nao_confirma(self):
        assert confirmacao_valida("", "vpn-exemplo") is False

    def test_e_sensivel_a_maiuscula(self):
        """O id é minúsculo por construção; aceitar variação convidaria a
        confirmar no automático."""
        assert confirmacao_valida("VPN-EXEMPLO", "vpn-exemplo") is False


class PaiFalso:
    def __init__(self):
        self.avisos = []

    def avisar(self, mensagem):
        self.avisos.append(mensagem)


class TestMontagem:
    """Com `pai=None` o diálogo não constrói widget nenhum — o que deixou
    `_montar` inteiro sem cobertura, e um AttributeError chegou à janela
    real. Estes testes constroem de verdade."""

    @pytest.mark.parametrize("criando", [True, False])
    def test_monta_sem_quebrar(self, criando):
        EditorPerfil(pai=PaiFalso(), cliente=ClienteFalso(), form=FORM_OK, criando=criando)

    def test_editando_monta_o_campo_de_senha(self):
        """O caso exato que quebrou: na edição o campo de senha recebe uma
        dica de 'deixe em branco para manter'."""
        ed = EditorPerfil(pai=PaiFalso(), cliente=ClienteFalso(), form=FORM_OK, criando=False)
        assert "senha" in ed._campos

    def test_monta_o_dialogo_de_remocao(self):
        class StatusFalso:
            class profile:
                id = "vpn-exemplo"
                name = "Rede A"

        DialogoRemocao(pai=PaiFalso(), status=StatusFalso(), cliente=ClienteFalso())


class ClienteComAssume(ClienteFalso):
    def __init__(self, erro=None, erro_assume=None):
        super().__init__(erro)
        self.erro_assume = erro_assume

    def assume(self, perfil, *, senha, confirmar=None):
        self.chamadas.append(("assume", perfil, senha, confirmar))
        if self.erro_assume:
            erro, self.erro_assume = self.erro_assume, None
            raise erro
        return {"ok": True}


class TestPerfilNaoGerenciado:
    """Editar um perfil configurado à mão precisa passar pelo `assume`: é ele
    que tira o script de rotas antigo do caminho. Sem isso, ficam dois
    scripts instalando rota para o mesmo túnel."""

    def test_salvar_perfil_nao_gerenciado_chama_assume(self):
        cliente = ClienteComAssume()
        ed = EditorPerfil(
            pai=None, cliente=cliente, form=FORM_OK, criando=False, gerenciado=False
        )

        ed.salvar()

        assert cliente.chamadas[0][0] == "assume"

    def test_perfil_gerenciado_continua_usando_update(self):
        cliente = ClienteComAssume()
        ed = EditorPerfil(
            pai=None, cliente=cliente, form=FORM_OK, criando=False, gerenciado=True
        )

        ed.salvar()

        assert cliente.chamadas[0][0] == "update"

    def test_criar_nunca_usa_assume(self):
        cliente = ClienteComAssume()
        ed = EditorPerfil(pai=None, cliente=cliente, form=FORM_OK, criando=True)

        ed.salvar()

        assert cliente.chamadas[0][0] == "create"

    def test_pedido_de_confirmacao_vira_mensagem_com_o_nome_do_script(self):
        """O helper recusa e diz quais scripts encontrou; o usuário precisa
        ver o nome para poder confirmar."""
        cliente = ClienteComAssume(
            erro_assume=ClientError(
                "há script de rotas escrito à mão: 51foco",
                codigo="confirmacao_necessaria",
                campo="confirmar",
            )
        )
        ed = EditorPerfil(
            pai=None, cliente=cliente, form=FORM_OK, criando=False, gerenciado=False
        )

        ed.salvar()

        assert ed.confirmacao_pendente is not None
        assert "51foco" in ed.mensagem_geral

    def test_confirmar_reenvia_com_o_nome_do_script(self):
        cliente = ClienteComAssume(
            erro_assume=ClientError(
                "há script: 51foco", codigo="confirmacao_necessaria", campo="confirmar"
            )
        )
        ed = EditorPerfil(
            pai=None, cliente=cliente, form=FORM_OK, criando=False, gerenciado=False
        )
        ed.salvar()

        ed.confirmar_e_salvar("51foco")

        assert cliente.chamadas[-1][0] == "assume"
        assert cliente.chamadas[-1][3] == "51foco"
