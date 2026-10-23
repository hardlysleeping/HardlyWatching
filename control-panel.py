#!/usr/bin/env python3
"""HardlyWatching -- control panel window.

A small window that shows whether the app is running, the address to open it
at, and buttons to open / copy the link and to start, stop or restart the
server. Closing this window does NOT stop the server -- it keeps running in
the background, and you can reopen this panel any time.

    ./control-panel.sh          (or: .venv/bin/python3 control-panel.py)

Draws with GTK4 when PyGObject is there, otherwise with Qt when PySide6 is in
the project's venv, otherwise with a plain terminal prompt. Qt is installed
into the venv because an immutable distro cannot be given PyGObject without
layering packages and rebooting -- Bazzite's system python has neither, so the
window used to fall back to a terminal prompt.
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PORT = int(os.environ.get("INSTA_PORT", "8765"))
HOST = os.environ.get("INSTA_HOST", "127.0.0.1")
URL = f"http://{'127.0.0.1' if HOST in ('0.0.0.0', '::') else HOST}:{PORT}"
PID_FILE = HERE / ".server.pid"
LOG_FILE = HERE / ".server.log"


# ---------- server process control ----------

def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def read_pid() -> int | None:
    try:
        pid = int(PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if _pid_alive(pid) else None


def port_open() -> bool:
    """True when something answers on the port (even if we did not start it,
    e.g. it is running as a systemd service)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.6)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def running() -> bool:
    return port_open() or read_pid() is not None


def _service_active() -> bool:
    if not shutil.which("systemctl"):
        return False
    r = subprocess.run(["systemctl", "--user", "is-active", "hardlywatching"],
                       capture_output=True, text=True)
    return r.stdout.strip() == "active"


