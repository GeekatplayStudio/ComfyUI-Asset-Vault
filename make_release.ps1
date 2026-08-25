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
    [switch]$SkipBuild,

    # Create the GitHub release too, with the changelog section as its notes
    # and the archive attached. Requires the `gh` CLI, authenticated.
    [switch]$Publish
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

# ------------------------------------------------------------------ version
# Windows PowerShell 5.1 reads as ANSI and writes UTF-8 *with* a BOM by
# default. Both are wrong here: the changelog is UTF-8, and a BOM would ride
# into the GitHub release body (which the in-app updater renders) and into
# SHA256SUMS.txt (which `sha256sum -c` would then reject).
$Utf8NoBom = New-Object System.Text.UTF8Encoding $false
function Read-Utf8([string]$path) { [System.IO.File]::ReadAllText($path, [System.Text.Encoding]::UTF8) }
function Write-Utf8([string]$path, [string]$text) { [System.IO.File]::WriteAllText($path, $text, $Utf8NoBom) }

$config = Read-Utf8 (Join-Path $Root 'backend\app\config.py')
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

# The in-app updater renders the release body as "what changed", so the notes
# are the changelog's own top section rather than a hand-written summary that
# could drift from it.
$changelog = Read-Utf8 (Join-Path $Root 'docs\CHANGELOG.md')
$sections = [regex]::Matches($changelog, '(?ms)^##\s+(?<title>.+?)\s*$(?<body>.*?)(?=^##\s+|\z)')
$notes = $null
foreach ($s in $sections) {
    $title = $s.Groups['title'].Value.Trim()
    if ($title -eq 'Unreleased' -or $title -like "$Version*") {
        $notes = $s.Groups['body'].Value.Trim()
        break
    }
}
if (-not $notes) { $notes = "See docs/CHANGELOG.md for what changed in $Version." }
$notesPath = Join-Path $Root 'release\release-notes.md'
New-Item -ItemType Directory -Force (Join-Path $Root 'release') | Out-Null
Write-Utf8 $notesPath $notes

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
    'apply_update.py',
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

# ---------------------------------------------------------------- checksum
# The in-app updater compares what it downloaded against the digest GitHub
# reports for the asset. This file is for anyone verifying by hand.
$hash = (Get-FileHash $Zip -Algorithm SHA256).Hash.ToLower()
$sumsPath = Join-Path $Root 'release\SHA256SUMS.txt'
Write-Utf8 $sumsPath "$hash  $(Split-Path -Leaf $Zip)`n"

# ------------------------------------------------------------------ summary
$size = [math]::Round((Get-Item $Zip).Length / 1MB, 1)
Write-Host '[4/4] Done.'
Write-Host ''
Write-Host "  $Zip  ($size MB)" -ForegroundColor Green
Write-Host "  sha256: $hash"
Write-Host "  notes:  $notesPath"
Write-Host ''
Write-Host '  A user unzips it, runs install_dependencies (Python only - Node is'
Write-Host '  not required because the interface ships pre-built), then start_app.'

# ------------------------------------------------------------------ publish
if ($Publish) {
    if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
        throw 'The GitHub CLI (gh) is not on PATH, so -Publish cannot create the release.'
    }
    Write-Host ''
    Write-Host "Publishing v$Version to GitHub ..." -ForegroundColor Yellow
    # The tag is what the in-app updater compares against, so it must be the
    # version and nothing else.
    gh release create "v$Version" $Zip $sumsPath `
        --title "v$Version" --notes-file $notesPath
    if ($LASTEXITCODE -ne 0) { throw "gh release create failed with $LASTEXITCODE" }
    Write-Host "Published. Installs on v$Version or older will now offer the update." -ForegroundColor Green
} else {
    Write-Host ''
    Write-Host '  Not published. Re-run with -Publish to create the GitHub release'
    Write-Host '  (tag v' -NoNewline; Write-Host "$Version" -NoNewline; Write-Host ', notes from the changelog, archive attached).'
}

# robocopy exits 1 for "files copied"; without this the script would look failed.
exit 0
