import os
import subprocess
import sys
import time

from vpn_manager import vpnctl
from vpn_manager.vpnctl import start, stop, restart, adopt, journal_tail, CONF_DIR

# N5 da revisão de 2026-08-07: PIDs "sintéticos" pequenos (4242, 4243, 123 —
# os valores originais aqui) alimentam uma varredura REAL de /proc desde que
# `_checar_orfao_ainda_vivo` (Important 2) existe. Com `pid_max` em 4194304 e
# a máquina rodando há dias, esses números ficam ALCANÇÁVEIS depois que o
# contador de PIDs der a volta — nesse dia o teste falharia reportando um
# "órfão" fantasma (um processo real qualquer que por acaso pegou aquele
# PID). `2**31` é o teto de `pid_t` (int de 32 bits): garantidamente maior
# que qualquer `pid_max` possível, em qualquer configuração.
PID_SINTETICO_A = 2**31 + 101
PID_SINTETICO_B = 2**31 + 102
PID_SINTETICO_C = 2**31 + 103


def _pids_sempre_validos(monkeypatch, pids):
    """Faz a revalidação passar, mas só para os PIDs sintéticos dados.

    Os testes de `adopt` herdados usam PIDs puramente sintéticos (nunca reais
    nesta máquina — ver `PID_SINTETICO_*` acima) que não existem em `/proc`;
    sem isso, a revalidação do Crítico B os descartaria antes mesmo de
    chamar `pkexec`, e os testes deixariam de exercitar o caminho que querem
    testar (kill/timeout/start).

    PID-específico (não um `lambda ...: True` cego) porque, desde o Important
    2/`_checar_orfao_ainda_vivo`, `adopt` também pode escanear TODO o /proc à
    procura de órfãos — um `True` incondicional faria o escaneamento marcar
    toda e qualquer VPN real desta máquina (e qualquer outro processo) como
    "órfão deste perfil" pelo resto do teste.
    """
    validos = set(pids)
    monkeypatch.setattr(vpnctl, "_is_openfortivpn_for_profile",
                         lambda pid, profile_id: pid in validos)


def _mortos_na_primeira_checagem(monkeypatch):
    """Faz `_wait_for_death` ver os PIDs como já mortos, sem polling real."""
    monkeypatch.setattr(vpnctl, "_proc_alive", lambda pid: False)


def _matar_pid_se_vivo(pid: int | None):
    """Limpeza de processo descartável de teste — best-effort e SEGURA.

    N6 da revisão de 2026-08-07: um `os.kill` cego num PID de limpeza é
    exatamente o erro que `adopt()` foi endurecido para nunca cometer contra
    processos reais — um filho de curta duração pode já ter saído e sido
    reciclado por OUTRO processo qualquer antes deste `finally` rodar; matar
    esse outro processo seria o mesmo bug, só que dentro do teste. Por isso
    confere `_proc_alive` antes de matar (reduz a janela, não a elimina por
    completo — mas é a mesma prudência que o próprio código de produção usa).
    `PermissionError` também entra no `except`: um PID reciclado por um
    processo de outro dono (ex.: root) faria `os.kill` explodir sem isso."""
    if pid is None or not vpnctl._proc_alive(pid):
        return
    try:
        os.kill(pid, 9)
        os.waitpid(pid, 0)
    except (ProcessLookupError, ChildProcessError, PermissionError):
        pass


