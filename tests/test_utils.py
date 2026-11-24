"""Tests for utils: filenames, URLs, paths, formatting, hashing."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from tserver_downloader.utils import (
    check_disk_space,
    format_eta,
    format_size,
    format_speed,
    is_safe_path,
    md5_file,
    resolve_collision,
    safe_join,
    sanitize_filename,
    sha256_file,
    validate_url,
)


def test_sanitize_filename_strips_separators():
    # Directory components are dropped; only the basename is kept.
    assert sanitize_filename("a/b\\c:d?.txt") == "c_d_.txt"
    assert sanitize_filename("a:b?.txt") == "a_b_.txt"
    assert "/" not in sanitize_filename("../../etc/passwd")
    assert sanitize_filename("  spaced name.mp4  ") == "spaced name.mp4"


def test_sanitize_filename_edge_cases():
    assert sanitize_filename("") == "download"
    assert sanitize_filename("..") == "download"
    assert sanitize_filename("...") == "download"
    assert sanitize_filename("CON.txt") == "_CON.txt"
    assert sanitize_filename("nul") == "_nul"


def test_sanitize_filename_truncates_long_names():
    long_name = "x" * 300 + ".mp4"
    result = sanitize_filename(long_name)
    assert len(result.encode("utf-8")) <= 200
    assert result.endswith(".mp4")


def test_resolve_collision(tmp_path):
    target = tmp_path / "file.bin"
    assert resolve_collision(target) == target
    target.write_bytes(b"x")
    assert resolve_collision(target) == tmp_path / "file (1).bin"
    (tmp_path / "file (1).bin").write_bytes(b"x")
    assert resolve_collision(target) == tmp_path / "file (2).bin"


def test_validate_url_accepts_http_https():
    assert validate_url("https://example.com/file.mp4").startswith("https://")
    assert validate_url("http://example.com/a/b?x=1") == "http://example.com/a/b?x=1"


def test_validate_url_rejects_bad_schemes():
    for bad in ("file:///etc/passwd", "javascript:alert(1)",
                "data:text/html,hi", "ftp://example.com/f", "example.com/f", ""):
        with pytest.raises(ValueError):
            validate_url(bad)


def test_validate_url_rejects_traversal():
    with pytest.raises(ValueError):
        validate_url("http://example.com/../../etc/passwd")
    with pytest.raises(ValueError):
        validate_url("http://example.com/%2e%2e/%2e%2e/etc/passwd")


def test_validate_url_rejects_credentials():
    with pytest.raises(ValueError):
        validate_url("http://user:pass@example.com/file")


def test_validate_url_blocks_private_and_metadata():
    for bad in (
        "http://127.0.0.1/file",
        "http://localhost/file",
        "http://10.0.0.5/file",
        "http://192.168.1.1/file",
        "http://172.16.0.9/file",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/file",
    ):
        with pytest.raises(ValueError):
            validate_url(bad)
    # Explicit opt-in allows local hosts.
    assert validate_url("http://127.0.0.1/file", allow_local=True)
    assert validate_url("http://localhost/file", allow_local=True)


def test_is_safe_path(tmp_path):
    assert is_safe_path(tmp_path, "sub/file.txt")
    assert is_safe_path(tmp_path, "sub/../file.txt")
    assert not is_safe_path(tmp_path, "../escape.txt")
    assert not is_safe_path(tmp_path, "/etc/passwd")


def test_safe_join(tmp_path):
    assert safe_join(tmp_path, "a", "b.txt") == (tmp_path / "a" / "b.txt").resolve()
    with pytest.raises(ValueError):
        safe_join(tmp_path, "..", "escape.txt")
    with pytest.raises(ValueError):
        safe_join(tmp_path, "a/../../escape.txt")


def test_format_size():
    assert format_size(None) == "Unknown"
    assert format_size(0) == "0 B"
    assert format_size(512) == "512 B"
    assert format_size(1024) == "1.0 KB"
    assert format_size(1536) == "1.5 KB"
    assert format_size(1024 * 1024) == "1.0 MB"
    assert format_size(5 * 1024 * 1024) == "5.0 MB"


def test_format_speed():
    assert format_speed(0) == "0 B/s"
    assert format_speed(None) == "0 B/s"
    assert format_speed(2048) == "2.0 KB/s"
    assert format_speed(1024 * 1024) == "1.0 MB/s"


def test_format_eta():
    assert format_eta(None) == "--"
    assert format_eta(float("inf")) == "--"
    assert format_eta(0) == "0s"
    assert format_eta(45) == "45s"
    assert format_eta(125) == "2m 05s"
    assert format_eta(3661) == "1h 01m 01s"


def test_hash_files(tmp_path):
    f = tmp_path / "data.bin"
    f.write_bytes(b"hello world")
    assert sha256_file(f) == hashlib.sha256(b"hello world").hexdigest()
    assert md5_file(f) == hashlib.md5(b"hello world").hexdigest()


def test_check_disk_space(tmp_path):
    assert check_disk_space(tmp_path, 1) is True
    assert check_disk_space(tmp_path, 10**18) is False
    assert check_disk_space(tmp_path / "nonexistent" / "dir", 1) is True
