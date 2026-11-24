# Architecture — TServer Downloader

## Module diagram

```
python -m tserver_downloader
        │
        ▼
src/tserver_downloader/__main__.py      CLI parsing (--portable/--log-dir),
        │                               logging setup, component wiring
        ▼
src/tserver_downloader/app.py           create_app() factory, theming,
        │                               update-check worker (threaded)
        ▼
src/tserver_downloader/ui/main_window.py  QMainWindow: sidebar + QStackedWidget
        │                               pages, downloads table, tray,
        │                               drag-and-drop, speed graph
        │
        ├─► src/tserver_downloader/ui/workers.py   DownloadWorker (QThread)
        │                                          polls engine, emits signals
        │
        ├─► engine (DownloadEngine)    download queue, connections, resume
        │   .add(url) -> task id, .list_tasks(), .pause/.resume/.cancel/
        │   .retry/.start/.get_task(), .scheduler (Scheduler)
        ├─► state (StateStore)         SQLite (state.db): tasks + segments
        ├─► config (Settings)          JSON-backed user preferences
        │   (settings.json; theme/proxy/update feed live in ui_prefs.json)
        ├─► media (StreamResolver / SiteResolverRegistry)
        │                              page URL -> [MediaFormat] -> direct URL
        ├─► adblock (AdBlockEngine)    EasyList-style matching + allowlist
        ├─► updater                    HTTPS manifest + SHA-256 verification
        └─► browser.ipc (IpcServer)    native-messaging / localhost IPC

extension/chrome|firefox               browser extension (JS)
        │  native messaging (stdin/stdout JSON)
        ▼
ipc host manifest (native-messaging-host.json, registered in HKCU)
```

The UI owns no download logic: it is a thin view over the engine. Every
button in the UI calls a real method — on the engine, the state store,
settings, the ad-block engine, the media resolver, or the IPC module —
through the duck-typed `call_engine()` helper in `ui/workers.py`, which
tries the known method names for each operation. Component imports in the
UI are guarded with `try/except`, so the UI still imports (and can be
smoke-tested) when a component module is absent; the affected actions
then show a clear error dialog instead of crashing.

## Data flow

1. A URL enters through Add URL / drag-and-drop / clipboard / extension /
   scheduler / media page. The UI validates it as `http(s)` and calls
   `engine.add(url)` on a background thread (the call probes the server
   first, so it must not run on the GUI thread); it returns a task id.
2. The engine splits the file into segments, downloads them over
   parallel HTTP range requests, writes to a `.part` file, and exposes
   per-task progress through `DownloadTask` properties (`progress` as a
   0.0–1.0 fraction, `speed` in bytes/s, `eta` in seconds).
3. `DownloadWorker` polls `engine.list_tasks()` every 500 ms on its own
   thread and emits the normalised download list to the main thread via
   a Qt signal. The main window updates the table, the dashboard
   counters and the speed graph from that signal only. Each poll also
   starts any scheduled tasks whose run window has opened (see below).
4. On completion the engine finalises the file into the download folder,
   the state store records the entry in history, and the UI posts a
   system-tray notification.
5. Settings changes are written by `Settings.save()`; the engine is told
   to re-apply the speed limit.

## Threading model

- **Main (GUI) thread:** all widgets, signals, dialogs, tray, drag-and-drop.
- **DownloadWorker (QThread):** polls the engine; never touches a widget
  directly — it only emits `progress_updated(list)` / `worker_error(str)`.
- **Engine threads:** owned by the engine (one per connection / download);
  the engine is responsible for its own locking. The UI only reads
  snapshots returned by the engine's accessors.
- **Update check:** a plain daemon `threading.Thread` in `app.py`; its
  callback is marshalled back to the GUI thread with `QTimer.singleShot`.
- Rule: no blocking I/O or engine call that can wait is ever made on the
  GUI thread except short, local operations (settings save, dialog input).

## State recovery

## Scheduler

A scheduled download is a normal engine task plus a run window:
`Scheduler.add_schedule(task_id, start_time, stop_time)`. The Scheduler
only *decides* when a task is due (`due_tasks(now)`, `should_run(...)`);
nothing in the engine polls it, so the UI's progress poll starts due
tasks (`engine.start(task_id)`) and clears their schedule, and only
does so while `settings.scheduler_enabled` is on. Removing a schedule
in the UI only removes the window; the underlying task stays queued.

## State recovery