class Recorder:
    """Registra os comandos executados e devolve um retorno programado."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.calls = []
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        rec = self

        class R:
            returncode = rec.returncode
            stdout = rec.stdout
            stderr = rec.stderr
        return R()


def test_start_chama_systemctl_sem_sudo():
    run = Recorder()
    r = start("vpn-exemplo", run=run)
    assert r.ok
    assert run.calls == [["systemctl", "start", "openfortivpn@vpn-exemplo.service"]]
    assert "sudo" not in run.calls[0]


def test_stop_chama_systemctl():
    run = Recorder()
    assert stop("vpn-exemplo", run=run).ok
    assert run.calls == [["systemctl", "stop", "openfortivpn@vpn-exemplo.service"]]


def test_restart_chama_systemctl():
    run = Recorder()
    assert restart("vpn-exemplo", run=run).ok
    assert run.calls == [["systemctl", "restart", "openfortivpn@vpn-exemplo.service"]]


def test_falha_devolve_stderr_na_mensagem():
    run = Recorder(returncode=1, stderr="Access denied")
    r = start("vpn-exemplo", run=run)
    assert r.ok is False
    assert "Access denied" in r.message


def test_polkit_negado_tem_mensagem_explicita():
    run = Recorder(returncode=1, stderr="Interactive authentication required.")
    r = start("vpn-exemplo", run=run)
    assert r.ok is False
    assert "autoriza" in r.message.lower()


def test_adopt_mata_pids_com_pkexec_e_depois_sobe(monkeypatch):
    _pids_sempre_validos(monkeypatch, (PID_SINTETICO_A, PID_SINTETICO_B))
    _mortos_na_primeira_checagem(monkeypatch)
    run = Recorder()
    r = adopt("vpn-teste-fake-adopt-xyz", (PID_SINTETICO_A, PID_SINTETICO_B), run=run)
    assert r.ok
    assert run.calls[0] == ["pkexec", "kill", "-TERM", str(PID_SINTETICO_A), str(PID_SINTETICO_B)]
    assert run.calls[1] == ["systemctl", "start", "openfortivpn@vpn-teste-fake-adopt-xyz.service"]


def test_adopt_nao_sobe_se_kill_falhar(monkeypatch):
    _pids_sempre_validos(monkeypatch, (PID_SINTETICO_A,))
    run = Recorder(returncode=1, stderr="cancelado")
    r = adopt("vpn-teste-fake-adopt-xyz", (PID_SINTETICO_A,), run=run)
    assert r.ok is False
    assert len(run.calls) == 1
    assert run.calls[0][:3] == ["pkexec", "kill", "-TERM"]


def test_journal_tail_devolve_saida():
    run = Recorder(stdout="linha 1\nlinha 2\n")
    r = journal_tail("vpn-exemplo", lines=2, run=run)
    assert r.ok is True
    assert r.text == "linha 1\nlinha 2\n"
    assert run.calls[0][:2] == ["journalctl", "-u"]


def test_adopt_com_pids_vazios():
    run = Recorder()
    r = adopt("vpn-exemplo", (), run=run)
    assert r.ok
    assert len(run.calls) == 1
    assert run.calls[0] == ["systemctl", "start", "openfortivpn@vpn-exemplo.service"]


def test_systemctl_timeout_devolve_actionresult_com_erro():
    import subprocess

    class RaisingRun:
        def __call__(self, cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, 90)

    r = start("vpn-exemplo", run=RaisingRun())
    assert r.ok is False
    assert isinstance(r.message, str)
    assert "TimeoutExpired" in r.message


def test_adopt_pkexec_timeout_nao_sobe(monkeypatch):
    import subprocess

    _pids_sempre_validos(monkeypatch, (PID_SINTETICO_C,))

    class RaisingRun:
        def __init__(self):
            self.call_count = 0

        def __call__(self, cmd, **kwargs):
            self.call_count += 1
            if "pkexec" in cmd:
                raise subprocess.TimeoutExpired(cmd, 60)
            # systemctl shouldn't be called
            raise RuntimeError("systemctl should not be called")

    run = RaisingRun()
    r = adopt("vpn-exemplo", (PID_SINTETICO_C,), run=run)
    assert r.ok is False
    assert "TimeoutExpired" in r.message
    assert run.call_count == 1


def test_journal_tail_com_erro_devolve_string():
    import subprocess

    class RaisingRun:
        def __call__(self, cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, 10)

    result = journal_tail("vpn-exemplo", run=RaisingRun())
    assert result.ok is False
    assert isinstance(result.text, str)
    assert "TimeoutExpired" in result.text


def test_journal_tail_returncode_nonzero_sem_stderr():
    run = Recorder(returncode=1, stderr="")
    result = journal_tail("vpn-exemplo", run=run)
    assert result.ok is False
    assert isinstance(result.text, str)
    assert len(result.text) > 0
    assert "falha" in result.text.lower()


# ═══════════════════════════════════════════════════════════════════════════
# Crítico B da auditoria: adopt() não pode recriar a duplicata que deveria
# curar. Três coisas testadas abaixo com processos reais e descartáveis
# (nunca as VPNs da máquina): espera pela morte de fato, timeout que barra o
# start, e revalidação de PID obsoleto/reciclado antes de matar.
# ═══════════════════════════════════════════════════════════════════════════


def _fake_openfortivpn(tmp_path, profile_id, script):
    """Symlink para o python atual chamado 'openfortivpn', invocado com um
    argumento que referencia o .conf do perfil — o suficiente para
    `_is_openfortivpn_for_profile` (que lê /proc/<pid>/cmdline) validar."""
    symlink = tmp_path / "openfortivpn"
    symlink.symlink_to(sys.executable)
    return subprocess.Popen([str(symlink), "-c", script, f"{CONF_DIR}/{profile_id}.conf"])


IGNORA_TERM_E_DORME = (
    "import signal, time; "
    "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    "time.sleep(5)"
)


def test_proc_alive_trata_zumbi_como_morto():
    """Um processo que já saiu mas cujo pai ainda não colheu (`wait()`) vira
    zumbi — `/proc/<pid>` continua existindo, mas ele não segura mais nenhum
    recurso (sessão SSL, pppd, rotas). Contar zumbi como "vivo" faria
    `_wait_for_death` nunca ver a morte de processos cujo pai real (ex.: init,
    ao reparentar o órfão adotado) demore a colher — bug descoberto ao testar
    esta mesma correção com um processo filho descartável do próprio teste:
    `Popen` + saída imediata cria um zumbi até o pai chamar `wait()`/`poll()`,
    e é crucial checar o estado ANTES de chamar qualquer um dos dois (eles
    colhem o zumbi e o fariam sumir de /proc, mascarando o caso)."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        zumbificou = False
        for _ in range(100):
            # Lê /proc diretamente — nunca proc.poll()/proc.wait() aqui, pois
            # colheriam o zumbi antes da checagem.
            with open(f"/proc/{proc.pid}/stat") as f:
                estado = f.read().rsplit(")", 1)[-1].split()[0]
            if estado == "Z":
                zumbificou = True
                break
            time.sleep(0.02)
        assert zumbificou, "processo não zumbificou a tempo (ambiente incomum?)"
        assert vpnctl._proc_alive(proc.pid) is False
    finally:
        proc.wait(timeout=5)


