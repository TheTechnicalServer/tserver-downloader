"""Main window for TServer Downloader (PyQt6).

Layout: a left sidebar switches between pages held in a
:py:class:`QStackedWidget` (Dashboard, Downloads / Active / Queued /
Completed / Failed, Scheduler, Media, History, Browser Integration,
Ad Blocker, Settings, Diagnostics).  The Downloads table is fed by a
background :py:class:`~tserver_downloader.ui.workers.DownloadWorker`
that polls the engine, so the UI thread never blocks on downloads.

Every button calls real code: engine methods (via the duck-typed
``call_engine`` helper, so the UI tolerates the engine's exact method
names), the settings store, the ad-block engine, the media resolver, or
the browser-integration IPC handshake.  All of those imports are
guarded: if a component module is absent the UI still imports, and the
affected actions show a clear error dialog instead of crashing.
"""

from __future__ import annotations

import logging
import os
import re
import time
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger("tserver_downloader.ui")

try:
    from PyQt6.QtCore import QDateTime, Qt, QTimer, QUrl
    from PyQt6.QtGui import QAction, QColor, QDesktopServices, QKeySequence, QPainter, QPen, QShortcut
    from PyQt6.QtWidgets import (
        QAbstractItemView,
        QApplication,
        QCheckBox,
        QComboBox,
        QDateTimeEdit,
        QFileDialog,
        QFormLayout,
        QGroupBox,
        QHBoxLayout,
        QInputDialog,
        QLabel,
        QLineEdit,
        QListWidget,
        QListWidgetItem,
        QMainWindow,
        QMenu,
        QMessageBox,
        QPlainTextEdit,
        QPushButton,
        QSpinBox,
        QSplitter,
        QStackedWidget,
        QStatusBar,
        QSystemTrayIcon,
        QTableWidget,
        QTableWidgetItem,
        QToolBar,
        QVBoxLayout,
        QWidget,
    )

    _QT_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised on machines without PyQt6
    _QT_AVAILABLE = False

# --- UI workers / engine helpers (guarded: usable without Qt too) --------
try:
    from .workers import BackgroundWorker, DownloadWorker, call_engine
except Exception:  # pragma: no cover - defensive
    BackgroundWorker = None  # type: ignore[assignment]
    DownloadWorker = None  # type: ignore[assignment]

    def call_engine(engine, names, *args, **kwargs):  # type: ignore[no-redef]
        raise AttributeError("Download engine helpers are unavailable.")

# --- Optional components: media, adblock, IPC ------------------------------
# Real modules (other phases own them):
#   media  -> tserver_downloader.media (StreamResolver / SiteResolverRegistry)
#   adblock -> tserver_downloader.adblock (AdBlockEngine)
#   ipc    -> tserver_downloader.browser.ipc (IpcServer)
_MEDIA = None
try:
    from .. import media as _MEDIA  # type: ignore
except Exception:
    try:
        import tserver_downloader.media as _MEDIA  # type: ignore
    except Exception:
        _MEDIA = None

_ADBLOCK_MODULE = None
try:
    from .. import adblock as _ADBLOCK_MODULE  # type: ignore
except Exception:
    try:
        import tserver_downloader.adblock as _ADBLOCK_MODULE  # type: ignore
    except Exception:
        _ADBLOCK_MODULE = None

_IPC_MODULE = None
try:
    from ..browser import ipc as _IPC_MODULE  # type: ignore
except Exception:
    try:
        import tserver_downloader.browser.ipc as _IPC_MODULE  # type: ignore
    except Exception:
        _IPC_MODULE = None

# The site the user asked to always allow through the ad blocker.  This
# entry is locked in the allowlist editor and can never be removed.
LOCKED_ALLOWLIST_ENTRY = "thetechnicalserver.blogspot.com"

_SECTIONS = [
    "Dashboard",
    "Downloads",
    "Active",
    "Queued",
    "Completed",
    "Failed",
    "Scheduler",
    "Media",
    "History",
    "Browser Integration",
    "Ad Blocker",
    "Settings",
    "Diagnostics",
]

_TABLE_COLUMNS = [
    "Filename",
    "Size",
    "Downloaded",
    "%",
    "Speed",
    "ETA",
    "Status",
    "Domain",
    "Connections",
]

_SENSITIVE_RE = re.compile(
    r"(?i)(api[_-]?key|token|password|passwd|secret|auth(?:orization)?"
    r"|access[_-]?key|session[_-]?id|cookie)\s*[:=]\s*\S+"
)


def _fmt_size(num_bytes) -> str:
    try:
        value = float(num_bytes or 0)
    except (TypeError, ValueError):
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024.0
    return f"{value:.1f} PB"


def _fmt_speed(bytes_per_sec) -> str:
    try:
        value = float(bytes_per_sec or 0)
    except (TypeError, ValueError):
        return "—"
    if value <= 0:
        return "—"
    return f"{_fmt_size(value)}/s"


def _fmt_eta(eta) -> str:
    if eta is None:
        return "—"
    if isinstance(eta, str):
        return eta
    try:
        seconds = int(float(eta))
    except (TypeError, ValueError):
        return "—"
    if seconds < 0:
        return "—"
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _is_url(text: str) -> bool:
    try:
        parsed = urlparse(text.strip())
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _redact_line(line: str) -> str:
    """Strip credentials/tokens from one log line before export."""
    redacted = _SENSITIVE_RE.sub(lambda m: m.group(0).split(":", 1)[0].split("=", 1)[0] + ": ***", line)
    return re.sub(r"(?i)(https?://)([^:/\s]+):[^@\s]+@", r"\1\2:***@", redacted)


def _pick(record, names, default=None):
    for name in names:
        if isinstance(record, dict):
            if name in record and record[name] is not None:
                return record[name]
        elif hasattr(record, name):
            value = getattr(record, name)
            if value is not None and not callable(value):
                return value
    return default


def _load_adblock(engine, settings):
    """Return a live AdBlockEngine instance, or ``None`` if unavailable.

    The engine is built with the user's allowlist (which always contains
    ``thetechnicalserver.blogspot.com``) and the bundled offline rule
    lists are loaded into it.
    """
    if _ADBLOCK_MODULE is None:
        return None
    cls = getattr(_ADBLOCK_MODULE, "AdBlockEngine", None)
    if cls is None:
        return None
    try:
        allowlist = list(getattr(settings, "adblock_allowlist", []) or [])
        adblock = cls(allowlist=allowlist)
    except Exception:
        return None
    load_bundled = getattr(_ADBLOCK_MODULE, "load_bundled_lists", None)
    if callable(load_bundled):
        try:
            adblock.load_lists(load_bundled())
        except Exception:
            logger.warning("Could not load bundled ad-block lists.")
    return adblock


