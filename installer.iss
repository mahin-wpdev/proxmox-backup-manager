; Inno Setup script for Proxmox Backup Manager
; Build: iscc /DAppVersion=2.0.0 installer.iss   (expects dist\ProxmoxBackupManager.exe)

#ifndef AppVersion
  #define AppVersion "2.0.3"
#endif

[Setup]
AppId={{C5D077B1-B819-445F-BD6D-FB5F508CA16C}
AppName=Proxmox Backup Manager
AppVersion={#AppVersion}
AppPublisher=mahin-wpdev
AppPublisherURL=https://github.com/mahin-wpdev/proxmox-backup-manager
AppSupportURL=https://github.com/mahin-wpdev/proxmox-backup-manager/issues
DefaultDirName={autopf}\Proxmox Backup Manager
DefaultGroupName=Proxmox Backup Manager
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=dist
OutputBaseFilename=ProxmoxBackupManager-Setup-{#AppVersion}
SetupIconFile=assets\icon.ico
UninstallDisplayIcon={app}\ProxmoxBackupManager.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "dist\ProxmoxBackupManager.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Proxmox Backup Manager"; Filename: "{app}\ProxmoxBackupManager.exe"
Name: "{group}\Uninstall Proxmox Backup Manager"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Proxmox Backup Manager"; Filename: "{app}\ProxmoxBackupManager.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\ProxmoxBackupManager.exe"; Description: "Open Proxmox Backup Manager"; Flags: nowait postinstall skipifsilent

[Code]
function AppIsRunning(): Boolean;
var R: Integer;
begin
  Exec(ExpandConstant('{cmd}'), '/C tasklist /FI "IMAGENAME eq ProxmoxBackupManager.exe" | find /I "ProxmoxBackupManager.exe"',
       '', SW_HIDE, ewWaitUntilTerminated, R);
  Result := (R = 0);
end;

procedure CloseApp();
var R: Integer;
begin
  Exec('taskkill.exe', '/F /IM ProxmoxBackupManager.exe', '', SW_HIDE, ewWaitUntilTerminated, R);
  Sleep(800);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if AppIsRunning() then
  begin
    if WizardSilent() or (MsgBox('Proxmox Backup Manager is open and will be closed to install the update.' + #13#10#13#10 +
        'If a backup is running right now, it will stop. Continue?', mbConfirmation, MB_YESNO) = IDYES) then
      CloseApp()
    else
      Result := 'Please close Proxmox Backup Manager and run Setup again.';
  end;
end;

function InitializeUninstall(): Boolean;
begin
  Result := True;
  if AppIsRunning() then
    CloseApp();
end;

[UninstallRun]
Filename: "schtasks.exe"; Parameters: "/Delete /F /TN ""Proxmox Auto Backup"""; Flags: runhidden; RunOnceId: "RemoveTask"

