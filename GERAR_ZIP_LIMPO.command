#!/bin/bash
# =============================================================================
#  SR-VORTEX — Gerador de ZIP limpo (macOS)
#  Cria um ZIP pronto para enviar para outro Mac — sem venv, node_modules,
#  .state, .git, .env com chaves, etc.
# =============================================================================

cd "$(dirname "$0")" || exit 1
SRC="$(pwd)"
NAME="SR-VORTEX"
STAMP="$(date +%Y%m%d-%H%M)"
ZIP="$HOME/Downloads/${NAME}-${STAMP}.zip"
TMP="$(mktemp -d)/SR-VORTEX"

echo ""
echo "╔════════════════════════════════════════════════════════════════╗"
echo "║   SR-VORTEX — GERADOR DE ZIP LIMPO                           ║"
echo "╚════════════════════════════════════════════════════════════════╝"
echo ""
echo "📂 Origem : $SRC"
echo "📦 Destino: $ZIP"
echo ""

# --- 1) Copia o projeto, pulando o que é local da máquina -------------------
echo "[1/5] Copiando código fonte..."
mkdir -p "$TMP"
rsync -a --progress \
  --exclude='venv' --exclude='.venv' --exclude='node_modules' \
  --exclude='.state' --exclude='uploads' --exclude='artifacts' \
  --exclude='__pycache__' --exclude='.pytest_cache' --exclude='.vite' \
  --exclude='.mypy_cache' --exclude='.ruff_cache' \
  --exclude='.git' --exclude='.claude' --exclude='.idea' --exclude='.vscode' \
  --exclude='dist' --exclude='Claude outputs' \
  --exclude='.env' --exclude='.env.bak' \
  --exclude='*.pyc' --exclude='*.pyo' --exclude='*.log' \
  --exclude='*.db' --exclude='*.db-wal' --exclude='*.db-shm' \
  --exclude='*.sqlite' --exclude='*.sqlite3' \
  --exclude='.deps_hash' --exclude='.pw_ok' --exclude='*.py.backup' \
  --exclude='Thumbs.db' --exclude='.DS_Store' \
  --exclude='package-lock.json' --exclude='_restart_trigger.py' \
  "$SRC/" "$TMP/" 2>/dev/null

# --- 2) Recria pastas vazias que o app precisa ------------------------------
echo "[2/5] Criando pastas vazias necessárias..."
for d in .state .state/screenshots uploads artifacts; do
  mkdir -p "$TMP/$d"
  touch "$TMP/$d/.gitkeep"
done

# --- 3) .env zerado a partir do .env.example --------------------------------
echo "[3/5] Copiando .env zerado..."
if [ -f "$TMP/.env.example" ]; then
  cp "$TMP/.env.example" "$TMP/.env"
  echo "  ✅ .env criado a partir do .env.example"
fi

# --- 4) Remove arquivos desnecessários para o destinatário ------------------
echo "[4/5] Limpando arquivos desnecessários..."
for f in mensagem-claudio.txt netlify_selftest.py deploy-fix.py deploy-fix.sh \
         test_fb_static_cache.py requirements-dev.txt \
         ATUALIZACOES_COMPLETAS.md INSTRUCOES_DEPLOY.md LEIA-ME-ZERADO.md; do
  [ -f "$TMP/$f" ] && rm -f "$TMP/$f" && echo "  removido: $f"
done
[ -d "$TMP/tests" ] && rm -rf "$TMP/tests" && echo "  removido: tests/"

# --- 5) Compacta ------------------------------------------------------------
echo "[5/5] Compactando..."
cd "$(dirname "$TMP")"
zip -rq "$ZIP" "$(basename "$TMP")"

FILES=$(find "$TMP" -type f | wc -l | tr -d ' ')
MB=$(du -m "$ZIP" | awk '{print $1}')
rm -rf "$(dirname "$TMP")"

echo ""
echo "╔════════════════════════════════════════════════════════════════╗"
echo "║   ✅ ZIP PRONTO                                                ║"
echo "╚════════════════════════════════════════════════════════════════╝"
echo "  📦 Arquivo : $ZIP"
echo "  📊 Tamanho : ${MB} MB   ($FILES arquivos)"
echo ""
echo "  O que NÃO está no zip (será recriado pelo instalador):"
echo "    - venv/             (ambiente Python)"
echo "    - node_modules/     (deps do frontend)"
echo "    - .state/           (banco, logs, screenshots)"
echo "    - .env com chaves   (vem zerado)"
echo ""
echo "  Envie o zip e peça pro amigo rodar:"
echo "  INSTALAR_DEPENDENCIAS.command"
echo ""
read -r -p "Pressione Enter para fechar..."
