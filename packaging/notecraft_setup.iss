; NoteCraft's installer - adapted from QBG One's qbg_setup.iss (QBG_Studio_Vibecraft/backend/studio).
; Compiled by packaging/build_installer.py with /DPackageDir= /DOutputDir= /DAppVersion= /DAppName=.
;
; - Per user, no admin: %LOCALAPPDATA%\Programs\NoteCraft. The same AppId forever, so every
;   update (the Update button runs this silently) finds the same installation.
; - Notes, runs, settings and the sign-in live in %USERPROFILE%\.note_studio, never here:
;   nothing in this folder is anyone's work, and an update replaces it all.
; - No [InstallDelete] / [UninstallDelete]: an update never deletes anything (QBG's rule).

[Setup]
AppId={{E74CCEA5-9A3A-4D46-8399-962681DC33DA}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=PW Notes Team
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
UsePreviousGroup=no
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename={#AppName}_Setup
Compression=lzma2/fast
SolidCompression=yes
WizardStyle=modern
SetupIconFile={#PackageDir}\_internal\app\note_studio\static\icons\notecraft.ico
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\{#AppName}.exe
CloseApplications=yes
RestartApplications=no
InfoBeforeFile={#PackageDir}\QUICK_START.txt

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "{#PackageDir}\{#AppName}.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#PackageDir}\QUICK_START.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#PackageDir}\_internal\python\*"; DestDir: "{app}\_internal\python"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "__pycache__\*"
Source: "{#PackageDir}\_internal\libs\*"; DestDir: "{app}\_internal\libs"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "__pycache__\*"
Source: "{#PackageDir}\_internal\app\*"; DestDir: "{app}\_internal\app"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "__pycache__\*"

[Icons]
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppName}.exe"; Tasks: desktopicon
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppName}.exe"
Name: "{group}\Quick start"; Filename: "{app}\QUICK_START.txt"

[Run]
Filename: "{app}\{#AppName}.exe"; Description: "Open {#AppName}"; Flags: nowait postinstall skipifsilent
