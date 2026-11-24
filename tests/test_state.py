"""Tests for the SQLite StateStore."""

from __future__ import annotations

import pytest

from tserver_downloader.state import (
    DownloadRecord,
    SegmentRecord,
    StateStore,
)


@pytest.fixture()
def store(tmp_path):
    s = StateStore(tmp_path / "test.db")
    yield s
    s.close()


def _record(task_id="t1", **overrides):
    data = dict(
        id=task_id, url="http://example.com/file.bin", filename="file.bin",
        dest_path="/tmp/downloads/file.bin", total_bytes=1000,
        downloaded_bytes=0, status="queued", priority=0, category="Video",
    )
    data.update(overrides)
    return DownloadRecord(**data)


def test_save_and_get_task(store):
    store.save_task(_record())
    got = store.get_task("t1")
    assert got is not None
    assert got.url == "http://example.com/file.bin"
    assert got.total_bytes == 1000
    assert got.status == "queued"
    assert store.get_task("missing") is None


def test_save_task_upserts(store):
    store.save_task(_record(downloaded_bytes=100))
    store.save_task(_record(downloaded_bytes=400, status="active"))
    got = store.get_task("t1")
    assert got.downloaded_bytes == 400
    assert got.status == "active"
    assert len(store.list_tasks()) == 1


def test_list_tasks_filter_by_status(store):
    store.save_task(_record("a", status="queued"))
    store.save_task(_record("b", status="completed"))
    store.save_task(_record("c", status="paused"))
    assert len(store.list_tasks()) == 3
    assert [t.id for t in store.list_tasks(status="completed")] == ["b"]
    assert [t.id for t in store.list_tasks(status="queued")] == ["a"]
    assert store.list_tasks(status="failed") == []


def test_update_progress(store):
    store.save_task(_record())
    store.update_progress("t1", 750)
    got = store.get_task("t1")
    assert got.downloaded_bytes == 750
    assert got.status == "queued"  # unchanged when no status given
    store.update_progress("t1", 1000, status="completed")
    got = store.get_task("t1")
    assert got.downloaded_bytes == 1000
    assert got.status == "completed"


def test_segments_roundtrip(store):
    store.save_task(_record())
    segs = [
        SegmentRecord("t1", 0, 0, 499, downloaded=500, status="done"),
        SegmentRecord("t1", 1, 500, 999, downloaded=120, status="active"),
    ]
    store.save_segments("t1", segs)
    got = store.get_segments("t1")
    assert len(got) == 2
    assert got[0].start == 0 and got[0].end == 499
    assert got[0].downloaded == 500 and got[0].status == "done"
    assert got[1].downloaded == 120
    # Saving again replaces the previous segments.
    store.save_segments("t1", [SegmentRecord("t1", 0, 0, 999)])
    assert len(store.get_segments("t1")) == 1


def test_update_segment(store):
    store.save_task(_record())
    store.save_segments("t1", [SegmentRecord("t1", 0, 0, 999)])
    store.update_segment("t1", 0, 250, status="active")
    seg = store.get_segments("t1")[0]
    assert seg.downloaded == 250
    assert seg.status == "active"


def test_delete_task_removes_segments(store):
    store.save_task(_record())
    store.save_segments("t1", [SegmentRecord("t1", 0, 0, 999)])
    store.delete_task("t1")
    assert store.get_task("t1") is None
    assert store.get_segments("t1") == []


def test_persistence_across_reopen(tmp_path):
    path = tmp_path / "persist.db"
    s1 = StateStore(path)
    s1.save_task(_record(downloaded_bytes=321, status="paused"))
    s1.save_segments("t1", [SegmentRecord("t1", 0, 0, 999, downloaded=321)])
    s1.close()
    s2 = StateStore(path)
    got = s2.get_task("t1")
    assert got.downloaded_bytes == 321
    assert got.status == "paused"
    assert s2.get_segments("t1")[0].downloaded == 321
    s2.close()
