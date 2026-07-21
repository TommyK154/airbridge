"""AirBridge system tray entry point for Windows.

Runs the AirBridge server in a background thread, controlled from a system
tray icon. Left-click shows a QR code popup; right-click opens a menu with
start/stop, browser shortcut, and Windows login autostart toggles.

Run with: uv run --extra tray tray.py (or tray_run.bat). On startup the
process respawns itself detached from the launching console and exits, so no
terminal window lingers and closing the launcher cannot kill the tray. Pass
--foreground to skip that and keep the console (for debugging). main.py
remains the headless terminal entry point.

As a frozen (PyInstaller) app there is no console and no respawn: the server
autostarts on launch, the QR popup opens automatically on the very first run,
and the login toggle registers the exe itself.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
import winreg
from ctypes import wintypes
from pathlib import Path
from tkinter import messagebox

import pystray
import qrcode
import uvicorn
from PIL import Image, ImageDraw, ImageTk

import main as airbridge

BASE_DIR = Path(__file__).parent
FROZEN = airbridge.FROZEN
CONFIG_PATH = airbridge.DATA_DIR / "config.json"
LOG_PATH = airbridge.DATA_DIR / "tray.log"
TRAY_BAT = BASE_DIR / "tray_run.bat"

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "AirBridge"

# Set in the respawned child so it knows it is already detached.
DETACHED_ENV = "AIRBRIDGE_TRAY_DETACHED"

# The login checkmarks read the registry directly (see login_entry_exists),
# so the config file only stores what the registry cannot express. It also
# doubles as the first-run marker for the frozen app.
DEFAULT_CONFIG = {
    "start_server_on_login": False,
}


# --------------------------------------------------------------------------- #
# Persistent tray settings (.airbridge/config.json)
# --------------------------------------------------------------------------- #
def load_config() -> dict:
    """Return the tray settings, falling back to defaults on any problem."""
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    merged = dict(data)  # preserve unknown keys for forward compatibility
    for key, default in DEFAULT_CONFIG.items():
        merged[key] = bool(data.get(key, default))
    return merged


def save_config(config: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8"
    )


# --------------------------------------------------------------------------- #
# Windows login autostart via HKCU\...\CurrentVersion\Run
# --------------------------------------------------------------------------- #
def set_run_value(command: str) -> None:
    # CreateKeyEx opens the key or creates it: a fresh user profile can
    # legitimately lack the Run key entirely.
    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
    ) as key:
        winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, command)


def delete_run_value() -> None:
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, RUN_VALUE)
    except FileNotFoundError:
        pass


def login_entry_exists() -> bool:
    """True when the HKCU Run value is present.

    The registry is the source of truth for the login checkmark, so the tray
    stays consistent with an entry created by the installer or removed by hand.
    """
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, RUN_VALUE)
        return True
    except FileNotFoundError:
        return False


def server_cli_flags(args: argparse.Namespace) -> list[str]:
    """Reproduce the current server options as a plain argument list."""
    flags = []
    if args.port is not None:
        flags += ["--port", str(args.port)]
    flags += ["--dir", str(args.dir)]
    if args.host != "0.0.0.0":
        flags += ["--host", args.host]
    if args.no_auth:
        flags.append("--no-auth")
    if args.max_mb:
        flags += ["--max-mb", str(args.max_mb)]
    if args.https:
        flags.append("--https")
    return flags


def login_command(args: argparse.Namespace, start_server: bool) -> str:
    """Build the registry Run command.

    Frozen: just the exe (it autostarts the server itself). From source:
    tray_run.bat plus the current flags.
    """
    if FROZEN:
        return subprocess.list2cmdline([sys.executable])
    parts = [str(TRAY_BAT)]
    if start_server:
        parts.append("--autostart")
    parts += server_cli_flags(args)
    return subprocess.list2cmdline(parts)


def find_free_port(host: str, preferred: int, attempts: int = 10) -> int:
    """Return preferred, or the next port after it that accepts a bind.

    Used only when the user did not choose a port, so the app just works even
    if something else occupies 8080. Falls back to preferred when the whole
    range is busy, letting the server produce its normal startup error.
    """
    for offset in range(attempts):
        port = preferred + offset
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                # Without exclusive use, a plain bind on Windows can succeed
                # against a port another process is already listening on,
                # making the probe lie. Listen to fully claim the port.
                if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                probe.bind((host, port))
                probe.listen(1)
        except OSError as exc:
            print(f"[tray] port {port} busy ({exc}), trying next", flush=True)
            continue
        return port
    return preferred


# --------------------------------------------------------------------------- #
# Server lifecycle: uvicorn in a daemon thread, reusing the app from main.py
# --------------------------------------------------------------------------- #
class ServerController:
    """Start and stop the AirBridge uvicorn server on a background thread."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.server: uvicorn.Server | None = None
        self.thread: threading.Thread | None = None
        self.url = ""  # full URL including the token query, for QR and browser
        self.base = ""  # scheme://ip:port, for display
        self.port = 0  # resolved on start

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self) -> None:
        """Configure main.cfg, launch uvicorn, and wait until it is listening.

        Raises RuntimeError with a user-facing message on any failure.
        """
        if self.running:
            return
        args = self.args
        if args.port is not None:
            # An explicitly chosen port is honored as-is; if it is busy the
            # startup failure below reports it.
            self.port = args.port
        else:
            preferred = int(os.environ.get("AIRBRIDGE_PORT", "8080"))
            self.port = find_free_port(args.host, preferred)
        print(
            f"[tray] using port {self.port} (requested: {args.port})", flush=True
        )
        cfg = airbridge.cfg
        cfg.shared_dir = Path(args.dir).expanduser().resolve()
        cfg.shared_dir.mkdir(parents=True, exist_ok=True)
        cfg.auth_enabled = not args.no_auth
        cfg.token = None if args.no_auth else secrets.token_urlsafe(9)
        cfg.max_mb = max(0, args.max_mb)
        cfg.max_bytes = cfg.max_mb * 1024 * 1024

        ip = airbridge.get_lan_ip()
        ssl_kwargs: dict[str, str] = {}
        scheme = "http"
        if args.https:
            airbridge.DATA_DIR.mkdir(parents=True, exist_ok=True)
            cert_path = airbridge.DATA_DIR / "cert.pem"
            key_path = airbridge.DATA_DIR / "key.pem"
            try:
                airbridge.ensure_self_signed(ip, cert_path, key_path)
            except SystemExit as exc:
                # ensure_self_signed exits when the tls extra is missing;
                # surface that as a dialog instead of killing the tray.
                raise RuntimeError(str(exc)) from None
            ssl_kwargs = {
                "ssl_certfile": str(cert_path),
                "ssl_keyfile": str(key_path),
            }
            scheme = "https"

        self.base = f"{scheme}://{ip}:{self.port}"
        self.url = self.base + (f"/?t={cfg.token}" if cfg.auth_enabled else "/")

        config = uvicorn.Config(
            airbridge.app,
            host=args.host,
            port=self.port,
            log_level="warning",
            **ssl_kwargs,
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(
            target=self.server.run, name="airbridge-server", daemon=True
        )
        self.thread.start()

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.server.started:
                return
            if not self.thread.is_alive():
                raise RuntimeError(
                    f"The server failed to start. Is port {self.port} already "
                    f"in use? Details may be in {LOG_PATH}"
                )
            time.sleep(0.1)
        raise RuntimeError("The server did not start within 10 seconds.")

    def stop(self) -> None:
        """Ask uvicorn to shut down gracefully, forcing it after a timeout."""
        if not self.running:
            return
        assert self.server is not None and self.thread is not None
        self.server.should_exit = True
        self.thread.join(timeout=10)
        if self.thread.is_alive():
            self.server.force_exit = True
            self.thread.join(timeout=5)


# --------------------------------------------------------------------------- #
# Images: tray icon and QR code, both drawn with Pillow (no asset files)
# --------------------------------------------------------------------------- #
def make_icon(running: bool, size: int = 64) -> Image.Image:
    """Draw the tray icon: a dark tile with up and down transfer arrows.

    The up arrow is green while the server runs and gray while stopped. The
    drawing is defined on a 64px grid and scaled, so the same function renders
    the runtime tray icon and the high-resolution .ico for the installer.
    """
    s = size / 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(
        [2 * s, 2 * s, 62 * s, 62 * s], radius=14 * s, fill=(14, 18, 22, 255)
    )
    accent = (63, 185, 80, 255) if running else (110, 118, 129, 255)
    white = (230, 237, 243, 255)
    # Up arrow (left): head then shaft.
    draw.polygon([(22 * s, 12 * s), (10 * s, 28 * s), (34 * s, 28 * s)], fill=accent)
    draw.rectangle([18 * s, 28 * s, 26 * s, 50 * s], fill=accent)
    # Down arrow (right): shaft then head.
    draw.rectangle([38 * s, 14 * s, 46 * s, 36 * s], fill=white)
    draw.polygon([(30 * s, 36 * s), (54 * s, 36 * s), (42 * s, 52 * s)], fill=white)
    return img


def make_qr_image(url: str) -> Image.Image:
    """Render the connect URL as a QR code PIL image."""
    qr = qrcode.QRCode(border=2)
    qr.add_data(url)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    scale = max(6, 360 // len(matrix))
    dim = len(matrix) * scale
    img = Image.new("RGB", (dim, dim), "white")
    draw = ImageDraw.Draw(img)
    for row_idx, row in enumerate(matrix):
        for col_idx, filled in enumerate(row):
            if filled:
                draw.rectangle(
                    [
                        col_idx * scale,
                        row_idx * scale,
                        (col_idx + 1) * scale - 1,
                        (row_idx + 1) * scale - 1,
                    ],
                    fill="black",
                )
    return img


# --------------------------------------------------------------------------- #
# QR popup placement: pure geometry plus Win32 cursor / work-area adapters
# --------------------------------------------------------------------------- #
MONITOR_DEFAULTTONEAREST = 2
SPI_GETWORKAREA = 0x0030


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", RECT),
        ("rcWork", RECT),
        ("dwFlags", wintypes.DWORD),
    ]


def compute_popup_position(
    win_w: int,
    win_h: int,
    work_area: tuple[int, int, int, int],
    anchor: tuple[int, int] | None = None,
    margin: int = 12,
) -> tuple[int, int]:
    """Place a win_w by win_h popup inside work_area, returning its top-left.

    work_area is (left, top, right, bottom) in virtual-screen coordinates and
    may have a negative origin. With an anchor (for example the cursor) the
    popup is laid up and to the left of it; with anchor None it sits in the
    margin-inset bottom-right corner. Pure and deterministic: no I/O, no
    ctypes, no tkinter. Each axis uses the same independent logic.
    """

    def place(size: int, lo: int, hi: int, anchor_pt: int | None) -> int:
        if anchor_pt is not None:
            target = anchor_pt - margin - size
        else:
            target = hi - margin - size
        avail = hi - lo
        if size >= avail:
            # Cannot fit: pin to the origin so we never overflow further out.
            return lo
        if size + 2 * margin <= avail:
            # Room for the full margin band on this axis.
            return max(lo + margin, min(target, hi - margin - size))
        # Fits, but not with two margins: let the margins collapse, stay inside.
        return max(lo, min(target, hi - size))

    left, top, right, bottom = work_area
    anchor_x = anchor[0] if anchor is not None else None
    anchor_y = anchor[1] if anchor is not None else None
    x = place(win_w, left, right, anchor_x)
    y = place(win_h, top, bottom, anchor_y)
    return (int(x), int(y))


def get_cursor_pos() -> tuple[int, int] | None:
    """Return the mouse position as (x, y) in virtual-screen coordinates.

    None off Windows or if the Win32 call fails, never raising.
    """
    try:
        user32 = ctypes.windll.user32
        user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
        user32.GetCursorPos.restype = wintypes.BOOL
        point = POINT()
        if not user32.GetCursorPos(ctypes.byref(point)):
            return None
        return (int(point.x), int(point.y))
    except Exception:
        return None


def get_work_area(
    point: tuple[int, int] | None = None,
) -> tuple[int, int, int, int] | None:
    """Return a usable work area (l, t, r, b), taskbars excluded.

    With a point, resolve the nearest monitor under it and return that
    monitor's work area, so the popup lands on the cursor's screen. Without a
    point, or if the per-monitor path fails, fall back to the primary monitor
    via SystemParametersInfoW. None off Windows or if both paths fail, never
    raising.
    """
    try:
        user32 = ctypes.windll.user32
    except Exception:
        return None

    if point is not None:
        try:
            user32.MonitorFromPoint.argtypes = [POINT, wintypes.DWORD]
            user32.MonitorFromPoint.restype = wintypes.HMONITOR
            user32.GetMonitorInfoW.argtypes = [
                wintypes.HMONITOR,
                ctypes.POINTER(MONITORINFO),
            ]
            user32.GetMonitorInfoW.restype = wintypes.BOOL
            monitor = user32.MonitorFromPoint(
                POINT(point[0], point[1]), MONITOR_DEFAULTTONEAREST
            )
            if monitor:
                info = MONITORINFO()
                info.cbSize = ctypes.sizeof(MONITORINFO)
                if user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                    r = info.rcWork
                    return (int(r.left), int(r.top), int(r.right), int(r.bottom))
        except Exception:
            pass  # fall through to the primary-monitor work area

    try:
        user32.SystemParametersInfoW.argtypes = [
            wintypes.UINT,
            wintypes.UINT,
            ctypes.c_void_p,
            wintypes.UINT,
        ]
        user32.SystemParametersInfoW.restype = wintypes.BOOL
        work = RECT()
        if not user32.SystemParametersInfoW(
            SPI_GETWORKAREA, 0, ctypes.byref(work), 0
        ):
            return None
        return (int(work.left), int(work.top), int(work.right), int(work.bottom))
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Tray application: pystray runs detached, tkinter owns the main thread
# --------------------------------------------------------------------------- #
class TrayApp:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.config = load_config()
        self.controller = ServerController(args)
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("AirBridge")
        self.qr_window: tk.Toplevel | None = None
        self.icon = pystray.Icon(
            "AirBridge",
            icon=make_icon(running=False),
            title="AirBridge (stopped)",
            menu=self._build_menu(),
        )

    # ---- threading helpers ------------------------------------------------ #
    def _on_ui(self, func, *func_args) -> None:
        """Run func on the tkinter main thread (menu handlers run elsewhere)."""
        self.root.after(0, lambda: func(*func_args))

    def _refresh(self) -> None:
        running = self.controller.running
        self.icon.icon = make_icon(running)
        self.icon.title = (
            f"AirBridge ({self.controller.base})" if running else "AirBridge (stopped)"
        )
        self.icon.update_menu()

    # ---- menu ------------------------------------------------------------- #
    def _build_menu(self) -> pystray.Menu:
        if FROZEN:
            # The frozen app always autostarts the server, so one toggle
            # covers "run at login" without a tray-vs-server distinction.
            login_items = (
                pystray.MenuItem(
                    "Run AirBridge at Windows login",
                    self._on_toggle_login_frozen,
                    checked=lambda item: login_entry_exists(),
                ),
            )
        else:
            login_items = (
                pystray.MenuItem(
                    "Start server on Windows login",
                    self._on_toggle_server_login,
                    checked=lambda item: self.config["start_server_on_login"],
                ),
                pystray.MenuItem(
                    "Launch tray on Windows login",
                    self._on_toggle_tray_login,
                    checked=lambda item: login_entry_exists(),
                ),
            )
        return pystray.Menu(
            pystray.MenuItem(
                "Start Server",
                self._on_start,
                enabled=lambda item: not self.controller.running,
            ),
            pystray.MenuItem(
                "Stop Server",
                self._on_stop,
                enabled=lambda item: self.controller.running,
            ),
            pystray.MenuItem("Show QR", self._on_show_qr, default=True),
            pystray.MenuItem(
                "Open in Browser",
                self._on_open_browser,
                enabled=lambda item: self.controller.running,
            ),
            pystray.MenuItem("Open Shared Folder", self._on_open_folder),
            pystray.Menu.SEPARATOR,
            *login_items,
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", self._on_exit),
        )

    # ---- server start/stop ------------------------------------------------ #
    def _on_start(self, icon=None, item=None) -> None:
        threading.Thread(target=self._start_server, daemon=True).start()

    def _start_server(self, show_qr: bool = False) -> None:
        try:
            self.controller.start()
        except Exception as exc:
            self._on_ui(messagebox.showerror, "AirBridge", str(exc))
            self._refresh()
            return
        airbridge.print_banner(
            self.controller.url, self.controller.base, self.controller.port
        )
        self._refresh()
        self.icon.notify(f"Server running at {self.controller.base}", "AirBridge")
        if show_qr:
            self._on_ui(self._show_qr, False)

    def _on_stop(self, icon=None, item=None) -> None:
        threading.Thread(target=self._stop_server, daemon=True).start()

    def _stop_server(self) -> None:
        self.controller.stop()
        self._refresh()
        self.icon.notify("Server stopped", "AirBridge")

    # ---- QR popup and browser --------------------------------------------- #
    def _on_show_qr(self, icon=None, item=None) -> None:
        self._on_ui(self._show_qr)

    def _position_qr_window(self, win: tk.Toplevel, use_cursor: bool) -> None:
        """Move win to its computed spot near the cursor (or bottom-right).

        Falls back to a full-screen rect when the Win32 work area is
        unavailable, so placement still stays on screen.
        """
        win.update_idletasks()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        anchor = get_cursor_pos() if use_cursor else None
        wa = get_work_area(anchor)
        if wa is None:
            wa = (0, 0, win.winfo_screenwidth(), win.winfo_screenheight())
        x, y = compute_popup_position(w, h, wa, anchor)
        win.geometry(f"+{x}+{y}")

    def _show_qr(self, use_cursor: bool = True) -> None:
        if self.qr_window is not None and self.qr_window.winfo_exists():
            self.qr_window.destroy()  # rebuild: the URL changes per start
        if not self.controller.running:
            messagebox.showinfo(
                "AirBridge",
                "The server is not running.\n"
                "Right-click the tray icon and choose Start Server first.",
                parent=self.root,
            )
            return

        url = self.controller.url
        win = tk.Toplevel(self.root)
        win.withdraw()  # stay hidden until positioned, so it cannot flash at the cascade spot
        win.title("AirBridge: scan to connect")
        win.resizable(False, False)
        win.attributes("-topmost", True)
        self.qr_window = win

        photo = ImageTk.PhotoImage(make_qr_image(url))
        label = tk.Label(win, image=photo, background="white")
        label.image = photo  # keep a reference or tkinter drops the image
        label.pack(padx=16, pady=(16, 8))

        tk.Label(win, text="Scan with the phone camera, or open:").pack()
        url_box = tk.Entry(win, width=min(len(url) + 2, 60), justify="center")
        url_box.insert(0, url)
        url_box.configure(state="readonly")
        url_box.pack(padx=16, pady=(4, 8), fill="x")

        def copy_url() -> None:
            self.root.clipboard_clear()
            self.root.clipboard_append(url)

        tk.Button(win, text="Copy URL", command=copy_url).pack(pady=(0, 12))

        self._position_qr_window(win, use_cursor)
        win.deiconify()

    def _on_open_browser(self, icon=None, item=None) -> None:
        if self.controller.running:
            webbrowser.open(self.controller.url)

    def _on_open_folder(self, icon=None, item=None) -> None:
        folder = Path(self.args.dir).expanduser()
        try:
            folder.mkdir(parents=True, exist_ok=True)
            os.startfile(folder)
        except OSError as exc:
            self._on_ui(
                messagebox.showerror,
                "AirBridge",
                f"Could not open the shared folder: {exc}",
            )

    # ---- login autostart toggles ------------------------------------------ #
    def _on_toggle_login_frozen(self, icon=None, item=None) -> None:
        """Single frozen-mode toggle: register or unregister the exe."""
        self._registry_update(remove=login_entry_exists(), start_server=True)

    def _on_toggle_server_login(self, icon=None, item=None) -> None:
        turned_on = not self.config["start_server_on_login"]
        self.config["start_server_on_login"] = turned_on
        # Starting the server at login requires launching the tray at login,
        # so turning this on ensures the Run entry; turning it off keeps the
        # entry (tray still launches) but drops the --autostart flag.
        self._registry_update(
            ensure=turned_on, start_server=turned_on, save=True
        )

    def _on_toggle_tray_login(self, icon=None, item=None) -> None:
        if login_entry_exists():
            # No tray process at login means no server at login either.
            self.config["start_server_on_login"] = False
            self._registry_update(remove=True, start_server=False, save=True)
        else:
            self._registry_update(
                ensure=True,
                start_server=self.config["start_server_on_login"],
                save=True,
            )

    def _registry_update(
        self,
        *,
        ensure: bool = False,
        remove: bool = False,
        start_server: bool = False,
        save: bool = False,
    ) -> None:
        """Apply a toggle: persist config and mirror it into the Run key.

        The Run value is rewritten whenever it should exist so the stored
        command always reflects the current flags and toggles.
        """
        try:
            if save:
                save_config(self.config)
            if remove:
                delete_run_value()
            elif ensure or login_entry_exists():
                set_run_value(login_command(self.args, start_server))
        except OSError as exc:
            self._on_ui(
                messagebox.showerror,
                "AirBridge",
                f"Could not update the login setting: {exc}",
            )
        self.icon.update_menu()

    # ---- exit --------------------------------------------------------------- #
    def _on_exit(self, icon=None, item=None) -> None:
        threading.Thread(target=self._exit, daemon=True).start()

    def _exit(self) -> None:
        self.controller.stop()
        self.icon.stop()
        self._on_ui(self.root.destroy)

    # ---- main loop ---------------------------------------------------------- #
    def run(self) -> None:
        if FROZEN:
            # The installed app just works: the server starts on launch, and
            # the very first run (no config file yet) pops the QR code so the
            # phone can connect with zero clicks.
            first_run = not CONFIG_PATH.exists()
            if first_run:
                save_config(self.config)
            threading.Thread(
                target=self._start_server,
                kwargs={"show_qr": first_run},
                daemon=True,
            ).start()
        elif self.args.autostart:
            threading.Thread(target=self._start_server, daemon=True).start()
        self.icon.run_detached()
        self.root.mainloop()


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def relaunch_detached(args: argparse.Namespace) -> bool:
    """Respawn this script hidden from any console, then have the caller exit.

    The venv's pythonw.exe is a uv trampoline built as a console program, so
    launching it still opens a terminal window that owns the tray (closing the
    window kills the icon). Instead, the first process respawns tray.py with a
    hidden console and exits. CREATE_NO_WINDOW rather than DETACHED_PROCESS:
    the trampoline chain spawns console-subsystem children, which with no
    console at all would allocate a fresh visible one; a hidden console is
    inherited by the whole chain. Returns True when the caller should exit.
    """
    if FROZEN or args.foreground or os.environ.get(DETACHED_ENV) == "1":
        # A frozen windowed exe has no console; nothing to detach from.
        return False
    cmd = [sys.executable, str(Path(__file__).resolve())]
    if args.autostart:
        cmd.append("--autostart")
    cmd += server_cli_flags(args)
    env = dict(os.environ)
    env[DETACHED_ENV] = "1"
    base_flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    # Try to break out of the launcher's job object first, so closing a VS Code
    # or Windows Terminal tab cannot reap the tray; some jobs forbid breakaway,
    # in which case retry without it.
    for extra in (subprocess.CREATE_BREAKAWAY_FROM_JOB, 0):
        try:
            subprocess.Popen(
                cmd,
                creationflags=base_flags | extra,
                env=env,
                cwd=str(BASE_DIR),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except OSError:
            if extra == 0:
                raise
    return True


def redirect_output_to_log() -> None:
    """Point stdout/stderr at the data-dir tray.log when there is no console.

    Three windowless cases: the detached child gets DEVNULL std handles (not
    None), pythonw gives None handles, and a frozen windowed exe gets
    PyInstaller's NullWriter objects (also not None). In all of them transfer
    logs, tracebacks, and uvicorn warnings would otherwise vanish.
    """
    windowless = FROZEN or os.environ.get(DETACHED_ENV) == "1"
    if not windowless and sys.stdout is not None and sys.stderr is not None:
        return
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
    if windowless or sys.stdout is None:
        sys.stdout = log
    if windowless or sys.stderr is None:
        sys.stderr = log


def parse_args() -> argparse.Namespace:
    """Mirror main.py's server flags, plus --autostart for the login entry."""
    parser = argparse.ArgumentParser(description="AirBridge system tray launcher")
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port to listen on (default 8080 or AIRBRIDGE_PORT, falling "
        "back to the next free port when that one is busy)",
    )
    parser.add_argument(
        "--dir",
        default=os.environ.get(
            "AIRBRIDGE_DIR", str(airbridge.DEFAULT_SHARED_DIR)
        ),
        help="Shared folder for transferred files",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Bind address (default 0.0.0.0, all interfaces)",
    )
    parser.add_argument(
        "--no-auth",
        action="store_true",
        help="Disable the access token (anyone on the LAN can connect)",
    )
    parser.add_argument(
        "--max-mb",
        type=int,
        default=0,
        help="Per-file upload size cap in MB (default 0, meaning unlimited)",
    )
    parser.add_argument(
        "--https",
        action="store_true",
        help="Serve over HTTPS with a cached self-signed cert (needs the 'tls' extra)",
    )
    parser.add_argument(
        "--autostart",
        action="store_true",
        help="Start the server immediately (used by the Windows login entry)",
    )
    parser.add_argument(
        "--foreground",
        action="store_true",
        help="Stay attached to the console instead of detaching (for debugging)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if relaunch_detached(args):
        return
    redirect_output_to_log()
    TrayApp(args).run()


if __name__ == "__main__":
    main()
