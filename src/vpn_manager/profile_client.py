"""Lado sem privilégio da configuração de perfis.

Monta o request, chama o helper por `pkexec` e traduz a resposta. Não valida
nada por segurança — a validação que conta é a do helper. O que existe aqui é
conveniência: falhar cedo com mensagem boa antes de acordar o prompt do
polkit.
"""

import json
import subprocess

from .helper_main import SENTINELA_SENHA
from .protocol import VERSAO
from .vpnctl import _mensagem_autorizacao_negada

# Caminho fixo, root:root, fora do $HOME. Apontar para a árvore do projeto
# seria escalada de privilégio: qualquer processo da sessão poderia editar o
# arquivo e ganhar root no próximo salvamento.
HELPER = "/usr/local/libexec/vpn-manager-helper"

# pkexec usa 126 para "não autorizado" e 127 para "não encontrado".
_PKEXEC_NAO_AUTORIZADO = 126
_PKEXEC_NAO_ENCONTRADO = 127


class ClientError(RuntimeError):
    """Falha na operação. `codigo` e `campo` vêm do erro estruturado do
    helper, quando houver, para o formulário marcar o campo certo."""

    def __init__(self, mensagem: str, *, codigo: str | None = None, campo: str | None = None):
        super().__init__(mensagem)
        self.codigo = codigo
        self.campo = campo


def _chamar(payload: dict, run) -> dict:
    corpo = json.dumps(payload, ensure_ascii=False)
    try:
        # A senha vai por stdin. Nunca em argv: /proc/<pid>/cmdline é legível
        # por qualquer usuário da máquina. Nunca em arquivo temporário: ele
        # sobreviveria a um crash.
        r = run(
            ["pkexec", HELPER],
            input=corpo,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except FileNotFoundError as e:
        raise ClientError(
            f"{HELPER} não encontrado — rode ./install.sh para instalar o helper"
        ) from e
    except (OSError, subprocess.SubprocessError) as e:
        raise ClientError(f"falha ao invocar o helper: {e}") from e

    if r.returncode == _PKEXEC_NAO_AUTORIZADO:
        raise ClientError(
            _mensagem_autorizacao_negada(r.stderr or "")
            or "Autorização negada ou prompt cancelado."
        )
    if r.returncode == _PKEXEC_NAO_ENCONTRADO:
        raise ClientError(f"{HELPER} não encontrado — rode ./install.sh")

    try:
        resposta = json.loads(r.stdout)
    except (json.JSONDecodeError, TypeError) as e:
        # O helper morreu antes de responder (OOM, sinal). Propagar
        # JSONDecodeError aqui não ajudaria ninguém.
        detalhe = (r.stderr or r.stdout or "").strip()[:200]
        raise ClientError(
            f"o helper terminou sem resposta válida (código {r.returncode})"
            + (f": {detalhe}" if detalhe else "")
        ) from e

    if not resposta.get("ok"):
        raise ClientError(
            resposta.get("detalhe") or "operação recusada",
            codigo=resposta.get("erro"),
            campo=resposta.get("campo"),
        )
    return resposta


def create(perfil: dict, *, senha: str, run=subprocess.run) -> dict:
    return _chamar(
        {"versao": VERSAO, "op": "create", "perfil": {**perfil, "senha": senha}}, run
    )


def read(pid: str, *, run=subprocess.run) -> dict:
    """Busca os campos gerenciados do .conf — que o app sem privilégio não
    consegue ler, por ser 600. A senha volta como sentinela."""
    return _chamar({"versao": VERSAO, "op": "read", "id": pid}, run)


def update(perfil: dict, *, senha: str | None, run=subprocess.run) -> dict:
    """`senha=None` significa manter a atual: manda a sentinela, e o helper
    reaproveita a que já está no arquivo."""
    return _chamar(
        {
            "versao": VERSAO,
            "op": "update",
            "perfil": {**perfil, "senha": senha if senha is not None else SENTINELA_SENHA},
        },
        run,
    )


def delete(pid: str, *, run=subprocess.run) -> dict:
    return _chamar({"versao": VERSAO, "op": "delete", "id": pid}, run)
