"""Tests for the Phase 3 media engine: detector, resolver, tracks,
segments, assembler, metadata and site resolvers."""

import shutil
import subprocess
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tserver_downloader.media import (
    MediaAssembler,
    MediaDetector,
    MediaFormat,
    MediaTrack,
    MetadataExtractor,
    SegmentDownloader,
    SegmentDownloadError,
    AssemblerError,
    StreamResolver,
    TrackSelector,
)
from tserver_downloader.media.manifest import ManifestParser
from tserver_downloader.media.resolver import SiteResolverRegistry
from tserver_downloader.media.resolvers.generic import GenericResolver
from tserver_downloader.media.resolvers.youtube import (
    REASON_NOT_PERMITTED,
    PermittedYouTubeResolver,
)

FFMPEG = shutil.which("ffmpeg")
NEEDS_FFMPEG = pytest.mark.skipif(
    FFMPEG is None, reason="ffmpeg not installed; assembly tests need it"
)

HLS_MASTER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360,CODECS="avc1.4d401e,mp4a.40.2"
360p.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2800000,RESOLUTION=1920x1080,CODECS="avc1.640028,mp4a.40.2"
1080p.m3u8
"""

HLS_MEDIA_720 = """#EXTM3U
#EXT-X-TARGETDURATION:10
#EXTINF:10.0,
seg0.ts
#EXTINF:10.0,
seg1.ts
#EXT-X-ENDLIST
"""

DASH_MPD = """<?xml version="1.0" encoding="UTF-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" mediaPresentationDuration="PT20S" type="static">
  <Period id="p0" duration="PT20S">
    <AdaptationSet mimeType="video/mp4" codecs="avc1.640028">
      <SegmentTemplate timescale="1" duration="10" startNumber="1"
        media="video/$RepresentationID$/$Number$.m4s"
        initialization="video/$RepresentationID$/init.m4s"/>
      <Representation id="v1" bandwidth="800000" width="640" height="360" frameRate="30"/>
      <Representation id="v2" bandwidth="2800000" width="1920" height="1080" frameRate="30"/>
    </AdaptationSet>
    <AdaptationSet mimeType="audio/mp4" codecs="mp4a.40.2">
      <SegmentTemplate timescale="1" duration="10" startNumber="1"
        media="audio/$RepresentationID$/$Number$.m4s"
        initialization="audio/$RepresentationID$/init.m4s"/>
      <Representation id="a1" bandwidth="128000"/>
    </AdaptationSet>
  </Period>
