"""Tests for native-messaging / localhost IPC (Phase 2)."""
import io
import json
import socket
import struct
import subprocess
import sys

import pytest

from tserver_downloader.browser.ipc import (
    IpcServer,
    generate_token,
    read_message,
    write_message,
)
from tserver_downloader.browser.native_host import default_dispatch


def test_ipc_length_prefixed_round_trip():
    buf = io.BytesIO()
    write_message(buf, {"type": "download", "url": "https://example.com/f.zip"})
    write_message(buf, {"type": "ping"})
    buf.seek(0)
    assert read_message(buf) == {"type": "download", "url": "https://example.com/f.zip"}
    assert read_message(buf) == {"type": "ping"}
    assert read_message(buf) is None  # clean EOF


def test_ipc_framing_is_little_endian_length_prefix():
    buf = io.BytesIO()
    write_message(buf, {"a": 1})
    raw = buf.getvalue()
    (length,) = struct.unpack("<I", raw[:4])
    assert length == len(raw) - 4
    assert json.loads(raw[4:]) == {"a": 1}


def test_ipc_rejects_oversized_message():
    buf = io.BytesIO()
    buf.write(struct.pack("<I", 2 * 1024 * 1024) + b"x" * 10)
    buf.seek(0)
    with pytest.raises(ValueError):
        read_message(buf)


def test_token_validation_rejects_bad_token():
    server = IpcServer(token="good-token")
    assert server.validate_token("good-token")
    assert not server.validate_token("bad-token")
    assert not server.validate_token("")
    assert not server.validate_token(None)


def test_generate_token_unique():
    assert generate_token() != generate_token()
    assert len(generate_token()) >= 20


def test_handle_message_download_ok():
    server = IpcServer()
    result = server.handle_message({
        "type": "download", "url": "https://example.com/file.zip",
        "filename": "file.zip", "referrer": "https://example.com/",
        "headers": {"User-Agent": "test"},
    })
    assert result["status"] == "ok"
    assert len(server.downloads) == 1
    assert server.downloads[0]["url"] == "https://example.com/file.zip"


def test_handle_message_rejects_file_url():
    server = IpcServer()
    result = server.handle_message(
        {"type": "download", "url": "file:///etc/passwd", "filename": "x"})
    assert result["status"] == "error"
    assert server.downloads == []


def test_handle_message_rejects_non_http():
    server = IpcServer()
    for url in ("ftp://example.com/f.zip", "javascript:alert(1)", "", None):
        result = server.handle_message({"type": "download", "url": url})
        assert result["status"] == "error", url
    assert server.downloads == []


def test_handle_message_rejects_traversal_filenames():
    server = IpcServer()
    for name in ("../evil.zip", "..\\evil.zip", "/etc/passwd", "a/b.zip", ".."):
        result = server.handle_message({
            "type": "download", "url": "https://example.com/f.zip",
            "filename": name})
        assert result["status"] == "error", name
    assert server.downloads == []


def test_handle_message_ping_and_unknown():
    server = IpcServer()
    assert server.handle_message({"type": "ping"})["status"] == "ok"
    assert server.handle_message({"type": "nope"})["status"] == "error"
    assert server.handle_message("not-a-dict")["status"] == "error"


def test_handshake_requires_valid_token():
    server = IpcServer(token="secret-token")
    ok = server.handle_message({"type": "handshake", "token": "secret-token"})
    assert ok["status"] == "ok"
    bad = server.handle_message({"type": "handshake", "token": "wrong"})
    assert bad["status"] == "error"


def test_ipc_server_localhost_only_and_handshake(tmp_path):
    port_file = tmp_path / "ipc.json"
    server = IpcServer(port=0, port_file=port_file)
    server.start()
    try:
        assert server.port > 0
        assert port_file.exists()
        info = json.loads(port_file.read_text())
        assert info["host"] == "127.0.0.1"
        assert info["port"] == server.port
        assert "token" not in info  # token must not leak into the port file

        with socket.create_connection(("127.0.0.1", server.port), timeout=5) as sock:
            stream = sock.makefile("rwb")
            write_message(stream, {"type": "handshake", "token": server.token,
                                   "app": "TServer Downloader", "version": "1.0.0"})
            assert read_message(stream)["status"] == "ok"
            write_message(stream, {"type": "download",
                                   "url": "https://example.com/a.zip",
                                   "filename": "a.zip"})
            assert read_message(stream)["status"] == "ok"
        assert len(server.downloads) == 1

        # Bad token handshake is rejected and the connection closes.
        with socket.create_connection(("127.0.0.1", server.port), timeout=5) as sock:
            stream = sock.makefile("rwb")
            write_message(stream, {"type": "handshake", "token": "wrong"})
            assert read_message(stream)["status"] == "error"

        # A non-handshake first message is rejected.
        with socket.create_connection(("127.0.0.1", server.port), timeout=5) as sock:
            stream = sock.makefile("rwb")
            write_message(stream, {"type": "ping"})
            assert read_message(stream)["status"] == "error"
    finally:
        server.stop()
    assert not port_file.exists()


def test_ipc_server_rejects_non_localhost_bind():
    with pytest.raises(ValueError):
        IpcServer(host="0.0.0.0")


def test_native_host_dispatch_writes_queue(tmp_path):
    queue = tmp_path / "downloads_queue.jsonl"
    result = default_dispatch(
        {"type": "download", "url": "https://example.com/f.zip",
         "filename": "f.zip", "referrer": "https://example.com/"},
        queue_path=queue)
    assert result["status"] == "ok"
    lines = queue.read_text().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["url"] == "https://example.com/f.zip"
    assert entry["filename"] == "f.zip"


def test_native_host_dispatch_rejects_bad_url(tmp_path):
    queue = tmp_path / "downloads_queue.jsonl"
    result = default_dispatch(
        {"type": "download", "url": "file:///etc/passwd", "filename": "x"},
        queue_path=queue)
    assert result["status"] == "error"
    assert not queue.exists()


def test_native_host_main_round_trip(tmp_path):
    queue = tmp_path / "queue.jsonl"
    env = {"PYTHONPATH": "src", "TSERVER_QUEUE": str(queue)}
    # Drive the module over stdio with length-prefixed messages.
    proc = subprocess.Popen(
        [sys.executable, "-m", "tserver_downloader.browser.native_host"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        cwd=str(__file__ and __import__("pathlib").Path(__file__).resolve().parents[1]),
        env={**__import__("os").environ, **env})
    try:
        out = io.BytesIO()
        write_message(out, {"type": "ping"})
        write_message(out, {"type": "download", "url": "https://example.com/f.zip",
                            "filename": "f.zip"})
        proc.stdin.write(out.getvalue())
        proc.stdin.close()
        raw = proc.stdout.read()
        assert proc.wait(timeout=10) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
    buf = io.BytesIO(raw)
    assert read_message(buf)["status"] == "ok"  # ping
    assert read_message(buf)["status"] == "ok"  # download queued
