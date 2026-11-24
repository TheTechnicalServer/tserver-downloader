"""Integration tests for cross-module gaps fixed after the phase build:
Settings theme/proxy/update fields, AdBlockEngine enable/disable,
engine-owned scheduler polling, and shutdown-after-complete (no-op off Windows).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from tserver_downloader.adblock.engine import AdBlockEngine
from tserver_downloader.adblock.lists import load_bundled_lists
from tserver_downloader.config import Settings
from tserver_downloader.engine import DownloadEngine


def test_settings_new_fields_round_trip(tmp_path):
    s = Settings(config_path=tmp_path / "settings.json")
    s.theme = "dark"
    s.proxy_enabled = True
    s.proxy_host = "proxy.example.com"
    s.proxy_port = 8080
    s.proxy_user = "user1"
    s.auto_update_check = False
    s.update_feed_url = "https://example.com/feed.json"
    s.save()
    loaded = Settings.load(tmp_path / "settings.json")
    assert loaded.theme == "dark"
    assert loaded.proxy_enabled is True
    assert loaded.proxy_host == "proxy.example.com"
    assert loaded.proxy_port == 8080
    assert loaded.proxy_user == "user1"
    assert loaded.auto_update_check is False
    assert loaded.update_feed_url == "https://example.com/feed.json"
    # Blog allowlist survives a round-trip and defaults are kept.
    assert "thetechnicalserver.blogspot.com" in loaded.adblock_allowlist


def test_adblock_enable_disable():
    engine = AdBlockEngine()
    engine.load_lists(load_bundled_lists())
    assert engine.is_enabled() is True
    assert engine.should_block("https://doubleclick.net/ads.js") is True
    engine.disable()
    assert engine.is_enabled() is False
    assert engine.should_block("https://doubleclick.net/ads.js") is False
    engine.enable()
    assert engine.should_block("https://doubleclick.net/ads.js") is True
    # Blog is never blocked, enabled or not.
    assert engine.should_block("https://thetechnicalserver.blogspot.com/") is False


def test_engine_check_schedules_starts_due_task(engine, http_server):
    """check_schedules() starts a queued task inside its window."""
    engine.settings.scheduler_enabled = True
    task_id = engine.add(http_server + "/file.bin")
    engine.pause(task_id)  # queued -> paused state is startable by scheduler
    now = datetime.now()
    engine.scheduler.add_schedule(task_id, now - timedelta(minutes=1),
                                  now + timedelta(minutes=10))
    started = engine.check_schedules(now)
    assert task_id in started
    assert engine.wait(task_id, timeout=30) == "completed"


def test_engine_check_schedules_disabled_is_noop(engine, http_server):
    engine.settings.scheduler_enabled = False
    task_id = engine.add(http_server + "/file.bin")
    engine.pause(task_id)
    now = datetime.now()
    engine.scheduler.add_schedule(task_id, now - timedelta(minutes=1),
                                  now + timedelta(minutes=10))
    assert engine.check_schedules(now) == []
    assert engine.get_task(task_id).status != "active"


def test_shutdown_after_complete_is_noop_on_linux(engine, http_server):
    """On Linux the shutdown action must only log, never power off."""
    engine.settings.shutdown_after_complete = "shutdown"
    task_id = engine.add(http_server + "/file.bin")
    engine.start(task_id)
    assert engine.wait(task_id, timeout=30) == "completed"