</MPD>
"""


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):  # silence request logging
        pass


@pytest.fixture()
def http_server(tmp_path):
    """Local HTTP server serving segments, playlists and an MPD."""
    for i in range(3):
        (tmp_path / f"seg{i}.ts").write_bytes(f"FAKE-TS-SEGMENT-{i}-".encode() * 20)
    (tmp_path / "master.m3u8").write_text(HLS_MASTER)
    (tmp_path / "360p.m3u8").write_text(HLS_MEDIA_720)
    (tmp_path / "1080p.m3u8").write_text(HLS_MEDIA_720)
    (tmp_path / "manifest.mpd").write_text(DASH_MPD)
    handler = partial(_QuietHandler, directory=str(tmp_path))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", tmp_path
    finally:
        server.shutdown()
        server.server_close()


def _formats():
    return [
        MediaFormat(url="http://x/v360.mp4", quality_label="360p", width=640,
                    height=360, codec="avc1.4d401e", bitrate=800_000,
                    is_video_only=True),
        MediaFormat(url="http://x/v720.mp4", quality_label="720p", width=1280,
                    height=720, codec="vp9", bitrate=1_400_000,
                    is_video_only=True),
        MediaFormat(url="http://x/v1080.mp4", quality_label="1080p", width=1920,
                    height=1080, codec="avc1.640028", bitrate=2_800_000,
                    is_video_only=True),
        MediaFormat(url="http://x/v2160.mp4", quality_label="2160p", width=3840,
                    height=2160, codec="avc1.640033", bitrate=8_000_000,
                    is_video_only=True),
        MediaFormat(url="http://x/a128.m4a", quality_label="audio", codec="mp4a.40.2",
                    audio_bitrate=128_000, is_audio_only=True),
    ]


# ------------------------------------------------------------------ detector

HTML_SAMPLE = """<html><body>
<video src="/videos/intro.mp4" poster="/img.jpg"></video>
<video><source src="clip.webm"></video>
<audio src="song.mp3"></audio>
<a href="movie.mkv">download</a>
<a href="/page.html">not media</a>
<video src="https://cdn.example.com/v.mp4?token=abc123"></video>
<video src="/videos/intro.mp4"></video>
</body></html>"""


class TestDetector:
    def test_detect_from_html(self):
        found = MediaDetector().detect_from_html(HTML_SAMPLE, "http://example.com/page/")
        urls = [c.url for c in found]
        assert urls == [
            "http://example.com/videos/intro.mp4",
            "http://example.com/page/clip.webm",
            "http://example.com/page/song.mp3",
            "http://example.com/page/movie.mkv",
        ]
        kinds = {c.url: c.kind for c in found}
        assert kinds["http://example.com/page/song.mp3"] == "audio"
        assert kinds["http://example.com/videos/intro.mp4"] == "video"
        assert all(c.source == "html" for c in found)

    def test_protected_urls_skipped(self):
        found = MediaDetector().detect_from_html(
            '<video src="https://cdn.example.com/v.mp4?token=abc"></video>',
            "http://example.com/",
        )
        assert found == []

    def test_detect_from_request_log(self):
        log = [
            {"url": "http://x/a.mp4", "content_type": "video/mp4", "size": 1234},
            {"url": "http://x/b.m3u8", "content_type": "application/vnd.apple.mpegurl"},
            {"url": "http://x/c.mpd", "content_type": "application/dash+xml"},
            {"url": "http://x/d.mp3", "content_type": "audio/mpeg"},
            {"url": "http://x/e.ts", "content_type": "application/octet-stream"},
            {"url": "http://x/page.html", "content_type": "text/html"},
            {"url": "http://x/img.png", "content_type": "image/png"},
            {"url": "http://x/f.mp4?token=zzz", "content_type": "video/mp4"},
            {"url": "http://x/a.mp4", "content_type": "video/mp4", "size": 1234},
        ]
        found = MediaDetector().detect_from_requests(log)
        urls = [c.url for c in found]
        assert urls == [
            "http://x/a.mp4", "http://x/b.m3u8", "http://x/c.mpd",
            "http://x/d.mp3", "http://x/e.ts",
        ]
        by_url = {c.url: c for c in found}
        assert by_url["http://x/a.mp4"].size == 1234
        assert by_url["http://x/b.m3u8"].kind == "manifest"
        assert by_url["http://x/b.m3u8"].manifest_type == "hls"
        assert by_url["http://x/c.mpd"].manifest_type == "dash"
        assert by_url["http://x/d.mp3"].kind == "audio"
        assert all(c.source == "request_log" for c in found)

    def test_url_classifiers(self):
        detector = MediaDetector()
        assert detector.is_media_url("http://x/v.mp4")
        assert detector.is_media_url("http://x/a.MP3?x=1")
        assert not detector.is_media_url("http://x/page.html")
        assert detector.is_manifest_url("http://x/list.m3u8")
        assert detector.is_manifest_url("http://x/manifest.mpd")
        assert not detector.is_manifest_url("http://x/v.mp4")
        assert detector.is_protected_url("http://x/v.mp4?token=abc")
        assert detector.is_protected_url("http://x/drm-widevine/v.mp4")
        assert not detector.is_protected_url("http://x/v.mp4")

    def test_empty_inputs(self):
        detector = MediaDetector()
        assert detector.detect_from_html("", "http://x/") == []
        assert detector.detect_from_requests([]) == []
        assert detector.detect_from_requests([{"no_url": True}]) == []


# ------------------------------------------------------------------- tracks

class TestTrackSelector:
    def test_select_best_defaults_to_1080(self):
        best = TrackSelector.select_best(_formats())
        assert best is not None and best.quality_label == "1080p"

    def test_select_best_respects_prefer_height(self):
        assert TrackSelector.select_best(_formats(), prefer_height=720).quality_label == "720p"
        assert TrackSelector.select_best(_formats(), prefer_height=480).quality_label == "360p"
        assert TrackSelector.select_best(_formats(), prefer_height=5000).quality_label == "2160p"

    def test_select_best_prefers_codec(self):
        best = TrackSelector.select_best(_formats(), prefer_codec="vp9")
        assert best is not None and best.quality_label == "720p"
        # Unknown codec preference falls back instead of returning nothing.
        assert TrackSelector.select_best(_formats(), prefer_codec="av1") is not None

    def test_select_best_empty_and_audio_fallback(self):
        assert TrackSelector.select_best([]) is None
        audio_only = [f for f in _formats() if f.is_audio_only]
        assert TrackSelector.select_best(audio_only).is_audio_only

    def test_select_by_quality(self):
        formats = _formats()
        assert TrackSelector.select_by_quality(formats, "720p").height == 720
        assert TrackSelector.select_by_quality(formats, "1080").height == 1080
        assert TrackSelector.select_by_quality(formats, "720P").height == 720
        # No exact height: closest wins (480 -> 360p).
        assert TrackSelector.select_by_quality(formats, "480p").height == 360
        assert TrackSelector.select_by_quality(formats, "banana") is None
        assert TrackSelector.select_by_quality([], "720p") is None

    def test_group_audio_video(self):
        video, audio = TrackSelector.group_audio_video(_formats())
        assert len(video) == 4
        assert len(audio) == 1
        assert audio[0].is_audio_only

    def test_choose_pair(self):
        video, audio = TrackSelector.group_audio_video(_formats())
        best_video, best_audio = TrackSelector.choose_pair(video, audio)
        assert best_video.quality_label == "1080p"
        assert best_audio.is_audio_only
        # Missing audio side yields None, not an error.
        v_only, a_only = TrackSelector.choose_pair(video, [])
        assert v_only is not None and a_only is None

    def test_media_track_dataclass(self):
        track = MediaTrack(url="http://x/v.mp4", kind="video", quality_label="720p",
                           width=1280, height=720)
        assert track.height == 720 and track.kind == "video"


# ------------------------------------------------------------------ segments

class TestSegmentDownloader:
    def test_downloads_segments_in_order(self, http_server, tmp_path):
        base, _ = http_server
        urls = [f"{base}/seg{i}.ts" for i in range(3)]
        progress = []
        paths = SegmentDownloader().download_segments(
            urls, tmp_path / "segs", max_workers=2,
            on_progress=lambda done, total, url: progress.append((done, total)),
        )
        assert [p.name for p in paths] == ["seg_00001.ts", "seg_00002.ts", "seg_00003.ts"]
        assert [p.read_bytes() for p in paths] == [
            f"FAKE-TS-SEGMENT-{i}-".encode() * 20 for i in range(3)
        ]
        assert sorted(d for d, _ in progress) == [1, 2, 3]
        assert all(total == 3 for _, total in progress)

    def test_empty_list_returns_empty(self, tmp_path):
        assert SegmentDownloader().download_segments([], tmp_path) == []

    def test_missing_segment_raises(self, http_server, tmp_path):
        base, _ = http_server
        with pytest.raises(SegmentDownloadError):
            SegmentDownloader().download_segments(
                [f"{base}/seg0.ts", f"{base}/missing.ts"], tmp_path / "segs2"
            )


# ----------------------------------------------------------------- assembler

def _make_media(tmp_path, name, args):
    out = tmp_path / name
    subprocess.run(
        [FFMPEG, "-y", *args, str(out)],
        capture_output=True, check=True, timeout=60,
    )
    return out


@NEEDS_FFMPEG
class TestAssembler:
    def test_concat_segments(self, tmp_path):
        real = []
        for i in range(2):
            real.append(_make_media(tmp_path, f"real{i}.ts", [
                "-f", "lavfi", "-i", "testsrc=duration=0.5:size=128x72:rate=10",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-f", "mpegts",
            ]))
        out = tmp_path / "joined.ts"
        result = MediaAssembler().concat_segments(real, out)
        assert result == out
        assert out.is_file() and out.stat().st_size > 0
        assert MediaAssembler().is_ffmpeg_available() is True

    def test_merge_audio_video(self, tmp_path):
        video = _make_media(tmp_path, "v.mp4", [
            "-f", "lavfi", "-i", "testsrc=duration=1:size=128x72:rate=10",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
        ])
        audio = _make_media(tmp_path, "a.m4a", [
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-c:a", "aac",
        ])
        out = tmp_path / "merged.mp4"
        result = MediaAssembler().merge_audio_video(video, audio, out)
        assert result == out and out.is_file() and out.stat().st_size > 0

    def test_missing_ffmpeg_raises_clear_error(self, tmp_path):
        assembler = MediaAssembler(ffmpeg_path="/nonexistent/ffmpeg")
        assert assembler.is_ffmpeg_available() is False
        with pytest.raises(AssemblerError, match="ffmpeg not found"):
            assembler.concat_segments([tmp_path / "x.ts"], tmp_path / "o.ts")

    def test_missing_input_raises(self, tmp_path):
        with pytest.raises(AssemblerError, match="not found"):
            MediaAssembler().merge_audio_video(
                tmp_path / "nope.mp4", tmp_path / "nope.m4a", tmp_path / "o.mp4"
            )


# ------------------------------------------------------------------ metadata

class TestMetadata:
    HTML = """<html><head>
