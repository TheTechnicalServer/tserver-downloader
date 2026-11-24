"""Media detection engine for TServer Downloader (Phase 3).

OWN architecture, written from scratch. No IDM code is used or referenced.

Detects, resolves, selects, downloads and assembles openly exposed media:
direct files, HLS/DASH manifests, and the metadata pages publish about
them. Only permitted, unencrypted content is handled -- this package never
bypasses DRM, signatures, logins, age-gates or paywalls, and it never
imports the core engine (media modules use only the standard library plus
``requests``).
"""

from __future__ import annotations

from .assembler import AssemblerError, MediaAssembler
from .detector import MediaCandidate, MediaDetector
from .manifest import (
    DashManifest,
    HlsManifest,
    ManifestError,
    ManifestParser,
    Representation,
    Variant,
)
from .metadata import MetadataExtractor, sanitize
from .resolver import MediaFormat, SiteResolverRegistry, StreamResolver
from .segments import SegmentDownloadError, SegmentDownloader
from .tracks import MediaTrack, TrackSelector

__all__ = [
    "MediaDetector",
    "ManifestParser",
    "StreamResolver",
    "SegmentDownloader",
    "TrackSelector",
    "MediaAssembler",
    "MetadataExtractor",
    "MediaTrack",
    "MediaFormat",
    # Supporting types / errors, exported for convenience.
    "MediaCandidate",
    "ManifestError",
    "AssemblerError",
    "SegmentDownloadError",
    "SiteResolverRegistry",
    "HlsManifest",
    "DashManifest",
    "Variant",
    "Representation",
    "sanitize",
]
