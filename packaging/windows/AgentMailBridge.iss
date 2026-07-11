#ifndef MyAppVersion
  #define MyAppVersion "0.9.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\..\dist\AgentMailBridge"
#endif
#ifndef OutputDir
  #define OutputDir "..\..\release"
#endif

#define MyAppName "AgentMailBridge"
#define MyAppPublisher "AgentMailBridge Open Source Project"
#define MyAppExeName "AgentMailBridge.exe"
#define MyAppId "{{2A4C036C-C691-42B3-A6C1-C0D7085E99F2}"

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
VersionInfoVersion={#MyAppVersion}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=AgentMailBridge-{#MyAppVersion}-Setup
SetupIconFile=..\..\agent_mail_bridge\resources\branding\agentmailbridge.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
CloseApplicationsFilter=AgentMailBridge.exe
RestartApplications=no
ChangesEnvironment=no
LicenseFile=..\..\LICENSE

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\AgentMailBridge"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{autodesktop}\AgentMailBridge"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动 AgentMailBridge"; Flags: nowait postinstall skipifsilent

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'AgentMailBridge');
end;
