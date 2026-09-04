#!/usr/bin/env python3
"""Render the product-first Catalyst Surface Agent submission deck."""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from render_demo_video import (
    AMBER,
    CYAN,
    GREEN,
    HEIGHT,
    MUTED,
    PANEL,
    PANEL_2,
    PURPLE,
    RED,
    ROOT,
    WHITE,
    WIDTH,
    base_slide,
    fit_lines,
    rounded,
    text,
)


COVER = ROOT / "docs" / "assets" / "catalyst_surface_agent_cover.png"

SLIDES = [
    ("", ""),
    ("THE PRODUCT", "A complete autonomous loop\nfor scheduled market events"),
    ("WEEKLY DISCOVERY", "One engine searches the market\nevery week"),
    ("BOUNDED INTELLIGENCE", "Featherless interprets.\nPolicy holds the keys"),
    ("BROKER LIFECYCLE", "Alpaca MCP closes the loop"),
    ("AUTONOMOUS EXECUTION", "Live gates run every minute\nuntil the account is flat"),
    ("VERIFIABLE BY DESIGN", "Judges can inspect every claim"),
    ("MEASURED DEPLOYMENT", "The engine found the move.\nPricing is the next edge"),
]


def label_box(draw: ImageDraw.ImageDraw, box, heading: str, body: str,
              accent=CYAN, number: str | None = None) -> None:
    x1, y1, x2, y2 = box
    rounded(draw, box, fill=PANEL, outline=accent)
    if number:
        text(draw, (x1 + 28, y1 + 24), number, 42, color=accent, bold=True)
        top = y1 + 88
    else:
        top = y1 + 32
    text(draw, (x1 + 30, top), heading, 24, color=accent, bold=True)
    wrapped = fit_lines(draw, body, x2 - x1 - 60, 22)
    text(draw, (x1 + 30, top + 55), wrapped, 22, color=WHITE, spacing=8)


def slide_two(index: int) -> Image.Image:
    scene = {"kicker": SLIDES[index][0], "title": SLIDES[index][1]}
    image, draw = base_slide(index, scene)
    items = [
        ("01", "DISCOVER", "Scan liquid names for scheduled event convexity", CYAN),
        ("02", "VERIFY", "Confirm the event and replay its own option history", PURPLE),
        ("03", "EXECUTE", "Apply live surface gates and exact maximum loss", GREEN),
        ("04", "PROVE", "Reconcile the broker and seal the evidence trail", AMBER),
    ]
    for i, (number, heading, body, accent) in enumerate(items):
        x = 80 + i * 440
        label_box(draw, (x, 500, x + 400, 835), heading, body, accent, number)
    text(draw, (960, 910),
         "The same agent discovers, decides, trades, checks its work, and records the result.",
         25, color=MUTED, anchor="ma")
    return image


def slide_three(index: int) -> Image.Image:
    scene = {"kicker": SLIDES[index][0], "title": SLIDES[index][1]}
    image, draw = base_slide(index, scene)
    stages = [
        ("64", "liquid names"), ("31", "usable surfaces"),
        ("9", "event-like"), ("6", "dated events"), ("1", "sealed plan"),
    ]
    for i, (count, label) in enumerate(stages):
        x = 80 + i * 356
        accent = GREEN if i == 4 else CYAN
        rounded(draw, (x, 505, x + 320, 735), fill=PANEL, outline=accent)
        text(draw, (x + 160, 548), count, 72, color=accent, bold=True,
             anchor="ma")
        text(draw, (x + 160, 655), label, 22, color=WHITE, anchor="ma")
        if i < 4:
            draw.line((x + 320, 620, x + 345, 620), fill=MUTED, width=4)
    rounded(draw, (260, 795, 1660, 910), fill=PANEL_2)
    text(draw, (960, 830),
         "AVGO survived this week. A different event or an empty plan can survive next week.",
         28, color=WHITE, bold=True, anchor="ma")
    return image


