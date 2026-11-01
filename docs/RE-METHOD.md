# METHOD — how the HX-KEG450 vendor protocol was recovered statically

This document is the reproducible recipe behind `OPCODES.md`. It records the exact
tooling, the order of steps, the traps, and how to re-verify any single claim in under a
minute.

**Scope discipline:** the analysis touched only files under
`~/Personal/research/hid-kb/hiska/extracted/app/`. The USB device was never opened, no
`hidraw` node was written, no Wine, nothing from the vendor app was executed. The only
"runtime" was static disassembly/decompilation of the shipped binaries plus the app's own
XML / language / GIF data files.

---

## 0. Target inventory

| item | detail |
|---|---|
| main binary | `app/DeviceDriver.exe`, PE32 i386, 1,771,008 B, built 2025-06-11, image base 0x400000, `.text` 0x401000–0x5551c9 (0x1541c9 bytes) |
| UI toolkit | `app/mui.dll` (MFC/ATL-ish, exports MUI::* UI classes; **no HID calls**, but it does own the RGB565 encoder `MUI::LCDViewList::GetImageRGB565Data` and the PC-side lighting effects) |
| runtimes | `mfc140u.dll`, `msvcp140.dll`, `vcruntime140.dll` (not analysed) |
| data | `config.xml`, `layouts/rgb-keyboard.xml`, `language/*.lan` (UTF-16LE), `gif/HX-KEG450/0.gif` |
| imports that matter | from `hid.dll` loaded **dynamically** via `LoadLibraryA("hid.dll")` + `GetProcAddress`; `setupapi.dll`, `kernel32` (CreateFile/WriteFile/ReadFile/DeviceIoControl/GetOverlappedResult), `wininet` (updater), `user32/shell32` |

Because `hid.dll` is loaded by name, there are no static IAT entries for `HidD_SetFeature`
etc.; they land in the app's own globals. Finding those globals is step 3.

---

## 1. Tooling actually used

| tool | version/notes | used for |
|---|---|---|
| `python3` | 3.12 | all scripting: PE parsing, byte scans, disassembly indexing, decompiled-C mining |
| `pefile` | 2024.8.26 (already installed) | PE headers, section map, import table (IAT VA → symbol) |
| `capstone` | 5.x (installed with `python3 -m pip install --user --break-system-packages capstone`) — ultimately not needed; `objdump` covered i386 cleanly | spare |
| `objdump` | GNU binutils 2.47 | full `.text` disassembly to a text file + targeted windows |
| **Ghidra headless** | 12.1.2 DEV at `/opt/ghidra`, Java 21 | recursive-descent analysis + decompilation of all 5,654 functions; `analyzeHeadless` with a small Java script |
| `strings`/`xxd`/`iconv` | coreutils | quick looks; UTF-16LE decoding of `.lan` and of wide strings in the binary |

No ghidra GUI was used (headless only). No radare2/IDA. Ghidra was not assumed available
by the task brief — it happens to be installed on this box and turned out to be the
single biggest time-saver; the fallback that also works is pure `objdump` + the
`hid.dll` function-pointer globals described below.

### Why the decompiler matters for this binary

The protocol code is MFC `thiscall` C++ built with `/O2`; the payload builders are easy to
read in pseudo-C but painful in raw i386 (`movups` copies of 65-byte structs, alloca
probes, SEH frames). The workflow used was: locate everything with cheap byte/objdump
scans first, then decompile **only** the ~40 relevant functions to read the semantics.
`evidence/` holds exactly those decompiled functions, so a reviewer never has to re-run
Ghidra to check a claim.

---

## 2. Step-by-step recipe

### Step 1 — PE map and section boundaries (2 minutes)

```bash
cd ~/Personal/research/hid-kb/hiska/extracted/app
python3 - <<'EOF'
import pefile
pe = pefile.PE('DeviceDriver.exe', fast_load=True)
ib = pe.OPTIONAL_HEADER.ImageBase
for s in pe.sections:
    print(s.Name.decode().rstrip('\x00'), hex(ib+s.VirtualAddress), hex(s.Misc_VirtualSize), hex(s.SizeOfRawData))
EOF
```

Result: `.text` at VA 0x401000, `.rdata` 0x556000 (contains IAT + strings + RTTI names),
`.data` 0x584000. **All addresses in this project are VAs = RVA + 0x400000**; Ghidra was
imported with the default image base, so its output and the objdump output are directly
comparable.

Whole-file disassembly for grepping (0.2 s, ~493k instructions, 14 MB text):

```bash
objdump -d --no-show-raw-insn -M intel DeviceDriver.exe > /tmp/keg450.asm
```

