"""Testes do diálogo de edição.

Não testam desenho — testam o que o diálogo DECIDE: quando chama o helper,
com quais dados, e o que faz quando ele recusa.
"""

import gi_stub

gi_stub.instalar()

import pytest  # noqa: E402

from vpn_manager.editor import EditorPerfil  # noqa: E402
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
            ao_salvar=lambda pid: avisos.append(pid),
        )

        ed.salvar()

        assert avisos == ["vpn-exemplo"]

    def test_falha_nao_avisa_quem_chamou(self):
        cliente = ClienteFalso(erro=ClientError("nao deu"))
        avisos = []
        ed = EditorPerfil(
            pai=None, cliente=cliente, form=FORM_OK, criando=True,
            ao_salvar=lambda pid: avisos.append(pid),
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
