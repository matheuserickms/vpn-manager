# Configuração de perfis pela interface — design

Data: 2026-08-07
Status: proposta, aguardando decisões humanas da §14
Escopo: criar, editar e remover um perfil openfortivpn **completo** pela UI —
`.conf`, script de rotas em `ip-up.d` e entrada no catálogo — sem terminal.

Todos os identificadores deste documento são fictícios (`vpn-exemplo`,
`10.0.0.0/24`, `10.0.0.10`, `vpn.exemplo.com`, `usuario.exemplo`).

---

## 1. Problema e motivação

Hoje o app é estritamente leitor: `catalog.load_catalog()` lê
`/etc/vpn-manager/profiles.toml`, `vpnctl.status_of()` lê systemd/`/proc`/rotas,
e as únicas escritas que existem passam por `systemctl` (autorizado pela regra
polkit de `data/50-openfortivpn.rules`) e por um `pkexec kill` dentro de
`vpnctl.adopt()`. Criar um perfil novo exige editar três arquivos como root:

1. `/etc/openfortivpn/vpn-exemplo.conf` — gateway, credenciais, `trusted-cert`,
   `set-routes = 0`, `set-dns = 0`, `pppd-ipparam = vpn-exemplo`, `persistent = 10`;
2. `/etc/ppp/ip-up.d/50vpnmgr-vpn-exemplo` — script executável que injeta as
   rotas por rede (necessário **porque** `set-routes = 0`);
3. `/etc/vpn-manager/profiles.toml` — o `[[profile]]` com redes e checks.

Os três precisam concordar entre si, e é exatamente essa concordância que o
processo manual não garante: o guard do script (`$6 = ipparam`) tem que bater
com o `pppd-ipparam` do `.conf`; as redes do script têm que bater com as
`redes` do catálogo (senão o app pinta `parcial` para sempre ou, pior, pinta
`ativo` sem alcance real); o `id` do catálogo tem que bater com o nome do
`.conf`. Um typo em qualquer elo produz um estado degradado sem diagnóstico.

A motivação central do design, portanto, não é só "não abrir terminal": é
**derivar os três artefatos de uma fonte única** (o formulário), eliminando por
construção a classe de erro "artefatos que discordam entre si".

## 2. Visão geral

```
  processo do usuário (GTK4)                    processo root (pkexec)
┌──────────────────────────────┐             ┌──────────────────────────────┐
│ window.py                    │             │ vpn-manager-helper           │
│  └─ editor.py (diálogo)      │   JSON no   │  (cópia congelada, root:root,│
│      └─ profile_client.py ───┼── stdin ───►│   fora da árvore do usuário) │
│          (valida, monta      │   JSON no   │  └─ profile_store.py         │
│           request, pkexec)   │◄── stdout ──┼      valida DE NOVO,         │
│                              │             │      snapshot → escreve →    │
│ validação aqui é só UX;      │             │      rollback se falhar      │
│ a fronteira de segurança     │             │                              │
│ é o helper                   │             │ escreve APENAS:              │
└──────────────────────────────┘             │  /etc/openfortivpn/<id>.conf │
                                             │  /etc/ppp/ip-up.d/50vpnmgr-* │
                                             │  /etc/vpn-manager/profiles.  │
                                             │    toml                      │
                                             └──────────────────────────────┘
```

Componentes novos:

| Componente | Onde vive | Depende de GTK? | Papel |
|---|---|---|---|
| `profile_store.py` | núcleo (`src/vpn_manager/`) | não | validação, (de)serialização dos 3 artefatos, plano de aplicação com snapshot/rollback. Todos os caminhos injetáveis. |
| `helper_main.py` | núcleo; **instalado como cópia congelada** em `/usr/local/lib/vpn-manager/` | não | `main()` fino: lê JSON do stdin, chama `profile_store`, responde JSON no stdout. É o único código que roda como root. |
| `profile_client.py` | núcleo | não | lado do usuário: monta o request, invoca `pkexec /usr/local/libexec/vpn-manager-helper`, traduz erros (reusa `vpnctl._mensagem_autorizacao_negada`). `run=subprocess.run` injetável, como todo o resto. |
| `editor.py` | UI | sim | diálogo de criar/editar; lógica de formulário extraída em funções puras testáveis. |
| `data/br.dev.matheus.VpnManager.policy` | polkit | — | action própria para o helper (ver §5). |

