"""Entry point: ``python -m tserver_downloader``.

Parses ``--portable`` / ``--log-dir`` (and ``--version``), sets up logging,
builds :py:class:`Settings`, :py:class:`StateStore` and
:py:class:`DownloadEngine`, then launches the PyQt6 UI.  When PyQt6 is not
installed it prints plain CLI help and exits with a clear message instead
of crashing on an import error.

The engine/settings/state classes are imported from their canonical
modules when present.  If a component module is not available (for
example a source checkout where the engine has not been installed yet),
settings fall back to a small JSON-backed implementation in this file so
the UI can still launch and explain what is missing; the download engine
is then ``None`` and the UI disables download actions with a real error
dialog.  This fallback is a real, working settings store, not a stub.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

if __package__ in (None, ""):
    # __main__.py executed as a plain script (``python __main__.py`` or
    # a PyInstaller entry point) has no parent package, so a relative
    # import would raise "attempted relative import with no known parent
    # package". Import absolutely instead; src/ is on sys.path in both
    # cases (PyInstaller adds the bundle dir, plain runs add it here).
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from tserver_downloader.app import __version__, create_app
else:
    from .app import __version__, create_app

logger = logging.getLogger("tserver_downloader")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def _project_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    # src/tserver_downloader/__main__.py -> project root is three levels up.
    return Path(__file__).resolve().parent.parent.parent


def data_dir(portable: bool) -> Path:
    """Directory for settings, state and logs."""
    if portable:
        return _project_root() / "data"
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / "TServer Downloader"
    return Path.home() / ".local" / "share" / "tserver-downloader"


def log_dir_for(portable: bool, explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    return data_dir(portable) / "logs"


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def setup_logging(log_dir: Path) -> Path:
    """Configure console + rotating-file logging; return the log file path."""
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "tserver-downloader.log"
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)
    try:
        file_handler = RotatingFileHandler(
            log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError as exc:
        print(f"Warning: could not open log file {log_file}: {exc}", file=sys.stderr)
    return log_file


# ---------------------------------------------------------------------------
# Component loading (canonical imports first, working fallback second)
# ---------------------------------------------------------------------------


class _FallbackSettings:
    """Minimal JSON-backed settings used only when the real Settings
    class cannot be imported.  Same attribute surface the UI expects."""

    FILENAME = "settings.json"

    def __init__(self, directory: Path):
        self._path = Path(directory) / self.FILENAME
        self.download_dir = str(Path.home() / "Downloads")
        self.max_connections = 8
        self.speed_limit_kbps = 0
        self.theme = "system"
        self.clipboard_monitor = True
        self.proxy_host = ""
        self.proxy_port = 0
        self.proxy_user = ""
        self.proxy_password = ""
        self.categories: dict = {
            "Video": ["mp4", "mkv", "webm"],
            "Audio": ["mp3", "flac", "wav"],
            "Documents": ["pdf", "docx", "zip"],
        }
        self.adblock_enabled = True
        self.adblock_allowlist: list = ["thetechnicalserver.blogspot.com"]
        self.auto_update_check = False
        self.update_feed_url = ""
        self.log_dir = ""
        self.load()

    def load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(data, dict):
            for key, value in data.items():
                if hasattr(self, key):
                    setattr(self, key, value)

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            key: getattr(self, key)
            for key in (
                "download_dir", "max_connections", "speed_limit_kbps", "theme",
                "clipboard_monitor", "proxy_host", "proxy_port", "proxy_user",
                "proxy_password", "categories", "adblock_enabled",
                "adblock_allowlist", "auto_update_check", "update_feed_url",
                "log_dir",
            )
        }
        self._path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _try_import(names):
    """Import the first available ``module:Class`` pair from ``names``."""
    import importlib

    for dotted in names:
        module_name, _, attr = dotted.partition(":")
        try:
            module = importlib.import_module(module_name)
            return getattr(module, attr)
        except (ImportError, AttributeError):
            continue
    return None


def load_components(data_path: Path):
    """Return ``(settings, state_store, engine)``.

    The real classes live in ``tserver_downloader.config`` (Settings,
    loaded with the ``Settings.load(path)`` classmethod),
    ``tserver_downloader.state`` (StateStore, constructed with the
    *file* path ``<data>/state.db``) and ``tserver_downloader.engine``
    (DownloadEngine).  Any component that fails to construct is logged
    and comes back as ``None`` (settings fall back to the built-in JSON
    store); the UI degrades gracefully instead of refusing to start.
    """
    settings_cls = _try_import(
        [
            "tserver_downloader.config:Settings",
            "tserver_downloader.settings:Settings",
            "tserver_downloader.settings:AppSettings",
        ]
    )
    settings = None
    if settings_cls is not None:
        try:
            settings = settings_cls.load(data_path / "settings.json")
        except Exception as exc:
            logger.warning("Settings.load() failed (%s); using defaults.", exc)
            try:
                settings = settings_cls()
            except Exception:
                settings = None
    if settings is None:
        logger.warning("Real Settings class not found; using built-in settings store.")
        settings = _FallbackSettings(data_path)

    state_cls = _try_import(
        [
            "tserver_downloader.state:StateStore",
            "tserver_downloader.store:StateStore",
        ]
    )
    state_store = None
    if state_cls is not None:
        for candidate in (data_path / "state.db", data_path):
            try:
                state_store = state_cls(candidate)
                break
            except Exception as exc:
                logger.warning("StateStore(%s) failed: %s", candidate, exc)
                state_store = None
        if state_store is None:
            logger.warning("StateStore not available; download history will not persist.")
    else:
        logger.warning("StateStore not found; download history will not persist.")

    engine_cls = _try_import(
        [
            "tserver_downloader.engine:DownloadEngine",
            "tserver_downloader.downloader:DownloadEngine",
            "tserver_downloader.core.engine:DownloadEngine",
        ]
    )
    engine = None
    if engine_cls is not None:
        try:
            engine = engine_cls(settings, state_store)
        except TypeError:
            try:
                engine = engine_cls(settings)
            except Exception as exc:
                logger.warning("Could not initialise DownloadEngine: %s", exc)
                engine = None
        except Exception as exc:
            logger.warning("Could not initialise DownloadEngine: %s", exc)
            engine = None
        if engine is not None:
            logger.info("Download engine initialised: %s", type(engine).__name__)
    else:
        logger.warning(
            "DownloadEngine not found; the UI will start without download "
            "capability until the engine module is installed."
        )
    return settings, state_store, engine


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

CLI_HELP = """\
TServer Downloader {version} -- command line help
================================================

