# 68k Assembly Stub Generation

This document is the deep technical reference for atrace's stub code
generator. It covers the three-region stub structure (prefix, variable,
suffix), the exact 68k instruction sequences with byte offsets, register
usage at each point, address patching, branch displacement calculation,
and the trampoline mechanism that captures return values without
clobbering caller registers.

The target audience is developers who need to understand, modify, or
debug the stub generator. All byte offsets, instruction encodings, and
register assignments are derived directly from `atrace/stub_gen.c` and
`atrace/atrace.h`.

For the binary layout of the events that stubs write, see
[event-format.md](event-format.md). For the system-level context in
which stubs operate, see [architecture.md](architecture.md).


## Stub Structure Overview

Each traced function gets its own stub -- a block of dynamically
generated 68k machine code that intercepts calls to the original library
function, records an event in the ring buffer, and then calls through to
the original implementation.

A stub consists of three contiguous regions:

```
+----------------------------+
|  Prefix                    |   216 bytes (standard)
|  (fast-path checks,        |   224 bytes (with NULL-argument filter)
|   register save,           |
|   ring buffer reservation, |
|   EClock capture,          |
|   event header fill)       |
+----------------------------+
|  Variable Region           |   Varies by function (metadata-driven)
|  (argument copy,           |
|   string capture,          |
|   task name capture,       |
|   valid=2 marker)          |
+----------------------------+
|  Suffix                    |   156 bytes (standard)
|  (MOVEM restore,           |   226 bytes (OpenLibrary, +70 BSD patching)
|   trampoline,              |   182 bytes (accept, +26 sockaddr capture)
|   post-call handler,       |
|   disabled path,           |
|   overflow path)           |
+----------------------------+
```

Total stub size = prefix + variable + suffix, rounded up to a ULONG
(4-byte) boundary. The stub is allocated with `MEMF_PUBLIC | MEMF_CLEAR`
via `AllocMem()` and installed into the library's jump table via
`SetFunction()`.

The prefix and suffix are copied from static template arrays
(`stub_prefix[]` and `stub_suffix[]`). The variable region is generated
at install time from per-function metadata in the patch descriptor. After
assembly, placeholder addresses and struct field displacements in the
templates are patched with actual runtime values.

Source: `stub_gen.c`, lines 1--19 (file header), `STUB_PREFIX_BYTES`
(216), `STUB_SUFFIX_BYTES` (156).


## Register Conventions

The stub uses registers as follows:

| Register | Role in stub |
|----------|-------------------------------------------|
| a5 | Multi-purpose: initially saves caller's a5, then loaded with PATCH_ADDR, ANCHOR_ADDR, or entry pointer at various points |
| a6 | Loaded with SysBase for Exec calls, TimerBase for ReadEClock, DOSBase for IoErr. Caller's a6 is saved/restored by MOVEM |
| a0 | Scratch: ring buffer pointer, patch pointer, string source, entry pointer (post-call) |
| a1 | Scratch: string destination |
| d0--d3 | Scratch: ring buffer index, sequence number, slot offset calculation |
| sp | Standard 68k stack pointer; the MOVEM frame and trampoline are built on the stack |

The MOVEM save/restore uses the mask `0xFFFA` / `0x5FFF`, which covers
d0--d7, a0--a4, and a6 -- 14 registers, 56 bytes of stack frame. Note
that a5 is NOT part of this MOVEM frame. It is saved and restored
separately via explicit `move.l a5, -(sp)` / `movea.l (sp)+, a5`
instructions because a5 is repurposed throughout the stub as a pointer
to various data structures.

### MOVEM Frame Layout

The MOVEM frame, pushed by `movem.l d0-d7/a0-a4/a6, -(sp)` at prefix
byte 76 (template byte 56, shifted +20 by the daemon task check), has
this layout on the stack (offsets from sp after the push):

| Offset | Register |
|--------|----------|
| 0 | d0 |
| 4 | d1 |
| 8 | d2 |
| 12 | d3 |
| 16 | d4 |
| 20 | d5 |
| 24 | d6 |
| 28 | d7 |
| 32 | a0 |
| 36 | a1 |
| 40 | a2 |
| 44 | a3 |
| 48 | a4 |
| 52 | a6 |

These offsets are used by the variable region to read argument values
from the saved register frame. The `reg_to_frame_offset()` inline
function in `atrace.h` maps register indices (0=d0 through 14=a6) to
these byte offsets. Index 13 (a5) returns -1 as a sentinel because a5
is not in the MOVEM frame.

### Register Index Encoding

The `arg_regs[]` arrays in `struct func_info` and `struct atrace_patch`
use this encoding:

| Index | Register | Index | Register |
|-------|----------|-------|----------|
| 0 | d0 | 8 | a0 |
| 1 | d1 | 9 | a1 |
| 2 | d2 | 10 | a2 |
| 3 | d3 | 11 | a3 |
| 4 | d4 | 12 | a4 |
| 5 | d5 | 13 | a5 |
| 6 | d6 | 14 | a6 |
| 7 | d7 | 15 | a7 |


## Prefix Region (216/224 bytes)

The prefix is identical for all patched functions. It performs fast-path
checks to determine whether tracing is active, saves all volatile
registers, reserves a ring buffer slot under interrupt inhibition,
captures the EClock timestamp, and fills the event header fields.

The prefix is assembled from the `stub_prefix[]` template (98 UWORDs =
196 bytes) with a 20-byte daemon task exclusion check always inserted
at byte 56, bringing the standard size to 216 bytes. All struct field
displacement and address slots contain placeholder `0x0000` values that
are patched after assembly.

### Instruction-by-Instruction Breakdown

#### Fast-Path Checks (bytes 0--29)

```
Byte   Encoding              Instruction                     Purpose
----   --------              -----------                     -------
  0    2F0D                  move.l a5, -(sp)                Save caller's a5
  2    2A7C 0000 0000        movea.l #PATCH_ADDR, a5         Load patch descriptor [1]
  8    4AAD 0000             tst.l enabled(a5)               Per-patch enable check
 12    6700 0000             beq.w .disabled                 Skip if this patch disabled
 16    2A7C 0000 0000        movea.l #ANCHOR_ADDR, a5        Load anchor struct
 22    4AAD 0000             tst.l global_enable(a5)         Global enable check
 26    6700 0000             beq.w .disabled                 Skip if tracing disabled
```

The two-level enable check (per-patch then global) allows individual
functions to be disabled without affecting others, and allows the entire
tracing system to be disabled atomically.

If either check fails, execution branches to `.disabled` in the suffix,
which restores a5 from the stack and tail-calls the original function.
At this point only a5 has been pushed onto the stack, so the disabled
path is a lightweight 3-instruction sequence.

#### Task Filter Check (bytes 30--55)

```
Byte   Encoding              Instruction                     Purpose
----   --------              -----------                     -------
 30    4AAD 0000             tst.l filter_task(a5)           Is task filter active?
 34    6714                  beq.s .no_filter (+20)          No filter -> proceed
 36    2F0E                  move.l a6, -(sp)                Save a6 temporarily
 38    2C78 0004             movea.l $4.w, a6                Load SysBase (abs short)
 42    2C6E 0114             movea.l 276(a6), a6             a6 = SysBase->ThisTask
 46    BDED 0000             cmpa.l filter_task(a5), a6      Compare to filter
 50    2C5F                  movea.l (sp)+, a6               Restore a6
 52    6600 0000             bne.w .disabled                 Mismatch -> skip
       ; .no_filter:
```

The task filter is set by the daemon during `TRACE RUN` to restrict
tracing to a specific process. When `filter_task` is NULL (the default),
the `tst.l` falls through immediately via `beq.s .no_filter`. When
non-NULL, SysBase is loaded to read `ThisTask` (offset 276), which is
compared against the filter. On mismatch, execution branches to
`.disabled`.

