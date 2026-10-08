#!/bin/bash
# =============================================================================
#  SR-VORTEX — Automação Completa (macOS)
#  Abre: Backend (8000) + Frontend (5173) + AdsPower + Google Chrome
#  Os scripts de Backend e Frontend se auto-reparam (instalam dependências).
# =============================================================================

cd "$(dirname "$0")" || exit 1
PROJ="$(pwd)"

echo "╔════════════════════════════════════════════════════════════════╗"
echo "║   SR-VORTEX — Automação Completa                              ║"
echo "║   Backend + Frontend + AdsPower + Google Chrome               ║"
echo "╚════════════════════════════════════════════════════════════════╝"
echo ""

# garante que os sub-scripts sejam executáveis
chmod +x "$PROJ/INICIAR_BACKEND.command" "$PROJ/INICIAR_FRONTEND.command" 2>/dev/null

# --- Backend em nova janela do Terminal ------------------------------------
echo "🚀 Iniciando Backend..."
open -a Terminal "$PROJ/INICIAR_BACKEND.command"

sleep 2

# --- Frontend em nova janela do Terminal -----------------------------------
echo "🚀 Iniciando Frontend..."
open -a Terminal "$PROJ/INICIAR_FRONTEND.command"

sleep 2

# --- AdsPower (não falha se não estiver instalado) --------------------------
echo "🚀 Abrindo AdsPower..."
open -a "AdsPower Global" 2>/dev/null || echo "   (aviso: AdsPower Global não encontrado — abra manualmente se precisar)"

sleep 1

# --- Google Chrome ----------------------------------------------------------
echo "🚀 Abrindo Google Chrome..."
open -a "Google Chrome" 2>/dev/null || echo "   (aviso: Google Chrome não encontrado)"

# Espera o frontend subir e abre no navegador
sleep 15
open "http://localhost:5173" 2>/dev/null

echo ""
echo "╔════════════════════════════════════════════════════════════════╗"
echo "║   ✅ Automação iniciada!                                       ║"
echo "╠════════════════════════════════════════════════════════════════╣"
echo "║   Backend:  http://localhost:8000                              ║"
echo "║   Frontend: http://localhost:5173                              ║"
echo "║   AdsPower: Aberto                                             ║"
echo "║   Chrome:   Aberto                                             ║"
echo "╠════════════════════════════════════════════════════════════════╣"
echo "║   Na 1ª vez, as janelas de Backend/Frontend vão instalar as   ║"
echo "║   dependências — aguarde elas dizerem que subiram.            ║"
echo "╚════════════════════════════════════════════════════════════════╝"
echo ""
echo "Pode fechar esta janela. As outras duas mantêm os servidores rodando."