- `StateStore` persists the download list, history and per-segment
  progress in SQLite (`state.db`, WAL mode) in the data directory
  (`%APPDATA%\TServer Downloader` on Windows,
  `~/.local/share/tserver-downloader` elsewhere, `./data` in portable
  mode). On start-up the engine reloads it, so interrupted downloads can
  be resumed or retried instead of lost; tasks that were `active` when
  the app died are restored as `paused`.
- Partial files are kept next to their temporary names; resume uses HTTP
  range requests when the server supports them.
- Logs use a rotating file handler (5 MB, 3 backups) so a long session
  cannot fill the disk.

## IPC protocol (browser integration)

The browser extension talks to the app through Chrome/Firefox native
messaging: the browser launches the registered host and exchanges
length-prefixed JSON messages over stdin/stdout.

- Handshake: extension sends `{"type": "ping"}`; the host replies
  `{"status": "ok", "message": "pong"}` (`IpcServer.handle_message`).
  A `{"type": "handshake", "token": ...}` exchange returns the app name
  and version after token validation. The UI's **Test Connection**
  button runs the ping dispatch against a real `IpcServer` instance and
  also checks the native-messaging manifest registration.
- Add download: extension sends
  `{"type": "add", "url": "<url>", "filename": "<optional>"}`; the host
  validates the URL and forwards it to `engine.add(url)`.
- The host manifest (`native-messaging-host.json`) is installed next to
  the executable and registered under
  `HKCU\Software\Google\Chrome\NativeMessagingHosts\com.tserver.downloader`
  and the equivalent Edge key by the Inno Setup installer. The browser
  enforces that only the extension ID listed in the manifest may connect.

## Media pipeline

Media page → `SiteResolverRegistry.resolve(url)` (falling back to
`StreamResolver().resolve(url)`) → list of `MediaFormat`
(`{url, quality_label, container, estimated_size, is_audio_only,
is_video_only}`) → user picks one → `engine.add(direct_url)`.
Detection runs on a background thread from the Detect button click; the
resolver parses the page (or its HLS/DASH manifest) and returns direct
media URLs. The engine then downloads the chosen stream like any other
file, and FFmpeg (when bundled) merges separate audio/video streams.

## Ad-block pipeline

`AdBlockEngine(allowlist=[...])` loads EasyList-style rules (bundled
offline lists via `load_bundled_lists()`) plus the user's custom rules
(`add_custom_rule(line)`). Every request the engine is about to make is
checked with `should_block(url)`; allowlisted domains bypass the check
entirely. `thetechnicalserver.blogspot.com` is pinned in the allowlist
(`DEFAULT_ALLOWLIST`) and the UI refuses to remove it. Statistics
(`get_stats()` → `blocked_count` / `allowed_count`, plus `rule_count()`)
are read back from the engine and shown on the Ad Blocker page. The
blocker only filters requests made by the downloader itself; it is not a
system-wide proxy.

## Updates

`app.check_for_updates()` fetches the configured update feed
(`ui_prefs.json` → `update_feed_url`) on a daemon thread and understands
two manifest shapes: the project's own updater manifest
(`{"version", "download_url", "sha256", "notes", "mandatory"}`, compared
with `updater.is_newer_version`) and the GitHub releases `/latest`
shape (`{"tag_name", "html_url"}`). `updater.download_update()` only
ever returns bytes whose SHA-256 matches the manifest, and downloaded
code is never executed by the updater. With no feed configured, the UI
says update checks are unavailable instead of guessing a URL.

## Security decisions

- URLs are validated as `http(s)` at every entry point.
- Log exports are sanitised: API keys, tokens, passwords and
  credentials embedded in URLs are redacted before writing.
- No downloaded file is executed automatically; opening a file is an
  explicit user action.
- Proxy credentials are stored in the local settings file only, never
  logged.
- The native messaging host accepts messages only from the extension ID
  in its manifest (enforced by the browser).
- No DRM bypass: the media resolver only downloads streams the page
  itself exposes; encrypted/DRM-protected content is not decrypted.

## Limitations

- **Windows build required for the installer:** the PyInstaller + Inno
  Setup pipeline runs on Windows (see `.github/workflows/build-windows.yml`).
  From-source runs work on Linux/macOS for development, but the tray,
  registry registration and installer are Windows-only.
- **Extension store policies:** the unpacked extension works for
  development; publishing to the Chrome Web Store / Edge Add-ons /
  Firefox Add-ons requires store review and a signed build.
- **No DRM bypass:** protected streams cannot be downloaded.
- **Resume depends on the server:** servers without HTTP range support
  force a restart from the beginning.
- **PyQt6 is GPL v3:** distributing the app requires GPL compliance or a
  commercial Qt licence / PySide6 replacement (see THIRD_PARTY_LICENSES.md).
