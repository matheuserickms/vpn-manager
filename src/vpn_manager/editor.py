"""Diálogo de criar e editar perfil.

Toda decisão vive em `editor_form` e em `profile_client`; aqui fica a
montagem dos campos e o vaivém com a interface. A classe é construível sem
janela mãe (`pai=None`) para poder ser testada.
"""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk  # noqa: E402

from . import profile_client
from .editor_form import (
    Form,
    erros_do_form,
    form_vazio,
    perfil_do_form,
    senha_para_envio,
)

# Erro do helper vem com o nome do campo do PROTOCOLO; o formulário usa
# nomes próprios. Sem esta tradução, o erro chegaria sem campo e viraria
# mensagem geral, deixando o usuário caçar o que foi recusado.
_CAMPO_DO_HELPER = {
    "redes": "redes_texto",
    "checks": "checks_texto",
    "gateway": "gateway_host",
    "usuario": "usuario",
    "username": "usuario",
    "id": "id",
    "nome": "nome",
    "proposito": "proposito",
    "porta": "gateway_porta",
    "host": "gateway_host",
    "trusted-cert": "trusted_cert",
}


def _traduzir_campo(campo: str | None) -> str | None:
    if not campo:
        return None
    # O helper manda coisas como "redes[2]"; o formulário tem um campo só.
    base = campo.split("[")[0]
    return _CAMPO_DO_HELPER.get(base, base)


