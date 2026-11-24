# Third-Party Licences

TServer Downloader is built on the following third-party software. Each
entry lists the component, its licence, and what it means for this
project. A copy of this file is installed next to the executable by the
Windows installer.

## Python

- **Licence:** Python Software Foundation License (PSF), BSD-style.
- **Source:** https://www.python.org/
- The interpreter and standard library are used to run the application.
  No source changes are made to Python itself.

## requests

- **Licence:** Apache License 2.0.
- **Source:** https://github.com/psf/requests
- HTTP client used by the engine and the update checker.

## PyQt6

- **Licence:** GNU General Public License v3 (GPL v3).
- **Source:** https://www.riverbankcomputing.com/software/pyqt/
- Provides the Qt bindings for the desktop UI.
- **Important — read this before distributing the app:** PyQt6 is GPL
  v3. If you distribute TServer Downloader (for example the Windows
  installer), you must comply with the GPL v3: that includes making the
  application's source code available to recipients under the GPL v3.
  The alternatives are to buy a commercial Qt licence from The Qt
  Company, or to replace PyQt6 with **PySide6** (the official Qt for
  Python bindings, licensed under the LGPL v3), which permits
  distribution of a closed-source application provided the LGPL terms
  are followed. This project does not bundle a commercial Qt licence.

## FFmpeg

- **Licence:** LGPL v2.1 or later / GPL v2 or later, depending on the
  build configuration (the standard Windows builds from
  https://www.gyan.dev/ffmpeg/builds/ are LGPL unless `--enable-gpl`
  components are included).
- **Source:** https://ffmpeg.org/ — source code: https://github.com/FFmpeg/FFmpeg
- `ffmpeg.exe` is bundled next to the application **only when present**
  in `third_party/` at build time (see `packaging/tserver-downloader.spec`).
  It is used to merge separate audio/video streams from media downloads.
- If you bundle FFmpeg, keep this notice, ship the FFmpeg licence text
  with the binary, and be prepared to provide the corresponding FFmpeg
  source on request, as the LGPL/GPL require. If the bundled build is a
  GPL build, the combined work's GPL obligations apply.

## EasyList

- **Licence:** Creative Commons Attribution-ShareAlike 3.0 Unported
  (CC BY-SA 3.0).
- **Source:** https://easylist.to/
- The ad-block rule lists used by the ad-block engine are derived from
  EasyList. Attribution is retained in the rule files; derivative rule
  sets shared publicly must remain under CC BY-SA 3.0.

## yt-dlp (concept only — NOT bundled)

- **Licence:** The Unlicense (public domain dedication).
- **Source:** https://github.com/yt-dlp/yt-dlp
- **This project does NOT bundle yt-dlp and does not depend on it.**
  Media detection is performed by this project's own resolver
  (`src/tserver_downloader/media.py`). yt-dlp is mentioned here only
  because the PyInstaller spec reserves a slot for an optional
  `yt-dlp.exe` helper if a distributor chooses to add one; in that case
  the distributor must add yt-dlp's licence notice to this file and
  comply with its terms (The Unlicense imposes effectively no conditions,
  but attribution is good practice).

## Inno Setup

- **Licence:** Inno Setup License (BSD-style, free for commercial use).
- **Source:** https://jrsoftware.org/isinfo.php
- Used to build the Windows installer (`installer/tserver-downloader.iss`).

## PyInstaller

- **Licence:** GPL v2 with a linking exception for the bootloader; the
  generated application is not infected by the bootloader's licence.
- **Source:** https://pyinstaller.org/
- Used to package the application for Windows. The bootloader exception
  means the packaged app's own licence is unaffected, but the PyQt6 GPL
  obligations above still apply to the application itself.
