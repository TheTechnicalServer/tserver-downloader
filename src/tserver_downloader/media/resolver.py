"""Stream resolution for TServer Downloader.

OWN architecture, written from scratch. No IDM code is used or referenced.

:class:`StreamResolver` turns a URL (optionally plus the page HTML it came
from) into a list of :class:`MediaFormat` values describing the downloadable
streams: direct media files, HLS variants, or DASH representations. It
delegates manifest parsing to :class:`~.manifest.ManifestParser` and media
detection to :class:`~.detector.MediaDetector`, and never imports the core
engine (no circular imports): media modules use only the standard library
plus ``requests``.

Safety: only openly exposed, unencrypted streams are resolved. Protected
(token / signature / DRM) URLs are skipped, encrypted manifests are rejected
by the parser, and nothing here bypasses signatures, DRM, age-gates or
logins.

Isolation: :meth:`StreamResolver.resolve` never raises -- any failure
(network error, malformed manifest, unexpected page shape) yields ``[]``.
Site-specific resolvers registered in :class:`SiteResolverRegistry` are
isolated the same way: a crashing resolver returns ``[]`` instead of
breaking the caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlparse

import requests

from .detector import MediaDetector
from .manifest import ManifestParser

__all__ = [
    "MediaFormat",
    "StreamResolver",
    "SiteResolverRegistry",
    "register_resolver",
    "get_resolver",
]

_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 TServerDownloader/1.0"
)

_AUDIO_EXTENSIONS = {".mp3", ".m4a", ".aac", ".ogg", ".oga", ".wav", ".flac", ".opus"}

ResolverFn = Callable[..., list["MediaFormat"]]


@dataclass
class MediaFormat:
    """One downloadable stream format.

    ``is_audio_only`` / ``is_video_only`` describe single-track streams
    (typical for DASH/HLS adaptive renditions); a progressive direct file
    has both False. ``estimated_size`` is in bytes when it can be derived,
    else ``None``.
    """

    url: str
    quality_label: str = "unknown"
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    codec: str | None = None
    container: str | None = None
    bitrate: int | None = None
    audio_bitrate: int | None = None
    estimated_size: int | None = None
    is_audio_only: bool = False
    is_video_only: bool = False


def _container_from_url(url: str) -> str | None:
    path = urlparse(url).path
    if "." in path.rsplit("/", 1)[-1]:
        return path.rsplit(".", 1)[-1].lower()
    return None


def _quality_label(height: int | None, bandwidth: int | None) -> str:
    if height:
        return f"{height}p"
    if bandwidth:
        return f"{bandwidth // 1000}k"
    return "unknown"


class StreamResolver:
    """Resolves a URL (and optional page HTML) into downloadable formats."""

    def __init__(self, timeout: int = 15) -> None:
        self.timeout = timeout
        self._detector = MediaDetector()
        self._parser = ManifestParser()
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": _DEFAULT_USER_AGENT})

    # ------------------------------------------------------------- public

    def resolve(self, url: str, html: str | None = None) -> list[MediaFormat]:
        """Return downloadable formats for ``url``; ``[]`` on any failure.

        Never raises. ``html``, when given, is treated as the page the URL
        came from: if ``url`` is a plain page (not itself media/manifest),
        candidates detected in the HTML are resolved instead.
        """
        try:
            return self._resolve_inner(url, html)
        except Exception:
            return []

    # ------------------------------------------------------------ internal

    def _resolve_inner(self, url: str, html: str | None) -> list[MediaFormat]:
        if not url or not isinstance(url, str):
            return []
        if self._detector.is_protected_url(url):
            return []
        if self._detector.is_manifest_url(url):
            return self._resolve_manifest(url)
        if self._detector.is_media_url(url):
            return [self._direct_format(url)]
        if html:
            formats: list[MediaFormat] = []
            for cand in self._detector.detect_from_html(html, url):
                if cand.kind == "manifest":
                    formats.extend(self._resolve_manifest(cand.url))
                else:
                    formats.append(self._direct_format(cand.url))
            return formats
        return []

    def _fetch_text(self, url: str) -> str:
        response = self._session.get(url, timeout=self.timeout)
        response.raise_for_status()
        response.encoding = response.encoding or "utf-8"
        return response.text

    def _resolve_manifest(self, url: str) -> list[MediaFormat]:
        text = self._fetch_text(url)
        if url.lower().split("?", 1)[0].endswith(".m3u8"):
            manifest = self._parser.parse_hls(text, url)
            return self._formats_from_hls(manifest, url)
        manifest_dash = self._parser.parse_dash(text, url)
        return self._formats_from_dash(manifest_dash, url)

    def _formats_from_hls(self, manifest, manifest_url: str) -> list[MediaFormat]:
        formats: list[MediaFormat] = []
        if manifest.is_master:
            for variant in manifest.variants:
                formats.append(
                    MediaFormat(
                        url=variant.uri,
                        quality_label=_quality_label(variant.height, variant.bandwidth),
                        width=variant.width,
                        height=variant.height,
                        fps=variant.fps,
                        codec=variant.codecs,
                        container="m3u8",
                        bitrate=variant.bandwidth,
                        is_video_only=False,
                    )
                )
            return formats
        # Media playlist: the playlist itself is the single stream.
        container = None
        if manifest.segments:
            container = _container_from_url(manifest.segments[0])
        formats.append(
            MediaFormat(
                url=manifest_url,
                quality_label="hls",
                container=container or "ts",
            )
        )
        return formats

    def _formats_from_dash(self, manifest, manifest_url: str) -> list[MediaFormat]:
        formats: list[MediaFormat] = []
        for rep in manifest.adaptations:
            is_audio = bool(rep.mime and rep.mime.startswith("audio/"))
            stream_url = rep.segments[0] if rep.segments else (rep.base_url or manifest_url)
            formats.append(
                MediaFormat(
                    url=stream_url,
                    quality_label=_quality_label(rep.height, rep.bandwidth),
                    width=rep.width,
                    height=rep.height,
                    fps=rep.fps,
                    codec=rep.codecs,
                    container="mpd",
                    bitrate=None if is_audio else rep.bandwidth,
                    audio_bitrate=rep.bandwidth if is_audio else None,
                    is_audio_only=is_audio,
                    is_video_only=not is_audio,
                )
            )
        return formats

    def _direct_format(self, url: str) -> MediaFormat:
        container = _container_from_url(url)
        is_audio = (container or "") in {e.lstrip(".") for e in _AUDIO_EXTENSIONS}
        return MediaFormat(
            url=url,
            quality_label="direct",
            container=container,
            is_audio_only=is_audio,
            is_video_only=False,
        )


def _generic_resolver(url: str, html: str | None = None) -> list[MediaFormat]:
    """Default resolver: direct files + HLS/DASH manifests, nothing else."""
    return StreamResolver().resolve(url, html=html)


class SiteResolverRegistry:
    """Registry of site-specific resolvers, isolated from each other.

    Resolvers are matched by name: a resolver registered under ``"youtube"``
    is used for URLs containing ``"youtube"``. Anything unmatched falls back
    to the generic resolver. Every resolver call is wrapped in ``try/except``
    so a faulty site resolver can never raise into the caller.
    """

    _resolvers: dict[str, ResolverFn] = {}

    @classmethod
    def register(cls, name: str, resolver_fn: ResolverFn) -> None:
        cls._resolvers[name.lower()] = resolver_fn

    @classmethod
    def get_resolver(cls, url: str) -> ResolverFn:
        lowered = (url or "").lower()
        for name, resolver_fn in cls._resolvers.items():
            if name in lowered:
                return resolver_fn
        return _generic_resolver

    @classmethod
    def list_resolvers(cls) -> list[str]:
        return sorted(cls._resolvers)

    @classmethod
    def resolve(cls, url: str, html: str | None = None) -> list[MediaFormat]:
        """Resolve through the matching site resolver; ``[]`` on any failure."""
        try:
            return list(cls.get_resolver(url)(url, html=html) or [])
        except Exception:
            return []


def register_resolver(name: str, resolver_fn: ResolverFn) -> None:
    """Module-level shortcut for :meth:`SiteResolverRegistry.register`."""
    SiteResolverRegistry.register(name, resolver_fn)


def get_resolver(url: str) -> ResolverFn:
    """Module-level shortcut for :meth:`SiteResolverRegistry.get_resolver`."""
    return SiteResolverRegistry.get_resolver(url)
