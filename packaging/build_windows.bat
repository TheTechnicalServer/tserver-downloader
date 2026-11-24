@echo off
REM Build TServer Downloader on Windows (PyInstaller + optional Inno Setup).
REM Run from the repository root:  packaging\build_windows.bat
setlocal
cd /d "%~dp0.."

where python >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python was not found on PATH. Install Python 3.12 from https://www.python.org/downloads/ and re-run this script.
    exit /b 1
)
python --version

if not exist ".venv" (
    echo Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo ERROR: Failed to create the virtual environment.
        exit /b 1
    )
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
if errorlevel 1 (
    echo ERROR: pip install -r requirements.txt failed.
    exit /b 1
)
pip install pyinstaller
if errorlevel 1 (
    echo ERROR: pip install pyinstaller failed.
    exit /b 1
)

echo Building with PyInstaller...
pyinstaller packaging\tserver-downloader.spec --noconfirm
if errorlevel 1 (
    echo ERROR: PyInstaller build failed.
    exit /b 1
)
echo PyInstaller output: dist\TServerDownloader\TServerDownloader.exe

set "ISCC="
where ISCC.exe >nul 2>&1
if not errorlevel 1 set "ISCC=ISCC.exe"
if "%ISCC%"=="" if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"

if "%ISCC%"=="" (
    echo WARNING: Inno Setup ^(ISCC.exe^) was not found, so no installer was built.
    echo Install Inno Setup 6 from https://jrsoftware.org/isdl.php and re-run this script to build the installer.
) else (
    echo Building installer with Inno Setup...
    "%ISCC%" installer\tserver-downloader.iss
    if errorlevel 1 (
        echo ERROR: Inno Setup build failed.
        exit /b 1
    )
    echo Installer output: installer\Output\TServerDownloaderSetup-1.0.0.exe
)
echo Build finished.
endlocal
