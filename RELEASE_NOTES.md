## Proxmox Backup Manager 2.0.3

**Download:** `ProxmoxBackupManager-Setup-2.0.3.exe` and run it to install (it updates older versions in place).

### Fixes
- **Updating no longer gets stuck on "Setup was unable to automatically close all applications".** If the app is open, Setup asks once and then closes it itself. Uninstall also closes it.

### From 2.0.2
- Fixed backups failing immediately (`App.ui() got an unexpected keyword argument 'text'`).
- Fixed broken icons and symbols.
- If a backup fails or is cancelled, the job on the Proxmox server is stopped too.
- Shows "Waiting: another backup is running" when the server is busy, and warns before starting a second backup.

Feature requests: https://github.com/mahin-wpdev/proxmox-backup-manager/issues
