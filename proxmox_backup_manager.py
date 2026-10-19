"""
Proxmox Backup Manager
Windows desktop app to manage any Proxmox VE host over SSH and back it up to this PC.

Run with --auto for the headless scheduled-backup mode.
"""
import base64
import ctypes
import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path

import paramiko
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog

try:
    import sv_ttk
except ImportError:  # theme is optional
    sv_ttk = None

APP_NAME = "Proxmox Backup Manager"
VERSION = "2.0.3"
TASK_NAME = "Proxmox Auto Backup"
CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / "ProxmoxBackupManager"
CONFIG_FILE = CONFIG_DIR / "config.json"
KEY_FILE = Path.home() / ".ssh" / "id_ed25519"
CHUNK = 1024 * 1024
NO_WINDOW = 0x08000000

DEFAULTS = {
    "host": "",
    "port": 22,
    "user": "root",
    "servers": [],
    "backup_dir": str(Path("D:/ProxmoxBackup") if Path("D:/").exists() else Path.home() / "ProxmoxBackup"),
    "every_days": 3,
    "keep_old": 0,
    "mode": "snapshot",
    "theme": "dark",
}

SHORTCUTS = [
    ("F5", "Refresh"),
    ("Ctrl+B", "Backup selected VMs to PC"),
    ("Ctrl+Shift+B", "Backup ALL (full backup)"),
    ("Ctrl+D", "Download selected backup file"),
    ("Ctrl+S", "Snapshots of selected VM"),
    ("Ctrl+O", "Open VM console in browser"),
    ("Ctrl+W", "Open Proxmox web UI"),
    ("Ctrl+T", "Open SSH terminal"),
    ("Ctrl+1 … Ctrl+6", "Switch page"),
    ("Esc", "Cancel running job"),
    ("Ctrl+L", "Log out"),
    ("F1", "Show shortcuts"),
]


# ----------------------------------------------------------------- utils
def load_config():
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass
    return cfg


def save_config(cfg):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def human(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def duration(sec):
    sec = int(sec or 0)
    d, sec = divmod(sec, 86400)
    h, sec = divmod(sec, 3600)
    m = sec // 60
    return f"{d}d {h}h" if d else (f"{h}h {m}m" if h else f"{m}m")


def eta(sec):
    if not sec or sec < 0 or sec > 86400 * 3:
        return "--"
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h}h {m}m" if h else (f"{m}m {s}s" if m else f"{s}s")


