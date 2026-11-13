#!/usr/bin/env python3
"""
Local HTTP clipboard mailbox server for ~200ms zero-popup iPhone text/Emoji pasting.

Based on ESP32 iPhone Agent (https://github.com/onehumanbeing/esp32-iphone-agent) by Henry Yuan
Copyright (c) 2026 Henry Yuan. Licensed under the MIT License (see LICENSE and NOTICE).
"""

from __future__ import annotations

import errno
import html
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import socket
import subprocess
import threading
import time
from urllib.parse import urlparse


def detect_lan_ip() -> str:
    """Probe the active LAN interface IP without transmitting packets."""
    for target in (("192.168.1.1", 80), ("8.8.8.8", 80)):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(target)
            ip = sock.getsockname()[0]
            if ip and ip != "0.0.0.0" and not ip.startswith("127."):
                return ip
        except OSError:
            pass
        finally:
            sock.close()
    return "127.0.0.1"


def copy_to_macos_pbcopy(text: str) -> bool:
    """Copy UTF-8 text into the macOS system clipboard via /usr/bin/pbcopy."""
    try:
        proc = subprocess.run(
            ["/usr/bin/pbcopy"],
            input=text.encode("utf-8"),
            check=True,
            timeout=3.0,
        )
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


class _ReuseThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], handler_cls, mailbox: ClipboardMailboxServer):
        self.mailbox = mailbox
        super().__init__(server_address, handler_cls)


