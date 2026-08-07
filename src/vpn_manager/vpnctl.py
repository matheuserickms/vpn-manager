import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .models import Profile, State
from .probe import RouteReadError, missing_networks, read_routes

CONF_DIR = Path("/etc/openfortivpn")
PPP_PID_GLOB = "/run/ppp*.pid"
CGROUP_ROOT = Path("/sys/fs/cgroup")
SYS_CLASS_NET = Path("/sys/class/net")

# Crítico B da auditoria: depois do `kill -TERM`, `adopt` espera os PIDs
# desaparecerem de fato antes de subir a unit — ver `_wait_for_death`. Módulo-
# nível (não default de parâmetro) de propósito: testes fazem
# `monkeypatch.setattr(vpnctl, "ADOPT_KILL_TIMEOUT", ...)` para não depender
# de esperar 15s de verdade.
ADOPT_KILL_TIMEOUT = 15.0
ADOPT_POLL_INTERVAL = 0.2


@dataclass(frozen=True)
class ProfileStatus:
    profile: Profile
    state: State
    iface: str | None
    missing: tuple[str, ...]
    external_pids: tuple[int, ...]
    unit_active: bool
    # Item 1 (Critical) da rodada de fechamento pré-merge: `True` só quando
    # TODAS as leituras deste ciclo (systemctl show, cgroup.procs, ip route)
    # tiveram sucesso. `False` não diz qual falhou nem por quê — de propósito
    # (ver justificativa no relatório de fechamento): o único consumidor que
    # importa (`window.py`) só precisa de uma coisa, "posso confiar no que
    # calculei desta vez", pra decidir se mostra "Não verificada"/suprime
    # ações destrutivas em vez de inventar uma leitura boa a partir de dado
    # ausente. Nunca decida `state` (EXTERNAL/PARTIAL) nem `missing` a partir
    # de um ciclo com `read_ok=False` — ver `status_of`.
    read_ok: bool


def unit_name(profile_id: str) -> str:
    return f"openfortivpn@{profile_id}.service"


def parse_unit_props(text: str) -> dict[str, str]:
    props = {}
    for line in text.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            props[key] = value
    return props


def _proc_comm(pid: int) -> str | None:
    """Nome curto do processo (`/proc/<pid>/comm`), ou `None` se sumiu.

    O kernel preenche esse campo a partir do nome do binário executado (não
    de `argv[0]`) — por isso o mesmo truque de symlink usado em testes para
    simular `openfortivpn` funciona aqui para simular `pppd`."""
    try:
        return Path(f"/proc/{pid}/comm").read_bytes().strip().decode(errors="replace")
    except OSError:
        return None


def _pidfile_confiavel(pid: int, iface_name: str) -> bool:
    """N3 da auditoria: um `/run/pppN.pid` pode ser obsoleto (sobra de um
    SIGKILL ou crash que não chegou a apagá-lo), o PID nele pode ter sido
    reciclado por qualquer outro processo, e o nome `pppN` pode ter sido
    reusado por um túnel diferente depois. Essa é a via de contaminação
    entre perfis. Três checagens baratas antes de confiar no arquivo:
    1. o PID ainda existe e não é zumbi;
    2. o processo dono do PID é de fato um `pppd`;
    3. a interface de rede correspondente ainda existe no kernel.
    """
    if not _proc_alive(pid):
        return False
    if _proc_comm(pid) != "pppd":
        return False
    if not (SYS_CLASS_NET / iface_name).exists():
        return False
    return True


def iface_for_pids(pids: set[int], pid_files: Iterable[Path]) -> str | None:
    """Interface (nome do arquivo `/run/pppN.pid`) cujo PID está em `pids`.

    Devolve `None` — nunca escolhe arbitrariamente — em dois casos:
    - nenhum pid file casar (nada roteado, ou não achado);
    - MAIS DE UM pid file casar (N2 da auditoria). Ambíguo não é exótico:
      acontece com duas instâncias externas do mesmo perfil (o incidente que
      motivou o projeto) ou com a unit ativa e um processo externo
      coexistindo (`resolve_state` prioriza `externo`, mas os PIDs da unit
      continuam no conjunto passado aqui). Escolher o primeiro em ordem
      lexicográfica (`ppp10` antes de `ppp2`) já pintou "Faltando" numa rede
      de fato roteada, só que pela interface errada. Melhor não saber do
      que afirmar errado.

    Cada candidato também passa por `_pidfile_confiavel` (N3) antes de
    contar como casamento.
    """
    casados = []
    for pid_file in sorted(pid_files):
        try:
            pid = int(pid_file.read_text().strip())
        except (OSError, ValueError):
            continue
        if pid not in pids:
            continue
        if not _pidfile_confiavel(pid, pid_file.stem):
            continue
        casados.append(pid_file.stem)
    return casados[0] if len(casados) == 1 else None