A tiny helper indexes that text file so any VA can be expanded with surrounding
instructions without re-running objdump:

```python
# ctx.py VA [lines_before] [lines_after]
```

### Step 2 — map the IAT (imports) to symbols (5 minutes)

`pefile` already resolves the import table; dumping `import.address -> (dll, name)` is
enough to recognise the runtime helpers used by every builder:

```
0x5560a8 Sleep            0x5560dc WriteFile        0x5560ec CreateFileA
0x5560e0 DeviceIoControl  0x5560d4 ReadFile         0x5560f0 LoadLibraryA
0x5560fc GetProcAddress   0x556100 LocalFree         0x55618c GetOverlappedResult
0x5560b0 GetLocalTime     0x556184 GetLastError
... plus the whole mui.dll table (MUI::LCDViewList::GetCurSel, GetImageRGB565Data,
    GetDelayTimes, CustomLightMode::*, KeyboardCtrl::*), setupapi, wininet, shell32
```

The **mui.dll import list is a free feature map**: `?GetImageRGB565Data@LCDViewList@MUI@@`,
`?GetKeyItems@CustomLightMode@MUI@@`, `?SetKeyMap@CustomLightMode@MUI@@`,
`?SaveGIFImageFile@ImageViewList@MUI@@`, etc. tell you what the UI can do even before you
read any code.

### Step 3 — find the HID function-pointer table and its users (10 minutes)

The strings `"hid.dll"`, `"HidD_SetFeature"`, `"HidD_GetFeature"`, `"HidP_GetCaps"`, …
live in `.rdata`. Find each string's VA, then search `.text` for the little-endian 4-byte
VA (i386 pushes string addresses as absolute immediates). All `GetProcAddress` results are
stored into `.data`/`.rdata` globals:

```
0x594710 hid.dll HMODULE
0x5950b4 HidD_GetAttributes         0x5950b8 HidD_GetProductString
0x5950a0 HidD_SetFeature            0x59509c HidD_GetFeature
0x5950a8 HidD_GetPreparsedData      0x5950ac HidP_GetCaps
```

The init function is `FUN_004509d0` (VA 0x4509d0). Next, search `.text` for
`call dword ptr [0x5950a0]` (`ff 15 a0 50 59 00`): there is exactly **one** call site,
inside `FUN_00451320` = the `HidD_SetFeature` wrapper. `call [0x59509c]` has **zero** call
sites — this build reads replies through
`FUN_004513c0` = `DeviceIoControl(handle, 0xB0192, in/out same 65-byte buffer)`, not via
`HidD_GetFeature`. (Both were resolved at init because it is a copy of the hidapi
Windows backend.)

From the wrappers, walk one level up: the output-report writer is `FUN_00451100`
(`WriteFile`), the input reader is `FUN_004511e0` (`ReadFile` + timeout). Searching for
*their* callers yields the five protocol primitives:

| primitive | VA | discovered by |
|---|---|---|
| `FEAT_WRITE` | 0x44edb0 | calls `FUN_00451320` then optional `FUN_004513c0`; ~41 call sites |
| `FEAT_WRITE_FAST` | 0x44efa0 | same but 5 ms sleeps + `0xFF` retry; 3 call-site functions |
| `FEAT_QUERY` | 0x44f280 | calls `FUN_00451320` then repeated `FUN_004513c0`; 1 caller |
| `DONGLE_SEND` | 0x44f4d0 | calls `FUN_00451100` with length 0x21 and sums bytes; 16 call sites |
| `BULK_PAGE_WRITE` | 0x44f1b0 | calls `FUN_00451100` with length 0x1001 then `FUN_004511e0(…,300)` |

### Step 4 — cheap command inventory without a decompiler (10 minutes)

Before decompiling anything, enumerate opcodes from immediate stores:

```bash
# header builders store `mov WORD PTR [buf], 0x??04`  (class 0x04 + opcode)
grep -nE "WORD PTR \[[^]]*\],0x[0-9a-f]{2}04$" /tmp/keg450.asm
```

That immediately produced `04 18`, `04 17`, `04 13`, `04 19`, `04 15`, `04 20`, `04 23`,
`04 28`, `04 72`, `04 F0`, `04 F5`. Commands whose opcode is written as a single byte
(`04 02`, `04 11`, `04 27`) were found by the next step.

### Step 5 — full function segmentation + call-site audit (10 minutes)

Pure-`objdump` segmentation works because MSVC pads every function with `int3`:

* a function starts at each instruction that follows an `int3` run; 3,833 functions found
  this way (Ghidra later found 5,654 with tail-chunks/FakeFunctions — use Ghidra's
  boundaries when available).
