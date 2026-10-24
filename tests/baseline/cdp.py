"""A very small Chrome DevTools Protocol client, stdlib only.

Why not websocket-client: the capture has to run with the repo's own Python
(`.venv/bin/python3`, which has Pillow) and must not grow a dependency for one
screenshot pass. This is the RFC 6455 client half: a masked text frame out, a
frame reader in, and the JSON-RPC shape CDP actually uses.

Used by capture.py; not a general-purpose library.
"""
from __future__ import annotations

import base64
import json
import os
import socket
import struct
import subprocess
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class CDPError(RuntimeError):
    pass


class WebSocket:
    def __init__(self, url: str, timeout: float = 30.0):
        u = urlparse(url)
        self.sock = socket.create_connection((u.hostname, u.port or 80), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = u.path + (("?" + u.query) if u.query else "")
        req = (
            "GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n" % (path, u.hostname, u.port or 80, key)
        )
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise CDPError("websocket handshake closed")
            buf += chunk
        head, self._rest = buf.split(b"\r\n\r\n", 1)
        if b"101" not in head.split(b"\r\n")[0]:
            raise CDPError("websocket handshake failed: %r" % head[:200])
        self._next_id = 0

    # ---------------------------------------------------------------- framing
    def _send_frame(self, payload: bytes, opcode: int = 0x1) -> None:
        header = bytearray([0x80 | opcode])
        n = len(payload)
        mask = os.urandom(4)
        if n < 126:
            header.append(0x80 | n)
        elif n < (1 << 16):
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def _read_exact(self, n: int) -> bytes:
        out = self._rest[:n]
        self._rest = self._rest[n:]
        while len(out) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise CDPError("socket closed mid-frame")
            need = n - len(out)
            out += chunk[:need]
            self._rest += chunk[need:]
        return out

    def _read_frame(self) -> tuple[bool, int, bytes]:
        b1, b2 = self._read_exact(2)
        fin = bool(b1 & 0x80)
        opcode = b1 & 0x0F
        masked = bool(b2 & 0x80)
        n = b2 & 0x7F
        if n == 126:
            n = struct.unpack(">H", self._read_exact(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self._read_exact(8))[0]
        mask = self._read_exact(4) if masked else None
        data = self._read_exact(n) if n else b""
        if mask:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        return fin, opcode, data

    def _recv_message(self) -> str:
        chunks = []
        while True:
            fin, opcode, data = self._read_frame()
            if opcode == 0x8:
                raise CDPError("websocket closed by peer")
            if opcode == 0x9:            # ping -> pong
                self._send_frame(data, opcode=0xA)
                continue
            if opcode in (0x1, 0x2, 0x0):
                chunks.append(data)
                if fin:
                    return b"".join(chunks).decode("utf-8", "replace")

    # ------------------------------------------------------------------- rpc
    def call(self, method: str, session_id: str | None = None, **params):
        self._next_id += 1
        mid = self._next_id
        msg = {"id": mid, "method": method, "params": params}
        if session_id:
            msg["sessionId"] = session_id
        self._send_frame(json.dumps(msg).encode())
        while True:
            msg = json.loads(self._recv_message())
            if msg.get("id") != mid:
                continue          # an event; this client does not subscribe
            if "error" in msg:
                raise CDPError("%s: %s" % (method, msg["error"]))
            return msg.get("result", {})

    def close(self) -> None:
        try:
            self._send_frame(b"", opcode=0x8)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def find_chrome() -> Path:
    env = os.environ.get("HWA_CHROME")
    if env and Path(env).is_file():
        return Path(env)
    base = Path.home() / ".hermes/tools/chromium-1208/chrome-linux64/chrome"
    if base.is_file():
        return base
    raise SystemExit("no chrome binary found; set HWA_CHROME")


def launch(profile_dir: Path, width: int = 1440, height: int = 900) -> tuple[subprocess.Popen, WebSocket]:
    """Start headless Chrome and attach to its first page target."""
    import shutil
    if profile_dir.exists():
        shutil.rmtree(profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen([
        str(find_chrome()), "--headless=new", "--no-sandbox", "--disable-gpu",
        "--disable-dev-shm-usage", "--hide-scrollbars", "--mute-audio",
        "--no-first-run", "--no-default-browser-check",
        "--force-device-scale-factor=1", "--disable-lcd-text",
        "--user-data-dir=%s" % profile_dir, "--remote-debugging-port=0",
        "--window-size=%d,%d" % (width, height), "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

    active = profile_dir / "DevToolsActivePort"
    deadline = time.time() + 30
    port = None
    while time.time() < deadline:
        if active.is_file():
            try:
                port = int(active.read_text().splitlines()[0].strip())
                break
            except (ValueError, IndexError):
                pass
        if proc.poll() is not None:
            raise SystemExit("chrome exited before opening a debugging port")
        time.sleep(0.1)
    if port is None:
        raise SystemExit("chrome did not write DevToolsActivePort")

    # Attach to the BROWSER endpoint, not the first page: each capture scope
    # gets a brand new tab. A tab carries its own sessionStorage, and the Shorts
    # page keeps its sitting ("which clips have I shown") there -- reusing a tab
    # makes the feed shrink load by load and the screenshot would show "1 / 2"
    # for an archive that holds three clips.
    with OPENER.open("http://127.0.0.1:%d/json/version" % port, timeout=5) as r:
        info = json.loads(r.read())
    ws = WebSocket(info["webSocketDebuggerUrl"])
    return proc, ws


def new_page(ws: WebSocket, url: str = "about:blank") -> tuple[str, str]:
    """A fresh tab: its own sessionStorage, localStorage and renderer."""
    target_id = ws.call("Target.createTarget", url=url)["targetId"]
    session_id = ws.call("Target.attachToTarget", targetId=target_id,
                         flatten=True)["sessionId"]
    ws.call("Page.enable", session_id=session_id)
    ws.call("Runtime.enable", session_id=session_id)
    return session_id, target_id


def close_page(ws: WebSocket, target_id: str) -> None:
    try:
        ws.call("Target.closeTarget", targetId=target_id)
    except CDPError:
        pass


def kill(proc: subprocess.Popen) -> None:
    import signal
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        proc.wait(timeout=5)
