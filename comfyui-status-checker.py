#!/usr/bin/python3
"""
ComfyUI Status Checker — always-on-top circular indicator for ComfyUI instance health.

Uses GTK3 + Cairo for true RGBA transparency (no square background).
Hover to see full status panel with queue, GPU/VRAM, and system info.
Drag to reposition. Right-click to quit.
"""

import argparse
import webbrowser
import base64
import glob
import json
import math
import os
import platform
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from urllib.request import urlopen, Request
from urllib.error import URLError

IS_WINDOWS = platform.system() == "Windows"

try:
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk, Gdk, GLib  # noqa: E402
except (ImportError, ValueError) as e:
    if IS_WINDOWS:
        print(
            "ERROR: GTK3 / PyGObject not found.\n\n"
            "On Windows, install via MSYS2:\n"
            "  1. Install MSYS2 from https://www.msys2.org/\n"
            "  2. In MSYS2 UCRT64 terminal run:\n"
            "       pacman -S mingw-w64-ucrt-x86_64-python-gobject mingw-w64-ucrt-x86_64-gtk3\n"
            "  3. Run this script using the MSYS2 Python:\n"
            "       /ucrt64/bin/python3 comfyui-status-checker.py\n\n"
            "Alternatively, install via pip (requires GTK3 runtime):\n"
            "  pip install PyGObject\n"
            "  and install GTK3 runtime from https://github.com/tschoonj/GTK-for-Windows-Runtime-Environment-Installer/releases",
            file=sys.stderr,
        )
    else:
        print(
            "ERROR: GTK3 / PyGObject not found.\n\n"
            "Install with your package manager:\n"
            "  Debian/Ubuntu:  sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0\n"
            "  Fedora:         sudo dnf install python3-gobject gtk3\n"
            "  Arch:           sudo pacman -S python-gobject gtk3",
            file=sys.stderr,
        )
    sys.exit(1)

# -- widget coordination (shared across status-checker widgets) -----------
WIDGET_NAME = "comfyui"
WIDGET_DIR = os.path.join(os.path.expanduser("~"), ".config", "status-widgets")
CORNER_FILE = os.path.join(WIDGET_DIR, "corner.json")
STACK_GAP = 50  # vertical pixels between stacked widgets


def _ensure_widget_dir():
    os.makedirs(WIDGET_DIR, exist_ok=True)


def _register_widget(name, xid=None):
    _ensure_widget_dir()
    path = os.path.join(WIDGET_DIR, f"{name}.json")
    with open(path, "w") as f:
        json.dump({"pid": os.getpid(), "name": name, "xid": xid}, f)


def _unregister_widget(name):
    try:
        os.remove(os.path.join(WIDGET_DIR, f"{name}.json"))
    except OSError:
        pass


def _pid_alive(pid):
    if IS_WINDOWS:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x100000, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def _get_active_widgets():
    """Return sorted list of active widget names."""
    _ensure_widget_dir()
    widgets = []
    for fname in sorted(os.listdir(WIDGET_DIR)):
        if fname.endswith(".json") and fname != "corner.json":
            path = os.path.join(WIDGET_DIR, fname)
            try:
                with open(path) as f:
                    data = json.load(f)
                pid = data.get("pid")
                if pid and _pid_alive(pid):
                    widgets.append(data["name"])
                else:
                    os.remove(path)
            except (json.JSONDecodeError, OSError, KeyError):
                pass
    return widgets


def _read_corner():
    try:
        with open(CORNER_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"corner_index": -1, "timestamp": 0}


def _write_corner(corner_index):
    _ensure_widget_dir()
    with open(CORNER_FILE, "w") as f:
        json.dump({"corner_index": corner_index, "timestamp": time.time()}, f)


BAR_FILE = os.path.join(WIDGET_DIR, "bar.json")


def _read_bar():
    """SH-widgetbar state: a dict while a live bar owns the layout, None when
    there is no bar, False when the file is mid-write (keep state, retry)."""
    try:
        with open(BAR_FILE) as f:
            raw = f.read()
    except OSError:
        return None
    try:
        bar = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return False
    if isinstance(bar, dict) and bar.get("pid") and _pid_alive(bar["pid"]):
        return bar
    return None


def _dock_panel_pos(bar_rect, orientation, x, y, win, pw, ph, mx, my, mw, mh):
    """Panel origin for a docked dot: perpendicular to the bar so it never
    covers the neighbouring dots — below a horizontal bar, beside a vertical
    one, flipping to the other side when the monitor edge is too close."""
    bx, by, bw, bh = bar_rect
    if orientation == "horizontal":
        py = by + bh + 8
        if py + ph > my + mh:
            py = by - ph - 8
        px = x + win // 2 - pw // 2
    else:
        px = bx + bw + 8
        if px + pw > mx + mw:
            px = bx - pw - 8
        py = y
    px = max(mx + 4, min(px, mx + mw - pw - 4))
    py = max(my + 4, min(py, my + mh - ph - 4))
    return px, py


# SH-widgetbar pill surface. A docked dot paints this behind itself because
# the bar leaves a hole under every slot (see sh-widgetbar.py).
_BAR_FILL = (0.055, 0.051, 0.043, 0.96)


def _xid_of(win):
    """X11 window id of a realized Gtk.Window (None on Wayland/unrealized).
    The bar uses it to move docked dots directly, in the same frame as itself."""
    try:
        import gi
        gi.require_version("GdkX11", "3.0")
        from gi.repository import GdkX11  # noqa: F401  (adds get_xid to windows)
        return win.get_window().get_xid()
    except Exception:
        return None


# -- defaults -------------------------------------------------------------
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8188
POLL_SECS = 2  # fast polling for generation status
SENSOR_SECS = 5  # local sensors change slower than the queue does


# -- local hardware sensors -----------------------------------------------
# ComfyUI's /system_stats reports VRAM only — no temperature, power, fan or
# utilisation. Those come from the machine itself, so they are read locally
# and ONLY when the monitored endpoint is this machine (see _endpoint_is_local).

def _endpoint_is_local(host):
    """True when `host` resolves to this machine.

    Sensors describe local hardware; showing them next to a remote server's
    VRAM would silently mix two machines in one panel.
    """
    if not host:
        return False
    if host in ("127.0.0.1", "::1", "localhost", "0.0.0.0"):
        return True
    try:
        addrs = {ai[4][0] for ai in socket.getaddrinfo(host, None)}
    except (socket.gaierror, UnicodeError, OSError):
        return False
    if addrs & {"127.0.0.1", "::1"}:
        return True
    try:
        local = {ai[4][0] for ai in socket.getaddrinfo(socket.gethostname(), None)}
    except (socket.gaierror, UnicodeError, OSError):
        local = set()
    return bool(addrs & local)


