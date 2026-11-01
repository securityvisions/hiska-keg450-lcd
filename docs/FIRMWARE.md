# HISKA HX-KEG450 — firmware / MCU / update-path investigation

Author: `firmware-re` (teammate). Scope: static, read-only analysis of the shipped Windows package,
the running USB descriptors (sysfs only), and public SONiX material. **No vendor app was run, no
hidraw node was opened for writing, no device command was sent.**

Evidence labels used throughout:

- **[C]** — confirmed here from a primary artifact (file bytes / PE structure / binary disassembly / sysfs descriptor), with address.
- **[H]** — confirmed on this device by the hardware branch of the team (see `~/Personal/hiska-keg450-lcd/docs/PROTOCOL.md`).
- **[R]** — reported by a third party (public code/docs), URL given.
- **[I]** — inference (reasoning from [C]/[R], not directly observed).
- **[U]** — unknown / not determined.

---

## 0. Bottom line

| Question | Answer | Confidence |
|---|---|---|
| Is there a firmware image in the shipped package? | **No.** The installer creates an **empty** `app/firmware/` directory and ships nothing inside it. No PE overlay, no RCDATA resource, no appended blob, no trailer data in GIF/PNG, no second archive. | **High [C]** |
| Where would a firmware image come from? | The app has an update path that (a) looks for `%APPDIR%\firmware\…` drop-in tool, or (b) downloads **`FirmwareUpdateTool.zip`** from the `<upgrade url>` in `config.xml` and runs `FirmwareUpdateTool.exe`. In this build `url=""`, so nothing is reachable. The actual flashing protocol lives in that **external tool, which is not shipped**. | **High [C]** |
| MCU? | **SONiX SN32F2xx family (ARM Cortex-M0), full-speed USB HID** — the vendor string is literally `SONiX`; interface/protocol topology and a sibling model's real `0C45:FEFE` dongle PID are SONiX. Exact part (`SN32F268`/`SN32F299`/`SN32F248B`/EVision·HFD rebrand) **not determinable from the package.** | Family **high**, part **unknown [U]** |
| Firmware version? | Host-visible version is **USB `bcdDevice` = `0x0106` ⇒ firmware "1.06"**. The bundled layout XML expects `version="120"` (= 1.20), so the vendor app considers the device **out of date** and would offer an update it cannot deliver. | **[C]** descriptor; version encoding [I] (strong) |
| Update protocol in the binary? | Only the **launcher/decision** logic is present, not the flasher. Recovered HID plumbing: 65-byte (report-ID+64) feature reports, status byte 3 (`01`=ok / `FF`=busy), 35 ms pacing; plus the full `04 xx` command set used for normal operation. | **[C]** |
| Can firmware be dumped over HID? | **No read-back path exists in this app, and the public SONiX ISP protocol has no memory-read command.** Dump requires SWD/ST-Link (`SonixQMK/sonix_dumper`) or a logic analyser on SPI flash. | **High [R]** |
| State read / telemetry we can use | Two vendor input channels exist that the team is *not* currently reading: **MI_01 → Report ID 5, 3 bytes, Usage Page `0xFFFF`/Usage `0x01`** (the vendor app polls this every **200 ms** and reacts to `A6 FF 01` / `A6 FE 02`), and **MI_00 → Usage Page `0xFF`/Usage `0x03`, 1 byte**. This is the most promising channel for live-display/state debugging. | **[C]** descriptors + disassembly |

---

## 1. Firmware-blob hunt — full negative result

### 1.1 What the installer actually contains

Artifacts (all read-only):

| Artifact | Size | SHA-256 |
|---|---|---|
| `~/Downloads/HX-KEG450-Driver-1.0.0.01.zip` (= copy at `research/hid-kb/hiska/HX-KEG450.zip`, identical md5 `d2b803bc…`) | 8,099,549 | `da6f6271d5be8e0c22e283a586d2df1e132bab548c3939f020ed5bccdb8bb45a` |
| inner `HX-KEG450 Driver-1.0.0.0(1).exe` (Inno Setup 6.1.0 unicode) | 8,644,451 | md5 `6c9c4f6db72307ed2a4435f1308ef89a` |
| extracted `app/DeviceDriver.exe` | 1,771,008 | `0c7878174842911b69db32c3d6f78a619abe112710b6e767dd8f166417f946c8` (md5 `aed401c7cb33a38196d79e23dd94c895`) |
| extracted `app/mui.dll` | 1,187,840 | `ec6d044f28d173a18015a85bc75534fc7c3f3009f5f1a3f10cef436cd7c19a2b` |

The ZIP contains exactly **one** file (7z listing) — no second archive, no hidden member.

`innoextract` (`/usr/bin/innoextract`, v1.9) on the setup EXE:

```
Listing "HX-KEG450" - setup data version 6.1.0 (unicode)
 - "app/DeviceDriver.exe" (1.69 MiB) - overwritten     <-- duplicate entry, see below
 - "app/firmware/"                                      <-- DIRECTORY ONLY, no files
   … 144 files total …
```
- setup data lists **146 entries**: **145 file entries = 144 distinct files + 1 duplicate** (`app/DeviceDriver.exe` appears twice) **plus exactly one directory**, `app/firmware/`, which is **empty**.
- the duplicated `DeviceDriver.exe` entry extracted twice; both copies are byte-identical
  (md5 `aed401c7…`), so it is an installer quirk, **not** a second/newer binary.
- `innoextract -e` writes **144 files**; `diff -r` against the pre-existing extraction in
  `research/hid-kb/hiska/extracted/app/` is **clean** (no missed files).
