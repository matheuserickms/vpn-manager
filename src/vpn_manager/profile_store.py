"""Validação, serialização e aplicação dos artefatos de um perfil de VPN.

Este módulo é o núcleo compartilhado entre o processo do usuário e o helper
privilegiado. A validação daqui roda nos dois lados: no cliente é conforto de
UX, no helper é a fronteira de segurança — ele não confia em nada que chega
pelo stdin. Por isso nada aqui depende de GTK, de root ou de /etc de verdade.
"""

import ipaddress
import os
import re
import shutil
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path

MAX_TEXTO = 200
MAX_HOST = 253


class ValidationError(ValueError):
    """Dado do usuário recusado antes de tocar em disco."""


# Restrições, uma a uma:
#   - minúsculas, dígitos e hífen: o id vira nome de arquivo (.conf e script
#     de ip-up.d) e nome de instância systemd;
#   - sem ponto: `run-parts` ignora silenciosamente arquivos cujo nome contém
#     ponto, então um id com ponto geraria um script de ip-up.d que nunca roda
#     e um perfil que sobe sem rota — falha silenciosa;
#   - hífen só é aceito porque existe o drop-in que troca %I por %i no template
#     do systemd; sem ele, `vpn-exemplo` viraria o caminho vpn/exemplo.conf;
#   - hífen não pode abrir nem fechar o nome (evita nome que parece opção de
#     linha de comando e arquivo terminado em separador);
#   - 2 a 32 caracteres.
_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,30}[a-z0-9]")


def validate_id(valor: object) -> str:
    """Devolve o id validado ou levanta ValidationError.

    É a validação mais crítica do módulo: o id é o único dado do usuário que
    entra em caminho de arquivo. Todo caminho escrito pelo helper é derivado
    daqui — o protocolo não tem parâmetro de caminho.
    """
    if not isinstance(valor, str):
        raise ValidationError(f"id: esperava texto, veio {type(valor).__name__}")
    if not _ID_RE.fullmatch(valor):
        raise ValidationError(
            f"id inválido: {valor!r} — use 2 a 32 caracteres, apenas minúsculas, "
            "dígitos e hífen, sem começar nem terminar com hífen"
        )
    return valor


def validate_network(valor: object) -> str:
    """Devolve a rede normalizada (ex.: "10.0.0.0/24").

    O retorno é RE-SERIALIZADO a partir de `ip_network`, nunca o texto que o
    usuário digitou. Essa string vai interpolada no script de `ip-up.d`, que
    roda como root a cada conexão: o que não parseia como rede não existe, e
    portanto não há o que injetar.

    `strict=True` recusa bits de host ligados (10.0.0.5/24). É engano comum, e
    adivinhar a intenção produziria uma rota silenciosamente errada.
    """
    if not isinstance(valor, str):
        raise ValidationError(f"rede: esperava texto, veio {type(valor).__name__}")
    try:
        rede = ipaddress.ip_network(valor, strict=True)
    except ValueError as e:
        raise ValidationError(f"rede inválida: {valor!r} — {e}") from None
    return str(rede)


# Hostname RFC 1123: labels de 1 a 63 caracteres separados por ponto, cada um
# começando e terminando com alfanumérico. Rejeita label vazio ("a..b") e
# hífen nas pontas.
_LABEL = r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
_HOSTNAME_RE = re.compile(rf"{_LABEL}(?:\.{_LABEL})*")


def validate_host(valor: object) -> str:
    """Aceita endereço IP (v4 ou v6) ou hostname RFC 1123."""
    if not isinstance(valor, str):
        raise ValidationError(f"host: esperava texto, veio {type(valor).__name__}")
    if not valor or len(valor) > MAX_HOST:
        raise ValidationError(f"host: precisa ter de 1 a {MAX_HOST} caracteres")
    try:
        ipaddress.ip_address(valor)
        return valor
    except ValueError:
        pass
    if not _HOSTNAME_RE.fullmatch(valor):
        raise ValidationError(f"host inválido: {valor!r}")
    return valor


def validate_port(valor: object) -> int:
    """Devolve a porta como int entre 1 e 65535."""
    # bool é subclasse de int: sem esta guarda, True viraria a porta 1.
    if isinstance(valor, bool):
        raise ValidationError("porta: esperava número, veio booleano")
    if isinstance(valor, int):
        porta = valor
    elif isinstance(valor, str) and valor.isdigit():
        porta = int(valor)
    else:
        raise ValidationError(f"porta: esperava número inteiro, veio {valor!r}")
    if not 1 <= porta <= 65535:
        raise ValidationError(f"porta fora da faixa 1-65535: {porta}")
    return porta


