"""HLS (.m3u8) and DASH (.mpd) manifest parsing for TServer Downloader.

OWN architecture, written from scratch using only the standard library
(``xml.etree`` for DASH). No IDM code is used or referenced.

Only openly exposed, unencrypted manifests are parsed. An HLS playlist that
declares encryption (``#EXT-X-KEY`` with a method other than ``NONE``) is
rejected with :class:`ManifestError` -- encrypted content is out of scope and
is never decrypted here. All URLs in a manifest are resolved against the
manifest's own URL, so relative segment/variant references become absolute.

Parsing never crashes the caller: malformed input raises
:class:`ManifestError` with a clear message, and callers that must not fail
(for example the stream resolver) convert that into an empty result.
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from urllib.parse import urljoin

__all__ = [
    "ManifestError",
    "Variant",
    "HlsManifest",
    "Representation",
    "DashManifest",
    "ManifestParser",
]


class ManifestError(ValueError):
    """Raised when a manifest is malformed, encrypted or not a manifest."""


@dataclass
class Variant:
    """One rendition listed in an HLS master playlist."""

    bandwidth: int | None = None
    resolution: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    codecs: str | None = None
    uri: str = ""


@dataclass
class HlsManifest:
    """Parsed HLS playlist.

    Master playlists carry ``variants`` (and ``media_playlists`` is the list
    of their URIs); media playlists carry ``segments``. ``is_master`` is
    True exactly when variants were found.
    """

    variants: list[Variant] = field(default_factory=list)
    segments: list[str] = field(default_factory=list)
    is_master: bool = False
    media_playlists: list[str] = field(default_factory=list)


@dataclass
class Representation:
    """One DASH representation (a single video or audio rendition)."""

    bandwidth: int | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    codecs: str | None = None
    mime: str | None = None
    segment_template: str | None = None
    base_url: str | None = None
    segments: list[str] = field(default_factory=list)


@dataclass
class DashManifest:
    """Parsed DASH MPD.

    ``periods`` is a list of ``{"id", "duration", "start"}`` dicts.
    ``adaptations`` is the flat list of all representations across all
    periods and adaptation sets.
    """

    periods: list[dict] = field(default_factory=list)
    adaptations: list[Representation] = field(default_factory=list)


_ATTR_RE = re.compile(r'''([A-Z0-9-]+)=("(?:[^"\\]|\\.)*"|[^,]*)''')
_DURATION_RE = re.compile(
    r"^PT(?:(?P<h>\d+(?:\.\d+)?)H)?(?:(?P<m>\d+(?:\.\d+)?)M)?"
    r"(?:(?P<s>\d+(?:\.\d+)?)S)?$"
)


def _parse_attr_list(text: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for match in _ATTR_RE.finditer(text or ""):
        value = match.group(2)
        if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        attrs[match.group(1)] = value
    return attrs


def _parse_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def _parse_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        if "/" in value:
            num, den = value.split("/", 1)
            return float(num) / float(den)
        return float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _parse_iso8601_duration(value: str | None) -> float | None:
    """Parse an ISO-8601 duration such as PT1H2M30.5S into seconds."""
    if not value:
        return None
    match = _DURATION_RE.match(value.strip())
    if not match:
        return None
    hours = float(match.group("h") or 0)
    minutes = float(match.group("m") or 0)
    seconds = float(match.group("s") or 0)
    return hours * 3600 + minutes * 60 + seconds


def _local_name(tag: str) -> str:
    """Strip any XML namespace from an element tag."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _children(elem: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in elem if _local_name(child.tag) == name]


def _first_child(elem: ET.Element, name: str) -> ET.Element | None:
    for child in elem:
        if _local_name(child.tag) == name:
            return child
    return None


def _base_url_of(elem: ET.Element, inherited: str) -> str:
    base_el = _first_child(elem, "BaseURL")
    if base_el is not None and (base_el.text or "").strip():
        return urljoin(inherited, base_el.text.strip())
    return inherited


