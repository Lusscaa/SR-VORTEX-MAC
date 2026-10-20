@echo off
setlocal EnableExtensions EnableDelayedExpansion
title Meta-Farm - Instalador de Dependencias (Windows)
color 0B

REM =============================================================================
REM  Meta-Farm - Instalador de TODAS as dependencias no Windows
REM  Equivalente aos scripts .command do Mac (INICIAR_BACKEND / INICIAR_FRONTEND)
REM
REM  Instala / configura:
REM    1) Python 3        -> venv + requirements.txt
REM    2) Playwright      -> navegador Chromium
REM    3) Node.js + npm   -> node_modules do frontend e do admin-ui
REM    4) Tesseract OCR   -> binario + idioma portugues (por.traineddata)
REM    5) .env            -> TESSERACT_CMD apontando pro binario
REM    6) Preflight       -> checagem final do ambiente
REM
REM  Uso: clique com o botao direito -> "Executar como administrador"
REM       (o modo admin so e necessario para instalar Python/Node/Tesseract)
REM =============================================================================

cd /d "%~dp0"
set "PROJ=%CD%"
set "ERROS=0"

echo.
echo ================================================================
echo    META-FARM - INSTALADOR DE DEPENDENCIAS (Windows)
echo ================================================================
echo.
echo  Pasta do projeto: %PROJ%
echo.

REM --- garante que estamos na pasta certa --------------------------------------
if not exist "requirements.txt" (
  echo [ERRO] requirements.txt nao encontrado nesta pasta.
  echo        Coloque este .bat dentro da pasta do projeto meta-farm.
  goto :FIM_ERRO
)

REM --- cria .env a partir do .env.example se nao existir ----------------------
if not exist "%PROJ%\.env" (
  if exist "%PROJ%\.env.example" (
    copy /y "%PROJ%\.env.example" "%PROJ%\.env" >nul
    echo  OK - .env criado a partir do .env.example
    echo     PREENCHA as 3 variaveis obrigatorias antes de rodar o bot:
    echo        PANEL_USERNAME, PANEL_PASSWORD, PANEL_AUTH_SECRET
    echo.
  ) else (
    echo  [AVISO] .env.example nao encontrado - crie o .env manualmente.
    echo.
  )
) else (
  echo  OK - .env ja existe ^(nao alterado^)
  echo.
)

REM --- amplia o PATH com os locais padrao de instalacao ------------------------
call :ADD_PATH "%ProgramFiles%\nodejs"
call :ADD_PATH "%ProgramFiles(x86)%\nodejs"
call :ADD_PATH "%ProgramFiles%\Tesseract-OCR"
call :ADD_PATH "%LOCALAPPDATA%\Programs\Tesseract-OCR"
call :ADD_PATH "%LOCALAPPDATA%\Microsoft\WindowsApps"
call :ADD_PATH "%APPDATA%\npm"
call :ADD_PATH "%LOCALAPPDATA%\Programs\Python\Python313"
call :ADD_PATH "%LOCALAPPDATA%\Programs\Python\Python313\Scripts"
call :ADD_PATH "%LOCALAPPDATA%\Programs\Python\Python312"
call :ADD_PATH "%LOCALAPPDATA%\Programs\Python\Python312\Scripts"
call :ADD_PATH "%LOCALAPPDATA%\Programs\Python\Python311"
call :ADD_PATH "%LOCALAPPDATA%\Programs\Python\Python311\Scripts"
call :ADD_PATH "%LOCALAPPDATA%\Programs\Python\Python310"
call :ADD_PATH "%LOCALAPPDATA%\Programs\Python\Python310\Scripts"

REM =============================================================================
echo ----------------------------------------------------------------
echo  [1/6] PYTHON 3
echo ----------------------------------------------------------------

set "PYSYS="
py -3 --version >nul 2>&1 && set "PYSYS=py -3"
if not defined PYSYS (
  python --version >nul 2>&1 && set "PYSYS=python"
)

