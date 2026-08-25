# Geekatplay ComfyUI Asset Vault - release packager
# Geekatplay Studio - Vladimir Chopine
#
# Produces release\GeekatplayAssetVault-v<VERSION>.zip: the app with the
# interface PRE-BUILT, so a user needs Python but never Node.js.  Node stays a
# build-time tool for this script and for anyone working from source - nothing
# is removed from the product, only from the user's prerequisites.

[CmdletBinding()]
param(
    # Skip the npm build and package whatever frontend\dist already holds.
    [switch]$SkipBuild
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

# ------------------------------------------------------------------ version
$config = Get-Content (Join-Path $Root 'backend\app\config.py') -Raw
if ($config -notmatch 'VERSION\s*=\s*"([^"]+)"') {
    throw 'Could not read VERSION from backend\app\config.py'
}
$Version = $Matches[1]
Write-Host "Packaging Geekatplay ComfyUI Asset Vault v$Version" -ForegroundColor Yellow

# ------------------------------------------------------------ fresh UI build
if (-not $SkipBuild) {
    Write-Host '[1/4] Building the interface ...'
    Push-Location (Join-Path $Root 'frontend')
    try {
        npm run build
        if ($LASTEXITCODE -ne 0) { throw 'npm run build failed' }
    } finally { Pop-Location }
} else {
    Write-Host '[1/4] Skipping the build - packaging the existing frontend\dist'
}
if (-not (Test-Path (Join-Path $Root 'frontend\dist\index.html'))) {
    throw 'frontend\dist\index.html is missing - nothing to package'
}

# ---------------------------------------------------------------- staging
Write-Host '[2/4] Staging the release tree ...'
$Stage = Join-Path $Root "release\stage\GeekatplayAssetVault-v$Version"
if (Test-Path (Join-Path $Root 'release\stage')) {
    Remove-Item (Join-Path $Root 'release\stage') -Recurse -Force
}
New-Item -ItemType Directory -Force $Stage | Out-Null

# The engine, minus caches, runtime data and the test tree.
robocopy (Join-Path $Root 'backend\app') (Join-Path $Stage 'backend\app') /E /NFL /NDL /NJH /NJS `
    /XD '__pycache__' | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy failed with $LASTEXITCODE" }
Copy-Item (Join-Path $Root 'backend\requirements.txt') (Join-Path $Stage 'backend\requirements.txt')
New-Item -ItemType Directory -Force (Join-Path $Stage 'backend\data') | Out-Null
New-Item -ItemType File (Join-Path $Stage 'backend\data\.gitkeep') | Out-Null

# The interface, already built.
robocopy (Join-Path $Root 'frontend\dist') (Join-Path $Stage 'frontend\dist') /E /NFL /NDL /NJH /NJS | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy failed with $LASTEXITCODE" }

# Launchers, installers, licence, documentation.
$topLevel = @(
    'start_app.bat', 'start_app.ps1', 'start_app.sh',
    'stop_app.bat', 'stop_app.sh',
    'show_service_status.ps1', 'show_service_status.sh',
    'install_dependencies.bat', 'install_dependencies.ps1', 'install_dependencies.sh',
    'LICENSE', 'README.md'
)
foreach ($name in $topLevel) {
    if (Test-Path (Join-Path $Root $name)) {
        Copy-Item (Join-Path $Root $name) (Join-Path $Stage $name)
    }
}
New-Item -ItemType Directory -Force (Join-Path $Stage 'docs') | Out-Null
Copy-Item (Join-Path $Root 'docs\*.md') (Join-Path $Stage 'docs\')

# ------------------------------------------------------------------ archive
Write-Host '[3/4] Compressing ...'
$Zip = Join-Path $Root "release\GeekatplayAssetVault-v$Version.zip"
if (Test-Path $Zip) { Remove-Item $Zip -Force }
Compress-Archive -Path $Stage -DestinationPath $Zip -CompressionLevel Optimal
Remove-Item (Join-Path $Root 'release\stage') -Recurse -Force

# ------------------------------------------------------------------ summary
$size = [math]::Round((Get-Item $Zip).Length / 1MB, 1)
Write-Host '[4/4] Done.'
Write-Host ''
Write-Host "  $Zip  ($size MB)" -ForegroundColor Green
Write-Host ''
Write-Host '  A user unzips it, runs install_dependencies (Python only - Node is'
Write-Host '  not required because the interface ships pre-built), then start_app.'

# robocopy exits 1 for "files copied"; without this the script would look failed.
exit 0
