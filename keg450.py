#!/usr/bin/env python3
"""HISKA HX-KEG450 — control the keyboard's 160x96 TFT screen and RGB lighting from Linux.

The vendor ships a Windows-only MFC application. This module talks to the same
two vendor HID interfaces directly:

    usage page 0xFF13   64-byte FEATURE reports    commands + status
    usage page 0xFF68   4096-byte OUTPUT reports   screen image pages

Typical use::

    dev = Device()
    dev.upload_payload(frames_to_payload([frame], [2000]))
    dev.set_light(mode=1, colour=(255, 0, 255), brightness=5)
    dev.close()

See docs/PROTOCOL.md for the byte-level protocol and how it was recovered.
"""
from __future__ import annotations

import fcntl
import glob
import os
import select
import struct
import sys
import time

__version__ = "1.0.0"

# ---------------------------------------------------------------- geometry ---
SCREEN_W = 160
SCREEN_H = 96
HEADER_LEN = 256          # gif_headlength in the vendor layout XML
MAX_FRAMES = 255          # gif_maxframes
PAGE = 4096               # one OUTPUT report
CMD_DELAY = 0.035         # cmd_delaytime, seconds between commands

# ---------------------------------------------------------------- framing ----
REPORT_ID = 0x00
BUF_LEN = 65              # report id byte + 64-byte report, as the kernel sees it
ACK_OK = 0x01
ACK_REJECTED = 0xFF
PAGE_ACK_MAGIC = b"\x01\x5a\x02"


