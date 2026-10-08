#!/bin/bash
# Meta-Farm - Preparar para novo MacBook (Double-click to run)

cd "$(dirname "$0")" || exit 1

cat << 'EOF'

╔════════════════════════════════════════════════════════════════╗
║   Meta-Farm - Preparar para novo MacBook                     ║
╚════════════════════════════════════════════════════════════════╝

Removendo informações desnecessárias:

EOF

# Remove histórico de runs e logs
if [ -d ".state" ]; then
    echo "   ❌ .state/ (histórico, logs, screenshots, caches)"
    rm -rf .state
fi

# Remove histórico git
if [ -d ".git" ]; then
    echo "   ❌ .git/ (histórico de commits)"
    rm -rf .git
fi

# Remove cache de Python
echo "   ❌ Caches Python (__pycache__, .pytest_cache, etc)"
find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
find . -type d -name ".mypy_cache" -exec rm -rf {} + 2>/dev/null || true
find . -type f -name "*.pyc" -delete 2>/dev/null || true
find . -type f -name "*.pyo" -delete 2>/dev/null || true

# Remove ambientes virtuais
if [ -d "venv" ]; then
    echo "   ❌ venv/ (ambiente virtual)"
    rm -rf venv
fi
if [ -d ".venv" ]; then
    echo "   ❌ .venv/"
    rm -rf .venv
fi

# Remove arquivos lock
echo "   ❌ poetry.lock, package-lock.json"
rm -f poetry.lock package-lock.json

# Remove node_modules
if [ -d "frontend/node_modules" ]; then
    echo "   ❌ frontend/node_modules/ (será reinstalado)"
    rm -rf frontend/node_modules
fi

# Remove arquivos temporários
echo "   ❌ Arquivos temporários (.DS_Store, *.tmp, *.bak)"
find . -type f -name ".DS_Store" -delete 2>/dev/null || true
find . -type f -name "*.tmp" -delete 2>/dev/null || true
find . -type f -name "*.bak" -delete 2>/dev/null || true
find . -type f -name "*.backup" -delete 2>/dev/null || true

cat << 'EOF'

✅ LIMPEZA COMPLETA!

Mantidas:
  ✓ core/ (código principal)
  ✓ api_server.py
  ✓ config.py
  ✓ requirements.txt
  ✓ frontend/
  ✓ .env (suas configurações)
  ✓ CLAUDE.md

📦 Próximo passo:
  1. Zipe esta pasta: meta-farm-main.zip
  2. Transfira para o novo MacBook
  3. No novo Mac:
     - pip install -r requirements.txt
     - cd frontend && npm install
     - Pronto! ✅

Pressione ENTER para fechar...
EOF

read -r