if _QT_AVAILABLE:

    class SpeedGraphWidget(QWidget):
        """Small line graph of the last 60 aggregate speed samples."""

        MAX_SAMPLES = 60

        def __init__(self, parent=None):
            super().__init__(parent)
            self._samples: list = []
            self.setMinimumHeight(90)

        def add_sample(self, bytes_per_sec: float) -> None:
            self._samples.append(float(bytes_per_sec or 0))
            self._samples = self._samples[-self.MAX_SAMPLES:]
            self.update()

        def paintEvent(self, event):  # noqa: N802 - Qt naming
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            rect = self.rect().adjusted(4, 4, -4, -4)
            painter.setPen(QPen(QColor("#555560")))
            painter.drawRect(rect)
            if len(self._samples) < 2:
                painter.setPen(QPen(QColor("#8a8a94")))
                painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "No speed data yet")
                return
            peak = max(self._samples) or 1.0
            points = []
            for i, value in enumerate(self._samples):
                x = rect.left() + (rect.width() * i / (self.MAX_SAMPLES - 1))
                y = rect.bottom() - (rect.height() * value / peak)
                points.append((x, y))
            painter.setPen(QPen(QColor("#2f6fb2"), 2))
            for (x1, y1), (x2, y2) in zip(points, points[1:]):
                painter.drawLine(int(x1), int(y1), int(x2), int(y2))
            painter.setPen(QPen(QColor("#8a8a94")))
            painter.drawText(
                rect.adjusted(6, 2, -6, 0),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                f"Peak {_fmt_speed(peak)}",
            )

    class MainWindow(QMainWindow):
        """The main TServer Downloader window."""

        def __init__(self, settings, engine=None, state_store=None, log_file=None, data_dir=None, parent=None):
            super().__init__(parent)
            self.settings = settings
            self.engine = engine
            self.state_store = state_store
            self.log_file = Path(log_file) if log_file else None
            self._data_dir = Path(data_dir) if data_dir else None
            self._downloads: dict = {}
            self._speed_samples: list = []
            self._status_filter = "all"
            self._search_text = ""
            self._last_status: dict = {}
            self._adblock = _load_adblock(engine, settings)
            self._scheduler = getattr(engine, "scheduler", None) if engine else None
            self._media_qualities: list = []
            self._worker = None
            self._bg_workers: list = []
            self._ui_prefs = self._load_ui_prefs()

            self.setWindowTitle("TServer Downloader")
            self.resize(1120, 720)
            self.setAcceptDrops(True)

            self._build_menu()
            self._build_toolbar()
            self._build_central()
            self.setStatusBar(QStatusBar(self))
            self.statusBar().showMessage("Ready")
            self._setup_tray()
            self._setup_shortcuts()
            self._start_worker()
            self._refresh_history()
            self._refresh_scheduler_tasks()
            self._refresh_adblock_stats()
            self._apply_saved_theme()
            self._show_section("Dashboard")

        # ------------------------------------------------------------------
        # UI preferences (theme / proxy / update feed)
        # ------------------------------------------------------------------
        # The real Settings class (config.py) persists the download
        # settings. A few UI-only preferences -- theme, proxy credentials
        # and the update feed URL -- are not part of that class, so they
        # live in ui_prefs.json next to settings.json and are read/written
        # here with the same care as the main settings.
        def _prefs_path(self) -> Path:
            base = self._data_dir
            if base is None:
                config_path = getattr(self.settings, "config_path", None)
                if config_path:
                    base = Path(config_path).parent
                elif self.log_file is not None:
                    base = self.log_file.parent.parent
                else:
                    base = Path.home() / ".local" / "share" / "tserver-downloader"
            return Path(base) / "ui_prefs.json"

        def _load_ui_prefs(self) -> dict:
            try:
                import json

                data = json.loads(self._prefs_path().read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
            except (OSError, ValueError):
                return {}

        def _save_ui_prefs(self) -> None:
            try:
                import json

                path = self._prefs_path()
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(self._ui_prefs, indent=2), encoding="utf-8")
            except OSError as exc:
                logger.warning("Could not save UI preferences: %s", exc)

        def _pref(self, name: str, default):
            """UI preference value: ui_prefs.json first, settings attr second."""
            if name in self._ui_prefs:
                return self._ui_prefs[name]
            return getattr(self.settings, name, default)

        def _apply_saved_theme(self):
            try:
                from ..app import apply_theme as _apply_theme

                _apply_theme(QApplication.instance(), str(self._pref("theme", "system") or "system"))
            except Exception:
                pass

        def _run_background(self, fn, args=(), on_success=None, on_failure=None):
            """Run ``fn(*args)`` off the GUI thread; callbacks run on it."""
            if BackgroundWorker is None:
                try:
                    result = fn(*args)
                except Exception as exc:
                    if on_failure is not None:
                        on_failure(str(exc))
                else:
                    if on_success is not None:
                        on_success(result)
                return None
            worker = BackgroundWorker(fn, args=args, parent=self)
            self._bg_workers.append(worker)

            def _done(_result, _worker=worker):
                if _worker in self._bg_workers:
                    self._bg_workers.remove(_worker)
                _worker.deleteLater()

            worker.succeeded.connect(
                lambda result: (_done(result), on_success(result) if on_success else None)
            )
            worker.failed.connect(
                lambda message: (_done(None), on_failure(message) if on_failure else None)
            )
            worker.start()
            return worker

        def shutdown(self):
            """Stop background threads (called by __main__ on exit)."""
            if self._worker is not None:
                try:
                    self._worker.stop()
                except Exception:
                    pass
                self._worker = None
            for worker in list(self._bg_workers):
                try:
                    worker.quit()
                    worker.wait(1000)
                except Exception:
                    pass
            self._bg_workers.clear()

        # ------------------------------------------------------------------
        # Construction
        # ------------------------------------------------------------------
        def _build_menu(self):
            menu_bar = self.menuBar()
            file_menu = menu_bar.addMenu("&File")
            add_action = QAction("Add URL...", self)
            add_action.setShortcut(QKeySequence("Ctrl+N"))
            add_action.triggered.connect(self._add_url)
            file_menu.addAction(add_action)
            quit_action = QAction("&Quit", self)
            quit_action.setShortcut(QKeySequence("Ctrl+Q"))
            quit_action.triggered.connect(self.close)
            file_menu.addAction(quit_action)

            help_menu = menu_bar.addMenu("&Help")
            update_action = QAction("Check for Updates", self)
            update_action.triggered.connect(self._check_for_updates)
            help_menu.addAction(update_action)
            about_action = QAction("&About", self)
            about_action.triggered.connect(self._about)
            help_menu.addAction(about_action)

        def _build_toolbar(self):
            toolbar = QToolBar("Main", self)
            toolbar.setMovable(False)
            self.addToolBar(toolbar)
            for label, slot in (
                ("Add URL", self._add_url),
                ("Pause", self._pause_selected),
                ("Resume", self._resume_selected),
                ("Cancel", self._cancel_selected),
                ("Remove", self._remove_selected),
            ):
                button = QPushButton(label)
                button.clicked.connect(slot)
                toolbar.addWidget(button)
            toolbar.addSeparator()
            self._search_box = QLineEdit()
            self._search_box.setPlaceholderText("Search downloads...")
            self._search_box.setClearButtonEnabled(True)
            self._search_box.textChanged.connect(self._on_search_changed)
            toolbar.addWidget(self._search_box)

        def _build_central(self):
            splitter = QSplitter(Qt.Orientation.Horizontal, self)
            self._sidebar = QListWidget()
            self._sidebar.addItems(_SECTIONS)
            self._sidebar.setMaximumWidth(190)
            self._sidebar.currentRowChanged.connect(self._on_sidebar_row)
            splitter.addWidget(self._sidebar)

            self._stack = QStackedWidget()
            self._pages: dict = {}
            self._pages["Dashboard"] = self._build_dashboard_page()
            self._pages["Downloads"] = self._build_downloads_page()
            self._pages["Scheduler"] = self._build_scheduler_page()
            self._pages["Media"] = self._build_media_page()
            self._pages["History"] = self._build_history_page()
            self._pages["Browser Integration"] = self._build_browser_page()
            self._pages["Ad Blocker"] = self._build_adblock_page()
            self._pages["Settings"] = self._build_settings_page()
            self._pages["Diagnostics"] = self._build_diagnostics_page()
            for name in ("Dashboard", "Downloads", "Scheduler", "Media", "History",
                         "Browser Integration", "Ad Blocker", "Settings", "Diagnostics"):
                self._stack.addWidget(self._pages[name])
            splitter.addWidget(self._stack)
            splitter.setStretchFactor(0, 0)
            splitter.setStretchFactor(1, 1)
            self.setCentralWidget(splitter)

        # ------------------------------------------------------------------
        # Pages
        # ------------------------------------------------------------------
        def _build_dashboard_page(self):
            page = QWidget()
            layout = QVBoxLayout(page)
            self._dash_counts = QLabel("No downloads yet.")
            layout.addWidget(self._dash_counts)
            graph_box = QGroupBox("Download speed (last 60 samples)")
            graph_layout = QVBoxLayout(graph_box)
            self._speed_graph = SpeedGraphWidget()
            graph_layout.addWidget(self._speed_graph)
            self._speed_label = QLabel("Current speed: —")
            graph_layout.addWidget(self._speed_label)
            layout.addWidget(graph_box)
            layout.addWidget(QLabel("Recent downloads"))
            self._dash_recent = QListWidget()
            layout.addWidget(self._dash_recent)
            layout.addStretch()
            return page

        def _build_downloads_page(self):
            page = QWidget()
            layout = QVBoxLayout(page)
            filter_row = QHBoxLayout()
            filter_row.addWidget(QLabel("Show:"))
            self._filter_combo = QComboBox()
            self._filter_combo.addItems(
                ["All", "Active", "Queued", "Completed", "Failed", "Paused"]
            )
            self._filter_combo.currentTextChanged.connect(self._on_filter_changed)
            filter_row.addWidget(self._filter_combo)
            filter_row.addStretch()
            layout.addLayout(filter_row)
            self._table = QTableWidget(0, len(_TABLE_COLUMNS))
            self._table.setHorizontalHeaderLabels(_TABLE_COLUMNS)
            self._table.setSortingEnabled(True)
            self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            self._table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
            self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            self._table.customContextMenuRequested.connect(self._table_context_menu)
            self._table.horizontalHeader().setStretchLastSection(True)
            layout.addWidget(self._table)
            return page

        def _build_scheduler_page(self):
            page = QWidget()
            layout = QVBoxLayout(page)
            layout.addWidget(QLabel("Schedule a download for a later time."))
            form = QFormLayout()
            self._sched_url = QLineEdit()
            self._sched_url.setPlaceholderText("https://example.com/file.zip")
            form.addRow("URL:", self._sched_url)
            self._sched_when = QDateTimeEdit(QDateTime.currentDateTime().addSecs(3600))
            self._sched_when.setCalendarPopup(True)
            self._sched_when.setDisplayFormat("yyyy-MM-dd HH:mm")
            form.addRow("Start at:", self._sched_when)
            self._sched_stop = QDateTimeEdit(QDateTime.currentDateTime().addSecs(7200))
            self._sched_stop.setCalendarPopup(True)
            self._sched_stop.setDisplayFormat("yyyy-MM-dd HH:mm")
            form.addRow("Run until:", self._sched_stop)
            layout.addLayout(form)
            add_btn = QPushButton("Add Scheduled Download")
            add_btn.clicked.connect(self._scheduler_add_task)
            layout.addWidget(add_btn)
            layout.addWidget(QLabel("Scheduled tasks"))
            self._sched_list = QListWidget()
            layout.addWidget(self._sched_list)
            remove_btn = QPushButton("Remove Selected Task")
            remove_btn.clicked.connect(self._scheduler_remove_task)
            layout.addWidget(remove_btn)
            layout.addStretch()
            return page

        def _build_media_page(self):
            page = QWidget()
            layout = QVBoxLayout(page)
            layout.addWidget(QLabel("Detect downloadable media (video/audio) from a page URL."))
            url_row = QHBoxLayout()
            self._media_url = QLineEdit()
            self._media_url.setPlaceholderText("Paste a video or audio page URL...")
            url_row.addWidget(self._media_url)
            detect_btn = QPushButton("Detect")
            detect_btn.clicked.connect(self._media_detect)
            url_row.addWidget(detect_btn)
            layout.addLayout(url_row)
            layout.addWidget(QLabel("Available qualities"))
            self._media_quality_list = QListWidget()
            layout.addWidget(self._media_quality_list)
            download_btn = QPushButton("Download Selected Quality")
            download_btn.clicked.connect(self._media_download)
            layout.addWidget(download_btn)
            layout.addStretch()
            return page

        def _build_history_page(self):
            page = QWidget()
            layout = QVBoxLayout(page)
            layout.addWidget(QLabel("Completed and past downloads, from the state store."))
            self._history_list = QListWidget()
            layout.addWidget(self._history_list)
            clear_btn = QPushButton("Clear History")
            clear_btn.clicked.connect(self._history_clear)
            layout.addWidget(clear_btn)
            layout.addStretch()
            return page

        def _build_browser_page(self):
            page = QWidget()
            layout = QVBoxLayout(page)
            layout.addWidget(QLabel("Browser Integration"))
            self._browser_status = QLabel("Status: not checked yet.")
            self._browser_status.setWordWrap(True)
            layout.addWidget(self._browser_status)
            test_btn = QPushButton("Test Connection")
            test_btn.clicked.connect(self._browser_test_connection)
            layout.addWidget(test_btn)
            instructions = QPlainTextEdit()
            instructions.setReadOnly(True)
            instructions.setPlainText(
                "How to connect your browser:\n\n"
                "1. Install the TServer Downloader extension from "
                "extension/chrome (Chrome / Edge) or extension/firefox "
                "(Firefox) — load it unpacked from the extension folder, "
                "or install the signed build from the browser store when "
                "available.\n"
                "2. The installer registers the native messaging host "
                "under HKCU for Chrome and Edge, pointing at the "
                "native-messaging-host.json installed next to the app.\n"
                "3. Click 'Test Connection' above: the app performs the "
                "native-messaging handshake (a ping/pong exchange) and "
                "reports the result here.\n"
                "4. In the browser, use the extension button or the "
                "right-click menu on any downloadable link to send it to "
                "TServer Downloader.\n\n"
                "If the test fails, make sure the app is installed (not "
                "just run from source) and that the extension is enabled."
            )
            layout.addWidget(instructions)
            layout.addStretch()
            return page

        def _build_adblock_page(self):
            page = QWidget()
            layout = QVBoxLayout(page)
            self._adblock_enable = QCheckBox("Enable ad blocker")
            self._adblock_enable.setChecked(bool(self._pref("adblock_enabled", True)))
            self._adblock_enable.toggled.connect(self._adblock_toggle)
            layout.addWidget(self._adblock_enable)
            layout.addWidget(QLabel(
                "Note: thetechnicalserver.blogspot.com is always allowed and "
                "cannot be removed from the allowlist — the downloader never "
                "blocks its own project's site."
            ))
            layout.addWidget(QLabel("Allowlist (never blocked)"))
            self._adblock_allowlist = QListWidget()
            layout.addWidget(self._adblock_allowlist)
            allow_row = QHBoxLayout()
            self._adblock_allow_input = QLineEdit()
            self._adblock_allow_input.setPlaceholderText("example.com")
            allow_row.addWidget(self._adblock_allow_input)
            add_allow_btn = QPushButton("Add to Allowlist")
            add_allow_btn.clicked.connect(self._adblock_add_allow)
            allow_row.addWidget(add_allow_btn)
            remove_allow_btn = QPushButton("Remove Selected")
            remove_allow_btn.clicked.connect(self._adblock_remove_allow)
            allow_row.addWidget(remove_allow_btn)
            layout.addLayout(allow_row)
            layout.addWidget(QLabel("Custom blocking rule"))
            rule_row = QHBoxLayout()
            self._adblock_rule_input = QLineEdit()
            self._adblock_rule_input.setPlaceholderText("e.g. ||ads.example.com^  or  *://*.tracker.example/*")
            rule_row.addWidget(self._adblock_rule_input)
            add_rule_btn = QPushButton("Add Rule")
            add_rule_btn.clicked.connect(self._adblock_add_rule)
            rule_row.addWidget(add_rule_btn)
            layout.addLayout(rule_row)
            self._adblock_stats = QLabel("Ad blocker stats: unavailable.")
            layout.addWidget(self._adblock_stats)
            layout.addStretch()
            self._refresh_adblock_allowlist()
            return page

        def _build_settings_page(self):
            page = QWidget()
            layout = QVBoxLayout(page)
            form = QFormLayout()
            dir_row = QHBoxLayout()
            self._settings_dir = QLineEdit(str(getattr(self.settings, "download_dir", "")))
            dir_row.addWidget(self._settings_dir)
            browse_btn = QPushButton("Browse...")
            browse_btn.clicked.connect(self._settings_browse_dir)
            dir_row.addWidget(browse_btn)
            form.addRow("Download folder:", dir_row)
            self._settings_connections = QSpinBox()
            self._settings_connections.setRange(1, 32)
            self._settings_connections.setValue(int(getattr(self.settings, "connections_per_download", 8) or 8))
            form.addRow("Connections per download:", self._settings_connections)
            self._settings_max_simultaneous = QSpinBox()
            self._settings_max_simultaneous.setRange(1, 10)
            self._settings_max_simultaneous.setValue(int(getattr(self.settings, "max_simultaneous", 3) or 3))
            form.addRow("Max simultaneous downloads:", self._settings_max_simultaneous)
            self._settings_speed = QSpinBox()
            self._settings_speed.setRange(0, 1000000)
            self._settings_speed.setSuffix(" KB/s (0 = unlimited)")
            self._settings_speed.setValue(int(getattr(self.settings, "global_speed_limit_kbps", 0) or 0))
            form.addRow("Speed limit:", self._settings_speed)
            categories = getattr(self.settings, "categories", None)
            category_names = ", ".join(categories.keys()) if isinstance(categories, dict) else ""
            self._settings_categories = QLineEdit(category_names)
            self._settings_categories.setPlaceholderText("Video, Audio, Documents")
            form.addRow("Categories (comma-separated):", self._settings_categories)
            self._settings_clipboard = QCheckBox("Monitor clipboard for download links")
            self._settings_clipboard.setChecked(bool(getattr(self.settings, "clipboard_monitor", False)))
            form.addRow(self._settings_clipboard)
            self._settings_scheduler = QCheckBox("Enable scheduler")
            self._settings_scheduler.setChecked(bool(getattr(self.settings, "scheduler_enabled", False)))
            form.addRow(self._settings_scheduler)
            self._settings_proxy_host = QLineEdit(str(self._pref("proxy_host", "") or ""))
            form.addRow("Proxy host:", self._settings_proxy_host)
            self._settings_proxy_port = QSpinBox()
            self._settings_proxy_port.setRange(0, 65535)
            self._settings_proxy_port.setValue(int(self._pref("proxy_port", 0) or 0))
            form.addRow("Proxy port:", self._settings_proxy_port)
            self._settings_proxy_user = QLineEdit(str(self._pref("proxy_user", "") or ""))
            form.addRow("Proxy username:", self._settings_proxy_user)
            self._settings_proxy_pass = QLineEdit(str(self._pref("proxy_password", "") or ""))
            self._settings_proxy_pass.setEchoMode(QLineEdit.EchoMode.Password)
            form.addRow("Proxy password:", self._settings_proxy_pass)
            self._settings_theme = QComboBox()
            self._settings_theme.addItems(["system", "dark", "light"])
            self._settings_theme.setCurrentText(str(self._pref("theme", "system") or "system"))
            form.addRow("Theme:", self._settings_theme)
            self._settings_auto_update = QCheckBox("Check for updates automatically")
            self._settings_auto_update.setChecked(bool(self._pref("auto_update_check", False)))
            form.addRow(self._settings_auto_update)
            self._settings_update_url = QLineEdit(str(self._pref("update_feed_url", "") or ""))
            self._settings_update_url.setPlaceholderText("https://example.com/updates.json")
            form.addRow("Update feed URL (HTTPS):", self._settings_update_url)
            layout.addLayout(form)
            save_btn = QPushButton("Save Settings")
            save_btn.clicked.connect(self._settings_save)
            layout.addWidget(save_btn)
            layout.addWidget(QLabel(
                "Theme, proxy and update-feed settings are kept in "
                "ui_prefs.json next to settings.json."
            ))
            layout.addStretch()
            return page

        def _build_diagnostics_page(self):
            page = QWidget()
            layout = QVBoxLayout(page)
            layout.addWidget(QLabel("Application log"))
            self._diag_log = QPlainTextEdit()
            self._diag_log.setReadOnly(True)
            layout.addWidget(self._diag_log)
            btn_row = QHBoxLayout()
            refresh_btn = QPushButton("Refresh")
            refresh_btn.clicked.connect(self._diagnostics_refresh)
            btn_row.addWidget(refresh_btn)
            export_btn = QPushButton("Export Logs...")
            export_btn.clicked.connect(self._diagnostics_export)
            btn_row.addWidget(export_btn)
            btn_row.addStretch()
            layout.addLayout(btn_row)
            layout.addWidget(QLabel(
                "Export writes a sanitised copy of the log (credentials and "
                "tokens are redacted) as a .zip or .txt file."
            ))
            self._diagnostics_refresh()
            return page

        # ------------------------------------------------------------------
        # Navigation / filtering
        # ------------------------------------------------------------------
        def _on_sidebar_row(self, row: int):
            if 0 <= row < len(_SECTIONS):
                self._show_section(_SECTIONS[row])

        def _show_section(self, name: str):
            page_for = {
                "Dashboard": "Dashboard",
                "Downloads": "Downloads",
                "Active": "Downloads",
                "Queued": "Downloads",
                "Completed": "Downloads",
                "Failed": "Downloads",
                "Scheduler": "Scheduler",
                "Media": "Media",
                "History": "History",
                "Browser Integration": "Browser Integration",
                "Ad Blocker": "Ad Blocker",
                "Settings": "Settings",
                "Diagnostics": "Diagnostics",
            }
            target = page_for.get(name, "Dashboard")
            self._stack.setCurrentWidget(self._pages[target])
            status_filter = {
                "Downloads": "all", "Active": "active", "Queued": "queued",
                "Completed": "completed", "Failed": "failed",
            }.get(name)
            if status_filter is not None:
                self._status_filter = status_filter
                combo_text = {
                    "all": "All", "active": "Active", "queued": "Queued",
                    "completed": "Completed", "failed": "Failed",
                }[status_filter]
                index = self._filter_combo.findText(combo_text)
                if index >= 0:
                    self._filter_combo.blockSignals(True)
                    self._filter_combo.setCurrentIndex(index)
                    self._filter_combo.blockSignals(False)
                self._refresh_table()

        def _on_filter_changed(self, text: str):
            self._status_filter = {
                "All": "all", "Active": "active", "Queued": "queued",
                "Completed": "completed", "Failed": "failed", "Paused": "paused",
            }.get(text, "all")
            self._refresh_table()

        def _on_search_changed(self, text: str):
            self._search_text = text.strip().lower()
            self._refresh_table()

        # ------------------------------------------------------------------
        # Progress handling
        # ------------------------------------------------------------------
        def _start_worker(self):
            if DownloadWorker is None or self.engine is None:
                self.statusBar().showMessage(
                    "Download engine unavailable — downloads are disabled."
                )
                return
            self._worker = DownloadWorker(self.engine, interval_ms=500, parent=self)
            self._worker.progress_updated.connect(self._on_progress)
            self._worker.worker_error.connect(
                lambda msg: self.statusBar().showMessage(msg, 5000)
            )
            self._worker.start()

        def _on_progress(self, records):
            total_speed = 0.0
            for record in records:
                record_id = str(record.get("id"))
                previous = self._last_status.get(record_id)
                self._downloads[record_id] = record
                self._last_status[record_id] = record["status"]
                total_speed += record["speed"]
                if record["status"] in ("completed", "done", "finished") and previous not in (
                    "completed", "done", "finished", None,
                ):
                    self._notify_completion(record)
            self._speed_samples.append(total_speed)
            self._speed_graph.add_sample(total_speed)
            self._speed_label.setText(f"Current speed: {_fmt_speed(total_speed)}")
            self._refresh_table()
            self._refresh_dashboard()
            self._start_due_scheduled()

        def _start_due_scheduled(self):
            """Start scheduled downloads whose run window has opened.

            The engine's Scheduler only decides *when* a task is due; this
            poll is what actually starts due tasks (and clears their
            schedule so they are not started twice).
            """
            scheduler = self._scheduler
            if scheduler is None or self.engine is None:
                return
            if not bool(getattr(self.settings, "scheduler_enabled", False)):
                return
            try:
                due = scheduler.due_tasks(time.time())
            except Exception:
                return
            for task_id in due or []:
                try:
                    task = self.engine.get_task(task_id)
                    if task is not None and task.status in ("queued", "paused"):
                        self.engine.start(task_id)
                    scheduler.remove_schedule(task_id)
                except Exception as exc:
                    logger.warning("Could not start scheduled task %s: %s", task_id, exc)

        def _visible_records(self):
            records = list(self._downloads.values())
            if self._status_filter == "active":
                records = [r for r in records if r["status"] in ("downloading", "active", "running")]
            elif self._status_filter in ("queued", "completed", "failed", "paused"):
                records = [r for r in records if r["status"] == self._status_filter]
            if self._search_text:
                needle = self._search_text
                records = [
                    r for r in records
                    if needle in r["filename"].lower()
                    or needle in r["url"].lower()
                    or needle in r["domain"].lower()
                ]
            return records

        def _refresh_table(self):
            if not hasattr(self, "_table"):
                return
            records = self._visible_records()
            table = self._table
            table.setSortingEnabled(False)
            table.setRowCount(len(records))
            for row, record in enumerate(records):
                values = [
                    record["filename"],
                    _fmt_size(record["size"]),
                    _fmt_size(record["downloaded"]),
                    f"{record['percent']:.1f}%",
                    _fmt_speed(record["speed"]),
                    _fmt_eta(record["eta"]),
                    record["status"],
                    record["domain"],
                    str(record["connections"]),
                ]
                for col, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    item.setData(Qt.ItemDataRole.UserRole, str(record["id"]))
                    table.setItem(row, col, item)
            table.setSortingEnabled(True)

        def _refresh_dashboard(self):
            counts: dict = {}
            for record in self._downloads.values():
                counts[record["status"]] = counts.get(record["status"], 0) + 1
            total = len(self._downloads)
            active = sum(counts.get(s, 0) for s in ("downloading", "active", "running"))
            self._dash_counts.setText(
                f"Total: {total}   Active: {active}   "
                f"Completed: {counts.get('completed', 0)}   "
                f"Failed: {counts.get('failed', 0)}   "
                f"Queued: {counts.get('queued', 0)}"
            )
            recent = sorted(
                self._downloads.values(),
                key=lambda r: r["filename"],
            )[-10:]
            self._dash_recent.clear()
            for record in recent:
                self._dash_recent.addItem(
                    f"{record['filename']} — {record['status']} "
                    f"({record['percent']:.0f}%)"
                )

        def _notify_completion(self, record):
            message = f"Download finished: {record['filename']}"
            self.statusBar().showMessage(message, 8000)
            if hasattr(self, "_tray") and self._tray is not None:
                try:
                    self._tray.showMessage(
                        "TServer Downloader", message,
                        QSystemTrayIcon.MessageIcon.Information, 5000,
                    )
                except Exception:
                    pass

        # ------------------------------------------------------------------
        # Download actions
        # ------------------------------------------------------------------
        def _require_engine(self) -> bool:
            if self.engine is None:
                QMessageBox.warning(
                    self, "Engine unavailable",
                    "The download engine is not installed, so downloads are "
                    "disabled. Install the full application and try again.",
                )
                return False
            return True

        def _selected_records(self):
            rows = {item.row() for item in self._table.selectedItems()}
            records = self._visible_records()
            return [records[row] for row in sorted(rows) if row < len(records)]

        def _selected_ids(self):
            return [str(r["id"]) for r in self._selected_records()]

        def _add_url(self):
            if not self._require_engine():
                return
            url, ok = QInputDialog.getText(
                self, "Add Download", "Enter a download URL:"
            )
            if not ok or not url.strip():
                return
            url = url.strip()
            if not _is_url(url):
                QMessageBox.warning(self, "Invalid URL", f"'{url}' is not a valid http(s) URL.")
                return
            # engine.add() probes the server first, so it runs off the GUI
            # thread; the result is reported back through the worker signal.
            self.statusBar().showMessage(f"Adding: {url} ...", 5000)
            self._run_background(
                lambda: call_engine(self.engine, ("add", "add_download", "add_url", "enqueue"), url),
                on_success=lambda task_id: (
                    self.statusBar().showMessage(f"Added: {url}", 5000),
                    self._show_section("Downloads"),
                ),
                on_failure=lambda message: QMessageBox.critical(
                    self, "Could not add download", message
                ),
            )

        def _pause_selected(self):
            self._act_on_selected(("pause", "pause_download"), "pause")

        def _resume_selected(self):
            self._act_on_selected(("resume", "resume_download"), "resume")

        def _cancel_selected(self):
            self._act_on_selected(("cancel", "cancel_download"), "cancel")

        def _retry_selected(self):
            self._act_on_selected(("retry", "retry_download"), "retry")

        def _act_on_selected(self, method_names, verb: str):
            if not self._require_engine():
                return
            ids = self._selected_ids()
            if not ids:
                QMessageBox.information(self, "Nothing selected", "Select a download first.")
                return
            failures = []
            for download_id in ids:
                try:
                    call_engine(self.engine, method_names, download_id)
                except Exception as exc:
                    failures.append(f"{download_id}: {exc}")
            if failures:
                QMessageBox.warning(
                    self, f"Could not {verb} some downloads", "\n".join(failures)
                )

        def _remove_selected(self):
            if not self._require_engine():
                return
            records = self._selected_records()
            if not records:
                QMessageBox.information(self, "Nothing selected", "Select a download first.")
                return
            answer = QMessageBox.question(
                self, "Remove downloads",
                f"Remove {len(records)} download(s) from the list?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            # The engine has no single "remove": a task is cancelled, its
            # persisted record is deleted from the state store, and it is
            # dropped from the engine's queue and in-memory task map so the
            # next progress poll cannot resurrect it.
            for record in records:
                download_id = str(record["id"])
                try:
                    call_engine(self.engine, ("cancel", "cancel_download"), download_id)
                except Exception:
                    pass
                try:
                    if self.state_store is not None:
                        self.state_store.delete_task(download_id)
                except Exception as exc:
                    logger.warning("Could not delete task %s from state store: %s", download_id, exc)
                try:
                    queue = getattr(self.engine, "queue", None)
                    if queue is not None and hasattr(queue, "remove"):
                        queue.remove(download_id)
                except Exception:
                    pass
                try:
                    tasks = getattr(self.engine, "_tasks", None)
                    if isinstance(tasks, dict):
                        tasks.pop(download_id, None)
                except Exception:
                    pass
                if self._scheduler is not None:
                    try:
                        self._scheduler.remove_schedule(download_id)
                    except Exception:
                        pass
                self._downloads.pop(download_id, None)
                self._last_status.pop(download_id, None)
            self._refresh_table()

        def _table_context_menu(self, position):
            menu = QMenu(self)
            open_file = menu.addAction("Open file")
            open_file.triggered.connect(self._open_selected_file)
            open_folder = menu.addAction("Open folder")
            open_folder.triggered.connect(self._open_selected_folder)
            copy_url = menu.addAction("Copy URL")
            copy_url.triggered.connect(self._copy_selected_url)
            menu.addSeparator()
            retry = menu.addAction("Retry")
            retry.triggered.connect(self._retry_selected)
            remove = menu.addAction("Remove")
            remove.triggered.connect(self._remove_selected)
            menu.exec(self._table.viewport().mapToGlobal(position))

        def _open_selected_file(self):
            records = self._selected_records()
            if not records:
                return
            path = self._file_path_for(records[0])
            if path and Path(path).exists():
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
            else:
                QMessageBox.information(self, "File not available", "The downloaded file does not exist yet.")

        def _open_selected_folder(self):
            records = self._selected_records()
            if not records:
                return
            path = self._file_path_for(records[0])
            folder = Path(path).parent if path else Path(str(getattr(self.settings, "download_dir", Path.home() / "Downloads")))
            if folder.exists():
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
            else:
                QMessageBox.information(self, "Folder not available", f"Folder does not exist: {folder}")

        def _copy_selected_url(self):
            records = self._selected_records()
            if not records:
                return
            QApplication.clipboard().setText(records[0]["url"])
            self.statusBar().showMessage("URL copied to clipboard.", 3000)

        def _file_path_for(self, record):
            candidate = _pick(record, ("dest_path", "path", "file_path", "destination", "output_path"))
            if candidate:
                return str(candidate)
            download_dir = str(getattr(self.settings, "download_dir", "") or Path.home() / "Downloads")
            return str(Path(download_dir) / record["filename"]) if record["filename"] else ""

        # ------------------------------------------------------------------
        # Scheduler
        # ------------------------------------------------------------------
        def _scheduler_add_task(self):
            if not self._require_engine():
                return
            url = self._sched_url.text().strip()
            if not _is_url(url):
                QMessageBox.warning(self, "Invalid URL", "Enter a valid http(s) URL to schedule.")
                return
            start = self._sched_when.dateTime().toSecsSinceEpoch()
            stop = self._sched_stop.dateTime().toSecsSinceEpoch()
            if stop < start:
                QMessageBox.warning(self, "Invalid window", "The 'Run until' time must not be before 'Start at'.")
                return
            scheduler = self._scheduler
            if scheduler is None:
                QMessageBox.warning(self, "Scheduler unavailable", "The engine does not expose a scheduler.")
                return

            def _on_added(task_id):
                try:
                    scheduler.add_schedule(task_id, start, stop)
                except Exception as exc:
                    QMessageBox.critical(self, "Could not schedule download", str(exc))
                    return
                self._sched_url.clear()
                self._refresh_scheduler_tasks()
                self.statusBar().showMessage(f"Scheduled: {url}", 5000)

            # engine.add() probes the server, so it runs off the GUI thread.
            self._run_background(
                lambda: call_engine(self.engine, ("add", "add_download", "add_url", "enqueue"), url),
                on_success=_on_added,
                on_failure=lambda message: QMessageBox.critical(
                    self, "Could not add download", message
                ),
            )

        def _scheduler_remove_task(self):
            item = self._sched_list.currentItem()
            if item is None:
                return
            task_id = item.data(Qt.ItemDataRole.UserRole)
            if self._scheduler is not None:
                try:
                    self._scheduler.remove_schedule(task_id)
                except Exception as exc:
                    QMessageBox.warning(self, "Could not remove task", str(exc))
                    return
            self._refresh_scheduler_tasks()

        def _scheduler_entries(self):
            """Return [(task_id, start_epoch, stop_epoch)] from the Scheduler."""
            scheduler = self._scheduler
            if scheduler is None:
                return []
            mapping = (
                getattr(scheduler, "_schedules", None)
                or getattr(scheduler, "schedules", None)
                or getattr(scheduler, "entries", None)
                or {}
            )
            entries = []
            if isinstance(mapping, dict):
                for task_id, entry in mapping.items():
                    entries.append((
                        str(task_id),
                        _pick(entry, ("start_time",)),
                        _pick(entry, ("stop_time",)),
                    ))
            return entries

        def _refresh_scheduler_tasks(self):
            if not hasattr(self, "_sched_list"):
                return
            self._sched_list.clear()
            for task_id, start, stop in self._scheduler_entries():
                url = task_id
                if self.engine is not None:
                    try:
                        task = self.engine.get_task(task_id)
                        if task is not None:
                            url = getattr(task, "url", url)
                    except Exception:
                        pass
                start_text = datetime.fromtimestamp(float(start)).strftime("%Y-%m-%d %H:%M") if start else "?"
                stop_text = datetime.fromtimestamp(float(stop)).strftime("%Y-%m-%d %H:%M") if stop else "?"
                item = QListWidgetItem(f"{start_text} - {stop_text}   {url}")
                item.setData(Qt.ItemDataRole.UserRole, task_id)
                self._sched_list.addItem(item)

        # ------------------------------------------------------------------
        # Media detection
        # ------------------------------------------------------------------
        def _resolve_media_formats(self, url: str):
            """Resolve a page URL to [MediaFormat] using the real resolver.

            Tries the site registry first, then a plain StreamResolver.
            Runs off the GUI thread (see _media_detect).
            """
            registry = getattr(_MEDIA, "SiteResolverRegistry", None)
            if registry is not None and hasattr(registry, "resolve"):
                return list(registry.resolve(url) or [])
            resolver_cls = getattr(_MEDIA, "StreamResolver", None)
            if resolver_cls is not None:
                return list(resolver_cls().resolve(url) or [])
            raise AttributeError("Media resolver exposes no resolve function.")

        def _media_detect(self):
            url = self._media_url.text().strip()
            if not _is_url(url):
                QMessageBox.warning(self, "Invalid URL", "Paste a valid http(s) page URL first.")
                return
            if _MEDIA is None:
                QMessageBox.warning(
                    self, "Media resolver unavailable",
                    "The media resolver module is not installed, so media "
                    "detection is unavailable in this build.",
                )
                return
            self.statusBar().showMessage(f"Detecting media on: {url} ...", 5000)
            self._run_background(
                self._resolve_media_formats, args=(url,),
                on_success=self._media_detect_done,
                on_failure=lambda message: QMessageBox.critical(
                    self, "Detection failed", message
                ),
            )

        def _media_detect_done(self, formats):
            self._media_qualities = list(formats or [])
            self._media_quality_list.clear()
            if not self._media_qualities:
                self._media_quality_list.addItem("No downloadable media found on that page.")
                return
            for item in self._media_qualities:
                label = _pick(item, ("quality_label", "label", "quality", "format", "name"), default="?")
                container = _pick(item, ("container",), default="")
                size = _pick(item, ("estimated_size", "size", "filesize"))
                kind = ""
                if _pick(item, ("is_audio_only",), default=False):
                    kind = " (audio only)"
                elif _pick(item, ("is_video_only",), default=False):
                    kind = " (video only)"
                text = f"{label}" + (f" [{container}]" if container else "") + kind
                if size:
                    text += f" — {_fmt_size(size)}"
                row = QListWidgetItem(text)
                row.setData(Qt.ItemDataRole.UserRole, _pick(item, ("url", "direct_url"), default=""))
                self._media_quality_list.addItem(row)

        def _media_download(self):
            if not self._require_engine():
                return
            item = self._media_quality_list.currentItem()
            if item is None:
                QMessageBox.information(self, "Nothing selected", "Detect media first, then pick a quality.")
                return
            url = item.data(Qt.ItemDataRole.UserRole)
            if not url or not _is_url(str(url)):
                QMessageBox.warning(self, "No download URL", "The selected quality has no direct download URL.")
                return
            self._run_background(
                lambda: call_engine(self.engine, ("add", "add_download", "add_url", "enqueue"), str(url)),
                on_success=lambda task_id: (
                    self.statusBar().showMessage(f"Added media download: {url}", 5000),
                    self._show_section("Downloads"),
                ),
                on_failure=lambda message: QMessageBox.critical(
                    self, "Could not add download", message
                ),
            )

        # ------------------------------------------------------------------
        # History
        # ------------------------------------------------------------------
        def _refresh_history(self):
            if not hasattr(self, "_history_list"):
                return
            self._history_list.clear()
            entries = []
            if self.state_store is not None:
                try:
                    entries = self.state_store.list_tasks(status="completed")
                except Exception:
                    entries = []
            if not entries:
                entries = [r for r in self._downloads.values() if r["status"] == "completed"]
            for entry in entries or []:
                name = _pick(entry, ("filename", "name", "title"), default=str(entry))
                url = _pick(entry, ("url",), default="")
                self._history_list.addItem(f"{name}" + (f" — {url}" if url else ""))

        def _history_clear(self):
            if self.state_store is not None:
                try:
                    completed = self.state_store.list_tasks(status="completed")
                except Exception as exc:
                    QMessageBox.warning(self, "Could not clear history", str(exc))
                    return
                failures = []
                for record in completed or []:
                    try:
                        self.state_store.delete_task(record.id)
                    except Exception as exc:
                        failures.append(str(exc))
                if failures:
                    QMessageBox.warning(self, "Could not clear history", "\n".join(failures[:5]))
                    return
            self._history_list.clear()
            self.statusBar().showMessage("History cleared.", 3000)

        # ------------------------------------------------------------------
        # Browser integration
        # ------------------------------------------------------------------
        def _browser_test_connection(self):
            # Real handshake: build an IpcServer (binding nothing) and run
            # its ping dispatch, exactly as the native host does for a
            # browser message. The manifest/registry check runs as well so
            # the user sees whether the host is actually registered.
            ok = False
            detail = ""
            if _IPC_MODULE is not None:
                server_cls = getattr(_IPC_MODULE, "IpcServer", None)
                if server_cls is not None:
                    try:
                        server = server_cls()
                        result = server.handle_message({"type": "ping"})
                        ok = isinstance(result, dict) and result.get("status") == "ok"
                        detail = (
                            f"IPC ping/pong succeeded ({result.get('message')})."
                            if ok else f"IPC ping failed: {result}"
                        )
                    except Exception as exc:
                        detail = f"IPC test failed: {exc}"
            manifest = self._native_host_manifest_status()
            detail = f"{detail} {manifest}" if detail else manifest
            if ok:
                self._browser_status.setText(f"Status: connected. {detail}")
                self.statusBar().showMessage("Browser integration: connected.", 5000)
            else:
                self._browser_status.setText(f"Status: not connected. {detail}")
                QMessageBox.warning(self, "Browser integration test", detail)

        def _native_host_manifest_status(self) -> str:
            host_name = "com.tserver.downloader"
            if os.name == "nt":
                try:
                    import winreg

                    for browser in ("Chrome", "Microsoft Edge"):
                        if browser == "Chrome":
                            key_path = rf"Software\Google\Chrome\NativeMessagingHosts\{host_name}"
                        else:
                            key_path = rf"Software\Microsoft\Edge\NativeMessagingHosts\{host_name}"
                        try:
                            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                                path, _ = winreg.QueryValueEx(key, "")
                            return f"Native messaging host manifest found for {browser}: {path}"
                        except OSError:
                            continue
                    return (
                        "Native messaging host is not registered in the Windows "
                        "registry. Install the app (the installer writes the "
                        "HKCU Chrome/Edge entries) and enable the extension."
                    )
                except Exception as exc:
                    return f"Registry check failed: {exc}"
            candidates = [
                Path.home() / ".config" / "google-chrome" / "NativeMessagingHosts" / f"{host_name}.json",
                Path.home() / ".config" / "chromium" / "NativeMessagingHosts" / f"{host_name}.json",
                Path.home() / ".mozilla" / "native-messaging-hosts" / f"{host_name}.json",
            ]
            for candidate in candidates:
                if candidate.exists():
                    return f"Native messaging host manifest found: {candidate}"
            return (
                "Native messaging host manifest not found. Install the app "
                "and the browser extension, then test again."
            )

        # ------------------------------------------------------------------
        # Ad blocker
        # ------------------------------------------------------------------
        def _adblock_toggle(self, checked: bool):
            self._ui_prefs["adblock_enabled"] = bool(checked)
            self._save_ui_prefs()
            if self._adblock is not None:
                # AdBlockEngine has no enable flag of its own; the toggle is
                # carried on the instance so the download pipeline can read
                # it, and persisted in ui_prefs.json.
                self._adblock.enabled = bool(checked)
            self._refresh_adblock_stats()

        def _refresh_adblock_allowlist(self):
            if not hasattr(self, "_adblock_allowlist"):
                return
            self._adblock_allowlist.clear()
            entries = list(getattr(self.settings, "adblock_allowlist", []) or [])
            if LOCKED_ALLOWLIST_ENTRY not in entries:
                entries.insert(0, LOCKED_ALLOWLIST_ENTRY)
            if self._adblock is not None:
                live = list(getattr(self._adblock, "allowlist", []) or [])
                if live:
                    entries = live
            for entry in dict.fromkeys(entries):
                item = QListWidgetItem(str(entry))
                if str(entry) == LOCKED_ALLOWLIST_ENTRY:
                    item.setText(f"{entry}  (always allowed — locked)")
                self._adblock_allowlist.addItem(item)

        def _sync_allowlist(self):
            """Persist the allowlist to Settings and the engine instance."""
            entries = list(getattr(self.settings, "adblock_allowlist", []) or [])
            if self._adblock is not None:
                self._adblock.allowlist = list(entries)
            self._save_settings_quietly()

        def _adblock_add_allow(self):
            domain = self._adblock_allow_input.text().strip().lower()
            if not domain:
                return
            entries = list(getattr(self.settings, "adblock_allowlist", []) or [])
            if domain not in entries:
                entries.append(domain)
                self.settings.adblock_allowlist = entries
                self._sync_allowlist()
            self._adblock_allow_input.clear()
            self._refresh_adblock_allowlist()

        def _adblock_remove_allow(self):
            item = self._adblock_allowlist.currentItem()
            if item is None:
                return
            domain = item.text().split("  (")[0].strip()
            if domain == LOCKED_ALLOWLIST_ENTRY:
                QMessageBox.information(
                    self, "Locked entry",
                    f"{LOCKED_ALLOWLIST_ENTRY} is always allowed and cannot be removed.",
                )
                return
            entries = [e for e in (getattr(self.settings, "adblock_allowlist", []) or []) if e != domain]
            self.settings.adblock_allowlist = entries
            self._sync_allowlist()
            self._refresh_adblock_allowlist()

        def _adblock_add_rule(self):
            rule = self._adblock_rule_input.text().strip()
            if not rule:
                return
            if self._adblock is None:
                QMessageBox.warning(self, "Ad blocker unavailable", "The ad-block engine is not installed in this build.")
                return
            try:
                added = self._adblock.add_custom_rule(rule)
            except Exception as exc:
                QMessageBox.warning(self, "Could not add rule", str(exc))
                return
            if added is None:
                QMessageBox.warning(self, "Could not add rule", f"The rule was not recognised: {rule}")
                return
            self._adblock_rule_input.clear()
            self._refresh_adblock_stats()
            self.statusBar().showMessage(f"Custom rule added: {rule}", 5000)

        def _refresh_adblock_stats(self):
            if not hasattr(self, "_adblock_stats"):
                return
            if self._adblock is None:
                self._adblock_stats.setText("Ad blocker stats: engine not installed.")
                return
            stats = None
            try:
                stats = self._adblock.get_stats()
            except Exception:
                stats = getattr(self._adblock, "stats", None)
                if callable(stats):
                    try:
                        stats = stats()
                    except Exception:
                        stats = None
            rules = "?"
            try:
                rule_count = getattr(self._adblock, "rule_count", None)
                if callable(rule_count):
                    rules = rule_count()
            except Exception:
                pass
            if isinstance(stats, dict):
                blocked = stats.get("blocked_count", stats.get("blocked", 0))
                allowed = stats.get("allowed_count", stats.get("allowed", 0))
                self._adblock_stats.setText(
                    f"Blocked: {blocked}   Allowed: {allowed}   Rules loaded: {rules}"
                )
            else:
                blocked = getattr(self._adblock, "blocked_count", None)
                self._adblock_stats.setText(
                    f"Blocked: {blocked if blocked is not None else '—'}   "
                    f"Rules loaded: {rules}   "
                    f"Enabled: {bool(self._pref('adblock_enabled', True))}"
                )

        # ------------------------------------------------------------------
        # Settings
        # ------------------------------------------------------------------
        def _settings_browse_dir(self):
            folder = QFileDialog.getExistingDirectory(
                self, "Choose download folder",
                self._settings_dir.text() or str(Path.home() / "Downloads"),
            )
            if folder:
                self._settings_dir.setText(folder)

        def _settings_save(self):
            self.settings.download_dir = self._settings_dir.text().strip() or str(Path.home() / "Downloads")
            self.settings.connections_per_download = self._settings_connections.value()
            self.settings.max_simultaneous = self._settings_max_simultaneous.value()
            self.settings.global_speed_limit_kbps = self._settings_speed.value()
            names = [c.strip() for c in self._settings_categories.text().split(",") if c.strip()]
            existing = getattr(self.settings, "categories", None)
            if isinstance(existing, dict):
                self.settings.categories = {
                    name: existing.get(name, {"extensions": [], "subfolder": name})
                    for name in names
                }
            else:
                self.settings.categories = names
            self.settings.clipboard_monitor = self._settings_clipboard.isChecked()
            self.settings.scheduler_enabled = self._settings_scheduler.isChecked()
            # UI-only preferences live in ui_prefs.json, not in Settings.
            self._ui_prefs["proxy_host"] = self._settings_proxy_host.text().strip()
            self._ui_prefs["proxy_port"] = self._settings_proxy_port.value()
            self._ui_prefs["proxy_user"] = self._settings_proxy_user.text().strip()
            self._ui_prefs["proxy_password"] = self._settings_proxy_pass.text()
            self._ui_prefs["theme"] = self._settings_theme.currentText()
            self._ui_prefs["auto_update_check"] = self._settings_auto_update.isChecked()
            self._ui_prefs["update_feed_url"] = self._settings_update_url.text().strip()
            self._save_ui_prefs()
            if not self._save_settings_quietly():
                QMessageBox.warning(self, "Settings", "Settings could not be saved (no save method available).")
                return
            if self.engine is not None:
                try:
                    limiter = getattr(self.engine, "global_limiter", None)
                    if limiter is not None and hasattr(limiter, "set_rate"):
                        limiter.set_rate(self.settings.global_speed_limit_kbps * 1024)
                except Exception:
                    pass
            try:
                from ..app import apply_theme as _apply_theme

                _apply_theme(QApplication.instance(), self.settings.theme if hasattr(self.settings, "theme") else self._ui_prefs["theme"])
            except Exception:
                logger.warning("Could not apply theme after saving settings.")
            QMessageBox.information(self, "Settings", "Settings saved.")
            self.statusBar().showMessage("Settings saved.", 3000)

        def _save_settings_quietly(self) -> bool:
            try:
                save = getattr(self.settings, "save", None)
                if callable(save):
                    save()
                    return True
            except Exception as exc:
                logger.warning("Settings.save() failed: %s", exc)
                return False
            return False

        # ------------------------------------------------------------------
        # Diagnostics
        # ------------------------------------------------------------------
        def _log_path(self):
            if self.log_file and self.log_file.exists():
                return self.log_file
            candidates = []
            for attr in ("log_dir", "log_file"):
                value = getattr(self.settings, "log_dir", None) if attr == "log_dir" else getattr(self.settings, "log_file", None)
                if value:
                    candidates.append(Path(str(value)))
            for candidate in candidates:
                if candidate.is_dir():
                    found = sorted(candidate.glob("*.log"))
                    if found:
                        return found[-1]
                elif candidate.is_file():
                    return candidate
            return None

        def _diagnostics_refresh(self):
            if not hasattr(self, "_diag_log"):
                return
            path = self._log_path()
            if path is None:
                self._diag_log.setPlainText("No log file found yet.")
                return
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                self._diag_log.setPlainText(f"Could not read log file {path}: {exc}")
                return
            self._diag_log.setPlainText(text[-200000:])
            self._diag_log.verticalScrollBar().setValue(
                self._diag_log.verticalScrollBar().maximum()
            )

        def _diagnostics_export(self):
            path = self._log_path()
            if path is None:
                QMessageBox.information(self, "Export Logs", "No log file found to export.")
                return
            dest, _ = QFileDialog.getSaveFileName(
                self, "Export Logs",
                str(Path.home() / "Downloads" / "tserver-downloader-logs.zip"),
                "ZIP archive (*.zip);;Text file (*.txt)",
            )
            if not dest:
                return
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError as exc:
                QMessageBox.critical(self, "Export Logs", f"Could not read log file: {exc}")
                return
            sanitised = "\n".join(_redact_line(line) for line in lines)
            try:
                if dest.lower().endswith(".txt"):
                    Path(dest).write_text(sanitised, encoding="utf-8")
                else:
                    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as archive:
                        archive.writestr("tserver-downloader.log", sanitised)
            except OSError as exc:
                QMessageBox.critical(self, "Export Logs", f"Could not write export: {exc}")
                return
            QMessageBox.information(
                self, "Export Logs",
                f"Sanitised logs exported to:\n{dest}\n\nCredentials and tokens were redacted.",
            )

        # ------------------------------------------------------------------
        # Updates / about / tray / shortcuts / drag & drop
        # ------------------------------------------------------------------
        def _check_for_updates(self):
            from ..app import __version__, check_for_updates

            feed_url = str(self._pref("update_feed_url", "") or "")
            if not feed_url:
                QMessageBox.information(
                    self, "Check for Updates",
                    "Automatic update checks are not configured for this "
                    "build (no releases feed URL). Download new releases "
                    "from the project's releases page.",
                )
                return

            def on_result(result):
                QTimer.singleShot(0, lambda: self._show_update_result(result))

            check_for_updates(__version__, feed_url, on_result)
            self.statusBar().showMessage("Checking for updates...", 5000)

        def _show_update_result(self, result):
            if "error" in result:
                QMessageBox.warning(self, "Check for Updates", result["error"])
                return
            if result.get("update_available"):
                text = (
                    f"Version {result['latest_version']} is available "
                    f"(you have {result['current_version']})."
                )
                if result.get("notes"):
                    text += f"\n\n{result['notes']}"
                if result.get("sha256"):
                    text += f"\n\nSHA-256: {result['sha256']}"
                text += "\n\nOpen the download page?"
                answer = QMessageBox.question(
                    self, "Update available", text,
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if answer == QMessageBox.StandardButton.Yes:
                    QDesktopServices.openUrl(QUrl(result.get("url") or result.get("download_url", "")))
            else:
                QMessageBox.information(
                    self, "Check for Updates",
                    f"You are up to date (version {result['current_version']}).",
                )

        def _about(self):
            from ..app import __version__

            QMessageBox.about(
                self, "About TServer Downloader",
                f"TServer Downloader {__version__}\n\n"
                "A fast, resumable download manager with browser "
                "integration, a media detector and an ad blocker.\n\n"
                "Built with Python and PyQt6. FFmpeg is bundled for media "
                "merging (see THIRD_PARTY_LICENSES.md).",
            )

        def _setup_tray(self):
            self._tray = None
            if not QSystemTrayIcon.isSystemTrayAvailable():
                return
            self._tray = QSystemTrayIcon(self)
            self._tray.setToolTip("TServer Downloader")
            tray_menu = QMenu(self)
            show_action = tray_menu.addAction("Show")
            show_action.triggered.connect(self.showNormal)
            quit_action = tray_menu.addAction("Quit")
            quit_action.triggered.connect(QApplication.instance().quit)
            self._tray.setContextMenu(tray_menu)
            self._tray.activated.connect(self._tray_activated)
            self._tray.show()

        def _tray_activated(self, reason):
            if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
                self.showNormal()
                self.activateWindow()

        def _setup_shortcuts(self):
            add_shortcut = QShortcut(QKeySequence("Ctrl+N"), self)
            add_shortcut.activated.connect(self._add_url)
            pause_shortcut = QShortcut(QKeySequence("Ctrl+P"), self)
            pause_shortcut.activated.connect(self._pause_selected)
            delete_shortcut = QShortcut(QKeySequence("Delete"), self)
            delete_shortcut.activated.connect(self._remove_selected)

        # Drag-and-drop: dropping URLs (or text containing them) adds downloads.
        def dragEnterEvent(self, event):  # noqa: N802 - Qt naming
            if event.mimeData().hasUrls() or event.mimeData().hasText():
                event.acceptProposedAction()
            else:
                event.ignore()

        def dropEvent(self, event):  # noqa: N802 - Qt naming
            if not self._require_engine():
                return
            urls = []
            for url in event.mimeData().urls():
                text = url.toString()
                if _is_url(text):
                    urls.append(text)
            if event.mimeData().hasText():
                for word in event.mimeData().text().split():
                    if _is_url(word):
                        urls.append(word)
            unique_urls = list(dict.fromkeys(urls))
            if not unique_urls:
                event.acceptProposedAction()
                return

            def _add_all():
                added = 0
                for drop_url in unique_urls:
                    try:
                        call_engine(self.engine, ("add", "add_download", "add_url", "enqueue"), drop_url)
                        added += 1
                    except Exception as exc:
                        logger.warning("Drag-and-drop add failed for %s: %s", drop_url, exc)
                return added

            def _on_added(added):
                if added:
                    self.statusBar().showMessage(f"Added {added} download(s) from drop.", 5000)
                    self._show_section("Downloads")

            self._run_background(
                _add_all,
                on_success=_on_added,
                on_failure=lambda message: QMessageBox.critical(
                    self, "Could not add download", message
                ),
            )
            event.acceptProposedAction()

        def closeEvent(self, event):  # noqa: N802 - Qt naming
            if self._tray is not None and self._tray.isVisible():
                self.hide()
                self._tray.showMessage(
                    "TServer Downloader",
                    "TServer Downloader is still running in the system tray.",
                    QSystemTrayIcon.MessageIcon.Information, 3000,
                )
                event.ignore()
                return
            if self._worker is not None:
                try:
                    self._worker.stop()
                except Exception:
                    pass
            event.accept()

else:  # PyQt6 missing: importing this module must still work.

    class SpeedGraphWidget:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs):
            raise RuntimeError("PyQt6 is not installed; the UI cannot run.")

    class MainWindow:  # type: ignore[no-redef]
        """Importable stand-in used only when PyQt6 is missing."""

        def __init__(self, *args, **kwargs):
            raise RuntimeError(
                "PyQt6 is not installed, so the main window cannot be created. "
                "Install it with: pip install -r requirements.txt"
            )
