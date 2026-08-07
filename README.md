# vpn-manager

Gerenciador de VPNs `openfortivpn` para desktop: mostra o estado de cada perfil (conectada, sem
rota, tunelada por processo externo, falhou, etc.) e oferece Conectar / Desconectar / Reconectar /
Adotar, tudo via `systemctl` autorizado por uma regra polkit — sem senha a cada clique.

Duas interfaces, dois processos, mais uma CLI:

- **Janela** (GTK4/libadwaita) — diagnóstico por perfil: estado, rotas e, no estado `falhou`, o
  log do systemd/journal (`journalctl -u openfortivpn@<perfil>`) num diálogo com botão de copiar.
  Os checks TCP do catálogo (`probe.run_check`) ainda não têm UI na janela — rodam pela CLI, ver
  abaixo.
- **Indicador de bandeja** (GTK3 + AyatanaAppIndicator3, processo separado) — ícone com o número de
  VPNs no ar e o estado agregado; abre a janela ao clicar.
- **CLI** (`python3 -m vpn_manager.cli`) — sem GUI: estado agregado de todos os perfis e os checks
  TCP de um perfil. É o único caminho para os checks TCP fora da janela e para diagnóstico numa
  máquina sem sessão gráfica (SSH, por exemplo). Ver seção "CLI" abaixo.

O núcleo (`catalog`, `probe`, `vpnctl`) não depende de GTK e é testável isoladamente; só `window.py`
e `indicator.py` importam `gi`. A CLI (`cli.py`) também não depende de `gi` — roda com qualquer
Python 3.12, inclusive dentro do `venv/` de desenvolvimento.

## Instalação

```bash
./install.sh
```

O script instala, nesta ordem:

1. `data/profiles.toml` em `/etc/vpn-manager/profiles.toml` (catálogo de perfis, `root:root 644`).
   **Não sobrescreve** um catálogo que já exista — se `/etc/vpn-manager/profiles.toml` já estiver lá
   (por exemplo, você já acrescentou uma quarta VPN à mão, ver seção abaixo), o script preserva o
   arquivo existente e grava a versão do repositório ao lado, em `profiles.toml.dist`, só para
   comparação (`diff /etc/vpn-manager/profiles.toml /etc/vpn-manager/profiles.toml.dist`).
2. `data/50-openfortivpn.rules` em `/etc/polkit-1/rules.d/50-openfortivpn.rules` — **precisa** ser
   `root:root 644`, senão o `polkitd` não lê o arquivo e toda ação volta a pedir senha.
3. Os lançadores `.desktop` em `~/.local/share/applications` (janela) e `~/.config/autostart`
   (indicador, autostart). O `Exec` de cada um é gravado com o caminho absoluto do projeto nesta
   máquina (`env PYTHONPATH=<projeto>/src python3 -m vpn_manager.window`, e o equivalente para o
   indicador) — **é o próprio `install.sh` quem preenche esse caminho na hora de instalar**, a
   partir dos modelos em `data/`. Se você mover o diretório do projeto depois de instalar, rode
   `./install.sh` de novo para regravar os dois lançadores com o caminho novo.

Pede `sudo` para os dois primeiros itens (arquivos de sistema); os lançadores vão para o `HOME`
do usuário, sem `sudo`.

Depois de instalar, confira a autorização — **sem** usar `systemctl start ... --dry-run`: esse verbo
não suporta `--dry-run` (o systemd ignora a flag silenciosamente e sobe a VPN de verdade, criando
exatamente a segunda instância em paralelo que este projeto existe para evitar). Use `pkcheck`, que
só consulta o polkit e não executa nada:

```bash
sudo pkcheck --action-id org.freedesktop.systemd1.manage-units \
  --process $$ --detail unit openfortivpn@vpn-exemplo.service && echo "polkit AUTORIZOU"
```

Precisa de `sudo` porque o polkit só aceita o parâmetro `--detail` vindo de um chamador confiável
(root) — `--process $$` continua apontando para o seu próprio processo, é só a consulta em si que
exige privilégio, nenhuma ação é executada.

Se pedir senha (ou `pkcheck` devolver negado), a regra não está sendo lida — confira dono/permissão
do arquivo (`ls -l /etc/polkit-1/rules.d/50-openfortivpn.rules`, tem que ser `-rw-r--r-- root root`)
e `journalctl -u polkit -n 20`.

### ADVERTÊNCIA DE MIGRAÇÃO

As VPNs que já estão em uso hoje rodam **fora do systemd** (processo `openfortivpn` solto, sem
unit). Passá-las para a unit `openfortivpn@<perfil>.service` — pelo botão **Adotar** da janela —
mata o processo avulso antes de subir a unit, e isso **derruba a conexão ativa**. Não faça essa
migração durante um atendimento em curso: espere uma janela sem uso da VPN em questão.

