@echo off
chcp 65001 >nul
title Enviar Atualizacao — Sr.Vortex
echo.
echo ============================================================
echo   ENVIAR ATUALIZACAO PARA O REPOSITORIO
echo ============================================================
echo.

cd /d "D:\meta-farm-codigo-V1.01"

echo [1/3] Adicionando arquivos...
git add -A

echo [2/3] Criando commit...
set /p MSG="Mensagem do commit (ou Enter para padrao): "
if "%MSG%"=="" set MSG=Atualizacao do bot

git commit -m "%MSG%"

echo [3/3] Enviando para o GitHub...
git push vortex main

echo.
echo ============================================================
echo   PRONTO! Atualizacao enviada.
echo   O amigo so precisa dar duplo clique no ATUALIZAR.bat
echo ============================================================
echo.
pause
