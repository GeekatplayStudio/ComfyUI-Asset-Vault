<#
Geekatplay ComfyUI Asset Vault - Geekatplay Studio - Vladimir Chopine

Live service verification shared by both launchers.

This deliberately queries the running API after startup instead of trusting a
spawned process or an open port.  A listener can exist before the application
is usable; these calls prove that the engine, UI, and background services are
actually responding.
#>

[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8127
)

$ErrorActionPreference = 'Stop'
$Base = "http://127.0.0.1:$Port"

function Write-Check([string]$state, [string]$name, [string]$message) {
    $colour = switch ($state) {
        'OK' { 'Green' }
        'WARN' { 'Yellow' }
        default { 'Red' }
    }
    Write-Host ("  [{0}] {1,-12} {2}" -f $state, $name, $message) -ForegroundColor $colour
}

try {
    $ping = Invoke-RestMethod -Uri "$Base/api/v1/ping" -TimeoutSec 5
    if (-not $ping.pong) { throw 'The ping endpoint did not return pong=true.' }
    $page = Invoke-WebRequest -Uri "$Base/" -UseBasicParsing -TimeoutSec 5
    if ($page.StatusCode -ne 200) { throw "The interface returned HTTP $($page.StatusCode)." }
    $hash = Invoke-RestMethod -Uri "$Base/api/v1/hash/status" -TimeoutSec 5
    $index = Invoke-RestMethod -Uri "$Base/api/v1/index/status" -TimeoutSec 5
    $embed = Invoke-RestMethod -Uri "$Base/api/v1/embeddings/status" -TimeoutSec 5
    $health = Invoke-RestMethod -Uri "$Base/api/v1/system/health" -TimeoutSec 5
} catch {
    Write-Check 'FAIL' 'Vault' $_.Exception.Message
    throw 'Live service verification failed.'
}

$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
    Select-Object -First 1
$pidText = if ($listener) { "PID $($listener.OwningProcess), port $Port" } else { "port $Port" }

Write-Host ''
Write-Host '  Live service report' -ForegroundColor Cyan
Write-Check 'OK' 'Vault API' $pidText
Write-Check 'OK' 'Interface' "$Base/ (HTTP $($page.StatusCode))"
Write-Check 'OK' 'Hash queue' ("configured {0}; running {1}; queued {2}" -f `
    $hash.concurrency, $hash.queue.running, $hash.queue.queued)
Write-Check 'OK' 'Indexer' ($(if ($index.active) { 'scan active' } else { 'idle' }))
Write-Check ($(if ($embed.state -eq 'ready') { 'OK' } else { 'WARN' })) 'Embeddings' `
    ("{0}; pending {1}" -f $embed.state, $embed.index.pending)

foreach ($check in $health.checks) {
    # These are asset findings, not failed runtime services.  Keep them
    # visible at startup without implying that the vault failed to start.
    $isAssetFinding = $check.id -in @('integrity', 'partial_downloads', 'suspect_remotes')
    $state = if ($isAssetFinding -and $check.status -ne 'ok') {
        'WARN'
    } else {
        switch ($check.status) {
            'ok' { 'OK' }
            'warn' { 'WARN' }
            default { 'FAIL' }
        }
    }
    $detail = $check.message
    if ([string]::IsNullOrWhiteSpace($detail) -and $check.count -gt 0) {
        $examples = @($check.items | Select-Object -First 3 | ForEach-Object {
            if ($_.name) { $_.name }
            elseif ($_.path) { $_.path }
            elseif ($_.package) { $_.package }
            else { 'item' }
        })
        $detail = "$($check.count) item(s): $($examples -join ', ')"
    }
    if ([string]::IsNullOrWhiteSpace($detail)) { $detail = 'ready' }
    Write-Check $state ("Health/{0}" -f $check.id) $detail
}

$requiredFailure = @($health.checks | Where-Object {
    $_.status -eq 'error' -and $_.id -in @('comfyui_root', 'database')
})
if ($requiredFailure.Count -gt 0) {
    Write-Check 'FAIL' 'Runtime health' 'A required runtime service needs attention.'
} elseif ($health.status -ne 'ok') {
    Write-Check 'WARN' 'Asset attention' 'Runtime services are healthy; review the warnings above when convenient.'
} else {
    Write-Check 'OK' 'Runtime health' 'All required services are healthy.'
}
