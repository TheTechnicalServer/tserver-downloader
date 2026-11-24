"""Pytest fixtures: local HTTP test server + engine/tmp-dir fixtures."""

from __future__ import annotations

import base64
import hashlib
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tserver_downloader.config import Settings
from tserver_downloader.engine import DownloadEngine
from tserver_downloader.state import StateStore

FILE_SIZE = 5 * 1024 * 1024  # 5 MB
_PATTERN = bytes(range(256))
DATA = (_PATTERN * (FILE_SIZE // 256 + 1))[:FILE_SIZE]
DATA_SHA256 = hashlib.sha256(DATA).hexdigest()
DATA_MD5 = hashlib.md5(DATA).hexdigest()

AUTH_USER = "testuser"
AUTH_PASS = "testpass"
_AUTH_HEADER = "Basic " + base64.b64encode(
    f"{AUTH_USER}:{AUTH_PASS}".encode()).decode()


class _Handler(BaseHTTPRequestHandler):
    """Test server: Range support, redirect, throttle, flaky, auth, 404."""

    protocol_version = "HTTP/1.1"
    flaky_failures = 2  # class-level counter, reset per test server

    def log_message(self, *args):  # silence test output
        pass

    # -- helpers -------------------------------------------------------
    def _send_data(self, data: bytes, range_header: str | None,
                   throttle: bool = False, content_type: str = "application/octet-stream"):
        total = len(data)
        if range_header and range_header.startswith("bytes="):
            spec = range_header[len("bytes="):].strip()
            try:
                start_s, end_s = spec.split("-", 1)
                start = int(start_s) if start_s else 0
                end = int(end_s) if end_s else total - 1
            except ValueError:
                start, end = 0, total - 1
            end = min(end, total - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{total}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Content-Type", content_type)
            self.end_headers()
            if self.command != "HEAD":
                self._write_body(data[start:end + 1], throttle)
            return
        self.send_response(200)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(total))
        self.send_header("Content-Type", content_type)
        self.end_headers()
        if self.command != "HEAD":
            self._write_body(data, throttle)

    def _write_body(self, data: bytes, throttle: bool = False):
        if not throttle:
            self.wfile.write(data)
            return
        chunk = 64 * 1024
        for i in range(0, len(data), chunk):
            self.wfile.write(data[i:i + chunk])
            self.wfile.flush()
            time.sleep(0.05)

    def _serve(self, path: str):
        if path == "/file.bin":
            self._send_data(DATA, self.headers.get("Range"))
        elif path == "/no-range.bin":
            # Ignores Range entirely: always 200, full body.
            self.send_response(200)
            self.send_header("Content-Length", str(len(DATA)))
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(DATA)
        elif path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/file.bin")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif path == "/slow.bin":
            self._send_data(DATA, self.headers.get("Range"), throttle=True)
        elif path == "/flaky.bin":
            type(self).flaky_failures -= 1
            if type(self).flaky_failures >= 0:
                body = b"flaky failure"
                self.send_response(500)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)
                return
            self._send_data(DATA, self.headers.get("Range"))
        elif path == "/auth.bin":
            if self.headers.get("Authorization") != _AUTH_HEADER:
                body = b"unauthorized"
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="test"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)
                return
            self._send_data(DATA, self.headers.get("Range"))
        elif path == "/notfound":
            body = b"not found"
            self.send_response(404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        else:
            body = b"not found"
            self.send_response(404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

    def do_GET(self):
        self._serve(self.path.split("?", 1)[0])

    def do_HEAD(self):
        self._serve(self.path.split("?", 1)[0])


@pytest.fixture()
def http_server():
    """A local HTTP server on 127.0.0.1 with an ephemeral port."""
    _Handler.flaky_failures = 2
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    yield base_url
    server.shutdown()
    server.server_close()


@pytest.fixture()
def tmp_download_dir(tmp_path):
    """Empty download directory for a test."""
    d = tmp_path / "downloads"
    d.mkdir()
    return d


@pytest.fixture()
def engine(tmp_path, tmp_download_dir):
    """A DownloadEngine backed by a temp SQLite store (local URLs allowed)."""
    settings = Settings(
        download_dir=str(tmp_download_dir),
        connections_per_download=4,
        max_simultaneous=3,
    )
    store = StateStore(tmp_path / "state.db")
    eng = DownloadEngine(settings, store, allow_local=True)
    yield eng
    # Best-effort cleanup of any still-running tasks.
    for task in eng.list_tasks():
        if task.status not in ("completed", "failed", "cancelled"):
            try:
                eng.cancel(task.id)
            except Exception:
                pass
    store.close()


@pytest.fixture()
def data_sha256():
    return DATA_SHA256


@pytest.fixture()
def data_md5():
    return DATA_MD5
