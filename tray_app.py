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
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROXY_JS = os.path.join(BASE_DIR, "go-session-proxy.mjs")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
# Local overrides, git-ignored: the only place the tray writes to. Holds the
# optional usage key, so it never lands in the tracked config.json.
CONFIG_LOCAL_PATH = os.path.join(BASE_DIR, "config.local.json")
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


def load_local_config():
    try:
        with open(CONFIG_LOCAL_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_env_file():
    """KEY=VALUE pairs from a .env beside the proxy, or one directory up.

    Only fills variables that are not already set, so a real environment always
    wins. Used for OPENCODE_GO_USAGE_KEY, which lives outside the repo and is
    handed to the child process through os.environ.
    """
    parent = os.path.dirname(BASE_DIR)
    for path in (os.path.join(BASE_DIR, ".env"), os.path.join(parent, ".env")):
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    if k and k not in os.environ:
                        os.environ[k] = v
        except Exception:
            pass
        return


load_env_file()


def usage_key():
    """The key the proxy will receive: the tray's local override first, then
    the environment (which includes any .env loaded above)."""
    local = str(load_local_config().get("usage_key") or "").strip()
    return local or os.environ.get("OPENCODE_GO_USAGE_KEY", "").strip()


# Opacity of the stats card. 1.0 is the default because anything below it lets
# bright text from the window behind show through the card (measured at 0.93,
# 0.97 and 0.99). Kept adjustable anyway - it is the user's screen.
def clamp_alpha(v):
    try:
        v = float(v)
    except Exception:
        return 1.0
    return min(1.0, max(0.3, v))


OVERLAY_ALPHA = clamp_alpha(load_local_config().get("overlay_alpha", 1.0))


def set_overlay_alpha(value):
    global OVERLAY_ALPHA
    OVERLAY_ALPHA = clamp_alpha(value)
    cfg = load_local_config()
    cfg["overlay_alpha"] = OVERLAY_ALPHA
    try:
        with open(CONFIG_LOCAL_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


def monitor_rects():
    """(x, y, w, h) for every attached monitor, or [] when unknown.

    Win32 in a DPI-unaware process reports the same coordinates Tk's geometry
    uses, which is the space a saved card position lives in.
    """
    if sys.platform != "win32":
        return []
    try:
        import ctypes
        from ctypes import wintypes

        found = []
        proc = ctypes.WINFUNCTYPE(
            ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.POINTER(wintypes.RECT), ctypes.c_double,
        )

        def each(_hmon, _hdc, lprect, _lparam):
            r = lprect.contents
            found.append((r.left, r.top, r.right - r.left, r.bottom - r.top))
            return 1

        ctypes.windll.user32.EnumDisplayMonitors(None, None, proc(each), 0)
        return found
    except Exception:
        return []


def pos_is_visible(x, y, rects=None):
    """True when the card's grab strip would land on an attached monitor.

    A position saved on a second screen is fine until that screen is
    unplugged: the card would still exist but nothing would draw it. In that
    case the caller falls back to the default corner. Without a monitor list
    (no Win32) the saved position is trusted.
    """
    rl = monitor_rects() if rects is None else rects
    if not rl:
        return True
    grab_x, grab_y = x + 60, y + 15  # the header is what you only need to reach
    for mx, my, mw, mh in rl:
        if mx <= grab_x <= mx + mw and my <= grab_y <= my + mh:
            return True
    return False


def save_overlay_pos(x, y):
    """Remember where the card was dropped, so it opens there next time."""
    try:
        cfg = load_local_config()
        cfg["overlay_pos"] = [int(x), int(y)]
        with open(CONFIG_LOCAL_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


def save_usage_key(key):
    cfg = load_local_config()
    if key:
        cfg["usage_key"] = key
    else:
        cfg.pop("usage_key", None)
    with open(CONFIG_LOCAL_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)

CFG = load_config()
PORT = int(CFG.get("port", 17387))
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
        key = usage_key()
        if key:
            env["OPENCODE_GO_USAGE_KEY"] = key
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


def reset_stats():
    """Zero the counters this proxy collected (POST /__stats/reset)."""
    url = f"http://127.0.0.1:{RUNTIME['port'] or PORT}/__stats/reset"
    try:
        req = urllib.request.Request(url, method="POST", data=b"")
        with urllib.request.urlopen(req, timeout=3) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def short_tokens(n):
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def resets_in(iso, now=None):
    """'Resets in 2h 53m' from an ISO timestamp, or '' when it is unusable."""
    if not iso:
        return ""
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        secs = int((t - (now or datetime.now(timezone.utc))).total_seconds())
    except Exception:
        return ""
    if secs <= 0:
        return "resetting"
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days:
        return f"Resets in {days}d {hours}h"
    if hours:
        return f"Resets in {hours}h {mins}m"
    return f"Resets in {mins}m"


def open_stats_overlay(ic=None, item=None):
    _stats_win["want"] = True
    if _stats_win["open"]:
        return
    _stats_win["open"] = True

    def _run():
        import tkinter as tk

        BG, DIM, FG, GOOD, BAD = "#0e1116", "#7d8695", "#e6edf3", "#22c55e", "#ef4444"
        TRACK, BADGE = "#2a313c", "#12261a"

        root = tk.Tk()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        # Opaque on purpose: 0.93, 0.97 and even 0.99 all let bright text from
        # the window behind show through the card, which made the numbers hard
        # to read. The hairline border is what keeps it looking like a floating
        # card rather than a hole in the screen.
        root.attributes("-alpha", OVERLAY_ALPHA)
        root.configure(bg=BG, highlightthickness=1, highlightbackground=TRACK)
        # Where the card opens: the position it was last dragged to, or a safe
        # default. Not winfo_screenwidth()-width: on a 125% display Tk reports
        # the full 1536 px while the drawable coordinate space is only ~1229,
        # so a right-edge placement lands off screen. 0.58 keeps a 300 px card
        # inside that 1229.
        sw = root.winfo_screenwidth()
        saved = load_local_config().get("overlay_pos")
        if (
            isinstance(saved, list)
            and len(saved) == 2
            and all(isinstance(v, (int, float)) for v in saved)
            and pos_is_visible(int(saved[0]), int(saved[1]))
        ):
            at = f"+{int(saved[0])}+{int(saved[1])}"
        else:
            at = f"+{int(sw * 0.58)}+48"
        root.geometry(f"300x302{at}")

        def drag_start(e):
            root._drag = (e.x_root - root.winfo_x(), e.y_root - root.winfo_y())

        def drag_move(e):
            dx, dy = getattr(root, "_drag", (0, 0))
            root.geometry(f"+{e.x_root - dx}+{e.y_root - dy}")

        def drag_end(_e=None):
            save_overlay_pos(root.winfo_x(), root.winfo_y())

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
        gear = tk.Label(head, text="\u2699", bg=BG, fg=DIM,
                        font=("Segoe UI", 10), cursor="hand2")
        gear.pack(side="right", padx=(0, 10))
        gear.bind("<Button-1>", lambda e: open_settings_dialog())

        big = tk.Label(root, text="\u2014", bg=BG, fg=FG, font=("Segoe UI", 26, "bold"))
        big.pack(pady=(2, 0))
        tk.Label(root, text="cache hit", bg=BG, fg=DIM, font=("Segoe UI", 8)).pack()

        body = tk.Label(root, text="waiting...", bg=BG, fg=DIM,
                        font=("Consolas", 9), justify="left")
        body.pack(pady=(6, 0))

        # Plan meters, straight from GET {base}/usage: label, "% used" badge,
        # reset countdown and a bar. Drawn once and updated in place, because
        # rebuilding widgets every 800 ms flickers and leaks.
        tk.Frame(root, bg=TRACK, height=1).pack(fill="x", padx=14, pady=(8, 9))

        plan = tk.Frame(root, bg=BG)
        plan.pack(fill="x", padx=14)

        rows = {}
        for key, title in (("rolling", "5-hour window"), ("weekly", "Weekly"),
                           ("monthly", "Monthly")):
            row = tk.Frame(plan, bg=BG)
            row.pack(fill="x", pady=(0, 9))
            top = tk.Frame(row, bg=BG)
            top.pack(fill="x")
            tk.Label(top, text=title, bg=BG, fg=FG,
                     font=("Segoe UI", 8, "bold")).pack(side="left")
            badge = tk.Label(top, text="\u2014", bg=BADGE, fg=GOOD,
                             font=("Segoe UI", 7, "bold"), padx=5)
            badge.pack(side="left", padx=(6, 0))
            resets = tk.Label(top, text="", bg=BG, fg=DIM, font=("Segoe UI", 7))
            resets.pack(side="right")
            track = tk.Frame(row, bg=TRACK, height=4)
            track.pack(fill="x", pady=(4, 0))
            track.pack_propagate(False)
            fill = tk.Frame(track, bg=GOOD)
            fill.place(x=0, y=0, relheight=1, relwidth=0)
            rows[key] = (badge, resets, fill)

        hint = tk.Label(root, text="", bg=BG, fg=DIM, font=("Segoe UI", 7))
        hint.pack(pady=(0, 8))

        for w in (root, head, dot, big, body):
            w.bind("<Button-1>", drag_start)
            w.bind("<B1-Motion>", drag_move)
            w.bind("<Double-Button-1>", hide)
        root.bind("<ButtonRelease-1>", drag_end)
        root.bind("<Escape>", hide)

        def fill_plan(acct):
            windows = (acct or {}).get("windows") or {}
            for key, (badge, resets, fill) in rows.items():
                w = windows.get(key) or {}
                pct = w.get("percent_used")
                if pct is None:
                    badge.config(text="\u2014", fg=DIM)
                    resets.config(text="")
                    fill.place_configure(relwidth=0)
                    continue
                pct = max(0.0, min(100.0, float(pct)))
                limited = (w.get("status") or "ok") != "ok"
                tint = BAD if limited else GOOD
                badge.config(text=f"{pct:.0f}% used", fg=tint)
                resets.config(text=resets_in(w.get("resets_at")), fg=BAD if limited else DIM)
                fill.config(bg=tint)
                fill.place_configure(relwidth=pct / 100)

        applied = {"alpha": OVERLAY_ALPHA}

        def tick():
            if not _stats_win["open"]:
                return
            if _stats_win["want"]:
                if not root.winfo_viewable():
                    root.deiconify()
            elif root.winfo_viewable():
                root.withdraw()

            # Opacity picked from the tray menu, applied without a restart.
            if applied["alpha"] != OVERLAY_ALPHA:
                root.attributes("-alpha", OVERLAY_ALPHA)
                applied["alpha"] = OVERLAY_ALPHA

            s = fetch_stats()
            if s is None:
                dot.config(fg=BAD)
                big.config(text="\u2014", fg=DIM)
                body.config(text=f"proxy not answering\n127.0.0.1:{RUNTIME['port'] or PORT}", fg=DIM)
                fill_plan(None)
                hint.config(text="")
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
                acct = s.get("account")
                fill_plan(acct)
                if acct is None:
                    hint.config(text="no usage key - add one from the tray menu")
                elif acct.get("error"):
                    hint.config(text="plan meters unavailable")
                elif s.get("responses_without_usage"):
                    hint.config(
                        text=f"{s['responses_without_usage']} responses had no usage - totals may be low"
                    )
                else:
                    hint.config(text="")
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


def open_settings_dialog(ic=None, item=None):
    """The only settings surface: the read-only usage key and the card's
    opacity. Reachable from the gear on the card and from the tray menu, so it
    still works with the card hidden. Writes config.local.json (git-ignored)
    and restarts the proxy so the child process picks up a new key."""
    def _run():
        import tkinter as tk

        BG, DIM, FG, FIELD = "#0e1116", "#7d8695", "#e6edf3", "#161b22"
        GOOD, BAD = "#22c55e", "#ef4444"
        root = tk.Tk()
        root.title("Overlay settings")
        root.configure(bg=BG)
        root.attributes("-topmost", True)
        root.geometry("540x348")
        root.resizable(False, False)

        def text(s, size=9, color=DIM, bold=False, pady=(0, 0)):
            tk.Label(root, text=s, bg=BG, fg=color, justify="left", wraplength=504,
                     anchor="w", font=("Segoe UI", size, "bold" if bold else "normal")
                     ).pack(fill="x", padx=18, pady=pady)

        text("Usage API key", 11, FG, True, (16, 2))
        text("Create it at opencode.ai/console -> Account -> Personal API keys.\n"
             "Permissions: Custom, then Usage: Read. Leave Models on None, so the key\n"
             "cannot call a model - it only has to read the three windows.",
             8, DIM, False, (0, 8))

        # Masked: the read-only key is still a credential, and this window is
        # the kind of thing that ends up in a screen recording.
        entry = tk.Entry(root, font=("Consolas", 10), bg=FIELD, fg=FG, show="\u2022",
                         insertbackground=FG, relief="flat", highlightthickness=1,
                         highlightbackground="#2a313c", highlightcolor="#3b82f6")
        entry.pack(fill="x", padx=18, ipady=6)
        entry.insert(0, usage_key())

        text(f"stored in {os.path.basename(CONFIG_LOCAL_PATH)} (git-ignored); the "
             f"environment and .env are the fallback. Empty it to remove the key.",
             8, DIM, False, (6, 16))

        text("Card opacity", 11, FG, True, (0, 6))
        # A slider, not radios: on a dark background the native radio indicator
        # is too low-contrast to tell which value is selected.
        row = tk.Frame(root, bg=BG)
        row.pack(fill="x", padx=18)
        alpha = tk.IntVar(value=int(round(OVERLAY_ALPHA * 100)))
        readout = tk.Label(row, text=f"{alpha.get()}%", bg=BG, fg=FG, width=5,
                           anchor="e", font=("Consolas", 9))
        readout.pack(side="right")
        # Hand-drawn slider: tk.Scale's knob is barely visible on a dark theme,
        # and this matches the bars the card already draws.
        SLIDER_W = 370

        def draw():
            pct = alpha.get()
            x = SLIDER_W * pct / 100
            canvas.delete("all")
            canvas.create_rectangle(0, 5, SLIDER_W, 11, fill=FIELD, outline="")
            canvas.create_rectangle(0, 5, x, 11, fill=GOOD, outline="")
            canvas.create_oval(x - 5, 2, x + 5, 14, fill=FG, outline=BG)

        def drag(e):
            pct = max(30, min(100, round(e.x / SLIDER_W * 100)))
            if pct != alpha.get():
                alpha.set(pct)
                readout.config(text=f"{pct}%")
                draw()
                set_overlay_alpha(pct / 100)  # live preview on the card

        canvas = tk.Canvas(row, width=SLIDER_W, height=16, bg=BG, bd=0,
                           highlightthickness=0, cursor="hand2")
        canvas.pack(side="left", padx=(0, 10))
        canvas.bind("<Button-1>", drag)
        canvas.bind("<B1-Motion>", drag)
        draw()
        text("100% keeps the numbers readable. Lower values let the window behind "
             "show through the card.", 8, DIM, False, (6, 0))

        def apply():
            save_usage_key(entry.get().strip())
            set_overlay_alpha(alpha.get() / 100)
            root.destroy()
            ctl.stop()
            if ctl.enabled and ctl.wanted:
                ctl.start()
            if ic:
                try:
                    ic.update_menu()
                except Exception:
                    pass

        bar = tk.Frame(root, bg=BG)
        bar.pack(fill="x", padx=18, pady=16, side="bottom")

        status = tk.Label(bar, text="", bg=BG, fg=GOOD, font=("Segoe UI", 8))
        status.pack(side="left")

        def do_reset():
            ok = reset_stats()
            status.config(text="stats reset" if ok else "proxy not answering",
                          fg=GOOD if ok else BAD)
            root.after(3000, lambda: status.config(text=""))

        for label, cmd in (("Cancel", root.destroy),
                           ("Reset stats", do_reset),
                           ("Save & restart proxy", apply)):
            tk.Button(bar, text=label, command=cmd, font=("Segoe UI", 9),
                      bg=FIELD, fg=FG, activebackground="#2a313c", activeforeground=FG,
                      relief="flat", bd=0, padx=12, pady=5, cursor="hand2"
                      ).pack(side="right", padx=(8, 0))

        entry.focus_set()
        entry.select_range(0, "end")
        root.bind("<Escape>", lambda e: root.destroy())
        root.mainloop()

    threading.Thread(target=_run, daemon=True).start()


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
        pystray.MenuItem("Settings...", open_settings_dialog),
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
