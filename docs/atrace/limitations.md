# Known Limitations

This document catalogs the known constraints, design trade-offs, and
platform-specific considerations of the atrace system. Each limitation
states what the constraint is, why it exists, what the practical impact
is, and any available workarounds.

## Event Capture Limits

### Maximum 4 Arguments Captured

The `args[4]` field in `struct atrace_event` stores at most four 32-bit
argument values per event (16 bytes at offset 12). Functions with more
than four arguments have the excess arguments silently dropped.

Affected functions:

| Function | Total args | Captured | Dropped |
|---|---|---|---|
| `sendto` | 6 | fd, buf, len, flags | to, tolen |
| `recvfrom` | 6 | fd, buf, len, flags | from, fromlen |
| `WaitSelect` | 6 | nfds, sigmask, readfds, writefds | exceptfds, timeout |
| `setsockopt` | 5 | fd, level, optname, optval | optlen |
| `getsockopt` | 5 | fd, level, optname, optval | optlen |
| `AddAppIconA` | 7 | id, userdata, text, msgport | lock, diskobj, taglist |
| `AddAppWindowA` | 5 | id, userdata, window, msgport | taglist |
| `AddAppMenuItemA` | 5 | id, userdata, text, msgport | taglist |

**Why:** The 128-byte fixed-size event structure leaves limited space
after accounting for timestamps, task name, string data, and metadata.
Four arguments covers the vast majority of traced functions (91 of 99
have four or fewer arguments).

**Impact:** Diagnostic information for the dropped arguments is
unavailable. For bsdsocket functions, the `arg_regs` order in `funcs.c`
is chosen to prioritize the most debugging-relevant values (e.g.,
`WaitSelect` captures `nfds` and `sigmask` rather than the less useful
fd_set pointers).

**Workaround:** None. The argument limit is baked into the event
structure. Adding more argument slots would require changing
`ATRACE_EVENT_SIZE` and breaking the 128-byte fixed-size layout.

See [event-format.md](event-format.md) for the complete event structure
layout.

### String Data Truncation (63 Characters)

The `string_data` field is 64 bytes (offset 34), providing space for 63
characters plus a NUL terminator. The stub copies string arguments using
a `dbeq` loop with counter `moveq #62, d0`, which copies at most 63
bytes before inserting a NUL.

**Why:** The 128-byte event structure constrains how much space is
available for string data. 64 bytes is a trade-off between capturing
useful path/name fragments and keeping events compact.

**Impact:** Long Amiga paths (e.g., `Work:Projects/LongDirectory/SubDir/
filename.txt`) are truncated. The truncation is silent -- there is no
indicator in the event that truncation occurred.

**Workaround:** None within atrace. If full paths are needed, correlate
truncated atrace output with other system information.

### Dual-String Mode: 31 Characters Each

When a function captures two string arguments (e.g., `Rename` captures
both `oldName` and `newName`, `MakeLink` captures `name` and `dest`),
the 64-byte `string_data` field is split into two 32-byte halves. Each
half holds at most 31 characters plus a NUL terminator. The stub uses
`moveq #30, d0` for each half.

**Why:** Two strings must share the same 64-byte field. Splitting evenly
at 32 bytes each is the simplest and most balanced approach.

**Impact:** Each string is limited to 31 characters instead of 63. Long
paths in rename or link operations lose more information than
single-string captures.

### Task Name Truncation (21 Characters)

The `task_name` field is 22 bytes (offset 106), holding at most 21
characters plus a NUL terminator. The stub copies using `moveq #20, d0`.

**Why:** The task name field was added to the event structure to provide
context without requiring a separate daemon-side lookup for every event.
22 bytes was the space remaining in the 128-byte layout.

**Impact:** Long CLI command names are truncated. For example, a process
named `AmiTCP/IP-DialupManager` would be stored as
`AmiTCP/IP-DialupManag`. The daemon also maintains a task name cache
with 64-byte names for richer display, but the event-embedded name is
what gets recorded.

### IoErr Captured Only on dos.library Failures