Mudanças em componentes existentes: `window.py` ganha botão "+" no header,
menu por linha e o botão "Configurar…" no estado `nao_configurado`;
`install.sh` instala helper, launcher do helper e `.policy`; `catalog.py` e
`models.py` não mudam (o editor escreve o mesmo formato que `load_catalog` lê
— e o teste de round-trip garante isso, ver §12).

## 3. Decisões de arquitetura

### 3.1 Helper privilegiado com interface estreita — e por que não as alternativas

**Decisão: um executável dedicado, invocado por `pkexec`, com action polkit
própria, que só sabe fazer cinco operações sobre três caminhos fixos.**

Alternativas consideradas e descartadas:

- **`pkexec tee`/`cp`/`install` por arquivo.** É conceder "escreva qualquer
  arquivo como root" à sessão — a action `org.freedesktop.policykit.exec`
  genérica não delimita nada. Inaceitável; nem com prompt de senha, porque o
  usuário não tem como auditar o que está autorizando.
- **Estender a regra `50-openfortivpn.rules` atual.** A regra autoriza
  `org.freedesktop.systemd1.manage-units` para units `openfortivpn@*.service`
  — não existe action polkit "escrever em /etc". Polkit autoriza *actions*,
  não arquivos; sem um helper que **defina** a action, não há o que autorizar.
- **Daemon D-Bus residente com polkit por método.** É o desenho "correto" de
  livro (é como o NetworkManager faz), mas exige um serviço de sistema
  permanente, ativação por D-Bus, arquivo de política de barramento — umas
  centenas de linhas de infraestrutura para três arquivos de texto que mudam
  algumas vezes por ano. O estudo `docs/melhorias-2026-08-07.md` §2.1 tem
  razão: não inventar daemon para 3 VPNs. O helper efêmero via pkexec dá a
  mesma fronteira de privilégio sem processo residente. Se um dia o §4.1
  daquele estudo (núcleo residente) acontecer, o helper migra para lá.
- **`sudo` com sudoers dedicado.** Foge do mecanismo que o projeto já usa
  (polkit, com agente gráfico do GNOME sob Wayland) e cria uma segunda
  autoridade de configuração. Descartado por consistência.

### 3.2 O helper NÃO pode rodar da árvore do usuário

Hoje os lançadores fazem `env PYTHONPATH=<projeto>/src python3 -m ...` — a
árvore git viva. Para a janela, isso é a armadilha de conveniência que o
estudo de melhorias (§3.3) já apontou. Para um **executável root**, é uma
escalada de privilégio pronta: se o pkexec apontar para um script no `$HOME`,
qualquer processo da sessão edita o arquivo e ganha root no próximo "Salvar".

Portanto: `install.sh` copia `profile_store.py` + `helper_main.py` para
`/usr/local/lib/vpn-manager/` (root:root, 755 dir / 644 arquivos) e instala um
launcher de ~5 linhas em `/usr/local/libexec/vpn-manager-helper` (root:root
755) que fixa `sys.path` **apenas** nessa cópia congelada. O `.policy` aponta
para esse caminho via anotação `org.freedesktop.policykit.exec.path`. Nada do
que roda como root pode ser gravável pelo usuário — essa é a invariante nº 1
do design, e ela tem um custo honesto: mudou `profile_store.py`, tem que
rodar `./install.sh` de novo. Aceito; é o mesmo contrato que os lançadores já
documentam.

### 3.3 Fonte única de verdade e artefatos gerados

O formulário produz um único objeto (request JSON, §6). O helper **gera** os
três artefatos a partir dele:

- o `.conf` sai de um serializador próprio (chave = valor por linha);
- o script de `ip-up.d` sai de um template fixo, com guard
  `[ "$6" = "<id>" ] || exit 0` e uma linha `ip route replace <rede> dev "$1"`
  por rede **do catálogo** — as duas listas não podem divergir porque são a
  mesma lista;
- o `[[profile]]` sai de um serializador TOML mínimo próprio (stdlib não tem
  writer; ver §12 para o teste de round-trip contra `tomllib`).

Consequência opinativa: para perfis gerenciados, `set-routes = 0`,
`set-dns = 0` e `pppd-ipparam = <id>` **não são editáveis**. Todo o modelo de
estado do app (`missing_networks` exigindo rota na interface do túnel,
`parcial`, o script por rede) assume rotas explícitas; um perfil com
`set-routes = 1` quebraria o contrato que `probe.py` verifica. A UI não
oferece a opção errada.

