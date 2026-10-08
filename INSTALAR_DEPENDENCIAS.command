#!/bin/bash
# =============================================================================
#  SR-VORTEX — Instalador de TODAS as dependências (macOS)
#
#  Instala / configura:
#    1) Python 3       → venv + requirements.txt
#    2) Playwright     → navegador Chromium
#    3) Node.js + npm  → node_modules do frontend e do admin-ui
#    4) Tesseract OCR  → brew install tesseract + idioma português
#    5) .env           → copia do .env.example se não existir
#    6) Preflight      → checagem final do ambiente
#
#  Uso: duplo clique OU no Terminal: bash INSTALAR_DEPENDENCIAS.command
# =============================================================================

cd "$(dirname "$0")" || exit 1
PROJ="$(pwd)"
ERROS=0

echo ""
echo "╔════════════════════════════════════════════════════════════════╗"
echo "║   SR-VORTEX — INSTALADOR DE DEPENDÊNCIAS (macOS)              ║"
echo "╚════════════════════════════════════════════════════════════════╝"
echo ""
echo "📂 Pasta do projeto: $PROJ"
echo ""

if [ ! -f "requirements.txt" ]; then
  echo "❌ requirements.txt não encontrado nesta pasta."
  echo "   Coloque este script dentro da pasta do projeto SR-VORTEX."
  read -r -p "Pressione Enter para fechar..."; exit 1
fi

# --- carrega Homebrew no PATH -----------------------------------------------
for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do
  [ -x "$b" ] && eval "$("$b" shellenv)" 2>/dev/null && break
done

# =============================================================================
echo "────────────────────────────────────────────────────────────────"
echo "  [1/6] PYTHON 3"
echo "────────────────────────────────────────────────────────────────"

if ! command -v python3 >/dev/null 2>&1; then
  echo "⚠️  Python 3 não encontrado. Tentando instalar via Homebrew..."
  if command -v brew >/dev/null 2>&1; then
    brew install python
  else
    echo "❌ Homebrew não encontrado."
    echo "   Instale o Homebrew: /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
    echo "   Depois rode este script de novo."
    read -r -p "Pressione Enter para fechar..."; exit 1
  fi
fi

PYSYS="$(command -v python3)"
echo "🐍 Python: $($PYSYS --version 2>&1)"

# --- venv -------------------------------------------------------------------
venv_ok() { [ -x "venv/bin/python" ] && venv/bin/python -c "import sys" >/dev/null 2>&1; }

if ! venv_ok; then
  echo "🔧 Criando ambiente virtual em venv/ ..."
  rm -rf venv
  "$PYSYS" -m venv venv || { echo "❌ Falha ao criar a venv."; read -r -p "Enter para fechar..."; exit 1; }
fi
VENV_PY="$PROJ/venv/bin/python"
echo "✅ venv: $("$VENV_PY" --version 2>&1)"

# =============================================================================
echo ""
echo "────────────────────────────────────────────────────────────────"
echo "  [2/6] DEPENDÊNCIAS PYTHON (requirements.txt)"
echo "────────────────────────────────────────────────────────────────"

echo "📦 Instalando (pode levar alguns minutos)..."
"$VENV_PY" -m pip install --upgrade pip setuptools wheel >/dev/null 2>&1
if "$VENV_PY" -m pip install -r requirements.txt; then
  echo "✅ Dependências Python instaladas."
else
  echo "❌ Falha ao instalar requirements.txt. Veja o erro acima."
  ERROS=$((ERROS + 1))
fi

if [ -f "requirements-dev.txt" ]; then
  "$VENV_PY" -m pip install -r requirements-dev.txt >/dev/null 2>&1
fi

# =============================================================================
echo ""
echo "────────────────────────────────────────────────────────────────"
echo "  [3/6] PLAYWRIGHT — navegador Chromium"
echo "────────────────────────────────────────────────────────────────"

if "$VENV_PY" -m playwright install chromium; then
  echo "✅ Chromium do Playwright instalado."
else
  echo "⚠️  Falha ao instalar o Chromium. Rode depois:"
  echo "   venv/bin/python -m playwright install chromium"
  ERROS=$((ERROS + 1))
fi

# =============================================================================
echo ""
echo "────────────────────────────────────────────────────────────────"
echo "  [4/6] NODE.JS + NPM (frontend e admin-ui)"
echo "────────────────────────────────────────────────────────────────"

if ! command -v node >/dev/null 2>&1; then
  echo "⚠️  Node.js não encontrado. Tentando instalar via Homebrew..."
  if command -v brew >/dev/null 2>&1; then
    brew install node
  else
    echo "❌ Instale o Node.js manualmente: https://nodejs.org"
    ERROS=$((ERROS + 1))
  fi
