# Protocol reference — HISKA HX-KEG450

Everything below was verified against the hardware unless a line is explicitly
marked **unverified**. Where a claim comes from a capture of the vendor tool,
the capture method is noted.

- Keyboard: HISKA HX-KEG450 (1800-compact, 3-mode: USB-C / 2.4 GHz dongle / BT)
- Vendor software: `HX-KEG450 Driver 1.0.0.0` — an MFC application
  (`DeviceDriver.exe` + `mui.dll`), packaged by an Inno Setup stub
- Screen: 160 × 96 px, RGB565, one stored animation slot, ≤ 255 frames
- All vendor traffic was captured with `usbmon` while the app ran under Wine,
  then replayed from Linux through `hidraw`.

## 1. USB topology

The device deliberately reports Apple's identifiers, so `lsusb` shows a keyboard
that is not one. This is why udev rules must match on IDs, not names.

| Field | Value |
|---|---|
| VID:PID (cable) | `05ac:024f` |
| Manufacturer string | `SONiX` |
| Product string | `HISKA HX-KEG450` |
| Interfaces | 4 (composite) |

| Interface | Usage page | Reports | Role |
|---|---|---|---|
| MI_00 | `0x01` Generic Desktop (keyboard) | in 8 B, out 1 B | typing, LED state |
| MI_01 | mixed consumer / system / keyboard / mouse / `0xFF` | 5 collections | media keys, mouse emulation |
| **MI_02** | **`0xFF68`** | in 64 B (usage `0x62`), **out 4096 B** (usage `0x63`) | screen image pages + per-page ACK |
| **MI_03** | **`0xFF13`** | in 64 B, out 64 B, feature 64 B (usage `0xFF00`) | commands and status |

Raw MI_02 descriptor (34 bytes):

```
06 68 FF        Usage Page (vendor 0xFF68)
09 61           Usage 0x61
A1 01           Collection (Application)
  09 62  15 00 26 FF 00  95 40  75 08  81 02     Input  64 bytes
  09 63  15 00 26 FF 00  96 00 10  75 08  91 02  Output 4096 bytes
C0              End Collection
```

Raw MI_03 descriptor (32 bytes):

```
06 13 FF        Usage Page (vendor 0xFF13)
09 01           Usage 0x01
A1 01           Collection (Application)
  15 00 26 FF 00 75 08 95 40 09 02 81 02   Input   64 bytes (usage 0x02)
  09 03 91 02                              Output  64 bytes (usage 0x03)
  0A 00 FF B1 02                           Feature 64 bytes (usage 0xFF00)
C0              End Collection
```

### Finding the nodes on Linux

Do not hard-code `hidrawN` — the numbers change between boots and between USB
ports. Match on the first bytes of the report descriptor instead:

```python
if descriptor.startswith(b"\x06\x68\xff"):    # 0xFF68  -> image node
elif descriptor.startswith(b"\x06\x13\xff"):  # 0xFF13  -> control node
```

`find_nodes()` in `keg450.py` does exactly this via
`/sys/class/hidraw/hidraw*/device/report_descriptor`.

## 2. Framing rules (these will bite you)

### 2.1 Control reports are "numbered" even though the descriptor says otherwise

`ioctl(HIDIOCSFEATURE, 64)` fails with `EOVERFLOW` on this device. The kernel
treats the 64-byte report as report ID `0x00` and expects a **65-byte buffer**:

```python
buf = b"\x00" + payload_64_bytes          # 65 bytes
fcntl.ioctl(fd, HIDIOCSFEATURE(65), buf)
```

Verified: sizes 1–64 fail, 65 succeeds, ≥66 fails with `EPIPE`. The vendor tool
running under Wine produces the same 65-byte transfers on the wire.

### 2.2 Status lives at byte 3

Replies are 64-byte reports (strip the leading report-ID byte):

| byte 3 | meaning |
|---|---|
| `0x01` | accepted |
| `0xFF` | rejected / busy |
| `0x00` | "no verdict" — either a plain echo or an ignored command |

Commands are echoed back with the verdict patched in, e.g. sending
`04 18 00 …` returns `04 18 00 01 00 …`.

The firmware answers through **GET_FEATURE**, not through the interrupt-IN
endpoint; `keg450.py` polls with `HIDIOCGFEATURE(65)` after each write.

