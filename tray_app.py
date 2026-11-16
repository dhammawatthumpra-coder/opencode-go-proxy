#!/usr/bin/env python3
"""Windows tray wrapper for the OpenCode Go proxy (go-session-proxy.mjs).

Runs the Node proxy as a child process and shows a system-tray icon managing:
  1. Proxy control:
     - status line (running / stopped, port, upstream)
     - Live log window (tails logs/proxy.log in real time)
     - Run in console window / Run hidden
     - Start / Stop / Restart
  2. Utility:
     - Copy harness base URL to clipboard
     - Floating stats overlay (cache hit %, tokens and money saved)
     - Open proxy folder
     - Auto-start with Windows on/off
     - Quit

Requires: pystray, pillow  (pip install pystray pillow)
Run with pythonw.exe for no console window (see start_tray.bat).
"""

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROXY_JS = os.path.join(BASE_DIR, "go-session-proxy.mjs")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
ICON_PATH = os.path.join(BASE_DIR, "icon.png")
TRAY_PY = os.path.abspath(__file__)

STARTUP_LNK = os.path.join(
    os.environ.get("APPDATA", ""),
    "Microsoft", "Windows", "Start Menu", "Programs", "Startup",
    "OpenCode Go Proxy.lnk",
)

MAX_LOG_BYTES = 5 * 1024 * 1024

try:
    import pystray
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("tray_app needs: pip install pystray pillow", file=sys.stderr)
    sys.exit(2)


def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


CFG = load_config()
PORT = int(CFG.get("port", 8787))
PORT_SCAN = int(CFG.get("port_scan", 20))
UPSTREAM = CFG.get("upstream", "https://opencode.ai/inference/go/v1")
LOG_PATH = os.path.join(BASE_DIR, CFG.get("log_file", "logs/proxy.log"))

# Filled in once the proxy reports the port it actually bound, which differs
# from PORT when the preferred port is taken and the proxy scans upward.
RUNTIME = {"port": None}


def base_url(port=None):
    p = port or RUNTIME["port"] or PORT
    return f"http://127.0.0.1:{p}/v1"


def read_runtime_port():
    """Last RUNTIME_PORT=<n> the proxy printed in its log, or None."""
    try:
        if not os.path.exists(LOG_PATH):
            return None
        with open(LOG_PATH, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 16384))
            tail = f.read().decode("utf-8", "replace")
    except Exception:
        return None
    matches = re.findall(r"RUNTIME_PORT=(\d+)", tail)
    return int(matches[-1]) if matches else None


def find_node():
    configured = (CFG.get("node_exe") or "").strip()
    if configured and os.path.exists(configured):
        return configured
    for cand in (r"C:\Program Files\nodejs\node.exe",
                 r"C:\Program Files (x86)\nodejs\node.exe"):
        if os.path.exists(cand):
            return cand
    return shutil.which("node") or "node"


def make_icon():
    if os.path.exists(ICON_PATH):
        try:
            return Image.open(ICON_PATH)
        except Exception:
            pass
    img = Image.new("RGBA", (64, 64), (30, 30, 46, 255))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([4, 4, 60, 60], radius=14, fill=(22, 163, 74, 255))
    try:
        font = ImageFont.truetype("arial.ttf", 24)
    except Exception:
        font = ImageFont.load_default()
    d.text((32, 32), "GO", fill=(255, 255, 255, 255), font=font, anchor="mm")
    try:
        img.save(ICON_PATH)
    except Exception:
        pass
    return img


