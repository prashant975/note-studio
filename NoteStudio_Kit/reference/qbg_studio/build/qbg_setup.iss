[Setup]
AppId={{C489EA28-BD98-49D4-B6A3-F11846E07DAD}
AppName=QBG One
AppVersion={#AppVersion}
AppPublisher=QBG Team
DefaultDirName={localappdata}\Programs\QBGStudio
DefaultGroupName=QBG One
UsePreviousGroup=no
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename=QBG_Studio_Setup
Compression=lzma2/fast
SolidCompression=yes
WizardStyle=modern
SetupIconFile={#PackageDir}\_internal\app\qbg_studio\static\icons\qbg-studio.ico
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\QBG_Studio.exe
CloseApplications=yes
RestartApplications=no
InfoBeforeFile={#PackageDir}\QUICK_START.txt

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "{#PackageDir}\QBG_Studio.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#PackageDir}\QUICK_START.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#PackageDir}\_internal\python\*"; DestDir: "{app}\_internal\python"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "__pycache__\*"
Source: "{#PackageDir}\_internal\app\*"; DestDir: "{app}\_internal\app"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "qbg_studio\data\*,qbg_studio\uploads\*,1_Existing_Bots_Windows_Ready\credentials.json,2_Question_Creation_Pipeline\config.py,2_Question_Creation_Pipeline\config.pyc,__pycache__\*"
Source: "{#PackageDir}\_internal\app\qbg_studio\data\*"; DestDir: "{app}\_internal\app\qbg_studio\data"; Flags: onlyifdoesntexist uninsneveruninstall recursesubdirs createallsubdirs
Source: "{#PackageDir}\_internal\app\qbg_studio\uploads\*"; DestDir: "{app}\_internal\app\qbg_studio\uploads"; Flags: onlyifdoesntexist uninsneveruninstall recursesubdirs createallsubdirs
Source: "{#PackageDir}\_internal\app\1_Existing_Bots_Windows_Ready\credentials.json"; DestDir: "{app}\_internal\app\1_Existing_Bots_Windows_Ready"; Flags: onlyifdoesntexist uninsneveruninstall
Source: "{#PackageDir}\_internal\app\2_Question_Creation_Pipeline\config.py*"; DestDir: "{app}\_internal\app\2_Question_Creation_Pipeline"; Flags: onlyifdoesntexist uninsneveruninstall

[Icons]
Name: "{autodesktop}\QBG One"; Filename: "{app}\QBG_Studio.exe"; IconFilename: "{app}\_internal\app\qbg_studio\static\icons\qbg-studio.ico"; Tasks: desktopicon
Name: "{group}\QBG One"; Filename: "{app}\QBG_Studio.exe"; IconFilename: "{app}\_internal\app\qbg_studio\static\icons\qbg-studio.ico"
Name: "{group}\Quick start"; Filename: "{app}\QUICK_START.txt"

[Run]
Filename: "{app}\_internal\python\python.exe"; Parameters: """{app}\_internal\app\setup_tools.py"""; Description: "Install missing Chrome / Pandoc (internet required)"; Flags: postinstall skipifsilent; Check: NeedTools
Filename: "{app}\QBG_Studio.exe"; Description: "Launch QBG One"; Flags: nowait postinstall skipifsilent

[Code]
{ Renamed from "QBG Studio" on 4 October 2026. An earlier install's shortcuts are
  renamed, never deleted, so nobody is left with two icons for one app. }
procedure RenameOld(const Old, New: String);
begin
  if (FileExists(Old) or DirExists(Old)) and not (FileExists(New) or DirExists(New)) then
    RenameFile(Old, New);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
  begin
    RenameOld(ExpandConstant('{autodesktop}\QBG Studio.lnk'), ExpandConstant('{autodesktop}\QBG One.lnk'));
    RenameOld(ExpandConstant('{userprograms}\QBG Studio'), ExpandConstant('{group}'));
    RenameOld(ExpandConstant('{group}\QBG Studio.lnk'), ExpandConstant('{group}\QBG One.lnk'));
  end;
end;

function NeedTools: Boolean;
begin
  Result := not ((FileExists(ExpandConstant('{pf}\Google\Chrome\Application\chrome.exe')) or
                  FileExists(ExpandConstant('{localappdata}\Google\Chrome\Application\chrome.exe'))) and
                 (FileExists(ExpandConstant('{pf}\Pandoc\pandoc.exe')) or
                  FileExists(ExpandConstant('{localappdata}\Pandoc\pandoc.exe'))));
end;
