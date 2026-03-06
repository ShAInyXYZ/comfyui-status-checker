#!/usr/bin/python3
"""
ComfyUI Status Checker — always-on-top circular indicator for ComfyUI instance health.

Uses GTK3 + Cairo for true RGBA transparency (no square background).
Hover to see full status panel with queue, GPU/VRAM, and system info.
Drag to reposition. Right-click to quit.
"""

import argparse
import base64
import json
import math
import os
import platform
import re
import socket
import struct
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


def _register_widget(name):
    _ensure_widget_dir()
    path = os.path.join(WIDGET_DIR, f"{name}.json")
    with open(path, "w") as f:
        json.dump({"pid": os.getpid(), "name": name}, f)


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


# -- defaults -------------------------------------------------------------
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8188
POLL_SECS = 2  # fast polling for generation status

# -- design tokens --------------------------------------------------------
BG_PANEL = (0.08, 0.08, 0.08)
BG_ROW = (0.10, 0.10, 0.10)
FG = (0.96, 0.96, 0.94)
FG_DIM = (0.54, 0.54, 0.50)
BORDER_CLR = (0.17, 0.17, 0.17)

DOT_RADIUS = 14
RING_RADIUS = 18
GLOW_RADIUS = 22
WIN_SIZE = GLOW_RADIUS * 2 + 8

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
COMFYUI_LOGO_PATH = (
    "M5.485 23.76c-.568 0-1.026-.207-1.325-.598-.307-.402-.387-.964-.22"
    "-1.54l.672-2.315a.605.605 0 00-.1-.536.622.622 0 00-.494-.243H2.085"
    "c-.568 0-1.026-.207-1.325-.598-.307-.403-.387-.964-.22-1.54l2.31"
    "-7.917.255-.87c.343-1.18 1.592-2.14 2.786-2.14h2.313c.276 0 .519"
    "-.18.595-.442l.764-2.633C9.906 1.208 11.155.249 12.35.249l4.945-.008"
    "h3.62c.568 0 1.027.206 1.325.597.307.402.387.964.22 1.54l-1.035"
    " 3.566c-.343 1.178-1.593 2.137-2.787 2.137l-4.956.01H11.37a.618.618"
    " 0 00-.594.441l-1.928 6.604a.605.605 0 00.1.537c.118.153.3.243.495"
    ".243l3.275-.006h3.61c.568 0 1.026.206 1.325.598.307.402.387.964.22"
    " 1.54l-1.036 3.565c-.342 1.179-1.592 2.138-2.786 2.138l-4.957.01"
    "h-3.61z"
)


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

        # widget coordination
        _register_widget(WIDGET_NAME)
        self._last_corner_ts = 0  # track corner file changes
        self.connect("destroy", lambda w: _unregister_widget(WIDGET_NAME))

        # websocket for generation progress
        self._ws = _ComfyWS(host, port, self._on_ws_progress, self._on_ws_complete)
        self._ws.start()
        self.connect("destroy", lambda w: self._ws.stop())

        # pulse timer (20fps)
        GLib.timeout_add(50, self._tick_pulse)
        # watch shared corner file (5 checks/sec)
        GLib.timeout_add(200, self._watch_corner)
        # poll thread
        threading.Thread(target=self._poll_loop, daemon=True).start()

    @staticmethod
    def _empty_data():
        return {
            "state": "offline",
            "running": 0,
            "pending": 0,
            "gpu_name": "—",
            "vram_total": 0,
            "vram_used": 0,
            "torch_vram_total": 0,
            "torch_vram_used": 0,
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
        cr.set_operator(0)  # CLEAR
        cr.paint()
        cr.set_operator(2)  # OVER

        cx = WIN_SIZE / 2
        cy = WIN_SIZE / 2
        r, g, b = self.color
        state = self.data["state"]

        # glow — faster pulse when generating
        speed = 2.0 if state == "generating" else 1.0
        pulse = 0.3 + 0.7 * (0.5 + 0.5 * math.sin(self.pulse_phase * speed))
        cr.set_source_rgba(r, g, b, 0.15 * pulse)
        cr.arc(cx, cy, GLOW_RADIUS, 0, 2 * math.pi)
        cr.fill()

        # ring
        cr.set_source_rgba(r, g, b, 0.5)
        cr.set_line_width(1.5)
        cr.arc(cx, cy, RING_RADIUS, 0, 2 * math.pi)
        cr.stroke()

        # main dot
        cr.set_source_rgb(r, g, b)
        cr.arc(cx, cy, DOT_RADIUS, 0, 2 * math.pi)
        cr.fill()

        # ComfyUI logo inside the dot
        cr.save()
        cr.arc(cx, cy, DOT_RADIUS, 0, 2 * math.pi)
        cr.clip()
        logo_size = DOT_RADIUS * 1.4
        cr.set_source_rgba(0.05, 0.05, 0.05, 0.7)
        draw_svg_logo(cr, COMFYUI_LOGO_PATH, cx, cy, logo_size)
        cr.fill()
        cr.restore()

        # progress arc overlay when generating
        if state == "generating":
            # spinning arc to indicate activity
            angle = self.pulse_phase * 3.0
            cr.set_source_rgba(1, 1, 1, 0.25)
            cr.set_line_width(2.5)
            cr.arc(cx, cy, DOT_RADIUS - 3, angle, angle + math.pi * 0.6)
            cr.stroke()

        # specular highlight
        cr.set_source_rgba(1, 1, 1, 0.12)
        cr.arc(cx - 3, cy - 3, DOT_RADIUS * 0.35, 0, 2 * math.pi)
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

            devices = sys_data.get("devices", [])
            if devices:
                dev = devices[0]
                data["gpu_name"] = dev.get("name", "—").split(" : ")[0]
                data["vram_total"] = dev.get("vram_total", 0)
                data["vram_used"] = dev.get("vram_total", 0) - dev.get("vram_free", 0)
                data["torch_vram_total"] = dev.get("torch_vram_total", 0)
                data["torch_vram_used"] = dev.get("torch_vram_total", 0) - dev.get("torch_vram_free", 0)

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
            self.destroy()

    def _on_button_release(self, widget, event):
        if event.button == 1:
            self.dragging = False
            self._drag_moved = False

    def _on_motion(self, widget, event):
        if self.dragging:
            self._drag_moved = True
            self.move(
                int(event.x_root) - self._drag_offset_x,
                int(event.y_root) - self._drag_offset_y,
            )

    def _on_key_press(self, widget, event):
        if event.keyval == Gdk.KEY_grave:  # ~ / ` key
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
        px = x + WIN_SIZE + 6
        py = y
        screen = self.get_screen()
        sw = screen.get_width()
        self.panel.show_all()
        pw = self.panel.get_allocated_width()
        if px + pw > sw:
            px = x - pw - 6
        self.panel.move(px, max(py, 4))

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
        self.inner.set_margin_start(16)
        self.inner.set_margin_end(16)
        self.inner.set_margin_top(12)
        self.inner.set_margin_bottom(12)
        self.box.pack_start(self.inner, True, True, 0)
        self.add(self.box)

        self._apply_css()

    def _apply_css(self):
        css = b"""
        window { background-color: rgba(20,20,20,0.96); border: 1px solid #2a2a2a; }
        .panel-title { font-family: "Teko"; font-size: 18px; font-weight: bold; color: #F5F5F0; }
        .panel-section { font-family: "JetBrains Mono"; font-size: 8px; font-weight: bold; color: #8A8A80; }
        .row { background-color: rgba(26,26,26,0.9); border-radius: 4px; padding: 6px 10px; margin: 1px 0; }
        .row-label { font-family: "JetBrains Mono"; font-size: 12px; color: #8A8A80; }
        .row-value { font-family: "JetBrains Mono"; font-size: 12px; color: #F5F5F0; }
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
        cr.set_source_rgba(0.08, 0.08, 0.08, 0.96)
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

        # -- GPU / VRAM section -----------------------------------------------
        self._add_section("GPU")
        self._add_row("Device", data["gpu_name"])

        vram_total = data["vram_total"]
        vram_used = data["vram_used"]
        if vram_total:
            self._add_bar_row(
                "VRAM",
                f"{fmt_bytes(vram_used)} / {fmt_bytes(vram_total)}",
                vram_used / vram_total,
            )

        torch_total = data["torch_vram_total"]
        torch_used = data["torch_vram_used"]
        if torch_total:
            self._add_bar_row(
                "Torch VRAM",
                f"{fmt_bytes(torch_used)} / {fmt_bytes(torch_total)}",
                torch_used / torch_total,
            )

        ram_total = data["ram_total"]
        ram_used = ram_total - data["ram_free"]
        if ram_total:
            self._add_bar_row(
                "System RAM",
                f"{fmt_bytes(ram_used)} / {fmt_bytes(ram_total)}",
                ram_used / ram_total,
            )

        self._add_sep()

        # -- system info ------------------------------------------------------
        self._add_section("SYSTEM")
        self._add_row("ComfyUI", data["comfyui_version"])
        self._add_row("PyTorch", data["pytorch_version"])
        self._add_row("Python", data["python_version"])
        self._add_endpoint_row(base_url)

        self._add_footer(data)
        self.show_all()

    # -- helpers ----------------------------------------------------------

    def _add_section(self, text):
        lbl = Gtk.Label(label=text)
        lbl.get_style_context().add_class("panel-section")
        lbl.set_halign(Gtk.Align.START)
        self.inner.pack_start(lbl, False, False, 4)

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
        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        row.get_style_context().add_class("row")

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        l = Gtk.Label(label=label)
        l.get_style_context().add_class("row-label")
        l.set_halign(Gtk.Align.START)
        top.pack_start(l, True, True, 0)

        v = Gtk.Label(label=value_text)
        v.get_style_context().add_class("row-value")
        v.set_halign(Gtk.Align.END)
        top.pack_end(v, False, False, 0)
        row.pack_start(top, False, False, 0)

        # progress bar via DrawingArea
        bar = Gtk.DrawingArea()
        bar.set_size_request(-1, 6)
        frac = max(0.0, min(1.0, fraction))
        bar.connect("draw", self._draw_bar, frac)
        row.pack_start(bar, False, False, 0)

        self.inner.pack_start(row, False, False, 0)

    @staticmethod
    def _draw_bar(widget, cr, fraction):
        alloc = widget.get_allocation()
        w, h = alloc.width, alloc.height

        # background
        cr.set_source_rgb(0.12, 0.12, 0.12)
        cr.rectangle(0, 0, w, h)
        cr.fill()

        # filled portion — color based on usage
        if fraction < 0.6:
            cr.set_source_rgb(0.13, 0.77, 0.37)  # green
        elif fraction < 0.85:
            cr.set_source_rgb(0.92, 0.70, 0.03)  # yellow
        else:
            cr.set_source_rgb(0.94, 0.27, 0.27)  # red

        cr.rectangle(0, 0, w * fraction, h)
        cr.fill()
        return False

    def _add_footer(self, data):
        self._add_sep()
        check_str = ""
        if data["last_check"]:
            check_str = f"Last checked: {data['last_check'].strftime('%H:%M:%S UTC')}"
        footer = Gtk.Label(label=f"{check_str}   ·   Right-click dot to quit")
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
