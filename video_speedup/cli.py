"""Command-line interface for video_speedup."""
from __future__ import annotations

import argparse
import datetime
import logging
import sys
from pathlib import Path

from .ffutil import (
    FFToolError, ProbeResult, atempo_chain, check_tools_available,
    chunk_video, make_overlay_text, probe,
    _parse_metadata_start_time, _parse_filename_date, concat_videos,
    stamp_chunk_creation_time,
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


def main(argv: list[str] | None = None) -> int:
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

    log.info(f"Speed: {speed}x  |  Chunk duration: {chunk_duration}s  |  "
             f"Audio filter: {atempo_chain(speed)}")
    log.info(f"Input  folder: {folder}")
    log.info(f"Output folder: {out_root}")
    log.info("-" * 60)

    # Per-file ffmpeg logs always live alongside the input files so the
    # log path is predictable regardless of where --output points.
    log_dir = folder / ".video_speedup_logs"
    log_dir.mkdir(exist_ok=True)

    # Intermediate chunk files land here
    chunks_dir = folder / "chunks"

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

    for day, day_videos in sorted(videos_by_day.items()):
        log.info(f"\n{'=' * 60}\nProcessing day: {day}\n{'=' * 60}")
        day_videos.sort(key=lambda p: p.name)
        
        day_spedup_chunks = []
        day_failed = False
        # Seed timestamp once from the first file; carry it forward across all
        # sequential source files so consecutive clips (_000, _001, ...) share
        # the same filename timestamp but don't reset the clock.
        day_offset_seconds: float | None = None
        day_date_str: str | None = None

        for src in day_videos:
            duration = _get_duration(src)
            do_chunk = chunk_duration > 0 and (duration is None or duration > chunk_duration)

            if do_chunk:
                log.info(f"\n[CHUNK] {src.name}  "
                         f"(duration={duration:.1f}s, chunk_duration={chunk_duration}s)")
                try:
                    chunks = chunk_video(src, chunks_dir, chunk_duration)
                except FFToolError as e:
                    log.error(f"  -> FAILED to chunk: {e}")
                    total_failed += 1
                    day_failed = True
                    continue
                log.info(f"  -> {len(chunks)} chunk(s) created in {chunks_dir.name}/")

                # Stamp each chunk with its sequential start time so it
                # is self-describing even when processed independently.
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
            else:
                chunks = [src]
                log.info(f"\n[PROCESS] {src.name}  "
                         f"(duration={duration:.1f}s, no chunking needed)")

            src_probe = _get_probe(src)

            # Only seed the clock from metadata/filename for the very first
            # source file of the day.  Subsequent files continue where the
            # previous one left off so the timer never jumps backward.
            if day_offset_seconds is None:
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
                else:
                    log.warning(
                        f"  [TIME] No 'creation_time' metadata found in {src.name}. "
                        "Timestamp overlay will be disabled for this day."
                    )

            current_date_str = day_date_str
            current_offset_seconds = day_offset_seconds

            chunk_failed_any = False
            source_spedup_chunks = []
            
            for idx, chunk_path in enumerate(chunks, start=1):
                if args.no_overlay or src_probe is None:
                    overlay_text = None
                else:
                    overlay_text = make_overlay_text(
                        src_probe, chunk_path,
                        speed=speed,
                        current_date_str=current_date_str,
                        pts_offset_seconds=current_offset_seconds,
                    )

                dest = folder / f"{chunk_path.stem}{suffix}{chunk_path.suffix}"

                # Probe chunk before speedup to add to exact offset
                if current_offset_seconds is not None:
                    chunk_dur = _get_duration(chunk_path)
                    if chunk_dur:
                        current_offset_seconds += chunk_dur

                if dest.exists():
                    log.info(f"  Skipping (output exists): {chunk_path.name}")
                    total_skipped += 1
                    source_spedup_chunks.append(dest)
                    continue

                log.info(f"  Processing: {chunk_path.name} -> {dest.name}")
                result = speed_up_video(
                    chunk_path, dest, speed, log_dir,
                    keep_fps=args.fps is None,
                    target_fps=args.fps,
                    overlay_text=overlay_text,
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

            if do_chunk and not args.keep_chunks and not chunk_failed_any:
                for chunk_path in chunks:
                    chunk_path.unlink(missing_ok=True)
                try:
                    chunks_dir.rmdir()
                except OSError:
                    pass

            day_spedup_chunks.extend(source_spedup_chunks)
            # Carry the accumulated offset into the next source file.
            day_offset_seconds = current_offset_seconds

        if day_spedup_chunks and not day_failed:
            merged_dest = out_root / f"{day}_merged{day_spedup_chunks[0].suffix}"
            log.info(f"\n[CONCAT] Merging {len(day_spedup_chunks)} videos into {merged_dest.name}...")
            concat_log = log_dir / f"{day}_concat.log"
            if concat_videos(day_spedup_chunks, merged_dest, concat_log):
                log.info(f"  -> Concat OK: {merged_dest.name}")
                if not args.keep_chunks:
                    for p in day_spedup_chunks:
                        p.unlink(missing_ok=True)
            else:
                log.error(f"  -> Concat FAILED. See {concat_log}")

    log.info("\n" + "-" * 60)
    log.info(
        f"Done. Processed: {total_processed} | "
        f"Skipped: {total_skipped} | Failed: {total_failed}"
    )
    log.info(f"Per-file ffmpeg logs saved in: {log_dir}")
    return 0 if total_failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