def validate_text(valor: object, *, campo: str) -> str:
    """Campo de texto livre (nome, propósito, rótulo, usuário).

    Caracteres de controle são REJEITADOS, não escapados. O `.conf` do
    openfortivpn é linha-a-linha e não tem mecanismo de escape confiável: uma
    quebra de linha num campo livre injetaria uma diretiva. Rejeitar é a única
    defesa que não depende de eu acertar o escape.
    """
    if not isinstance(valor, str):
        raise ValidationError(f"{campo}: esperava texto, veio {type(valor).__name__}")
    if len(valor) > MAX_TEXTO:
        raise ValidationError(f"{campo}: máximo de {MAX_TEXTO} caracteres")
    for ch in valor:
        if unicodedata.category(ch) == "Cc":
            raise ValidationError(
                f"{campo}: caractere de controle não é permitido ({ch!r})"
            )
    return valor


MARCADOR = "# gerado por vpn-manager — edite pela interface, não à mão"

# Diretivas que o app gera e das quais depende. Não são editáveis pela UI:
# probe.py verifica rota na interface do túnel e missing_networks assume rotas
# explícitas, então set-routes = 1 quebraria o modelo de estado inteiro.
_DIRETIVAS_FIXAS = (
    ("set-routes", "0"),
    ("set-dns", "0"),
)


def _campos_validados(perfil: dict) -> dict:
    """Valida o perfil inteiro e devolve os valores normalizados."""
    return {
        "id": validate_id(perfil.get("id")),
        "nome": validate_text(perfil.get("nome"), campo="nome"),
        "proposito": validate_text(perfil.get("proposito"), campo="proposito"),
        "gateway_host": validate_host(perfil.get("gateway_host")),
        "gateway_porta": validate_port(perfil.get("gateway_porta")),
        "username": validate_text(perfil.get("username"), campo="username"),
        "redes": [validate_network(r) for r in perfil.get("redes", [])],
        "checks": [
            {
                "host": validate_host(c.get("host")),
                "porta": validate_port(c.get("porta")),
                "rotulo": validate_text(c.get("rotulo"), campo="rotulo"),
            }
            for c in perfil.get("checks", [])
        ],
    }


def render_conf(perfil: dict, *, senha: str, preservar: list[str] | None = None) -> str:
    """Monta o /etc/openfortivpn/<id>.conf.

    `preservar` recebe linhas de um .conf escrito à mão que a interface não
    conhece: elas são mantidas verbatim, para que assumir o gerenciamento de um
    perfil manual não descarte opções que o usuário pôs de propósito.
    """
    campos = _campos_validados(perfil)
    # A senha não passa por validate_text: ela aceita qualquer byte imprimível,
    # inclusive os que rejeitamos em campo livre. O que ela não pode ter é
    # controle — uma quebra de linha injetaria uma diretiva no arquivo.
    if not isinstance(senha, str):
        raise ValidationError("senha: esperava texto")
    for ch in senha:
        if unicodedata.category(ch) == "Cc":
            raise ValidationError("senha: caractere de controle não é permitido")

    cert = perfil.get("trusted_cert") or ""
    if cert and not re.fullmatch(r"[0-9a-fA-F]{64}", cert):
        raise ValidationError("trusted-cert: esperava 64 dígitos hexadecimais")

    linhas = [MARCADOR, f"# perfil: {campos['id']}", ""]
    linhas.append(f"host = {campos['gateway_host']}")
    linhas.append(f"port = {campos['gateway_porta']}")
    linhas.append(f"username = {campos['username']}")
    linhas.append(f"password = {senha}")
    if cert:
        linhas.append(f"trusted-cert = {cert}")
    linhas.append("")
    for chave, valor in _DIRETIVAS_FIXAS:
        linhas.append(f"{chave} = {valor}")
    linhas.append(f"pppd-ipparam = {campos['id']}")

    if preservar:
        linhas.append("")
        linhas.append("# linhas preservadas do arquivo original")
        linhas.extend(preservar)

    return "\n".join(linhas) + "\n"


