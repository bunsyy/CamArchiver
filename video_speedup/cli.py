"""Command-line interface for video_speedup."""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import sys
import time
from pathlib import Path

from .ffutil import (
    FFToolError, ProbeResult, atempo_chain, check_tools_available,
    chunk_video, make_overlay_text, probe,
    _parse_metadata_start_time, _parse_filename_date, concat_videos,
    stamp_chunk_creation_time, detect_gpu_encoder,
)
from .speedup import speed_up_video

log = logging.getLogger("video_speedup")

VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v"}


def find_videos(folder: Path) -> list[Path]:
    """Return sorted video files directly inside *folder* (non-recursive).

    Excludes files already inside the ``chunks/`` sub-directory so we don't
    accidentally re-process intermediate chunk files.
    """
    chunks_dir = folder / "chunks"
    return sorted(
        p for p in folder.iterdir()
        if p.is_file()
        and p.suffix.lower() in VIDEO_EXTENSIONS
        and p.parent != chunks_dir
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="video_speedup",
        description=(
            "Chunk videos into ≤N-second segments, then speed each one up "
            "while preserving audio pitch. Tolerant of corrupted AAC streams."
        ),
    )
    p.add_argument("folder", type=Path, help="Input folder containing source video files")
    p.add_argument(
        "-o", "--output", type=Path, default=None, metavar="DIR",
        help="Output folder for merged videos (default: <folder>/x{speed}/ is created automatically)",
    )
    p.add_argument(
        "-s", "--speed", type=float, default=5.0,
        help="Speed multiplier (default: 5.0)",
    )
    p.add_argument(
        "--chunk-duration", type=float, default=60.0, metavar="SECONDS",
        help="Maximum duration of each chunk in seconds before speed-up "
             "(default: 60). Use 0 to skip chunking.",
    )
    p.add_argument(
        "--compress", action="store_true",
        help="Encode output video with H.265 (HEVC) instead of H.264 for smaller file sizes.",
    )
    p.add_argument(
        "--preset", default="medium",
        choices=["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"],
        help="x265 encoder preset when --compress is used (default: medium). "
             "Slower presets produce smaller files at the same quality.",
    )
    p.add_argument(
        "--gpu", action="store_true",
        help="Use GPU-accelerated encoding via VideoToolbox (macOS) / NVENC (NVIDIA) / VAAPI (Linux). "
             "Falls back to CPU if no GPU encoder is found.",
    )
    p.add_argument(
        "--gpu-quality", type=int, default=None, metavar="N",
        help="Quality for GPU encoders. Auto-selected based on the encoder: "
             "65 for VideoToolbox (-q:v, 1-100), or 28 for NVENC/VAAPI (-cq/-qp, 0-51).",
    )
    p.add_argument(
        "--fps", type=float, default=None,
        help="Force a specific output frame rate (default: keep source fps)",
    )
    p.add_argument(
        "--suffix", type=str, default=None,
        help="Output filename suffix, default '_{speed}x'",
    )
    p.add_argument(
        "--keep-chunks", action="store_true",
        help="Keep intermediate chunk files after processing (default: delete them)",
    )
    p.add_argument(
        "--no-overlay", action="store_true",
        help="Do not burn metadata overlay into output videos",
    )
    p.add_argument(
        "-v", "--verbose", action="store_true", help="Verbose logging",
    )
    return p


def _get_probe(path: Path) -> ProbeResult | None:
    """Return a ProbeResult for *path*, or None on failure."""
    try:
        return probe(path)
    except Exception:
        return None


def _get_duration(path: Path) -> float | None:
    """Return the container duration of *path* in seconds, or None on failure."""
    p = _get_probe(path)
    return p.format_duration if p else None


def _format_duration(seconds: float) -> str:
    if seconds is None:
        return "0s"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h}h {m}m {s}s"
    elif m > 0:
        return f"{m}m {s}s"
    return f"{s}s"


