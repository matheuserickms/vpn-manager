import json
import os
import subprocess
import sys
from pathlib import Path

from vpn_manager import vpnctl
from vpn_manager.models import Profile, State
from vpn_manager.vpnctl import (
    ProfileStatus, unit_name, parse_unit_props, iface_for_pids, resolve_state, status_of,
)

PERFIL = Profile(
    id="vpn-exemplo", name="Rede B", purpose="Servidores",
    networks=("10.0.1.0/24",), checks=(),
)

ATIVA = {"ActiveState": "active", "SubState": "running"}
PARADA = {"ActiveState": "inactive", "SubState": "dead"}
FALHOU = {"ActiveState": "failed", "SubState": "failed"}


def _matar_pid_se_vivo(pid: int | None):
    """Limpeza de processo descartável de teste — best-effort e SEGURA.

    N6 da revisão de 2026-08-07: um `os.kill` cego num PID de limpeza é
    exatamente o erro que `adopt()` foi endurecido para nunca cometer contra
    processos reais — um filho de curta duração pode já ter saído e sido
    reciclado por OUTRO processo antes deste `finally` rodar. Confere
    `_proc_alive` antes de matar, e `PermissionError` entra no `except` (PID
    reciclado por um processo de outro dono faria `os.kill` explodir sem
    isso)."""
    if pid is None or not vpnctl._proc_alive(pid):
        return
    try:
        os.kill(pid, 9)
        os.waitpid(pid, 0)
    except (ProcessLookupError, ChildProcessError, PermissionError):
        pass


def test_unit_name():
    assert unit_name("vpn-exemplo") == "openfortivpn@vpn-exemplo.service"


def test_parse_unit_props():
    saida = (
        "ActiveState=active\n"
        "SubState=running\n"
        "ControlGroup=/system.slice/system-openfortivpn.slice/openfortivpn@vpn-exemplo.service\n"
    )
    props = parse_unit_props(saida)
    assert props["ActiveState"] == "active"
    assert props["ControlGroup"].endswith("openfortivpn@vpn-exemplo.service")


def test_parse_unit_props_valor_com_igual():
    props = parse_unit_props("ExecStart={ path=/usr/bin/openfortivpn ; argv[]=-c a.conf }\n")
    assert props["ExecStart"].startswith("{ path=")


def _pppd_de_verdade(tmp_path, sys_class_net, iface_name):
    """Processo real cujo `/proc/<pid>/comm` é "pppd" (via symlink — o
    kernel preenche esse campo a partir do binário executado, não de
    `argv[0]`) e cuja interface aparece em `SYS_CLASS_NET` — o suficiente
    para passar em `_pidfile_confiavel` (N3 da auditoria)."""
    (sys_class_net / iface_name).mkdir(parents=True, exist_ok=True)
    symlink = tmp_path / "pppd"
    if not symlink.exists():
        symlink.symlink_to(sys.executable)
    return subprocess.Popen([str(symlink), "-c", "import time; time.sleep(5)"])


def test_iface_for_pids_casa_pid_file(tmp_path, monkeypatch):
    sys_class_net = tmp_path / "sys-class-net"
    sys_class_net.mkdir()
    monkeypatch.setattr(vpnctl, "SYS_CLASS_NET", sys_class_net)

    proc = _pppd_de_verdade(tmp_path, sys_class_net, "ppp1")
    try:
        (tmp_path / "ppp0.pid").write_text("111\n")
        (tmp_path / "ppp1.pid").write_text(f"{proc.pid}\n")
        iface = iface_for_pids({proc.pid, 333}, tmp_path.glob("ppp*.pid"))
        assert iface == "ppp1"
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_iface_for_pids_sem_correspondencia(tmp_path):
    (tmp_path / "ppp0.pid").write_text("111\n")
    assert iface_for_pids({999}, tmp_path.glob("ppp*.pid")) is None


def test_iface_for_pids_ignora_arquivo_corrompido(tmp_path, monkeypatch):
    sys_class_net = tmp_path / "sys-class-net"
    sys_class_net.mkdir()
    monkeypatch.setattr(vpnctl, "SYS_CLASS_NET", sys_class_net)

    proc = _pppd_de_verdade(tmp_path, sys_class_net, "ppp1")
    try:
        (tmp_path / "ppp0.pid").write_text("lixo\n")
        (tmp_path / "ppp1.pid").write_text(f"{proc.pid}\n")
        assert iface_for_pids({proc.pid}, tmp_path.glob("ppp*.pid")) == "ppp1"
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_iface_for_pids_pid_morto_nao_conta(tmp_path):
    """N3: um pid file obsoleto (PID já morreu) não pode ser confiado —
    mesmo que, por coincidência, o número ainda esteja no conjunto `pids`."""
    pid_morto = 2**31 + 11  # nunca existiu
    (tmp_path / "ppp0.pid").write_text(f"{pid_morto}\n")
    assert iface_for_pids({pid_morto}, tmp_path.glob("ppp*.pid")) is None


