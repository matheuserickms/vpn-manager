#!/bin/bash
# install.sh — instala os arquivos de sistema do vpn-manager.
set -euo pipefail
cd "$(dirname "$0")"
PROJECT_DIR="$(pwd)"
SRC_DIR="$PROJECT_DIR/src"

echo "==> catálogo de perfis"
sudo install -d -m 755 /etc/vpn-manager
# O catálogo real vive em /etc/vpn-manager/profiles.toml e NÃO é versionado:
# ele contém as redes e hosts internos de quem usa. O repositório carrega só o
# exemplo genérico.
if [ -e /etc/vpn-manager/profiles.toml ]; then
  echo "    /etc/vpn-manager/profiles.toml já existe — preservando."
  sudo install -m 644 data/profiles.example.toml /etc/vpn-manager/profiles.example.toml
  echo "    exemplo do repositório gravado ao lado, para comparar campos novos:"
  echo "        diff /etc/vpn-manager/profiles.toml /etc/vpn-manager/profiles.example.toml"
else
  sudo install -m 644 data/profiles.example.toml /etc/vpn-manager/profiles.toml
  echo "    catálogo criado a partir do EXEMPLO — os perfis são fictícios."
  echo "    edite antes de usar:  sudo nano /etc/vpn-manager/profiles.toml"
fi

echo "==> regra polkit (root:root 644, exigido pelo polkitd)"
sudo install -o root -g root -m 644 \
  data/50-openfortivpn.rules /etc/polkit-1/rules.d/50-openfortivpn.rules

echo "==> lançadores (Exec aponta para $SRC_DIR via PYTHONPATH — mover o projeto de"
echo "    lugar exige rodar ./install.sh de novo para regravar os dois arquivos)"
install -d -m 755 ~/.local/share/applications ~/.config/autostart
TMP_DESKTOP="$(mktemp)"
TMP_AUTOSTART="$(mktemp)"
trap 'rm -f "$TMP_DESKTOP" "$TMP_AUTOSTART"' EXIT
sed "s#__VPN_MANAGER_SRC__#$SRC_DIR#g" \
  data/br.dev.matheus.VpnManager.desktop > "$TMP_DESKTOP"
sed "s#__VPN_MANAGER_SRC__#$SRC_DIR#g" \
  data/vpn-manager-indicator.desktop > "$TMP_AUTOSTART"
install -m 644 "$TMP_DESKTOP" ~/.local/share/applications/br.dev.matheus.VpnManager.desktop
install -m 644 "$TMP_AUTOSTART" ~/.config/autostart/vpn-manager-indicator.desktop

echo
echo "Instalado. Teste a autorização SEM subir nada de verdade — atenção:"
echo "\"systemctl start ... --dry-run\" NÃO é suportado para o verbo start (a flag é"
echo "silenciosamente ignorada e uma segunda VPN sobe de verdade). Use pkcheck, que só"
echo "consulta o polkit sem executar nada; precisa de sudo porque --details só é aceito"
echo "de um chamador confiável, mesmo consultando o SEU processo (\$\$):"
echo "    sudo pkcheck --action-id org.freedesktop.systemd1.manage-units \\"
echo "      --process \$\$ --detail unit openfortivpn@vpn-exemplo.service && echo \"polkit AUTORIZOU\""
echo
echo "ATENÇÃO — migração: as VPNs em uso hoje rodam FORA do systemd."
echo "Passá-las para as units DERRUBA as conexões ativas. Faça isso fora"
echo "de um atendimento em curso, usando o botão Adotar de cada perfil."