- `innoextract -t` (checksum verification over the whole setup data stream) passes: **all payload
  data is accounted for by those 144 files.** Nothing is left over to hide a blob in.
- `innoextract -l -m` (exclude temp/`ExtractTemporaryFile` payloads) gives the same list: **no
  temp-embedded files** such as a firmware payload.

### 1.2 PE structure of the two code images

```
DeviceDriver.exe  PE32 i386, ImageBase 0x400000, TimeDateStamp 2025-06-11, linker 14.0
  .text   raw 1,393,152  entropy 6.59
  .rdata  raw   185,856  entropy 5.58
  .data   raw    31,232  entropy 6.78
  .rsrc   raw   106,496  entropy 3.77
  end_of_sections 1,771,008 == file size   -> OVERLAY: 0 bytes
mui.dll           PE32 i386, ImageBase 0x10000000
  end_of_sections 1,187,840 == file size   -> OVERLAY: 0 bytes
```
- **No overlay** on either image ⇒ no appended firmware blob.
- `DeviceDriver.exe` resources: `RT_ICON`, `RT_MENU`, `RT_DIALOG`, `RT_STRING`, `RT_ACCELERATOR`,
  `RT_GROUP_ICON`, `RT_VERSION`, `RT_MANIFEST` only. **No `RT_RCDATA`, no BIN/ZIP/DLL payload.**
  Version info: `FileVersion/ProductVersion = 1.0.0.0`, `Copyright (C) 2023`.
- `mui.dll` resources: `PNG` (UI skins), `TTF` (Open Sans), `RT_MANIFEST`. Nothing executable/blob-like.
- The other DLLs are stock MSVC runtimes (`mfc140u.dll`, `msvcp140.dll`, `vcruntime140.dll`), and
  `*.lib` are linker import libraries.

### 1.3 Content scan of everything shipped

Entropy scan over all 144 files (full table was produced during analysis; highlights):

| File | size | entropy | note |
|---|---|---|---|
| `mfc140u.dll` | 4,862,264 | 7.02 | stock MS runtime |
| `DeviceDriver.exe` | 1,771,008 | 6.61 | code |
| `gif/HX-KEG450/0.gif` | 1,415,575 | 7.88 | GIF trailer `0x3B` at offset 1,415,574 → **0 trailing bytes** |
| `mui.dll` | 1,187,840 | 6.66 | code |
| every `*.png` | — | ~7.9 | `IEND` chunk is the last 8 bytes → **0 trailing bytes** |
| `language/*.lan`, `layouts/*.xml`, `config.xml` | small | 3.6–5.4 | text |

- No file has an anomalous size/entropy pair that would indicate a wrapped firmware image (e.g. no
  unexplained ~32–256 KiB high-entropy region; a Cortex-M image of that kind would appear as a new
  file or as overlay/`RT_RCDATA`, and neither exists).