if not defined PYSYS (
  echo  Python 3 nao encontrado. Instalando via winget...
  where winget >nul 2>&1
  if errorlevel 1 (
    echo  [ERRO] winget indisponivel. Instale o Python 3.12 manualmente:
    echo         https://www.python.org/downloads/windows/
    echo         IMPORTANTE: marque "Add python.exe to PATH" no instalador.
    goto :FIM_ERRO
  )
  winget install --id Python.Python.3.12 -e --accept-source-agreements --accept-package-agreements --silent
  call :ADD_PATH "%LOCALAPPDATA%\Programs\Python\Python312"
  call :ADD_PATH "%LOCALAPPDATA%\Programs\Python\Python312\Scripts"
  where py >nul 2>&1 && (py -3 --version >nul 2>&1 && set "PYSYS=py -3")
  if not defined PYSYS ( where python >nul 2>&1 && set "PYSYS=python" )
)

if not defined PYSYS (
  echo  [ERRO] Python continua indisponivel. FECHE e ABRA este .bat de novo
  echo         (o PATH so atualiza em um terminal novo).
  goto :FIM_ERRO
)

for /f "delims=" %%V in ('%PYSYS% --version 2^>^&1') do set "PYVER=%%V"
echo  OK - Python do sistema: %PYVER%

REM --- venv --------------------------------------------------------------------
set "VENV_PY=%PROJ%\venv\Scripts\python.exe"
set "NOVA_VENV=0"

if exist "%VENV_PY%" (
  "%VENV_PY%" -c "import sys" >nul 2>&1
  if errorlevel 1 (
    echo  venv quebrada (provavelmente copiada de outra maquina). Recriando...
    rmdir /s /q "%PROJ%\venv" >nul 2>&1
    set "NOVA_VENV=1"
  )
) else (
  set "NOVA_VENV=1"
)

if "!NOVA_VENV!"=="1" (
  echo  Criando ambiente virtual em venv\ ...
  %PYSYS% -m venv "%PROJ%\venv"
  if errorlevel 1 (
    echo  [ERRO] Falha ao criar a venv.
    goto :FIM_ERRO
  )
)

if not exist "%VENV_PY%" (
  echo  [ERRO] venv\Scripts\python.exe nao existe apos a criacao.
  goto :FIM_ERRO
)
for /f "delims=" %%V in ('"%VENV_PY%" --version 2^>^&1') do set "VENVVER=%%V"
echo  OK - venv: %VENVVER%

REM =============================================================================
echo.
echo ----------------------------------------------------------------
echo  [2/6] DEPENDENCIAS PYTHON (requirements.txt)
echo ----------------------------------------------------------------
echo  streamlit, fastapi, uvicorn, playwright, pydantic, httpx[socks],
echo  pdfplumber, pytesseract, Pillow, pyotp, tenacity, openpyxl,
echo  python-dotenv, python-multipart, PyJWT, requests, dnspython
echo.
echo  Instalando (pode levar alguns minutos)...

"%VENV_PY%" -m pip install --upgrade pip setuptools wheel
"%VENV_PY%" -m pip install -r "%PROJ%\requirements.txt"
if errorlevel 1 (
  echo  [ERRO] Falha ao instalar o requirements.txt. Veja o erro acima.
  set /a ERROS+=1
) else (
  echo  OK - dependencias Python instaladas.
)

if exist "%PROJ%\requirements-dev.txt" (
  echo  Instalando dependencias de desenvolvimento (pytest)...
  "%VENV_PY%" -m pip install -r "%PROJ%\requirements-dev.txt" >nul 2>&1
)

REM =============================================================================
echo.
echo ----------------------------------------------------------------
echo  [3/6] PLAYWRIGHT - navegador Chromium
echo ----------------------------------------------------------------

