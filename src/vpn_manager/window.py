import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from .models import State  # noqa: E402

# Tabela canônica da seção 2 do documento de layout.
# Três canais redundantes: ícone (forma), classe (cor) e rótulo (palavra).
APRESENTACAO = {
    State.ACTIVE:       ("network-vpn-symbolic",              "success",   "Conectada"),
    State.PARTIAL:      ("network-vpn-no-route-symbolic",     "warning",   "Sem rota"),
    State.EXTERNAL:     ("dialog-warning-symbolic",           "warning",   "Fora do systemd"),
    State.INACTIVE:     ("network-vpn-disconnected-symbolic", "dim-label", "Desconectada"),
    State.FAILED:       ("dialog-error-symbolic",             "error",     "Falhou"),
    State.UNCONFIGURED: ("network-vpn-disabled-symbolic",     "dim-label", "Não configurada"),
    State.CONNECTING:   ("network-vpn-acquiring-symbolic",    "accent",    "Conectando…"),
}

# Ações por estado — tabela "Ações por estado" do documento de design.
BOTAO_PRINCIPAL = {
    State.ACTIVE:       ("Desconectar", "stop"),
    State.PARTIAL:      ("Reconectar",  "restart"),
    State.EXTERNAL:     ("Adotar",      "adopt"),
    State.INACTIVE:     ("Conectar",    "start"),
    State.FAILED:       ("Conectar",    "start"),
    State.UNCONFIGURED: (None,          None),
    State.CONNECTING:   (None,          None),
}

EXPANDE_SOZINHO = {State.PARTIAL, State.EXTERNAL, State.FAILED, State.UNCONFIGURED}

# Crítico A da auditoria: `status.missing` só é preenchido pelo `status_of` nos
# estados abaixo (ver `vpnctl.py`) — nos demais é uma tupla vazia por
# construção, não porque a rota foi checada e está OK. Uma linha de rede só
# pode afirmar "Roteada"/"Faltando" quando o estado pertence a este conjunto;
# fora dele o app não tem informação confiável e precisa dizer isso, não
# inventar um selo verde.
#
# EXTERNAL entrou neste conjunto na revisão seguinte (Important 1(b)):
# `status_of` agora acha o `pppd` filho de um processo externo (a mesma
# travessia pai→filho do Crítico 1) e calcula a interface dele também, então
# `missing` passa a ser informação real para EXTERNAL — não mais uma tupla
# vazia por falta de como calcular. Isso bate com os wireframes §3.1/§3.4 do
# documento de layout, que sempre mostraram `externo` com rota verificada.
ESTADOS_COM_ROTA_VERIFICADA = {State.ACTIVE, State.PARTIAL, State.EXTERNAL}

# N1 da revisão seguinte: pertencer a ESTADOS_COM_ROTA_VERIFICADA não basta.
# Um `openfortivpn` EXTERNO recém-aparecido (ainda autenticando, por ex. os
# 10-20s de um 2FA) já é EXTERNAL com `external_pids` preenchido, mas ainda
# não forkou o `pppd` — `status.iface` vem `None`. `missing_networks` com
# `iface=None` devolve TODAS as redes (ver probe.py), o que pareceria
# "Faltando" em tudo — indistinguível de uma verificação real que deu
# negativo. `verificado` (função abaixo) exige as duas coisas: estado certo
# *e* interface resolvida.
def _rota_verificada(status) -> bool:
    # Item 1 (Critical) da rodada de fechamento pré-merge: `status.read_ok`
    # entra na conta pelo mesmo motivo do `iface is not None` acima — mesmo
    # com `state` em ESTADOS_COM_ROTA_VERIFICADA e `iface` resolvido, se a
    # leitura deste ciclo foi degradada (systemctl ou `ip route` falharam),
    # `status.missing` já vem `()` de `status_of` por não ter dado pra
    # calcular — mas sem checar `read_ok` aqui, essa ausência seria lida como
    # "verificado e nada falta", pintando "Roteada" numa rede que não foi
    # checada. Mentira por omissão do tipo oposto ao que motivou este mesmo
    # `if`.
    return (
        status.read_ok
        and status.state in ESTADOS_COM_ROTA_VERIFICADA
        and status.iface is not None
    )