def slide_four(index: int) -> Image.Image:
    scene = {"kicker": SLIDES[index][0], "title": SLIDES[index][1]}
    image, draw = base_slide(index, scene)
    rounded(draw, (80, 485, 900, 895), fill=PANEL, outline=PURPLE)
    text(draw, (125, 525), "MODEL AUTHORITY", 23, color=PURPLE, bold=True)
    allowed = [
        "Check whether the catalyst remains intact",
        "Return typed direction, novelty, and confidence",
        "Require source-grounded committee agreement",
        "Veto risk when evidence is missing or malformed",
    ]
    for i, line in enumerate(allowed):
        draw.ellipse((126, 610 + i * 63, 142, 626 + i * 63), fill=PURPLE)
        text(draw, (170, 597 + i * 63), line, 23)
    rounded(draw, (940, 485, 1840, 895), fill=(31, 27, 47), outline=RED)
    text(draw, (985, 525), "POLICY BOUNDARY", 23, color=RED, bold=True)
    denied = [
        "Cannot invent a ticker or contract",
        "Cannot enlarge quantity or maximum loss",
        "Cannot relax price, freshness, or liquidity gates",
        "Cannot delay the fixed deadline exit",
    ]
    for i, line in enumerate(denied):
        text(draw, (990, 596 + i * 63), "LOCK", 18, color=RED, bold=True)
        text(draw, (1070, 597 + i * 63), line, 23)
    return image


def slide_five(index: int) -> Image.Image:
    scene = {"kicker": SLIDES[index][0], "title": SLIDES[index][1]}
    image, draw = base_slide(index, scene)
    cx, cy = 960, 690
    draw.ellipse((780, 510, 1140, 870), fill=(24, 48, 69), outline=CYAN, width=4)
    text(draw, (cx, 620), "ALPACA", 35, color=CYAN, bold=True, anchor="ma")
    text(draw, (cx, 675), "MCP", 64, color=WHITE, bold=True, anchor="ma")
    text(draw, (cx, 770), "broker truth", 22, color=MUTED, anchor="ma")
    items = [
        (135, 500, "ACCOUNT + CLOCK", "Know capital and market state"),
        (135, 700, "NEWS + STOCK DATA", "Ground the live event"),
        (135, 900, "OPTION CHAINS", "Select executable contracts"),
        (1325, 500, "HISTORICAL OPTIONS", "Replay candidate outcomes"),
        (1325, 700, "ORDERS + FILLS", "Execute and track intent"),
        (1325, 900, "POSITIONS + EQUITY", "Reconcile and verify flat"),
    ]
    for x, y, heading, body in items:
        box_y = y - 92
        rounded(draw, (x, box_y, x + 460, box_y + 145), fill=PANEL,
                outline=(47, 64, 98), radius=22)
        text(draw, (x + 24, box_y + 22), heading, 19, color=CYAN, bold=True)
        text(draw, (x + 24, box_y + 65), body, 20, color=WHITE)
        if x < cx:
            draw.line((x + 460, box_y + 72, 780, cy), fill=(64, 91, 131), width=3)
        else:
            draw.line((1140, cy, x, box_y + 72), fill=(64, 91, 131), width=3)
    return image


def slide_six(index: int) -> Image.Image:
    scene = {"kicker": SLIDES[index][0], "title": SLIDES[index][1]}
    image, draw = base_slide(index, scene)
    steps = [
        ("SEALED PLAN", "immutable input", AMBER),
        ("LIVE SURFACE", "fresh quotes", CYAN),
        ("RISK PLAN", "exact max loss", PURPLE),
        ("ORDER", "idempotent", GREEN),
        ("RECONCILE", "broker position", CYAN),
        ("EXIT", "market clock", GREEN),
    ]
    for i, (heading, body, accent) in enumerate(steps):
        x = 80 + i * 293
        rounded(draw, (x, 535, x + 250, 735), fill=PANEL, outline=accent)
        text(draw, (x + 125, 584), heading, 20, color=accent, bold=True,
             anchor="ma")
        text(draw, (x + 125, 656), body, 20, color=WHITE, anchor="ma")
        if i < 5:
            draw.line((x + 250, 635, x + 280, 635), fill=MUTED, width=4)
    rounded(draw, (250, 790, 1670, 915), fill=(23, 47, 55), outline=GREEN)
    text(draw, (960, 820), "FAIL CLOSED", 20, color=GREEN, bold=True,
         anchor="ma")
    text(draw, (960, 860),
         "Stale data, unknown order state, or failed reconciliation removes permission to trade.",
         25, color=WHITE, anchor="ma")
    return image