## 4. Onde fica a senha — decisão e defesa

**Decisão: (a) texto puro no `.conf`, root:root 600 — como é hoje.**

Análise das três opções contra o modelo de ameaça real (notebook de trabalho,
um usuário, disco preferencialmente com LUKS):

| Critério | (a) `.conf` 600 | (b) systemd-creds + TPM | (c) libsecret/keyring |
|---|---|---|---|
| Protege contra processo malicioso na sessão do usuário | sim (arquivo é root-only) | sim | **não** — qualquer processo da sessão lê o keyring destravado |
| Protege contra root local | não | **não** — root roda `systemd-creds decrypt` à vontade; o TPM decifra para quem tiver root na mesma máquina | não |
| Protege contra roubo do disco desligado | não (é o LUKS que protege) | sim | sim (keyring cifrado pela senha de login) |
| VPN sobe no boot / fora da sessão | sim | sim | **não** — a unit roda como root via systemd; não existe sessão nem keyring destravado nesse contexto |
| openfortivpn consome direto | sim (`password =`) | não — exigiria wrapper que monta config em `/run` a partir de `$CREDENTIALS_DIRECTORY` (expor via argv vazaria em `/proc/*/cmdline`) | não — exigiria injetar senha no start, reescrevendo o caminho `systemctl start` inteiro |
| Portabilidade do perfil entre máquinas | total (copiar arquivo) | **zero** — credencial atada ao TPM daquela placa; troca de firmware/placa = credencial ilegível e VPN morta em silêncio | média (re-cadastrar no keyring novo) |
| Custo no instalador | zero | drop-in por instância + verificação de TPM + fluxo de re-cifragem | dependência nova + fluxo de primeira senha |
| Custo no código | ~0 | alto | alto e invasivo (muda `vpnctl.start`) |
| Compatível com os `.conf` manuais existentes | sim, formato idêntico | não (migração obrigatória) | não |

O ponto decisivo: **(c) é pior que (a) para a ameaça mais realista** (código
malicioso rodando como o usuário — um `pip install` envenenado, uma extensão
de navegador). O keyring da sessão é legível por qualquer processo do
usuário; o `.conf` 600 exige root. E (b) só adiciona proteção num cenário
(disco frio) que o LUKS já cobre melhor, cobrando o preço permanente de
perfis intransportáveis e de um modo de falha novo e silencioso (TPM muda,
VPN para de subir sem mensagem útil). (b) também não protege de root — quem
tem root decifra a credencial na própria máquina, então a "cifra" compra
quase nada contra os ataques que sobram.

**Riscos que estou aceitando com (a), explicitamente:**

1. Backup de `/etc` não cifrado carrega a senha em claro. Mitigação fora do
   app: cifrar backups. O documento do perfil exportado pela UI (se um dia
   existir) nunca inclui a senha.
2. Root local lê a senha. Todas as três opções falham aqui; não é
   diferenciador.
3. Sem LUKS, roubo do notebook expõe a senha. A mitigação certa é LUKS, não
   TPM por credencial; o README deve dizer isso com todas as letras.

Regras de manuseio no código, para a decisão (a) não apodrecer:

- a senha viaja do `Gtk.PasswordEntry` ao helper **só via stdin do pkexec**
  (nunca argv — `/proc/*/cmdline` é público; nunca arquivo temporário);
- o verbo `read` do helper (§6) devolve os campos do `.conf` **sem** a senha
  (sentinela `"__mantida__"`); a UI mostra o campo vazio com placeholder
  "manter a atual" — a senha nunca volta ao processo sem privilégio;
- no update, senha ausente/sentinela = preservar a linha `password` atual.

## 5. Modelo de segurança

### 5.1 Action polkit própria, com senha de admin

Nova action `br.dev.matheus.vpn-manager.manage-profiles` num `.policy`
instalado em `/usr/share/polkit-1/actions/`, com
`allow_active = auth_admin_keep` e anotação
`org.freedesktop.policykit.exec.path = /usr/local/libexec/vpn-manager-helper`.

