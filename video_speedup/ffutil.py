"""Thin wrappers around ffmpeg/ffprobe for probing and running commands."""
from __future__ import annotations

import datetime
import json
import math
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


class FFToolError(RuntimeError):
    """Raised when ffmpeg or ffprobe is missing or a call fails unexpectedly."""


def check_tools_available() -> None:
    missing = [t for t in ("ffmpeg", "ffprobe") if shutil.which(t) is None]
    if missing:
        raise FFToolError(
            f"Missing required tool(s): {', '.join(missing)}. "
            "Install ffmpeg (e.g. `brew install ffmpeg`) and ensure it's on PATH."
        )


@dataclass
class StreamInfo:
    duration: float | None
    nb_frames: int | None
    r_frame_rate: str | None
    codec_type: str
    # Video-only fields (None for audio streams)
    width: int | None = field(default=None)
    height: int | None = field(default=None)


@dataclass
class ProbeResult:
    format_duration: float | None
    video: StreamInfo | None
    audio: StreamInfo | None
    format_tags: dict[str, str] = field(default_factory=dict)
    video_tags: dict[str, str] = field(default_factory=dict)


def _parse_frame_rate(rate: str | None) -> float | None:
    if not rate:
        return None
    if "/" in rate:
        num, _, den = rate.partition("/")
        try:
            num_f, den_f = float(num), float(den)
            return num_f / den_f if den_f else None
        except ValueError:
            return None
    try:
        return float(rate)
    except ValueError:
        return None


