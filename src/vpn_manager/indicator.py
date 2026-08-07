"""Indicador de bandeja. Processo SEPARADO da janela: esta biblioteca é GTK3
e o PyGObject não carrega Gtk 3.0 e 4.0 no mesmo processo."""
import subprocess
import sys
import traceback

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import AyatanaAppIndicator3 as AppIndicator  # noqa: E402
from gi.repository import GLib, Gtk  # noqa: E402

from .cli import CatalogError, _catalogo  # noqa: E402
from .models import State  # noqa: E402
from .vpnctl import status_of  # noqa: E402

INTERVALO_MS = 10_000

# No painel do Shell o ícone é recolorido para branco: cor não distingue nada.
# A distinção no painel é só silhueta; o detalhe vem no texto do menu.
ICONE_OK = "network-vpn-symbolic"
ICONE_ATENCAO = "dialog-warning-symbolic"
ICONE_VAZIO = "network-vpn-disconnected-symbolic"

ROTULOS = {
    State.ACTIVE: "Conectada",
    State.PARTIAL: "Sem rota",
    State.EXTERNAL: "Fora do systemd",
    State.INACTIVE: "Desconectada",
    State.FAILED: "Falhou",
    State.UNCONFIGURED: "Não configurada",
    State.CONNECTING: "Conectando…",
}

# Seção 5 do documento de layout: N ("túnel no ar") = ativo + parcial + externo.
# CONNECTING foi incluído aqui também: a linha "Ação em curso" da mesma tabela
# mostra label "N", não vazio — decisão registrada no relatório da task, já que
# a seção não é 100% explícita sobre CONNECTING entrar na soma. CONNECTING fica
# de fora de ATENCAO porque essa mesma linha usa status ACTIVE, não ATTENTION.
NO_AR = (State.ACTIVE, State.PARTIAL, State.EXTERNAL, State.CONNECTING)
ATENCAO = (State.PARTIAL, State.EXTERNAL, State.FAILED)


