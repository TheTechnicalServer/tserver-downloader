"""Tests for HLS / DASH manifest parsing (Phase 3 media engine)."""

import pytest

from tserver_downloader.media.manifest import (
    DashManifest,
    HlsManifest,
    ManifestError,
    ManifestParser,
)

HLS_MASTER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360,FRAME-RATE=30,CODECS="avc1.4d401e,mp4a.40.2"
360p.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1400000,RESOLUTION=1280x720,FRAME-RATE=30,CODECS="avc1.4d401f,mp4a.40.2"
720p.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2800000,RESOLUTION=1920x1080,FRAME-RATE=30,CODECS="avc1.640028,mp4a.40.2"
1080p.m3u8
"""

HLS_MEDIA = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXTINF:10.0,
seg0.ts
#EXTINF:10.0,
seg1.ts
#EXTINF:10.0,
seg2.ts
#EXTINF:10.0,
seg3.ts
#EXTINF:10.0,
seg4.ts
#EXT-X-ENDLIST
"""

HLS_ENCRYPTED = """#EXTM3U
#EXT-X-KEY:METHOD=AES-128,URI="key.bin"
#EXTINF:10.0,
seg0.ts
"""

DASH_MPD = """<?xml version="1.0" encoding="UTF-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" mediaPresentationDuration="PT30S"
     minBufferTime="PT1.5S" type="static">
  <Period id="p0" duration="PT30S">
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

DASH_TIMELINE_MPD = """<?xml version="1.0" encoding="UTF-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" mediaPresentationDuration="PT15S" type="static">
  <Period id="p0" duration="PT15S">
    <AdaptationSet mimeType="video/mp4">
      <SegmentTemplate timescale="1" startNumber="0"
        media="v/$Number$.m4s" initialization="v/init.m4s">
        <SegmentTimeline>
          <S t="0" d="5" r="1"/>
          <S d="5"/>
        </SegmentTimeline>
      </SegmentTemplate>
      <Representation id="v1" bandwidth="1000000" width="1280" height="720" frameRate="30000/1001"/>
    </AdaptationSet>
  </Period>
