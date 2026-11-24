"""Track selection for TServer Downloader.

OWN architecture, written from scratch. No IDM code is used or referenced.

Given the formats a resolver found, :class:`TrackSelector` picks the best
video rendition, a rendition by quality label, splits audio/video tracks,
and chooses an audio+video pair for merging. Pure selection logic: no
network access, no downloads, no side effects.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .resolver import MediaFormat

__all__ = ["MediaTrack", "TrackSelector"]


@dataclass
class MediaTrack:
    """A single media track (one audio or video rendition)."""

    url: str
    kind: str = "video"  # "video" or "audio"
    quality_label: str = "unknown"
    width: int | None = None
    height: int | None = None
    codec: str | None = None
    bitrate: int | None = None
    container: str | None = None


def _height_of(fmt: MediaFormat) -> int | None:
    if fmt.height:
        return fmt.height
    match = re.search(r"(\d+)\s*p", fmt.quality_label or "")
    return int(match.group(1)) if match else None


def _as_list(value) -> list[MediaFormat]:
    if value is None:
        return []
    if isinstance(value, MediaFormat):
        return [value]
    return list(value)


class TrackSelector:
    """Selects tracks from a list of :class:`MediaFormat` values."""

    @staticmethod
    def select_best(
        formats: list[MediaFormat],
        prefer_height: int = 1080,
        prefer_codec: str | None = None,
    ) -> MediaFormat | None:
        """Best video format, preferring ``prefer_height`` and ``prefer_codec``.

        Prefers the tallest rendition at or below ``prefer_height``; if none
        is at or below it, takes the smallest rendition above it. Ties break
        toward the higher bitrate. When ``prefer_codec`` is given, renditions
        whose codec contains it (case-insensitive) win; if none matches, the
        codec preference is ignored rather than returning nothing. With no
        video formats at all, falls back to the best audio format; with no
        formats, returns ``None``.
        """
        formats = list(formats or [])
        if not formats:
            return None
        video = [f for f in formats if not f.is_audio_only]
        pool = video or [f for f in formats if f.is_audio_only]
        if not pool:
            return None
        if prefer_codec and video:
            wanted = prefer_codec.lower()
            matched = [f for f in video if wanted in (f.codec or "").lower()]
            if matched:
                pool = matched

        def sort_key(fmt: MediaFormat) -> tuple[int, int, int]:
            height = _height_of(fmt) or 0
            if height <= prefer_height:
                return (0, prefer_height - height, -(fmt.bitrate or 0))
            return (1, height - prefer_height, -(fmt.bitrate or 0))

        return sorted(pool, key=sort_key)[0]

    @staticmethod
    def select_by_quality(
        formats: list[MediaFormat], quality_label: str
    ) -> MediaFormat | None:
        """Format matching ``quality_label`` (``"1080p"``, ``"720"``, ...).

        Exact label match first (case-insensitive), then the rendition whose
        height is numerically closest to the number in the label. Returns
        ``None`` when nothing matches and the label carries no number.
        """
        formats = list(formats or [])
        if not formats or not quality_label:
            return None
        wanted = quality_label.strip().lower()
        for fmt in formats:
            if (fmt.quality_label or "").strip().lower() == wanted:
                return fmt
        number = re.search(r"(\d+)", wanted)
        if not number:
            return None
        target = int(number.group(1))
        with_height = [f for f in formats if _height_of(f)]
        if not with_height:
            return None
        return min(
            with_height,
            key=lambda f: (abs((_height_of(f) or 0) - target), -(f.bitrate or 0)),
        )

    @staticmethod
    def group_audio_video(
        formats: list[MediaFormat],
    ) -> tuple[list[MediaFormat], list[MediaFormat]]:
        """Split formats into ``(video_formats, audio_formats)``."""
        formats = list(formats or [])
        video = [f for f in formats if not f.is_audio_only]
        audio = [f for f in formats if f.is_audio_only]
        return video, audio

    @staticmethod
    def choose_pair(
        video, audio
    ) -> tuple[MediaFormat | None, MediaFormat | None]:
        """Choose the (video, audio) pair to merge.

        ``video`` / ``audio`` may each be a single format, a list, or
        ``None``. The best video comes from :meth:`select_best`; the best
        audio is the one with the highest audio bitrate (falling back to
        overall bitrate). Either side may be ``None`` when unavailable.
        """
        video_list = _as_list(video)
        audio_list = _as_list(audio)
        best_video = TrackSelector.select_best(video_list) if video_list else None
        best_audio: MediaFormat | None = None
        if audio_list:
            best_audio = max(
                audio_list,
                key=lambda f: (f.audio_bitrate or f.bitrate or 0),
            )
        return best_video, best_audio