class ProfileRow(Adw.ExpanderRow):
    """Uma linha por perfil. Seção 4.2 do documento de layout."""

    def __init__(self, status, on_action, on_journal, on_edit=None, on_delete=None):
        super().__init__()
        self._on_action = on_action
        self._on_edit = on_edit
        self._on_delete = on_delete
        # Item 3 da rodada de fechamento pré-merge: callback pra abrir o
        # diagnóstico do journal (só usado quando `status.state is FAILED`
        # constrói a `JournalRow` — ver `_reconstruir_corpo`).
        self._on_journal = on_journal
        self.set_subtitle_lines(2)
        self.set_show_enable_switch(False)

        self._icone = Gtk.Image(pixel_size=16)
        self._icone.set_accessible_role(Gtk.AccessibleRole.PRESENTATION)
        self.add_prefix(self._icone)

        self._rotulo = Gtk.Label(xalign=1.0, single_line_mode=True)
        self._pilha = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE,
                                hhomogeneous=False)
        self._botao = Gtk.Button()
        self._botao.connect("clicked", self._clicou)
        self._pilha.add_named(self._botao, "idle")

        ocupado = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._spinner = Gtk.Spinner()
        self._texto_ocupado = Gtk.Label()
        ocupado.append(self._spinner)
        ocupado.append(self._texto_ocupado)
        self._pilha.add_named(ocupado, "busy")
        self._pilha.add_named(Gtk.Box(), "none")

        # Editar e remover ficam fora da `_pilha`: a pilha troca para
        # "busy" durante uma ação, e esconder o acesso à configuração
        # junto seria efeito colateral sem motivo. O que os desabilita é
        # o estado do perfil, não haver ação em curso.
        self._editar = Gtk.Button(icon_name="document-edit-symbolic",
                                  tooltip_text="Editar perfil",
                                  css_classes=["flat"])
        self._editar.update_property([Gtk.AccessibleProperty.LABEL], ["Editar perfil"])
        self._editar.connect("clicked", self._clicou_editar)
        self._remover = Gtk.Button(icon_name="user-trash-symbolic",
                                   tooltip_text="Remover perfil",
                                   css_classes=["flat"])
        self._remover.update_property([Gtk.AccessibleProperty.LABEL], ["Remover perfil"])
        self._remover.connect("clicked", self._clicou_remover)

        sufixo = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
                         valign=Gtk.Align.CENTER)
        sufixo.append(self._rotulo)
        sufixo.append(self._pilha)
        sufixo.append(self._editar)
        sufixo.append(self._remover)
        self.add_suffix(sufixo)

        self._linhas_corpo = []
        # Important 3 da revisão seguinte ao Crítico B: precisa sobreviver a
        # um `update()` chamado NO MEIO de uma ação em andamento — ver
        # `set_busy`/`clear_busy`.
        self._ocupado = False
        self.update(status)

    def _clicou(self, _botao):
        if self._ocupado:
            return
        # Item 1 (Critical) da rodada de fechamento pré-merge: defesa em
        # profundidade — o botão já fica escondido (ver `update`) quando
        # `read_ok` é `False`, mas um clique em trânsito entre um `update()`
        # que ainda mostrava o botão e outro que já o escondeu não pode
        # disparar a ação mesmo assim.
        if not self._status.read_ok:
            return
        _, acao = BOTAO_PRINCIPAL[self._status.state]
        if acao:
            self._on_action(self._status, acao)

    def _clicou_editar(self, _botao):
        if self._on_edit is not None:
            self._on_edit(self._status)

    def _clicou_remover(self, _botao):
        if self._on_delete is not None:
            self._on_delete(self._status)

    def update(self, status):
        self._status = status
        icone, classe, rotulo = APRESENTACAO[status.state]

        self.set_title(GLib.markup_escape_text(status.profile.name))
        self.set_subtitle(GLib.markup_escape_text(status.profile.purpose))
        # Antes esta linha ficava insensível em `nao_configurado`, quando
        # não havia o que fazer a respeito. Agora há: é exatamente o
        # estado em que "Editar" resolve o problema (perfil no catálogo
        # sem .conf), então a linha precisa continuar clicável.
        self.set_sensitive(True)
        # `not status.read_ok` força a expansão mesmo em estados que
        # normalmente ficam recolhidos (`ativo`/`inativo`): é quando a linha
        # de diagnóstico abaixo ("Não foi possível confirmar…") mais precisa
        # aparecer sem exigir um clique extra.
        self.set_expanded(status.state in EXPANDE_SOZINHO or not status.read_ok)

        self._icone.set_from_icon_name(icone)
        self._icone.set_css_classes([classe])
        self._rotulo.set_label(rotulo)
        self._rotulo.set_css_classes([classe])

        # Important 3 da revisão seguinte: `adopt` pode levar até ~165s no
        # pior caso (pkexec 60s + espera 15s + systemctl start 90s). Um
        # refresh disparado nesse meio-tempo (manual, Ctrl+R, ou o refresh
        # automático ao final de OUTRA ação) chama `update()` nesta mesma
        # linha — sem a guarda abaixo, o botão "idle" voltaria antes da ação
        # terminar, e daria pra clicar "Adotar" (ou qualquer botão) duas
        # vezes, dois `pkexec kill` + `systemctl start` concorrentes. Antes a
        # janela para isso era de milissegundos (kill sem espera); agora que
        # `adopt` espera de verdade, a janela ficou grande o bastante para um
        # clique humano.
        if not self._ocupado:
            texto_botao, _ = BOTAO_PRINCIPAL[status.state]
            # Item 1 (Critical) da rodada de fechamento pré-merge: com
            # `read_ok=False` não oferece NENHUMA ação de estado — nem
            # Adotar/Reconectar (destrutivas) nem Conectar/Desconectar. A
            # única forma de agir sobre um perfil com leitura degradada é
            # `Atualizar` (botão global do cabeçalho), até um ciclo bom
            # confirmar o estado de verdade.
            if texto_botao is None or not status.read_ok:
                self._pilha.set_visible_child_name("none")
            else:
                self._botao.set_label(texto_botao)
                self._botao.set_css_classes(
                    ["suggested-action"] if status.state is State.EXTERNAL else []
                )
                self._pilha.set_visible_child_name("idle")

        from .editor_form import pode_editar, pode_remover
        self._editar.set_sensitive(pode_editar(status.state))
        self._remover.set_sensitive(pode_remover(status.state))
        self._editar.set_tooltip_text(
            "Editar perfil" if pode_editar(status.state)
            else "Não dá para editar um perfil que roda fora do systemd"
        )
        self._remover.set_tooltip_text(
            "Remover perfil" if pode_remover(status.state)
            else "Desconecte antes de remover"
        )

        self._reconstruir_corpo(status)

    def set_busy(self, texto: str):
        """Feedback de ação demorada. Spinner SEMPRE acompanhado de texto:
        com animações desligadas o spinner fica parado e vira ícone morto."""
        self._ocupado = True
        self._texto_ocupado.set_label(texto)
        self._spinner.start()
        self._pilha.set_visible_child_name("busy")

    def clear_busy(self):
        """Libera a linha para voltar a refletir o botão idle num próximo
        `update()`. Chamado quando a ação de fato termina (ver
        `VpnWindow._terminou`) — nunca automaticamente por um `update()`."""
        self._ocupado = False
        self._spinner.stop()

    def _reconstruir_corpo(self, status):
        for linha in self._linhas_corpo:
            self.remove(linha)
        self._linhas_corpo = []

        diagnostico = self._frase_de_diagnostico(status)
        if diagnostico:
            # Item 1 (Critical): quando a leitura está degradada, o ícone da
            # linha de diagnóstico não pode vir de `APRESENTACAO[status.state]`
            # — esse estado pode ser, por exemplo, `ativo` (ícone verde,
            # `.success`) mesmo quando a leitura que o produziu falhou (ver
            # `status_of`). Um ícone verde ao lado de "não foi possível
            # confirmar" é o mesmo tipo de sinal misto que a "Não verificada"
            # já evita nas linhas de rede logo abaixo.
            if not status.read_ok:
                icone, classe = "dialog-warning-symbolic", "warning"
            else:
                icone, classe, _ = APRESENTACAO[status.state]
            linha = Adw.ActionRow(title=diagnostico, activatable=False, selectable=False)
            linha.set_title_lines(0)
            img = Gtk.Image(icon_name=icone, pixel_size=16)
            img.set_css_classes([classe])
            linha.add_prefix(img)
            self.add_row(linha)
            self._linhas_corpo.append(linha)

        verificado = _rota_verificada(status)
        for rede in status.profile.networks:
            linha = Adw.ActionRow(title=rede, activatable=False, selectable=False)
            # Seção 4.3: rótulo textual + ícone — nunca só o ícone. O rótulo
            # herda .dim-label (é a palavra "roteada"/"faltando"/"não
            # verificada" que informa, a cor fica no ícone ao lado quando há
            # ícone).
            sufixo = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6,
                             valign=Gtk.Align.CENTER)
            if verificado:
                ausente = rede in status.missing
                sufixo.append(Gtk.Label(label="Faltando" if ausente else "Roteada",
                                        css_classes=["dim-label"]))
                sufixo.append(Gtk.Image(
                    icon_name="network-vpn-no-route-symbolic" if ausente else "emblem-ok-symbolic",
                    css_classes=["warning"] if ausente else ["success"],
                    pixel_size=16,
                ))
            else:
                # Túnel parado, falhou ou não configurado: o app não checou a
                # rota desta rede. Nenhum ícone — dizer "Roteada" ou
                # "Faltando" aqui seria inventar um resultado.
                #
                # Important 1(a) da revisão seguinte: o rótulo NÃO pode ser
                # "Sem rota" — essa é a palavra canônica do estado `parcial`
                # (§2 do doc de layout), que significa "o túnel está no ar e
                # a rota REALMENTE falta". Usar a mesma palavra aqui para
                # "não sei" era uma mentira por omissão: nesta máquina, com
                # as 3 VPNs em EXTERNAL e as 8 redes de fato roteadas, a
                # janela dizia "Sem rota" para todas — um falso negativo pior
                # que o falso positivo original, porque empurra o usuário
                # para Adotar/Reconectar e derrubar um túnel que funcionava.
                sufixo.append(Gtk.Label(label="Não verificada", css_classes=["dim-label"]))
            linha.add_suffix(sufixo)
            self.add_row(linha)
            self._linhas_corpo.append(linha)

        # Item 3 da rodada de fechamento pré-merge: `falhou` era o único
        # estado sem remédio nem explicação na UI — só oferecia "Conectar"
        # (que falharia de novo, pelo mesmo motivo) e nenhum jeito de ver o
        # POR QUÊ sem sair da janela. `journal_tail` (vpnctl.py) já existe,
        # já tem testes, nunca era chamado daqui. Linha ativável, igual à
        # §4.3/§3.7 do documento de layout — abre o diagnóstico num
        # `AdwDialog` via `VpnWindow._abrir_diagnostico`.
        if status.state is State.FAILED:
            linha = Adw.ActionRow(
                title="Ver diagnóstico do systemd", activatable=True, selectable=False,
            )
            seta = Gtk.Image(icon_name="go-next-symbolic")
            seta.set_css_classes(["dim-label"])
            linha.add_suffix(seta)
            linha.connect("activated", lambda _l: self._on_journal(self._status))
            self.add_row(linha)
            self._linhas_corpo.append(linha)

    @staticmethod
    def _frase_de_diagnostico(status) -> str | None:
        # Item 1 (Critical) da rodada de fechamento pré-merge: prioridade
        # máxima. Nenhuma outra frase (mesmo a de `falhou`/`parcial`/`externo`
        # abaixo) pode ser dita com confiança quando a própria leitura que a
        # produziria está degradada.
        if not status.read_ok:
            return ("Não foi possível confirmar o estado desta VPN nesta "
                    "leitura (falha ao consultar o systemd e/ou a tabela de "
                    "rotas). Atualize para tentar de novo antes de agir.")
        if status.state is State.PARTIAL:
            return ("O túnel está no ar, mas falta rota para "
                    + ", ".join(status.missing)
                    + ". Serviços nessas redes não respondem.")
        if status.state is State.EXTERNAL:
            return ("Há um processo openfortivpn deste perfil rodando fora do systemd "
                    f"(PID {', '.join(str(p) for p in status.external_pids)}). "
                    "Conectar agora criaria uma segunda instância.")
        if status.state is State.FAILED:
            from .vpnctl import unit_name
            return (f"A unit {unit_name(status.profile.id)} falhou. "
                    "Veja o log abaixo para o motivo.")
        if status.state is State.UNCONFIGURED:
            return f"/etc/openfortivpn/{status.profile.id}.conf não existe."
        return None