def test_iface_for_pids_processo_vivo_mas_nao_e_pppd(tmp_path, monkeypatch):
    """N3: PID vivo e presente em `pids`, mas o processo dono não é um
    `pppd` de verdade (comm diferente) — pid file não pode ser confiado."""
    sys_class_net = tmp_path / "sys-class-net"
    sys_class_net.mkdir()
    (sys_class_net / "ppp1").mkdir()
    monkeypatch.setattr(vpnctl, "SYS_CLASS_NET", sys_class_net)

    # Processo real, mas SEM o truque do symlink "pppd" — comm vem do nome
    # real do interpretador (python3/python3.12/...), não "pppd".
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    try:
        (tmp_path / "ppp1.pid").write_text(f"{proc.pid}\n")
        assert iface_for_pids({proc.pid}, tmp_path.glob("ppp*.pid")) is None
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_iface_for_pids_interface_ausente_no_sysfs(tmp_path, monkeypatch):
    """N3: PID vivo, é um pppd de verdade, casa com `pids` — mas a interface
    já não existe mais no kernel (sysfs). Não pode ser confiado."""
    sys_class_net = tmp_path / "sys-class-net"
    sys_class_net.mkdir()  # "ppp1" NÃO criado de propósito
    monkeypatch.setattr(vpnctl, "SYS_CLASS_NET", sys_class_net)

    symlink = tmp_path / "pppd"
    symlink.symlink_to(sys.executable)
    proc = subprocess.Popen([str(symlink), "-c", "import time; time.sleep(5)"])
    try:
        (tmp_path / "ppp1.pid").write_text(f"{proc.pid}\n")
        assert iface_for_pids({proc.pid}, tmp_path.glob("ppp*.pid")) is None
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_iface_for_pids_casamento_multiplo_devolve_none(tmp_path, monkeypatch):
    """N2 da auditoria, reproduzindo o cenário exato que a revisão provou:
    duas instâncias (duas interfaces ppp*, dois pppd reais) ambas com PID no
    conjunto `pids` — escolher a primeira em ordem lexicográfica ("ppp10"
    antes de "ppp2") já pintou "Faltando" numa rede roteada, só que pela
    interface errada. `None` (honesto) é a única resposta segura."""
    sys_class_net = tmp_path / "sys-class-net"
    sys_class_net.mkdir()
    monkeypatch.setattr(vpnctl, "SYS_CLASS_NET", sys_class_net)

    symlink = tmp_path / "pppd"
    symlink.symlink_to(sys.executable)
    proc_a = _pppd_de_verdade(tmp_path, sys_class_net, "ppp10")
    proc_b = _pppd_de_verdade(tmp_path, sys_class_net, "ppp2")
    try:
        (tmp_path / "ppp10.pid").write_text(f"{proc_a.pid}\n")
        (tmp_path / "ppp2.pid").write_text(f"{proc_b.pid}\n")
        # Ordem lexicográfica de sorted(["ppp10.pid", "ppp2.pid"]) põe
        # "ppp10.pid" primeiro — exatamente a armadilha que N2 apontou.
        assert sorted(tmp_path.glob("ppp*.pid"))[0].name == "ppp10.pid"
        iface = iface_for_pids({proc_a.pid, proc_b.pid}, tmp_path.glob("ppp*.pid"))
        assert iface is None
    finally:
        proc_a.terminate()
        proc_b.terminate()
        proc_a.wait(timeout=5)
        proc_b.wait(timeout=5)


def test_estado_nao_configurado_vence_tudo():
    assert resolve_state(PERFIL, False, ATIVA, (), "ppp2", ()) == State.UNCONFIGURED


def test_estado_externo_prevalece_sobre_ativo():
    """Unit ativa E processo avulso: é a duplicata original. Externo vence."""
    assert resolve_state(PERFIL, True, ATIVA, (4242,), "ppp2", ()) == State.EXTERNAL


def test_estado_externo_com_unit_parada():
    assert resolve_state(PERFIL, True, PARADA, (4242,), None, ()) == State.EXTERNAL


