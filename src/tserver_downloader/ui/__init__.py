"""TServer Downloader package (UI subpackage)."""

try:
    from .main_window import MainWindow, SpeedGraphWidget
    from .workers import (
        BackgroundWorker,
        DownloadWorker,
        fetch_downloads,
        normalize_download,
    )
except Exception:  # PyQt6 or engine modules missing: import must still succeed
    MainWindow = None  # type: ignore[assignment]
    SpeedGraphWidget = None  # type: ignore[assignment]
    BackgroundWorker = None  # type: ignore[assignment]
    DownloadWorker = None  # type: ignore[assignment]
    fetch_downloads = None  # type: ignore[assignment]
    normalize_download = None  # type: ignore[assignment]

__all__ = [
    "MainWindow",
    "SpeedGraphWidget",
    "BackgroundWorker",
    "DownloadWorker",
    "fetch_downloads",
    "normalize_download",
]
