# Geekatplay ComfyUI Asset Vault - launcher (PowerShell)
# Vladimir Chopine
#
# Behaves exactly like start_app.bat: applies a staged update, builds the
# interface (or reuses a pre-built one), starts the engine on 127.0.0.1:8127,
# waits until it accepts connections, then opens that same port.
#
#   powershell -ExecutionPolicy Bypass -File .\start_app.ps1
#
# The engine runs independently of this window. Close it freely; run
# stop_app.bat only when you want to stop the vault.

$ErrorActionPreference = 'Stop'

$Root   = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$Port   = 8127
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

function Get-EngineProcess {
    # Matched on the command line, so it also finds an engine this window did not start.
    Get-CimInstance Win32_Process | Where-Object {
        $_.Name -eq 'python.exe' -and $_.CommandLine -like "*app.main:app*--port $Port*"
    }
}

function Stop-Engine {
    # Clean first, so the engine folds its database log; forced only as a fallback.
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root 'stop_app.ps1') -Quiet
}

Write-Head '==================================================================='
Write-Head '    Geekatplay ComfyUI Asset Vault'
Write-Head '    Vladimir Chopine'
Write-Head '==================================================================='
Write-Host ''

# ------------------------------------------------------------------- checks
# First run: nothing is installed yet, so install it now. This makes starting
# the app the only step a new user needs.
if (-not (Test-Path $Py)) {
    Write-Host 'First run detected - installing everything the vault needs.'
    Write-Host 'This takes a few minutes and only happens once.'
    Write-Host ''
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root 'install_dependencies.ps1')
    if ($LASTEXITCODE -ne 0) { Fail 'The automatic install did not finish. Scroll up for the reason.' }
    Write-Host ''
}
if (-not (Test-Path $Py)) {
    Fail @"
Python virtual environment not found at $Root\venv.
        Run install_dependencies.ps1 and read what it says.
"@
}

# Release archives ship a pre-built interface, so Node.js is only needed to
# build one from source.  With node_modules present (a dev checkout) the
# interface is always rebuilt so source edits are never served stale.
$PreBuilt = (-not (Test-Path (Join-Path $Root 'frontend\node_modules'))) -and
            (Test-Path (Join-Path $Root 'frontend\dist\index.html'))

if (-not $PreBuilt -and -not (Test-Path (Join-Path $Root 'frontend\node_modules'))) {
    Fail @"
No built interface at $Root\frontend\dist and no frontend
        dependencies at $Root\frontend\node_modules.
        Run install_dependencies.bat first, or use a release archive
        that ships the interface pre-built.
"@
}

# ----------------------------------------------- apply a staged update
# Nothing is running yet and nothing is imported, which is the only safe
# moment to replace the app's own files.  Exits 0 when nothing is staged.
if (Test-Path (Join-Path $Root 'backend\data\updates\pending.json')) {
    Write-Host '[0/3] Applying the downloaded update ...'
    & $Py (Join-Path $Root 'apply_update.py')
    if ($LASTEXITCODE -ge 2) {
        Fail @"
The update failed and could not be rolled back.
        Your previous files are in backend\data\updates\backup.
"@
    } elseif ($LASTEXITCODE -eq 1) {
        Write-Host '[WARN]  The update failed and was rolled back. Continuing on the' -ForegroundColor Yellow
        Write-Host '        current version.' -ForegroundColor Yellow
    }
}

if (Test-PortListening $Port) {
    Fail @"
Port $Port is already in use.
        Another copy of the Asset Vault may already be running.
        Close it, or run stop_app.bat, then try again.
"@
}

# ---------------------------------------------------------- build interface
# Serve the production build from the engine.  This keeps hashing independent
# of the Vite development server, so closing/reloading the UI cannot stop it.
if ($PreBuilt) {
    Write-Host '[1/3] Interface build already present - reusing frontend\dist'
} else {
    Write-Host '[1/3] Building the interface ...'
    Push-Location (Join-Path $Root 'frontend')
    try {
        & npm run build
        if ($LASTEXITCODE -ne 0) { Fail 'The interface build failed. Fix the errors above and try again.' }
    } finally {
        Pop-Location
    }
}

