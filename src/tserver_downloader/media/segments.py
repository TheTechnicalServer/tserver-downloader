"""Segment downloading for TServer Downloader.

OWN architecture, written from scratch. No IDM code is used or referenced.

:class:`SegmentDownloader` fetches HLS/DASH media segments (``.ts`` /
``.m4s`` / similar) with a small thread pool, retries each segment, verifies
every segment is non-empty, and writes them in playlist order as
``seg_00001.ts``, ``seg_00002.ts``, ... so the assembler can concatenate
them. Only the segment URLs the caller supplies are fetched; nothing here
decrypts, de-obfuscates or bypasses protection -- a segment that fails
after retries aborts the download with :class:`SegmentDownloadError`
instead of producing a silently corrupt file.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

import requests

__all__ = ["SegmentDownloadError", "SegmentDownloader"]

_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 TServerDownloader/1.0"
)

_SEGMENT_EXTENSIONS = {".ts", ".m4s", ".mp4", ".m4v", ".webm"}


class SegmentDownloadError(RuntimeError):
    """Raised when a segment cannot be downloaded or is empty."""


class SegmentDownloader:
    """Downloads an ordered list of media segments into a directory."""

    def __init__(self, timeout: int = 30, retries: int = 3) -> None:
        self.timeout = timeout
        self.retries = retries

    def download_segments(
        self,
        segment_urls: list[str],
        dest_dir: str | Path,
        headers: dict | None = None,
        max_workers: int = 4,
        on_progress=None,
    ) -> list[Path]:
        """Download ``segment_urls`` in order into ``dest_dir``.

        Returns the list of written file paths in playlist order. Each file
        is named ``seg_00001.<ext>`` etc., keeping the segment's own
        extension (``.ts`` by default). ``on_progress(done, total, url)`` is
        called after each segment finishes. Raises
        :class:`SegmentDownloadError` if any segment fails after retries or
        arrives empty; already-downloaded segments are left on disk.
        """
        urls = list(segment_urls or [])
        if not urls:
            return []
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        total = len(urls)
        paths: list[Path | None] = [None] * total

        request_headers = {"User-Agent": _DEFAULT_USER_AGENT}
        if headers:
            request_headers.update(headers)

        def _download_one(index: int, url: str) -> Path:
            ext = self._segment_extension(url)
            target = dest / f"seg_{index + 1:05d}{ext}"
            last_error: Exception | None = None
            for attempt in range(1, self.retries + 1):
                try:
                    response = requests.get(
                        url, headers=request_headers, timeout=self.timeout
                    )
                    response.raise_for_status()
                    data = response.content
                    if not data:
                        raise SegmentDownloadError(
                            f"segment {url} downloaded empty"
                        )
                    target.write_bytes(data)
                    return target
                except SegmentDownloadError:
                    raise
                except Exception as exc:  # noqa: BLE001 - retried, then reported
                    last_error = exc
                    if attempt < self.retries:
                        time.sleep(0.5 * attempt)
            raise SegmentDownloadError(
                f"failed to download segment {url} after {self.retries} "
                f"attempts: {last_error}"
            )

        workers = max(1, int(max_workers or 1))
        done = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_download_one, i, url): i
                for i, url in enumerate(urls)
            }
            for future in as_completed(futures):
                index = futures[future]
                # Raises SegmentDownloadError on failure; aborts the pool.
                paths[index] = future.result()
                done += 1
                if on_progress is not None:
                    on_progress(done, total, urls[index])

        return [p for p in paths if p is not None]

    @staticmethod
    def _segment_extension(url: str) -> str:
        path = urlparse(url).path
        ext = ""
        if "." in path.rsplit("/", 1)[-1]:
            ext = "." + path.rsplit(".", 1)[-1].lower()
        return ext if ext in _SEGMENT_EXTENSIONS else ".ts"
