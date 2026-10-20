$ErrorActionPreference = 'Stop'
$ProgressPreference    = 'SilentlyContinue'

$src   = $PSScriptRoot
$name  = 'SR-VORTEX'
$stamp = Get-Date -Format 'yyyyMMdd-HHmm'
$tmp   = Join-Path $env:TEMP ('mf_zip_' + [guid]::NewGuid().ToString('N').Substring(0,8))
$stage = Join-Path $tmp $name
$zip   = Join-Path ([Environment]::GetFolderPath('UserProfile') + '\Downloads') ("$name-$stamp.zip")

Write-Host ''
Write-Host '=====================================================' -ForegroundColor Cyan
Write-Host '  SR-VORTEX - GERADOR DE ZIP LIMPO' -ForegroundColor Cyan
Write-Host '=====================================================' -ForegroundColor Cyan
Write-Host ''
Write-Host "Origem : $src"
Write-Host "Destino: $zip"
Write-Host ''

# ---------------------------------------------------------------------------
# 1) Copia o projeto, pulando tudo que e especifico da sua maquina
# ---------------------------------------------------------------------------
New-Item -ItemType Directory -Path $stage -Force | Out-Null

$xd = @(
    # ambientes que nao funcionam em outra maquina
    'venv', '.venv', 'node_modules',
    # estado / dados da SUA instancia
    '.state', 'uploads', 'artifacts',
    # caches
    '__pycache__', '.pytest_cache', '.vite', '.mypy_cache', '.ruff_cache',
    # git / IDE / claude
    '.git', '.claude', '.idea', '.vscode',
    # frontend build (sera regenerado)
    'dist',
    # outros
    'Claude outputs'
)

$xf = @(
    '*.pyc', '*.pyo', '*.log',
    '.env', '.env.bak',
    '*.db', '*.db-wal', '*.db-shm', '*.sqlite', '*.sqlite3',
    '.deps_hash', '.pw_ok', '*.py.backup',
    'Thumbs.db', '.DS_Store',
    'package-lock.json',
    '_restart_trigger.py'
)

Write-Host '[1/5] Copiando codigo fonte (sem venv, node_modules, .state, .git)...' -ForegroundColor Yellow
$rcArgs = @($src, $stage, '/E', '/NFL', '/NDL', '/NJH', '/NJS', '/NP', '/R:1', '/W:1', '/MT:16',
            '/XD') + $xd + @('/XF') + $xf
& robocopy.exe @rcArgs | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy falhou (codigo $LASTEXITCODE)" }

# ---------------------------------------------------------------------------
# 2) Recria pastas vazias que o app precisa
# ---------------------------------------------------------------------------
Write-Host '[2/5] Criando pastas vazias necessarias...' -ForegroundColor Yellow
foreach ($d in @('.state', '.state\screenshots', 'uploads')) {
    $p = Join-Path $stage $d
    New-Item -ItemType Directory -Path $p -Force | Out-Null
    Set-Content -Path (Join-Path $p '.gitkeep') -Value '' -NoNewline
}

# ---------------------------------------------------------------------------
# 3) .env zerado a partir do .env.example
# ---------------------------------------------------------------------------
Write-Host '[3/5] Copiando .env zerado (a partir do .env.example)...' -ForegroundColor Yellow
$example = Join-Path $stage '.env.example'
if (Test-Path $example) {
    Copy-Item $example (Join-Path $stage '.env') -Force
    Write-Host '  OK - .env criado a partir do .env.example' -ForegroundColor Green
} else {
    Write-Host '  AVISO: .env.example nao encontrado' -ForegroundColor Red
}

# ---------------------------------------------------------------------------
# 4) Remove arquivos que nao servem pro destinatario
# ---------------------------------------------------------------------------
Write-Host '[4/5] Limpando arquivos desnecessarios...' -ForegroundColor Yellow
$remover = @(
    'mensagem-claudio.txt',
    'netlify_selftest.py',
    'CORRIGIR_CONFIG.bat',
    'CORRIGIR_PYTHON.bat',
    'ATUALIZACOES_COMPLETAS.md',
    'INSTRUCOES_DEPLOY.md',
    'LEIA-ME-ZERADO.md',
    'deploy-fix.py',
    'deploy-fix.sh',
    'test_fb_static_cache.py',
    'requirements-dev.txt',
    'PREPARAR_PARA_NOVO_MAC.command',
    'LIMPAR_CONTAS_DESCARTADAS.command',
    'LIMPAR_CONTAS_DESCARTADAS.py'
)
foreach ($f in $remover) {
    $fp = Join-Path $stage $f
    if (Test-Path $fp) { Remove-Item $fp -Force; Write-Host "  removido: $f" }
}

$testsDir = Join-Path $stage 'tests'
if (Test-Path $testsDir) { Remove-Item $testsDir -Recurse -Force; Write-Host '  removido: tests/' }

# ---------------------------------------------------------------------------
# 5) Compacta
# ---------------------------------------------------------------------------
Write-Host '[5/5] Compactando...' -ForegroundColor Yellow
if (Test-Path $zip) { Remove-Item $zip -Force }
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::CreateFromDirectory(
    $stage, $zip, [System.IO.Compression.CompressionLevel]::Optimal, $true)

$files = (Get-ChildItem $stage -Recurse -File | Measure-Object).Count
$mb    = [math]::Round((Get-Item $zip).Length / 1MB, 1)
Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue

Write-Host ''
Write-Host '=====================================================' -ForegroundColor Green
Write-Host '  ZIP PRONTO' -ForegroundColor Green
Write-Host '=====================================================' -ForegroundColor Green
Write-Host "  Arquivo : $zip"
Write-Host "  Tamanho : $mb MB   ($files arquivos)"
Write-Host ''
Write-Host '  O que NAO esta no zip (sera recriado pelo instalador):'
Write-Host '    - venv/             (ambiente Python)'
Write-Host '    - node_modules/     (deps do frontend)'
Write-Host '    - .state/           (banco, logs, screenshots)'
Write-Host '    - .env com chaves   (vem zerado)'
Write-Host ''
Write-Host '  Envie o zip e peca pro seu amigo seguir o arquivo:'
Write-Host '  "LEIA ANTES DE INICIAR O BOT.txt"'
Write-Host ''
Read-Host 'Enter para fechar'
