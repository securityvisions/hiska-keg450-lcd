# Hardware probe results — HISKA HX-KEG450

Everything here was measured on the real device (USB-C, firmware 1.06,
`/dev/hidraw6` = image / 0xFF68, `/dev/hidraw7` = control / 0xFF13).
Read-only unless stated. Date of capture: session of 2026-11-01.

## 1. `04 F5` is a per-key RGB *readback*, not a heartbeat

This contradicts the earlier assumption (and the vendor app's own idle polling
pattern, which sends `04 F5 … 09` every ~30 ms and ignores the payload).

```
04 F5 <page>                          page = 0x00, 0x01, 0x02
```

The reply is an echo (`04 F5 <page> 01 …`) followed by **16 data reports of
64 bytes**, i.e. 256 four-byte records:

```
record = [key_index] [b1] [b2] [b3]        16 records per 64-byte report
```

Observed for a keyboard currently set to *Static magenta, brightness 5*:

```
page 0  key 0x00 = 8e 00 0c      key 0x10..0x12 = 8e 8e 00 (yellow)
        key 0x01..0x0f, 0x13..  = 7e 00 fd   (magenta)
page 1  key 0x00 = 00 6f 8e      rest = 7e 00 fd
page 2  key 0x00 = 8e 2b 00      rest = 7e 00 fd
```

Three pages of 256 keys each strongly suggests **one page per onboard profile**
(the vendor UI has exactly three "My Exclusive Config" slots). Only a handful of
key indices differ, which matches the vendor's own per-key default overlay.

**Unknown:** the exact channel encoding. `7e 00 fd` for a magenta LED is not a
plain 0–255 RGB triple (red saturates at `7e` = 126 while blue reaches `fd` =
253), so there is a scale or a packed field we have not decoded. Readback itself
is confirmed, decoding is not.

**Follow-up test (important caveat).** We then set six known solid colours
(red, green, blue, white, grey, magenta) and read page 0 after each: the two
"ordinary" key slots stayed at `7e 00 00` regardless, while key 0 cycled through
`(0,142,25) → (142,38,0) → (2,0,142) → (0,142,7) → (142,0,5) → (0,1,142)` — i.e.
the same three magnitudes rotating through the three channels, and every page
held only 13 distinct values.

That is not what a static colour readback looks like. The most likely reading is
that `04 F5` hands back a **live LED buffer / effect frame stream** (or a
different layer from the one `04 13` writes), not the stored per-key config.

So: the channel exists and returns 3 × 256 four-byte per-key records [C], but
its semantics are **unresolved** [U]. It is not (yet) a reliable "what colour is
key X" oracle. Do not build on it before decoding the page/argument meaning
against a known per-key pattern (set every key to a distinct colour via the
vendor app, then read).

## 2. The MI_01 vendor event channel (`0xFF13`-sibling, report ID 5) is silent

`hidraw5` was read for 5 s idle, 4 s after a lighting change, and 6 s across a
screen commit. **Zero reports.** The vendor app polls this endpoint every 200 ms
and reacts to `05 A6 FF 01` / `05 A6 FE 02`, but nothing was emitted for idle,
lighting writes, or uploads. Whatever it reports needs an event we cannot
trigger programmatically (almost certainly the rotary knob, the on-screen menu,
or a mode switch).

`hidraw4` (MI_00) carries ordinary keyboard input reports; its last byte is the
1-byte vendor usage `0xFF/0x03`, not yet correlated with anything.

## 3. Screen upload: the device answers with *two* ACKs per page

A single `04 18 / 04 72 / 8 pages / 04 02` upload, with the page ACK drained
after each page write, still leaves 8 `01 5A 02` reports queued on the image
node afterwards. So each page produces two ACK reports, or the commit burst does.

Practical consequence: `read_page_ack()` can consume a stale ACK and still look
healthy. It is harmless (every ACK is identical) but means the ACK is a weak
success signal — it does not prove a specific page landed.

## 4. Firmware version is readable without any HID traffic

```
/sys/bus/usb/devices/3-10.4/bcdDevice = 0106  ->  firmware 1.06
```

The vendor app formats `HidD_GetAttributes.VersionNumber` as hex and compares it
to `<firmware version="120">` in `layouts/rgb-keyboard.xml`, so **it believes
this keyboard is out of date** (1.06 < 1.20) and offers an update it cannot
deliver (`config.xml` has an empty `<upgrade url>`). No dump channel exists; the
real flasher is an external `FirmwareUpdateTool.exe` that is not shipped.

## 5. What this changes for the live-display pipeline

The display is a **store, not a framebuffer**, and re-uploading a frame does
change what the panel shows — verified directly: three consecutive uploads of
`11` / `22` / `33` left the panel showing `33`. So "live" problems are schedule
problems in the client, not protocol problems. The studio app now:

* aligns clock pushes to the minute boundary (+1.2 s),
* retries a failed push after 3 s (30 s after five failures),
* shows a green `live · pushed Ns ago` / amber `stale` indicator, and logs every
  push so a silent stall is visible.

Measured after the change: pushes at `14:23:01`, `14:24:02`, `14:25:02` — i.e.
the panel changes about a second after each minute ticks over.
