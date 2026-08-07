import pytest
from pathlib import Path
from vpn_manager.catalog import load_catalog, CatalogError
from vpn_manager.models import Profile, Check


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "profiles.toml"
    p.write_text(text, encoding="utf-8")
    return p


def test_carrega_perfil_completo(tmp_path):
    path = write(tmp_path, """
[[profile]]
id = "vpn-exemplo"
nome = "Rede A"
proposito = "Serviço interno"
redes = ["10.0.0.0/24"]
checks = [
  { host = "10.0.0.10", porta = 443, rotulo = "Serviço X" },
]
""")
    profiles = load_catalog(path)
    assert len(profiles) == 1
    p = profiles[0]
    assert p == Profile(
        id="vpn-exemplo",
        name="Rede A",
        purpose="Serviço interno",
        networks=("10.0.0.0/24",),
        checks=(Check(host="10.0.0.10", port=443, label="Serviço X"),),
    )


def test_perfil_sem_id_e_erro(tmp_path):
    path = write(tmp_path, """
[[profile]]
nome = "Sem id"
proposito = "x"
redes = []
checks = []
""")
    with pytest.raises(CatalogError, match="id"):
        load_catalog(path)


def test_check_malformado_e_erro(tmp_path):
    path = write(tmp_path, """
[[profile]]
id = "a"
nome = "A"
proposito = "x"
redes = []
checks = [ { host = "1.2.3.4" } ]
""")
    with pytest.raises(CatalogError, match="porta"):
        load_catalog(path)


def test_arquivo_ausente_e_erro(tmp_path):
    with pytest.raises(CatalogError, match="não encontrado"):
        load_catalog(tmp_path / "nao-existe.toml")


def test_toml_invalido_e_erro(tmp_path):
    path = write(tmp_path, "isto não é toml [[[")
    with pytest.raises(CatalogError):
        load_catalog(path)