class _ClipboardHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        return

    def address_string(self) -> str:
        return self.client_address[0]

    def _send_bytes(self, status: int, content_type: str, body: bytes, extra_headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        self.close_connection = True

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, "application/json; charset=utf-8", body, {"Cache-Control": "no-store"})

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        mb: ClipboardMailboxServer = self.server.mailbox  # type: ignore[attr-defined]

        if path == "/clip":
            text, gen = mb._snapshot()
            body = text.encode("utf-8")
            self._send_bytes(
                200,
                "text/plain; charset=utf-8",
                body,
                {"Cache-Control": "no-store"},
            )
            mb._mark_fetched(gen)
            return

        if path == "/health":
            self._send_json(200, mb._health_payload())
            return

        if path == "/setup":
            clip_escaped = html.escape(mb.clip_url)
            page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AgentPaste Setup</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; margin: 20px; line-height: 1.5; color: #111; }}
  .card {{ max-width: 560px; margin: auto; padding: 18px; border: 1px solid #ddd; border-radius: 12px; }}
  code {{ display: block; padding: 10px 12px; background: #f4f4f6; border-radius: 8px; word-break: break-all; font-size: 15px; }}
  button {{ margin-top: 8px; padding: 8px 14px; font-size: 15px; border-radius: 8px; border: 0; background: #007aff; color: #fff; }}
</style>
</head>
<body>
<div class="card">
  <h2>AgentPaste Shortcut Setup</h2>
  <p>Copy this clipboard mailbox URL on your iPhone:</p>
  <code id="url">{clip_escaped}</code>
  <button onclick="navigator.clipboard && navigator.clipboard.writeText(document.getElementById('url').textContent)">Copy URL</button>
  <ol>
    <li>Open <b>Shortcuts</b> on iPhone and create a shortcut named <b>AgentPaste</b> with 2 actions:<br>
        <b>Get Contents of URL</b> (<code>{clip_escaped}</code>) &rarr; <b>Copy to Clipboard</b> (enable <b>Local Only</b>).</li>
    <li>Open <b>Settings &rarr; Accessibility &rarr; Touch &rarr; AssistiveTouch &rarr; Devices</b>, pick the ESP32 mouse, and assign <b>Button 3</b> to <b>AgentPaste</b>.</li>
    <li>Run the shortcut once so iOS allows local network access to your Mac without future prompts.</li>
  </ol>
</div>
</body>
</html>
"""
            self._send_bytes(200, "text/html; charset=utf-8", page.encode("utf-8"), {"Cache-Control": "no-store"})
            return

        self._send_json(404, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path != "/clip":
            self._send_json(404, {"ok": False, "error": "not_found"})
            return

        if self.client_address[0] not in ("127.0.0.1", "::1"):
            self._send_json(403, {"ok": False, "error": "localhost_only"})
            return

        raw_len = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_len)
        except ValueError:
            self._send_json(400, {"ok": False, "error": "invalid_content_length"})
            return
        if length < 0 or length > 16 * 1024 * 1024:
            self._send_json(400, {"ok": False, "error": "invalid_content_length"})
            return

        raw_body = self.rfile.read(length) if length > 0 else b""
        try:
            text = raw_body.decode("utf-8")
        except UnicodeDecodeError:
            self._send_json(400, {"ok": False, "error": "invalid_utf8"})
            return

        mb: ClipboardMailboxServer = self.server.mailbox  # type: ignore[attr-defined]
        gen = mb.stage(text)
        self._send_json(200, {"ok": True, "gen": gen})


class ClipboardMailboxServer:
    """Thread-safe HTTP clipboard server with generation-tracked fetch confirmation."""

    def __init__(self, host: str = "0.0.0.0", port: int | None = None) -> None:
        bind_port = int(os.environ.get("IPHONE_CLIP_PORT", "8766")) if port is None else int(port)
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._text: str = ""
        self._gen: int = 0
        self._fetched_gen: int = 0
        self._fetches: int = 0
        self._closed: bool = False
        self._lan_ip: str = detect_lan_ip()
        self._server = _ReuseThreadingHTTPServer((host, bind_port), _ClipboardHandler, self)
        self._thread = threading.Thread(
            target=lambda: self._server.serve_forever(poll_interval=0.05),
            name="clipboard-http",
            daemon=True,
        )
        self._thread.start()

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def lan_ip(self) -> str:
        return self._lan_ip

    @property
    def clip_url(self) -> str:
        return f"http://{self._lan_ip}:{self.port}/clip"

    @property
    def setup_url(self) -> str:
        return f"http://{self._lan_ip}:{self.port}/setup"

    def stage(self, text: str) -> int:
        """Store UTF-8 text and advance generation counter."""
        with self._cond:
            self._text = text
            self._gen += 1
            return self._gen

    def wait_fetched(self, gen: int, timeout: float = 0.85) -> bool:
        """Block until GET /clip flushes generation >= gen or timeout expires."""
        deadline = time.monotonic() + max(0.0, timeout)
        with self._cond:
            while self._fetched_gen < gen:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(timeout=remaining)
            return True

    def _snapshot(self) -> tuple[str, int]:
        with self._cond:
            return self._text, self._gen

    def _mark_fetched(self, gen: int) -> None:
        with self._cond:
            self._fetches += 1
            if gen > self._fetched_gen:
                self._fetched_gen = gen
            self._cond.notify_all()

    def _health_payload(self) -> dict:
        with self._cond:
            gen = self._gen
            fetched_gen = self._fetched_gen
            fetches = self._fetches
        return {
            "ok": True,
            "port": self.port,
            "lan_ip": self.lan_ip,
            "clip_url": self.clip_url,
            "setup_url": self.setup_url,
            "gen": gen,
            "fetched_gen": fetched_gen,
            "fetches": fetches,
        }

    def close(self) -> None:
        """Shut down the HTTP server and wait for the server thread to exit."""
        if self._closed:
            return
        self._closed = True
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2.0)


class _RemoteClipboardClient:
    """HTTP proxy to an already-running ClipboardMailboxServer in another process."""

    def __init__(self, health: dict) -> None:
        self._port = int(health["port"])
        self._lan_ip = str(health["lan_ip"])
        self._clip_url = str(health["clip_url"])
        self._setup_url = str(health["setup_url"])

    @property
    def port(self) -> int:
        return self._port

    @property
    def lan_ip(self) -> str:
        return self._lan_ip

    @property
    def clip_url(self) -> str:
        return self._clip_url

    @property
    def setup_url(self) -> str:
        return self._setup_url

    def _request_json(self, method: str, path: str, body: bytes | None = None) -> dict:
        conn = http.client.HTTPConnection("127.0.0.1", self._port, timeout=1.5)
        try:
            headers = {"Content-Length": str(len(body))} if body is not None else {}
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            if resp.status != 200:
                raise RuntimeError(f"Clipboard mailbox {method} {path} returned HTTP {resp.status}")
            return json.loads(raw.decode("utf-8"))
        finally:
            conn.close()

    def stage(self, text: str) -> int:
        """Stage UTF-8 text on the running local mailbox server via POST /clip."""
        res = self._request_json("POST", "/clip", body=text.encode("utf-8"))
        return int(res["gen"])

    def wait_fetched(self, gen: int, timeout: float = 0.85) -> bool:
        """Poll GET /health every 25ms until fetched_gen >= gen or timeout expires."""
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                health = self._request_json("GET", "/health")
                if int(health.get("fetched_gen", 0)) >= gen:
                    return True
            except (OSError, ValueError, RuntimeError):
                pass
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.025, remaining))

    def close(self) -> None:
        """No-op for cross-process client proxy (server stays running)."""
        return


def _probe_remote_health(bind_port: int) -> dict | None:
    conn = http.client.HTTPConnection("127.0.0.1", bind_port, timeout=1.0)
    try:
        conn.request("GET", "/health")
        resp = conn.getresponse()
        if resp.status != 200:
            return None
        data = json.loads(resp.read().decode("utf-8"))
        return data if data.get("ok") is True else None
    except (OSError, ValueError):
        return None
    finally:
        conn.close()


_SERVER_LOCK = threading.Lock()
_SERVER_SINGLETON: ClipboardMailboxServer | None = None


def get_clipboard_server(port: int | None = None) -> ClipboardMailboxServer | _RemoteClipboardClient:
    """Return the process-wide ClipboardMailboxServer singleton, or proxy to an existing server."""
    global _SERVER_SINGLETON
    with _SERVER_LOCK:
        if _SERVER_SINGLETON is None or _SERVER_SINGLETON._closed:
            bind_port = int(os.environ.get("IPHONE_CLIP_PORT", "8766")) if port is None else int(port)
            try:
                _SERVER_SINGLETON = ClipboardMailboxServer(port=port)
            except OSError as exc:
                if exc.errno == errno.EADDRINUSE and bind_port > 0:
                    health = _probe_remote_health(bind_port)
                    if health is not None:
                        return _RemoteClipboardClient(health)
                raise
        return _SERVER_SINGLETON
