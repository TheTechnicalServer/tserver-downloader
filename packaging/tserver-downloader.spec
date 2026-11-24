# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for TServer Downloader (Windows).

One-directory build (onedir) for reliability: a single-file build is
slower to start and more fragile when the app needs its bundled
ffmpeg.exe / yt-dlp.exe helpers next to it.

Run from the repository root with:
    pyinstaller packaging/tserver-downloader.spec

Optional bundled helpers: if ``third_party/ffmpeg.exe`` exists it is
bundled as a binary; if it is absent the build continues and the app
falls back to a system ffmpeg on PATH. The same applies to the icon.
"""

import os
from pathlib import Path

SPEC_DIR = Path(SPECPATH)  # noqa: F821 - provided by PyInstaller
PROJECT_ROOT = SPEC_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"

block_cipher = None

# --- Optional binaries / data (bundled only when present) ------------------
binaries = []
for name in ("ffmpeg.exe", "yt-dlp.exe"):
    candidate = PROJECT_ROOT / "third_party" / name
    if candidate.exists():
        binaries.append((str(candidate), "."))
    else:
        print(f"NOTE: {candidate} not found; not bundling {name}.")

datas = []
license_note = PROJECT_ROOT / "THIRD_PARTY_LICENSES.md"
if license_note.exists():
    datas.append((str(license_note), "."))

icon = None
icon_candidate = PROJECT_ROOT / "packaging" / "tserver-downloader.ico"
if icon_candidate.exists():
    icon = str(icon_candidate)
else:
    print("NOTE: no icon found at packaging/tserver-downloader.ico; building without one.")

# Only the Qt modules the app actually uses (see src/: QtCore, QtGui,
# QtWidgets). Do NOT use collect_all("PyQt6") here: it pulls in every
# PyQt6 submodule (QtBluetooth, QtDesigner, QtQuick3D, QtSql, QtWebEngine,
# ...) and PyInstaller then spends 40+ minutes analysing and copying
# hundreds of MB of DLLs the app never uses. That is what stalled
# Run #2 for 45 minutes. PyInstaller's standard hooks already collect
# the plugins/translations for the modules listed below.
hiddenimports = [
    "PyQt6.QtCore",
    "PyQt6.QtGui",
    "PyQt6.QtWidgets",
    "PyQt6.QtNetwork",
]

a = Analysis(
    [str(SRC_DIR / "tserver_downloader" / "__main__.py")],
    pathex=[str(SRC_DIR)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="TServerDownloader",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX disabled: compressing ~200 MB of Qt binaries with UPX is
    # extremely slow on GitHub runners and can stall the build for
    # 20+ minutes. Output is slightly larger without it; the app
    # and installer work exactly the same.
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="TServerDownloader",
)
