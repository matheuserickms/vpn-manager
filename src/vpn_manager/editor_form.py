"""Lógica do formulário de perfil, sem nenhuma dependência de GTK.

Separado de `editor.py` para poder ser testado direto, sem instalar um `gi`
falso. O diálogo lê e escreve `Form`; tudo que decide alguma coisa mora aqui.

A validação daqui é conveniência: erro imediato no campo certo, antes de
acordar o prompt do polkit. A validação que vale para segurança é a do
helper, que roda como root e não confia neste processo.
"""

from typing import NamedTuple

from .profile_store import (
    ValidationError,
    validate_host,
    validate_id,
    validate_network,
    validate_port,
    validate_text,
)

# Campo de senha vazio na edição significa "manter a que está lá".
_OBRIGATORIOS = (
    ("id", "informe um identificador"),
    ("nome", "informe um nome"),
    ("gateway_host", "informe o endereço do gateway"),
    ("usuario", "informe o usuário"),
)


class Form(NamedTuple):
    """Estado do formulário. Tudo texto, como sai dos campos da interface."""

    id: str = ""
    nome: str = ""
    proposito: str = ""
    gateway_host: str = ""
    gateway_porta: str = "443"
    usuario: str = ""
    senha: str = ""
    trusted_cert: str = ""
    redes_texto: str = ""
    checks_texto: str = ""


def form_vazio() -> Form:
    return Form()


def parse_redes(texto: str) -> list[str]:
    """Uma rede por linha. Linhas em branco são ignoradas, não são erro."""
    return [linha.strip() for linha in texto.splitlines() if linha.strip()]


def parse_checks(texto: str) -> list[dict]:
    """Uma checagem por linha, no formato `host:porta [rótulo]`.

    Levanta ValueError para problema de FORMA (falta a porta, porta não
    numérica). Validação de conteúdo — host plausível, porta na faixa — fica
    com o profile_store.
    """
    checks = []
    for linha in texto.splitlines():
        linha = linha.strip()
        if not linha:
            continue
        alvo, _, rotulo = linha.partition(" ")
        host, sep, porta = alvo.rpartition(":")
        if not sep or not host:
            raise ValueError(f"esperado host:porta em {linha!r}")
        if not porta.isdigit():
            raise ValueError(f"porta não numérica em {linha!r}")
        checks.append(
            {"host": host, "porta": int(porta), "rotulo": rotulo.strip() or alvo}
        )
    return checks


def erros_do_form(form: Form, *, criando: bool) -> dict[str, str]:
    """Devolve {campo: mensagem}. Vazio significa formulário aceitável."""
    erros: dict[str, str] = {}

    for campo, mensagem in _OBRIGATORIOS:
        if not getattr(form, campo).strip():
            erros[campo] = mensagem

    # Mesmas regras do helper — repetidas aqui só para o erro aparecer no
    # campo enquanto o usuário digita, em vez de depois do prompt de senha.
    if "id" not in erros:
        try:
            validate_id(form.id.strip())
        except ValidationError as e:
            erros["id"] = str(e)

    if "gateway_host" not in erros:
        try:
            validate_host(form.gateway_host.strip())
        except ValidationError as e:
            erros["gateway_host"] = str(e)

    for campo in ("nome", "proposito", "usuario"):
        if campo in erros:
            continue
        try:
            validate_text(getattr(form, campo), campo=campo)
        except ValidationError as e:
            erros[campo] = str(e)

    if criando and not form.senha:
        erros["senha"] = "informe a senha"

    try:
        validate_port(form.gateway_porta.strip())
    except ValidationError as e:
        erros["gateway_porta"] = str(e)

    if form.trusted_cert.strip():
        cert = form.trusted_cert.strip()
        if len(cert) != 64 or any(c not in "0123456789abcdefABCDEF" for c in cert):
            erros["trusted_cert"] = "esperado um hash de 64 dígitos hexadecimais"

    for rede in parse_redes(form.redes_texto):
        try:
            validate_network(rede)
        except ValidationError as e:
            erros["redes_texto"] = str(e)
            break

    try:
        parse_checks(form.checks_texto)
    except ValueError as e:
        erros["checks_texto"] = str(e)

    return erros


def perfil_do_form(form: Form) -> dict:
    """Monta o perfil no formato do protocolo.

    Sem a senha: ela viaja como argumento separado, para não passear por
    dicionários que podem acabar copiados, logados ou serializados.
    """
    return {
        "id": form.id.strip(),
        "nome": form.nome.strip(),
        "proposito": form.proposito.strip(),
        "gateway": {
            "host": form.gateway_host.strip(),
            "porta": int(form.gateway_porta.strip() or 443),
        },
        "usuario": form.usuario.strip(),
        "trusted_cert": form.trusted_cert.strip(),
        "redes": parse_redes(form.redes_texto),
        "checks": parse_checks(form.checks_texto),
    }


def senha_para_envio(form: Form, *, criando: bool) -> str | None:
    """`None` quer dizer "mantenha a senha que já está no arquivo"."""
    if criando:
        return form.senha
    return form.senha or None


def form_de_leitura(resposta: dict) -> Form:
    """Preenche o formulário com o que o helper devolveu no verbo `read`."""
    p = resposta["perfil"]
    gateway = p.get("gateway") or {}
    return Form(
        id=p.get("id", ""),
        nome=p.get("nome", ""),
        proposito=p.get("proposito", ""),
        gateway_host=gateway.get("host", ""),
        gateway_porta=str(gateway.get("porta", 443)),
        usuario=p.get("usuario", ""),
        # Nasce vazio, nunca com a sentinela: mostrar "__mantida__" num campo
        # de senha faria o usuário achar que aquilo é a senha e apagá-la sem
        # querer ao digitar por cima.
        senha="",
        trusted_cert=p.get("trusted_cert", ""),
        redes_texto="\n".join(p.get("redes", [])),
        checks_texto="\n".join(
            f"{c['host']}:{c['porta']} {c.get('rotulo', '')}".strip()
            for c in p.get("checks", [])
        ),
    )