def test_estado_ativo_com_todas_as_rotas():
    assert resolve_state(PERFIL, True, ATIVA, (), "ppp2", ()) == State.ACTIVE


def test_estado_parcial_quando_falta_rota():
    estado = resolve_state(PERFIL, True, ATIVA, (), "ppp2", ("10.0.1.0/24",))
    assert estado == State.PARTIAL


def test_estado_falhou():
    assert resolve_state(PERFIL, True, FALHOU, (), None, ()) == State.FAILED


def test_estado_inativo():
    assert resolve_state(PERFIL, True, PARADA, (), None, ()) == State.INACTIVE


def test_estado_conectando():
    props = {"ActiveState": "activating", "SubState": "start"}
    assert resolve_state(PERFIL, True, props, (), None, ()) == State.CONNECTING


def test_status_of_nao_propaga_timeout_do_systemctl():
    """systemctl (ou ip) travando não deve derrubar status_of — deve degradar.

    Usa um id de perfil sem correlação com nada real no sistema (sem .conf, sem
    processo homônimo) para que o resultado não dependa do que está rodando na
    máquina de teste — só a ausência de exceção e o formato do retorno importam.
    """
    perfil_isolado = Profile(
        id="vpn-teste-timeout-inexistente-xyz",
        name="Teste", purpose="Teste",
        networks=("192.0.2.0/24",), checks=(),
    )

    def run_que_estoura_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0] if args else "cmd", timeout=5)

    status = status_of(perfil_isolado, run=run_que_estoura_timeout)

    assert isinstance(status, ProfileStatus)
    assert status.state == State.UNCONFIGURED


def test_status_of_calcula_iface_de_processo_externo_via_filho_pppd(tmp_path, monkeypatch):
    """Important 1(b) da revisão de 2026-08-07: `status_of` precisa achar a
    interface ppp* de um processo openfortivpn EXTERNO (fora do systemd)
    também — não só da unit. A técnica é a mesma do Critical 1: o `pppd` é
    filho direto do `openfortivpn`, e é o PID do `pppd` (não do
    `openfortivpn`) que aparece em `/run/pppN.pid`.

    Sem isso, EXTERNAL nunca teria rota verificada, e a §3.1/§3.4 do
    documento de layout (que sempre mostraram `externo` com rota real —
    "foi exatamente o que aconteceu com as duas instâncias simultâneas do
    vpn-exemplo") ficaria estruturalmente impossível de implementar
    fielmente. `CONF_DIR`/`PPP_PID_GLOB` são monkeypatchados para um
    `tmp_path` porque os reais (`/etc/openfortivpn`, `/run`) exigem root."""
    conf_dir = tmp_path / "openfortivpn"
    conf_dir.mkdir()
    perfil_id = "vpn-teste-external-iface"
    (conf_dir / f"{perfil_id}.conf").write_text("# fake\n")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    sys_class_net = tmp_path / "sys-class-net"
    sys_class_net.mkdir()
    (sys_class_net / "ppp7").mkdir()  # N3: iface_for_pids agora exige a interface no "sysfs"

    monkeypatch.setattr(vpnctl, "CONF_DIR", conf_dir)
    monkeypatch.setattr(vpnctl, "PPP_PID_GLOB", str(run_dir / "ppp*.pid"))
    monkeypatch.setattr(vpnctl, "SYS_CLASS_NET", sys_class_net)

    # Processo pai real ("openfortivpn") com um filho real ("pppd") — ppid de
    # verdade, visível em /proc, não um mock. O filho é executado via um
    # symlink chamado "pppd": o kernel preenche /proc/<pid>/comm com o nome
    # do binário executado, então isso é o suficiente para `_pidfile_confiavel`
    # (N3) aceitar o filho como um pppd de verdade.
    symlink_pppd = tmp_path / "pppd"
    symlink_pppd.symlink_to(sys.executable)
    script_pai = (
        "import subprocess, sys, time\n"
        f"filho = subprocess.Popen([{str(symlink_pppd)!r}, '-c', 'import time; time.sleep(5)'])\n"
        "print(filho.pid, flush=True)\n"
        "time.sleep(5)\n"
    )
    symlink = tmp_path / "openfortivpn-bin"
    symlink.symlink_to(sys.executable)
    pai = subprocess.Popen(
        [str(symlink), "-c", script_pai, f"{conf_dir}/{perfil_id}.conf"],
        stdout=subprocess.PIPE, text=True,
    )
    pid_filho = None
    try:
        pid_filho = int(pai.stdout.readline().strip())
        (run_dir / "ppp7.pid").write_text(f"{pid_filho}\n")

        profile = Profile(
            id=perfil_id, name="Teste", purpose="Teste",
            networks=("10.99.99.0/24",), checks=(),
        )

        class FakeRun:
            def __call__(self, cmd, **kwargs):
                class R:
                    pass
                r = R()
                r.stderr = ""
                if cmd[0] == "systemctl":
                    r.returncode = 0
                    r.stdout = ""  # unit nunca existiu para este perfil de teste
                elif cmd[:2] == ["ip", "-j"]:
                    r.returncode = 0
                    r.stdout = json.dumps([{"dst": "10.99.99.0/24", "dev": "ppp7"}])
                else:
                    r.returncode = 1
                    r.stdout = ""
                return r

        status = status_of(profile, run=FakeRun())
    finally:
        pai.terminate()
        pai.wait(timeout=5)
        pai.stdout.close()
        # `pai` morre; o filho (o "pppd" desta simulação) NÃO — vira órfão e
        # continuaria rodando pelos 5s dele. Mata explicitamente (com
        # checagem de vivacidade antes — N6 da revisão de 2026-08-07).
        _matar_pid_se_vivo(pid_filho)

    assert status.state == State.EXTERNAL
    assert status.iface == "ppp7"
    assert status.missing == ()  # rota presente E verificada de verdade


