#!/bin/bash
# =============================================================================
#  SR-VORTEX — Atualizar (macOS)
#
#  O que faz:
#    1) git pull  (baixa atualizações do código)
#    2) pip install -r requirements.txt  (instala deps Python novas)
#    3) npm install no frontend  (instala deps Node novas)
#
#  O que NÃO mexe (seus dados ficam intactos):
#    - .env  (suas chaves e senhas)
#    - .state/  (banco, logs, screenshots)
#    - uploads/
#    - venv/  (só instala pacotes novos por cima)
# =============================================================================

cd "$(dirname "$0")" || exit 1
PROJ="$(pwd)"

echo ""
echo "╔════════════════════════════════════════════════════════════════╗"
echo "║   SR-VORTEX — ATUALIZADOR (macOS)                            ║"
echo "╚════════════════════════════════════════════════════════════════╝"
echo ""
echo "📂 Pasta: $PROJ"
echo ""

# --- carrega Homebrew no PATH -----------------------------------------------
for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do
  [ -x "$b" ] && eval "$("$b" shellenv)" 2>/dev/null && break
done

# --- Verifica se é um repositório git ----------------------------------------
if [ ! -d ".git" ]; then
  echo "❌ Esta pasta não é um repositório git."
  echo "   Você precisa ter clonado o projeto com:"
  echo "   git clone https://github.com/Lusscaa/sr-vortex.git"
  read -r -p "Pressione Enter para fechar..."; exit 1
fi

if ! command -v git >/dev/null 2>&1; then
  echo "❌ Git não encontrado. Instale com: brew install git"
  read -r -p "Pressione Enter para fechar..."; exit 1
fi

# =============================================================================
echo "────────────────────────────────────────────────────────────────"
echo "  [1/3] BAIXANDO ATUALIZAÇÕES (git pull)"
echo "────────────────────────────────────────────────────────────────"
echo ""

STASHED=0
if ! git diff --quiet 2>/dev/null; then
  echo "📦 Você tem alterações locais. Salvando antes de atualizar..."
  if git stash push -u -m "backup-antes-atualizar-$(date +%Y-%m-%d-%H%M)"; then
    STASHED=1
    echo "✅ Alterações salvas. Serão restauradas no final."
  else
    echo "⚠️  Não consegui salvar suas alterações. Continuando..."
  fi
fi

if git pull origin main; then
  echo "✅ Código atualizado."
else
  echo "❌ Falha ao baixar atualizações."
  echo "   Verifique sua conexão com a internet."
fi

# =============================================================================
echo ""
echo "────────────────────────────────────────────────────────────────"
echo "  [2/3] ATUALIZANDO DEPENDÊNCIAS PYTHON"
echo "────────────────────────────────────────────────────────────────"

VENV_PY="$PROJ/venv/bin/python"
if [ ! -x "$VENV_PY" ]; then
  echo "⚠️  venv não encontrada. Rode INSTALAR_DEPENDENCIAS.command primeiro."
else
  if "$VENV_PY" -m pip install -r requirements.txt --quiet; then
    echo "✅ Dependências Python atualizadas."
  else
    echo "⚠️  Falha ao atualizar deps Python."
  fi
  "$VENV_PY" -m playwright install chromium >/dev/null 2>&1
fi

# =============================================================================
echo ""
echo "────────────────────────────────────────────────────────────────"
echo "  [3/3] ATUALIZANDO DEPENDÊNCIAS DO FRONTEND"
echo "────────────────────────────────────────────────────────────────"

if ! command -v node >/dev/null 2>&1; then
  echo "⚠️  Node.js não encontrado. Pulando frontend."
elif [ -f "frontend/package.json" ]; then
  cd frontend
  if npm install --silent; then
    echo "✅ Dependências do frontend atualizadas."
  else
    echo "⚠️  Falha ao atualizar deps do frontend."
  fi
  cd "$PROJ"
fi

# --- Restaura alterações locais -----------------------------------------------
if [ "$STASHED" -eq 1 ]; then
  echo ""
  echo "📦 Restaurando suas alterações locais..."
  if git stash pop 2>/dev/null; then
    echo "✅ Alterações locais restauradas."
  else
    echo "⚠️  Não foi possível restaurar automaticamente."
    echo "   Suas alterações estão salvas. Rode: git stash list"
  fi
fi

# =============================================================================
echo ""
echo "════════════════════════════════════════════════════════════════"
echo "   ✅ ATUALIZAÇÃO CONCLUÍDA"
echo "════════════════════════════════════════════════════════════════"
echo ""
echo "  Suas configurações (.env, banco, proxies, contas) NÃO foram"
echo "  alteradas. Só o código do bot foi atualizado."
echo ""
echo "  Agora rode INICIAR_TUDO.command para usar a versão nova."
echo ""
read -r -p "Pressione Enter para fechar..."