UNITS = {"B": 1, "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3, "TiB": 1024 ** 4}


# --- Windows DPAPI: encrypt saved passwords so only this Windows user can read them
class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data, protect):
    buf = ctypes.create_string_buffer(data, len(data))
    inp = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = _Blob()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError("DPAPI failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def encrypt(text):
    return base64.b64encode(_dpapi(text.encode(), True)).decode()


def decrypt(token):
    try:
        return _dpapi(base64.b64decode(token), False).decode()
    except Exception:
        return ""


# ----------------------------------------------------------------- SSH / Proxmox
class Proxmox:
    def __init__(self, host, user="root", port=22, password=None):
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kw = dict(hostname=host, port=int(port), username=user, timeout=15)
        if password:
            kw.update(password=password, look_for_keys=False, allow_agent=False)
        self.client.connect(**kw)
        self.client.get_transport().set_keepalive(30)
        self.host, self.user, self.port = host, user, int(port)
        self.node = self.run("hostname")[1].strip()

    def run(self, cmd, timeout=300):
        _, out, err = self.client.exec_command(cmd, timeout=timeout)
        code = out.channel.recv_exit_status()
        return code, out.read().decode(errors="replace"), err.read().decode(errors="replace")

    def pvesh(self, method, path, **params):
        args = " ".join(f"--{k} {shlex.quote(str(v))}" for k, v in params.items())
        code, out, err = self.run(f"pvesh {method} {shlex.quote(path)} {args} --output-format json")
        if code != 0:
            raise RuntimeError((err or out).strip())
        try:
            return json.loads(out) if out.strip() else None
        except ValueError:
            return out.strip()

    def close(self):
        self.client.close()

    def has_zstd(self):
        if not hasattr(self, "_zstd"):
            self._zstd = self.run("command -v zstd")[0] == 0
        return self._zstd

    def ext(self, gtype):
        z = "zst" if self.has_zstd() else "gz"
        return f"vma.{z}" if gtype == "qemu" else f"tar.{z}"

    # --- info
    def version(self):
        return self.run("pveversion")[1].strip()

    def node_status(self):
        st = self.pvesh("get", f"/nodes/{self.node}/status")
        return st

    def guests(self):
        res = self.pvesh("get", "/cluster/resources", type="vm") or []
        out = []
        for r in res:
            if r.get("template"):
                continue
            out.append({"type": r["type"], "id": str(r["vmid"]), "name": r.get("name", ""), "status": r.get("status", ""),
                        "node": r.get("node", self.node), "cpu": r.get("cpu", 0), "maxcpu": r.get("maxcpu", 0),
                        "mem": r.get("mem", 0), "maxmem": r.get("maxmem", 0), "disk": r.get("maxdisk", 0),
                        "uptime": r.get("uptime", 0)})
        return sorted(out, key=lambda g: int(g["id"]))

    def storage(self):
        res = self.pvesh("get", f"/nodes/{self.node}/storage") or []
        return sorted(res, key=lambda s: s["storage"])

    def dump_files(self):
        script = r"""for s in $(pvesm status -content backup 2>/dev/null | awk 'NR>1 && $3=="active"{print $1}'); do
  pvesm list "$s" --content backup 2>/dev/null | awk 'NR>1{print $1}' | while read v; do
    p=$(pvesm path "$v" 2>/dev/null); [ -f "$p" ] && stat -c '%s	%y	%n' "$p"
  done
done"""
        out = self.run(script)[1]
        files = []
        for line in out.splitlines():
            p = line.split("\t")
            if len(p) == 3:
                files.append({"size": int(p[0]), "date": p[1][:16], "path": p[2]})
        return sorted(files, key=lambda f: f["date"], reverse=True)

    def _gpath(self, g):
        return f"/nodes/{g['node']}/{g['type']}/{g['id']}"

    def guest_action(self, g, action):
        return self.pvesh("create", f"{self._gpath(g)}/status/{action}")

    def snapshots(self, g):
        return [s for s in (self.pvesh("get", f"{self._gpath(g)}/snapshot") or []) if s.get("name") != "current"]

    def snapshot_create(self, g, name, desc=""):
        return self.pvesh("create", f"{self._gpath(g)}/snapshot", snapname=name, description=desc)

    def snapshot_rollback(self, g, name):
        return self.pvesh("create", f"{self._gpath(g)}/snapshot/{name}/rollback")

    def snapshot_delete(self, g, name):
        return self.pvesh("delete", f"{self._gpath(g)}/snapshot/{name}")

    def next_id(self):
        return str(self.pvesh("get", "/cluster/nextid"))

    # --- transfers
    def stream_backup(self, g, dest, mode="snapshot", progress=None, cancel=None):
        """Run vzdump on the node and stream the compressed result into dest.
        progress(pct or None, bytes_received, speed, status)  status: 'waiting' | 'starting' | None"""
        gid = int(g["id"])
        tag = f"/tmp/pbm-{gid}-{int(time.time())}-{os.getpid()}"
        log, pidf = tag + ".log", tag + ".pid"
        comp = "zstd -T0 -3" if self.has_zstd() else "gzip -1"
        inner = f"vzdump {gid} --stdout --mode {shlex.quote(mode)} 2>{log} | {comp}"
        if g.get("node") and g["node"] != self.node:  # guest lives on another cluster node
            inner = (f"ssh -o BatchMode=yes root@{shlex.quote(g['node'])} {shlex.quote(inner)}; "
                     f"ssh root@{shlex.quote(g['node'])} cat {log} > {log}")
        cmd = f"echo $$ > {pidf}; {inner}"
        chan = self.client.get_transport().open_session()
        chan.exec_command(cmd)
        chan.settimeout(1.0)
        state = {"pct": None, "status": "starting"}
        stop = threading.Event()

        def watch_log():
            rx = re.compile(r"INFO:\s+(\d+)% \(")
            while not stop.wait(2):
                try:
                    tail = self.run(f"tail -c 3000 {log} 2>/dev/null", timeout=20)[1]
                    m = rx.findall(tail)
                    if m:
                        state["pct"], state["status"] = int(m[-1]), None
                    elif "global lock - waiting" in tail and "got global lock" not in tail:
                        state["status"] = "waiting"
                    elif tail:
                        state["status"] = "starting"
                except Exception:
                    pass
        threading.Thread(target=watch_log, daemon=True).start()

        def kill_remote():
            self.run(f"p=$(cat {pidf} 2>/dev/null) && [ -n \"$p\" ] && pkill -TERM -P $p; sleep 2; "
                     f"p=$(cat {pidf} 2>/dev/null) && [ -n \"$p\" ] && pkill -KILL -P $p", timeout=30)

        done, start, last = 0, time.time(), 0
        try:
            with open(dest, "wb") as f:
                while True:
                    if cancel and cancel.is_set():
                        kill_remote()
                        chan.close()
                        raise RuntimeError("Cancelled")
                    try:
                        data = chan.recv(CHUNK)
                    except TimeoutError:
                        data = None
                    except Exception as e:  # socket.timeout on older Pythons
                        if "timed out" not in str(e):
                            raise
                        data = None
                    if data == b"":
                        break
                    if data:
                        if not done:
                            start = time.time()
                        f.write(data)
                        done += len(data)
                    now = time.time()
                    if progress and now - last > 0.3:
                        last = now
                        try:
                            progress(state["pct"], done, done / max(now - start, 0.001) if done else 0, state["status"])
                        except Exception:
                            pass  # never let a display problem stop the backup
            chan.recv_exit_status()
        except BaseException:
            try:
                kill_remote()
                chan.close()
            except Exception:
                pass
            raise
        finally:
            stop.set()
            self.run(f"rm -f {pidf}")
        out = self.run(f"cat {log}; rm -f {log}")[1]
        return "Finished Backup" in out, out

    def running_backups(self):
        """Command lines of vzdump jobs currently running or queued on this node."""
        out = self.run("ps -eo args | grep '^/usr/bin/perl -T /usr/bin/vzdump' | grep -v grep")[1]
        return [l.replace("/usr/bin/perl -T /usr/bin/", "").strip() for l in out.splitlines() if l.strip()]

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

    def upload(self, local, remote, progress=None, cancel=None):
        sftp = self.client.open_sftp()
        start = time.time()

        def cb(done, total):
            if cancel and cancel.is_set():
                raise RuntimeError("Cancelled")
            if progress:
                progress(done, total, done / max(time.time() - start, 0.001))
        try:
            sftp.put(local, remote, callback=cb)
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
        return self.run("mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && "
                        f"(grep -qxF {shlex.quote(pubkey)} ~/.ssh/authorized_keys || "
                        f"echo {shlex.quote(pubkey)} >> ~/.ssh/authorized_keys)")


# ----------------------------------------------------------------- backup job
def full_backup(pve, cfg, log, progress=None, cancel=None, guests=None):
    """Back up host config + guests into backup_dir/<host>/<date>. Returns failure count.
    progress(item_label, item_pct, item_bytes, speed, overall_pct, index, count, item_total, status)"""
    root = Path(cfg["backup_dir"]) / pve.host.replace(":", "_")
    full = guests is None
    date = datetime.now().strftime("%Y-%m-%d") if full else "manual/" + datetime.now().strftime("%Y-%m-%d_%H%M")
    target = root / date
    target.mkdir(parents=True, exist_ok=True)
    log(f"Backup started -> {target}")

    if full:
        pve.download_host_config(target / "pve-host-config.tar.gz")
        log("Host config saved")
        guests = pve.guests()

    total = sum(g["disk"] for g in guests) or 1
    finished = 0
    failed = 0
    for i, g in enumerate(guests, 1):
        if cancel and cancel.is_set():
            log("Cancelled")
            return failed + 1
        dest = target / f"vzdump-{g['type']}-{g['id']}-{g['name']}.{pve.ext(g['type'])}"
        log(f"[{i}/{len(guests)}] {g['id']} {g['name']}  (disk {human(g['disk'])})")

        def prog(pct, got, speed, status=None, g=g, i=i):
            if progress:
                cur = g["disk"] * (pct or 0) / 100
                progress(f"{g['id']} {g['name']}", pct, got, speed,
                         (finished + cur) * 100 / total, i, len(guests), g["disk"], status)
        try:
            ok, _ = pve.stream_backup(g, dest, cfg.get("mode", "snapshot"), prog, cancel)
        except Exception as e:
            ok = False
            log(f"   error: {e}")
        size = dest.stat().st_size if dest.exists() else 0
        finished += g["disk"]
        if ok and size > 0:
            log(f"   OK  saved {human(size)}")
        else:
            log(f"   FAILED {g['id']}")
            failed += 1

    if full and failed == 0:
        keep = int(cfg.get("keep_old", 0))
        old = sorted([d for d in root.iterdir() if d.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", d.name)
                      and d.name != date], key=lambda d: d.name, reverse=True)
        for d in old[keep:]:
            shutil.rmtree(d, ignore_errors=True)
            log(f"Removed old backup {d.name}")
        (root / "last-success.txt").write_text(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    log("Backup finished OK" if failed == 0 else f"Backup finished with {failed} failure(s)")
    return failed


def auto_mode():
    cfg = load_config()
    root = Path(cfg["backup_dir"])
    root.mkdir(parents=True, exist_ok=True)

    def log(m):
        with open(root / "backup-log.txt", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {m}\n")

    failed = 0
    for s in cfg.get("servers", []):
        if not s.get("auto", True):
            continue
        mark = root / s["host"].replace(":", "_") / "last-success.txt"
        try:
            last = datetime.strptime(mark.read_text().strip(), "%Y-%m-%d %H:%M:%S")
            if (datetime.now() - last).total_seconds() < cfg["every_days"] * 86400:
                continue
        except Exception:
            pass
        try:
            pve = Proxmox(s["host"], s.get("user", "root"), s.get("port", 22), decrypt(s["pw"]) if s.get("pw") else None)
        except Exception:
            continue  # offline: next hourly run will try again
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
    return f'"{pyw if pyw.exists() else sys.executable}" "{Path(__file__).resolve()}" --auto'


def schtasks(*args):
    return subprocess.run(["schtasks", *args], capture_output=True, text=True, creationflags=NO_WINDOW)


# ----------------------------------------------------------------- GUI
ACCENT = "#ff7a1a"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} {VERSION}")
        w = min(1180, self.winfo_screenwidth() - 40)
        h = min(760, self.winfo_screenheight() - 80)
        self.geometry(f"{w}x{h}+{(self.winfo_screenwidth() - w) // 2}+10")
        self.minsize(min(940, w), min(600, h))
        try:
            self.iconbitmap(Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "assets" / "icon.ico")
        except Exception:
            pass
        self.cfg = load_config()
        self.pve = None
        self.guests = []
        self.q = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        if sv_ttk:
            sv_ttk.set_theme(self.cfg.get("theme", "dark"))
        self._styles()
        self.login = self._build_login()
        self.main = self._build_main()
        self._bind_keys()
        self.after(120, self._poll)
        if self.cfg.get("auto_login"):
            s = self._server(self.cfg.get("host"))
            if s and s.get("pw"):
                self.after(300, self.do_login)

    # ---------- look
    def _styles(self):
        st = ttk.Style(self)
        st.configure("Title.TLabel", font=("Segoe UI Semibold", 22))
        st.configure("H2.TLabel", font=("Segoe UI Semibold", 14))
        st.configure("Muted.TLabel", foreground="#8b93a7")
        st.configure("Big.TLabel", font=("Segoe UI Semibold", 26))
        st.configure("Stat.TLabel", font=("Segoe UI Semibold", 18))
        st.configure("Nav.TButton", anchor="w", padding=(16, 10), font=("Segoe UI", 11))
        st.configure("Treeview", rowheight=30)
        st.configure("Green.Horizontal.TProgressbar", thickness=14)

    # ---------- login
    def _build_login(self):
        outer = ttk.Frame(self)
        outer.pack(fill="both", expand=True)
        card = ttk.Frame(outer, padding=36)
        card.place(relx=0.5, rely=0.5, anchor="center")
        ttk.Label(card, text="🖥  " + APP_NAME, style="Title.TLabel").grid(row=0, column=0, columnspan=2, pady=(0, 4))
        ttk.Label(card, text="Back up any Proxmox VE server to this PC", style="Muted.TLabel").grid(row=1, column=0, columnspan=2, pady=(0, 22))

        self.v_host = tk.StringVar(value=self.cfg.get("host", ""))
        self.v_port = tk.StringVar(value=str(self.cfg.get("port", 22)))
        self.v_user = tk.StringVar(value=self.cfg.get("user", "root"))
        self.v_pass = tk.StringVar()
        self.v_remember = tk.BooleanVar(value=True)
        self.v_autologin = tk.BooleanVar(value=bool(self.cfg.get("auto_login")))

        def field(r, label, widget):
            ttk.Label(card, text=label).grid(row=r, column=0, sticky="w", pady=6, padx=(0, 14))
            widget.grid(row=r, column=1, sticky="ew", pady=6)
        self.cb_host = ttk.Combobox(card, textvariable=self.v_host, width=30,
                                    values=[s["host"] for s in self.cfg.get("servers", [])])
        self.cb_host.bind("<<ComboboxSelected>>", lambda _: self._pick_server())
        field(2, "Proxmox IP / host", self.cb_host)
        field(3, "SSH port", ttk.Entry(card, textvariable=self.v_port, width=32))
        field(4, "Username", ttk.Entry(card, textvariable=self.v_user, width=32))
        pw = ttk.Entry(card, textvariable=self.v_pass, width=32, show="•")
        pw.bind("<Return>", lambda _: self.do_login())
        field(5, "Password", pw)
        ttk.Checkbutton(card, text="Remember login details (password encrypted on this PC)",
                        variable=self.v_remember).grid(row=6, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Checkbutton(card, text="Log in automatically when the app opens",
                        variable=self.v_autologin).grid(row=7, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Label(card, text="Leave password empty to use your SSH key.", style="Muted.TLabel").grid(row=8, column=0, columnspan=2, sticky="w", pady=(6, 14))
        self.btn_login = ttk.Button(card, text="Connect", style="Accent.TButton", command=self.do_login)
        self.btn_login.grid(row=9, column=0, columnspan=2, sticky="ew", ipady=4)
        self.login_msg = ttk.Label(card, text="", foreground="#ef4444", wraplength=380)
        self.login_msg.grid(row=10, column=0, columnspan=2, pady=10)
        ttk.Button(card, text="Forget selected server", command=self.forget_server).grid(row=11, column=0, columnspan=2)
        self._pick_server()
        return outer

    def _server(self, host):
        return next((s for s in self.cfg.get("servers", []) if s["host"] == host), None)

    def _pick_server(self):
        s = self._server(self.v_host.get())
        if s:
            self.v_user.set(s.get("user", "root"))
            self.v_port.set(str(s.get("port", 22)))
            self.v_pass.set(decrypt(s["pw"]) if s.get("pw") else "")

    def forget_server(self):
        h = self.v_host.get()
        if self._server(h) and messagebox.askyesno(APP_NAME, f"Forget saved login for {h}?"):
            self.cfg["servers"] = [s for s in self.cfg["servers"] if s["host"] != h]
            save_config(self.cfg)
            self.cb_host["values"] = [s["host"] for s in self.cfg["servers"]]
            self.v_pass.set("")

    def do_login(self):
        host = self.v_host.get().strip()
        if not host:
            return self.login_msg.config(text="Enter the Proxmox IP.")
        self.btn_login.state(["disabled"])
        self.login_msg.config(text="Connecting…", foreground="#8b93a7")
        user, port, pw = self.v_user.get().strip() or "root", int(self.v_port.get() or 22), self.v_pass.get()

        def work():
            try:
                pve = Proxmox(host, user, port, pw or None)
                self.q.put(("login_ok", (pve, pve.version(), pw)))
            except Exception as e:
                self.q.put(("login_fail", str(e) or e.__class__.__name__))
        threading.Thread(target=work, daemon=True).start()

    def _login_ok(self, pve, ver, pw):
        self.pve = pve
        srv = {"host": pve.host, "user": pve.user, "port": pve.port, "auto": True}
        old = self._server(pve.host)
        if old:
            srv["auto"] = old.get("auto", True)
        if self.v_remember.get() and pw:
            srv["pw"] = encrypt(pw)
        self.cfg["servers"] = [s for s in self.cfg.get("servers", []) if s["host"] != pve.host] + [srv]
        self.cfg.update(host=pve.host, user=pve.user, port=pve.port, auto_login=self.v_autologin.get())
        save_config(self.cfg)
        self.cb_host["values"] = [s["host"] for s in self.cfg["servers"]]
        self.btn_login.state(["!disabled"])
        self.login_msg.config(text="")
        self.login.pack_forget()
        self.main.pack(fill="both", expand=True)
        self.lbl_server.config(text=f"{pve.user}@{pve.host}")
        self.file_sizes = {}
        self.lbl_ver.config(text=f"node {pve.node} · PVE {ver.split('/')[1] if '/' in ver else ver}")
        self.show_page("dashboard")
        self.refresh()

    # ---------- main layout
    def _build_main(self):
        main = ttk.Frame(self)
        side = ttk.Frame(main, padding=(10, 14))
        side.pack(side="left", fill="y")
        ttk.Label(side, text="Proxmox", style="H2.TLabel").pack(anchor="w", padx=8)
        ttk.Label(side, text="Backup Manager", style="Muted.TLabel").pack(anchor="w", padx=8, pady=(0, 16))
        self.nav = {}
        for key, label in [("dashboard", "📊  Dashboard"), ("guests", "🖥  VMs & Containers"),
                           ("server", "🗄  Backups on server"), ("local", "💾  Backups on this PC"),
                           ("storage", "📦  Storage"), ("settings", "⚙  Auto backup & settings")]:
            b = ttk.Button(side, text=label, style="Nav.TButton", command=lambda k=key: self.show_page(k))
            b.pack(fill="x", pady=2)
            self.nav[key] = b
        ttk.Frame(side).pack(fill="y", expand=True)
        self.lbl_server = ttk.Label(side, text="", font=("Segoe UI Semibold", 10))
        self.lbl_server.pack(anchor="w", padx=8)
        self.lbl_ver = ttk.Label(side, text="", style="Muted.TLabel")
        self.lbl_ver.pack(anchor="w", padx=8, pady=(0, 8))
        ttk.Button(side, text="⌨  Shortcuts (F1)", command=self.show_shortcuts).pack(fill="x", pady=2)
        ttk.Button(side, text="◐  Light / dark", command=self.toggle_theme).pack(fill="x", pady=2)
        ttk.Button(side, text="⎋  Log out", command=self.do_logout).pack(fill="x", pady=2)

        ttk.Separator(main, orient="vertical").pack(side="left", fill="y")
        right = ttk.Frame(main)
        right.pack(side="left", fill="both", expand=True)
        self.content = ttk.Frame(right, padding=(18, 14, 18, 6))
        self.content.pack(fill="both", expand=True)
        self.pages = {
            "dashboard": self._page_dashboard(),
            "guests": self._page_guests(),
            "server": self._page_server(),
            "local": self._page_local(),
            "storage": self._page_storage(),
            "settings": self._page_settings(),
        }
        self._build_progress(right)
        return main

    def show_page(self, key):
        for k, p in self.pages.items():
            p.pack_forget()
        self.pages[key].pack(fill="both", expand=True)
        for k, b in self.nav.items():
            b.configure(style="Accent.TButton" if k == key else "Nav.TButton")
        self.current_page = key
        if key == "local":
            self.refresh_local()

    def _page(self, title, subtitle=""):
        f = ttk.Frame(self.content)
        head = ttk.Frame(f)
        head.pack(fill="x", pady=(0, 10))
        ttk.Label(head, text=title, style="Title.TLabel").pack(side="left")
        if subtitle:
            ttk.Label(head, text="   " + subtitle, style="Muted.TLabel").pack(side="left", pady=(10, 0))
        return f, head

    def _tree(self, parent, cols, widths, anchors=None):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(frame, columns=cols, show="headings", selectmode="extended")
        for i, (c, w) in enumerate(zip(cols, widths)):
            tree.heading(c, text=c)
            tree.column(c, width=w, anchor=(anchors[i] if anchors else "w"), stretch=(i == len(cols) - 1 or w > 200))
        sb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        tree.tag_configure("running", foreground="#22c55e")
        tree.tag_configure("stopped", foreground="#8b93a7")
        return tree

    def _toolbar(self, parent, buttons):
        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=(0, 10))
        for item in buttons:
            if item is None:
                ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8)
                continue
            text, cmd, *accent = item
            ttk.Button(bar, text=text, command=cmd, style="Accent.TButton" if accent else "TButton").pack(side="left", padx=(0, 6))
        return bar

    # ---------- pages
    def _page_dashboard(self):
        f, head = self._page("Dashboard")
        ttk.Button(head, text="⟳ Refresh", command=self.refresh).pack(side="right")
        grid = ttk.Frame(f)
        grid.pack(fill="x")
        self.stats = {}
        for i, (key, label) in enumerate([("cpu", "CPU"), ("mem", "RAM"), ("disk", "Root disk"),
                                          ("vms", "VMs running"), ("uptime", "Uptime"), ("last", "Last PC backup")]):
            card = ttk.LabelFrame(grid, text=label, padding=14)
            card.grid(row=i // 3, column=i % 3, sticky="nsew", padx=6, pady=6)
            v = ttk.Label(card, text="–", style="Stat.TLabel")
            v.pack(anchor="w")
            sub = ttk.Label(card, text="", style="Muted.TLabel")
            sub.pack(anchor="w")
            bar = ttk.Progressbar(card, maximum=100) if key in ("cpu", "mem", "disk") else None
            if bar:
                bar.pack(fill="x", pady=(6, 0))
            self.stats[key] = (v, sub, bar)
        for c in range(3):
            grid.columnconfigure(c, weight=1)

        q = ttk.LabelFrame(f, text="Quick actions", padding=14)
        q.pack(fill="x", pady=(12, 0), padx=6)
        acts = [("💾 Backup ALL to PC", self.backup_all, 1), ("🌐 Open web UI", self.open_web),
                ("⌨ SSH terminal", self.open_ssh), ("🔄 Check for updates", self.check_updates),
                ("⏻ Reboot node", lambda: self.node_power("reboot")), ("⏼ Shut down node", lambda: self.node_power("shutdown"))]
        for i, (text, cmd, *acc) in enumerate(acts):
            ttk.Button(q, text=text, command=cmd, style="Accent.TButton" if acc else "TButton").grid(
                row=i // 3, column=i % 3, padx=4, pady=4, sticky="ew")
        for c in range(3):
            q.columnconfigure(c, weight=1)
        return f

    def _page_guests(self):
        f, _ = self._page("VMs & Containers", "right-click a VM for more")
        self._toolbar(f, [("💾 Backup selected (Ctrl+B)", self.backup_selected, 1), ("Backup ALL", self.backup_all), None,
                          ("▶ Start", lambda: self.guest_action("start")), ("⏹ Shutdown", lambda: self.guest_action("shutdown")),
                          ("⏼ Stop", lambda: self.guest_action("stop")), ("↻ Reboot", lambda: self.guest_action("reboot")), None,
                          ("📷 Snapshots", self.open_snapshots), ("🖥 Console", self.open_console)])
        self.tree_guests = self._tree(f, ("ID", "Name", "Type", "Status", "CPU", "RAM", "Disk", "Uptime", "Node"),
                                      (70, 230, 70, 90, 70, 150, 100, 100, 100),
                                      ("center", "w", "center", "center", "e", "e", "e", "e", "w"))
        self.tree_guests.bind("<Double-1>", lambda _: self.open_console())
        menu = tk.Menu(self, tearoff=0)
        for text, cmd in [("Backup to PC", self.backup_selected), ("Snapshots…", self.open_snapshots),
                          ("Open console", self.open_console), (None, None), ("Start", lambda: self.guest_action("start")),
                          ("Shutdown", lambda: self.guest_action("shutdown")), ("Reboot", lambda: self.guest_action("reboot")),
                          ("Stop (force)", lambda: self.guest_action("stop"))]:
            menu.add_separator() if text is None else menu.add_command(label=text, command=cmd)

        def popup(e):
            row = self.tree_guests.identify_row(e.y)
            if row and row not in self.tree_guests.selection():
                self.tree_guests.selection_set(row)
            menu.tk_popup(e.x_root, e.y_root)
        self.tree_guests.bind("<Button-3>", popup)
        return f

    def _page_server(self):
        f, _ = self._page("Backups on server", "backup files stored on Proxmox storages")
        self._toolbar(f, [("⬇ Download to PC (Ctrl+D)", self.download_selected, 1),
                          ("♻ Restore on server", self.restore_server_file), None,
                          ("🗑 Delete from server", self.delete_selected)])
        self.tree_files = self._tree(f, ("Date", "Size", "File"), (150, 110, 600), ("w", "e", "w"))
        self.tree_files.bind("<Double-1>", lambda _: self.download_selected())
        return f

    def _page_local(self):
        f, _ = self._page("Backups on this PC")
        self._toolbar(f, [("📂 Open folder", self.open_folder, 1), ("⟳ Refresh", self.refresh_local), None,
                          ("⬆ Upload & restore to Proxmox", self.restore_local_file), ("🗑 Delete", self.delete_local)])
        self.lbl_local = ttk.Label(f, text="", style="Muted.TLabel")
        self.lbl_local.pack(anchor="w", pady=(0, 6))
        self.tree_local = self._tree(f, ("Modified", "Size", "File"), (150, 110, 600), ("w", "e", "w"))
        return f

    def _page_storage(self):
        f, _ = self._page("Storage")
        self.tree_store = self._tree(f, ("Name", "Type", "Content", "Total", "Used", "Free", "Use %", "Active"),
                                     (130, 80, 250, 100, 100, 100, 80, 70),
                                     ("w", "w", "w", "e", "e", "e", "e", "center"))
        return f

    def _page_settings(self):
        f, _ = self._page("Auto backup & settings")
        self.v_dir = tk.StringVar(value=self.cfg["backup_dir"])
        self.v_days = tk.StringVar(value=str(self.cfg["every_days"]))
        self.v_keep = tk.StringVar(value=str(self.cfg["keep_old"]))
        self.v_mode = tk.StringVar(value=self.cfg["mode"])

        g = ttk.LabelFrame(f, text="Backup settings", padding=14)
        g.pack(fill="x")
        ttk.Label(g, text="Save backups in").grid(row=0, column=0, sticky="w", pady=5)
        ttk.Entry(g, textvariable=self.v_dir, width=56).grid(row=0, column=1, padx=8, sticky="w")
        ttk.Button(g, text="Browse…", command=lambda: self.v_dir.set(filedialog.askdirectory() or self.v_dir.get())).grid(row=0, column=2)
        ttk.Label(g, text="Back up every (days)").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Spinbox(g, from_=1, to=60, textvariable=self.v_days, width=8).grid(row=1, column=1, sticky="w", padx=8)
        ttk.Label(g, text="Old backups to keep").grid(row=2, column=0, sticky="w", pady=5)
        kf = ttk.Frame(g)
        kf.grid(row=2, column=1, sticky="w", padx=8)
        ttk.Spinbox(kf, from_=0, to=20, textvariable=self.v_keep, width=8).pack(side="left")
        ttk.Label(kf, text="  0 = delete the old one after a new backup succeeds", style="Muted.TLabel").pack(side="left")
        ttk.Label(g, text="Backup mode").grid(row=3, column=0, sticky="w", pady=5)
        mf = ttk.Frame(g)
        mf.grid(row=3, column=1, sticky="w", padx=8)
        ttk.Combobox(mf, textvariable=self.v_mode, values=("snapshot", "suspend", "stop"), width=10, state="readonly").pack(side="left")
        ttk.Label(mf, text="  snapshot = VMs keep running", style="Muted.TLabel").pack(side="left")
        ttk.Button(g, text="Save settings", style="Accent.TButton", command=self.save_settings).grid(row=4, column=1, sticky="w", padx=8, pady=(10, 0))

        a = ttk.LabelFrame(f, text="Auto backup", padding=14)
        a.pack(fill="x", pady=12)
        ttk.Label(a, text="Checks every hour and backs up each saved server when its last backup is older than the set days.\n"
                          "If the PC was off or offline, the backup runs within an hour of coming back online.\n"
                          "Logs in with your saved password or an SSH key.", style="Muted.TLabel").pack(anchor="w")
        self.lbl_auto = ttk.Label(a, text="", font=("Segoe UI Semibold", 11))
        self.lbl_auto.pack(anchor="w", pady=8)
        b = ttk.Frame(a)
        b.pack(anchor="w")
        ttk.Button(b, text="Enable auto backup", style="Accent.TButton", command=self.enable_auto).pack(side="left")
        ttk.Button(b, text="Disable", command=self.disable_auto).pack(side="left", padx=6)
        ttk.Button(b, text="Run auto backup now", command=self.run_auto_now).pack(side="left")
        ttk.Button(b, text="Open log", command=self.open_log).pack(side="left", padx=6)

        k = ttk.LabelFrame(f, text="SSH key (optional, more secure than a saved password)", padding=14)
        k.pack(fill="x")
        ttk.Button(k, text="Create & install SSH key on this server", command=self.setup_key).pack(anchor="w")
        self.update_auto_label()
        return f

    # ---------- progress panel
    def _build_progress(self, parent):
        p = ttk.Frame(parent, padding=(18, 4, 18, 12))
        p.pack(fill="x")
        ttk.Separator(p).pack(fill="x", pady=(0, 8))
        top = ttk.Frame(p)
        top.pack(fill="x")
        self.lbl_pct = ttk.Label(top, text="", style="Big.TLabel", width=5)
        self.lbl_pct.pack(side="left")
        mid = ttk.Frame(top)
        mid.pack(side="left", fill="x", expand=True, padx=10)
        self.lbl_job = ttk.Label(mid, text="Ready", font=("Segoe UI Semibold", 11))
        self.lbl_job.pack(anchor="w")
        self.lbl_detail = ttk.Label(mid, text="", style="Muted.TLabel")
        self.lbl_detail.pack(anchor="w")
        self.btn_cancel = ttk.Button(top, text="✕ Cancel (Esc)", command=self.cancel.set, state="disabled")
        self.btn_cancel.pack(side="right")
        self.pbar = ttk.Progressbar(p, maximum=100)
        self.pbar.pack(fill="x", pady=(6, 2))
        self.lbl_overall = ttk.Label(p, text="", style="Muted.TLabel")
        self.lbl_overall.pack(anchor="w")
        self.pbar_all = ttk.Progressbar(p, maximum=100)
        self.logbox = tk.Text(p, height=5, font=("Consolas", 9), state="disabled", relief="flat", borderwidth=0)
        self.logbox.pack(fill="x", pady=(6, 0))

    def set_progress(self, label, pct, done, total, speed, overall=None, idx=None, count=None):
        """Called from the UI thread."""
        self.lbl_job.config(text=label)
        if pct is not None:
            self.pbar.config(mode="determinate", value=pct)
            self.lbl_pct.config(text=f"{pct:.0f}%")
        else:
            self.pbar.config(mode="indeterminate")
            self.pbar.step(3)
            self.lbl_pct.config(text="…")
        parts = []
        if total:
            if pct is not None and done is None:
                parts.append(f"{human(total * pct / 100)} of {human(total)} read")
            elif done is not None:
                parts.append(f"{human(done)} of {human(total)}")
        if done is not None and total is None:
            parts.append(f"{human(done)} received")
        if speed:
            parts.append(f"{human(speed)}/s")
            if total and pct:
                left = (100 - pct) / pct * (time.time() - self.item_start)
                parts.append(f"~{eta(left)} left")
        self.lbl_detail.config(text="   •   ".join(parts))
        if overall is not None and count:
            if not self.pbar_all.winfo_ismapped():
                self.pbar_all.pack(fill="x", pady=(2, 0), before=self.logbox)
            self.pbar_all.config(value=overall)
            self.lbl_overall.config(text=f"Overall: item {idx} of {count}  •  {overall:.0f}% of {human(self.job_total)}  •  "
                                         f"elapsed {eta(time.time() - self.job_start)}")

    # ---------- queue / threading
    def log(self, msg):
        self.q.put(("log", msg))

    def ui(self, fn, *a, **kw):
        self.q.put(("call", (fn, a, kw)))

    def _poll(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "log":
                    self.logbox.config(state="normal")
                    self.logbox.insert("end", f"{datetime.now():%H:%M:%S}  {data}\n")
                    self.logbox.see("end")
                    self.logbox.config(state="disabled")
                elif kind == "call":
                    fn, a, kw = data
                    try:
                        fn(*a, **kw)
                    except Exception as e:  # a UI glitch must never break a running job
                        print("ui error:", e, file=sys.stderr)
                elif kind == "done":
                    self.busy = False
                    self.btn_cancel.state(["disabled"])
                    self.pbar.config(mode="determinate", value=100 if data and not data.startswith(("Error", "Cancel")) else 0)
                    self.lbl_job.config(text=data or "Done")
                    self.lbl_detail.config(text=f"finished at {datetime.now():%H:%M}")
                    if data and not data.startswith("Error"):
                        self.bell()
                    self.refresh()
                elif kind == "login_ok":
                    self._login_ok(*data)
                elif kind == "login_fail":
                    self.btn_login.state(["!disabled"])
                    self.login_msg.config(text=f"Login failed: {data}", foreground="#ef4444")
                    if self.login.winfo_ismapped() is False:
                        self.main.pack_forget()
                        self.login.pack(fill="both", expand=True)
        except queue.Empty:
            pass
        self.after(150, self._poll)

    def job(self, fn, label, total=0):
        if self.busy:
            return messagebox.showwarning(APP_NAME, "A job is already running. Wait or press Esc to cancel.")
        self.busy = True
        self.cancel.clear()
        self.btn_cancel.state(["!disabled"])
        self.job_total, self.job_start, self.item_start = total, time.time(), time.time()
        self.pbar_all.pack_forget()
        self.lbl_overall.config(text="")
        self.lbl_job.config(text=label)
        self.lbl_detail.config(text="")
        self.lbl_pct.config(text="")

        def run():
            try:
                msg = fn()
            except Exception as e:
                self.log(f"Error: {e}")
                msg = "Cancelled" if "Cancelled" in str(e) else f"Error: {e}"
            self.q.put(("done", msg))
        threading.Thread(target=run, daemon=True).start()

    def bg(self, fn):
        def run():
            try:
                fn()
            except Exception as e:
                self.log(f"Error: {e}")
        threading.Thread(target=run, daemon=True).start()

    def need_login(self):
        return self.pve is None

    # ---------- refresh
    def refresh(self):
        if self.need_login():
            return

        def work():
            pve = self.pve
            guests = pve.guests()
            self.guests = guests
            st = pve.node_status()
            stores = pve.storage()
            self.ui(self._fill_all, guests, st, None, stores)
            self.ui(self._fill_files, pve.dump_files())
        self.bg(work)

    def _fill_all(self, guests, st, files, stores):
        t = self.tree_guests
        sel = t.selection()
        t.delete(*t.get_children())
        for g in guests:
            iid = f"{g['type']}:{g['id']}"
            run = g["status"] == "running"
            t.insert("", "end", iid=iid, tags=("running" if run else "stopped",), values=(
                g["id"], g["name"], "VM" if g["type"] == "qemu" else "CT", ("● " if run else "○ ") + g["status"],
                f"{g['cpu'] * 100:.0f}%" if run else "-",
                f"{human(g['mem'])} / {human(g['maxmem'])}" if run else human(g["maxmem"]),
                human(g["disk"]), duration(g["uptime"]) if run else "-", g["node"]))
        t.selection_set([i for i in sel if t.exists(i)])

        t = self.tree_store
        t.delete(*t.get_children())
        for s in stores:
            tot, used = s.get("total", 0), s.get("used", 0)
            t.insert("", "end", values=(s["storage"], s.get("type", ""), s.get("content", ""), human(tot) if tot else "-",
                                        human(used) if tot else "-", human(s.get("avail", 0)) if tot else "-",
                                        f"{used * 100 / tot:.0f}%" if tot else "-", "yes" if s.get("active") else "no"))

        cpu = st.get("cpu", 0) * 100
        mem = st.get("memory", {})
        rfs = st.get("rootfs", {})
        self._stat("cpu", f"{cpu:.0f}%", f"{st.get('cpuinfo', {}).get('cpus', '?')} cores · load {', '.join(st.get('loadavg', [])[:1])}", cpu)
        mp = mem.get("used", 0) * 100 / (mem.get("total") or 1)
        self._stat("mem", f"{mp:.0f}%", f"{human(mem.get('used'))} of {human(mem.get('total'))}", mp)
        dp = rfs.get("used", 0) * 100 / (rfs.get("total") or 1)
        self._stat("disk", f"{dp:.0f}%", f"{human(rfs.get('avail'))} free of {human(rfs.get('total'))}", dp)
        running = sum(g["status"] == "running" for g in guests)
        self._stat("vms", f"{running} / {len(guests)}", "running / total")
        self._stat("uptime", duration(st.get("uptime")), f"node {self.pve.node}")
        mark = Path(self.cfg["backup_dir"]) / self.pve.host.replace(":", "_") / "last-success.txt"
        last = mark.read_text().strip() if mark.exists() else "never"
        self._stat("last", last[:10] if last != "never" else "never", last[11:16] if last != "never" else "use Backup ALL or auto backup")
        self.update_auto_label()

    def _fill_files(self, files):
        self.file_sizes = {}
        t = self.tree_files
        t.delete(*t.get_children())
        for f in files:
            self.file_sizes[f["path"]] = f["size"]
            t.insert("", "end", iid=f["path"], values=(f["date"], human(f["size"]), f["path"]))

    def _stat(self, key, value, sub, pct=None):
        v, s, bar = self.stats[key]
        v.config(text=value)
        s.config(text=sub)
        if bar is not None and pct is not None:
            bar.config(value=pct)

    # ---------- shortcuts / misc
    def _bind_keys(self):
        b = self.bind_all
        guard = lambda fn: (lambda e: None if self.need_login() else fn())
        b("<F5>", guard(lambda: (self.refresh(), self.refresh_local())))
        b("<Control-b>", guard(self.backup_selected))
        b("<Control-B>", guard(self.backup_all))
        b("<Control-d>", guard(self.download_selected))
        b("<Control-s>", guard(self.open_snapshots))
        b("<Control-o>", guard(self.open_console))
        b("<Control-w>", guard(self.open_web))
        b("<Control-t>", guard(self.open_ssh))
        b("<Control-l>", guard(self.do_logout))
        b("<Escape>", lambda e: self.cancel.set() if self.busy else None)
        b("<F1>", lambda e: self.show_shortcuts())
        for i, k in enumerate(["dashboard", "guests", "server", "local", "storage", "settings"], 1):
            b(f"<Control-Key-{i}>", guard(lambda k=k: self.show_page(k)))

    def show_shortcuts(self):
        messagebox.showinfo("Keyboard shortcuts", "\n".join(f"{k:<18}{v}" for k, v in SHORTCUTS))

    def toggle_theme(self):
        if not sv_ttk:
            return
        self.cfg["theme"] = "light" if self.cfg.get("theme", "dark") == "dark" else "dark"
        sv_ttk.set_theme(self.cfg["theme"])
        self._styles()
        save_config(self.cfg)

    def do_logout(self):
        if self.busy and not messagebox.askyesno(APP_NAME, "A job is running. Cancel it and log out?"):
            return
        self.cancel.set()
        if self.pve:
            self.pve.close()
        self.pve = None
        self.cfg["auto_login"] = False
        self.v_autologin.set(False)
        save_config(self.cfg)
        self.main.pack_forget()
        self.login.pack(fill="both", expand=True)

    def open_web(self):
        webbrowser.open(f"https://{self.pve.host}:8006")

    def open_ssh(self):
        subprocess.Popen(["cmd", "/c", "start", "Proxmox SSH", "ssh", "-p", str(self.pve.port), f"{self.pve.user}@{self.pve.host}"])

    def open_console(self):
        gs = self._selected()
        if not gs:
            return messagebox.showinfo(APP_NAME, "Select a VM first.")
        g = gs[0]
        kind = "kvm" if g["type"] == "qemu" else "lxc"
        webbrowser.open(f"https://{self.pve.host}:8006/?console={kind}&novnc=1&vmid={g['id']}&vmname={g['name']}&node={g['node']}&resize=scale")

    def check_updates(self):
        def work():
            code, out, _ = self.pve.run("apt list --upgradable 2>/dev/null | tail -n +2", timeout=60)
            pk = [l.split("/")[0] for l in out.splitlines() if l.strip()]
            self.ui(messagebox.showinfo, APP_NAME, f"{len(pk)} package update(s) available (from the last apt update).\n\n"
                    + "\n".join(pk[:25]) + ("\n…" if len(pk) > 25 else "")
                    + "\n\nInstall them from the web UI: Node → Updates.")
        self.bg(work)

    def node_power(self, cmd):
        if not messagebox.askyesno(APP_NAME, f"{cmd.capitalize()} the whole Proxmox node '{self.pve.node}'?\n\n"
                                             "All VMs on it will go down.", icon="warning"):
            return
        self.bg(lambda: (self.pve.pvesh("create", f"/nodes/{self.pve.node}/status", command=cmd),
                         self.log(f"Node {cmd} sent")))

    # ---------- guests
    def _selected(self):
        sel = set(self.tree_guests.selection())
        return [g for g in self.guests if f"{g['type']}:{g['id']}" in sel]

    def guest_action(self, action):
        gs = self._selected()
        if not gs:
            return messagebox.showinfo(APP_NAME, "Select a VM first.")
        names = ", ".join(f"{g['id']} {g['name']}" for g in gs)
        if action != "start" and not messagebox.askyesno(APP_NAME, f"{action.capitalize()} {names}?"):
            return

        def work():
            for g in gs:
                try:
                    self.pve.guest_action(g, action)
                    self.log(f"{action} {g['id']}: sent")
                except Exception as e:
                    self.log(f"{action} {g['id']}: {e}")
            time.sleep(3)
            return f"{action.capitalize()} sent to {len(gs)} guest(s)"
        self.job(work, f"{action.capitalize()}…")

    def _run_backup(self, guests):
        total = sum(g["disk"] for g in guests)

        def prog(label, pct, got, speed, overall, idx, count, item_total, status=None):
            if getattr(self, "_last_item", None) != idx:
                self._last_item = idx
                self.item_start = time.time()
            if status == "waiting":
                self.item_start = time.time()
                title = f"Waiting: another backup is running on the server ({label})"
                detail = "Proxmox runs one backup at a time. This one starts automatically when that finishes."
            elif status == "starting" and not pct:
                title, detail = f"Preparing {label}…", "Taking snapshot and starting the backup"
            else:
                title = f"Backing up {label}"
                detail = (f"{human(item_total * (pct or 0) / 100)} of {human(item_total)} read"
                          f"   •   {human(got)} saved   •   {human(speed)}/s"
                          + (f"   •   ~{eta((100 - pct) / pct * (time.time() - self.item_start))} left" if pct else ""))
            self.ui(self.set_progress, title, pct, None, item_total, speed, overall, idx, count)
            self.ui(self.lbl_detail.config, text=detail)
        return total, prog

    def _busy_on_server(self):
        try:
            jobs = self.pve.running_backups()
        except Exception:
            return False
        if not jobs:
            return False
        return not messagebox.askyesno(
            APP_NAME, "A backup is already running on this Proxmox server:\n\n" + "\n".join(jobs[:6]) +
            "\n\nProxmox runs one backup at a time, so yours will wait in line until it finishes.\nStart anyway?",
            icon="warning")

    def backup_selected(self):
        gs = self._selected()
        if not gs:
            self.show_page("guests")
            return messagebox.showinfo(APP_NAME, "Select one or more VMs first.")
        if not self.busy and self._busy_on_server():
            return
        total, prog = self._run_backup(gs)
        self.job(lambda: f"Backup done: {len(gs) - full_backup(self.pve, self.cfg, self.log, prog, self.cancel, gs)} of {len(gs)} OK",
                 f"Backing up {len(gs)} guest(s), {human(total)} of disks", total)

    def backup_all(self):
        total = sum(g["disk"] for g in self.guests)
        if not messagebox.askyesno(APP_NAME, f"Back up the host config and ALL {len(self.guests)} VMs/containers now?\n"
                                             f"Disk size to read: {human(total)}\nSaved to: {self.cfg['backup_dir']}"):
            return
        if not self.busy and self._busy_on_server():
            return
        _, prog = self._run_backup(self.guests)
        self.job(lambda: ("Full backup finished OK" if full_backup(self.pve, self.cfg, self.log, prog, self.cancel) == 0
                          else "Full backup finished with failures (see log)"), "Full backup…", total)

    def open_snapshots(self):
        gs = self._selected()
        if not gs:
            self.show_page("guests")
            return messagebox.showinfo(APP_NAME, "Select a VM first.")
        SnapshotWindow(self, gs[0])

    # ---------- server files
    def download_selected(self):
        paths = list(self.tree_files.selection())
        if not paths:
            self.show_page("server")
            return messagebox.showinfo(APP_NAME, "Select a backup file first.")
        dest_dir = Path(self.cfg["backup_dir"]) / self.pve.host.replace(":", "_") / "downloads"
        total = sum(self.file_sizes.get(p, 0) for p in paths)

        def work():
            dest_dir.mkdir(parents=True, exist_ok=True)
            base = 0
            for i, p in enumerate(paths, 1):
                name = p.rsplit("/", 1)[-1]
                self.item_start = time.time()
                self.log(f"Downloading {name}")

                def prog(d, t, s, name=name, i=i, base=base):
                    self.ui(self.set_progress, f"Downloading {name}", d * 100 / (t or 1), d, t, s,
                            (base + d) * 100 / (total or 1), i, len(paths))
                self.pve.download(p, str(dest_dir / name), prog, self.cancel)
                base += self.file_sizes.get(p, 0)
            return f"Downloaded {len(paths)} file(s), {human(total)} → {dest_dir}"
        self.job(work, "Downloading…", total)

    def delete_selected(self):
        paths = list(self.tree_files.selection())
        if not paths or not messagebox.askyesno(APP_NAME, "Permanently delete from Proxmox?\n\n" + "\n".join(paths), icon="warning"):
            return

        def work():
            for p in paths:
                base = re.sub(r"\.(vma|tar)(\.(zst|gz|lzo))?$", "", p)
                self.pve.run(f"rm -f {shlex.quote(p)} {shlex.quote(base + '.log')} {shlex.quote(p + '.notes')}")
                self.log(f"Deleted {p}")
            return f"Deleted {len(paths)} file(s)"
        self.job(work, "Deleting…")

    def _ask_restore(self, filename):
        is_ct = ".tar" in filename or "-lxc-" in filename
        newid = simpledialog.askstring(APP_NAME, "Restore as VM ID (must be unused):", initialvalue=self.pve.next_id(), parent=self)
        if not newid:
            return None
        stores = [s["storage"] for s in self.pve.storage() if ("rootdir" if is_ct else "images") in s.get("content", "")]
        store = simpledialog.askstring(APP_NAME, f"Target storage ({', '.join(stores)}):",
                                       initialvalue=stores[0] if stores else "local-lvm", parent=self)
        if not store:
            return None
        return is_ct, int(newid), store

    def _restore_cmd(self, path, is_ct, newid, store):
        if is_ct:
            return f"pct restore {newid} {shlex.quote(path)} --storage {shlex.quote(store)}"
        return f"qmrestore {shlex.quote(path)} {newid} --storage {shlex.quote(store)}"

    def restore_server_file(self):
        paths = list(self.tree_files.selection())
        if not paths:
            return messagebox.showinfo(APP_NAME, "Select a backup file first.")
        ans = self._ask_restore(paths[0])
        if not ans:
            return

        def work():
            code, out, err = self.pve.run(self._restore_cmd(paths[0], *ans), timeout=86400)
            self.log((out + err).strip()[-800:])
            return f"Restored as {ans[1]}" if code == 0 else "Error: restore failed (see log)"
        self.job(work, f"Restoring as ID {ans[1]}…")

    # ---------- local files
    def _local_root(self):
        return Path(self.cfg["backup_dir"])

    def refresh_local(self):
        root = self._local_root()
        t = self.tree_local
        t.delete(*t.get_children())
        if not root.exists():
            self.lbl_local.config(text=f"{root} does not exist yet")
            return
        files = sorted([p for p in root.rglob("*") if p.is_file() and re.search(r"\.(zst|gz|lzo|vma|tar)$", p.name)],
                       key=lambda p: p.stat().st_mtime, reverse=True)
        total = 0
        for p in files:
            s = p.stat()
            total += s.st_size
            t.insert("", "end", iid=str(p), values=(datetime.fromtimestamp(s.st_mtime).strftime("%Y-%m-%d %H:%M"),
                                                     human(s.st_size), str(p.relative_to(root))))
        free = shutil.disk_usage(root).free
        self.lbl_local.config(text=f"{len(files)} files · {human(total)} used · {human(free)} free on this drive · {root}")

    def open_folder(self):
        root = self._local_root()
        root.mkdir(parents=True, exist_ok=True)
        os.startfile(root)

    def delete_local(self):
        sel = list(self.tree_local.selection())
        if sel and messagebox.askyesno(APP_NAME, f"Delete {len(sel)} file(s) from this PC?", icon="warning"):
            for p in sel:
                Path(p).unlink(missing_ok=True)
            self.refresh_local()

    def restore_local_file(self):
        sel = list(self.tree_local.selection())
        if not sel:
            return messagebox.showinfo(APP_NAME, "Select a backup file first.")
        local = Path(sel[0])
        ans = self._ask_restore(local.name)
        if not ans:
            return
        size = local.stat().st_size
        free = int(self.pve.run("df -B1 --output=avail /var/lib/vz | tail -1")[1].strip() or 0)
        if free < size:
            return messagebox.showerror(APP_NAME, f"Not enough space on Proxmox /var/lib/vz: need {human(size)}, free {human(free)}.")
        name = re.sub(r"[^\w.\-]", "_", local.name)
        if not name.startswith("vzdump-"):
            name = "vzdump-" + name
        remote = f"/var/lib/vz/dump/{name}"

        def work():
            self.item_start = time.time()
            self.pve.upload(str(local), remote, lambda d, t, s: self.ui(self.set_progress, f"Uploading {local.name}",
                                                                        d * 100 / (t or 1), d, t, s), self.cancel)
            self.ui(self.set_progress, f"Restoring as ID {ans[1]}…", None, None, None, None)
            code, out, err = self.pve.run(self._restore_cmd(remote, *ans), timeout=86400)
            self.log((out + err).strip()[-800:])
            self.pve.run(f"rm -f {shlex.quote(remote)}")
            return f"Restored as {ans[1]}" if code == 0 else "Error: restore failed (see log)"
        self.job(work, "Uploading…", size)

    # ---------- settings / auto
    def save_settings(self, quiet=False):
        try:
            self.cfg.update(backup_dir=self.v_dir.get().strip(), every_days=int(self.v_days.get()),
                            keep_old=int(self.v_keep.get()), mode=self.v_mode.get())
        except ValueError:
            return messagebox.showerror(APP_NAME, "Days and keep must be numbers.")
        save_config(self.cfg)
        if not quiet:
            messagebox.showinfo(APP_NAME, "Settings saved.")

    def update_auto_label(self):
        r = schtasks("/Query", "/TN", TASK_NAME, "/FO", "LIST")
        on = r.returncode == 0 and "--auto" in schtasks("/Query", "/TN", TASK_NAME, "/XML").stdout
        self.lbl_auto.config(text=(f"● Enabled: every {self.cfg['every_days']} day(s) → {self.cfg['backup_dir']}" if on
                                   else ("○ An older auto backup task exists. Click Enable to replace it." if r.returncode == 0
                                         else "○ Auto backup is off")),
                             foreground="#22c55e" if on else "#8b93a7")

    def enable_auto(self):
        self.save_settings(quiet=True)
        srv = self._server(self.pve.host) if self.pve else None
        if srv and not srv.get("pw") and not KEY_FILE.exists():
            return messagebox.showwarning(APP_NAME, "Auto backup needs a saved password (tick 'Remember login' when "
                                                    "logging in) or an SSH key (button below).")
        r = schtasks("/Create", "/F", "/TN", TASK_NAME, "/SC", "HOURLY", "/MO", "1", "/TR", task_command())
        if r.returncode != 0:
            return messagebox.showerror(APP_NAME, r.stderr or r.stdout)
        self.update_auto_label()
        messagebox.showinfo(APP_NAME, f"Auto backup enabled for {len(self.cfg.get('servers', []))} saved server(s), "
                                      f"every {self.cfg['every_days']} day(s).")

    def disable_auto(self):
        schtasks("/Delete", "/F", "/TN", TASK_NAME)
        self.update_auto_label()

    def run_auto_now(self):
        if messagebox.askyesno(APP_NAME, "Run the auto backup in the background now? It only backs up servers that are due."):
            schtasks("/Run", "/TN", TASK_NAME)

    def open_log(self):
        p = self._local_root() / "backup-log.txt"
        if p.exists():
            os.startfile(p)
        else:
            messagebox.showinfo(APP_NAME, "No auto backup log yet.")

    def setup_key(self):
        def work():
            if not KEY_FILE.exists():
                KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
                subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(KEY_FILE), "-q"], check=True, creationflags=NO_WINDOW)
            code, _, err = self.pve.install_key(Path(str(KEY_FILE) + ".pub").read_text())
            if code != 0:
                raise RuntimeError(err)
            self.ui(messagebox.showinfo, APP_NAME, "SSH key installed. Auto backup can log in without a password.")
        self.bg(work)


class SnapshotWindow(tk.Toplevel):
    def __init__(self, app, g):
        super().__init__(app)
        self.app, self.g = app, g
        self.title(f"Snapshots · {g['id']} {g['name']}")
        self.geometry("640x420")
        self.transient(app)
        f = ttk.Frame(self, padding=14)
        f.pack(fill="both", expand=True)
        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Button(bar, text="📷 Take snapshot", style="Accent.TButton", command=self.create).pack(side="left")
        ttk.Button(bar, text="↺ Roll back", command=self.rollback).pack(side="left", padx=6)
        ttk.Button(bar, text="🗑 Delete", command=self.delete).pack(side="left")
        ttk.Button(bar, text="⟳", width=3, command=self.load).pack(side="right")
        self.tree = app._tree(f, ("Name", "Date", "Description"), (160, 150, 280))
        self.status = ttk.Label(f, text="", style="Muted.TLabel")
        self.status.pack(anchor="w", pady=(6, 0))
        self.load()

    def _bg(self, fn, msg):
        self.status.config(text=msg)

        def run():
            try:
                fn()
                self.app.ui(self.load)
            except Exception as e:
                self.app.ui(messagebox.showerror, APP_NAME, str(e))
                self.app.ui(self.status.config, text="")
        threading.Thread(target=run, daemon=True).start()

    def load(self):
        def run():
            snaps = self.app.pve.snapshots(self.g)
            self.app.ui(self._fill, snaps)
        threading.Thread(target=run, daemon=True).start()

    def _fill(self, snaps):
        if not self.winfo_exists():
            return
        self.tree.delete(*self.tree.get_children())
        for s in sorted(snaps, key=lambda s: s.get("snaptime", 0), reverse=True):
            when = datetime.fromtimestamp(s["snaptime"]).strftime("%Y-%m-%d %H:%M") if s.get("snaptime") else ""
            self.tree.insert("", "end", iid=s["name"], values=(s["name"], when, s.get("description", "").strip()))
        self.status.config(text=f"{len(snaps)} snapshot(s)")

    def _sel(self):
        s = self.tree.selection()
        return s[0] if s else None

    def create(self):
        name = simpledialog.askstring(APP_NAME, "Snapshot name (letters, numbers, _ or -):",
                                      initialvalue=datetime.now().strftime("snap_%Y%m%d_%H%M"), parent=self)
        if name:
            desc = simpledialog.askstring(APP_NAME, "Description (optional):", parent=self) or ""
            self._bg(lambda: self.app.pve.snapshot_create(self.g, name, desc), "Taking snapshot…")

    def rollback(self):
        n = self._sel()
        if n and messagebox.askyesno(APP_NAME, f"Roll back {self.g['name']} to '{n}'?\nChanges after it will be lost.",
                                     icon="warning", parent=self):
            self._bg(lambda: self.app.pve.snapshot_rollback(self.g, n), "Rolling back…")

    def delete(self):
        n = self._sel()
        if n and messagebox.askyesno(APP_NAME, f"Delete snapshot '{n}'?", parent=self):
            self._bg(lambda: self.app.pve.snapshot_delete(self.g, n), "Deleting…")


if __name__ == "__main__":
    if "--auto" in sys.argv:
        sys.exit(auto_mode())
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    App().mainloop()
