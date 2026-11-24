"""Download engine: segmented, resumable, speed-limited downloads.

Design notes
------------
* One ``.part`` file per download. Segments write at their byte offset
  into that file under a single lock; the file is pre-allocated with
  ``truncate`` when the total size is known and fsynced on finalize.
* Progress and segment state are persisted to SQLite so a paused task
  can be resumed after the application restarts.
* Retries use exponential backoff and respect ``Retry-After`` on 429.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import requests

from .config import Settings
from .logging_setup import get_logger
from .state import (
    STATUS_ACTIVE,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PAUSED,
    STATUS_QUEUED,
    DownloadRecord,
    SegmentRecord,
    StateStore,
)
from .utils import (
    check_disk_space,
    format_eta,
    format_size,
    format_speed,
    md5_file,
    resolve_collision,
    safe_join,
    sanitize_filename,
    sha256_file,
    validate_url,
)

CHUNK_SIZE = 64 * 1024
SEGMENT_MIN_SIZE = 1 * 1024 * 1024  # only segment files >= 1 MB
REQUEST_TIMEOUT = 30
MAX_RETRIES = 5
BACKOFF_BASE = 1.0
BACKOFF_FACTOR = 2.0
MAX_REDIRECTS = 5


class Status:
    """Task status constants."""

    QUEUED = STATUS_QUEUED
    ACTIVE = STATUS_ACTIVE
    PAUSED = STATUS_PAUSED
    COMPLETED = STATUS_COMPLETED
    FAILED = STATUS_FAILED
    CANCELLED = STATUS_CANCELLED


TERMINAL_STATUSES = {STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED}


class DownloadError(Exception):
    """Raised for any failure in probing or downloading a file."""


# ----------------------------------------------------------------------
# Speed limiter
# ----------------------------------------------------------------------
class SpeedLimiter:
    """Token-bucket rate limiter.

    ``rate_bytes_per_sec <= 0`` means unlimited. ``acquire(n)`` blocks
    until ``n`` bytes of budget are available.
    """

    def __init__(self, rate_bytes_per_sec: float = 0):
        self.rate = float(rate_bytes_per_sec or 0)
        self._tokens = 0.0
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, n: int) -> None:
        """Block until ``n`` bytes may be transferred."""
        if self.rate <= 0 or n <= 0:
            return
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last
                self._last = now
                self._tokens = min(self.rate, self._tokens + elapsed * self.rate)
                if self._tokens >= n:
                    self._tokens -= n
                    return
                deficit = n - self._tokens
                wait = deficit / self.rate
            time.sleep(min(wait, 0.1))


# ----------------------------------------------------------------------
# Task / segment models
# ----------------------------------------------------------------------
@dataclass
class Segment:
    """One byte-range segment of a download."""

    index: int
    start: int
    end: int  # inclusive; -1 = to end of file / unknown length
    downloaded: int = 0
    status: str = "pending"  # pending | active | done

    @property
    def length(self) -> Optional[int]:
        """Total bytes in this segment, or None if unknown."""
        if self.end < 0:
            return None
        return self.end - self.start + 1

    @property
    def remaining(self) -> Optional[int]:
        """Bytes still to download in this segment, or None if unknown."""
        if self.length is None:
            return None
        return max(0, self.length - self.downloaded)

    def to_record(self, download_id: str) -> SegmentRecord:
        return SegmentRecord(
            download_id=download_id, index=self.index, start=self.start,
            end=self.end, downloaded=self.downloaded, status=self.status,
        )


@dataclass
class DownloadTask:
    """A download task (in-memory view; persisted via DownloadRecord)."""

    id: str
    url: str
    filename: str
    dest_path: str
    total_bytes: Optional[int] = None
    downloaded_bytes: int = 0
    status: str = STATUS_QUEUED
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    priority: int = 0
    category: str = "Other"
    checksum: Optional[str] = None
    checksum_type: str = "sha256"
    error: Optional[str] = None
    # Runtime-only fields (not persisted).
    headers: dict = field(default_factory=dict, repr=False, compare=False)
    cookies: dict = field(default_factory=dict, repr=False, compare=False)
    proxy: Optional[str] = field(default=None, repr=False, compare=False)
    auth: Optional[tuple] = field(default=None, repr=False, compare=False)
    connections: int = 8
    speed_limit_kbps: int = 0
    supports_range: Optional[bool] = field(default=None, repr=False, compare=False)
    segments: list[Segment] = field(default_factory=list, repr=False, compare=False)
    segment_progress: list[int] = field(default_factory=list, repr=False, compare=False)
    allow_local: bool = field(default=False, repr=False, compare=False)

    _speed_ema: float = field(default=0.0, repr=False, compare=False)
    _last_sample_at: float = field(default=0.0, repr=False, compare=False)
    _last_sample_bytes: int = field(default=0, repr=False, compare=False)

    @property
    def progress(self) -> float:
        """Fraction downloaded (0.0–1.0); 0.0 when total is unknown."""
        if not self.total_bytes:
            return 0.0
        return min(1.0, self.downloaded_bytes / self.total_bytes)

    @property
    def speed(self) -> float:
        """Current download speed in bytes/sec (EMA)."""
        return self._speed_ema

    @property
    def eta(self) -> Optional[float]:
        """Estimated seconds remaining, or None if unknown."""
        if not self.total_bytes or self._speed_ema <= 0:
            return None
        remaining = self.total_bytes - self.downloaded_bytes
        return max(0.0, remaining / self._speed_ema)

    @property
    def part_path(self) -> str:
        return self.dest_path + ".part"

    def sample_speed(self) -> None:
        """Update the speed EMA from bytes downloaded since last sample."""
        now = time.monotonic()
        if self._last_sample_at == 0:
            self._last_sample_at = now
            self._last_sample_bytes = self.downloaded_bytes
            return
        dt = now - self._last_sample_at
        if dt >= 0.25:
            rate = (self.downloaded_bytes - self._last_sample_bytes) / dt
            if rate >= 0:
                self._speed_ema = rate if self._speed_ema == 0 else 0.5 * self._speed_ema + 0.5 * rate
            self._last_sample_at = now
            self._last_sample_bytes = self.downloaded_bytes

    def to_record(self) -> DownloadRecord:
        return DownloadRecord(
            id=self.id, url=self.url, filename=self.filename,
            dest_path=self.dest_path, total_bytes=self.total_bytes,
            downloaded_bytes=self.downloaded_bytes, status=self.status,
            created_at=self.created_at, updated_at=self.updated_at,
            priority=self.priority, category=self.category,
            checksum=self.checksum, checksum_type=self.checksum_type,
            error=self.error,
        )

    @classmethod
    def from_record(cls, record: DownloadRecord, **runtime) -> "DownloadTask":
        return cls(
            id=record.id, url=record.url, filename=record.filename,
            dest_path=record.dest_path, total_bytes=record.total_bytes,
            downloaded_bytes=record.downloaded_bytes, status=record.status,
            created_at=record.created_at, updated_at=record.updated_at,
            priority=record.priority, category=record.category,
            checksum=record.checksum, checksum_type=record.checksum_type,
            error=record.error, **runtime,
        )

    def describe(self) -> str:
        """One-line human-readable summary (for logs / UI)."""
        return (
            f"{self.filename} [{self.status}] "
            f"{format_size(self.downloaded_bytes)}/{format_size(self.total_bytes)} "
            f"({self.progress:.0%}) {format_speed(self.speed)} "
            f"ETA {format_eta(self.eta)}"
        )


# ----------------------------------------------------------------------
# Queue manager
# ----------------------------------------------------------------------
class QueueManager:
    """Priority queue of task ids. Higher priority first, FIFO within it."""

    def __init__(self):
        self._lock = threading.Lock()
        self._queued: list[tuple[int, float, str]] = []  # (-priority, created_at, id)

    def add(self, task: DownloadTask) -> None:
        """Enqueue a task (no-op if already queued)."""
        with self._lock:
            if any(tid == task.id for _, _, tid in self._queued):
                return
            self._queued.append((-task.priority, task.created_at, task.id))
            self._queued.sort()

    def remove(self, task_id: str) -> None:
        with self._lock:
            self._queued = [e for e in self._queued if e[2] != task_id]

    def pop_next(self) -> Optional[str]:
        """Remove and return the highest-priority queued task id, or None."""
        with self._lock:
            if not self._queued:
                return None
            return self._queued.pop(0)[2]

    def peek(self) -> Optional[str]:
        with self._lock:
            return self._queued[0][2] if self._queued else None

    def __len__(self) -> int:
        with self._lock:
            return len(self._queued)

    def ordered_ids(self) -> list[str]:
        with self._lock:
            return [tid for _, _, tid in self._queued]


# ----------------------------------------------------------------------
# Scheduler (pure logic, no sleeping)
# ----------------------------------------------------------------------
@dataclass
class ScheduleEntry:
    """A time window during which a task may run."""

    task_id: str
    start_time: float  # epoch seconds
    stop_time: float  # epoch seconds


class Scheduler:
    """Decides which scheduled tasks are due at a given moment."""

    def __init__(self):
        self._schedules: dict[str, ScheduleEntry] = {}

    @staticmethod
    def _to_epoch(value: datetime | float | int) -> float:
        if isinstance(value, datetime):
            return value.timestamp()
        return float(value)

    def add_schedule(self, task_id: str, start_time: datetime | float,
                     stop_time: datetime | float) -> None:
        """Register a run window for a task. Raises ValueError if inverted."""
        start = self._to_epoch(start_time)
        stop = self._to_epoch(stop_time)
        if stop < start:
            raise ValueError("stop_time must not be before start_time")
        self._schedules[task_id] = ScheduleEntry(task_id, start, stop)

    def remove_schedule(self, task_id: str) -> None:
        self._schedules.pop(task_id, None)

    def should_run(self, task_id: str, now: datetime | float) -> bool:
        """True if ``now`` falls inside the task's scheduled window."""
        entry = self._schedules.get(task_id)
        if entry is None:
            return False
        ts = self._to_epoch(now)
        return entry.start_time <= ts <= entry.stop_time

    def due_tasks(self, now: datetime | float) -> list[str]:
        """Task ids whose window contains ``now``, earliest start first."""
        ts = self._to_epoch(now)
        due = [e for e in self._schedules.values()
               if e.start_time <= ts <= e.stop_time]
        due.sort(key=lambda e: e.start_time)
        return [e.task_id for e in due]