Note that a6 is temporarily saved and restored within this block because
it is used to access SysBase. The caller's a6 (which holds the target
library base for the intercepted call) must not be disturbed.

#### Daemon Task Exclusion Check (bytes 56--75)

This 20-byte block is always inserted at byte 56 (between the task
filter and the MOVEM save). It prevents the daemon's own calls to
library functions from generating trace events, which would create
feedback loops when the daemon calls functions it is tracing.

```
Byte   Encoding              Instruction                     Purpose
----   --------              -----------                     -------
 56    2F0E                  move.l a6, -(sp)                Save a6 temporarily
 58    2C78 0004             movea.l $4.w, a6                Load SysBase (abs short)
 62    2C6E 0114             movea.l 276(a6), a6             a6 = SysBase->ThisTask
 66    BDED 0000             cmpa.l daemon_task(a5), a6      Compare to daemon task
 70    2C5F                  movea.l (sp)+, a6               Restore a6
 72    6700 0000             beq.w .disabled                 Daemon -> skip event
       ; .no_daemon_check:
```

The check compares `SysBase->ThisTask` against `anchor->daemon_task`
(offset 100 in the anchor struct). When they match, the current task is
the daemon itself, and execution branches to `.disabled` to skip event
recording. When `daemon_task` is NULL (no daemon connected), the
comparison never matches because `ThisTask` is always a valid non-zero
pointer.

Like the task filter check, a6 is temporarily saved and restored
because it is needed to access SysBase. This block is not conditional
-- it is emitted for every stub, even when no daemon is connected.
The `daemon_task` field is set by the daemon at startup and cleared on
disconnect.

This block shifts all subsequent prefix bytes by +20. The register
save (MOVEM) starts at byte 76 instead of the template's byte 56.

#### NULL-Argument Filter (8 bytes, optional)

For functions with `skip_null_arg != 0`, an 8-byte NULL check is
inserted at byte 76 (after the daemon task check), between the daemon
check and the MOVEM save. This shifts all subsequent prefix bytes by an
additional +8, making the total prefix size 224 bytes.

The insertion point is chosen so that the branch-to-`.disabled` path
only needs to pop saved a5 -- if the NULL check were placed after the
MOVEM save, the disabled path would also need to pop the 56-byte MOVEM
frame. The `beq.w .disabled` displacement at byte 82 is calculated at
install time, like all other prefix-to-suffix branches.

```
; For address registers (e.g. skip_null_arg = a0):
 76    B0FC 0000             cmpa.w #0, a0                   Full-width test (sign-ext)
 80    6700 0000             beq.w .disabled                 NULL -> skip event

; For data registers (e.g. skip_null_arg = d1):
 76    4A81                  tst.l d1                        Full 32-bit test
 78    4E71                  nop                             Pad to 4 bytes
 80    6700 0000             beq.w .disabled                 NULL/zero -> skip event
```

Address registers use `cmpa.w #0, An` (opcode `0xB0FC | ((reg-8) << 9)`,
4 bytes). The `.w` immediate is sign-extended to 32 bits by the 68k, so
this tests the full address. Data registers use `tst.l Dn` (opcode
`0x4A80 | reg`, 2 bytes) followed by a `nop` to maintain alignment. The
`.l` size is required -- `.w` would only test the low 16 bits, causing
false NULL matches on BPTRs like `0x00010000`.

Functions using this filter include `FindTask` (skip when a1=NULL, as
`FindTask(NULL)` is called constantly to get the current task),
`UnLock` (skip when d1=0, since `UnLock(0)` is a no-op), and
`LockPubScreen` / `UnlockPubScreen` (skip when a0=NULL).

#### Register Save (bytes 76--79, or 84--87 with NULL filter)

```
Byte   Encoding              Instruction                     Purpose
----   --------              -----------                     -------
 76    48E7 FFFA             movem.l d0-d7/a0-a4/a6, -(sp)  Save 14 registers (56 bytes)
```

This saves all registers that the stub might use as scratch, preserving
the caller's complete register state. After this point, the caller's
argument values are accessible via stack-relative addressing into the
MOVEM frame.

#### Ring Buffer Slot Reservation (bytes 80--145, or +8 with NULL filter)

This section runs under `Disable()`/`Enable()` interrupt inhibition to
ensure atomicity of the ring buffer position update. No other code (on
a single-CPU Amiga) can execute between `Disable()` and `Enable()`.

```
Byte   Encoding              Instruction                     Purpose
----   --------              -----------                     -------
 80    2C78 0004             movea.l $4.w, a6                SysBase
 84    4EAE FF88             jsr _LVODisable(a6)             Inhibit interrupts (-120)
 88    206D 0000             movea.l ring(a5), a0            a0 = ring buffer header
 92    2028 0000             move.l write_pos(a0), d0        d0 = current write_pos
 96    2200                  move.l d0, d1                   d1 = copy
 98    5281                  addq.l #1, d1                   d1 = write_pos + 1
100    B2A8 0000             cmp.l capacity(a0), d1          d1 >= capacity?
104    6502                  bcs.s .nowrap (+2)              No wrap needed
106    7200                  moveq #0, d1                    Wrap to 0
       ; .nowrap:
108    B2A8 0000             cmp.l read_pos(a0), d1          d1 == read_pos? (full)
112    6700 0000             beq.w .overflow                 Buffer full -> overflow
116    2141 0000             move.l d1, write_pos(a0)        Commit new write_pos
120    207C 0000 0000        movea.l #PATCH_ADDR, a0         Reload patch descriptor [2]
126    52A8 0000             addq.l #1, use_count(a0)        Increment use_count
130    222D 0000             move.l event_sequence(a5), d1   d1 = current sequence
134    52AD 0000             addq.l #1, event_sequence(a5)   Increment sequence
138    2400                  move.l d0, d2                   d2 = slot index (old write_pos)
140    2601                  move.l d1, d3                   d3 = sequence number
142    4EAE FF82             jsr _LVOEnable(a6)              Re-enable interrupts (-126)
```

After `Enable()`, d2 holds the ring buffer slot index and d3 holds the
sequence number. These values are preserved across the `Enable()` call
because d2 and d3 are not modified by `Enable()`. The `use_count`
increment ensures the system can detect stubs that are mid-execution
during a global disable, allowing safe drain before teardown.

The capacity check uses `bcs.s` (branch if carry set, i.e., unsigned
less-than) rather than subtraction, avoiding a modulo operation. If
`d1 >= capacity`, it wraps to 0.

#### Entry Pointer Calculation (bytes 146--155, or +8 with NULL filter)

```
Byte   Encoding              Instruction                     Purpose
----   --------              -----------                     -------
146    EF82                  asl.l #7, d2                    d2 = slot_index * 128
148    2A7C 0000 0000        movea.l #RING_ENTRIES_ADDR, a5  a5 = entries base
154    DBC2                  adda.l d2, a5                   a5 = &entries[slot_index]
```

The `asl.l #7` multiplies the slot index by 128 (the event size is
exactly 2^7 bytes). After `adda.l`, a5 points to the start of the
allocated event entry. For the remainder of the prefix and the entire
variable region, a5 is the event pointer used for all field writes.

#### EClock Capture (bytes 156--183, or +8 with NULL filter)

```
Byte   Encoding              Instruction                     Purpose
----   --------              -----------                     -------
156    2C7C 0000 0000        movea.l #TIMER_BASE, a6         a6 = timer.device base
162    518F                  subq.l #8, sp                   Allocate 8-byte EClockVal
164    204F                  movea.l a7, a0                  a0 = &EClockVal (on stack)
166    4EAE FFC4             jsr -60(a6)                     ReadEClock(a0) = LVO -60
170    2B6F 0004 0064        move.l 4(sp), 100(a5)           ev_lo -> entry->eclock_lo
176    3B6F 0002 0068        move.w 2(sp), 104(a5)           ev_hi low word -> eclock_hi
182    508F                  addq.l #8, sp                   Deallocate EClockVal
```

