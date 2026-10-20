@echo off
setlocal EnableExtensions EnableDelayedExpansion
title Meta-Farm - Backend (porta 8000)
color 0A

REM =============================================================================
REM  Meta-Farm - Backend (FastAPI/Uvicorn, porta 8000) - Windows
REM  Auto-reparo: recria a venv se estiver quebrada e reinstala as dependencias
REM  quando o requirements.txt mudar.
REM =============================================================================

cd /d "%~dp0"
set "PROJ=%CD%"

echo.
echo ================================================================
echo    META-FARM BACKEND - Uvicorn (porta 8000)
echo ================================================================
echo.
echo  Pasta: %PROJ%
echo.

if not exist "requirements.txt" (
  echo [ERRO] requirements.txt nao encontrado. Este .bat precisa estar na
  echo        pasta raiz do projeto meta-farm.
  pause & exit /b 1
)

REM --- Python do sistema -------------------------------------------------------
set "PYSYS="
where py >nul 2>&1 && (py -3 --version >nul 2>&1 && set "PYSYS=py -3")
if not defined PYSYS ( where python >nul 2>&1 && set "PYSYS=python" )
if not defined PYSYS (
  echo [ERRO] Python 3 nao encontrado. Rode INSTALAR_DEPENDENCIAS.bat primeiro.
  pause & exit /b 1
)

REM --- venv --------------------------------------------------------------------
set "VENV_PY=%PROJ%\venv\Scripts\python.exe"
set "NOVA_VENV=0"

if exist "%VENV_PY%" (
  "%VENV_PY%" -c "import sys" >nul 2>&1
  if errorlevel 1 set "NOVA_VENV=1"
) else (
  set "NOVA_VENV=1"
)

if "!NOVA_VENV!"=="1" (
  echo  venv ausente ou quebrada. Recriando...
  rmdir /s /q "%PROJ%\venv" >nul 2>&1
  %PYSYS% -m venv "%PROJ%\venv"
  if errorlevel 1 ( echo [ERRO] Falha ao criar a venv. & pause & exit /b 1 )
)

for /f "delims=" %%V in ('"%VENV_PY%" --version 2^>^&1') do set "VENVVER=%%V"
echo  OK - venv: %VENVVER%

REM --- dependencias: so reinstala quando o requirements muda -------------------
set "REQ_HASH="
for /f "skip=1 delims=" %%H in ('certutil -hashfile "%PROJ%\requirements.txt" SHA1 2^>nul') do (
  if not defined REQ_HASH set "REQ_HASH=%%H"
)
set "REQ_HASH=%REQ_HASH: =%"
if not defined REQ_HASH set "REQ_HASH=sem-hash"
set "SENT=%PROJ%\venv\.deps_hash"
set "PRECISA=0"
if not exist "%SENT%" (
  set "PRECISA=1"
) else (
  set "OLD_HASH="
  for /f "usebackq delims=" %%H in ("%SENT%") do set "OLD_HASH=%%H"
  if not "!OLD_HASH!"=="!REQ_HASH!" set "PRECISA=1"
)

if "!PRECISA!"=="1" (
  echo  Instalando dependencias do requirements.txt ^(pode levar alguns minutos^)...
  "%VENV_PY%" -m pip install --upgrade pip >nul 2>&1
  "%VENV_PY%" -m pip install -r "%PROJ%\requirements.txt"
  if errorlevel 1 (
    echo [ERRO] Falha ao instalar as dependencias. Veja o erro acima.
    pause & exit /b 1
  )
  > "%SENT%" echo !REQ_HASH!
  echo  OK - dependencias instaladas.
) else (
  echo  OK - dependencias ja instaladas ^(requirements sem mudancas^).
)

REM --- navegador do Playwright: so na primeira vez -----------------------------
if not exist "%PROJ%\venv\.pw_ok" (
  echo  Instalando o Chromium do Playwright ^(uma unica vez^)...
  "%VENV_PY%" -m playwright install chromium >nul 2>&1
  if not errorlevel 1 (
    > "%PROJ%\venv\.pw_ok" echo ok
  ) else (
    echo  ^(aviso: nao consegui instalar o Chromium agora - o servidor sobe assim mesmo^)
  )
)

if not exist "%PROJ%\.state" mkdir "%PROJ%\.state" >nul 2>&1

echo.
echo  Iniciando o backend em http://localhost:8000 ...
echo  ^(Ctrl+C para parar^)
echo.
"%VENV_PY%" -m uvicorn api_server:app --host 0.0.0.0 --port 8000 --reload

echo.
echo  Backend encerrado.
pause
