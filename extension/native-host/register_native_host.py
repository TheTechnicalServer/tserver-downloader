"""Register the TServer Downloader native messaging host (Windows only).

Writes HKCU\\Software\\Google\\Chrome\\NativeMessagingHosts\\
com.tserver.downloader (and the Edge equivalent) pointing at
``com.tserver.downloader.json`` next to this script. On non-Windows
systems it refuses gracefully instead of failing.

Usage:
    python register_native_host.py [--manifest PATH] [--host-path PATH]

``--host-path`` rewrites the "path" field of the manifest before
registering it, so the placeholder in the shipped template is replaced
with the real installed native-host location.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HOST_NAME = "com.tserver.downloader"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=None,
                        help="path to com.tserver.downloader.json")
    parser.add_argument("--host-path", default=None,
                        help="absolute path to the native host executable/script")
    args = parser.parse_args(argv)

    if sys.platform != "win32":
        print("This script only registers the native host on Windows "
              "(current platform: %s). Nothing was changed." % sys.platform)
        return 2

    manifest = Path(args.manifest) if args.manifest else \
        Path(__file__).resolve().parent / "com.tserver.downloader.json"
    if not manifest.exists():
        print("ERROR: manifest not found: %s" % manifest)
        return 1

    if args.host_path:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data["path"] = args.host_path
        manifest.write_text(json.dumps(data, indent=2), encoding="utf-8")
        print("Updated manifest path to: %s" % args.host_path)

    try:
        import winreg
    except ImportError as exc:  # pragma: no cover - Windows-only import
        print("ERROR: winreg unavailable: %s" % exc)
        return 1

    for browser in ("Google\\Chrome", "Microsoft\\Edge"):
        key_path = ("Software\\%s\\NativeMessagingHosts\\%s"
                    % (browser, HOST_NAME))
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, str(manifest))
        print("Registered under %s" % browser)
    print("Registered TServer Downloader native messaging host.")
    print("Manifest: %s" % manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