"%VENV_PY%" -m playwright install chromium
if errorlevel 1 (
  echo  [AVISO] Falha ao baixar o Chromium do Playwright.
  echo          Rode depois: venv\Scripts\python.exe -m playwright install chromium
  set /a ERROS+=1
) else (
  echo  OK - Chromium do Playwright instalado.
)

REM =============================================================================
echo.
echo ----------------------------------------------------------------
echo  [4/6] NODE.JS + NPM (frontend e admin-ui)
echo ----------------------------------------------------------------

where node >nul 2>&1
if errorlevel 1 (
  echo  Node.js nao encontrado. Instalando via winget...
  where winget >nul 2>&1
  if errorlevel 1 (
    echo  [AVISO] winget indisponivel. Instale o Node LTS manualmente:
    echo          https://nodejs.org/en/download
  ) else (
    winget install --id OpenJS.NodeJS.LTS -e --accept-source-agreements --accept-package-agreements --silent
    call :ADD_PATH "%ProgramFiles%\nodejs"
  )
)

where node >nul 2>&1
if errorlevel 1 (
  echo  [AVISO] Node continua indisponivel nesta janela.
  echo          FECHE e ABRA este .bat de novo para instalar o frontend.
  set /a ERROS+=1
  goto :PULA_NODE
)

for /f "delims=" %%V in ('node -v 2^>^&1') do set "NODEVER=%%V"
echo  OK - Node: %NODEVER%

call :NPM_INSTALL "%PROJ%\frontend"  "frontend (painel, porta 5173)"
call :NPM_INSTALL "%PROJ%\admin-ui"  "admin-ui (painel admin)"

:PULA_NODE

REM =============================================================================
echo.
echo ----------------------------------------------------------------
echo  [5/6] TESSERACT OCR (+ idioma portugues)
echo ----------------------------------------------------------------

set "TESS_EXE="
if exist "%ProgramFiles%\Tesseract-OCR\tesseract.exe"           set "TESS_EXE=%ProgramFiles%\Tesseract-OCR\tesseract.exe"
if not defined TESS_EXE if exist "%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe" set "TESS_EXE=%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"

if not defined TESS_EXE (
  echo  Tesseract nao encontrado. Instalando via winget...
  where winget >nul 2>&1
  if errorlevel 1 (
    echo  [AVISO] winget indisponivel. Baixe o instalador manualmente:
    echo          https://github.com/UB-Mannheim/tesseract/wiki
  ) else (
    winget install --id UB-Mannheim.TesseractOCR -e --accept-source-agreements --accept-package-agreements --silent
  )
  if exist "%ProgramFiles%\Tesseract-OCR\tesseract.exe"           set "TESS_EXE=%ProgramFiles%\Tesseract-OCR\tesseract.exe"
  if not defined TESS_EXE if exist "%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe" set "TESS_EXE=%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"
)

