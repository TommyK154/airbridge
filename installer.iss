; AirBridge Windows installer (Inno Setup 6).
; Build after PyInstaller: ISCC.exe installer.iss /DAppVersion=1.0.0
; Installs per-user (no admin prompt) and offers a start-at-login task that
; writes the same HKCU Run value the in-app tray toggle manages.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{7C1F4E7A-4B7D-4D0B-9A6C-1D2E7F3A9B10}
AppName=AirBridge
AppVersion={#AppVersion}
AppPublisher=Tommy Kargul
AppPublisherURL=https://github.com/TommyK154/airbridge
DefaultDirName={autopf}\AirBridge
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=AirBridge-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\AirBridge.exe
CloseApplications=yes

[Tasks]
Name: "startlogin"; Description: "Start AirBridge automatically when you sign in to Windows"

[Files]
Source: "dist\AirBridge\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{autoprograms}\AirBridge"; Filename: "{app}\AirBridge.exe"

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "AirBridge"; ValueData: """{app}\AirBridge.exe"""; Flags: uninsdeletevalue; Tasks: startlogin

[Run]
Filename: "{app}\AirBridge.exe"; Description: "Launch AirBridge now"; Flags: nowait postinstall skipifsilent
