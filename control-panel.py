#!/usr/bin/env python3
"""Instagram Downloader -- control panel window.

A small window that shows whether the app is running, the address to open it
at, and buttons to open / copy the link and to start, stop or restart the
server. Closing this window does NOT stop the server -- it keeps running in
the background, and you can reopen this panel any time.

    python3 control-panel.py

Uses GTK (PyGObject), already present on GNOME desktops. Falls back to a
plain terminal prompt if GTK is unavailable.
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
    r = subprocess.run(["systemctl", "--user", "is-active", "insta-downloader"],
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
        subprocess.run(["systemctl", "--user", "stop", "insta-downloader"],
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
        subprocess.run(["systemctl", "--user", "restart", "insta-downloader"],
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
        win.set_title("Instagram Downloader")
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


def run_gui() -> int:
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk, GLib
    except Exception:
        return run_terminal_fallback()

    app = Gtk.Application(application_id="ing.hardlysleep.insta-downloader")
    panel = Panel(Gtk, GLib)

    def on_activate(a):
        panel.build(a)

    app.connect("activate", on_activate)
    return app.run([])


def run_terminal_fallback() -> int:
    print("Instagram Downloader")
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