def test_status_of_externo_com_rota_faltando_de_verdade(tmp_path, monkeypatch):
    """Mesmo cenário do teste acima, mas a rota da rede NÃO existe — EXTERNAL
    verificado precisa conseguir dizer "Faltando" tanto quanto "Roteada"."""
    conf_dir = tmp_path / "openfortivpn"
    conf_dir.mkdir()
    perfil_id = "vpn-teste-external-iface-sem-rota"
    (conf_dir / f"{perfil_id}.conf").write_text("# fake\n")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    sys_class_net = tmp_path / "sys-class-net"
    sys_class_net.mkdir()
    (sys_class_net / "ppp8").mkdir()

    monkeypatch.setattr(vpnctl, "CONF_DIR", conf_dir)
    monkeypatch.setattr(vpnctl, "PPP_PID_GLOB", str(run_dir / "ppp*.pid"))
    monkeypatch.setattr(vpnctl, "SYS_CLASS_NET", sys_class_net)

    symlink_pppd = tmp_path / "pppd"
    symlink_pppd.symlink_to(sys.executable)
    script_pai = (
        "import subprocess, sys, time\n"
        f"filho = subprocess.Popen([{str(symlink_pppd)!r}, '-c', 'import time; time.sleep(5)'])\n"
        "print(filho.pid, flush=True)\n"
        "time.sleep(5)\n"
    )
    symlink = tmp_path / "openfortivpn-bin"
    symlink.symlink_to(sys.executable)
    pai = subprocess.Popen(
        [str(symlink), "-c", script_pai, f"{conf_dir}/{perfil_id}.conf"],
        stdout=subprocess.PIPE, text=True,
    )
    pid_filho = None
    try:
        pid_filho = int(pai.stdout.readline().strip())
        (run_dir / "ppp8.pid").write_text(f"{pid_filho}\n")

        profile = Profile(
            id=perfil_id, name="Teste", purpose="Teste",
            networks=("10.88.88.0/24",), checks=(),
        )

        class FakeRun:
            def __call__(self, cmd, **kwargs):
                class R:
                    pass
                r = R()
                r.stderr = ""
                if cmd[0] == "systemctl":
                    r.returncode = 0
                    r.stdout = ""
                elif cmd[:2] == ["ip", "-j"]:
                    r.returncode = 0
                    r.stdout = json.dumps([])  # nenhuma rota
                else:
                    r.returncode = 1
                    r.stdout = ""
                return r

        status = status_of(profile, run=FakeRun())
    finally:
        pai.terminate()
        pai.wait(timeout=5)
        pai.stdout.close()
        _matar_pid_se_vivo(pid_filho)

    assert status.state == State.EXTERNAL
    assert status.iface == "ppp8"
    assert status.missing == ("10.88.88.0/24",)  # verificado, e falta de verdade


