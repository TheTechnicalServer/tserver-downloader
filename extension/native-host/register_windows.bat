@echo off
REM Register the TServer Downloader native messaging host (Chrome, Windows).
REM Run this file from the folder that contains com.tserver.downloader.json,
REM after editing that file's "path" to point at the installed native host
REM executable / python script. Requires no admin rights (HKCU only).
setlocal
set "MANIFEST=%~dp0com.tserver.downloader.json"
if not exist "%MANIFEST%" (
  echo ERROR: manifest not found: %MANIFEST%
  exit /b 1
)
reg add "HKCU\Software\Google\Chrome\NativeMessagingHosts\com.tserver.downloader" /ve /t REG_SZ /d "%MANIFEST%" /f
if errorlevel 1 (
  echo ERROR: failed to write the registry key.
  exit /b 1
)
reg add "HKCU\Software\Microsoft\Edge\NativeMessagingHosts\com.tserver.downloader" /ve /t REG_SZ /d "%MANIFEST%" /f
echo Registered TServer Downloader native messaging host.
echo Manifest: %MANIFEST%
endlocal
