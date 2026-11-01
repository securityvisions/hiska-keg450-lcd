#!/usr/bin/env python3
"""Push changing content (clock, system load, Claude usage) to the HX-KEG450 screen.

The keyboard has no framebuffer: "live" means re-uploading a 160x96 frame every
N seconds (8 pages, about 1.2 s per upload). Keep the interval at 30 s or more —
each upload rewrites the screen image in flash.

    ./live.py --provider clock  --interval 60
    ./live.py --provider clock  --interval 60 --sync-clock
    ./live.py --provider sys    --interval 30
    ./live.py --provider claude --interval 120
    ./live.py --provider light  --also-light      # adaptive RGB by CPU load
    ./live.py --provider text --text "BUILD OK" --once
    ./live.py --provider sys --preview /tmp/frame.png
    ./live.py --install-service                  # run the clock via systemd
    ./live.py --uninstall-service

--sync-clock also pushes the local time to the keyboard RTC (04 28) when
the loop starts and then once an hour. The panel shows the uploaded frame
either way; the RTC only matters when the keyboard's built-in clock display
mode is selected.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
import time
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from PIL import Image, ImageDraw, ImageFont

import keg450

W, H = keg450.SCREEN_W, keg450.SCREEN_H

# Prefer the Open Sans that ships with the vendor package (drop the TTFs in
# ./fonts), then anything fontconfig can resolve, then a hard-coded path.
FONT_DIRS = (os.path.join(_HERE, "fonts"),)
FONT_FILES = {
    "bold": ("OpenSans-ExtraBold.ttf", "OpenSans-Semibold.ttf"),
    "regular": ("OpenSans-Regular.ttf", "OpenSans-Light.ttf"),
}
FC_MATCH = {
    "bold": ("DejaVu Sans:bold", "Liberation Sans:bold", "sans-serif:bold"),
    "regular": ("DejaVu Sans", "Liberation Sans", "sans-serif"),
}

_FONT_CACHE: dict[str, str] = {}

BACKGROUND = (8, 10, 16)
TEXT = (255, 255, 255)
DIM = (110, 110, 130)
ACCENT = (0, 200, 255)
GOOD = (80, 220, 120)
WARN = (255, 170, 0)
BAD = (255, 70, 70)

CLAUDE_PROJECTS = os.path.expanduser("~/.claude/projects")
CONTEXT_LIMIT = 200_000


# ------------------------------------------------------------------ drawing --
def _resolve_font(kind: str) -> str | None:
    """Find one font file for ``kind``; result is cached for the process."""
    cached = _FONT_CACHE.get(kind)
    if cached is not None:
        return cached or None

    for name in FONT_FILES[kind]:
        for directory in FONT_DIRS:
            path = os.path.join(directory, name)
            if os.path.exists(path):
                _FONT_CACHE[kind] = path
                return path

    import shutil
    import subprocess

    if shutil.which("fc-match"):
        for query in FC_MATCH[kind]:
            try:
                result = subprocess.run(
                    ["fc-match", "-f", "%{file}", query],
                    capture_output=True, text=True, timeout=2, check=False)
            except (OSError, subprocess.SubprocessError):
                break
            path = result.stdout.strip()
            if path and os.path.exists(path):
                _FONT_CACHE[kind] = path
                return path

    _FONT_CACHE[kind] = ""
    return None


def _font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    path = _resolve_font(kind)
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default()


def render(title: str, big: str, small: str = "", fraction: float | None = None,
           bar_colour=ACCENT, big_colour=TEXT) -> Image.Image:
    """Compose one 160x96 frame: caption, large value, gauge, footnote."""
    image = Image.new("RGB", (W, H), BACKGROUND)
    draw = ImageDraw.Draw(image)
    draw.text((5, 3), title.upper(), font=_font("regular", 12), fill=DIM)

    size = 34 if len(big) <= 6 else (26 if len(big) <= 9 else 20)
    font = _font("bold", size)
    draw.text(((W - draw.textlength(big, font=font)) / 2, 22), big,
              font=font, fill=big_colour)

    if fraction is not None:
        draw.rectangle([6, 68, W - 7, 78], outline=DIM)
        filled = int((W - 15) * max(0.0, min(1.0, fraction)))
        if filled > 0:
            draw.rectangle([7, 69, 6 + filled, 77], fill=bar_colour)

    if small:
        font = _font("regular", 12)
        draw.text((max(4, (W - draw.textlength(small, font=font)) / 2), 82),
                  small, font=font, fill=DIM)
    return image


# --------------------------------------------------------------- providers ---
def provider_clock() -> Image.Image:
    now = datetime.now()
    return render(now.strftime("%a %d %b"), now.strftime("%H:%M"), str(now.year))


def provider_text(text: str) -> Image.Image:
    return render("text", text)


def provider_sys() -> Image.Image:
    cpu = cpu_percent()
    return render("cpu", f"{cpu:.0f}%", f"RAM {memory_percent():.0f}%"
                  + (f"  {cpu_temp():.0f}C" if cpu_temp() else ""),
                  cpu / 100.0, _load_colour(cpu))


def provider_claude() -> Image.Image:
    used = claude_window_usage()
    tokens, limit, age = claude_context()
    fraction = tokens / limit
    colour = GOOD if fraction < 0.5 else (WARN if fraction < 0.8 else BAD)
    if used["turns"]:
        footnote = f"5h {(used['in'] + used['out'] + used['cache']) / 1000:.0f}k tok"
    else:
        footnote = f"last session {age}" if age else "no claude session"
    return render("claude context", f"{100 * fraction:.0f}%", footnote, fraction, colour)


def provider_light() -> Image.Image:
    cpu = cpu_percent()
    colour = _load_colour(cpu)
    return render("lighting", f"{cpu:.0f}%", "solid colour from CPU load",
                  cpu / 100.0, colour, colour)


PROVIDERS = {
    "clock": provider_clock,
    "sys": provider_sys,
    "claude": provider_claude,
    "light": provider_light,
}


# ------------------------------------------------------------------ metrics --
def cpu_percent(sample: float = 0.35) -> float:
    def snapshot():
        with open("/proc/stat") as fh:
            fields = [int(v) for v in fh.readline().split()[1:]]
        idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
        return idle, sum(fields)

    idle0, total0 = snapshot()
    time.sleep(sample)
    idle1, total1 = snapshot()
    delta = total1 - total0
    return 0.0 if delta <= 0 else max(0.0, min(100.0, 100.0 * (1 - (idle1 - idle0) / delta)))


def memory_percent() -> float:
    info = {}
    with open("/proc/meminfo") as fh:
        for line in fh:
            key, value = line.split(":", 1)
            info[key] = int(value.split()[0])
    available = info.get("MemAvailable", info.get("MemFree", 0))
    return 100.0 * (1 - available / info["MemTotal"])


def cpu_temp() -> float | None:
    for path in sorted(glob.glob("/sys/class/hwmon/hwmon*/temp*_input")):
        try:
            with open(path) as fh:
                value = int(fh.read().strip()) / 1000.0
        except (OSError, ValueError):
            continue
        if 20 < value < 120:
            return value
    return None


def _load_colour(cpu: float) -> tuple[int, int, int]:
    if cpu < 40:
        return (0, 200, 90)
    if cpu < 75:
        return (255, 190, 0)
    return (255, 60, 60)


def _human_age(seconds: float) -> str:
    if seconds < 120:
        return "just now"
    if seconds < 7200:
        return f"{seconds / 60:.0f}m ago"
    if seconds < 172800:
        return f"{seconds / 3600:.0f}h ago"
    return f"{seconds / 86400:.0f}d ago"


# ------------------------------------------------------------ claude source ---
def _session_files() -> list[str]:
    return glob.glob(os.path.join(CLAUDE_PROJECTS, "**", "*.jsonl"), recursive=True)


def claude_window_usage(window_hours: float = 5.0) -> dict:
    """Token totals over the rolling window, read from local Claude Code logs."""
    cutoff = time.time() - window_hours * 3600
    totals = {"in": 0, "out": 0, "cache": 0, "turns": 0}
    for path in _session_files():
        try:
            if os.path.getmtime(path) < cutoff:
                continue
            with open(path, errors="replace") as fh:
                for line in fh:
                    if '"usage"' not in line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    usage = (record.get("message") or {}).get("usage")
                    if not usage:
                        continue
                    try:
                        when = datetime.fromisoformat(
                            record.get("timestamp", "").replace("Z", "+00:00")).timestamp()
                    except ValueError:
                        when = time.time()
                    if when < cutoff:
                        continue
                    totals["in"] += usage.get("input_tokens", 0)
                    totals["out"] += usage.get("output_tokens", 0)
                    totals["cache"] += usage.get("cache_read_input_tokens", 0)
                    totals["cache"] += usage.get("cache_creation_input_tokens", 0)
                    totals["turns"] += 1
        except OSError:
            continue
    return totals


def claude_context() -> tuple[int, int, str]:
    """``(context_tokens, limit, age)`` for the most recently touched session."""
    files = sorted(_session_files(), key=os.path.getmtime, reverse=True)
    for path in files[:3]:
        last = None
        try:
            with open(path, errors="replace") as fh:
                for line in fh:
                    if '"usage"' in line:
                        last = line
        except OSError:
            continue
        if not last:
            continue
        try:
            usage = (json.loads(last).get("message") or {}).get("usage") or {}
        except json.JSONDecodeError:
            continue
        tokens = (usage.get("input_tokens", 0)
                  + usage.get("cache_read_input_tokens", 0)
                  + usage.get("cache_creation_input_tokens", 0))
        if tokens:
            return tokens, CONTEXT_LIMIT, _human_age(time.time() - os.path.getmtime(path))
    return 0, CONTEXT_LIMIT, ""


# ----------------------------------------------------------------- systemd ---
# One unit name shared with the GUI app (hx450-studio): the last tool to write it
# wins, so two pushers can never fight over the panel.
UNIT_NAME = "hiska-keg450-live.service"
UNIT_PATH = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "systemd", "user", UNIT_NAME)


def _unit_escape(value: str) -> str:
    """Quote one ExecStart argument the way systemd parses it."""
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'"{value}"' if (not value or any(c.isspace() for c in value)) else value


def service_exec_line(args) -> str:
    """ExecStart pointing at *this* checkout with the selected provider/interval."""
    argv = [os.path.abspath(__file__), "--provider", args.provider]
    if args.provider == "text":
        argv += ["--text", args.text]
    argv += ["--interval", f"{args.interval:g}"]
    if args.also_light:
        argv.append("--also-light")
    if args.sync_clock:
        argv.append("--sync-clock")
    if args.verbose:
        argv.append("--verbose")
    return " ".join(_unit_escape(part) for part in argv)


def service_unit_text(exec_line: str) -> str:
    return f"""\
