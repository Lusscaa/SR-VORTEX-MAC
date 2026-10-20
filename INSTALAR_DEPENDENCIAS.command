#!/bin/bash
# =============================================================================
#  SR-Vortex — Instalador de TODAS as dependências no macOS
#
#  Instala / configura:
#    1) Homebrew       → gerenciador de pacotes (se não tiver)
#    2) Python 3       → venv + requirements.txt
#    3) Playwright     → navegador Chromium
#    4) Node.js + npm  → node_modules do frontend
#    5) Tesseract OCR  → binário + idioma português (por.traineddata)
#    6) .env           → TESSERACT_CMD apontando pro binário
#    7) Preflight      → checagem final do ambiente
#
#  Uso: clique duplo neste arquivo (Finder) ou rode: bash INSTALAR_DEPENDENCIAS.command
# =============================================================================

cd "$(dirname "$0")" || exit 1
PROJ="$(pwd)"
ERROS=0

echo ""
echo "================================================================"
echo "   SR-VORTEX — INSTALADOR DE DEPENDÊNCIAS (macOS)"
echo "================================================================"
echo ""
echo "  Pasta do projeto: $PROJ"
echo ""

# --- carrega o Homebrew no PATH (Intel: /usr/local | Apple Silicon: /opt/homebrew)
for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do
  [ -x "$b" ] && eval "$("$b" shellenv)" 2>/dev/null && break
done

# --- garante que estamos na pasta certa ----------------------------------------
if [ ! -f "requirements.txt" ]; then
  echo "❌ [ERRO] requirements.txt não encontrado nesta pasta."
  echo "         Coloque este script dentro da pasta do projeto SR-Vortex."
  read -r -p "Pressione Enter para fechar..."; exit 1
fi

# --- cria .env a partir do .env.example se não existir -----------------------
if [ ! -f "$PROJ/.env" ]; then
  if [ -f "$PROJ/.env.example" ]; then
    cp "$PROJ/.env.example" "$PROJ/.env"
    echo "  ✅ .env criado a partir do .env.example"
    echo "     ⚠️  PREENCHA as 3 variáveis obrigatórias antes de rodar o bot:"
    echo "        PANEL_USERNAME, PANEL_PASSWORD, PANEL_AUTH_SECRET"
    echo ""
  else
    echo "  ⚠️  .env.example não encontrado — crie o .env manualmente."
    echo ""
  fi
else
  echo "  ✅ .env já existe (não alterado)"
  echo ""
fi

# =============================================================================
echo "----------------------------------------------------------------"
echo "  [1/7] HOMEBREW"
echo "----------------------------------------------------------------"

if ! command -v brew >/dev/null 2>&1; then
  echo "  Homebrew não encontrado. Instalando..."
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    [ -x "$b" ] && eval "$("$b" shellenv)" 2>/dev/null && break
  done
  if command -v brew >/dev/null 2>&1; then
    echo "  ✅ Homebrew instalado."
  else
    echo "  ⚠️  Homebrew não ficou acessível. Feche e reabra o Terminal, depois rode de novo."
    ERROS=$((ERROS + 1))
  fi
else
  echo "  ✅ Homebrew já instalado: $(brew --version | head -1)"
fi

# =============================================================================
echo ""
echo "----------------------------------------------------------------"
echo "  [2/7] PYTHON 3"
echo "----------------------------------------------------------------"

if ! command -v python3 >/dev/null 2>&1; then
  echo "  Python 3 não encontrado. Instalando via Homebrew..."
  if command -v brew >/dev/null 2>&1; then
    brew install python@3.12
  else
    echo "  ❌ Homebrew indisponível. Instale o Python 3 manualmente:"
    echo "     https://www.python.org/downloads/macos/"
    read -r -p "Pressione Enter para fechar..."; exit 1
  fi
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "  ❌ Python continua indisponível. Feche e abra o Terminal de novo."
  read -r -p "Pressione Enter para fechar..."; exit 1
fi

PYSYS="$(command -v python3)"
echo "  ✅ Python do sistema: $($PYSYS --version 2>&1)"

# --- venv ---------------------------------------------------------------------
VENV_PY="$PROJ/venv/bin/python"
NOVA_VENV=0

