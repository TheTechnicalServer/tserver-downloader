"""Media assembly for TServer Downloader (ffmpeg wrapper).

OWN architecture, written from scratch. No IDM code is used or referenced.

:class:`MediaAssembler` merges a separate audio track into a video file and
concatenates downloaded segments, both through the system ``ffmpeg``
binary. ``ffmpeg`` is invoked with an argument list only -- never
``shell=True`` -- and with a timeout, so untrusted file names cannot inject
commands. If ``ffmpeg`` is not installed, every operation raises
:class:`AssemblerError` with a clear message instead of failing obscurely.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

__all__ = ["AssemblerError", "MediaAssembler"]


class AssemblerError(RuntimeError):
    """Raised when assembly fails or ffmpeg is unavailable."""


class MediaAssembler:
    """Merges audio/video and concatenates segments using ffmpeg."""

    def __init__(self, ffmpeg_path: str = "ffmpeg") -> None:
        self.ffmpeg_path = ffmpeg_path

    # ------------------------------------------------------------ checks

    def is_ffmpeg_available(self) -> bool:
        """True when the ffmpeg binary can be found on PATH / at the path."""
        return shutil.which(self.ffmpeg_path) is not None

    def _require_ffmpeg(self) -> None:
        if not self.is_ffmpeg_available():
            raise AssemblerError(
                f"ffmpeg not found ({self.ffmpeg_path!r}). Install ffmpeg "
                "and make sure it is on PATH before merging or concatenating."
            )

    @staticmethod
    def _require_file(path: str | Path, label: str) -> Path:
        p = Path(path)
        if not p.is_file():
            raise AssemblerError(f"{label} not found: {p}")
        return p

    # ------------------------------------------------------------ actions

    def merge_audio_video(
        self,
        video_path: str | Path,
        audio_path: str | Path,
        output_path: str | Path,
        ffmpeg_path: str | None = None,
        timeout: int = 600,
    ) -> Path:
        """Mux ``audio_path`` into ``video_path`` (stream copy) -> output.

        ``ffmpeg_path`` overrides the path given at construction when
        provided; otherwise the construction-time path is used.

        Raises :class:`AssemblerError` if ffmpeg is missing, an input is
        missing, or ffmpeg exits non-zero (stderr is included in the
        message).
        """
        if ffmpeg_path is not None:
            self.ffmpeg_path = ffmpeg_path
        self._require_ffmpeg()
        video = self._require_file(video_path, "video file")
        audio = self._require_file(audio_path, "audio file")
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        self._run(
            [
                self.ffmpeg_path,
                "-y",
                "-i",
                str(video),
                "-i",
                str(audio),
                "-c",
                "copy",
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                str(output),
            ],
            timeout=timeout,
            what="merge audio and video",
        )
        return output

    def concat_segments(
        self,
        segment_paths: list[str | Path],
        output_path: str | Path,
        ffmpeg_path: str | None = None,
        timeout: int = 600,
    ) -> Path:
        """Concatenate segment files (ffmpeg concat demuxer) -> output.

        ``ffmpeg_path`` overrides the path given at construction when
        provided; otherwise the construction-time path is used.

        The concat list is written to a safely created temporary file
        (deleted afterwards) with ``-safe 0`` so absolute segment paths are
        accepted. Raises :class:`AssemblerError` on missing segments,
        missing ffmpeg, or ffmpeg failure.
        """
        if ffmpeg_path is not None:
            self.ffmpeg_path = ffmpeg_path
        self._require_ffmpeg()
        segments = [self._require_file(p, "segment file") for p in segment_paths]
        if not segments:
            raise AssemblerError("no segments given to concatenate")
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        list_file = tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        )
        try:
            for seg in segments:
                # Single-quote the path, escaping embedded quotes.
                safe = str(seg).replace("'", "'\\''")
                list_file.write(f"file '{safe}'\n")
            list_file.close()
            self._run(
                [
                    self.ffmpeg_path,
                    "-y",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    list_file.name,
                    "-c",
                    "copy",
                    str(output),
                ],
                timeout=timeout,
                what="concatenate segments",
            )
        finally:
            Path(list_file.name).unlink(missing_ok=True)
        return output

    # ------------------------------------------------------------ helpers

    def _run(self, args: list[str], timeout: int, what: str) -> None:
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise AssemblerError(
                f"ffmpeg timed out while trying to {what} after {timeout}s"
            ) from exc
        except OSError as exc:
            raise AssemblerError(
                f"could not run ffmpeg to {what}: {exc}"
            ) from exc
        if result.returncode != 0:
            detail = (result.stderr or "").strip().splitlines()
            tail = " ".join(detail[-3:]) if detail else "no ffmpeg output"
            raise AssemblerError(
                f"ffmpeg failed to {what} (exit {result.returncode}): {tail}"
            )
