"""Application factory and theming for TServer Downloader.

``create_app`` is the single place a :py:class:`QApplication` is built, so
both the GUI entry point and any future tooling share the same setup.
PyQt6 is optional at import time: when it is missing ``create_app``
returns ``None`` and the caller (see ``__main__``) prints a clear message
instead of crashing on an import error.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from typing import Any, Callable, Optional

try:
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication

    _QT_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised on machines without PyQt6
    _QT_AVAILABLE = False
    QApplication = None  # type: ignore[assignment]
    Qt = None  # type: ignore[assignment]

__version__ = "1.0.0"
APP_NAME = "TServer Downloader"
ORG_NAME = "TServer"

_DARK_STYLESHEET = """
QMainWindow, QWidget { background-color: #1e1e24; color: #e8e8ec; }
QListWidget, QTableWidget, QPlainTextEdit, QLineEdit, QComboBox, QSpinBox,
QDateTimeEdit { background-color: #26262e; color: #e8e8ec;
  border: 1px solid #3a3a44; border-radius: 4px; }
QListWidget::item:selected, QTableWidget::item:selected {
  background-color: #2f6fb2; color: #ffffff; }
QPushButton { background-color: #2f6fb2; color: #ffffff; border: none;
  padding: 6px 14px; border-radius: 4px; }
QPushButton:hover { background-color: #3a80c4; }
QPushButton:disabled { background-color: #3a3a44; color: #8a8a94; }
QToolBar { background-color: #26262e; border-bottom: 1px solid #3a3a44; }
QStatusBar { background-color: #26262e; color: #b8b8c0; }
QHeaderView::section { background-color: #2c2c34; color: #e8e8ec;
  padding: 4px; border: 1px solid #3a3a44; }
QGroupBox { border: 1px solid #3a3a44; border-radius: 4px; margin-top: 8px; }
QGroupBox::title { subcontrol-origin: margin; left: 8px; color: #b8b8c0; }
QCheckBox, QLabel { color: #e8e8ec; }
QMenu { background-color: #26262e; color: #e8e8ec;
  border: 1px solid #3a3a44; }
QMenu::item:selected { background-color: #2f6fb2; color: #ffffff; }
"""

_LIGHT_STYLESHEET = """
QMainWindow, QWidget { background-color: #f4f4f6; color: #1c1c22; }
QListWidget, QTableWidget, QPlainTextEdit, QLineEdit, QComboBox, QSpinBox,
QDateTimeEdit { background-color: #ffffff; color: #1c1c22;
  border: 1px solid #c8c8d0; border-radius: 4px; }
QListWidget::item:selected, QTableWidget::item:selected {
  background-color: #2f6fb2; color: #ffffff; }
QPushButton { background-color: #2f6fb2; color: #ffffff; border: none;
  padding: 6px 14px; border-radius: 4px; }
QPushButton:hover { background-color: #3a80c4; }
QPushButton:disabled { background-color: #d0d0d8; color: #7a7a84; }
QToolBar { background-color: #ffffff; border-bottom: 1px solid #c8c8d0; }
QStatusBar { background-color: #ffffff; color: #55555e; }
QHeaderView::section { background-color: #ececf0; color: #1c1c22;
  padding: 4px; border: 1px solid #c8c8d0; }
QGroupBox { border: 1px solid #c8c8d0; border-radius: 4px; margin-top: 8px; }
QGroupBox::title { subcontrol-origin: margin; left: 8px; color: #55555e; }
QMenu { background-color: #ffffff; color: #1c1c22;
  border: 1px solid #c8c8d0; }
QMenu::item:selected { background-color: #2f6fb2; color: #ffffff; }
"""


def detect_system_theme() -> str:
    """Return ``"dark"`` or ``"light"`` for the OS theme, best effort."""
    if not _QT_AVAILABLE:
        return "dark"
    try:
        app = QApplication.instance()
        if app is not None:
            hints = app.styleHints()
            scheme = hints.colorScheme()
            # Qt.ColorScheme.Dark == 1 in Qt 6.5+; compare by name if present.
            name = getattr(scheme, "name", str(scheme)).lower()
            if "dark" in name:
                return "dark"
            if "light" in name:
                return "light"
    except Exception:
        pass
    return "dark"


def apply_theme(app: Any, theme: str = "system") -> str:
    """Apply ``theme`` (``"dark"`` / ``"light"`` / ``"system"``) to ``app``.

    Returns the resolved theme name (``"dark"`` or ``"light"``) that was
    actually applied, so callers can record it.
    """
    if app is None:
        return "dark"
    resolved = detect_system_theme() if theme in ("system", "auto", None) else theme
    if resolved not in ("dark", "light"):
        resolved = "dark"
    try:
        app.setStyleSheet(
            _DARK_STYLESHEET if resolved == "dark" else _LIGHT_STYLESHEET
        )
    except Exception:
        pass
    return resolved


def create_app(settings: Any = None):
    """Build and return the :py:class:`QApplication`, or ``None``.

    Returns ``None`` when PyQt6 is not installed.  ``settings`` may be any
    object with a ``theme`` attribute (``"dark"`` / ``"light"`` /
    ``"system"``); when omitted the system theme is used.
    """
    if not _QT_AVAILABLE:
        return None
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(ORG_NAME)
    app.setApplicationVersion(__version__)
    theme = getattr(settings, "theme", "system") if settings is not None else "system"
    apply_theme(app, theme or "system")
    return app


def _parse_version(text: str) -> tuple:
    parts = []
    for chunk in str(text).strip().lstrip("v").split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def check_for_updates(
    current_version: str,
    feed_url: Optional[str],
    on_result: Callable[[dict], None],
) -> Optional[threading.Thread]:
    """Check a releases feed for a newer version, on a background thread.

    ``feed_url`` is the JSON releases endpoint configured for this build
    (stored in ``ui_prefs.json`` as ``update_feed_url``). Two manifest
    shapes are understood: the project's own updater manifest
    (``{"version", "download_url", "sha256", "notes", "mandatory"}``,
    checked with :mod:`tserver_downloader.updater` when it is
    importable) and the GitHub releases ``/latest`` shape
    (``{"tag_name", "html_url"}``). When no feed is configured this
    returns ``None`` and calls nothing -- automatic update checks are
    simply unavailable for that build, and the UI says so instead of
    guessing a URL. ``on_result`` is always called on the worker thread
    with a dict like ``{"update_available": bool, "latest_version": str,
    "url": str}`` (plus ``download_url`` / ``sha256`` / ``notes`` for an
    updater manifest) or ``{"error": str}``; the UI marshals it back to
    the main thread.
    """
    if not feed_url:
        return None

    try:
        from .updater import is_newer_version as _updater_is_newer
    except Exception:
        _updater_is_newer = None

    def _work() -> None:
        try:
            request = urllib.request.Request(
                feed_url, headers={"User-Agent": f"{APP_NAME}/{current_version}"}
            )
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
            if not isinstance(payload, dict):
                on_result({"error": "Update feed did not return a JSON object."})
                return
            if payload.get("version") and payload.get("download_url"):
                # Project updater manifest (see updater.check_for_update).
                latest = str(payload["version"]).strip()
                newer = (
                    _updater_is_newer(latest, current_version)
                    if _updater_is_newer is not None
                    else _parse_version(latest) > _parse_version(current_version)
                )
                on_result(
                    {
                        "update_available": newer,
                        "latest_version": latest,
                        "current_version": current_version,
                        "url": str(payload["download_url"]),
                        "download_url": str(payload["download_url"]),
                        "sha256": str(payload.get("sha256", "")),
                        "notes": str(payload.get("notes", "")),
                        "mandatory": bool(payload.get("mandatory", False)),
                    }
                )
                return
            latest = str(
                payload.get("tag_name") or payload.get("version") or ""
            ).strip()
            url = str(
                payload.get("html_url") or payload.get("url") or feed_url
            )
            if not latest:
                on_result({"error": "Update feed did not contain a version."})
                return
            on_result(
                {
                    "update_available": _parse_version(latest)
                    > _parse_version(current_version),
                    "latest_version": latest,
                    "current_version": current_version,
                    "url": url,
                }
            )
        except Exception as exc:  # network failure is reported, not raised
            on_result({"error": f"Update check failed: {exc}"})

    thread = threading.Thread(target=_work, name="update-check", daemon=True)
    thread.start()
    return thread