* for every function, collect its direct `call` targets and its `mov WORD/BYTE PTR [...],
  0x..04` stores.
* functions that call `0x413510` (the thread-local device-object accessor — every command
  sender starts with it) or that call one of the five primitives are the protocol
  functions. Their direct-call targets give the UI dispatcher map (§7 of `OPCODES.md`).

This is also the completeness proof for §8 "opcodes absent": **every** call site of every
primitive was enumerated and assigned a function, and those functions contain no other
`04 xx` builder.

### Step 6 — Ghidra headless decompilation (5 minutes of machine time)

Command used (project and output in `/tmp`, repository untouched):

```bash
mkdir -p /tmp/ghkeg/proj
/opt/ghidra/support/analyzeHeadless /tmp/ghkeg/proj keg450 \
  -import "$HOME/Personal/research/hid-kb/hiska/extracted/app/DeviceDriver.exe" \
  -scriptPath /tmp/ghkeg/scripts -postScript DumpAll.java /tmp/ghkeg/out \
  -deleteProject -analysisTimeoutPerFile 300
```

`DumpAll.java` (source of truth: this file was small enough to keep in the analysis log;
re-create from this description):

1. `getFunctionManager().getFunctions(true)` — all non-external functions;
2. for each: decompile with `DecompInterface` (`decompileFunction(f, 120, monitor)`) and
   write `/*=== FUNC <va> <name> size=… ===*/` + the C to `decompiled_all.c`;
3. also dump `blocks.txt`, `imports.txt`, `functions.txt`, `callgraph.txt`,
   `strings_xrefs.txt` (defined strings and their code references);
4. it runs as a *post* script so auto-analysis (including the slow
   `DecompilerParameterId`) is already finished.

Output used here: `/tmp/ghkeg/out/decompiled_all.c` (5,654 functions, ~300k lines,
1.1 MB). Every function cited in `OPCODES.md` was copied from it into `evidence/`.

Gotchas that cost time:

* **Java-script API differences** — Ghidra 12 removed `Address.isExternal()`,
  `DefinedDataIterator.definedData(Program)` and `MemoryBlock.getPermissions()`; the
  script was adjusted to `getListing().getDefinedData(true)` and no permissions.
  Java scripts must explicitly `import ghidra.app.script.GhidraScript;`.
* `analyzeHeadless` needs the project directory to exist (`mkdir -p`), otherwise it
  aborts before doing anything.
* The folder has **no** PDB; Ghidra's PDB analyser logs a warning, ignore it.
* MFC/C++ `thiscall` means the first argument is in `ECX`: decompiled signatures with
  `(void *this, …)` are correct; the singleton accessor `FUN_00413510` supplies that `this`
  for all protocol functions.

### Step 7 — read, cross-check, write down fields

For each builder: read the decompiled C around the `FUN_0044edb0` calls, extract the byte
offsets of every field, then cross-check against

* `layouts/rgb-keyboard.xml` — `cmd_delaytime`, `gif_headlength`, `gif_maxframes`,
  `gif_count`, `width`, `height`, `brightness_max`, `speed_max`, `default_mode`;
* `language/1033.lan` — UI feature names by numeric string id (UTF-16LE);
* the SQL schema strings embedded in the EXE (`t_key_macro_data`, `t_config_data`,
  `t_profile_data`, `t_light_data`, `t_macro_data`, …) — these name the DB columns whose
  values are copied into payload fields;
* the wide config-key strings in `.rdata` (`disable_alttab`, `disable_altf4`,
  `disable_win`, `fn_switch`, `sleep_time`, `key_respondtime`) — decoding these as UTF-16LE
  is what pinned the settings-block byte meanings;
* the clean-room sibling AJAZZ AK820 protocol spec (`../hid-kb/AK820Pro_Protocol_Spec.md`)
  as a *hypothesis generator* only — every field cited here was re-verified in this
  binary's own instructions, and the differences (no `04 05`, no dongle handshake `0x02`,
  no page-ACK validation, extra `0x0F`/`0x1F`, different updater path) are recorded.

### Step 8 — fold in hardware evidence and note the gaps

The last pass merges the hardware agent's measurements into the atlas as *separate*
claims (labelled as hardware input) instead of silently rewriting code findings. Concretely
the `04 F5` section now records both facts side by side:

* code: the app asks for 9 × 64-byte reports of page 0 (payload byte 2 = 0) and reads the
  4-byte records as `[key_index,R,G,B]`;
* hardware: the firmware returns 16 × 64-byte reports (256 records) per page, pages 0/1/2
  exist, and the readback colour channel is **not** plain R,G,B (magenta → `7e 00 fd`).

