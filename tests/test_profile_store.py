import os

import pytest

from vpn_manager.catalog import load_catalog
from vpn_manager.models import Check
from vpn_manager.profile_store import (
    ValidationError,
    ApplyError,
    Paths,
    apply_profile,
    remove_profile,
    render_conf,
    render_ip_up_script,
    render_toml,
    validate_host,
    validate_id,
    validate_network,
    validate_port,
    validate_text,
)


class TestValidateId:
    """O `id` é o único dado do usuário que entra em caminho de arquivo
    (.conf, script de ip-up.d) e em nome de unit systemd. É a validação
    mais crítica do módulo."""

    @pytest.mark.parametrize(
        "valido",
        [
            "vpn",
            "vpn-exemplo",
            "a1",
            "vpn-exemplo-2",
            "x" * 32,
        ],
    )
    def test_aceita_ids_bem_formados(self, valido):
        assert validate_id(valido) == valido

    @pytest.mark.parametrize(
        "vetor",
        [
            "../../../etc/cron.d/x",
            "vpn/../../etc/passwd",
            "vpn/exemplo",
        ],
    )
    def test_rejeita_travessia_de_caminho(self, vetor):
        with pytest.raises(ValidationError):
            validate_id(vetor)

    def test_rejeita_ponto_porque_run_parts_ignora_silenciosamente(self):
        """run-parts pula arquivos com ponto no nome. Um id com ponto
        geraria um script de ip-up.d que nunca roda, e o perfil subiria
        sem rota nenhuma — falha silenciosa, o pior tipo."""
        with pytest.raises(ValidationError):
            validate_id("vpn.exemplo")

    @pytest.mark.parametrize(
        "vetor",
        ["vpn exemplo", "vpn\nexemplo", "vpn\texemplo", "vpn;rm -rf /", "vpn$(id)"],
    )
    def test_rejeita_espaco_controle_e_metacaracteres(self, vetor):
        with pytest.raises(ValidationError):
            validate_id(vetor)

    @pytest.mark.parametrize("vetor", ["-vpn", "vpn-", "VPN", "Vpn-Exemplo"])
    def test_rejeita_hifen_nas_pontas_e_maiuscula(self, vetor):
        with pytest.raises(ValidationError):
            validate_id(vetor)

    @pytest.mark.parametrize("vetor", ["", "x" * 33])
    def test_rejeita_vazio_e_longo_demais(self, vetor):
        with pytest.raises(ValidationError):
            validate_id(vetor)

    def test_rejeita_tipo_errado(self):
        with pytest.raises(ValidationError):
            validate_id(None)

    def test_mensagem_de_erro_cita_o_campo(self):
        with pytest.raises(ValidationError, match="id"):
            validate_id("VPN")


class TestValidateNetwork:
    """A rede é interpolada no script de ip-up.d, que roda como root a cada
    conexão. O que sai daqui vai re-serializado a partir de ip_network — o
    texto do usuário nunca chega ao script."""

    def test_normaliza_e_devolve_a_rede(self):
        assert validate_network("10.0.0.0/24") == "10.0.0.0/24"

    def test_aceita_ipv6(self):
        assert validate_network("2001:db8::/32") == "2001:db8::/32"

    def test_rejeita_bits_de_host_ligados(self):
        """10.0.0.5/24 é engano comum: o usuário quis 10.0.0.0/24. Recusar é
        melhor que adivinhar — a rota errada é silenciosa."""
        with pytest.raises(ValidationError):
            validate_network("10.0.0.5/24")

    @pytest.mark.parametrize(
        "vetor",
        [
            "10.0.0.0/24; rm -rf /",
            "10.0.0.0/24 && curl evil",
            "$(id)",
            "`id`",
            "10.0.0.0/24\nip route del default",
            "10.0.0.0/999",
            "nao-e-rede",
            "",
        ],
    )
    def test_rejeita_injecao_e_lixo(self, vetor):
        with pytest.raises(ValidationError):
            validate_network(vetor)

    def test_rejeita_tipo_errado(self):
        with pytest.raises(ValidationError):
            validate_network(42)


