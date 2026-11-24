"""Generic site resolver for TServer Downloader.

OWN architecture, written from scratch. No IDM code is used or referenced.

The generic resolver handles any site the same way: direct media files and
openly exposed HLS/DASH manifests, via :class:`StreamResolver`. It adds no
site-specific tricks, no signature handling and no login flow. It is the
fallback every URL gets when no site-specific resolver matches.
"""

from __future__ import annotations

from ..resolver import MediaFormat, StreamResolver

__all__ = ["GenericResolver", "resolve"]


class GenericResolver:
    """Resolves direct media files and HLS/DASH manifests on any site."""

    def __init__(self, timeout: int = 15) -> None:
        self._inner = StreamResolver(timeout=timeout)

    def resolve(self, url: str, html: str | None = None) -> list[MediaFormat]:
        """Return downloadable formats for ``url``; ``[]`` on any failure."""
        return self._inner.resolve(url, html=html)


def resolve(url: str, html: str | None = None) -> list[MediaFormat]:
    """Module-level shortcut for :meth:`GenericResolver.resolve`."""
    return GenericResolver().resolve(url, html=html)
