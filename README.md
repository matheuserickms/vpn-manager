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

### Se você já tem VPNs rodando à mão

Um `openfortivpn` iniciado no terminal (`sudo openfortivpn -c ...`) aparece como
**Fora do systemd**. O botão **Adotar** o passa para a unit
`openfortivpn@<perfil>.service`, mas para isso mata o processo avulso antes de
subir a unit — o que **derruba a conexão ativa**. Não faça durante um
atendimento em curso.

Depois de adotado, o perfil sobe com `systemctl start openfortivpn@<perfil>`,
não segura o terminal e o log fica no `journalctl -u`.

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

## Acrescentando uma VPN

Pela interface: botão **+** no cabeçalho da janela. O diálogo pede o gateway, o
usuário, a senha, as redes que devem aparecer roteadas e as portas a verificar —
e grava os três arquivos que um perfil precisa:

| Arquivo | Para quê |
|---|---|
| `/etc/openfortivpn/<id>.conf` | credenciais e opções do openfortivpn (root:root 600) |
| `/etc/ppp/ip-up.d/50vpnmgr-<id>` | instala as rotas quando o túnel sobe |
| `/etc/vpn-manager/profiles.toml` | o que a interface mostra e verifica |

Escrever nesses caminhos exige root, então salvar pede a senha de administrador
uma vez (`auth_admin_keep` — salvar um perfil não vira três prompts). Conectar e
desconectar seguem sem senha: a pior consequência ali é um túnel a mais ou a
menos, enquanto escrever em `ip-up.d` é **execução de código como root a cada
conexão**.

Restrições que a interface impõe de propósito:

- **`id`** aceita 2 a 32 caracteres, minúsculas, dígitos e hífen. Sem ponto:
  `run-parts` ignora silenciosamente arquivos com ponto no nome, e o script de
  rotas nunca rodaria — o perfil subiria sem rota, sem erro nenhum.
- **`set-routes`, `set-dns` e `pppd-ipparam`** não são editáveis. O app verifica
  se as rotas esperadas apareceram na interface do túnel; `set-routes = 1`
  quebraria esse contrato inteiro.
- **Renomear** não existe. Mudaria o nome da unit, do `.conf` e do script de uma
  vez; um perfil conectado ficaria órfão. Remova e crie de novo.
- **Remover** exige digitar o `id`. É a única ação sem volta da interface.
- **Editar um perfil conectado** funciona, mas o túnel de pé continua com a
  configuração antiga em memória. O diálogo oferece reconectar — sem isso, a
  edição não tem efeito.

### Perfis que já existiam

Um `.conf` escrito à mão continua funcionando como sempre. Para editá-lo pela
interface, use **assumir gerenciamento**: as diretivas que o app não conhece
(`persistent`, `half-internet-routes`, o que houver) são preservadas verbatim, e
a senha do arquivo é reaproveitada — você não precisa lembrá-la.

O script de rotas antigo é movido para `/var/lib/vpn-manager/undo/`, mediante
confirmação pelo nome, para não ficarem dois scripts instalando rota no mesmo
túnel. A detecção é por menção ao `ipparam`, que é o guard que todo script de
`ip-up.d` usa — um script que instala rota sem citar o perfil passa despercebido
e continua rodando ao lado do novo.

Assumir exige o perfil desconectado: o `ipparam` antigo segue na memória do
processo vivo e, com `persistent`, um redial voltaria com a configuração velha.

### Onde fica a senha, e o que isso protege

No `.conf`, em texto puro, root:root 600 — como sempre foi. A alternativa óbvia
(keyring da sessão) seria **pior** contra a ameaça mais realista: qualquer
processo rodando como você lê o keyring destravado, enquanto o `.conf` exige
root. E a unit sobe como root, fora da sessão, onde não há keyring nenhum.

O que isso **não** protege: alguém que roube o notebook e leia o disco. Essa é
responsabilidade do **LUKS**, não deste aplicativo. Se o disco não estiver
cifrado, a senha da VPN está legível para quem tiver o hardware em mãos.

Editar manualmente também continua possível — o app relê o catálogo a cada
atualização de estado (janela: ⟳/Ctrl+R/F5 ou fim de qualquer ação; indicador: a
cada 10 s), então uma janela aberta mostra o perfil novo no próximo gatilho.

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
