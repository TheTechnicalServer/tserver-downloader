"""Background workers for the TServer Downloader UI.

The UI never blocks on the download engine: all polling happens in a
:py:class:`QThread` and results are delivered to the main thread through
Qt signals.  The helpers in this module are also usable without Qt (for
example from a headless smoke check), so the Qt import is guarded.
"""

from __future__ import annotations

from typing import Any, Iterable
from urllib.parse import urlparse

try:
    from PyQt6.QtCore import QThread, pyqtSignal

    _QT_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised on machines without PyQt6
    _QT_AVAILABLE = False
    QThread = object  # type: ignore[assignment,misc]
    pyqtSignal = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Download-record normalisation
# ---------------------------------------------------------------------------

def _pick(record: Any, names: Iterable[str], default: Any = None) -> Any:
    """Return the first non-None value found under any of ``names``.

    ``record`` may be a ``dict`` or an object with attributes.
    """
    for name in names:
        if isinstance(record, dict):
            if name in record and record[name] is not None:
                return record[name]
        elif hasattr(record, name):
            value = getattr(record, name)
            if value is not None and not callable(value):
                return value
    return default


def normalize_download(record: Any) -> dict:
    """Normalise one engine download record to a plain ``dict``.

    The engine may hand us objects or dicts; the UI only ever deals with
    the keys produced here.  Missing numeric fields are derived when
    possible (``percent`` from downloaded/total, ``domain`` from the URL).
    """
    url = str(_pick(record, ("url", "source_url", "link"), default="") or "")
    filename = str(
        _pick(record, ("filename", "file_name", "name", "title"), default="") or ""
    )
    if not filename and url:
        filename = urlparse(url).path.rsplit("/", 1)[-1] or url
    size = _pick(record, ("size", "total_size", "total_bytes", "filesize"), default=0)
    downloaded = _pick(
        record, ("downloaded", "downloaded_bytes", "bytes_downloaded"), default=0
    )
    try:
        size = int(size or 0)
    except (TypeError, ValueError):
        size = 0
    try:
        downloaded = int(downloaded or 0)
    except (TypeError, ValueError):
        downloaded = 0
    percent = _pick(record, ("percent", "percentage"), default=None)
    if percent is None:
        # The engine exposes progress as a 0.0-1.0 fraction property.
        progress = _pick(record, ("progress",), default=None)
        if progress is not None:
            try:
                percent = float(progress) * 100.0
            except (TypeError, ValueError):
                percent = None
    if percent is None:
        percent = (downloaded / size * 100.0) if size > 0 else 0.0
    try:
        percent = float(percent)
    except (TypeError, ValueError):
        percent = 0.0
    percent = max(0.0, min(100.0, percent))
    status = str(_pick(record, ("status", "state"), default="unknown") or "unknown")
    domain = str(_pick(record, ("domain", "host"), default="") or "")
    if not domain and url:
        domain = urlparse(url).netloc
    speed = _pick(record, ("speed", "speed_bps", "download_speed"), default=0)
    try:
        speed = float(speed or 0)
    except (TypeError, ValueError):
        speed = 0.0
    eta = _pick(record, ("eta", "eta_seconds", "time_remaining"), default=None)
    connections = _pick(
        record, ("connections", "num_connections", "threads"), default=1
    )
    try:
        connections = int(connections or 1)
    except (TypeError, ValueError):
        connections = 1
    return {
        "id": _pick(record, ("id", "download_id", "uid"), default=url or filename),
        "url": url,
        "filename": filename,
        "size": size,
        "downloaded": downloaded,
        "percent": percent,
        "speed": speed,
        "eta": eta,
        "status": status.lower(),
        "domain": domain,
        "connections": connections,
    }


