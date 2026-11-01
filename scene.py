#!/usr/bin/env python3
"""A living scene for the HX-KEG450 panel: drifting nebula, white telemetry, one small pet.

The keyboard stores an *animation*, so this renders a short loop (background motion
+ a breathing pet) with the current numbers baked in, uploads it once, and refreshes
every minute. The motion keeps running on the device; we only re-upload when the
data changes.

    ./scene.py --preview /tmp/scene.png      # render a contact sheet, touch nothing
    ./scene.py --once                        # push one loop now
    ./scene.py                                # keep refreshing every minute

Background: procedural by default (no assets, no licence questions), or any GIF:

    ./scene.py --background ~/Pictures/loop.gif
"""
from __future__ import annotations

import argparse
import math
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime

import numpy as np
from PIL import Image

import keg450
import live

W, H = keg450.SCREEN_W, keg450.SCREEN_H
FRAMES = 24
FRAME_MS = 110

WHITE = (245, 247, 252)
DIM = (150, 156, 172)
FAINT = (96, 102, 118)


# ----------------------------------------------------------------- 5x7 font --
_GLYPHS = {
    " ": ["     "] * 7,
    "0": [" ### ", "#   #", "#  ##", "# # #", "##  #", "#   #", " ### "],
    "1": ["  #  ", " ##  ", "  #  ", "  #  ", "  #  ", "  #  ", " ### "],
    "2": [" ### ", "#   #", "    #", "   # ", "  #  ", " #   ", "#####"],
    "3": ["#####", "   # ", "  #  ", "   # ", "    #", "#   #", " ### "],
    "4": ["   # ", "  ## ", " # # ", "#  # ", "#####", "   # ", "   # "],
    "5": ["#####", "#    ", "#### ", "    #", "    #", "#   #", " ### "],
    "6": ["  ## ", " #   ", "#    ", "#### ", "#   #", "#   #", " ### "],
    "7": ["#####", "    #", "   # ", "  #  ", " #   ", " #   ", " #   "],
    "8": [" ### ", "#   #", "#   #", " ### ", "#   #", "#   #", " ### "],
    "9": [" ### ", "#   #", "#   #", " ####", "    #", "   # ", " ##  "],
    ":": ["     ", "  #  ", "     ", "     ", "  #  ", "     ", "     "],
    ".": ["     ", "     ", "     ", "     ", "     ", "     ", "  #  "],
    "%": ["##  #", "##  #", "   # ", "  #  ", " #   ", "#  ##", "   ##"],
    "/": ["    #", "    #", "   # ", "  #  ", " #   ", "#    ", "#    "],
    "-": ["     ", "     ", "     ", "#####", "     ", "     ", "     "],
    "A": [" ### ", "#   #", "#   #", "#####", "#   #", "#   #", "#   #"],
    "B": ["#### ", "#   #", "#   #", "#### ", "#   #", "#   #", "#### "],
    "C": [" ####", "#    ", "#    ", "#    ", "#    ", "#    ", " ####"],
    "D": ["#### ", "#   #", "#   #", "#   #", "#   #", "#   #", "#### "],
    "E": ["#####", "#    ", "#    ", "#### ", "#    ", "#    ", "#####"],
    "F": ["#####", "#    ", "#    ", "#### ", "#    ", "#    ", "#    "],
    "G": [" ####", "#    ", "#    ", "#  ##", "#   #", "#   #", " ####"],
    "H": ["#   #", "#   #", "#   #", "#####", "#   #", "#   #", "#   #"],
    "I": [" ### ", "  #  ", "  #  ", "  #  ", "  #  ", "  #  ", " ### "],
    "J": ["   ##", "    #", "    #", "    #", "#   #", "#   #", " ### "],
    "K": ["#   #", "#  # ", "# #  ", "##   ", "# #  ", "#  # ", "#   #"],
    "L": ["#    ", "#    ", "#    ", "#    ", "#    ", "#    ", "#####"],
    "M": ["#   #", "## ##", "# # #", "#   #", "#   #", "#   #", "#   #"],
    "N": ["#   #", "##  #", "# # #", "#  ##", "#   #", "#   #", "#   #"],
    "O": [" ### ", "#   #", "#   #", "#   #", "#   #", "#   #", " ### "],
    "P": ["#### ", "#   #", "#   #", "#### ", "#    ", "#    ", "#    "],
    "Q": [" ### ", "#   #", "#   #", "#   #", "# # #", "#  # ", " ## #"],
    "R": ["#### ", "#   #", "#   #", "#### ", "# #  ", "#  # ", "#   #"],
    "S": [" ####", "#    ", "#    ", " ### ", "    #", "    #", "#### "],
    "T": ["#####", "  #  ", "  #  ", "  #  ", "  #  ", "  #  ", "  #  "],
    "U": ["#   #", "#   #", "#   #", "#   #", "#   #", "#   #", " ### "],
    "V": ["#   #", "#   #", "#   #", "#   #", "#   #", " # # ", "  #  "],
    "W": ["#   #", "#   #", "#   #", "#   #", "# # #", "## ##", "#   #"],
    "X": ["#   #", "#   #", " # # ", "  #  ", " # # ", "#   #", "#   #"],
    "Y": ["#   #", "#   #", " # # ", "  #  ", "  #  ", "  #  ", "  #  "],
    "Z": ["#####", "    #", "   # ", "  #  ", " #   ", "#    ", "#####"],
}


