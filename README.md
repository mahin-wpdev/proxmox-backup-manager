# Proxmox Backup Manager

A small Windows desktop app for backing up **any Proxmox VE server** to your own PC.

## Features
- Log in to any Proxmox host over SSH (password or SSH key). Servers you have used are remembered (passwords are never saved).
- See every VM and container. Start, shut down, stop or reboot them.
- Back up selected VMs, or everything, **straight to your PC**. The backup is streamed, so it needs no free space on the Proxmox host.
- See and download backup files already on any Proxmox backup storage, with progress, speed and time left. Delete old ones from the host.
- See storage usage.
- **Auto backup** every N days through Windows Task Scheduler:
  - It checks every hour. If the PC was off or offline, the backup runs within an hour of coming back online.
  - The old backup is removed only after the new one fully succeeds. You choose how many old ones to keep.
  - It backs up every server you have logged in to. Each server gets its own folder.
- Host config backup (`/etc/pve`, network, hosts, cron, SSH keys).

## Download
Get `ProxmoxBackupManager.exe` from the [Releases](../../releases) page and run it. No install needed.

## Using it
1. Open the app and enter the Proxmox IP, user (`root`) and password.
2. **VMs / Containers** tab: select VMs and click **Backup selected to PC**, or **Backup ALL to PC**.
3. **Settings & Auto backup** tab:
   - Pick the folder, how many days between backups, and how many old backups to keep.
   - Click **Create & install SSH key on Proxmox** once. Auto backup uses the key instead of a password.
   - Click **Enable auto backup**.

Backups are saved like this:
```
<backup folder>\<proxmox-ip>\<date>\          full / auto backups
<backup folder>\<proxmox-ip>\manual\...       selected-VM backups
<backup folder>\<proxmox-ip>\downloads\       files downloaded from Proxmox
<backup folder>\backup-log.txt                auto backup log
```

## Restoring
Copy the `.vma.zst` file to `/var/lib/vz/dump/` on any Proxmox server, then use **Restore** in the web UI, or run:
```
qmrestore /var/lib/vz/dump/<file>.vma.zst <new-vmid>
```
For containers (`.tar.zst`), run `pct restore <new-vmid> <file>`.

## Run from source
```
pip install -r requirements.txt
python proxmox_backup_manager.py
```
To build the .exe:
```
pyinstaller --onefile --windowed --name ProxmoxBackupManager proxmox_backup_manager.py
```

## Notes
- Settings are stored in `%APPDATA%\ProxmoxBackupManager\config.json`. Passwords are never stored.
- `snapshot` mode backs up running VMs without stopping them.
- If `zstd` is missing on the host, it falls back to gzip.
- The app trusts a host's SSH key on the first connection. Use it on networks you trust.