The post-call handler in the stub suffix captures `IoErr()` only when
two conditions are met: the event's `lib_id` equals `LIB_DOS` (value 1),
checked by `cmp.b #LIB_DOS, 1(a0)` at suffix offset 34, and the
function's return value is zero (indicating failure), checked by
`tst.l d0` / `bne.s .skip_ioerr` immediately after. Events from
non-DOS libraries or from successful DOS calls have the `ioerr` field
set to zero and the `FLAG_HAS_IOERR` flag cleared.

**Why:** `IoErr()` is a dos.library concept. Other libraries use
different error reporting mechanisms (e.g., bsdsocket.library uses
`Errno()`, exec.library returns error codes directly). Calling `IoErr()`
for non-DOS functions would produce meaningless values.

**Impact:** Error diagnosis for non-DOS functions relies solely on the
return value. bsdsocket errno values, for example, are not captured.

**Workaround:** For bsdsocket errors, the return value itself (typically
-1) indicates failure. The specific errno must be determined through
other means.

### IoErr Field Truncation (UBYTE, Max 255)

The `ioerr` field in `struct atrace_event` is a single `UBYTE` at offset
98, storing only the low byte of the `IoErr()` return value.

**Why:** The 128-byte fixed-size event structure requires tight packing.
A full 32-bit IoErr field would consume 3 additional bytes. Standard
AmigaOS DOS error codes range from 103 (`ERROR_OBJECT_IN_USE`) to 233
(`ERROR_NO_DISK`), all fitting within 0-255.

**Impact:** Custom or extended error codes above 255 are truncated to
their low byte. This is unlikely to occur in practice with standard
AmigaOS software, but third-party DOS handlers that define error codes
above 255 would produce misleading values.

See [event-format.md](event-format.md) for the ioerr field layout.

## Ring Buffer Limits

### No Backpressure on Producers

When the ring buffer is full (write position would equal read position
after advancing), the stub increments an `overflow` counter and skips
event recording. The traced function call still executes normally -- only
the trace event is lost.

**Why:** The stub runs in the context of the calling task with interrupts
disabled (`Disable()`/`Enable()` around slot reservation). Blocking the
producer would freeze the calling task and potentially deadlock the
system, since the daemon (consumer) might need CPU time to drain events.

**Impact:** Under high call rates, events are silently dropped. The
daemon detects overflow by checking the ring's `overflow` counter and
reports it to clients as `# OVERFLOW <n> events dropped`. The dropped
events are unrecoverable.

**Workaround:** Increase ring buffer capacity with the `BUFSZ` argument
to `atrace_loader` (default 8192 entries). Disable high-frequency
functions that are not relevant to the investigation. Use TRACE RUN with
stub-level task filtering to reduce the event rate.

See [ring-buffer.md](ring-buffer.md) for the ring buffer architecture.

### Default Buffer Size: 8192 Entries (1 MB)

The default ring buffer holds 8192 events at 128 bytes each, totaling
1 MB of memory. This is allocated from `MEMF_PUBLIC` at load time and
persists until reboot (or QUIT, which frees only the ring buffer).

**Why:** 8192 entries balances memory consumption against the ability to
absorb burst activity without overflow. At typical system call rates
(hundreds to low thousands per second), this provides several seconds of
buffering.

**Impact:** Systems with limited memory may need a smaller buffer.
High-throughput scenarios (e.g., tracing all functions during a busy
network transfer) may need a larger buffer to avoid overflow.

**Workaround:** Specify `BUFSZ <n>` when loading atrace. Minimum is 16
entries.

### Event Sequence Counter Wrap

The `event_sequence` field in the anchor is a 32-bit `ULONG` that
increments by one for each event. It wraps to zero after 4,294,967,295
events. The per-event `sequence` field inherits this value.

**Why:** A 32-bit counter is natural for 68k and fits in the anchor
structure without expansion.

**Impact:** At sustained rates of 10,000 events per second, wrap-around
takes approximately 5 days. In practice, trace sessions rarely last that
long. Wrap-around could cause brief ordering ambiguity in the daemon's
stale-event filtering for TRACE RUN (which compares sequence numbers
against `run_start_seq`).