The `EClockVal` structure is 8 bytes (two ULONGs: ev_hi and ev_lo).
`ReadEClock()` fills it and returns the EClock frequency (ignored here;
stored once in `anchor->eclock_freq` at init time). The stub captures
the low 32 bits of `ev_lo` (at stack offset 4) into `entry->eclock_lo`
(event offset 100) and the low 16 bits of `ev_hi` (at stack offset 2)
into `entry->eclock_hi` (event offset 104). See
[event-format.md](event-format.md) for how the daemon reconstructs
timestamps from these fields.

#### Event Header Fill (bytes 184--215, or +8 with NULL filter)

```
Byte   Encoding              Instruction                     Purpose
----   --------              -----------                     -------
184    2B43 0004             move.l d3, 4(a5)                entry->sequence = d3
188    207C 0000 0000        movea.l #PATCH_ADDR, a0         Reload patch descriptor [3]
194    1B68 0000 0001        move.b 0(a0), 1(a5)             entry->lib_id = patch->lib_id
200    3B68 0002 0002        move.w 2(a0), 2(a5)             entry->lvo_offset = patch->lvo_offset
206    2C78 0004             movea.l $4.w, a6                SysBase (also used by variable region)
210    2B6E 0114 0008        move.l 276(a6), 8(a5)           entry->caller_task = ThisTask
```

After this block, the event header fields (sequence, lib_id, lvo_offset,
caller_task) are populated. The patch descriptor is reloaded into a0
(third occurrence of PATCH_ADDR) to copy `lib_id` and `lvo_offset`.
SysBase is loaded into a6 for `ThisTask` (at prefix byte 206) and
remains there for use by the task name capture code in the variable
region.

### Prefix Placeholder Summary

The prefix contains these placeholder values that are patched after
assembly:

| Placeholder | Occurrences | Byte offsets (high word) | Patched with |
|---|---|---|---|
| PATCH_ADDR | 3 | 4, 122+ns, 190+ns | `(ULONG)patch` |
| ANCHOR_ADDR | 1 | 18 | `(ULONG)anchor` |
| RING_ENTRIES_ADDR | 1 | 150+ns | `(ULONG)entries` |
| TIMER_BASE_ADDR | 1 | 158+ns | `(ULONG)anchor->timer_base` |

Where `ns` is the null shift: 0 for standard stubs, 8 for stubs with
the NULL-argument filter. Offsets before byte 76 are never shifted
because the NULL check is inserted at byte 76 (after the daemon task
check).


## Variable Region

The variable region is generated dynamically at install time from the
per-function metadata in the patch descriptor. It is assembled into a
local `var_buf[120]` array (120 UWORDs, providing ample margin for the
worst case) and then copied into the stub between the prefix and suffix.

The variable region contains, in order:

1. Argument copy instructions (0--12 words)
2. `arg_count` immediate write (3 words)
3. `flags` immediate write (3 words)
4. String capture OR indirect name deref (mutually exclusive, variable size)
5. Task name capture (47 words, always present)
6. `valid=2` pre-call marker (2 words, always present)

### 1. Argument Copy

For each argument (up to 4, capped at `min(arg_count, 4)`), a
`move.l` copies the value from the MOVEM frame on the stack into the
event's `args[]` array:

```
move.l <frame_offset>(sp), <entry_arg_offset>(a5)
```

Encoding: `0x2B6F`, followed by the source frame offset (UWORD), then
the destination event offset (UWORD). Each argument copy is 3 words (6
bytes).

The source frame offset comes from `reg_to_frame_offset(arg_regs[i])`.
The destination offset is `offsetof(struct atrace_event, args) + i * 4`,
which is 12, 16, 20, or 24 for args[0] through args[3].

Example for `OpenLibrary(a1=libName, d0=version)`:
- arg0 (a1): `move.l 36(sp), 12(a5)` -- a1's MOVEM frame offset is 36
- arg1 (d0): `move.l 0(sp), 16(a5)` -- d0's MOVEM frame offset is 0

Functions with more than 4 arguments (e.g., `sendto` with 6) only
capture the first 4.

### 2. arg_count Immediate

```
move.b #<count>, 32(a5)                  ; entry->arg_count
```

Encoding: `0x1B7C`, immediate byte value (UWORD), offset 32 (UWORD).
3 words (6 bytes).

The count is normally `min(arg_count, 4)`. For `DEREF_IOREQUEST` and
`DEREF_TEXTATTR` functions, `arg_count` is forced to 2 because the
deref capture writes an extra value (io_Command or ta_YSize) into
`args[1]`, and the daemon needs to know how many args fields are valid.

### 3. flags Immediate

```
move.b #1, 33(a5)                        ; entry->flags = FLAG_HAS_ECLOCK
```

Encoding: `0x1B7C`, `0x0001`, offset of `flags` field (UWORD). 3 words
(6 bytes).

The `FLAG_HAS_ECLOCK` bit (0x01) is always set because the EClock
capture in the prefix always runs. The `FLAG_HAS_IOERR` bit (0x02) may
be set later by the suffix's post-call handler for dos.library functions.

### 4. String Capture

String capture and indirect name dereference are mutually exclusive.
A function has one or the other (or neither), never both. If both
`string_args` and `name_deref_type` are non-zero, `stub_generate_and_install`
returns -1 (programming error).

#### 4a. Single Direct String Capture

Used when exactly one bit is set in `string_args`. Copies up to 63
characters from a NUL-terminated C string argument into
`entry->string_data` (event offset 34).

```
movea.l <frame_ofs>(sp), a0              ; Load string pointer from saved register
lea     34(a5), a1                       ; a1 = &entry->string_data
tst.l   a0                               ; NULL check
beq.s   +8                               ; Skip to clr.b if NULL
moveq   #62, d0                          ; Max 62 iterations (63 chars + stop)
.copy:
move.b  (a0)+, (a1)+                     ; Copy one byte
dbeq    d0, .copy                        ; Loop until NUL or count exhausted
clr.b   (a1)                             ; NUL-terminate
```

11 words (22 bytes). The `dbeq` instruction decrements d0 and loops
back unless d0 reaches -1 (count exhausted) or the Z flag is set (NUL
byte copied). The `clr.b (a1)` after the loop ensures NUL-termination
even when the source string is longer than 62 characters.

#### 4b. Dual String Capture

Used when exactly two bits are set in `string_args` (currently only
`Rename` and `MakeLink`). The 64-byte `string_data` field is split into
two 32-byte halves: `string_data[0..31]` at offset 34 and
`string_data[32..63]` at offset 66. Each half captures up to 31
characters.

Each half follows the same pattern:

```
movea.l <frame_ofs>(sp), a0              ; Load string pointer
lea     <dest_ofs>(a5), a1               ; Destination half
tst.l   a0                               ; NULL check
beq.s   +12                              ; Skip to .null
moveq   #30, d0                          ; Max 30 iterations (31 chars)
.copy:
move.b  (a0)+, (a1)+                     ; Copy one byte
dbeq    d0, .copy                        ; Loop until NUL or exhausted
clr.b   (a1)                             ; NUL-terminate
bra.s   +4                               ; Skip .null block
.null:
clr.b   <dest_ofs>(a5)                   ; Clear first byte (empty string)
```

14 words per half, 28 words total (56 bytes). The `beq.s +12` branch
displacement skips `moveq`(2) + `move.b`(2) + `dbeq`(4) + `clr.b`(2) +
`bra.s`(2) = 12 bytes.

#### 4c. Indirect Name Dereference (DEREF types)

Indirect deref follows a chain of struct pointers to capture a
human-readable name (library name, device name, font name, window title,
volume name) into `string_data`. Eight deref types are supported:

**DEREF_LN_NAME** (type 1) -- 18 words (36 bytes)

