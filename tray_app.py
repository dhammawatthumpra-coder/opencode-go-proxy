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


def dark_title_bar(root):
    """Ask DWM to draw this window's title bar dark.

    Tk has no say in the caption colour; Windows 11 exposes it through
    DwmSetWindowAttribute. Attribute 20 is the current one, 19 the
    pre-2004 id. Failures are ignored - a light title bar is only cosmetic.
    """
    if sys.platform != "win32":
        return

    def apply():
        try:
            import ctypes

            hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
            on = ctypes.c_int(1)
            for attr in (20, 19):
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(on), ctypes.sizeof(on)
                ) == 0:
                    break
        except Exception:
            pass

    # Deferred and repeated: set before the window is mapped, Windows drops it,
    # and a slow first paint can land after the first attempt.
    try:
        for delay in (100, 400, 1200):
            root.after(delay, apply)
    except Exception:
        pass


def watch_alpha(root):
    """Keep a window's alpha in step with the one shared setting.

    Every window follows OVERLAY_ALPHA, so moving the slider updates the card,
    the settings window and the history window at once."""
    state = {"v": None}

    def tick():
        try:
            if state["v"] != OVERLAY_ALPHA:
                root.attributes("-alpha", OVERLAY_ALPHA)
                state["v"] = OVERLAY_ALPHA
            root.after(400, tick)
        except Exception:
            pass  # window closed

    tick()


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


