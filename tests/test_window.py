"""Testes de `window.py` que não precisam de GTK real.

O `venv/` de desenvolvimento não tem `gi` de propósito (README: só o
`python3` do sistema tem GTK4/Adw reais — ver `install.sh`). Este arquivo
instala um `gi` FALSO em `sys.modules`, mínimo o bastante para o import de
`vpn_manager.window` não explodir, e o suficiente para exercitar a lógica
pura do worker de refresh (threads, flag de concorrência, `GLib.idle_add`)
sem abrir nenhum widget real. Nenhum teste aqui chama `.present()`/
`Gtk.main()` nem toca em nada visível.

`GLib.idle_add` roda a callback IMEDIATAMENTE e SINCRONAMENTE (não há main
loop GTK nos testes) — é isso que permite provar, do lado de fora, que o
worker chegou até o `idle_add` mesmo no caminho de exceção inesperada.
"""
import sys
import time
import types


def _instalar_gi_falso():
    if "gi" in sys.modules and getattr(sys.modules["gi"], "_falso_vpn_manager", False):
        return  # já instalado por uma importação anterior deste mesmo arquivo
    if "gi" in sys.modules and not getattr(sys.modules["gi"], "_falso_vpn_manager", False):
        return  # `gi` real já carregado (não deveria acontecer neste venv) — não sobrescreve

    class _Base:
        def __init__(self, *a, **k):
            pass

    def _modulo_com_classes(nome_modulo, nomes_classes):
        mod = types.ModuleType(nome_modulo)
        for nome in nomes_classes:
            setattr(mod, nome, type(nome, (_Base,), {}))
        return mod

    Adw = _modulo_com_classes("gi.repository.Adw", [
        "ExpanderRow", "ApplicationWindow", "Application", "Toast",
        "ToastOverlay", "ToolbarView", "WindowTitle", "HeaderBar", "Banner",
        "PreferencesPage", "PreferencesGroup", "AlertDialog", "Dialog",
        "StatusPage",
    ])
    Adw.ResponseAppearance = types.SimpleNamespace(SUGGESTED=1, DESTRUCTIVE=2)

    Gtk = _modulo_com_classes("gi.repository.Gtk", [
        "Image", "Label", "Stack", "Button", "Box", "Spinner",
        "ScrolledWindow", "TextBuffer", "TextView", "Menu",
    ])
    Gtk.Orientation = types.SimpleNamespace(HORIZONTAL=0, VERTICAL=1)
    Gtk.Align = types.SimpleNamespace(CENTER=0)
    Gtk.PolicyType = types.SimpleNamespace(AUTOMATIC=0, NEVER=1)
    Gtk.StackTransitionType = types.SimpleNamespace(CROSSFADE=0)
    Gtk.WrapMode = types.SimpleNamespace(WORD_CHAR=0)
    Gtk.AccessibleRole = types.SimpleNamespace(PRESENTATION=0)
    Gtk.AccessibleProperty = types.SimpleNamespace(LABEL=0)

    Gio = types.ModuleType("gi.repository.Gio")

    class _SimpleAction(_Base):
        def connect(self, *a, **k):
            pass

    Gio.SimpleAction = _SimpleAction
    Gio.SimpleAction.new = staticmethod(lambda *a, **k: _SimpleAction())

    GLib = types.ModuleType("gi.repository.GLib")
    GLib.SOURCE_REMOVE = False
    GLib.SOURCE_CONTINUE = True

    def _idle_add(fn, *args, **kwargs):
        fn(*args)
        return 0

    GLib.idle_add = _idle_add
    GLib.timeout_add = lambda *a, **k: 0
    GLib.markup_escape_text = lambda s: s

    repository = types.ModuleType("gi.repository")
    repository.Adw = Adw
    repository.Gtk = Gtk
    repository.Gio = Gio
    repository.GLib = GLib

    gi = types.ModuleType("gi")
    gi._falso_vpn_manager = True
    gi.require_version = lambda *a, **k: None
    gi.repository = repository

    sys.modules["gi"] = gi
    sys.modules["gi.repository"] = repository
    sys.modules["gi.repository.Adw"] = Adw
    sys.modules["gi.repository.Gtk"] = Gtk
    sys.modules["gi.repository.Gio"] = Gio
    sys.modules["gi.repository.GLib"] = GLib


_instalar_gi_falso()

import vpn_manager.window as window_mod  # noqa: E402


