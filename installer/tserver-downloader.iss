; Inno Setup script for TServer Downloader.
; Build with:  ISCC installer\tserver-downloader.iss
; (or run packaging\build_windows.ps1 / .bat, which calls this automatically)
;
; Notes:
; - File associations (.tserver) are intentionally NOT registered: there is
;   no .tserver file format in this project, and registering a meaningless
;   association would be misleading. See README.md.
; - FFmpeg: if third_party\ffmpeg.exe is bundled by the PyInstaller build,
;   its licence (LGPL/GPL, see THIRD_PARTY_LICENSES.md) travels with the
;   app in THIRD_PARTY_LICENSES.md, installed next to the executable.

#define MyAppName "TServer Downloader"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "TServer"
#define MyAppExeName "TServerDownloader.exe"

[Setup]
AppId={{8A3F2C1D-4E5B-4A7C-9D2E-1F0A6B8C3D4E}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=TServerDownloaderSetup-{#MyAppVersion}
Compression=lzma
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}
; SetupIconFile intentionally omitted: packaging/tserver-downloader.ico does not exist yet, and Inno Setup fails the build if the icon file is missing.
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\TServerDownloader\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; Native messaging host manifest (installed by the app's PyInstaller build
; when present; harmless if absent -- the file is simply skipped).
Source: "..\dist\TServerDownloader\native-messaging-host.json"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "..\THIRD_PARTY_LICENSES.md"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
; Native messaging host registration for the browser extension.
; Chrome / Edge read HKCU\...\NativeMessagingHosts\com.tserver.downloader
; and expect the default value to be the full path of the manifest JSON.
Root: HKCU; Subkey: "Software\Google\Chrome\NativeMessagingHosts\com.tserver.downloader"; ValueType: string; ValueName: ""; ValueData: "{app}\native-messaging-host.json"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Microsoft\Edge\NativeMessagingHosts\com.tserver.downloader"; ValueType: string; ValueName: ""; ValueData: "{app}\native-messaging-host.json"; Flags: uninsdeletekey

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\logs"