def fetch_downloads(engine: Any) -> list:
    """Ask the engine for its current download list, tolerating API drift.

    Tries the known accessor names in order and accepts either a list of
    records or a mapping of id -> record.  Returns normalised dicts.
    """
    if engine is None:
        return []
    records = None
    for name in (
        "list_tasks",
        "get_downloads",
        "list_downloads",
        "all_downloads",
        "get_all",
    ):
        method = getattr(engine, name, None)
        if callable(method):
            records = method()
            break
    if records is None:
        for name in ("downloads", "items", "queue"):
            value = getattr(engine, name, None)
            if value is not None:
                records = value() if callable(value) else value
                break
    if records is None:
        return []
    if isinstance(records, dict):
        records = list(records.values())
    return [normalize_download(r) for r in records]


def call_engine(engine: Any, names: Iterable[str], *args: Any, **kwargs: Any) -> Any:
    """Call the first engine method found under ``names``.

    Raises :py:class:`AttributeError` with a readable message when the
    engine exposes none of them, so the UI can show a real error dialog
    instead of failing silently.
    """
    if engine is None:
        raise AttributeError("Download engine is not available.")
    for name in names:
        method = getattr(engine, name, None)
        if callable(method):
            return method(*args, **kwargs)
    raise AttributeError(
        "Download engine does not provide any of: " + ", ".join(names)
    )


# ---------------------------------------------------------------------------
# Polling worker (Qt only)
# ---------------------------------------------------------------------------

if _QT_AVAILABLE:

    class DownloadWorker(QThread):
        """Poll the engine for progress and emit it to the UI thread.

        Signals:
            progress_updated: emitted with the normalised download list.
            worker_error: emitted with a message when a poll fails; the
                worker keeps polling afterwards instead of dying.
        """

        progress_updated = pyqtSignal(list)
        worker_error = pyqtSignal(str)

        def __init__(self, engine: Any, interval_ms: int = 500, parent=None):
            super().__init__(parent)
            self._engine = engine
            self._interval_ms = max(100, int(interval_ms))
            self._running = True

        def run(self) -> None:
            consecutive_errors = 0
            while self._running:
                try:
                    records = fetch_downloads(self._engine)
                    consecutive_errors = 0
                    self.progress_updated.emit(records)
                except Exception as exc:  # engine hiccup: report, keep polling
                    consecutive_errors += 1
                    if consecutive_errors <= 3:
                        self.worker_error.emit(f"Progress poll failed: {exc}")
                # Sleep in small slices so stop() takes effect promptly.
                slept = 0
                while self._running and slept < self._interval_ms:
                    self.msleep(50)
                    slept += 50

        def stop(self) -> None:
            """Ask the thread to finish, then wait for it (call from UI)."""
            self._running = False
            self.wait(2000)

    class BackgroundWorker(QThread):
        """Run one callable off the GUI thread and report back via signals.

        Used for engine calls that can block (``engine.add`` probes the
        server first; media detection fetches a page).  ``succeeded``
        carries the callable's return value; ``failed`` carries the
        exception message.  Both signals fire on the GUI thread.
        """

        succeeded = pyqtSignal(object)
        failed = pyqtSignal(str)

        def __init__(self, fn, args=(), kwargs=None, parent=None):
            super().__init__(parent)
            self._fn = fn
            self._args = tuple(args)
            self._kwargs = dict(kwargs or {})

        def run(self) -> None:
            try:
                self.succeeded.emit(self._fn(*self._args, **self._kwargs))
            except Exception as exc:  # reported to the UI, not raised
                self.failed.emit(str(exc))

else:  # PyQt6 missing: importing this module must still work.

    class DownloadWorker:  # type: ignore[no-redef]
        """Placeholder raised only if instantiated without PyQt6."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError(
                "PyQt6 is not installed, so the download worker cannot run. "
                "Install it with: pip install -r requirements.txt"
            )

    class BackgroundWorker:  # type: ignore[no-redef]
        """Placeholder raised only if instantiated without PyQt6."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError(
                "PyQt6 is not installed, so background tasks cannot run. "
                "Install it with: pip install -r requirements.txt"
            )