def slide_seven(index: int) -> Image.Image:
    scene = {"kicker": SLIDES[index][0], "title": SLIDES[index][1]}
    image, draw = base_slide(index, scene)
    label_box(draw, (80, 500, 610, 865), "LIVE DASHBOARD",
              "Selection funnel, model committee, current plan, broker lifecycle, and P&L.",
              CYAN, "01")
    label_box(draw, (695, 500, 1225, 865), "HASH CHAIN",
              "Every decision is secret-redacted and tamper-evident. Anyone can recompute it.",
              PURPLE, "02")
    label_box(draw, (1310, 500, 1840, 865), "FAILURE DRILLS",
              "Eight of eight rehearsal groups passed, including stale data and model failure.",
              GREEN, "03")
    text(draw, (960, 925),
         "The evidence trail records rejected ideas as clearly as executed orders.",
         24, color=MUTED, anchor="ma")
    return image


def slide_eight(index: int) -> Image.Image:
    scene = {"kicker": SLIDES[index][0], "title": SLIDES[index][1]}
    image, draw = base_slide(index, scene)
    metrics = [
        ("MOVE", "-6.17%", "entry to exit", GREEN),
        ("PREMIUM", "8.04%", "of spot paid", AMBER),
        ("EXIT", "09:45:18", "18 sec after target", CYAN),
        ("ACCOUNT", "-10.70%", "measured paper result", RED),
    ]
    for i, (heading, value, note, accent) in enumerate(metrics):
        x = 80 + i * 440
        rounded(draw, (x, 470, x + 400, 655), fill=PANEL, outline=(47, 64, 98))
        text(draw, (x + 25, 495), heading, 17, color=MUTED, bold=True)
        text(draw, (x + 25, 535), value, 39, color=accent, bold=True)
        text(draw, (x + 25, 595), note, 18, color=MUTED)
    upgrades = [
        ("VALUE MARGIN", "Trade only when conservative liquidation value clears the marketable debit."),
        ("UNCERTAINTY SIZE", "Reduce allocation as premium and confidence uncertainty increase."),
        ("STRUCTURE CHOICE", "Use capped convexity for moderate moves, or remain flat."),
    ]
    for i, (heading, body) in enumerate(upgrades):
        x = 80 + i * 586
        rounded(draw, (x, 705, x + 540, 905), fill=PANEL_2,
                outline=[CYAN, PURPLE, GREEN][i])
        text(draw, (x + 25, 735), heading, 19,
             color=[CYAN, PURPLE, GREEN][i], bold=True)
        text(draw, (x + 25, 785), fit_lines(draw, body, 485, 20),
             20, color=WHITE, spacing=6)
    text(draw, (960, 950),
         "github.com/yarneo/catalyst-surface-agent     catalyst-surface-agent.streamlit.app",
         20, color=MUTED, anchor="ma")
    return image


RENDERERS = {
    1: slide_two,
    2: slide_three,
    3: slide_four,
    4: slide_five,
    5: slide_six,
    6: slide_seven,
    7: slide_eight,
}


def render(output: Path, pages_dir: Path) -> None:
    if not COVER.exists():
        raise SystemExit(f"cover image missing: {COVER}")
    pages_dir.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    pages: list[Path] = []
    cover = Image.open(COVER).convert("RGB").resize((WIDTH, HEIGHT))
    cover_path = pages_dir / "slide-01.png"
    cover.save(cover_path)
    pages.append(cover_path)
    for index in range(1, 8):
        page = RENDERERS[index](index)
        page_path = pages_dir / f"slide-{index + 1:02d}.png"
        page.save(page_path)
        pages.append(page_path)

    page_size = (13.333333 * 72, 7.5 * 72)
    pdf = canvas.Canvas(str(output), pagesize=page_size, pageCompression=1)
    for page_path in pages:
        pdf.drawImage(ImageReader(str(page_path)), 0, 0,
                      width=page_size[0], height=page_size[1])
        pdf.showPage()
    pdf.save()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output", type=Path,
        default=ROOT / "output" / "pdf" / "catalyst_surface_agent_pitch_deck_v2.pdf")
    parser.add_argument(
        "--pages-dir", type=Path,
        default=ROOT / "tmp" / "pdfs" / "catalyst-pitch-v2")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    render(args.output.resolve(), args.pages_dir.resolve())
    print(args.output.resolve())
