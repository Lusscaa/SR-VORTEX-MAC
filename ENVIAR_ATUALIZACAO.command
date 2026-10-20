#!/bin/bash
# =============================================================================
#  SR-VORTEX — Enviar Atualização para o Repositório (macOS)
# =============================================================================

cd "$(dirname "$0")" || exit 1

echo ""
echo "════════════════════════════════════════════════════════════════"
echo "   ENVIAR ATUALIZAÇÃO PARA O REPOSITÓRIO"
echo "════════════════════════════════════════════════════════════════"
echo ""

echo "[1/3] Adicionando arquivos..."
git add -A

echo "[2/3] Criando commit..."
read -r -p "Mensagem do commit (ou Enter para padrão): " MSG
[ -z "$MSG" ] && MSG="Atualização do bot"

git commit -m "$MSG"

echo "[3/3] Enviando para o GitHub..."
git push origin main

echo ""
echo "════════════════════════════════════════════════════════════════"
echo "   ✅ PRONTO! Atualização enviada."
echo "   O amigo só precisa dar duplo clique no ATUALIZAR.command"
echo "════════════════════════════════════════════════════════════════"
echo ""
read -r -p "Pressione Enter para fechar..."
