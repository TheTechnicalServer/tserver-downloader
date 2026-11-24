"""Application settings: JSON load/save in the user data directory."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


def _user_data_dir() -> Path:
    """Return the per-user data directory for this application."""
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "tserver-downloader"
    home = Path.home()
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        return base / "TServer Downloader"
    if os.name == "posix" and os.uname().sysname == "Darwin":
        return home / "Library" / "Application Support" / "TServer Downloader"
    return home / ".local" / "share" / "tserver-downloader"


DEFAULT_CATEGORIES = {
    "Video": {
        "extensions": [".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv"],
        "subfolder": "Video",
    },
    "Audio": {
        "extensions": [".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a"],
        "subfolder": "Audio",
    },
    "Documents": {
        "extensions": [".pdf", ".doc", ".docx", ".txt", ".xls", ".xlsx", ".ppt", ".pptx"],
        "subfolder": "Documents",
    },
    "Archives": {
        "extensions": [".zip", ".rar", ".7z", ".tar", ".gz", ".bz2"],
        "subfolder": "Archives",
    },
    "Programs": {
        "extensions": [".exe", ".msi", ".dmg", ".deb", ".rpm", ".apk"],
        "subfolder": "Programs",
    },
    "Images": {
        "extensions": [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg"],
        "subfolder": "Images",
    },
    "Other": {"extensions": [], "subfolder": "Other"},
}

DEFAULT_INTERCEPT_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".webm", ".mp3", ".wav", ".flac",
    ".pdf", ".zip", ".rar", ".7z", ".exe", ".msi", ".iso", ".dmg",
    ".jpg", ".jpeg", ".png", ".gif", ".doc", ".docx", ".xls", ".xlsx",
}


@dataclass
class Settings:
    """All user-configurable settings for TServer Downloader."""

    download_dir: str = str(Path.home() / "Downloads" / "TServer")
    connections_per_download: int = 8
    max_simultaneous: int = 3
    global_speed_limit_kbps: int = 0  # 0 = unlimited
    categories: dict = field(default_factory=lambda: json.loads(json.dumps(DEFAULT_CATEGORIES)))
    intercept_extensions: set = field(default_factory=lambda: set(DEFAULT_INTERCEPT_EXTENSIONS))
    excluded_domains: set = field(default_factory=set)
    adblock_allowlist: list = field(default_factory=lambda: ["thetechnicalserver.blogspot.com"])
    clipboard_monitor: bool = False
    scheduler_enabled: bool = False
    shutdown_after_complete: str = "none"  # "none" | "shutdown" | "sleep" | "hibernate"
    # UI / integration preferences (previously kept in ui_prefs.json only;
    # they now live here so Settings is the single source of truth).
    theme: str = "system"  # "system" | "light" | "dark"
    proxy_enabled: bool = False
    proxy_host: str = ""
    proxy_port: int = 0
    proxy_user: str = ""
    # Stored locally only; never written to logs (see logging_setup).
    proxy_password: str = ""
    auto_update_check: bool = True
    update_feed_url: str = ""

    # --- persistence -------------------------------------------------
    config_path: Path | None = field(default=None, repr=False, compare=False)

    def _path(self) -> Path:
        if self.config_path is not None:
            return Path(self.config_path)
        return _user_data_dir() / "settings.json"

    def to_dict(self) -> dict:
        """Serialise settings to a plain JSON-compatible dict."""
        return {
            "download_dir": self.download_dir,
            "connections_per_download": self.connections_per_download,
            "max_simultaneous": self.max_simultaneous,
            "global_speed_limit_kbps": self.global_speed_limit_kbps,
            "categories": self.categories,
            "intercept_extensions": sorted(self.intercept_extensions),
            "excluded_domains": sorted(self.excluded_domains),
            "adblock_allowlist": list(self.adblock_allowlist),
            "clipboard_monitor": self.clipboard_monitor,
            "scheduler_enabled": self.scheduler_enabled,
            "shutdown_after_complete": self.shutdown_after_complete,
            "theme": self.theme,
            "proxy_enabled": self.proxy_enabled,
            "proxy_host": self.proxy_host,
            "proxy_port": self.proxy_port,
            "proxy_user": self.proxy_user,
            "proxy_password": self.proxy_password,
            "auto_update_check": self.auto_update_check,
            "update_feed_url": self.update_feed_url,
        }

    @classmethod
    def from_dict(cls, data: dict, config_path: Path | None = None) -> "Settings":
        """Build a Settings object from a dict, tolerating missing keys."""
        data = dict(data or {})
        categories = data.get("categories") or json.loads(json.dumps(DEFAULT_CATEGORIES))
        # Merge user categories over defaults so missing ones survive upgrades.
        merged = json.loads(json.dumps(DEFAULT_CATEGORIES))
        if isinstance(categories, dict):
            merged.update(categories)
        return cls(
            download_dir=data.get("download_dir", str(Path.home() / "Downloads" / "TServer")),
            connections_per_download=int(data.get("connections_per_download", 8)),
            max_simultaneous=int(data.get("max_simultaneous", 3)),
            global_speed_limit_kbps=int(data.get("global_speed_limit_kbps", 0)),
            categories=merged,
            intercept_extensions=set(data.get("intercept_extensions") or DEFAULT_INTERCEPT_EXTENSIONS),
            excluded_domains=set(data.get("excluded_domains") or []),
            adblock_allowlist=list(
                data.get("adblock_allowlist") or ["thetechnicalserver.blogspot.com"]
            ),
            clipboard_monitor=bool(data.get("clipboard_monitor", False)),
            scheduler_enabled=bool(data.get("scheduler_enabled", False)),
            shutdown_after_complete=data.get("shutdown_after_complete", "none"),
            theme=str(data.get("theme", "system") or "system"),
            proxy_enabled=bool(data.get("proxy_enabled", False)),
            proxy_host=str(data.get("proxy_host", "") or ""),
            proxy_port=int(data.get("proxy_port", 0) or 0),
            proxy_user=str(data.get("proxy_user", "") or ""),
            proxy_password=str(data.get("proxy_password", "") or ""),
            auto_update_check=bool(data.get("auto_update_check", True)),
            update_feed_url=str(data.get("update_feed_url", "") or ""),
            config_path=config_path,
        )

    def save(self, path: Path | None = None) -> Path:
        """Write settings as JSON to the config path. Returns the path used."""
        target = Path(path) if path is not None else self._path()
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        os.replace(tmp, target)
        self.config_path = target
        return target

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        """Load settings from disk, falling back to defaults if absent/corrupt."""
        target = Path(path) if path is not None else _user_data_dir() / "settings.json"
        if not target.exists():
            settings = cls(config_path=target)
            return settings
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("settings file is not a JSON object")
        except (ValueError, OSError, UnicodeDecodeError):
            return cls(config_path=target)
        return cls.from_dict(data, config_path=target)

    def category_for_filename(self, filename: str) -> str:
        """Return the category name matching a filename's extension."""
        ext = Path(filename).suffix.lower()
        for name, info in self.categories.items():
            if ext in [e.lower() for e in info.get("extensions", [])]:
                return name
        return "Other"

    def subfolder_for_category(self, category: str) -> str:
        """Return the subfolder name for a category (defaults to 'Other')."""
        info = self.categories.get(category)
        if info:
            return info.get("subfolder", category)
        return "Other"
