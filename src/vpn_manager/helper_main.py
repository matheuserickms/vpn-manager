"""O único código deste projeto que roda como root.

Recebe um request JSON no stdin, responde JSON no stdout, sai com 0 ou 1.
Não interpreta argumentos de linha de comando e não aceita caminho nenhum
pelo protocolo: tudo que ele escreve é derivado do `id` validado.

Premissa de trabalho: **o chamador é hostil**. Pode ser o app com bug, ou
qualquer processo da sessão que convenceu o polkit. A validação do cliente é
conforto de interface; a que vale é a daqui.
"""

import fcntl
import os
import shutil
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

    campos, preservar, gerenciado = _parse_conf(paths.conf(pid).read_text(encoding="utf-8"))

    # Sem esta checagem, editar um perfil manual gerava o script
    # gerenciado e deixava o antigo no lugar: dois scripts instalando rota
    # para o mesmo túnel. O `create` já recusava caso análogo; o `update`
    # não fazia o equivalente.
    if not gerenciado:
        return 1, resposta_erro(
            "nao_gerenciado",
            f"{pid} foi configurado à mão; use assumir gerenciamento antes de editar "
            "pela interface",
            campo="id",
        )

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


def _op_assume(req, paths, unit_ativa):
    """Adota um .conf escrito à mão, transformando-o em perfil gerenciado.

    Não é `update` disfarçado: aqui o arquivo de origem não foi escrito por
    nós, pode ter diretivas que a interface não conhece, e o script de rotas
    antigo (se houver) precisa sair de cena para não duplicar rota.
    """
    perfil = perfil_interno(req["perfil"])
    pid = validate_id(perfil.get("id"))
    conf = paths.conf(pid)

    if not conf.exists():
        return 1, resposta_erro("nao_encontrado", f"{conf} não existe", campo="id")

    campos, preservar, gerenciado = _parse_conf(conf.read_text(encoding="utf-8"))
    if gerenciado:
        return 1, resposta_erro(
            "ja_gerenciado",
            f"{pid} já é gerenciado pela interface; use editar em vez de assumir",
            campo="id",
        )

    # O ipparam antigo continua na memória do processo vivo. Somado a
    # `persistent`, um redial voltaria com a configuração velha e sem as
    # rotas novas — estado difícil de diagnosticar depois.
    if unit_ativa(pid):
        return 1, resposta_erro(
            "perfil_ativo",
            f"desconecte {pid} antes de assumir o gerenciamento",
        )

    # Decisão D3: o script manual antigo sai de cena, mas só com confirmação
    # nominal — é a única coisa escrita à mão que esta operação remove.
    confirmar = req.get("confirmar")
    antigos = _scripts_manuais(paths, pid, campos.get("pppd-ipparam"))
    if antigos and not confirmar:
        return 1, resposta_erro(
            "confirmacao_necessaria",
            "há script de rotas escrito à mão para este perfil; confirme o nome "
            f"para movê-lo para o histórico: {', '.join(a.name for a in antigos)}",
            campo="confirmar",
        )
    if antigos and confirmar not in {a.name for a in antigos}:
        return 1, resposta_erro(
            "confirmacao_invalida",
            f"o nome informado não corresponde a nenhum script deste perfil",
            campo="confirmar",
        )

    senha = req["perfil"].get("senha", SENTINELA_SENHA)
    if senha == SENTINELA_SENHA:
        # Assumir não pode exigir que o usuário lembre a senha que já está
        # no arquivo.
        senha = campos.get("password", "")

    _mover_para_undo(paths, pid, antigos)
    apply_profile(perfil, senha=senha, paths=paths, preservar=preservar)
    return 0, resposta_ok({"id": pid, "preservadas": len(preservar)})


def _scripts_manuais(paths: Paths, pid: str, ipparam: str | None = None) -> list[Path]:
    """Scripts de ip-up.d que pertencem a este perfil e não são nossos.

    Procura pelo id E pelo `pppd-ipparam` atual do .conf. Num perfil
    escrito à mão os dois costumam divergir — `pppd-ipparam = exemplo`
    para o perfil `vpn-exemplo` — e o script antigo casa pelo ipparam,
    que é o que o pppd passa em $6. Procurar só pelo id não acha o que
    importa.
    """
    if not paths.ip_up_dir.exists():
        return []
    termos = {pid}
    if ipparam:
        termos.add(ipparam)
    achados = []
    for arquivo in sorted(paths.ip_up_dir.iterdir()):
        if not arquivo.is_file() or arquivo.name.startswith("50vpnmgr-"):
            continue
        try:
            texto = arquivo.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Script que não cita nem o id nem o ipparam passa despercebido;
        # não há como adivinhar a intenção dele.
        if any(termo in texto for termo in termos):
            achados.append(arquivo)
    return achados


def _mover_para_undo(paths: Paths, pid: str, arquivos: list[Path]) -> None:
    """Tira os scripts antigos do caminho sem apagá-los.

    Dois scripts injetando rota para o mesmo túnel é exatamente a classe de
    duplicata que este projeto existe para tornar visível.
    """
    if not arquivos:
        return
    destino = paths.undo_dir / f"{pid}-assumido"
    destino.mkdir(parents=True, exist_ok=True)
    os.chmod(paths.undo_dir, 0o700)
    os.chmod(destino, 0o700)
    for arquivo in arquivos:
        shutil.move(str(arquivo), str(destino / arquivo.name))


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

        # O parse do protocolo valida forma, e `perfil` é opcional lá
        # porque `read` e `delete` não usam. Sem esta guarda, os verbos
        # que precisam dele estouravam KeyError e a resposta virava
        # "erro interno" — inútil para quem está do outro lado.
        if op in ("create", "update", "assume") and not req.get("perfil"):
            stdout.write(
                resposta_erro("perfil_ausente", f"a operação {op} exige o campo perfil")
            )
            return 1

        if op == "create":
            codigo, corpo = _op_create(req, paths)
        elif op == "read":
            codigo, corpo = _op_read(req, paths)
        elif op == "update":
            codigo, corpo = _op_update(req, paths)
        elif op == "delete":
            codigo, corpo = _op_delete(req, paths, unit_ativa)
        else:  # assume
            codigo, corpo = _op_assume(req, paths, unit_ativa)

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
