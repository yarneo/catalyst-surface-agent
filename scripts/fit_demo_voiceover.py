#!/usr/bin/env python3
"""Fit a human voiceover take to the eight-scene demo video.

The default cut map matches the reviewed Sep 4 recording. It removes only the
leading slate, two verbal stumbles, and trailing room tone. Long internal pauses
are shortened without changing the speaker's overall cadence.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path

from render_demo_video import ROOT, SCENES, duration, render_scene


# One or more source ranges for each scene. Multiple ranges are joined when a
# verbal stumble sits between otherwise useful sentences.
CUTS = [
    [(2.03, 25.63)],
    [(25.63, 61.27)],
    [(61.27, 96.75)],
    [(96.75, 125.35)],
    [(125.35, 152.35)],
    [(152.35, 185.55)],
    [(187.10, 190.85), (195.65, 218.30)],
    [(221.30, 255.58)],
]


def run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def audio_filter(ranges: list[tuple[float, float]]) -> str:
    pieces = []
    labels = []
    for index, (start, end) in enumerate(ranges):
        label = f"a{index}"
        pieces.append(
            f"[0:a]atrim=start={start}:end={end},"
            f"asetpts=PTS-STARTPTS[{label}]"
        )
        labels.append(f"[{label}]")
    if len(labels) == 1:
        joined = f"{labels[0]}anull[out]"
    else:
        # The only multi-range scene removes a spoken false start. A tiny
        # crossfade avoids a waveform click without creating an audible echo.
        joined = f"{labels[0]}{labels[1]}acrossfade=d=0.05:c1=tri:c2=tri[out]"
    return ";".join([*pieces, joined])


def render(source: Path, output: Path) -> dict:
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            raise SystemExit(f"missing required tool: {tool}")
    if not source.exists():
        raise SystemExit(f"voiceover not found: {source}")

    build = output.parent / "voiceover_build"
    if build.exists():
        shutil.rmtree(build)
    build.mkdir(parents=True)
    videos: list[Path] = []
    audios: list[Path] = []
    timeline: list[dict] = []
    cursor = 0.0

    for index, (scene, ranges) in enumerate(zip(SCENES, CUTS)):
        stem = f"scene-{index + 1:02d}"
        image_path = build / f"{stem}.png"
        audio_path = build / f"{stem}.wav"
        video_path = build / f"{stem}.mp4"
        render_scene(index, scene).save(image_path, quality=95)
        run([
            "ffmpeg", "-loglevel", "error", "-y", "-i", str(source),
            "-filter_complex", audio_filter(ranges), "-map", "[out]",
            "-c:a", "pcm_s24le", str(audio_path),
        ])
        seconds = duration(audio_path)
        run([
            "ffmpeg", "-loglevel", "error", "-y", "-loop", "1",
            "-framerate", "30", "-i", str(image_path),
            "-t", f"{seconds:.6f}", "-c:v", "libx264", "-preset", "medium",
            "-crf", "19", "-tune", "stillimage", "-pix_fmt", "yuv420p",
            "-an", "-movflags", "+faststart",
            str(video_path),
        ])
        videos.append(video_path)
        audios.append(audio_path)
        timeline.append({
            "scene": index + 1,
            "start_seconds": round(cursor, 3),
            "end_seconds": round(cursor + seconds, 3),
            "duration_seconds": round(seconds, 3),
        })
        cursor += seconds

    concat = build / "segments.txt"
    concat.write_text("".join(f"file '{path.name}'\n" for path in videos))
    audio_concat = build / "audio-segments.txt"
    audio_concat.write_text("".join(f"file '{path.name}'\n" for path in audios))
    output.parent.mkdir(parents=True, exist_ok=True)
    raw_video = build / "final-video.mp4"
    run([
        "ffmpeg", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
        "-i", str(concat), "-c", "copy", "-movflags", "+faststart",
        str(raw_video),
    ])
    # Keep the voice as one continuous recording. One gentle, global cleanup
    # avoids the loudness jumps and room-noise pumping caused by processing each
    # slide independently. The denoiser is intentionally conservative.
    run([
        "ffmpeg", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
        "-i", str(audio_concat), "-i", str(raw_video),
        "-map", "1:v:0", "-map", "0:a:0", "-c:v", "copy",
        "-af", (
            "highpass=f=70,afftdn=nr=6:nf=-45,"
            "loudnorm=I=-16:LRA=7:TP=-1.5,aresample=48000"
        ),
        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
        "-metadata", "title=Catalyst Surface Agent — Final Demo",
        "-metadata", "artist=Yar + Starboi", str(output),
    ])
    result = {
        "output": str(output),
        "source": str(source),
        "duration_seconds": round(duration(output), 3),
        "resolution": "1920x1080",
        "scenes": timeline,
    }
    manifest = output.with_suffix(".json")
    manifest.write_text(json.dumps(result, indent=2) + "\n")
    shutil.rmtree(build)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("voiceover", type=Path)
    parser.add_argument(
        "--output", type=Path,
        default=ROOT / "reports" / "catalyst_surface_agent_final.mp4")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    print(json.dumps(render(args.voiceover.resolve(), args.output.resolve()),
                     indent=2))