def probe(path: Path) -> ProbeResult:
    """Run ffprobe and return duration/frame info for the container,
    video stream, and audio stream (each may be missing/None)."""
    cmd = [
        "ffprobe",
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise FFToolError(f"ffprobe failed on {path}: {proc.stderr.strip()}")

    data = json.loads(proc.stdout)
    fmt = data.get("format", {})
    format_duration = float(fmt["duration"]) if fmt.get("duration") else None
    format_tags = fmt.get("tags", {})

    video = None
    audio = None
    video_tags: dict[str, str] = {}
    for s in data.get("streams", []):
        info = StreamInfo(
            duration=float(s["duration"]) if s.get("duration") else None,
            nb_frames=int(s["nb_frames"]) if s.get("nb_frames") else None,
            r_frame_rate=s.get("r_frame_rate"),
            codec_type=s.get("codec_type", ""),
            width=int(s["width"]) if s.get("width") else None,
            height=int(s["height"]) if s.get("height") else None,
        )
        if info.codec_type == "video" and video is None:
            video = info
            video_tags = s.get("tags", {})
        elif info.codec_type == "audio" and audio is None:
            audio = info

    return ProbeResult(
        format_duration=format_duration,
        video=video,
        audio=audio,
        format_tags=format_tags,
        video_tags=video_tags,
    )


# ---------------------------------------------------------------------------
# Overlay helpers
# ---------------------------------------------------------------------------

_FILENAME_DT_RE = re.compile(
    r"(\d{4})_(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})(?:_(\d{3}))?"
)


def _parse_metadata_start_time(src_probe: ProbeResult) -> datetime.datetime | None:
    """Extract recording start time exclusively from container/stream metadata.

    Two conventions are handled:

    * **SJCAM / action-cam originals**: ``creation_time`` is written at the
      *end* of recording, so we subtract the container duration.
    * **CamArchiver chunks**: stamped with our ``stamp_chunk_creation_time``
      helper, which writes ``creation_time`` as the chunk's *start* time and
      marks the file with ``comment=camarchiver_chunk``.  No subtraction needed.

    Returns None when no ``creation_time`` tag is present.
    """
    is_our_chunk = "camarchiver_chunk" in src_probe.format_tags.get("comment", "")

    creation_time_str = (
        src_probe.format_tags.get("creation_time")
        or src_probe.video_tags.get("creation_time")
    )
    if not creation_time_str:
        return None
    try:
        dt = datetime.datetime.fromisoformat(creation_time_str.replace("Z", "+00:00"))
        if not is_our_chunk and src_probe.format_duration:
            # Original cam convention: creation_time = end of recording.
            dt -= datetime.timedelta(seconds=src_probe.format_duration)
        # For our chunks: creation_time already IS the start, no adjustment.
        return dt
    except ValueError:
        return None


def stamp_chunk_creation_time(chunk_path: Path, start_dt: datetime.datetime) -> None:
    """Rewrite a chunk's ``creation_time`` metadata in-place to *start_dt*.

    The chunk is re-muxed (stream-copy, no re-encode) with two metadata tags:
    * ``creation_time`` – ISO-8601 UTC string of the chunk's recording start.
    * ``comment=camarchiver_chunk`` – sentinel so ``_parse_metadata_start_time``
      knows to treat ``creation_time`` as the start (not the end).
    """
    ts_str = start_dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{start_dt.microsecond // 1000:03d}000Z"
    tmp = chunk_path.with_suffix(".~camarch" + chunk_path.suffix)
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(chunk_path),
        "-metadata", f"creation_time={ts_str}",
        "-metadata", "comment=camarchiver_chunk",
        "-c", "copy",
        str(tmp),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode == 0:
        tmp.replace(chunk_path)
    else:
        tmp.unlink(missing_ok=True)
        raise FFToolError(
            f"Failed to stamp creation_time on {chunk_path.name}: "
            f"{proc.stderr.strip()[-300:]}"
        )


def _parse_filename_date(stem: str) -> str | None:
    """Extract just the YYYY_MMDD part of the filename."""
    m = _FILENAME_DT_RE.search(stem)
    if not m:
        return None

    year, month, day, _, _, _, _ = m.groups()
    return f"{year}_{month}{day}"


def _escape_drawtext_path(path: str) -> str:
    """Escape a file-system path for use as an ffmpeg filter option value."""
    path = path.replace("\\", "\\\\")
    path = path.replace("'",  "\\'")
    path = path.replace(":",  "\\:")
    return path


def make_overlay_text(
    src_probe: ProbeResult,
    src_path: Path,
    speed: float,
    current_date_str: str | None = None,
    pts_offset_seconds: float | None = None,
) -> str:
    """Build the metadata text to burn into the video.

    Lines (only non-empty ones are included):
      1. Original source filename stem
      2. Ticking timer with exact datetime
      3. Resolution  FPS  Frame
      4. Speed multiplier
    """
    lines: list[str] = []

    # Line 1 – filename
    lines.append(src_path.stem)

    # Line 2 – datetime or ticking timer
    if current_date_str and pts_offset_seconds is not None:
        lines.append(f"{current_date_str}  %{{pts:hms:{pts_offset_seconds:.3f}}}")

    # Line 3 – resolution + fps + frame
    res_parts: list[str] = []
    if src_probe.video:
        v = src_probe.video
        if v.width and v.height:
            res_parts.append(f"{v.width}x{v.height}")
        fps = _parse_frame_rate(v.r_frame_rate)
        if fps is not None:
            res_parts.append(f"{fps:.4g}fps")
    res_parts.append("Frame: %{n}")
    
    if res_parts:
        lines.append("  ".join(res_parts))

    # Line 4 – speed
    lines.append(f"{speed:g}x speed")

    return "\n".join(lines)


def make_drawtext_filter(text: str) -> str:
    """Return an ffmpeg ``drawtext`` filter that burns the given text
    into the bottom-left corner of the video frame.

    Layout:
      - 10px padding from the left and bottom edges
      - Semi-transparent black box behind the text
      - White text, 18px, 3px extra line spacing
    """
    escaped_text = _escape_drawtext_path(text)
    return (
        f"drawtext=text='{escaped_text}'"
        ":x=10"
        ":y=H-text_h-10"
        ":fontsize=18"
        ":fontcolor=white"
        ":box=1"
        ":boxcolor=black@0.55"
        ":boxborderw=8"
        ":line_spacing=3"
    )


# ---------------------------------------------------------------------------
# ffmpeg / chunking helpers
# ---------------------------------------------------------------------------

def atempo_chain(speed: float) -> str:
    """Build an ffmpeg atempo filter chain for the given speed multiplier.

    The atempo filter only accepts values in [0.5, 2.0], so for speeds
    outside that range we chain multiple atempo stages.

    Examples:
      speed=5.0  ->  "atempo=2.0,atempo=2.0,atempo=1.25"
      speed=2.0  ->  "atempo=2.0"
      speed=1.5  ->  "atempo=1.5"
    """
    if speed <= 0:
        raise ValueError(f"speed must be positive, got {speed}")

    stages: list[str] = []
    remaining = speed

    while remaining > 2.0 + 1e-9:
        stages.append("atempo=2.0")
        remaining /= 2.0

    while remaining < 0.5 - 1e-9:
        stages.append("atempo=0.5")
        remaining /= 0.5

    if abs(remaining - 1.0) > 1e-9:
        # Round to 6 significant figures to avoid floating-point noise
        stages.append(f"atempo={remaining:.6g}")

    return ",".join(stages) if stages else "atempo=1.0"


def chunk_video(src: Path, out_dir: Path, chunk_duration: float) -> list[Path]:
    """Split *src* into segments of at most *chunk_duration* seconds.

    Uses stream copy (no re-encode) for speed.  Segments are written to
    *out_dir* with names ``<stem>_chunk_NNN<ext>`` and are returned in
    sorted order.

    Raises FFToolError if ffmpeg exits non-zero.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    pattern = out_dir / f"{src.stem}_chunk_%03d{src.suffix}"
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(src),
        "-f", "segment",
        "-segment_time", str(int(math.ceil(chunk_duration))),
        "-reset_timestamps", "1",
        "-c", "copy",
        str(pattern),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise FFToolError(
            f"Chunking failed for {src.name}: {proc.stderr.strip()[-400:]}"
        )

    chunks = sorted(out_dir.glob(f"{src.stem}_chunk_*{src.suffix}"))
    if not chunks:
        raise FFToolError(f"Chunking produced no output files for {src.name}")
    return chunks


def run_ffmpeg(args: list[str], log_path: Path) -> tuple[bool, str]:
    """Run an ffmpeg command, capturing stderr to log_path.
    Returns (success, stderr_text)."""
    cmd = ["ffmpeg", "-loglevel", "error", "-y", *args]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    log_path.write_text(proc.stderr)
    return proc.returncode == 0, proc.stderr


def concat_videos(chunk_paths: list[Path], dest: Path, log_path: Path) -> bool:
    """Concatenate multiple videos into a single output using the concat demuxer."""
    if not chunk_paths:
        return False

    list_path = log_path.with_suffix(".list.txt")
    with list_path.open("w") as f:
        for p in chunk_paths:
            # ffmpeg requires quotes if paths have spaces, but standard single quotes
            # around the absolute path is safest.
            f.write(f"file '{p.absolute()}'\n")

    args = [
        "-f", "concat",
        "-safe", "0",
        "-i", str(list_path),
        "-c", "copy",
        str(dest)
    ]
    ok, _ = run_ffmpeg(args, log_path)
    return ok