## Stub Limits

### Cannot Unload Patches Without Reboot

Once `atrace_loader` installs stubs via `SetFunction()`, they cannot be
safely removed at runtime. The QUIT command frees the ring buffer and
removes the semaphore, but the stub code, patch descriptors, and anchor
structure remain allocated in `MEMF_PUBLIC` memory permanently.

**Why:** `SetFunction()` patches are global modifications to library
jump tables. After atrace patches a function, other code (other patch
utilities, system components) may chain additional `SetFunction()` calls
on top, storing atrace's stub address as their "original" to call
through. Removing atrace's stub would leave dangling pointers in those
chains.

**Impact:** After QUIT, stubs remain installed but operate as transparent
pass-throughs because `global_enable` is set to 0. Every traced function
incurs a small overhead (test `enabled`, test `global_enable`, branch to
original) even when tracing is fully disabled. This overhead is minimal
(a few 68k instructions) but is not zero.

**Workaround:** Reboot to fully remove all patches. Use
`amigactl reboot` to trigger a clean restart.

### No Re-Entrancy Tracking

The stubs do not detect or count re-entrant calls. If function A's stub
is executing and the traced function calls another traced function B
before A's stub completes, both events are recorded independently with
no indication of nesting.

**Why:** Re-entrancy tracking would require per-task state that persists
across stub invocations, adding complexity and memory overhead that is
not justified for the intended use cases.

**Impact:** The event stream is a flat sequence. Call depth and
caller-callee relationships must be inferred from timestamps, task
identity, and domain knowledge. For example, an `OpenLibrary` event
followed by a `Lock` event from the same task probably represents the
library's initialization code, but atrace does not make this explicit.

### Post-Call Memory Read Restriction (UAE JIT)

In the stub suffix (post-call handler, reached via trampoline RTS from
dynamically allocated `MEMF_PUBLIC` memory), data memory reads can cause
the UAE JIT compiler to freeze. Memory writes and register operations
work correctly. This affects only the post-call code path, not the
pre-call variable region.

**Why:** The UAE JIT has specific behavior when executing code reached
via RTS into dynamically allocated memory. The exact mechanism involves
JIT cache management for code that was not present when the JIT compiled
the calling code.

**Impact:** All memory-reading operations that need to dereference
pointers (e.g., reading `SysBase->ThisTask` for task name capture, or
following pointer chains for indirect name dereferencing) are performed
in the pre-call variable region where memory reads work correctly. The
post-call handler is limited to writing the return value and IoErr into
the event structure using values already in registers.

This constraint applies only to UAE emulation and does not affect real 68k hardware.

## Library and Function Table Limits

### Static Function Table (99 Functions, 7 Libraries)

The set of traceable functions is defined at compile time in
`atrace/funcs.c`. The current table contains 99 functions across 7
libraries:

| Library | Count |
|---|---|
| exec.library | 31 |
| dos.library | 26 |
| intuition.library | 14 |
| bsdsocket.library | 15 |
| graphics.library | 2 |
| icon.library | 5 |
| workbench.library | 6 |

Adding new functions requires modifying source code and recompiling.
There is no runtime mechanism to add trace points for arbitrary
functions.

**Why:** The stub generator needs compile-time metadata (LVO offset,
argument register assignments, string argument bitmask, indirect name
dereference type) to emit correct machine code for each function. This
metadata cannot be safely inferred at runtime.

**Impact:** Functions not in the table cannot be traced. Entire libraries
not in the table (e.g., `gadtools.library`, `layers.library`,
`commodities.library`) are invisible to atrace.

**Workaround:** Follow the procedure in
[adding-functions.md](adding-functions.md) to add new functions. This
requires cross-compilation and redeployment.

### Library ID Space (UBYTE, Max 255)

The `lib_id` field in events and patches is `UBYTE`, limiting the system
to 256 distinct libraries. The current implementation uses IDs 0-6 with
`#define` constants in `atrace.h`.