- Canonic firmware-container magics (`SN32`, `ITE`, `UPD`, `BIN`, `HEX`, `DFU`, `ISP`, `SN32F`,
  `0x55AA` command words) appear **nowhere** in DeviceDriver.exe / mui.dll as data; only as the
  high-level strings discussed in §3 (`firmware\`, `FirmwareUpdateTool.zip`).
- The bundled `0.gif` is the *sample boot animation* data, not firmware: 160×96 palette GIF,
  225 frames, 40 ms/frame [H, earlier report].

### 1.4 Sibling HISKA packages (cross-check)

Downloaded and inspected the other two HISKA packages from the same mirror
(`https://diginovan.com/hiska-driver-keyboards-hx-keg405-hx-keg450-hx-keg460/`):

| Package | Contents | Firmware dir | `upgrade url` | `<firmware version>` |
|---|---|---|---|---|
| `HX-KEG450-Driver-1.0.0.01.zip` | this device | **empty** | `""` | 120 |
| `HX-KEG460-Driver-1.0.0.0.zip` | `DeviceDriver.exe` md5 `3e48d095…` (different build), same mui/MFC layout | **empty** | `""` | 120 |
| `HX-KEG405.zip` | completely different, older toolchain (`Gaming Keyboard.exe` + `.ini` configs) | absent | n/a | n/a |

KEG460's `app/config.xml` adds a third mode that is *very* informative for §2:

```xml
<mode value="2" desc="2.4G TYPE-A" vid="0C45" pid="FEFE" product_name="2.4G Dongle"
      hid_interface="VID_0C45&PID_FEFE&MI_03"/>
```

`0C45` is **Sonix Technology's USB VID**; the fake Apple `05AC:024F` is only used for the cable and
the white-label 2.4 GHz receiver.

### 1.5 Conclusion for the blob hunt

**There is no firmware image anywhere in the shipped HISKA HX-KEG450 package.** The `firmware/`
directory is a *drop-in location* that the application probes at update time; the package ships it
empty on purpose because the actual flasher is downloaded separately (and in this build the URL is
blank, so not even that works).

---

## 2. MCU identification — evidence vs inference

### 2.1 Hard evidence collected this session

| # | Evidence | Source | Strength |
|---|---|---|---|
| E1 | USB `iManufacturer` string = **`SONiX`** | live descriptor `lsusb -v -d 05ac:024f` / sysfs `manufacturer`; also in team `PROTOCOL.md` [H] | **direct** — SoC-family vendor |
| E2 | `idVendor:idProduct = 05ac:024f` (Apple IDs deliberately cloned) | live descriptor | spoof, no MCU info |
| E3 | `bcdDevice = 0x0106`, `iSerial` absent, `bcdUSB = 2.00`, **Full Speed (12 Mbps)**, 4 HID interfaces, EP max packet 64 | live descriptor + `/sys/bus/usb/devices/3-10.4/…` | **direct** — 12 Mbps USB = Cortex-M0-class SONiX SN32F2xx (SN32F2xx is USB 2.0 full speed) |
| E4 | Sibling KEG460 config declares real SONiX dongle `VID_0C45&PID_FEFE` | KEG460 `config.xml` | **strong** — `0C45` = Sonix Technology |
| E5 | Interface map is the SONiX OEM private-HID pair: MI_02 = Usage Page `0xFF68` (64 IN / 4096 OUT), MI_03 = Usage Page `0xFF13` (64 IN / 64 OUT / 64 FEATURE); MI_01 contains vendor collection Usage Page `0xFFFF`, Usage `0x01`, **Report ID 5**, 3-byte input | read from `/sys/class/hidraw/hidraw{4..7}/device/report_descriptor` (cached by kernel, no USB traffic) | **strong** — this exact topology is implemented by SN32QMK-class boards (AULA/AJAZZ/Epomaker) [R] |
| E6 | `04 xx` command family + `AA 55` trailer + 35 ms `cmd_delaytime` + 256-byte GIF header + RGB565 160×96 | `DeviceDriver.exe` disassembly + `layouts/rgb-keyboard.xml` [C]/[H] | **strong** — the SN32 OEM LCD-keyboard SDK |
| E7 | No external driver: everything is class-compliant HID (SetupAPI enumeration + `hid.dll` `HidD_*` feature reports via `GetProcAddress`) | imports/strings of DeviceDriver.exe [C] | consistent with SN32F2xx (USB HID MCU family), not with e.g. a WinUSB/DFU firmware stack |
| E8 | Firmware version exposed as `bcdDevice` and compared to a package version string | disassembly §3.2 [C] | typical SONiX OEM convention |

### 2.2 What is NOT evidence

- No public HX-KEG450 teardown, FCC photos, or MCU marking found in this session.
  `web_search` failed (`configured web provider "deepseek-official" is not registered`); fallbacks
  (`html.duckduckgo.com`, `lite.duckduckgo.com`, `bing.com/html`) were bot-blocked. GitHub repo search
  found only the team's own two repos (`securityvisions/…`).
- The `firmware/` dir being empty means the usual giveaway (a `*.bin` whose first bytes are a Cortex-M
  vector table) does not exist.
- `05AC`/`0C45` PIDs in this family are **not** chip identifiers (they are OEM-bootloader/user
  identities — see §4.3).

### 2.3 Verdict

- **Family: SONiX SN32F2xx (ARM Cortex-M0, USB 2.0 full-speed, 16–256 KB flash). High confidence**,
  driven by E1 (literal `SONiX` manufacturer string) + E4 + E5 + E6.
- **Exact part / rebrand: unknown.** Candidates from the publicly supported SN32F2 set (SonixQMK):
  `SN32F248 / SN32F248B / SN32F268`, rebrands `eVision VS11K09A / VS11K13A`, `HFD HFD2201KBA /
  HFD48KP500`; larger parts (`SN32F268/299`) are more likely for a board that also has a screen, RGB
  matrix and 3-mode wireless, but that is **[I]**.
- **Explicitly not supported:** Yichip, ITE, BY Tech, SinoWealth. Nothing in the package or
  descriptors points to them (the shipped `ITE Device` and `Figma` hidraw nodes on this host belong to
  other peripherals, not the keyboard).

---

## 3. Update path recovered from `DeviceDriver.exe`

Reverse engineering was done with plain `objdump -d -M intel` (PE32 i386; all addresses below are
**absolute VAs**, image base `0x400000`), plus `pefile` for IAT/string mapping. Key helpers:

| Component | Address | Recovered behaviour |
|---|---|---|
| HID library load + resolve | `0x4509d0` | `LoadLibraryA("hid.dll")` (`0x56fec0`), `GetProcAddress` × 10: `HidD_GetAttributes`, `HidD_GetSerialNumberString`, `HidD_GetManufacturerString`, `HidD_GetProductString`, `HidD_SetFeature`, `HidD_GetFeature`, `HidD_GetIndexedString`, `HidD_GetPreparsedData`, `HidD_FreePreparsedData`, `HidP_GetCaps`; function pointers in globals `0x59509c..0x5950bc`, module in `0x594710` |
| Device enumeration | `0x450ae0` | HID class GUID `{4d1e55b2-f16f-11cf-88cb-001111000030}`, `SetupDiGetClassDevsW` + `SetupDiEnumDeviceInterfaces` + `SetupDiGetDeviceInterfaceDetailW` + `CreateFileW`; fills per-device record with **VID/PID/VersionNumber from `HidD_GetAttributes`**, product/manufacturer strings, parsed `mi_XX` index |
| Feature send | `0x451320` | `HidD_SetFeature(handle, buf, 0x41)` — **65-byte buffer = report ID 0 + 64 bytes** [H matches] |
| Feature read | `0x4513c0` | `DeviceIoControl` with `IOCTL 0x000B0192` + `GetOverlappedResult`, 100 ms timeout |
| Interrupt-IN read | `0x4511e0` | `ReadFile` on overlapped event; timeout arg (called with `0x12c`=300 ms for image pages, `0x0a` for notifications) |
| Command sender (`64-byte` FEATURE + status poll) | `0x44edb0` | memsets 0x41 buf, `buf[0]=0` report ID, copies 64-byte payload to `buf+1`, `HidD_SetFeature`; if returned >0 then reads reply and requires **reply payload byte 3 == `0x01`** (`[ebp-0x44]`) |
| Command sender with retry/pace | `0x44efa0` | same, with `Sleep(5)` between phases, retries when reply byte 3 == `0xFF` (busy); chunked mode for > 0x41 bytes (64-byte chunks) |
| Wireless (2.4 G) frame sender | `0x44f4d0` | builds **33-byte frame** (`0x21`), writes **byte-sum checksum into frame byte 32** (`sum(buf[0..31]) & 0xFF`, computed in a loop at `0x44f53b-0x44f54b`), then `WriteFile` via `0x451100` (MI_03 OUT endpoint, report ID + 32) [R matches AK820 2.4 G frame] |
| Image page sender | `0x44f1b0` | 4097-byte buffer (report ID + 4096), `WriteFile`, then reads 0x1001-byte reply with 300 ms timeout |

### 3.1 The update UI and its two providers

`DeviceDriver.exe` contains an in-app upgrade flow: `UpgradeButton` class (mui.dll), `UpdateTipDlg`,
and language string IDs from `app/language/*.lan` (UTF-16LE):

```
156=Driver Update            157=Firmware Update
175=You are currently using the latest version.
176=A new driver version is detected. Do you want to update?
177=The current firmware is up to date. Do you still want to flash the firmware?
180=The new firmware has been found. For better use experience, we recommend that you upgrade the firmware
181=1. During the firmware upgrade process, please do not plug in or unplug the device or restart the computer.
182=2. If the firmware upgrade fails, please try to upgrade the firmware again.
185=Upgrade
```

Two launcher functions (called from the update-tip dialog command dispatcher at `0x434520`-`0x43465b`,
where `ebx==0x14` → software update, `ebx==0x15` → firmware update):

**A. Software self-update — `0x4352b0`**
```
"%s\temp\Keyboard-Software-Setup.zip"  (0x56e9b8 / 0x56ea38)
"%s\temp\Keyboard-Software-Setup.exe"  (0x56e9f0)
-> DeleteFileW, unzip via COM helper 0x45a3e0, PathFileExistsW, ShellExecuteW("open"),
   CreateProcessW, then exit(0) (the updater replaces itself)
```

**B. Firmware update — `0x435540`**
```
"%s\temp\FirmwareUpdateTool.zip"  (0x56eac4 / 0x56eb04)
"%s\temp\FirmwareUpdateTool.exe"  (0x56ea94 / 0x56ea84)
-> same unzip + PathFileExistsW + CreateProcessW/ShellExecuteW("open") path
```
So the **only thing the shipped app does for firmware is: download a ZIP, unzip it to `%TEMP%`, and
launch `FirmwareUpdateTool.exe` — a separate executable that contains the real ISP/flashing code.**
The HID flashing protocol is therefore **not present in `DeviceDriver.exe`** and must be recovered from
`FirmwareUpdateTool.zip`, which we do not have.

**Download side (WinINet, `0x45a3e0`-`0x45a700`)**: `InternetGetConnectedState` (`0x45a44d`),
`InternetOpenA` (`0x45a48a`), `InternetOpenUrlA` (`0x45a501`), `HttpQueryInfoW`, `InternetReadFile`
(`0x45a689`); the URL comes from `config.xml` `<software><upgrade url="…">` (parser element string
`"upgrade"` at `0x56db64`), **which is empty in every HISKA package we have**.

**Local drop-in path (`0x415180`, firmware-update button handler)** — the most interesting function:
- `0x4151aa` → `0x44f840` fetches the selected device record and returns **`bcdDevice`** (see 3.2);
- `0x4151d7` formats it with **`"%x"`** (string `0x56d4bc`) into a version string;
- `0x4151e2` loads the package's expected version string from the main object (`+0x7e4`, set from the
  `layouts/rgb-keyboard.xml` `<firmware version="…">` attribute at `0x415d3b`), i.e. `"120"`;
