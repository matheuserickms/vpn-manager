"""Estende o `gi` falso de test_window com os widgets que o editor usa.

Existe para que o diálogo possa ser testado sem GTK de verdade. Só cobre o
que o código realmente chama — não é uma emulação do GTK.
"""

import sys
import types

from test_window import _instalar_gi_falso


class _Widget:
    def __init__(self, *a, **k):
        self._props = dict(k)
        self._filhos = []
        self._sinais = {}

    # --- API mínima que o editor usa -------------------------------------
    def add(self, filho):
        self._filhos.append(filho)

    def append(self, filho):
        self._filhos.append(filho)

    def connect(self, sinal, callback, *args):
        self._sinais.setdefault(sinal, []).append((callback, args))

    def emitir(self, sinal, *extra):
        """Só para o teste: dispara o que a interface dispararia."""
        for callback, args in self._sinais.get(sinal, []):
            callback(self, *extra, *args)

    def get_text(self):
        return self._props.get("text", "")

    def set_text(self, valor):
        self._props["text"] = valor

    def set_sensitive(self, valor):
        self._props["sensitive"] = valor

    def get_sensitive(self):
        return self._props.get("sensitive", True)

    def add_css_class(self, nome):
        self._props.setdefault("css", []).append(nome)

    def remove_css_class(self, nome):
        if nome in self._props.get("css", []):
            self._props["css"].remove(nome)

    def set_subtitle(self, valor):
        self._props["subtitle"] = valor

    def set_title(self, valor):
        self._props["title"] = valor

    def set_child(self, filho):
        self._props["child"] = filho

    def add_top_bar(self, filho):
        self._filhos.append(filho)

    def pack_start(self, filho):
        self._filhos.append(filho)

    def pack_end(self, filho):
        self._filhos.append(filho)

    def present(self, *a, **k):
        self._props["apresentado"] = True

    def close(self):
        self._props["fechado"] = True

    def set_content_width(self, v):
        pass

    def set_content_height(self, v):
        pass


def instalar():
    _instalar_gi_falso()
    repo = sys.modules.get("gi.repository")
    if repo is None:  # gi real presente; nada a fazer
        return

    for nome in ("EntryRow", "PasswordEntryRow", "ActionRow", "SwitchRow"):
        if not hasattr(repo.Adw, nome):
            setattr(repo.Adw, nome, type(nome, (_Widget,), {}))

    # As classes do stub original não têm a API acima; substitui as que o
    # editor usa por versões baseadas em _Widget.
    for nome in ("Dialog", "PreferencesPage", "PreferencesGroup", "HeaderBar",
                 "ToolbarView", "Toast", "ToastOverlay", "AlertDialog"):
        setattr(repo.Adw, nome, type(nome, (_Widget,), {}))
    for nome in ("Button", "Label", "Box", "TextView", "TextBuffer",
                 "ScrolledWindow", "Spinner"):
        setattr(repo.Gtk, nome, type(nome, (_Widget,), {}))

    if not isinstance(getattr(repo.GLib, "idle_add", None), types.FunctionType):
        repo.GLib.idle_add = lambda fn, *a, **k: (fn(*a), 0)[1]