def start_server() -> tuple[bool, str]:
    if running():
        return True, "Already running."
    log = open(LOG_FILE, "a")
    try:
        p = subprocess.Popen(
            [sys.executable, str(HERE / "server.py")],
            cwd=str(HERE), stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as e:
        return False, f"Could not start: {e}"
    PID_FILE.write_text(str(p.pid))
    for _ in range(20):
        if port_open():
            return True, "Started."
        time.sleep(0.3)
    return True, "Starting (not answering yet)."


def stop_server() -> tuple[bool, str]:
    if _service_active():
        subprocess.run(["systemctl", "--user", "stop", "hardlywatching"],
                       capture_output=True, text=True)
        return True, "Service stopped."
    pid = read_pid()
    if pid is None:
        return True, "Not running."
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as e:
        return False, f"Could not stop: {e}"
    for _ in range(20):
        if not _pid_alive(pid):
            break
        time.sleep(0.2)
    try:
        PID_FILE.unlink()
    except OSError:
        pass
    return True, "Stopped."


def restart_server() -> tuple[bool, str]:
    if _service_active():
        subprocess.run(["systemctl", "--user", "restart", "hardlywatching"],
                       capture_output=True, text=True)
        return True, "Service restarted."
    stop_server()
    time.sleep(0.5)
    return start_server()


def copy_to_clipboard(text: str) -> bool:
    for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"]):
        if shutil.which(cmd[0]):
            try:
                subprocess.run(cmd, input=text, text=True, timeout=5)
                return True
            except (OSError, subprocess.SubprocessError):
                continue
    return False


def open_in_browser() -> None:
    try:
        subprocess.Popen(["xdg-open", URL], start_new_session=True)
    except OSError:
        pass


# ---------- GTK window ----------

class Panel:
    def __init__(self, Gtk, GLib):
        self.Gtk = Gtk
        self.GLib = GLib

    def build(self, app):
        Gtk = self.Gtk
        win = Gtk.ApplicationWindow(application=app)
        win.set_title("HardlyWatching")
        win.set_default_size(380, 240)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{m}")(16)
        win.set_child(box)

        self.state = Gtk.Label()
        self.detail = Gtk.Label(wrap=True)
        self.detail.add_css_class("dim-label")
        box.append(self.state)
        box.append(self.detail)

        link_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.open_btn = Gtk.Button(label="Open in browser")
        self.open_btn.connect("clicked", self.on_open)
        self.copy_btn = Gtk.Button(label="Copy link")
        self.copy_btn.connect("clicked", self.on_copy)
        link_box.append(self.open_btn)
        link_box.append(self.copy_btn)
        box.append(link_box)

        ctrl_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.start_btn = Gtk.Button(label="Start")
        self.start_btn.connect("clicked", self.on_start)
        self.stop_btn = Gtk.Button(label="Stop")
        self.stop_btn.connect("clicked", self.on_stop)
        self.restart_btn = Gtk.Button(label="Restart")
        self.restart_btn.connect("clicked", self.on_restart)
        for b in (self.start_btn, self.stop_btn, self.restart_btn):
            ctrl_box.append(b)
        box.append(ctrl_box)

        self.status = Gtk.Label()
        self.status.add_css_class("dim-label")
        box.append(self.status)

        note = Gtk.Label(label="Closing this window keeps it running.")
        note.add_css_class("dim-label")
        box.append(note)

        self.refresh()
        self.GLib.timeout_add_seconds(2, self.refresh)
        win.present()

    def refresh(self):
        is_up = running()
        self.state.set_markup(
            "<span foreground='#2ec27e' weight='bold'>\u25cf Running</span>"
            if is_up else
            "<span foreground='#e01b24' weight='bold'>\u25cf Stopped</span>")
        self.detail.set_text(URL if is_up else "The app is not running.")
        self.open_btn.set_sensitive(is_up)
        self.copy_btn.set_sensitive(is_up)
        self.start_btn.set_sensitive(not is_up)
        self.stop_btn.set_sensitive(is_up)
        self.restart_btn.set_sensitive(is_up)
        return False

    def on_open(self, *_):
        if not running():
            self.status.set_text("Not running -- press Start first.")
            return
        open_in_browser()

    def on_copy(self, *_):
        self.status.set_text("Link copied." if copy_to_clipboard(URL)
                             else "Copy failed.")

    def on_start(self, *_):
        self.status.set_text(start_server()[1])
        self.GLib.timeout_add(600, self.refresh)

    def on_stop(self, *_):
        self.status.set_text(stop_server()[1])
        self.GLib.timeout_add(600, self.refresh)

    def on_restart(self, *_):
        self.status.set_text(restart_server()[1])
        self.GLib.timeout_add(600, self.refresh)


# ---------- Qt window ----------

def run_qt() -> int | None:
    """The panel in Qt. Returns None when PySide6 is not installed."""
    try:
        from PySide6.QtCore import QTimer
        from PySide6.QtGui import QIcon
        from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel,
                                       QPushButton, QVBoxLayout, QWidget)
    except Exception:
        return None

    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("HardlyWatching")
    app.setDesktopFileName("hardlywatching")
    icon_path = HERE / "icon-192.png"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    win = QWidget()
    win.setWindowTitle("HardlyWatching")
    win.resize(400, 260)

    box = QVBoxLayout(win)
    box.setContentsMargins(16, 16, 16, 16)
    box.setSpacing(10)

    dim = "color: palette(mid);"
    state = QLabel()
    detail = QLabel()
    detail.setWordWrap(True)
    detail.setStyleSheet(dim)
    box.addWidget(state)
    box.addWidget(detail)

    def new_button(text):
        return QPushButton(text)

    link_row = QHBoxLayout()
    open_btn, copy_btn = new_button("Open in browser"), new_button("Copy link")
    link_row.addWidget(open_btn)
    link_row.addWidget(copy_btn)
    box.addLayout(link_row)

    ctrl_row = QHBoxLayout()
    start_btn, stop_btn, restart_btn = new_button("Start"), new_button("Stop"), new_button("Restart")
    for b in (start_btn, stop_btn, restart_btn):
        ctrl_row.addWidget(b)
    box.addLayout(ctrl_row)

    status = QLabel()
    status.setStyleSheet(dim)
    note = QLabel("Closing this window keeps the app running.")
    note.setStyleSheet(dim)
    box.addWidget(status)
    box.addWidget(note)
    box.addStretch(1)

    def refresh():
        up = running()
        state.setText(
            '<span style="color:#2ec27e;font-weight:600">&#9679; Running</span>'
            if up else
            '<span style="color:#e01b24;font-weight:600">&#9679; Stopped</span>')
        detail.setText(URL if up else "The app is not running.")
        open_btn.setEnabled(up)
        copy_btn.setEnabled(up)
        start_btn.setEnabled(not up)
        stop_btn.setEnabled(up)
        restart_btn.setEnabled(up)
        return False

    def on_open():
        if not running():
            status.setText("Not running -- press Start first.")
            return
        open_in_browser()

    def on_copy():
        # Qt's own clipboard first: wl-copy is not on every desktop, and the
        # toolkit already owns the selection.
        try:
            app.clipboard().setText(URL)
            status.setText("Link copied.")
        except Exception:
            status.setText("Link copied." if copy_to_clipboard(URL) else "Copy failed.")

    def on_start():
        status.setText(start_server()[1])
        QTimer.singleShot(600, refresh)

    def on_stop():
        status.setText(stop_server()[1])
        QTimer.singleShot(600, refresh)

    def on_restart():
        status.setText(restart_server()[1])
        QTimer.singleShot(600, refresh)

    open_btn.clicked.connect(on_open)
    copy_btn.clicked.connect(on_copy)
    start_btn.clicked.connect(on_start)
    stop_btn.clicked.connect(on_stop)
    restart_btn.clicked.connect(on_restart)

    # The app can be started from elsewhere (a desktop click on Start, the
    # systemd unit coming up), so the window watches instead of trusting the
    # state it drew last.
    timer = QTimer(win)
    timer.timeout.connect(refresh)
    timer.start(2000)

    refresh()
    win.show()
    return app.exec()


