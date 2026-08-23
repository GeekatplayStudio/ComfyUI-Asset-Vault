# Geekatplay ComfyUI Asset Vault - launcher (PowerShell)
# Vladimir Chopine
#
# Behaves exactly like start_app.bat: starts the engine on 127.0.0.1:8127, waits
# until it really accepts connections, then runs the interface on port 3000.
#
#   powershell -ExecutionPolicy Bypass -File .\start_app.ps1
#
# Ctrl+C in this window stops the interface; the engine is stopped on the way out.

$ErrorActionPreference = 'Stop'

$Root   = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$Port   = 8127
$UiPort = 3000
$Py     = Join-Path $Root 'venv\Scripts\python.exe'
$LogFile = Join-Path $Root 'backend_log.txt'

# Not every host supports a window title (a redirected or embedded console does not).
try { $Host.UI.RawUI.WindowTitle = 'Geekatplay ComfyUI Asset Vault' } catch {}

function Write-Head($text) { Write-Host $text -ForegroundColor Yellow }

function Fail($text) {
    Write-Host ''
    Write-Host "[ERROR] $text" -ForegroundColor Red
    Write-Host ''
    exit 1
}

function Test-PortListening([int]$p) {
    $conn = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
    return [bool]$conn
}

function Stop-Engine {
    $conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    foreach ($c in $conns) {
        try { Stop-Process -Id $c.OwningProcess -Force -ErrorAction Stop } catch {}
    }
}

Write-Head '==================================================================='
Write-Head '    Geekatplay ComfyUI Asset Vault'
Write-Head '    Vladimir Chopine'
Write-Head '==================================================================='
Write-Host ''

# ------------------------------------------------------------------- checks
if (-not (Test-Path $Py)) {
    Fail @"
Python virtual environment not found at $Root\venv.
        Run install_dependencies.bat first.
"@
}

if (-not (Test-Path (Join-Path $Root 'frontend\node_modules'))) {
    Fail @"
Frontend dependencies not installed at $Root\frontend\node_modules.
        Run install_dependencies.bat first.
"@
}

if (Test-PortListening $Port) {
    Fail @"
Port $Port is already in use.
        Another copy of the Asset Vault may already be running.
        Close it, or run stop_app.bat, then try again.
"@
}

# ------------------------------------------------------------------ backend
Write-Host "[1/3] Starting the vault engine on http://127.0.0.1:$Port ..."
# Launched through cmd so stdout and stderr merge into one backend_log.txt,
# exactly as start_app.bat does it - the troubleshooting guide points at that
# single file.
#
# The whole thing is ONE string. Start-Process re-quotes a multi-element
# -ArgumentList using backslash escaping, which cmd.exe does not understand; a
# single string is passed through verbatim, so cmd sees the quoting it expects.
$argLine = '/c ""' + $Py + '" -m uvicorn app.main:app --host 127.0.0.1 --port ' +
           $Port + ' --app-dir backend > "' + $LogFile + '" 2>&1"'
$engine = Start-Process -FilePath $env:ComSpec -ArgumentList $argLine `
    -WorkingDirectory $Root -WindowStyle Minimized -PassThru

# --------------------------------------------- wait until it really listens
Write-Host '[2/3] Waiting for the engine to accept connections ...'
$ready = $false
foreach ($i in 1..45) {
    if (Test-PortListening $Port) { $ready = $true; break }
    if ($engine.HasExited) { break }
    Start-Sleep -Seconds 1
}

if (-not $ready) {
    Write-Host ''
    Write-Host '[ERROR] The engine did not start within 45 seconds.' -ForegroundColor Red
    Write-Host '        Look at backend_log.txt for the reason. Last lines:' -ForegroundColor Red
    Write-Host '---------------------------------------------------------------'
    if (Test-Path $LogFile) { Get-Content $LogFile -Tail 20 }
    Write-Host '---------------------------------------------------------------'
    Stop-Engine
    exit 1
}
Write-Host '      Engine is up.'

# ----------------------------------------------------------------- frontend
Write-Host "[3/3] Starting the interface on http://localhost:$UiPort ..."
Write-Host ''
Write-Head '==================================================================='
Write-Host '  Asset Vault is running.'
Write-Host "    Interface : http://localhost:$UiPort"
Write-Host "    API docs  : http://127.0.0.1:$Port/docs"
Write-Host ''
Write-Host '  Close this window or run stop_app.bat to shut it down.'
Write-Head '==================================================================='
Write-Host ''

Start-Process "http://localhost:$UiPort"

try {
    Push-Location (Join-Path $Root 'frontend')
    & npm run dev
} finally {
    Pop-Location
    Write-Host ''
    Write-Host 'Interface stopped. Shutting the engine down ...'
    Stop-Engine
}