if [ -x "$VENV_PY" ]; then
  if ! "$VENV_PY" -c "import sys" >/dev/null 2>&1; then
    echo "  🔧 venv quebrada (provavelmente copiada de outro Mac). Recriando..."
    rm -rf "$PROJ/venv"
    NOVA_VENV=1
  fi
else
  NOVA_VENV=1
fi

if [ "$NOVA_VENV" = "1" ]; then
  echo "  Criando ambiente virtual em venv/ ..."
  "$PYSYS" -m venv "$PROJ/venv"
  if [ $? -ne 0 ]; then
    echo "  ❌ Falha ao criar a venv."
    read -r -p "Pressione Enter para fechar..."; exit 1
  fi
fi

if [ ! -x "$VENV_PY" ]; then
  echo "  ❌ venv/bin/python não existe após a criação."
  read -r -p "Pressione Enter para fechar..."; exit 1
fi
echo "  ✅ venv: $("$VENV_PY" --version 2>&1)"

# =============================================================================
echo ""
echo "----------------------------------------------------------------"
echo "  [3/7] DEPENDÊNCIAS PYTHON (requirements.txt)"
echo "----------------------------------------------------------------"
echo "  streamlit, fastapi, uvicorn, playwright, pydantic, httpx[socks],"
echo "  pdfplumber, pytesseract, Pillow, pyotp, tenacity, openpyxl,"
echo "  python-dotenv, python-multipart, PyJWT, requests, dnspython"
echo ""
echo "  Instalando (pode levar alguns minutos)..."

"$VENV_PY" -m pip install --upgrade pip setuptools wheel >/dev/null 2>&1
if "$VENV_PY" -m pip install -r "$PROJ/requirements.txt"; then
  echo "  ✅ Dependências Python instaladas."
else
  echo "  ❌ Falha ao instalar o requirements.txt. Veja o erro acima."
  ERROS=$((ERROS + 1))
fi

if [ -f "$PROJ/requirements-dev.txt" ]; then
  echo "  Instalando dependências de desenvolvimento (pytest)..."
  "$VENV_PY" -m pip install -r "$PROJ/requirements-dev.txt" >/dev/null 2>&1
fi

# =============================================================================
echo ""
echo "----------------------------------------------------------------"
echo "  [4/7] PLAYWRIGHT — navegador Chromium"
echo "----------------------------------------------------------------"

if "$VENV_PY" -m playwright install chromium; then
  echo "  ✅ Chromium do Playwright instalado."
  echo "ok" > "$PROJ/venv/.pw_ok"
else
  echo "  ⚠️  Falha ao baixar o Chromium do Playwright."
  echo "     Rode depois: venv/bin/python -m playwright install chromium"
  ERROS=$((ERROS + 1))
fi

# =============================================================================
echo ""
echo "----------------------------------------------------------------"
echo "  [5/7] NODE.JS + NPM (frontend)"
echo "----------------------------------------------------------------"

if ! command -v node >/dev/null 2>&1; then
  echo "  Node.js não encontrado. Instalando via Homebrew..."
  if command -v brew >/dev/null 2>&1; then
    brew install node
  else
    echo "  ⚠️  Homebrew indisponível. Instale o Node LTS manualmente:"
    echo "     https://nodejs.org/en/download"
  fi
fi

if ! command -v node >/dev/null 2>&1; then
  echo "  ⚠️  Node continua indisponível."
  ERROS=$((ERROS + 1))
else
  echo "  ✅ Node: $(node -v)   npm: $(npm -v)"

  # --- frontend
  if [ -f "$PROJ/frontend/package.json" ]; then
    echo ""
    echo "  Instalando frontend (painel, porta 5173)..."
    cd "$PROJ/frontend"
    if [ -d "node_modules" ]; then
      if node -e "require.resolve('vite')" >/dev/null 2>&1; then
        echo "  ✅ node_modules já instalado."
      else
        echo "  🔧 node_modules incompleto. Reinstalando limpo..."
        rm -rf node_modules package-lock.json
        if npm install; then
          echo "  ✅ Frontend instalado."
        else
          echo "  ⚠️  npm install falhou no frontend."
          ERROS=$((ERROS + 1))
        fi
      fi
    else
      if npm install; then
        echo "  ✅ Frontend instalado."
      else
        echo "  ⚠️  npm install falhou no frontend."
        ERROS=$((ERROS + 1))
      fi
    fi
    cd "$PROJ"
  fi
