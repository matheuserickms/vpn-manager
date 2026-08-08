"""Contrato entre o processo do usuário e o helper privilegiado.

Request JSON no stdin, response JSON no stdout. Este módulo é a primeira
coisa que toca dado vindo de fora do processo root — e ele parte do princípio
de que o chamador é hostil: pode ser qualquer processo da sessão que convenceu
o polkit, ou o próprio app com bug.

Regra geral: **fail-closed**. O que não é explicitamente reconhecido é erro,
nunca ignorado em silêncio.
"""

import json

VERSAO = 1

# O maior request legítimo tem uns poucos KiB. O limite existe para que ler o
# stdin não vire consumo de memória sem teto.
LIMITE_BYTES = 64 * 1024

# Ninguém tem 33 redes num perfil. Teto barato contra abuso.
MAX_ITENS = 32

VERBOS = frozenset({"create", "read", "update", "delete", "assume"})

_CHAVES_TOPO = frozenset({"versao", "op", "perfil", "id", "confirmar"})
_CHAVES_PERFIL = frozenset(
    {
        "id",
        "nome",
        "proposito",
        "gateway",
        "usuario",
        "senha",
        "trusted_cert",
        "redes",
        "checks",
    }
)
_CHAVES_GATEWAY = frozenset({"host", "porta"})
_CHAVES_CHECK = frozenset({"host", "porta", "rotulo"})


class ProtocolError(ValueError):
    """Request malformado. Nunca chega a tocar em disco."""


def _sem_chaves_estranhas(d: dict, permitidas: frozenset, onde: str) -> None:
    extras = set(d) - permitidas
    if extras:
        raise ProtocolError(f"{onde}: campo desconhecido {sorted(extras)!r}")


def parse_request(texto: str) -> dict:
    """Valida a forma do request. Não valida o conteúdo dos campos — isso é
    do profile_store, que roda depois e é a validação de segurança de fato."""
    if len(texto) > LIMITE_BYTES:
        raise ProtocolError(f"request acima de {LIMITE_BYTES} bytes")

    try:
        req = json.loads(texto)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ProtocolError(f"JSON inválido: {e}") from None

    if not isinstance(req, dict):
        raise ProtocolError("request precisa ser um objeto JSON")

    _sem_chaves_estranhas(req, _CHAVES_TOPO, "request")

    # Versão é conferida antes de tudo: uma versão futura pode dar outro
    # significado aos mesmos campos, e interpretá-los com as regras de hoje
    # seria pior que recusar.
    if req.get("versao") != VERSAO:
        raise ProtocolError(
            f"versão não suportada: {req.get('versao')!r} (este helper fala {VERSAO})"
        )

    op = req.get("op")
    if op not in VERBOS:
        raise ProtocolError(f"operação desconhecida: {op!r}")

    perfil = req.get("perfil")
    if perfil is not None:
        if not isinstance(perfil, dict):
            raise ProtocolError("perfil precisa ser um objeto")
        _sem_chaves_estranhas(perfil, _CHAVES_PERFIL, "perfil")

        gateway = perfil.get("gateway")
        if gateway is not None:
            if not isinstance(gateway, dict):
                raise ProtocolError("gateway precisa ser um objeto")
            _sem_chaves_estranhas(gateway, _CHAVES_GATEWAY, "gateway")

        redes = perfil.get("redes", [])
        if not isinstance(redes, list):
            raise ProtocolError("redes precisa ser uma lista")
        if len(redes) > MAX_ITENS:
            raise ProtocolError(f"no máximo {MAX_ITENS} redes por perfil")

        checks = perfil.get("checks", [])
        if not isinstance(checks, list):
            raise ProtocolError("checks precisa ser uma lista")
        if len(checks) > MAX_ITENS:
            raise ProtocolError(f"no máximo {MAX_ITENS} checks por perfil")
        for c in checks:
            if not isinstance(c, dict):
                raise ProtocolError("cada check precisa ser um objeto")
            _sem_chaves_estranhas(c, _CHAVES_CHECK, "check")

    return req


def perfil_interno(perfil: dict) -> dict:
    """Traduz o perfil do protocolo para o formato plano do profile_store.

    A senha NÃO entra no dicionário: ela é argumento separado de
    `apply_profile`. Carregá-la aqui arriscaria acabar serializada no
    catálogo, que é legível por todos.
    """
    gateway = perfil.get("gateway") or {}
    return {
        "id": perfil.get("id"),
        "nome": perfil.get("nome"),
        "proposito": perfil.get("proposito"),
        "gateway_host": gateway.get("host"),
        "gateway_porta": gateway.get("porta"),
        "username": perfil.get("usuario"),
        "trusted_cert": perfil.get("trusted_cert", ""),
        "redes": list(perfil.get("redes", [])),
        "checks": [dict(c) for c in perfil.get("checks", [])],
    }


def resposta_ok(dados: dict | None = None) -> str:
    return json.dumps({"ok": True, **(dados or {})}, ensure_ascii=False)


def resposta_erro(erro: str, detalhe: str, campo: str | None = None) -> str:
    """Erro estruturado: o formulário marca o campo certo em vez de mostrar
    um toast genérico e deixar o usuário caçar o que recusou."""
    corpo = {"ok": False, "erro": erro, "detalhe": detalhe}
    if campo is not None:
        corpo["campo"] = campo
    return json.dumps(corpo, ensure_ascii=False)