class TestValidateHost:
    @pytest.mark.parametrize(
        "valido", ["10.0.0.10", "vpn.exemplo.com", "host-1.exemplo.com", "2001:db8::1"]
    )
    def test_aceita_ip_e_hostname(self, valido):
        assert validate_host(valido) == valido

    @pytest.mark.parametrize(
        "vetor",
        ["", "host com espaco", "host\nssh", "-host.exemplo.com", "host..exemplo", "x" * 254],
    )
    def test_rejeita_malformados(self, vetor):
        with pytest.raises(ValidationError):
            validate_host(vetor)


class TestValidatePort:
    @pytest.mark.parametrize("valido", [1, 443, 65535])
    def test_aceita_faixa_valida(self, valido):
        assert validate_port(valido) == valido

    def test_aceita_texto_numerico(self):
        assert validate_port("443") == 443

    @pytest.mark.parametrize("vetor", [0, -1, 65536, "abc", "44 3", None, 1.5])
    def test_rejeita_fora_da_faixa_e_lixo(self, vetor):
        with pytest.raises(ValidationError):
            validate_port(vetor)

    @pytest.mark.parametrize("vetor", [True, False])
    def test_rejeita_booleano(self, vetor):
        """bool é subclasse de int em Python: sem guarda explícita, True
        passaria como porta 1 e False cairia na checagem de faixa por acaso."""
        with pytest.raises(ValidationError):
            validate_port(vetor)


class TestValidateText:
    """Campos livres (nome, propósito, rótulo, usuário). Vão para o .conf, que
    é linha-a-linha e não tem escape confiável, e para o TOML. Quebra de linha
    e controle são rejeitados, não escapados."""

    def test_aceita_texto_comum_com_acento(self):
        assert validate_text("Rede A — matriz", campo="nome") == "Rede A — matriz"

    @pytest.mark.parametrize(
        "vetor",
        [
            "linha\ninjetada",
            "retorno\rcarro",
            "tab\there",
            "nulo\x00byte",
            "escape\x1b[31m",
        ],
    )
    def test_rejeita_quebra_de_linha_e_controle(self, vetor):
        with pytest.raises(ValidationError):
            validate_text(vetor, campo="nome")

    def test_rejeita_longo_demais(self):
        with pytest.raises(ValidationError):
            validate_text("x" * 201, campo="nome")

    def test_mensagem_cita_o_campo(self):
        with pytest.raises(ValidationError, match="proposito"):
            validate_text("a\nb", campo="proposito")

    def test_rejeita_tipo_errado(self):
        with pytest.raises(ValidationError):
            validate_text(None, campo="nome")


# --------------------------------------------------------------------------
# 1.2 — serialização dos três artefatos
# --------------------------------------------------------------------------

PERFIL = {
    "id": "vpn-exemplo",
    "nome": "Rede A",
    "proposito": "Serviços internos",
    "gateway_host": "vpn.exemplo.com",
    "gateway_porta": 443,
    "username": "usuario",
    "trusted_cert": "a" * 64,
    "redes": ["10.0.0.0/24", "10.0.1.0/24"],
    "checks": [{"host": "10.0.0.10", "porta": 443, "rotulo": "Serviço web"}],
}