def test_status_of_externo_sem_pppd_ainda_nao_vira_faltando(tmp_path, monkeypatch):
    """N1 da revisão seguinte: um `openfortivpn` externo recém-aparecido
    (ainda autenticando — com 2FA isso dura 10-20s) já é `EXTERNAL`
    (`external_pids` preenchido), mas ainda não forkou o `pppd` — `iface`
    fica `None`. `missing_networks(redes, None, ())` devolveria TODAS as
    redes (ver `probe.py`) se `status_of` não blindasse contra isso: a
    janela pintaria "Faltando" em tudo, indistinguível de uma checagem real
    negativa, e empurraria o usuário a Adotar um túnel que só está demorando
    para autenticar."""
    conf_dir = tmp_path / "openfortivpn"
    conf_dir.mkdir()
    perfil_id = "vpn-teste-external-sem-pppd-ainda"
    (conf_dir / f"{perfil_id}.conf").write_text("# fake\n")
    run_dir = tmp_path / "run"
    run_dir.mkdir()  # nenhum ppp*.pid — o pppd realmente ainda não existe

    monkeypatch.setattr(vpnctl, "CONF_DIR", conf_dir)
    monkeypatch.setattr(vpnctl, "PPP_PID_GLOB", str(run_dir / "ppp*.pid"))

    symlink = tmp_path / "openfortivpn-bin"
    symlink.symlink_to(sys.executable)
    # SEM filho: openfortivpn sozinho, como estaria nos primeiros segundos do
    # handshake/2FA, antes do pppd existir.
    proc = subprocess.Popen(
        [str(symlink), "-c", "import time; time.sleep(5)", f"{conf_dir}/{perfil_id}.conf"],
    )
    try:
        profile = Profile(
            id=perfil_id, name="Teste", purpose="Teste",
            networks=("10.77.77.0/24",), checks=(),
        )

        class FakeRun:
            def __call__(self, cmd, **kwargs):
                class R:
                    pass
                r = R()
                r.stderr = ""
                if cmd[0] == "systemctl":
                    r.returncode = 0
                    r.stdout = ""
                elif cmd[:2] == ["ip", "-j"]:
                    r.returncode = 0
                    r.stdout = json.dumps([])
                else:
                    r.returncode = 1
                    r.stdout = ""
                return r

        status = status_of(profile, run=FakeRun())
    finally:
        proc.terminate()
        proc.wait(timeout=5)

    assert status.state == State.EXTERNAL
    assert status.iface is None
    assert status.missing == ()  # não verificado — nunca "todas faltando"


# ═══════════════════════════════════════════════════════════════════════════
# Item 1 (CRITICAL) da rodada de fechamento pré-merge de 2026-08-07: os três
# vetores provados pelo revisor onde uma FALHA DE LEITURA (não um estado real
# do sistema) virava um estado alarmante com botão destrutivo. Cada teste
# abaixo reproduz o vetor com um processo real e/ou um `run` fake que falha
# exatamente como o revisor descreveu, e prova que `status_of` degrada com
# honestidade em vez de inventar `externo`/`parcial`.
# ═══════════════════════════════════════════════════════════════════════════


def test_status_of_systemctl_timeout_nao_vira_externo(tmp_path, monkeypatch):
    """Vetor A: `systemctl show` estoura timeout -> antes da correção,
    `status_of` engolia a exceção e usava `props={}` -> sem `ControlGroup`,
    `_unit_pids` devolvia `set()` -> `_external_pids` classificava um
    processo openfortivpn REAL deste perfil (rodando agora, de verdade) como
    externo -> `resolve_state` devolvia EXTERNAL, oferecendo Adotar (mata o
    processo) sobre um túnel possivelmente são. `read_ok=False` precisa
    bloquear essa dedução."""
    conf_dir = tmp_path / "openfortivpn"
    conf_dir.mkdir()
    perfil_id = "vpn-teste-vetor-a"
    (conf_dir / f"{perfil_id}.conf").write_text("# fake\n")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setattr(vpnctl, "CONF_DIR", conf_dir)
    monkeypatch.setattr(vpnctl, "PPP_PID_GLOB", str(run_dir / "ppp*.pid"))

    symlink = tmp_path / "openfortivpn-bin"
    symlink.symlink_to(sys.executable)
    proc = subprocess.Popen(
        [str(symlink), "-c", "import time; time.sleep(5)", f"{conf_dir}/{perfil_id}.conf"],
    )
    try:
        profile = Profile(id=perfil_id, name="Teste", purpose="Teste",
                           networks=("10.55.55.0/24",), checks=())

        def run_systemctl_estoura(cmd, **kwargs):
            if cmd[0] == "systemctl":
                raise subprocess.TimeoutExpired(cmd, 5)

            class R:
                returncode = 0
                stdout = "[]"
                stderr = ""
            return R()

        status = status_of(profile, run=run_systemctl_estoura)
    finally:
        proc.terminate()
        proc.wait(timeout=5)

    assert status.state is not State.EXTERNAL
    assert status.external_pids == ()
    assert status.read_ok is False