- both are converted with `_wtoi` (`0x556468`) and compared (`0x41520f`):
  - `package_ver <= device_ver` → shows language IDs **157 + 175** ("…latest version").
  - `package_ver > device_ver` (device older) → builds `%APPDIR%\firmware\…` string from
    `firmware\` (wide `0x56d4c4`) at `0x4152ca`, **`PathFileExistsW`** it at `0x415319`;
    if it exists → **`CreateProcessW`** it directly at `0x415380` (runs the drop-in tool);
    if not → download branch (needs the non-empty `upgrade url`).

### 3.2 The firmware version is the USB `bcdDevice` — recovered chain

`DeviceDriver.exe` never sends a "get firmware version" HID command. It reads the version from the
USB descriptor:

1. Enumeration fills a device record from `HidD_GetAttributes` (`0x450d13`, `HIDD_ATTRIBUTES.Size=0xc`);
   then `0x450e42-0x450e66` copies words from the attributes struct into the record:
   `record+4 = VendorID`, `record+6 = ProductID`, **`record+0xc = VersionNumber`** (`[esp+0x4c]`).
2. When device objects are created (`0x44f950`), `record+0xc` is copied to `device+0x418` (`0x44faf5`).
3. `0x44f840` returns `device+0x418` for the selected device (`0x44f91b`).
4. `0x4151e2-0x41520f` formats it with `"%x"` and `_wtoi`-compares it to the XML `<firmware version>`.

For this device `bcdDevice = 0x0106` ⇒ string **`"106"`** ⇒ 106 < 120 ⇒ **the app always thinks a
firmware update exists** and would show the "new firmware found" message (180) if the update URL
worked. `120` in the XML is most plausibly firmware **v1.20** (same hexadecimal-digit encoding), i.e.
the vendor has a newer firmware that is *not distributed with this driver build*.
**[I, strong]** — the `%x`/`_wtoi` detail is directly in the code; the "1.20" reading is the only
consistent interpretation of `version="120"` for a `0x0106` device.

### 3.3 Normal-operation HID protocol extracted (for completeness; task-1 owns the full atlas)

`04 xx` opcodes present in `DeviceDriver.exe`, with the address of the instruction that materializes
them (little-endian word writes; `04` is the low byte):

| Opcode | Meaning | Address(es) |
|---|---|---|
| `04 18` | begin transaction | `0x4141e0`, `0x418596`, `0x422ff6`, `0x423aa3`, `0x42b03f`, `0x433ff3`, `0x44b813`, `0x44b970` |
| `04 72` | screen metadata (slot, page count LE16 at bytes 8-9) | `0x42306d` |
| `04 02` | commit | `0x42325f`, `0x432aa4` |
| `04 F0` | end / finish | `0x418d34`, `0x42b119`, `0x434098`, `0x44b8c9`, `0x44bcd1` |
| `04 13` | enter lighting write mode (byte 8 must be `01`) | `0x42b05e`, `0x434012`, `0x44b832` |
| `04 15` | (macro / erase related in family) | `0x42dab3` |
| `04 19` | (erase display memory in family) | `0x42d63b` |
| `04 20` | real-time per-key / user-light data (chunked 0x200/0xC0) | `0x427fb4`, `0x432914` |
| `04 23` | per-key static | `0x44b99b` |
| `04 11` / `04 27` | keymap read/write pair selected by argument (payload[8]=9) | `0x4185cc` (al=0x11 or 0x27) |
| `04 17` | (unknown; begin-transaction sibling) | `0x4141ff` |
| `04 28` | RTC / clock sync | `0x423acd` |
| `04 F5` | heartbeat / poll | `0x42ae16` |

Note: `04 2B` does **not** exist as an opcode — the word `0x2b04` at `0x418486`/`0x418aa1` is a
*keycode* value inside the remap table builder, not a command.

**Screen upload sequence as implemented (confirmed independently of the hardware capture)** — function
`0x422e00`-`0x423345`:
```
04 18                                        (0x422ff6, SendCommand 0x44edb0)
04 72 <slot> … <pages_lo> <pages_hi>         (0x42306d; slot = LCDViewList current sel + 1, at 0x4233a6)
per page: 4097-byte WriteFile (0x44f1b0) + 4097-byte ReadFile 300 ms (drain/ACK)
04 02                                        (0x42325f)
```
The page-writer does **not** validate the ACK content (it only requires the `WriteFile` to succeed and
then reads the reply); the hardware team measured the reply as `01 5A 02` on the image node [H] — this
is the SONiX per-page ACK.

Status semantics [C + H]:
- 65-byte feature buffer: `buf[0] = 0x00` report ID, `buf[1..64] = payload`.
- reply payload **byte 3** = verdict: `0x01` accepted, `0xFF` busy/rejected; `0x00` = no verdict/echo.
- 35 ms inter-command pacing from `cmd_delaytime`; app additionally sleeps 5 ms inside the retry sender.

**Wireless command path** (`0x44f4d0`): 33-byte OUT frame on MI_03 (report ID + 32 payload), payload
byte 31 = byte-sum of bytes 0..30. One UI handler sends it with payload byte 1 = `0x1F`
(`0x4345e9`) — a wireless-only command not in the USB `04 xx` set. Not needed for the current work,
but it is the reason the 2.4 GHz mode has a different tooling shape.

### 3.4 What the update protocol therefore is (and is not)

- **In the shipped app:** version check (descriptor-based) → prompt → download/launch external
  `FirmwareUpdateTool.exe` (or run a drop-in `%APPDIR%\firmware\…` tool). No flashing bytes, no
  bootloader entry, no erase/program/verify opcodes.
- **Actual flashing protocol: [U]** — it is inside `FirmwareUpdateTool.zip` which is not shipped and
  not downloadable from this build (`url=""`). It may be the public SONiX ISP protocol (§4.2) wrapped
  in a vendor UI, or a private bootloader protocol; we cannot tell without the tool.
- If we ever want the tool: look for another HISKA/SONiX reseller distributing
  `FirmwareUpdateTool.zip`, or a driver package whose `config.xml` has a non-empty `upgrade url`.

---

## 4. Can the firmware be dumped over HID?

### 4.1 From the vendor protocol: no

- The app's complete command set (`04 xx`, §3.3) has **begin / metadata / data / commit / end /
  lighting / keymap / clock / heartbeat**. There is **no read-memory, read-flash, read-any-large-block
  command**, and no opcode that returns more than the 64-byte status reply.
- The MI_02 image channel is **out-only for data** (4096-byte OUT, 64-byte IN for ACKs only).
- `04 F5` is a heartbeat with an empty/`FF` reply [H]; `04 05` (the family's device-info read) was not
  found in this binary by either the word-immediate scan (`mov WORD …`, all `04 xx` sites in §3.3) or
  the byte-pair construction scan at the sender call sites — **[C]/[I], i.e. strong but a negative can
  never be proven exhaustively** — so there is no "device info returns firmware bytes" path we can find.

### 4.2 Public SONiX ISP material (the only documented SONiX protocol)

Recovered/verified references (locally cached and re-fetched this session):

- **SonixQMK docs** — https://sonixqmk.github.io/SonixDocs/ — which SN32 parts are supported, and the
  warning "we recommend having a copy of the stock firmware before flashing" (because there is no
  dump path).
- **sonix-rs PROTOCOL.md** (local: `research/hid-kb/repos/Weeabo-Inc_sonix-rs/PROTOCOL.md`) —
  reverse-read of `SonixFlasherC`; ISP over the **same 65-byte HID feature-report transport**
  (`report ID 0` + 64 payload), command base `0x55AA`:
  - `CMD_GET_FW_VERSION` = `0x55AA0001`, `CMD_SET_ENCRYPTION_ALGO` = …0003, `CMD_ENABLE_ERASE` = …0004,
    `CMD_ENABLE_PROGRAM` = …0005, `CMD_GET_CHECKSUM` = …0006, `CMD_RETURN_USER_MODE` = …0007;
  - response: `buf[0..3]` = echoed command LE, `buf[4..7]` = `0xFAFAFAFA` ACK, then data;
  - `CMD_GET_FW_VERSION` returns at `buf[8]` family version, `buf[9]` chip version, `buf[11]` chip
    revision, `buf[14..15]` code-security (CS0..CS3);
  - **no read-memory command** — the operations are identify / erase / program / checksum / reboot.
- **OEM→ISP reboot magics** (sent as a normal 64-byte feature report, `magic[0]` at offset 0 LE,
  `magic[1]` at offset 4 LE), from sonix-rs: `sonix`/`evision` = `0x5AA555AA` / `0xCC3300FF`;
  `hfd` = `0x5A8942AA` / `0xCC6271FF`. These are **the OEM firmware being asked to jump into its ISP
  bootloader**, documented for other SONiX boards but **not measured on this device** — see risk note.
- **ISP-mode USB IDs** (bootloader identities, from sonix-rs): `0C45:7900` (SN22X/23X/24X),
  `0C45:7040` (SN24XB), `0C45:7160` (SN24XC), `0C45:7010` (SN26X), `0C45:7120` (SN28X),
  `0C45:7140` (SN29X). Our board's ISP PID is **unmeasured**. (User-mode PIDs like `0C45:8009/800A/800B`
  and KEG460's `0C45:FEFE` are *not* in this table — consistent with them being firmware identities.)
- **Bootloader entry on stock firmware**: on the boards this community has documented, entry is (a) the
  OEM→ISP magic above ("Reboot to Bootloader" button in SonixFlasher), or (b) for boards already
  reflashed with the QMK *jumploader*, a key held at power-up. Stock SONiX firmware is **not** known to
  expose a key-combo bootloader entry by itself. Sources:
  https://github.com/SonixQMK/sonix-flasher (README: "click Reboot to Bootloader if your keyboard is
  listed"), https://github.com/SonixQMK/sonix-keyboard-bootloader (README: key-held-at-plug-in is a
  *jumploader* feature, and the SN32F260 ISP bootloader is in user space so erasing it bricks the MCU).
- **Dumping firmware (the only known way)** — https://github.com/SonixQMK/sonix_dumper — "Dumping is
  done using an ST-Link device connected to the chip's SWD interface and uses the OpenOCD software";
  it also describes a code-protection bypass via boot-ROM LDR/STR gadgets. **This requires opening the
  keyboard and soldering/pogo to SWD.**

### 4.3 Verdict

**No, the running firmware cannot be dumped over HID with anything we have found.** There is no vendor
read-back command, and the public ISP protocol has no memory-read primitive. The only documented dump
route is **physical SWD (ST-Link/OpenOCD)**; the only non-physical route is flashing (one-way,
destructive without a prior dump).

**If bootloader entry is ever wanted for a non-destructive read**, the *safest* experiment is the
OEM→ISP magic feature report followed by sending only `CMD_GET_FW_VERSION` in ISP mode (identifies
chip family/version/security with zero writes). But note:
- the magic pair variants are board-specific (`sonix/evision` vs `hfd`) and unverified for HISKA;
- entering ISP and then failing to return cleanly means the board sits in bootloader (recoverable by
  `CMD_RETURN_USER_MODE`, but still a state change);
- on SN32F26x-class parts the ISP bootloader lives in user flash and the community explicitly warns
  about bricking; do **not** send erase/program/CS commands on this board until a dump exists.

---

## 5. State/telemetry that could verify what the panel is doing

This is the part most relevant to the Lead's live-display problem. The panel itself is a **boot-animation
store, not a framebuffer** [H]: the only write path is `04 18 / 04 72 / pages / 04 02`, and the only
read-back in that channel is the per-page ACK. However, the device exposes **two vendor input
channels** that the current tooling ignores:

### 5.1 MI_01 vendor event channel — Report ID 5 (Usage Page 0xFFFF / Usage 0x01)

Raw descriptor read from `/sys/class/hidraw/hidraw5/device/report_descriptor` (kernel cache, no traffic):

```
05 0c 09 01 a1 01 85 03 …                   Consumer report ID 3
05 01 09 80 a1 01 85 02 …                   System   report ID 2
05 01 09 06 a1 01 85 01 …                   Keyboard report ID 1 (bitmap)
05 01 09 02 a1 01 85 06 …                   Mouse    report ID 6
06 ff ff 09 01 a1 01 85 05 09 01 15 00
26 ff 00 75 08 95 03 81 02 c0               *** vendor event: ID 5, 3 × u8 IN ***
```

The vendor app polls exactly this endpoint in its **200 ms UI timer** (function `0x435160`):

```
ReadFile(handle, buf, 65, timeout=10 ms)      ; 0x4511e0
if buf[0] != 0:
    FormatDebugString("mouse notify data: %02X %02X %02X %02X", buf[0..3])  ; narrow string @0x56e968
    if buf[0]==0x05 && buf[1]==0xA6 && buf[2]==0xFF && buf[3]==0x01 -> state[obj+0x775]=1; 0x41f8f0()
    if buf[0]==0x05 && buf[1]==0xA6 && buf[2]==0xFE && buf[3]==0x02 -> state[obj+0x775]=0; 0x41f8f0()
    else -> 0x419270(buf[...])   ; runs ShellExecute/actions, reads an index with _wtoi
