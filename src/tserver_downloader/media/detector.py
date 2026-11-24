"""Media detection for TServer Downloader.

OWN architecture, written from scratch. No IDM code is used or referenced.

Detects downloadable media that a page or a network log exposes in the open:
direct ``<video>`` / ``<audio>`` / ``<source>`` elements, plain links to media
files, and network requests whose content type or extension is media or a
streaming manifest.

Scope / safety rules (deliberate, not accidental):

* Only permitted, openly exposed content is detected. This module never
  follows, unwraps or defeats authentication, DRM, signed URLs or paywalls.
* Any URL that carries an auth token (``token=``), a signature (``sig=`` /
  ``signature=``) or a DRM marker (``drm``, ``widevine``, ``fairplay``,
  ``playready``, ``license``) is skipped, not followed.
* Encrypted manifests (HLS ``EXT-X-KEY`` with a method other than ``NONE``)
  are rejected by the manifest parser; this detector never hands such a URL
  to a downloader.
* Detection is purely observational: it reads HTML / a request log that the
  caller supplies. It performs no network access itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlparse

__all__ = [
    "MediaCandidate",
    "MediaDetector",
    "MEDIA_EXTENSIONS",
    "MANIFEST_EXTENSIONS",
    "MEDIA_CONTENT_TYPES",
    "MANIFEST_CONTENT_TYPES",
    "PROTECTED_URL_MARKERS",
]

# Extensions treated as directly downloadable media files.
MEDIA_EXTENSIONS = frozenset(
    {
        ".mp4",
        ".webm",
        ".mkv",
        ".mov",
        ".avi",
        ".m4v",
        ".mp3",
        ".m4a",
        ".aac",
        ".ogg",
        ".oga",
        ".wav",
        ".flac",
        ".opus",
        ".ts",
        ".m4s",
    }
)

# Streaming-manifest extensions. These are playlists, not media bytes.
MANIFEST_EXTENSIONS = frozenset({".m3u8", ".mpd"})

AUDIO_EXTENSIONS = frozenset(
    {".mp3", ".m4a", ".aac", ".ogg", ".oga", ".wav", ".flac", ".opus"}
)

# Content-type prefixes / substrings that identify media or manifests.
MEDIA_CONTENT_TYPES = ("video/", "audio/")
MANIFEST_CONTENT_TYPES = (
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "application/dash+xml",
    "application/vnd.apple.mpegurl",
)

# Lower-cased substrings that mark a URL as auth/DRM protected. Such URLs
# are never followed: detecting them is not permission to use them.
PROTECTED_URL_MARKERS = (
    "token=",
    "sig=",
    "signature=",
    "drm",
    "widevine",
    "fairplay",
    "playready",
    "license",
)


def _extension(url: str) -> str:
    """Lower-cased file extension of a URL's path, ignoring query/fragment."""
    path = unquote(urlparse(url).path or "")
    dot = path.rfind(".")
    if dot == -1:
        return ""
    ext = path[dot:].lower()
    return ext if re.fullmatch(r"\.[a-z0-9]{2,5}", ext) else ""


def _is_protected(url: str) -> bool:
    lowered = url.lower()
    return any(marker in lowered for marker in PROTECTED_URL_MARKERS)


@dataclass
class MediaCandidate:
    """One openly exposed media item found by :class:`MediaDetector`.

    ``kind`` is ``"video"``, ``"audio"`` or ``"manifest"``. For manifests,
    ``manifest_type`` is ``"hls"`` or ``"dash"``. ``source`` records where
    the candidate came from: ``"html"`` or ``"request_log"``.
    """

    url: str
    kind: str = "video"
    content_type: str | None = None
    size: int | None = None
    source: str = "html"
    manifest_type: str | None = None


class _MediaHtmlParser(HTMLParser):
    """Collects media URLs from <video>/<audio>/<source> and <a> elements."""

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self._base_url = base_url
        self.urls: list[str] = []

    def _add(self, raw: str | None) -> None:
        if not raw:
            return
        raw = raw.strip()
        if not raw or raw.startswith(("data:", "blob:", "javascript:")):
            return
        self.urls.append(urljoin(self._base_url, raw))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_d = dict(attrs)
        if tag in ("video", "audio", "source"):
            self._add(attrs_d.get("src"))
        elif tag == "a":
            self._add(attrs_d.get("href"))


