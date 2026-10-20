#!/bin/bash
# =============================================================================
#  SR-Vortex — Gerador de ZIP limpo (macOS)
#  Gera um arquivo .zip pronto para enviar, sem venv, node_modules, .state etc.
# =============================================================================

set -euo pipefail
cd "$(dirname "$0")"

SRC="$(pwd)"
NAME="SR-VORTEX"
STAMP="$(date +%Y%m%d-%H%M)"
DOWNLOADS="$HOME/Downloads"
ZIP_FILE="$DOWNLOADS/${NAME}-${STAMP}.zip"

echo ""
echo "====================================================="
echo "  SR-VORTEX — GERADOR DE ZIP LIMPO (macOS)"
echo "====================================================="
echo ""
echo "  Origem : $SRC"
echo "  Destino: $ZIP_FILE"
echo ""

# ---------------------------------------------------------------------------
# 1) Copia o projeto, pulando tudo que é específico da máquina
# ---------------------------------------------------------------------------
TMP_DIR=$(mktemp -d -t mf_zip)
STAGE="$TMP_DIR/$NAME"
mkdir -p "$STAGE"

echo "[1/5] Copiando código fonte (sem venv, node_modules, .state, .git)..."

rsync -a \
  --exclude='venv/' \
  --exclude='.venv/' \
  --exclude='node_modules/' \
  --exclude='.state/' \
  --exclude='uploads/' \
  --exclude='artifacts/' \
  --exclude='__pycache__/' \
  --exclude='.pytest_cache/' \
  --exclude='.vite/' \
  --exclude='.mypy_cache/' \
  --exclude='.ruff_cache/' \
  --exclude='.git/' \
  --exclude='.claude/' \
  --exclude='.idea/' \
  --exclude='.vscode/' \
  --exclude='dist/' \
  --exclude='Claude outputs/' \
  --exclude='*.pyc' \
  --exclude='*.pyo' \
  --exclude='*.log' \
  --exclude='.env' \
  --exclude='.env.bak' \
  --exclude='*.db' \
  --exclude='*.db-wal' \
  --exclude='*.db-shm' \
  --exclude='*.sqlite' \
  --exclude='*.sqlite3' \
  --exclude='.deps_hash' \
  --exclude='.pw_ok' \
  --exclude='*.py.backup' \
  --exclude='Thumbs.db' \
  --exclude='.DS_Store' \
  --exclude='package-lock.json' \
  --exclude='_restart_trigger.py' \
  "$SRC/" "$STAGE/"

# ---------------------------------------------------------------------------
# 2) Recria pastas vazias que o app precisa
# ---------------------------------------------------------------------------
echo "[2/5] Criando pastas vazias necessárias..."
for d in .state .state/screenshots uploads; do
  mkdir -p "$STAGE/$d"
  touch "$STAGE/$d/.gitkeep"
done

# ---------------------------------------------------------------------------
# 3) .env zerado a partir do .env.example
# ---------------------------------------------------------------------------
echo "[3/5] Copiando .env zerado (a partir do .env.example)..."
if [ -f "$STAGE/.env.example" ]; then
  cp "$STAGE/.env.example" "$STAGE/.env"
  echo "  OK — .env criado a partir do .env.example"
else
  echo "  AVISO: .env.example não encontrado"
fi

# ---------------------------------------------------------------------------
# 4) Remove arquivos que não servem pro destinatário
# ---------------------------------------------------------------------------
echo "[4/5] Limpando arquivos desnecessários..."
REMOVER=(
  "mensagem-claudio.txt"
  "netlify_selftest.py"
  "CORRIGIR_CONFIG.bat"
  "CORRIGIR_PYTHON.bat"
  "ATUALIZACOES_COMPLETAS.md"
  "INSTRUCOES_DEPLOY.md"
  "LEIA-ME-ZERADO.md"
  "deploy-fix.py"
  "deploy-fix.sh"
  "test_fb_static_cache.py"
  "requirements-dev.txt"
  "PREPARAR_PARA_NOVO_MAC.command"
  "LIMPAR_CONTAS_DESCARTADAS.command"
  "LIMPAR_CONTAS_DESCARTADAS.py"
)
for f in "${REMOVER[@]}"; do
  if [ -f "$STAGE/$f" ]; then
    rm -f "$STAGE/$f"
    echo "  removido: $f"
  fi
done

if [ -d "$STAGE/tests" ]; then
  rm -rf "$STAGE/tests"
  echo "  removido: tests/"
fi

# Garante que os scripts .command estão executáveis
chmod +x "$STAGE"/*.command 2>/dev/null || true

# ---------------------------------------------------------------------------
# 5) Compacta
# ---------------------------------------------------------------------------
echo "[5/5] Compactando..."
rm -f "$ZIP_FILE"
cd "$TMP_DIR"
zip -r -q "$ZIP_FILE" "$NAME"
cd "$SRC"

FILE_COUNT=$(find "$STAGE" -type f | wc -l | tr -d ' ')
SIZE_MB=$(du -sm "$ZIP_FILE" | awk '{print $1}')
rm -rf "$TMP_DIR"

echo ""
echo "====================================================="
echo "  ✅ ZIP PRONTO"
echo "====================================================="
echo "  Arquivo : $ZIP_FILE"
echo "  Tamanho : ${SIZE_MB} MB   ($FILE_COUNT arquivos)"
echo ""
echo "  O que NÃO está no zip (será recriado pelo instalador):"
echo "    - venv/             (ambiente Python)"
echo "    - node_modules/     (deps do frontend)"
echo "    - .state/           (banco, logs, screenshots)"
echo "    - .env com chaves   (vem zerado)"
echo ""
echo "  Envie o zip e peça pro destinatário:"
echo "    1. Descompactar"
echo "    2. Clicar duplo em INSTALAR_DEPENDENCIAS.command (Mac)"
echo "       ou INSTALAR_DEPENDENCIAS.bat (Windows)"
echo "    3. Preencher o .env"
echo "    4. Clicar em INICIAR_TUDO.command (Mac)"
echo "       ou INICIAR_TUDO.bat (Windows)"
echo ""
read -r -p "Pressione Enter para fechar..."