Opinião firme: **escrever como root em /etc deve pedir a senha de admin; subir
e derrubar unit continua sem senha.** A regra atual dá clique-sem-senha para
`start`/`stop` porque a pior consequência é derrubar/subir um túnel — chato,
reversível. Escrever em `/etc/ppp/ip-up.d/` é **execução de código como root
a cada conexão**; mesmo com o helper validando conteúdo, um `YES` silencioso
significaria que qualquer código rodando na sessão cria/edita perfis (e
redireciona um gateway para `vpn.exemplo.com` do atacante) sem o usuário ver
nada. `auth_admin_keep` mantém a autorização por alguns minutos, então o
fluxo "salvar perfil = 1 prompt" não vira "3 prompts". Criar/editar perfil é
operação de baixa frequência; um prompt é preço justo pela visibilidade.
(Contraponto registrado para a decisão humana da §14: se o dono achar o
prompt inaceitável, a alternativa é uma regra `rules.d` com `subject.user`,
como a das units — mas aí o modelo de ameaça da sessão fica igual ao do
keyring da §4, e eu registro discordância.)

### 5.2 O helper é a fronteira — validação dupla

A validação no `profile_client.py`/`editor.py` é conforto de UX (erro
imediato no formulário). A validação que conta acontece **dentro do helper**,
que não confia em nada vindo do stdin: o chamador pode ser qualquer processo
da sessão que convenceu o polkit (ou o próprio app com bug). O helper:

- só escreve nos três caminhos fixos, derivados do `id` validado — não existe
  parâmetro de caminho no protocolo;
- revalida cada campo com as regras da §10 **antes** de tocar em disco;
- rejeita request com campos desconhecidos (fail-closed; versão do protocolo
  no request para evoluir sem ambiguidade);
- limita o request a 64 KiB e o número de redes/checks (32 cada) — ninguém
  tem 33 redes num perfil; limite barato contra abuso;
- escreve com `O_NOFOLLOW`/`os.replace` de arquivo temporário criado no mesmo
  diretório (nunca segue symlink pré-plantado; `os.replace` substitui o nome,
  não o alvo do link);
- serializa a si mesmo com `flock` em `/run/vpn-manager-helper.lock` (duas
  janelas salvando ao mesmo tempo não intercalam escritas).

### 5.3 Vetores de injeção considerados

| Vetor | Ataque | Defesa |
|---|---|---|
| `id` do perfil | `../../cron.d/x` vira escrita fora dos diretórios | regex fechado da §10 (sem `/`, `.`, espaço); o `id` é o único dado que entra em caminho |
| rede "CIDR" | `10.0.0.0/24; rm -rf /` interpolado no script de ip-up.d, que roda como root | a rede é parseada com `ipaddress.ip_network(strict=True)` e o script recebe `str(rede_parseada)` — nunca o texto do usuário. O que não parseia, não existe |
| campos livres (`nome`, `proposito`, `rotulo`) | fechar aspas no TOML e injetar um `[[profile]]` extra, ou quebrar linha no `.conf` e injetar diretiva | serializador TOML próprio escapa `"`/`\`/controle; **proibido `\n` e caracteres de controle em qualquer campo** (rejeição, não escape, no `.conf` — formato linha-a-linha não tem escape confiável) |
| `username`/`senha` | `\n` injetando diretiva no `.conf` (ex.: uma linha `pppd-ipparam` maliciosa) | mesmo bloqueio de controle/`\n`; senha admite qualquer outro byte imprimível |
| `host` do gateway/check | idem + confusão de parser | regex de hostname RFC 1123 ou `ip_address()`; porta `int` 1–65535 |
| request inteiro | campos extras que uma versão futura interprete diferente | versão explícita + rejeição de chave desconhecida |
| binário/launcher do helper | trocar o código que roda como root | cópia congelada root:root fora do `$HOME` (§3.2); `.policy` com `exec.path` fixo |

## 6. Protocolo do helper

Cinco verbos, request JSON no stdin, response JSON no stdout, exit code 0/1.
Ilustrativo (não normativo):

```json
{"versao": 1, "op": "create",
 "perfil": {"id": "vpn-exemplo", "nome": "Rede A", "proposito": "…",
            "gateway": {"host": "vpn.exemplo.com", "porta": 443},
            "usuario": "usuario.exemplo", "senha": "…",
            "trusted_cert": "<hash>",
            "redes": ["10.0.0.0/24"],
            "checks": [{"host": "10.0.0.10", "porta": 443, "rotulo": "Serviço X"}]}}
