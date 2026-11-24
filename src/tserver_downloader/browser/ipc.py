"""Native-messaging / localhost IPC protocol.

Chrome native messaging framing: every message is a 4-byte little-endian
length prefix followed by a UTF-8 JSON payload. The same framing is used
for the localhost TCP IPC server so the extension and the desktop app
share one protocol.

Security: the TCP server binds 127.0.0.1 only, rejects any peer that is
not a loopback address, and requires a per-process token handshake.
Tokens and cookies are never logged.
"""
from __future__ import annotations

import json
import os
import secrets
import socket
import struct
import threading
from pathlib import Path
from urllib.parse import urlparse

APP_NAME = "TServer Downloader"
APP_VERSION = "1.0.0"
MAX_MESSAGE_BYTES = 1024 * 1024  # 1 MiB, same cap Chrome applies


def read_message(stream):
    """Read one length-prefixed JSON message from a binary stream.

    Returns the decoded object, or ``None`` on clean EOF.
    Raises ValueError / EOFError on truncated or oversized messages.
    """
    header = stream.read(4)
    if not header:
        return None
    if len(header) < 4:
        raise EOFError("truncated native message header")
    (length,) = struct.unpack("<I", header)
    if length > MAX_MESSAGE_BYTES:
        raise ValueError("native message too large: %d bytes" % length)
    payload = stream.read(length) if length else b""
    if len(payload) < length:
        raise EOFError("truncated native message body")
    if not payload:
        return None
    return json.loads(payload.decode("utf-8"))


def write_message(stream, obj):
    """Write one length-prefixed JSON message to a binary stream."""
    payload = json.dumps(obj).encode("utf-8")
    if len(payload) > MAX_MESSAGE_BYTES:
        raise ValueError("native message too large: %d bytes" % len(payload))
    stream.write(struct.pack("<I", len(payload)))
    stream.write(payload)
    stream.flush()


def generate_token():
    """Generate a fresh per-process IPC auth token."""
    return secrets.token_urlsafe(32)


def _is_safe_filename(filename):
    if not isinstance(filename, str) or not filename:
        return False
    if filename != filename.strip():
        return False
    if "/" in filename or "\\" in filename:
        return False
    if ".." in filename:
        return False
    if filename in (".", ".."):
        return False
    # Reject absolute paths / drive letters / NUL.
    if filename.startswith(("/", "\\")) or "\x00" in filename:
        return False
    if len(filename) > 1 and filename[1] == ":":
        return False
    return True


def _is_valid_download_url(url):
    if not isinstance(url, str) or not url:
        return False
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    if not parsed.hostname:
        return False
    return True


class IpcServer:
    """Localhost-only TCP IPC server with token auth.

    ``download_callback`` (if given) is called with the validated
    download dict and its return value is ignored; the default behaviour
    records the request in ``self.downloads``.
    """

    def __init__(self, host="127.0.0.1", port=0, token=None,
                 download_callback=None, port_file=None):
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise ValueError("IPC server may only bind to localhost")
        self.host = "127.0.0.1" if host == "localhost" else host
        self.port = port
        self.token = token or generate_token()
        self.download_callback = download_callback
        self.port_file = Path(port_file) if port_file else None
        self.downloads = []
        self._sock = None
        self._thread = None
        self._running = False

    def validate_token(self, token):
        if not isinstance(token, str) or not token:
            return False
        return secrets.compare_digest(token, self.token)

    # -- message dispatch ------------------------------------------------
    def handle_message(self, msg):
        """Dispatch one message dict. Returns {status, message[, ...]}."""
        if not isinstance(msg, dict):
            return {"status": "error", "message": "message must be a JSON object"}
        msg_type = msg.get("type")
        if msg_type == "ping":
            return {"status": "ok", "message": "pong"}
        if msg_type == "handshake":
            token = msg.get("token")
            if not self.validate_token(token):
                return {"status": "error", "message": "invalid token"}
            return {"status": "ok", "message": "handshake accepted",
                    "app": APP_NAME, "version": APP_VERSION}
        if msg_type == "download":
            url = msg.get("url")
            filename = msg.get("filename")
            if not _is_valid_download_url(url):
                return {"status": "error",
                        "message": "invalid URL: only http:// and https:// are allowed"}
            if filename is not None and not _is_safe_filename(filename):
                return {"status": "error",
                        "message": "invalid filename: path traversal is not allowed"}
            entry = {
                "url": url,
                "filename": filename,
                "referrer": msg.get("referrer"),
                "headers": msg.get("headers") or {},
            }
            self.downloads.append(entry)
            try:
                if self.download_callback is not None:
                    self.download_callback(entry)
            except Exception as exc:  # callback failure must not break IPC
                return {"status": "error", "message": "download callback failed: %s" % exc}
            return {"status": "ok", "message": "download queued"}
        return {"status": "error", "message": "unknown message type: %r" % (msg_type,)}

    # -- server lifecycle ------------------------------------------------
    def start(self):
        """Bind 127.0.0.1 on an ephemeral port and serve in a thread."""
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, self.port))
        self._sock.listen(5)
        self._sock.settimeout(0.5)
        self.port = self._sock.getsockname()[1]
        self._running = True
        if self.port_file is not None:
            self.port_file.parent.mkdir(parents=True, exist_ok=True)
            # Port file contains host/port only -- never the token.
            self.port_file.write_text(
                json.dumps({"host": self.host, "port": self.port}), encoding="utf-8")
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self.port

    def _serve(self):
        while self._running:
            try:
                conn, addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle_connection,
                             args=(conn, addr), daemon=True).start()

    def _handle_connection(self, conn, addr):
        # Reject anything that is not loopback, even if it somehow
        # reached the socket.
        peer = addr[0] if addr else ""
        if peer not in ("127.0.0.1", "::1"):
            try:
                conn.close()
            except OSError:
                pass
            return
        try:
            stream = conn.makefile("rwb")
            first = read_message(stream)
            if first is None:
                return
            if not isinstance(first, dict) or first.get("type") != "handshake":
                write_message(stream, {"status": "error",
                                       "message": "first message must be a handshake"})
                return
            result = self.handle_message(first)
            write_message(stream, result)
            if result.get("status") != "ok":
                return
            # Authenticated session: handle further messages on this
            # connection until the client closes it.
            while True:
                msg = read_message(stream)
                if msg is None:
                    break
                write_message(stream, self.handle_message(msg))
        except (EOFError, ValueError, OSError, json.JSONDecodeError):
            return
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def stop(self):
        self._running = False
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self.port_file is not None:
            try:
                self.port_file.unlink(missing_ok=True)
            except OSError:
                pass

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()
        return False
