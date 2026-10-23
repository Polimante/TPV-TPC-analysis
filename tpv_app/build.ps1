# Builds dist\TPV-TPC.exe (single file, no console window).
# Usage:  powershell -ExecutionPolicy Bypass -File build.ps1
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

python -m pip install --quiet --upgrade pyinstaller
python -m pip install --quiet -r requirements.txt

python -m PyInstaller main.py `
    --name TPV-TPC `
    --onefile `
    --windowed `
    --noconfirm `
    --clean `
    --add-data "styles.qss;." `
    --add-data "DOCUMENTACAO.md;." `
    --collect-submodules pyvisa `
    --collect-submodules pyvisa_py `
    --copy-metadata pyvisa `
    --exclude-module matplotlib `
    --exclude-module tkinter
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

Write-Host "`nBuilt: $PSScriptRoot\dist\TPV-TPC.exe"