```

- `create <perfil>` — falha se `id` já existe no catálogo **ou** se
  `/etc/openfortivpn/<id>.conf` já existe (colisão com perfil manual não
  catalogado é erro, não sobrescrita).
- `read <id>` — devolve os campos gerenciados parseados do `.conf` (senha
  como sentinela), as linhas não reconhecidas verbatim, e se o arquivo tem o
  marcador de gerenciamento (§8). É o que alimenta o diálogo de edição —
  o app sem privilégio não lê arquivos 600.
- `update <id> <perfil>` — reescreve os três artefatos; preserva senha
  (sentinela) e linhas não gerenciadas (§8). `id` é imutável; renomear está
  fora de escopo (§13).
- `delete <id>` — recusa se a unit está ativa ou se há processo externo do
  perfil (o helper mesmo verifica, `systemctl show` + varredura tipo
  `_is_openfortivpn_for_profile` — não confia no estado que o chamador viu);
  remove catálogo → script → conf, nessa ordem (§7).
- `assume <id> <perfil>` — migração assistida de perfil manual (§8).

Respostas de erro são estruturadas
(`{"ok": false, "erro": "rede_invalida", "detalhe": "…", "campo": "redes[2]"}`)
para o formulário marcar o campo certo, não só um toast genérico.

## 7. Atomicidade e rollback

Não existe rename atômico atravessando três arquivos em três diretórios;
o que dá para garantir é **tudo-ou-nada observável** com snapshot + ordem de
visibilidade:

1. **Validar tudo** antes de tocar em qualquer arquivo (a maioria das falhas
   morre aqui, com zero efeito colateral).
2. **Snapshot**: copiar os artefatos atuais afetados para
   `/var/lib/vpn-manager/undo/<timestamp>/` com um manifest do que existia
   (inclusive "não existia"). Também serve de backup de último recurso para o
   usuário.
3. **Escrever**: para cada artefato, temp file no mesmo diretório
   (`.tmp-<pid>`), `fsync`, `os.replace`. Ordem de criação/edição:
   `.conf` → script → **catálogo por último**. O catálogo é o que torna o
   perfil visível ao app (`load_catalog` → janela/indicador); se falhar no
   segundo passo, sobra um `.conf` órfão invisível — inerte e detectável —
   nunca um perfil visível apontando para artefatos ausentes (que viraria
   `nao_configurado` ou, pior, `parcial` eterno).
   Na **remoção**, ordem inversa: catálogo primeiro (perfil some da UI),
   depois script, depois conf.
4. **Rollback**: qualquer falha no passo 3 restaura o snapshot na ordem
   inversa do que já foi aplicado e devolve erro estruturado. Falha durante o
   próprio rollback (disco cheio, por ex.) devolve `erro: "inconsistente"`
   com o caminho do snapshot — o helper nunca finge sucesso parcial.
5. O estado transitório pior possível (morte por SIGKILL no meio do passo 3)
   deixa: artefatos novos + catálogo velho = perfil órfão invisível, ou
   catálogo novo + artefatos já escritos = consistente. Nunca "catálogo novo,
   conf ausente". Um verbo `doctor` futuro (o `cli doctor` do estudo de
   melhorias) pode listar órfãos; não bloqueia este design.

Sem `daemon-reload` em nenhum caso: units são instâncias de template e os
três arquivos são lidos no start do processo, não pelo systemd.

## 8. Migração: perfis manuais existentes

Princípio: **não destruir o que o app não entende.** Os perfis de hoje foram
escritos à mão, podem ter opções que a UI não conhece e — caso real — o
`pppd-ipparam` deles não é igual ao `id` (o script chama-se
`/etc/ppp/ip-up.d/50nomequalquer` com guard próprio).

- Arquivos criados pelo app levam marcador na primeira linha
  (`# gerenciado pelo vpn-manager — edite pela interface`). Presença do
  marcador = pode reescrever; ausência = perfil manual.
- **Editar perfil manual** abre o diálogo em modo "assumir gerenciamento":
  os campos reconhecidos vêm preenchidos (via `read`), as linhas não
  reconhecidas do `.conf` aparecem numa seção "Opções preservadas" somente
  leitura, e o texto explica o que vai acontecer. No `assume`/`update`, o
  helper reescreve os campos gerenciados e **reproduz as linhas não
  reconhecidas verbatim** num bloco demarcado no fim do `.conf`. Nunca as
  descarta.