The writer (`04 23`, verified byte-exact) is paired with that reader and the page-byte
constraint is written down explicitly (§4.6/§4.8 of `OPCODES.md`). The same pass records
the two independent negative findings (`04 05` absent; firmware version = USB `bcdDevice`
0x0106, no flash opcodes) and marks them as confirmed twice.

The remaining gap — what transform the firmware applies to the colour channels on readback
— cannot be settled from the binary (the writer is byte-exact; the transform is inside the
keyboard firmware). The cheapest hardware experiment is: write N known colours with
`04 23`, read them back with `04 F5 page 0`, fit the channel map; repeat for pages 1/2 by
setting payload byte 2 in the `04 F5`/`04 23` header (the vendor app never does).

### Step 9 — evidence and deliverable

```bash
# decompiled functions cited in the atlas
ls /home/parsavisions/Personal/research/re-full/static/evidence/
```

Every function name in `OPCODES.md` §12 maps 1:1 to `evidence/FUN_00xxxxxx.c`. To check a
claim, open the file and look at the `FUN_0044edb0(...)` calls: e.g.
`evidence/FUN_00423a80.c` literally contains

```c
local_54 = 0x1804;                 /* 04 18 */
…
local_54 = 0x2804; local_4c = 1;   /* 04 28, [8]=1 */
…
local_52 = 0x5a;                   /* block[2] = 0x5A */
local_51 = (undefined1)((ulonglong)local_64.wYear % 2000);
local_50 = (undefined1)local_64.wMonth;  … local_4a = wDayOfWeek;
local_16 = 0x55aa;                 /* block[62..63] */
```

which is the entire clock-block spec from the atlas.

---

## 3. Re-verification cheat-sheet

| question | command |
|---|---|
| is there any other `04 xx` command? | `grep -nE "WORD PTR \[[^]]*\],0x[0-9a-f]{2}04$" /tmp/keg450.asm` + `grep -c "call   0x413510" /tmp/keg450.asm` (61 send sites, all mapped by function) |
| what does a given VA look like in C? | `evidence/FUN_xxxxxxxx.c`, or re-run the Ghidra recipe above |
| who calls primitive X? | `grep -n "call   0x44edb0" /tmp/keg450.asm` (41 sites), `0x44efa0` (6), `0x44f280` (1), `0x44f4d0` (16), `0x44f1b0` (1) |
| is a string used by the protocol? | `strings_xrefs.txt` from the Ghidra dump, or search the LE 4-byte VA in `.text` (i386 absolute immediates) |
| which report length is the command channel? | `FUN_0044f950`: `0x41` = command, `0x21/0x21` = dongle, `0xFFFF/1` = events, `0x1001` output = bulk |

---

## 4. Known limitations of this method

* **No hardware and no USB captures**: timing tolerances, the exact ACK echo bytes and
  whether the firmware tolerates missing `04 F0` are still hardware questions (the other
  agent's task). The code-level facts are stated separately from the hardware facts in
  `OPCODES.md`.
* **Pixel packing and effects live in `mui.dll`** (`GetImageRGB565Data`,
  `CustomLightMode`); this analysis only proves that the EXE copies the encoder's output
  and at which buffer offsets/pitches. Reversing `mui.dll` is a separate (and optional)
  job if a pixel-perfect reimplementation of the GIF encoder / PC effects is needed.
* **Database is created at runtime** by the app; schema and column names are recovered from
  the embedded SQL strings, but the actual default values (light mode numbering, mode
  parameter ranges for modes 1–19) are not present in the EXE — they are populated in the
  SQLite DB on first run. Those semantics are therefore marked *inferred*.
* Function boundaries from the `int3`-padding heuristic differ slightly from Ghidra's; the
  atlas always quotes Ghidra VAs, which match the disassembly exactly for every cited
  function.
* Only this build (`HX-KEG450 Driver-1.0.0.0`, EXE timestamp 2025-06-11) is described.
  The sibling AK820 build differs in several places (no `04 05`, dongle handshake,
  `01 5A 02` page-ACK check, different updater paths), so do not assume cross-model
  compatibility of the *host* behaviour, only of the family wire format.

---

## 5. Artefacts produced

| path | contents |
|---|---|
| `OPCODES.md` | the atlas: channels, primitives, all wired `04 xx` commands with byte layouts, data-block formats, 2.4 GHz family, event channel, UI→opcode map, negative findings, confidence table |
| `METHOD.md` | this file |
| `evidence/FUN_00xxxxxx.c` | Ghidra decompilation of every function cited in the atlas (65 files), one per address = RVA+0x400000 |
