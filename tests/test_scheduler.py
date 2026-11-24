"""Tests for the Scheduler (pure time-window logic)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tserver_downloader.engine import Scheduler


def _dt(hour: int) -> datetime:
    return datetime(2026, 11, 24, hour, 0, 0, tzinfo=timezone.utc)


def test_should_run_inside_window():
    sched = Scheduler()
    sched.add_schedule("t1", _dt(2), _dt(5))
    assert sched.should_run("t1", _dt(3)) is True
    assert sched.should_run("t1", _dt(2)) is True  # inclusive start
    assert sched.should_run("t1", _dt(5)) is True  # inclusive stop
    assert sched.should_run("t1", _dt(1)) is False
    assert sched.should_run("t1", _dt(6)) is False


def test_should_run_unknown_task():
    assert Scheduler().should_run("nope", _dt(3)) is False


def test_due_tasks_returns_window_members_in_start_order():
    sched = Scheduler()
    sched.add_schedule("late", _dt(3), _dt(6))
    sched.add_schedule("early", _dt(1), _dt(4))
    sched.add_schedule("outside", _dt(10), _dt(12))
    assert sched.due_tasks(_dt(3)) == ["early", "late"]
    assert sched.due_tasks(_dt(5)) == ["late"]
    assert sched.due_tasks(_dt(8)) == []


def test_due_tasks_accepts_epoch_seconds():
    sched = Scheduler()
    start = _dt(2).timestamp()
    stop = _dt(5).timestamp()
    sched.add_schedule("t1", start, stop)
    assert sched.due_tasks(start + 3600) == ["t1"]
    assert sched.should_run("t1", start + 7200) is True
    assert sched.should_run("t1", stop + 1) is False


def test_inverted_window_rejected():
    sched = Scheduler()
    with pytest.raises(ValueError):
        sched.add_schedule("t1", _dt(5), _dt(2))


def test_remove_schedule():
    sched = Scheduler()
    sched.add_schedule("t1", _dt(1), _dt(4))
    sched.remove_schedule("t1")
    assert sched.should_run("t1", _dt(2)) is False
    assert sched.due_tasks(_dt(2)) == []


def test_overwrite_schedule():
    sched = Scheduler()
    sched.add_schedule("t1", _dt(1), _dt(2))
    sched.add_schedule("t1", _dt(10), _dt(12))
    assert sched.should_run("t1", _dt(1)) is False
    assert sched.should_run("t1", _dt(11)) is True


def test_zero_length_window():
    sched = Scheduler()
    sched.add_schedule("t1", _dt(3), _dt(3))
    assert sched.should_run("t1", _dt(3)) is True
    assert sched.should_run("t1", _dt(4)) is False


def test_scheduler_with_real_now():
    sched = Scheduler()
    now = datetime.now(timezone.utc)
    sched.add_schedule("t1", now - timedelta(minutes=1), now + timedelta(minutes=1))
    assert sched.should_run("t1", now) is True
    assert sched.due_tasks(now) == ["t1"]