class VpnWindow(Adw.ApplicationWindow):
    def __init__(self, app, perfis):
        super().__init__(application=app, default_width=480, default_height=720)
        self.set_size_request(360, 400)
        self._perfis = perfis
        self._linhas = {}
        self._ultimos_status = []
        # Item 2 (Important) da rodada de fechamento pré-merge: mesma guarda
        # de concorrência que `indicator.py` já tinha (Task 6) — `True`
        # enquanto há uma leitura em voo (thread rodando ou idle_add
        # agendado, ainda sem resposta aplicada). Sem isso, construtor + ⟳
        # clicável à vontade + refresh ao fim de cada ação empilhavam
        # threads, e com `systemctl` lento a resposta MAIS VELHA podia
        # chegar por último, restaurando na tela o estado anterior à ação
        # que o usuário acabou de executar.
        self._refresh_em_andamento = False

        self._toasts = Adw.ToastOverlay()
        self.set_content(self._toasts)

        barra = Adw.ToolbarView()
        self._toasts.set_child(barra)

        self._titulo = Adw.WindowTitle(title="VPN Manager", subtitle="")
        cabecalho = Adw.HeaderBar(title_widget=self._titulo)
        atualizar = Gtk.Button(icon_name="view-refresh-symbolic",
                               tooltip_text="Atualizar estado (Ctrl+R)",
                               action_name="win.refresh")
        atualizar.update_property([Gtk.AccessibleProperty.LABEL], ["Atualizar estado"])
        cabecalho.pack_start(atualizar)

        novo = Gtk.Button(icon_name="list-add-symbolic",
                          tooltip_text="Novo perfil de VPN")
        novo.update_property([Gtk.AccessibleProperty.LABEL], ["Novo perfil de VPN"])
        novo.connect("clicked", lambda *_: self.abrir_editor(None))
        cabecalho.pack_end(novo)

        barra.add_top_bar(cabecalho)

        # Item 5 da rodada de fechamento pré-merge: o tooltip acima promete
        # "Ctrl+R" — antes disso não existia `GAction` nem atalho nenhum por
        # trás da promessa. `win.refresh` é a mesma ação que o botão ⟳ usa
        # (`action_name` acima), então o atalho e o clique fazem exatamente a
        # mesma coisa por construção, não duas cópias de lógica que podem
        # divergir. F5 é o atalho gêmeo já documentado na §4.5 do layout.
        acao_refresh = Gio.SimpleAction.new("refresh", None)
        acao_refresh.connect("activate", lambda *_: self.refresh())
        self.add_action(acao_refresh)
        app.set_accels_for_action("win.refresh", ["<Control>r", "F5"])

        self._banner = Adw.Banner(revealed=False)
        barra.add_top_bar(self._banner)

        pagina = Adw.PreferencesPage()
        self._grupo = Adw.PreferencesGroup()
        pagina.add(self._grupo)
        rolagem = Gtk.ScrolledWindow(
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            child=pagina,
        )
        barra.set_content(rolagem)

        # Item 4 da rodada de fechamento pré-merge: rodapé "Estado lido às
        # HH:MM" (§3.1/§4.1 do documento de layout). Não há refresh
        # periódico (decisão registrada no relatório de fechamento) — uma
        # janela deixada aberta mostra estado de idade arbitrária sem
        # nenhuma pista de que está velho; o rodapé é o jeito barato de
        # nunca deixar isso silencioso. `add_bottom_bar` (em vez de encaixar
        # dentro do `AdwPreferencesPage`, como o wireframe literal desenha)
        # porque `AdwPreferencesPage.add()` só aceita `AdwPreferencesGroup` —
        # e um rodapé fixo, visível independente da posição da rolagem,
        # serve melhor o motivo do próprio rodapé existir.
        self._rodape = Gtk.Label(css_classes=["caption", "dim-label"],
                                 margin_top=6, margin_bottom=6)
        barra.add_bottom_bar(self._rodape)

        self.refresh()

    def refresh(self):
        """Lê o catálogo e o estado de todos os perfis numa thread de trabalho.

        `status_of` chama `systemctl show` e lê rotas (timeout de até 5s cada);
        rodar isso na thread do GLib congelaria a janela inteira. O padrão é o
        mesmo de `_acao`: thread de trabalho produz dados, `GLib.idle_add` aplica
        na UI de volta na thread principal — nenhum widget é tocado fora dela.
        """
        # Item 2 (Important): pula o ciclo se o anterior ainda não voltou —
        # mesmo padrão de `indicator.py` (`_consulta_em_andamento`). O flag é
        # liberado em `_refresh_falhou`/`_aplicar_refresh` (os dois pontos
        # onde o `idle_add` do worker desemboca) e, se agendar o worker
        # falhar ANTES de qualquer thread/idle_add existir, aqui mesmo no
        # `except` — um flag preso em `True` congelaria as atualizações desta
        # janela para sempre, o mesmo bug que a Task 6 já resolveu uma vez do
        # lado do indicador.
        if self._refresh_em_andamento:
            return
        self._refresh_em_andamento = True
        try:
            self._disparar_refresh()
        except Exception:
            self._refresh_em_andamento = False
            raise

    def _disparar_refresh(self):
        from .cli import _catalogo
        from .catalog import CatalogError
        from .vpnctl import status_of

        perfis_atuais = self._perfis

        def trabalho():
            # Correção 3 da re-revisão de fechamento pré-merge: o try/except
            # de agendamento (comentário antigo abaixo) só cobria o `for
            # status_of`, e `_catalogo()` ficava DE FORA dele. `load_catalog`
            # faz `int(_require(c, "porta"))` e `tuple(entry.get("redes"))`
            # sem proteção: uma "porta" não-numérica levanta `ValueError`,
            # umas "redes" não-lista levanta `TypeError` — nenhum dos dois é
            # `CatalogError`, então nenhum dos dois era pego pelo `except
            # CatalogError` logo abaixo. A thread morria ali, sem agendar
            # NENHUM `idle_add`, e o flag `_refresh_em_andamento` ficava preso
            # em `True` para sempre — ⟳, Ctrl+R, F5 e o refresh pós-ação
            # mortos, sem toast, até reiniciar o processo. Isso virou
            # alcançável de verdade a partir do item 6 (releitura do catálogo
            # a cada ciclo) combinado com o README convidando o usuário a
            # editar o TOML sem reiniciar. `try/finally` ENVOLVENDO tudo —
            # igual ao padrão que `indicator.py` já usa em `_disparar_consulta`
            # — garante o `idle_add` (e portanto a liberação do flag) mesmo
            # para uma exceção completamente inesperada, não só para o caso
            # já catalogado (`CatalogError`) ou o já corrigido (erros de
            # `status_of`).
            perfis = perfis_atuais
            resultados = None
            erro = None
            try:
                # Item 6 da rodada de fechamento pré-merge: relê o catálogo a
                # cada atualização — antes a janela lia uma vez em
                # `do_activate` e usava a lista congelada pelo resto da
                # sessão, ao contrário do que o README afirmava. Se o
                # catálogo sumiu ou ficou inválido nesta leitura específica,
                # cai pra última lista boa conhecida em vez de travar o
                # refresh inteiro por causa disso.
                try:
                    perfis = _catalogo()
                except CatalogError:
                    perfis = perfis_atuais
                resultados = [status_of(perfil) for perfil in perfis]
            except Exception as e:
                erro = f"{type(e).__name__}: {e}"
            finally:
                if erro is not None:
                    GLib.idle_add(self._refresh_falhou, erro)
                else:
                    GLib.idle_add(self._aplicar_refresh, perfis, resultados)

        import threading
        threading.Thread(target=trabalho, daemon=True).start()

    def _refresh_falhou(self, mensagem: str):
        self._refresh_em_andamento = False
        self._toasts.add_toast(
            Adw.Toast(title=f"Falha ao atualizar o estado: {mensagem}", timeout=6)
        )
        return GLib.SOURCE_REMOVE

    def _aplicar_refresh(self, perfis, resultados):
        self._refresh_em_andamento = False
        self._perfis = perfis
        # Guardado para que uma ação disparada de fora da linha (salvar
        # com reconexão, remover) encontre o status sem reler tudo.
        self._ultimos_status = list(resultados)

        # Item 6: remove linhas de perfis que saíram do catálogo nesta
        # releitura — sem isso, tirar um `[[profile]]` do TOML deixaria a
        # linha órfã na tela pra sempre (a lista congelada de antes nem
        # tinha esse problema pela razão errada: nunca relia o catálogo).
        ids_atuais = {status.profile.id for status in resultados}
        for pid in [p for p in self._linhas if p not in ids_atuais]:
            self._grupo.remove(self._linhas.pop(pid))

        ativos = externos = 0
        for status in resultados:
            if status.state is State.ACTIVE:
                ativos += 1
            if status.state is State.EXTERNAL:
                externos += 1
            if status.profile.id in self._linhas:
                self._linhas[status.profile.id].update(status)
            else:
                linha = ProfileRow(status, self._acao, self._abrir_diagnostico,
                                   on_edit=self.abrir_editor,
                                   on_delete=self.confirmar_remocao)
                self._linhas[status.profile.id] = linha
                self._grupo.add(linha)

        self._titulo.set_subtitle(f"{ativos} conectada(s)")
        self._banner.set_revealed(externos > 0)
        if externos:
            self._banner.set_title(
                f"{externos} VPN(s) rodando fora do systemd. Use Adotar para assumir o controle."
            )
        self._rodape.set_label(f"Estado lido às {time.strftime('%H:%M')}")
        return GLib.SOURCE_REMOVE

    def _acao(self, status, acao):
        # Seção 3.11 do documento de layout: "Adotar" encerra um túnel que
        # está no ar. Nesta máquina isso mata um processo openfortivpn real
        # em uso — exige confirmação explícita antes de disparar a ação.
        if acao == "adopt":
            self._confirmar_adotar(status)
            return
        self._executar_acao(status, acao)

    def _confirmar_adotar(self, status):
        pids = ", ".join(str(p) for p in status.external_pids)
        dialogo = Adw.AlertDialog(
            heading=f"Adotar a VPN {status.profile.name}?",
            body=(
                f"O processo openfortivpn atual (PID {pids}) será encerrado e o "
                "perfil subirá pela unit do systemd. A conexão cai por alguns "
                "segundos.\n\nNão faça isso durante um atendimento em curso."
            ),
        )
        dialogo.add_response("cancel", "Cancelar")
        dialogo.add_response("adopt", "Adotar")
        # Seção 3.11: "Adotar" usa .suggested-action, não .destructive-action —
        # a interrupção é temporária e o resultado é o estado correto; o peso
        # do aviso está no corpo do texto, não na cor do botão.
        dialogo.set_response_appearance("adopt", Adw.ResponseAppearance.SUGGESTED)
        dialogo.set_default_response("cancel")
        dialogo.set_close_response("cancel")
        dialogo.connect("response", self._respondeu_adotar, status)
        dialogo.present(self)

    def _respondeu_adotar(self, _dialogo, resposta, status):
        if resposta == "adopt":
            self._executar_acao(status, "adopt")

    def _executar_acao(self, status, acao):
        from . import vpnctl
        # Correção 4 da re-revisão de fechamento pré-merge: com a releitura
        # de catálogo (item 6), um perfil pode sair do TOML enquanto o
        # diálogo "Adotar?" está aberto — quando a resposta chega, a linha já
        # foi removida de `self._linhas` em `_aplicar_refresh`, e o índice
        # direto levantava `KeyError`. `_terminou` já usa `.get` pelo mesmo
        # motivo; a ação em si ainda é segura de rodar (opera sobre
        # `status.profile.id`/PIDs capturados no momento do clique, não sobre
        # o catálogo atual) — só não há linha nenhuma pra marcar "ocupada".
        linha = self._linhas.get(status.profile.id)
        rotulos = {"start": "Conectando…", "stop": "Desconectando…",
                   "restart": "Reconectando…", "adopt": "Adotando…"}
        if linha is not None:
            linha.set_busy(rotulos[acao])

        def trabalho():
            # N7 da revisão seguinte ao Crítico B: o `read_bytes` em
            # `_proc_stat_fields` tapou o gatilho CONHECIDO de "trava em
            # Adotando… para sempre" (comm não-UTF-8), mas não a CLASSE —
            # qualquer exceção inesperada aqui nunca chegava ao
            # `GLib.idle_add`, e sem isso `clear_busy()` nunca é chamado.
            # Este try/except mata a categoria inteira, não só o caso já
            # conhecido.
            try:
                if acao == "adopt":
                    r = vpnctl.adopt(status.profile.id, status.external_pids)
                else:
                    r = getattr(vpnctl, acao)(status.profile.id)
            except Exception as e:
                r = vpnctl.ActionResult(
                    False, f"Erro inesperado ao executar a ação: {type(e).__name__}: {e}"
                )
            GLib.idle_add(self._terminou, status.profile.id, r)

        import threading
        threading.Thread(target=trabalho, daemon=True).start()

    def _terminou(self, profile_id, resultado):
        # Important 3: libera a linha ANTES do refresh — senão o `update()`
        # disparado por este mesmo refresh ainda a encontraria ocupada e
        # manteria o botão escondido por mais um ciclo.
        linha = self._linhas.get(profile_id)
        if linha is not None:
            linha.clear_busy()
        if not resultado.ok:
            self._toasts.add_toast(Adw.Toast(title=resultado.message, timeout=6))
        self.refresh()
        return GLib.SOURCE_REMOVE

    def abrir_editor(self, status):
        """Abre o diálogo de perfil. `status=None` cria um perfil novo.

        Editar exige ler o `.conf`, que é 600 — só o helper enxerga. Por isso
        a leitura vai para uma thread: `pkexec` pode ficar parado esperando a
        senha, e isso na thread do GLib congelaria a janela.
        """
        from .editor import EditorPerfil
        from .editor_form import form_vazio

        if status is None:
            EditorPerfil(pai=self, form=form_vazio(), criando=True,
                         ao_salvar=self._depois_de_salvar).present()
            return

        pid = status.profile.id
        estado = status.state

        def trabalho():
            from . import profile_client
            from .editor_form import form_de_leitura

            try:
                resposta = profile_client.read(pid)
                form = form_de_leitura(resposta)
                gerenciado = resposta.get("gerenciado", True)
                erro = None
            except Exception as e:  # noqa: BLE001
                form, gerenciado, erro = None, True, e
            GLib.idle_add(self._abrir_editor_com, form, erro, estado, gerenciado)

        import threading
        threading.Thread(target=trabalho, daemon=True).start()

    def _abrir_editor_com(self, form, erro, estado, gerenciado=True):
        from .editor import EditorPerfil
        from .editor_form import oferecer_reconectar

        if erro is not None:
            self.avisar(f"Não foi possível ler o perfil: {erro}")
            return GLib.SOURCE_REMOVE

        EditorPerfil(
            pai=self,
            form=form,
            criando=False,
            # Item 4.3: com o túnel no ar, salvar reescreve o .conf mas a
            # conexão viva continua com a configuração antiga em memória.
            # Sem reconectar, a edição não tem efeito nenhum.
            reconectar_apos_salvar=oferecer_reconectar(estado),
            # Perfil escrito à mão passa pelo `assume`, não pelo `update`.
            gerenciado=gerenciado,
            ao_salvar=self._depois_de_salvar,
        ).present()
        return GLib.SOURCE_REMOVE

    def _depois_de_salvar(self, pid, reconectar=False):
        """Chamado pelo editor quando o helper aceitou a mudança."""
        self._toasts.add_toast(Adw.Toast(title=f"Perfil {pid} salvo", timeout=3))
        if reconectar:
            status = next(
                (s for s in self._ultimos_status if s.profile.id == pid), None
            )
            if status is not None:
                self._acao(status, "restart")
                return
        self.refresh()

    def pedir_confirmacao_de_assume(self, editor, mensagem: str):
        """O helper achou um script de rotas escrito à mão e quer autorização
        para movê-lo. É a única coisa manual que assumir remove, então a
        pergunta é explícita e nomeia o arquivo."""
        import re

        nomes = re.findall(r"\b\d\d[\w.-]+", mensagem)
        nome = nomes[-1] if nomes else None

        dialogo = Adw.AlertDialog(
            heading="Assumir este perfil?",
            body=(
                f"{mensagem}\n\n"
                "Ele é movido para /var/lib/vpn-manager/undo/ — não é apagado."
                if nome
                else mensagem
            ),
        )
        dialogo.add_response("cancelar", "Cancelar")
        dialogo.add_response("mover", "Mover e assumir")
        dialogo.set_response_appearance("mover", Adw.ResponseAppearance.DESTRUCTIVE)
        dialogo.set_default_response("cancelar")
        dialogo.set_close_response("cancelar")

        def respondeu(_d, resposta):
            if resposta == "mover" and nome:
                editor.confirmar_e_salvar(nome)

        dialogo.connect("response", respondeu)
        dialogo.present(self)

    def avisar(self, mensagem: str):
        """Toast de erro. Usado pelo editor, que não tem acesso ao overlay."""
        self._toasts.add_toast(Adw.Toast(title=mensagem, timeout=6))

    def confirmar_remocao(self, status):
        """Remoção pede o `id` digitado: é a única ação sem volta da interface
        (decisão D5). O snapshot no undo existe, mas não é exposto aqui."""
        from .editor import DialogoRemocao

        DialogoRemocao(pai=self, status=status, ao_remover=self._depois_de_remover).present()

    def _depois_de_remover(self, pid):
        self._toasts.add_toast(Adw.Toast(title=f"Perfil {pid} removido", timeout=3))
        self.refresh()

    def _abrir_diagnostico(self, status):
        """Item 3 da rodada de fechamento pré-merge: chamado pela `JournalRow`
        de um perfil `falhou`. `journal_tail` roda `journalctl` (timeout de
        10s) — mesma disciplina de thread de trabalho + `GLib.idle_add` do
        resto da janela; nada de subprocess na thread do GLib.
        """
        from . import vpnctl

        perfil_id = status.profile.id
        perfil_nome = status.profile.name

        def trabalho():
            # Correção 5 da re-revisão de fechamento pré-merge: sem
            # try/except aqui, uma exceção inesperada de `journal_tail`
            # (que já tem seu próprio try/except para os erros esperados de
            # subprocess, mas não é blindado contra qualquer outra coisa)
            # nunca chegava ao `GLib.idle_add` — o diálogo simplesmente nunca
            # abria, em silêncio, sem toast nem log. Mesmo padrão das
            # correções 3/N7: captura, agenda o `idle_add` de qualquer jeito,
            # e mostra o erro ao usuário em vez de engolir.
            try:
                resultado = vpnctl.journal_tail(perfil_id)
            except Exception as e:
                GLib.idle_add(self._diagnostico_falhou, f"{type(e).__name__}: {e}")
                return
            GLib.idle_add(self._mostrar_diagnostico, perfil_nome, resultado)

        import threading
        threading.Thread(target=trabalho, daemon=True).start()

    def _diagnostico_falhou(self, mensagem: str):
        self._toasts.add_toast(
            Adw.Toast(title=f"Não foi possível abrir o diagnóstico: {mensagem}", timeout=6)
        )
        return GLib.SOURCE_REMOVE

    def _mostrar_diagnostico(self, perfil_nome, resultado):
        # Minor relacionado ao item 3: `journal_tail` agora distingue "log de
        # verdade" (`resultado.ok`) de "falha ao tentar ler o log"
        # (`resultado.text` sendo a mensagem de erro nesse caso) — o texto
        # mostrado no diálogo precisa dizer qual dos dois é, nunca exibir a
        # mensagem de erro formatada como se fosse uma linha do journalctl.
        if resultado.ok:
            texto = resultado.text or "(log vazio)"
        else:
            texto = f"Não foi possível ler o log: {resultado.text}"

        # §4.4 do documento de layout: AdwDialog (1.5) 640×480.
        dialogo = Adw.Dialog(content_width=640, content_height=480)
        barra = Adw.ToolbarView()
        dialogo.set_child(barra)

        cabecalho = Adw.HeaderBar(
            title_widget=Adw.WindowTitle(title=f"Diagnóstico — {perfil_nome}")
        )
        copiar = Gtk.Button(icon_name="edit-copy-symbolic", tooltip_text="Copiar log")
        copiar.update_property([Gtk.AccessibleProperty.LABEL], ["Copiar log"])
        copiar.connect("clicked", self._copiar_diagnostico, texto)
        cabecalho.pack_end(copiar)
        barra.add_top_bar(cabecalho)

        buffer = Gtk.TextBuffer()
        buffer.set_text(texto)
        visor = Gtk.TextView(
            buffer=buffer, editable=False, cursor_visible=False,
            css_classes=["monospace"], top_margin=12, bottom_margin=12,
            left_margin=12, right_margin=12, wrap_mode=Gtk.WrapMode.WORD_CHAR,
        )
        rolagem = Gtk.ScrolledWindow(
            child=visor, vexpand=True,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            hscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
        )
        barra.set_content(rolagem)

        dialogo.present(self)
        return GLib.SOURCE_REMOVE

    def _copiar_diagnostico(self, _botao, texto):
        self.get_clipboard().set(texto)
        self._toasts.add_toast(Adw.Toast(title="Log copiado", timeout=3))