class _ToastsFalsos:
    """Fica no lugar de `Adw.ToastOverlay` — só grava o que foi anunciado."""

    def __init__(self):
        self.toasts = []

    def add_toast(self, toast):
        self.toasts.append(toast)


def _esperar(condicao, timeout=5.0):
    prazo = time.monotonic() + timeout
    while not condicao() and time.monotonic() < prazo:
        time.sleep(0.01)
    return condicao()


# ═══════════════════════════════════════════════════════════════════════════
# Correção 3 da re-revisão de fechamento pré-merge: `_catalogo()` roda FORA
# de qualquer try/except no worker de `refresh()`. `load_catalog` (catalog.py)
# faz `int(_require(c, "porta"))` e `tuple(entry.get("redes"))` sem proteção
# — uma "porta" não-numérica levanta `ValueError`, "redes" não-iterável
# levanta `TypeError`; nenhum dos dois é `CatalogError`. Antes da correção, a
# thread do worker morria ali, sem agendar nenhum `idle_add`, e
# `_refresh_em_andamento` ficava preso em `True` para sempre — ⟳, Ctrl+R, F5
# e o refresh pós-ação mortos, sem toast, até reiniciar o processo inteiro.
# ═══════════════════════════════════════════════════════════════════════════


def test_disparar_refresh_libera_flag_quando_catalogo_levanta_valueerror(monkeypatch):
    win = object.__new__(window_mod.VpnWindow)
    win._perfis = ()
    win._toasts = _ToastsFalsos()
    win._refresh_em_andamento = True  # simula refresh() já ter marcado "em voo"

    def catalogo_explode():
        raise ValueError("porta inválida: '443x'")  # mesmo tipo do bug real (int() falhando)

    monkeypatch.setattr("vpn_manager.cli._catalogo", catalogo_explode)

    win._disparar_refresh()

    assert _esperar(lambda: win._refresh_em_andamento is False), (
        "flag _refresh_em_andamento ficou preso em True — o worker morreu "
        "sem agendar idle_add nenhum"
    )
    assert len(win._toasts.toasts) == 1, "usuário não recebeu nenhum toast de falha"


def test_disparar_refresh_libera_flag_quando_catalogo_levanta_typeerror(monkeypatch):
    """Mesmo vetor, mas com `TypeError` (ex.: `redes` não-iterável no TOML) —
    a re-revisão citou os dois tipos como reprodutíveis."""
    win = object.__new__(window_mod.VpnWindow)
    win._perfis = ()
    win._toasts = _ToastsFalsos()
    win._refresh_em_andamento = True

    def catalogo_explode():
        raise TypeError("'int' object is not iterable")

    monkeypatch.setattr("vpn_manager.cli._catalogo", catalogo_explode)

    win._disparar_refresh()

    assert _esperar(lambda: win._refresh_em_andamento is False)
    assert len(win._toasts.toasts) == 1


def test_disparar_refresh_catalogerror_cai_para_lista_anterior(monkeypatch):
    """Controle negativo: `CatalogError` (o caso já tratado antes da
    correção 3) continua caindo para a última lista boa conhecida via
    `_aplicar_refresh` — não pode virar `_refresh_falhou`. A correção não
    pode ter alargado o `except CatalogError` interno a ponto de tratar o
    caso já esperado como se fosse inesperado."""
    from vpn_manager.catalog import CatalogError

    win = object.__new__(window_mod.VpnWindow)
    win._perfis = ()  # perfis_atuais vazio -> lista de status_of fica vazia, nada a chamar
    win._toasts = _ToastsFalsos()
    win._refresh_em_andamento = True
    win._linhas = {}
    win._grupo = types.SimpleNamespace(remove=lambda *_: None, add=lambda *_: None)
    win._titulo = types.SimpleNamespace(set_subtitle=lambda *_: None)
    win._banner = types.SimpleNamespace(set_revealed=lambda *_: None, set_title=lambda *_: None)
    win._rodape = types.SimpleNamespace(set_label=lambda *_: None)

    def catalogo_explode():
        raise CatalogError("catálogo sumiu")

    monkeypatch.setattr("vpn_manager.cli._catalogo", catalogo_explode)

    win._disparar_refresh()

    assert _esperar(lambda: win._refresh_em_andamento is False)
    assert win._toasts.toasts == []  # nenhum toast de falha — CatalogError não é erro inesperado
