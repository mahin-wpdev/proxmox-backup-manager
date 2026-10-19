## Proxmox Backup Manager 2.0.1

**Download:** `ProxmoxBackupManager-Setup-2.0.1.exe` and run it to install (it updates 2.0.0 in place).

### Fixes
- Progress now shows right away. If another backup is already running on the server, the app says **"Waiting: another backup is running"** instead of showing nothing.
- The app warns you before starting a backup when one is already running on the server.
- **Cancel now also stops the backup on the Proxmox server**, not just in the app.
- Each backup run uses its own log file, so two backups can no longer mix up each other's results.

Feature requests: https://github.com/mahin-wpdev/proxmox-backup-manager/issues