One-level dereference: `struct->ln_Name` at offset 10. Used by
`CloseLibrary`, `ObtainSemaphore`, `ReleaseSemaphore`, `GetMsg`,
`PutMsg`, `DeleteMsgPort`, `AddPort`, `WaitPort`.

```
movea.l <frame_ofs>(sp), a0              ; Load struct pointer
tst.l   a0                               ; NULL check (struct)
beq.s   +24                              ; -> .skip_name
movea.l 10(a0), a0                       ; a0 = struct->ln_Name
tst.l   a0                               ; NULL check (name pointer)
beq.s   +16                              ; -> .skip_name
lea     34(a5), a1                       ; &entry->string_data
moveq   #62, d0                          ; Max 62 iterations
.copy:
move.b  (a0)+, (a1)+
dbeq    d0, .copy                        ; -4 displacement
clr.b   (a1)                             ; NUL-terminate
bra.s   +4                               ; -> .done
.skip_name:
clr.b   34(a5)                           ; Empty string_data
; .done:
```

**DEREF_IOREQUEST** (type 2) -- 27 words (54 bytes)

Two-level dereference: `IORequest->io_Device` (offset 20) then
`->ln_Name` (offset 10). Also captures `io_Command` (UWORD at offset
28) into `args[1]`. Used by `DoIO`, `SendIO`, `WaitIO`, `AbortIO`,
`CheckIO`, `CloseDevice`.

```
movea.l <frame_ofs>(sp), a0              ; Load IORequest pointer
tst.l   a0                               ; NULL check
beq.s   +42                              ; -> .skip_name
; io_Command capture:
moveq   #0, d0                           ; Zero-extend
move.w  28(a0), d0                       ; d0 = io_Command (UWORD)
move.l  d0, 16(a5)                       ; entry->args[1] = io_Command
; First deref: io_Device:
movea.l 20(a0), a0                       ; a0 = ioReq->io_Device
tst.l   a0                               ; NULL check
beq.s   +24                              ; -> .skip_name
; Second deref: ln_Name:
movea.l 10(a0), a0                       ; a0 = device->ln_Name
tst.l   a0                               ; NULL check
beq.s   +16                              ; -> .skip_name
; String copy (same as DEREF_LN_NAME):
lea     34(a5), a1
moveq   #62, d0
.copy: move.b (a0)+, (a1)+
dbeq    d0, .copy
clr.b   (a1)
bra.s   +4
.skip_name: clr.b 34(a5)
```

**DEREF_TEXTATTR** (type 3) -- 22 words (44 bytes)

Dereferences `TextAttr->ta_Name` (offset 0) and captures `ta_YSize`
(UWORD at offset 4) into `args[1]`. Used by `OpenFont`.

```
movea.l <frame_ofs>(sp), a0              ; Load TextAttr pointer
tst.l   a0                               ; NULL check
beq.s   +32                              ; -> .skip_name
; ta_YSize capture:
moveq   #0, d0
move.w  4(a0), d0                        ; d0 = ta_YSize
move.l  d0, 16(a5)                       ; entry->args[1] = ta_YSize
; ta_Name deref (offset 0, uses (a0) addressing):
movea.l (a0), a0                         ; a0 = textAttr->ta_Name
tst.l   a0                               ; NULL check
beq.s   +16                              ; -> .skip_name
; String copy:
lea     34(a5), a1
moveq   #62, d0
.copy: move.b (a0)+, (a1)+
dbeq    d0, .copy
clr.b   (a1)
bra.s   +4
.skip_name: clr.b 34(a5)
```

**DEREF_LOCK_VOLUME** (type 4) -- 35 words (70 bytes)

Three BPTR dereferences to resolve a DOS lock to its volume name.
The chain is: `Lock BPTR -> FileLock -> fl_Volume (BPTR, offset 16) ->
DosList -> dol_Name (BPTR to BSTR, offset 40) -> BSTR -> string data`.
Used by `CurrentDir`.

Each BPTR is converted to a real address with `asl.l #2, d0` (BPTRs
are longword-aligned addresses divided by 4). The BSTR format has a
length byte followed by string data (no NUL terminator), so the copy
loop uses `dbf` (decrement and branch unconditionally) rather than
`dbeq`, and appends a `:` character and NUL terminator to form a proper
AmigaOS volume name (e.g., `Workbench:`).

The string length is clamped to 61 characters to leave room for the
`:` suffix and NUL terminator within the 64-byte `string_data` field.

```
movea.l <frame_ofs>(sp), a0              ; Load lock BPTR
move.l  a0, d0                           ; NULL check (BPTR=0 is NULL)
beq.s   +58                              ; -> .skip_vol
asl.l   #2, d0                           ; BADDR(lock)
movea.l d0, a0                           ; a0 = FileLock*
move.l  16(a0), d0                       ; d0 = fl_Volume (BPTR)
beq.s   +48                              ; -> .skip_vol
asl.l   #2, d0                           ; BADDR(volume)
movea.l d0, a0                           ; a0 = DosList*
move.l  40(a0), d0                       ; d0 = dol_Name (BPTR to BSTR)
beq.s   +38                              ; -> .skip_vol
asl.l   #2, d0                           ; BADDR(BSTR)
movea.l d0, a0                           ; a0 = BSTR pointer
moveq   #0, d0
move.b  (a0)+, d0                        ; d0 = BSTR length byte
beq.s   +28                              ; -> .skip_vol (empty)
cmp.w   #61, d0                          ; Clamp to 61
bls.s   +2                               ; -> .len_ok
moveq   #61, d0
; .len_ok:
lea     34(a5), a1                       ; &entry->string_data
subq.w  #1, d0                           ; Adjust for dbf
.vol_copy:
move.b  (a0)+, (a1)+
dbf     d0, .vol_copy                    ; -4 displacement
move.b  #':', (a1)+                      ; Append ':'
clr.b   (a1)                             ; NUL-terminate
bra.s   +4                               ; -> .vol_done
.skip_vol:
clr.b   34(a5)                           ; Empty string_data
; .vol_done:
```

**DEREF_NW_TITLE** (type 5), **DEREF_WIN_TITLE** (type 6),
**DEREF_NS_TITLE** (type 7), **DEREF_SCR_TITLE** (type 8)

DEREF_WIN_TITLE, DEREF_NS_TITLE, and DEREF_SCR_TITLE are each 18
words (36 bytes) and follow the same pattern as `DEREF_LN_NAME` but
with a different field offset:

| Type | Struct | Field | Offset |
|---|---|---|---|
| DEREF_NW_TITLE | NewWindow | Title | 26 |
| DEREF_WIN_TITLE | Window | Title | 32 |
| DEREF_NS_TITLE | NewScreen | DefaultTitle | 20 |
| DEREF_SCR_TITLE | Screen | Title | 22 |

DEREF_NW_TITLE has an extended code path for `OpenWindowTagList`. When
the NewWindow pointer (first argument) is NULL, the stub walks the tag
list (second argument) looking for `WA_Title` (tag ID `0x8000006E`).
The tag walker iterates up to 64 tags with a safety limit, handling
the standard Utility tag protocol: `TAG_DONE` (0, terminates),
`TAG_IGNORE` (1, skip this tag), `TAG_MORE` (2, follow `ti_Data` as a
pointer to a continuation tag list), and `TAG_SKIP` (3, skip `ti_Data`
following tags). If `WA_Title` is found, its `ti_Data` value is used
as the title string pointer for the standard string copy loop. If the
NewWindow pointer is non-NULL, the original `offset 26` dereference
is used.

### 5. Task Name Capture

Always present. 47 words (94 bytes). Captures the name of the calling
task or process into `entry->task_name` (event offset 106, 22 bytes
including NUL).

The code tries the CLI command name first (for CLI processes, which
have a human-readable program name), falling back to `tc_Node.ln_Name`
for non-CLI processes and plain tasks.