**Why:** A single byte is sufficient for the foreseeable number of
libraries and keeps the event structure compact.

**Impact:** Not a practical limitation. The 7 currently supported
libraries are far below the 256 limit.

### Missing Libraries Are Silently Skipped

When `atrace_loader` installs patches, it attempts to `OpenLibrary()`
each library in the table. Libraries that fail to open (e.g.,
`bsdsocket.library` when no TCP/IP stack is running) have their
patches skipped. The loader reports the count of installed vs. total
patches.

**Why:** Not all systems have all libraries available. A missing TCP/IP
stack should not prevent tracing of exec and dos functions.

**Impact:** The number of active patches may be less than the total 99.
Use `atrace_loader STATUS` or `amigactl trace status` to see which
patches are installed.

## Daemon Limits

### Maximum 8 Concurrent Clients

The daemon supports at most `MAX_CLIENTS` (8) simultaneous connections,
defined in `daemon/daemon.h`. This limit applies to all amigactld
connections, not just trace sessions. A TRACE START session, a file
transfer, and a REPL session each consume one client slot.

**Why:** The daemon uses a static array of client structures for
simplicity and deterministic memory usage on the Amiga.

**Impact:** If all 8 slots are occupied, new connections are refused.
Since each trace session requires a dedicated connection (streaming mode
occupies the connection for the duration), running multiple simultaneous
trace sessions reduces the slots available for other operations.

### Single Stub-Level Task Filter

The `filter_task` field in the anchor structure is a single global
`APTR`. Only one TRACE RUN session can use stub-level task filtering at
a time. If a second TRACE RUN starts while the first is active, the
second falls back to daemon-side filtering only.

**Why:** The stubs execute in interrupt-disabled context and compare
`SysBase->ThisTask` against this single field. Supporting multiple
filter tasks would require a list traversal under Disable, which is
unacceptable for latency.

**Impact:** The first TRACE RUN gets efficient stub-level filtering
(non-matching events never enter the ring buffer). Subsequent concurrent
TRACE RUN sessions receive daemon-side filtering only, meaning all
events from all tasks enter the ring buffer and are filtered when the
daemon reads them. This increases the risk of ring buffer overflow under
high event rates.

**Workaround:** Avoid running multiple TRACE RUN sessions simultaneously.
If necessary, increase the ring buffer size to absorb the additional
event volume.

### No Child Process Tracing in TRACE RUN

TRACE RUN's task filter compares `Task` pointers for exact equality. If
the traced program launches child processes (via `SystemTagList`,
`CreateProc`, or similar), those child processes have different `Task`
pointers and their events are not captured by the TRACE RUN session.

**Why:** There is no reliable, low-overhead way to determine at stub
execution time whether a given task is a descendant of the traced
process. AmigaOS does not maintain parent-child process trees.

**Impact:** Programs that delegate work to child processes will have
incomplete traces. Only calls made directly by the launched process
are captured.

**Workaround:** Use TRACE START with a `PROC=` filter instead of TRACE
RUN. The `PROC=` filter performs substring matching on task names, which
may catch child processes if they have similar names. However, this is
a daemon-side filter and does not provide stub-level filtering
efficiency.

See [trace-run.md](trace-run.md) for TRACE RUN details.

### Event Formatting Buffer (512 Bytes)

The daemon formats each event into a static 512-byte buffer
(`trace_line_buf[512]` in `daemon/trace.c`). Events whose formatted
representation would exceed this length are truncated.

**Why:** A static buffer avoids dynamic allocation in the event-
processing hot path.

**Impact:** Not a practical limitation under normal circumstances. A
fully populated verbose event with timestamps, 4 hex arguments, 63-char
string, 21-char task name, and IoErr fits well within 512 bytes.

### Task Name Cache (64 Entries)

The daemon maintains a cache of 64 task name entries
(`TASK_CACHE_SIZE`) plus a 32-entry history ring for exited tasks
(`TASK_HISTORY_SIZE`). The cache is refreshed by walking the system task
lists under `Forbid()` every 20 poll cycles (approximately 2 seconds).

