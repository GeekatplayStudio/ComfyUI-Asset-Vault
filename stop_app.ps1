# Geekatplay ComfyUI Asset Vault - stop (PowerShell)
# Vladimir Chopine
#
# Stops the vault engine on port 8127 the clean way: it is asked to shut down,
# finishes what it is doing, folds its database log into the database, and exits
# on its own. Only an engine that does not answer, or does not finish within the
# grace period, is ended by force.
#
#   powershell -ExecutionPolicy Bypass -File .\stop_app.ps1 [-Quiet]
#
# Why not just kill it: a forced stop skips the log fold. The log then keeps its
# full size (it reached 1.3 GB after the 2.5 database upgrade) and the next start
# has to fold it first - 15 minutes on a hard disk, during which the vault looks
# as if it will not start.

param(
    [switch]$Quiet,
    [int]$Port = 8127,
    [int]$GraceSeconds = 300
)

$ErrorActionPreference = 'Continue'

function Say($text) { if (-not $Quiet) { Write-Host $text } }

function Get-EngineProcess {
    Get-CimInstance Win32_Process | Where-Object {
        $_.Name -eq 'python.exe' -and $_.CommandLine -like "*app.main:app*--port $Port*"
    }
}

function Test-Alive([int]$id) { [bool](Get-Process -Id $id -ErrorAction SilentlyContinue) }

if (-not $Quiet) {
    Write-Host '==================================================================='
    Write-Host '    Stopping Geekatplay ComfyUI Asset Vault'
    Write-Host '==================================================================='
    Write-Host ''
}

$listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty OwningProcess -Unique | Where-Object { $_ -ne 0 })
$stopped = $false

if ($listeners.Count -gt 0) {
    Say "  Asking the vault engine (pid $($listeners -join ', ')) to shut down cleanly ..."
    $asked = $false
    try {
        $null = Invoke-WebRequest -UseBasicParsing -Method Post -TimeoutSec 10 `
            -Uri "http://127.0.0.1:$Port/api/v1/system/shutdown" `
            -Headers @{ 'X-Vault-Request' = '1' }
        $asked = $true
    } catch {
        Say '  The engine did not answer the shutdown request.'
    }

    if ($asked) {
        $deadline = (Get-Date).AddSeconds($GraceSeconds)
        $next = (Get-Date).AddSeconds(10)
        while ((Get-Date) -lt $deadline -and ($listeners | Where-Object { Test-Alive $_ })) {
            if ((Get-Date) -ge $next) {
                Say '  Still finishing up (saving the database) ...'
                $next = (Get-Date).AddSeconds(10)
            }
            Start-Sleep -Milliseconds 500
        }
    }

    foreach ($id in $listeners) {
        if (Test-Alive $id) {
            Say "  The engine (pid $id) did not exit on its own; stopping it by force."
            try { Stop-Process -Id $id -Force -ErrorAction Stop } catch {}
        }
    }
    $stopped = $true
    Say "  Stopped the vault engine on port $Port."
}

# An engine that is still getting ready has no port to ask on yet. Ending it is
# safe - the database survives a forced stop - it only repeats its start-up
# work next time.
foreach ($p in @(Get-EngineProcess)) {
    if (Test-Alive $p.ProcessId) {
        Say "  Stopping a vault engine that was still getting ready (pid $($p.ProcessId))."
        try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop } catch {}
        $stopped = $true
    }
}

if (-not $stopped) { Say "  Nothing was running on port $Port." }

if (-not $Quiet) {
    Write-Host ''
    Write-Host '  Note: close or refresh the browser tab after stopping the vault.'
    Write-Host ''
}
exit 0