class ProxyCtl:
    def __init__(self):
        self.proc = None
        self.lock = threading.RLock()
        self.wanted = True     # False after Quit
        self.enabled = True    # False after Stop
        self.mode = "hidden"   # "hidden" | "console"
        self.logfh = None
        self.started_at = 0.0
        self.fail_streak = 0

    # -- helpers -----------------------------------------------------------
    def _env(self):
        env = os.environ.copy()
        env["PORT"] = str(PORT)
        env["PORT_SCAN"] = str(PORT_SCAN)
        env["UPSTREAM"] = UPSTREAM
        env["SESSION_MODE"] = CFG.get("session_mode", "conversation")
        if CFG.get("log_requests", True):
            env["LOG"] = "1"
        if CFG.get("log_usage", True):
            env["USAGE"] = "1"
        return env

    def _rotate_log(self):
        try:
            if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > MAX_LOG_BYTES:
                old = LOG_PATH + ".1"
                if os.path.exists(old):
                    os.remove(old)
                os.replace(LOG_PATH, old)
        except Exception:
            pass

    def _open_log(self):
        try:
            os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
            return open(LOG_PATH, "ab")
        except Exception:
            return None

    def _close_log(self):
        if self.logfh:
            try:
                self.logfh.close()
            except Exception:
                pass
            self.logfh = None

    # -- lifecycle ---------------------------------------------------------
    def start(self):
        with self.lock:
            if self.proc and self.proc.poll() is None:
                return
            RUNTIME["port"] = None
            self._rotate_log()
            cmd = [find_node(), PROXY_JS]

            if self.mode == "console":
                self._close_log()
                self.proc = subprocess.Popen(
                    cmd, cwd=BASE_DIR, env=self._env(),
                    creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
                )
            else:
                self.logfh = self._open_log()
                kwargs = {}
                if self.logfh:
                    kwargs["stdout"] = self.logfh
                    kwargs["stderr"] = subprocess.STDOUT
                else:
                    kwargs["stdout"] = subprocess.DEVNULL
                    kwargs["stderr"] = subprocess.DEVNULL
                self.proc = subprocess.Popen(
                    cmd, cwd=BASE_DIR, env=self._env(),
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    **kwargs,
                )
            self.started_at = time.time()

    def stop(self):
        with self.lock:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except Exception:
                    self.proc.kill()
            self.proc = None
            self._close_log()

    def alive(self):
        with self.lock:
            return self.proc is not None and self.proc.poll() is None

    def set_mode(self, mode):
        with self.lock:
            if mode == self.mode:
                return
            self.stop()
            self.mode = mode
            if self.enabled and self.wanted:
                time.sleep(0.3)
                self.start()

    def set_enabled(self, enabled):
        with self.lock:
            self.enabled = enabled
            if enabled and self.wanted:
                self.start()
            else:
                self.stop()


ctl = ProxyCtl()
icon = None

# ------------------------------------------------------------ windows & actions ---

_log_win_open = False


def open_log_window(ic=None, item=None):
    """Open a live-tailing log window (single instance)."""
    global _log_win_open
    if _log_win_open:
        return
    _log_win_open = True

    def _run():
        global _log_win_open
        try:
            import tkinter as tk
            from tkinter.scrolledtext import ScrolledText
        except ImportError:
            _log_win_open = False
            return

        root = tk.Tk()
        root.title("OpenCode Go Proxy - Live Log")
        root.geometry("950x620")

        txt = ScrolledText(root, state="disabled",
                           font=("Consolas", 9), bg="#1e1e1e", fg="#d4d4d4")
        txt.pack(fill="both", expand=True)

        closed = {"v": False}

        def on_close():
            closed["v"] = True
            global _log_win_open
            _log_win_open = False
            root.destroy()

        root.protocol("WM_DELETE_WINDOW", on_close)

        txt.config(state="normal")
        txt.insert("end", f"# {base_url()}  ->  {UPSTREAM}\n"
                          f"# log: {LOG_PATH}\n\n")
        txt.config(state="disabled")

        pos = os.path.getsize(LOG_PATH) if os.path.exists(LOG_PATH) else 0

        def poll():
            nonlocal pos
            if closed["v"]:
                return
            try:
                size = os.path.getsize(LOG_PATH) if os.path.exists(LOG_PATH) else 0
                if size < pos:
                    pos = 0
                if size > pos:
                    with open(LOG_PATH, encoding="utf-8", errors="replace") as f:
                        f.seek(pos)
                        data = f.read()
                    pos = size
                    if data:
                        txt.config(state="normal")
                        txt.insert("end", data)
                        nlines = int(txt.index("end-1c").split(".")[0])
                        if nlines > 3000:
                            txt.delete("1.0", "%d.0" % (nlines - 3000))
                        txt.see("end")
                        txt.config(state="disabled")
            except Exception:
                pass
            root.after(500, poll)

        root.after(500, poll)
        root.mainloop()

    threading.Thread(target=_run, daemon=True).start()


# ------------------------------------------------------------ stats overlay ---

# A small floating card that only reads GET /__stats. Shown and hidden from the
# tray menu; while hidden it keeps the window alive but withdrawn, so toggling
# is instant and the Tk loop is only ever created once.
_stats_win = {"want": False, "open": False}


def stats_url():
    return f"http://127.0.0.1:{RUNTIME['port'] or PORT}/__stats"


