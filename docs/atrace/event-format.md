# The 128-Byte Event Structure

This document is the canonical reference for the binary layout of
`struct atrace_event`, the fixed-size record that atrace stubs write into
the ring buffer for every traced library call. All byte offsets, sizes,
and type information here are derived directly from `atrace/atrace.h`.

For how the daemon interprets and formats these fields for display, see
[reading-output.md](reading-output.md). For per-function metadata
(which arguments are captured, which deref type applies, error
classification), see [traced-functions.md](traced-functions.md).


## Field Layout

Every event occupies exactly 128 bytes (`ATRACE_EVENT_SIZE`). This size
is enforced at compile time by a static assertion in `atrace/main.c`:

```c
typedef char assert_event_size [(sizeof(struct atrace_event) == 128) ? 1 : -1];
```

The power-of-two size enables the stub to compute the entry address from
a ring buffer index using a single `asl.l #7` (shift left by 7)
instruction rather than a multiply.

| Field | Offset | Size | C Type | Description |
|-------------|--------|------|-----------------|------------------------------------------------|
| valid | 0 | 1 | volatile UBYTE | Slot state: 0=empty, 1=complete, 2=in-progress |
| lib_id | 1 | 1 | UBYTE | Library identifier (0--6) |
| lvo_offset | 2 | 2 | WORD | Negative LVO offset (e.g. -30 for dos.Open) |
| sequence | 4 | 4 | ULONG | Monotonic sequence number |
| caller_task | 8 | 4 | APTR | Pointer to the calling task's Task/Process struct |
| args[4] | 12 | 16 | ULONG[4] | Up to 4 captured function arguments |
| retval | 28 | 4 | ULONG | Function return value |
| arg_count | 32 | 1 | UBYTE | Number of valid entries in args[] (0--4) |
| flags | 33 | 1 | UBYTE | Bitfield: FLAG_HAS_ECLOCK, FLAG_HAS_IOERR |
| string_data | 34 | 64 | char[64] | String argument or indirect name capture |
| ioerr | 98 | 1 | UBYTE | DOS IoErr() value captured post-call |
| bsd_flag | 99 | 1 | UBYTE | bsdsocket detection flag (set by OpenLibrary stub) |
| eclock_lo | 100 | 4 | ULONG | EClock timestamp, low 32 bits |
| eclock_hi | 104 | 2 | UWORD | EClock timestamp, high 16 bits |
| task_name | 106 | 22 | char[22] | Calling task/process name (21 chars + NUL) |

Total: 128 bytes.


## Field Details

### valid (offset 0, 1 byte)