```

Interpretation **[I, high]**: report `05 A6 FF 01` / `05 A6 FE 02` is a **vendor notification event**
(report ID 5 byte + 3 payload bytes; `A6` = event code, then a 2-byte parameter pair `FF 01` / `FE 02`
that toggles a boolean app state and triggers a callback). The debug string calls it "mouse notify
data" because it is polled on the composite's MI_01 interface, but the collection is vendor-defined —
the exact semantics (battery? charging? connection mode? screen state? a knob/OSD event?) are
**unknown**. This is exactly the kind of channel that could report "what the panel is showing" and it
is currently a blind spot.

### 5.2 MI_00 vendor input — Usage Page 0xFF / Usage 0x03, 1 byte

Raw descriptor of `/sys/class/hidraw/hidraw4/device/report_descriptor` ends with:

```
05 ff 09 03 75 08 95 01 81 02     ; vendor page 0x00FF, usage 0x03, 1-byte input inside the
                                   ; boot-keyboard application collection (after 5 LED bytes)
```
An extra 1-byte vendor input report on the keyboard interface. Probably keyboard state/LED/wireless
status. Untested.

### 5.3 Commands that read state (what we did/didn't find)

- **Firmware version: not a HID read at all.** It is the USB `bcdDevice` (`0x0106`) read by the host from
  the device descriptor (§3.2). Any OS tool can read it: `lsusb -v -d 05ac:024f | grep bcdDevice`.
  No need to risk `04 05`.
- **`04 F5` heartbeat**: the vendor app's ~30 ms poll; hardware sees empty/`FF` replies [H]. It carries
  no state in the observed samples.
- **Screen mode / animation slot / frame info: no read command found.** `04 72` is write-only metadata;
  the app learns the slot from its own UI selection (`LCDViewList::GetCurSel()+1`, `0x4233a6`), not from
  the device.
- **The only per-page feedback** is the `01 5A 02` ACK on the image node [H]; the vendor app just drains
  it (`0x44f1b0` → `ReadFile 0x1001`, 300 ms).
- `04 05` (the family's device-info read, EA75 class) is **not present** in this binary's command set —
  so no VID/PID/version readback command exists here.

### 5.4 Why this matters for the live-display problem

Because the screen content is *stored*, not streamed, the observable state that can distinguish
"commit succeeded / animation playing / panel showing something else" is limited to:
1. the ACK/event stream (`01 5A 02` page ACKs; any extra reports),
2. the MI_01 Report-ID-5 vendor events (unknown semantics — the only plausible "screen/device state"
   notification the firmware sends),
3. the vendor app's displayed expectations (which use `bcdDevice`).

Recommendation: capture **both** idle and during-upload traffic on **hidraw5** and **hidraw6**, not
only the feature channel.

---

## 6. What to try next on hardware (ranked; I did not and will not run these)

Legend: R = read-only (no device state change expected), C = cosmetic/state-changing, D = dangerous.

| # | Risk | Experiment | Why / expected outcome |
|---|---|---|---|
| 1 | **R** | Open `hidraw5` (MI_01) and read reports for ~10 s while idle; then while pressing keys, turning the knob, switching lighting modes, and switching device modes (USB↔2.4 G↔BT if possible). Log every report; expect **Report ID 5** frames of 3 bytes. Correlate `A6 xx yy` codes with actions. | This is the vendor app's 200 ms notification channel (`0x435160`). If any code correlates with "screen/display/boot-animation" the Lead gets a state oracle. Also try after a `04 02` commit and during the boot animation. |
| 2 | **R** | Read `hidraw4` (MI_00) vendor collection (usage 0xFF/0x03) while typing / toggling LEDs / connecting. | 1-byte vendor input nobody is reading; cheap. |
| 3 | **R** | While idle after a successful upload, read the **image node** interrupt IN (hidraw6) with a timeout; do the same during the 1.2 s after `04 02`. Timestamp every report; confirm only `01 5A 02` appears or find extras. | Tells whether the firmware reports commit/animation status on the image node (cheap, no writes). |
| 4 | **R** | `04 F5` heartbeat poll (`04 F5 … 09`) and `04 05` **last** (family docs say `04 05` can poison the connection until reopen) — log the complete 64-byte replies for all opcodes already known, look for non-zero bytes beyond byte 3. | Fills the gap "does any reply leak state". `04 05` is a deliberate risk: do it last, then close/reopen hidraw. |
| 5 | **R** | Verify the `bcdDevice`-as-firmware-version theory **without device writes**: run the vendor app under Wine (isolated prefix, `config.xml` hid_interface pointed at an enumerable interface as the earlier team note describes), open the Firmware Update dialog with `upgrade url=""`. Expect message 180 "new firmware found" (device 0x106 vs XML 120). Then edit `layouts/rgb-keyboard.xml` to `<firmware version="106">` and reopen: expect message 175 "latest version". If that flips, the theory is **confirmed**. | Conclusive, no HID writes; also tells us whether v1.20 exists (i.e. the vendor's newer firmware is real). |
| 6 | **C** | Re-upload a single test frame and compare `04 72` byte 2 = `0x00` vs `0x01`: the vendor code sends slot `GetCurSel()+1` (1-based), hardware used `0` successfully. Check which value leaves the panel showing the new image after replug. | Explains any "panel shows old content" symptom. |
| 7 | **C** | Try the SONiX OEM→ISP magic pair via a 64-byte feature report (`5AA555AA`/`CC3300FF`, then the `hfd` variant) while watching `lsusb` for a `0C45:70xx/79xx` identity change. **Only if flashing is actually intended.** If it enters ISP, immediately send only `0x55AA0001` (GET_FW_VERSION) and then `0x55AA0007` (RETURN_USER_MODE). | Would identify the exact MCU family/chip version/security level (the single biggest unknown). But: state change, unverified magics, brick risk on SN32F26x-class parts, and **no dump path** — do not do this before deciding the end goal. |
| 8 | **D** | Anything that erases/programs flash, or key-hold-at-power-up bootloader tests on stock firmware. | Do not. There is no backup route; stock firmware is unrecoverable without a prior SWD dump. If firmware RE matters, the correct next step is **opening the case for SWD** (`sonix_dumper`), not USB poking. |
| — | **R (offline)** | Hunt for `FirmwareUpdateTool.zip` elsewhere (other HISKA/SONiX resellers, Wayback Machine of `hiska.co`, KEG460/KEG405 support pages). Extract it and RE the actual flasher: that is where the bootloader-entry, chunk framing, verify and reboot commands live. | Only path to the real update protocol without hardware risk. |

---

## 7. Unknowns (stated plainly)

1. **Exact MCU part** (`SN32F248B`? `SN32F268`? `SN32F299`? eVision/HFD rebrand?) — needs either a
   teardown photo or the ISP `CMD_GET_FW_VERSION` reply. [U]
2. **Semantics of MI_01 Report-ID-5 event `A6 xx yy`** — we know the vendor app parses `A6 FF 01` and
   `A6 FE 02` and calls a callback, but not what physical state it represents. [U]
3. **Whether `<firmware version="120">` means "expected v1.20"** (very likely, [I]) and whether that
   firmware exists anywhere. [U]
4. **The actual flashing protocol** (entry, framing, checksum/verify, reboot opcodes) — lives in the
   unshipped `FirmwareUpdateTool.exe`. [U]
5. **ISP magic pair for this board** (`sonix` vs `hfd`), ISP PID, and whether the ISP bootloader is in
   userspace (brick risk). [U]
6. **Wireless-path command `0x1F`** (33-byte frame, MI_03) purpose. [U] — out of scope here.
7. Did the 2.4 GHz receiver on *this* board present `0C45:FEFE` (as KEG460's config lists) or the
   Apple-spoofed `05AC:024F` (as KEG450's config lists)? Both appear in the family; the KEG450 config
   only lists the fake IDs. Cheap to check by plugging the dongle and running `lsusb`. [U]

---

## 8. Reproduction / artifacts

```bash
# 1. installer payload inventory (no execution)
innoextract -l  ~/Downloads/HX-KEG450-Driver-1.0.0.01.zip        # -> "Not a supported Inno Setup installer!" (it's a ZIP)
innoextract -l  "…/HX-KEG450 Driver-1.0.0.0(1).exe"             # -> 145 entries incl. empty app/firmware/
innoextract -t  "…/HX-KEG450 Driver-1.0.0.0(1).exe"             # checksum-verifies the whole payload
innoextract -e --collisions rename -d /tmp/fwre/dual "…exe"     # both DeviceDriver.exe copies (identical md5)
innoextract -l -m "…exe"                                        # same list -> no temp/embedded payloads
diff -r /tmp/fwre/full/app ~/Personal/research/hid-kb/hiska/extracted/app   # clean

