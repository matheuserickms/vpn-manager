# Backlog — configuração de perfis pela interface

Derivado de [`2026-08-07-configuracao-de-perfis-design.md`](2026-08-07-configuracao-de-perfis-design.md).
As referências `§n` apontam para seções daquele documento.

Objetivo: criar, editar e remover um perfil de VPN completo pela interface —
`/etc/openfortivpn/<id>.conf`, o script de rotas em `/etc/ppp/ip-up.d/` e a
entrada em `/etc/vpn-manager/profiles.toml` — sem abrir um terminal.

Esforço: **P** ≈ uma sessão · **M** ≈ duas ou três · **G** ≈ mais que isso.

---

## Fase 0 — Decisões que bloqueiam o resto

Nenhuma linha de código antes destas. Todas são do dono do projeto, não do
design (§14).

- [ ] **D1. Prompt de senha para escrita em /etc, ou clique silencioso?**
      O design propõe `auth_admin_keep` na action nova e argumenta que escrever
      em `ip-up.d` é execução de código como root a cada conexão — um `YES`
      silencioso daria isso a qualquer código da sessão. A alternativa é uma
      regra `rules.d` como a das units. **Recomendado: prompt.** — §5.1
- [ ] **D2. Ratificar a senha em texto puro no `.conf` (root:root 600).**
      Implica aceitar que proteção contra roubo do notebook é papel do LUKS, não
      do app. O design mostra que libsecret seria pior contra a ameaça realista
      e que systemd-creds não protege de root local. — §4
- [ ] **D3. Quão agressivo é o "assumir gerenciamento"?**
      Mover o script manual antigo para o diretório de undo (proposto) ou nunca
      tocar nele e apenas instruir. É o único passo da migração que remove algo
      escrito à mão. — §8
- [ ] **D4. Congelar janela e indicador junto com o helper?**
      O `install.sh` vai ganhar a mecânica de cópia congelada de qualquer jeito.
      Recomendado sim, em tarefa separada — não bloqueia esta feature.
- [ ] **D5. Exigir digitar o `id` para confirmar remoção?**
      Proteção proporcional ou fricção exagerada para poucos perfis. Mantido no
      design por ser a única ação sem volta. — §11

---

## Fase 1 — Núcleo sem privilégio

Tudo testável sem root e sem tocar em `/etc`. Nada aqui roda elevado.

- [ ] **1.1 `profile_store.py`: validação (P)**
      Regras únicas, usadas depois nos dois lados da fronteira. `id` casando
      `^[a-z0-9][a-z0-9-]{0,30}[a-z0-9]$` — sem ponto, porque `run-parts` ignora
      silenciosamente arquivos com `.`; hífen só é permitido porque o drop-in
      `%i` existe. Redes por `ipaddress.ip_network(strict=True)`. Host por
      RFC 1123 ou `ip_address()`. Porta inteira 1–65535. `\n` e caracteres de
      controle rejeitados em todo campo. — §10
      *Aceite:* um teste por vetor da tabela de injeção da §5.3, todos falhando
      na validação.

- [ ] **1.2 `profile_store.py`: serialização dos três artefatos (M)**
      Serializador do `.conf` (chave = valor por linha), do script de `ip-up.d`
      (template fixo, guard por `ipparam`, uma linha `ip route replace` por rede
      do catálogo) e um writer TOML mínimo — a stdlib só tem leitor. Redes vão
      para o script como `str(ip_network(...))`, nunca como texto do usuário.
      — §3.3
      *Aceite:* round-trip — o TOML gerado é lido de volta por `tomllib` e por
      `load_catalog`, produzindo o `Profile` de origem.

- [ ] **1.3 `profile_store.py`: plano de aplicação com snapshot e rollback (M)**
      Snapshot em `/var/lib/vpn-manager/undo/`, escrita em temporário no mesmo
      diretório com `O_NOFOLLOW` + `os.replace`, catálogo por último — o perfil
      só fica visível quando os três artefatos existem. Rollback em ordem
      inversa. Pior caso é órfão invisível, nunca perfil visível quebrado. — §7
      *Aceite:* testes com `Paths` injetável apontando para `tmp_path`, injetando
      falha em cada etapa e conferindo que o estado final é o inicial.

## Fase 2 — Fronteira privilegiada

- [ ] **2.1 Protocolo do helper (P)**
      Request/response JSON versionado, cinco verbos, limite de 64 KiB e de 32
      redes/checks. Fail-closed: chave desconhecida é erro. O verbo `read`
      devolve sentinela no lugar da senha — ela nunca volta ao processo sem
      privilégio. — §6
      *Aceite:* request com campo extra, com versão futura e acima do limite são
      todos rejeitados.

