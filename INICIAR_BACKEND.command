#!/bin/bash
# =============================================================================
#  Meta-Farm — Backend (FastAPI/Uvicorn, porta 8000)
#  Auto-reparo: recria a venv se estiver quebrada (ex.: projeto copiado de
#  outro Mac) e instala as dependencias do requirements.txt antes de subir.
# =============================================================================

cd "$(dirname "$0")" || exit 1
PROJ="$(pwd)"

echo "╔════════════════════════════════════════════════════════════════╗"
echo "║   Meta-Farm Backend — Uvicorn (porta 8000)                     ║"
echo "╚════════════════════════════════════════════════════════════════╝"
echo ""
echo "📂 Diretório: $PROJ"
echo ""

# --- carrega o Homebrew no PATH (Intel: /usr/local | Apple Silicon: /opt/homebrew)
for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do
  [ -x "$b" ] && eval "$("$b" shellenv)" 2>/dev/null && break
done

# --- garante Python 3 -------------------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
  echo "⚠️  Python 3 não encontrado. Tentando instalar via Homebrew..."
  if command -v brew >/dev/null 2>&1; then
    brew install python
  else
    echo "❌ Homebrew não encontrado. Instale o Python 3 e rode de novo."
    read -r -p "Pressione Enter para fechar..."; exit 1
  fi
fi
PYSYS="$(command -v python3)"
echo "🐍 Python do sistema: $PYSYS ($($PYSYS --version 2>&1))"

# --- valida a venv; recria se estiver quebrada ------------------------------
venv_ok() { [ -x "venv/bin/python" ] && venv/bin/python -c "import sys" >/dev/null 2>&1; }

if ! venv_ok; then
  echo "🔧 venv ausente ou quebrada (provavelmente copiada de outro Mac). Recriando..."
  rm -rf venv
  "$PYSYS" -m venv venv || { echo "❌ Falha ao criar a venv."; read -r -p "Enter para fechar..."; exit 1; }
  NOVA_VENV=1
fi
VENV_PY="$PROJ/venv/bin/python"
echo "✅ venv: $("$VENV_PY" --version 2>&1)"

# --- instala/atualiza dependencias (só quando o requirements muda) ----------
REQ_HASH="$(shasum requirements.txt 2>/dev/null | awk '{print $1}')"
SENT="venv/.deps_hash"
if [ "${NOVA_VENV:-0}" = "1" ] || [ ! -f "$SENT" ] || [ "$(cat "$SENT" 2>/dev/null)" != "$REQ_HASH" ]; then
  echo "📦 Instalando dependências do requirements.txt (pode levar alguns minutos)..."
  "$VENV_PY" -m pip install --upgrade pip >/dev/null 2>&1
  if "$VENV_PY" -m pip install -r requirements.txt; then
    echo "$REQ_HASH" > "$SENT"
    echo "✅ Dependências instaladas."
  else
    echo "❌ Falha ao instalar dependências. Veja o erro acima."
    read -r -p "Pressione Enter para fechar..."; exit 1
  fi
else
  echo "✅ Dependências já instaladas (requirements sem mudanças)."
fi

# --- extras best-effort (não interrompem se falharem) -----------------------
# tesseract: necessário para OCR (pytesseract) em tempo de execução
if ! command -v tesseract >/dev/null 2>&1 && command -v brew >/dev/null 2>&1; then
  echo "🔎 Instalando tesseract (OCR)..."; brew install tesseract >/dev/null 2>&1 || true
fi
# navegadores do Playwright (usados pela automação); só na primeira vez
if [ ! -f "venv/.pw_ok" ]; then
  echo "🌐 Instalando navegador do Playwright (Chromium)... (uma única vez)"
  if "$VENV_PY" -m playwright install chromium >/dev/null 2>&1; then
    touch "venv/.pw_ok"
  else
    echo "   (aviso: não consegui instalar o Chromium do Playwright agora — o servidor sobe mesmo assim)"
  fi
fi

echo ""
echo "🔄 Iniciando Backend em http://localhost:8000 ..."
echo ""
exec "$VENV_PY" -m uvicorn api_server:app --host 0.0.0.0 --port 8000 --reload
