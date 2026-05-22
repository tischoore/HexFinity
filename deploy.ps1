# Build a Blender extension zip for HexFinity, and optionally junction the
# source folder into Blender's user_default extensions directory for live
# development.
#
# Usage:
#   .\deploy.ps1            # build dist\hexfinity-<version>.zip
#   .\deploy.ps1 -Dev       # also create/refresh the user_default junction
#   .\deploy.ps1 -BlenderVersion 5.2 -Dev   # target a different Blender version

[CmdletBinding()]
param(
    [switch]$Dev,
    [string]$BlenderVersion = '5.1'
)

$ErrorActionPreference = 'Stop'

$repoRoot = $PSScriptRoot
$srcDir   = Join-Path $repoRoot 'hexfinity'
$distDir  = Join-Path $repoRoot 'dist'
$manifest = Join-Path $srcDir 'blender_manifest.toml'

if (-not (Test-Path $manifest)) {
    throw "Manifest not found: $manifest"
}

$versionLine = Select-String -Path $manifest -Pattern '^\s*version\s*=\s*"([^"]+)"' | Select-Object -First 1
if (-not $versionLine) { throw "Could not parse version from $manifest" }
$version = $versionLine.Matches[0].Groups[1].Value

$zipPath = Join-Path $distDir "hexfinity-$version.zip"
Write-Host "Building $zipPath"

if (-not (Test-Path $distDir)) {
    New-Item -ItemType Directory -Path $distDir | Out-Null
}
if (Test-Path $zipPath) { Remove-Item $zipPath -Force }

# Stage a clean copy without __pycache__ so the zip stays reproducible.
$staging = Join-Path $env:TEMP "hexfinity-build-$([guid]::NewGuid())"
try {
    New-Item -ItemType Directory -Path $staging | Out-Null
    Copy-Item -Path (Join-Path $srcDir '*') -Destination $staging -Recurse
    Get-ChildItem -Path $staging -Recurse -Directory -Filter '__pycache__' |
        Remove-Item -Recurse -Force
    Get-ChildItem -Path $staging -Recurse -File -Filter '*.pyc' |
        Remove-Item -Force

    # Manifest at zip root — matches Blender extension layout and the existing dist zip.
    Compress-Archive -Path (Join-Path $staging '*') -DestinationPath $zipPath -Force
} finally {
    if (Test-Path $staging) { Remove-Item $staging -Recurse -Force }
}

Write-Host "Wrote $zipPath ($([math]::Round((Get-Item $zipPath).Length / 1KB, 1)) KB)"

if ($Dev) {
    $extRoot   = Join-Path $env:APPDATA "Blender Foundation\Blender\$BlenderVersion\extensions\user_default"
    $junction  = Join-Path $extRoot 'hexfinity'

    if (-not (Test-Path $extRoot)) {
        New-Item -ItemType Directory -Path $extRoot -Force | Out-Null
    }

    if (Test-Path $junction) {
        $existing = Get-Item $junction -Force
        $isLink = $existing.Attributes.ToString() -match 'ReparsePoint'
        if ($isLink -and $existing.Target -and ($existing.Target -contains $srcDir)) {
            Write-Host "Junction already points at $srcDir — leaving alone."
        } else {
            Write-Host "Replacing existing path at $junction"
            Remove-Item $junction -Recurse -Force
            New-Item -ItemType Junction -Path $junction -Target $srcDir | Out-Null
            Write-Host "Created junction: $junction -> $srcDir"
        }
    } else {
        New-Item -ItemType Junction -Path $junction -Target $srcDir | Out-Null
        Write-Host "Created junction: $junction -> $srcDir"
    }

    Write-Host ""
    Write-Host "In Blender: Edit -> Preferences -> Get Extensions, refresh, enable 'HexFinity'."
} else {
    Write-Host ""
    Write-Host "Next: in Blender, Edit -> Preferences -> Get Extensions -> Install from Disk... -> select $zipPath"
    Write-Host "Or rerun with -Dev to junction the source folder for live editing."
}
