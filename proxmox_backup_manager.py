"""
Proxmox Backup Manager
A small Windows desktop app to log in to a Proxmox VE host over SSH,
manage VMs/containers, take backups straight to this PC, download existing
backup files, and schedule automatic backups.

Run with --auto for the headless scheduled-backup mode (uses SSH key login).
"""
import json
import os
import queue
import shlex
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import paramiko
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

APP_NAME = "Proxmox Backup Manager"
TASK_NAME = "Proxmox Auto Backup"
CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / "ProxmoxBackupManager"
CONFIG_FILE = CONFIG_DIR / "config.json"
KEY_FILE = Path.home() / ".ssh" / "id_ed25519"
CHUNK = 1024 * 1024

DEFAULTS = {
    "host": "",
    "port": 22,
    "user": "root",
    "backup_dir": str(Path("D:/ProxmoxBackup") if Path("D:/").exists() else Path.home() / "ProxmoxBackup"),
    "every_days": 3,
    "keep_old": 0,
    "mode": "snapshot",
}


# ----------------------------------------------------------------- config
def load_config():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass
    return cfg


def save_config(cfg):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def human(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024


# ----------------------------------------------------------------- SSH core
class Proxmox:
    def __init__(self, host, user="root", port=22, password=None):
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kwargs = dict(hostname=host, port=int(port), username=user, timeout=15)
        if password:
            kwargs.update(password=password, look_for_keys=False, allow_agent=False)
        else:
            kwargs.update(look_for_keys=True, allow_agent=True)
        self.client.connect(**kwargs)
        self.host = host

    def run(self, cmd, timeout=120):
        _, out, err = self.client.exec_command(cmd, timeout=timeout)
        code = out.channel.recv_exit_status()
        return code, out.read().decode(errors="replace"), err.read().decode(errors="replace")

    def close(self):
        self.client.close()

    # --- info
    def has_zstd(self):
        if not hasattr(self, "_zstd"):
            self._zstd = self.run("command -v zstd")[0] == 0
        return self._zstd

    def ext(self, gtype):
        z = "zst" if self.has_zstd() else "gz"
        return f"vma.{z}" if gtype == "qemu" else f"tar.{z}"

    def version(self):
        return self.run("pveversion")[1].strip()

    def guests(self):
        """Return list of dicts: type, id, name, status."""
        res = []
        _, out, _ = self.run("qm list")
        for line in out.splitlines()[1:]:
            p = line.split()
            if len(p) >= 3:
                res.append({"type": "qemu", "id": p[0], "name": p[1], "status": p[2]})
        _, out, _ = self.run("pct list")
        for line in out.splitlines()[1:]:
            p = line.split()
            if len(p) >= 2:
                res.append({"type": "lxc", "id": p[0], "status": p[1], "name": p[-1] if len(p) >= 3 else ""})
        return res

    def storage(self):
        _, out, _ = self.run("pvesm status")
        rows = []
        for line in out.splitlines()[1:]:
            p = line.split()
            if len(p) >= 7:
                rows.append(p[:7])
        return rows

    def dump_files(self):
        """Backup files on every active storage that holds backups."""
        script = r"""for s in $(pvesm status -content backup 2>/dev/null | awk 'NR>1 && $3=="active"{print $1}'); do
  pvesm list "$s" --content backup 2>/dev/null | awk 'NR>1{print $1}' | while read v; do
    p=$(pvesm path "$v" 2>/dev/null); [ -f "$p" ] && stat -c '%s	%y	%n' "$p"
  done
done"""
        _, out, _ = self.run(script)
        files = []
        for line in out.splitlines():
            p = line.split("\t")
            if len(p) == 3:
                files.append({"size": int(p[0]), "date": p[1][:16], "path": p[2]})
        if files:
            return sorted(files, key=lambda f: f["date"], reverse=True)
        path = "/var/lib/vz/dump"
        _, out, _ = self.run(f"find {shlex.quote(path)} -maxdepth 1 -type f \\( -name '*.zst' -o -name '*.gz' -o -name '*.lzo' -o -name '*.vma' -o -name '*.tar' \\) -printf '%s\\t%TY-%Tm-%Td %TH:%TM\\t%p\\n'")
        files = []
        for line in out.splitlines():
            p = line.split("\t")
            if len(p) == 3:
                files.append({"size": int(p[0]), "date": p[1], "path": p[2]})
        return sorted(files, key=lambda f: f["date"], reverse=True)

    def guest_action(self, gtype, gid, action):
        tool = "qm" if gtype == "qemu" else "pct"
        return self.run(f"{tool} {action} {int(gid)}", timeout=300)

    # --- transfers
    def stream_backup(self, gid, dest, mode="snapshot", progress=None, cancel=None):
        """Run vzdump on the host and stream the compressed result into dest."""
        log = f"/tmp/pbm-vzdump-{int(gid)}.log"
        comp = "zstd -T0 -3" if self.has_zstd() else "gzip -1"
        cmd = f"vzdump {int(gid)} --stdout --mode {shlex.quote(mode)} 2>{log} | {comp}"
        chan = self.client.get_transport().open_session()
        chan.exec_command(cmd)
        done = 0
        start = time.time()
        with open(dest, "wb") as f:
            while True:
                if cancel and cancel.is_set():
                    chan.close()
                    raise RuntimeError("Cancelled")
                data = chan.recv(CHUNK)
                if not data:
                    break
                f.write(data)
                done += len(data)
                if progress:
                    progress(done, None, done / max(time.time() - start, 0.001))
        chan.recv_exit_status()
        _, out, _ = self.run(f"cat {log}")
        ok = "Finished Backup" in out
        return ok, out

    def download(self, remote, dest, progress=None, cancel=None):
        sftp = self.client.open_sftp()
        start = time.time()

        def cb(done, total):
            if cancel and cancel.is_set():
                raise RuntimeError("Cancelled")
            if progress:
                progress(done, total, done / max(time.time() - start, 0.001))
        try:
            sftp.get(remote, dest, callback=cb)
        finally:
            sftp.close()

    def download_host_config(self, dest):
        cmd = ("tar czf - /etc/pve /etc/network/interfaces /etc/hosts /etc/hostname "
               "/etc/resolv.conf /etc/vzdump.conf /etc/cron* /root/.ssh 2>/dev/null")
        chan = self.client.get_transport().open_session()
        chan.exec_command(cmd)
        with open(dest, "wb") as f:
            while True:
                data = chan.recv(CHUNK)
                if not data:
                    break
                f.write(data)
        chan.recv_exit_status()

    def install_key(self, pubkey):
        pubkey = pubkey.strip()
        cmd = ("mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && "
               f"grep -qxF {shlex.quote(pubkey)} ~/.ssh/authorized_keys || "
               f"echo {shlex.quote(pubkey)} >> ~/.ssh/authorized_keys")
        return self.run(cmd)


# ----------------------------------------------------------------- full backup job
def full_backup(pve, cfg, log, progress=None, cancel=None):
    """Back up host config + every guest into backup_dir/<date>. Returns failure count."""
    root = Path(cfg["backup_dir"]) / pve.host.replace(":", "_")
    date =datetime.now().strftime("%Y-%m-%d")
    target = root / date
    target.mkdir(parents=True, exist_ok=True)
    log(f"Backup started -> {target}")

    pve.download_host_config(target / "pve-host-config.tar.gz")
    log("Host config saved")

    failed = 0
    for g in pve.guests():
        if cancel and cancel.is_set():
            log("Cancelled")
            return 1
        ext = pve.ext(g["type"])
        dest = target / f"vzdump-{g['type']}-{g['id']}-{g['name']}-{date}.{ext}"
        log(f"Backing up {g['type']} {g['id']} ({g['name']}) ...")
        try:
            ok, _ = pve.stream_backup(
                g["id"], dest, cfg.get("mode", "snapshot"),
                progress=(lambda d, t, s, g=g: progress(f"{g['id']} {g['name']}", d, t, s)) if progress else None,
                cancel=cancel)
        except Exception as e:
            ok = False
            log(f"  error: {e}")
        size = dest.stat().st_size if dest.exists() else 0
        if ok and size > 0:
            log(f"  OK  {human(size)}")
        else:
            log(f"  FAIL {g['type']} {g['id']}")
            failed += 1

    if failed == 0:
        keep = int(cfg.get("keep_old", 0))
        old = sorted([d for d in root.iterdir()
                      if d.is_dir() and len(d.name) == 10 and d.name[4] == "-" and d.name != date],
                     key=lambda d: d.name, reverse=True)
        for d in old[keep:]:
            import shutil
            shutil.rmtree(d, ignore_errors=True)
            log(f"Removed old backup {d.name}")
        (root / "last-success.txt").write_text(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        log("Backup finished OK")
    else:
        log(f"Backup finished with {failed} failure(s); old backups kept")
    return failed


def auto_mode():
    """Headless run for Task Scheduler: only back up when due and the host is reachable."""
    cfg = load_config()
    root = Path(cfg["backup_dir"])
    root.mkdir(parents=True, exist_ok=True)
    logfile = root / "backup-log.txt"

    def log(m):
        with open(logfile, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {m}\n")

    servers = cfg.get("servers") or ([{"host": cfg["host"], "user": cfg["user"], "port": cfg["port"]}] if cfg.get("host") else [])
    failed = 0
    for s in servers:
        mark = root / s["host"].replace(":", "_") / "last-success.txt"
        if mark.exists():
            try:
                last = datetime.strptime(mark.read_text().strip(), "%Y-%m-%d %H:%M:%S")
                if (datetime.now() - last).total_seconds() < cfg["every_days"] * 86400:
                    continue
            except Exception:
                pass
        try:
            pve = Proxmox(s["host"], s.get("user", "root"), s.get("port", 22))
        except Exception:
            continue  # offline / unreachable: try again next hour
        try:
            failed += full_backup(pve, cfg, lambda m, h=s["host"]: log(f"[{h}] {m}"))
        finally:
            pve.close()
    return 1 if failed else 0


# ----------------------------------------------------------------- scheduler
def task_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --auto'
    pyw = Path(sys.executable).with_name("pythonw.exe")
    exe = pyw if pyw.exists() else Path(sys.executable)
    return f'"{exe}" "{Path(__file__).resolve()}" --auto'


def schedule_enable():
    cmd = ["schtasks", "/Create", "/F", "/TN", TASK_NAME, "/SC", "HOURLY", "/MO", "1", "/TR", task_command()]
    return subprocess.run(cmd, capture_output=True, text=True, creationflags=0x08000000)


def schedule_disable():
    return subprocess.run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME],
                          capture_output=True, text=True, creationflags=0x08000000)


def schedule_status():
    r = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST"],
                       capture_output=True, text=True, creationflags=0x08000000)
    return r.stdout if r.returncode == 0 else "Auto backup is not enabled."


