## Proxmox Backup Manager 2.0.2

**Download:** `ProxmoxBackupManager-Setup-2.0.2.exe` and run it to install (it updates older versions in place).

### Fixes
- **Backups failed immediately** with `App.ui() got an unexpected keyword argument 'text'`. Fixed, and a progress-display problem can no longer stop a backup.
- **Broken icons and symbols** (text like `â€¢` and `ðŸ“Š`) in 2.0.1. Fixed.
- If a backup fails or is cancelled, the job on the Proxmox server is stopped too, so nothing is left running.

### From 2.0.1
- Progress shows right away. If another backup is already running on the server, the app says **"Waiting: another backup is running"**.
- The app warns you before starting a backup when one is already running on the server.
- Each backup run uses its own log file.

Feature requests: https://github.com/mahin-wpdev/proxmox-backup-manager/issues
