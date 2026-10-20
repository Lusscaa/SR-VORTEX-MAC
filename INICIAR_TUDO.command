#!/bin/bash
# =============================================================================
#  SR-Vortex — Sobe backend (8000) e frontend (5173) no macOS
# =============================================================================

cd "$(dirname "$0")" || exit 1
PROJ="$(pwd)"

echo ""
echo "╔════════════════════════════════════════════════════════════════╗"
echo "║   SR-VORTEX — INICIANDO BACKEND + FRONTEND                    ║"
echo "╚════════════════════════════════════════════════════════════════╝"
echo ""

# garante que os sub-scripts estejam executáveis
chmod +x "$PROJ/INICIAR_BACKEND.command" "$PROJ/INICIAR_FRONTEND.command" 2>/dev/null

if [ ! -f "INICIAR_BACKEND.command" ]; then
  echo "❌ INICIAR_BACKEND.command não encontrado nesta pasta."
  read -r -p "Pressione Enter para sair..."; exit 1
fi

# --- Backend em nova janela do Terminal ----------------------------------------
echo "🚀 Abrindo o backend (http://localhost:8000)..."
open -a Terminal "$PROJ/INICIAR_BACKEND.command"

echo "  Aguardando o backend subir (15s)..."
sleep 15

# --- Frontend em nova janela do Terminal ---------------------------------------
echo "🚀 Abrindo o frontend (http://localhost:5173)..."
open -a Terminal "$PROJ/INICIAR_FRONTEND.command"

echo "  Aguardando o Vite compilar (12s)..."
sleep 12

open "http://localhost:5173"

echo ""
echo "╔════════════════════════════════════════════════════════════════╗"
echo "║   ✅ Backend e Frontend rodando em janelas separadas           ║"
echo "╠════════════════════════════════════════════════════════════════╣"
echo "║   Backend:  http://localhost:8000                              ║"
echo "║   Frontend: http://localhost:5173                              ║"
echo "╠════════════════════════════════════════════════════════════════╣"
echo "║   Para parar: feche as duas janelas ou Ctrl+C em cada uma.    ║"
echo "╚════════════════════════════════════════════════════════════════╝"
echo ""
echo "  Lembre-se: o AdsPower precisa estar ABERTO e LOGADO para a"
echo "  esteira rodar (API local em 127.0.0.1:50325)."
echo ""
echo "  Pode fechar esta janela. As outras duas mantêm os servidores."