- O script de ip-up.d manual é o caso espinhoso: o helper gera o script
  gerenciado novo (`50vpnmgr-<id>`, guard pelo `id`) e reescreve
  `pppd-ipparam = <id>` no `.conf` — o script velho, com guard pelo ipparam
  antigo, ficaria morto mas **ainda executável pelo run-parts** se outro
  perfil usar o mesmo ipparam. Então o `assume` lista o(s) script(s) cujo
  guard casa com o ipparam antigo, pede confirmação nominal na UI e os
  **move** para o diretório de undo (mover para fora de `ip-up.d` é
  obrigatório; renomear no lugar não bastaria — e nomes com `.` o run-parts
  ignora, mas não conte com isso).
- **`assume` exige o perfil desconectado.** Motivo concreto: o processo
  conectado carrega o ipparam antigo em memória; com `persistent`, um redial
  re-executa os scripts com o ipparam antigo — que acabou de ser removido —
  e o túnel volta sem rotas (`parcial`) até um restart de verdade. O diálogo
  oferece "Desconectar e assumir" como ação única.
- Detecção ambígua (nenhum script casa, ou dois casam): o helper não adivinha
  — devolve a lista e a UI mostra "não consegui identificar o script de rotas
  antigo; ele será preservado, remova manualmente depois". `ip route replace`
  é idempotente; script velho sobrando produz rota duplicada inofensiva, não
  quebra.

## 9. Edição de perfil conectado

Fatos: o `.conf` é lido pelo openfortivpn **no start**; o script de ip-up.d
roda no próximo evento de up. Editar não muda nada do túnel corrente — e é
assim que deve ser: salvar nunca pode derrubar conexão como efeito colateral.

- Salvar num perfil `ativo`/`parcial` termina com um diálogo de duas saídas:
  **"Salvar"** (aplica nos arquivos; vale na próxima conexão) e
  **"Salvar e reconectar"** (encadeia `vpnctl.restart` — que já existe e já
  é assíncrono — após o helper responder ok).
- Se apenas `redes`/`checks` mudaram, o efeito na janela é imediato e honesto:
  uma rede recém-adicionada aparece "Faltando" (a rota de fato não existe no
  túnel corrente) — que é exatamente o empurrão certo para "Reconectar".
  O diálogo avisa isso em vez de deixar parecer bug.
- `delete` recusa perfil conectado ou com processo externo (§6) — a UI nem
  oferece "Remover" nesses estados; a recusa do helper é a segunda linha.
- Perfil `externo` (pré-migração via Adotar): edição bloqueada com explicação
  — mexer nos arquivos de um túnel que o app não controla nem consegue
  reconectar limpo é convite a estado inconsistente. Adote primeiro.

## 10. Validação (regras únicas, usadas nos dois lados)

Uma função por campo em `profile_store.py`, importada pelo editor (UX) e pelo
helper (segurança) — nunca duas implementações.

| Campo | Regra | Motivo |
|---|---|---|
| `id` | `^[a-z0-9][a-z0-9-]{0,30}[a-z0-9]$` (ou 1 char alfanumérico) | vira caminho (`<id>.conf`, unit, script): sem `/` nem `..` por construção. Sem `.`: o run-parts **ignora silenciosamente** arquivos com ponto — o perfil conectaria sem rotas. Sem `_`/maiúsculas por higiene. Hífen é permitido **porque** o drop-in `%i` está instalado (sem ele, `%I` desescapa `-`→`/` e o perfil morre com exit 254, ver comentário no `install.sh`); o helper confere a existência de `/etc/systemd/system/openfortivpn@.service.d/override.conf` e recusa `id` com hífen se o drop-in sumiu, com mensagem apontando `./install.sh`. Subconjunto do regex da regra polkit (`[\w.-]+`), então a autorização de unit continua casando |
| colisão | `id` inédito no catálogo E `.conf` inexistente (create) | não sobrescrever perfil manual desconhecido |
| `gateway.host` / `checks[].host` | `ipaddress.ip_address()` ou hostname RFC 1123 (labels `[a-z0-9-]`, sem espaço/controle) | vai para o `.conf` e para `socket.create_connection` |
| portas | int 1–65535 | idem |
| `redes` | `ip_network(strict=True)`, IPv4, 1–32 itens, sem duplicata; serializa a forma canônica | é interpolada num script executado como root (§5.3); `strict` rejeita `10.0.0.1/24` que produziria rota diferente da intenção |
| `nome`, `proposito`, `rotulo` | não vazio, ≤ 120 chars, sem `\n`/controle | vão para TOML e para a UI |
| `usuario`, `senha`, `trusted_cert` | sem `\n`/controle; `trusted_cert` `^[0-9a-f]{64}$` (sha256) | formato linha-a-linha do `.conf` não tem escape |
| `checks[].host` ⊂ `redes` | **aviso**, não erro | check fora das redes declaradas quase sempre é typo, mas há casos legítimos |