def test_status_of_cgroup_ilegivel_nao_vira_externo(tmp_path, monkeypatch):
    """Vetor B: `systemctl show` funciona e diz a unit ACTIVE com um
    `ControlGroup`, mas `cgroup.procs` desse caminho é ilegível (aqui:
    simplesmente não existe — mesmo efeito de uma permissão negada ou um
    cgroup que sumiu entre a leitura da propriedade e a leitura do arquivo).
    Mesmo desfecho do Vetor A, mesmo caminho (`_unit_pids` devolve `set()`
    por FALHA, não porque a unit está parada) — e mesma correção: nunca
    EXTERNAL a partir de um `unit_pids` vazio que não sabemos se é de
    verdade."""
    conf_dir = tmp_path / "openfortivpn"
    conf_dir.mkdir()
    perfil_id = "vpn-teste-vetor-b"
    (conf_dir / f"{perfil_id}.conf").write_text("# fake\n")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setattr(vpnctl, "CONF_DIR", conf_dir)
    monkeypatch.setattr(vpnctl, "PPP_PID_GLOB", str(run_dir / "ppp*.pid"))
    # CGROUP_ROOT aponta pra um diretório vazio: o caminho que o systemctl
    # anuncia não existe ali dentro -> `cgroup.procs` levanta FileNotFoundError.
    cgroup_vazio = tmp_path / "cgroup-vazio"
    cgroup_vazio.mkdir()
    monkeypatch.setattr(vpnctl, "CGROUP_ROOT", cgroup_vazio)

    symlink = tmp_path / "openfortivpn-bin"
    symlink.symlink_to(sys.executable)
    proc = subprocess.Popen(
        [str(symlink), "-c", "import time; time.sleep(5)", f"{conf_dir}/{perfil_id}.conf"],
    )
    try:
        profile = Profile(id=perfil_id, name="Teste", purpose="Teste",
                           networks=("10.66.66.0/24",), checks=())

        def run_ok(cmd, **kwargs):
            class R:
                pass
            r = R()
            r.stderr = ""
            if cmd[0] == "systemctl":
                r.returncode = 0
                r.stdout = (
                    "ActiveState=active\nSubState=running\n"
                    f"ControlGroup=/system.slice/openfortivpn@{perfil_id}.service\n"
                )
            elif cmd[:2] == ["ip", "-j"]:
                r.returncode = 0
                r.stdout = "[]"
            else:
                r.returncode = 1
                r.stdout = ""
            return r

        status = status_of(profile, run=run_ok)
    finally:
        proc.terminate()
        proc.wait(timeout=5)

    assert status.state is not State.EXTERNAL
    assert status.external_pids == ()
    assert status.read_ok is False


def test_status_of_ip_route_falha_nao_vira_parcial(tmp_path, monkeypatch):
    """Vetor C — o que morde HOJE, sem precisar de migração nenhuma: `ip
    route show` falha ou estoura timeout -> antes da correção, `read_routes`
    devolvia `()`, indistinguível de "nenhuma rota" -> toda rede do perfil
    virava ausente -> `parcial`, com a frase "falta rota" e o botão
    Reconectar (`systemctl restart`) sobre um túnel são. `read_ok=False`
    precisa impedir essa dedução: sem rota lida, sem "faltando" inventado."""
    conf_dir = tmp_path / "openfortivpn"
    conf_dir.mkdir()
    perfil_id = "vpn-teste-vetor-c"
    (conf_dir / f"{perfil_id}.conf").write_text("# fake\n")
    monkeypatch.setattr(vpnctl, "CONF_DIR", conf_dir)

    profile = Profile(id=perfil_id, name="Teste", purpose="Teste",
                       networks=("10.99.0.0/24",), checks=())

    def run_ip_falha(cmd, **kwargs):
        class R:
            pass
        r = R()
        r.stderr = ""
        if cmd[0] == "systemctl":
            r.returncode = 0
            r.stdout = "ActiveState=active\nSubState=running\n"
        elif cmd[:2] == ["ip", "-j"]:
            r.returncode = 1  # 'ip route' falhou
            r.stdout = ""
        else:
            r.returncode = 1
            r.stdout = ""
        return r

    status = status_of(profile, run=run_ip_falha)

    assert status.state is not State.PARTIAL
    assert status.missing == ()
    assert status.read_ok is False