The graphical interface needs PyQt6, which is not installed in this
environment.  Install it and relaunch:

    pip install -r requirements.txt
    python -m tserver_downloader

Options:
    --portable     Keep settings, state and logs in ./data next to the app
                   instead of the per-user application data folder.
    --log-dir DIR  Write logs to DIR instead of the default location.
    --version      Print the version and exit.

Download management (engine CLI) is available once the engine is
installed; run the GUI for the full download manager.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tserver_downloader",
        description="TServer Downloader -- fast, resumable download manager.",
    )
    parser.add_argument(
        "--portable",
        action="store_true",
        help="Store settings/state/logs in ./data next to the app.",
    )
    parser.add_argument(
        "--log-dir",
        default=None,
        help="Directory for log files (default: app data/logs).",
    )
    parser.add_argument(
        "--version", action="store_true", help="Print the version and exit."
    )
    return parser


def main(argv: list | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.version:
        print(f"TServer Downloader {__version__}")
        return 0

    data_path = data_dir(args.portable)
    data_path.mkdir(parents=True, exist_ok=True)
    log_path = setup_logging(log_dir_for(args.portable, args.log_dir))
    logger.info("TServer Downloader %s starting (portable=%s)", __version__, args.portable)
    logger.info("Data directory: %s", data_path)
    logger.info("Log file: %s", log_path)

    settings, state_store, engine = load_components(data_path)
    if getattr(settings, "log_dir", "") in ("", None):
        try:
            settings.log_dir = str(log_path.parent)
        except Exception:
            pass

    app = create_app(settings)
    if app is None:
        print(CLI_HELP.format(version=__version__))
        print(
            "ERROR: PyQt6 is required for the graphical interface but is not "
            "installed.\nInstall it with:  pip install PyQt6>=6.5.0",
            file=sys.stderr,
        )
        return 2

    try:
        from .ui.main_window import MainWindow
    except ImportError:  # same plain-script / frozen entry-point case
        from tserver_downloader.ui.main_window import MainWindow

    window = MainWindow(
        settings, engine, state_store, log_file=log_path, data_dir=data_path
    )
    window.show()
    try:
        return int(app.exec())
    finally:
        try:
            window.shutdown()
        except Exception:
            logger.exception("Error while shutting down the main window.")
        try:
            if state_store is not None and hasattr(state_store, "close"):
                state_store.close()
        except Exception:
            logger.exception("Error while closing the state store.")
        try:
            if engine is not None and hasattr(engine, "shutdown"):
                engine.shutdown()
        except Exception:
            logger.exception("Error while shutting down the download engine.")


if __name__ == "__main__":
    raise SystemExit(main())