## 11. Fluxo de UI

Pontos de entrada, sem quebrar o existente:

1. **Header bar**: botão "+" (`list-add-symbolic`) ao lado do ⟳ →
   diálogo "Nova VPN".
2. **Estado `nao_configurado`**: hoje a linha diz "`/etc/openfortivpn/<id>.conf`
   não existe" e não oferece nada (`BOTAO_PRINCIPAL` = `None`). Passa a
   oferecer **"Configurar…"** — abre o diálogo pré-preenchido com o que o
   catálogo já sabe (caso "entrada no TOML sem conf"). É a única mudança de
   comportamento numa linha existente.
3. **Linha do perfil**: `Adw.ExpanderRow` não tem menu nativo; acrescentar ao
   corpo expandido duas `ActionRow` ativáveis — "Editar perfil…" e "Remover
   perfil…" — no mesmo padrão visual da "Ver diagnóstico do systemd" que o
   estado `falhou` já usa. "Remover" só aparece em `inativo`/`falhou` (§9) e
   abre `Adw.AlertDialog` com `.destructive-action`, exigindo digitar o `id`
   para confirmar (remoção apaga credencial e rotas; é a única ação
   irreversível do app).

O diálogo (`editor.py`): `Adw.Dialog` com `Adw.PreferencesPage` — grupos
"Identificação" (id travado na edição), "Gateway" (host, porta, usuário,
senha `Gtk.PasswordEntry`, trusted-cert com texto de ajuda de onde tirar o
hash), "Redes" e "Checks" (listas com adicionar/remover linha), e "Opções
preservadas" (só perfis manuais, §8). Validação por campo no `notify::text`
com `add_css_class("error")` + mensagem — os erros aparecem no campo, antes
do Salvar.

Concorrência, seguindo o padrão documentado de `window.py`/`indicator.py` à
risca: o clique em Salvar desabilita o botão, mostra spinner+texto (nunca
spinner sozinho), roda `profile_client` numa `threading.Thread` e volta por
`GLib.idle_add`; try/except abrangente no worker com `idle_add` garantido no
`finally` (a lição das correções 3/5/N7 — usar o helper `rodar_em_thread` do
estudo de melhorias §2.4 se ele já tiver sido extraído; senão, copiar o
padrão com o try/finally completo). O pkexec pode ficar **minutos** esperando
o prompt do agente polkit: timeout generoso (120 s) e o diálogo permanece
aberto até a resposta — cancelar o prompt vira erro traduzido por
`_mensagem_autorizacao_negada`, já preparado para o "Not authorized" do
pkexec. Ao término com sucesso: fechar diálogo, toast, `self.refresh()` — a
releitura de catálogo a cada ciclo (item 6 do fechamento) faz a linha nova
aparecer sem código adicional.

O indicador não muda: relê o catálogo a cada 10 s e vê o perfil novo sozinho.

Atrito existente que este design expõe (registro opinativo): o fallback de
`cli._catalogo()` para `data/profiles.toml` do repositório — razoável para o
app leitor — fica perigoso com editor: se `/etc/vpn-manager/profiles.toml`
estiver momentaneamente ilegível, a janela mostraria os perfis fictícios do
repo enquanto o editor escreve nos reais. O editor deve operar **sempre e
somente** sobre `/etc` (o caminho é do helper, não do fallback), e vale
reavaliar o fallback em sessão gráfica.

## 12. Estratégia de teste (sem root, sem /etc)

Mesma disciplina dos 75 testes atuais: núcleo puro, dependências injetadas.