def _substitute_template(template: str, values: dict[str, str]) -> str:
    """Apply DASH SegmentTemplate $Variable$ substitution (``$$`` -> ``$``)."""
    out = template.replace("$$", "\x00")
    for key, val in values.items():
        out = out.replace(f"${key}$", val)
    # $Number%03d$ style width formatting, e.g. $Number%05d$.
    def _fmt(match: re.Match) -> str:
        var, fmt = match.group(1), match.group(2)
        if var not in values:
            return match.group(0)
        try:
            return fmt % int(values[var])
        except (TypeError, ValueError):
            return values[var]

    out = re.sub(r"\$([A-Za-z]+)(%0\d+d)\$", _fmt, out)
    return out.replace("\x00", "$")


class ManifestParser:
    """Parses HLS and DASH manifests. Stateless; safe to reuse."""

    # ------------------------------------------------------------------ HLS

    def parse_hls(self, text: str, base_url: str) -> HlsManifest:
        """Parse an HLS playlist.

        Raises :class:`ManifestError` on empty / non-HLS input and on
        encrypted playlists (``#EXT-X-KEY`` with METHOD != NONE).
        """
        if not text or not text.strip():
            raise ManifestError("empty HLS playlist")
        lines = [line.strip() for line in text.splitlines()]
        if not lines or lines[0] != "#EXTM3U":
            raise ManifestError("not an HLS playlist: missing #EXTM3U header")

        variants: list[Variant] = []
        segments: list[str] = []
        pending: Variant | None = None

        for line in lines[1:]:
            if not line:
                continue
            if line.startswith("#EXT-X-KEY"):
                attrs = _parse_attr_list(line.split(":", 1)[1] if ":" in line else "")
                method = attrs.get("METHOD", "NONE").upper()
                if method != "NONE":
                    raise ManifestError(
                        f"encrypted HLS manifest (EXT-X-KEY METHOD={method}) "
                        "is not supported; only unencrypted content is handled"
                    )
                continue
            if line.startswith("#EXT-X-STREAM-INF:"):
                attrs = _parse_attr_list(line.split(":", 1)[1])
                width = height = None
                resolution = attrs.get("RESOLUTION")
                if resolution and "x" in resolution:
                    parts = resolution.lower().split("x", 1)
                    width, height = _parse_int(parts[0]), _parse_int(parts[1])
                pending = Variant(
                    bandwidth=_parse_int(attrs.get("BANDWIDTH")),
                    resolution=resolution,
                    width=width,
                    height=height,
                    fps=_parse_float(attrs.get("FRAME-RATE")),
                    codecs=attrs.get("CODECS"),
                )
                continue
            if line.startswith("#"):
                continue
            uri = urljoin(base_url, line)
            if pending is not None:
                pending.uri = uri
                variants.append(pending)
                pending = None
            else:
                segments.append(uri)

        return HlsManifest(
            variants=variants,
            segments=segments,
            is_master=bool(variants),
            media_playlists=[v.uri for v in variants],
        )

    # ----------------------------------------------------------------- DASH

    def parse_dash(self, xml_text: str, base_url: str) -> DashManifest:
        """Parse a DASH MPD document.

        Raises :class:`ManifestError` on malformed XML or when the root
        element is not ``MPD``.
        """
        if not xml_text or not xml_text.strip():
            raise ManifestError("empty DASH manifest")
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            raise ManifestError(f"malformed DASH manifest XML: {exc}") from exc
        if _local_name(root.tag) != "MPD":
            raise ManifestError(
                f"not a DASH manifest: root element is '{_local_name(root.tag)}', "
                "expected 'MPD'"
            )

        total_duration = _parse_iso8601_duration(
            root.get("mediaPresentationDuration")
        )
        mpd_base = _base_url_of(root, base_url)
        periods: list[dict] = []
        adaptations: list[Representation] = []

        period_elems = _children(root, "Period")
        if not period_elems:
            raise ManifestError("DASH manifest contains no Period elements")

        for period in period_elems:
            period_base = _base_url_of(period, mpd_base)
            period_duration = _parse_iso8601_duration(period.get("duration"))
            periods.append(
                {
                    "id": period.get("id"),
                    "duration": period_duration,
                    "start": period.get("start"),
                }
            )
            for adapt in _children(period, "AdaptationSet"):
                mime = adapt.get("mimeType")
                adapt_codecs = adapt.get("codecs")
                adapt_base = _base_url_of(adapt, period_base)
                adapt_template = _first_child(adapt, "SegmentTemplate")
                for rep in _children(adapt, "Representation"):
                    adaptations.append(
                        self._parse_representation(
                            rep,
                            mime_type=mime,
                            codecs=adapt_codecs,
                            base=adapt_base,
                            template=adapt_template,
                            period_duration=period_duration or total_duration,
                        )
                    )

        return DashManifest(periods=periods, adaptations=adaptations)

    def _parse_representation(
        self,
        rep: ET.Element,
        mime_type: str | None,
        codecs: str | None,
        base: str,
        template: ET.Element | None,
        period_duration: float | None,
    ) -> Representation:
        rep_base = _base_url_of(rep, base)
        rep_template = _first_child(rep, "SegmentTemplate") or template
        rep_id = rep.get("id") or ""
        bandwidth = _parse_int(rep.get("bandwidth"))
        rep_codecs = rep.get("codecs") or codecs
        mime = rep.get("mimeType") or mime_type

        segment_template: str | None = None
        segments: list[str] = []
        if rep_template is not None:
            media = rep_template.get("media")
            if media:
                segment_template = urljoin(rep_base, media)
            segments = self._segments_from_template(
                rep_template, rep_base, rep_id, bandwidth, period_duration
            )

        if not segments:
            seg_list = _first_child(rep, "SegmentList")
            if seg_list is not None:
                for seg_url in _children(seg_list, "SegmentURL"):
                    media = seg_url.get("media")
                    if media:
                        segments.append(urljoin(rep_base, media))
                init = _first_child(seg_list, "Initialization")
                if init is not None and init.get("sourceURL"):
                    segments.insert(
                        0, urljoin(rep_base, init.get("sourceURL") or "")
                    )

        if not segments and rep_base and rep_base != base:
            # Plain BaseURL-only representation: the file itself is the media.
            segments = [rep_base]

        return Representation(
            bandwidth=bandwidth,
            width=_parse_int(rep.get("width")),
            height=_parse_int(rep.get("height")),
            fps=_parse_float(rep.get("frameRate")),
            codecs=rep_codecs,
            mime=mime,
            segment_template=segment_template,
            base_url=rep_base,
            segments=segments,
        )

    def _segments_from_template(
        self,
        template: ET.Element,
        base: str,
        rep_id: str,
        bandwidth: int | None,
        period_duration: float | None,
    ) -> list[str]:
        media = template.get("media")
        if not media:
            return []
        timescale = _parse_int(template.get("timescale")) or 1
        start_number = _parse_int(template.get("startNumber"))
        if start_number is None:
            start_number = 1
        duration = _parse_int(template.get("duration"))
        timeline = _first_child(template, "SegmentTimeline")

        numbers: list[int] = []
        times: list[int] = []
        if timeline is not None:
            current_time: int | None = None
            current_number = start_number
            for s_elem in _children(timeline, "S"):
                seg_d = _parse_int(s_elem.get("d"))
                if seg_d is None:
                    continue
                t_attr = _parse_int(s_elem.get("t"))
                if t_attr is not None:
                    current_time = t_attr
                repeat = _parse_int(s_elem.get("r")) or 0
                for _ in range(repeat + 1):
                    numbers.append(current_number)
                    times.append(current_time if current_time is not None else 0)
                    current_number += 1
                    if current_time is not None:
                        current_time += seg_d
        elif duration and period_duration:
            count = math.ceil((period_duration * timescale) / duration)
            numbers = [start_number + i for i in range(count)]
            times = [i * duration for i in range(count)]
        else:
            return []

        segments: list[str] = []
        init = template.get("initialization")
        if init:
            segments.append(
                urljoin(
                    base,
                    _substitute_template(
                        init,
                        {
                            "RepresentationID": rep_id,
                            "Bandwidth": str(bandwidth or ""),
                        },
                    ),
                )
            )
        for number, seg_time in zip(numbers, times):
            values = {
                "RepresentationID": rep_id,
                "Bandwidth": str(bandwidth or ""),
                "Number": str(number),
                "Time": str(seg_time),
            }
            segments.append(
                urljoin(base, _substitute_template(media, values))
            )
        return segments
