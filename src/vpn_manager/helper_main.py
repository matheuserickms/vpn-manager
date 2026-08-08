"""O único código deste projeto que roda como root.

Recebe um request JSON no stdin, responde JSON no stdout, sai com 0 ou 1.
Não interpreta argumentos de linha de comando e não aceita caminho nenhum
pelo protocolo: tudo que ele escreve é derivado do `id` validado.

Premissa de trabalho: **o chamador é hostil**. Pode ser o app com bug, ou
qualquer processo da sessão que convenceu o polkit. A validação do cliente é
conforto de interface; a que vale é a daqui.
"""

import fcntl
from pathlib import Path

from .catalog import CatalogError, load_catalog
from .profile_store import (
    ApplyError,
    Paths,
    ValidationError,
    apply_profile,
    remove_profile,
    validate_id,
)
from .protocol import (
    ProtocolError,
    parse_request,
    perfil_interno,
    resposta_erro,
    resposta_ok,
)

# Devolvida no lugar da senha pelo verbo `read`, e aceita de volta no `update`
# significando "mantenha a que já está lá". A senha nunca volta ao processo
# sem privilégio.
SENTINELA_SENHA = "__mantida__"

# Diretivas que a interface gera e entende. Qualquer outra linha do .conf é
# preservada verbatim, para não destruir opção posta à mão.
_CHAVES_GERENCIADAS = frozenset(
    {
        "host",
        "port",
        "username",
        "password",
        "trusted-cert",
        "set-routes",
        "set-dns",
        "pppd-ipparam",
    }
)

_MARCADOR = "vpn-manager"


def _parse_conf(texto: str) -> tuple[dict[str, str], list[str], bool]:
    """Separa o .conf em (campos gerenciados, linhas a preservar, gerenciado?).

    Comentários são descartados; linhas com chave desconhecida e linhas soltas
    são preservadas. O snapshot no undo cobre o que se perder aqui.
    """
    campos: dict[str, str] = {}
    preservar: list[str] = []
    gerenciado = False

    for i, linha in enumerate(texto.splitlines()):
        despido = linha.strip()
        if i == 0 and despido.startswith("#") and _MARCADOR in despido:
            gerenciado = True
            continue
        if not despido or despido.startswith("#"):
            continue
        if "=" not in despido:
            preservar.append(linha)
            continue
        chave, _, valor = despido.partition("=")
        chave, valor = chave.strip(), valor.strip()
        if chave in _CHAVES_GERENCIADAS:
            campos[chave] = valor
        else:
            preservar.append(linha)

    return campos, preservar, gerenciado


def _perfil_do_catalogo(paths: Paths, pid: str):
    if not paths.catalog.exists():
        return None
    try:
        for p in load_catalog(paths.catalog):
            if p.id == pid:
                return p
    except CatalogError:
        return None
    return None


def _op_create(req, paths):
    perfil = perfil_interno(req["perfil"])
    pid = validate_id(perfil.get("id"))

    if _perfil_do_catalogo(paths, pid) is not None:
        return 1, resposta_erro("id_em_uso", f"o perfil {pid} já existe", campo="id")
    # Um .conf fora do catálogo é perfil manual. Sobrescrever apagaria a
    # configuração de alguém sem aviso — é erro, não oportunidade.
    if paths.conf(pid).exists():
        return 1, resposta_erro(
            "conf_existe",
            f"{paths.conf(pid)} já existe e não está no catálogo; "
            "use 'assumir gerenciamento' em vez de criar",
            campo="id",
        )

    apply_profile(perfil, senha=req["perfil"].get("senha", ""), paths=paths)
    return 0, resposta_ok({"id": pid})


