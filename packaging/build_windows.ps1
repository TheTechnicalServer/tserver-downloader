<#
.SYNOPSIS
    Build TServer Downloader on Windows (PyInstaller + optional Inno Setup).
.DESCRIPTION
    Creates a virtual environment, installs dependencies, builds the app
    with PyInstaller (packaging/tserver-downloader.spec), then builds the
    installer with Inno Setup (ISCC) when it is installed.
    Run from the repository root in PowerShell:
        powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
#>
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Fail($Message) {
    Write-Host "ERROR: $Message" -ForegroundColor Red
    exit 1
}

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Fail "Python was not found on PATH. Install Python 3.12 from https://www.python.org/downloads/ and re-run this script."
}
$PythonVersion = (python --version) 2>&1
Write-Host "Using $PythonVersion"

if (-not (Test-Path ".\.venv")) {
    Write-Host "Creating virtual environment..."
    python -m venv .venv
}
& .\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { Fail "pip install -r requirements.txt failed." }
pip install pyinstaller
if ($LASTEXITCODE -ne 0) { Fail "pip install pyinstaller failed." }

Write-Host "Building with PyInstaller..."
pyinstaller packaging\tserver-downloader.spec --noconfirm
if ($LASTEXITCODE -ne 0) { Fail "PyInstaller build failed." }
Write-Host "PyInstaller output: dist\TServerDownloader\TServerDownloader.exe"

$Iscc = Get-Command ISCC.exe -ErrorAction SilentlyContinue
if (-not $Iscc) {
    $Candidate = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
    if (Test-Path $Candidate) { $Iscc = Get-Command $Candidate }
}
if ($Iscc) {
    Write-Host "Building installer with Inno Setup..."
    & $Iscc.Source installer\tserver-downloader.iss
    if ($LASTEXITCODE -ne 0) { Fail "Inno Setup build failed." }
    Write-Host "Installer output: installer\Output\TServerDownloaderSetup-1.0.0.exe"
} else {
    Write-Host "WARNING: Inno Setup (ISCC.exe) was not found, so no installer was built." -ForegroundColor Yellow
    Write-Host "Install Inno Setup 6 from https://jrsoftware.org/isdl.php and re-run this script to build the installer."
}
Write-Host "Build finished."
