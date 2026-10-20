@echo off
title Gerar ZIP limpo - meta_automation
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0GERAR_ZIP_LIMPO.ps1"
if errorlevel 1 (
  echo.
  echo Algo deu errado. Copie a mensagem acima.
  pause
)