fi

# =============================================================================
echo ""
echo "----------------------------------------------------------------"
echo "  [6/7] TESSERACT OCR (+ idioma português)"
echo "----------------------------------------------------------------"

TESS_EXE=""
if command -v tesseract >/dev/null 2>&1; then
  TESS_EXE="$(command -v tesseract)"
fi

if [ -z "$TESS_EXE" ]; then
  echo "  Tesseract não encontrado. Instalando via Homebrew..."
  if command -v brew >/dev/null 2>&1; then
    if brew install tesseract; then
      TESS_EXE="$(command -v tesseract)"
    fi
  else
    echo "  ⚠️  Homebrew indisponível. Instale manualmente:"
    echo "     brew install tesseract"
  fi
fi

if [ -z "$TESS_EXE" ]; then
  echo "  ⚠️  Tesseract não instalado — o OCR do comprovante vai falhar."
  ERROS=$((ERROS + 1))
else
  echo "  ✅ Tesseract: $TESS_EXE"

  # Idioma português
  TESSDATA=""
  if command -v brew >/dev/null 2>&1; then
    TESSDATA="$(brew --prefix)/share/tessdata"
  fi
  if [ -z "$TESSDATA" ] || [ ! -d "$TESSDATA" ]; then
    TESSDATA="/usr/local/share/tessdata"
  fi

  if [ -d "$TESSDATA" ] && [ ! -f "$TESSDATA/por.traineddata" ]; then
    echo "  Baixando idioma português (por.traineddata)..."
    curl -fsSL -o "$TESSDATA/por.traineddata" \
      "https://raw.githubusercontent.com/tesseract-ocr/tessdata/main/por.traineddata" 2>/dev/null \
      && echo "  ✅ Idioma português instalado." \
      || echo "  ⚠️  Falha ao baixar por.traineddata."
  elif [ -f "$TESSDATA/por.traineddata" ]; then
    echo "  ✅ Idioma português já presente."
  fi

  # Grava TESSERACT_CMD no .env se ainda não existir
  if [ -f "$PROJ/.env" ]; then
    if ! grep -q "^TESSERACT_CMD=." "$PROJ/.env" 2>/dev/null; then
      cp "$PROJ/.env" "$PROJ/.env.bak"
      echo "TESSERACT_CMD=$TESS_EXE" >> "$PROJ/.env"
      echo "  ✅ TESSERACT_CMD gravado no .env (backup em .env.bak)"
    else
      echo "  ✅ TESSERACT_CMD já preenchido no .env (não alterado)"
    fi
  else
    echo "  ⚠️  .env não encontrado — crie o .env antes de rodar o robô."
  fi
fi

# =============================================================================
echo ""
echo "----------------------------------------------------------------"
echo "  [7/7] PREFLIGHT — checagem do ambiente"
echo "----------------------------------------------------------------"

mkdir -p "$PROJ/.state"

if [ -f "$PROJ/preflight.py" ]; then
  "$VENV_PY" "$PROJ/preflight.py"
else
  echo "  (preflight.py não encontrado — pulando)"
fi

# --- permissões dos scripts .command -----------------------------------------
chmod +x "$PROJ"/*.command 2>/dev/null || true

# =============================================================================
echo ""
echo "================================================================"
if [ "$ERROS" = "0" ]; then
  echo "   ✅ INSTALAÇÃO CONCLUÍDA"
else
  echo "   ⚠️  INSTALAÇÃO CONCLUÍDA COM $ERROS AVISO(S) — veja acima"
fi
echo "================================================================"
echo ""
echo "  Falta ainda (não dá pra automatizar):"
echo "    - AdsPower aberto e logado  (API local em 127.0.0.1:50325)"
echo "    - .env preenchido: PANEL_USERNAME, PANEL_PASSWORD, PANEL_AUTH_SECRET"
echo ""
echo "  Para rodar:"
echo "    INICIAR_BACKEND.command    → API   em http://localhost:8000"
echo "    INICIAR_FRONTEND.command   → Painel em http://localhost:5173"
echo "    INICIAR_TUDO.command       → sobe os dois de uma vez"
echo "    INICIAR_AUTOMACAO.command  → sobe tudo + abre AdsPower e Chrome"
echo ""
read -r -p "Pressione Enter para fechar..."
