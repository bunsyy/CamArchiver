# video-speedup

Speed up video files while preserving audio pitch, with built-in tolerance
for corrupted AAC audio streams (common on SJCAM / action-cam footage
interrupted mid-recording).

The tool automatically chunks large videos for stability, burns a highly accurate
date/time overlay into the footage using video metadata, and seamlessly merges
all processed videos from a given day into a final output file.

## Requirements

- Python 3.9+
- `ffmpeg` and `ffprobe` on your PATH (`brew install ffmpeg` on macOS)
- The `aac_at` decoder (Apple AudioToolbox) is used as the primary audio
  path and is macOS-only. On other platforms the tool automatically falls
  back to ffmpeg's native decoder with lenient error handling.

## Install

```bash
cd video_speedup
pip install -e .
```

Or run directly without installing:

```bash
python -m video_speedup.cli /path/to/folder
```

## Usage

```bash
# Basic – output goes to <input>/x5/ automatically
video-speedup /path/to/videos
python3 -m video_speedup.cli /path/to/videos

# Explicit output folder
video-speedup /path/to/videos --output /path/to/output
python3 -m video_speedup.cli /path/to/videos -o /path/to/output

# Compress to H.265 (HEVC) for smaller file sizes
video-speedup /path/to/videos --compress

# Full example: 5x speed, 60s chunks, H.265 compression, custom output
video-speedup /path/to/videos -o /path/to/output --speed 5 --chunk-duration 60 --compress
python3 -m video_speedup.cli /path/to/videos -o /path/to/output --speed 5 --chunk-duration 60 --compress
```

> `python3 -m video_speedup.cli` is equivalent to the `video-speedup` command and works without installing the package — just `cd` into the repo root first.


### Features

- **Chunking for Stability**: Use `--chunk-duration` to split large videos before processing. This prevents OOM errors or audio drift on long recordings. Temporary chunks are placed in a `chunks/` folder and cleaned up automatically.
- **H.265 Compression**: Use `--compress` to encode output videos with HEVC (H.265) at CRF 23. This significantly reduces output file size compared to default H.264 while maintaining excellent visual quality.
- **Accurate Timestamps**: Burns an overlay in the bottom-left corner with the exact recording date and time (precise to the millisecond). Derived from the container's `creation_time` metadata or the filename pattern (`YYYY_MMDD_HHMMSS_mmm.MP4`).
- **Automatic Merging**: Groups source videos by day, processes them, and uses FFmpeg's `concat` demuxer to losslessly merge the speed-up chunks of each day together.
- **Flexible Output**: Use `-o / --output <DIR>` to specify exactly where merged videos are written. Without it, outputs go to `<input>/x{speed}/` automatically.
- **Agent Skill**: You can run `/speed-up-x5` in the chat to seamlessly speed up your videos!
- **Resilient**: Skips files whose output already exists and never drops audio silently — tries multiple decode strategies before giving up.
- **Diagnostics**: Writes per-file ffmpeg logs to `<input>/.video_speedup_logs/`.

## Duration diagnostic

For every processed file, the tool compares the **expected** output
duration (source duration ÷ speed) against the **actual** output duration
and prints a warning if they differ by more than 10% (or 2 seconds,
whichever is larger). This is meant to catch cases like: a 5-minute
source sped up 5x should produce ~60s of output — if you instead get
something much shorter (e.g. ~22s), the warning will flag it so it
doesn't go unnoticed, and you can check the matching log file in
`.video_speedup_logs/` for what ffmpeg reported.

If you hit this warning consistently, it's worth isolating whether it's
audio- or video-side by testing with `-an` (video only, no audio) against
the source file directly and comparing durations.
