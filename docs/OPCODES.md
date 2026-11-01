# HISKA HX-KEG450 vendor protocol — static opcode atlas

Source of every claim below: static analysis of the shipping Windows driver
`~/Personal/research/hid-kb/hiska/extracted/app/DeviceDriver.exe`
(PE32 i386, 1,771,008 bytes, timestamp 2025-06-11, image base 0x400000, one `.text`
of 1,391,053 bytes). `mui.dll` was only inspected for its import surface; it does **not**
contain any HID I/O (all `HidD_*`, `CreateFile*`, `WriteFile` calls are in the EXE).

No USB device was touched, nothing was executed (no Wine). Everything is read out of the
file image. Hardware-side confirmation comes from the other agent's task; where this
document cites hardware it says so explicitly.

Confidence labels used per section:

| label | meaning |
|---|---|
| **verified-in-code** | the byte construction was read directly in the disassembly/decompiler of the shipping binary; the opcode and every field offset listed can be pointed at an instruction |
| **verified-hw** | established on the real keyboard by the hardware agent (cross-check, not re-derived here) |
| **inferred** | strongly implied by adjacent code / the sibling AJAZZ AK820 protocol and the app's own XML+language files, but not directly readable out of a byte-construction instruction |
| **unknown** | not resolved; stated as such |

Addresses are **VAs** (image base 0x400000); subtract 0x400000 for the RVA/file-relative
address. Per-function decompiled C for every address cited here is checked into
`evidence/` (`FUN_00xxxxxx.c` = Ghidra decompilation of that function).

---

## 1. Transport: how the binary finds and drives the two channels

The app never matches on USB usage page 0xFF13 / 0xFF68. It enumerates every HID
interface, keeps the ones whose VID/PID/product match `config.xml`, and then classifies
them **by the interface's reported report lengths** (the values come from `HidP_GetCaps`
in the hidapi-style open helper `FUN_00450f60`; the event interface is classified by
usage page/usage from the enumeration info). This was read in `FUN_0044f950`
(RVA 0x4F950, device enumeration/classification), `evidence/FUN_0044f950.c` and
`evidence/FUN_00450f60.c`:

| condition on the HID interface | role in the app | stored at |
|---|---|---|
| `FeatureReportByteLength == 0x41` (65) | **wired command channel** — 64-byte payload + 1 report-ID byte | command device object |
| `OutputReportByteLength == 0x21 && InputReportByteLength == 0x21` (33/33) | **2.4 GHz dongle command channel** — 32-byte payload + 1 report-ID byte | dongle object |
| `UsagePage == 0xFFFF && Usage == 0x0001` | **event / notification channel** (keyboard → host) | `this+0x28` |
| `OutputReportByteLength == 0x1001` (4097) | **screen/bulk channel** — 4096-byte page + 1 report-ID byte | `this+0x2c` |

Anything else is opened and immediately closed. So the 0xFF13/0xFF68 page numbers are a
property of the firmware, not something the driver asks for: the driver just needs the
sizes 65 / 4097. (The `config.xml` `hid_interface="…&MI_00"`/`MI_03` strings are used for
matching, not for choosing the channel.)

Additional descriptor data read by the app (`FUN_00450ae0`, `HidD_GetAttributes` +
`HidD_GetProductString`/`GetManufacturerString`/`GetSerialNumberString`):

| value | source | use |
|---|---|---|
| VID / PID | `HIDD_ATTRIBUTES.VendorID/ProductID` (device object +4 / +6) | matching `config.xml` |
| USB `bcdDevice` | `HIDD_ATTRIBUTES.VersionNumber` (device object +0xc) | shown as the *firmware version* |
| product / manufacturer / serial | `HidD_Get*String` | UI |

This is why there is **no wired `04 05` "device info" command** in this build (§8).

---

## 2. The five send primitives (verified-in-code)

All device I/O goes through these. Everything later in this document is "which 64 bytes
are handed to which primitive, in what order".

| # | name | VA | RVA | signature (thiscall) | behaviour |
|---|---|---|---|---|---|
| 1 | `FEAT_WRITE` | `0x44edb0` | `0x4EDB0` | `(u8* data, u32 len, bool want_ack) -> bool` | one 65-byte `HID Feature` report if `len <= 65`; if `len > 65` splits into `len>>6` chunks of 64 bytes, one report per chunk. `Sleep(cmd_delaytime)` = **35 ms** before every report (field `this+0x30`, set from `layouts/rgb-keyboard.xml <cmd_delaytime value="35"/>`). Single packet + `want_ack` → after another 35 ms it reads the reply back into the same 65-byte buffer and returns `true` iff **report byte 4 == 0x01** (= payload byte 3). Multi-chunk path never reads a reply; retries a failed chunk up to 10× |
| 2 | `FEAT_WRITE_FAST` | `0x44efa0` | `0x4EFA0` | `(u8* data, u32 len) -> bool` | same framing but 5 ms sleeps (2 ms between multi-chunks, abort if a block takes > 100 ms); after the single-packet write it reads the reply, **retries once if report byte 4 == 0xFF**, and returns `true` only if the (final) reply's byte 4 is `0x01`. Multi-chunk calls sleep 2 ms/chunk and effectively return `true` (they check neither reply nor ack). Used only for real-time LED streaming |
| 3 | `FEAT_QUERY` | `0x44f280` | `0x4F280` | `(u8* data, u32 packets)` | `Sleep(3)`, `SetFeature(data)`, `Sleep(5)`, poll reply up to 10× (5 ms apart) until **report byte 4 == 0x01**; then read up to `packets` (max 9) more 64-byte feature reports and copy each payload sequentially into `data[0..]`. Only caller: `04 F5` |
| 4 | `DONGLE_SEND` | `0x44f4d0` | `0x4F4D0` | `(u8* data, u32 len)` | 2.4 GHz only: stores `checksum = sum(data[0..len-1]) & 0xFF` at `data[32]`, writes a **33-byte Output report** `[00][payload 32]` with `WriteFile`. `Sleep(5)`, then up to 20 × `read(33 bytes, 5 ms)` matching the reply's bytes `[1..3]` against the request's `[1..3]` (cmd/len/idx echo); on match copies the reply over `data`; 10 ms between attempts |
| 5 | `BULK_PAGE_WRITE` | `0x44f1b0` | `0x4F1B0` | `(void* page)` | screen only: builds `[00] + 4096 bytes`, `WriteFile` 4097-byte Output report, then `hid_read_timeout(4097 bytes, 300 ms)` — **the reply is read but never checked** (no `01 5A 02` validation anywhere in the binary) |

Underneath them are the thin hidapi-style wrappers (same file, `FUN_00451320` =
`HidD_SetFeature`, `FUN_004513c0` = feature read-back through
`DeviceIoControl(handle, 0xB0192, same buffer as in+out, …)` — i.e. read the
device's reply *and* status into the same buffer, `FUN_00451100` = `WriteFile` output
report, `FUN_004511e0` = `ReadFile` with timeout).

