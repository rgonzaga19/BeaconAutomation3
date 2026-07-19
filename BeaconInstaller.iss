; -------------------------------
; Beabots Installer
; -------------------------------

#define MyAppName "Beabots"
#define MyAppVersion "3.0.0"
#define MyAppPublisher "Romel Gonzaga"
#define MyAppURL "https://github.com/rgonzaga19"
#define MyAppExeName "Beabots.exe"

[Setup]
AppId={{D2A91D2F-0B2F-4B8E-9B79-4B2B5A8D7F01}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}

DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}

DisableProgramGroupPage=yes

OutputDir=Output
OutputBaseFilename=Beabots_Setup_v3.0.0

Compression=lzma
SolidCompression=yes
WizardStyle=modern

SetupIconFile=bot.ico

ArchitecturesInstallIn64BitMode=x64compatible

UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a Desktop shortcut"; GroupDescription: "Additional icons:"

[Files]
; release\win-unpacked is electron-builder's output (npm run dist), which
; already contains resources\server\ — server.exe, ms-playwright, and
; templates — bundled automatically via package.json's extraResources
; config, as long as ms-playwright was pasted into python-build\server\
; before running npm run dist.
Source: "release\win-unpacked\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Beabots"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\Beabots"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Beabots"; Flags: nowait postinstall skipifsilent