class TestRenderConf:
    def test_gera_as_diretivas_do_openfortivpn(self):
        texto = render_conf(PERFIL, senha="s3nha")
        assert "host = vpn.exemplo.com" in texto
        assert "port = 443" in texto
        assert "username = usuario" in texto
        assert "password = s3nha" in texto
        assert f"trusted-cert = {'a' * 64}" in texto

    def test_fixa_as_diretivas_que_o_app_depende(self):
        """probe.py checa rota na interface do túnel e missing_networks assume
        rotas explícitas. set-routes = 1 quebraria esse contrato, então a UI
        não oferece a opção — o serializador fixa."""
        texto = render_conf(PERFIL, senha="s3nha")
        assert "set-routes = 0" in texto
        assert "set-dns = 0" in texto
        assert "pppd-ipparam = vpn-exemplo" in texto

    def test_marca_o_arquivo_como_gerenciado_na_primeira_linha(self):
        assert render_conf(PERFIL, senha="x").splitlines()[0].startswith("#")
        assert "vpn-manager" in render_conf(PERFIL, senha="x").splitlines()[0]

    def test_preserva_linhas_desconhecidas_de_conf_manual(self):
        """Migração: um .conf escrito à mão pode ter opções que a UI não
        conhece. Não destruir o que não se entende."""
        texto = render_conf(PERFIL, senha="x", preservar=["half-internet-routes = 1"])
        assert "half-internet-routes = 1" in texto

    def test_omite_trusted_cert_quando_ausente(self):
        perfil = dict(PERFIL, trusted_cert="")
        assert "trusted-cert" not in render_conf(perfil, senha="x")

    def test_valida_antes_de_serializar(self):
        with pytest.raises(ValidationError):
            render_conf(dict(PERFIL, id="VPN"), senha="x")

    def test_recusa_senha_com_quebra_de_linha(self):
        """Uma senha com \\n injetaria uma diretiva no .conf."""
        with pytest.raises(ValidationError):
            render_conf(PERFIL, senha="senha\npppd-ipparam = outro")


class TestRenderIpUpScript:
    def test_uma_rota_por_rede(self):
        texto = render_ip_up_script(PERFIL)
        assert "ip route replace 10.0.0.0/24 dev \"$1\"" in texto
        assert "ip route replace 10.0.1.0/24 dev \"$1\"" in texto

    def test_tem_guard_pelo_ipparam(self):
        """O script roda para TODA conexão ppp da máquina. Sem o guard, ele
        instalaria as rotas deste perfil no túnel de outro."""
        texto = render_ip_up_script(PERFIL)
        assert '"$6" = "vpn-exemplo"' in texto
        assert "exit 0" in texto

    def test_comeca_com_shebang_e_marcador(self):
        linhas = render_ip_up_script(PERFIL).splitlines()
        assert linhas[0] == "#!/bin/sh"
        assert "vpn-manager" in linhas[1]

    def test_rede_vai_normalizada_nao_como_o_usuario_digitou(self):
        perfil = dict(PERFIL, redes=["10.0.0.0/255.255.255.0"])
        assert "10.0.0.0/24" in render_ip_up_script(perfil)

    def test_rejeita_rede_com_injecao(self):
        with pytest.raises(ValidationError):
            render_ip_up_script(dict(PERFIL, redes=["10.0.0.0/24; rm -rf /"]))


class TestRenderToml:
    def test_round_trip_por_load_catalog(self, tmp_path):
        """O teste que importa: o TOML gerado tem que ser lido de volta pelo
        catalog.py existente, produzindo o mesmo perfil. É o que impede o
        serializador próprio de divergir do parser da stdlib."""
        destino = tmp_path / "profiles.toml"
        destino.write_text(render_toml([PERFIL]), encoding="utf-8")

        perfis = load_catalog(destino)

        assert len(perfis) == 1
        p = perfis[0]
        assert p.id == "vpn-exemplo"
        assert p.name == "Rede A"
        assert p.purpose == "Serviços internos"
        assert p.networks == ("10.0.0.0/24", "10.0.1.0/24")
        assert p.checks == (Check(host="10.0.0.10", port=443, label="Serviço web"),)

    def test_round_trip_com_varios_perfis(self, tmp_path):
        outro = dict(PERFIL, id="vpn-exemplo-2", nome="Rede B", redes=[], checks=[])
        destino = tmp_path / "profiles.toml"
        destino.write_text(render_toml([PERFIL, outro]), encoding="utf-8")

        perfis = load_catalog(destino)

        assert [p.id for p in perfis] == ["vpn-exemplo", "vpn-exemplo-2"]

    def test_escapa_aspas_e_barra_invertida_sem_quebrar_o_parser(self, tmp_path):
        perfil = dict(PERFIL, nome='Rede "A" \\ matriz')
        destino = tmp_path / "profiles.toml"
        destino.write_text(render_toml([perfil]), encoding="utf-8")

        assert load_catalog(destino)[0].name == 'Rede "A" \\ matriz'

    def test_nao_serializa_a_senha(self):
        """O catálogo é 644; a senha só existe no .conf 600."""
        assert "senha" not in render_toml([PERFIL]).lower()
        assert "password" not in render_toml([PERFIL]).lower()


