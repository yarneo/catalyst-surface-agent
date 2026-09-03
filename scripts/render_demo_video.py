#!/usr/bin/env python3
"""Render the measured-result demo as a narrated 1080p MP4 on macOS.

The output lives under reports/, which is intentionally ignored by Git. The
script contains no account access and reads no mutable runtime data.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parent.parent
WIDTH, HEIGHT = 1920, 1080
FONT_REGULAR = Path("/System/Library/Fonts/SFNS.ttf")
FONT_BOLD = Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf")

BG_TOP = (9, 13, 27)
BG_BOTTOM = (16, 24, 45)
PANEL = (24, 33, 57)
PANEL_2 = (30, 42, 70)
WHITE = (242, 246, 255)
MUTED = (163, 177, 205)
CYAN = (69, 208, 255)
PURPLE = (154, 119, 255)
GREEN = (75, 216, 154)
RED = (255, 103, 124)
AMBER = (255, 195, 92)


SCENES = [
    {
        "kicker": "MEASURED DEPLOYMENT · FINAL",
        "title": "A real autonomous run.\nA real loss. A better agent.",
        "narration": (
            "This is Catalyst Surface Agent, built by Yar and Starboi. It is an "
            "autonomous options system for scheduled market events. This run lost "
            "ten point seven zero percent. We are leading with that number because "
            "auditability is part of the product, not a slide added afterward. The "
            "agent completed the full lifecycle and finished flat at the broker."
        ),
    },
    {
        "kicker": "ONE REUSABLE WEEKLY ENGINE",
        "title": "AVGO was selected—\nnot hard-coded.",
        "narration": (
            "This is not an A V G O only bot. Before the week, the same engine "
            "scanned sixty-four liquid names, measured thirty-one usable option "
            "surfaces, found nine event-like term structures, verified six dated "
            "candidates, and promoted one plan. A later week can select a different "
            "event, or correctly seal an empty plan when the evidence is weak."
        ),
    },
    {
        "kicker": "BOUNDED AI · BROKER TRUTH",
        "title": "AI interprets.\nCode controls risk.",
        "narration": (
            "Featherless runs multiple models concurrently and requires typed, "
            "source-grounded agreement. It can veto an entry when an event leaked, "
            "changed, or already resolved. It cannot invent a ticker, enlarge size, "
            "weaken a price limit, or delay the exit. Alpaca M C P supplies account, "
            "clock, news, market data, orders, positions, and final portfolio truth."
        ),
    },
    {
        "kicker": "AUTONOMOUS LIFECYCLE",
        "title": "Discover. Seal. Execute.\nReconcile. Prove.",
        "narration": (
            "Weekly intelligence discovers events, verifies independent calendars, "
            "replays historical options, and cryptographically seals a plan before "
            "measurement. Then a one-minute supervisor repeats every live gate, "
            "sizes exact maximum loss, submits idempotent multi-leg orders, reconciles "
            "broker exposure, and exits on the market clock. Missing or malformed "
            "evidence removes risk; it never creates permission to trade."
        ),
    },
    {
        "kicker": "WHAT ACTUALLY TRADED",
        "title": "13 straddles in.\nFlat on schedule.",
        "narration": (
            "Fresh entry checks passed on Wednesday. The agent bought thirteen near "
            "the money September fourth straddles at the three sixty-seven fifty "
            "strike, for a combined debit of twenty-nine dollars and sixty cents. "
            "That deployed thirty-eight thousand four hundred eighty dollars. The "
            "fixed exit filled Thursday at nine forty-five and eighteen seconds, for "
            "a twenty-one dollar and thirty-seven cent credit."
        ),
    },
    {
        "kicker": "THE STRATEGY LESSON",
        "title": "The gate passed.\nThe evidence did not extend that far.",
        "narration": (
            "The loss revealed what the headline backtest hid. Entry premium was "
            "eight point zero five percent of spot, below our frozen eight point five "
            "percent ceiling, but more expensive than every accepted historical "
            "event. We extrapolated beyond direct support and allocated too much from "
            "only eight observations. The twenty-seven point eight percent trade loss "
            "became a ten point seven percent account loss."
        ),
    },
    {
        "kicker": "FAILURES BECAME TESTS",
        "title": "The exit worked.\nThe audit found everything else.",
        "narration": (
            "The live run also exposed bounded clock skew, a model-cache edge case, "
            "exit retry semantics, a stale dashboard, and a post-exit phantom registry "
            "row. Close-only order intent prevented that local defect from creating "
            "reverse exposure. Each failure now has a regression test. The repaired "
            "supervisor reaches D O N E with final equity of eighty-nine thousand two "
            "hundred ninety-nine dollars and thirty cents, and zero open positions."
        ),
    },
    {
        "kicker": "VERSION 2",
        "title": "Require value.\nSize uncertainty. Accept no trade.",
        "narration": (
            "Version two does not replace eight point five percent with another "
            "arbitrary threshold. It trades only when a conservative estimate of "
            "next-session liquidation value clears the current marketable debit, "
            "spread, and uncertainty. Size falls as price and uncertainty rise. "
            "Moderate moves use capped convexity, or no trade. The lasting result is "
            "a reusable agent that can discover, decide, execute, reconcile, learn, "
            "and prove exactly what happened."
        ),
    },
]


def font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    path = FONT_BOLD if bold else FONT_REGULAR
    return ImageFont.truetype(str(path), size)


def background() -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT))
    pixels = image.load()
    for y in range(HEIGHT):
        ratio = y / (HEIGHT - 1)
        color = tuple(round(a + (b - a) * ratio)
                      for a, b in zip(BG_TOP, BG_BOTTOM))
        for x in range(WIDTH):
            glow = max(0.0, 1.0 - math.hypot(x - 1580, y - 160) / 950)
            pixels[x, y] = tuple(min(255, round(c + glow * v))
                                 for c, v in zip(color, (5, 9, 18)))
    return image


def rounded(draw: ImageDraw.ImageDraw, box, *, fill=PANEL, outline=None,
            radius=28, width=2):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline,
                           width=width)


def text(draw: ImageDraw.ImageDraw, xy, value: str, size: int, *, color=WHITE,
         bold=False, anchor=None, spacing=8):
    draw.multiline_text(xy, value, fill=color, font=font(size, bold=bold),
                        anchor=anchor, spacing=spacing)


def fit_lines(draw: ImageDraw.ImageDraw, value: str, max_width: int,
              size: int, *, bold=False) -> str:
    face = font(size, bold=bold)
    lines: list[str] = []
    for paragraph in value.splitlines():
        words = paragraph.split()
        line = ""
        for word in words:
            candidate = f"{line} {word}".strip()
            if draw.textlength(candidate, font=face) <= max_width:
                line = candidate
            else:
                if line:
                    lines.append(line)
                line = word
        lines.append(line)
    return "\n".join(lines)


def base_slide(index: int, scene: dict) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = background()
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((80, 66, 98, 120), radius=9, fill=CYAN)
    text(draw, (124, 75), scene["kicker"], 25, color=CYAN, bold=True)
    text(draw, (1818, 75), f"0{index + 1} / 08", 22, color=MUTED,
         anchor="ra")
    text(draw, (80, 150), scene["title"], 68, bold=True, spacing=5)
    draw.line((80, 1000, 1840, 1000), fill=(56, 70, 103), width=2)
    text(draw, (80, 1020), "CATALYST SURFACE AGENT", 18, color=MUTED,
         bold=True)
    text(draw, (1840, 1020), "YAR + STARBOI", 18, color=MUTED, bold=True,
         anchor="ra")
    return image, draw


def metric(draw, x, y, w, title, value, note, accent=CYAN):
    rounded(draw, (x, y, x + w, y + 190), fill=PANEL, outline=(47, 64, 98))
    text(draw, (x + 28, y + 25), title.upper(), 18, color=MUTED, bold=True)
    text(draw, (x + 28, y + 64), value, 43, color=accent, bold=True)
    text(draw, (x + 28, y + 130), note, 20, color=MUTED)


def render_scene(index: int, scene: dict) -> Image.Image:
    image, draw = base_slide(index, scene)
    if index == 0:
        metric(draw, 80, 520, 400, "Final equity", "$89,299.30",
               "from $100,000", RED)
        metric(draw, 505, 520, 400, "Account return", "-10.70%",
               "measured, realized", RED)
        metric(draw, 930, 520, 400, "Broker state", "FLAT",
               "zero open positions", GREEN)
        metric(draw, 1355, 520, 485, "Exit", "09:45:18 ET",
               "18 seconds after target", GREEN)
        rounded(draw, (80, 750, 1840, 910), fill=(35, 28, 48),
                outline=(110, 67, 91))
        text(draw, (120, 785), "THE POINT", 18, color=RED, bold=True)
        text(draw, (120, 825),
             "The system did not win the trade. It did complete the lifecycle, "
             "preserve truth, and produce a better falsifiable design.",
             29, color=WHITE)
    elif index == 1:
        stages = [("64", "liquid names"), ("31", "usable surfaces"),
                  ("9", "event-like"), ("6", "dated events"),
                  ("1", "sealed plan")]
        start_x, y, gap, card_w = 80, 520, 36, 320
        for i, (count, label) in enumerate(stages):
            x = start_x + i * (card_w + gap)
            accent = GREEN if i == len(stages) - 1 else CYAN
            rounded(draw, (x, y, x + card_w, y + 220), fill=PANEL,
                    outline=accent)
            text(draw, (x + card_w / 2, y + 40), count, 74, color=accent,
                 bold=True, anchor="ma")
            text(draw, (x + card_w / 2, y + 145), label, 23, color=WHITE,
                 anchor="ma")
            if i < len(stages) - 1:
                draw.polygon([(x + card_w + 8, y + 110),
                              (x + card_w + 25, y + 100),
                              (x + card_w + 25, y + 120)], fill=MUTED)
        rounded(draw, (80, 790, 1840, 910), fill=PANEL_2)
        text(draw, (120, 825),
             "A GENERAL ENGINE  →  a different event next week  →  or no trade",
             30, color=WHITE, bold=True)
    elif index == 2:
        rounded(draw, (80, 500, 890, 900), fill=PANEL, outline=PURPLE)
        text(draw, (125, 535), "FEATHERLESS COMMITTEE", 24,
             color=PURPLE, bold=True)
        for y, line in enumerate([
                "Grounded event interpretation",
                "Typed multi-model quorum",
                "May veto risk",
                "Cannot create or enlarge a trade"]):
            draw.ellipse((130, 615 + y * 60, 146, 631 + y * 60), fill=PURPLE)
            text(draw, (168, 604 + y * 60), line, 25)
        rounded(draw, (930, 500, 1840, 900), fill=PANEL, outline=CYAN)
        text(draw, (975, 535), "ALPACA MCP LIFECYCLE", 24,
             color=CYAN, bold=True)
        for y, line in enumerate([
                "Account + market clock",
                "News + stock + option data",
                "Orders + reconciliation",
                "Positions + final portfolio equity"]):
            draw.ellipse((980, 615 + y * 60, 996, 631 + y * 60), fill=CYAN)
            text(draw, (1018, 604 + y * 60), line, 25)
    elif index == 3:
        labels = [("DISCOVER", "events"), ("VALIDATE", "evidence"),
                  ("SEAL", "policy"), ("EXECUTE", "orders"),
                  ("RECONCILE", "broker"), ("PROVE", "hash chain")]
        y = 550
        for i, (top, bottom) in enumerate(labels):
            x = 80 + i * 293
            accent = [CYAN, PURPLE, AMBER, CYAN, GREEN, PURPLE][i]
            rounded(draw, (x, y, x + 245, y + 190), fill=PANEL,
                    outline=accent)
            text(draw, (x + 122, y + 48), top, 24, color=accent,
                 bold=True, anchor="ma")
            text(draw, (x + 122, y + 115), bottom, 23, color=WHITE,
                 anchor="ma")
            if i < len(labels) - 1:
                draw.line((x + 245, y + 95, x + 280, y + 95), fill=MUTED,
                          width=4)
                draw.polygon([(x + 280, y + 95), (x + 268, y + 87),
                              (x + 268, y + 103)], fill=MUTED)
        rounded(draw, (290, 790, 1630, 900), fill=(23, 47, 55),
                outline=GREEN)
        text(draw, (960, 825),
             "FAIL CLOSED: missing evidence removes risk—it never grants it",
             29, color=GREEN, bold=True, anchor="ma")
    elif index == 4:
        metric(draw, 80, 510, 400, "Structure", "LONG STRADDLE",
               "Sep 4 · $367.50 strike", PURPLE)
        metric(draw, 505, 510, 400, "Quantity", "13",
               "call + put pairs", CYAN)
        metric(draw, 930, 510, 400, "Entry debit", "$29.60",
               "$38,480 deployed", AMBER)
        metric(draw, 1355, 510, 485, "Exit credit", "$21.37",
               "09:45:18 ET", RED)
        draw.line((140, 810, 1780, 810), fill=(66, 84, 122), width=6)
        for x, label, color in [(220, "15:26:54\nENTRY", CYAN),
                                (960, "EARNINGS\nEVENT", PURPLE),
                                (1700, "09:45:18\nFLAT", GREEN)]:
            draw.ellipse((x - 18, 792, x + 18, 828), fill=color)
            text(draw, (x, 850), label, 20, color=color, bold=True,
                 anchor="ma", spacing=4)
    elif index == 5:
        rounded(draw, (80, 485, 1160, 900), fill=PANEL,
                outline=(47, 64, 98))
        text(draw, (120, 520), "ACCEPTED HISTORICAL PREMIUM / SPOT", 20,
             color=MUTED, bold=True)
        values = [6.20, 7.60, 6.63, 5.74, 6.65, 7.74, 8.05]
        labels = ["'24 Q3", "'24 Q4", "'25 Q2", "'25 Q3", "'25 Q4",
                  "'26 Q1", "LIVE"]
        baseline = 820
        for i, (value, label) in enumerate(zip(values, labels)):
            x = 135 + i * 140
            height = value * 31
            color = RED if label == "LIVE" else CYAN
            draw.rounded_rectangle((x, baseline - height, x + 78, baseline),
                                   radius=10, fill=color)
            text(draw, (x + 39, baseline - height - 35), f"{value:.2f}%", 18,
                 color=color, bold=True, anchor="ma")
            text(draw, (x + 39, baseline + 22), label, 17, color=MUTED,
                 anchor="ma")
        draw.line((120, baseline, 1120, baseline), fill=(66, 84, 122), width=2)
        rounded(draw, (1210, 485, 1840, 900), fill=(47, 28, 43), outline=RED)
        text(draw, (1255, 535), "THE EXTRAPOLATION", 20, color=RED, bold=True)
        text(draw, (1255, 600), "8.05%", 72, color=RED, bold=True)
        text(draw, (1255, 700), "was under the rule", 25)
        text(draw, (1255, 744), "but above every accepted", 25)
        text(draw, (1255, 788), "historical example.", 25, bold=True)
        text(draw, (1255, 850), "8 events ≠ confidence", 22, color=AMBER)
    elif index == 6:
        rows = [
            ("CLOCK SKEW", "bounded freshness tolerance"),
            ("MODEL CACHE", "fresh typed entry quorum"),
            ("RETRY IDS", "terminal vs unknown semantics"),
            ("STALE UI", "broker truth + final artifact"),
            ("PHANTOM ROW", "terminal recovery invariant"),
        ]
        for i, (failure, fix) in enumerate(rows):
            y = 485 + i * 87
            rounded(draw, (80, y, 650, y + 66), fill=(47, 28, 43))
            text(draw, (115, y + 20), failure, 21, color=RED, bold=True)
            draw.line((690, y + 33, 830, y + 33), fill=MUTED, width=4)
            draw.polygon([(830, y + 33), (816, y + 24), (816, y + 42)],
                         fill=MUTED)
            rounded(draw, (870, y, 1660, y + 66), fill=(23, 47, 55))
            text(draw, (905, y + 20), fix, 23, color=GREEN, bold=True)
            text(draw, (1710, y + 20), "TESTED", 18, color=GREEN, bold=True)
        rounded(draw, (80, 925, 1840, 975), fill=PANEL_2)
        text(draw, (960, 937),
             "CLOSE-ONLY INTENT KEPT BROKER EXPOSURE AT ZERO",
             19, color=GREEN, bold=True, anchor="ma")
    else:
        items = [
            ("1", "EXECUTABLE VALUE MARGIN",
             "Conservative liquidation value must clear debit + costs."),
            ("2", "UNCERTAINTY-AWARE SIZE",
             "Allocation falls as premium and confidence width rise."),
            ("3", "NO-TRADE IS AN OUTCOME",
             "Use capped convexity—or stay flat when value is absent."),
        ]
        for i, (number, heading, body) in enumerate(items):
            x = 80 + i * 586
            rounded(draw, (x, 500, x + 540, 820), fill=PANEL,
                    outline=[CYAN, PURPLE, GREEN][i])
            text(draw, (x + 35, 535), number, 58,
                 color=[CYAN, PURPLE, GREEN][i], bold=True)
            text(draw, (x + 35, 635), heading, 21, color=WHITE, bold=True)
            wrapped = fit_lines(draw, body, 455, 23)
            text(draw, (x + 35, 700), wrapped, 23, color=MUTED, spacing=7)
        text(draw, (960, 885),
             "DISCOVER  ·  DECIDE  ·  EXECUTE  ·  RECONCILE  ·  LEARN",
             27, color=CYAN, bold=True, anchor="ma")
        text(draw, (960, 935), "github.com/yarneo/catalyst-surface-agent",
             21, color=MUTED, anchor="ma")
    return image


def run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def duration(path: Path) -> float:
    output = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ], text=True)
    return float(output.strip())


def render(output: Path, *, voice: str, rate: int, keep_build: bool) -> None:
    for tool in ("say", "ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            raise SystemExit(f"missing required tool: {tool}")

    build = output.parent / "demo_build"
    if build.exists():
        shutil.rmtree(build)
    build.mkdir(parents=True)
    segments: list[Path] = []

    for index, scene in enumerate(SCENES):
        stem = f"scene-{index + 1:02d}"
        image_path = build / f"{stem}.png"
        text_path = build / f"{stem}.txt"
        audio_path = build / f"{stem}.aiff"
        video_path = build / f"{stem}.mp4"
        render_scene(index, scene).save(image_path, quality=95)
        text_path.write_text(scene["narration"] + "\n")
        run(["say", "-v", voice, "-r", str(rate), "-f", str(text_path),
             "-o", str(audio_path)])
        seconds = duration(audio_path) + 0.5
        frames = max(1, round(seconds * 30))
        run([
            "ffmpeg", "-loglevel", "error", "-y",
            "-loop", "1", "-i", str(image_path), "-i", str(audio_path),
            "-filter_complex",
            (f"[0:v]scale=1980:1114,zoompan="
             f"z='min(zoom+0.00010,1.025)':"
             f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
             f"d={frames}:s=1920x1080:fps=30,format=yuv420p[v];"
             f"[1:a]highpass=f=80,lowpass=f=12000,"
             f"loudnorm=I=-16:LRA=7:TP=-1.5,apad=pad_dur=0.5[a]"),
            "-map", "[v]", "-map", "[a]", "-t", f"{seconds:.3f}",
            "-c:v", "libx264", "-preset", "medium", "-crf", "19",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
            str(video_path),
        ])
        segments.append(video_path)

    concat = build / "segments.txt"
    concat.write_text("".join(f"file '{path.name}'\n" for path in segments))
    output.parent.mkdir(parents=True, exist_ok=True)
    run([
        "ffmpeg", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
        "-i", str(concat), "-c", "copy", "-movflags", "+faststart",
        "-metadata", "title=Catalyst Surface Agent — Measured Deployment",
        "-metadata", "artist=Yar + Starboi", str(output),
    ])

    manifest = {
        "output": str(output),
        "duration_seconds": round(duration(output), 3),
        "resolution": f"{WIDTH}x{HEIGHT}",
        "voice": voice,
        "rate": rate,
        "scenes": len(SCENES),
    }
    (output.parent / "catalyst_surface_agent_demo.json").write_text(
        json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))
    if not keep_build:
        shutil.rmtree(build)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output", type=Path,
        default=ROOT / "reports" / "catalyst_surface_agent_demo.mp4")
    parser.add_argument("--voice", default="Samantha")
    parser.add_argument("--rate", type=int, default=180)
    parser.add_argument("--keep-build", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    render(args.output.resolve(), voice=args.voice, rate=args.rate,
           keep_build=args.keep_build)
