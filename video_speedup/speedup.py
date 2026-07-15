"""Core logic: speed up a single video file, with diagnostics and
fallback strategies for corrupted audio streams."""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path

from .ffutil import ProbeResult, atempo_chain, make_drawtext_filter, probe, run_ffmpeg, detect_gpu_encoder

log = logging.getLogger("video_speedup")


@dataclass
class SpeedupResult:
    source: Path
    output: Path | None
    ok: bool
    strategy: str
    expected_duration: float | None
    actual_duration: float | None
    message: str = ""

    @property
    def duration_ok(self) -> bool:
        if self.expected_duration is None or self.actual_duration is None:
            return True  # can't judge, don't flag
        # allow 10% tolerance for keyframe/rounding slack
        return abs(self.actual_duration - self.expected_duration) <= max(
            2.0, self.expected_duration * 0.10
        )


def _expected_output_duration(src_probe: ProbeResult, speed: float) -> float | None:
    src_duration = src_probe.format_duration
    if src_duration is None:
        return None
    return src_duration / speed


def speed_up_video(
    src: Path,
    dest: Path,
    speed: float,
    log_dir: Path,
    keep_fps: bool = True,
    target_fps: float | None = None,
    overlay_text: str | None = None,
    compress: bool = False,
    preset: str = "fast",
    use_gpu: bool = False,
    gpu_quality: int | None = None,
) -> SpeedupResult:
    """Speed up a single video file by `speed`x, preserving audio pitch.

    If *overlay_text* is provided it is burned into the bottom-left corner
    of every frame via ffmpeg's drawtext filter.

    Tries, in order:
      1. Apple AudioToolbox AAC decoder (aac_at) - tolerates corrupted AAC
         frames far better than ffmpeg's native decoder (macOS only).
      2. Native decoder with lenient error-handling flags.

    Always keeps audio - never silently falls back to video-only.
    """
    src_probe = probe(src)
    expected = _expected_output_duration(src_probe, speed)

    # Determine output fps: explicit target, or keep source fps (default)
    src_fps: float | None = None
    if src_probe.video:
        from .ffutil import _parse_frame_rate
        src_fps = _parse_frame_rate(src_probe.video.r_frame_rate)

    out_fps: float | None = target_fps if (not keep_fps and target_fps) else src_fps

    # Build video filter chain.
    # IMPORTANT: drawtext MUST be applied before setpts so that the burnt-in
    # timestamps reflect original real-world time (and fly by at 5x speed), 
    # rather than ticking slowly against the compressed PTS.
    vf_parts = []
    if overlay_text:
        vf_parts.append(make_drawtext_filter(overlay_text))
    
    vf_parts.append(f"setpts=PTS/{speed}")
    
    if out_fps:
        # Always lock output to a fixed fps — prevents QuickTime from showing
        # the slow-motion scrubber bar that appears on high-fps source videos.
        vf_parts.append(f"fps={out_fps:.6g}")
        
    vf = ",".join(vf_parts)

    af = atempo_chain(speed)

    if use_gpu:
        gpu_enc, gpu_label = detect_gpu_encoder(compress=compress)
        if gpu_enc:
            # Determine appropriate quality flag and default based on encoder type
            if gpu_label == "VideoToolbox":
                quality_flag = "-q:v"
                q_val = str(gpu_quality if gpu_quality is not None else 65)
            elif gpu_label == "NVENC":
                quality_flag = "-cq"
                q_val = str(gpu_quality if gpu_quality is not None else 28)
            elif gpu_label == "VAAPI":
                quality_flag = "-qp"
                q_val = str(gpu_quality if gpu_quality is not None else 28)
            else:
                quality_flag = "-q:v"
                q_val = str(gpu_quality if gpu_quality is not None else 65)

            if compress:
                video_enc = ["-c:v", gpu_enc, quality_flag, q_val, "-tag:v", "hvc1"]
            else:
                video_enc = ["-c:v", gpu_enc, quality_flag, q_val]
        else:
            log.warning(
                "  [GPU] --gpu requested but no supported GPU encoder found "
                "(VideoToolbox / NVENC / VAAPI). Falling back to CPU encoding."
            )
            use_gpu = False  # triggers the CPU branch below

    if not use_gpu:
        video_enc = (
            ["-c:v", "libx265", "-crf", "23", "-preset", preset, "-tag:v", "hvc1"]
            if compress
            else ["-c:v", "libx264"]
        )

    strategies = []

    if sys.platform == "darwin":
        strategies.append(
            (
                "aac_at",
                [
                    "-c:a", "aac_at",
                    "-i", str(src),
                    "-filter_complex", f"[0:v]{vf}[v];[0:a]{af}[a]",
                    "-map", "[v]", "-map", "[a]",
                    *video_enc,
                    "-c:a", "aac", "-b:a", "128k",
                    str(dest),
                ],
            )
        )

    strategies.append(
        (
            "native_tolerant",
            [
                "-fflags", "+discardcorrupt",
                "-err_detect", "ignore_err",
                "-i", str(src),
                "-filter_complex", f"[0:v]{vf}[v];[0:a]{af}[a]",
                "-map", "[v]", "-map", "[a]",
                *video_enc,
                "-c:a", "aac", "-b:a", "128k",
                str(dest),
            ],
        )
    )

    last_err = ""
    for name, args in strategies:
        log_path = log_dir / f"{src.stem}.{name}.log"
        ok, err = run_ffmpeg(args, log_path)
        if ok and dest.exists():
            out_probe = probe(dest)
            result = SpeedupResult(
                source=src,
                output=dest,
                ok=True,
                strategy=name,
                expected_duration=expected,
                actual_duration=out_probe.format_duration,
            )
            if not result.duration_ok:
                result.message = (
                    f"WARNING: output duration {result.actual_duration:.1f}s "
                    f"differs from expected {expected:.1f}s (see {log_path})"
                )
                log.warning(result.message)
            return result
        last_err = err
        dest.unlink(missing_ok=True)

    return SpeedupResult(
        source=src,
        output=None,
        ok=False,
        strategy="none",
        expected_duration=expected,
        actual_duration=None,
        message=f"All strategies failed. Last error: {last_err.strip()[-300:]}",
    )