[Unit]
Description=HISKA HX-KEG450 live clock on the keyboard LCD
Documentation=https://github.com/securityvisions/hiska-keg450-lcd
After=default.target

[Service]
Type=simple
# Generated by `live.py --install-service`; re-run it to change provider/interval.
# The GUI app installs this same unit name: last writer wins, so only one pusher runs.
ExecStart={exec_line}
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
"""


def device_present() -> bool:
    try:
        keg450.find_nodes()
    except (RuntimeError, OSError):
        return False
    return True


def _systemctl(*args: str) -> tuple[int, str]:
    try:
        done = subprocess.run(["systemctl", "--user", *args],
                              capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return done.returncode, (done.stdout + done.stderr).strip()


def warn_if_unit_active() -> None:
    """A foreground loop and the user unit both push to the same panel."""
    if os.environ.get("INVOCATION_ID"):      # we are the unit: nothing to warn about
        return
    try:
        if _systemctl("is-active", "--quiet", UNIT_NAME)[0] == 0:
            print(f"warning: {UNIT_NAME} is already running — two pushers will fight "
                  f"over the screen.\n"
                  f"  stop it with: systemctl --user disable --now {UNIT_NAME}",
                  file=sys.stderr, flush=True)
    except Exception:                        # systemd missing or not our concern
        pass


def install_service(args, dry_run: bool) -> int:
    content = service_unit_text(service_exec_line(args))
    commands = (("daemon-reload",), ("enable", "--now", UNIT_NAME))
    if dry_run:
        print(f"# would write {UNIT_PATH}\n")
        print(content, end="")
        print("\n# would then run:")
        for command in commands:
            print("#   systemctl --user " + " ".join(command))
        return 0

    if not device_present():
        print("refusing to install: no HX-KEG450 HID device found.\n"
              "  * connect the keyboard with the USB-C cable\n"
              "  * check the udev rule and the 'input' group (`make info`)",
              file=sys.stderr)
        return 2

    os.makedirs(os.path.dirname(UNIT_PATH), exist_ok=True)
    with open(UNIT_PATH, "w", encoding="utf-8") as fh:
        fh.write(content)

    for command in commands:
        code, output = _systemctl(*command)
        if code != 0:
            print(f"wrote {UNIT_PATH} but `systemctl --user {' '.join(command)}` "
                  f"failed:\n  {output or 'no output'}",
                  file=sys.stderr)
            return 1

    print(f"installed {UNIT_PATH} and started {UNIT_NAME}")
    print(f"watch it with: journalctl --user -u {UNIT_NAME} -f")
    return 0


def uninstall_service(dry_run: bool) -> int:
    if dry_run:
        print(f"# would run: systemctl --user disable --now {UNIT_NAME}")
        print("# would run: systemctl --user daemon-reload")
        print(f"# would remove {UNIT_PATH}")
        return 0

    if not os.path.exists(UNIT_PATH):
        print(f"{UNIT_NAME} is not installed ({UNIT_PATH} does not exist)")
        return 0

    _systemctl("disable", "--now", UNIT_NAME)    # already-dead units still get removed
    _systemctl("daemon-reload")
    os.unlink(UNIT_PATH)
    print(f"removed {UNIT_PATH}; {UNIT_NAME} is stopped and disabled")
    return 0


# --------------------------------------------------------------------- main ---
def next_delay(provider: str, interval: float) -> float:
    """Clock pushes are aligned to the minute boundary so the panel flips on time."""
    if provider == "clock":
        now = datetime.now()
        until_next_minute = 60 - now.second - now.microsecond / 1_000_000 + 1.2
        return max(1.0, min(until_next_minute, interval))
    return max(1.0, interval)


def upload(frame: Image.Image, verbose: bool = False, also_light: bool = False,
           sync_clock: bool = False) -> None:
    with keg450.Device(verbose=verbose) as dev:
        if sync_clock and not dev.clock_sync(quiet=True):
            print(f"[{datetime.now():%H:%M:%S}] RTC sync was not acknowledged",
                  file=sys.stderr, flush=True)
        dev.upload_payload(keg450.frames_to_payload([frame], [2000]), quiet=True)
        if also_light:
            dev.set_light(1, _load_colour(cpu_percent()), 5, 3, quiet=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--provider", default="clock",
                        choices=sorted(PROVIDERS) + ["text"])
    parser.add_argument("--text", default="hello", help="text for --provider text")
    parser.add_argument("--interval", type=float, default=60.0,
                        help="seconds between uploads (>=30 is kind to the flash)")
    parser.add_argument("--once", action="store_true", help="push one frame and exit")
    parser.add_argument("--preview", metavar="PNG", help="write the frame and exit")
    parser.add_argument("--also-light", action="store_true",
                        help="also set a solid RGB colour from CPU load")
    parser.add_argument("--sync-clock", action="store_true",
                        help="also push the local time to the keyboard RTC "
                             "(on start and once an hour)")
    parser.add_argument("--install-service", action="store_true",
                        help="write and enable the user systemd unit for this "
                             "checkout, then exit")
    parser.add_argument("--uninstall-service", action="store_true",
                        help="stop, disable and remove the user systemd unit, then exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="with --install-service/--uninstall-service, only print "
                             "what would be written and run")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    if args.install_service and args.uninstall_service:
        parser.error("--install-service and --uninstall-service are mutually exclusive")

    def build() -> Image.Image:
        if args.provider == "text":
            return provider_text(args.text)
        return PROVIDERS[args.provider]()

    if args.install_service or args.uninstall_service:
        if args.interval < 10:
            print("refusing interval < 10 s: every upload rewrites the screen image in flash",
                  file=sys.stderr)
            return 2
        if args.install_service:
            return install_service(args, args.dry_run)
        return uninstall_service(args.dry_run)

    if args.dry_run:
        print("note: --dry-run only affects --install-service/--uninstall-service",
              file=sys.stderr)

    if args.preview:
        build().save(args.preview)
        print(f"wrote {args.preview}")
        return 0

    if args.interval < 10:
        print("refusing interval < 10 s: every upload rewrites the screen image in flash",
              file=sys.stderr)
        return 2

    if args.once:
        upload(build(), args.verbose, args.also_light, args.sync_clock)
        return 0

    warn_if_unit_active()

    round_number = 0
    synced_at: float | None = None
    while True:
        round_number += 1
        sync = args.sync_clock and (synced_at is None
                                    or time.monotonic() - synced_at >= 3600)
        try:
            started = time.monotonic()
            upload(build(), args.verbose, args.also_light, sync)
            if sync:
                synced_at = time.monotonic()
            print(f"[{datetime.now():%H:%M:%S}] frame {round_number} pushed "
                  f"in {time.monotonic() - started:.1f}s", flush=True)
        except Exception as exc:             # a dead keyboard must not kill the loop
            print(f"[{datetime.now():%H:%M:%S}] push failed: {exc}",
                  file=sys.stderr, flush=True)
        time.sleep(next_delay(args.provider, args.interval))


if __name__ == "__main__":
    sys.exit(main())