# ----------------------------------------------------------------------
# Clipboard monitor (opt-in only)
# ----------------------------------------------------------------------
_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)


class ClipboardMonitor:
    """Opt-in clipboard watcher that reports URLs found on the clipboard.

    Uses ``tkinter`` when available; otherwise it is a graceful no-op.
    Never imports pyperclip.
    """

    def __init__(self, callback: Optional[Callable[[str], None]] = None,
                 interval: float = 1.0):
        self.callback = callback
        self.interval = interval
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_text: Optional[str] = None

    def check_clipboard(self, text: Optional[str]) -> Optional[str]:
        """Extract the first valid download URL from clipboard text."""
        if not text:
            return None
        for candidate in _URL_RE.findall(text):
            candidate = candidate.rstrip(".,);]")
            try:
                return validate_url(candidate)
            except ValueError:
                continue
        return None

    def _read_clipboard(self) -> Optional[str]:
        try:
            import tkinter
            root = tkinter.Tk()
            root.withdraw()
            try:
                return root.clipboard_get()
            finally:
                root.destroy()
        except Exception:
            return None

    def _poll(self) -> None:
        while self._running:
            text = self._read_clipboard()
            if text is not None and text != self._last_text:
                self._last_text = text
                url = self.check_clipboard(text)
                if url and self.callback:
                    try:
                        self.callback(url)
                    except Exception:
                        pass
            time.sleep(self.interval)

    def start(self) -> None:
        """Start polling the clipboard in a background thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._poll, daemon=True,
                                        name="clipboard-monitor")
        self._thread.start()

    def stop(self) -> None:
        """Stop polling."""
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=self.interval + 1)
            self._thread = None


# ----------------------------------------------------------------------
# Engine
# ----------------------------------------------------------------------
class DownloadEngine:
    """Core download engine: segmented, resumable, queued downloads."""

    def __init__(self, settings: Optional[Settings] = None,
                 state_store: Optional[StateStore] = None,
                 logger: Optional[logging.Logger] = None,
                 on_progress: Optional[Callable[[DownloadTask], None]] = None,
                 allow_local: bool = False):
        self.settings = settings or Settings()
        self.store = state_store
        self.logger = logger or get_logger()
        self.on_progress = on_progress
        self.allow_local = allow_local

        self._tasks: dict[str, DownloadTask] = {}
        self._lock = threading.RLock()
        self._file_locks: dict[str, threading.Lock] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._pause_events: dict[str, threading.Event] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._active_count = 0
        self.queue = QueueManager()
        self.scheduler = Scheduler()
        self.global_limiter = SpeedLimiter(
            (self.settings.global_speed_limit_kbps or 0) * 1024
        )
        self._last_progress_cb = 0.0

        # Recover tasks persisted by a previous run.
        if self.store is not None:
            for record in self.store.list_tasks():
                if record.status == STATUS_ACTIVE:
                    record.status = STATUS_PAUSED
                    self.store.save_task(record)
                task = DownloadTask.from_record(
                    record, connections=self.settings.connections_per_download,
                    allow_local=self.allow_local,
                )
                self._tasks[task.id] = task

    # -- session -------------------------------------------------------
    def _make_session(self, task: DownloadTask) -> requests.Session:
        session = requests.Session()
        session.max_redirects = MAX_REDIRECTS
        if task.proxy:
            session.proxies = {"http": task.proxy, "https": task.proxy}
        if task.auth:
            session.auth = tuple(task.auth)
        return session

    def _session_kwargs(self, task: DownloadTask) -> dict:
        kwargs: dict = {"timeout": REQUEST_TIMEOUT}
        if task.cookies:
            kwargs["cookies"] = task.cookies
        return kwargs

    # -- task management ----------------------------------------------
    def add(self, url: str, dest_dir: Optional[str] = None, filename: Optional[str] = None,
            headers: Optional[dict] = None, cookies: Optional[dict] = None,
            proxy: Optional[str] = None, auth: Optional[tuple] = None,
            priority: int = 0, category: Optional[str] = None,
            checksum: Optional[str] = None, checksum_type: str = "sha256",
            connections: Optional[int] = None,
            speed_limit_kbps: int = 0) -> str:
        """Create (or return the existing) task for a URL. Returns task id."""
        url = validate_url(url, allow_local=self.allow_local)
        connections = connections or self.settings.connections_per_download

        # Probe first: gives total size, range support, real filename.
        probe_headers = dict(headers or {})
        total, supports_range, probed_name, _ctype = self._probe(
            url, headers=probe_headers, cookies=cookies, proxy=proxy, auth=auth
        )
        final_name = sanitize_filename(filename or probed_name or "download")
        category = category or self.settings.category_for_filename(final_name)
        subfolder = self.settings.subfolder_for_category(category)
        base_dir = Path(dest_dir) if dest_dir else Path(self.settings.download_dir) / subfolder
        base_dir.mkdir(parents=True, exist_ok=True)
        dest_path = safe_join(base_dir, final_name)

        # Duplicate detection: same URL + same destination.
        existing = self._find_duplicate(url, str(dest_path))
        if existing is not None:
            return existing

        # A complete file already on disk with no record: adopt it.
        if dest_path.exists() and (total is None or dest_path.stat().st_size == total):
            if checksum and not self._checksum_ok(dest_path, checksum, checksum_type):
                pass  # stale/corrupt file: fall through and re-download
            else:
                record = DownloadRecord(
                    id=str(uuid.uuid4()), url=url, filename=final_name,
                    dest_path=str(dest_path), total_bytes=total,
                    downloaded_bytes=dest_path.stat().st_size,
                    status=STATUS_COMPLETED, priority=priority, category=category,
                    checksum=checksum, checksum_type=checksum_type,
                )
                if self.store is not None:
                    self.store.save_task(record)
                task = DownloadTask.from_record(
                    record, headers=dict(headers or {}), cookies=dict(cookies or {}),
                    proxy=proxy, auth=auth, connections=connections,
                    speed_limit_kbps=speed_limit_kbps, allow_local=self.allow_local,
                )
                self._tasks[task.id] = task
                return task.id

        task_id = str(uuid.uuid4())
        task = DownloadTask(
            id=task_id, url=url, filename=final_name, dest_path=str(dest_path),
            total_bytes=total, downloaded_bytes=0, status=STATUS_QUEUED,
            priority=priority, category=category, checksum=checksum,
            checksum_type=checksum_type, headers=dict(headers or {}),
            cookies=dict(cookies or {}), proxy=proxy, auth=auth,
            connections=connections, speed_limit_kbps=speed_limit_kbps,
            supports_range=supports_range, allow_local=self.allow_local,
        )
        # If a .part file already exists, keep its bytes for resume.
        part = Path(task.part_path)
        if part.exists():
            task.downloaded_bytes = part.stat().st_size
            if total is not None:
                task.downloaded_bytes = min(task.downloaded_bytes, total)
        self._tasks[task_id] = task
        self._persist(task)
        self.logger.info("Added download %s -> %s", task.id, task.dest_path)
        return task_id

    def _find_duplicate(self, url: str, dest_path: str) -> Optional[str]:
        """Return the id of an existing task for the same URL+destination."""
        for task in list(self._tasks.values()):
            if task.url == url and task.dest_path == dest_path:
                if task.status == STATUS_COMPLETED:
                    final = Path(task.dest_path)
                    if final.exists():
                        if task.checksum:
                            if self._checksum_ok(final, task.checksum, task.checksum_type):
                                return task.id
                            continue
                        if task.total_bytes is None or final.stat().st_size == task.total_bytes:
                            return task.id
                        continue
                    # Record says completed but file is gone: re-download.
                    continue
                # Queued/paused/active or resumable .part: reuse it.
                return task.id
        return None

    @staticmethod
    def _checksum_ok(path: Path, checksum: str, checksum_type: str) -> bool:
        try:
            if checksum_type.lower() == "md5":
                return md5_file(path) == checksum.lower()
            return sha256_file(path) == checksum.lower()
        except OSError:
            return False

    # -- probing -------------------------------------------------------
    def _probe(self, url: str, headers: Optional[dict] = None,
               cookies: Optional[dict] = None, proxy: Optional[str] = None,
               auth: Optional[tuple] = None):
        """Return (total_bytes|None, supports_range, filename, content_type).

        Tries HEAD first, then a ``Range: bytes=0-0`` GET. Transient
        failures are retried with exponential backoff.
        """
        probe_task = DownloadTask(
            id="__probe__", url=url, filename="", dest_path="",
            headers=dict(headers or {}), cookies=dict(cookies or {}),
            proxy=proxy, auth=auth,
        )
        last_error: Optional[Exception] = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                return self._probe_once(probe_task)
            except DownloadError as exc:
                if exc.args and exc.args[0] == "transient":
                    last_error = exc
                    self._sleep_backoff(attempt, None)
                    continue
                raise
            except requests.RequestException as exc:
                last_error = exc
                self._sleep_backoff(attempt, None)
        raise DownloadError(f"probe failed after retries: {last_error}")

    def _probe_once(self, task: DownloadTask):
        session = self._make_session(task)
        try:
            resp = session.head(task.url, headers=task.headers or None,
                                allow_redirects=True, **self._session_kwargs(task))
            try:
                self._raise_for_status(resp)
            except DownloadError as exc:
                if "HTTP 405" in str(exc) or "HTTP 501" in str(exc):
                    return self._probe_range_get(session, task)
                raise
            if resp.status_code == 200:
                return self._probe_from_response(resp, task.url)
            # Some servers reject HEAD; fall through to ranged GET.
            return self._probe_range_get(session, task)
        except requests.RequestException as exc:
            # HEAD not allowed / network hiccup: try ranged GET.
            if isinstance(exc, requests.HTTPError):
                raise
            try:
                return self._probe_range_get(session, task)
            except DownloadError:
                raise
            except requests.RequestException as exc2:
                raise DownloadError("transient", str(exc2)) from exc2
        finally:
            session.close()

    def _probe_range_get(self, session: requests.Session, task: DownloadTask):
        headers = dict(task.headers or {})
        headers["Range"] = "bytes=0-0"
        resp = session.get(task.url, headers=headers, stream=True,
                           allow_redirects=True, **self._session_kwargs(task))
        self._raise_for_status(resp)
        total, supports_range, name, ctype = self._probe_from_response(resp, task.url)
        resp.close()
        return total, supports_range, name, ctype

    def _probe_from_response(self, resp: requests.Response, url: str):
        total = None
        content_length = resp.headers.get("Content-Length")
        content_range = resp.headers.get("Content-Range")
        if content_range:
            # "bytes 0-0/12345"
            try:
                total = int(content_range.rsplit("/", 1)[1])
            except (ValueError, IndexError):
                total = None
        if total is None and content_length:
            try:
                total = int(content_length)
                if resp.status_code == 206 and content_range is None:
                    total = None  # ranged GET without total: unreliable
            except ValueError:
                total = None
        supports_range = resp.headers.get("Accept-Ranges", "").lower() == "bytes" \
            or resp.status_code == 206 or content_range is not None
        name = self._filename_from_headers(resp.headers) or self._filename_from_url(url)
        return total, supports_range, name, resp.headers.get("Content-Type")

    @staticmethod
    def _raise_for_status(resp: requests.Response) -> None:
        status = resp.status_code
        if status in (200, 206):
            return
        if status == 404:
            raise DownloadError("HTTP 404 Not Found")
        if status == 403:
            raise DownloadError("HTTP 403 Forbidden")
        if status == 416:
            raise DownloadError("HTTP 416 Range Not Satisfiable")
        if status == 429:
            retry_after = resp.headers.get("Retry-After")
            raise DownloadError("transient", f"HTTP 429 Too Many Requests (Retry-After: {retry_after})")
        if 500 <= status < 600:
            raise DownloadError("transient", f"HTTP {status} server error")
        if status in (401,):
            raise DownloadError("HTTP 401 Unauthorized")
        raise DownloadError(f"HTTP {status} error")

    @staticmethod
    def _filename_from_headers(headers) -> Optional[str]:
        cd = headers.get("Content-Disposition", "")
        if not cd:
            return None
        # RFC 5987: filename*=UTF-8''...
        m = re.search(r"filename\*\s*=\s*(?:UTF-8''|[^']*''?)([^;]+)", cd, re.IGNORECASE)
        if m:
            return urllib.parse.unquote(m.group(1).strip().strip('"'))
        m = re.search(r'filename\s*=\s*"([^"]+)"', cd, re.IGNORECASE)
        if m:
            return m.group(1)
        m = re.search(r"filename\s*=\s*([^;\s]+)", cd, re.IGNORECASE)
        if m:
            return m.group(1).strip()
        return None

    @staticmethod
    def _filename_from_url(url: str) -> str:
        path = urllib.parse.urlparse(url).path
        name = urllib.parse.unquote(path.rsplit("/", 1)[-1])
        return name or "download"

    # -- segmentation --------------------------------------------------
    def _decide_segments(self, total: Optional[int], supports_range: bool,
                         connections: int) -> list[tuple[int, int]]:
        """Split a download into byte ranges.

        Single segment unless the file is >= 1 MB and the server
        supports Range. Fewer segments for small/slow transfers.
        """
        if total is None or total < SEGMENT_MIN_SIZE or not supports_range:
            return [(0, -1)]
        n = max(1, min(int(connections), 8))
        # Small files do not benefit from many connections.
        if total < 5 * SEGMENT_MIN_SIZE:
            n = min(n, 2)
        elif total < 20 * SEGMENT_MIN_SIZE:
            n = min(n, 4)
        base, remainder = divmod(total, n)
        ranges = []
        start = 0
        for i in range(n):
            end = start + base - 1 + (1 if i < remainder else 0)
            ranges.append((start, end))
            start = end + 1
        return ranges

    # -- lifecycle -----------------------------------------------------
    def start(self, task_id: str) -> None:
        """Queue (and start when a slot is free) a task."""
        task = self._get(task_id)
        if task.status in (STATUS_COMPLETED, STATUS_ACTIVE):
            return
        if task.status == STATUS_CANCELLED:
            raise DownloadError("cannot start a cancelled task")
        task.status = STATUS_QUEUED
        self._persist(task)
        self.queue.add(task)
        self._maybe_start_next()

    def _maybe_start_next(self) -> list[str]:
        started: list[str] = []
        with self._lock:
            while self._active_count < self.settings.max_simultaneous:
                next_id = self.queue.pop_next()
                if next_id is None:
                    return started
                task = self._tasks.get(next_id)
                if task is None or task.status != STATUS_QUEUED:
                    continue
                self._launch(task)
                started.append(next_id)
        return started

    def _launch(self, task: DownloadTask) -> None:
        task.status = STATUS_ACTIVE
        self._active_count += 1
        self._pause_events[task.id] = threading.Event()
        self._cancel_events[task.id] = threading.Event()
        self._persist(task)
        thread = threading.Thread(target=self._run_task, args=(task,),
                                  daemon=True, name=f"download-{task.id[:8]}")
        self._threads[task.id] = thread
        thread.start()

    def start_next(self) -> Optional[str]:
        """Start the next queued task if a slot is free. Returns its id."""
        started = self._maybe_start_next()
        return started[0] if started else None

    # -- scheduler polling (engine-owned; UI also calls check_schedules)
    def check_schedules(self, now: Optional[datetime] = None) -> list[str]:
        """Start due scheduled tasks and pause tasks past their window.

        No-op unless ``settings.scheduler_enabled``. Safe to call from a
        UI poll loop or the background scheduler thread. Returns ids
        started in this call.
        """
        if not self.settings.scheduler_enabled:
            return []
        now = now or datetime.now()
        started: list[str] = []
        for task_id in self.scheduler.due_tasks(now):
            task = self._tasks.get(task_id)
            if task is not None and task.status in (STATUS_QUEUED, STATUS_PAUSED):
                try:
                    self.resume(task_id)
                    started.append(task_id)
                except Exception:
                    self.logger.exception("Failed to start scheduled task %s", task_id)
        # Pause active tasks whose window has ended.
        for task_id, entry in list(self.scheduler._schedules.items()):
            if entry.stop_time <= self.scheduler._to_epoch(now):
                task = self._tasks.get(task_id)
                if task is not None and task.status == STATUS_ACTIVE:
                    try:
                        self.pause(task_id)
                    except Exception:
                        pass
        return started

    def start_scheduler(self, interval: float = 15.0) -> None:
        """Start a daemon thread polling schedules every ``interval`` s."""
        if getattr(self, "_scheduler_thread", None) is not None and self._scheduler_thread.is_alive():
            return
        self._scheduler_stop = threading.Event()
        self._scheduler_thread = threading.Thread(
            target=self._scheduler_loop, args=(interval,), daemon=True,
            name="scheduler")
        self._scheduler_thread.start()

    def stop_scheduler(self) -> None:
        """Stop the scheduler polling thread, if running."""
        stop = getattr(self, "_scheduler_stop", None)
        if stop is not None:
            stop.set()
        thread = getattr(self, "_scheduler_thread", None)
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)

    def _scheduler_loop(self, interval: float) -> None:
        while not self._scheduler_stop.is_set():
            try:
                self.check_schedules()
            except Exception:
                self.logger.exception("Scheduler poll failed")
            self._scheduler_stop.wait(timeout=interval)

    def _maybe_shutdown_after_complete(self) -> None:
        """Shutdown/sleep/hibernate when the queue is fully drained.

        Only acts on Windows (``shutdown.exe`` / ``rundll32``); on other
        platforms it only logs, so tests and Linux runs never power off.
        """
        action = (self.settings.shutdown_after_complete or "none").lower()
        if action == "none":
            return
        with self._lock:
            pending = [t for t in self._tasks.values()
                       if t.status in (STATUS_ACTIVE, STATUS_QUEUED)]
        if pending:
            return
        self.logger.info("All downloads complete; shutdown action: %s", action)
        if os.name != "nt":
            return
        import subprocess
        try:
            if action == "shutdown":
                subprocess.run(["shutdown", "/s", "/t", "60"], check=False)
            elif action == "sleep":
                subprocess.run(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"], check=False)
            elif action == "hibernate":
                subprocess.run(["rundll32.exe", "powrprof.dll,SetSuspendState", "1,1,0"], check=False)
        except Exception:
            self.logger.exception("Shutdown action failed")

    def pause(self, task_id: str) -> None:
        """Pause a task, persisting progress and segment state."""
        task = self._get(task_id)
        if task.status == STATUS_COMPLETED:
            return
        event = self._pause_events.get(task_id)
        if event is not None:
            event.set()
        thread = self._threads.get(task_id)
        if thread is not None and thread.is_alive():
            thread.join(timeout=10)
        if task.status == STATUS_ACTIVE:
            task.status = STATUS_PAUSED
        self._persist(task)
        self._persist_segments(task)

    def resume(self, task_id: str) -> None:
        """Resume a paused/failed task, reloading state from the store."""
        task = self._get(task_id)
        if task.status == STATUS_COMPLETED:
            return
        if task.status == STATUS_ACTIVE:
            return
        # Reload persisted progress (covers restart recovery).
        if self.store is not None:
            record = self.store.get_task(task_id)
            if record is not None:
                task.downloaded_bytes = record.downloaded_bytes
                task.total_bytes = record.total_bytes
                task.error = None
            saved = self.store.get_segments(task_id)
            if saved:
                task.segments = [
                    Segment(index=s.index, start=s.start, end=s.end,
                            downloaded=s.downloaded, status=s.status)
                    for s in saved
                ]
        task.status = STATUS_QUEUED
        task.error = None
        self._persist(task)
        self.queue.add(task)
        self._maybe_start_next()

    def cancel(self, task_id: str, delete_partial: bool = False) -> None:
        """Cancel a task; optionally delete its partial file."""
        task = self._get(task_id)
        self.queue.remove(task_id)
        event = self._cancel_events.get(task_id)
        if event is not None:
            event.set()
        pause_event = self._pause_events.get(task_id)
        if pause_event is not None:
            pause_event.set()
        thread = self._threads.get(task_id)
        if thread is not None and thread.is_alive():
            thread.join(timeout=10)
        if task.status != STATUS_COMPLETED:
            task.status = STATUS_CANCELLED
        self._persist(task)
        if delete_partial:
            try:
                Path(task.part_path).unlink(missing_ok=True)
            except OSError:
                pass

    def retry(self, task_id: str) -> None:
        """Re-queue a failed task for another attempt."""
        task = self._get(task_id)
        if task.status != STATUS_FAILED:
            raise DownloadError("only failed tasks can be retried")
        self.resume(task_id)

    def get_task(self, task_id: str) -> DownloadTask:
        return self._get(task_id)

    def list_tasks(self, status: Optional[str] = None) -> list[DownloadTask]:
        tasks = list(self._tasks.values())
        if status is not None:
            tasks = [t for t in tasks if t.status == status]
        return sorted(tasks, key=lambda t: t.created_at, reverse=True)

    def wait(self, task_id: str, timeout: float = 60.0) -> str:
        """Block until the task reaches a terminal status. Returns status."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            task = self._get(task_id)
            if task.status in TERMINAL_STATUSES:
                return task.status
            time.sleep(0.05)
        return self._get(task_id).status

    # -- download run --------------------------------------------------
    def _run_task(self, task: DownloadTask) -> None:
        try:
            self._run_with_retries(task)
        except Exception as exc:  # noqa: BLE001 - surface as task failure
            if task.status != STATUS_CANCELLED and not self._cancel_events.get(task.id, threading.Event()).is_set():
                task.status = STATUS_FAILED
                task.error = str(exc)
                self.logger.error("Download %s failed: %s", task.id, exc)
        finally:
            with self._lock:
                self._active_count = max(0, self._active_count - 1)
            self._persist(task)
            self._persist_segments(task)
            self._emit_progress(task)
            self._maybe_start_next()
            self._maybe_shutdown_after_complete()

    def _run_with_retries(self, task: DownloadTask) -> None:
        last_error: Optional[Exception] = None
        for attempt in range(MAX_RETRIES + 1):
            if self._is_cancelled(task):
                task.status = STATUS_CANCELLED
                return
            if self._is_paused(task):
                task.status = STATUS_PAUSED
                return
            try:
                self._download_once(task)
                return  # success (or paused/cancelled inside)
            except DownloadError as exc:
                if exc.args and exc.args[0] == "transient":
                    last_error = exc
                    retry_after = self._retry_after_seconds(str(exc))
                    self.logger.warning(
                        "Download %s transient failure (attempt %d): %s",
                        task.id, attempt + 1, exc)
                    self._sleep_backoff(attempt, retry_after)
                    continue
                raise
            except (requests.RequestException, OSError) as exc:
                last_error = exc
                self.logger.warning(
                    "Download %s network failure (attempt %d): %s",
                    task.id, attempt + 1, exc)
                self._sleep_backoff(attempt, None)
        raise DownloadError(f"failed after {MAX_RETRIES + 1} attempts: {last_error}")

    def _download_once(self, task: DownloadTask) -> None:
        # Re-probe when size/range support is unknown (e.g. after restart).
        if task.total_bytes is None or task.supports_range is None:
            total, supports_range, _name, _ctype = self._probe(
                task.url, headers=task.headers, cookies=task.cookies,
                proxy=task.proxy, auth=task.auth,
            )
            if task.total_bytes is None:
                task.total_bytes = total
            task.supports_range = supports_range

        if task.total_bytes and not check_disk_space(task.dest_path, task.total_bytes):
            raise DownloadError("insufficient disk space for download")

        # Build / restore segments.
        if not task.segments:
            ranges = self._decide_segments(
                task.total_bytes, bool(task.supports_range), task.connections)
            task.segments = [Segment(i, s, e) for i, (s, e) in enumerate(ranges)]
            # Resume into existing .part bytes for a single-segment file.
            part = Path(task.part_path)
            if len(task.segments) == 1 and part.exists() and task.total_bytes:
                already = min(part.stat().st_size, task.total_bytes)
                task.segments[0].downloaded = already
                task.downloaded_bytes = already
            elif len(task.segments) > 1 and part.exists():
                # Multi-segment resume: distribute existing bytes per segment.
                size = part.stat().st_size
                for seg in task.segments:
                    seg.downloaded = max(0, min(seg.length or 0, size - seg.start))
                    seg.downloaded = min(seg.downloaded, seg.length or 0)
                task.downloaded_bytes = sum(s.downloaded for s in task.segments)
        task.segment_progress = [s.downloaded for s in task.segments]
        self._persist_segments(task)

        # Pre-allocate the .part file when the total size is known.
        part_path = Path(task.part_path)
        part_path.parent.mkdir(parents=True, exist_ok=True)
        with self._file_lock(task.id):
            if task.total_bytes:
                with open(part_path, "ab") as f:
                    f.truncate(task.total_bytes)
            else:
                part_path.touch(exist_ok=True)

        session = self._make_session(task)
        try:
            with ThreadPoolExecutor(max_workers=len(task.segments),
                                    thread_name_prefix="segment") as pool:
                futures = [pool.submit(self._download_segment, task, seg, session)
                           for seg in task.segments]
                for fut in futures:
                    fut.result()  # propagates segment errors
        finally:
            session.close()

        if self._is_cancelled(task):
            task.status = STATUS_CANCELLED
            self._persist(task)
            return
        if self._is_paused(task):
            task.status = STATUS_PAUSED
            self._persist(task)
            self._persist_segments(task)
            return

        self._finalize(task)

    def _download_segment(self, task: DownloadTask, seg: Segment,
                          session: requests.Session) -> None:
        """Download one segment, writing at its offset into the .part file."""
        seg.status = "active"
        limiter = SpeedLimiter((task.speed_limit_kbps or 0) * 1024)
        while True:
            if seg.length is not None and seg.downloaded >= seg.length:
                seg.status = "done"
                return
            if self._is_cancelled(task) or self._is_paused(task):
                seg.status = "pending"
                return
            start = seg.start + seg.downloaded
            headers = dict(task.headers or {})
            if seg.end >= 0:
                headers["Range"] = f"bytes={start}-{seg.end}"
            elif start > 0:
                headers["Range"] = f"bytes={start}-"
            try:
                resp = session.get(task.url, headers=headers, stream=True,
                                   allow_redirects=True, **self._session_kwargs(task))
            except requests.RequestException as exc:
                raise DownloadError("transient", str(exc)) from exc
            try:
                self._raise_for_status(resp)
                if seg.end >= 0 and resp.status_code == 200:
                    # Server ignored Range: only valid for a single segment
                    # starting at 0; otherwise the data would be corrupt.
                    if not (len(task.segments) == 1 and start == 0):
                        raise DownloadError("server does not support Range requests")
                with self._file_lock(task.id):
                    f = open(task.part_path, "r+b")
                try:
                    for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                        if self._is_cancelled(task) or self._is_paused(task):
                            seg.status = "pending"
                            return
                        if not chunk:
                            continue
                        self.global_limiter.acquire(len(chunk))
                        limiter.acquire(len(chunk))
                        with self._file_lock(task.id):
                            # Absolute offset: segment start + bytes already
                            # downloaded within this segment.
                            f.seek(seg.start + seg.downloaded)
                            f.write(chunk)
                            f.flush()
                        seg.downloaded += len(chunk)
                        with self._lock:
                            task.downloaded_bytes = sum(
                                s.downloaded for s in task.segments)
                            task.segment_progress = [s.downloaded for s in task.segments]
                        task.sample_speed()
                        self._emit_progress(task)
                finally:
                    f.close()
                # Segment finished (or server closed the stream).
                if seg.length is None:
                    seg.status = "done"
                    return
                if seg.downloaded >= seg.length:
                    seg.status = "done"
                    return
                # Stream ended early: loop around and resume the remainder.
            finally:
                resp.close()

    def _finalize(self, task: DownloadTask) -> None:
        """Verify checksum, fsync, and atomically rename .part -> final."""
        part_path = Path(task.part_path)
        with self._file_lock(task.id):
            with open(part_path, "r+b") as f:
                f.flush()
                os.fsync(f.fileno())
        if task.total_bytes and part_path.stat().st_size != task.total_bytes:
            raise DownloadError(
                f"size mismatch: got {part_path.stat().st_size}, "
                f"expected {task.total_bytes}")
        if task.checksum:
            if not self._checksum_ok(part_path, task.checksum, task.checksum_type):
                raise DownloadError(
                    f"checksum mismatch ({task.checksum_type})")
        dest = Path(task.dest_path)
        if dest.exists():
            dest = resolve_collision(dest)
            task.dest_path = str(dest)
            task.filename = dest.name
        os.replace(part_path, dest)
        task.downloaded_bytes = dest.stat().st_size
        task.status = STATUS_COMPLETED
        task.error = None
        self._persist(task)
        self.logger.info("Download completed: %s", task.dest_path)

    # -- helpers -------------------------------------------------------
    def _get(self, task_id: str) -> DownloadTask:
        task = self._tasks.get(task_id)
        if task is None:
            if self.store is not None:
                record = self.store.get_task(task_id)
                if record is not None:
                    task = DownloadTask.from_record(
                        record, connections=self.settings.connections_per_download,
                        allow_local=self.allow_local)
                    self._tasks[task.id] = task
                    return task
            raise DownloadError(f"unknown task id: {task_id}")
        return task

    def _file_lock(self, task_id: str) -> threading.Lock:
        with self._lock:
            return self._file_locks.setdefault(task_id, threading.Lock())

    def _is_paused(self, task: DownloadTask) -> bool:
        event = self._pause_events.get(task.id)
        return bool(event is not None and event.is_set())

    def _is_cancelled(self, task: DownloadTask) -> bool:
        event = self._cancel_events.get(task.id)
        return bool(event is not None and event.is_set())

    def _persist(self, task: DownloadTask) -> None:
        if self.store is not None:
            task.updated_at = time.time()
            self.store.save_task(task.to_record())

    def _persist_segments(self, task: DownloadTask) -> None:
        if self.store is not None and task.segments:
            self.store.save_segments(
                task.id, [s.to_record(task.id) for s in task.segments])

    def _emit_progress(self, task: DownloadTask) -> None:
        if self.on_progress is None:
            return
        now = time.monotonic()
        if task.status in TERMINAL_STATUSES or now - self._last_progress_cb >= 0.25:
            self._last_progress_cb = now
            try:
                self.on_progress(task)
            except Exception:
                pass

    @staticmethod
    def _retry_after_seconds(message: str) -> Optional[float]:
        m = re.search(r"Retry-After:\s*(\d+)", message)
        return float(m.group(1)) if m else None

    @staticmethod
    def _sleep_backoff(attempt: int, retry_after: Optional[float]) -> None:
        if retry_after is not None:
            time.sleep(min(retry_after, 60))
            return
        delay = BACKOFF_BASE * (BACKOFF_FACTOR ** attempt)
        time.sleep(min(delay, 30))
