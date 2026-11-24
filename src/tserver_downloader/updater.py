"""Update checking: HTTPS-only manifest fetch + SHA-256 package verification.

Downloaded update bytes are only ever verified against an expected
SHA-256 hash. This module never executes downloaded code.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Optional

import requests

REQUEST_TIMEOUT = 30


@dataclass
class UpdateInfo:
    """Metadata about an available update."""

    version: str
    download_url: str
    sha256: str
    notes: str = ""
    mandatory: bool = False


def _parse_version(version: str) -> tuple[int, ...]:
    parts = []
    for piece in str(version).strip().lstrip("v").split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def is_newer_version(candidate: str, current: str) -> bool:
    """True if ``candidate`` is a strictly newer version than ``current``."""
    return _parse_version(candidate) > _parse_version(current)


def verify_update_package(data: bytes, expected_sha256: str) -> bool:
    """True iff the SHA-256 of ``data`` matches ``expected_sha256``."""
    if not data or not expected_sha256:
        return False
    actual = hashlib.sha256(data).hexdigest()
    return actual.lower() == expected_sha256.strip().lower()


def check_for_update(current_version: str, update_url: str,
                     timeout: int = REQUEST_TIMEOUT) -> Optional[UpdateInfo]:
    """Check an HTTPS update manifest for a newer release.

    Returns an :class:`UpdateInfo` when a newer version is advertised,
    or ``None`` when up to date / unreachable / malformed. Only HTTPS
    manifests are accepted. Raises ValueError for non-HTTPS URLs.
    """
    if not update_url.lower().startswith("https://"):
        raise ValueError("update URL must use HTTPS")
    try:
        resp = requests.get(update_url, timeout=timeout,
                            headers={"Accept": "application/json"})
        resp.raise_for_status()
        payload = resp.json()
    except (requests.RequestException, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    version = str(payload.get("version", "")).strip()
    download_url = str(payload.get("download_url", "")).strip()
    sha256 = str(payload.get("sha256", "")).strip()
    if not version or not download_url or not sha256:
        return None
    if not download_url.lower().startswith("https://"):
        return None
    if not is_newer_version(version, current_version):
        return None
    return UpdateInfo(
        version=version,
        download_url=download_url,
        sha256=sha256,
        notes=str(payload.get("notes", "")),
        mandatory=bool(payload.get("mandatory", False)),
    )


def download_update(info: UpdateInfo, timeout: int = 120) -> bytes:
    """Download an update package and verify its SHA-256.

    Returns the verified bytes. Raises ValueError if the hash does not
    match or the URL is not HTTPS. The bytes are never executed here.
    """
    if not info.download_url.lower().startswith("https://"):
        raise ValueError("update download URL must use HTTPS")
    resp = requests.get(info.download_url, timeout=timeout)
    resp.raise_for_status()
    data = resp.content
    if not verify_update_package(data, info.sha256):
        raise ValueError("update package SHA-256 verification failed")
    return data