This code runs in the pre-call variable region (not the post-call
handler) because memory reads in the post-call suffix handler have been
observed to cause UAE JIT freezes when high-frequency functions are
traced. The task name is the same before and after the call since the
same task is executing.

At entry, a6 = SysBase (set by prefix byte 206) and a5 = event pointer.

```
; --- Try CLI path first ---
movea.l 276(a6), a0                      ; a0 = SysBase->ThisTask
cmpi.b  #13, 8(a0)                       ; NT_PROCESS check (ln_Type)
bne.s   +50                              ; Not a Process -> .use_task_name
move.l  172(a0), d0                      ; d0 = pr_CLI (BPTR)
beq.s   +44                              ; No CLI -> .use_task_name
asl.l   #2, d0                           ; BADDR
movea.l d0, a1                           ; a1 = CommandLineInterface*
move.l  16(a1), d0                       ; d0 = cli_CommandName (BPTR to BSTR)
beq.s   +34                              ; No name -> .use_task_name
asl.l   #2, d0                           ; BADDR(BSTR)
movea.l d0, a1                           ; a1 = BSTR pointer
moveq   #0, d0
move.b  (a1)+, d0                        ; d0 = BSTR length
beq.s   +24                              ; Empty -> .use_task_name
cmp.w   #21, d0                          ; Clamp to 21 chars
bls.s   +2                               ; -> .cli_len_ok
moveq   #21, d0
; .cli_len_ok:
lea     106(a5), a0                      ; &entry->task_name
subq.w  #1, d0                           ; Adjust for dbf
.cli_copy:
move.b  (a1)+, (a0)+
dbf     d0, .cli_copy                    ; -4 displacement
clr.b   (a0)                             ; NUL-terminate
bra.s   +32                              ; -> .name_done

; --- Fallback: tc_Node.ln_Name ---
; .use_task_name:
movea.l 276(a6), a0                      ; Reload ThisTask
movea.l 10(a0), a0                       ; a0 = ln_Name (C string)
move.l  a0, d0                           ; NULL check
beq.s   +16                              ; -> .name_clear
lea     106(a5), a1                      ; &entry->task_name
moveq   #20, d0                          ; Max 21 chars (dbeq stops at NUL)
.tn_copy:
move.b  (a0)+, (a1)+
dbeq    d0, .tn_copy                     ; -4 displacement
clr.b   (a1)                             ; NUL-terminate
bra.s   +4                               ; -> .name_done
; .name_clear:
clr.b   106(a5)                          ; Empty task_name
; .name_done:
```

Key details:

- The NT_PROCESS guard (`cmpi.b #13, 8(a0)`) is essential because a
  plain Task struct is only 92 bytes, and `pr_CLI` is at Process offset
  172 -- 80 bytes past the end of a Task. Reading offset 172 from a
  plain Task would access unrelated memory.

- CLI command names are BSTRs (BCPL strings): a BPTR to a length-prefixed
  string with no NUL terminator. The code converts the BPTR to a real
  address with `asl.l #2`, reads the length byte, clamps to 21
  characters, and copies the string data. Before copying, a `cmpi.b
  #0x20` validation check ensures the first byte of the BSTR data is
  a printable ASCII character (>= 0x20). Strings starting with control
  characters are treated as stale or corrupted pointers, and the stub
  falls through to the `ln_Name` fallback.

- The `ln_Name` fallback uses `dbeq` (stop on NUL) because `ln_Name`
  is a NUL-terminated C string, while the CLI path uses `dbf` (stop on
  count only) because BSTRs are not NUL-terminated. A `cmpi.b #0x20`
  validation check on the `ln_Name` string's first character rejects
  strings starting with control characters, setting `task_name[0]` to
  NUL instead.

### 6. valid=2 Pre-Call Marker

The last instruction in the variable region:

```
move.b  #2, (a5)                         ; entry->valid = 2
```

Encoding: `0x1ABC`, `0x0002`. 2 words (4 bytes).

The value 2 marks the event as "in-progress." This must happen before
the suffix's trampoline calls the original function because blocking
functions (e.g., `RunCommand`, `WaitSelect`) can block indefinitely. If
the event remained at `valid=0` during the block, the daemon's consumer
loop could not advance past this slot, freezing all event consumption
system-wide.

The daemon interprets `valid=2` as a pre-call event: the pre-call fields
(lib_id, args, string_data, etc.) are valid, but post-call fields
(retval, ioerr) are not yet populated. After the original function
returns, the suffix post-call handler overwrites `valid` with 1.

See [event-format.md](event-format.md) for the full semantics of the
`valid` field.


## Suffix Region (156/182/226 bytes)

The suffix template is defined in `stub_suffix[]` (78 UWORDs = 156
bytes). The absolute byte offsets within the assembled stub shift based
on the variable region size.

Two function-specific variants insert additional code between the
return value capture and the IoErr check, expanding the suffix:

- **OpenLibrary** stubs insert a 70-byte bsdsocket per-opener patching
  block, bringing the suffix to 226 bytes.
- **accept()** stubs insert a 26-byte sockaddr capture block, bringing
  the suffix to 182 bytes.

All suffix-relative offsets after the variant insertion point shift by
the insertion size (70 or 26 bytes) when these blocks are present.

The suffix contains four functional blocks (standard suffix):

1. MOVEM restore and trampoline (bytes 0--29)
2. Post-call handler (bytes 30--105)
3. Disabled fast path (bytes 106--115)
4. Overflow path (bytes 116--155)

### MOVEM Restore and Trampoline (suffix bytes 0--29)

The trampoline mechanism calls the original library function while
preserving the entry pointer and sequence number across the call. The
challenge is passing these values through the call without clobbering
any registers that the original function might use.

The solution uses the stack: the entry pointer and the event's sequence
number are smuggled beneath the caller's original saved a5 value.
After the original function returns, it lands at `.post_call` (via a
pushed return address), where these values are accessible at known
stack offsets.

```
Byte   Encoding              Instruction                     Purpose
----   --------              -----------                     -------
  0    4CDF 5FFF             movem.l (sp)+, d0-d7/a0-a4/a6  Restore all saved regs
  4    2F17                  move.l (sp), -(sp)              Duplicate saved_a5 on stack
  6    2F4D 0004             move.l a5, 4(sp)                Store entry ptr over old slot
 10    2F03                  move.l d3, -(sp)                Push saved sequence number
 12    2A6F 000C             movea.l 12(sp), a5              Restore a5 from duplicate
 16    2F7C 0000 0000 000C   move.l #0, 12(sp)               Clear duplicate slot
 24    487A 000A             pea 10(pc)                      Push .post_call address
 28    2F3C 0000 0000        move.l #ORIG_ADDR, -(sp)        Push original func address [1]
 34    4E75                  rts                             "Jump" to original function
```

Stack state evolution through the trampoline:

```
After MOVEM restore (byte 0):
  sp -> [saved_a5]  [caller's return addr]  [caller's args...]

After move.l (sp), -(sp) (byte 4):
  sp -> [saved_a5]  [saved_a5]  [caller's return addr]  ...

After move.l a5, 4(sp) (byte 6):
  sp -> [saved_a5]  [entry_ptr]  [caller's return addr]  ...

After move.l d3, -(sp) (byte 10):
  sp -> [saved_seq]  [saved_a5]  [entry_ptr]  [caller's return addr]  ...

After movea.l 12(sp), a5 (byte 12):
  sp -> [saved_seq]  [saved_a5]  [entry_ptr]  [caller's return addr]  ...
  a5 = caller's original a5 (restored)

After pea 10(pc) (byte 24):
  sp -> [.post_call]  [saved_seq]  [saved_a5]  [entry_ptr]  [caller's return addr]  ...

After move.l #ORIG_ADDR, -(sp) (byte 28):
  sp -> [ORIG_ADDR]  [.post_call]  [saved_seq]  [saved_a5]  [entry_ptr]  [caller's return addr]  ...

After rts (byte 34):
  Pops ORIG_ADDR into PC -> executes original function
  sp -> [.post_call]  [saved_seq]  [saved_a5]  [entry_ptr]  [caller's return addr]  ...
```