# ------------------------------------------------------------- hid plumbing --
def _ioc(direction: int, type_char: str, nr: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (ord(type_char) << 8) | nr


def _hid_set_feature(size: int) -> int:
    return _ioc(3, "H", 0x06, size)


def _hid_get_feature(size: int) -> int:
    return _ioc(3, "H", 0x07, size)


def find_nodes() -> tuple[str, str]:
    """Return ``(image_node, control_node)`` by inspecting HID report descriptors."""
    image = control = None
    pattern = "/sys/class/hidraw/hidraw*/device/report_descriptor"
    for desc_path in sorted(glob.glob(pattern)):
        try:
            with open(desc_path, "rb") as fh:
                head = fh.read(4)
        except OSError:
            continue
        node = "/dev/" + desc_path.split("/")[4]
        if head.startswith(b"\x06\x68\xff"):        # Usage Page 0xFF68
            image = node
        elif head.startswith(b"\x06\x13\xff"):      # Usage Page 0xFF13
            control = node
    if image is None or control is None:
        raise RuntimeError(
            "HX-KEG450 not found (image=%r, control=%r).\n"
            "  * is the keyboard connected with the USB-C cable?\n"
            "  * is the udev rule installed and are you in the 'input' group?"
            % (image, control)
        )
    return image, control


class Device:
    """A live connection to the keyboard's vendor interfaces."""

    LIGHT_MODES = (
        "static", "singleon", "singleoff", "glittering", "fluttering",
        "colourful", "breath", "spectrum", "outward", "scrolling",
        "rolling", "rotating", "explode", "launch", "ripples", "flowing",
        "pulsating", "tilt", "shuttle", "off",
    )

    def __init__(self, verbose: bool = False):
        self.image_path, self.control_path = find_nodes()
        self.verbose = verbose
        self.image_fd = os.open(self.image_path, os.O_RDWR | os.O_NONBLOCK)
        self.control_fd = os.open(self.control_path, os.O_RDWR | os.O_NONBLOCK)
        self._drain()

    def __enter__(self) -> "Device":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        for fd in (self.image_fd, self.control_fd):
            try:
                os.close(fd)
            except OSError:
                pass

    # -- control channel ---------------------------------------------------
    def _drain(self, limit: int = 32) -> None:
        """Discard stale replies; the firmware's reply queue lags behind."""
        for _ in range(limit):
            ready, _, _ = select.select([self.control_fd], [], [], 0.03)
            if not ready:
                return
            try:
                if not os.read(self.control_fd, 64):
                    return
            except OSError:
                return

    def send_feature(self, payload: bytes) -> None:
        """Write one 64-byte FEATURE report (the kernel wants the report-id byte too)."""
        if len(payload) != 64:
            raise ValueError(f"feature payload must be 64 bytes, got {len(payload)}")
        fcntl.ioctl(self.control_fd, _hid_set_feature(BUF_LEN), bytes([REPORT_ID]) + payload)

    def read_reply(self, timeout: float = 0.30) -> bytes | None:
        """Return the 64-byte status report, or None if the device stayed silent."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ready, _, _ = select.select([self.control_fd], [], [], remaining)
            if ready:
                try:
                    data = os.read(self.control_fd, 64)
                except BlockingIOError:
                    continue
                except OSError:
                    break
                if data:
                    return data
        # The firmware answers through GET_FEATURE rather than interrupt IN.
        try:
            buf = bytearray(BUF_LEN)
            fcntl.ioctl(self.control_fd, _hid_get_feature(BUF_LEN), buf, True)
            return bytes(buf[1:])
        except OSError:
            return None

    def command(self, *args: int) -> bytes | None:
        """Send a short command and return its 64-byte reply."""
        payload = bytes(args) + bytes(64 - len(args))
        self.send_feature(payload)
        time.sleep(0.002)
        reply = self.read_reply()
        if self.verbose:
            shown = reply[:8].hex(" ") if reply else "<none>"
            print(f"  > {payload[:8].hex(' ')}   < {shown}")
        time.sleep(CMD_DELAY)
        return reply

    @staticmethod
    def status(reply: bytes | None) -> str:
        """Decode the status byte (offset 3) of a reply."""
        if reply is None or len(reply) < 4:
            return "no-reply"
        code = reply[3]
        if code == ACK_OK:
            return "ok"
        if code == ACK_REJECTED:
            return "rejected"
        return f"0x{code:02x}"

    # -- image channel -----------------------------------------------------
    def _write_page(self, page: bytes) -> None:
        """hidraw strips the report-id byte, so the buffer is 4097 bytes."""
        if len(page) != PAGE:
            raise ValueError(f"page must be {PAGE} bytes, got {len(page)}")
        os.write(self.image_fd, b"\x00" + page)

    def _read_page_ack(self, timeout: float = 0.60) -> bytes | None:
        """Page acknowledgements arrive on the 0xFF68 node, starting ``01 5A 02``."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            ready, _, _ = select.select([self.image_fd], [], [], remaining)
            if not ready:
                return None
            try:
                data = os.read(self.image_fd, 64)
            except BlockingIOError:
                continue
            except OSError:
                return None
            if data:
                return data

    def upload_payload(self, payload: bytes, slot: int = 0x00, quiet: bool = False) -> int:
        """Stream a GIF payload to the screen and commit it. Returns the page count."""
        pages = (len(payload) + PAGE - 1) // PAGE
        payload = payload + b"\xff" * (pages * PAGE - len(payload))
        say = (lambda *a: None) if quiet else print

        self._drain()
        say(f"  begin     : {self.status(self.command(0x04, 0x18))}")
        reply = self.command(0x04, 0x72, slot, 0x00, 0x00, 0x00, 0x00, 0x00,
                             pages & 0xFF, (pages >> 8) & 0xFF)
        say(f"  metadata  : {self.status(reply)}  ({pages} pages)")

        started = time.monotonic()
        for index in range(pages):
            self._write_page(payload[index * PAGE:(index + 1) * PAGE])
            ack = self._read_page_ack()
            if ack is None or not ack.startswith(PAGE_ACK_MAGIC):
                raise RuntimeError(
                    "page %d/%d was not acknowledged (%s)"
                    % (index + 1, pages, "timeout" if ack is None else ack[:8].hex(" "))
                )
            if not quiet and (index + 1 == pages or (index + 1) % 100 == 0):
                elapsed = max(time.monotonic() - started, 1e-6)
                print(f"\r  pages     : {index + 1}/{pages} "
                      f"({100.0 * (index + 1) / pages:5.1f}%)  "
                      f"{elapsed:5.1f}s @ {(index + 1) / elapsed:4.1f}/s ",
                      end="", flush=True)
        if not quiet:
            print()

        say(f"  commit    : {self.status(self.command(0x04, 0x02))}")
        return pages

    # -- rgb lighting ------------------------------------------------------
    def set_light(self, mode: int = 1, colour: tuple[int, int, int] = (255, 0, 0),
                  brightness: int = 5, speed: int = 3,
                  colourful: bool = False, quiet: bool = False) -> bool:
        """Apply a lighting mode.

        ``04 13`` must carry 0x01 in its 9th byte, otherwise the commit is
        silently accepted and ignored. ``colourful`` selects the rainbow
        palette (the vendor UI's "Colourful" checkbox); when it is set the RGB
        value is ignored.
        """
        red, green, blue = colour
        data = bytes([
            mode & 0xFF, red & 0xFF, green & 0xFF, blue & 0xFF,
            0, 0, 0, 0,
            0x01 if colourful else 0x00,
            brightness & 0xFF, speed & 0xFF,
            0, 0, 0, 0xAA, 0x55,
        ]) + bytes(48)

        self._drain()
        begin = self.command(0x04, 0x18)
        enter = self.command(0x04, 0x13, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x01)
        self.send_feature(data)
        time.sleep(CMD_DELAY)
        commit = self.command(0x04, 0x02)
        self.command(0x04, 0xF0)
        if not quiet:
            print(f"  begin     : {self.status(begin)}")
            print(f"  light-mode: {self.status(enter)}")
            print(f"  commit    : {self.status(commit)}")
        return self.status(commit) == "ok"

    # -- misc --------------------------------------------------------------
    def clock_sync(self, quiet: bool = False) -> bool:
        """Push the local clock to the keyboard's RTC."""
        now = time.localtime()
        reply = self.command(0x04, 0x28, now.tm_year % 100, now.tm_mon,
                             now.tm_mday, now.tm_hour, now.tm_min, now.tm_sec)
        ok = self.status(reply) == "ok"
        if not quiet:
            print(f"  clock sync: {self.status(reply)}")
        return ok


# ------------------------------------------------------------------ payload --
def rgb565_le(image) -> bytes:
    """Convert a 160x96 PIL image to little-endian RGB565 bytes."""
    if image.size != (SCREEN_W, SCREEN_H):
        raise ValueError(f"frame must be {SCREEN_W}x{SCREEN_H}, got {image.size}")
    raw = image.convert("RGB").tobytes()
    out = bytearray(len(raw) // 3 * 2)
    for src in range(0, len(raw), 3):
        red, green, blue = raw[src], raw[src + 1], raw[src + 2]
        value = ((red & 0xF8) << 8) | ((green & 0xFC) << 3) | (blue >> 3)
        struct.pack_into("<H", out, (src // 3) * 2, value)
    return bytes(out)


def frames_to_payload(frames, delays_ms) -> bytes:
    """Build the GIF payload: 256-byte header followed by RGB565 frames.

    header[0]      frame count
    header[1..N]   per-frame delay in 2 ms units
    header[N+1..]  0xFF fill
    """
    frames = list(frames)
    delays_ms = list(delays_ms)
    if not frames:
        raise ValueError("no frames given")
    if len(frames) > MAX_FRAMES:
        raise ValueError(f"at most {MAX_FRAMES} frames, got {len(frames)}")
    if len(delays_ms) != len(frames):
        raise ValueError("delays length does not match frame count")

    header = bytearray(b"\xff" * HEADER_LEN)
    header[0] = len(frames)
    for index, delay in enumerate(delays_ms):
        header[1 + index] = max(1, min(255, int(round(delay / 2.0))))

    body = bytearray(header)
    for frame in frames:
        body += rgb565_le(frame)
    return bytes(body)


def load_image(path: str):
    """Load a still image or an animated GIF as ``(frames, delays_ms)``."""
    from PIL import Image, ImageSequence

    source = Image.open(path)
    frames, delays = [], []
    for frame in ImageSequence.Iterator(source):
        frames.append(frame.convert("RGB").resize((SCREEN_W, SCREEN_H), Image.LANCZOS))
        delays.append(frame.info.get("duration") or 40)
    if len(frames) > MAX_FRAMES:                     # drop evenly if oversized
        step = len(frames) / MAX_FRAMES
        picks = [int(i * step) for i in range(MAX_FRAMES)]
        frames = [frames[i] for i in picks]
        delays = [delays[i] for i in picks]
    return frames, delays


def make_testcard():
    """A four-colour sanity card: red/green/blue/white quadrants, yellow corner."""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (SCREEN_W, SCREEN_H), "black")
    draw = ImageDraw.Draw(image)
    half_w, half_h = SCREEN_W // 2, SCREEN_H // 2
    draw.rectangle([0, 0, half_w - 1, half_h - 1], fill=(255, 0, 0))
    draw.rectangle([half_w, 0, SCREEN_W - 1, half_h - 1], fill=(0, 255, 0))
    draw.rectangle([0, half_h, half_w - 1, SCREEN_H - 1], fill=(0, 0, 255))
    draw.rectangle([half_w, half_h, SCREEN_W - 1, SCREEN_H - 1], fill=(255, 255, 255))
    draw.line([0, 0, 40, 40], fill=(255, 255, 0), width=2)
    draw.rectangle([0, 0, 15, 3], fill=(255, 255, 0))
    return image


# ---------------------------------------------------------------------- cli --
def _parse_colour(text: str) -> tuple[int, int, int]:
    value = text.lstrip("#")
    if len(value) != 6:
        raise ValueError("colour must be RRGGBB")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def main(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="keg450.py",
        description="HISKA HX-KEG450: TFT screen and RGB lighting control.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("info", help="show the detected hidraw nodes")
    sub.add_parser("clock", help="push the local time to the keyboard RTC")
    sub.add_parser("testcard", help="upload a 1-frame colour test card")

    upload = sub.add_parser("upload", help="upload a still image or animated GIF")
    upload.add_argument("path")

    light = sub.add_parser("light", help="set the RGB lighting")
    light.add_argument("--mode", default="static",
                       help="mode name, 1-20, or 'list' to print them")
    light.add_argument("--color", default="ff0000", help="RRGGBB (ignored with --colorful)")
    light.add_argument("--brightness", type=int, default=5, help="0-5")
    light.add_argument("--speed", type=int, default=3, help="0-5")
    light.add_argument("--colorful", action="store_true", help="use the rainbow palette")

    args = parser.parse_args(argv)

    if args.command == "info":
        image, control = find_nodes()
        print(f"image node  : {image}  (0xFF68, {PAGE}-byte output)")
        print(f"control node: {control}  (0xFF13, 64-byte feature)")
        print(f"screen      : {SCREEN_W}x{SCREEN_H} RGB565, max {MAX_FRAMES} frames")
        return 0

    if args.command == "light" and args.mode == "list":
        for index, name in enumerate(Device.LIGHT_MODES, start=1):
            print(f"{index:2d}  {name}")
        return 0

    with Device(verbose=args.verbose) as dev:
        if args.command == "clock":
            return 0 if dev.clock_sync() else 1

        if args.command == "testcard":
            payload = frames_to_payload([make_testcard()], [2000])
            print("uploading test card (yellow marker marks the top-left corner)")
            dev.upload_payload(payload)
            return 0

        if args.command == "upload":
            frames, delays = load_image(args.path)
            print(f"uploading {len(frames)} frame(s) from {args.path}")
            dev.upload_payload(frames_to_payload(frames, delays))
            return 0

        if args.command == "light":
            mode = args.mode
            if mode.isdigit():
                mode_number = int(mode)
            else:
                try:
                    mode_number = Device.LIGHT_MODES.index(mode.lower()) + 1
                except ValueError:
                    print(f"unknown mode {mode!r}; use 'light --mode list'", file=sys.stderr)
                    return 2
            colour = _parse_colour(args.color)
            print(f"lighting: mode={mode_number} ({Device.LIGHT_MODES[mode_number - 1]}) "
                  f"colour={colour} brightness={args.brightness} "
                  f"speed={args.speed} colourful={args.colorful}")
            return 0 if dev.set_light(mode_number, colour, args.brightness,
                                      args.speed, colourful=args.colorful) else 1

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