def test_status_of_ip_route_timeout_nao_vira_parcial(tmp_path, monkeypatch):
    """Mesmo Vetor C, mas via timeout (`subprocess.TimeoutExpired`) em vez de
    `returncode != 0` — a "prova do revisor" cobre os dois. Timeout no `ip`
    não passa por `RouteReadError` (é levantado pelo próprio `run`, não por
    `read_routes`); `status_of` precisa capturar os dois tipos de falha."""
    conf_dir = tmp_path / "openfortivpn"
    conf_dir.mkdir()
    perfil_id = "vpn-teste-vetor-c-timeout"
    (conf_dir / f"{perfil_id}.conf").write_text("# fake\n")
    monkeypatch.setattr(vpnctl, "CONF_DIR", conf_dir)

    profile = Profile(id=perfil_id, name="Teste", purpose="Teste",
                       networks=("10.98.0.0/24",), checks=())

    def run_ip_estoura(cmd, **kwargs):
        if cmd[:2] == ["ip", "-j"]:
            raise subprocess.TimeoutExpired(cmd, 5)

        class R:
            returncode = 0
            stdout = "ActiveState=active\nSubState=running\n"
            stderr = ""
        return R()

    status = status_of(profile, run=run_ip_estoura)

    assert status.state is not State.PARTIAL
    assert status.missing == ()
    assert status.read_ok is False


# ═══════════════════════════════════════════════════════════════════════════
# Re-revisão de fechamento pré-merge (2026-08-07): um quarto caminho para o
# mesmo tipo de falha (Correção 1) e um congelamento do gate `verificado`
# que limpava o CAMPO mas não o ESTADO (Correção 2) — os dois reproduzidos
# empiricamente pela re-revisão, ambos vetores de leitura degradada virando
# estado alarmante com botão destrutivo.
# ═══════════════════════════════════════════════════════════════════════════


def test_status_of_unit_ativa_sem_controlgroup_nao_vira_externo(tmp_path, monkeypatch):
    """Correção 1: `systemctl show` pode responder `rc=0` com a unit `active`
    mas SEM `ControlGroup` no stdout — medido empiricamente pela re-revisão.
    O gate antigo (`systemctl_ok = proc.returncode == 0`) confundia "o
    comando funcionou" com "aprendi o cgroup": `_unit_pids("")` devolvia
    `ok=True` (a MESMA resposta de uma unit legitimamente parada), então
    `_external_pids` rodava e classificava um processo openfortivpn REAL
    deste perfil (rodando agora, de verdade) como externo — `resolve_state`
    dava EXTERNAL, oferecendo Adotar (`pkexec kill` como root) sobre um
    túnel são. Este teste teria falhado ANTES da correção com
    `status.state == State.EXTERNAL`."""
    conf_dir = tmp_path / "openfortivpn"
    conf_dir.mkdir()
    perfil_id = "vpn-teste-correcao1-sem-cgroup"
    (conf_dir / f"{perfil_id}.conf").write_text("# fake\n")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setattr(vpnctl, "CONF_DIR", conf_dir)
    monkeypatch.setattr(vpnctl, "PPP_PID_GLOB", str(run_dir / "ppp*.pid"))

    symlink = tmp_path / "openfortivpn-bin"
    symlink.symlink_to(sys.executable)
    proc = subprocess.Popen(
        [str(symlink), "-c", "import time; time.sleep(5)", f"{conf_dir}/{perfil_id}.conf"],
    )
    try:
        profile = Profile(id=perfil_id, name="Teste", purpose="Teste",
                           networks=("10.44.44.0/24",), checks=())

        def run_ativo_sem_controlgroup(cmd, **kwargs):
            class R:
                pass
            r = R()
            r.stderr = ""
            if cmd[0] == "systemctl":
                r.returncode = 0
                # rc=0, unit "active", mas SEM linha "ControlGroup=" — o
                # caso exato que a re-revisão mediu.
                r.stdout = "ActiveState=active\nSubState=running\n"
            elif cmd[:2] == ["ip", "-j"]:
                r.returncode = 0
                r.stdout = "[]"
            else:
                r.returncode = 1
                r.stdout = ""
            return r

        status = status_of(profile, run=run_ativo_sem_controlgroup)
    finally:
        proc.terminate()
        proc.wait(timeout=5)

    assert status.state is not State.EXTERNAL
    assert status.external_pids == ()
    assert status.read_ok is False


