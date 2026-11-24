"""Utility helpers: filename safety, URL validation, formatting, hashing."""

from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import shutil
import urllib.parse
from pathlib import Path

# Characters illegal on Windows / problematic on Unix filenames.
_ILLEGAL_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_MAX_FILENAME_LEN = 200


def sanitize_filename(name: str) -> str:
    """Return a filesystem-safe filename (no path separators, no traversal).

    Empty or dot-only results fall back to ``"download"``. The result is
    truncated to a safe length while preserving the extension.
    """
    if not name:
        return "download"
    # Strip any directory components the caller may have smuggled in.
    name = name.replace("\\", "/")
    name = name.rsplit("/", 1)[-1].strip()
    name = _ILLEGAL_RE.sub("_", name)
    name = name.strip(" .")
    if not name or name in {".", ".."}:
        return "download"
    stem, dot, ext = name.rpartition(".")
    if not stem:  # e.g. ".gitignore" — keep as-is
        stem, ext, dot = name, "", ""
    if stem.upper() in _RESERVED_NAMES:
        stem = "_" + stem
    name = stem + (dot + ext if dot else "")
    if len(name.encode("utf-8")) > _MAX_FILENAME_LEN:
        # Truncate the stem, keeping the extension.
        ext_bytes = len(ext.encode("utf-8")) + (1 if dot else 0)
        keep = max(1, _MAX_FILENAME_LEN - ext_bytes)
        stem_bytes = stem.encode("utf-8")[:keep].decode("utf-8", "ignore").rstrip(". ")
        name = stem_bytes + (dot + ext if dot else "")
    return name or "download"


def resolve_collision(path: str | Path) -> Path:
    """Return a non-existing path, appending ``(1)``, ``(2)``, ... if needed."""
    path = Path(path)
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    n = 1
    while True:
        candidate = path.with_name(f"{stem} ({n}){suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def is_safe_path(base: str | Path, path: str | Path) -> bool:
    """True if ``path`` resolves inside ``base`` (no traversal escape)."""
    base = Path(base).resolve()
    try:
        resolved = (base / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
    except (ValueError, OSError):
        return False
    return resolved == base or base in resolved.parents


def safe_join(base: str | Path, *parts: str) -> Path:
    """Join path parts under ``base``, raising ValueError on traversal."""
    base = Path(base)
    result = base
    for part in parts:
        part = str(part).replace("\\", "/")
        result = result / part
    resolved = result.resolve()
    base_resolved = base.resolve()
    if resolved != base_resolved and base_resolved not in resolved.parents:
        raise ValueError(f"unsafe path: {result} escapes {base}")
    return resolved


def validate_url(url: str, allow_local: bool = False) -> str:
    """Validate a download URL. Returns the normalised URL.

    Raises ValueError for non-http/https schemes, missing hosts, credentials
    in the URL, path traversal, and (unless ``allow_local``) private,
    loopback, link-local, multicast or reserved IP hosts.
    """
    if not url or not isinstance(url, str):
        raise ValueError("URL must be a non-empty string")
    url = url.strip()
    parsed = urllib.parse.urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"unsupported URL scheme: {scheme or '(none)'}")
    if parsed.username or parsed.password:
        raise ValueError("URLs with embedded credentials are not allowed")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise ValueError("URL has no host")
    if ".." in parsed.path or ".." in urllib.parse.unquote(parsed.path):
        raise ValueError("URL path contains traversal")
    if not allow_local:
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None
        if ip is not None:
            if (
                ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified
            ):
                raise ValueError(f"private/local IP host not allowed: {host}")
        elif host in ("localhost",) or host.endswith(".local") or host.endswith(".localhost"):
            raise ValueError(f"local host not allowed: {host}")
    return url


def format_size(n: int | float | None) -> str:
    """Human-readable byte size, e.g. 1.5 MB. ``None`` -> 'Unknown'."""
    if n is None:
        return "Unknown"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(n)} B"
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def format_speed(bps: float | None) -> str:
    """Human-readable transfer speed, e.g. 2.3 MB/s."""
    if bps is None or bps <= 0:
        return "0 B/s"
    return f"{format_size(bps)}/s"


def format_eta(seconds: float | None) -> str:
    """Human-readable ETA, e.g. 1h 02m 03s. ``None``/inf -> '--'."""
    if seconds is None:
        return "--"
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        return "--"
    if seconds != seconds or seconds in (float("inf"), float("-inf")) or seconds < 0:
        return "--"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def _hash_file(path: str | Path, algo: str) -> str:
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_file(path: str | Path) -> str:
    """SHA-256 hex digest of a file."""
    return _hash_file(path, "sha256")


def md5_file(path: str | Path) -> str:
    """MD5 hex digest of a file."""
    return _hash_file(path, "md5")


def check_disk_space(path: str | Path, needed_bytes: int) -> bool:
    """True if the filesystem holding ``path`` has >= ``needed_bytes`` free."""
    target = Path(path)
    if not target.exists():
        target = target.parent
        while not target.exists():
            target = target.parent
    try:
        free = shutil.disk_usage(str(target)).free
    except OSError:
        return False
    return free >= max(0, int(needed_bytes))