def _format_size(size_bytes: int) -> str:
    if size_bytes == 0:
        return "0 B"
        
    # macOS uses base-10. Ubuntu/Windows typically use base-2.
    base = 1000.0 if sys.platform == "darwin" else 1024.0
    
    units = ("B", "KB", "MB", "GB", "TB")
    i = 0
    size = float(size_bytes)
    while size >= base and i < len(units) - 1:
        size /= base
        i += 1
    return f"{size:.2f} {units[i]}"


def _write_markdown_report(report_path: Path, stats_dict: dict, speed: float, elapsed_str: str) -> None:
    os_name = "macOS (Base-10)" if sys.platform == "darwin" else "Ubuntu/Windows (Base-2)"
    lines = [
        f"# Video Speedup Report (x{speed:g})",
        "",
        f"**Total Execution Time (This Run):** {elapsed_str}",
        "",
        "| Date | Original Videos | Original Duration | Original Size | Final Duration | Final Size | Storage Saved | OS Format |",
        "|------|-----------------|-------------------|---------------|----------------|------------|---------------|-----------|"
    ]
    
    total_orig = 0
    total_final = 0
    total_saved = 0
    total_videos = 0
    total_orig_dur = 0.0
    total_final_dur = 0.0
    
    for day in sorted(stats_dict.keys()):
        stat = stats_dict[day]
        lines.append(
            f"| {day} | {stat['num_videos']} | "
            f"{_format_duration(stat['original_duration'])} | "
            f"{_format_size(stat['original_bytes'])} | "
            f"{_format_duration(stat['final_duration'])} | "
            f"{_format_size(stat['final_bytes'])} | "
            f"{_format_size(stat['saved_bytes'])} | "
            f"{os_name} |"
        )
        total_orig += stat['original_bytes']
        total_final += stat['final_bytes']
        total_saved += stat['saved_bytes']
        total_videos += stat['num_videos']
        total_orig_dur += stat['original_duration']
        total_final_dur += stat['final_duration']
        
    lines.append(
        f"| **Total** | **{total_videos}** | "
        f"**{_format_duration(total_orig_dur)}** | "
        f"**{_format_size(total_orig)}** | "
        f"**{_format_duration(total_final_dur)}** | "
        f"**{_format_size(total_final)}** | "
        f"**{_format_size(total_saved)}** | "
        f"**-** |"
    )
    
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    start_time = time.time()
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )

    try:
        check_tools_available()
    except FFToolError as e:
        log.error(str(e))
        return 1

    folder: Path = args.folder
    if not folder.is_dir():
        log.error(f"Not a directory: {folder}")
        return 1

    speed: float = args.speed
    chunk_duration: float = args.chunk_duration
    suffix: str = args.suffix or f"_{speed:g}x"

    # Resolve output folder: explicit --output, or auto <folder>/x{speed}/
    out_root: Path = args.output if args.output else folder / f"x{speed:g}"
    out_root.mkdir(parents=True, exist_ok=True)

    # ── Resolve encoder (once, so we can report it in the banner) ────────────
    if args.gpu:
        gpu_enc, gpu_label = detect_gpu_encoder(compress=args.compress)
    else:
        gpu_enc, gpu_label = None, None

    if gpu_enc:
        enc_backend  = f"GPU  ({gpu_label})"
        enc_name     = gpu_enc
        enc_extra    = ""  # VideoToolbox / NVENC don't use libx265 presets
    else:
        if args.gpu:
            # --gpu was requested but nothing found — warn and fall back
            log.warning("⚠  No GPU encoder found (VideoToolbox / NVENC / VAAPI). Falling back to CPU.")
        enc_backend = "CPU"
        if args.compress:
            enc_name  = "libx265 (HEVC/H.265)"
            enc_extra = f"  preset={args.preset}"
        else:
            enc_name  = "libx264 (H.264)"
            enc_extra = ""

    chunk_info = f"{chunk_duration}s" if chunk_duration > 0 else "disabled"
    fps_info   = f"{args.fps}" if args.fps else "source fps"
    overlay_info = "off" if args.no_overlay else "on"

    log.info("=" * 60)
    log.info("  video_speedup — encoding settings")
    log.info("=" * 60)
    log.info(f"  Encoding     : {enc_backend}")
    log.info(f"  Video codec  : {enc_name}{enc_extra}")
    log.info(f"  Audio filter : {atempo_chain(speed)}")
    log.info(f"  Speed        : {speed:g}x")
    log.info(f"  Chunk        : {chunk_info}")
    log.info(f"  FPS          : {fps_info}")
    log.info(f"  Overlay      : {overlay_info}")
    log.info("-" * 60)
    log.info(f"  Input        : {folder}")
    log.info(f"  Output       : {out_root}")
    log.info("=" * 60)

    # Per-file ffmpeg logs always live alongside the input files so the
    # log path is predictable regardless of where --output points.
    log_dir = folder / ".video_speedup_logs"
    log_dir.mkdir(exist_ok=True)

    # Intermediate chunk files land here (in current working directory)
    chunks_dir = Path.cwd() / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)

    source_videos = find_videos(folder)
    source_videos = [v for v in source_videos if not v.stem.endswith(suffix) and not v.stem.endswith("_merged")]

    if not source_videos:
        log.info(f"No video files found in {folder}")
        return 0

    from collections import defaultdict
    videos_by_day = defaultdict(list)
    for v in source_videos:
        day = _parse_filename_date(v.stem)
        if day:
            videos_by_day[day].append(v)
        else:
            videos_by_day["unknown_date"].append(v)

    log.info(f"Found {len(source_videos)} source video(s) across {len(videos_by_day)} day(s).")

    total_processed = total_skipped = total_failed = 0
    
    report_stats = {}
    history_file = out_root / ".conversion_history.json"
    if history_file.exists():
        try:
            with open(history_file, "r") as f:
                report_stats = json.load(f)
        except Exception as e:
            log.warning(f"Could not load history file: {e}")

    for day, day_videos in sorted(videos_by_day.items()):
        original_bytes = sum(p.stat().st_size for p in day_videos if p.exists())
        original_duration = sum((_get_duration(p) or 0.0) for p in day_videos if p.exists())
        log.info(f"\n{'=' * 60}\nProcessing day: {day}\n{'=' * 60}")
        day_videos.sort(key=lambda p: p.name)
        
        # Check if the final merged video for this day already exists in the output folder
        merged_dest = out_root / f"{day}{day_videos[0].suffix}"
        if merged_dest.exists():
            log.info(f"  [SKIP] Merged video '{merged_dest.name}' already exists in target folder. Skipping this day.")
            total_skipped += len(day_videos)
            continue
        
        day_spedup_chunks = []
        day_failed = False
        # Seed timestamp once from the first file; carry it forward across all
        # sequential source files so consecutive clips (_000, _001, ...) share
        # the same filename timestamp but don't reset the clock.
        day_offset_seconds: float | None = None
        day_date_str: str | None = None

        log.info("\n--- Phase 1: Chunking ---")
        day_tasks = []
        total_src = len(day_videos)

        for i, src in enumerate(day_videos):
            pct = int((i / total_src) * 100)
            log.info(f"\n[PHASE 1] {pct}% - Analyzing {src.name}")
            
            duration = _get_duration(src)
            do_chunk = chunk_duration > 0 and (duration is None or duration > chunk_duration)
            src_probe = _get_probe(src)

            # Try to seed the clock from metadata/filename for THIS source file.
            start_dt = _parse_metadata_start_time(src_probe) if src_probe else None
            if start_dt:
                day_date_str = start_dt.strftime("%Y-%m-%d")
                day_offset_seconds = (
                    start_dt.hour * 3600
                    + start_dt.minute * 60
                    + start_dt.second
                    + start_dt.microsecond / 1e6
                )
                log.info(f"  [TIME] Seeded from metadata creation_time → {start_dt.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} UTC")
            elif day_offset_seconds is not None:
                log.info(f"  [TIME] No 'creation_time' metadata found in {src.name}. Continuing from previous file's clock.")
            else:
                log.warning(
                    f"  [TIME] No 'creation_time' metadata found in {src.name}. "
                    "Timestamp overlay will be disabled for this file."
                )

            # Capture the start time for THIS specific source file before we advance it
            file_start_date_str = day_date_str
            file_start_offset_seconds = day_offset_seconds

            if do_chunk:
                src_chunks_dir = chunks_dir / src.stem
                done_file = src_chunks_dir / ".phase1_done"
                
                if done_file.exists():
                    chunks = sorted(src_chunks_dir.glob(f"{src.stem}_chunk_*{src.suffix}"))
                    log.info(f"\n[CHUNK] {src.name} (duration={duration:.1f}s, chunk_duration={chunk_duration}s) already exists in the chunks folder. Skipping it.")
                    
                    if day_offset_seconds is not None:
                        stamp_offset = day_offset_seconds
                        for chunk_path in chunks:
                            stamp_offset += (_get_duration(chunk_path) or chunk_duration)
                        day_offset_seconds = stamp_offset
                else:
                    log.info(f"\n[CHUNK] {src.name} (duration={duration:.1f}s, chunk_duration={chunk_duration}s)")
                    try:
                        chunks = chunk_video(src, src_chunks_dir, chunk_duration)
                    except FFToolError as e:
                        log.error(f"  -> FAILED to chunk: {e}")
                        total_failed += 1
                        day_failed = True
                        continue
                    log.info(f"  -> {len(chunks)} chunk(s) created in {chunks_dir.name}/{src.stem}/")
    
                    # Stamp each chunk with its sequential start time
                    if day_date_str and day_offset_seconds is not None:
                        base_date = datetime.datetime.strptime(day_date_str, "%Y-%m-%d").replace(
                            tzinfo=datetime.timezone.utc
                        )
                        stamp_offset = day_offset_seconds
                        for chunk_path in chunks:
                            chunk_start_dt = base_date + datetime.timedelta(seconds=stamp_offset)
                            try:
                                stamp_chunk_creation_time(chunk_path, chunk_start_dt)
                            except FFToolError as e:
                                log.warning(f"  [TIME] Could not stamp {chunk_path.name}: {e}")
                            chunk_dur = _get_duration(chunk_path) or chunk_duration
                            stamp_offset += chunk_dur
                        
                        # Advance the global clock for the NEXT source file (if it lacks metadata)
                        day_offset_seconds = stamp_offset
                    
                    done_file.touch()
            else:
                chunks = [src]
                src_chunks_dir = None
                log.info(f"\n[PROCESS] {src.name} (duration={duration:.1f}s, no chunking needed)")

                # Advance the global clock for the NEXT source file (if it lacks metadata)
                if day_offset_seconds is not None:
                    day_offset_seconds += duration if duration else 0

            day_tasks.append({
                "src_probe": src_probe,
                "chunks": chunks,
                "do_chunk": do_chunk,
                "src_chunks_dir": src_chunks_dir,
                "start_date_str": file_start_date_str,
                "start_offset_seconds": file_start_offset_seconds,
            })

        if day_failed:
            continue

        log.info("\n--- Phase 2: Speed Up ---")
        total_chunks = sum(len(t["chunks"]) for t in day_tasks)
        chunks_done = 0

        for task in day_tasks:
            src_probe = task["src_probe"]
            chunks = task["chunks"]
            current_date_str = task["start_date_str"]
            current_offset_seconds = task["start_offset_seconds"]
            
            chunk_failed_any = False
            source_spedup_chunks = []
            
            for idx, chunk_path in enumerate(chunks, start=1):
                pct = int((chunks_done / total_chunks) * 100) if total_chunks > 0 else 100
                if args.no_overlay or src_probe is None:
                    overlay_text = None
                else:
                    overlay_text = make_overlay_text(
                        src_probe, chunk_path,
                        speed=speed,
                        current_date_str=current_date_str,
                        pts_offset_seconds=current_offset_seconds,
                    )

                dest = chunks_dir / f"{chunk_path.stem}{suffix}{chunk_path.suffix}"

                # Probe chunk before speedup to add to exact offset
                if current_offset_seconds is not None:
                    chunk_dur = _get_duration(chunk_path)
                    if chunk_dur:
                        current_offset_seconds += chunk_dur

                if dest.exists():
                    log.info(f"  [PHASE 2] {pct}% - Skipping (output exists): {chunk_path.name}")
                    total_skipped += 1
                    source_spedup_chunks.append(dest)
                    chunks_done += 1
                    continue

                log.info(f"  [PHASE 2] {pct}% - Processing: {chunk_path.name} -> {dest.name}")
                result = speed_up_video(
                    chunk_path, dest, speed, log_dir,
                    keep_fps=args.fps is None,
                    target_fps=args.fps,
                    overlay_text=overlay_text,
                    compress=args.compress,
                    preset=args.preset,
                    use_gpu=args.gpu,
                    gpu_quality=args.gpu_quality,
                )

                if result.ok:
                    total_processed += 1
                    note = f" [{result.strategy}]"
                    if result.message:
                        note += f"\n    {result.message}"
                    log.info(f"    -> OK{note}")
                    source_spedup_chunks.append(dest)
                else:
                    total_failed += 1
                    chunk_failed_any = True
                    day_failed = True
                    log.error(f"    -> FAILED: {result.message}")
                    
                chunks_done += 1

            task["chunk_failed_any"] = chunk_failed_any
            day_spedup_chunks.extend(source_spedup_chunks)

        if day_spedup_chunks and not day_failed:
            log.info("\n--- Phase 3: Clean Up & Merge ---")
            log.info(f"\n[CONCAT] Merging {len(day_spedup_chunks)} videos into {merged_dest.name}...")
            concat_log = log_dir / f"{day}_concat.log"
            if concat_videos(day_spedup_chunks, merged_dest, concat_log):
                log.info(f"  -> Concat OK: {merged_dest.name}")
                
                final_bytes = merged_dest.stat().st_size
                final_duration = _get_duration(merged_dest) or 0.0
                report_stats[day] = {
                    "num_videos": len(day_videos),
                    "original_bytes": original_bytes,
                    "final_bytes": final_bytes,
                    "saved_bytes": original_bytes - final_bytes,
                    "original_duration": original_duration,
                    "final_duration": final_duration
                }
                
                # Live update the conversion report and history after this day is done
                try:
                    with open(history_file, "w") as f:
                        json.dump(report_stats, f, indent=2)
                except Exception as e:
                    log.warning(f"Failed to write history file: {e}")
                
                report_path = out_root / "conversion_report.md"
                elapsed_seconds = time.time() - start_time
                elapsed_str = str(datetime.timedelta(seconds=int(elapsed_seconds)))
                _write_markdown_report(report_path, report_stats, speed, elapsed_str)
                
                if not args.keep_chunks:
                    # Clean up raw chunks
                    for task in day_tasks:
                        if task["do_chunk"] and not task["chunk_failed_any"]:
                            for chunk_path in task["chunks"]:
                                chunk_path.unlink(missing_ok=True)
                            try:
                                if task["src_chunks_dir"]: 
                                    (task["src_chunks_dir"] / ".phase1_done").unlink(missing_ok=True)
                                    task["src_chunks_dir"].rmdir()
                            except OSError:
                                pass
                    # Clean up sped-up chunks
                    for p in day_spedup_chunks:
                        p.unlink(missing_ok=True)
            else:
                log.error(f"  -> Concat FAILED. See {concat_log}")

    log.info("\n" + "-" * 60)
    
    elapsed_seconds = time.time() - start_time
    elapsed_str = str(datetime.timedelta(seconds=int(elapsed_seconds)))
    
    log.info(
        f"Done in {elapsed_str}. Processed: {total_processed} | "
        f"Skipped: {total_skipped} | Failed: {total_failed}"
    )
    log.info(f"Per-file ffmpeg logs saved in: {log_dir}")
    
    if report_stats:
        report_path = out_root / "conversion_report.md"
        _write_markdown_report(report_path, report_stats, speed, elapsed_str)
        log.info(f"Markdown storage report saved to: {report_path}")

    if not args.keep_chunks:
        try:
            chunks_dir.rmdir()
        except OSError:
            pass
    return 0 if total_failed == 0 else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)  # conventional exit code for Ctrl+C