**Why:** Resolving task names requires walking kernel task lists, which
must be done under `Forbid()`. Caching amortizes this cost.

**Impact:** Systems running more than 64 simultaneous tasks may have
some tasks resolve to their hex `Task` pointer (`0x00XXXXXX`) instead of
their name, if the task is not in the cache when its event is processed.
Events from tasks that have already exited may also show hex pointers if
the task exited before the cache was refreshed. The 32-entry history
ring mitigates this for recently exited tasks.

### Filter Name Limit (32 Per Filter)

Each client's trace filter supports at most `MAX_FILTER_NAMES` (32)
entries for library whitelist/blacklist and function whitelist/blacklist.

**Why:** Static arrays in the per-client filter structure keep memory
usage bounded and deterministic.

**Impact:** Filters specifying more than 32 individual function names
silently ignore the excess. This is unlikely to be a practical issue
since most filter expressions use a handful of names.

### Daemon Self-Event Filtering

The daemon silently filters all events where the originating task is its
own process (amigactld). Library calls made by the daemon itself --
including the dos.library and exec.library calls used to service client
requests -- never appear in trace output.

**Why:** Without self-filtering, every traced library call the daemon
makes while formatting and sending events would generate additional
events, creating a feedback loop. The daemon's own activity would
dominate the event stream and could cause ring buffer overflow.

**Impact:** Daemon activity is invisible in trace output. If
investigating daemon behavior itself (e.g., debugging amigactld file
operations), atrace cannot provide visibility. This filtering is
unconditional and cannot be disabled.

## EClock Timestamp Limits

### 48-Bit Timestamp Range

EClock timestamps are stored as a 32-bit low word (`eclock_lo`, offset
100) and a 16-bit high word (`eclock_hi`, offset 104), providing 48
bits of tick resolution.

At a typical Amiga EClock frequency of ~709,379 Hz, the 48-bit counter
wraps after approximately:

    2^48 / 709379 = ~396,749 seconds = ~4.6 days

**Why:** The full EClock is 64 bits (returned by `ReadEClock()` as two
32-bit values). Storing only 48 bits saves 2 bytes in the 128-byte event
structure. The high 16 bits of the upper 32-bit word are discarded.

**Impact:** Trace sessions lasting longer than ~4.6 days (at standard
NTSC EClock rates) would experience timestamp wrap-around and produce
incorrect elapsed-time calculations. PAL systems with ~715,909 Hz have a
slightly shorter range. In practice, this is not a meaningful constraint.

### Microsecond Precision, Not Sub-Microsecond

EClock timestamps are converted to wall-clock time with microsecond
precision. The conversion formula in `eclock_format_time()` computes
microseconds via:

    elapsed_us = (remainder * 1000) / (freq / 1000)

This yields microsecond granularity. The underlying EClock tick period is
approximately 1.41 microseconds (at ~709 kHz), so the true resolution is
one EClock tick, not one microsecond.

**Why:** Microsecond precision matches the AmigaOS `DateStamp` and
`timer.device` conventions and is sufficient for system call tracing.

**Impact:** Events occurring within the same EClock tick (1.41
microseconds apart) receive identical timestamps. This is rarely relevant
for system call tracing, where individual calls typically take tens to
thousands of microseconds.

### Wall-Clock Epoch from Session Start

The daemon captures a wall-clock epoch (via `DateStamp()`) when a trace
session starts. Per-event timestamps are computed as offsets from the
first EClock-bearing event added to this wall-clock base. The EClock
baseline is set lazily from the first event, not from an explicit
`ReadEClock()` call (the daemon does not have a timer.device unit open).

**Why:** The daemon cannot call `ReadEClock()` directly. Using the first
event's EClock value as the baseline introduces at most one poll cycle
(~100 ms) of offset from the `DateStamp()` epoch.

**Impact:** Timestamps are self-consistent within a session but may
differ from external wall-clock measurements by up to ~100 ms at the
session boundary. This offset is fixed for the session and does not
drift.

## Platform Limits