def _run(cmd, timeout=4):
    """Run a command, returning stdout or None. Never raises."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 else None


_NVIDIA_SMI_FIELDS = (
    "index,temperature.gpu,utilization.gpu,power.draw,power.limit,fan.speed"
)


def read_gpu_sensors():
    """Per-GPU telemetry from nvidia-smi, keyed by device index.

    Returns {} when nvidia-smi is absent or fails — the panel simply omits
    the rows rather than showing zeros.
    """
    out = _run(["nvidia-smi", f"--query-gpu={_NVIDIA_SMI_FIELDS}",
                "--format=csv,noheader,nounits"])
    if not out:
        return {}

    def num(tok):
        tok = tok.strip()
        try:
            return float(tok)
        except ValueError:
            return None  # "[N/A]" on cards without that sensor

    sensors = {}
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 6:
            continue
        idx = num(parts[0])
        if idx is None:
            continue
        sensors[int(idx)] = {
            "temp": num(parts[1]),
            "util": num(parts[2]),
            "power": num(parts[3]),
            "power_limit": num(parts[4]),
            "fan": num(parts[5]),
        }
    return sensors


def _find_cpu_temp_input():
    """Locate a CPU package temperature file under hwmon, or None.

    Prefers the known CPU drivers, then any sensor labelled Package/Tdie/Tctl.
    """
    prefer = ("k10temp", "zenpower", "coretemp")
    try:
        hwmons = sorted(glob.glob("/sys/class/hwmon/hwmon*"))
    except OSError:
        return None

    labelled = []
    for h in hwmons:
        try:
            with open(os.path.join(h, "name")) as f:
                name = f.read().strip()
        except OSError:
            continue
        for inp in sorted(glob.glob(os.path.join(h, "temp*_input"))):
            label = ""
            try:
                with open(inp.replace("_input", "_label")) as f:
                    label = f.read().strip()
            except OSError:
                pass
            if name in prefer:
                # Tdie/Tctl/Package is the die reading; fall back to first input
                if label in ("Tdie", "Tctl", "Package id 0") or not label:
                    return inp
                labelled.append(inp)
            elif label.startswith(("Package", "Tdie", "Tctl")):
                labelled.append(inp)
    return labelled[0] if labelled else None


class _CpuUtil:
    """CPU utilisation from successive /proc/stat deltas."""

    def __init__(self):
        self._prev = None

    def read(self):
        try:
            with open("/proc/stat") as f:
                parts = f.readline().split()
        except OSError:
            return None
        if len(parts) < 5 or parts[0] != "cpu":
            return None
        vals = [float(v) for v in parts[1:]]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0.0)
        total = sum(vals)
        prev, self._prev = self._prev, (idle, total)
        if prev is None:
            return None  # need two samples for a delta
        d_idle, d_total = idle - prev[0], total - prev[1]
        if d_total <= 0:
            return None
        return max(0.0, min(100.0, (1.0 - d_idle / d_total) * 100.0))


def read_cpu_sensors(temp_input, util_reader):
    """CPU temperature (°C) and utilisation (%), each None when unavailable."""
    temp = None
    if temp_input:
        try:
            with open(temp_input) as f:
                temp = int(f.read().strip()) / 1000.0
        except (OSError, ValueError):
            temp = None
    return {"temp": temp, "util": util_reader.read()}

# -- design tokens --------------------------------------------------------
BG_PANEL = (0.08, 0.08, 0.08)
BG_ROW = (0.10, 0.10, 0.10)
FG = (0.96, 0.96, 0.94)
FG_DIM = (0.54, 0.54, 0.50)
BORDER_CLR = (0.17, 0.17, 0.17)

DOT_RADIUS = 14
RING_RADIUS = 18
GLOW_RADIUS = 22
WIN_SIZE = 44        # avatar + bubble fit in this; must match DOT in sh-widgetbar.py
AVATAR_RADIUS = 17   # the brand disc
BUBBLE_RADIUS = 5    # status bubble, top-right, chat-app style

# state → color
STATE_COLORS = {
    "offline":     (0.42, 0.42, 0.50),  # grey
    "idle":        (0.13, 0.77, 0.37),  # green
    "generating":  (0.23, 0.51, 0.96),  # blue
    "queued":      (0.92, 0.70, 0.03),  # yellow — pending items waiting
    "error":       (0.94, 0.27, 0.27),  # red
}

STATE_LABELS = {
    "offline":     "OFFLINE",
    "idle":        "IDLE",
    "generating":  "GENERATING",
    "queued":      "QUEUED",
    "error":       "ERROR",
}

TOAST_DURATION_MS = 4000  # how long toasts stay visible


# -- minimal websocket client for ComfyUI progress -----------------------

class _ComfyWS:
    """Background websocket listener for ComfyUI execution progress."""

    def __init__(self, host, port, on_progress, on_complete):
        self.host = host
        self.port = port
        self.on_progress = on_progress  # callback(step, total)
        self.on_complete = on_complete  # callback()
        self.client_id = str(uuid.uuid4())
        self._sock = None
        self._running = False

    def start(self):
        self._running = True
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self._running = False
        try:
            if self._sock:
                self._sock.close()
        except OSError:
            pass

    def update_endpoint(self, host, port):
        self.host = host
        self.port = port
        self.stop()
        self.start()

    def _run(self):
        while self._running:
            try:
                self._connect()
                self._read_loop()
            except Exception:
                pass
            if self._running:
                time.sleep(3)

    def _connect(self):
        self._sock = socket.create_connection(
            (self.host, int(self.port)), timeout=10
        )
        key = base64.b64encode(os.urandom(16)).decode()
        handshake = (
            f"GET /ws?clientId={self.client_id} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self._sock.sendall(handshake.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("WS handshake failed")
            resp += chunk

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("WS closed")
            buf += chunk
        return buf

    def _read_frame(self):
        header = self._recv_exact(2)
        opcode = header[0] & 0x0F
        length = header[1] & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._recv_exact(8))[0]
        data = self._recv_exact(length) if length else b""
        if opcode == 0x08:
            return None
        if opcode == 0x09:  # ping → pong
            self._sock.sendall(bytes([0x8A, 0]))
            return self._read_frame()
        if opcode == 0x02:  # binary frame, skip
            return b""
        return data

    def _read_loop(self):
        while self._running:
            data = self._read_frame()
            if data is None:
                break
            if not data:
                continue
            try:
                msg = json.loads(data)
                t = msg.get("type")
                if t == "progress":
                    v = msg["data"]["value"]
                    mx = msg["data"]["max"]
                    GLib.idle_add(self.on_progress, v, mx)
                elif t == "executing" and msg.get("data", {}).get("node") is None:
                    GLib.idle_add(self.on_complete)
            except (json.JSONDecodeError, KeyError):
                pass


# -- ComfyUI logo SVG path (viewBox 0 0 24 24) ---------------------------
COMFY_BRAND = (0.129, 0.098, 0.153)    # comfy.org plum #211927
COMFY_LIME = (0.949, 1.0, 0.349)       # comfy.org lime #F2FF59
COMFYUI_LOGO_PATH = (
    "M31.0126 30.4797C31.0576 30.3275 31.0822 30.1671 31.0822 29.9985C31.0822 29.0649 30.3294 2"
    "8.3081 29.4006 28.3081H21.8643C21.4593 28.3122 21.1279 27.9832 21.1279 27.576C21.1279 27.5"
    "019 21.1401 27.432 21.1565 27.3662L23.1858 20.259C23.2717 19.9465 23.5581 19.7161 23.8936 "
    "19.7161L31.4586 19.7079C33.0542 19.7079 34.4003 18.6262 34.8053 17.1497L35.9427 13.1889C35"
    ".9795 13.0491 36 12.8969 36 12.7447C36 11.8152 35.2513 11.0625 34.3266 11.0625H25.1742C23."
    "5868 11.0625 22.2448 12.136 21.8316 13.5961L21.0624 16.2983C20.9724 16.6068 20.6901 16.833"
    " 20.3546 16.833H18.1575C16.5823 16.833 15.2526 17.8859 14.8271 19.3295L12.0614 29.0402C12."
    "0205 29.1841 12 29.3404 12 29.4967C12 30.4304 12.7528 31.1871 13.6816 31.1871H15.8418C16.2"
    "468 31.1871 16.5782 31.5162 16.5782 31.9275C16.5782 31.9974 16.5701 32.0673 16.5496 32.133"
    "1L15.7845 34.8107C15.7477 34.9546 15.7232 35.1027 15.7232 35.2549C15.7232 36.1844 16.4719 "
    "36.937 17.3965 36.937L26.553 36.9288C28.1446 36.9288 29.4865 35.8512 29.8957 34.3829L31.00"
    "85 30.4838L31.0126 30.4797Z"
)  # comfy.org favicon mark (48-box)


def _normalize_arc_flags(d):
    """Pre-process SVG path to separate concatenated arc flags.

    SVG arc flags (0|1) can appear glued together or to subsequent numbers,
    e.g. ``a.6.6 0 00-.1-.5`` means large-arc=0, sweep=0, dx=-0.1, dy=-0.5.
    This inserts commas so the tokenizer sees each flag as a separate token.
    """
    out = []
    i = 0
    in_arc = False
    arc_param = 0  # which param within current 7-param arc group
    while i < len(d):
        ch = d[i]
        if ch.isalpha() and ch != 'e' and ch != 'E':
            in_arc = ch in ('a', 'A')
            arc_param = 0
            out.append(ch)
            i += 1
            continue
        if not in_arc:
            out.append(ch)
            i += 1
            continue
        # inside arc — count parameters
        if ch in (' ', ',', '\t', '\n', '\r'):
            out.append(ch)
            i += 1
            continue
        # start of a number or flag
        if arc_param % 7 in (3, 4):
            # flag position: consume exactly one char (0 or 1)
            out.append(ch)
            out.append(',')
            arc_param += 1
            i += 1
        else:
            # consume a full number (stop at second decimal point)
            j = i
            if j < len(d) and d[j] in '+-':
                j += 1
            has_dot = False
            while j < len(d) and (d[j].isdigit() or (d[j] == '.' and not has_dot)):
                if d[j] == '.':
                    has_dot = True
                j += 1
            # exponent
            if j < len(d) and d[j] in ('e', 'E'):
                j += 1
                if j < len(d) and d[j] in '+-':
                    j += 1
                while j < len(d) and d[j].isdigit():
                    j += 1
            out.append(d[i:j])
            out.append(',')
            arc_param += 1
            i = j
    return ''.join(out)


def _parse_svg_path(d):
    """Parse SVG path 'd' attribute into (command, numbers) tuples."""
    d = _normalize_arc_flags(d)
    tokens = re.findall(
        r'[MmLlHhVvCcSsQqTtAaZz]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?', d
    )
    cmds = []
    i = 0
    cmd = None
    while i < len(tokens):
        if tokens[i].isalpha():
            cmd = tokens[i]
            i += 1
        nums = []
        while i < len(tokens) and not tokens[i].isalpha():
            nums.append(float(tokens[i]))
            i += 1
        if cmd:
            cmds.append((cmd, nums))
    return cmds


def _svg_arc_to_cairo(cr, rx, ry, rotation, large_arc, sweep, ex, ey, sx, sy):
    """Convert SVG arc params to Cairo arcs."""
    r = (abs(rx) + abs(ry)) / 2
    if r < 1e-6:
        cr.line_to(ex, ey)
        return
    dx = ex - sx
    dy = ey - sy
    dist = math.hypot(dx, dy)
    if dist < 1e-6:
        return
    if r < dist / 2:
        r = dist / 2
    mx, my = (sx + ex) / 2, (sy + ey) / 2
    d = math.sqrt(max(0, r * r - (dist / 2) ** 2))
    nx, ny = -dy / dist, dx / dist
    if large_arc != sweep:
        cx_, cy_ = mx + d * nx, my + d * ny
    else:
        cx_, cy_ = mx - d * nx, my - d * ny
    a1 = math.atan2(sy - cy_, sx - cx_)
    a2 = math.atan2(ey - cy_, ex - cx_)
    if sweep:
        cr.arc(cx_, cy_, r, a1, a2)
    else:
        cr.arc_negative(cx_, cy_, r, a1, a2)


def draw_svg_logo(cr, path_d, cx, cy, size, viewbox=24):
    """Draw an SVG path centered at (cx, cy) scaled to fit 'size' pixels."""
    scale = size / viewbox
    ox = cx - size / 2
    oy = cy - size / 2

    cmds = _parse_svg_path(path_d)
    x, y = 0.0, 0.0
    sx, sy = 0.0, 0.0
    lx2, ly2 = 0.0, 0.0

    for cmd, nums in cmds:
        n = nums
        if cmd == 'M':
            for j in range(0, len(n), 2):
                x, y = n[j], n[j + 1]
                if j == 0:
                    cr.move_to(ox + x * scale, oy + y * scale)
                    sx, sy = x, y
                else:
                    cr.line_to(ox + x * scale, oy + y * scale)
        elif cmd == 'm':
            for j in range(0, len(n), 2):
                x += n[j]; y += n[j + 1]
                if j == 0:
                    cr.move_to(ox + x * scale, oy + y * scale)
                    sx, sy = x, y
                else:
                    cr.line_to(ox + x * scale, oy + y * scale)
        elif cmd == 'L':
            for j in range(0, len(n), 2):
                x, y = n[j], n[j + 1]
                cr.line_to(ox + x * scale, oy + y * scale)
        elif cmd == 'l':
            for j in range(0, len(n), 2):
                x += n[j]; y += n[j + 1]
                cr.line_to(ox + x * scale, oy + y * scale)
        elif cmd == 'H':
            for v in n:
                x = v
                cr.line_to(ox + x * scale, oy + y * scale)
        elif cmd == 'h':
            for v in n:
                x += v
                cr.line_to(ox + x * scale, oy + y * scale)
        elif cmd == 'V':
            for v in n:
                y = v
                cr.line_to(ox + x * scale, oy + y * scale)
        elif cmd == 'v':
            for v in n:
                y += v
                cr.line_to(ox + x * scale, oy + y * scale)
        elif cmd == 'C':
            for j in range(0, len(n), 6):
                x1, y1 = n[j], n[j+1]
                x2, y2 = n[j+2], n[j+3]
                x, y = n[j+4], n[j+5]
                cr.curve_to(
                    ox + x1 * scale, oy + y1 * scale,
                    ox + x2 * scale, oy + y2 * scale,
                    ox + x * scale, oy + y * scale,
                )
                lx2, ly2 = x2, y2
        elif cmd == 'c':
            for j in range(0, len(n), 6):
                x1, y1 = x + n[j], y + n[j+1]
                x2, y2 = x + n[j+2], y + n[j+3]
                x += n[j+4]; y += n[j+5]
                cr.curve_to(
                    ox + x1 * scale, oy + y1 * scale,
                    ox + x2 * scale, oy + y2 * scale,
                    ox + x * scale, oy + y * scale,
                )
                lx2, ly2 = x2, y2
        elif cmd == 'S':
            for j in range(0, len(n), 4):
                x1, y1 = 2 * x - lx2, 2 * y - ly2
                x2, y2 = n[j], n[j+1]
                x, y = n[j+2], n[j+3]
                cr.curve_to(
                    ox + x1 * scale, oy + y1 * scale,
                    ox + x2 * scale, oy + y2 * scale,
                    ox + x * scale, oy + y * scale,
                )
                lx2, ly2 = x2, y2
        elif cmd == 's':
            for j in range(0, len(n), 4):
                x1, y1 = 2 * x - lx2, 2 * y - ly2
                x2, y2 = x + n[j], y + n[j+1]
                x += n[j+2]; y += n[j+3]
                cr.curve_to(
                    ox + x1 * scale, oy + y1 * scale,
                    ox + x2 * scale, oy + y2 * scale,
                    ox + x * scale, oy + y * scale,
                )
                lx2, ly2 = x2, y2
        elif cmd == 'A':
            for j in range(0, len(n), 7):
                ex, ey = n[j+5], n[j+6]
                _svg_arc_to_cairo(
                    cr, n[j], n[j+1], n[j+2], int(n[j+3]), int(n[j+4]),
                    ox + ex * scale, oy + ey * scale,
                    ox + x * scale, oy + y * scale,
                )
                x, y = ex, ey
        elif cmd == 'a':
            for j in range(0, len(n), 7):
                ex, ey = x + n[j+5], y + n[j+6]
                _svg_arc_to_cairo(
                    cr, n[j] * scale, n[j+1] * scale, n[j+2],
                    int(n[j+3]), int(n[j+4]),
                    ox + ex * scale, oy + ey * scale,
                    ox + x * scale, oy + y * scale,
                )
                x, y = ex, ey
        elif cmd in ('Z', 'z'):
            cr.close_path()
            x, y = sx, sy


def fmt_bytes(b):
    """Format bytes as human-readable."""
    if b is None:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(b) < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


def fmt_pct(used, total):
    """Format a used/total as percentage."""
    if not total:
        return "—"
    return f"{used / total * 100:.0f}%"


def _short_gpu_name(name):
    """Drop vendor/brand noise that repeats on every card."""
    for prefix in ("NVIDIA GeForce ", "NVIDIA ", "AMD Radeon ", "AMD ", "Intel(R) "):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def fmt_temp(c):
    """Format a °C reading."""
    if c is None:
        return "—"
    return f"{c:.0f}°C"


def _device_info(dev, fallback_index=0):
    """Normalise one entry of /system_stats devices[] into what the panel draws.

    Everything is derived from the response — the allocator suffix, the
    device index and the short name are only *parsed* if present, never
    assumed. Any device the server reports (cuda, cpu, mps, xpu, ...)
    survives this untouched.
    """
    raw_name = (dev.get("name") or "").strip()

    # ComfyUI formats names as "cuda:0 NVIDIA GeForce RTX 3090 : cudaMallocAsync".
    # Split the allocator suffix off the end if the " : " separator is there.
    if " : " in raw_name:
        name_part, allocator = (s.strip() for s in raw_name.rsplit(" : ", 1))
    else:
        name_part, allocator = raw_name, ""

    # Lead token is the torch device string ("cuda:0") when it looks like one.
    label, short_name = name_part, name_part
    head, _, tail = name_part.partition(" ")
    if ":" in head and tail:
        label, short_name = head, tail.strip()

    index = dev.get("index")
    if index is None:
        index = fallback_index

    vram_total = dev.get("vram_total") or 0
    vram_free = dev.get("vram_free") or 0
    torch_total = dev.get("torch_vram_total") or 0
    torch_free = dev.get("torch_vram_free") or 0

    return {
        "label": label or f"device {index}",       # "cuda:0"
        "name": short_name or raw_name or "—",     # "NVIDIA GeForce RTX 3090"
        "type": dev.get("type") or "",
        "index": index,
        "allocator": allocator,
        "vram_total": vram_total,
        "vram_used": max(0, vram_total - vram_free),
        "torch_vram_total": torch_total,
        "torch_vram_used": max(0, torch_total - torch_free),
    }


class DotWindow(Gtk.Window):
    """The small circular always-on-top dot."""

    def __init__(self, host, port):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.host = host
        self.port = port
        self.base_url = f"http://{host}:{port}"

        self.set_title("ComfyUI Status")
        self.set_decorated(False)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        # UTILITY receives keyboard focus; DOCK does not on most Linux WMs
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.set_default_size(WIN_SIZE, WIN_SIZE)
        self.set_size_request(WIN_SIZE, WIN_SIZE)
        self.set_resizable(False)
        self.move(20, 70)  # offset below Claude status light

        # RGBA transparency
        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual:
            self.set_visual(visual)
        self.set_app_paintable(True)

        self.set_can_focus(True)
        self.set_accept_focus(True)

        self.connect("draw", self._on_draw)
        self.connect("button-press-event", self._on_button)
        self.connect("button-release-event", self._on_button_release)
        self.connect("motion-notify-event", self._on_motion)
        self.connect("enter-notify-event", self._on_enter)
        self.connect("leave-notify-event", self._on_leave)
        self.connect("key-press-event", self._on_key_press)
        self.connect("destroy", Gtk.main_quit)

        self.set_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
            | Gdk.EventMask.ENTER_NOTIFY_MASK
            | Gdk.EventMask.LEAVE_NOTIFY_MASK
            | Gdk.EventMask.KEY_PRESS_MASK
        )

        # state
        self.color = STATE_COLORS["offline"]
        self.data = self._empty_data()
        self.pulse_phase = 0.0
        self.panel = None
        self.dragging = False
        self._drag_offset_x = 0
        self._drag_offset_y = 0
        self._drag_moved = False
        self._corner_margin = 8
        self._prev_state = None
        self._toast = None
        self._progress_toast = None
        self._ws_progress = (0, 0)  # (step, total) from websocket

        # local sensor state — only meaningful when ComfyUI runs on this machine
        self._sensors_local = _endpoint_is_local(host)
        self._cpu_temp_input = _find_cpu_temp_input() if self._sensors_local else None
        self._cpu_util = _CpuUtil()
        self._gpu_sensor_cache = {}
        self._gpu_sensor_ts = 0.0
        self._cpu_sensor_cache = None
        self._cpu_sensor_ts = 0.0

        # widget coordination
        _register_widget(WIDGET_NAME)
        self.connect("realize", lambda w: _register_widget(WIDGET_NAME, _xid_of(w)))
        self._last_corner_ts = 0  # track corner file changes
        self._docked = False
        self._last_bar_ts = 0
        self._bar_rect = None
        self._bar_target = None
        self._bar_orientation = "horizontal"
        self.connect("destroy", lambda w: _unregister_widget(WIDGET_NAME))

        # websocket for generation progress
        self._ws = _ComfyWS(host, port, self._on_ws_progress, self._on_ws_complete)
        self._ws.start()
        self.connect("destroy", lambda w: self._ws.stop())

        # pulse timer (20fps)
        GLib.timeout_add(50, self._tick_pulse)
        # watch shared corner file (5 checks/sec)
        GLib.timeout_add(50, self._watch_corner)   # 50 ms: tracks a bar drag smoothly
        # poll thread
        threading.Thread(target=self._poll_loop, daemon=True).start()

    @staticmethod
    def _empty_data():
        return {
            "state": "offline",
            "running": 0,
            "pending": 0,
            "devices": [],
            "cpu": {"temp": None, "util": None},
            "ram_total": 0,
            "ram_free": 0,
            "comfyui_version": "—",
            "pytorch_version": "—",
            "python_version": "—",
            "os": "—",
            "last_check": None,
        }

    # -- drawing ----------------------------------------------------------

    def _on_draw(self, widget, cr):
        cr.set_operator(0); cr.paint(); cr.set_operator(2)   # clear to transparent
        if self._docked:   # the bar has a hole here: paint its surface under us
            cr.set_source_rgba(*_BAR_FILL)
            cr.paint()
        cx = cy = WIN_SIZE / 2
        R = AVATAR_RADIUS
        sr, sg, sb = self.color          # status colour lives only in the bubble
        state = self.data["state"]

        # Something wrong: a slow breathing ring around the avatar, nothing else.
        if state == "error":
            pulse = 0.5 + 0.5 * math.sin(self.pulse_phase)
            cr.set_source_rgba(sr, sg, sb, 0.10 + 0.25 * pulse)
            cr.set_line_width(1)
            cr.arc(cx, cy, R + 3, 0, 2 * math.pi)
            cr.stroke()

        # brand disc, with a faint hairline so dark brands still read on the pill
        cr.set_source_rgb(*COMFY_BRAND)
        cr.arc(cx, cy, R, 0, 2 * math.pi)
        cr.fill()
        cr.set_source_rgba(0.937, 0.925, 0.902, 0.14)
        cr.set_line_width(1)
        cr.arc(cx, cy, R - 0.5, 0, 2 * math.pi)
        cr.stroke()

        # the logo, in its brand foreground
        cr.save()
        cr.arc(cx, cy, R, 0, 2 * math.pi)
        cr.clip()
        cr.set_source_rgb(*COMFY_LIME)
        draw_svg_logo(cr, COMFYUI_LOGO_PATH, cx, cy, R * 2 * 0.8, 48)
        cr.fill()
        cr.restore()
        if state == "generating":       # spinning arc just inside the disc edge
            angle = self.pulse_phase * 3.0
            cr.set_source_rgba(*COMFY_LIME, 0.9)
            cr.set_line_width(2)
            cr.arc(cx, cy, R - 2, angle, angle + math.pi * 0.6)
            cr.stroke()

        # status bubble, top-right, cut out of the disc like a chat presence dot
        bx, by = cx + R * 0.70, cy - R * 0.70
        if self._docked:
            cr.set_source_rgba(*_BAR_FILL)
        else:
            cr.set_operator(0)           # free-floating: punch a transparent gap
        cr.arc(bx, by, BUBBLE_RADIUS + 2, 0, 2 * math.pi)
        cr.fill()
        cr.set_operator(2)
        cr.set_source_rgb(sr, sg, sb)
        cr.arc(bx, by, BUBBLE_RADIUS, 0, 2 * math.pi)
        cr.fill()
        return False

    def _tick_pulse(self):
        self.pulse_phase += 0.08
        self.queue_draw()
        return True

    # -- polling ----------------------------------------------------------

    def _poll_loop(self):
        while True:
            data = self._fetch()
            GLib.idle_add(self._apply_data, data)
            time.sleep(POLL_SECS)

    # -- local sensors ----------------------------------------------------

    def _read_gpu_sensors(self):
        """nvidia-smi telemetry, throttled and cached. {} when not applicable."""
        if not self._sensors_local:
            return {}
        now = time.monotonic()
        if now - self._gpu_sensor_ts >= SENSOR_SECS:
            self._gpu_sensor_cache = read_gpu_sensors()
            self._gpu_sensor_ts = now
        return self._gpu_sensor_cache

    def _read_cpu(self):
        """CPU temp + utilisation, throttled and cached."""
        empty = {"temp": None, "util": None}
        if not self._sensors_local:
            return empty
        now = time.monotonic()
        if now - self._cpu_sensor_ts >= SENSOR_SECS:
            self._cpu_sensor_cache = read_cpu_sensors(
                self._cpu_temp_input, self._cpu_util
            )
            self._cpu_sensor_ts = now
        return self._cpu_sensor_cache or empty

    def _fetch(self):
        data = self._empty_data()
        data["last_check"] = datetime.now(timezone.utc)

        try:
            # queue status
            with urlopen(
                Request(f"{self.base_url}/queue",
                        headers={"User-Agent": "comfyui-status-checker/1.0"}),
                timeout=5,
            ) as r:
                q = json.loads(r.read())
            running = len(q.get("queue_running", []))
            pending = len(q.get("queue_pending", []))
            data["running"] = running
            data["pending"] = pending

            if running > 0:
                data["state"] = "generating"
            elif pending > 0:
                data["state"] = "queued"
            else:
                data["state"] = "idle"

            # system stats
            with urlopen(
                Request(f"{self.base_url}/system_stats",
                        headers={"User-Agent": "comfyui-status-checker/1.0"}),
                timeout=5,
            ) as r:
                sys_data = json.loads(r.read())

            system = sys_data.get("system", {})
            data["comfyui_version"] = system.get("comfyui_version", "—")
            data["pytorch_version"] = system.get("pytorch_version", "—")
            data["python_version"] = system.get("python_version", "—").split("(")[0].strip()
            data["os"] = system.get("os", "—")
            data["ram_total"] = system.get("ram_total", 0)
            data["ram_free"] = system.get("ram_free", 0)

            # every device the server reports — count, order and naming
            # all come from the response, nothing assumed
            data["cpu"] = self._read_cpu()
            devices = [
                _device_info(dev, i)
                for i, dev in enumerate(sys_data.get("devices", []) or [])
            ]

            # local telemetry (temp / power / util / fan) merged in by index
            gpu_sensors = self._read_gpu_sensors()
            for dev in devices:
                dev.update(gpu_sensors.get(dev["index"], {}))
            data["devices"] = devices

        except (URLError, KeyError, json.JSONDecodeError, OSError, ConnectionError):
            data["state"] = "offline"

        return data

    def _apply_data(self, data):
        new_state = data["state"]
        old_state = self._prev_state

        # detect state changes and show toast
        if old_state is not None and new_state != old_state:
            color = STATE_COLORS.get(new_state, STATE_COLORS["offline"])
            label = STATE_LABELS.get(new_state, new_state.upper())
            if new_state == "generating":
                self._show_toast(f"Generating...", color)
            elif new_state == "idle" and old_state == "generating":
                self._show_toast("Generation complete", color)
            elif new_state == "offline":
                self._show_toast("ComfyUI offline", color)
            elif new_state == "idle" and old_state == "offline":
                self._show_toast("ComfyUI online", color)
            elif new_state == "queued":
                self._show_toast(f"Queued ({data['pending']} pending)", color)

        self._prev_state = new_state
        self.data = data
        self.color = STATE_COLORS.get(new_state, STATE_COLORS["offline"])
        self.queue_draw()
        if self.panel and self.panel.get_visible():
            self.panel.update_data(data, self.base_url)
        return False

    def _show_toast(self, message, color=None):
        if self._toast:
            try:
                self._toast.destroy()
            except Exception:
                pass
        self._toast = ToastWindow(self, message, color)
        self._toast.popup()

    def _on_ws_progress(self, step, total):
        if total <= 0:
            return
        pct = int(step / total * 100)
        color = STATE_COLORS["generating"]
        msg = f"Step {step}/{total}  ({pct}%)"
        if self._progress_toast and self._progress_toast.get_visible() and self._progress_toast._opacity > 0.5:
            self._progress_toast.update_text(msg)
        else:
            if self._progress_toast:
                try:
                    self._progress_toast.destroy()
                except Exception:
                    pass
            self._progress_toast = ToastWindow(self, msg, color)
            self._progress_toast.popup(duration_ms=6000)
        self._ws_progress = (step, total)

    def _on_ws_complete(self):
        self._ws_progress = (0, 0)
        if self._progress_toast:
            try:
                self._progress_toast.destroy()
            except Exception:
                pass
            self._progress_toast = None

    # -- mouse events -----------------------------------------------------

    def _on_button(self, widget, event):
        if event.button == 1:
            self._close_panel()
            self.dragging = True
            self._drag_moved = False
            wx, wy = self.get_position()
            self._drag_offset_x = int(event.x_root) - wx
            self._drag_offset_y = int(event.y_root) - wy
        elif event.button == 3:
            self._show_menu(event)

    def _show_menu(self, event):
        """Refresh, open the page, and Quit last behind a separator."""
        self._close_panel()
        menu = Gtk.Menu()
        menu.attach_to_widget(self, None)
        self._menu = menu

        def add(label, cb):
            it = Gtk.MenuItem(label=label)
            it.connect("activate", lambda *_: cb())
            menu.append(it)

        add("Refresh now", self._refresh_now)
        add("Open ComfyUI", lambda: webbrowser.open(self.base_url))
        menu.append(Gtk.SeparatorMenuItem())
        add("Quit ComfyUI widget", self.destroy)
        menu.show_all()
        menu.popup_at_pointer(event)

    def _refresh_now(self):
        def work():
            try:
                data = self._fetch()
            except Exception:
                return
            GLib.idle_add(self._apply_data, data)
        threading.Thread(target=work, daemon=True).start()

    def _on_button_release(self, widget, event):
        if event.button == 1:
            self.dragging = False
            self._drag_moved = False

    def _on_motion(self, widget, event):
        if self.dragging and not self._docked:
            self._drag_moved = True
            self.move(
                int(event.x_root) - self._drag_offset_x,
                int(event.y_root) - self._drag_offset_y,
            )

    def _on_key_press(self, widget, event):
        if event.keyval == Gdk.KEY_grave:  # ~ / ` key
            if not self._docked:
                self._cycle_corner()
            return True
        return False

    def _cycle_corner(self):
        """Advance to next side position and broadcast to all widgets."""
        self._close_panel()
        n_positions = Gdk.Display.get_default().get_n_monitors() * 2
        corner = _read_corner()
        new_index = (corner["corner_index"] + 1) % n_positions
        _write_corner(new_index)
        self._apply_corner(new_index)

    def _watch_corner(self):
        """Poll shared corner file for changes from other widgets."""
        # SH-widgetbar takes precedence: while a live bar publishes a slot
        # for us, sit in it and ignore corner cycling.
        bar = _read_bar()
        if bar is False:            # bar.json mid-write (bar being dragged): hold
            return True
        slot = (bar or {}).get("slots", {}).get(WIDGET_NAME)
        if slot:
            target = (int(slot[0]) - WIN_SIZE // 2, int(slot[1]) - WIN_SIZE // 2)
            if target != self._bar_target:
                self._bar_target = target
                self._close_panel()
                self.move(*target)
                try:
                    self.get_window().raise_()  # dots sit on top of the pill
                except Exception:
                    pass
            self._bar_rect = bar.get("rect")
            self._bar_orientation = bar.get("orientation", "horizontal")
            self._docked = True
            # Swallow corner changes while docked: the bar decides where we
            # are, and leaving it later must leave us where we stand.
            self._last_corner_ts = max(self._last_corner_ts,
                                       _read_corner()["timestamp"])
            return True
        self._docked = False
        self._bar_target = None
        corner = _read_corner()
        if corner["timestamp"] > self._last_corner_ts:
            self._last_corner_ts = corner["timestamp"]
            if corner["corner_index"] >= 0:
                self._apply_corner(corner["corner_index"])
        return True

    def _apply_corner(self, corner_index):
        """Move this widget to the given side, cycling across all monitors."""
        self._close_panel()
        widgets = _get_active_widgets()
        try:
            my_order = widgets.index(WIDGET_NAME)
        except ValueError:
            my_order = 0

        # build list of all positions: 2 sides per monitor
        display = Gdk.Display.get_default()
        n_mon = display.get_n_monitors()
        positions = []
        for i in range(n_mon):
            mon = display.get_monitor(i)
            geo = mon.get_geometry()
            positions.append((geo.x, geo.y, geo.width, geo.height))  # left
            positions.append((geo.x, geo.y, geo.width, geo.height))  # right

        idx = corner_index % len(positions)
        mx, my, sw, sh = positions[idx]
        is_right = idx % 2 == 1

        m = self._corner_margin
        n_widgets = len(widgets) if widgets else 1
        total_height = (n_widgets - 1) * STACK_GAP + WIN_SIZE
        center_y = my + (sh - total_height) // 2 + my_order * STACK_GAP
        if is_right:
            bx = mx + sw - WIN_SIZE - m
        else:
            bx = mx + m
        self.move(bx, center_y)

    def _show_endpoint_dialog(self):
        self._close_panel()
        dialog = Gtk.Dialog(
            title="Change Endpoint",
            transient_for=self,
            modal=True,
            destroy_with_parent=True,
        )
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Connect", Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)
        dialog.set_keep_above(True)

        content = dialog.get_content_area()
        content.set_spacing(8)
        content.set_margin_start(12)
        content.set_margin_end(12)
        content.set_margin_top(8)
        content.set_margin_bottom(4)

        host_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        host_label = Gtk.Label(label="Host:")
        host_label.set_width_chars(5)
        host_label.set_xalign(1)
        host_entry = Gtk.Entry()
        host_entry.set_text(self.host)
        host_entry.set_hexpand(True)
        host_box.pack_start(host_label, False, False, 0)
        host_box.pack_start(host_entry, True, True, 0)
        content.pack_start(host_box, False, False, 0)

        port_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        port_label = Gtk.Label(label="Port:")
        port_label.set_width_chars(5)
        port_label.set_xalign(1)
        port_entry = Gtk.Entry()
        port_entry.set_text(str(self.port))
        port_entry.set_hexpand(True)
        port_entry.set_activates_default(True)
        port_box.pack_start(port_label, False, False, 0)
        port_box.pack_start(port_entry, True, True, 0)
        content.pack_start(port_box, False, False, 0)

        dialog.show_all()
        response = dialog.run()

        if response == Gtk.ResponseType.OK:
            new_host = host_entry.get_text().strip()
            new_port_str = port_entry.get_text().strip()
            try:
                new_port = int(new_port_str)
            except ValueError:
                new_port = self.port
            if new_host:
                self.host = new_host
                self.port = new_port
                self.base_url = f"http://{self.host}:{self.port}"
                # sensors describe local hardware — re-evaluate for the new host
                self._sensors_local = _endpoint_is_local(self.host)
                self._cpu_temp_input = (
                    _find_cpu_temp_input() if self._sensors_local else None
                )
                self._gpu_sensor_cache = {}
                self._gpu_sensor_ts = 0.0
                self._cpu_sensor_cache = None
                self._cpu_sensor_ts = 0.0
                # reset to offline until next poll confirms connection
                self.data = self._empty_data()
                self._prev_state = None
                self.color = STATE_COLORS["offline"]
                self.queue_draw()
                self._ws.update_endpoint(self.host, self.port)

        dialog.destroy()

    def _on_enter(self, widget, event):
        if not self.dragging:
            self._show_panel()

    def _on_leave(self, widget, event):
        GLib.timeout_add(200, self._check_close_panel)

    def _show_panel(self):
        if self.panel and self.panel.get_visible():
            return
        if self.panel:
            self.panel.destroy()
        self.panel = PanelWindow(self)
        self.panel.update_data(self.data, self.base_url)
        x, y = self.get_position()
        screen = self.get_screen()
        sw, sh = screen.get_width(), screen.get_height()
        self.panel.show_all()
        pw = self.panel.get_allocated_width()
        ph = self.panel.get_allocated_height()
        if self._docked and self._bar_rect:
            px, py = _dock_panel_pos(self._bar_rect, self._bar_orientation, x, y,
                                     WIN_SIZE, pw, ph, 0, 0, sw, sh)
        else:
            px = x + WIN_SIZE + 6
            py = max(y, 4)
            if px + pw > sw:
                px = x - pw - 6
        self.panel.move(px, py)

    def _close_panel(self):
        if self.panel and self.panel.get_visible():
            self.panel.hide()

    def _check_close_panel(self):
        if not self.panel or not self.panel.get_visible():
            return False
        display = Gdk.Display.get_default()
        seat = display.get_default_seat()
        pointer = seat.get_pointer()
        _, mx, my = pointer.get_position()

        dx, dy = self.get_position()
        if dx <= mx <= dx + WIN_SIZE and dy <= my <= dy + WIN_SIZE:
            return False

        px, py = self.panel.get_position()
        pw = self.panel.get_allocated_width()
        ph = self.panel.get_allocated_height()
        if px <= mx <= px + pw and py <= my <= py + ph:
            return False

        self._close_panel()
        return False


class ToastWindow(Gtk.Window):
    """Small notification popup that auto-dismisses."""

    def __init__(self, parent_dot, message, color=None):
        super().__init__(type=Gtk.WindowType.POPUP)
        self.set_decorated(False)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_type_hint(Gdk.WindowTypeHint.NOTIFICATION)
        self.set_app_paintable(True)
        self.parent_dot = parent_dot
        self._opacity = 1.0
        self._color = color or (0.96, 0.96, 0.94)

        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual:
            self.set_visual(visual)

        self.connect("draw", self._on_draw)

        self._label = Gtk.Label()
        self._label.set_markup(
            f'<span font_family="JetBrains Mono" font_size="9000" '
            f'foreground="#{int(self._color[0]*255):02x}'
            f'{int(self._color[1]*255):02x}{int(self._color[2]*255):02x}">'
            f'{GLib.markup_escape_text(message)}</span>'
        )
        self._label.set_margin_start(12)
        self._label.set_margin_end(12)
        self._label.set_margin_top(8)
        self._label.set_margin_bottom(8)
        self.add(self._label)

    def popup(self, duration_ms=TOAST_DURATION_MS):
        self.show_all()
        x, y = self.parent_dot.get_position()
        toast_h = self.get_allocated_height()
        ty = y + (WIN_SIZE - toast_h) // 2
        self.move(x + WIN_SIZE + 8, ty)
        GLib.timeout_add(duration_ms, self._start_fade)

    def update_text(self, message):
        self._label.set_markup(
            f'<span font_family="JetBrains Mono" font_size="9000" '
            f'foreground="#{int(self._color[0]*255):02x}'
            f'{int(self._color[1]*255):02x}{int(self._color[2]*255):02x}">'
            f'{GLib.markup_escape_text(message)}</span>'
        )

    def _start_fade(self):
        GLib.timeout_add(30, self._fade_tick)
        return False

    def _fade_tick(self):
        self._opacity -= 0.06
        if self._opacity <= 0:
            self.destroy()
            return False
        self.queue_draw()
        return True

    def _on_draw(self, widget, cr):
        alloc = self.get_allocation()
        cr.set_source_rgba(0.08, 0.08, 0.08, 0.92 * self._opacity)
        self._rounded_rect(cr, 0, 0, alloc.width, alloc.height, 6)
        cr.fill()
        cr.set_source_rgba(0.17, 0.17, 0.17, self._opacity)
        cr.set_line_width(1)
        self._rounded_rect(cr, 0.5, 0.5, alloc.width - 1, alloc.height - 1, 6)
        cr.stroke()
        self._label.set_opacity(self._opacity)
        return False

    @staticmethod
    def _rounded_rect(cr, x, y, w, h, r):
        cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
        cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
        cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
        cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
        cr.close_path()


class PanelWindow(Gtk.Window):
    """The hover detail panel."""

    def __init__(self, parent):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.parent_dot = parent
        self.set_decorated(False)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        # TOOLTIP works on Linux; UTILITY is more reliable on Windows
        if IS_WINDOWS:
            self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        else:
            self.set_type_hint(Gdk.WindowTypeHint.TOOLTIP)
        self.set_resizable(False)

        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual:
            self.set_visual(visual)
        self.set_app_paintable(True)
        self.connect("draw", self._on_draw_bg)
        self.connect("leave-notify-event", self._on_leave)
        self.set_events(Gdk.EventMask.LEAVE_NOTIFY_MASK)

        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.box.set_margin_start(1)
        self.box.set_margin_end(1)
        self.box.set_margin_top(1)
        self.box.set_margin_bottom(1)

        self.inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.inner.set_margin_start(14)
        self.inner.set_margin_end(14)
        self.inner.set_margin_top(10)
        self.inner.set_margin_bottom(10)
        self.box.pack_start(self.inner, True, True, 0)
        self.add(self.box)

        self._apply_css()

    def _apply_css(self):
        css = b"""
        window { background-color: rgba(18,18,18,0.99); border: 1px solid #2a2a2a; }
        .panel-title { font-family: "Teko"; font-size: 18px; font-weight: bold; color: #F5F5F0; }
        .panel-section { font-family: "JetBrains Mono"; font-size: 8px; font-weight: bold; color: #8A8A80; }
        .row { background-color: rgba(26,26,26,0.9); border-radius: 4px; padding: 4px 10px; margin: 1px 0; }
        .row-label { font-family: "JetBrains Mono"; font-size: 11px; color: #8A8A80; }
        .row-value { font-family: "JetBrains Mono"; font-size: 11px; color: #F5F5F0; }
        .metric-label { font-family: "JetBrains Mono"; font-size: 7px; font-weight: bold; color: #6A6A64; }
        .footer { font-family: "Inter"; font-size: 8px; color: #5a5a54; }
        .sep { background-color: #2a2a2a; min-height: 1px; }
        .bar-bg { background-color: #1a1a1a; border-radius: 2px; min-height: 6px; }
        """
        provider = Gtk.CssProvider()
        provider.load_from_data(css)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

    def _on_draw_bg(self, widget, cr):
        alloc = self.get_allocation()
        cr.set_source_rgba(0.07, 0.07, 0.07, 0.99)
        cr.rectangle(0, 0, alloc.width, alloc.height)
        cr.fill()
        cr.set_source_rgba(0.17, 0.17, 0.17, 1)
        cr.set_line_width(1)
        cr.rectangle(0.5, 0.5, alloc.width - 1, alloc.height - 1)
        cr.stroke()
        return False

    def _on_leave(self, widget, event):
        GLib.timeout_add(200, self.parent_dot._check_close_panel)

    def update_data(self, data, base_url):
        for child in self.inner.get_children():
            self.inner.remove(child)

        state = data["state"]
        color = STATE_COLORS.get(state, STATE_COLORS["offline"])
        color_hex = "#{:02x}{:02x}{:02x}".format(
            int(color[0] * 255), int(color[1] * 255), int(color[2] * 255)
        )
        label = STATE_LABELS.get(state, "UNKNOWN")

        # -- header -----------------------------------------------------------
        title = Gtk.Label(label="COMFYUI STATUS")
        title.get_style_context().add_class("panel-title")
        title.set_halign(Gtk.Align.START)
        self.inner.pack_start(title, False, False, 0)

        badge = Gtk.Label()
        badge.set_markup(
            f'<span font_family="JetBrains Mono" font_size="7000" '
            f'font_weight="bold" background="{color_hex}" '
            f'foreground="#0D0D0D">  {label}  </span>'
        )
        badge.set_halign(Gtk.Align.START)
        self.inner.pack_start(badge, False, False, 2)

        self._add_sep()

        # -- queue section (side by side) -------------------------------------
        self._add_section("QUEUE")
        qrow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        qrow.get_style_context().add_class("row")
        for qlabel, qval in [("Running", str(data["running"])), ("Pending", str(data["pending"]))]:
            ql = Gtk.Label(label=qlabel)
            ql.get_style_context().add_class("row-label")
            ql.set_yalign(0.5)
            qv = Gtk.Label(label=qval)
            qv.get_style_context().add_class("row-value")
            qv.set_yalign(0.5)
            qrow.pack_start(ql, False, False, 4)
            qrow.pack_start(qv, False, False, 0)
            if qlabel == "Running":
                spacer = Gtk.Label(label="")
                spacer.set_hexpand(True)
                qrow.pack_start(spacer, True, True, 0)
        self.inner.pack_start(qrow, False, False, 0)

        if state == "offline":
            self._add_sep()
            self._add_endpoint_row(base_url)
            self._add_footer(data)
            self.show_all()
            return

        self._add_sep()

        # -- device sections — one per device the server reports ---------------
        devices = data.get("devices") or []
        for i, dev in enumerate(devices):
            if i:
                self._add_sep()

            # header names the device only when there is more than one
            if len(devices) > 1:
                self._add_section(f"{dev['label'].upper()}  ·  {_short_gpu_name(dev['name'])}")
            else:
                self._add_section(f"GPU  ·  {_short_gpu_name(dev['name'])}")

            vram_total = dev["vram_total"]
            vram_used = dev["vram_used"]
            if vram_total:
                self._add_bar_row(
                    "VRAM",
                    f"{fmt_bytes(vram_used)} / {fmt_bytes(vram_total)}"
                    f"  ({fmt_pct(vram_used, vram_total)})",
                    vram_used / vram_total,
                )

            torch_total = dev["torch_vram_total"]
            torch_used = dev["torch_vram_used"]
            if torch_total:
                self._add_bar_row(
                    "Torch VRAM",
                    f"{fmt_bytes(torch_used)} / {fmt_bytes(torch_total)}",
                    torch_used / torch_total,
                )
            # when the allocator hides torch's pool (cudaMallocAsync) the row is
            # simply omitted — the allocator is named in the footer instead

            # local telemetry — one compact row, each cell only when reported
            self._add_metrics_row([
                ("LOAD", f"{dev['util']:.0f}%") if dev.get("util") is not None else None,
                ("TEMP", fmt_temp(dev["temp"])) if dev.get("temp") is not None else None,
                ("POWER", f"{dev['power']:.0f}W") if dev.get("power") is not None else None,
                ("FAN", f"{dev['fan']:.0f}%") if dev.get("fan") is not None else None,
            ])

        # -- CPU section — only when local sensors reported ---------------------
        cpu = data.get("cpu") or {}
        ram_total = data["ram_total"]
        ram_used = ram_total - data["ram_free"]
        if cpu.get("temp") is not None or cpu.get("util") is not None:
            self._add_sep()
            self._add_section("CPU")
            self._add_metrics_row([
                ("LOAD", f"{cpu['util']:.0f}%") if cpu.get("util") is not None else None,
                ("TEMP", fmt_temp(cpu["temp"])) if cpu.get("temp") is not None else None,
                ("RAM", f"{fmt_pct(ram_used, ram_total)}") if ram_total else None,
            ])

        self._add_sep()

        # -- system info ------------------------------------------------------
        self._add_section("SYSTEM")

        if ram_total:
            self._add_bar_row(
                "RAM",
                f"{fmt_bytes(ram_used)} / {fmt_bytes(ram_total)}",
                ram_used / ram_total,
            )

        self._add_metrics_row([
            ("COMFYUI", data["comfyui_version"]),
            ("TORCH", data["pytorch_version"].split("+")[0]),
            ("PYTHON", data["python_version"]),
        ])
        self._add_endpoint_row(base_url)

        self._add_footer(data)
        self.show_all()

    # -- helpers ----------------------------------------------------------

    def _add_section(self, text):
        lbl = Gtk.Label(label=text)
        lbl.get_style_context().add_class("panel-section")
        lbl.set_halign(Gtk.Align.START)
        self.inner.pack_start(lbl, False, False, 2)

    def _add_sep(self):
        sep = Gtk.Separator()
        sep.get_style_context().add_class("sep")
        self.inner.pack_start(sep, False, False, 8)

    def _add_row(self, label, value, dim_value=False):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.get_style_context().add_class("row")

        l = Gtk.Label(label=label)
        l.get_style_context().add_class("row-label")
        l.set_halign(Gtk.Align.START)
        l.set_hexpand(True)
        l.set_xalign(0)
        l.set_yalign(0.5)
        row.pack_start(l, True, True, 0)

        v = Gtk.Label(label=value)
        if dim_value:
            v.get_style_context().add_class("row-label")
        else:
            v.get_style_context().add_class("row-value")
        v.set_halign(Gtk.Align.END)
        v.set_yalign(0.5)
        v.set_max_width_chars(32)
        v.set_ellipsize(3)  # PANGO_ELLIPSIZE_END
        row.pack_end(v, False, False, 0)

        self.inner.pack_start(row, False, False, 0)

    def _add_metrics_row(self, metrics):
        """Several short readings on one line: [(label, value), ...].

        Telemetry values are 3-6 characters, so a full-width row each wastes
        vertical space. Spread them evenly instead.
        """
        metrics = [m for m in metrics if m]
        if not metrics:
            return
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        row.get_style_context().add_class("row")
        row.set_homogeneous(True)

        for label, value in metrics:
            cell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            l = Gtk.Label(label=label)
            l.get_style_context().add_class("metric-label")
            l.set_xalign(0)
            v = Gtk.Label(label=value)
            v.get_style_context().add_class("row-value")
            v.set_xalign(0)
            cell.pack_start(l, False, False, 0)
            cell.pack_start(v, False, False, 0)
            row.pack_start(cell, True, True, 0)

        self.inner.pack_start(row, False, False, 0)

    def _add_endpoint_row(self, url):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.get_style_context().add_class("row")

        l = Gtk.Label(label="Endpoint")
        l.get_style_context().add_class("row-label")
        l.set_halign(Gtk.Align.START)
        l.set_hexpand(True)
        l.set_xalign(0)
        l.set_yalign(0.5)
        row.pack_start(l, True, True, 0)

        btn = Gtk.Button(label=url)
        btn.set_relief(Gtk.ReliefStyle.NONE)
        btn.get_child().get_style_context().add_class("row-label")
        btn.connect("clicked", lambda w: self.parent_dot._show_endpoint_dialog())
        row.pack_end(btn, False, False, 0)

        self.inner.pack_start(row, False, False, 0)

    def _add_bar_row(self, label, value_text, fraction):
        # one line: label | meter | value
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        row.get_style_context().add_class("row")

        l = Gtk.Label(label=label)
        l.get_style_context().add_class("row-label")
        l.set_xalign(0)
        l.set_size_request(84, -1)
        row.pack_start(l, False, False, 0)

        bar = Gtk.DrawingArea()
        bar.set_size_request(100, 10)
        bar.set_valign(Gtk.Align.CENTER)
        frac = max(0.0, min(1.0, fraction))
        bar.connect("draw", self._draw_bar, frac)
        row.pack_start(bar, True, True, 0)

        v = Gtk.Label(label=value_text)
        v.get_style_context().add_class("row-value")
        v.set_xalign(1)
        row.pack_end(v, False, False, 0)

        self.inner.pack_start(row, False, False, 0)

    @staticmethod
    def _draw_bar(widget, cr, fraction):
        alloc = widget.get_allocation()
        w, h = alloc.width, alloc.height
        r = h / 2

        def pill(width):
            cr.new_path()
            cr.arc(r, r, r, math.pi / 2, 3 * math.pi / 2)
            cr.arc(max(r, width - r), r, r, 3 * math.pi / 2, math.pi / 2)
            cr.close_path()

        cr.set_source_rgb(0.118, 0.106, 0.090)   # track: surface 3
        pill(w)
        cr.fill()
        if fraction < 0.6:
            cr.set_source_rgb(0.427, 0.643, 0.416)
        elif fraction < 0.85:
            cr.set_source_rgb(0.851, 0.647, 0.239)
        else:
            cr.set_source_rgb(0.812, 0.325, 0.278)
        if fraction > 0:
            pill(max(h, w * fraction))
            cr.fill()
        return False

    def _add_footer(self, data):
        self._add_sep()
        check_str = ""
        if data["last_check"]:
            check_str = f"Last checked: {data['last_check'].strftime('%H:%M:%S UTC')}"
        footer = Gtk.Label(label=f"{check_str}   ·   Right-click: menu")
        footer.get_style_context().add_class("footer")
        footer.set_halign(Gtk.Align.START)
        self.inner.pack_start(footer, False, False, 0)


def main():
    parser = argparse.ArgumentParser(description="ComfyUI Status Checker")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"ComfyUI host (default: {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"ComfyUI port (default: {DEFAULT_PORT})")
    args = parser.parse_args()

    dot = DotWindow(args.host, args.port)
    dot.show_all()
    Gtk.main()


if __name__ == "__main__":
    main()
