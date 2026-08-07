"""CLI mínima para validar o núcleo sem abrir GUI.

Uso:
    python3 -m vpn_manager.cli status
    python3 -m vpn_manager.cli check <perfil>
"""
import sys
from pathlib import Path

from .catalog import CatalogError, load_catalog
from .probe import run_check
from .vpnctl import status_of

CATALOGO_LOCAL = Path(__file__).resolve().parents[2] / "data" / "profiles.toml"


def _catalogo():
    for caminho in (Path("/etc/vpn-manager/profiles.toml"), CATALOGO_LOCAL):
        try:
            return load_catalog(caminho)
        except CatalogError:
            continue
    raise CatalogError("catálogo não encontrado")


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if not argv or argv[0] not in ("status", "check"):
        print(__doc__)
        return 2

    try:
        perfis = _catalogo()
    except CatalogError:
        print("catálogo não encontrado", file=sys.stderr)
        return 1

    if argv[0] == "status":
        for p in perfis:
            st = status_of(p)
            iface = st.iface or "—"
            faltando = f"  faltando: {', '.join(st.missing)}" if st.missing else ""
            externo = f"  PIDs externos: {st.external_pids}" if st.external_pids else ""
            # Item 1 (Critical) da rodada de fechamento pré-merge: a CLI é o
            # caminho de diagnóstico sem sessão gráfica (item 6) — precisa
            # sinalizar leitura degradada com a mesma honestidade que a
            # janela, não só a janela.
            degradado = "  [LEITURA DEGRADADA]" if not st.read_ok else ""
            print(f"{p.name:<16} {st.state:<16} {iface:<6}{faltando}{externo}{degradado}")
        return 0

    alvo = argv[1] if len(argv) > 1 else ""
    for p in perfis:
        if p.id == alvo:
            for c in p.checks:
                r = run_check(c)
                print(f"  {c.label:<16} {c.host}:{c.port:<6} "
                      f"{'ok' if r.ok else 'FALHOU — ' + (r.error or '')}")
            return 0
    print(f"perfil desconhecido: {alvo}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
