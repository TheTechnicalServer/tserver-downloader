"""Permitted-only YouTube resolver for TServer Downloader.

OWN architecture, written from scratch. No IDM code is used or referenced.

.. warning::
   This resolver works **only** when the content is already openly
   permitted. It parses metadata from a page the caller supplies
   (``og:title``, ``og:video:url`` if present) and any HLS/DASH manifest
   URL that appears in that page in the open. It does **NOT**:

   * decrypt, unwrap or re-derive video signatures (``sig`` / ``s`` params),
   * call private / undocumented YouTube player APIs,
   * bypass age-gates, logins, DRM or paywalls,
   * fetch or use signed ``videoplayback`` URLs.

   If no permitted direct or manifest URL is found in the supplied page,
   it returns ``[]`` and records the reason ``"not_permitted_or_not_found"``
   on :attr:`PermittedYouTubeResolver.last_reason`. It never raises.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urlparse

from ..detector import MediaDetector
from ..resolver import MediaFormat, StreamResolver

__all__ = ["PermittedYouTubeResolver"]

REASON_NOT_PERMITTED = "not_permitted_or_not_found"

_YOUTUBE_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com")

# Manifest URLs that may appear in page markup (incl. JSON-escaped forms).
_MANIFEST_URL_RE = re.compile(
    r"https?://[^\s\"'<>\\]+?\.(?:m3u8|mpd)(?:[?#][^\s\"'<>]*)?",
    re.IGNORECASE,
)

# Signed / protected URL fragments that must never be followed.
_BLOCKED_MARKERS = ("signature=", "sig=", "videoplayback", "token=")


class _OgMetaParser(HTMLParser):
    """Collects Open Graph video metadata from supplied page HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "meta":
            return
        attrs_d = dict(attrs)
        key = attrs_d.get("property") or attrs_d.get("name")
        content = attrs_d.get("content")
        if key and content:
            self.meta.setdefault(key.strip().lower(), content.strip())


class PermittedYouTubeResolver:
    """YouTube resolver for openly permitted streams only.

    See the module docstring: no signature decryption, no private APIs, no
    DRM / age-gate / login bypass. Returns ``[]`` unless a permitted direct
    media URL or manifest URL is present in the supplied page.
    """

    def __init__(self, timeout: int = 15) -> None:
        self.timeout = timeout
        self.last_reason: str = ""
        self._detector = MediaDetector()
        self._stream_resolver = StreamResolver(timeout=timeout)

    def resolve(self, url: str, html: str | None = None) -> list[MediaFormat]:
        """Return permitted formats, or ``[]`` with ``last_reason`` set.

        Never raises.
        """
        try:
            return self._resolve_inner(url, html)
        except Exception:
            self.last_reason = REASON_NOT_PERMITTED
            return []

    def _resolve_inner(self, url: str, html: str | None) -> list[MediaFormat]:
        host = (urlparse(url or "").hostname or "").lower()
        if not any(host == h or host.endswith("." + h) for h in _YOUTUBE_HOSTS):
            self.last_reason = REASON_NOT_PERMITTED
            return []
        if not html:
            # Without supplied page metadata there is nothing permitted to
            # read; fetching/deriving a stream some other way is out of
            # scope for this resolver.
            self.last_reason = REASON_NOT_PERMITTED
            return []

        candidates: list[str] = []

        parser = _OgMetaParser()
        try:
            parser.feed(html)
        except Exception:
            pass
        og_video = parser.meta.get("og:video:url") or parser.meta.get(
            "og:video:secure_url"
        )
        if og_video:
            candidates.append(og_video)

        # Manifest URLs embedded in the page markup (JSON-escaped or plain).
        unescaped = html.replace("\\/", "/").replace("\\u0026", "&")
        candidates.extend(_MANIFEST_URL_RE.findall(unescaped))

        seen: set[str] = set()
        for candidate in candidates:
            candidate = candidate.strip()
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            lowered = candidate.lower()
            if any(marker in lowered for marker in _BLOCKED_MARKERS):
                continue
            if self._detector.is_protected_url(candidate):
                continue
            if self._detector.is_manifest_url(candidate):
                formats = self._stream_resolver.resolve(candidate)
                if formats:
                    self.last_reason = ""
                    return formats
            elif self._detector.is_media_url(candidate):
                formats = self._stream_resolver.resolve(candidate)
                if formats:
                    self.last_reason = ""
                    return formats

        self.last_reason = REASON_NOT_PERMITTED
        return []
