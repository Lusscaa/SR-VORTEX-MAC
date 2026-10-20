@echo off
setlocal EnableExtensions EnableDelayedExpansion
title Meta-Farm - Frontend (porta 5173)
color 0E

REM =============================================================================
REM  Meta-Farm - Frontend (React/Vite, porta 5173) - Windows
REM  Auto-reparo: reinstala node_modules limpo quando estao ausentes ou vieram
REM  de outra maquina/arquitetura (corrige erros do @rollup/rollup-*).
REM =============================================================================

cd /d "%~dp0\frontend"
set "FE=%CD%"

echo.
echo ================================================================
echo    META-FARM FRONTEND - React/Vite (porta 5173)
echo ================================================================
echo.
echo  Pasta: %FE%
echo.

if not exist "package.json" (
  echo [ERRO] package.json nao encontrado. Este .bat precisa estar na pasta
  echo        raiz do projeto ^(ao lado da pasta frontend^).
  pause & exit /b 1
)

REM --- amplia o PATH com os locais padrao do Node ------------------------------
if exist "%ProgramFiles%\nodejs" set "PATH=%PATH%;%ProgramFiles%\nodejs"
if exist "%APPDATA%\npm" set "PATH=%PATH%;%APPDATA%\npm"

where node >nul 2>&1
if errorlevel 1 (
  echo [ERRO] Node.js nao encontrado. Rode INSTALAR_DEPENDENCIAS.bat primeiro
  echo        ^(ou instale o Node LTS em https://nodejs.org^).
  pause & exit /b 1
)

for /f "delims=" %%V in ('node -v 2^>^&1') do set "NODEVER=%%V"
for /f "delims=" %%V in ('npm -v 2^>^&1') do set "NPMVER=%%V"
echo  OK - Node: %NODEVER%   npm: %NPMVER%

REM --- decide se precisa reinstalar --------------------------------------------
set "NEED_INSTALL=0"
set "ARCH=%PROCESSOR_ARCHITECTURE%"

if not exist "node_modules" (
  set "NEED_INSTALL=1"
) else (
  if not exist "node_modules\.mf_arch" (
    echo  node_modules parece ter vindo de outra maquina. Reinstalando limpo...
    set "NEED_INSTALL=1"
  ) else (
    set "OLD_ARCH="
    for /f "usebackq delims=" %%A in ("node_modules\.mf_arch") do set "OLD_ARCH=%%A"
    if not "!OLD_ARCH!"=="%ARCH%" (
      echo  node_modules de outra arquitetura ^(!OLD_ARCH!^). Reinstalando limpo...
      set "NEED_INSTALL=1"
    )
  )
  if "!NEED_INSTALL!"=="0" (
    node -e "require.resolve('vite')" >nul 2>&1
    if errorlevel 1 set "NEED_INSTALL=1"
  )
)

if "!NEED_INSTALL!"=="1" (
  echo  Removendo node_modules e package-lock.json antigos...
  rmdir /s /q node_modules >nul 2>&1
  del /q package-lock.json >nul 2>&1
  echo  Rodando npm install ^(pode levar alguns minutos^)...
  call npm install
  if errorlevel 1 (
    echo [ERRO] Falha no npm install. Veja o erro acima.
    pause & exit /b 1
  )
  > "node_modules\.mf_arch" echo %ARCH%
  echo  OK - dependencias do frontend instaladas.
) else (
  echo  OK - node_modules ja instalado para esta arquitetura ^(%ARCH%^).
)

echo.
echo  Iniciando o frontend em http://localhost:5173 ...
echo  ^(Ctrl+C para parar^)
echo.
call npm run dev

echo.
echo  Frontend encerrado.
pause
