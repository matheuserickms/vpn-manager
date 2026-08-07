#!/usr/bin/env bash
# =============================================================================
# publicar.sh — sincroniza a branch pública a partir da branch de trabalho.
#
#   ./publicar.sh                 # publica, pedindo confirmação
#   ./publicar.sh --dry-run       # roda todas as checagens e não publica
#   ./publicar.sh -m "mensagem"   # mensagem do commit público
#
# POR QUE ISTO EXISTE
# A branch de trabalho tem histórico com dados internos; a pública nasceu de um
# commit órfão limpo. Sincronizar à mão (`git checkout master -- arquivo`) é
# fácil de esquecer e não verifica nada. Aqui a publicação só acontece se a
# árvore estiver limpa, os testes passarem e a varredura não achar nada.
#
# COMO FUNCIONA
# Não troca de branch e não toca no diretório de trabalho: cria o commit com
# `git commit-tree`, usando a árvore da branch de trabalho e a pública como
# pai. Como a árvore só contém o que está versionado, tudo que o .gitignore
# exclui fica de fora por construção.
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")"

TRABALHO=master
PUBLICA=publico
REMOTO=origin
REMOTO_RAMO=main
ARQ_PROIBIDO=.publicar-proibido

DRY_RUN=false
MSG=""

while [[ $# -gt 0 ]]; do
	case "$1" in
	--dry-run) DRY_RUN=true; shift ;;
	-m) MSG="${2:-}"; shift 2 ;;
	*) echo "opção desconhecida: $1" >&2; exit 1 ;;
	esac
done

erro() {
	echo "erro: $*" >&2
	exit 1
}
passo() { echo "==> $*"; }

# ---------------------------------------------------------------------------
# 1. Pré-condições
# ---------------------------------------------------------------------------
passo "verificando o repositório"
git rev-parse --git-dir >/dev/null 2>&1 || erro "não é um repositório git"

[[ -z "$(git status --porcelain)" ]] ||
	erro "árvore suja — commite ou guarde as mudanças antes de publicar"

for b in "$TRABALHO" "$PUBLICA"; do
	git rev-parse --verify -q "$b" >/dev/null || erro "branch '$b' não existe"
done

# O arquivo de padrões NÃO é versionado de propósito: ele lista justamente os
# termos que não podem sair daqui. Versioná-lo publicaria a própria lista.
[[ -f "$ARQ_PROIBIDO" ]] ||
	erro "$ARQ_PROIBIDO não existe — copie de $ARQ_PROIBIDO.example e preencha"

# ---------------------------------------------------------------------------
# 2. Testes
# ---------------------------------------------------------------------------
if [[ -x venv/bin/python ]]; then
	passo "rodando a suíte"
	PYTHONPATH=src venv/bin/python -m pytest tests/ -q >/dev/null ||
		erro "testes falharam — não publico código quebrado"
	echo "    ok"
else
	echo "    aviso: venv não encontrado, testes pulados"
fi

# ---------------------------------------------------------------------------
# 3. Varredura de dados sensíveis na árvore que seria publicada
# ---------------------------------------------------------------------------
passo "varrendo '$TRABALHO' em busca de dados internos"

mapfile -t PADROES < <(grep -vE '^[[:space:]]*(#|$)' "$ARQ_PROIBIDO" || true)
[[ ${#PADROES[@]} -gt 0 ]] || erro "nenhum padrão útil em $ARQ_PROIBIDO"
PADRAO="$(
	IFS='|'
	echo "${PADROES[*]}"
)"

# git grep sai com 0 quando ENCONTRA — encontrar aqui é motivo de abortar.
if git grep -I -n -i -E "$PADRAO" "$TRABALHO" -- . 2>/dev/null; then
	echo >&2
	erro "os trechos acima seriam publicados — publicação abortada"
fi
echo "    limpo: ${#PADROES[@]} padrões verificados, nenhuma ocorrência"

# ---------------------------------------------------------------------------
# 4. O que muda
# ---------------------------------------------------------------------------
TREE="$(git rev-parse "$TRABALHO^{tree}")"
TREE_PUB="$(git rev-parse "$PUBLICA^{tree}")"

if [[ "$TREE" == "$TREE_PUB" ]]; then
	passo "árvore idêntica à última publicação — nada a fazer"
	git push --dry-run -q "$REMOTO" "$PUBLICA:$REMOTO_RAMO" 2>/dev/null &&
		echo "    o remoto já está em dia"
	exit 0
fi

passo "diferenças que seriam publicadas"
git diff --stat "$PUBLICA" "$TRABALHO" | sed 's/^/    /'

[[ -n "$MSG" ]] || MSG="$(git log -1 --format='%s' "$TRABALHO")"
echo
echo "    mensagem do commit público: $MSG"

if $DRY_RUN; then
	echo
	passo "--dry-run: todas as checagens passaram, nada foi publicado"
	exit 0
fi

# ---------------------------------------------------------------------------
# 5. Publicar
# ---------------------------------------------------------------------------
echo
read -rp "Publicar em $REMOTO/$REMOTO_RAMO? [s/N] " OK
[[ "${OK,,}" == "s" ]] || {
	echo "cancelado."
	exit 1
}

passo "criando commit na branch '$PUBLICA'"
COMMIT="$(git commit-tree "$TREE" -p "$(git rev-parse "$PUBLICA")" -m "$MSG")"
git update-ref "refs/heads/$PUBLICA" "$COMMIT" ||
	erro "falha ao atualizar refs/heads/$PUBLICA"

passo "enviando"
git push -q "$REMOTO" "$PUBLICA:$REMOTO_RAMO"

echo
echo "publicado: $(git log -1 --oneline "$PUBLICA")"
echo "branch de trabalho intacta: $(git log -1 --oneline "$TRABALHO")"