def resolve_state(
    profile: Profile,
    conf_exists: bool,
    unit_props: dict[str, str],
    external_pids: tuple[int, ...],
    iface: str | None,
    missing: tuple[str, ...],
) -> State:
    if not conf_exists:
        return State.UNCONFIGURED
    # Externo prevalece: com unit ativa E processo avulso, "Conectar" duplicaria.
    if external_pids:
        return State.EXTERNAL

    active = unit_props.get("ActiveState", "inactive")
    if active == "failed":
        return State.FAILED
    if active in ("activating", "deactivating"):
        return State.CONNECTING
    if active == "active":
        return State.PARTIAL if missing else State.ACTIVE
    return State.INACTIVE


def _unit_pids(control_group: str) -> tuple[set[int], bool]:
    """PIDs no cgroup da unit, e se essa leitura é digna de confiança.

    Item 1 (Critical) da rodada de fechamento, Vetores A/B: `set()` significa
    duas coisas MUITO diferentes, e antes desta função só devolvia uma —
    `control_group` vazio (unit nunca subiu, ou está parada: vazio de
    verdade, `ok=True`) e `cgroup.procs` ilegível com `control_group`
    preenchido (a unit diz que tem um cgroup, mas não deu pra ler quem está
    nele: vazio por FALHA, `ok=False`). O chamador (`status_of`) usa esse
    segundo elemento pra nunca deixar um `set()` por falha alimentar
    `_external_pids` — sem isso, os PIDs DA PRÓPRIA UNIT (que deveriam ter
    sido excluídos) sobram na varredura de `/proc` e viram "externos".
    """
    if not control_group:
        return set(), True
    procs = CGROUP_ROOT / control_group.lstrip("/") / "cgroup.procs"
    try:
        return {int(line) for line in procs.read_text().split()}, True
    except (OSError, ValueError):
        return set(), False


