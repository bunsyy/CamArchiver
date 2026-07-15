"""Command-line interface for video_speedup."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .ffutil import FFToolError, ProbeResult, atempo_chain, check_tools_available, chunk_video, make_overlay_text, probe
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
    p.add_argument("folder", type=Path, help="Folder containing video files")
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

    log.info(f"Speed: {speed}x  |  Chunk duration: {chunk_duration}s  |  "
             f"Audio filter: {atempo_chain(speed)}")
    log.info(f"Source folder: {folder}")
    log.info("-" * 60)

    # Per-file ffmpeg logs land here
    log_dir = folder / ".video_speedup_logs"
    log_dir.mkdir(exist_ok=True)

    # Intermediate chunk files land here
    chunks_dir = folder / "chunks"

    source_videos = find_videos(folder)
    source_videos = [v for v in source_videos if not v.stem.endswith(suffix)]

    if not source_videos:
        log.info(f"No video files found in {folder}")
        return 0

    log.info(f"Found {len(source_videos)} source video(s).")

    total_processed = total_skipped = total_failed = 0

    for src in source_videos:
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
                continue
            log.info(f"  -> {len(chunks)} chunk(s) created in {chunks_dir.name}/")
        else:
            # Video is already ≤chunk_duration (or chunking disabled); treat it
            # as a single "chunk" so the rest of the pipeline is identical.
            chunks = [src]
            log.info(f"\n[PROCESS] {src.name}  "
                     f"(duration={duration:.1f}s, no chunking needed)")

        # Build overlay text once per source video (shared metadata).
        # Chunk index is injected per-chunk below.
        src_probe = _get_probe(src)

        # ------------------------------------------------------------------ #
        # Speed up each chunk                                                  #
        # ------------------------------------------------------------------ #
        chunk_failed_any = False
        for idx, chunk_path in enumerate(chunks, start=1):
            # Build per-chunk overlay (includes chunk N/M counter if multi-chunk)
            if args.no_overlay or src_probe is None:
                overlay_text = None
            else:
                overlay_text = make_overlay_text(
                    src_probe, src,
                    speed=speed,
                    chunk_index=idx if len(chunks) > 1 else None,
                    total_chunks=len(chunks) if len(chunks) > 1 else None,
                )

            # Output sits alongside the *source* video, not in chunks/
            dest = folder / f"{chunk_path.stem}{suffix}{chunk_path.suffix}"

            if dest.exists():
                log.info(f"  Skipping (output exists): {chunk_path.name}")
                total_skipped += 1
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
            else:
                total_failed += 1
                chunk_failed_any = True
                log.error(f"    -> FAILED: {result.message}")

        # Clean up chunk files unless --keep-chunks was requested or the
        # source *was* the chunk (no splitting happened)
        if do_chunk and not args.keep_chunks and not chunk_failed_any:
            for chunk_path in chunks:
                chunk_path.unlink(missing_ok=True)
            # Remove the chunks dir if now empty
            try:
                chunks_dir.rmdir()
            except OSError:
                pass  # not empty — other chunks still there

    log.info("\n" + "-" * 60)
    log.info(
        f"Done. Processed: {total_processed} | "
        f"Skipped: {total_skipped} | Failed: {total_failed}"
    )
    log.info(f"Per-file ffmpeg logs saved in: {log_dir}")
    return 0 if total_failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