def test_wait_for_death_detecta_processo_que_morre_sozinho():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.05)"])
    try:
        sobreviventes = vpnctl._wait_for_death((proc.pid,))
    finally:
        proc.wait(timeout=5)
    assert sobreviventes == ()


def test_wait_for_death_timeout_devolve_sobreviventes(monkeypatch):
    """Processo que ignora TERM e nunca some: `_wait_for_death` não pode
    esperar para sempre — respeita ADOPT_KILL_TIMEOUT e devolve quem
    sobreviveu."""
    monkeypatch.setattr(vpnctl, "ADOPT_KILL_TIMEOUT", 0.2)
    monkeypatch.setattr(vpnctl, "ADOPT_POLL_INTERVAL", 0.05)
    proc = subprocess.Popen([sys.executable, "-c", IGNORA_TERM_E_DORME])
    try:
        sobreviventes = vpnctl._wait_for_death((proc.pid,))
        assert sobreviventes == (proc.pid,)
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_adopt_nao_sobe_se_processo_nao_morrer_a_tempo(monkeypatch):
    """Regra de ouro do Crítico B: se o processo não morre dentro do timeout,
    `adopt` NÃO chama `start` — subir agora criaria a duplicata."""
    monkeypatch.setattr(vpnctl, "ADOPT_KILL_TIMEOUT", 0.2)
    monkeypatch.setattr(vpnctl, "ADOPT_POLL_INTERVAL", 0.05)
    proc = subprocess.Popen([sys.executable, "-c", IGNORA_TERM_E_DORME])
    # `vpnctl._proc_alive` (não só `pid == proc.pid`) importa aqui: depois que
    # `proc` morre, `/proc/<pid>` pode persistir como zumbi até o `finally`
    # colher — um monkeypatch ingênuo por pid faria o escaneamento de órfãos
    # do Important 2 (`_checar_orfao_ainda_vivo`) achar esse zumbi e reportar
    # um "órfão" que já morreu de verdade.
    monkeypatch.setattr(vpnctl, "_is_openfortivpn_for_profile",
                         lambda pid, profile_id: pid == proc.pid and vpnctl._proc_alive(pid))
    try:
        run = Recorder()  # pkexec "funciona" (returncode 0), mas o processo real ignora o TERM
        r = adopt("vpn-teste-fake-adopt-xyz", (proc.pid,), run=run)
        assert r.ok is False
        assert "não encerrou" in r.message
        assert str(proc.pid) in r.message
        assert len(run.calls) == 1  # só o kill — systemctl start nunca foi chamado
        assert run.calls[0][:3] == ["pkexec", "kill", "-TERM"]
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_adopt_espera_processo_morrer_de_verdade_antes_de_subir(monkeypatch):
    """Regressão central do Crítico B: `kill -TERM` só ENVIA o sinal, o `run`
    de pkexec retorna antes do processo real sumir. Este processo ignora TERM
    e só sai sozinho ~0.15s depois. Se `adopt` chamasse `start` assim que o
    pkexec retornasse (o bug original), o systemctl start seria disparado com
    o processo real ainda vivo — a janela de duas instâncias coexistindo."""
    monkeypatch.setattr(vpnctl, "ADOPT_POLL_INTERVAL", 0.02)
    script = (
        "import signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "time.sleep(0.15)"
    )
    proc = subprocess.Popen([sys.executable, "-c", script])
    # `vpnctl._proc_alive` (não só `pid == proc.pid`) importa aqui: depois que
    # `proc` morre, `/proc/<pid>` pode persistir como zumbi até o `finally`
    # colher — um monkeypatch ingênuo por pid faria o escaneamento de órfãos
    # do Important 2 (`_checar_orfao_ainda_vivo`) achar esse zumbi e reportar
    # um "órfão" que já morreu de verdade.
    monkeypatch.setattr(vpnctl, "_is_openfortivpn_for_profile",
                         lambda pid, profile_id: pid == proc.pid and vpnctl._proc_alive(pid))

    class RecorderQueObserva:
        def __init__(self):
            self.calls = []
            self.proc_vivo_no_start = None

        def __call__(self, cmd, **kwargs):
            self.calls.append(cmd)
            if cmd[:2] == ["systemctl", "start"]:
                self.proc_vivo_no_start = proc.poll() is None

            class R:
                returncode = 0
                stdout = ""
                stderr = ""
            return R()

    run = RecorderQueObserva()
    try:
        r = adopt("vpn-teste-fake-adopt-xyz", (proc.pid,), run=run)
    finally:
        proc.wait(timeout=5)

    assert r.ok
    assert run.proc_vivo_no_start is False


