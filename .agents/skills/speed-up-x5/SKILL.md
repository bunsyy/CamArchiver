---
name: speed-up-x5
description: Speed up videos by 5x using the video_speedup CLI tool.
---
# Speed up Videos 5x

When the user triggers this skill, help them process their videos at 5x speed using the local `video_speedup` tool. Follow these exact steps:

## Step 1 – Gather Required Paths

Check if the user explicitly provided an **input directory** and an **output directory** in their message.

If either is missing, **stop and ask**:
> "I can speed up your videos to 5x! Please provide:
> - **Input folder** – the directory containing the source `.MP4` files
> - **Output folder** – where you want the final merged video saved"

Do not proceed until you have both paths confirmed.

## Step 2 – Run the Pipeline

Once you have both paths, run:

```bash
python -m video_speedup.cli <input_folder> --output <output_folder> --chunk-duration 60 --speed 5 --compress
```

- `<input_folder>` — the directory containing source videos
- `<output_folder>` — where the merged `YYYYMMDD_merged.MP4` will be written (created if it doesn't exist)

## Step 3 – Report Results

After the command completes, confirm success to the user and show the path of the merged output file(s) inside the output folder.

