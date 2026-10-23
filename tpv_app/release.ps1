# Publishes a new version: every installed copy of TPV-TPC.exe will offer it.
#
#   1. Bump __version__ in version.py and commit your changes.
#   2. powershell -ExecutionPolicy Bypass -File release.ps1 -Notes "What changed"
#
# Builds the exe, pushes, and creates GitHub release v<version> with the exe attached.
param(
    [Parameter(Mandatory = $true)][string]$Notes
)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$version = python -c "from version import __version__; print(__version__)"
$tag = "v$version"

if (git status --porcelain) { throw "Uncommitted changes - commit them first." }
gh release view $tag 2>$null | Out-Null
if ($LASTEXITCODE -eq 0) { throw "Release $tag already exists - bump __version__ in version.py." }

& "$PSScriptRoot\build.ps1"

git push origin HEAD
if ($LASTEXITCODE -ne 0) { throw "git push failed" }

gh release create $tag "dist\TPV-TPC.exe" --title "TPV-TPC $tag" --notes $Notes
if ($LASTEXITCODE -ne 0) { throw "gh release create failed" }

Write-Host "`nPublished $tag"