class EditorPerfil:
    """Diálogo de perfil. `criando=False` significa edição de perfil existente."""

    def __init__(self, *, pai, cliente=profile_client, form: Form | None = None,
                 criando: bool = True, ao_salvar=None,
                 reconectar_apos_salvar: bool = False,
                 gerenciado: bool = True):
        self._pai = pai
        self._cliente = cliente
        self._form = form or form_vazio()
        self._criando = criando
        self._ao_salvar = ao_salvar
        # Só faz sentido editando: um perfil recém-criado não tem túnel no ar.
        self._reconectar = reconectar_apos_salvar and not criando
        # Perfil escrito à mão precisa passar pelo `assume`, que tira o
        # script de rotas antigo do caminho. Editar direto deixaria dois
        # scripts instalando rota para o mesmo túnel.
        self._gerenciado = gerenciado
        # Nome do script antigo que o helper pediu para confirmar.
        self.confirmacao_pendente: str | None = None

        # Renomear muda o nome da unit, do .conf e do script; um perfil
        # conectado ficaria órfão. Está fora de escopo por decisão do design.
        self.id_editavel = criando
        self.erros_visiveis: dict[str, str] = {}
        self.mensagem_geral: str | None = None

        self._campos: dict[str, object] = {}
        # Título original de cada campo, para restaurar quando o erro sai.
        self._rotulos: dict[str, str] = {}
        self._dialogo = None
        if pai is not None:
            self._montar()

    # -- montagem ----------------------------------------------------------
    def _montar(self):
        self._dialogo = Adw.Dialog(title="Novo perfil" if self._criando else "Editar perfil")
        self._dialogo.set_content_width(520)

        barra = Adw.ToolbarView()
        cabecalho = Adw.HeaderBar()
        cancelar = Gtk.Button(label="Cancelar")
        cancelar.connect("clicked", lambda *_: self._dialogo.close())
        salvar = Gtk.Button(label="Salvar", css_classes=["suggested-action"])
        salvar.connect("clicked", lambda *_: self.salvar())
        cabecalho.pack_start(cancelar)
        cabecalho.pack_end(salvar)
        barra.add_top_bar(cabecalho)

        pagina = Adw.PreferencesPage()
        identidade = Adw.PreferencesGroup(title="Identificação")
        self._linha(identidade, "id", "Identificador", sensivel=self.id_editavel)
        self._linha(identidade, "nome", "Nome")
        self._linha(identidade, "proposito", "Propósito")
        pagina.add(identidade)

        conexao = Adw.PreferencesGroup(title="Gateway")
        self._linha(conexao, "gateway_host", "Endereço")
        self._linha(conexao, "gateway_porta", "Porta")
        self._linha(conexao, "usuario", "Usuário")
        self._linha(conexao, "senha", "Senha", senha=True)
        self._linha(conexao, "trusted_cert", "trusted-cert (opcional)")
        pagina.add(conexao)

        rede = Adw.PreferencesGroup(
            title="Rotas e verificações",
            description="Uma rede por linha. Verificações no formato host:porta rótulo.",
        )
        self._linha(rede, "redes_texto", "Redes")
        self._linha(rede, "checks_texto", "Verificações")
        pagina.add(rede)

        barra.set_content(pagina)
        self._dialogo.set_child(barra)

    def _linha(self, grupo, campo, rotulo, *, senha=False, sensivel=True):
        # AdwEntryRow e AdwPasswordEntryRow NÃO têm set_subtitle — só
        # AdwActionRow e AdwExpanderRow têm. Dica e erro vão no título e no
        # tooltip, que existem em qualquer widget.
        if senha and not self._criando:
            rotulo = f"{rotulo} (em branco = manter a atual)"

        classe = Adw.PasswordEntryRow if senha else Adw.EntryRow
        linha = classe(title=rotulo)
        linha.set_text(getattr(self._form, campo))
        if not sensivel:
            linha.set_sensitive(False)
            linha.set_tooltip_text("o identificador não pode ser alterado")
        grupo.add(linha)
        self._campos[campo] = linha
        self._rotulos[campo] = rotulo

    def _ler_form(self) -> Form:
        if not self._campos:
            return self._form
        valores = {campo: linha.get_text() for campo, linha in self._campos.items()}
        return self._form._replace(**valores)

    # -- ação --------------------------------------------------------------
    def salvar(self):
        """Valida, chama o helper numa thread e trata a resposta."""
        self._form = self._ler_form()
        self.mensagem_geral = None

        erros = erros_do_form(self._form, criando=self._criando)
        if erros:
            self._marcar(erros)
            return

        self._enviar(confirmar=None)

    def confirmar_e_salvar(self, nome_do_script: str):
        """Reenvia o `assume` autorizando mover o script antigo."""
        self.confirmacao_pendente = None
        self._enviar(confirmar=nome_do_script)

    def _enviar(self, *, confirmar):
        perfil = perfil_do_form(self._form)
        senha = senha_para_envio(self._form, criando=self._criando)
        criando = self._criando
        gerenciado = self._gerenciado

        def trabalho():
            # pkexec bloqueia enquanto o usuário digita a senha; isso não pode
            # acontecer na thread do GLib, senão a janela inteira congela.
            try:
                if criando:
                    self._cliente.create(perfil, senha=senha)
                elif not gerenciado:
                    self._cliente.assume(perfil, senha=senha, confirmar=confirmar)
                else:
                    self._cliente.update(perfil, senha=senha)
                erro = None
            except profile_client.ClientError as e:
                erro = e
            except Exception as e:  # noqa: BLE001
                erro = profile_client.ClientError(f"erro inesperado: {type(e).__name__}: {e}")
            GLib.idle_add(self._terminou, perfil["id"], erro)

        if self._pai is None:
            trabalho()  # no teste, síncrono
            return

        import threading

        threading.Thread(target=trabalho, daemon=True).start()

    def _terminou(self, pid, erro):
        if erro is None:
            self.erros_visiveis = {}
            if self._dialogo is not None:
                self._dialogo.close()
            if self._ao_salvar is not None:
                self._ao_salvar(pid, reconectar=self._reconectar)
            return GLib.SOURCE_REMOVE

        # O helper achou script manual e quer o nome confirmado. Não é erro
        # de campo: é uma pergunta, e a resposta é do usuário.
        if erro.codigo == "confirmacao_necessaria":
            self.confirmacao_pendente = pid
            self.mensagem_geral = str(erro)
            if self._pai is not None:
                self._pai.pedir_confirmacao_de_assume(self, str(erro))
            return GLib.SOURCE_REMOVE

        campo = _traduzir_campo(erro.campo)
        if campo:
            self._marcar({campo: str(erro)})
        else:
            self.mensagem_geral = str(erro)
            if self._pai is not None:
                self._pai.avisar(str(erro))
        return GLib.SOURCE_REMOVE

    def _marcar(self, erros: dict[str, str]):
        self.erros_visiveis = erros
        for campo, linha in self._campos.items():
            if campo in erros:
                linha.add_css_class("error")
                # Sem subtitle disponível, a mensagem vai para o título e o
                # tooltip: o título é o que se lê sem interagir, o tooltip
                # cabe o texto inteiro quando a mensagem é longa.
                linha.set_title(f"{self._rotulos[campo]} — {erros[campo]}")
                linha.set_tooltip_text(erros[campo])
            else:
                linha.remove_css_class("error")
                linha.set_title(self._rotulos[campo])
                linha.set_tooltip_text(None)

    def present(self):
        if self._dialogo is not None and self._pai is not None:
            self._dialogo.present(self._pai)