def text_width(text: str, scale: int = 1, tracking: int = 1) -> int:
    if not text:
        return 0
    return len(text) * 6 * scale - tracking * scale


def draw_text(px: np.ndarray, x: int, y: int, text: str, colour, scale: int = 1) -> int:
    for index, char in enumerate(text.upper()):
        glyph = _GLYPHS.get(char, _GLYPHS[" "])
        ox = x + index * 6 * scale
        for row, line in enumerate(glyph):
            for col, cell in enumerate(line):
                if cell != "#":
                    continue
                y0 = y + row * scale
                x0 = ox + col * scale
                if 0 <= y0 < H - scale + 1 and 0 <= x0 < W - scale + 1:
                    px[y0:y0 + scale, x0:x0 + scale] = colour
    return x + text_width(text, scale)


# ------------------------------------------------------------- backgrounds --
def _value_noise(shape, cells: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    grid = rng.random((cells, int(cells * shape[1] / shape[0])))
    image = Image.fromarray((grid * 255).astype(np.uint8)).resize(
        (shape[1], shape[0]), Image.BICUBIC
    )
    return np.asarray(image, dtype=np.float32) / 255.0


def vignette() -> np.ndarray:
    ys = np.linspace(-1, 1, H)[:, None]
    xs = np.linspace(-1, 1, W)[None, :]
    radius = np.sqrt(xs ** 2 + ys ** 2) / math.sqrt(2)
    return np.clip(1.15 - 0.55 * radius, 0.35, 1.0)[..., None].astype(np.float32)


def starfield(seed: int = 3, count: int = 26) -> np.ndarray:
    rng = np.random.default_rng(seed)
    stars = np.zeros((H, W, 1), dtype=np.float32)
    for _ in range(count):
        x, y = rng.integers(0, W), rng.integers(0, H)
        stars[y, x, 0] = rng.uniform(40, 120)
    return stars


def nebula_background(frames: int = FRAMES) -> list[np.ndarray]:
    """Slow indigo/violet smoke, generated rather than downloaded."""
    base = []
    octaves = [(5, 1.0), (11, 0.55), (23, 0.3), (47, 0.15)]
    for seed in range(3):
        layer = np.zeros((H, W), dtype=np.float32)
        for cells, weight in octaves:
            layer += weight * _value_noise((H, W), cells, seed * 7 + cells)
        base.append(layer / sum(weight for _, weight in octaves))

    # domain warp: push the field around with a second noise, animated over time
    warp = _value_noise((H, W), 8, 99)
    frames_out = []
    for index in range(frames):
        phase = index / frames * 2 * math.pi
        shift_x = int(6 * math.sin(phase))
        shift_y = int(3 * math.cos(phase * 0.7))
        field = np.roll(np.roll(base[0], shift_x, axis=1), shift_y, axis=0)
        field = 0.75 * field + 0.35 * np.roll(base[1], -shift_x, axis=1)
        field = field * (0.75 + 0.25 * warp)

        # deep indigo -> violet -> a whisper of rose, never bright enough to fight text
        v = np.clip(field, 0, 1)[..., None]
        cold = np.array([10, 12, 28], dtype=np.float32)
        mid = np.array([46, 34, 96], dtype=np.float32)
        warm = np.array([92, 52, 120], dtype=np.float32)
        lower = cold + (mid - cold) * np.clip(v * 1.6, 0, 1)
        upper = mid + (warm - mid) * np.clip((v - 0.55) * 2.2, 0, 1)
        rgb = np.where(v > 0.55, upper, lower) * vignette() + starfield()
        frames_out.append(np.clip(rgb, 0, 255).astype(np.uint8))
    return frames_out


def gif_background(path: str, frames: int = FRAMES) -> list[np.ndarray]:
    """Any GIF, centre-cropped to 5:3, darkened and desaturated so white text reads."""
    source = Image.open(path)
    total = getattr(source, "n_frames", 1)
    out = []
    for index in range(frames):
        source.seek(int(index * total / frames) % max(total, 1))
        frame = source.convert("RGB")
        scale = max(W / frame.width, H / frame.height)
        resized = frame.resize((max(W, int(frame.width * scale)), max(H, int(frame.height * scale))), Image.LANCZOS)
        left = (resized.width - W) // 2
        top = (resized.height - H) // 2
        crop = resized.crop((left, top, left + W, top + H))
        arr = np.asarray(crop, dtype=np.float32)
        grey = arr.mean(axis=2, keepdims=True)
        arr = 0.7 * arr + 0.3 * grey            # soften, keep the colour
        arr = arr * np.array([0.92, 0.94, 1.12])  # lean it toward indigo
        arr = arr * vignette() * 0.5            # dim hard: the text must win
        out.append(np.clip(arr, 0, 255).astype(np.uint8))
    return out


def scrim(frame: np.ndarray, top: int = 34) -> np.ndarray:
    """Darken the top band so the telemetry reads over any background."""
    out = frame.astype(np.float32)
    for y in range(H):
        factor = 0.62 if y < top else 0.0
        if y >= top:
            factor = max(0.0, 0.62 * (1 - (y - top) / 16))
        out[y] *= 1.0 - factor
    return np.clip(out, 0, 255).astype(np.uint8)


# --------------------------------------------------------------------- pet ---
def draw_pet(px: np.ndarray, x: int, y: int, frame: int, mood: str) -> None:
    """A 12x12 blob. It breathes, blinks, and gets wide-eyed when the CPU is hot."""
    bob = 1 if (frame // 6) % 2 == 0 else 0
    y += bob
    body = (230, 236, 248)
    shade = (150, 158, 178)
    eye = (20, 22, 30)

    # soft shadow so it sits on the scene
    for col in range(3, 10):
        px[y + 8, x + col] = (28, 30, 42)
    # ears
    for col in (2, 3, 8, 9):
        px[y, x + col] = body
    px[y - 1, x + 2] = body
    px[y - 1, x + 9] = body
    # tail nub
    px[y + 6, x + 11] = body
    px[y + 5, x + 11] = shade

    for row in range(8):
        for col in range(12):
            dx = (col - 5.5) / 5.8
            dy = (row - 3.5) / 4.2
            if dx * dx + dy * dy <= 1.0:
                px[y + row, x + col] = body
    # flat bottom
    for col in range(2, 10):
        px[y + 7, x + col] = shade

    blink = (frame % 22) in (0, 1)
    if blink:
        for col in (3, 8):
            px[y + 3, x + col] = shade
    elif mood == "hot":
        # worried: taller eyes, brows down
        for col in (3, 8):
            px[y + 2, x + col] = eye
            px[y + 3, x + col] = eye
        px[y + 1, x + 2] = eye
        px[y + 1, x + 9] = eye
    else:
        for col in (3, 8):
            px[y + 3, x + col] = eye
        px[y + 5, x + 5] = shade
        px[y + 5, x + 6] = shade


# ------------------------------------------------------------------- scene ---
@dataclass
class Snapshot:
    context: float
    cpu: float
    ram: float
    temp: float | None
    clock: str


def snapshot() -> Snapshot:
    tokens, _limit, _age = live.claude_context()
    # Sessions routinely exceed 200k now, so report against a 1M window instead
    # of pinning the hero number at 100%.
    context = min(100.0, 100.0 * tokens / 1_000_000)
    cpu = live.cpu_percent(0.2)
    ram = live.memory_percent()
    temp = live.cpu_temp()
    return Snapshot(context, cpu, ram, temp, datetime.now().strftime("%H:%M"))


def build_scene(background: list[np.ndarray], data: Snapshot) -> list[Image.Image]:
    mood = "hot" if data.cpu >= 70 else "calm"
    frames = []
    for index, base in enumerate(background):
        frame = scrim(base).copy()
        height, width = frame.shape[:2]
        px = frame  # write straight into RGB array

        # telemetry: one hero number, one support line
        draw_text(px, 5, 4, f"CLAUDE {data.context:.0f}%", WHITE, 2)
        support = f"CPU {data.cpu:.0f}%  RAM {data.ram:.0f}%"
        if data.temp:
            support += f"  {data.temp:.0f}C"
        draw_text(px, 5, 21, support, DIM, 1)

        # clock, bottom-left, quiet
        draw_text(px, 5, H - 11, data.clock, FAINT, 1)

        draw_pet(px, W - 17, H - 21, index, mood)
        frames.append(Image.fromarray(px.astype(np.uint8), "RGB"))
    return frames


# -------------------------------------------------------------------- main ---
def render(args) -> list[Image.Image]:
    if args.background:
        background = gif_background(args.background)
    else:
        background = nebula_background()
    return build_scene(background, snapshot())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Animated scene for the HX-KEG450: nebula, telemetry and a small pet.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--background", metavar="GIF", help="use this GIF instead of the generated nebula")
    parser.add_argument("--preview", metavar="PNG", help="write a contact sheet and exit")
    parser.add_argument("--once", action="store_true", help="push one loop and exit")
    parser.add_argument("--interval", type=float, default=900.0,
                        help="seconds between refreshes (default 900: every rewrite costs flash)")
    parser.add_argument("--sync-clock", action="store_true", help="also push the RTC before each loop")
    args = parser.parse_args(argv)

    if args.preview:
        frames = render(args)
        cols, rows = 4, 2
        sheet = Image.new("RGB", (cols * (W * 2 + 8), rows * (H * 2 + 8)), (12, 12, 16))
        picks = [int(i * len(frames) / (cols * rows)) for i in range(cols * rows)]
        for index, pick in enumerate(picks):
            tile = frames[pick].resize((W * 2, H * 2), Image.NEAREST)
            sheet.paste(tile, ((index % cols) * (W * 2 + 8), (index // cols) * (H * 2 + 8)))
        sheet.save(args.preview)
        print(f"wrote {args.preview}  ({len(frames)} frames)")
        return 0

    def push_once() -> None:
        frames = render(args)
        payload = keg450.frames_to_payload(frames, [FRAME_MS] * len(frames))
        device = keg450.Device()
        try:
            device.upload_payload(payload, quiet=True)
            if args.sync_clock:
                # 04 28 before an upload makes the panel reject the first page;
                # push the RTC afterwards instead.
                time.sleep(1.0)
                device.clock_sync(quiet=True)
        finally:
            device.close()

    def push() -> None:
        # The panel needs a moment after a previous upload before it accepts a new
        # animation, so retry a couple of times instead of giving up for the hour.
        for attempt in range(1, 4):
            try:
                push_once()
                print(f"[{datetime.now():%H:%M:%S}] refreshed", flush=True)
                return
            except Exception as error:
                print(f"[{datetime.now():%H:%M:%S}] attempt {attempt} failed: {error}",
                      file=sys.stderr, flush=True)
                time.sleep(8)

    if args.once:
        push()
        return 0

    while True:
        try:
            push()
        except Exception as error:                      # keep the loop alive
            print(f"[{datetime.now():%H:%M:%S}] {error}", file=sys.stderr, flush=True)
        time.sleep(max(60.0, args.interval))


if __name__ == "__main__":
    sys.exit(main())