## Como rodar

`vpn_manager` vive em `src/`, não em `PYTHONPATH` nenhum por padrão — `python3 -m vpn_manager.window`
sozinho falha com `ModuleNotFoundError` mesmo rodando da raiz do projeto. A partir do diretório do
projeto, aponte o `PYTHONPATH` para `src`:

```bash
PYTHONPATH=src python3 -m vpn_manager.window       # janela
PYTHONPATH=src python3 -m vpn_manager.indicator    # indicador de bandeja
```

Depois de `./install.sh`, não precisa disso: o indicador sobe sozinho no login (autostart) e a
janela aparece no menu de aplicativos como "Gerenciador de VPNs" — os dois lançadores instalados já
embutem o `PYTHONPATH` absoluto (ver seção Instalação acima).

Não é `pip install`: o Ubuntu 24.04 bloqueia `pip install .`/`pip install --user .` no `python3` do
sistema (PEP 668, `externally-managed-environment`), e o `venv/` do projeto não serve para rodar a
GUI porque foi criado sem `--system-site-packages` — o `gi` (PyGObject/GTK) só existe no `python3` do
sistema, instalado via `apt`. Por isso janela e indicador sempre rodam com o `python3` do sistema
mais `PYTHONPATH`, nunca dentro do `venv` (esse é só para a suíte de testes, ver "Ambiente de
desenvolvimento" abaixo).

Requer GTK4 + libadwaita (janela) e GTK3 + `gir1.2-ayatanaappindicator3-0.1` (indicador) instalados
no sistema — não são dependências Python, são bindings GObject introspection.

## Acrescentando uma quarta VPN

Não mexe em código: edita `/etc/vpn-manager/profiles.toml` (o mesmo arquivo instalado pelo
`install.sh`) e adiciona um bloco `[[profile]]`:

```toml
[[profile]]
id          = "vpn-novo"                 # = nome do /etc/openfortivpn/<id>.conf e da unit systemd
nome        = "Nome exibido na UI"
proposito   = "Uma linha explicando pra que serve"
redes       = ["10.0.0.0/24"]            # sub-redes que devem aparecer roteadas quando a VPN sobe
checks      = [
  { host = "10.0.0.10", porta = 443, rotulo = "Serviço X" },
]
```

O `id` precisa bater com o nome do arquivo de configuração do `openfortivpn`
(`/etc/openfortivpn/vpn-novo.conf`) — é a partir dele que o app monta o nome da unit
(`openfortivpn@vpn-novo.service`). Depois de editar, não precisa reiniciar nada: janela e indicador
releem o catálogo a cada atualização de estado (janela: toda vez que ⟳/Ctrl+R/F5 é acionado, ou ao
fim de qualquer ação; indicador: a cada ciclo de 10s). Uma janela já aberta mostra a quarta VPN no
próximo desses gatilhos, sem precisar fechar e reabrir.

## CLI

`python3 -m vpn_manager.cli` roda sem GUI — útil para diagnóstico via SSH (sem sessão gráfica) e é
o único jeito de rodar os checks TCP do catálogo fora da janela:

```bash
PYTHONPATH=src python3 -m vpn_manager.cli status          # estado agregado de todos os perfis
PYTHONPATH=src python3 -m vpn_manager.cli check vpn-exemplo   # checks TCP de um perfil específico
```

`status` imprime uma linha por perfil (nome, estado, interface, redes faltando e PIDs externos
quando aplicável) e marca `[LEITURA DEGRADADA]` quando `systemctl`/`ip route` falharam ou estouraram
timeout nesta consulta — o mesmo sinal que a janela usa pra suprimir Adotar/Reconectar (ver
`ProfileStatus.read_ok` em `vpnctl.py`). `check <perfil>` roda os checks TCP (`probe.run_check`)
definidos no catálogo para aquele perfil e imprime aberto/recusado/tempo esgotado por destino.

Não depende de `gi`/GTK — roda com o `python3` do sistema ou com o `venv/` de desenvolvimento
(ver "Ambiente de desenvolvimento" abaixo), sem precisar instalar bindings GObject.

## Ambiente de desenvolvimento

O `python3` do sistema não tem `pytest`; a suíte só roda dentro de um virtualenv do projeto. Crie
um (é git-ignorado, cada clone faz o seu):

```bash
python3 -m venv venv
./venv/bin/pip install -e '.[dev]'
```

E rode a suíte:

```bash
./venv/bin/python -m pytest tests/ -v
```

O núcleo (`catalog`, `probe`, `vpnctl`) é testado sem GTK e sem tocar em VPNs reais — todo acesso a
`systemctl`/`journalctl`/rede é injetado por parâmetro (`run=...`) e substituído por fake nos testes.