class MediaDetector:
    """Detects openly exposed media in HTML and in network request logs.

    The detector is stateless and performs no network access. All URLs it
    returns are absolute, de-duplicated (first occurrence wins) and free of
    auth/DRM-protected links.
    """

    def is_media_url(self, url: str) -> bool:
        """True if the URL looks like a direct media file by extension."""
        if not url or not isinstance(url, str):
            return False
        return _extension(url) in MEDIA_EXTENSIONS

    def is_manifest_url(self, url: str) -> bool:
        """True if the URL looks like an HLS (.m3u8) or DASH (.mpd) manifest."""
        if not url or not isinstance(url, str):
            return False
        return _extension(url) in MANIFEST_EXTENSIONS

    def is_protected_url(self, url: str) -> bool:
        """True if the URL carries auth/DRM markers and must not be followed.

        Such URLs are skipped by every detection method in this class.
        """
        return _is_protected(url or "")

    def _kind_for(self, url: str, content_type: str | None) -> tuple[str, str | None]:
        """Return (kind, manifest_type) for a URL / content-type pair."""
        ext = _extension(url)
        ct = (content_type or "").lower()
        if ext in MANIFEST_EXTENSIONS or "mpegurl" in ct or "dash+xml" in ct:
            manifest_type = "dash" if (ext == ".mpd" or "dash" in ct) else "hls"
            return "manifest", manifest_type
        if ext in AUDIO_EXTENSIONS or ct.startswith("audio/"):
            return "audio", None
        return "video", None

    def _is_media(self, url: str, content_type: str | None) -> bool:
        if self.is_media_url(url) or self.is_manifest_url(url):
            return True
        ct = (content_type or "").lower()
        return ct.startswith(MEDIA_CONTENT_TYPES) or any(
            marker in ct for marker in MANIFEST_CONTENT_TYPES
        )

    @staticmethod
    def _dedupe(candidates: list[MediaCandidate]) -> list[MediaCandidate]:
        seen: set[str] = set()
        out: list[MediaCandidate] = []
        for cand in candidates:
            if cand.url not in seen:
                seen.add(cand.url)
                out.append(cand)
        return out

    def detect_from_html(self, html: str, base_url: str) -> list[MediaCandidate]:
        """Find media in HTML: <video>/<audio>/<source> src and <a> links.

        Relative URLs are resolved against ``base_url``. Auth/DRM-protected
        URLs and non-media links are skipped. Never raises on malformed
        HTML; returns whatever could be parsed.
        """
        if not html or not base_url:
            return []
        parser = _MediaHtmlParser(base_url)
        try:
            parser.feed(html)
        except Exception:
            # Malformed HTML must not crash the caller; keep what we got.
            pass
        candidates: list[MediaCandidate] = []
        for url in parser.urls:
            if _is_protected(url):
                continue
            if not self._is_media(url, None):
                continue
            kind, manifest_type = self._kind_for(url, None)
            candidates.append(
                MediaCandidate(
                    url=url, kind=kind, source="html", manifest_type=manifest_type
                )
            )
        return self._dedupe(candidates)

    def detect_from_requests(
        self, requests_log: list[dict]
    ) -> list[MediaCandidate]:
        """Find media in a network request log.

        Each entry is a dict with ``url`` and optional ``content_type`` and
        ``size``. An entry qualifies when its content type is video/audio or
        a manifest type, or its URL ends in a media/manifest extension.
        Auth/DRM-protected URLs are skipped.
        """
        candidates: list[MediaCandidate] = []
        for entry in requests_log or []:
            if not isinstance(entry, dict):
                continue
            url = entry.get("url") or ""
            if not url or _is_protected(url):
                continue
            content_type = entry.get("content_type")
            if not self._is_media(url, content_type):
                continue
            kind, manifest_type = self._kind_for(url, content_type)
            size = entry.get("size")
            candidates.append(
                MediaCandidate(
                    url=url,
                    kind=kind,
                    content_type=content_type,
                    size=size if isinstance(size, int) else None,
                    source="request_log",
                    manifest_type=manifest_type,
                )
            )
        return self._dedupe(candidates)