**Status / reply semantics (verified-in-code, matches hardware):** the status byte is
**report byte 4** = **payload byte 3**: `0x01` = accepted, `0xFF` = rejected (fast path
retries), anything else = treated as "no ack". `FEAT_WRITE` and `FEAT_QUERY` require
`== 0x01`; `FEAT_WRITE_FAST` retries on `== 0xFF` and otherwise requires `== 0x01`. Note that data blocks > 65 bytes
(tables, macro image, screen pages) are **never acked** by the app even when it passes
`want_ack=1` — the multi-chunk branch of `FEAT_WRITE` doesn't read anything.

---

## 3. Wired command inventory (`0xFF13`, 64-byte HID Feature reports)

`04 xx` = class 0x04 + opcode. Report on the wire is `[report-ID 0x00][payload[0..63]]`.
Unless noted, bytes not listed are 0x00, and the trailing `AA 55` appears at the offset
given by each payload format below (it is a plain end-of-data marker, not a checksum —
verified by reading where the constant `0x55AA` is written in each builder).

| opcode | name | header fields | followed by | end of session | builder (VA / RVA) |
|---|---|---|---|---|---|
| `04 18` | begin / open session | payload = `04 18 00…` | — | — | every command builder, e.g. `0x4141a0` / `0x141A0` |
| `04 02` | commit / apply | payload = `04 02 00…` | — | — | every command builder |
| `04 F0` | end session | payload = `04 F0 00…` | — | — | `0x418550`, `0x42afd0`, `0x433fd0`, `0x44b7f0`, `0x44b900` |
| `04 13` | lighting-mode parameters | `[2]=0`, `[8]=1` | one 64-byte light block (no ack) | yes (`04 F0`) | `0x42afd0` / `0x2AFD0`; also `0x433fd0`, `0x44b7f0` |
| `04 17` | system settings | `[2]=1`, `[8]=1` | one 64-byte settings block (no ack) | **no** | `0x4141a0` / `0x141A0` |
| `04 11` | key map, base layer | `[2]=0`, `[8]=9` | 576-byte table (9 chunks) | yes (`04 F0`, acked) | `0x418550` / `0x18550` |
| `04 27` | key map, Fn layer | same, opcode 0x27 | 576-byte table | yes | `0x418550` |
| `04 19` | macro session begin | payload = `04 19 00…` | — (this is the *begin* of a macro write, not a wrapper for `04 15` in general) | — | `0x42d5d0` / `0x2D5D0` |
| `04 15` | macro memory image | `[8]=n` packets | `n × 64` bytes macro image; 30 ms before and after | **no** | `0x42d5d0` |
| `04 23` | per-key static colours | `[8]=9` (RGB) or `[8]=3` (mono) | 576-byte RGB table or 192-byte mono table | yes (`04 F0`) | `0x44b900` / `0x4B900` |
| `04 20` | real-time per-key frame | `[2]=0`, `[8]=8` (RGB) or `[8]=3` (mono) | 512-byte RGB frame or 192-byte mono frame; then `04 02` — **no `04 18`/`04 F0`** | — | `0x427f60` / `0x27F60`, `0x4328c0` / `0x328C0` |
| `04 F5` | read current per-key colours | `[8]=9` or `[8]=3` | `FEAT_QUERY` returns 9×64 / 3×64 bytes | — | `0x42adb0` / `0x2ADB0` |
| `04 28` | clock / RTC sync | `[8]=1` | one 64-byte time block (acked) | **no** | `0x423a80` / `0x23A80` |
| `04 72` | screen/animation metadata | `[2]=slot`, `[8..9]=chunk count LE16` | N × 4096-byte pages on the 4097-byte Output interface, each followed by a 4097-byte read (ignored) | **no** | `0x422ac0` / `0x22AC0` |

Opcodes that do **not** exist on the wired channel in this build: `04 05` (device info),
`04 0F`, `04 12`, `04 1F`, `04 03`, `04 04`, `04 06`, `04 09`, `04 0B`, `04 0C`, `04 14`.
A complete sweep of every call site of every send primitive (§METHOD) produced exactly
the 15 rows above and nothing else.

### 3.1 Sequence notation used in this document

`✓` = sent as a single packet with `want_ack=1`, so the app reads the reply and treats
payload byte 3; `·` = `want_ack=0` (no reply read); **[bulk]** = multi-chunk write, never
acked.

```
lighting      : 04 18 ✓ | 04 13 ✓ | light block ·      | 04 02 ✓ | 04 F0 ·
key map       : 04 18 ✓ | 04 11|27 ✓ | 576 B [bulk]    | 04 02 ✓ | 04 F0 ✓
per-key (user): 04 18 ✓ | 04 13 ✓ | mode-80 blk ·      | 04 02 ✓ | 04 F0 ·
                04 18 ✓ | 04 23 ✓ | colour table [bulk] | 04 02 ✓ | 04 F0 ·
settings      : 04 18 ✓ | 04 17 ✓ | settings block ·   | 04 02 ✓
clock         : 04 18 ✓ | 04 28 ✓ | time block ✓       | 04 02 ✓
macros        : 04 19 ✓ | 04 15 ✓ | 30 ms | image [bulk] | 30 ms | 04 02 ✓
TFT screen    : 04 18 ✓ | 04 72 ✓ | N × (4096 B [bulk]) | 04 02 ✓
real-time LED : 04 20 (fast) | frame [fast, ≤3 chunks] | 04 02 (fast)     (repeat every ~30 ms)
read back LED : 04 F5 (query) → 8 × 64 B | 04 02 ✓
```

---

## 4. Data-block layouts

### 4.1 Lighting-mode block — `04 13`, 64 bytes — **verified-in-code**

Builder `FUN_0042afd0` (RVA 0x2AFD0), `evidence/FUN_0042afd0.c`.

| off | size | field | value |
|---|---|---|---|
| 0 | 1 | mode | `[this+0x804]` — the selected lighting mode (UI/DB value). `0x00` = off, `0x01..0x13` = on-board modes, `0x80` = per-key "user lighting" |
| 1 | 1 | R | `COLORREF & 0xFF` (Windows COLORREF is `0x00BBGGRR`) |
| 2 | 1 | G | `(COLORREF >> 8) & 0xFF` |
| 3 | 1 | B | `(COLORREF >> 16) & 0xFF` |
| 4–7 | 4 | 0 | |
| 8 | 1 | colorful | 1 = effect uses its own multi-colour palette, 0 = use R/G/B above |
| 9 | 1 | brightness | 0–5 (`<light … brightness_max="5"/>` in the layout XML) |
| 10 | 1 | speed | 0–5 (`speed_max="5"`) |
| 11 | 1 | direction | 0 / 1 |
| 12–13 | 2 | 0 | |
| 14–15 | 2 | `AA 55` | end marker |
| 16–63 | 48 | 0 | |

When `mode == 0` only byte 0 is non-zero (and `AA 55` at 14–15).
The values come from a DB record loaded by `FUN_00408520`; the layout of the block is
exactly the AK820 family layout. The *names* of modes 1..19 are **inferred** from
`language/1033.lan` IDs 521–540 and the XML `default_mode="11"` (§9).

Two variants of the block are used when entering per-key mode:

* `FUN_0044b7f0` (RVA 0x4B7F0) — `04 18 ✓`, `04 13 ✓` (`[8]=1`), then block
  `80 00 00 00  00 00 00 00  00 <brightness> 00 00 00 00 AA 55`, then `04 02 ✓`, `04 F0 ·`.
  Used as the preamble of `04 23` uploads.
