## Proxmox Backup Manager 2.0.0

**Download:** `ProxmoxBackupManager-Setup-2.0.0.exe` and run it to install.

### New
- New modern UI with a sidebar, a dark/light theme and a dashboard (CPU, RAM, disk, uptime, last backup).
- Windows installer with Start menu and desktop shortcuts, and an uninstaller.
- Remember login details. The password is encrypted with Windows DPAPI. Optional auto-login.
- Live backup progress: % done, GB of total GB, speed, time left, and an overall bar.
- Snapshots: take, roll back, delete.
- Open the VM console and the web UI in your browser, and open an SSH terminal.
- Restore a backup on the server, or upload a local backup and restore it.
- Check for updates, and reboot or shut down the node.
- Keyboard shortcuts (press F1 in the app).
- Auto backup now covers every saved server, and can use a saved password or an SSH key.
- Works with Proxmox clusters: backs up guests on other nodes too.

Feature requests: https://github.com/mahin-wpdev/proxmox-backup-manager/issues
