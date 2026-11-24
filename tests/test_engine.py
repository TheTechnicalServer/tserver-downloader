"""Tests for the download engine (live local HTTP server)."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

import pytest

from tserver_downloader.config import Settings
from tserver_downloader.engine import (
    DownloadEngine,
    DownloadError,
    QueueManager,
    SpeedLimiter,
    Status,
)
from tserver_downloader.state import StateStore


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_normal_download(engine, http_server, tmp_download_dir, data_sha256):
    task_id = engine.add(f"{http_server}/file.bin")
    engine.start(task_id)
    assert engine.wait(task_id, timeout=30) == Status.COMPLETED
    task = engine.get_task(task_id)
    final = Path(task.dest_path)
    assert final.exists()
    assert not Path(task.part_path).exists()
    assert _sha256(final) == data_sha256
    assert task.progress == pytest.approx(1.0)


def test_segmented_download_hash_matches(engine, http_server, data_sha256):
    task_id = engine.add(f"{http_server}/file.bin", connections=4)
    engine.start(task_id)
    assert engine.wait(task_id, timeout=30) == Status.COMPLETED
    task = engine.get_task(task_id)
    assert len(task.segments) > 1
    assert _sha256(Path(task.dest_path)) == data_sha256


def test_pause_resume_slow_file(engine, http_server, tmp_download_dir, data_sha256):
    task_id = engine.add(f"{http_server}/slow.bin", connections=2)
    engine.start(task_id)
    time.sleep(0.7)
    task = engine.get_task(task_id)
    assert task.status == Status.ACTIVE
    assert 0 < task.downloaded_bytes < task.total_bytes
    engine.pause(task_id)
    task = engine.get_task(task_id)
    assert task.status == Status.PAUSED
    assert 0 < task.downloaded_bytes < task.total_bytes
    assert Path(task.part_path).exists()
    engine.resume(task_id)
    assert engine.wait(task_id, timeout=60) == Status.COMPLETED
    assert _sha256(Path(task.dest_path)) == data_sha256


def test_no_range_server_single_segment(engine, http_server, data_sha256):
    task_id = engine.add(f"{http_server}/no-range.bin", connections=4)
    engine.start(task_id)
    assert engine.wait(task_id, timeout=30) == Status.COMPLETED
    task = engine.get_task(task_id)
    assert len(task.segments) == 1
    assert _sha256(Path(task.dest_path)) == data_sha256


def test_redirect_followed(engine, http_server, data_sha256):
    task_id = engine.add(f"{http_server}/redirect")
    engine.start(task_id)
    assert engine.wait(task_id, timeout=30) == Status.COMPLETED
    assert _sha256(Path(engine.get_task(task_id).dest_path)) == data_sha256


def test_retry_on_flaky_server(engine, http_server, data_sha256):
    task_id = engine.add(f"{http_server}/flaky.bin")
    engine.start(task_id)
    assert engine.wait(task_id, timeout=60) == Status.COMPLETED
    assert _sha256(Path(engine.get_task(task_id).dest_path)) == data_sha256


def test_404_error(engine, http_server):
    with pytest.raises(DownloadError, match="404"):
        engine.add(f"{http_server}/notfound")


def test_auth_download(engine, http_server, data_sha256):
    task_id = engine.add(f"{http_server}/auth.bin", auth=("testuser", "testpass"))
    engine.start(task_id)
    assert engine.wait(task_id, timeout=30) == Status.COMPLETED
    assert _sha256(Path(engine.get_task(task_id).dest_path)) == data_sha256


def test_duplicate_detection(engine, http_server):
    first = engine.add(f"{http_server}/file.bin")
    second = engine.add(f"{http_server}/file.bin")
    assert first == second
    assert len(engine.list_tasks()) == 1


def test_duplicate_detection_after_complete(engine, http_server):
    task_id = engine.add(f"{http_server}/file.bin")
    engine.start(task_id)
    assert engine.wait(task_id, timeout=30) == Status.COMPLETED
    again = engine.add(f"{http_server}/file.bin")
    assert again == task_id
    assert engine.get_task(again).status == Status.COMPLETED


def test_checksum_verification_pass(engine, http_server, data_sha256):
    task_id = engine.add(f"{http_server}/file.bin", checksum=data_sha256)
    engine.start(task_id)
    assert engine.wait(task_id, timeout=30) == Status.COMPLETED


def test_checksum_verification_fail(engine, http_server):
    task_id = engine.add(f"{http_server}/file.bin", checksum="0" * 64)
    engine.start(task_id)
    assert engine.wait(task_id, timeout=30) == Status.FAILED
    task = engine.get_task(task_id)
    assert "checksum" in (task.error or "").lower()
    assert not Path(task.dest_path).exists()


def test_restart_recovery(engine, http_server, tmp_path, tmp_download_dir,
                          data_sha256):
    task_id = engine.add(f"{http_server}/slow.bin", connections=2)
    engine.start(task_id)
    time.sleep(0.7)
    engine.pause(task_id)
    assert engine.get_task(task_id).status == Status.PAUSED

    # A brand-new engine over the same store resumes and finishes.
    settings = Settings(download_dir=str(tmp_download_dir),
                        connections_per_download=4, max_simultaneous=3)
    store = StateStore(tmp_path / "state.db")
    engine2 = DownloadEngine(settings, store, allow_local=True)
    try:
        engine2.resume(task_id)
        assert engine2.wait(task_id, timeout=60) == Status.COMPLETED
        assert _sha256(Path(engine2.get_task(task_id).dest_path)) == data_sha256
    finally:
        store.close()


def test_queue_priority_ordering(http_server, tmp_path, tmp_download_dir):
    settings = Settings(download_dir=str(tmp_download_dir),
                        connections_per_download=2, max_simultaneous=1)
    store = StateStore(tmp_path / "state.db")
    eng = DownloadEngine(settings, store, allow_local=True)
    try:
        slow = eng.add(f"{http_server}/slow.bin", priority=0)
        high = eng.add(f"{http_server}/file.bin", filename="high.bin", priority=10)
        medium = eng.add(f"{http_server}/file.bin", filename="medium.bin", priority=5)
        eng.start(slow)
        eng.start(high)
        eng.start(medium)
        assert eng.wait(slow, timeout=30) == Status.COMPLETED
        assert eng.wait(high, timeout=30) == Status.COMPLETED
        assert eng.wait(medium, timeout=30) == Status.COMPLETED
        # High priority must finish before medium priority.
        assert eng.get_task(high).updated_at < eng.get_task(medium).updated_at
    finally:
        store.close()


def test_queue_manager_ordering():
    from tserver_downloader.engine import DownloadTask
    qm = QueueManager()
    low = DownloadTask(id="low", url="http://example.com/a", filename="a",
                       dest_path="/tmp/a", priority=1)
    high = DownloadTask(id="high", url="http://example.com/b", filename="b",
                        dest_path="/tmp/b", priority=10)
    mid = DownloadTask(id="mid", url="http://example.com/c", filename="c",
                       dest_path="/tmp/c", priority=5)
    qm.add(low)
    qm.add(high)
    qm.add(mid)
    assert qm.pop_next() == "high"
    assert qm.pop_next() == "mid"
    assert qm.pop_next() == "low"
    assert qm.pop_next() is None


def test_speed_limiter_token_bucket():
    limiter = SpeedLimiter(1_000_000)  # 1 MB/s
    start = time.monotonic()
    limiter.acquire(200_000)  # needs ~0.2s of budget from an empty bucket
    elapsed = time.monotonic() - start
    assert elapsed >= 0.15
    assert elapsed < 2.0
    # Unlimited limiter returns immediately.
    fast = SpeedLimiter(0)
    start = time.monotonic()
    fast.acquire(10_000_000)
    assert time.monotonic() - start < 0.1


def test_invalid_urls_rejected_by_engine(engine, tmp_path, tmp_download_dir):
    with pytest.raises(ValueError):
        engine.add("file:///etc/passwd")
    with pytest.raises(ValueError):
        engine.add("javascript:alert(1)")
    # With local hosts disallowed, the cloud metadata address is rejected
    # before any network access is attempted.
    settings = Settings(download_dir=str(tmp_download_dir),
                        connections_per_download=2, max_simultaneous=1)
    store = StateStore(tmp_path / "strict.db")
    strict = DownloadEngine(settings, store, allow_local=False)
    try:
        with pytest.raises(ValueError):
            strict.add("http://169.254.169.254/latest/meta-data/")
        with pytest.raises(ValueError):
            strict.add("http://127.0.0.1/file.bin")
    finally:
        store.close()