# --------------------------------------------------------------------------
# 1.3 — aplicação com snapshot e rollback
# --------------------------------------------------------------------------


def paths_de_teste(tmp_path):
    return Paths(
        conf_dir=tmp_path / "etc/openfortivpn",
        ip_up_dir=tmp_path / "etc/ppp/ip-up.d",
        catalog=tmp_path / "etc/vpn-manager/profiles.toml",
        undo_dir=tmp_path / "var/lib/vpn-manager/undo",
    )


class TestApplyCreate:
    def test_escreve_os_tres_artefatos(self, tmp_path):
        p = paths_de_teste(tmp_path)
        apply_profile(PERFIL, senha="s3nha", paths=p)

        assert (p.conf_dir / "vpn-exemplo.conf").exists()
        assert (p.ip_up_dir / "50vpnmgr-vpn-exemplo").exists()
        assert p.catalog.exists()

    def test_conf_e_600_e_script_e_executavel(self, tmp_path):
        p = paths_de_teste(tmp_path)
        apply_profile(PERFIL, senha="s3nha", paths=p)

        assert (p.conf_dir / "vpn-exemplo.conf").stat().st_mode & 0o777 == 0o600
        assert (p.ip_up_dir / "50vpnmgr-vpn-exemplo").stat().st_mode & 0o111

    def test_catalogo_fica_legivel(self, tmp_path):
        """O catálogo não tem segredo e é lido pelo app sem privilégio."""
        p = paths_de_teste(tmp_path)
        apply_profile(PERFIL, senha="s3nha", paths=p)
        assert p.catalog.stat().st_mode & 0o777 == 0o644

    def test_perfil_aparece_no_catalogo_lido_pelo_app(self, tmp_path):
        p = paths_de_teste(tmp_path)
        apply_profile(PERFIL, senha="s3nha", paths=p)
        assert [x.id for x in load_catalog(p.catalog)] == ["vpn-exemplo"]

    def test_nao_duplica_perfil_existente(self, tmp_path):
        p = paths_de_teste(tmp_path)
        apply_profile(PERFIL, senha="s3nha", paths=p)
        apply_profile(dict(PERFIL, nome="Rede A editada"), senha="s3nha", paths=p)

        perfis = load_catalog(p.catalog)
        assert len(perfis) == 1
        assert perfis[0].name == "Rede A editada"

    def test_preserva_outros_perfis_do_catalogo(self, tmp_path):
        p = paths_de_teste(tmp_path)
        apply_profile(PERFIL, senha="x", paths=p)
        apply_profile(dict(PERFIL, id="vpn-exemplo-2", nome="Rede B"), senha="x", paths=p)

        assert [x.id for x in load_catalog(p.catalog)] == ["vpn-exemplo", "vpn-exemplo-2"]