def test_adopt_ignora_pid_obsoleto_reciclado():
    """PID que já não é mais openfortivpn deste perfil (obsoleto desde o
    último refresh, possivelmente reciclado por outro processo qualquer) é
    ignorado — nunca deve ir para o `kill`. Usa um processo real comum (não
    é openfortivpn) para provar que a revalidação de fato lê
    /proc/<pid>/cmdline, e não um mock que sempre diz sim."""
    outro_processo = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    try:
        run = Recorder()
        r = adopt("vpn-teste-fake-adopt-xyz", (outro_processo.pid,), run=run)
        assert r.ok
        # Nenhum órfão de verdade: sem pkexec, vai direto pro start.
        assert run.calls == [["systemctl", "start", "openfortivpn@vpn-teste-fake-adopt-xyz.service"]]
    finally:
        outro_processo.terminate()
        outro_processo.wait(timeout=5)


def test_adopt_revalida_e_mata_so_o_pid_valido(tmp_path):
    """PIDs mistos: um openfortivpn de verdade deste perfil e um processo
    qualquer que por acaso está no mesmo lote (PID obsoleto). Só o válido
    entra no `pkexec kill`; o outro é ignorado com segurança."""
    valido = _fake_openfortivpn(tmp_path, "vpn-teste-fake-adopt-xyz", IGNORA_TERM_E_DORME)
    invalido = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])

    class RecorderQueMata:
        def __init__(self):
            self.calls = []

        def __call__(self, cmd, **kwargs):
            self.calls.append(cmd)
            if cmd[0] == "pkexec":
                valido.kill()  # SIGKILL simula o processo real terminando

            class R:
                returncode = 0
                stdout = ""
                stderr = ""
            return R()

    run = RecorderQueMata()
    try:
        r = adopt("vpn-teste-fake-adopt-xyz", (valido.pid, invalido.pid), run=run)
        assert r.ok
        assert run.calls[0] == ["pkexec", "kill", "-TERM", str(valido.pid)]
        assert str(invalido.pid) not in run.calls[0]
        assert run.calls[1] == ["systemctl", "start", "openfortivpn@vpn-teste-fake-adopt-xyz.service"]
    finally:
        valido.wait(timeout=5)
        invalido.terminate()
        invalido.wait(timeout=5)