When the original function executes `rts`, it pops `.post_call` into PC,
returning execution to the post-call handler. The stack frame is:
`[retval] [saved_seq] [entry_ptr] [caller's return addr]` (where
retval is pushed immediately by the post-call handler).

### Post-Call Handler (suffix bytes 30--105, or extended with variant blocks)

This block executes after the original function returns. It saves the
return value, performs a sequence guard check to detect reused slots,
optionally runs a function-specific post-call block (OpenLibrary BSD
patching or accept() sockaddr capture), optionally captures `IoErr()`
for dos.library functions, marks the event as complete, and returns to
the original caller.

```
Byte   Encoding              Instruction                     Purpose
----   --------              -----------                     -------
 30    2F00                  move.l d0, -(sp)                Save retval (d0)
 32    206F 0008             movea.l 8(sp), a0               a0 = entry ptr (under retval, saved_seq)
 36    B2AF 0004             cmp.l 4(sp), d1                 Compare saved_seq with entry->sequence
                                                             (d1 loaded from 4(a0) at byte 38)
 ...                         beq.s .seq_ok                   Match -> proceed with writes
                             ; Mismatch: slot reused, skip writes
                             ; (still decrements use_count)
                             bra.s .skip_writes
       ; .seq_ok:
 ...   2140 001C             move.l d0, 28(a0)               entry->retval = d0
```

The sequence guard compares the sequence number saved on the stack
during the trampoline setup against the sequence number currently in
the entry slot. If they match, the slot still belongs to this event
and the handler writes `retval` and sets `valid=1`. On mismatch, the
slot has been reused by a different event (due to circular overwrite
or ring wrap), so the handler skips the data writes but still
decrements `use_count`.

#### IoErr Capture (suffix, within post-call handler)

IoErr is only captured for dos.library functions that returned 0
(failure). This conditional avoids calling `IoErr()` for every traced
function, which would be both wasteful and incorrect (IoErr is
per-process state, only meaningful after a DOS failure). The IoErr
capture is skipped on sequence mismatch (the `.skip_writes` path
bypasses it).

```
Byte   Encoding              Instruction                     Purpose
----   --------              -----------                     -------
 ...   0C28 0001 0001        cmp.b #LIB_DOS, 1(a0)          Is this a DOS function?
       6620                  bne.s .skip_ioerr               No -> skip
       4A80                  tst.l d0                        retval == 0? (failure)
       661C                  bne.s .skip_ioerr               Success -> skip
       2F0E                  move.l a6, -(sp)                Save caller's a6
       2C7C 0000 0000        movea.l #DOS_BASE, a6           Load DOSBase
       4EAE FF7C             jsr -132(a6)                    IoErr() = LVO -132
       2C5F                  movea.l (sp)+, a6               Restore a6
       206F 0008             movea.l 8(sp), a0               Reload entry ptr
       1140 0062             move.b d0, 98(a0)               entry->ioerr = (UBYTE)d0
       0028 0002 0021        or.b #2, 33(a0)                 entry->flags |= FLAG_HAS_IOERR
```

The IoErr result is stored as a single byte (`UBYTE`) at event offset
98. This means only IoErr values 0--255 are captured; higher values are
truncated. In practice, AmigaOS IoErr values fit within this range.

The `or.b #2, 33(a0)` sets `FLAG_HAS_IOERR` (0x02) in the flags field
without disturbing `FLAG_HAS_ECLOCK` (0x01) that was set earlier.

Note that a0 is reloaded from the stack after the `IoErr()` call
because the call may have clobbered a0. The entry pointer is at
`8(sp)` (under the saved retval and saved sequence number).

#### OpenLibrary BSD Patching Block (70 bytes)

For the OpenLibrary stub (exec.library, LVO -552), a 70-byte block is
inserted between the return value capture and the IoErr check. This
implements stub-side bsdsocket per-opener patching,
which eliminates the race window that existed with daemon-side patching.

When a program calls `OpenLibrary("bsdsocket.library", ...)`, the
variable region's BSD flag check (30 bytes) compares the captured
library name against `"bsdsocke"` and sets `entry->bsd_flag` (offset
99) to 0xFF on match. The suffix block reads this flag to decide
whether to patch the returned library base.

```
Byte   Encoding              Instruction                     Purpose
----   --------              -----------                     -------
 34    4A28 0063             tst.b 99(a0)                    bsd_flag set?
 38    6700 0040             beq.w .no_bsd_patch (+64)       No flag -> skip
 42    4A80                  tst.l d0                        OpenLibrary returned NULL?
 44    6700 003A             beq.w .no_bsd_patch (+58)       NULL base -> skip
 48    48E7 6072             movem.l d1-d2/a1-a3/a6, -(sp)  Save scratch registers
 52    2C78 0004             movea.l $4.w, a6                SysBase
 56    247C 0000 0000        movea.l #BSD_TABLE, a2          Patch table address [patched]
 62    240A                  move.l a2, d2                   NULL check
 64    6700 001C             beq.w .bsd_done (+28)           No table -> skip
 68    2640                  movea.l d0, a3                  a3 = new bsdsocket base
 70    740E                  moveq #14, d2                   Loop counter (15 entries)
       ; .bsd_loop:
 72    224B                  movea.l a3, a1                  a1 = library base
 74    3052                  movea.w (a2), a0                a0.w = LVO offset
 76    202A 0004             move.l 4(a2), d0                d0 = stub code address
 80    4EAE FE5C             jsr -420(a6)                    SetFunction(a1, a0, d0)
 84    504A                  addq.l #8, a2                   Next table entry
 86    51CA FFF0             dbf d2, .bsd_loop (-16)         Iterate
 90    4EAE FD84             jsr -636(a6)                    CacheClearU()
       ; .bsd_done:
 94    4CDF 4E06             movem.l (sp)+, d1-d2/a1-a3/a6  Restore registers
 98    206F 0004             movea.l 4(sp), a0               Reload entry pointer
102    2017                  move.l (sp), d0                 Reload retval
       ; .no_bsd_patch: (byte 104, continues to IoErr check)
```

The `bsd_table` is a 15-entry array of `struct bsd_patch_entry` (8
bytes each: 2-byte LVO offset, 2 bytes padding, 4-byte stub address).
The loop calls `SetFunction()` for each bsdsocket LVO on the
newly-opened library base, redirecting the caller's bsdsocket function
table entries to atrace stubs. This ensures that bsdsocket calls are
traced from the moment the library is opened, with no window for
untraced calls.

After the loop, `CacheClearU()` flushes instruction caches because
`SetFunction()` modifies the library's jump table (executable code).
Registers d1-d2/a1-a3/a6 are saved and restored around the block to
avoid clobbering values needed by the rest of the post-call handler.
After restore, a0 and d0 are reloaded from the stack since they were
used as scratch.

#### Accept Sockaddr Capture Block (26 bytes)

For the accept() stub (bsdsocket.library, LVO -48), a 26-byte block is
inserted between the return value capture and the IoErr check to
capture the sockaddr_in of the accepted
connection. The variable region pre-clears `string_data[0..7]` so that
failed accepts show zero bytes.

```
Byte   Encoding              Instruction                     Purpose
----   --------              -----------                     -------
 34    0C80 FFFF FFFF        cmpi.l #-1, d0                  accept() failed?
 40    6712                  beq.s +18                       Error -> .skip_accept
 42    2268 0010             movea.l 16(a0), a1              a1 = args[1] (sockaddr ptr)
 46    2209                  move.l a1, d1                   NULL check
 48    670A                  beq.s +10                       NULL -> .skip_accept
 50    2151 0022             move.l (a1), 34(a0)             string_data[0..3] = sa_family + port
 54    2169 0004 0026        move.l 4(a1), 38(a0)            string_data[4..7] = sin_addr
       ; .skip_accept: (byte 60)
```