def _restart_under_venv() -> None:
    """Re-launch on the project's venv python, which is where the GUI lives.

    A click on the menu icon can land on the system python, which on an immutable
    distro has neither PyGObject nor PySide6 and cannot be given one without
    layering packages and rebooting. The venv's python has PySide6, so hand
    ourselves over to it once; HW_PANEL_VENV stops that from looping.
    """
    if os.environ.get("HW_PANEL_VENV") == "1":
        return
    venv_py = HERE / ".venv" / "bin" / "python3"
    try:
        if not venv_py.exists():
            return
        if Path(sys.executable).resolve() == venv_py.resolve():
            return
    except OSError:
        return
    env = dict(os.environ, HW_PANEL_VENV="1")
    try:
        os.execve(str(venv_py),
                  [str(venv_py), str(HERE / "control-panel.py"), *sys.argv[1:]], env)
    except OSError:
        return


def run_gui() -> int:
    _restart_under_venv()
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk, GLib
    except Exception:
        qt = run_qt()
        if qt is not None:
            return qt
        return run_terminal_fallback()

    app = Gtk.Application(application_id="ing.hardlysleep.hardlywatching")
    panel = Panel(Gtk, GLib)

    def on_activate(a):
        panel.build(a)

    app.connect("activate", on_activate)
    return app.run([])


def run_terminal_fallback() -> int:
    print("HardlyWatching")
    print("  status:", "running" if running() else "stopped")
    print("  address:", URL)
    print()
    print("Commands: start | stop | restart | status | open | quit")
    while True:
        try:
            cmd = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if cmd in ("quit", "q", "exit"):
            return 0
        elif cmd == "start":
            print(start_server()[1])
        elif cmd == "stop":
            print(stop_server()[1])
        elif cmd == "restart":
            print(restart_server()[1])
        elif cmd == "status":
            print("running" if running() else "stopped")
        elif cmd == "open":
            open_in_browser() if running() else print("not running")
        else:
            print("unknown command")


if __name__ == "__main__":
    sys.exit(run_gui())
