#!/bin/bash
# =============================================================================
#  Meta-Farm — Frontend (React/Vite, porta 5173)
#  Auto-reparo: reinstala node_modules limpo quando estao ausentes ou vieram
#  de outro Mac/arquitetura (corrige o erro @rollup/rollup-darwin-*).
# =============================================================================

cd "$(dirname "$0")/frontend" || exit 1
FE="$(pwd)"

echo "╔════════════════════════════════════════════════════════════════╗"
echo "║   Meta-Farm Frontend — React/Vite (porta 5173)                ║"
echo "╚════════════════════════════════════════════════════════════════╝"
echo ""
echo "📂 Diretório: $FE"
echo ""

# --- carrega Homebrew no PATH ----------------------------------------------
for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do
  [ -x "$b" ] && eval "$("$b" shellenv)" 2>/dev/null && break
done

# --- garante Node + npm -----------------------------------------------------
if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
  echo "⚠️  Node.js/npm não encontrados. Tentando instalar via Homebrew..."
  if command -v brew >/dev/null 2>&1; then
    brew install node
  else
    echo "❌ Homebrew não encontrado. Instale o Node.js e rode de novo."
    read -r -p "Pressione Enter para fechar..."; exit 1
  fi
fi
echo "🟢 Node: $(node -v)   npm: $(npm -v)"

# --- decide se precisa reinstalar -------------------------------------------
# Marca a arquitetura na instalacao; se mudou (ou nao existe), refaz limpo.
NEED_INSTALL=0
ARCH="$(uname -m)"
if [ ! -d "node_modules" ]; then
  NEED_INSTALL=1
elif [ ! -f "node_modules/.mf_arch" ] || [ "$(cat node_modules/.mf_arch 2>/dev/null)" != "$ARCH" ]; then
  echo "🔧 node_modules parece ter vindo de outro Mac/arquitetura. Reinstalando limpo..."
  NEED_INSTALL=1
elif ! node -e "require.resolve('vite')" >/dev/null 2>&1; then
  NEED_INSTALL=1
fi

if [ "$NEED_INSTALL" = "1" ]; then
  echo "🧹 Removendo node_modules e package-lock.json antigos..."
  rm -rf node_modules package-lock.json
  echo "📦 Rodando npm install (pode levar alguns minutos)..."
  if npm install; then
    echo "$ARCH" > node_modules/.mf_arch
    echo "✅ Dependências do frontend instaladas."
  else
    echo "❌ Falha no npm install. Veja o erro acima."
    read -r -p "Pressione Enter para fechar..."; exit 1
  fi
else
  echo "✅ node_modules já instalado para esta arquitetura ($ARCH)."
fi

echo ""
echo "🔄 Iniciando Frontend em http://localhost:5173 ..."
echo ""
exec npm run dev
