# Proxmox Backup Manager

A free, easy Windows app for backing up **any Proxmox VE server** to your own PC, and for doing everyday Proxmox jobs without the command line.

![icon](assets/icon.png)

## Download
Go to the [Releases](../../releases/latest) page, download **`ProxmoxBackupManager-Setup-x.y.z.exe`** and run it. The installer adds Start menu and desktop shortcuts, and an uninstaller.

## Features
**Login**
- Connect to any Proxmox host over SSH with a password or an SSH key.
- **Remember login details.** The password is encrypted with Windows DPAPI, so only your Windows account can read it.
- Optional auto-login, and a list of saved servers.

**Dashboard**
- CPU, RAM, root disk, running VMs, uptime, and when the last backup to this PC ran.
- Quick actions: Backup ALL, open web UI, SSH terminal, check for updates, reboot or shut down the node.

**VMs & Containers**
- See CPU, RAM, disk size, uptime and node for every VM and container.
- Start, shut down, stop, reboot.
- **Snapshots:** take, roll back, delete.
- **Console:** opens the noVNC console in your browser (double-click a VM).
- Right-click menu with all actions.

**Backup to this PC**
- Back up selected VMs, or everything including the host config, **straight to your PC**. It needs no free space on the Proxmox host.
- Live progress: **% done, GB read of total GB, GB saved, speed, time left**, plus an overall bar for multi-VM jobs.

**Backups on server**
- See backup files on every Proxmox backup storage.
- Download them with % / speed / time left. Restore them on the server, or delete them.

**Backups on this PC**
- See local backups and the free disk space.
- **Upload & restore** a local backup to Proxmox as a new VM ID.

**Auto backup**
- Runs every N days through Windows Task Scheduler, for every saved server.
- If the PC was off or offline, the backup runs within an hour of coming back online.
- The old backup is removed only after the new one fully succeeds. You choose how many to keep.

**Other**
- Dark and light theme.
- Keyboard shortcuts.

## Keyboard shortcuts
| Key | Action |
|---|---|
| F5 | Refresh |
| Ctrl+B | Backup selected VMs to PC |
| Ctrl+Shift+B | Backup ALL |
| Ctrl+D | Download selected backup file |
| Ctrl+S | Snapshots of selected VM |
| Ctrl+O | Open VM console |
| Ctrl+W | Open Proxmox web UI |
| Ctrl+T | Open SSH terminal |
| Ctrl+1 … Ctrl+6 | Switch page |
| Esc | Cancel running job |
| Ctrl+L | Log out |
| F1 | Show shortcuts |

## Where backups go
```
<backup folder>\<proxmox-ip>\<date>\          full / auto backups
<backup folder>\<proxmox-ip>\manual\...       selected-VM backups
<backup folder>\<proxmox-ip>\downloads\       files downloaded from Proxmox
<backup folder>\backup-log.txt                auto backup log
```

## Feature requests
Need something? [Open an issue](../../issues/new) and describe it.

## Run from source
```
pip install -r requirements.txt
python proxmox_backup_manager.py
```
Each `v*` tag is built by GitHub Actions (`.github/workflows/release.yml`), which produces the Inno Setup installer.

## Notes
- Settings are stored in `%APPDATA%\ProxmoxBackupManager\config.json`.
- Disks marked **"No backup"** in Proxmox are skipped by vzdump, so only the config of those VMs is saved.
- If `zstd` is missing on the host, the app falls back to gzip.
- The app trusts a host's SSH key on the first connection. Use it on networks you trust.
