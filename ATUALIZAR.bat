@echo off
setlocal EnableExtensions EnableDelayedExpansion
title SR-VORTEX - Atualizar
color 0B

REM =============================================================================
REM  SR-VORTEX - Atualiza o bot para a versao mais recente
REM
REM  O que ele faz:
REM    1) git pull  (baixa as atualizacoes do codigo)
REM    2) pip install -r requirements.txt  (instala deps Python novas)
REM    3) npm install no frontend  (instala deps Node novas)
REM
REM  O que ele NAO mexe (seus dados ficam intactos):
REM    - .env  (suas chaves e senhas)
REM    - .state/  (banco, logs, screenshots)
REM    - uploads/
REM    - venv/  (so instala pacotes novos por cima)
REM =============================================================================

cd /d "%~dp0"
set "PROJ=%CD%"

echo.
echo ================================================================
echo    SR-VORTEX - ATUALIZADOR
echo ================================================================
echo.
echo  Pasta: %PROJ%
echo.

REM --- Verifica se eh um repositorio git --------------------------------------
if not exist ".git" (
  echo  [ERRO] Esta pasta nao e um repositorio git.
  echo         Voce precisa ter clonado o projeto com:
  echo         git clone https://github.com/Lusscaa/SR-VORTEX-MAC.git
  echo.
  goto :FIM
)

REM --- Verifica se git esta instalado -----------------------------------------
where git >nul 2>&1
if errorlevel 1 (
  echo  [ERRO] Git nao encontrado. Instale com:
  echo         winget install --id Git.Git -e
  echo.
  goto :FIM
)

REM =============================================================================
echo ----------------------------------------------------------------
echo  [1/3] BAIXANDO ATUALIZACOES (git pull)
echo ----------------------------------------------------------------
echo.

REM --- Salva alteracoes locais se houver (nao perde nada) ---------------------
git diff --quiet >nul 2>&1
if errorlevel 1 (
  echo  Voce tem alteracoes locais. Salvando antes de atualizar...
  git stash push -u -m "backup-antes-atualizar-%DATE:~0,10%-%TIME:~0,5%"
  if errorlevel 1 (
    echo  [AVISO] Nao consegui salvar suas alteracoes. Continuando...
  ) else (
    echo  OK - alteracoes salvas. Serao restauradas no final.
    set "STASHED=1"
  )
)

git pull origin main
if errorlevel 1 (
  echo.
  echo  [ERRO] Falha ao baixar atualizacoes.
  echo         Verifique sua conexao com a internet.
  echo         Se o erro persistir, fale com o Lusca.
  echo.
  goto :RESTAURAR
)
echo.
echo  OK - codigo atualizado.

REM =============================================================================
echo.
echo ----------------------------------------------------------------
echo  [2/3] ATUALIZANDO DEPENDENCIAS PYTHON
echo ----------------------------------------------------------------

set "VENV_PY=%PROJ%\venv\Scripts\python.exe"
if not exist "%VENV_PY%" (
  echo  [AVISO] venv nao encontrada. Rode INSTALAR_DEPENDENCIAS.bat primeiro.
  goto :NODE
)

"%VENV_PY%" -m pip install -r "%PROJ%\requirements.txt" --quiet
if errorlevel 1 (
  echo  [AVISO] Falha ao atualizar deps Python. Rode INSTALAR_DEPENDENCIAS.bat.
) else (
  echo  OK - dependencias Python atualizadas.
)

REM --- Atualiza Playwright se necessario --------------------------------------
"%VENV_PY%" -m playwright install chromium >nul 2>&1

:NODE
REM =============================================================================
echo.
echo ----------------------------------------------------------------
echo  [3/3] ATUALIZANDO DEPENDENCIAS DO FRONTEND
echo ----------------------------------------------------------------

where node >nul 2>&1
if errorlevel 1 (
  echo  [AVISO] Node.js nao encontrado. Pulando frontend.
  goto :RESTAURAR
)

if exist "%PROJ%\frontend\package.json" (
  pushd "%PROJ%\frontend"
  call npm install --silent
  if errorlevel 1 (
    echo  [AVISO] Falha ao atualizar deps do frontend.
  ) else (
    echo  OK - dependencias do frontend atualizadas.
  )
  popd
) else (
  echo  [AVISO] frontend/package.json nao encontrado.
)

:RESTAURAR
REM --- Restaura alteracoes locais se salvou -----------------------------------
if defined STASHED (
  echo.
  echo  Restaurando suas alteracoes locais...
  git stash pop >nul 2>&1
  if errorlevel 1 (
    echo  [AVISO] Nao foi possivel restaurar automaticamente.
    echo          Suas alteracoes estao salvas. Rode: git stash list
  ) else (
    echo  OK - alteracoes locais restauradas.
  )
)

REM =============================================================================
echo.
echo ================================================================
echo    ATUALIZACAO CONCLUIDA
echo ================================================================
echo.
echo  Suas configuracoes (.env, banco, proxies, contas) NAO foram
echo  alteradas. So o codigo do bot foi atualizado.
echo.
echo  Agora rode INICIAR_TUDO.bat para usar a versao nova.
echo.

:FIM
pause
