# Complete capability atlas — HISKA HX-KEG450

What the keyboard can actually do, and the bytes that do it. Everything below was
recovered statically from the vendor binary (`DeviceDriver.exe`, MFC, PE32 i386)
and cross-checked against hardware where noted. The full byte-level detail with
function addresses and decompiled evidence lives in [`OPCODES.md`](OPCODES.md);
how it was recovered is in [`RE-METHOD.md`](RE-METHOD.md).

Confidence: **[C]** verified in code or on hardware · **[I]** inferred · **[U]** unknown.

## Transports at a glance

| Channel | Where | Framing |
|---|---|---|
| Command | usage page `0xFF13` | 64-byte HID **Feature** reports (65-byte ioctl buffer, report ID `0x00`), status = byte 3 (`01` ok / `FF` busy / `00` no verdict) |
| Image | usage page `0xFF68` | 4096-byte **Output** reports (write 4097 bytes), page ACK `01 5A 02` on the same node |
| Events | MI_01, usage page `0xFFFF`/usage 1, report ID 5 | 3-byte vendor notifications, polled by the vendor app every 200 ms |
| Dongle | 2.4 GHz receiver | 33-byte frames with a byte-sum checksum at `[32]` |

Sequences are always `04 18` (begin ✓) → opcode ✓ → data block → `04 02` (commit ✓)
→ optionally `04 F0` (end). `AA 55` at the end of a block is a plain marker, not a
checksum. Commands are spaced 35 ms.

## Everything the keyboard supports

### Lighting

| Capability | Opcode | Notes |
|---|---|---|
| 19 on-board effects + Off | `04 13` + light block | mode `0x00` off, `0x01–0x13` effects, `0x80` per-key; fields: mode, R, G, B, colourful, brightness 0–5, speed 0–5, direction 0/1 **[C]** |
| Per-key static colours | `04 23` | 576-byte RGB table (144 keys) or 192-byte mono; `[8]=9` RGB, `[8]=3` mono. Device itself holds 256 records × 3 pages **[C] framing, [U] page model** |
| Real-time per-key streaming | `04 20` | 512-byte frame, repeated every ~30 ms, no begin/end wrapper — this is how the vendor drives its PC-side effects and the music-rhythm mode **[C]** |
| Read back per-key colours | `04 F5` | returns 9 × 64 B; the app decodes `[idx][R][G][B]`. See the hardware caveat below **[C]** framing, **[U]** semantics |

### Keys and macros

| Capability | Opcode | Notes |
|---|---|---|
| Key remapping, base layer | `04 11` | 576-byte table, 9 chunks, whole layer re-sent on every edit **[C]** |
| Key remapping, Fn layer | `04 27` | same table, different opcode **[C]** |
| Entry types | — | disable, key combo, macro reference, mouse, multimedia, Windows shortcut, open program, open URL, send text, consumer **[C]** |
| Macros | `04 19` + `04 15` | 3584-byte macro image: 100-entry u16 offset table + blocks; event words `B0/30/90/10/50`; key tables must be re-sent after macro writes because keys reference macro slots **[C]** |

### Settings

| Capability | Opcode | Notes |
|---|---|---|
| Disable Alt+Tab / Alt+F4 / Win | `04 17` | block bytes 2 / 3 / 4 **[C]** |
| Fn switch: momentary vs toggle | `04 17` | byte 5 **[C] field, [I] value** |
| Sleep time: off / 1 / 5 / 30 min | `04 17` | byte 6 **[C] field, [I] values** |
| Key response time (debounce) 1–5 | `04 17` | byte 8 **[C] field, [I] values** |

### Display

| Capability | Opcode | Notes |
|---|---|---|
| Boot animation / still frame (160×96 RGB565) | `04 72` + pages | 256-byte header (frame count + per-frame delay/2 ms), 255 frames max, one slot; slot = `GetCurSel()+1` **[C]** |
| Clock / RTC sync | `04 28` | time block: slot, `0x5A` magic, year−2000, month, day, hour, min, sec **[C]** |

### Device

| Capability | Notes |
|---|---|
| Firmware version | USB `bcdDevice` = `0x0106` → "1.06"; the vendor app compares it to `<firmware version="120">`, so it always thinks an update exists **[C]** |
| Firmware update | **No HID command.** Downloads and runs an external `FirmwareUpdateTool.exe`; `config.xml` `upgrade url` is empty in this build **[C]** |
| Connection/battery events | MI_01 report ID 5: `05 A6 FF 01` / `05 A6 FE 02` **[C]** frames, **[U]** meaning |
| 2.4 GHz dongle | 33-byte frames, commands `0x03/0x0D`, `0x05/0x10`, `0x05/0x01`, `0x07/0x10`, `0x09`, `0x0B/0x1C` (music bands), `0x0C/0x10`, `0x0F`, `0x10/0x12`, `0x14`, `0x1F`, `0x20/0x01` (battery poll), `0x7F`+`0x80` **[C]** |

## What does *not* exist (negative findings)

* **`04 05` device-info read** — absent from this build; identity comes from the USB
  descriptor and `HidD_GetAttributes`. (Related boards have it, and on those it can
  poison the session.)
* **No profile byte on the wire.** The three "My Exclusive Config" slots are a
  host-side database; switching a profile just re-uploads every subsystem.
* **No reset opcode.** "Reset Keyboard" re-uploads from the database.
* **No checksum** on wired blocks (`AA 55` is a marker) and the vendor app never
  validates the `01 5A 02` page ACK.
* **No firmware dump path.** Public SONiX ISP has no read-memory command; dumping
  needs SWD (`SonixQMK/sonix_dumper`).
* **No Bluetooth support** anywhere in the vendor software.

## Hardware corrections to earlier assumptions

| Earlier assumption | Measured |
|---|---|
| `04 F5` is a heartbeat poll | It returns **9 × 64 B of per-key colour records** (3 pages × 256 records on the device). The vendor app's 30 ms `04 F5 … 09` poll *is* a colour read, it just ignores the result |
| `04 72` slot is always 0 | The app sends `GetCurSel()+1`; both `0x00` and `0x01` are accepted by hardware |
| The screen might not refresh on re-upload | It does: three consecutive uploads left the panel showing the third frame, so "live" failures are client scheduling problems, not protocol ones |
| `04 F5` is a reliable "what colour is key X" oracle | **Not yet.** Setting six known solid colours did not move the values; it looks like a live LED buffer or a different layer. Needs a known-per-key write/read sweep before it can be trusted |

## Tools

| Tool | What it does |
|---|---|
| [`keg450.py`](../keg450.py) | image/GIF upload, lighting, clock — the reference implementation |
| [`live.py`](../live.py) | periodic frames, clock pushes aligned to the minute |
| [`../hx450-studio`](https://github.com/securityvisions/hx450-studio) | the desktop studio |
| `probe.py` | gated opcode prober for hardware experiments (lives in the research tree, not shipped here) |

## Safety notes

Any opcode in this document **writes device state**. Key remapping and macros can
leave a keyboard that is hard to type on until it is restored from the vendor
app's database; per-key lighting and settings are cosmetic. There is no factory
reset command, only the vendor app's re-upload. Do not poke `04 0F`-style
erase/reset opcodes from related families — they are not part of this build and
were deliberately left out of the tooling.