# ----------------------------------------------------------------- GUI
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("980x640")
        self.minsize(820, 520)
        self.cfg = load_config()
        self.pve = None
        self.q = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False

        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")

        self._build_login()
        self._build_main()
        self.main.pack_forget()
        self.after(100, self._poll)

    # ---------- login screen
    def _build_login(self):
        self.login = ttk.Frame(self, padding=30)
        self.login.pack(expand=True)
        ttk.Label(self.login, text=APP_NAME, font=("Segoe UI", 18, "bold")).grid(row=0, column=0, columnspan=2, pady=(0, 20))
        self.v_host = tk.StringVar(value=self.cfg["host"])
        self.v_port = tk.StringVar(value=str(self.cfg["port"]))
        self.v_user = tk.StringVar(value=self.cfg["user"])
        self.v_pass = tk.StringVar()
        ttk.Label(self.login, text="Proxmox IP / host").grid(row=1, column=0, sticky="w", pady=4, padx=(0, 10))
        self.cb_host = ttk.Combobox(self.login, textvariable=self.v_host, width=30,
                                    values=[s["host"] for s in self.cfg.get("servers", [])])
        self.cb_host.grid(row=1, column=1, pady=4)
        self.cb_host.bind("<<ComboboxSelected>>", self._pick_server)
        for i, (label, var, show) in enumerate([("SSH port", self.v_port, None),
                                                ("User", self.v_user, None),
                                                ("Password", self.v_pass, "•")], start=2):
            ttk.Label(self.login, text=label).grid(row=i, column=0, sticky="w", pady=4, padx=(0, 10))
            e = ttk.Entry(self.login, textvariable=var, width=32, show=show or "")
            e.grid(row=i, column=1, pady=4)
            if i == 4:
                e.bind("<Return>", lambda _: self.do_login())
        ttk.Label(self.login, text="Leave password empty to log in with your SSH key.",
                  foreground="gray").grid(row=5, column=0, columnspan=2, pady=(4, 10))
        self.btn_login = ttk.Button(self.login, text="Log in", command=self.do_login)
        self.btn_login.grid(row=6, column=0, columnspan=2, sticky="ew")
        self.login_msg = ttk.Label(self.login, text="", foreground="red")
        self.login_msg.grid(row=7, column=0, columnspan=2, pady=8)

    def _pick_server(self, _=None):
        for s in self.cfg.get("servers", []):
            if s["host"] == self.v_host.get():
                self.v_user.set(s.get("user", "root"))
                self.v_port.set(str(s.get("port", 22)))

    def do_login(self):
        host, user, pw = self.v_host.get().strip(), self.v_user.get().strip(), self.v_pass.get()
        if not host:
            self.login_msg.config(text="Enter the Proxmox IP.")
            return
        self.btn_login.state(["disabled"])
        self.login_msg.config(text="Connecting...", foreground="gray")

        def work():
            try:
                pve = Proxmox(host, user, int(self.v_port.get() or 22), pw or None)
                ver = pve.version()
                self.q.put(("login_ok", (pve, ver)))
            except Exception as e:
                self.q.put(("login_fail", str(e)))
        threading.Thread(target=work, daemon=True).start()

    # ---------- main window
    def _build_main(self):
        self.main = ttk.Frame(self)
        top = ttk.Frame(self.main, padding=(10, 8))
        top.pack(fill="x")
        self.lbl_conn = ttk.Label(top, text="", font=("Segoe UI", 10, "bold"))
        self.lbl_conn.pack(side="left")
        ttk.Button(top, text="Log out", command=self.do_logout).pack(side="right")
        ttk.Button(top, text="Refresh", command=self.refresh).pack(side="right", padx=6)

        nb = ttk.Notebook(self.main)
        nb.pack(fill="both", expand=True, padx=10)
        self._tab_guests(nb)
        self._tab_files(nb)
        self._tab_storage(nb)
        self._tab_auto(nb)

        # progress + log
        bottom = ttk.Frame(self.main, padding=10)
        bottom.pack(fill="x")
        row = ttk.Frame(bottom)
        row.pack(fill="x")
        self.lbl_prog = ttk.Label(row, text="Idle")
        self.lbl_prog.pack(side="left")
        self.btn_cancel = ttk.Button(row, text="Cancel", command=self.cancel.set, state="disabled")
        self.btn_cancel.pack(side="right")
        self.pbar = ttk.Progressbar(bottom, mode="determinate")
        self.pbar.pack(fill="x", pady=4)
        self.logbox = tk.Text(bottom, height=7, font=("Consolas", 9), state="disabled")
        self.logbox.pack(fill="x")

    def _tree(self, parent, cols, widths):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(frame, columns=cols, show="headings", selectmode="extended")
        for c, w in zip(cols, widths):
            tree.heading(c, text=c)
            tree.column(c, width=w, anchor="w")
        sb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        return tree

    def _tab_guests(self, nb):
        tab = ttk.Frame(nb, padding=8)
        nb.add(tab, text="VMs / Containers")
        bar = ttk.Frame(tab)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Button(bar, text="Backup selected to PC", command=self.backup_selected).pack(side="left")
        ttk.Button(bar, text="Backup ALL to PC", command=self.backup_all).pack(side="left", padx=6)
        for a in ("start", "shutdown", "stop", "reboot"):
            ttk.Button(bar, text=a.capitalize(), command=lambda a=a: self.guest_action(a)).pack(side="right", padx=2)
        self.tree_guests = self._tree(tab, ("ID", "Name", "Type", "Status"), (80, 300, 100, 120))

    def _tab_files(self, nb):
        tab = ttk.Frame(nb, padding=8)
        nb.add(tab, text="Backup files on Proxmox")
        bar = ttk.Frame(tab)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Button(bar, text="Download selected", command=self.download_selected).pack(side="left")
        ttk.Button(bar, text="Delete selected from Proxmox", command=self.delete_selected).pack(side="right")
        self.tree_files = self._tree(tab, ("Date", "Size", "File"), (140, 100, 560))

    def _tab_storage(self, nb):
        tab = ttk.Frame(nb, padding=8)
        nb.add(tab, text="Storage")
        self.tree_store = self._tree(tab, ("Name", "Type", "Status", "Total", "Used", "Free", "Use %"),
                                     (140, 80, 80, 110, 110, 110, 80))

    def _tab_auto(self, nb):
        tab = ttk.Frame(nb, padding=12)
        nb.add(tab, text="Settings & Auto backup")
        self.v_dir = tk.StringVar(value=self.cfg["backup_dir"])
        self.v_days = tk.StringVar(value=str(self.cfg["every_days"]))
        self.v_keep = tk.StringVar(value=str(self.cfg["keep_old"]))
        self.v_mode = tk.StringVar(value=self.cfg["mode"])

        g = ttk.LabelFrame(tab, text="Backup settings", padding=10)
        g.pack(fill="x")
        ttk.Label(g, text="Save backups in").grid(row=0, column=0, sticky="w")
        ttk.Entry(g, textvariable=self.v_dir, width=60).grid(row=0, column=1, padx=6, pady=3)
        ttk.Button(g, text="Browse", command=lambda: self.v_dir.set(filedialog.askdirectory() or self.v_dir.get())).grid(row=0, column=2)
        ttk.Label(g, text="Backup every (days)").grid(row=1, column=0, sticky="w")
        ttk.Spinbox(g, from_=1, to=30, textvariable=self.v_days, width=6).grid(row=1, column=1, sticky="w", padx=6, pady=3)
        ttk.Label(g, text="Old backups to keep").grid(row=2, column=0, sticky="w")
        ttk.Spinbox(g, from_=0, to=10, textvariable=self.v_keep, width=6).grid(row=2, column=1, sticky="w", padx=6, pady=3)
        ttk.Label(g, text="(0 = delete the old one after a new backup succeeds)", foreground="gray").grid(row=2, column=1, sticky="e")
        ttk.Label(g, text="Backup mode").grid(row=3, column=0, sticky="w")
        ttk.Combobox(g, textvariable=self.v_mode, values=("snapshot", "suspend", "stop"), width=10, state="readonly").grid(row=3, column=1, sticky="w", padx=6, pady=3)
        ttk.Button(g, text="Save settings", command=self.save_settings).grid(row=4, column=1, sticky="w", padx=6, pady=(8, 0))

        k = ttk.LabelFrame(tab, text="SSH key (needed for auto backup)", padding=10)
        k.pack(fill="x", pady=10)
        ttk.Label(k, text="Auto backup logs in with an SSH key, not a password. Click once while logged in.").pack(anchor="w")
        ttk.Button(k, text="Create & install SSH key on Proxmox", command=self.setup_key).pack(anchor="w", pady=4)

        a = ttk.LabelFrame(tab, text="Auto backup (Windows Task Scheduler)", padding=10)
        a.pack(fill="x")
        ttk.Label(a, text="Checks every hour. Backs up when the last backup is older than the set days.\n"
                          "If the PC was off or offline, it backs up within an hour of coming back online.").pack(anchor="w")
        b = ttk.Frame(a)
        b.pack(anchor="w", pady=6)
        ttk.Button(b, text="Enable auto backup", command=self.enable_auto).pack(side="left")
        ttk.Button(b, text="Disable", command=self.disable_auto).pack(side="left", padx=6)
        ttk.Button(b, text="Status", command=lambda: messagebox.showinfo(APP_NAME, schedule_status())).pack(side="left")
        ttk.Button(b, text="Open backup folder", command=lambda: os.startfile(self.v_dir.get()) if Path(self.v_dir.get()).exists() else None).pack(side="left", padx=6)

    # ---------- helpers
    def log(self, msg):
        self.q.put(("log", msg))

    def progress(self, label, done, total, speed):
        self.q.put(("prog", (label, done, total, speed)))

    def _poll(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "log":
                    self.logbox.config(state="normal")
                    self.logbox.insert("end", f"{datetime.now():%H:%M:%S}  {data}\n")
                    self.logbox.see("end")
                    self.logbox.config(state="disabled")
                elif kind == "prog":
                    label, done, total, speed = data
                    if total:
                        pct = done * 100 / total
                        left = (total - done) / speed if speed else 0
                        self.pbar.config(mode="determinate", value=pct)
                        self.lbl_prog.config(text=f"{label}: {human(done)} / {human(total)} ({pct:.0f}%)  "
                                                  f"{human(speed)}/s  ~{int(left // 60)} min left")
                    else:
                        self.pbar.config(mode="indeterminate")
                        self.pbar.step(2)
                        self.lbl_prog.config(text=f"{label}: {human(done)} received  {human(speed)}/s")
                elif kind == "done":
                    self.busy = False
                    self.btn_cancel.state(["disabled"])
                    self.pbar.config(mode="determinate", value=0)
                    self.lbl_prog.config(text=data or "Idle")
                    self.refresh()
                elif kind == "login_ok":
                    self.pve, ver = data
                    self.cfg.update(host=self.v_host.get().strip(), user=self.v_user.get().strip(),
                                    port=int(self.v_port.get() or 22))
                    srv = {"host": self.cfg["host"], "user": self.cfg["user"], "port": self.cfg["port"]}
                    self.cfg["servers"] = [s for s in self.cfg.get("servers", []) if s["host"] != srv["host"]] + [srv]
                    save_config(self.cfg)
                    self.cb_host["values"] = [s["host"] for s in self.cfg["servers"]]
                    self.v_pass.set("")
                    self.btn_login.state(["!disabled"])
                    self.login.pack_forget()
                    self.main.pack(fill="both", expand=True)
                    self.lbl_conn.config(text=f"Connected: {self.cfg['user']}@{self.cfg['host']}   {ver}")
                    self.refresh()
                elif kind == "login_fail":
                    self.btn_login.state(["!disabled"])
                    self.login_msg.config(text=f"Login failed: {data}", foreground="red")
                elif kind == "fill":
                    tree, rows = data
                    tree.delete(*tree.get_children())
                    for iid, vals in rows:
                        tree.insert("", "end", iid=iid, values=vals)
                elif kind == "info":
                    messagebox.showinfo(APP_NAME, data)
                elif kind == "error":
                    messagebox.showerror(APP_NAME, data)
        except queue.Empty:
            pass
        self.after(150, self._poll)

    def job(self, fn, label="Working"):
        if self.busy:
            messagebox.showwarning(APP_NAME, "Another job is running. Wait or press Cancel.")
            return
        self.busy = True
        self.cancel.clear()
        self.btn_cancel.state(["!disabled"])
        self.lbl_prog.config(text=label)

        def run():
            msg = None
            try:
                msg = fn()
            except Exception as e:
                self.log(f"Error: {e}")
                msg = f"Error: {e}"
            self.q.put(("done", msg))
        threading.Thread(target=run, daemon=True).start()

    def bg(self, fn):
        def run():
            try:
                fn()
            except Exception as e:
                self.log(f"Error: {e}")
        threading.Thread(target=run, daemon=True).start()

    # ---------- actions
    def refresh(self):
        if not self.pve:
            return

        def work():
            self.guests = self.pve.guests()
            self.q.put(("fill", (self.tree_guests,
                                 [(f"{g['type']}:{g['id']}", (g["id"], g["name"], g["type"], g["status"])) for g in self.guests])))
            files = self.pve.dump_files()
            self.q.put(("fill", (self.tree_files, [(f["path"], (f["date"], human(f["size"]), f["path"])) for f in files])))
            self.file_sizes = {f["path"]: f["size"] for f in files}
            rows = []
            for i, s in enumerate(self.pve.storage()):
                name, typ, status, total, used, avail, pct = s
                to = lambda v: human(int(v) * 1024) if v.isdigit() and int(v) else "-"
                rows.append((str(i), (name, typ, status, to(total), to(used), to(avail), pct)))
            self.q.put(("fill", (self.tree_store, rows)))
        self.bg(work)

    def do_logout(self):
        if self.busy and not messagebox.askyesno(APP_NAME, "A job is running. Cancel it and log out?"):
            return
        self.cancel.set()
        if self.pve:
            self.pve.close()
        self.pve = None
        self.main.pack_forget()
        self.login.pack(expand=True)
        self.login_msg.config(text="")

    def _selected_guests(self):
        sel = self.tree_guests.selection()
        return [g for g in getattr(self, "guests", []) if f"{g['type']}:{g['id']}" in sel]

    def guest_action(self, action):
        gs = self._selected_guests()
        if not gs:
            return messagebox.showinfo(APP_NAME, "Select a VM first.")
        names = ", ".join(f"{g['id']} {g['name']}" for g in gs)
        if action in ("stop", "shutdown", "reboot") and not messagebox.askyesno(APP_NAME, f"{action.capitalize()} {names}?"):
            return

        def work():
            for g in gs:
                code, out, err = self.pve.guest_action(g["type"], g["id"], action)
                self.log(f"{action} {g['id']}: {'OK' if code == 0 else (err or out).strip()}")
            return f"{action} done"
        self.job(work, f"{action}...")

    def _backup(self, gs):
        root = Path(self.cfg["backup_dir"]) / self.pve.host.replace(":", "_") / "manual" /datetime.now().strftime("%Y-%m-%d_%H%M")

        def work():
            root.mkdir(parents=True, exist_ok=True)
            fails = 0
            for g in gs:
                ext = self.pve.ext(g["type"])
                dest = root / f"vzdump-{g['type']}-{g['id']}-{g['name']}.{ext}"
                self.log(f"Backing up {g['id']} {g['name']} -> {dest}")
                ok, _ = self.pve.stream_backup(g["id"], dest, self.cfg["mode"],
                                               progress=lambda d, t, s, g=g: self.progress(f"{g['id']} {g['name']}", d, t, s),
                                               cancel=self.cancel)
                self.log(f"  {'OK' if ok else 'FAILED'}  {human(dest.stat().st_size)}")
                fails += 0 if ok else 1
            return f"Backup finished: {len(gs) - fails} OK, {fails} failed  ->  {root}"
        self.job(work, "Backing up...")

    def backup_selected(self):
        gs = self._selected_guests()
        if not gs:
            return messagebox.showinfo(APP_NAME, "Select one or more VMs first.")
        self._backup(gs)

    def backup_all(self):
        if not messagebox.askyesno(APP_NAME, "Back up the host config and ALL VMs/containers now?\n"
                                             "Old dated backups are removed afterwards according to your settings."):
            return
        self.job(lambda: f"Full backup done, {full_backup(self.pve, self.cfg, self.log, self.progress, self.cancel)} failure(s)",
                 "Full backup...")

    def download_selected(self):
        paths = list(self.tree_files.selection())
        if not paths:
            return messagebox.showinfo(APP_NAME, "Select a file first.")
        dest_dir = Path(self.cfg["backup_dir"]) / self.pve.host.replace(":", "_") / "downloads"

        def work():
            dest_dir.mkdir(parents=True, exist_ok=True)
            for p in paths:
                name = p.rsplit("/", 1)[-1]
                self.log(f"Downloading {name}")
                self.pve.download(p, str(dest_dir / name),
                                  progress=lambda d, t, s, n=name: self.progress(n, d, t, s), cancel=self.cancel)
                self.log("  done")
            return f"Downloaded {len(paths)} file(s) -> {dest_dir}"
        self.job(work, "Downloading...")

    def delete_selected(self):
        paths = list(self.tree_files.selection())
        if not paths:
            return
        if not messagebox.askyesno(APP_NAME, "Permanently delete these files from Proxmox?\n\n" + "\n".join(paths)):
            return

        def work():
            for p in paths:
                base = p.split(".vma")[0].split(".tar")[0]
                self.pve.run(f"rm -f {shlex.quote(p)} {shlex.quote(base)}.log {shlex.quote(p)}.notes")
                self.log(f"Deleted {p}")
            return "Delete done"
        self.job(work, "Deleting...")

    def save_settings(self):
        try:
            self.cfg.update(backup_dir=self.v_dir.get().strip(), every_days=int(self.v_days.get()),
                            keep_old=int(self.v_keep.get()), mode=self.v_mode.get())
        except ValueError:
            return messagebox.showerror(APP_NAME, "Days and keep must be numbers.")
        save_config(self.cfg)
        messagebox.showinfo(APP_NAME, "Settings saved.")

    def setup_key(self):
        def work():
            if not KEY_FILE.exists():
                KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
                subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(KEY_FILE), "-q"],
                               check=True, creationflags=0x08000000)
            pub = Path(str(KEY_FILE) + ".pub").read_text().strip()
            code, _, err = self.pve.install_key(pub)
            if code != 0:
                raise RuntimeError(err)
            self.q.put(("info", "SSH key installed. Auto backup can now log in without a password."))
        self.bg(work)

    def enable_auto(self):
        self.save_settings()
        if not KEY_FILE.exists():
            return messagebox.showwarning(APP_NAME, "Set up the SSH key first.")
        r = schedule_enable()
        if r.returncode == 0:
            messagebox.showinfo(APP_NAME, f"Auto backup enabled: every {self.cfg['every_days']} day(s) into {self.cfg['backup_dir']}.")
        else:
            messagebox.showerror(APP_NAME, r.stderr or r.stdout)

    def disable_auto(self):
        r = schedule_disable()
        messagebox.showinfo(APP_NAME, "Auto backup disabled." if r.returncode == 0 else (r.stderr or r.stdout))


if __name__ == "__main__":
    if "--auto" in sys.argv:
        sys.exit(auto_mode())
    App().mainloop()