def fetch_stats():
    """Current /__stats payload, or None when the proxy is not answering."""
    try:
        with urllib.request.urlopen(stats_url(), timeout=0.5) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def short_tokens(n):
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def open_stats_overlay(ic=None, item=None):
    _stats_win["want"] = True
    if _stats_win["open"]:
        return
    _stats_win["open"] = True

    def _run():
        import tkinter as tk

        BG, DIM, FG, GOOD, BAD = "#0e1116", "#7d8695", "#e6edf3", "#22c55e", "#ef4444"

        root = tk.Tk()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-alpha", 0.93)
        root.configure(bg=BG)
        # Not winfo_screenwidth()-272: on a 125% display Tk reports the full
        # 1536 px while the drawable coordinate space is only ~1229, so a
        # right-edge placement lands off screen. A fraction is always visible
        # and the card can be dragged anywhere.
        root.geometry(f"252x164+{int(root.winfo_screenwidth() * 0.62)}+48")

        def drag_start(e):
            root._drag = (e.x_root - root.winfo_x(), e.y_root - root.winfo_y())

        def drag_move(e):
            dx, dy = getattr(root, "_drag", (0, 0))
            root.geometry(f"+{e.x_root - dx}+{e.y_root - dy}")

        def hide(_e=None):
            _stats_win["want"] = False
            root.withdraw()

        head = tk.Frame(root, bg=BG)
        head.pack(fill="x", padx=14, pady=(10, 0))
        dot = tk.Label(head, text="\u25cf", bg=BG, fg=BAD, font=("Segoe UI", 9))
        dot.pack(side="left")
        tk.Label(head, text=" GO PROXY", bg=BG, fg=DIM,
                 font=("Segoe UI", 8, "bold")).pack(side="left")
        close = tk.Label(head, text="\u2715", bg=BG, fg=DIM, font=("Segoe UI", 9), cursor="hand2")
        close.pack(side="right")
        close.bind("<Button-1>", hide)

        big = tk.Label(root, text="\u2014", bg=BG, fg=FG, font=("Segoe UI", 26, "bold"))
        big.pack(pady=(2, 0))
        tk.Label(root, text="cache hit", bg=BG, fg=DIM, font=("Segoe UI", 8)).pack()

        body = tk.Label(root, text="waiting...", bg=BG, fg=DIM,
                        font=("Consolas", 9), justify="left")
        body.pack(pady=(6, 0))

        for w in (root, head, dot, big, body):
            w.bind("<Button-1>", drag_start)
            w.bind("<B1-Motion>", drag_move)
            w.bind("<Double-Button-1>", hide)
        root.bind("<Escape>", hide)

        def tick():
            if not _stats_win["open"]:
                return
            if _stats_win["want"]:
                if not root.winfo_viewable():
                    root.deiconify()
            elif root.winfo_viewable():
                root.withdraw()

            s = fetch_stats()
            if s is None:
                dot.config(fg=BAD)
                big.config(text="\u2014", fg=DIM)
                body.config(text=f"proxy not answering\n127.0.0.1:{RUNTIME['port'] or PORT}", fg=DIM)
            else:
                dot.config(fg=GOOD)
                big.config(text=f"{s.get('hit_pct', 0)}%", fg=FG)
                body.config(
                    text=(
                        f"saved    ${s.get('est_saved_usd', 0):.4f}\n"
                        f"cached   {short_tokens(s.get('tokens_saved', 0))} tok\n"
                        f"traffic  {s.get('requests', 0)} req / {s.get('sessions', 0)} ses"
                    ),
                    fg=DIM,
                )
            root.after(800, tick)

        def on_destroy():
            _stats_win["open"] = False
            _stats_win["want"] = False

        root.protocol("WM_DELETE_WINDOW", on_destroy)
        root.after(200, tick)
        root.mainloop()

    threading.Thread(target=_run, daemon=True).start()


def overlay_label(item=None):
    return "Hide stats overlay" if _stats_win["want"] else "Show stats overlay"


def on_toggle_overlay(ic, item):
    if _stats_win["want"]:
        _stats_win["want"] = False
    else:
        open_stats_overlay()
    ic.update_menu()


def copy_base_url(ic=None, item=None):
    """Copy the harness base URL to the clipboard."""
    try:
        subprocess.run("clip", input=base_url().encode("utf-8"), check=True)
    except Exception:
        pass


def on_open_folder(ic=None, item=None):
    os.startfile(BASE_DIR)


# ------------------------------------------------------------- auto-start ---

def autostart_enabled():
    return os.path.exists(STARTUP_LNK)