def confirmacao_valida(digitado: str, pid: str) -> bool:
    """A remoção só prossegue se o usuário digitar o id exato.

    Sensível a maiúscula de propósito: o id é minúsculo por construção, então
    aceitar variação seria abrir espaço para confirmar no automático — o que
    anula a razão de existir da confirmação.
    """
    return digitado.strip() == pid


class DialogoRemocao:
    """Confirmação de remoção (decisão D5).

    Remover apaga o `.conf`, o script de rotas e a entrada do catálogo. Há
    cópia no undo, mas ela não é exposta pela interface — então, do ponto de
    vista de quem clica, é irreversível.
    """

    def __init__(self, *, pai, status, cliente=profile_client, ao_remover=None):
        self._pai = pai
        self._status = status
        self._cliente = cliente
        self._ao_remover = ao_remover
        self._pid = status.profile.id
        self.erro: str | None = None

        self._dialogo = None
        self._entrada = None
        if pai is not None:
            self._montar()

    def _montar(self):
        self._dialogo = Adw.AlertDialog(
            heading=f"Remover {self._status.profile.name}?",
            body=(
                f"Isto apaga a configuração, o script de rotas e a entrada do "
                f"catálogo do perfil {self._pid}.\n\n"
                f"Digite {self._pid} para confirmar."
            ),
        )
        self._entrada = Adw.EntryRow(title="Identificador do perfil")
        self._dialogo.set_extra_child(self._entrada)
        self._dialogo.add_response("cancelar", "Cancelar")
        self._dialogo.add_response("remover", "Remover")
        self._dialogo.set_response_appearance("remover", Adw.ResponseAppearance.DESTRUCTIVE)
        self._dialogo.set_default_response("cancelar")
        self._dialogo.set_close_response("cancelar")
        self._dialogo.connect("response", self._respondeu)

    def _respondeu(self, _dialogo, resposta):
        if resposta != "remover":
            return
        digitado = self._entrada.get_text() if self._entrada is not None else ""
        self.remover(digitado)

    def remover(self, digitado: str):
        if not confirmacao_valida(digitado, self._pid):
            self.erro = "o identificador digitado não confere"
            if self._pai is not None:
                self._pai.avisar(self.erro)
            return

        pid = self._pid

        def trabalho():
            try:
                self._cliente.delete(pid)
                erro = None
            except profile_client.ClientError as e:
                erro = e
            except Exception as e:  # noqa: BLE001
                erro = profile_client.ClientError(f"erro inesperado: {type(e).__name__}: {e}")
            GLib.idle_add(self._terminou, pid, erro)

        if self._pai is None:
            trabalho()
            return

        import threading

        threading.Thread(target=trabalho, daemon=True).start()

    def _terminou(self, pid, erro):
        if erro is not None:
            self.erro = str(erro)
            if self._pai is not None:
                self._pai.avisar(str(erro))
        elif self._ao_remover is not None:
            self._ao_remover(pid)
        return GLib.SOURCE_REMOVE

    def present(self):
        if self._dialogo is not None and self._pai is not None:
            self._dialogo.present(self._pai)