fi

if command -v node >/dev/null 2>&1; then
  echo "🟢 Node: $(node -v)   npm: $(npm -v)"

  # Frontend
  if [ -f "frontend/package.json" ]; then
    echo ""
    echo "📦 Instalando frontend..."
    cd frontend
    rm -rf node_modules package-lock.json 2>/dev/null
    if npm install; then
      echo "$(uname -m)" > node_modules/.mf_arch
      echo "✅ Frontend instalado."
    else
      echo "❌ npm install falhou no frontend."
      ERROS=$((ERROS + 1))
    fi
    cd "$PROJ"
  fi

  # Admin-UI
  if [ -f "admin-ui/package.json" ]; then
    echo ""
    echo "📦 Instalando admin-ui..."
    cd admin-ui
    rm -rf node_modules package-lock.json 2>/dev/null
    if npm install; then
      echo "✅ Admin-UI instalado."
    else
      echo "⚠️  npm install falhou no admin-ui."
      ERROS=$((ERROS + 1))
    fi
    cd "$PROJ"
  fi
else
  echo "⚠️  Node.js continua indisponível. Frontend não instalado."
  ERROS=$((ERROS + 1))
fi

# =============================================================================
echo ""
echo "────────────────────────────────────────────────────────────────"
echo "  [5/6] TESSERACT OCR (+ idioma português)"
echo "────────────────────────────────────────────────────────────────"

if ! command -v tesseract >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then
    echo "🔎 Instalando Tesseract..."
    brew install tesseract
  else
    echo "⚠️  Tesseract não encontrado e Homebrew indisponível."
    ERROS=$((ERROS + 1))
  fi
fi

if command -v tesseract >/dev/null 2>&1; then
  echo "✅ Tesseract: $(tesseract --version 2>&1 | head -1)"
  # Idioma português
  TESSDATA="$(tesseract --print-parameters 2>/dev/null | grep tessdata | awk '{print $2}' 2>/dev/null)"
  if [ -z "$TESSDATA" ]; then
    TESSDATA="$(brew --prefix 2>/dev/null)/share/tessdata"
  fi
  if [ -d "$TESSDATA" ] && [ ! -f "$TESSDATA/por.traineddata" ]; then
    echo "📥 Baixando idioma português (por.traineddata)..."
    curl -sL "https://raw.githubusercontent.com/tesseract-ocr/tessdata/main/por.traineddata" \
      -o "$TESSDATA/por.traineddata" 2>/dev/null && echo "✅ Idioma português instalado." \
      || echo "⚠️  Falha ao baixar por.traineddata."
  fi
else
  echo "⚠️  Tesseract não instalado — OCR de comprovantes vai falhar."
fi

# =============================================================================
echo ""
echo "────────────────────────────────────────────────────────────────"
echo "  [6/6] ARQUIVO .env"
echo "────────────────────────────────────────────────────────────────"

if [ ! -f ".env" ]; then
  if [ -f ".env.example" ]; then
    cp .env.example .env
    echo "✅ .env criado a partir do .env.example."
    echo "   ⚠️  PREENCHA as variáveis obrigatórias (PANEL_USERNAME, PANEL_PASSWORD, PANEL_AUTH_SECRET)."
  else
    echo "⚠️  .env.example não encontrado — crie o .env manualmente."
  fi
else
  echo "✅ .env já existe (não alterado)."
fi

# --- Preflight --------------------------------------------------------------
mkdir -p .state .state/screenshots uploads artifacts

if [ -f "preflight.py" ]; then
  echo ""
  echo "🔍 Checagem do ambiente (preflight)..."
  "$VENV_PY" preflight.py 2>/dev/null || true
fi

# =============================================================================
echo ""
echo "════════════════════════════════════════════════════════════════"
if [ "$ERROS" -eq 0 ]; then
  echo "   ✅ INSTALAÇÃO CONCLUÍDA"
else
  echo "   ⚠️  INSTALAÇÃO CONCLUÍDA COM $ERROS AVISO(S) — veja acima"
fi
echo "════════════════════════════════════════════════════════════════"
echo ""
echo "  Falta ainda (não dá pra automatizar):"
echo "    - AdsPower aberto e logado (API local em 127.0.0.1:50325)"
echo "    - .env preenchido: PANEL_USERNAME, PANEL_PASSWORD, PANEL_AUTH_SECRET"
echo ""
echo "  Para rodar:"
echo "    INICIAR_BACKEND.command   → API    em http://localhost:8000"
echo "    INICIAR_FRONTEND.command  → Painel em http://localhost:5173"
echo "    INICIAR_TUDO.command      → sobe os dois de uma vez"
echo ""
read -r -p "Pressione Enter para fechar..."
