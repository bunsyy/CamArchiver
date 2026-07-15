# video-speedup

Speed up video files while preserving audio pitch, with built-in tolerance
for corrupted AAC audio streams (common on SJCAM / action-cam footage
interrupted mid-recording).

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
video-speedup /path/to/folder
video-speedup /path/to/folder --speed 3
video-speedup /path/to/folder --speed 5 --fps 30
```

- Skips files whose name already ends in the speed suffix (e.g. `_5x`).
- Skips files whose output already exists.
- Never drops audio silently — tries multiple decode strategies before
  giving up on a file.
- Writes a per-file ffmpeg log to `<folder>/.video_speedup_logs/`.

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