### 2.3 Image pages need the report-ID byte too

`write(fd, page_4096)` silently truncates a page whose first byte is `0x00`
(hidraw strips the report ID). Always write **4097** bytes:

```python
os.write(image_fd, b"\x00" + page)
```

### 2.4 Command spacing

The vendor layout XML sets `cmd_delaytime = 35` ms and the firmware needs it:
space commands by ~35 ms, and leave ≥1 ms between a SET and its GET (a faster
read can return your own request back and look like a phantom ACK).

## 3. Control command set

Short commands are 64 bytes, only the first bytes are significant.

| Command | Meaning |
|---|---|
| `04 18` | begin a transaction |
| `04 72 <slot> 00 00 00 00 00 <pages_lo> <pages_hi>` | screen metadata: slot, page count (LE16) |
| `04 02` | commit |
| `04 F0` | end / finish |
| `04 13 … 01` | enter lighting write mode (see §5) |
| `04 28 <yy mm dd hh mm ss>` | set RTC — **unverified** (sent once by the vendor tool; effect not confirmed) |
| `04 F5 … 09` | heartbeat/poll the vendor tool sends every ~30 ms |

Our tool does **not** need the `04 F5` heartbeat: every upload and lighting
change works with plain command sequences.

## 4. Screen upload

```
04 18                                          begin
04 72 00 00 00 00 00 00 <pages_lo> <pages_hi>  metadata
N × 4096-byte OUTPUT report on 0xFF68           page data
04 02                                          commit
```

Each page must be acknowledged. The ACK is a 64-byte interrupt-IN report on the
**image node** (not the control node) beginning `01 5A 02`. A missing ACK means
the device dropped the page; abort and re-send the whole animation.

### 4.1 Payload layout

The whole payload is one stream, padded at the end (not per frame) with `0xFF`
to a multiple of 4096:

```
offset 0        frame count            (1..255)
offset 1..N     per-frame delay        in 2 ms units (delay_ms / 2, min 1)
offset N+1..255 0xFF fill
offset 256..    frames, row-major, RGB565 little-endian:
                v = (R & 0xF8) << 8 | (G & 0xFC) << 3 | (B >> 3)
                low byte first, 160 × 96 = 15360 px = 30720 bytes per frame
```

### 4.2 Page-count arithmetic

```
1 frame   : ceil((256 + 1 × 30720) / 4096) = 8 pages
225 frames: ceil((256 + 225 × 30720) / 4096) = 1688 pages
255 frames: ceil((256 + 255 × 30720) / 4096) = 1913 pages
```

Both figures were confirmed end-to-end: a 225-frame GIF uploaded in 1688 pages.

### 4.3 Measured throughput

| Case | Pages | Wall time | Rate |
|---|---|---|---|
| 1 frame | 8 | 1.2 s | 6.7 pages/s |
| 225 frames | 1688 | 228.8 s | 7.4 pages/s |

Roughly 30 KB/s of pixel data — the firmware serialises flash writes, so this is
the device's ceiling, not the USB link's.

### 4.4 Orientation and colour order

Confirmed on hardware with a four-colour card plus a yellow corner marker:
the first pixel of the stream is the **top-left** pixel, rows run left-to-right,
top-to-bottom, RGB565 little-endian. No flip, rotation or byte swap is needed.

## 5. RGB lighting

```
04 18                                              begin
04 13 00 00 00 00 00 00 01 00 …                    enter lighting write mode
<mode> <R> <G> <B> 00 00 00 00 <colorful> <brightness> <speed> 00 00 00 AA 55
04 02                                              commit
04 F0                                              end
```

Two details decide whether it works at all:

1. **`04 13` must carry `0x01` in byte 8.** Without it the device answers the
   later commit with `0x00` and silently keeps the previous effect.
2. **Byte 8 of the data packet is the vendor UI's "Colourful" checkbox.**
   `1` = rainbow palette (RGB ignored), `0` = solid `R G B`. Getting this wrong
   is what makes a "static magenta" request produce a static rainbow.