<title>Fallback Title</title>
<meta property="og:title" content="My Great Video">
<meta property="og:description" content="A description here.">
<meta property="og:image" content="http://x/thumb.jpg">
<meta property="og:video:duration" content="PT1M35S">
</head><body></body></html>"""

    def test_extract_from_html(self):
        meta = MetadataExtractor().extract_from_html(self.HTML)
        assert meta["title"] == "My Great Video"
        assert meta["description"] == "A description here."
        assert meta["thumbnail"] == "http://x/thumb.jpg"
        assert meta["duration"] == 95

    def test_title_falls_back_to_title_tag(self):
        html = "<html><head><title>Only Title</title></head></html>"
        assert MetadataExtractor().extract_from_html(html)["title"] == "Only Title"

    def test_missing_fields_are_none(self):
        meta = MetadataExtractor().extract_from_html("<html></html>")
        assert meta == {"title": None, "description": None,
                        "thumbnail": None, "duration": None}

    def test_title_from_url_and_sanitize(self):
        extractor = MetadataExtractor()
        assert extractor.extract_title_from_url(
            "http://x/videos/my-great_video%20clip.mp4") == "my great video clip"
        assert extractor.extract_title_from_url("http://x/") == ""
        from tserver_downloader.media.metadata import sanitize
        assert sanitize('A/B: C*D? "E"') == "AB CD E"
        assert sanitize("") == "untitled"
        assert sanitize(None) == "untitled"
        assert len(sanitize("x" * 500)) == 200


# ------------------------------------------------------------------ resolver

class TestStreamResolver:
    def test_direct_file_needs_no_network(self):
        formats = StreamResolver().resolve("https://cdn.example.com/video.mp4")
        assert len(formats) == 1
        assert formats[0].container == "mp4"
        assert formats[0].quality_label == "direct"
        assert formats[0].is_audio_only is False

    def test_direct_audio_flagged(self):
        formats = StreamResolver().resolve("https://cdn.example.com/song.mp3")
        assert formats[0].is_audio_only is True

    def test_page_html_candidates_resolved(self):
        html = '<video src="http://x/clip.webm"></video><a href="http://x/a.mp3">a</a>'
        formats = StreamResolver().resolve("http://x/page.html", html=html)
        assert {f.url for f in formats} == {"http://x/clip.webm", "http://x/a.mp3"}

    def test_hls_master_resolved_from_server(self, http_server):
        base, _ = http_server
        formats = StreamResolver().resolve(f"{base}/master.m3u8")
        assert sorted(f.quality_label for f in formats) == ["1080p", "360p"]
        assert all(f.container == "m3u8" for f in formats)
        assert all(f.url.startswith(base) for f in formats)

    def test_dash_resolved_from_server(self, http_server):
        base, _ = http_server
        formats = StreamResolver().resolve(f"{base}/manifest.mpd")
        assert len(formats) == 3
        video = [f for f in formats if f.is_video_only]
        audio = [f for f in formats if f.is_audio_only]
        assert len(video) == 2 and len(audio) == 1
        assert {f.quality_label for f in video} == {"360p", "1080p"}
        assert audio[0].audio_bitrate == 128000

    def test_unreachable_manifest_returns_empty_not_raise(self):
        assert StreamResolver().resolve("http://127.0.0.1:9/master.m3u8") == []
        assert StreamResolver().resolve("") == []
        assert StreamResolver().resolve("http://127.0.0.1:9/page") == []

    def test_manifest_parser_available(self):
        parser = ManifestParser()
        manifest = parser.parse_hls(HLS_MASTER, "http://x/master.m3u8")
        assert len(manifest.variants) == 2


class TestRegistryIsolation:
    def test_crashing_resolver_returns_empty(self):
        def boom(url, html=None):
            raise RuntimeError("boom")

        SiteResolverRegistry.register("crashsite.test", boom)
        assert SiteResolverRegistry.get_resolver("https://crashsite.test/watch") is boom
        assert SiteResolverRegistry.resolve("https://crashsite.test/watch") == []
        assert "crashsite.test" in SiteResolverRegistry.list_resolvers()

    def test_unknown_url_gets_generic_resolver(self):
        resolver = SiteResolverRegistry.get_resolver("https://example.com/v.mp4")
        assert resolver("https://example.com/v.mp4")[0].container == "mp4"

    def test_generic_resolver_class(self):
        assert GenericResolver().resolve("https://example.com/v.mp4")[0].url.endswith("v.mp4")


class TestYouTubeResolver:
    YT_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    def test_no_permitted_media_returns_empty(self):
        html = ("<html><head><title>Some Video - YouTube</title>"
                '<meta property="og:title" content="Some Video"></head></html>')
        resolver = PermittedYouTubeResolver()
        assert resolver.resolve(self.YT_URL, html=html) == []
        assert resolver.last_reason == REASON_NOT_PERMITTED

    def test_no_html_returns_empty_without_raising(self):
        resolver = PermittedYouTubeResolver()
        assert resolver.resolve(self.YT_URL, html=None) == []
        assert resolver.last_reason == REASON_NOT_PERMITTED

    def test_non_youtube_url_returns_empty(self):
        resolver = PermittedYouTubeResolver()
        assert resolver.resolve("https://example.com/v.mp4", html="<html></html>") == []
        assert resolver.last_reason == REASON_NOT_PERMITTED

    def test_signed_urls_never_followed(self):
        html = ("<html><body>"
                "https://r1---sn.example.googlevideo.com/videoplayback?"
                "expire=1&amp;signature=ABCDEF&amp;mime=video%2Fmp4 "
                "https://cdn.example.com/signed.m3u8?signature=ABCDEF"
                "</body></html>")
        resolver = PermittedYouTubeResolver()
        assert resolver.resolve(self.YT_URL, html=html) == []
        assert resolver.last_reason == REASON_NOT_PERMITTED

    def test_permitted_direct_og_video_url_resolved(self):
        html = ('<html><head><meta property="og:video:url" '
                'content="https://cdn.example.com/clip.mp4"></head></html>')
        formats = PermittedYouTubeResolver().resolve(self.YT_URL, html=html)
        assert len(formats) == 1
        assert formats[0].url == "https://cdn.example.com/clip.mp4"