The sockaddr pointer was saved in `args[1]` (event offset 16) during
the variable region's argument copy. The block reads it from the event
entry (via a0, the entry pointer) rather than from registers, since
all registers were restored by the MOVEM and trampoline before the
original accept() was called.

The capture only fires when accept() succeeds (d0 != -1). On success,
8 bytes of the sockaddr_in structure are copied into `string_data`:
bytes 0--3 contain `sin_family` (2 bytes) and `sin_port` (2 bytes,
network byte order), and bytes 4--7 contain `sin_addr` (4 bytes,
network byte order). The daemon decodes these fields for display.

#### Event Completion and Return (suffix bytes 86--105)

```
Byte   Encoding              Instruction                     Purpose
----   --------              -----------                     -------
 86    10BC 0001             move.b #1, (a0)                 entry->valid = 1 (complete)
       ; .skip_writes:
 90    207C 0000 0000        movea.l #PATCH_ADDR, a0         Load patch descriptor [4]
 96    53A8 0000             subq.l #1, use_count(a0)        Decrement use_count
100    201F                  move.l (sp)+, d0                Restore retval
102    508F                  addq.l #8, sp                   Pop saved_seq + entry pointer
104    4E75                  rts                             Return to original caller
```

Setting `valid` to 1 signals the daemon's consumer that all event fields
(including retval and ioerr) are now populated. On a sequence mismatch,
execution jumps to `.skip_writes`, bypassing the `valid=1` and `retval`
writes but still decrementing `use_count`. The `use_count` decrement
allows the global disable procedure to detect when all in-flight stubs
have completed.

Stack cleanup: d0 (retval) is popped first, then the saved sequence
number and entry pointer are discarded with `addq.l #8, sp`. Finally,
`rts` returns to the original caller using the return address that was
on the stack when the stub was first entered.

### Disabled Fast Path (suffix bytes 106--115)

This is the target of all `.disabled` branches in the prefix (and the
daemon task check and optional NULL-argument filter). It executes when
tracing is disabled for this function, globally, when the task filter
rejects the current task, or when the calling task is the daemon itself.

```
Byte   Encoding              Instruction                     Purpose
----   --------              -----------                     -------
106    2A5F                  movea.l (sp)+, a5               Restore caller's a5
108    2F3C 0000 0000        move.l #ORIG_ADDR, -(sp)        Push original function [2]
114    4E75                  rts                             Tail-call original
```

This path is minimal: 3 instructions, 10 bytes. When the stub is
disabled, the overhead is: save a5, load patch address, test enabled,
branch, restore a5, push original, rts. The original function address
is pushed onto the stack and the `rts` instruction pops it into PC,
effectively performing a tail-call with the original stack frame intact.

### Overflow Path (suffix bytes 116--155)

Executed when the ring buffer is full (write_pos + 1 == read_pos). This
path implements circular overwrite: it increments the overflow counter,
advances `read_pos` to discard the oldest event, re-enables interrupts,
and branches back to the prefix write path to record the new event in
the freed slot.

```
Byte   Encoding              Instruction                     Purpose
----   --------              -----------                     -------
116    52A8 0000             addq.l #1, overflow(a0)         ring->overflow++
120    2028 0008             move.l read_pos(a0), d0         d0 = current read_pos
124    5280                  addq.l #1, d0                   d0 = read_pos + 1
126    B0A8 0000             cmp.l capacity(a0), d0          d0 >= capacity?
130    6502                  bcs.s .nowrap2 (+2)             No wrap needed
132    7000                  moveq #0, d0                    Wrap to 0
       ; .nowrap2:
134    2140 0008             move.l d0, read_pos(a0)         Commit new read_pos
138    4EAE FF82             jsr _LVOEnable(a6)              Re-enable interrupts (-126)
142    ...                   bra.w .prefix_write             Branch back to prefix write path
```

Note that a6 still holds SysBase at this point (loaded at prefix byte
80 for the `Disable()` call), so the `Enable()` call is valid. After
enabling interrupts, execution branches back to the prefix's event fill
code (the entry pointer calculation and event population), using the
slot that was just freed by advancing `read_pos`. The original function
is still traced -- unlike the old drop-new-event design, the circular
buffer always records the newest events.

### Suffix Placeholder Summary

| Placeholder | Occurrences | Suffix-relative offsets (high word) | Patched with |
|---|---|---|---|
| ORIG_ADDR | 3 | 30, 110+pri, 144+pri | Return value of `SetFunction()` |
| PATCH_ADDR | 1 | 92+pri | `(ULONG)patch` |
| DOS_BASE_ADDR | 1 | 62+pri | dos.library base address |
| BSD_TABLE | 1 | 58 (OpenLibrary only) | `(ULONG)anchor->bsd_table` |
| use_count displacement | 1 | 98+pri | `offsetof(atrace_patch, use_count)` |
| overflow displacement | 1 | 118+pri | `offsetof(atrace_ringbuf, overflow)` |

Where `pri` is the post-retval insertion size: 0 for standard stubs,
70 for OpenLibrary, 26 for accept(). Offsets at or after the variant
insertion point shift by `pri`; offsets before the insertion point are
unaffected.


## Address and Displacement Patching

After the three regions are assembled into contiguous memory, all
placeholder values must be patched with actual runtime addresses and
struct field offsets. This is handled by section 4 of
`stub_generate_and_install()`.

### patch_addr() Helper

The `patch_addr()` function writes a 32-bit address into the stub at a
given byte offset by splitting it into two consecutive UWORDs (high word
first, matching 68k big-endian byte order for immediate operands):

```c
static void patch_addr(UWORD *stub, int byte_offset, ULONG addr)
{
    stub[byte_offset / 2]     = (UWORD)(addr >> 16);
    stub[byte_offset / 2 + 1] = (UWORD)(addr);
}
```

The byte offset always points to the high word of the 32-bit immediate
in the instruction encoding. For `movea.l #addr, An`, the instruction
is: opcode (2 bytes) + high word (2 bytes) + low word (2 bytes), so the
byte offset is 2 past the opcode.

### Address Patches

All address patches, showing which runtime value is written and where:

| Address | Source | Occurrences | Byte offsets |
|---|---|---|---|
| PATCH_ADDR | `(ULONG)patch` | 4 | Prefix: 4, 122+ns, 190+ns; Suffix: start+80+pri |
| ANCHOR_ADDR | `(ULONG)anchor` | 1 | Prefix: 18 |
| RING_ENTRIES_ADDR | `(ULONG)entries` | 1 | Prefix: 150+ns |
| TIMER_BASE_ADDR | `(ULONG)anchor->timer_base` | 1 | Prefix: 158+ns |
| DOS_BASE_ADDR | `dos_base` (dos.library) | 1 | Suffix: start+62+pri |
| BSD_TABLE | `anchor->bsd_table` | 1 | Suffix: start+58 (OpenLibrary only) |
| ORIG_ADDR | `SetFunction()` return | 3 | Suffix: start+30, start+110+pri, start+144+pri |

Where `ns` is the null shift (0 or 8), `start` is `suffix_start`
(prefix_bytes + variable region bytes), and `pri` is the post-retval
insertion size (0, 70, or 26). ORIG_ADDR occurrence 1 (at start+18) is
before the insertion point and is not shifted.

### Struct Field Displacement Patches

The template uses `0x0000` placeholders for struct field offsets in
instructions like `tst.l offset(a5)`. These are patched with values
from `offsetof()`:

**Prefix patches (atrace_patch fields):**

