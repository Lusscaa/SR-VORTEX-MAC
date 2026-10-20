@echo off
setlocal EnableExtensions
title Meta-Farm - Iniciar tudo

REM =============================================================================
REM  Meta-Farm - sobe backend (8000) e frontend (5173) em janelas separadas
REM =============================================================================

cd /d "%~dp0"

echo.
echo ================================================================
echo    META-FARM - INICIANDO BACKEND + FRONTEND
echo ================================================================
echo.

if not exist "INICIAR_BACKEND.bat" (
  echo [ERRO] INICIAR_BACKEND.bat nao encontrado nesta pasta.
  pause & exit /b 1
)

echo  Abrindo o backend  ^(http://localhost:8000^)...
start "Meta-Farm Backend" cmd /k "%~dp0INICIAR_BACKEND.bat"

echo  Aguardando o backend subir ^(15s^)...
timeout /t 15 /nobreak >nul

echo  Abrindo o frontend ^(http://localhost:5173^)...
start "Meta-Farm Frontend" cmd /k "%~dp0INICIAR_FRONTEND.bat"

echo  Aguardando o Vite compilar ^(12s^)...
timeout /t 12 /nobreak >nul

start "" http://localhost:5173

echo.
echo  Pronto. Backend e frontend rodando em janelas separadas.
echo  Para parar: feche as duas janelas ou pressione Ctrl+C em cada uma.
echo.
echo  Lembre-se: o AdsPower precisa estar ABERTO e LOGADO para a
echo  esteira rodar ^(API local em 127.0.0.1:50325^).
echo.
pause