def set_autostart(enable):
    if not enable:
        try:
            os.remove(STARTUP_LNK)
        except OSError:
            pass
        return

    pyw = sys.executable or ""
    if pyw.lower().endswith("python.exe"):
        cand = pyw[: -len("python.exe")] + "pythonw.exe"
        if os.path.exists(cand):
            pyw = cand

    ps = (
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%s');"
        "$s.TargetPath='%s';"
        "$s.Arguments='\"%s\"';"
        "$s.WorkingDirectory='%s';"
        "$s.WindowStyle=7;"
        "$s.Description='OpenCode Go proxy tray';"
        "$s.Save()"
    ) % (STARTUP_LNK, pyw, TRAY_PY, BASE_DIR)

    encoded = base64.b64encode(ps.encode("utf-16-le")).decode()
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        check=False,
    )


def autostart_label(item=None):
    return ("Auto-start with Windows: ON" if autostart_enabled()
            else "Auto-start with Windows: OFF")


def on_toggle_autostart(ic=None, item=None):
    set_autostart(not autostart_enabled())
    if ic:
        ic.update_menu()


# ---------------------------------------------------------------- menu items ---

def status_line(item=None):
    if not ctl.enabled:
        state = "STOPPED"
    elif ctl.alive():
        state = "RUNNING"
    else:
        state = "STARTING..."
    where = "console" if ctl.mode == "console" else "tray"
    port = RUNTIME["port"] or PORT
    return f"\u25cf Go proxy: {state} ({where})  :{port}"


def url_line(item=None):
    return f"   {base_url()}"


def mode_label(item=None):
    return "Run hidden (no console)" if ctl.mode == "console" else "Run in console window"


def on_toggle_mode(ic, item):
    ctl.set_mode("hidden" if ctl.mode == "console" else "console")
    ic.update_menu()


def enabled_label(item=None):
    return "Stop proxy" if ctl.enabled else "Start proxy"


def on_toggle_enabled(ic, item):
    ctl.set_enabled(not ctl.enabled)
    ic.update_menu()


def on_restart(ic, item):
    ctl.stop()
    time.sleep(0.5)
    if ctl.enabled:
        ctl.start()
    ic.update_menu()


def on_quit(ic, item):
    ctl.wanted = False
    ctl.set_enabled(False)
    ic.stop()


def port_watcher():
    """Pick up the port the proxy actually bound and refresh the menu."""
    while ctl.wanted:
        if ctl.alive() and RUNTIME["port"] is None:
            found = read_runtime_port()
            if found:
                RUNTIME["port"] = found
                if icon:
                    try:
                        icon.update_menu()
                    except Exception:
                        pass
        time.sleep(0.5)


def watchdog():
    while ctl.wanted:
        if ctl.enabled and not ctl.alive():
            # Back off when a start keeps dying immediately (port in use, node
            # missing, bad config) instead of hammering every 3 seconds.
            lived = time.time() - ctl.started_at if ctl.started_at else 0
            ctl.fail_streak = ctl.fail_streak + 1 if lived < 5 else 0
            ctl.start()
            time.sleep(min(3 * ctl.fail_streak, 30) if ctl.fail_streak else 3)
            if icon:
                try:
                    icon.update_menu()
                except Exception:
                    pass
            continue
        if ctl.alive() and time.time() - ctl.started_at > 10:
            ctl.fail_streak = 0
        time.sleep(3)


def main():
    ctl.start()
    threading.Thread(target=port_watcher, daemon=True).start()
    threading.Thread(target=watchdog, daemon=True).start()

    proxy_menu = pystray.Menu(
        pystray.MenuItem("Live log...", open_log_window),
        pystray.MenuItem(mode_label, on_toggle_mode),
        pystray.MenuItem(enabled_label, on_toggle_enabled),
        pystray.MenuItem("Restart proxy", on_restart),
    )

    main_menu = pystray.Menu(
        pystray.MenuItem(status_line, lambda i, it: None, enabled=False),
        pystray.MenuItem(url_line, lambda i, it: None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Copy base URL", copy_base_url),
        pystray.MenuItem(overlay_label, on_toggle_overlay),
        pystray.MenuItem(autostart_label, on_toggle_autostart),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Proxy control", proxy_menu),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Open proxy folder", on_open_folder),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", on_quit),
    )

    global icon
    icon = pystray.Icon("opencode-go-proxy", make_icon(),
                        f"OpenCode Go Proxy :{PORT}", main_menu)
    icon.run()


if __name__ == "__main__":
    main()