def _cmdline_args(pid: int) -> list[str] | None:
    """Argumentos de `/proc/<pid>/cmdline`, ou None se o processo já sumiu."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    return [a.decode(errors="replace") for a in raw.split(b"\0") if a]


def _is_openfortivpn_for_profile(pid: int, profile_id: str) -> bool:
    """True se `pid` ainda é um processo openfortivpn deste perfil.

    Usado tanto para achar processos externos quanto (Crítico B da auditoria)
    para revalidar PIDs vindos de um refresh anterior antes de matá-los: entre
    a leitura e o clique, o processo pode ter morrido e o PID ter sido
    reciclado por outro processo qualquer — matar como root um PID reciclado
    mata um inocente.
    """
    args = _cmdline_args(pid)
    if not args or "openfortivpn" not in args[0]:
        return False
    pattern = re.compile(rf"{re.escape(str(CONF_DIR))}/{re.escape(profile_id)}\.conf$")
    return any(pattern.search(a) for a in args)


def _external_pids(profile_id: str, unit_pids: set[int]) -> tuple[int, ...]:
    """Processos openfortivpn deste perfil que NÃO estão no cgroup da unit."""
    found = []
    for proc in Path("/proc").glob("[0-9]*"):
        pid = int(proc.name)
        if pid in unit_pids:
            continue
        if _is_openfortivpn_for_profile(pid, profile_id):
            found.append(pid)
    return tuple(sorted(found))


def _proc_stat_fields(pid: int) -> list[bytes] | None:
    """Campos de `/proc/<pid>/stat` a partir do 3º (state, ppid, pgrp, ...).

    Lê em bytes (não `read_text`) pelo mesmo motivo de `_cmdline_args`: um
    `comm` (nome do processo, campo 2) com bytes não-UTF-8 faria `read_text`
    levantar `UnicodeDecodeError` — que não é `OSError` e escaparia dos
    `try/except` de `adopt`, travando a linha em "Adotando…" para sempre (o
    worker de `window.py` não tem try/except ao redor da ação). `comm` pode
    conter espaços e parênteses, então usa o ')' mais à direita para achar o
    fim do campo com segurança.
    """
    try:
        raw = Path(f"/proc/{pid}/stat").read_bytes()
    except OSError:
        return None
    _, _, resto = raw.rpartition(b")")
    if not resto:
        return None
    return resto.split()


def _proc_alive(pid: int) -> bool:
    """True se o PID existe e não é um zumbi.

    Um processo zumbi já terminou de verdade (soltou o túnel SSL, o pppd, as
    rotas) e só aguarda o pai chamar `wait()` — para o propósito de `adopt`
    (saber se subir a unit criaria uma segunda instância competindo por rota),
    zumbi conta como morto. Sem isso, um processo adotado cujo pai (por ex.
    init, ao reparentar um órfão) demore a colher o zumbi pareceria "vivo"
    indefinidamente.
    """
    campos = _proc_stat_fields(pid)
    if not campos:
        return False
    return campos[0] != b"Z"


def _ppid(pid: int) -> int | None:
    """PID do pai, lido de `/proc/<pid>/stat` (campo 4: state, ppid, ...)."""
    campos = _proc_stat_fields(pid)
    if not campos or len(campos) < 2:
        return None
    try:
        return int(campos[1])
    except ValueError:
        return None


def _child_pids(pid: int) -> tuple[int, ...]:
    """PIDs cujo pai é `pid` — ex.: o `pppd` de um `openfortivpn`.

    Crítico B (item 1) da auditoria: quem roda os hooks de
    `/etc/ppp/ip-down.d/` é o `pppd`, filho direto do `openfortivpn`, não o
    próprio `openfortivpn`. `adopt` precisa incluir esses filhos na espera —
    ver uso em `adopt`.
    """
    found = []
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            filho = int(proc.name)
        except ValueError:
            continue
        if _ppid(filho) == pid:
            found.append(filho)
    return tuple(sorted(found))


def _wait_for_death(pids: tuple[int, ...]) -> tuple[int, ...]:
    """Espera os PIDs desaparecerem de `/proc`; devolve os que sobreviverem.

    `kill -TERM` só ENVIA o sinal — não espera o processo terminar. Genérica
    de propósito: não sabe nada sobre openfortivpn/pppd, só espera o conjunto
    de PIDs que o chamador passar. É `adopt` quem decide QUAIS PIDs importam
    — inclusive os filhos `pppd` (ver `_child_pids`).

    Por que esperar o `pppd`, não só o `openfortivpn` pai (checado nesta
    máquina antes de escrever isto — `/etc/ppp/ip-down.d/` só tem o
    `0000usepeerdns`, que restaura `resolv.conf`; não há script aqui que
    remova rota, então "o ip-down tardio apaga a rota que o ip-up novo
    acabou de criar" NÃO é o mecanismo literal desta instalação):
    - o `pppd` velho ainda segura a interface `pppN` (e a rota associada a
      ela) até sair — subir a unit nova enquanto ele existe pode deixar duas
      interfaces `ppp*` vivas para o mesmo túnel ao mesmo tempo;
    - `ip-down.d/0000usepeerdns` restaura o `resolv.conf` de antes; se isso
      rodar DEPOIS do `ip-up` da unit nova já ter posto o DNS certo, o
      `resolv.conf` volta a apontar para o resolver errado;
    - em instalações com outros scripts em `ip-down.d/` (não é o caso
      *desta* máquina, mas o código não pode assumir isso do ambiente de
      quem o roda), a apagada tardia de rota é exatamente o mecanismo
      original.
    Em qualquer um desses casos, o resultado observável é o mesmo que
    motivou o projeto: dois processos do mesmo perfil competindo pelo mesmo
    recurso de rede.

    `ADOPT_KILL_TIMEOUT`/`ADOPT_POLL_INTERVAL` são lidos do módulo (não como
    default de parâmetro) para que os testes consigam encurtá-los via
    monkeypatch sem esperar o timeout de verdade.
    """
    remaining = {p for p in pids if _proc_alive(p)}
    deadline = time.monotonic() + ADOPT_KILL_TIMEOUT
    while remaining and time.monotonic() < deadline:
        time.sleep(ADOPT_POLL_INTERVAL)
        remaining = {p for p in remaining if _proc_alive(p)}
    return tuple(sorted(remaining))


def status_of(profile: Profile, run=subprocess.run) -> ProfileStatus:
    conf_exists = (CONF_DIR / f"{profile.id}.conf").exists()

    try:
        proc = run(
            ["systemctl", "show", unit_name(profile.id),
             "-p", "ActiveState", "-p", "SubState", "-p", "ControlGroup"],
            capture_output=True, text=True, timeout=5,
        )
        systemctl_ok = proc.returncode == 0
        props = parse_unit_props(proc.stdout if systemctl_ok else "")
        # Correção 1 da re-revisão de fechamento pré-merge: `rc == 0` só prova
        # que o COMANDO funcionou, não que aprendemos o cgroup. Uma unit
        # `active` sem `ControlGroup` no `stdout` (visto empiricamente) faz
        # `_unit_pids("")` devolver `ok=True` — a mesma resposta de uma unit
        # parada de verdade — e daí `_external_pids` roda e classifica os
        # PIDs DA PRÓPRIA UNIT como externos. Unit parada continua `ok=True`
        # (nunca perde botões): só exige `ControlGroup` quando `active`.
        if systemctl_ok and props.get("ActiveState") == "active" and not props.get("ControlGroup"):
            systemctl_ok = False
    except (subprocess.SubprocessError, OSError):
        systemctl_ok = False
        props = {}

    unit_pids, cgroup_ok = _unit_pids(props.get("ControlGroup", ""))
    # Item 1 (Critical) da rodada de fechamento pré-merge, Vetores A e B:
    # `unit_pids` vazio por FALHA de leitura (systemctl não respondeu/estourou
    # timeout, ou cgroup.procs ilegível) é indistinguível, olhando só pro
    # valor, de "a unit está mesmo parada" — mas as consequências são
    # opostas. Um vazio de verdade não tem processo nenhum pra reclassificar;
    # um vazio por falha faz `_external_pids` (que só sabe excluir o que está
    # em `unit_pids`) tratar os PIDs DA PRÓPRIA UNIT como externos —
    # `resolve_state` prioriza EXTERNAL, e a tela oferece Adotar (mata o
    # processo) sobre um túnel possivelmente são. `unit_read_ok` é a
    # diferença: só roda a varredura de processos externos quando dá pra
    # confiar que `unit_pids` reflete a realidade; senão, `external` fica
    # vazio por DESCONHECIMENTO, não por afirmação.
    unit_read_ok = systemctl_ok and cgroup_ok
    external = _external_pids(profile.id, unit_pids) if unit_read_ok else ()
    # Important 1(b) da auditoria: um processo externo (fora do systemd)
    # também tem um pppd filho segurando a interface ppp*, exatamente como a
    # unit tem — só que fora do cgroup, então `iface_for_pids` precisa dos
    # PIDs desses filhos além dos da unit para achar a interface de um
    # perfil `externo`. Sem isso, EXTERNAL nunca teria como saber se está
    # roteado — foi exatamente essa incapacidade que motivou o projeto (duas
    # instâncias externas simultâneas do mesmo perfil, nenhuma indicação de
    # qual tinha rota).
    pids_com_pppd_externo = set(unit_pids)
    for pid in external:
        pids_com_pppd_externo.update(_child_pids(pid))
    iface = iface_for_pids(pids_com_pppd_externo, Path("/").glob(PPP_PID_GLOB.lstrip("/")))

    try:
        routes = read_routes(run=run)
        routes_ok = True
    except (RouteReadError, subprocess.SubprocessError, OSError):
        routes = ()
        routes_ok = False

    # `read_ok` combina os dois: só é True se a leitura da unit E das rotas
    # tiverem funcionado neste ciclo. Vetor C (a rodada de fechamento):
    # `read_routes` agora levanta `RouteReadError` em vez de devolver `()`
    # (ver probe.py) justamente para não repetir aqui o mesmo erro do
    # Vetor A/B do lado das rotas.
    read_ok = unit_read_ok and routes_ok
    # `missing` só é calculado quando `read_ok` — não só quando as rotas
    # leram bem. Motivo: `iface` também depende da leitura da unit (via
    # `pids_com_pppd_externo` acima); se a unit falhou, `iface` pode vir
    # `None` só por DESCONHECIMENTO, e `missing_networks` com `iface=None`
    # devolve TODAS as redes (ver probe.py) — isso pintaria "Faltando" em
    # tudo e resolveria PARTIAL mesmo com as rotas lidas com sucesso. Gate
    # único e conservador: sem leitura completa e boa, sem "faltando"
    # nenhum.
    missing = missing_networks(profile.networks, iface, routes) if read_ok else ()
    # Correção 2 da re-revisão de fechamento pré-merge: o gate por interface
    # resolvida precisa valer ANTES de calcular o `state`, não só depois ao
    # publicar o campo. Sem isto, uma falha isolada em resolver `iface` (pid
    # file ilegível, apagado entre o glob e a leitura, ou reprovado por
    # `_pidfile_confiavel`) — que não é rastreada em `read_ok` — faz
    # `missing_networks` devolver TODAS as redes (ver probe.py), e
    # `resolve_state` resolve PARTIAL de verdade a partir disso; só o CAMPO
    # publicado saía vazio depois (gate antigo), deixando a frase "falta rota
    # para " sem nenhuma rede listada enquanto o estado já tinha sido
    # calculado com a mentira.
    if iface is None:
        missing = ()

    state = resolve_state(profile, conf_exists, props, external, iface, missing)
    # N1 da revisão seguinte: `iface is None` faz `missing_networks` devolver
    # TODAS as redes (ver probe.py) — indistinguível de "verificado e
    # realmente ausente". Isso é falso para EXTERNAL em fase de connect (o
    # openfortivpn já apareceu como processo, `external_pids` já está
    # preenchido, mas o `pppd` ainda não forkou — com 2FA isso dura 10-20s).
    # Sem a guarda `iface is not None`, esse intervalo pintaria "Faltando"
    # em tudo e empurraria o usuário a Adotar/Reconectar um túnel que ainda
    # está subindo. Estado verificado exige ESTADO CERTO *e* interface
    # resolvida — as duas condições, não só uma.
    verificado = iface is not None and state in (State.ACTIVE, State.PARTIAL, State.EXTERNAL)
    return ProfileStatus(
        profile=profile,
        state=state,
        iface=iface,
        missing=missing if verificado else (),
        external_pids=external,
        unit_active=props.get("ActiveState") == "active",
        read_ok=read_ok,
    )


@dataclass(frozen=True)
class ActionResult:
    ok: bool
    message: str


_MARCADORES_AUTORIZACAO_NEGADA = (
    "Interactive authentication required",  # systemctl via D-Bus sem polkit
    "Not authorized",  # pkexec, quando o usuário cancela o prompt de senha
)


def _mensagem_autorizacao_negada(stderr: str) -> str | None:
    """Traduz os stderr de autorização negada do systemctl e do pkexec.

    Mesma mensagem amigável nos dois casos — o usuário não precisa saber
    qual dos dois recusou, só que a regra polkit provavelmente não está
    instalada (ou ele cancelou o prompt)."""
    if any(marcador in stderr for marcador in _MARCADORES_AUTORIZACAO_NEGADA):
        return (
            "Autorização negada. A regra polkit "
            "/etc/polkit-1/rules.d/50-openfortivpn.rules está instalada?"
        )
    return None


def _systemctl(verb: str, profile_id: str, run) -> ActionResult:
    try:
        proc = run(
            ["systemctl", verb, unit_name(profile_id)],
            capture_output=True, text=True, timeout=90,
        )
    except (subprocess.SubprocessError, OSError) as e:
        return ActionResult(
            False,
            f"erro ao chamar systemctl: {type(e).__name__}: {e}",
        )
    if proc.returncode == 0:
        return ActionResult(True, "")
    stderr = (proc.stderr or "").strip()
    return ActionResult(
        False,
        _mensagem_autorizacao_negada(stderr) or stderr or f"falha ao executar '{verb}'",
    )


def start(profile_id: str, run=subprocess.run) -> ActionResult:
    return _systemctl("start", profile_id, run)


def stop(profile_id: str, run=subprocess.run) -> ActionResult:
    return _systemctl("stop", profile_id, run)


def restart(profile_id: str, run=subprocess.run) -> ActionResult:
    return _systemctl("restart", profile_id, run)


def _checar_orfao_ainda_vivo(profile_id: str) -> ActionResult | None:
    """Devolve um `ActionResult(False, ...)` se ainda houver (ou houver de
    novo) um openfortivpn deste perfil rodando fora do systemd; `None` se não
    houver nenhum — aí sim é seguro chamar `start`.

    `unit_pids=set()` é proposital: neste ponto de `adopt` a unit ainda não
    foi (re)erguida, então qualquer processo do perfil vivo agora é, por
    definição, avulso — não precisa (nem tem como, sem uma chamada real a
    `systemctl show`) filtrar por cgroup da unit.
    """
    orfaos = _external_pids(profile_id, set())
    if not orfaos:
        return None
    pids_txt = ", ".join(str(p) for p in orfaos)
    return ActionResult(
        False,
        f"Há um processo openfortivpn deste perfil (PID {pids_txt}) rodando fora "
        "do systemd que este Adotar não capturou. Atualize a tela e tente de novo "
        "— subir a unit agora criaria uma duplicata.",
    )


def adopt(profile_id: str, pids: tuple[int, ...], run=subprocess.run) -> ActionResult:
    """Encerra os processos avulsos (rodam como root: exige pkexec) e sobe a unit.

    Proteções contra a duplicata que este comando deveria curar (Crítico B da
    auditoria, itens 1–3, mais os Important 2 da revisão seguinte):
    1. Revalida cada PID antes de matar — pode estar obsoleto desde o último
       refresh e ter sido reciclado por outro processo. (Important 2:) se a
       revalidação zerar `pids` não-vazio — o processo pode ter sido
       reiniciado com PID novo entre o refresh e o clique —, rechecha se
       ainda há um órfão de verdade antes de decidir se é seguro seguir para
       `start`; "fail-open" (seguir pro start só porque os PIDs conhecidos
       morreram) recriaria a duplicata que a revalidação deveria evitar.
    2. Depois do `kill -TERM`, espera os processos desaparecerem de verdade —
       o sinal só é enviado, não aguardado — e ISSO INCLUI os filhos `pppd`
       (Crítico 1 da revisão seguinte): é o `pppd`, não o `openfortivpn` pai,
       quem segura a interface `ppp*` e roda os hooks de `ip-down.d` até
       sair (ver `_wait_for_death` para o que isso significa de verdade
       nesta instalação, não só em tese).
    3. Se algum sobreviver ao timeout, NÃO sobe a unit — regra de ouro: nunca
       subir com um órfão ainda vivo. Depois que a espera confirma a morte,
       rechecha mais uma vez que não surgiu outro órfão nesse meio-tempo
       antes de finalmente chamar `start`.
    """
    alvos = tuple(p for p in pids if _is_openfortivpn_for_profile(p, profile_id))
    if not alvos:
        if pids:
            erro = _checar_orfao_ainda_vivo(profile_id)
            if erro is not None:
                return erro
        return start(profile_id, run=run)

    pppds = tuple(c for pid in alvos for c in _child_pids(pid))
    try:
        proc = run(
            ["pkexec", "kill", "-TERM", *[str(p) for p in alvos]],
            capture_output=True, text=True, timeout=60,
        )
    except (subprocess.SubprocessError, OSError) as e:
        return ActionResult(
            False,
            f"erro ao chamar pkexec: {type(e).__name__}: {e}",
        )
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        return ActionResult(
            False,
            _mensagem_autorizacao_negada(stderr)
            or stderr
            or "não foi possível encerrar o processo avulso",
        )

    sobreviventes = _wait_for_death(alvos + pppds)
    if sobreviventes:
        pids_txt = ", ".join(str(p) for p in sobreviventes)
        return ActionResult(
            False,
            f"O processo (PID {pids_txt}) não encerrou a tempo. "
            "Subir a unit agora criaria uma segunda instância disputando a "
            "mesma rota — tente Adotar de novo ou encerre o processo manualmente.",
        )

    erro = _checar_orfao_ainda_vivo(profile_id)
    if erro is not None:
        return erro
    return start(profile_id, run=run)


@dataclass(frozen=True)
class JournalResult:
    """Item 3 da rodada de fechamento pré-merge: `journal_tail` devolvia uma
    `str` nos dois casos — log de verdade OU a mensagem de erro de quem
    tentou ler o log —, e quem chamava não tinha como saber qual dos dois
    tinha nas mãos antes de mostrar na tela (o `JournalRow` exibiria "Not
    authorized" ou "TimeoutExpired" formatado como se fosse uma linha do
    `journalctl`). `ok` é a distinção explícita que faltava."""
    ok: bool
    text: str


def journal_tail(profile_id: str, lines: int = 20, run=subprocess.run) -> JournalResult:
    try:
        proc = run(
            ["journalctl", "-u", unit_name(profile_id), "-n", str(lines), "--no-pager"],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.SubprocessError, OSError) as e:
        return JournalResult(False, f"erro ao chamar journalctl: {type(e).__name__}: {e}")
    if proc.returncode == 0:
        return JournalResult(True, proc.stdout)
    stderr = (proc.stderr or "").strip()
    return JournalResult(False, stderr or "falha ao recuperar logs (journalctl retornou erro)")
