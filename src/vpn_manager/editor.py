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
                 criando: bool = True, ao_salvar=None):
        self._pai = pai
        self._cliente = cliente
        self._form = form or form_vazio()
        self._criando = criando
        self._ao_salvar = ao_salvar

        # Renomear muda o nome da unit, do .conf e do script; um perfil
        # conectado ficaria órfão. Está fora de escopo por decisão do design.
        self.id_editavel = criando
        self.erros_visiveis: dict[str, str] = {}
        self.mensagem_geral: str | None = None

        self._campos: dict[str, object] = {}
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
        classe = Adw.PasswordEntryRow if senha else Adw.EntryRow
        linha = classe(title=rotulo)
        linha.set_text(getattr(self._form, campo))
        if not sensivel:
            linha.set_sensitive(False)
        if senha and not self._criando:
            linha.set_subtitle("deixe em branco para manter a atual")
        grupo.add(linha)
        self._campos[campo] = linha

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

        perfil = perfil_do_form(self._form)
        senha = senha_para_envio(self._form, criando=self._criando)
        criando = self._criando

        def trabalho():
            # pkexec bloqueia enquanto o usuário digita a senha; isso não pode
            # acontecer na thread do GLib, senão a janela inteira congela.
            try:
                if criando:
                    self._cliente.create(perfil, senha=senha)
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
                self._ao_salvar(pid)
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
                linha.set_subtitle(erros[campo])
            else:
                linha.remove_css_class("error")

    def present(self):
        if self._dialogo is not None and self._pai is not None:
            self._dialogo.present(self._pai)