* `FUN_00433fd0` (RVA 0x33FD0) — same wrapper but block = `[0]=[this+0x808]` (the mode
  byte, in practice 0x80), `[9]` left 0, `AA 55` at 14–15. Used by "user lighting /
  real-time lighting" page activation.

### 4.2 System-settings block — `04 17`, 64 bytes — **verified-in-code**

Builder `FUN_004141a0` (RVA 0x141A0), `evidence/FUN_004141a0.c`. The app reads each field
from the profile DB (keys are literal wide strings in the binary: `disable_alttab`,
`disable_altf4`, `disable_win`, `fn_switch`, `sleep_time`, `key_respondtime`) and
`GetCheck()` state of the three checkboxes.

| off | size | field | value | UI origin |
|---|---|---|---|---|
| 0 | 1 | 0 | constant 0 | |
| 1 | 1 | 1 | constant 1 | |
| 2 | 1 | disable Alt+Tab | 0/1 | checkbox `[this+0x678]` (config key `disable_alttab`) — **verified-in-code** |
| 3 | 1 | disable Alt+F4 | 0/1 | checkbox `[this+0x67c]` (`disable_altf4`) — **verified-in-code** |
| 4 | 1 | disable Win | 0/1 | checkbox `[this+0x680]` (`disable_win`) — **verified-in-code** |
| 5 | 1 | fn_switch | 0 = momentary, 1 = toggle (**inferred** from language IDs 293/294; value is passed through from config key `fn_switch` verbatim) | "Fn Switch Function" combo |
| 6 | 1 | sleep_time | 0 = no sleep, 1 = 1 min, 2 = 5 min, 3 = 30 min (**inferred** from language IDs 142–145; value is the combo index read from config `sleep_time`) | "Sleep Time" combo |
| 7 | 1 | 0 | | |
| 8 | 1 | key_respondtime | 1–5 = debounce level (**inferred** from language IDs 600–624; config `key_respondtime` verbatim) | "Key Response Time" combo |
| 9–61 | 53 | 0 | | |
| 62–63 | 2 | `AA 55` | end marker | |

### 4.3 Clock block — `04 28`, 64 bytes — **verified-in-code**

Builder `FUN_00423a80` (RVA 0x23A80), `evidence/FUN_00423a80.c`. `GetLocalTime()` →
`SYSTEMTIME`, then:

| off | size | field |
|---|---|---|
| 0 | 1 | 0 |
| 1 | 1 | screen slot = `LCDViewList::GetCurSel()+1` (1-based; = 1 for this model, `gif_count="1"`) |
| 2 | 1 | `0x5A` magic |
| 3 | 1 | year − 2000 (`wYear % 2000`) |
| 4 | 1 | month |
| 5 | 1 | day |
| 6 | 1 | hour |
| 7 | 1 | minute |
| 8 | 1 | second |
| 9 | 1 | 0 |
| 10 | 1 | day of week, Sunday = 0 (`SYSTEMTIME.wDayOfWeek`) |
| 11–61 | 51 | 0 |
| 62–63 | 2 | `AA 55` |

The block itself is sent with `want_ack=1` (the reply is read), unlike the light/settings
blocks.

### 4.4 Key-map table — `04 11` / `04 27`, 576 bytes — **verified-in-code**

Builder `FUN_00418550` (RVA 0x18550), `evidence/FUN_00418550.c`. The table is rebuilt from
the app's SQLite DB (`SELECT * FROM t_key_macro_data WHERE profile=? and fn_layer=?`) each
time a key is remapped — there is no "change one key" command on the wired channel; the
whole layer is re-sent. Layout: entry for key index *k* at `4*k .. 4*k+3`, `k` from the
layout XML `key_index` attribute; `AA 55` at 574–575; unused entries are 4× `0x00`
("use factory default"). Keys in the Fn-layer-disabled set are forced to 0 on the Fn layer.

`macro_type` (DB) → entry bytes (all **verified-in-code**, `FUN_00418550` cases):

| DB type | UI meaning (lang IDs 201–228) | entry bytes |
|---|---|---|
| (none) | Default Key | `00 00 00 00` |
| 1 | Disable Key | `05 03 00 00` |
| 2 | Keyboard key / Multi-Key | `02 <mod-bitmask> <HID usage> 00`; bare modifier target (usage E0–E7) with no extra mod → `02 <1<<(usage-E0)> 00 00` (translation table `FUN_00451b80`: E0→01, E1→02, E2→04, E3→08, E4→10, E5→20, E6→40, E7→80) |
| 3 | Macro Definition | `06 <macro slot> <play mode> <repeat count>` (slot = index of the macro in the macro list) |
| 4 | *(UI label unknown; row is skipped)* | writes nothing → `00 00 00 00` |
| 5 | Mouse Function | `01 01 <btn> 00`: btn `01` Left, `04` Middle, `02` Right, `03` "double click"; wheel: `01 03 01 00` up, `01 03 FF 00` down; `01 01 08 00` = "Mouse Button 5", `01 01 10 00` = "Mouse Button 4" (UI mapping per lang 220–228) |
| 6 | Multimedia | `03 <usage> 00 00`: `CD` Play/Pause, `B7` Stop, `B6` Prev, `B5` Next, `E9` Vol+, `EA` Vol−, `E2` Mute |
| 7 | Windows Shortcut | `02 <mod> <usage> 00`: `08 07` Win+D, `08 08` Win+E, `08 0F` Win+L, `01 1A` Ctrl+W, `04 2B` Alt+Tab, `01 06` Ctrl+C, `01 19` Ctrl+V, `01 1B` Ctrl+X; value 10 → `02 02 00 00`, value 11 → `02 01 00 00` |
| 8, 9, 10, 11 | Open Program / Open Website / Send Text / other software function | `05 02 <id> 00` — the keyboard reports `<id>` back on the event channel and the **host** performs the action |
| 12 | Consumer / app key | `03 <usage_lo> <usage_hi> 00`; mapping table `FUN_00419450` (verified-in-code): id 500→`94 01` (My Computer 0x0194), 501→`92 01` (Calculator 0x0192), 502→`8A 01` (Mail 0x018A), 503→`83 01` (Media 0x0183), 504→`CD 00`, 505→`B7 00`, 506→`B6 00`, 507→`B5 00`, 508→`E9 00`, 509→`EA 00`, 510→`E2 00`, 511→`23 02` (WWW Home 0x0223), 512→`27 02` (Refresh), 513→`26 02` (Stop), 514→`24 02` (Back), 515→`25 02` (Forward), 516→`2A 02` (Favourites), 517→`21 02` (Search) |
| 13 | *(Fn-layer function, UI label unknown)* | `07 <b1> <b2> <b3> 00` — three bytes copied straight from DB columns 0x1c/0x20/0x24 |

### 4.5 Macro memory image — `04 19` + `04 15`, n×64 bytes — **verified-in-code**

Builder `FUN_0042d5d0` (RVA 0x2D5D0), `evidence/FUN_0042d5d0.c`. A fixed 3584-byte
(0xE00) buffer, pre-zeroed:

```
0x000 .. 0x18F   100 × u32 LE offset table (one entry per macro slot)
                 entry = u16 LE offset of the macro block, bytes 2..3 = 00
                 empty slot = HE  FF FF FF FF
0x190 ..         macro blocks, back to back:
                 u16 LE event_count, 6 × 00, then event_count × 4-byte events
```

