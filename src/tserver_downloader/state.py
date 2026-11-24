"""Persistent download state in SQLite (WAL mode)."""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

# Task status values — kept in sync with engine.Status.
STATUS_QUEUED = "queued"
STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"


@dataclass
class DownloadRecord:
    """One download task as stored in SQLite."""

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


@dataclass
class SegmentRecord:
    """One byte-range segment of a download."""

    download_id: str
    index: int
    start: int
    end: int  # inclusive; -1 means "to end of file / unknown"
    downloaded: int = 0
    status: str = "pending"  # pending | active | done


_SCHEMA = """
CREATE TABLE IF NOT EXISTS downloads (
    id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    filename TEXT NOT NULL,
    dest_path TEXT NOT NULL,
    total_bytes INTEGER,
    downloaded_bytes INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'queued',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    category TEXT NOT NULL DEFAULT 'Other',
    checksum TEXT,
    checksum_type TEXT NOT NULL DEFAULT 'sha256',
    error TEXT
);
CREATE TABLE IF NOT EXISTS segments (
    download_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    start INTEGER NOT NULL,
    end INTEGER NOT NULL,
    downloaded INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    PRIMARY KEY (download_id, idx),
    FOREIGN KEY (download_id) REFERENCES downloads(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_downloads_status ON downloads(status);
CREATE INDEX IF NOT EXISTS idx_downloads_url ON downloads(url);
"""


class StateStore:
    """Thread-safe SQLite store for download tasks and their segments."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # -- tasks -------------------------------------------------------
    def save_task(self, record: DownloadRecord) -> None:
        """Insert or replace a task record (single transaction)."""
        record.updated_at = time.time()
        with self._lock:
            with self._conn:
                self._conn.execute(
                    """INSERT INTO downloads
                       (id, url, filename, dest_path, total_bytes, downloaded_bytes,
                        status, created_at, updated_at, priority, category,
                        checksum, checksum_type, error)
                       VALUES (:id, :url, :filename, :dest_path, :total_bytes,
                        :downloaded_bytes, :status, :created_at, :updated_at,
                        :priority, :category, :checksum, :checksum_type, :error)
                       ON CONFLICT(id) DO UPDATE SET
                        url=excluded.url, filename=excluded.filename,
                        dest_path=excluded.dest_path,
                        total_bytes=excluded.total_bytes,
                        downloaded_bytes=excluded.downloaded_bytes,
                        status=excluded.status, updated_at=excluded.updated_at,
                        priority=excluded.priority, category=excluded.category,
                        checksum=excluded.checksum,
                        checksum_type=excluded.checksum_type,
                        error=excluded.error""",
                    asdict(record),
                )

    def get_task(self, task_id: str) -> Optional[DownloadRecord]:
        """Return the task record, or None if it does not exist."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM downloads WHERE id = ?", (task_id,)
            ).fetchone()
        return self._row_to_record(row) if row else None

    def list_tasks(self, status: Optional[str] = None) -> list[DownloadRecord]:
        """List tasks, optionally filtered by status, newest first."""
        with self._lock:
            if status is None:
                rows = self._conn.execute(
                    "SELECT * FROM downloads ORDER BY created_at DESC"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM downloads WHERE status = ? ORDER BY created_at DESC",
                    (status,),
                ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def update_progress(self, task_id: str, downloaded_bytes: int,
                        status: Optional[str] = None,
                        error: Optional[str] = None) -> None:
        """Update a task's progress (and optionally status/error)."""
        with self._lock:
            with self._conn:
                if status is not None:
                    self._conn.execute(
                        """UPDATE downloads SET downloaded_bytes = ?,
                           status = ?, error = ?, updated_at = ? WHERE id = ?""",
                        (downloaded_bytes, status, error, time.time(), task_id),
                    )
                else:
                    self._conn.execute(
                        """UPDATE downloads SET downloaded_bytes = ?,
                           updated_at = ? WHERE id = ?""",
                        (downloaded_bytes, time.time(), task_id),
                    )

    def set_status(self, task_id: str, status: str,
                   error: Optional[str] = None) -> None:
        """Set a task's status (and optional error message)."""
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "UPDATE downloads SET status = ?, error = ?, updated_at = ? WHERE id = ?",
                    (status, error, time.time(), task_id),
                )

    def delete_task(self, task_id: str) -> None:
        """Delete a task and its segments."""
        with self._lock:
            with self._conn:
                self._conn.execute("DELETE FROM segments WHERE download_id = ?", (task_id,))
                self._conn.execute("DELETE FROM downloads WHERE id = ?", (task_id,))

    # -- segments ----------------------------------------------------
    def save_segments(self, download_id: str, segments: list[SegmentRecord]) -> None:
        """Replace all segments for a download (single transaction)."""
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "DELETE FROM segments WHERE download_id = ?", (download_id,)
                )
                for seg in segments:
                    self._conn.execute(
                        """INSERT INTO segments
                           (download_id, idx, start, end, downloaded, status)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (download_id, seg.index, seg.start, seg.end,
                         seg.downloaded, seg.status),
                    )

    def update_segment(self, download_id: str, index: int, downloaded: int,
                       status: Optional[str] = None) -> None:
        """Update one segment's downloaded count / status."""
        with self._lock:
            with self._conn:
                if status is not None:
                    self._conn.execute(
                        """UPDATE segments SET downloaded = ?, status = ?
                           WHERE download_id = ? AND idx = ?""",
                        (downloaded, status, download_id, index),
                    )
                else:
                    self._conn.execute(
                        "UPDATE segments SET downloaded = ? WHERE download_id = ? AND idx = ?",
                        (downloaded, download_id, index),
                    )

    def get_segments(self, download_id: str) -> list[SegmentRecord]:
        """Return segments for a download, ordered by index."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM segments WHERE download_id = ? ORDER BY idx",
                (download_id,),
            ).fetchall()
        return [
            SegmentRecord(
                download_id=r["download_id"], index=r["idx"], start=r["start"],
                end=r["end"], downloaded=r["downloaded"], status=r["status"],
            )
            for r in rows
        ]

    def close(self) -> None:
        """Close the database connection."""
        with self._lock:
            self._conn.close()

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> DownloadRecord:
        return DownloadRecord(
            id=row["id"], url=row["url"], filename=row["filename"],
            dest_path=row["dest_path"], total_bytes=row["total_bytes"],
            downloaded_bytes=row["downloaded_bytes"], status=row["status"],
            created_at=row["created_at"], updated_at=row["updated_at"],
            priority=row["priority"], category=row["category"],
            checksum=row["checksum"], checksum_type=row["checksum_type"],
            error=row["error"],
        )