def test_adopt_todos_os_pids_obsoletos_nao_chama_pkexec():
    """Se nenhum PID sobreviver à revalidação, não há órfão a matar — segue
    para o start normalmente, sem tentar pkexec contra PIDs que não existem
    mais como openfortivpn deste perfil.

    PIDs `2**31` e `2**31 + 1` (não `999999`/`999998`): o kernel permite
    configurar `pid_max` até `2**22` (4194304) em sistemas 32-bit de
    `pid_max` clássico, mas 999999 já seria alcançável nesse teto — só acima
    de `2**31` (o limite de `pid_t`, um `int` de 32 bits) é garantido que
    nenhum PID real vai colidir, em qualquer configuração de `pid_max`."""
    run = Recorder()
    r = adopt("vpn-teste-fake-adopt-xyz", (2**31, 2**31 + 1), run=run)
    assert r.ok
    assert len(run.calls) == 1
    assert run.calls[0][0] == "systemctl"


# ═══════════════════════════════════════════════════════════════════════════
# Revisão seguinte (2026-08-07): Critical 1 (espera não cobria o pppd filho),
# Important 2 (fail-open na revalidação podia recriar a duplicata) e Minor 1
# (pkexec cancelado devolvia stderr em inglês sem tradução).
# ═══════════════════════════════════════════════════════════════════════════


def _script_pai_com_filho_real(script_filho: str) -> str:
    """Código para um processo Python que gera um FILHO real (ppid de
    verdade, visível em /proc) e imprime o PID dele — o suficiente para
    simular a relação openfortivpn→pppd sem precisar do binário real."""
    return (
        "import subprocess, sys, time\n"
        f"filho = subprocess.Popen([sys.executable, '-c', {script_filho!r}])\n"
        "print(filho.pid, flush=True)\n"
        "time.sleep(5)\n"
    )


def test_child_pids_acha_filho_real():
    """`_child_pids` (base do Critical 1) precisa achar um filho de verdade
    via ppid — não é um mock, é a relação real de processos do SO."""
    pai = subprocess.Popen(
        [sys.executable, "-c", _script_pai_com_filho_real("import time; time.sleep(5)")],
        stdout=subprocess.PIPE, text=True,
    )
    pid_filho = None
    try:
        pid_filho = int(pai.stdout.readline().strip())
        assert vpnctl._child_pids(pai.pid) == (pid_filho,)
    finally:
        pai.terminate()
        pai.wait(timeout=5)
        # N4 da revisão de 2026-08-07: matar só o pai não basta — o filho
        # (sleep 5) sobrevive como órfão pelo resto da duração dele. `pai`
        # é filho do processo de teste, então fechar o `stdout` (pipe)
        # evita o ResourceWarning de handle não liberado.
        pai.stdout.close()
        _matar_pid_se_vivo(pid_filho)


