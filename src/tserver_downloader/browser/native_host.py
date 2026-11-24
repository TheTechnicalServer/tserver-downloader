"""Native messaging host for the TServer Downloader browser extension.

Reads length-prefixed JSON messages from stdin (Chrome native messaging
protocol, see :mod:`tserver_downloader.browser.ipc`), dispatches them,
and writes length-prefixed JSON responses to stdout.

Default dispatch: a ``{"type": "download", ...}`` request is validated
by :meth:`IpcServer.handle_message` and appended as one JSON line to
``downloads_queue.jsonl`` in the user data directory, where the desktop
app picks it up. A custom ``callback`` may be supplied for embedding.

Runnable as::

    python -m tserver_downloader.browser.native_host
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .ipc import IpcServer, read_message, write_message


def user_data_dir():
    """Per-user data directory for queue/state files."""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "TServer Downloader"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "TServer Downloader"
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "tserver-downloader"


def get_queue_path():
    return user_data_dir() / "downloads_queue.jsonl"


def default_dispatch(msg, queue_path=None):
    """Validate ``msg`` and, for downloads, append it to the queue file.

    Returns a ``{status, message}`` dict. Never raises for a bad message.
    """
    # Reuse the IPC server's validation/dispatch without binding a socket.
    server = IpcServer()
    try:
        result = server.handle_message(msg)
    except Exception as exc:
        return {"status": "error", "message": "dispatch failed: %s" % exc}
    if result.get("status") == "ok" and isinstance(msg, dict) \
            and msg.get("type") == "download":
        path = Path(queue_path) if queue_path else get_queue_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "url": msg.get("url"),
                "filename": msg.get("filename"),
                "referrer": msg.get("referrer"),
                "headers": msg.get("headers") or {},
            }
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
        except OSError as exc:
            return {"status": "error", "message": "could not write queue: %s" % exc}
    return result


def main(callback=None, queue_path=None):
    """Serve native messages on stdin/stdout until stdin closes."""
    dispatch = callback or (lambda msg: default_dispatch(msg, queue_path=queue_path))
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    while True:
        try:
            msg = read_message(stdin)
        except (EOFError, ValueError):
            break
        if msg is None:
            break
        try:
            result = dispatch(msg)
        except Exception as exc:
            result = {"status": "error", "message": "dispatch failed: %s" % exc}
        if not isinstance(result, dict):
            result = {"status": "error", "message": "handler returned no response"}
        try:
            write_message(stdout, result)
        except (BrokenPipeError, OSError, ValueError):
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