class Indicator:
    def __init__(self):
        # _catalogo() levanta CatalogError se o catálogo não existir/for inválido.
        # Sem tratar isso aqui, a primeira atualização derrubaria o processo do
        # indicador (bandeja de longa duração) com stack trace.
        self._perfis: tuple = ()
        self._erro_catalogo: str | None = None
        try:
            self._perfis = _catalogo()
        except CatalogError as e:
            self._erro_catalogo = str(e)

        # Handles das janelas abertas via "Detalhes…", para colher (poll()) as
        # que já terminaram e evitar processos zumbis (achado D da revisão).
        self._janelas: list[subprocess.Popen] = []

        # Guarda contra ciclos sobrepostos (achado B, parte 2): True enquanto
        # há uma consulta em voo (thread rodando ou idle_add agendado, ainda
        # sem resposta aplicada). _atualizar() pula o tick se este flag já
        # estiver True, em vez de empilhar outra thread batendo em systemctl.
        self._consulta_em_andamento = False

        self._ind = AppIndicator.Indicator.new(
            "vpn-manager", ICONE_VAZIO,
            AppIndicator.IndicatorCategory.SYSTEM_SERVICES,
        )
        self._ind.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self._menu = Gtk.Menu()
        self._ind.set_menu(self._menu)
        self._atualizar()
        GLib.timeout_add(INTERVALO_MS, self._atualizar)

    def _atualizar(self):
        """Callback do `GLib.timeout_add` — roda na thread principal do GTK3.

        Precisa ser rápido e nunca deixar uma exceção escapar: uma exceção não
        tratada aqui não derruba o processo (verificado empiricamente), mas o
        GLib simplesmente para de reinvocar esta fonte — sem um item "Atualizar
        estado" no menu, o ícone ficaria congelado no último estado para
        sempre. Daí o try/except abrangente e o retorno incondicional de
        `GLib.SOURCE_CONTINUE`.

        Também não dispara uma nova consulta se a anterior ainda não voltou
        (`self._consulta_em_andamento`): com três perfis e um `systemctl`
        lento/travado, um ciclo de `status_of()` pode passar dos 10s do timer,
        e sem essa guarda threads se acumulariam sem limite e a resposta mais
        antiga poderia chegar depois da mais nova e sobrescrever o menu com
        dado obsoleto.
        """
        if self._consulta_em_andamento:
            print(
                "indicator: ciclo de atualização anterior ainda em voo "
                "(systemctl/rotas lentos?) — pulando este tick",
                file=sys.stderr,
            )
        else:
            self._consulta_em_andamento = True
            try:
                self._disparar_consulta()
            except Exception:
                traceback.print_exc()
                # a exceção aconteceu antes de qualquer thread/idle_add ser
                # agendado — sem isso o flag ficaria preso em True para
                # sempre, congelando as atualizações (achado C por outra
                # porta). Caminhos que já agendaram algo (thread ou idle_add
                # direto) limpam o flag em _aplicar(), não aqui.
                self._consulta_em_andamento = False

        try:
            self._colher_janelas()
        except Exception:
            traceback.print_exc()
        return GLib.SOURCE_CONTINUE

    def _disparar_consulta(self):
        """`status_of()` chama `systemctl show` e lê rotas (até 5s de timeout
        cada); rodar isso direto neste callback travaria o menu do indicador
        por vários segundos a cada ciclo de 10s — o clique no ícone não abriria
        o menu nesse intervalo. Mesmo padrão de `window.py`: uma thread de
        trabalho produz os dados, `GLib.idle_add` aplica no menu de volta na
        thread principal. Nenhum widget é tocado fora dela.
        """
        # Item 6 da rodada de fechamento pré-merge: relê o catálogo em TODO
        # ciclo, não só quando o ciclo anterior deu erro. Antes disso, uma
        # quarta VPN acrescentada ao TOML só aparecia no indicador depois que
        # o catálogo tivesse ficado inválido uma vez (o README dizia
        # "releem o catálogo a cada atualização" — só a metade que checa erro
        # fazia isso de verdade). `_catalogo()` só lê um arquivo TOML local:
        # barato reconferir a cada 10s. Se a releitura falhar mas já
        # tínhamos um catálogo bom, segue com o último conhecido em vez de
        # travar o indicador por causa de uma falha transitória.
        try:
            perfis = _catalogo()
            self._perfis = perfis
            self._erro_catalogo = None
        except CatalogError as e:
            if self._perfis:
                perfis = self._perfis
            else:
                self._erro_catalogo = str(e)
                GLib.idle_add(self._aplicar, None, str(e))
                return

        def trabalho():
            resultados = None
            erro = None
            try:
                resultados = [status_of(perfil) for perfil in perfis]
            except Exception as e:
                erro = f"falha ao consultar estado: {e}"
            finally:
                # try/finally garante que o idle_add — e portanto a liberação
                # de _consulta_em_andamento em _aplicar() — sempre acontece,
                # mesmo se status_of() levantar algo além de Exception. Um
                # flag preso em True congelaria as próximas atualizações para
                # sempre, o mesmo bug do achado C por outro caminho.
                GLib.idle_add(self._aplicar, resultados, erro)

        import threading
        threading.Thread(target=trabalho, daemon=True).start()

    def _aplicar(self, resultados, erro):
        """Roda na thread principal via `GLib.idle_add` — aqui é seguro tocar
        no `Gtk.Menu`. Blindado também: um erro ao reconstruir o menu não pode
        deixar os itens antigos pela metade nem propagar para fora do idle
        callback (mesmo raciocínio do achado C, aplicado ao outro lado da
        thread). O `finally` libera `_consulta_em_andamento` em qualquer
        caminho — sucesso, erro de consulta ou erro de reconstrução do menu —
        para o próximo tick sempre poder disparar uma nova consulta."""
        try:
            self._reconstruir_menu(resultados, erro)
        except Exception:
            traceback.print_exc()
        finally:
            self._consulta_em_andamento = False
        return GLib.SOURCE_REMOVE

    def _reconstruir_menu(self, resultados, erro):
        for item in self._menu.get_children():
            self._menu.remove(item)

        no_ar = atencao = 0
        if erro is not None:
            item = Gtk.MenuItem(label=f"Falha ao atualizar: {erro}")
            item.set_sensitive(False)
            self._menu.append(item)
            atencao = 1
        else:
            for st in resultados:
                # Correção 6 da re-revisão de fechamento pré-merge: com o
                # `systemctl` falhando, `status_of` degrada (`read_ok=False`)
                # e o `state` cru vira tipicamente INACTIVE (props vazio) —
                # nem NO_AR nem ATENCAO, então a bandeja mostrava
                # "Desconectada" e derrubava a contagem pra 0, sem NENHUM
                # sinal de atenção (antes desta rodada, o mesmo `systemctl`
                # falhando virava EXTERNAL/ATTENTION por acidente — um
                # falso-positivo, mas pelo menos não silencioso). Janela e CLI
                # já marcam essa degradação (`read_ok`); o indicador — a
                # única superfície sempre visível — não pode ser o único
                # lugar mentindo "tudo bem". Sem botão destrutivo aqui, então
                # basta não mentir: texto do item + conta como atenção.
                if not st.read_ok:
                    atencao += 1
                    rotulo = "Não verificada"
                else:
                    if st.state in NO_AR:
                        no_ar += 1
                    if st.state in ATENCAO:
                        atencao += 1
                    rotulo = ROTULOS[st.state]
                item = Gtk.MenuItem(label=f"{st.profile.name} — {rotulo}")
                item.set_sensitive(False)
                self._menu.append(item)

        self._menu.append(Gtk.SeparatorMenuItem())
        detalhes = Gtk.MenuItem(label="Detalhes…")
        detalhes.connect("activate", self._abrir_janela)
        self._menu.append(detalhes)
        sair = Gtk.MenuItem(label="Sair")
        sair.connect("activate", lambda *_: Gtk.main_quit())
        self._menu.append(sair)
        self._menu.show_all()

        # Contagem sempre visível quando há túnel no ar (ativo, parcial, externo
        # ou conectando) — era a pergunta que originou o projeto. Ícone E status
        # mudam: nem toda versão da extensão do Shell honra ATTENTION.
        self._ind.set_label(str(no_ar) if no_ar else "", "")
        if atencao:
            self._ind.set_icon_full(ICONE_ATENCAO, "VPN exige atenção")
            self._ind.set_status(AppIndicator.IndicatorStatus.ATTENTION)
        else:
            self._ind.set_icon_full(ICONE_OK if no_ar else ICONE_VAZIO, "VPN")
            self._ind.set_status(AppIndicator.IndicatorStatus.ACTIVE)

    def _abrir_janela(self, *_):
        # start_new_session desacopla a janela da sessão do indicador (não
        # recebe sinais endereçados ao grupo de processo do indicador); o
        # handle fica em self._janelas para _colher_janelas() reapear o
        # processo quando ele terminar, evitando zumbi (achado D).
        self._janelas.append(
            subprocess.Popen(
                [sys.executable, "-m", "vpn_manager.window"],
                start_new_session=True,
            )
        )

    def _colher_janelas(self):
        self._janelas = [p for p in self._janelas if p.poll() is None]


def main():
    Indicator()
    Gtk.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