# 2. PE structural checks (python3 + pefile)
#    - sections/entropy, overlay == 0 bytes, resource type enumeration (no RCDATA)
#    - strings: DeviceDriver.exe contains "hid.dll", "HidD_*", "firmware\", "FirmwareUpdateTool.zip",
#      "Keyboard-Software-Setup.zip"; no SN32/ISP/DFU/0C45 tokens

# 3. disassembly used for every address in §3
objdump -d -M intel /tmp/fwre/full/app/DeviceDriver.exe > /tmp/fwre/dd.objdump.asm
#    key VAs: 0x4509d0 hid.dll resolve | 0x450ae0 SetupAPI enumeration | 0x451320 SetFeature |
#    0x4513c0 feature read | 0x4511e0 interrupt read | 0x44edb0 / 0x44efa0 command senders |
#    0x44f4d0 wireless 33-byte sender | 0x44f1b0 image page writer | 0x415180 update handler |
#    0x435160 200 ms notify poll | 0x4352b0 software update | 0x435540 firmware update |
#    0x45a3e0-0x45a700 WinINet download

# 4. live descriptors (read-only sysfs, no USB traffic)
lsusb -v -d 05ac:024f        # bcdDevice 1.06, iManufacturer SONiX, no serial, full speed
for n in 4 5 6 7; do xxd /sys/class/hidraw/hidraw$n/device/report_descriptor; done
```

Local artifacts created by this investigation (outside the repo, scratch only):
`/tmp/fwre/full/app/` (second extraction), `/tmp/fwre/dual/` (collision-renamed extraction),
`/tmp/fwre/sib/{405,460}x/` (sibling HISKA packages), `/tmp/fwre/dd.objdump.asm`.