- **`profile_store`**: recebe um `Paths` (dataclass com `conf_dir`,
  `ip_up_dir`, `catalog_path`, `undo_dir`) — nos testes, tudo sob `tmp_path`.
  Cobre: geração dos três artefatos e permissões (600/755/644 via
  `os.stat`); **round-trip** (catálogo serializado → `tomllib.loads` →
  `load_catalog` real → `Profile` igual ao de entrada; `.conf` serializado →
  parser próprio → campos iguais); preservação de linhas desconhecidas;
  recusa de todos os vetores da §5.3 (id com `../`, rede com `;`, nome com
  `\n`, chave JSON desconhecida); rollback (monkeypatch fazendo o segundo
  `os.replace` levantar `OSError` → asserta que o primeiro artefato voltou
  ao estado do snapshot); ordem catálogo-por-último (falha no passo 2 →
  catálogo intocado).
- **`helper_main`**: `main(stdin=StringIO, stdout=StringIO, paths=Paths(tmp))`
  chamado direto — o mesmo `main` que o launcher instalado invoca. Cobre
  protocolo: JSON inválido, versão errada, verbo desconhecido, respostas de
  erro estruturadas, `delete` recusando unit ativa (com `run=Recorder`
  devolvendo `ActiveState=active`).
- **`profile_client`**: `run=Recorder` (o de `test_actions.py`) — asserta
  argv exato (`["pkexec", "/usr/local/libexec/vpn-manager-helper"]`, caminho
  fixo, **nenhum** dado de perfil no argv), payload no `input=`, tradução de
  "Not authorized", timeout, resposta não-JSON.
- **`editor`**: lógica de formulário extraída pura (`validar_formulario(dados)
  -> dict[campo, erro]`) testada sem GTK; o wiring do diálogo entra em
  `tests/test_window.py` via `_instalar_gi_falso()` (acrescentando
  `PasswordEntry` e afins ao fake), no mesmo estilo dos testes de flag/worker
  existentes — inclusive o teste "exceção inesperada no worker do Salvar
  libera o botão e mostra toast".
- **Fora da suíte** (checklist manual pós-install, como hoje): prompt real do
  polkit sob Wayland, permissões da cópia congelada, `pkcheck` da action nova.

## 13. Fora de escopo (v1)

- Renomear `id` (delete+create explícitos pelo usuário).
- Exportar/importar perfil entre máquinas (a decisão da §4 deixa isso
  possível no futuro — sem senha no export).
- Obter `trusted-cert` automaticamente do gateway (a UI ensina onde achar o
  hash; automatizar toca rede como root e merece design próprio).
- `set-dns`, rotas por host, opções avançadas do openfortivpn além das
  gerenciadas — ficam no bloco preservado (§8).
- Outros tipos de VPN (estudo de melhorias §3.2); mas o protocolo versionado
  do helper e o `Paths` injetável foram desenhados para não atrapalhar essa
  generalização.
- `cli doctor`/verbo `doctor` no helper (consistência entre artefatos,
  órfãos) — o design deixa o lugar pronto, não o implementa.

## 14. Riscos e pontos que exigem decisão humana

Riscos aceitos:

1. Helper novo = superfície root nova. Mitigada por interface estreita,
   validação dupla, cópia congelada e prompt de admin — mas é código novo
   rodando como root, e a revisão dele deve ser a mais dura do projeto.
2. Senha em claro no `.conf` (análise e mitigações na §4).
3. Serializador TOML próprio pode divergir do parser em casos exóticos —
  contido pelo round-trip via `tomllib` + rejeição de caracteres de controle.
4. Estado transitório de morte violenta do helper deixa órfão invisível
   (§7.5) — detectável, não corruptor.

Decisões que são do dono, não minhas:

1. **Prompt de senha para escrita** (`auth_admin_keep`, §5.1) ou clique
   silencioso via `rules.d`. Recomendo fortemente o prompt; está argumentado.
2. **Ratificar a decisão da senha** (§4) — em particular aceitar que a
   proteção contra roubo do notebook é responsabilidade do LUKS, não do app.
3. **Agressividade do `assume`** (§8): mover o script manual antigo para o
   undo automaticamente (com confirmação nominal) ou nunca tocar nele e só
   instruir. Propus mover; é o único passo da migração que apaga algo escrito
   à mão.
4. **Congelar junto a janela/indicador** (estudo de melhorias §3.3) na mesma
   leva do helper congelado, já que o `install.sh` vai ganhar essa mecânica
   de qualquer jeito — recomendo que sim, numa tarefa separada.
5. **Digitar o `id` para confirmar remoção** (§11) — proteção proporcional ou
   fricção exagerada para 3 perfis? Mantive por ser a única ação sem volta.