def render_ip_up_script(perfil: dict) -> str:
    """Monta o /etc/ppp/ip-up.d/<NN>vpnmgr-<id>.

    O pppd roda todo script deste diretório para QUALQUER conexão ppp da
    máquina, passando o ipparam em $6. Sem o guard, este script instalaria as
    rotas deste perfil na interface de outro túnel.
    """
    campos = _campos_validados(perfil)
    linhas = [
        "#!/bin/sh",
        MARCADOR,
        f"# perfil: {campos['id']}",
        "",
        "# $1 = interface, $6 = ipparam. Só age no túnel deste perfil.",
        f'[ "$6" = "{campos["id"]}" ] || exit 0',
        "",
    ]
    # As redes vão como str(ip_network(...)) — nunca o texto do usuário.
    for rede in campos["redes"]:
        linhas.append(f'ip route replace {rede} dev "$1"')
    linhas.append("")
    linhas.append("exit 0")
    return "\n".join(linhas) + "\n"


def _toml_str(valor: str) -> str:
    """String TOML básica. A stdlib tem leitor (tomllib) mas não escritor.

    Só precisa lidar com `"` e `\\`: caracteres de controle já foram rejeitados
    na validação, então não existe \\n para escapar aqui.
    """
    return '"' + valor.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_toml(perfis: list[dict], comentarios: dict | None = None) -> str:
    """Monta o /etc/vpn-manager/profiles.toml.

    A senha NUNCA entra aqui: o catálogo é legível por todos (644) e a
    credencial existe só no .conf 600.

    `comentarios` vem de `extrair_comentarios` sobre o arquivo atual.
    Reescrever o catálogo inteiro apagaria as anotações de quem o mantém
    à mão — os dados sobreviveriam, o conhecimento não.
    """
    comentarios = comentarios or {}
    inline = comentarios.get("inline", {})
    topo = comentarios.get("topo", [])

    blocos = list(topo)
    if topo:
        blocos.append("")
    blocos += [MARCADOR, ""]
    for perfil in perfis:
        campos = _campos_validados(perfil)
        pid = campos["id"]

        def com(campo, linha):
            """Recoloca o comentário que este campo tinha, se tinha."""
            sufixo = inline.get((pid, campo))
            return f"{linha}        {sufixo}" if sufixo else linha

        blocos.append("[[profile]]")
        blocos.append(com("id", f"id          = {_toml_str(pid)}"))
        blocos.append(com("nome", f"nome        = {_toml_str(campos['nome'])}"))
        blocos.append(
            com("proposito", f"proposito   = {_toml_str(campos['proposito'])}")
        )
        redes = ", ".join(_toml_str(r) for r in campos["redes"])
        blocos.append(com("redes", f"redes       = [{redes}]"))
        if campos["checks"]:
            blocos.append("checks      = [")
            for c in campos["checks"]:
                blocos.append(
                    f"  {{ host = {_toml_str(c['host'])}, "
                    f"porta = {c['porta']}, rotulo = {_toml_str(c['rotulo'])} }},"
                )
            blocos.append("]")
        else:
            blocos.append("checks      = []")
        blocos.append("")
    return "\n".join(blocos)


class ApplyError(RuntimeError):
    """Falha ao gravar os artefatos. O estado em disco foi revertido."""


@dataclass(frozen=True)
class Paths:
    """Todos os caminhos que o módulo escreve.

    Injetável para que o teste rode sem root e sem tocar em /etc de verdade —
    e para que o helper não tenha nenhum caminho vindo do protocolo.
    """

    conf_dir: Path = Path("/etc/openfortivpn")
    ip_up_dir: Path = Path("/etc/ppp/ip-up.d")
    catalog: Path = Path("/etc/vpn-manager/profiles.toml")
    undo_dir: Path = Path("/var/lib/vpn-manager/undo")

    def conf(self, pid: str) -> Path:
        return self.conf_dir / f"{pid}.conf"

    def script(self, pid: str) -> Path:
        # O prefixo numérico define a ordem no run-parts; "vpnmgr" identifica
        # o que é nosso sem depender de ler o conteúdo.
        return self.ip_up_dir / f"50vpnmgr-{pid}"


