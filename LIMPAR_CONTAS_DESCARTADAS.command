#!/bin/bash
# Meta-Farm - Limpar Contas Descartadas (Double-click to run)

cd "$(dirname "$0")" || exit 1

python3 LIMPAR_CONTAS_DESCARTADAS.py

echo ""
echo "Pressione ENTER para fechar..."
read -r