class ErrorWindow(Adw.ApplicationWindow):
    """Catálogo ausente, vazio ou inválido — seção 3.9 do documento de layout.

    `_catalogo()` (`.cli`) levanta `CatalogError` para os três casos; o texto
    específico já vem de dentro da exceção, então aqui só há uma tela genérica
    em vez de três variantes de `AdwStatusPage`.
    """

    def __init__(self, app, mensagem):
        super().__init__(application=app, default_width=480, default_height=720)
        self.set_size_request(360, 400)
        self._app = app

        barra = Adw.ToolbarView()
        self.set_content(barra)
        barra.add_top_bar(Adw.HeaderBar(title_widget=Adw.WindowTitle(title="VPN Manager")))

        self._pagina = Adw.StatusPage(
            icon_name="dialog-error-symbolic",
            title="Catálogo inválido",
            description=GLib.markup_escape_text(mensagem),
        )
        tentar = Gtk.Button(label="Tentar de novo", halign=Gtk.Align.CENTER,
                             css_classes=["suggested-action", "pill"])
        tentar.connect("clicked", self._tentar_de_novo)
        self._pagina.set_child(tentar)
        barra.set_content(self._pagina)

    def _tentar_de_novo(self, _botao):
        from .catalog import CatalogError
        from .cli import _catalogo
        try:
            perfis = _catalogo()
        except CatalogError as erro:
            self._pagina.set_description(GLib.markup_escape_text(str(erro)))
            return
        self.close()
        VpnWindow(self._app, perfis).present()


class VpnApplication(Adw.Application):
    def __init__(self):
        super().__init__(application_id="br.dev.matheus.VpnManager")

    def do_activate(self):
        win = self.props.active_window
        if win is None:
            from .catalog import CatalogError
            from .cli import _catalogo
            try:
                perfis = _catalogo()
            except CatalogError as erro:
                win = ErrorWindow(self, str(erro))
            else:
                win = VpnWindow(self, perfis)
        win.present()


def main():
    return VpnApplication().run(None)


if __name__ == "__main__":
    raise SystemExit(main())