| Offset | Field |
|---|---|
| 0 | mode id (1-based, see below) |
| 1 | red |
| 2 | green |
| 3 | blue |
| 4–7 | zero |
| 8 | colourful palette flag |
| 9 | brightness, 0–5 |
| 10 | speed, 0–5 |
| 11–13 | zero |
| 14–15 | `AA 55` |

Mode ids are the order of the vendor UI list:

| # | Mode | # | Mode |
|---|---|---|---|
| 1 | Static | 11 | Rolling |
| 2 | SingleOn | 12 | Rotating |
| 3 | SingleOff | 13 | Explode |
| 4 | Glittering | 14 | Launch |
| 5 | Fluttering | 15 | Ripples |
| 6 | Colourful | 16 | Flowing |
| 7 | Breath | 17 | Pulsating |
| 8 | Spectrum | 18 | Tilt |
| 9 | Outward | 19 | Shuttle |
| 10 | Scrolling | 20 | Off |

Verified by capture: selecting Static / Breath / Rolling in the UI produced data
packets with mode byte `01` / `07` / `0b`, matching the vendor default
`default_mode="11"` (Rolling) in the layout XML.

## 6. Where the geometry came from

The vendor package contains `layouts/rgb-keyboard.xml`, which both the app and
the firmware config reference:

```xml
<cmd_delaytime value="35" />
<screen gif_headlength="256" gif_maxframes="255" gif_count="1" width="160" height="96" />
<light default_mode="11" default_brightness="5" default_speed="3"
       brightness_max="5" speed_max="5" />
<menu screen="1" />
<menu_wireless screen="0" />
```

`menu_wireless screen="0"` matches the fact that the screen is unavailable while
the keyboard is in 2.4 GHz dongle or Bluetooth mode — use the cable.

## 7. Things that are still unknown

* **2.4 GHz dongle pages (unverified).** `config.xml` maps dongle mode to
  `MI_03` and related OEM firmware families use `0xFF59` / `0xFF60` on the
  receiver, but this device was only exercised over the cable.
* **Key remapping, macro recording, per-key lighting, clock.** The vendor tool
  uses further `04 xx` opcodes (`04 11`, `04 15`, `04 20`, `04 23`, `04 27`, …)
  that we did not capture because they were never triggered for this model.
* **Fan/polling semantics of `04 F5`.** It behaves like a heartbeat with
  `0xFF` replies while idle; nothing we implemented depends on it.
* **Whether a commit wears flash on every write.** Strongly assumed; keep
  refresh intervals ≥30 s.

## 8. Method (how this was recovered)

1. Installed the vendor driver into an isolated Wine prefix. Wine's HID parser
   rejects interface MI_00's descriptor ("HID parser values overflow"), so the
   app never saw the device. Editing its `config.xml` so the detection string
   points at `MI_01` (which Wine does enumerate) unlocked the full UI without
   patching any code.
2. Drove the UI with `xdotool` and captured real traffic three ways:
   `strace` on Wine's `winedevice.exe` (showed the 4097-byte page writes and the
   `HIDIOCSFEATURE`/`HIDIOCGFEATURE` pattern) and `usbmon` on the USB bus
   (showed the actual 64-byte control transfers and their replies).
3. Located the two vendor interfaces by diffing `report_descriptor` files, then
   wrote a minimal `hidraw` client and replayed the sequences on Linux until the
   device answered `0x01`.
4. Settled ambiguous bytes (colourful flag, `04 13` write mode, page ACK
   placement) by controlled A/B tests on the hardware, watching the screen.

## 9. Prior art

The `0xFF68` + `0xFF13` interface pair and the `04 xx` command family show up
across SONiX-based OEM LCD keyboards. These projects were invaluable for
cross-checking framing rules:

* [aula-ea75max — PROTOCOL.md](https://github.com/pedrovrima/aula-ea75max/blob/main/PROTOCOL.md)
* [aks075-linux — protocol.md](https://github.com/aar-rafi/aks075-linux/blob/main/docs/protocol.md)
* [aula-f108pro-lcd-tool-macos](https://github.com/jiarong0423/aula-f108pro-lcd-tool-macos)
* [AK820 Pro opcode gist](https://gist.github.com/TheJenos/9d5116a1dd6896388b4fb6f5fbf66ce7)

None of them documents this exact model, and the geometry, slots and the
"colourful" flag were resolved here, on the device.