4-byte event encoding (byte 3 = type, byte 2 = code, bytes 0–1 = argument):

| type | bytes 2–3 | meaning |
|---|---|---|
| `B0` | `00 <HID usage>` | key down (`FUN_00451b80`-family parser; usage = HID keyboard usage) |
| `30` | `00 <HID usage>` | key up |
| `90` | `<mask> 00` where mask `01` left / `04` middle / `02` right | mouse button down |
| `10` | same masks | mouse button up |
| `50` | `<delay u16 LE> 00` with minimum 10 ms | delay (every recorded action is followed by one of these) |

Packet count: `n = size/64 + (size modulo 64 ? 2 : 1)` where `size` = end of last used
block. `AA 55` is written at buffer offset `n*64 − 2`. The header `04 15` carries
`[8] = n`; the image is then sent as one `n*64`-byte `FEAT_WRITE`, preceded and followed by
`Sleep(30)`.

### 4.6 Per-key static colours — `04 23` — **verified-in-code**

Builder `FUN_0044b900` (RVA 0x4B900), which first calls `FUN_0044b7f0` (mode-entry block,
§4.1). Then, depending on board type `[this+0x7ac]` (RGB vs single-colour):

| board | `04 23 [8]` | table | last two bytes |
|---|---|---|---|
| RGB | 9 | 576 bytes: for each lit key index *k* → `[k, R, G, B]` at `4*k` | `AA 55` at 574–575 |
| single-colour | 3 | 192 bytes: byte *k* = `0xFF` if the key is on, else 0 | `AA 55` at 190–191 |

Only keys whose colour value is non-zero are written; key index 0 is skipped. The app
fills the table from the on-screen keyboard model, sorted by key index.

**This is the writer that pairs with the `04 F5` reader.** Both sides use the same 4-byte
record `[key_index][b1][b2][b3]` (`[key_index,R,G,B]` as the app builds it). Two
constraints are worth stating explicitly because they shape the pairing:

* The app writes only **144 records** (`[8]=9`, 576 B) and reads only **144 records**
  (`FEAT_QUERY` hard-caps at 9 × 64-byte reads, `FUN_0044f280`). The firmware page is
  larger: the hardware agent measured `04 F5 <page>` returning **16 × 64-byte reports =
  1024 B = 256 records**, pages 0/1/2 (three on-board profile pages). So the app sees only
  the first 144 records of page 0 and never touches pages 1/2.
* Neither builder sets payload byte 2 (the page argument) or byte 3: both stay 0. The app
  therefore always reads and writes **page 0** and has no feature that targets the other
  two firmware pages (the app's "3 profiles" are host-side SQLite profiles, not these
  pages). A reimplementation that wants to read/write pages 1/2 must set byte 2 itself —
  the vendor app never does.

The volatile analogue is `04 20` (§4.7): 512 B = 128 records streamed to the live LED
buffer, brightness pre-scaled, no persistence, no page byte.

### 4.7 Real-time per-key frame — `04 20` — **verified-in-code**

Builder `FUN_00427f60` (RVA 0x27F60), duplicate for the music page `FUN_004328c0`
(RVA 0x328C0), `evidence/FUN_00427f60.c`.

| offset | field |
|---|---|
| `[0]=0x04`, `[1]=0x20`, `[2]=0`, `[8]=8` (RGB) / `3` (mono) | header, sent with `FEAT_WRITE_FAST` |

Frame body:

* RGB: 512 bytes = 128 × `[key_index, R, G, B]`, entries in ascending key-index order
  (iteration over a `std::map` keyed by key index). R/G/B are pre-scaled:
  `value = colour * brightness / 256` where `brightness` is the key item's brightness
  field (0–255). 8 feature reports.
* mono: 192 bytes, one brightness byte per key index. 3 feature reports.

Then `04 02` (also via `FEAT_WRITE_FAST`). No `04 18`, no `04 F0`. While the "Real-time
Lighting" or "Music Rhythm" page is open the worker thread repeats this every ~30 ms
(see §7).

### 4.8 Read-back — `04 F5` — **verified-in-code**

Builder `FUN_0042adb0` (RVA 0x2ADB0), `evidence/FUN_0042adb0.c`. Only runs when the
device is in USB mode (`[this+0x784]==0`).

1. `Sleep(10)`, payload `04 F5 00 00 00 00 00 00 <9|3> 00…` — **byte 2 (page) = 0**.
2. `FEAT_QUERY(payload, 9)` / `(…,3)` → status byte must be 1, then 9 (or 3) × 64-byte
   feature reports are copied sequentially into the same 600-byte buffer.
3. `04 02 ✓`.
4. The reply buffer has exactly the layout of the `04 23` table: `[key_index, R, G, B]` at
   `4*k` (RGB) or one brightness byte per key (mono). The app parses it and colourises the
   on-screen keyboard preview. This is the only true "read device state" command in the
   build.

**Hardware cross-check (input from the hardware agent, taken as given here):**

* the device returns **16 × 64-byte reports per page** (1024 B = 256 records) for pages
  **0/1/2**; the app only ever asks for 9 reports of page 0, so its own view is truncated
  to 144 records;
* the record framing `[key_index][b1][b2][b3]` matches what the app expects, but the
  colour channel encoding is **not plain 0–255 R,G,B**: magenta read back as
  `7e 00 fd`, whereas the app would emit `[k, FF, 00, FF]` through `04 23` and then would
  have decoded `7e 00 fd` as COLORREF `0x00FD007E` (R=0x7E, G=0, B=0xFD) — magenta-ish,
  but not the value it wrote. Possible explanations (not distinguishable from this binary
  alone): the firmware applies a per-channel brightness/curve on readback, or the three
  bytes are a different channel encoding that happens to be `[R,G,B]`-shaped only for the
  app's own use. **The writer side here is verified byte-exact; the readback channel
  decoding is unknown.** Resolve with a hardware sweep that writes known (R,G,B) values
  through `04 23` and reads them back through `04 F5`.

### 4.9 Screen / boot-animation upload — `04 72` + bulk pages — **verified-in-code**

Builder `FUN_00422ac0` (RVA 0x22AC0), `evidence/FUN_00422ac0.c`.
Everything below was read out of that one function.

Image buffer (heap, pre-filled with `0xFF`):

```
[0]              frame_count = min(list length, <gif_maxframes> = 255)
[1 .. N]         per-frame delay byte = max(1, delay_ms / 2)      (2 ms units, integer division)
[256 ..]         frames, each width*height*2 bytes of RGB565 little-endian,
                 row-major as produced by mui.dll!LCDViewList::GetImageRGB565Data(CString file, BYTE* out, int w, int h)
```

* header length = `<gif_headlength>` = 256 (XML); width/height taken from the layout XML
  (160 × 96), frame bytes = 160*96*2 = 30,720.
* total size = `256 + frames*30720`; `chunks = ceil(size / 4096)`.
* buffer allocation size = `size + 0x1000`, filled with 0xFF.
* image file path per frame comes from the LCDViewList item (`+0x14`) and the delay from
  item `+0xc`.

Sequence:

```
04 18 ✓
04 72 [2]=slot [8]=chunks&0xFF [9]=chunks>>8   ✓      slot = LCDViewList::GetCurSel()+1
for each chunk: [bulk] 4097-byte Output report (0x00 + 4096 bytes), then read 4097 bytes 300 ms (ignored)
04 02 ✓
```

The per-page read is only there to drain the interface; the app does **not** validate the
`01 5A 02` page acknowledgement. A UI progress bar is updated between pages
(`MProgress::SetProgress`, `MControlFont::SetTitle`).

### 4.10 Profiles and layers — what is on the wire and what is not

* **Layers** are a wire concept: base vs Fn is the only difference between `04 11` and
  `04 27` (2.4 GHz: cmd `0x10` vs `0x12`). The Fn layer additionally has a
  "cannot be remapped" key set (`[this+0x82c]`, a `std::set` of key codes populated from
  the layout XML `fnlayer_disable`; empty for this model's XML but checked in
  `FUN_00418550`) — those entries are forced to `00 00 00 00` on the Fn layer. The
  Momentary/Toggle behaviour of the Fn key itself is not a layer command; it is settings
  block byte 5 (`fn_switch`).
* **Profiles are host-side only.** There is no profile byte in any wired or 2.4 GHz
  payload (verified by reading every builder: bytes that could carry an id are always fixed
  0). The app keeps 3 profiles in SQLite (`t_profile_data`, and `profile` is a column of
  `t_key_macro_data`, `t_config_data`, `t_macro_data`, `t_light_data`, …). Selecting a
  profile simply rebuilds every payload from that profile's rows and re-uploads the
  affected subsystems (task 14 / task 6/8/9/11/12/16/19). Therefore two tools can fight
  each other: whichever writes last defines the keyboard state, and the app's DB is just
  its own memory of what it last wrote.
* The hardware-observed **pages 0/1/2 of the `04 F5`/`04 23` per-key store are a firmware
  concept** the vendor app does not expose — it always uses page 0 (§4.6/§4.8). Do not
  confuse them with the app's 3 profiles.

---

## 5. 2.4 GHz dongle command family — **verified-in-code** (secondary channel)

The 2.4 GHz receiver is the same VID/PID (`05AC:024F` per `config.xml`, `mode value="2"`,
`MI_03`) and is reachable in this build; the screen upload is disabled over 2.4 GHz
(`<menu_wireless … screen="0"/>`), the rest is implemented. Framing:

```
33-byte Output report: [0]=report-ID 0x00, [1]=cmd, [2]=len, [3]=idx,
                       [4 .. 31] = data (28 bytes max), [32] = sum(bytes[0..31]) & 0xFF
reply: same 33-byte report read back; the app matches reply[1..3] == request[1..3]
```

| cmd | len/idx | meaning | wired equivalent | builder |
|---|---|---|---|---|
| `0x03` / `0x0D` | len 4 | set **one** key (base / Fn layer) — `[4]=key_index`, followed by 4-byte action | one entry of `04 11`/`04 27` | `0x419130` / `0x19130` |
| `0x05` | `0x10` | lighting-mode block, 16 data bytes at `[4..19]` (same layout as §4.1, `AA 55` at data 14–15) | `04 13` | `0x42b170` / `0x2B170` |
| `0x05` | `0x01` | enter per-key/user lighting mode (`[4]=mode`, in practice 0x80, `[5]=brightness`, `AA 55` at data 14–15) | `04 13` mode block | `0x434900` / `0x34900` |
| `0x07` | `0x10` | system-settings block, 16 data bytes at `[4..19]` (same layout as §4.2) | `04 17` | `0x414410` / `0x14410` |
| `0x09` | slice len | macro image, 28-byte slices, `[3]` = slice index, last slice `[2]` = remaining length; same 3584-byte image as §4.5 | `04 19`+`04 15` | `0x42db50` / `0x2DB50` |
| `0x0B` | `0x1C` | music-visualiser spectrum frame: `[4]` foreground mode, `[5]` background mode, `[6]` amplitude, `[7..27]` 21 band levels | none over USB (USB streams `04 20`) | `0x432b00` / `0x32B00` |
| `0x0C` | `0x10` | clock block, 16 data bytes at `[4..19]` (same layout as §4.3) | `04 28` | `0x423bb0` / `0x23BB0` |
| `0x0F` | 0 | sent once at the start of the factory-reset re-sync when the device is in 2.4 GHz mode | (wired reset = re-upload everything, no command) | `0x434ee0` / `0x34EE0` |
| `0x10` / `0x12` | slice len | key table, base / Fn layer, 28-byte slices, 576 bytes total, last slice `[2]` = remaining length | `04 11` / `04 27` | `0x418df0` / `0x18DF0` |
| `0x14` | slice len | per-key colour table (576/192 bytes) in 28-byte slices | `04 23` | `0x44bd70` / `0x4BD70` |
| `0x1F` | 0 | task 13, purpose unknown, sent by the worker thread when the UI queues task 13 | — | `0x434230` inline |
| `0x20` | `0x01` | battery query; reply `[0]=0x20`, `[3]=battery %` clamped 0–100; polled at most every 15 s in 2.4 GHz mode | — | `0x4357d0` / `0x357D0` |
| `0x7F` | `0x03` | screen upload header: `[4]`=slot, `[5..7]`=24-bit packet count; then `0x80|(i>>16)` data frames with 28 bytes each | `04 72` | `0x423360` / `0x23360` |
| `0x80 \| (i>>16)` | i&0xFF etc. | screen data frame | bulk pages | `0x423360` |
| `0x02` | — | dongle↔keyboard link handshake in the *sibling* AJAZZ build — **not sent by this build** (no call site). Link state is learned from the event channel instead | — | — |

---

## 6. Event / notification channel (keyboard → host) — **verified-in-code**

Classified by `UsagePage == 0xFFFF && Usage == 0x0001` (`FUN_0044f950`). Polled by
`FUN_00435160` (RVA 0x35160) at most every **200 ms** with `read(65 bytes, 10 ms timeout)`:

| report | meaning |
|---|---|
| `05 A6 FF 01 …` | 2.4 GHz link came up → app re-reads device state |
| `05 A6 FE 02 …` | 2.4 GHz link went down / keyboard asleep |
| `05 A6 <id> …` | key bound to a "software function" (`05 02 <id> 00` in the key table) was pressed; host runs the stored action |

This is the only inbound path besides feature-report acks and the battery reply.

---

## 7. Threading / scheduling constants that shape the protocol

Read in the worker-task dispatcher `FUN_00434230` (RVA 0x34230) and the idle branch at its
tail; useful when reimplementing timing.

* One worker thread drains a task queue; at most **one task per 30 ms tick**.
* Task → device operation map (wired / 2.4 GHz):