# ------------------------------------------------------------------ backend
# An engine from an earlier launch may still be getting ready (not listening
# yet, so the port test above passed). Starting a second one on the same
# database would only make both slower: wait for that one instead.
if (Get-EngineProcess) {
    Write-Host '[2/3] A vault engine from an earlier launch is still getting ready.'
    Write-Host '      Waiting for it instead of starting a second one.'
} else {
    Write-Host "[2/3] Starting the vault engine on http://127.0.0.1:$Port ..."
    # Launched through cmd so stdout and stderr merge into one backend_log.txt,
    # exactly as start_app.bat does it - the troubleshooting guide points at that
    # single file.
    #
    # The whole thing is ONE string. Start-Process re-quotes a multi-element
    # -ArgumentList using backslash escaping, which cmd.exe does not understand; a
    # single string is passed through verbatim, so cmd sees the quoting it expects.
    $argLine = '/c ""' + $Py + '" -m uvicorn app.main:app --host 127.0.0.1 --port ' +
               $Port + ' --app-dir backend > "' + $LogFile + '" 2>&1"'
    $null = Start-Process -FilePath $env:ComSpec -ArgumentList $argLine `
        -WorkingDirectory $Root -WindowStyle Minimized -PassThru
}

# --------------------------------------------- wait until it really listens
# No fixed deadline while the engine is alive: after an upgrade, or after it was
# not stopped cleanly, it first folds its database log, which on a hard disk
# takes minutes. Giving up at 45 seconds reported a working engine as failed
# and left it running with nobody waiting for it.
Write-Host '[3/3] Waiting for the engine to accept connections ...'
$ready = $false
$maxWait = 3600
for ($waited = 1; $waited -le $maxWait; $waited++) {
    if (Test-PortListening $Port) { $ready = $true; break }
    if ($waited % 15 -eq 0) {
        if (-not (Get-EngineProcess)) { break }
        if ($waited -eq 15) {
            Write-Host '      The engine is still getting ready. After an upgrade, or when it was'
            Write-Host '      not stopped cleanly, this can take several minutes on a hard disk.'
            Write-Host '      Leave this window open - the browser opens when it is ready.'
        }
        $last = if (Test-Path $LogFile) { Get-Content -LiteralPath $LogFile -Tail 1 } else { '' }
        Write-Host "      still starting ($waited s) $last"
    }
    Start-Sleep -Seconds 1
}

if (-not $ready) {
    Write-Host ''
    if (Get-EngineProcess) {
        Write-Host '[WARN]  The engine is still working after an hour and has not opened its port.' -ForegroundColor Yellow
        Write-Host "        It has been left running. Open http://127.0.0.1:$Port/ later, or run" -ForegroundColor Yellow
        Write-Host '        stop_app.bat to stop it. Last lines of backend_log.txt:' -ForegroundColor Yellow
    } else {
        Write-Host '[ERROR] The engine stopped before it was ready.' -ForegroundColor Red
        Write-Host '        Look at backend_log.txt for the reason. Last lines:' -ForegroundColor Red
    }
    Write-Host '---------------------------------------------------------------'
    if (Test-Path $LogFile) { Get-Content $LogFile -Tail 20 }
    Write-Host '---------------------------------------------------------------'
    exit 1
}
Write-Host '      Engine is up.'

# ------------------------------------------------------- live verification
try {
    & (Join-Path $Root 'show_service_status.ps1') -Port $Port
    if (-not $?) { throw 'The engine failed a live service check.' }
} catch {
    Stop-Engine
    Fail "The engine opened its port but failed a live service check. See backend_log.txt for details. $($_.Exception.Message)"
}

# ----------------------------------------------------------------- interface
Write-Host ''
Write-Head '==================================================================='
Write-Host '  Asset Vault is running independently of this launcher window.'
Write-Host "    Interface : http://127.0.0.1:$Port/"
Write-Host "    API docs  : http://127.0.0.1:$Port/docs"
Write-Host ''
Write-Host '  Close this window freely. Run stop_app.bat only when you want to stop the vault.'
Write-Head '==================================================================='
Write-Host ''

Start-Process "http://127.0.0.1:$Port/"