### Single-CPU Assumption

The ring buffer uses `Disable()`/`Enable()` (interrupt masking) for
mutual exclusion during slot reservation and overflow handling. This is
correct on single-CPU 68k systems, where disabling interrupts guarantees
atomicity.

**Why:** `Disable()`/`Enable()` is the standard AmigaOS mechanism for
short critical sections. It is faster than semaphore acquisition and
appropriate for the stub hot path.

**Impact:** atrace is not safe on hypothetical SMP AmigaOS systems.
Multi-core systems would require atomic compare-and-swap operations or
spinlocks for the ring buffer. This is not a practical concern for the
target platform (classic 68k Amiga and UAE emulation).

### 68k Only

The stub code generator emits raw 68k machine code. The stubs are not
portable to other architectures (PPC AmigaOS 4, AROS x86, MorphOS).

**Why:** The stub mechanism patches library jump tables with hand-crafted
68k assembly for minimum overhead. This is inherently architecture-
specific.

**Impact:** atrace works only on 68k AmigaOS (real hardware or emulated
via UAE). It cannot be used on AmigaOS 4, MorphOS, or AROS
without a complete rewrite of the stub generator.

### AmigaOS 2.0+ Required

atrace requires Kickstart 37 or later (AmigaOS 2.0). It uses
`AllocVec()`, named semaphores, and `timer.device` EClock functionality
that are not available on Kickstart 1.x.

### Libraries Must Be Openable

Patches are installed only for libraries that `OpenLibrary()` can
successfully open at load time. `bsdsocket.library` requires a running
TCP/IP stack; if no stack is active, all 15 bsdsocket function patches
are skipped.

**Workaround:** Start the TCP/IP stack before loading atrace. If atrace
is already loaded without bsdsocket patches, reboot and reload after
starting the stack.

## Memory Persistence

### Allocated Memory Persists Until Reboot

All atrace memory allocations use `MEMF_PUBLIC` and are never freed
(except the ring buffer on QUIT):

- **Anchor structure** (92 bytes): persists always.
- **Patch descriptors** (40 bytes x 99 = 3,960 bytes): persists always.
- **Stub code** (~300-500 bytes per patch, ~30-50 KB total): persists
  always.
- **Ring buffer** (default 1 MB): freed by QUIT, persists otherwise.
- **Semaphore name string**: persists always.

**Why:** Stubs and patch descriptors cannot be freed because they are
referenced by library jump tables and potentially by other `SetFunction`
chains. The anchor is the root of the structure graph and must remain
valid.

**Impact:** Loading atrace consumes approximately 1.04 MB permanently
(with default buffer size). QUIT recovers the 1 MB ring buffer but
leaves ~40 KB of overhead. This memory is only reclaimed by rebooting.

## Event Ordering

### Sequence vs. Wall-Clock Order

Events are assigned sequence numbers under `Disable()` during ring
buffer slot reservation. The sequence reflects the order in which stubs
reserved slots, not the order in which the traced functions returned.
Two events with adjacent sequence numbers may have overlapping execution
if they occurred in different tasks (one task was preempted between slot
reservation and function completion).

**Impact:** The sequence number provides a reliable total order of when
calls were initiated. The EClock timestamps provide a separate measure
of when the event was recorded (pre-call). For most analysis, sequence
order is the correct ordering to use.

### In-Progress Events (valid=2)

Stubs set `valid=2` before calling the original function and `valid=1`
after the function returns. The daemon's consumer may encounter events
with `valid=2` for blocking functions (e.g., `WaitSelect`, `Execute`,
`RunCommand`). After `INFLIGHT_PATIENCE` consecutive encounters (3
encounters, approximately 200 ms of waiting), the daemon consumes the event as-is with
a potentially stale return value of 0.

**Impact:** For long-blocking functions, the event may appear in the
output before the function has returned. The return value and IoErr
fields are unreliable for such events. The daemon suppresses IoErr
display for `valid=2` events to avoid showing misleading error codes.

See [ring-buffer.md](ring-buffer.md) for details on the valid flag
protocol.