The `valid` field is a tri-state slot marker that synchronizes the
producer (stub code running in the calling task's context) with the
consumer (the daemon's polling loop in `trace_poll_events()`).

| Value | Meaning | Written by |
|-------|---------|------------|
| 0 | Slot is empty. No event data is present. The ring buffer is allocated with `MEMF_CLEAR`, so all slots start at 0. The daemon also clears valid to 0 after consuming an event. | Ring buffer allocator; daemon after consumption |
| 1 | Event is complete. All fields -- including retval, ioerr, and flags -- are fully populated. | Stub post-call handler (suffix byte 74) |
| 2 | Event is in-progress. The original function is currently executing. Pre-call fields (lib_id, lvo_offset, sequence, caller_task, args, arg_count, flags, string_data, eclock_lo/hi, task_name) are valid. Post-call fields (retval, ioerr, FLAG_HAS_IOERR) may contain stale data from a previous ring buffer occupant. | Stub variable region (last instruction before suffix) |

The stub sets `valid=2` immediately before the trampoline jumps to the
original library function. This is necessary because blocking functions
(e.g. `RunCommand`, `Execute`, `WaitSelect`) can execute for an
unbounded duration. If the slot remained at `valid=0` during this time,
the consumer could not advance past it, stalling the entire ring buffer.

When the original function returns, the post-call handler writes the
return value into `retval`, optionally captures IoErr, and then sets
`valid=1` to signal completion.

The daemon handles `valid=2` events with patience: it waits up to
`INFLIGHT_PATIENCE` (3) consecutive polls at the same ring position
before consuming the event as-is. At the default 20ms poll interval,
this gives non-blocking functions approximately 60ms to complete. For
truly blocking functions that exceed this patience, the event is consumed
with incomplete post-call fields. The daemon suppresses IoErr display
for such events by checking `ev->valid == 1` before formatting the IoErr
value.


### lib_id (offset 1, 1 byte)

Identifies which AmigaOS shared library the traced function belongs to.
Values are defined as `LIB_*` constants in `atrace.h`:

| Value | Constant | Library |
|-------|--------------|---------------------|
| 0 | LIB_EXEC | exec.library |
| 1 | LIB_DOS | dos.library |
| 2 | LIB_INTUITION | intuition.library |
| 3 | LIB_BSDSOCKET | bsdsocket.library |
| 4 | LIB_GRAPHICS | graphics.library |
| 5 | LIB_ICON | icon.library |
| 6 | LIB_WORKBENCH | workbench.library |

Written by the stub prefix, which copies the value from the patch
descriptor: `move.b 0(a0), 1(a5)` at prefix byte 174 (where a0 points
to the `atrace_patch` struct and a5 points to the event entry).


### lvo_offset (offset 2, 2 bytes)

The negative Library Vector Offset that identifies the specific function
within its library. For example, `dos.Open` has LVO -30, meaning its
jump table entry is at offset -30 from the library base pointer.

This field, combined with `lib_id`, uniquely identifies the traced
function. The daemon's `lookup_func()` uses this pair to find the
corresponding `trace_func_entry` for name resolution and argument
formatting.

Written by the stub prefix: `move.w 2(a0), 2(a5)` at prefix byte 180.


### sequence (offset 4, 4 bytes)

A monotonically increasing 32-bit counter assigned from
`anchor->event_sequence` under `Disable()`/`Enable()` protection. Each
event gets a unique sequence number.

The sequence number serves two purposes:

1. **Ordering.** Events are numbered in the order their ring buffer
   slots were reserved, which corresponds to the order the library calls
   entered the stub. This is not necessarily the order the calls
   completed.

2. **Gap detection.** A gap in sequence numbers seen by the consumer
   indicates dropped events due to ring buffer overflow. The overflow
   counter in the ring buffer header tracks the total count.

The counter wraps at 2^32 (approximately 4.3 billion events).

Written by the stub prefix at byte 164: `move.l d3, 4(a5)`.


### caller_task (offset 8, 4 bytes)

A raw pointer to the calling task's `struct Task` (or `struct Process`).
This is captured from `SysBase->ThisTask` (offset 276 from ExecBase).

The daemon uses this pointer for:
- **Task name resolution.** It maintains a task name cache (64 entries,
  refreshed every ~2 seconds) that maps task pointers to resolved names.
- **Self-filtering.** Events from the daemon's own task pointer are
  silently dropped.
- **Task filter matching.** During `TRACE RUN`, only events from the
  target task's pointer are consumed.

The pointer is not dereferenced by the stub -- it is stored as an opaque
identifier. All name resolution happens on the daemon side.

Written by the stub prefix at byte 190: `move.l 276(a6), 8(a5)` (where
a6 = SysBase).


### args[4] (offset 12, 16 bytes)

Up to four function arguments, each stored as a 32-bit ULONG. The
number of valid entries is indicated by `arg_count`.

Arguments are captured from the saved register frame on the stack. The
stub's prefix saves all volatile registers (`d0-d7/a0-a4/a6`) via
`MOVEM.L` before reserving a ring buffer slot. The variable region then
copies specific registers from the frame using offsets computed from the
patch descriptor's `arg_regs[]` array.

For functions with indirect deref capture (`DEREF_IOREQUEST`,
`DEREF_TEXTATTR`), the stub forces `arg_count` to 2 and stores an
additional derived value in `args[1]`:
- `DEREF_IOREQUEST`: `args[1]` = `io_Command` (UWORD from IORequest
  offset 28, zero-extended to ULONG).
- `DEREF_TEXTATTR`: `args[1]` = `ta_YSize` (UWORD from TextAttr offset
  4, zero-extended to ULONG).

Written by the stub variable region (pre-call). Each argument is a
`move.l d16(sp), d16(a5)` instruction that copies from the MOVEM frame
to the event entry.


### retval (offset 28, 4 bytes)

The return value of the original library function, captured from
register d0 after the function returns.

For `void` functions, this field is written but its value is meaningless.
The daemon checks the per-function `result_type` metadata to determine
how to format and classify the return value.

Written by the stub post-call handler (suffix byte 30): `move.l d0,
28(a0)` (where a0 = event entry pointer retrieved from the stack after
the trampoline return).

For in-progress events (`valid=2`), this field may contain stale data
from a previous ring buffer occupant. The daemon accounts for this when
formatting.


### arg_count (offset 32, 1 byte)

The number of valid entries in the `args[]` array, ranging from 0 to 4.
Functions with more than 4 register arguments have only the first 4
captured.

For functions with `DEREF_IOREQUEST` or `DEREF_TEXTATTR` name capture,
`arg_count` is forced to 2 regardless of the function's actual argument
count. This accounts for the derived field stored in `args[1]`.

Written by the stub variable region (pre-call) as an immediate byte:
`move.b #<count>, 32(a5)`.


### flags (offset 33, 1 byte)

A bitfield with two defined flags:

| Bit | Mask | Constant | Meaning |
|-----|------|-------------------|---------|
| 0 | 0x01 | FLAG_HAS_ECLOCK | EClock timestamp fields (eclock_lo, eclock_hi) are valid |
| 1 | 0x02 | FLAG_HAS_IOERR | The ioerr field contains a valid IoErr() value |

Bits 2--7 are reserved and currently unused.

**FLAG_HAS_ECLOCK** is set in the variable region (pre-call) as part of
the initial flags write: `move.b #1, 33(a5)`. This flag is always set
because EClock capture is mandatory (timer.device must be open for
atrace to load). The flag exists for forward compatibility -- a future
version could conditionally disable EClock capture.

**FLAG_HAS_IOERR** is set in the post-call handler (suffix bytes 68--73)
using a bit-OR instruction: `or.b #2, 33(a0)`. It is only set when both
conditions are met:
1. The function belongs to dos.library (`lib_id == LIB_DOS`).
2. The function returned zero (indicating failure for most DOS
   functions).

The bit-OR approach (`or.b #2`) preserves FLAG_HAS_ECLOCK (bit 0) that
was already set in the variable region.


### string_data (offset 34, 64 bytes)

A 64-byte buffer used to capture human-readable string data associated
with the function call. The content and layout depend on the function's
configuration in the patch descriptor.

There are three mutually exclusive capture modes:

#### Direct String Capture (single string)

For functions that take a C string argument (e.g. `dos.Open` takes a
filename, `exec.OpenLibrary` takes a library name), the string is copied
directly from the register argument. Maximum 63 characters plus a NUL
terminator. The copy loop uses `moveq #62, d0` as a counter with `dbeq`
(decrement-and-branch-until-equal), stopping at either the counter limit
or a NUL byte in the source, whichever comes first.

If the source string is 63 characters or longer, it is truncated. The
daemon appends `...` to indicate truncation when the captured string
fills all 63 bytes.

A NULL pointer check prevents crashes: if the string argument register
is NULL, `string_data[0]` is set to NUL (empty string).

Which argument is treated as the string is determined by the
`string_args` bitmask in the patch descriptor. Bit N set means argument
N is a string.

#### Direct String Capture (dual string)

Functions with two string arguments (currently `dos.Rename` and
`dos.MakeLink`) split `string_data` into two 32-byte halves:

| Half | Byte range | Max chars | Content |
|------|----------------------|-----------|---------|
| First | string_data[0..31] | 31 | First string argument |
| Second | string_data[32..63] | 31 | Second string argument |

Each half is independently NUL-terminated. The copy loop for each half
uses `moveq #30, d0` as a counter. Each half has its own NULL pointer
check.

#### Indirect Name Capture (deref types)

For functions that take struct pointers rather than strings, the stub
dereferences the pointer to extract a human-readable name. The
`name_deref_type` field in the patch descriptor selects the
dereference strategy:

| Type | Constant | Description | Used by |
|------|---------------------|-------------|---------|
| 0 | DEREF_NONE | No indirect capture | Most functions |
| 1 | DEREF_LN_NAME | `struct->ln_Name` (offset 10) | ObtainSemaphore, ReleaseSemaphore, GetMsg, PutMsg, CloseLibrary, DeleteMsgPort, AddPort, WaitPort |
| 2 | DEREF_IOREQUEST | `IORequest->io_Device->ln_Name` (offsets 20, then 10) | DoIO, SendIO, WaitIO, AbortIO, CheckIO, CloseDevice |
| 3 | DEREF_TEXTATTR | `TextAttr->ta_Name` (offset 0) | OpenFont |
| 4 | DEREF_LOCK_VOLUME | Lock BPTR -> `fl_Volume` -> `dol_Name` BSTR | CurrentDir |
| 5 | DEREF_NW_TITLE | `NewWindow->Title` (offset 26) | OpenWindow, OpenWindowTagList |
| 6 | DEREF_WIN_TITLE | `Window->Title` (offset 32) | CloseWindow, ActivateWindow, WindowToFront, WindowToBack |
| 7 | DEREF_NS_TITLE | `NewScreen->DefaultTitle` (offset 20) | OpenScreen, OpenScreenTagList |
| 8 | DEREF_SCR_TITLE | `Screen->Title` (offset 22) | CloseScreen |
| 9 | DEREF_SOCKADDR | `sockaddr_in`: 8 bytes into `string_data[0..7]` (from `arg_regs[1]`) | bind, connect |
| 10 | DEREF_SOCKADDR_3 | `sockaddr_in`: 8 bytes into `string_data[0..7]` (from `arg_regs[3]`) | sendto |

Direct string capture and indirect name capture are mutually exclusive.
A function uses one or the other, never both. The stub generator rejects
any patch descriptor that has both `string_args != 0` and
`name_deref_type != 0`.

All indirect capture paths include NULL pointer checks at each level of
indirection to prevent crashes from invalid struct pointers.

For `DEREF_LOCK_VOLUME`, the stub performs three BPTR-to-pointer
conversions (`asl.l #2`) and reads a BSTR (length-prefixed string),
appending `:` to form a standard AmigaOS volume name (e.g. `SYS:`).
Volume names are clamped to 61 characters.

Written by the stub variable region (pre-call).


### ioerr (offset 98, 1 byte)

The value of `IoErr()` captured immediately after the original function
returns, but only for dos.library functions that returned a failure
value (zero).

The stub's post-call handler checks `lib_id == LIB_DOS` and `retval ==
0` before calling `IoErr()` via `jsr -132(a6)` (where a6 is loaded with
the dos.library base). The low byte of the IoErr result is stored here.

**Limitation:** This field is a `UBYTE`, limiting it to values 0--255.
Standard AmigaOS DOS error codes range from 103 to 233, which all fit
within this range. However, any custom or third-party error codes
exceeding 255 are truncated to their low byte. This is a deliberate
space optimization within the 128-byte event structure. See
[limitations.md](limitations.md) for further discussion.

When valid, the daemon decodes the value to a human-readable name (e.g.
205 = "object not found") using its `dos_error_name()` lookup table,
which covers all standard AmigaOS error codes from 103 through 233.

The `FLAG_HAS_IOERR` bit in `flags` indicates whether this field
contains valid data. When the flag is not set, this field should be
ignored.

Written by the stub post-call handler (suffix byte 64): `move.b d0,
98(a0)`. The FLAG_HAS_IOERR bit is then set at suffix bytes 68--73:
`or.b #2, 33(a0)`.


### bsd_flag (offset 99, 1 byte)

bsdsocket detection flag, written by the OpenLibrary stub's variable
region. After string capture fills `string_data` with the library name,
the stub compares the first 8 bytes against `"bsdsocke"` (the first 8
characters of `bsdsocket.library`). If matched, `bsd_flag` is set to
0xFF; otherwise it remains 0 (cleared by `clr.b` before the comparison).

The post-call suffix handler reads this byte to decide whether to apply
per-opener bsdsocket patching to the newly-opened library base. This
flag exists only in OpenLibrary events; all other function stubs leave
the byte at its `MEMF_CLEAR` default of zero.


### eclock_lo (offset 100, 4 bytes)

The low 32 bits of the EClock timestamp captured at the moment the stub
reserves a ring buffer slot, before the original function is called.

The EClock is read via `ReadEClock()` (`jsr -60(a6)` where a6 =
timer.device base). `ReadEClock` fills an 8-byte `EClockVal` structure
on the stack with the current EClock counter. The stub copies the low
longword (offset 4 of the struct, since Amiga is big-endian and the low
32 bits are at the higher address) to this field.

Written by the stub prefix at byte 150: `move.l 4(sp), 100(a5)`.


### eclock_hi (offset 104, 2 bytes)

The low 16 bits of the EClock counter's high longword. Together with
`eclock_lo`, this forms a 48-bit timestamp:

```
full_eclock = (eclock_hi << 32) | eclock_lo
```

Only 16 bits of the high longword are stored (the low word at offset 2
of the `EClockVal` struct). For a typical PAL EClock frequency of
709,379 Hz, 48 bits provides a range of approximately 3.97 days before
wrapping. The full 64-bit EClock value would last centuries, but 48 bits
is sufficient for any practical tracing session and saves 2 bytes in the
event structure.

The daemon converts the 48-bit EClock value to wall-clock time by:
1. Capturing a wall-clock epoch (`DateStamp`) when the trace session
   starts.
2. Lazily capturing the EClock baseline from the first event with
   `FLAG_HAS_ECLOCK`.
3. Computing elapsed ticks as a 48-bit subtraction (event minus
   baseline).
4. Converting elapsed ticks to seconds and microseconds using the
   `eclock_freq` value from the anchor.

The EClock frequency varies by video standard: 709,379 Hz for PAL
systems, 715,909 Hz for NTSC systems. The actual frequency is read from
`ReadEClock()`'s return value during `atrace_loader` initialization and
stored in `anchor->eclock_freq`.

Written by the stub prefix at byte 156: `move.w 2(sp), 104(a5)`.


### task_name (offset 106, 22 bytes)

The name of the calling task or process, captured in the stub's variable
region (pre-call). Maximum 21 characters plus a NUL terminator.

The capture logic follows a two-tier resolution:

1. **CLI processes.** If `tc_Node.ln_Type == NT_PROCESS` (13) and
   `pr_CLI` (Process offset 172) is non-NULL, the stub reads the CLI
   command name via `pr_CLI -> cli_CommandName` (CLI struct offset 16).
   The command name is a BSTR (BPTR to a length-prefixed string). The
   BPTR is converted to a real address with `asl.l #2`, and up to 21
   bytes of the string data are copied. No basename extraction is
   performed at the stub level -- the full command name (potentially
   including path components) is stored as-is. Basename extraction
   happens on the daemon side.

2. **Non-CLI tasks.** If the task is not an NT_PROCESS or has no CLI
   structure, the stub falls back to `tc_Node.ln_Name` (Node offset 10),
   which is a standard C string pointer. Up to 21 characters are copied
   with a `dbeq` loop.

The NT_PROCESS type check is critical: a plain `struct Task` is only 92
bytes, while `pr_CLI` lives at Process offset 172 -- reading that offset
from a plain Task would access memory 80 bytes past the end of the
struct. The stub verifies `cmpi.b #13, 8(a0)` before accessing any
Process-specific fields.

If the ln_Name pointer is NULL, `task_name[0]` is set to NUL (empty
string).

The daemon uses this embedded name as a fallback when the task name
cache cannot resolve the caller_task pointer (e.g. because the process
has already exited). The daemon's `trace_format_event()` prefers its own
cache-resolved name but falls back to `ev->task_name` when the cache
yields only a generic `<task 0x...>` placeholder.

Written by the stub variable region (pre-call), 47 words (94 bytes) of
generated code.


## Write Phases: Pre-Call vs. Post-Call

The event fields are populated in two distinct phases, separated by the
execution of the original library function:

### Pre-call (prefix + variable region)

All fields except `retval`, `ioerr`, and FLAG_HAS_IOERR:

| Field | Written by |
|-------------|-----------|
| sequence | Prefix (byte 164) |
| lib_id | Prefix (byte 174) |
| lvo_offset | Prefix (byte 180) |
| caller_task | Prefix (byte 190) |
| eclock_lo | Prefix (byte 150) |
| eclock_hi | Prefix (byte 156) |
| args[0..3] | Variable region |
| arg_count | Variable region |
| flags | Variable region (FLAG_HAS_ECLOCK = 0x01) |
| string_data | Variable region |
| task_name | Variable region |
| valid | Variable region (set to 2, last instruction) |

### Post-call (suffix post-call handler)

Completed after the original function returns via the trampoline:

| Field | Written by |
|-------|-----------|
| retval | Suffix (byte 30) |
| ioerr | Suffix (byte 64), dos.library failures only |
| flags | Suffix (bytes 68--73), OR with FLAG_HAS_IOERR |
| valid | Suffix (byte 74), set to 1 |


## Patch Descriptor (struct atrace_patch)

Each traced function has a corresponding 40-byte patch descriptor that
the stub code references at runtime. The patch descriptor controls the
stub's behavior: which function to call, whether it is enabled, and how
to capture arguments.

| Field | Offset | Size | C Type | Description |
|-----------------|--------|------|-----------------|----------------------------------------------|
| lib_id | 0 | 1 | UBYTE | Library identifier (same as event lib_id) |
| padding0 | 1 | 1 | UBYTE | Alignment padding |
| lvo_offset | 2 | 2 | WORD | Negative LVO offset |
| func_id | 4 | 2 | UWORD | Function index within the library |
| arg_count | 6 | 2 | UWORD | Number of register arguments |
| enabled | 8 | 4 | volatile ULONG | Per-patch enable flag (0=disabled, 1=enabled) |
| use_count | 12 | 4 | volatile ULONG | In-flight event count for drain synchronization |
| original | 16 | 4 | APTR | Original function address (saved by SetFunction) |
| stub_code | 20 | 4 | APTR | Pointer to allocated stub memory |
| stub_size | 24 | 4 | ULONG | Stub memory allocation size |
| arg_regs[8] | 28 | 8 | UBYTE[8] | Register index for each argument (0=d0 through 14=a6) |
| string_args | 36 | 1 | UBYTE | Bitmask: bit N set = argument N is a C string |
| name_deref_type | 37 | 1 | UBYTE | DEREF_* constant for indirect name capture |
| skip_null_arg | 38 | 1 | UBYTE | Register to NULL-check for skip filtering (0=disabled) |
| padding_end | 39 | 1 | UBYTE | Alignment padding |

Compile-time assertion: `sizeof(struct atrace_patch) == 40`.

The `enabled` and `use_count` fields are declared `volatile` because
they are accessed concurrently by stub code (running in arbitrary task
contexts) and by the loader/daemon (running in their own process
contexts). The `enabled` flag is the fast-path check: stubs test it
first and branch directly to the disabled path (transparent pass-through
to the original function) if it is zero. The `use_count` is incremented
when a stub begins processing and decremented after the post-call
handler completes, allowing the `DISABLE` command to wait for all
in-flight events to drain before declaring tracing fully stopped.


## Ring Buffer Header (struct atrace_ringbuf)

The ring buffer header is a 16-byte structure that precedes the event
entry array in memory:

| Field | Offset | Size | C Type | Description |
|-----------|--------|------|-----------------|-----------------------------------------------|
| capacity | 0 | 4 | ULONG | Number of event slots |
| write_pos | 4 | 4 | volatile ULONG | Next slot to be written by a producer (stub) |
| read_pos | 8 | 4 | volatile ULONG | Next slot to be read by the consumer (daemon) |
| overflow | 12 | 4 | volatile ULONG | Cumulative count of dropped events |

Compile-time assertion: `sizeof(struct atrace_ringbuf) == 16`.

The event entry array begins immediately after this header at byte
offset 16. The total allocation size is `16 + 128 * capacity` bytes.
Memory is allocated with `MEMF_PUBLIC | MEMF_CLEAR`, ensuring all event
slots start with `valid=0` and the ring buffer survives the loader
process's exit.

Slot reservation is performed under `Disable()`/`Enable()` to prevent
concurrent stubs from claiming the same slot. The producer advances
`write_pos`, and if `write_pos` would equal `read_pos` (buffer full),
the `overflow` counter is incremented and the event is dropped.