| Byte offset | Instruction | Patched with |
|---|---|---|
| 10 | `tst.l enabled(a5)` | `offsetof(atrace_patch, enabled)` = 8 |
| 128+ns | `addq.l #1, use_count(a0)` | `offsetof(atrace_patch, use_count)` = 12 |

**Prefix patches (atrace_anchor fields):**

| Byte offset | Instruction | Patched with |
|---|---|---|
| 24 | `tst.l global_enable(a5)` | `offsetof(atrace_anchor, global_enable)` = 56 |
| 32 | `tst.l filter_task(a5)` | `offsetof(atrace_anchor, filter_task)` = 80 |
| 48 | `cmpa.l filter_task(a5), a6` | `offsetof(atrace_anchor, filter_task)` = 80 |
| 68 | `cmpa.l daemon_task(a5), a6` | `offsetof(atrace_anchor, daemon_task)` = 100 |
| 90+ns | `movea.l ring(a5), a0` | `offsetof(atrace_anchor, ring)` = 60 |
| 132+ns | `move.l event_sequence(a5), d1` | `offsetof(atrace_anchor, event_sequence)` = 72 |
| 136+ns | `addq.l #1, event_sequence(a5)` | `offsetof(atrace_anchor, event_sequence)` = 72 |

**Prefix patches (atrace_ringbuf fields):**

| Byte offset | Instruction | Patched with |
|---|---|---|
| 94+ns | `move.l write_pos(a0), d0` | `offsetof(atrace_ringbuf, write_pos)` = 4 |
| 102+ns | `cmp.l capacity(a0), d1` | `offsetof(atrace_ringbuf, capacity)` = 0 |
| 110+ns | `cmp.l read_pos(a0), d1` | `offsetof(atrace_ringbuf, read_pos)` = 8 |
| 118+ns | `move.l d1, write_pos(a0)` | `offsetof(atrace_ringbuf, write_pos)` = 4 |

**Suffix patches** (offsets shift by `pri` for variant stubs):

| Suffix-relative offset | Instruction | Patched with |
|---|---|---|
| 98+pri | `subq.l #1, use_count(a0)` | `offsetof(atrace_patch, use_count)` = 12 |
| 118+pri | `addq.l #1, overflow(a0)` | `offsetof(atrace_ringbuf, overflow)` = 12 |

### Branch Displacement Calculation

The prefix contains four `beq.w` / `bne.w` branches that target labels
in the suffix. Because the variable region sits between prefix and
suffix, these branch displacements depend on the total variable region
size and must be calculated at install time.

For 68k `Bcc.w` instructions, the displacement is a signed 16-bit value
relative to the address of the displacement word itself (i.e., PC + 2
after fetching the opcode). The formula is:

```
displacement = target_byte - (branch_byte + 2)
```

Where `branch_byte` is the byte offset of the `Bcc.w` opcode and the
displacement word immediately follows it.

**Prefix-to-suffix branches:**

| Branch | Opcode byte | Displacement byte | Target |
|---|---|---|---|
| `beq.w .disabled` | 12 | 14 | suffix_start + 106 + pri |
| `beq.w .disabled` | 26 | 28 | suffix_start + 106 + pri |
| `bne.w .disabled` | 52 | 54 | suffix_start + 106 + pri |
| `beq.w .disabled` (daemon) | 72 | 74 | suffix_start + 106 + pri |
| `beq.w .overflow` | 112+ns | 114+ns | suffix_start + 116 + pri |

Where `pri` is the post-retval insertion size: 0 for standard stubs,
70 for OpenLibrary, 26 for accept(). The `.disabled` and `.overflow`
labels in the suffix shift by the insertion size because they are
located after the variant insertion point.

**NULL-argument filter branch** (when present):

| Branch | Opcode byte | Displacement byte | Target |
|---|---|---|---|
| `beq.w .disabled` | 80 | 82 | suffix_start + 106 + pri |

Displacement calculations from the source code:

```c
p[BEQ_DISABLED_1 / 2] = (UWORD)(disabled_byte - (12 + 2));
p[BEQ_DISABLED_2 / 2] = (UWORD)(disabled_byte - (26 + 2));
p[BNE_DISABLED_3 / 2] = (UWORD)(disabled_byte - (52 + 2));
p[BEQ_DISABLED_4 / 2] = (UWORD)(disabled_byte - (72 + 2));
p[(BEQ_OVERFLOW + ns) / 2] = (UWORD)(overflow_byte - (112 + ns + 2));

/* disabled_byte = suffix_start + 106 + pri */
/* overflow_byte = suffix_start + 116 + pri */
```

Note that branches at byte offsets below 76 are NOT shifted in position
(the NULL check is inserted at byte 76), but their displacements are
larger when the NULL filter is present because the target moved further
away.


## Installation Sequence

The final steps of `stub_generate_and_install()` install the assembled
stub into the target library's jump table:

1. **First `CacheClearU()`**: Flushes the 68k instruction and data
   caches. This is necessary because the stub was written to allocated
   memory using data writes, but will be executed as code. On 68020+
   processors with separate instruction and data caches, stale
   instruction cache lines could cause execution of uninitialized memory.

2. **`Disable()` + `SetFunction()`**: Under interrupt inhibition,
   `SetFunction()` atomically replaces the library's jump table entry
   for this LVO with the stub's starting address. It returns the
   previous function address (either the library's original
   implementation or a previous patch).

3. **Patch ORIG_ADDR**: The return value of `SetFunction()` is written
   into all three ORIG_ADDR slots in the suffix (trampoline push,
   disabled path, overflow path). This must happen after `SetFunction()`
   because the original address is not known until the jump table entry
   is read.

4. **Second `CacheClearU()`**: Flushes caches again after writing the
   ORIG_ADDR values. Without this, the instruction cache might contain
   stale data at the ORIG_ADDR slots (the zero placeholders), causing
   the stub to jump to address 0 instead of the original function.

5. **`Enable()`**: Re-enables interrupts. From this point on, any call
   to the patched library function will execute the stub.

6. **Fill patch descriptor**: `patch->original`, `patch->stub_code`, and
   `patch->stub_size` are set for later use by the status display and
   potential future removal.


## Limitations

- **No stub removal**: Stubs are installed permanently. The `QUIT`
  command disables tracing (`global_enable = 0`) and frees the ring
  buffer, but the stubs remain in memory and in the library jump tables
  as transparent pass-throughs. A reboot is required to fully remove
  them.

- **Maximum 4 captured arguments**: The `args[4]` array limits capture
  to 4 arguments per function. Functions with more arguments (e.g.,
  `sendto` with 6) only have their first 4 arguments recorded.

- **IoErr truncation**: The `ioerr` field is a single `UBYTE`, so
  IoErr values above 255 are truncated. Standard AmigaOS error codes
  fit within this range, but custom error codes from third-party
  libraries could be lost.

- **String length limits**: Single-string capture is limited to 63
  characters; dual-string capture is limited to 31 characters per
  string; task name capture is limited to 21 characters. Longer strings
  are silently truncated.

- **No re-entrancy protection**: If a traced function is called from
  within another traced function's stub (e.g., the EClock `ReadEClock`
  call triggers a traced exec.library call), the inner call will also
  be traced. In practice, the OS functions called by the stub
  (`Disable`, `Enable`, `ReadEClock`, `IoErr`) are either not traced or
  are handled correctly, but adding traces for these functions would
  cause infinite recursion.

- **Post-call memory reads**: Memory reads in the suffix post-call
  handler (after the trampoline `rts` returns from the original
  function) have been observed to cause UAE JIT freezes when
  high-frequency functions are traced. This is believed to be a JIT
  interaction with code reached via the trampoline mechanism. All memory
  reads that could be moved to the pre-call region (such as task name
  capture) have been moved there. The remaining post-call reads
  (entry pointer from stack, retval from d0) have not exhibited this
  problem.