def _escrever_atomico(destino: Path, conteudo: str, modo: int, replace=os.replace) -> None:
    """Grava via temporário no MESMO diretório e renomeia.

    Mesmo diretório porque `os.replace` só é atômico dentro de um sistema de
    arquivos. O modo é aplicado antes do rename, para o arquivo nunca existir
    no destino com permissão frouxa.
    """
    destino.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=destino.parent, prefix=f".{destino.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(conteudo)
        os.chmod(tmp, modo)
        replace(tmp, destino)
    except Exception:
        # O temporário não pode sobreviver a uma falha: ele tem a senha.
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _snapshot(paths: Paths, pid: str) -> dict[Path, tuple[bytes, int] | None]:
    """Guarda conteúdo E modo dos arquivos que serão tocados.

    `None` significa "não existia" — precisamos distinguir isso de "existia
    vazio" para o rollback saber se apaga ou restaura.

    O modo vai junto porque adivinhá-lo na volta erra: o script de ip-up.d
    precisa ser executável (run-parts ignora o que não é), e restaurá-lo como
    644 faria as rotas pararem de ser instaladas em silêncio.
    """
    estado: dict[Path, tuple[bytes, int] | None] = {}
    for alvo in (paths.conf(pid), paths.script(pid), paths.catalog):
        if alvo.exists():
            estado[alvo] = (alvo.read_bytes(), alvo.stat().st_mode & 0o777)
        else:
            estado[alvo] = None
    return estado


def _restaurar(estado: dict[Path, tuple[bytes, int] | None]) -> None:
    for alvo, guardado in estado.items():
        if guardado is None:
            alvo.unlink(missing_ok=True)
            continue
        conteudo, modo = guardado
        # Mesma escrita atômica do caminho feliz, e pelo mesmo motivo: um
        # `write_bytes` direto ATRAVESSA symlink, então um link plantado no
        # destino faria o rollback escrever no alvo dele.
        _escrever_bytes_atomico(alvo, conteudo, modo)


def _escrever_bytes_atomico(destino: Path, conteudo: bytes, modo: int) -> None:
    destino.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=destino.parent, prefix=f".{destino.name}.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(conteudo)
        os.chmod(tmp, modo)
        # `os.replace` substitui a ENTRADA DE DIRETÓRIO; se o destino for um
        # symlink, o link é trocado pelo arquivo, e o alvo dele fica intacto.
        os.replace(tmp, destino)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _catalogo_atual(paths: Paths) -> list[dict]:
    """Lê o catálogo existente como lista de dicionários no formato do editor."""
    if not paths.catalog.exists():
        return []
    from .catalog import CatalogError, load_catalog

    try:
        perfis = load_catalog(paths.catalog)
    except CatalogError:
        return []
    return [
        {
            "id": p.id,
            "nome": p.name,
            "proposito": p.purpose,
            "redes": list(p.networks),
            "checks": [
                {"host": c.host, "porta": c.port, "rotulo": c.label} for c in p.checks
            ],
            # Campos que só existem no .conf; o catálogo não os guarda. São
            # revalidados na serialização, então precisam de valor plausível.
            "gateway_host": "0.0.0.0",
            "gateway_porta": 443,
            "username": "-",
        }
        for p in perfis
    ]


def _comentarios_atuais(paths: Paths) -> dict:
    """Anotações do catálogo em disco, para sobreviverem à reescrita."""
    if not paths.catalog.exists():
        return {}
    try:
        return extrair_comentarios(paths.catalog.read_text(encoding="utf-8"))
    except OSError:
        # Perder comentário é ruim; falhar o salvamento por causa disso
        # seria pior.
        return {}


def _guardar_no_undo(paths: Paths, pid: str, marca: str) -> None:
    destino = paths.undo_dir / f"{pid}-{marca}"
    destino.mkdir(parents=True, exist_ok=True)
    # O diretório inteiro é 700: os .conf guardados aqui têm a senha em claro.
    os.chmod(paths.undo_dir, 0o700)
    os.chmod(destino, 0o700)
    for origem in (paths.conf(pid), paths.script(pid)):
        if origem.exists():
            copia = destino / origem.name
            shutil.copy2(origem, copia)
            os.chmod(copia, origem.stat().st_mode & 0o777)


