; Inno Setup script for Proxmox Backup Manager
; Build: iscc /DAppVersion=2.0.0 installer.iss   (expects dist\ProxmoxBackupManager.exe)

#ifndef AppVersion
  #define AppVersion "2.0.0"
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
CloseApplications=yes

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

[UninstallRun]
Filename: "schtasks.exe"; Parameters: "/Delete /F /TN ""Proxmox Auto Backup"""; Flags: runhidden; RunOnceId: "RemoveTask"