if not defined TESS_EXE (
  echo  [AVISO] Tesseract nao instalado - o OCR do comprovante vai falhar.
  set /a ERROS+=1
) else (
  echo  OK - Tesseract: %TESS_EXE%
  for %%D in ("%TESS_EXE%") do set "TESS_DIR=%%~dpD"
  set "TESSDATA=!TESS_DIR!tessdata"
  if not exist "!TESSDATA!\por.traineddata" (
    echo  Baixando idioma portugues ^(por.traineddata^)...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; try { Invoke-WebRequest -UseBasicParsing -Uri 'https://raw.githubusercontent.com/tesseract-ocr/tessdata/main/por.traineddata' -OutFile '!TESSDATA!\por.traineddata' } catch { }"
    if exist "!TESSDATA!\por.traineddata" (
      echo  OK - idioma portugues instalado.
    ) else (
      echo  [AVISO] Falha ao baixar por.traineddata. Baixe manualmente de
      echo          https://github.com/tesseract-ocr/tessdata e coloque em
      echo          !TESSDATA!
    )
  ) else (
    echo  OK - idioma portugues ja presente.
  )

  REM --- grava TESSERACT_CMD no .env se ainda nao existir ----------------------
  if exist "%PROJ%\.env" (
    REM  procura a chave JA PREENCHIDA (com pelo menos 1 caractere de valor)
    findstr /r /c:"^TESSERACT_CMD=." "%PROJ%\.env" >nul 2>&1
    if errorlevel 1 (
      REM  ausente OU vazia: grava/substitui a linha via PowerShell
      copy /y "%PROJ%\.env" "%PROJ%\.env.bak" >nul
      powershell -NoProfile -ExecutionPolicy Bypass -Command "$p='%PROJ%\.env'; $t='!TESS_EXE!'; $l=Get-Content -LiteralPath $p -Encoding UTF8; $i=0; $done=$false; foreach($x in $l){ if($x -match '^\s*TESSERACT_CMD\s*='){ $l[$i]='TESSERACT_CMD=' + $t; $done=$true }; $i++ }; if(-not $done){ $l += ('TESSERACT_CMD=' + $t) }; Set-Content -LiteralPath $p -Value $l -Encoding UTF8"
      echo  OK - TESSERACT_CMD gravado no .env ^(backup em .env.bak^)
    ) else (
      echo  OK - TESSERACT_CMD ja preenchido no .env ^(nao alterado^)
    )
  ) else (
    echo  [AVISO] .env nao encontrado - crie o .env antes de rodar o robo.
  )
)

REM =============================================================================
echo.
echo ----------------------------------------------------------------
echo  [6/6] PREFLIGHT - checagem do ambiente
echo ----------------------------------------------------------------

if not exist "%PROJ%\.state" mkdir "%PROJ%\.state" >nul 2>&1

if exist "%PROJ%\preflight.py" (
  "%VENV_PY%" "%PROJ%\preflight.py"
) else (
  echo  ^(preflight.py nao encontrado - pulando^)
)

REM =============================================================================
echo.
echo ================================================================
if "%ERROS%"=="0" (
  echo    INSTALACAO CONCLUIDA
) else (
  echo    INSTALACAO CONCLUIDA COM %ERROS% AVISO^(S^) - veja acima
)
echo ================================================================
echo.
echo  Falta ainda ^(nao da pra automatizar^):
echo    - AdsPower aberto e logado  ^(API local em 127.0.0.1:50325^)
echo    - .env preenchido: PANEL_USERNAME, PANEL_PASSWORD, PANEL_AUTH_SECRET
echo.
echo  Para rodar:
echo    INICIAR_BACKEND.bat    -^> API   em http://localhost:8000
echo    INICIAR_FRONTEND.bat   -^> Painel em http://localhost:5173
echo    INICIAR_TUDO.bat       -^> sobe os dois de uma vez
echo.
pause
exit /b 0

:FIM_ERRO
echo.
echo ================================================================
echo    INSTALACAO INTERROMPIDA - corrija o erro acima
echo ================================================================
echo.
pause
exit /b 1

REM =============================================================================
REM  Sub-rotinas
REM =============================================================================

:ADD_PATH
if exist "%~1" set "PATH=%PATH%;%~1"
exit /b 0

:NPM_INSTALL
REM  %1 = pasta   %2 = rotulo amigavel
if not exist "%~1\package.json" (
  echo  ^(pulando %~2 - package.json nao encontrado^)
  exit /b 0
)
echo.
echo  Instalando %~2 ...
pushd "%~1"
if exist "node_modules" (
  node -e "require.resolve('vite')" >nul 2>&1
  if not errorlevel 1 (
    echo  OK - node_modules ja instalado.
    popd
    exit /b 0
  )
  echo  node_modules incompleto ^(ou veio de outra maquina^). Reinstalando limpo...
  rmdir /s /q node_modules >nul 2>&1
  del /q package-lock.json >nul 2>&1
)
call npm install
if errorlevel 1 (
  echo  [AVISO] npm install falhou em %~2.
  set /a ERROS+=1
) else (
  echo  OK - %~2 instalado.
)
popd
exit /b 0