def apply_profile(
    perfil: dict,
    *,
    senha: str,
    paths: Paths,
    preservar: list[str] | None = None,
    replace=os.replace,
) -> None:
    """Cria ou atualiza um perfil, escrevendo os três artefatos.

    A ordem é deliberada: `.conf` → script → catálogo. O catálogo é o que
    torna o perfil VISÍVEL para a interface, então ele vai por último — assim
    uma falha no meio deixa no máximo um artefato órfão e invisível, nunca um
    perfil que a interface mostra e que não sobe.

    Toda a validação acontece antes da primeira escrita.
    """
    # Renderizar tudo primeiro: se algum campo for inválido, nada foi tocado.
    conf = render_conf(perfil, senha=senha, preservar=preservar)
    script = render_ip_up_script(perfil)
    pid = validate_id(perfil.get("id"))

    # Editar substitui NO LUGAR; só perfil novo vai para o fim. A ordem do
    # catálogo é como o usuário organizou o arquivo, e a janela a reflete —
    # reordenar por causa de uma edição mexe em algo que ninguém pediu.
    atuais = _catalogo_atual(paths)
    posicao = next((i for i, p in enumerate(atuais) if p["id"] == pid), None)
    if posicao is None:
        lista = atuais + [perfil]
    else:
        lista = list(atuais)
        lista[posicao] = perfil
    catalogo = render_toml(lista, _comentarios_atuais(paths))

    estado = _snapshot(paths, pid)
    try:
        _escrever_atomico(paths.conf(pid), conf, 0o600, replace)
        _escrever_atomico(paths.script(pid), script, 0o755, replace)
        _escrever_atomico(paths.catalog, catalogo, 0o644, replace)
    except Exception as e:
        _restaurar(estado)
        raise ApplyError(f"falha ao gravar o perfil {pid}: {e}") from e


def remove_profile(pid: str, *, paths: Paths, marca: str = "removido", replace=os.replace) -> None:
    """Remove os três artefatos, guardando cópia no undo antes.

    O catálogo é reescrito primeiro aqui: tirar o perfil de vista antes de
    apagar os arquivos evita a janela em que a interface mostra um perfil cujo
    `.conf` já sumiu.
    """
    pid = validate_id(pid)
    estado = _snapshot(paths, pid)
    _guardar_no_undo(paths, pid, marca)

    restantes = [p for p in _catalogo_atual(paths) if p["id"] != pid]
    try:
        _escrever_atomico(
            paths.catalog,
            render_toml(restantes, _comentarios_atuais(paths)),
            0o644,
            replace,
        )
        paths.conf(pid).unlink(missing_ok=True)
        paths.script(pid).unlink(missing_ok=True)
    except Exception as e:
        _restaurar(estado)
        raise ApplyError(f"falha ao remover o perfil {pid}: {e}") from e


def extrair_comentarios(texto: str) -> dict:
    """Levanta os comentários de um profiles.toml existente.

    Devolve `{"topo": [linhas], "inline": {(id, campo): "# ..."}}`.

    Só isso: comentários entre perfis ou depois do último campo não são
    mapeados. É o suficiente para o caso que motivou o item — anotação no
    cabeçalho e explicação no fim da linha de um campo — sem virar um parser
    de TOML completo, que é justamente o que a stdlib não expõe.
    """
    topo: list[str] = []
    inline: dict[tuple[str, str], str] = {}
    perfil_atual: str | None = None
    ainda_no_topo = True

    for linha in texto.splitlines():
        despido = linha.strip()

        if despido.startswith("[[profile]]"):
            ainda_no_topo = False
            perfil_atual = None
            continue

        if ainda_no_topo:
            if despido.startswith("#"):
                # O marcador que nós mesmos escrevemos não é anotação humana;
                # ele é reinserido por render_toml e duplicaria a cada ciclo.
                if despido != MARCADOR:
                    topo.append(despido)
                continue
            if despido:
                ainda_no_topo = False

        if "=" not in despido or despido.startswith("#"):
            continue

        campo, _, resto = despido.partition("=")
        campo = campo.strip()

        # Uma cerquilha dentro de string não abre comentário. Percorre o resto
        # contando aspas para achar a que está de fato fora.
        dentro = False
        corte = None
        anterior = ""
        for i, ch in enumerate(resto):
            if ch == '"' and anterior != "\\":
                dentro = not dentro
            elif ch == "#" and not dentro:
                corte = i
                break
            anterior = ch

        if campo == "id" and not dentro:
            valor = resto[:corte] if corte is not None else resto
            perfil_atual = valor.strip().strip('"')

        if corte is not None and perfil_atual is not None:
            inline[(perfil_atual, campo)] = resto[corte:].strip()

    return {"topo": topo, "inline": inline}