class TestOrdemEAtomicidade:
    def test_catalogo_e_escrito_por_ultimo(self, tmp_path):
        """O perfil só pode ficar visível quando os três artefatos existem.
        Se o catálogo fosse primeiro, uma falha depois deixaria a interface
        mostrando um perfil que não sobe."""
        p = paths_de_teste(tmp_path)
        vistos = []
        real = os.replace

        def espiao(src, dst):
            vistos.append(str(dst))
            return real(src, dst)

        apply_profile(PERFIL, senha="x", paths=p, replace=espiao)
        assert vistos[-1] == str(p.catalog)

    def test_falha_no_script_nao_deixa_conf_para_tras(self, tmp_path):
        """Rollback: nenhum dos três artefatos sobrevive a uma falha no meio."""
        p = paths_de_teste(tmp_path)
        real = os.replace

        def falha_no_script(src, dst):
            if "50vpnmgr" in str(dst):
                raise OSError("disco cheio")
            return real(src, dst)

        with pytest.raises(ApplyError):
            apply_profile(PERFIL, senha="x", paths=p, replace=falha_no_script)

        assert not (p.conf_dir / "vpn-exemplo.conf").exists()
        assert not p.catalog.exists()

    def test_falha_no_catalogo_restaura_o_estado_anterior(self, tmp_path):
        """Editar um perfil existente e falhar no último passo tem que devolver
        exatamente o .conf de antes — inclusive a senha antiga."""
        p = paths_de_teste(tmp_path)
        apply_profile(PERFIL, senha="senha-antiga", paths=p)
        antes = (p.conf_dir / "vpn-exemplo.conf").read_text()
        catalogo_antes = p.catalog.read_text()

        real = os.replace

        def falha_no_catalogo(src, dst):
            if str(dst) == str(p.catalog):
                raise OSError("disco cheio")
            return real(src, dst)

        with pytest.raises(ApplyError):
            apply_profile(
                dict(PERFIL, nome="Editado"),
                senha="senha-nova",
                paths=p,
                replace=falha_no_catalogo,
            )

        assert (p.conf_dir / "vpn-exemplo.conf").read_text() == antes
        assert p.catalog.read_text() == catalogo_antes

    def test_valida_tudo_antes_de_tocar_em_disco(self, tmp_path):
        """Perfil inválido não pode deixar arquivo pela metade."""
        p = paths_de_teste(tmp_path)
        with pytest.raises(ValidationError):
            apply_profile(dict(PERFIL, redes=["nao-e-rede"]), senha="x", paths=p)
        assert not p.conf_dir.exists() or not any(p.conf_dir.iterdir())


class TestRemove:
    def test_apaga_os_tres_artefatos(self, tmp_path):
        p = paths_de_teste(tmp_path)
        apply_profile(PERFIL, senha="x", paths=p)
        apply_profile(dict(PERFIL, id="vpn-exemplo-2"), senha="x", paths=p)

        remove_profile("vpn-exemplo", paths=p)

        assert not (p.conf_dir / "vpn-exemplo.conf").exists()
        assert not (p.ip_up_dir / "50vpnmgr-vpn-exemplo").exists()
        assert [x.id for x in load_catalog(p.catalog)] == ["vpn-exemplo-2"]

    def test_guarda_copia_no_undo_antes_de_apagar(self, tmp_path):
        p = paths_de_teste(tmp_path)
        apply_profile(PERFIL, senha="x", paths=p)
        remove_profile("vpn-exemplo", paths=p)

        guardados = list(p.undo_dir.rglob("vpn-exemplo.conf"))
        assert guardados, "o .conf removido tem que sobreviver no undo"

    def test_undo_nao_e_legivel_por_outros(self, tmp_path):
        """O snapshot carrega a senha em claro; herda o 600 do original."""
        p = paths_de_teste(tmp_path)
        apply_profile(PERFIL, senha="x", paths=p)
        remove_profile("vpn-exemplo", paths=p)

        for conf in p.undo_dir.rglob("vpn-exemplo.conf"):
            assert conf.stat().st_mode & 0o077 == 0

    def test_id_invalido_nao_apaga_nada(self, tmp_path):
        p = paths_de_teste(tmp_path)
        apply_profile(PERFIL, senha="x", paths=p)
        with pytest.raises(ValidationError):
            remove_profile("../../etc/passwd", paths=p)
        assert (p.conf_dir / "vpn-exemplo.conf").exists()