- [ ] **2.2 `helper_main.py` (M)**
      `main()` fino sobre `profile_store`. Revalida tudo o que vem do stdin — o
      chamador pode ser qualquer processo que convenceu o polkit. Só escreve nos
      três caminhos derivados do `id`; não existe parâmetro de caminho no
      protocolo. `flock` em `/run/vpn-manager-helper.lock`. Confere que o drop-in
      do systemd existe antes de aceitar `id` com hífen. — §5.2
      *Aceite:* `helper_main(stdin, stdout, paths)` chamável direto no teste, sem
      subprocess e sem root.

- [ ] **2.3 `profile_client.py` (P)**
      Lado do usuário: monta o request, invoca `pkexec`, traduz erros reusando
      `vpnctl._mensagem_autorizacao_negada`. Senha viaja **só por stdin** —
      nunca argv, porque `/proc/*/cmdline` é público; nunca arquivo temporário.
      `run=subprocess.run` injetável, como no resto do projeto. — §4, §6
      *Aceite:* teste com `Recorder` provando que a senha não aparece em nenhum
      argumento da chamada.

## Fase 3 — Instalação

- [ ] **3.1 Action polkit própria (P)**
      `data/br.dev.matheus.VpnManager.policy` com a action nova, anotação
      `exec.path` fixa e o valor de `allow_active` decidido em **D1**. — §5.1
      *Aceite:* `pkcheck` autoriza a action nova sem afetar a das units.

- [ ] **3.2 Cópia congelada no `install.sh` (M)**
      `profile_store.py` + `helper_main.py` para `/usr/local/lib/vpn-manager/`
      (root:root) e launcher em `/usr/local/libexec/vpn-manager-helper` que fixa
      `sys.path` só nessa cópia. **Nada que roda como root pode ser gravável
      pelo usuário** — invariante nº 1. Custo honesto: mudou o núcleo, roda o
      instalador de novo. — §3.2
      *Aceite:* nenhum caminho envolvido na execução root é gravável pelo
      usuário; verificar com `find -writable`.

## Fase 4 — Interface

- [ ] **4.1 `editor.py`: diálogo de criar e editar (G)**
      Lógica de formulário em funções puras, testáveis sem GTK. Campo de senha
      vazio com placeholder "manter a atual" na edição; ausência ou sentinela
      significa preservar. `set-routes`, `set-dns` e `pppd-ipparam` não são
      editáveis em perfil gerenciado — o modelo de estado de `probe.py` depende
      deles. — §3.3, §11
      *Aceite:* testes das funções de formulário sem importar GTK; o diálogo usa
      o `_instalar_gi_falso()` de `test_window.py`.

- [ ] **4.2 Integração na `window.py` (M)**
      Botão "+" no header, menu por linha e o botão "Configurar…" no estado
      `nao_configurado`. Nada de subprocess na thread do GLib — seguir o padrão
      já documentado no arquivo. — §11
      *Aceite:* a janela não congela durante um salvamento; o estado da lista
      atualiza sozinho depois.

- [ ] **4.3 Editar perfil conectado (P)**
      Salvar nunca derruba conexão. Oferecer "Salvar" e "Salvar e reconectar",
      encadeando o `vpnctl.restart` existente. `delete` e edição de perfil
      `externo` bloqueados. — §9

## Fase 5 — Migração

- [ ] **5.1 Assumir gerenciamento de perfil manual (M)**
      Marcador na primeira linha dos artefatos gerenciados. Linhas desconhecidas
      do `.conf` preservadas verbatim — não destruir o que a UI não entende.
      Exige perfil desconectado: o `ipparam` antigo em memória somado a
      `persistent` faria o redial voltar sem rotas. Comportamento com o script
      antigo conforme **D3**. — §8
      *Aceite:* um `.conf` com opções desconhecidas sobrevive a um ciclo de
      assumir → editar → salvar.

## Fase 6 — Fechamento

- [ ] **6.1 Revisão de segurança do helper (P)**
      A revisão mais dura do projeto, por ser código novo rodando como root.
      Percorrer a tabela de vetores da §5.3 item a item contra o código real.
- [ ] **6.2 README e `install.sh` (P)**
      Documentar o fluxo novo e dizer com todas as letras que a proteção contra
      roubo do notebook é o LUKS, não o app. — §4

---

## Fora de escopo nesta leva

Renomear `id` · exportar/importar perfil entre máquinas · obter `trusted-cert`
automaticamente do gateway · `set-dns` e opções avançadas além das gerenciadas ·
outros tipos de VPN · verbo `doctor`. O design deixa lugar para todos. — §13

## Riscos aceitos

Helper novo é superfície root nova (mitigada por interface estreita, validação
dupla, cópia congelada e prompt) · senha em claro no `.conf` (§4) · serializador
TOML próprio pode divergir do parser em casos exóticos (contido pelo round-trip)
· morte violenta do helper deixa órfão invisível, detectável e não corruptor.