def test_status_of_unit_parada_sem_controlgroup_continua_ok(tmp_path, monkeypatch):
    """Correção 1, guarda contrária: unit de verdade PARADA (`inactive`) sem
    `ControlGroup` é o caso normal, não uma falha — não pode perder botões.
    `read_ok` continua `True` e o perfil segue oferecendo "Conectar"."""
    conf_dir = tmp_path / "openfortivpn"
    conf_dir.mkdir()
    perfil_id = "vpn-teste-correcao1-parada"
    (conf_dir / f"{perfil_id}.conf").write_text("# fake\n")
    monkeypatch.setattr(vpnctl, "CONF_DIR", conf_dir)

    profile = Profile(id=perfil_id, name="Teste", purpose="Teste",
                       networks=("10.45.45.0/24",), checks=())

    def run_parada_sem_controlgroup(cmd, **kwargs):
        class R:
            pass
        r = R()
        r.stderr = ""
        if cmd[0] == "systemctl":
            r.returncode = 0
            r.stdout = "ActiveState=inactive\nSubState=dead\n"
        elif cmd[:2] == ["ip", "-j"]:
            r.returncode = 0
            r.stdout = "[]"
        else:
            r.returncode = 1
            r.stdout = ""
        return r

    status = status_of(profile, run=run_parada_sem_controlgroup)

    assert status.state is State.INACTIVE
    assert status.read_ok is True


def test_status_of_iface_nao_resolvida_nao_vira_parcial(tmp_path, monkeypatch):
    """Correção 2: a resolução de `iface` pode falhar por conta própria (pid
    file ilegível, apagado entre o `glob` e a leitura, ou reprovado por
    `_pidfile_confiavel`) SEM que isso entre em `read_ok` — a leitura da
    unit e das rotas continua boa neste cenário (unit ativa com
    `ControlGroup` de verdade, `cgroup.procs` legível, `ip route` ok). Antes
    da correção, `missing_networks(redes, None, rotas)` devolvia TODAS as
    redes e ISSO alimentava `resolve_state`, que resolvia PARTIAL de
    verdade; só o CAMPO publicado (`status.missing`) saía vazio depois (gate
    tarde demais) — a prova textual era a frase "falta rota para " sem
    nenhuma rede listada. Este teste teria falhado ANTES da correção com
    `status.state == State.PARTIAL`."""
    conf_dir = tmp_path / "openfortivpn"
    conf_dir.mkdir()
    perfil_id = "vpn-teste-correcao2"
    (conf_dir / f"{perfil_id}.conf").write_text("# fake\n")
    run_dir = tmp_path / "run"
    run_dir.mkdir()  # nenhum ppp*.pid: iface_for_pids não tem como casar nada
    cgroup_root = tmp_path / "cgroup"
    monkeypatch.setattr(vpnctl, "CONF_DIR", conf_dir)
    monkeypatch.setattr(vpnctl, "PPP_PID_GLOB", str(run_dir / "ppp*.pid"))
    monkeypatch.setattr(vpnctl, "CGROUP_ROOT", cgroup_root)

    symlink = tmp_path / "openfortivpn-bin"
    symlink.symlink_to(sys.executable)
    proc = subprocess.Popen(
        [str(symlink), "-c", "import time; time.sleep(5)", f"{conf_dir}/{perfil_id}.conf"],
    )
    try:
        controle = f"/system.slice/openfortivpn@{perfil_id}.service"
        cgroup_dir = cgroup_root / controle.lstrip("/")
        cgroup_dir.mkdir(parents=True)
        (cgroup_dir / "cgroup.procs").write_text(f"{proc.pid}\n")

        profile = Profile(id=perfil_id, name="Teste", purpose="Teste",
                           networks=("10.33.33.0/24",), checks=())

        def run_ok(cmd, **kwargs):
            class R:
                pass
            r = R()
            r.stderr = ""
            if cmd[0] == "systemctl":
                r.returncode = 0
                r.stdout = (
                    "ActiveState=active\nSubState=running\n"
                    f"ControlGroup={controle}\n"
                )
            elif cmd[:2] == ["ip", "-j"]:
                r.returncode = 0
                # rota presente para a rede do perfil, só que num dev que
                # `iface_for_pids` nunca vai conseguir confirmar (nenhum
                # ppp*.pid casa com o PID da unit neste teste).
                r.stdout = json.dumps([{"dst": "10.33.33.0/24", "dev": "ppp0"}])
            else:
                r.returncode = 1
                r.stdout = ""
            return r

        status = status_of(profile, run=run_ok)
    finally:
        proc.terminate()
        proc.wait(timeout=5)

    assert status.read_ok is True  # systemctl e rotas leram bem — só a iface não resolveu
    assert status.iface is None
    assert status.state not in (State.PARTIAL, State.EXTERNAL)
    assert status.missing == ()
