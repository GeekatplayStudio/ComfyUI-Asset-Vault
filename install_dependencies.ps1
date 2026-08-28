# Geekatplay ComfyUI Asset Vault - dependency installer (PowerShell)
# Vladimir Chopine
#
# Identical in effect to install_dependencies.bat. Use whichever you prefer.
#
#   powershell -ExecutionPolicy Bypass -File .\install_dependencies.ps1

$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
$Py = Join-Path $Root 'venv\Scripts\python.exe'

function Write-Head($text) { Write-Host $text -ForegroundColor Cyan }
function Write-Step($text) { Write-Host $text -ForegroundColor Yellow }
function Write-Ok($text)   { Write-Host "      $text" -ForegroundColor Green }
function Write-Note($text) { Write-Host "      $text" -ForegroundColor DarkGray }

function Fail($text) {
    Write-Host ''
    Write-Host "[ERROR] $text" -ForegroundColor Red
    Write-Host ''
    exit 1
}

Write-Head '==================================================================='
Write-Head '    Geekatplay ComfyUI Asset Vault'
Write-Head '    Vladimir Chopine - dependency installer'
Write-Head '==================================================================='
Write-Host ''

# ------------------------------------------------------------- [1/5] Python
Write-Step '[1/5] Looking for Python 3.11 or newer ...'
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Fail @'
Python was not found on PATH.
        Install Python 3.12 from https://www.python.org/downloads/windows/
        and tick "Add python.exe to PATH" during setup.
'@
}

& python -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)'
if ($LASTEXITCODE -ne 0) {
    Fail 'The Python on PATH is too old. This app needs 3.11 or newer; 3.12 is what it is developed and tested against.'
}
Write-Ok "Found $(& python --version 2>&1)"

# --------------------------------------------------------- [2/5] virtualenv
Write-Step "[2/5] Preparing the virtual environment in $Root\venv ..."
if (Test-Path $Py) {
    Write-Note 'Already present - reusing it.'
} else {
    & python -m venv (Join-Path $Root 'venv')
    if ($LASTEXITCODE -ne 0) { Fail 'Could not create the virtual environment.' }
    Write-Ok 'Created.'
}
if (-not (Test-Path $Py)) {
    Fail "$Py is missing even though the environment was created. Delete the venv folder and run this installer again."
}

# ---------------------------------------------------- [3/5] backend packages
Write-Step "[3/5] Installing the engine's Python packages ..."
& $Py -m pip install --upgrade pip --disable-pip-version-check -q
if ($LASTEXITCODE -ne 0) { Write-Note 'pip could not upgrade itself. Continuing with the current version.' }

& $Py -m pip install -r (Join-Path $Root 'backend\requirements.txt') --disable-pip-version-check
if ($LASTEXITCODE -ne 0) {
    Fail @'
Installing the Python packages failed. Scroll up for the reason.
        The usual causes are no internet connection or a proxy that
        blocks pypi.org.
'@
}

& $Py -c 'import fastapi, uvicorn, pydantic, httpx, PIL, numpy, yaml, onnxruntime, tokenizers'
if ($LASTEXITCODE -ne 0) {
    Fail 'The packages installed but cannot all be imported. Delete the venv folder and run this installer again.'
}
Write-Ok 'Engine packages verified.'

# --------------------------------------------------- [4/5] frontend packages
Write-Step "[4/5] Installing the interface's Node packages ..."
$npm = Get-Command npm -ErrorAction SilentlyContinue
if (-not $npm) {
    if (Test-Path (Join-Path $Root 'frontend\dist\index.html')) {
        Write-Ok 'Node.js is not installed, and it is not needed: this archive'
        Write-Note 'ships the interface pre-built at frontend\dist. The engine'
        Write-Note 'serves it directly at http://127.0.0.1:8127/.'
    } else {
        Write-Host ''
        Write-Host '[WARN]  Node.js was not found on PATH, so the interface was skipped.' -ForegroundColor Yellow
        Write-Host '        Install Node.js 18 or newer from https://nodejs.org and run' -ForegroundColor Yellow
        Write-Host '        this installer again. The engine and its API already work.' -ForegroundColor Yellow
    }
} else {
    Write-Ok "Node $(& node --version 2>&1)"
    Push-Location (Join-Path $Root 'frontend')
    try {
        & npm install
        if ($LASTEXITCODE -ne 0) { Fail 'npm install failed. Scroll up for the reason.' }

        # ------------------------------------------------------ [5/5] SPA build
        Write-Step '[5/5] Building the interface ...'
        & npm run build
        if ($LASTEXITCODE -ne 0) {
            Write-Host '[WARN]  The production build failed. start_app will try to build' -ForegroundColor Yellow
            Write-Host '        the interface again before launching.' -ForegroundColor Yellow
        } else {
            Write-Ok 'Built. The engine can now also serve the interface directly'
            Write-Note 'at http://127.0.0.1:8127/ without a development server.'
        }
    } finally {
        Pop-Location
    }
}

Write-Host ''
Write-Head '==================================================================='
Write-Host '    Installation finished.' -ForegroundColor Green
Write-Host '    Launch the app with start_app.bat  (or .\start_app.ps1)' -ForegroundColor Green
Write-Head '==================================================================='
Write-Host ''
