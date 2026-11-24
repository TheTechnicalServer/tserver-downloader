"""Metadata extraction for TServer Downloader.

OWN architecture, written from scratch. No IDM code is used or referenced.

:class:`MetadataExtractor` reads the metadata a page openly publishes about
its media -- Open Graph / ``<title>`` / ``itemprop`` tags in supplied HTML --
and derives a human-readable title from a URL. It performs no network
access and never guesses at data the page does not provide (missing fields
are ``None``). :func:`sanitize` turns a title into a safe file-name stem.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import unquote, urlparse

__all__ = ["MetadataExtractor", "sanitize"]

_ILLEGAL_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE = re.compile(r"\s+")
_ISO8601_DURATION = re.compile(
    r"^PT(?:(?P<h>\d+(?:\.\d+)?)H)?(?:(?P<m>\d+(?:\.\d+)?)M)?"
    r"(?:(?P<s>\d+(?:\.\d+)?)S)?$"
)


def _parse_duration(value: str | None) -> int | None:
    """Parse a duration to whole seconds.

    Accepts plain seconds (``"95"``) and ISO-8601 durations (``"PT1M35S"``).
    Returns ``None`` when the value cannot be understood.
    """
    if value is None:
        return None
    value = str(value).strip()
    if not value:
        return None
    try:
        return int(float(value))
    except ValueError:
        pass
    match = _ISO8601_DURATION.match(value)
    if not match:
        return None
    hours = float(match.group("h") or 0)
    minutes = float(match.group("m") or 0)
    seconds = float(match.group("s") or 0)
    return int(hours * 3600 + minutes * 60 + seconds)


class _MetaHtmlParser(HTMLParser):
    """Collects <title> text and meta tag content from a page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self._in_title = False
        self.meta: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
            return
        if tag != "meta":
            return
        attrs_d = dict(attrs)
        key = attrs_d.get("property") or attrs_d.get("name") or attrs_d.get("itemprop")
        content = attrs_d.get("content")
        if key and content:
            self.meta.setdefault(key.strip().lower(), content.strip())

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)


class MetadataExtractor:
    """Extracts openly published media metadata from supplied HTML."""

    def extract_from_html(self, html: str) -> dict:
        """Return ``{title, description, thumbnail, duration}``.

        Sources, in priority order: Open Graph tags (``og:title``,
        ``og:description``, ``og:image``, ``og:video:duration``), then
        ``<title>``, ``description`` / ``twitter:description`` meta tags,
        and ``itemprop="duration"``. ``duration`` is whole seconds or
        ``None``; every missing field is ``None``.
        """
        if not html:
            return {"title": None, "description": None, "thumbnail": None, "duration": None}
        parser = _MetaHtmlParser()
        try:
            parser.feed(html)
        except Exception:
            pass
        meta = parser.meta
        title = meta.get("og:title")
        if not title:
            joined = "".join(parser.title_parts).strip()
            title = joined or None
        description = (
            meta.get("og:description")
            or meta.get("description")
            or meta.get("twitter:description")
        )
        thumbnail = meta.get("og:image") or meta.get("twitter:image")
        duration_raw = (
            meta.get("og:video:duration")
            or meta.get("video:duration")
            or meta.get("duration")
        )
        return {
            "title": title,
            "description": description,
            "thumbnail": thumbnail,
            "duration": _parse_duration(duration_raw),
        }

    def extract_title_from_url(self, url: str) -> str:
        """Derive a readable title from a URL's last path segment.

        Decodes percent-escapes, drops the file extension, and turns
        ``-`` / ``_`` separators into spaces. Returns ``""`` when the URL
        carries no usable segment.
        """
        if not url:
            return ""
        path = unquote(urlparse(url).path or "").rstrip("/")
        segment = path.rsplit("/", 1)[-1] if path else ""
        if "." in segment:
            segment = segment.rsplit(".", 1)[0]
        title = _WHITESPACE.sub(" ", segment.replace("-", " ").replace("_", " ")).strip()
        return title


def sanitize(title: str | None) -> str:
    """Make ``title`` safe as a file-name stem.

    Removes characters illegal in file names (``<>:"/\\|?*`` and control
    characters), collapses whitespace, trims trailing dots/spaces, and
    caps the length at 200 characters. Returns ``"untitled"`` for empty
    input.
    """
    if not title:
        return "untitled"
    cleaned = _ILLEGAL_FILENAME_CHARS.sub("", title)
    cleaned = _WHITESPACE.sub(" ", cleaned).strip().strip(".")
    if len(cleaned) > 200:
        cleaned = cleaned[:200].rstrip().rstrip(".")
    return cleaned or "untitled"
