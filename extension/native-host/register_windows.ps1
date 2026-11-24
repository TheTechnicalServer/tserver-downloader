# Register the TServer Downloader native messaging host (Chrome/Edge, Windows).
# Run from the folder containing com.tserver.downloader.json, after editing
# that file's "path" to point at the installed native host. HKCU only, no admin.
$ErrorActionPreference = 'Stop'
$manifest = Join-Path $PSScriptRoot 'com.tserver.downloader.json'
if (-not (Test-Path $manifest)) {
    Write-Error "Manifest not found: $manifest"
    exit 1
}
foreach ($browser in @('Google\Chrome', 'Microsoft\Edge')) {
    $key = "HKCU:\Software\$browser\NativeMessagingHosts\com.tserver.downloader"
    if (-not (Test-Path $key)) { New-Item -Path $key -Force | Out-Null }
    Set-ItemProperty -Path $key -Name '(Default)' -Value $manifest
    Write-Host "Registered under $browser"
}
Write-Host "Registered TServer Downloader native messaging host."
Write-Host "Manifest: $manifest"