| task | handler (wired) | handler (2.4 GHz) | meaning |
|---|---|---|---|
| 1 | `0x434700` | — | initial connect setup |
| 2/3/4 | `0x41f650`+`0x41f8f0`(+`0x4357d0`) | same | re-enumerate / connection state / battery |
| 5 | `0x434750` | — | reload all UI pages from DB + clock sync |
| 6 | `0x434840` → `0x418550` ×2 | `0x418df0` ×2 | upload both key layers |
| 7 | `0x4348a0` → `0x418550` | `0x419130` | upload one layer / one key |
| 8 | `0x42d5d0` | `0x42db50` | upload macros |
| 9 | `0x42afd0` | `0x42b170` | lighting mode |
| 10 (0xA) | `0x433fd0` | `0x434900` | enter user-lighting mode |
| 11 (0xB) | `0x44b900` | `0x44bd70` | per-key colours |
| 12 (0xC) | `0x4141a0` | `0x414410` | system settings |
| 13 (0xD) | — | inline cmd `0x1F` | unknown |
| 14 (0xE) | `0x434ee0` (re-sync with bitmask + 2.4G `0x0F`) | — | factory-reset re-upload |
| 15 (0xF) | `0x434e80` | — | save GIF image **file** (local, `SaveGIFImageFile`) |
| 16 (0x10) | `0x422ac0` | `0x423360` | screen upload |
| 17 (0x11) | `0x4349a0` | — | updater/website helper flow (no device command) |
| 19 (0x13) | `0x434ec0` → `0x423a80` | `0x423bb0` | clock sync |
| 20 (0x14) | `0x4352b0` | — | download & run "Keyboard-Software-Setup.exe" (driver update) |
| 21 (0x15) | `0x435540` | — | download & run "FirmwareUpdateTool.exe" (firmware update) |

* Idle branch (no task queued), by current tab index `[this+0x540]`:
  page 3 (Lighting Mode) → `04 F5` read-back **every tick**;
  page 6 (Real-time Lighting) → `04 20` frame every tick;
  page 7 (Music Rhythm) → `0x4332c0` (renders spectrum, then `04 20` over USB or `0x0B`
  over 2.4 GHz);
  then `0x4357d0` (battery poll, 2.4 GHz, ≥15 s apart) and `0x435160` (event read,
  ≥200 ms apart).
* `FEAT_WRITE` pacing is the XML's `cmd_delaytime` 35 ms; `FEAT_WRITE_FAST` calls
  `timeBeginPeriod(1)` and uses 5 ms / 2 ms.

---

## 8. Opcodes / features that are absent from the binary (negative findings)

These were specifically looked for and are **not** present in this build:

| item | finding |
|---|---|
| wired `04 05` "device info / VID-PID readback" | **absent — confirmed twice**: no builder exists in this EXE (complete sweep of all send primitives) and the hardware agent's independent scan of the protocol did not find it either. Device identity comes from the USB descriptors (`HidD_GetAttributes` → VID/PID/bcdDevice, `HidD_GetProductString`…); the app never displays data returned by a device command other than `04 F5` and the 2.4 GHz battery reply. |
| wired `04 19` used as a general session opener | only used as the *macro upload begin*, immediately followed by `04 15` |
| wired reset / restore-defaults command | **none**. "Reset Keyboard" / "Factory Reset" delete rows in the host SQLite DB and then the worker re-uploads every subsystem (task 14 with a capability bitmask at `[this+0x7a4]`: 0x2 → `0x42c720`, 0x1 → `0x4170d0`, 0x4 → `0x42a580`, 0x100 → `0x433e20`, 0x10 → `0x449370`, 0x8 → `0x4277d0`, 0x20 → `0x432cd0`, 0x80 → screen if an LCD frame exists). On 2.4 GHz a `0x0F` frame is sent first. |
| firmware version read over HID | **absent** — it is the USB `bcdDevice` (`HIDD_ATTRIBUTES.VersionNumber`, stored as device-object field +0xc by `FUN_00450ae0`); hardware reports **0x0106**, shown as hex. The XML `<firmware version="120">` is the app-side expected version used by the update check (the exact comparison predicate was not traced). |
| firmware update over HID | **absent**. Updating downloads a ZIP over WinINet and runs an external EXE: strings `%s\temp\FirmwareUpdateTool.zip`, `%s\temp\FirmwareUpdateTool.exe`, `Keyboard-Software-Setup.zip/.exe`; handlers `0x4352b0` (driver) and `0x435540` (firmware). For this model the XML has `firmware file="" url=""`, so the update UI has nothing to offer. No flash/ISP opcodes exist in this binary. |
| profile number in any packet | **absent**. Three profiles exist only in the host DB (`t_profile_data`); every command is rebuilt from the current profile's rows. No byte in any payload carries a profile id. |
| layer id other than via `04 11` vs `04 27` | wired: only the opcode selects the layer. 2.4 GHz: cmd `0x10` base vs `0x12` Fn. |
| side-light command | layout XML has `sidelight="0"`; no side-light-specific builder exists in the binary (the light block's mode byte range would carry it in the sibling builds). |
| checksum on the wired channel | **none** — the wired payloads have no checksum; only the 2.4 GHz 33-byte frame has one (sum of bytes). |
| page ACK validation (`01 5A 02`) | **not checked**; `0x44f1b0` reads 4097 bytes with a 300 ms timeout and discards them. |

---

## 9. UI feature → opcode mapping (nothing in the vendor UI is unaccounted for)

Sources for the feature list: `app/layouts/rgb-keyboard.xml` capability flags,
`app/language/1033.lan` tab/menu strings (UTF-16LE), the SQLite schema strings embedded in
the EXE, and the builder functions in §3. Tabs are language IDs 51–58.

| UI feature (language id) | layout flag | what is sent | confidence |
|---|---|---|---|
| **Global: apply / save** ("Apply" = 22, per-page OK/Apply) | — | 04 18 → opcode(s) → data → 04 02 → (04 F0 when the builder does) | verified-in-code |
| **My Exclusive Config** (51/65), 3 profiles | — | host-side DB only; switching profile re-runs the per-subsystem uploads (task 14 bitmask). No profile byte on the wire | verified-in-code |
| **Macro Manager** (52) | `macro="1"` | 04 19, 04 15 (`[8]=n`), 3584-byte macro image, 04 02. Key tables are re-sent afterwards because macros are referenced by slot number | verified-in-code |
| **Lighting Mode** (53) | `light_mode="1"` | 04 18, 04 13, light block, 04 02, 04 F0 | verified-in-code |
| · Lighting Color / Palette / Colorful / Brightness / Speed / Direction (79–85) | `<light brightness_max="5" speed_max="5" default_mode="11" default_brightness="5" default_speed="3"/>` | fields 1–3, 8, 9, 10, 11 of the light block | verified-in-code |
| · LED Off / 19 on-board modes | (lang 521–540) | light block mode byte 0 / 1–19 | mode numbering **inferred** |
| **User Lighting** (54) | `user_light="1"` | mode-entry block (`80 … brightness … AA55`) then 04 18, 04 23 (`[8]=9`, 576 B = 144 records, page byte 0), 04 02, 04 F0; read back for preview with 04 F5 (page byte 0, 9 × 64 B). Hardware shows the device stores 256 records per page and has pages 0/1/2 | verified-in-code (framing); page model from hardware |
| **Real-time Lighting** (55) | `custom_light="1"` | PC-side effects (in `mui.dll!CustomLightMode`); enters user-lighting mode (task 10) then streams 04 20 frames every ~30 ms with `FEAT_WRITE_FAST` + 04 02; no 04 18/04 F0 | verified-in-code |
| **Music Rhythm** (56) | `music="1"` | USB: same 04 20 stream (task-driven, page 7); 2.4 GHz: cmd `0x0B 0x1C` with 21 spectrum bands. Music effect modes: lang 101–120 | verified-in-code (USB), inferred (band value semantics) |
| **System Settings** (57) | `<function fn_layer="1" sleep_time="1" key_respondtime="1"/>` | 04 18, 04 17 (`[2]=1 [8]=1`), settings block, 04 02 | verified-in-code |
| · Disable Win / Alt+F4 / Alt+Tab (160–162) | — | settings block bytes 4 / 3 / 2 (respectively) | verified-in-code |
| · Fn Switch Function Momentary/Toggle (292–294) | — | settings block byte 5 | field verified, value semantics inferred |
| · Sleep Time No Sleep/1/5/30 min (141–145) | `sleep_time="1"` | settings block byte 6 | field verified, value semantics inferred |
| · Key Response Time Level 1–5 (140, 149) | `key_respondtime="1"` | settings block byte 8 | field verified, value semantics inferred |
| **Key remapping** (Top Layer 290 / Fn Layer 291) | `fn_layer="1"` | 04 18, 04 11 (base) or 04 27 (Fn), `[8]=9`, 576-byte table, 04 02, 04 F0; every key edit re-sends the whole layer | verified-in-code |
| · Default/Disable/Key/Multi-Key/Mouse/Multimedia/Windows Shortcut/Open Program/Open Website/Send Text/Consumer (201–236, 251–279) | — | entry encodings §4.4 | verified-in-code |
| · Layers (Top/Fn) / Fn switch mode | — | `04 11` vs `04 27`; Fn switch in settings byte 5 | verified-in-code |
| **TFT Screen** (58) | `screen="1" gif_headlength="256" gif_maxframes="255" gif_count="1" width="160" height="96"` | 04 18, 04 72, N × 4096-byte pages, 04 02 | verified-in-code |
| · Frame editor / brushes / text / delays / import-export GIF (800–887) | — | purely host-side (mui.dll `ImageView`, `LCDViewList`); only the final RGB565 buffer + per-frame delay bytes go to the device | verified-in-code |
| · Time Syns (827) | — | 04 18, 04 28 (`[8]=1`), time block, 04 02 | verified-in-code |
| · Save data and upload boot animation (821/848) | — | screen upload above (task 16) | verified-in-code |
| · Delete LED / Delete current LCD data (835/836) | — | local frame deletion; the device is only touched on "Apply"/upload | inferred (no dedicated command) |
| **Connected devices / battery** (60–63) | — | 2.4 GHz cmd `0x20 0x01` every ≤15 s; event reports `05 A6 FF/FE`; descriptor strings | verified-in-code |
| **Reset Keyboard** (152/166) | — | DB reset + full re-upload (task 14 bitmask); 2.4 GHz `0x0F` first | verified-in-code |
| **Factory Reset** (153/168) | — | same re-upload path as Reset, wider DB wipe | verified-in-code |
| **Driver Update / Firmware Update** (156/157, 175–186) | `<firmware version="120" file="" url=""/>` | external programs only (downloaded ZIP + EXE); no HID command | verified-in-code |
| **Sleep/off and 2.4 GHz link state** | — | event channel `05 A6 FF 01` / `05 A6 FE 02` | verified-in-code |
| **Side Light** (660–670) | `sidelight="0"` (disabled) | not implemented in this model's UI path; would ride in the light-block mode byte (`0x20..0x29` in the sibling build) | absent/inferred |
| Bluetooth | — | not supported by this software at all | verified-in-code (no BT code path; lang string 62 says so) |

---

## 10. Byte-for-byte examples produced directly from the builders

These are the exact 64-byte payloads / blocks the app builds for the fields it has, so a
reimplementation can be diffed against them.

```
begin           04 18 00 00 00 00 00 00 00 00 ... (64 B)
commit          04 02 00 00 00 00 00 00 00 00 ...
end             04 F0 00 00 00 00 00 00 00 00 ...
lighting hdr    04 13 00 00 00 00 00 00 01 00 ...
settings hdr    04 17 01 00 00 00 00 00 01 00 ...
keymap hdr      04 11 00 00 00 00 00 00 09 00 ...      (04 27 for Fn)
per-key hdr     04 23 00 00 00 00 00 00 09 00 ...      ([8]=3 on a mono board)
clock hdr       04 28 00 00 00 00 00 00 01 00 ...
screen hdr      04 72 01 00 00 00 00 00 51 00 ...      (slot 1, 0x0051 = 81 chunks, 10-frame example: 256+10*30720=307456 B → 76 chunks = 0x4C)
realtime hdr    04 20 00 00 00 00 00 00 08 00 ...      ([8]=3 on a mono board)
read-back hdr   04 F5 00 00 00 00 00 00 09 00 ...
macro hdr       04 19 ...   then   04 15 00 00 00 00 00 00 <n> 00 ...

light block (mode 1 = static, red, colorful 0, brightness 5, speed 3, direction 0)
                01 FF 00 00 00 00 00 00 00 05 03 00 00 00 AA 55  then zero to 64
settings block  00 01 <nA> <nF> <nW> <fn> <sleep> 00 <debounce> 00 ... 00 AA 55   (AA55 at 62-63)
clock block     00 <slot> 5A <yy> <mm> <dd> <hh> <mi> <ss> 00 <dow> 00 ... 00 AA 55
per-key entry   <key_index> <R> <G> <B>      (at table offset 4*key_index)
realtime entry  <key_index> <R*br/256> <G*br/256> <B*br/256>
screen header   0A 32 32 ... (frames, delay/2 ...) | 0xFF fill | frames from offset 256
```

For 160×96 the chunk counts are: 1 frame → 8 (`08 00`), 2 → 16 (`10 00`),
10 → 76 (`4C 00`), 225 → 1688 (`98 06`), 255 → 1913 (`79 07`).

---

## 11. Confidence summary and what is still unknown

**Verified-in-code (opcode + every field offset):** the five primitives and their timing /
ack semantics; interface classification (65 / 33 / 0xFFFF+1 / 4097); `04 18`, `04 02`,
`04 F0`, `04 13` + light block, `04 17` + settings block, `04 11`/`04 27` + full keymap
encoding, `04 19`+`04 15` + macro image, `04 23` + colour tables, `04 20` + frame layout,
`04 F5` read-back, `04 28` + clock block, `04 72` + screen buffer/pages; the 2.4 GHz frame
layout, checksum, reply matching and every cmd byte listed in §5; the event-channel poll and
report values; the external firmware/driver-update path.

**Inferred, not proven from instructions:**

* semantic names/order of values that the app just passes through from its DB/UI:
  light mode 1–19 numbering (lang 521–540), `fn_switch` 0/1, `sleep_time` 0–3,
  `key_respondtime` 1–5, macro `play mode`/`repeat` codes, key DB type 4 and type 13 labels;
* RGB565 packing details and row order — the app hands the frame to
  `mui.dll!LCDViewList::GetImageRGB565Data` and copies its output; the packing itself lives
  in mui.dll (not reversed here). Only the buffer geometry and the `0xFF` prefill are from
  the EXE;
* the 2.4 GHz single-key frame byte order (`cmd 0x03/0x0D`) — the builder writes
  `[2]=04, [4..], [6]=key code`, which does not line up with the simple "cmd,len,idx,payload"
  reading as cleanly as the other frames; treat it as *uncertain*;
* exact meaning of 2.4 GHz cmd `0x1F` (task 13) and `0x0F` (factory-reset notify);
* whether `04 F0` is required by this firmware/harmful — the app sends it after lighting,
  keymap and per-key uploads but not after settings/clock/macros/screen; the sibling AKS075
  project reports it resets the GIF player, so its effect here is **unknown**.

**Unknown / to be settled on hardware, not by this binary:**

* the exact per-key colour **readback channel encoding** (hardware: magenta `04 F5` bytes
  `7e 00 fd`; the writer here is byte-exact `[k,R,G,B]`, so the transform is firmware-side
  — see §4.8);
* whether pages 1/2 of the `04 F5`/`04 23` record store are reachable with payload byte 2
  as page selector (the vendor app never sets it);
* the firmware's internal command validation (which bytes it actually parses);
* whether the on-board lighting effect algorithms are in firmware (they are: the app only
  sends mode numbers);
* the exact `bcdDevice` vs XML `<firmware version>` comparison predicate.

**Out of scope here:** the `mui.dll` RGB565 encoder and the PC-rendered real-time/music
effects (separate target; could be reversed from mui.dll if a pixel-faithful
reimplementation is wanted).

---

## 12. Function/method map (cited addresses)

| VA | RVA | name (recovered) | evidence file |
|---|---|---|---|
| 0x413510 | 0x13510 | thread-local singleton accessor returning the protocol/device object | `FUN_00413510.c` |
| 0x44edb0 | 0x4EDB0 | `FEAT_WRITE(data,len,want_ack)` — 35 ms feature-report writer + ack | `FUN_0044edb0.c` |
| 0x44efa0 | 0x4EFA0 | `FEAT_WRITE_FAST` — 5 ms writer, retry on 0xFF | `FUN_0044efa0.c` |
| 0x44f280 | 0x4F280 | `FEAT_QUERY(data,packets)` — write + multi-report read-back | `FUN_0044f280.c` |
| 0x44f4d0 | 0x4F4D0 | `DONGLE_SEND(data,len)` — 33-byte 2.4 GHz frame + checksum | `FUN_0044f4d0.c` |
| 0x44f1b0 | 0x4F1B0 | `BULK_PAGE_WRITE(page)` — 4097-byte screen page + 300 ms read | `FUN_0044f1b0.c` |
| 0x4509d0 | 0x509D0 | hidapi initialiser: `LoadLibrary("hid.dll")` + 10× `GetProcAddress` | — |
| 0x450ae0 | 0x50AE0 | hidapi enumeration (`SetupDi*` + caps) | — |
| 0x44f950 | 0x4F950 | device open + interface classification + device list | — |
| 0x451320 | 0x51320 | `HidD_SetFeature(handle, [00]+64, 65)` | `FUN_00451320.c` |
| 0x4513c0 | 0x513C0 | feature read-back via `DeviceIoControl(0xB0192)` into same buffer | `FUN_004513c0.c` |
| 0x451100 | 0x51100 | output-report `WriteFile` (4097 or 33 bytes) | `FUN_00451100.c` |
| 0x4511e0 | 0x511E0 | input-report `ReadFile` with timeout | `FUN_004511e0.c` |
| 0x4141a0 | 0x141A0 | system settings upload (`04 17`) | `FUN_004141a0.c` |
| 0x414410 | 0x14410 | system settings upload, 2.4 GHz (`0x07 0x10`) | `FUN_00414410.c` |
| 0x418550 | 0x18550 | keymap upload, wired (`04 11` / `04 27`) | `FUN_00418550.c` |
| 0x418df0 | 0x18DF0 | keymap upload, 2.4 GHz (`0x10` / `0x12`) | `FUN_00418df0.c` |
| 0x419130 | 0x19130 | single-key upload, 2.4 GHz (`0x03` / `0x0D`) | `FUN_00419130.c` |
| 0x422ac0 | 0x22AC0 | screen upload, wired (`04 72` + pages) | `FUN_00422ac0.c` |
| 0x423360 | 0x23360 | screen upload, 2.4 GHz (`0x7F`) | `FUN_00423360.c` |
| 0x423a80 | 0x23A80 | clock sync, wired (`04 28`) | `FUN_00423a80.c` |
| 0x423bb0 | 0x23BB0 | clock sync, 2.4 GHz (`0x0C 0x10`) | `FUN_00423bb0.c` |
| 0x42adb0 | 0x2ADB0 | read live key colours (`04 F5`) | `FUN_0042adb0.c` |
| 0x42afd0 | 0x2AFD0 | lighting mode upload, wired (`04 13`) | `FUN_0042afd0.c` |
| 0x42b170 | 0x2B170 | lighting mode upload, 2.4 GHz (`0x05 0x10`) | `FUN_0042b170.c` |
| 0x42d5d0 | 0x2D5D0 | macro upload, wired (`04 19`/`04 15`) | `FUN_0042d5d0.c` |
| 0x42db50 | 0x2DB50 | macro upload, 2.4 GHz (`0x09`) | `FUN_0042db50.c` |
| 0x427f60 | 0x27F60 | real-time LED frame stream, wired (`04 20`) | `FUN_00427f60.c` |
| 0x4328c0 | 0x328C0 | same stream, music page | `FUN_004328c0.c` |
| 0x432b00 | 0x32B00 | music spectrum frame, 2.4 GHz (`0x0B 0x1C`) | `FUN_00432b00.c` |
| 0x433fd0 | 0x33FD0 | enter user-lighting mode, wired (`04 13` mode block) | `FUN_00433fd0.c` |
| 0x434230 | 0x34230 | worker task dispatcher (includes task-13 `0x1F` frame) | `FUN_00434230.c` |
| 0x434900 | 0x34900 | enter user-lighting mode, 2.4 GHz (`0x05 0x01`) | `FUN_00434900.c` |
| 0x434ec0 | 0x34EC0 | clock-sync task (19) dispatcher | `FUN_00434ec0.c` |
| 0x434ee0 | 0x34EE0 | factory-reset / full re-sync (task 14, incl. 2.4 GHz `0x0F`) | `FUN_00434ee0.c` |
| 0x435160 | 0x35160 | event-channel poll (200 ms) | `FUN_00435160.c` |
| 0x4357d0 | 0x357D0 | status/battery poll, 2.4 GHz (`0x20 0x01`) | `FUN_004357d0.c` |
| 0x44b7f0 | 0x4B7F0 | per-key mode-entry block (`80` + brightness) | `FUN_0044b7f0.c` |
| 0x44b900 | 0x4B900 | per-key colour upload, wired (`04 23`) | `FUN_0044b900.c` |
| 0x44bd70 | 0x4BD70 | per-key colour upload, 2.4 GHz (`0x14`) | `FUN_0044bd70.c` |
| 0x451b80 | 0x51B80 | modifier-usage → modifier-bitmask (E0–E7) | — (documented in §4.4) |
| 0x419450 | 0x19450 | consumer-key id → HID consumer usage | — (documented in §4.4) |

`evidence/` also contains the full decompiled bodies of these functions; `METHOD.md`
explains how the whole set can be regenerated from the EXE in about ten minutes.
