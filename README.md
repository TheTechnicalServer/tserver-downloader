# TServer Downloader

A fast, resumable download manager for Windows, with browser integration,
a media detector, a scheduler and a built-in ad blocker. Written in
Python with a PyQt6 desktop UI.

## Features

- **Multi-connection downloads** — split a file across up to 32 connections, with resume after interruption.
- **Download manager UI** — Dashboard, Downloads / Active / Queued / Completed / Failed views, live speed graph, search, sorting, drag-and-drop URLs, system tray with notifications.
- **Browser integration** — a Chrome/Edge/Firefox extension sends links to the app over native messaging.
- **Media detection** — paste a video/audio page URL, pick a quality, and download it.
- **Scheduler** — queue downloads to start at a chosen date and time.
- **Ad blocker** — blocks ad/tracker requests using EasyList-style rules, with an allowlist. `thetechnicalserver.blogspot.com` is always allowed and cannot be removed from the allowlist.
- **Clipboard monitoring** — download links copied to the clipboard can be picked up automatically (optional).
- **Proxy support** — HTTP proxy with optional authentication.
- **Diagnostics** — built-in log viewer and sanitised log export (credentials are redacted).
- **Dark / light / system theme**, portable mode, automatic update checks (when a releases feed is configured).

## Install

### Windows installer (recommended)

1. Download `TServerDownloaderSetup-1.0.0.exe` from the project's releases page.
2. Run it. It installs to `%ProgramFiles%\TServer Downloader` (per-user install), adds a Start Menu entry, an optional desktop icon, and registers the native messaging host for Chrome and Edge.
3. Launch **TServer Downloader** from the Start Menu.

### From source

Requires Python 3.12 and pip.

```powershell
git clone <repository-url>
cd tserver-downloader
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -e .
python -m tserver_downloader
```

Portable mode (settings/state/logs kept in `./data` next to the app):

```powershell
python -m tserver_downloader --portable
```

Other options: `--log-dir DIR` (custom log directory), `--version`.

## Usage

- **Add a download:** click **Add URL**, press `Ctrl+N`, drag a link onto the window, or send a link from the browser extension.
- **Pause / Resume / Cancel / Remove:** toolbar buttons, `Ctrl+P` to pause, `Delete` to remove, or right-click a download for Open file, Open folder, Copy URL, Retry, Remove.
- **Scheduler:** open the Scheduler page, enter a URL, a start time and a "run until" time, click **Add Scheduled Download**. The task is created immediately and held queued; the app starts it automatically when its run window opens (requires the scheduler to be enabled in Settings).
- **Media:** open the Media page, paste a page URL, click **Detect**, pick a quality, click **Download Selected Quality**.
- **Settings:** download folder, connections per download, speed limit, categories, clipboard monitoring, proxy, theme, update checks — click **Save Settings**.
- **Closing the window** minimises the app to the system tray; right-click the tray icon and choose **Quit** to exit fully.

## Browser extension install

1. Chrome / Edge: open `chrome://extensions`, enable **Developer mode**, click **Load unpacked**, and select `extension/chrome`.
2. Firefox: open `about:debugging#/runtime/this-firefox`, click **Load Temporary Add-on**, and select `extension/firefox/manifest.json` (or install the signed `.xpi` when available).
3. In the app, open **Browser Integration** and click **Test Connection** — it runs the native-messaging handshake and reports the result. The installer registers the host for Chrome and Edge automatically; a from-source run does not write registry entries, so the test will report the manifest as not found until the app is installed.

## Ad blocker allowlist

The ad blocker never blocks the project's own site: `thetechnicalserver.blogspot.com` is pinned in the allowlist on the **Ad Blocker** page and the UI refuses to remove it. Other domains can be added or removed freely, and custom blocking rules can be added in EasyList syntax.

## Configuration

Settings live in `%APPDATA%\TServer Downloader\settings.json` (or `./data/settings.json` in portable mode) and are editable from the **Settings** page. The settings file holds the download settings; theme, proxy credentials and the update feed URL are UI-only preferences kept in `ui_prefs.json` in the same folder.

| Setting | Meaning |
|---|---|
| Download folder (`download_dir`) | Where finished files are saved |
| Connections per download (`connections_per_download`) | 1–32 parallel connections |
| Max simultaneous downloads (`max_simultaneous`) | How many downloads run at once |
| Speed limit (`global_speed_limit_kbps`) | KB/s, 0 = unlimited |
| Categories | Download categories (name → extensions/subfolder) |
| Clipboard monitor (`clipboard_monitor`) | Pick up copied download links |
| Enable scheduler (`scheduler_enabled`) | Allow scheduled downloads to start |
| Proxy host / port / user / password | Optional HTTP proxy (ui_prefs.json) |
| Theme | `system`, `dark`, or `light` (ui_prefs.json) |
| Check for updates automatically | Poll the configured update feed (ui_prefs.json) |
| Update feed URL (HTTPS) | JSON update manifest (ui_prefs.json) |

Logs live in `%APPDATA%\TServer Downloader\logs\tserver-downloader.log`. Download state lives in `state.db` (SQLite) in the same data folder.

## Troubleshooting

- **"PyQt6 is not installed"** — run `pip install -r requirements.txt` in the virtual environment and relaunch.
- **"Download engine is not available"** — the app started without its engine module; reinstall from the installer or a complete source checkout.
- **Browser test fails** — install the app (not just run from source), enable the extension, and check the extension has permission for the site.
- **A download won't resume** — the server must support HTTP range requests; otherwise the download restarts from the beginning.
- **Downloads are slow** — try fewer connections (some servers throttle per-connection), or check the speed limit in Settings.
- **Log export** — use Diagnostics → **Export Logs...**; credentials and tokens are redacted automatically.

## Security

- Download URLs are validated as `http(s)` before being accepted.
- Log exports are sanitised: API keys, tokens, passwords and credentials embedded in URLs are redacted.
- The app never bypasses DRM, never captures credentials, and never runs downloaded files automatically — use **Open file** deliberately.
- The native messaging host only accepts messages from the project's own extension ID; the browser enforces that binding.

## License note

TServer Downloader's own code is distributed under the terms in the repository. It depends on third-party software under different licences — see [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md). **Important: PyQt6 is licensed under the GPL v3.** If you distribute this application, you must comply with the GPL v3 (including providing source for the application), or replace PyQt6 with a commercial Qt licence / PySide6. See THIRD_PARTY_LICENSES.md for the full picture.