def _op_read(req, paths):
    pid = validate_id(req.get("id"))
    if not paths.conf(pid).exists():
        return 1, resposta_erro("nao_encontrado", f"{pid} não existe", campo="id")

    campos, preservar, gerenciado = _parse_conf(paths.conf(pid).read_text(encoding="utf-8"))
    do_catalogo = _perfil_do_catalogo(paths, pid)

    perfil = {
        "id": pid,
        "nome": do_catalogo.name if do_catalogo else "",
        "proposito": do_catalogo.purpose if do_catalogo else "",
        "gateway": {"host": campos.get("host", ""), "porta": int(campos.get("port", 443) or 443)},
        "usuario": campos.get("username", ""),
        # A senha NÃO sai daqui. É o motivo de este verbo existir.
        "senha": SENTINELA_SENHA,
        "trusted_cert": campos.get("trusted-cert", ""),
        "redes": list(do_catalogo.networks) if do_catalogo else [],
        "checks": (
            [{"host": c.host, "porta": c.port, "rotulo": c.label} for c in do_catalogo.checks]
            if do_catalogo
            else []
        ),
    }
    return 0, resposta_ok({"perfil": perfil, "preservar": preservar, "gerenciado": gerenciado})


def _op_update(req, paths):
    perfil = perfil_interno(req["perfil"])
    pid = validate_id(perfil.get("id"))

    if not paths.conf(pid).exists():
        return 1, resposta_erro("nao_encontrado", f"{pid} não existe", campo="id")

    campos, preservar, _ = _parse_conf(paths.conf(pid).read_text(encoding="utf-8"))

    senha = req["perfil"].get("senha", SENTINELA_SENHA)
    if senha == SENTINELA_SENHA:
        senha = campos.get("password", "")

    apply_profile(perfil, senha=senha, paths=paths, preservar=preservar)
    return 0, resposta_ok({"id": pid})


def _op_delete(req, paths, unit_ativa):
    pid = validate_id(req.get("id"))

    if not paths.conf(pid).exists():
        return 1, resposta_erro("nao_encontrado", f"{pid} não existe", campo="id")
    # Checado aqui, não no cliente: o estado que a janela mostrou pode estar
    # velho, e apagar o .conf de um túnel no ar deixa a conexão órfã.
    if unit_ativa(pid):
        return 1, resposta_erro(
            "perfil_ativo", f"o perfil {pid} está conectado; desconecte antes de remover"
        )

    remove_profile(pid, paths=paths)
    return 0, resposta_ok({"id": pid})


def _unit_ativa_real(pid: str) -> bool:
    import subprocess

    try:
        r = subprocess.run(
            ["systemctl", "is-active", f"openfortivpn@{pid}.service"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        # Não conseguir verificar não pode virar "pode apagar".
        return True
    return r.stdout.strip() == "active"


def helper_main(stdin, stdout, *, paths: Paths, unit_ativa=None, lock_path: Path | None = None) -> int:
    """Ponto de entrada. Devolve o código de saída do processo."""
    unit_ativa = unit_ativa or _unit_ativa_real
    trava = None
    try:
        if lock_path is not None:
            # Duas janelas salvando ao mesmo tempo não podem intercalar
            # escritas nos mesmos três arquivos.
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            trava = open(lock_path, "w")
            fcntl.flock(trava, fcntl.LOCK_EX)

        req = parse_request(stdin.read())
        op = req["op"]

        if op == "create":
            codigo, corpo = _op_create(req, paths)
        elif op == "read":
            codigo, corpo = _op_read(req, paths)
        elif op == "update":
            codigo, corpo = _op_update(req, paths)
        elif op == "delete":
            codigo, corpo = _op_delete(req, paths, unit_ativa)
        else:  # assume
            codigo, corpo = 1, resposta_erro(
                "nao_implementado", "assumir gerenciamento ainda não está disponível"
            )

    except ProtocolError as e:
        codigo, corpo = 1, resposta_erro("protocolo", str(e))
    except ValidationError as e:
        codigo, corpo = 1, resposta_erro("validacao", str(e))
    except ApplyError as e:
        codigo, corpo = 1, resposta_erro("io", str(e))
    except Exception as e:
        # Nunca deixar traceback sair no stdout: quebraria o parser do cliente
        # e poderia revelar caminho ou conteúdo de arquivo.
        codigo, corpo = 1, resposta_erro("interno", type(e).__name__)
    finally:
        if trava is not None:
            trava.close()

    stdout.write(corpo)
    return codigo