def test_child_pids_processo_sem_filho_devolve_vazio():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    try:
        assert vpnctl._child_pids(proc.pid) == ()
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_adopt_espera_filho_pppd_alem_do_pai(monkeypatch):
    """Regressão do Critical 1: quem roda os hooks de `ip-down.d` é o `pppd`,
    filho direto do `openfortivpn` — não o próprio `openfortivpn`. A
    docstring antiga de `_wait_for_death` já AFIRMAVA cobrir "encerrando o
    pppd, rodando os scripts de ip-down", mas o código só esperava o pai.

    Este teste tem um processo pai real que gera um filho real (ppid de
    verdade via /proc) que sobrevive ao pai por ~0.2s ignorando TERM — como o
    pppd faria ao terminar os hooks de ip-down. Se `adopt` chamar `start`
    antes do filho sumir (o bug), o observer abaixo pega o filho vivo no
    exato momento do `systemctl start`."""
    filho_script = (
        "import signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "time.sleep(0.2)"
    )
    pai = subprocess.Popen(
        [sys.executable, "-c", _script_pai_com_filho_real(filho_script)],
        stdout=subprocess.PIPE, text=True,
    )
    pid_filho = int(pai.stdout.readline().strip())

    monkeypatch.setattr(vpnctl, "_is_openfortivpn_for_profile",
                         lambda pid, profile_id: pid == pai.pid and vpnctl._proc_alive(pid))
    monkeypatch.setattr(vpnctl, "ADOPT_POLL_INTERVAL", 0.02)

    class RecorderQueObserva:
        def __init__(self):
            self.calls = []
            self.filho_vivo_no_start = None

        def __call__(self, cmd, **kwargs):
            self.calls.append(cmd)
            if cmd[0] == "pkexec":
                pai.terminate()  # o pai morre rápido; o filho (fingindo pppd) não
            if cmd[:2] == ["systemctl", "start"]:
                self.filho_vivo_no_start = vpnctl._proc_alive(pid_filho)

            class R:
                returncode = 0
                stdout = ""
                stderr = ""
            return R()

    run = RecorderQueObserva()
    try:
        r = adopt("vpn-teste-fake-adopt-xyz", (pai.pid,), run=run)
    finally:
        pai.wait(timeout=5)
        pai.stdout.close()
        # N6 da revisão de 2026-08-07: por essa altura o filho de 0,2s quase
        # certamente já morreu sozinho (é literalmente o que este teste
        # verificou) — `_matar_pid_se_vivo` confere antes de mandar o sinal,
        # em vez de um `os.kill` cego que poderia acertar um PID reciclado
        # por outro processo qualquer (o mesmo defeito que `adopt()` foi
        # endurecido para nunca cometer contra processos reais).
        _matar_pid_se_vivo(pid_filho)

    assert r.ok
    assert run.filho_vivo_no_start is False


def test_adopt_fail_open_detecta_orfao_com_pid_novo(tmp_path):
    """Important 2: o PID conhecido (de um refresh antigo) pode ter morrido e
    o processo real ter sido reiniciado com PID NOVO entre o refresh e o
    clique — a revalidação zera `alvos` (PID antigo já não bate com nada) e,
    sem a checagem desta correção, `adopt` seguiria "fail-open" direto pro
    `start`, recriando a exata duplicata que a revalidação deveria evitar.

    Usa um processo real ("PID novo") que a revalidação nunca viu, e um PID
    velho puramente sintético (obsoleto) como argumento — `adopt` precisa
    achar o processo novo pela varredura de órfãos, não pelo argumento."""
    perfil_id = "vpn-teste-fake-adopt-failopen"
    processo_novo = _fake_openfortivpn(tmp_path, perfil_id, IGNORA_TERM_E_DORME)
    pid_obsoleto = 2**31 + 7  # nunca existiu; representa o PID velho do refresh
    try:
        run = Recorder()
        r = adopt(perfil_id, (pid_obsoleto,), run=run)
        assert r.ok is False
        assert str(processo_novo.pid) in r.message
        assert run.calls == []  # nem pkexec nem systemctl foram chamados
    finally:
        processo_novo.kill()
        processo_novo.wait(timeout=5)


def test_adopt_pkexec_cancelado_traduz_mensagem(monkeypatch):
    """Minor 1: pkexec cancelado (usuário recusa o prompt de senha) devolve
    stderr em inglês ("Not authorized") — precisa da mesma tradução amigável
    que o `_systemctl` já aplica para o polkit do systemd."""
    _pids_sempre_validos(monkeypatch, (PID_SINTETICO_A,))
    run = Recorder(
        returncode=127,
        stderr="Error executing command as another user: Not authorized\n",
    )
    r = adopt("vpn-teste-fake-adopt-xyz", (PID_SINTETICO_A,), run=run)
    assert r.ok is False
    assert "autoriza" in r.message.lower()
    assert "Not authorized" not in r.message