def save_pos(name, x, y):
    """Remember a window position in config.local.json, so it reopens there."""
    try:
        cfg = load_local_config()
        cfg[name] = [int(x), int(y)]
        with open(CONFIG_LOCAL_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


def saved_pos(name):
    """' WxH+x+y' suffix for the saved position, or '' when there is none.

    Positions are checked against the monitor list: a spot on a screen that
    has since been unplugged is dropped rather than opening off-desktop.
    """
    saved = load_local_config().get(name)
    if (
        isinstance(saved, list)
        and len(saved) == 2
        and all(isinstance(v, (int, float)) for v in saved)
        and pos_is_visible(int(saved[0]), int(saved[1]))
    ):
        return f"+{int(saved[0])}+{int(saved[1])}"
    return ""


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

# One Tk interpreter for every window, living in its own thread.
#
# The first version gave each window its own tk.Tk() in its own thread. Tcl
# tolerates that until the interpreters are torn down, and then aborts the
# whole process ("Tcl_AsyncDelete: async handler deleted by the wrong thread")
# when one of them is collected - which is why opening Settings and History,
# closing both, then clicking the gear again killed the tray. Everything is a
# Toplevel under this single root now, so every Tk call happens on one thread.
_UI = {"root": None, "ready": False}


def _ui_loop():
    import tkinter as tk

    root = tk.Tk()
    root.withdraw()  # hidden master; only the Toplevels are visible
    _UI["root"] = root
    _UI["ready"] = True
    root.mainloop()


def ui_post(build):
    """Run `build` on the Tk thread, starting that thread the first time."""
    if _UI["root"] is None:
        threading.Thread(target=_ui_loop, daemon=True).start()
        for _ in range(500):
            if _UI["ready"]:
                break
            time.sleep(0.01)
    root = _UI["root"]
    if root is None:
        return
    root.after(0, build)


# ------------------------------------------------------------ windows & actions ---

_live_win = {"open": False}


def open_log_window(ic=None, item=None):
    """Flow: what talks to what, with light running along the wires.

    It replaced a plain tail window, which looked empty until a request
    happened to arrive - nothing is written to the log while the proxy sits
    idle. The tail of the log stays underneath for when something looks off.
    """
    if _live_win["open"]:
        return
    _live_win["open"] = True

    def build():
        import random
        import tkinter as tk

        BG, DIM, FG, GOOD, BAD = "#0a0d12", "#7d8695", "#e6edf3", "#22c55e", "#ef4444"
        NODE, EDGE = "#111820", "#27313d"
        W, H = 780, 420

        root = tk.Toplevel(_UI["root"])
        root.title("Flow")
        root.configure(bg=BG)
        root.attributes("-topmost", True)
        root.attributes("-alpha", OVERLAY_ALPHA)
        root.geometry(
            f"{W + 32}x640"
            + (saved_pos("flow_pos") or f"+{int(root.winfo_screenwidth() * 0.22)}+30")
        )
        root.resizable(False, False)
        watch_alpha(root)
        dark_title_bar(root)

        c = tk.Canvas(root, width=W, height=H, bg=BG, bd=0, highlightthickness=0)
        c.pack(padx=16, pady=(12, 6))

        # faint stars, so the dark background has some depth
        rnd = random.Random(7)
        for _ in range(110):
            x, y = rnd.randrange(W), rnd.randrange(H)
            c.create_oval(x, y, x + 2, y + 2, outline="",
                          fill=rnd.choice(("#151d26", "#1a2430", "#101720")))

        def box(x, y, w, h, title):
            c.create_rectangle(x, y, x + w, y + h, fill=NODE, outline=EDGE)
            c.create_line(x + 1, y + 1, x + w - 1, y + 1, fill="#1d2733")
            c.create_text(x + 14, y + 20, text=title, fill=DIM, anchor="w",
                          font=("Segoe UI", 8, "bold"))
            return (c.create_text(x + 14, y + 44, text="", fill=FG, anchor="w",
                                  font=("Consolas", 10)),
                    c.create_text(x + 14, y + 66, text="", fill=DIM, anchor="w",
                                  font=("Segoe UI", 8)))

        MAIN = [(198, 152), (298, 152)]
        MAIN2 = [(472, 152), (572, 152)]
        BR_A = [(388, 212), (388, 250), (318, 250), (318, 288)]
        BR_B = [(388, 212), (388, 250), (454, 250), (454, 288)]

        def wire(pts, outer, inner):
            xy = [v for p in pts for v in p]
            c.create_line(*xy, fill=outer, width=9, capstyle="round", joinstyle="round")
            c.create_line(*xy, fill=inner, width=3, capstyle="round", joinstyle="round")

        for pts in (MAIN, MAIN2):
            wire(pts, "#0b1f15", "#17402a")
        for pts in (BR_A, BR_B):
            wire(pts, "#0c1622", "#1a3a52")

        harness = box(30, 112, 168, 84, "HARNESS")
        proxy = box(298, 98, 174, 114, "PROXY")
        upstream = box(572, 112, 178, 84, "OPENCODE")
        sid = box(238, 288, 160, 76, "SESSION ID")
        cache = box(406, 288, 160, 76, "PROMPT CACHE")

        c.create_text(248, 138, text="HTTP", fill=DIM, font=("Segoe UI", 7))
        c.create_text(522, 138, text="HTTPS", fill=DIM, font=("Segoe UI", 7))

        def point_at(pts, t):
            segs, total = [], 0.0
            for i in range(len(pts) - 1):
                (x0, y0), (x1, y1) = pts[i], pts[i + 1]
                d = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
                segs.append((d, x0, y0, x1, y1))
                total += d
            left = (t % 1.0) * total
            for d, x0, y0, x1, y1 in segs:
                if left <= d:
                    f = left / d if d else 0
                    return x0 + (x1 - x0) * f, y0 + (y1 - y0) * f
                left -= d
            return pts[-1]

        PULSES = []

        def add_pulses(pts, count, speed, head, tail):
            for k in range(count):
                PULSES.append({
                    "pts": pts, "t": k / count, "v": speed,
                    "head": c.create_oval(0, 0, 0, 0, fill=head, outline=""),
                    "tail": c.create_oval(0, 0, 0, 0, fill=tail, outline=""),
                })

        add_pulses(MAIN, 3, 0.011, "#8bffc4", "#1f7a4d")
        add_pulses(MAIN2, 3, 0.010, "#8bffc4", "#1f7a4d")
        add_pulses(BR_A, 2, 0.008, "#7fd4ff", "#1d5875")
        add_pulses(BR_B, 2, 0.008, "#7fd4ff", "#1d5875")

        # The log keeps running underneath, one line at a time, colour-coded:
        # it is the part that tells you what actually moved.
        tail = tk.Text(root, height=11, bg="#080b0f", fg="#8b949e", bd=0,
                       font=("Consolas", 8), wrap="none", state="disabled",
                       highlightthickness=0)
        tail.pack(fill="both", expand=True, padx=16, pady=(0, 14))
        tail.tag_config("req", foreground="#7d8695")
        tail.tag_config("usage", foreground="#22c55e")
        tail.tag_config("stats", foreground="#7fd4ff")
        tail.tag_config("warn", foreground="#ef4444")

        def tag_for(line):
            if line.startswith("usage"):
                return "usage"
            if line.startswith("  stats"):
                return "stats"
            if "\u26a0" in line or "unavailable" in line:
                return "warn"
            return "req"

        def append(lines, replace=False):
            if not lines:
                return
            tail.config(state="normal")
            if replace:
                tail.delete("1.0", "end")
            for ln in lines:
                tail.insert("end", ln + "\n", tag_for(ln))
            if int(tail.index("end-1c").split(".")[0]) > 400:
                tail.delete("1.0", "200.0")
            tail.see("end")
            tail.config(state="disabled")

        pos = {"v": os.path.getsize(LOG_PATH) if os.path.exists(LOG_PATH) else 0}
        try:
            if os.path.exists(LOG_PATH):
                with open(LOG_PATH, "rb") as f:
                    f.seek(max(0, pos["v"] - 8192))
                    seed = f.read().decode("utf-8", "replace").splitlines()
                append([ln for ln in seed if ln.strip()][-12:], replace=True)
        except Exception:
            pass

        def poll_log():
            if not root.winfo_exists():
                return
            try:
                if os.path.exists(LOG_PATH):
                    size = os.path.getsize(LOG_PATH)
                    if size < pos["v"]:
                        pos["v"] = 0
                    if size > pos["v"]:
                        with open(LOG_PATH, "rb") as f:
                            f.seek(pos["v"])
                            data = f.read().decode("utf-8", "replace")
                        pos["v"] = size
                        append([ln for ln in data.splitlines() if ln.strip()])
            except Exception:
                pass
            root.after(400, poll_log)

        state = {"req": None, "flash": 0}

        def animate():
            if not root.winfo_exists():
                return
            for p in PULSES:
                p["t"] = (p["t"] + p["v"]) % 1.0
                hx, hy = point_at(p["pts"], p["t"])
                tx, ty = point_at(p["pts"], p["t"] - 0.045)
                c.coords(p["head"], hx - 3, hy - 3, hx + 3, hy + 3)
                c.coords(p["tail"], tx - 2, ty - 2, tx + 2, ty + 2)
            if state["flash"]:
                state["flash"] -= 1
                c.itemconfig(proxy[0], fill="#8bffc4" if state["flash"] % 2 else FG)
            root.after(45, animate)

        def tick():
            if not root.winfo_exists():
                return

            s = fetch_stats()
            if s is None:
                c.itemconfig(harness[0], text="waiting", fill=DIM)
                c.itemconfig(harness[1], text="proxy not answering", fill=BAD)
                for pair in (proxy, upstream, sid, cache):
                    c.itemconfig(pair[0], text="\u2014", fill=DIM)
                    c.itemconfig(pair[1], text="", fill=DIM)
            else:
                c.itemconfig(harness[0], text=base_url().replace("http://", ""), fill=FG)
                c.itemconfig(harness[1], text="your agent", fill=DIM)

                c.itemconfig(proxy[0],
                             text=f"{s.get('requests', 0)} req \u00b7 {s.get('hit_pct', 0)}% hit",
                             fill=FG)
                c.itemconfig(proxy[1],
                             text=f"{s.get('sessions', 0)} session \u00b7 "
                                  f"{short_tokens(s.get('input_tokens', 0))} tok in",
                             fill=DIM)

                acct = (s.get("account") or {}).get("windows") or {}

                def used(k):
                    v = (acct.get(k) or {}).get("percent_used")
                    return f"{v:.0f}%" if isinstance(v, (int, float)) else "\u2014"

                c.itemconfig(upstream[0],
                             text=UPSTREAM.replace("https://", ""), fill=FG)
                c.itemconfig(upstream[1],
                             text=f"plan {used('rolling')} \u00b7 {used('weekly')} \u00b7 {used('monthly')}",
                             fill=DIM)

                c.itemconfig(sid[0], text="stable per chat", fill=FG)
                c.itemconfig(sid[1], text="same x-opencode-session", fill=DIM)
                c.itemconfig(cache[0], text=f"{s.get('hit_pct', 0)}% hit", fill=GOOD)
                c.itemconfig(cache[1],
                             text=f"{short_tokens(s.get('tokens_saved', 0))} tok saved",
                             fill=DIM)

                req = s.get("requests", 0)
                if state["req"] is not None and req > state["req"]:
                    state["flash"] = 6
                state["req"] = req

            root.after(700, tick)

        def close(_e=None):
            _live_win["open"] = False
            root.destroy()

        root.protocol("WM_DELETE_WINDOW", close)
        root.bind("<Escape>", close)
        animate()
        tick()
        poll_log()

    ui_post(build)


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

        root = tk.Toplevel(_UI["root"])
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
        at = saved_pos("overlay_pos") or f"+{int(sw * 0.58)}+48"
        # 318, not 302: the hint line under the plan rows ("no usage key",
        # "N responses had no usage") needs the room or it gets clipped.
        root.geometry(f"300x318{at}")

        def drag_start(e):
            root._drag = (e.x_root - root.winfo_x(), e.y_root - root.winfo_y())

        def drag_move(e):
            dx, dy = getattr(root, "_drag", (0, 0))
            root.geometry(f"+{e.x_root - dx}+{e.y_root - dy}")

        def drag_end(_e=None):
            save_pos("overlay_pos", root.winfo_x(), root.winfo_y())

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

        watch_alpha(root)

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

    ui_post(_run)


def overlay_label(item=None):
    return "Hide stats overlay" if _stats_win["want"] else "Show stats overlay"


def on_toggle_overlay(ic, item):
    if _stats_win["want"]:
        _stats_win["want"] = False
    else:
        open_stats_overlay()
    ic.update_menu()


def open_settings_dialog(ic=None, item=None):
    """One window for everything reachable from the card: the read-only usage
    key, the card's opacity, and the proxy actions that also live in the tray
    menu — which stays, for when the card is hidden or the tray is all there
    is. Writes config.local.json (git-ignored)."""
    def _run():
        import tkinter as tk

        BG, DIM, FG, FIELD = "#0e1116", "#7d8695", "#e6edf3", "#161b22"
        GOOD, BAD = "#22c55e", "#ef4444"
        root = tk.Toplevel(_UI["root"])
        root.title("Overlay settings")
        root.configure(bg=BG)
        dark_title_bar(root)
        root.attributes("-topmost", True)
        root.attributes("-alpha", OVERLAY_ALPHA)
        root.geometry("560x452" + (saved_pos("settings_pos") or f"+{int(root.winfo_screenwidth() * 0.40)}+110"))
        root.resizable(False, False)
        watch_alpha(root)

        # Same memory as the card: the window opens where it was last closed.
        # Debounced because <Configure> fires for every pixel of a drag.
        pending = {"id": None}

        def remember(_e=None):
            if pending["id"]:
                root.after_cancel(pending["id"])
            pending["id"] = root.after(
                500, lambda: save_pos("settings_pos", root.winfo_x(), root.winfo_y())
            )

        root.bind("<Configure>", remember)

        def text(s, size=9, color=DIM, bold=False, pady=(0, 0)):
            tk.Label(root, text=s, bg=BG, fg=color, justify="left", wraplength=524,
                     anchor="w", font=("Segoe UI", size, "bold" if bold else "normal")
                     ).pack(fill="x", padx=18, pady=pady)

        def button(parent, label, cmd, width=None):
            b = tk.Button(parent, text=label, command=cmd, font=("Segoe UI", 9),
                          bg=FIELD, fg=FG, activebackground="#2a313c",
                          activeforeground=FG, relief="flat", bd=0, padx=10, pady=4,
                          cursor="hand2", width=width)
            return b

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

        text("Window opacity", 11, FG, True, (0, 6))
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
             "show through - the card, this window and the history window all "
             "share it.", 8, DIM, False, (6, 0))

        # The tray menu stays, but these are the same actions for when the card
        # is what you are looking at.
        text("Proxy", 11, FG, True, (0, 6))
        state = tk.Label(root, text=status_line(), bg=BG, fg=DIM, anchor="w",
                         font=("Consolas", 8))
        state.pack(fill="x", padx=18)

        row1 = tk.Frame(root, bg=BG)
        row1.pack(fill="x", padx=18, pady=(6, 0))
        button(row1, "Copy base URL", copy_base_url).pack(side="left", padx=(0, 8))
        button(row1, "Flow", open_log_window).pack(side="left", padx=(0, 8))
        button(row1, "History", open_history_window).pack(side="left")

        row2 = tk.Frame(root, bg=BG)
        row2.pack(fill="x", padx=18, pady=(6, 0))

        def refresh():
            state.config(text=status_line())
            toggle_btn.config(text="Stop proxy" if ctl.enabled else "Start proxy")
            try:
                if icon:
                    icon.update_menu()
            except Exception:
                pass

        def restart():
            ctl.stop()
            time.sleep(0.2)
            if ctl.enabled and ctl.wanted:
                ctl.start()
            refresh()

        def toggle():
            ctl.set_enabled(not ctl.enabled)
            refresh()

        button(row2, "Open folder", on_open_folder).pack(side="left", padx=(0, 8))
        button(row2, "Restart proxy", restart).pack(side="left", padx=(0, 8))
        toggle_btn = button(row2, "Stop proxy" if ctl.enabled else "Start proxy", toggle)
        toggle_btn.pack(side="left")

        # A tick label rather than tk.Checkbutton: on this dark background the
        # native indicator draws a dark mark in a dark box, so the state cannot
        # be read at a glance.
        auto = {"on": autostart_enabled()}

        def auto_text():
            return ("\u2713 " if auto["on"] else "\u2610 ") + "Start with Windows"

        auto_btn = tk.Label(row2, text=auto_text(), bg=BG,
                            fg=FG if auto["on"] else DIM,
                            font=("Segoe UI", 9), cursor="hand2")
        auto_btn.pack(side="right")

        def toggle_auto(_e=None):
            auto["on"] = not auto["on"]
            set_autostart(auto["on"])
            auto_btn.config(text=auto_text(), fg=FG if auto["on"] else DIM)
            refresh()

        auto_btn.bind("<Button-1>", toggle_auto)

        def apply():
            old = usage_key()
            new = entry.get().strip()
            save_usage_key(new)
            set_overlay_alpha(alpha.get() / 100)
            root.destroy()
            # Only bounce the proxy when the key actually changed; opacity is
            # read live by the card.
            if new != old:
                ctl.stop()
                if ctl.enabled and ctl.wanted:
                    ctl.start()
            try:
                if icon:
                    icon.update_menu()
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
                           ("Save", apply)):
            tk.Button(bar, text=label, command=cmd, font=("Segoe UI", 9),
                      bg=FIELD, fg=FG, activebackground="#2a313c", activeforeground=FG,
                      relief="flat", bd=0, padx=12, pady=5, cursor="hand2"
                      ).pack(side="right", padx=(8, 0))

        entry.focus_set()
        entry.select_range(0, "end")
        root.bind("<Escape>", lambda e: root.destroy())

    ui_post(_run)