</MPD>
"""


@pytest.fixture()
def parser():
    return ManifestParser()


class TestHlsMaster:
    def test_three_variants_parsed(self, parser):
        manifest = parser.parse_hls(HLS_MASTER, "http://example.com/hls/master.m3u8")
        assert isinstance(manifest, HlsManifest)
        assert manifest.is_master is True
        assert len(manifest.variants) == 3

        v360, v720, v1080 = manifest.variants
        assert (v360.width, v360.height) == (640, 360)
        assert v360.bandwidth == 800000
        assert v360.fps == 30.0
        assert v360.codecs == "avc1.4d401e,mp4a.40.2"
        assert v360.uri == "http://example.com/hls/360p.m3u8"
        assert (v720.width, v720.height) == (1280, 720)
        assert v720.bandwidth == 1400000
        assert v720.uri == "http://example.com/hls/720p.m3u8"
        assert (v1080.width, v1080.height) == (1920, 1080)
        assert v1080.bandwidth == 2800000
        assert v1080.uri == "http://example.com/hls/1080p.m3u8"

    def test_media_playlists_listed(self, parser):
        manifest = parser.parse_hls(HLS_MASTER, "http://example.com/hls/master.m3u8")
        assert manifest.media_playlists == [v.uri for v in manifest.variants]
        assert len(manifest.media_playlists) == 3

    def test_relative_variant_urls_resolved(self, parser):
        manifest = parser.parse_hls(HLS_MASTER, "https://cdn.example.com/a/b/master.m3u8")
        assert all(u.startswith("https://cdn.example.com/a/b/") for u in manifest.media_playlists)


class TestHlsMediaPlaylist:
    def test_segments_listed_in_order(self, parser):
        manifest = parser.parse_hls(HLS_MEDIA, "http://example.com/hls/720p.m3u8")
        assert manifest.is_master is False
        assert manifest.variants == []
        assert manifest.segments == [
            f"http://example.com/hls/seg{i}.ts" for i in range(5)
        ]

    def test_encrypted_playlist_rejected(self, parser):
        with pytest.raises(ManifestError, match="encrypted"):
            parser.parse_hls(HLS_ENCRYPTED, "http://example.com/hls/enc.m3u8")

    def test_malformed_input_raises_manifest_error(self, parser):
        with pytest.raises(ManifestError):
            parser.parse_hls("", "http://example.com/hls/x.m3u8")
        with pytest.raises(ManifestError):
            parser.parse_hls("not a playlist at all", "http://example.com/hls/x.m3u8")
        with pytest.raises(ManifestError):
            parser.parse_hls("#EXTINF:10.0,\nseg0.ts\n", "http://example.com/hls/x.m3u8")


class TestDash:
    def test_two_video_one_audio_representation(self, parser):
        manifest = parser.parse_dash(DASH_MPD, "http://example.com/dash/manifest.mpd")
        assert isinstance(manifest, DashManifest)
        assert len(manifest.periods) == 1
        assert manifest.periods[0]["id"] == "p0"
        assert len(manifest.adaptations) == 3

        video = [r for r in manifest.adaptations if (r.mime or "").startswith("video/")]
        audio = [r for r in manifest.adaptations if (r.mime or "").startswith("audio/")]
        assert len(video) == 2
        assert len(audio) == 1

        v1, v2 = sorted(video, key=lambda r: r.bandwidth or 0)
        assert (v1.width, v1.height, v1.bandwidth) == (640, 360, 800000)
        assert v1.fps == 30.0
        assert (v2.width, v2.height, v2.bandwidth) == (1920, 1080, 2800000)
        assert audio[0].bandwidth == 128000
        assert audio[0].codecs == "mp4a.40.2"

    def test_dash_segment_urls_resolved(self, parser):
        manifest = parser.parse_dash(DASH_MPD, "http://example.com/dash/manifest.mpd")
        rep = next(r for r in manifest.adaptations if r.height == 1080)
        # init segment + 3 media segments (PT30S / 10s each)
        assert len(rep.segments) == 4
        assert rep.segments[0] == "http://example.com/dash/video/v2/init.m4s"
        assert rep.segments[1:] == [
            f"http://example.com/dash/video/v2/{i}.m4s" for i in (1, 2, 3)
        ]
        assert rep.segment_template == "http://example.com/dash/video/$RepresentationID$/$Number$.m4s"
        assert rep.base_url == "http://example.com/dash/manifest.mpd"

    def test_segment_timeline_with_repeats(self, parser):
        manifest = parser.parse_dash(
            DASH_TIMELINE_MPD, "http://example.com/dash/manifest.mpd"
        )
        assert len(manifest.adaptations) == 1
        rep = manifest.adaptations[0]
        assert rep.fps == pytest.approx(30000 / 1001)
        # init + segments 0, 1 (r=1 repeat) and 2
        assert rep.segments == [
            "http://example.com/dash/v/init.m4s",
            "http://example.com/dash/v/0.m4s",
            "http://example.com/dash/v/1.m4s",
            "http://example.com/dash/v/2.m4s",
        ]

    def test_malformed_dash_raises_manifest_error(self, parser):
        with pytest.raises(ManifestError):
            parser.parse_dash("", "http://example.com/dash/manifest.mpd")
        with pytest.raises(ManifestError):
            parser.parse_dash("<MPD><Period>", "http://example.com/dash/manifest.mpd")
        with pytest.raises(ManifestError):
            parser.parse_dash("<foo/>", "http://example.com/dash/manifest.mpd")
        with pytest.raises(ManifestError):
            parser.parse_dash(
                '<?xml version="1.0"?><MPD xmlns="urn:mpeg:dash:schema:mpd:2011"/>',
                "http://example.com/dash/manifest.mpd",
            )
