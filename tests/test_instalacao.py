"""Testes dos artefatos de instalação.

São arquivos de dados, não código — mas há duas consistências entre eles e o
Python que, se quebrarem, produzem falhas difíceis de diagnosticar: o caminho
do helper e a decisão de autorização.
"""

import ast
import stat
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from vpn_manager.profile_client import HELPER

RAIZ = Path(__file__).resolve().parent.parent
POLICY = RAIZ / "data" / "br.dev.matheus.VpnManager.policy"
LAUNCHER = RAIZ / "data" / "vpn-manager-helper"
INSTALL = RAIZ / "install.sh"

ACTION_ID = "br.dev.matheus.vpn-manager.manage-profiles"


@pytest.fixture
def acao():
    raiz = ET.parse(POLICY).getroot()
    for a in raiz.findall("action"):
        if a.get("id") == ACTION_ID:
            return a
    pytest.fail(f"action {ACTION_ID} não encontrada no .policy")


class TestPolicy:
    def test_e_xml_valido(self):
        ET.parse(POLICY)

    def test_exec_path_bate_com_o_caminho_usado_pelo_cliente(self, acao):
        """Se o .policy apontar para um binário e o Python chamar outro, o
        polkit recusa com uma mensagem que não explica nada."""
        anotacoes = {
            a.get("key"): a.text for a in acao.findall("annotate")
        }
        assert anotacoes["org.freedesktop.policykit.exec.path"] == HELPER

    def test_exige_senha_de_admin_conforme_D1(self, acao):
        """Decisão D1 do backlog. Escrever em ip-up.d é execução de código
        como root a cada conexão; um YES silencioso daria isso a qualquer
        processo da sessão."""
        assert acao.find("defaults/allow_active").text == "auth_admin_keep"

    def test_nao_autoriza_sessao_inativa_nem_remota(self, acao):
        assert acao.find("defaults/allow_any").text == "no"
        assert acao.find("defaults/allow_inactive").text == "no"

    def test_tem_descricao_em_portugues_para_o_prompt(self, acao):
        """O texto do prompt é o que o usuário lê antes de digitar a senha."""
        mensagens = {m.get("{http://www.w3.org/XML/1998/namespace}lang"): m.text
                     for m in acao.findall("message")}
        assert "pt_BR" in mensagens or "pt" in mensagens


class TestLauncher:
    def test_existe_e_e_executavel_no_repositorio(self):
        assert LAUNCHER.exists()
        assert LAUNCHER.stat().st_mode & stat.S_IXUSR

    def test_roda_python_em_modo_isolado(self):
        """`-I` faz o interpretador ignorar PYTHONPATH, o diretório do script
        e o site-packages do usuário. Sem isso, alguém com controle do
        ambiente injetaria um módulo no processo root."""
        assert LAUNCHER.read_text().splitlines()[0] == "#!/usr/bin/python3 -I"

    def test_fixa_o_sys_path_na_copia_congelada(self):
        assert "/usr/local/lib/vpn-manager" in LAUNCHER.read_text()

    def test_passa_o_lock_para_serializar_escritas_concorrentes(self):
        assert "lock_path" in LAUNCHER.read_text()

    def test_o_unico_caminho_inserido_no_sys_path_e_a_copia_congelada(self):
        """Lido do código, não do texto: comentário que menciona PYTHONPATH é
        explicação, não comportamento. O que importa é qual literal chega ao
        sys.path de um processo root."""
        inseridos = []
        for no in ast.walk(ast.parse(LAUNCHER.read_text())):
            if not isinstance(no, ast.Call):
                continue
            alvo = no.func
            if (
                isinstance(alvo, ast.Attribute)
                and alvo.attr in ("insert", "append")
                and isinstance(alvo.value, ast.Attribute)
                and alvo.value.attr == "path"
            ):
                inseridos += [
                    a.value
                    for a in no.args
                    if isinstance(a, ast.Constant) and isinstance(a.value, str)
                ]

        assert inseridos == ["/usr/local/lib/vpn-manager"]


class TestInstalador:
    def test_instala_o_policy(self):
        assert "br.dev.matheus.VpnManager.policy" in INSTALL.read_text()
        assert "/usr/share/polkit-1/actions" in INSTALL.read_text()

    def test_instala_a_copia_congelada_como_root(self):
        texto = INSTALL.read_text()
        assert "/usr/local/lib/vpn-manager" in texto
        assert "-o root -g root" in texto

    def test_copia_todos_os_modulos_que_o_helper_importa(self):
        """Faltar um módulo só aparece na primeira vez que o usuário salva um
        perfil — com o prompt de senha já gasto."""
        texto = INSTALL.read_text()
        for modulo in (
            "helper_main.py",
            "profile_store.py",
            "protocol.py",
            "catalog.py",
            "models.py",
            "__init__.py",
        ):
            assert modulo in texto, f"{modulo} não é copiado pelo install.sh"