# ------------------------------------------------------------ history card ---

_history_win = {"open": False}


def fetch_history():
    url = f"http://127.0.0.1:{RUNTIME['port'] or PORT}/__stats/history"
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def money(v):
    v = float(v or 0)
    return f"${v:.2f}" if v >= 0.1 else f"${v:.4f}"


def open_history_window(ic=None, item=None):
    """Today / 7 / 30 days from the proxy's per-day ledger, plus the last 14
    days as bars.

    Reset stats only zeroes the card's counters; this ledger keeps recording,
    so the history is the view to trust for "how much did I use this week"."""
    if _history_win["open"]:
        return
    _history_win["open"] = True

    def _run():
        import tkinter as tk

        BG, DIM, FG, GOOD, BAD = "#0e1116", "#7d8695", "#e6edf3", "#22c55e", "#ef4444"
        TRACK, FIELD = "#2a313c", "#161b22"
        W = 560

        root = tk.Toplevel(_UI["root"])
        root.title("Usage history")
        root.configure(bg=BG)
        dark_title_bar(root)
        root.attributes("-topmost", True)
        root.attributes("-alpha", OVERLAY_ALPHA)
        root.geometry(
            f"{W}x336"
            + (saved_pos("history_pos") or f"+{int(root.winfo_screenwidth() * 0.40)}+150")
        )
        root.resizable(False, False)
        watch_alpha(root)

        pending = {"id": None}

        def remember(_e=None):
            if pending["id"]:
                root.after_cancel(pending["id"])
            pending["id"] = root.after(
                500, lambda: save_pos("history_pos", root.winfo_x(), root.winfo_y())
            )

        root.bind("<Configure>", remember)

        body = tk.Frame(root, bg=BG)
        body.pack(fill="x", padx=20, pady=(16, 0))

        for c, head in enumerate(("", "today", "7 days", "30 days")):
            tk.Label(body, text=head, bg=BG, fg=DIM, font=("Segoe UI", 8, "bold"),
                     anchor="e" if c else "w"
                     ).grid(row=0, column=c, sticky="ew", padx=(0, 14), pady=(0, 6))
        for c in range(1, 4):
            body.grid_columnconfigure(c, weight=1)

        cells = {}
        for r, name in enumerate(("requests", "tokens saved", "hit rate", "saved"), start=1):
            tk.Label(body, text=name, bg=BG, fg=DIM, font=("Segoe UI", 9), anchor="w"
                     ).grid(row=r, column=0, sticky="w", padx=(0, 18), pady=2)
            for c in range(1, 4):
                cell = tk.Label(body, text="\u2014", bg=BG, fg=FG, width=9,
                                anchor="e", font=("Consolas", 10))
                cell.grid(row=r, column=c, sticky="e", padx=(0, 14), pady=2)
                cells[(r, c)] = cell

        tk.Frame(root, bg=TRACK, height=1).pack(fill="x", padx=20, pady=(14, 8))
        tk.Label(root, text="last 14 days - money saved per day", bg=BG, fg=DIM,
                 anchor="w", font=("Segoe UI", 8)).pack(fill="x", padx=20)

        canvas = tk.Canvas(root, width=W - 40, height=86, bg=BG, bd=0, highlightthickness=0)
        canvas.pack(padx=20, pady=(6, 0))

        def draw_bars(days):
            canvas.delete("all")
            last = days[-14:]
            slot = (W - 40) / 14
            vals = [float(d.get("est_saved_usd", 0)) for d in last]
            top = max(vals) if vals and max(vals) > 0 else 1
            base = 74
            for i, v in enumerate(vals):
                x = i * slot + 3
                h = 3 + (v / top) * 58
                canvas.create_rectangle(x, base - h, x + slot - 6, base,
                                        fill=GOOD if v else FIELD, outline="")
            canvas.create_line(0, base + 1, W - 40, base + 1, fill=TRACK)

        bar = tk.Frame(root, bg=BG)
        bar.pack(fill="x", padx=20, pady=14, side="bottom")
        status = tk.Label(bar, text="", bg=BG, fg=DIM, font=("Segoe UI", 8))
        status.pack(side="left")

        def refresh():
            h = fetch_history()
            if h is None:
                status.config(text="proxy not answering", fg=BAD)
                return
            win = h.get("windows") or {}
            for c, key in ((1, "today"), (2, "week"), (3, "month")):
                d = win.get(key) or {}
                cells[(1, c)].config(text=f"{int(d.get('requests', 0)):,}")
                cells[(2, c)].config(text=short_tokens(d.get("tokens_saved", 0)))
                cells[(3, c)].config(text=f"{d.get('hit_pct', 0)}%")
                cells[(4, c)].config(text=money(d.get("est_saved_usd", 0)))
            days = h.get("days") or []
            draw_bars(days)
            if days:
                status.config(text=f"{len(days)} day(s) recorded - reset never clears these",
                              fg=DIM)
            else:
                # The card's totals come from the persisted counters; this
                # ledger only knows what the proxy recorded after it existed.
                status.config(text="nothing recorded yet - the next request starts today",
                              fg=DIM)

        alive = {"v": True}

        def poll():
            if not alive["v"]:
                return
            refresh()
            root.after(5000, poll)  # the numbers move while the proxy is busy

        def close(_e=None):
            alive["v"] = False
            _history_win["open"] = False
            root.destroy()

        for label, cmd in (("Close", close), ("Refresh", refresh)):
            tk.Button(bar, text=label, command=cmd, font=("Segoe UI", 9), bg=FIELD,
                      fg=FG, activebackground=TRACK, activeforeground=FG,
                      relief="flat", bd=0, padx=12, pady=5, cursor="hand2"
                      ).pack(side="right", padx=(8, 0))

        root.protocol("WM_DELETE_WINDOW", close)
        root.bind("<Escape>", close)
        poll()

    ui_post(_run)


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

    # The card is the point of the thing, so it is on unless told otherwise.
    if CFG.get("overlay_show_on_start", True):
        open_stats_overlay()

    proxy_menu = pystray.Menu(
        pystray.MenuItem("Flow...", open_log_window),
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
        pystray.MenuItem("Usage history...", open_history_window),
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
