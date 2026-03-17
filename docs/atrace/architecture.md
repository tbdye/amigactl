# System Architecture

atrace is a library call tracing system for AmigaOS. It intercepts calls
to system library functions, records them into a shared ring buffer, and
streams them over TCP to a remote client for display and analysis. The
system comprises three cooperating components: an installer program on
the Amiga, the amigactld daemon on the Amiga, and a Python client on the
client machine.

This document describes how these components fit together, the shared
data structures that connect them, and the complete lifecycle of a trace
event from function call interception to client display.

## Component Overview

```
 Amiga (68k)                              Client (Python)
+------------------------------------------+  +------------------------+
|                                          |  |                        |
|  atrace_loader        amigactld          |  |  amigactl CLI /        |
|  (installer)          (daemon)           |  |  Python library        |
|       |                  |               |  |       |                |
|       v                  v               |  |       v                |
|  +---------+  +------------------+       |  |  +----------+          |
|  | Patches |  | trace.c module   |       |  |  | trace_   |          |
|  | (stubs) |  | - discovery      |       |  |  | start()  |          |
|  |         |  | - polling        |       |  |  | trace_   |          |
|  +---------+  | - formatting     |       |  |  | run()    |          |
|       |       | - filtering      |       |  |  | trace_   |          |
|       v       +--------+---------+       |  |  | ui.py    |          |
|  +---------+           |                 |  |  +-----+----+          |
|  |  Ring   |<- reads --|                 |  |        |               |
|  | Buffer  |           |                 |  |        |               |
|  +---------+      TCP/6800 --------------|--+--------+               |
|                                          |  |                        |
|  Named Semaphore                         |  |                        |
|  "atrace_patches"                        |  |                        |
|  (IPC discovery)                         |  |                        |
+------------------------------------------+  +------------------------+
```

### atrace_loader (Installer Program)

The installer program (`C:atrace_loader`) runs once to set up the
tracing infrastructure, then exits. It is not an AmigaOS resident module
and does not use `AddResident()`. All memory it allocates uses
`MEMF_PUBLIC`, which persists after the loader process terminates.

The loader:

1. Allocates the anchor structure (104 bytes).
2. Allocates the ring buffer (16-byte header + 128 bytes per entry).
3. Allocates the patch descriptor array (40 bytes per patch).
4. Opens `timer.device` and reads the EClock frequency.
5. For each supported library, opens the library, generates a stub for
   each traced function, and installs it via `SetFunction()`.
6. Auto-disables non-Basic tier functions (Detail, Verbose, Manual tiers
   are disabled by default).
7. Registers the anchor as a named semaphore (`AddSemaphore`), making it
   discoverable by the daemon.

After installation, the loader accepts reconfiguration commands (`STATUS`,
`ENABLE`, `DISABLE`, `QUIT`) when run again; it locates the existing
installation via `FindSemaphore()`.

ReadArgs template: `BUFSZ/K/N,DISABLE/S,STATUS/S,ENABLE/S,QUIT/S,FUNCS/M`

Source: `atrace/main.c`

### amigactld (Daemon)

The amigactld daemon integrates trace functionality through its `trace.c`
module. The daemon:

- Discovers the atrace installation via `FindSemaphore("atrace_patches")`
  at startup and lazily on each `TRACE START` command.
- Auto-loads `C:atrace_loader` via `SystemTags()` if the semaphore is not
  found when a client requests tracing.
- Polls the ring buffer on each event loop iteration when any client has
  an active trace session.
- Formats raw events into tab-separated text lines with function names,
  argument formatting, return value classification, and timestamps.
- Applies per-client server-side filters (library, function, process name,
  errors-only).
- Streams formatted events to connected clients as DATA chunks over the
  amigactl wire protocol.
- Supports up to 8 concurrent clients (`MAX_CLIENTS`), each with
  independent filter state.

Source: `daemon/trace.c`, `daemon/trace.h`

### Python Client

The Python client library (`amigactl` package) provides both
programmatic and interactive access to trace streams:

- `AmigaCtl.trace_start()` opens a streaming connection and invokes a
  callback for each parsed event.
- `AmigaCtl.trace_run()` launches a program on the Amiga and traces only
  its library calls, auto-terminating when the process exits.
- `AmigaCtl.stop_trace()` sends `STOP` and drains remaining events.
- `TraceViewer` (in `trace_ui.py`) provides an interactive terminal UI
  with pause, scrollback, function toggle grid, tier switching, search,
  and statistics -- all rendered with ANSI escape sequences (no curses
  dependency).

Source: `client/amigactl/__init__.py`, `client/amigactl/trace_ui.py`

## Shared Data Structures

All shared state between the installer program and the daemon resides in
`MEMF_PUBLIC` memory on the Amiga. The structures are defined in
`atrace/atrace.h`.

### Anchor Structure (`struct atrace_anchor`, 104 bytes)

The anchor is the top-level structure, discoverable via
`FindSemaphore("atrace_patches")`. It embeds a `SignalSemaphore` as its
first member, so it can be cast directly from the semaphore pointer.

| Field | Offset | Size | Description |
|-----------------|--------|------|-----------------------------------------------|
| `sem` | 0 | 46 | `SignalSemaphore` (system list node) |
| `sem_padding` | 46 | 2 | Alignment to 4-byte boundary |
| `magic` | 48 | 4 | `0x41545243` (`'ATRC'`) -- validation tag |
| `version` | 52 | 2 | Protocol version |
| `flags` | 54 | 2 | Reserved |
| `global_enable` | 56 | 4 | `0` = tracing disabled, `1` = active |
| `ring` | 60 | 4 | Pointer to ring buffer |
| `patch_count` | 64 | 2 | Total number of patches |
| `padding1` | 66 | 2 | Alignment |
| `patches` | 68 | 4 | Pointer to patch descriptor array |
| `event_sequence`| 72 | 4 | Monotonically increasing event counter |
| `events_consumed`| 76 | 4 | Consumer progress counter |
| `filter_task` | 80 | 4 | Task pointer for TRACE RUN filtering (`NULL` = all tasks) |
| `eclock_freq` | 84 | 4 | EClock frequency in Hz (from `ReadEClock`) |
| `timer_base` | 88 | 4 | `timer.device` base pointer (for stub EClock calls) |
| `bsd_table` | 92 | 4 | Pointer to BSD per-opener patching table (`NULL` if no bsdsocket patches) |
| `bsd_table_count`| 96 | 2 | Number of entries in BSD patching table |
| `padding2` | 98 | 2 | Alignment |
| `daemon_task` | 100 | 4 | Daemon task pointer for stub-level self-filtering (`NULL` = no exclusion) |

The `global_enable`, `filter_task`, and `daemon_task` fields are declared
`volatile` and are read by stub code running in interrupt-like contexts (under
`Disable()`/`Enable()` in the stub fast path).

### Ring Buffer (`struct atrace_ringbuf`, 16-byte header)

The ring buffer is a single contiguous allocation: a 16-byte header
followed by an array of 128-byte event entries.

| Field | Offset | Size | Description |
|------------|--------|------|----------------------------------------------|
| `capacity` | 0 | 4 | Number of event slots |
| `write_pos`| 4 | 4 | Next write slot index (producer) |
| `read_pos` | 8 | 4 | Next read slot index (consumer) |
| `overflow` | 12 | 4 | Count of events dropped due to buffer full |

Total allocation size: `16 + 128 * capacity` bytes. The default capacity
is 8192 entries (1 MB total), configurable via the `BUFSZ` argument to
`atrace_loader`.

The ring buffer uses a single-producer (stubs under `Disable()`) /
single-consumer (daemon polling) model. The `write_pos` and `read_pos`
fields are `volatile`. Slot reservation and `write_pos` advancement
happen atomically under `Disable()`/`Enable()`, so concurrent stubs from
different tasks serialize correctly.

See [Ring Buffer](ring-buffer.md) for detailed operational semantics.

### Event Entry (`struct atrace_event`, 128 bytes)

Each event occupies exactly 128 bytes, enabling shift-based indexing
(`slot << 7`) for fast address computation in 68k assembly stubs.

| Field | Offset | Size | Description |
|--------------|--------|------|-----------------------------------------------|
| `valid` | 0 | 1 | `0` = empty, `2` = in-progress, `1` = complete |
| `lib_id` | 1 | 1 | Library identifier (0-6) |
| `lvo_offset` | 2 | 2 | Library vector offset (negative) |
| `sequence` | 4 | 4 | Global sequence number |
| `caller_task`| 8 | 4 | `FindTask(NULL)` pointer of calling task |
| `args[4]` | 12 | 16 | Up to 4 captured arguments |
| `retval` | 28 | 4 | Return value (set by post-call handler) |
| `arg_count` | 32 | 1 | Number of valid entries in `args[]` |
| `flags` | 33 | 1 | `FLAG_HAS_ECLOCK` (0x01), `FLAG_HAS_IOERR` (0x02) |
| `string_data`| 34 | 64 | Captured string argument or indirect name |
| `ioerr` | 98 | 1 | Low byte of DOS `IoErr()` result (if `FLAG_HAS_IOERR` set) |
| `bsd_flag` | 99 | 1 | bsdsocket detection flag (set by OpenLibrary stub suffix) |
| `eclock_lo` | 100 | 4 | Low 32 bits of EClock timestamp |
| `eclock_hi` | 104 | 2 | Low 16 bits of EClock high word |
| `task_name` | 106 | 22 | Calling task's `tc_Node.ln_Name` (21 chars + NUL) |

See [Event Format](event-format.md) for the complete field layout and
encoding details.

### Patch Descriptor (`struct atrace_patch`, 40 bytes)

Each traced function has a patch descriptor that holds its metadata and
runtime state.

| Field | Offset | Size | Description |
|------------------|--------|------|-----------------------------------------------|
| `lib_id` | 0 | 1 | Library identifier |
| `padding0` | 1 | 1 | Alignment |
| `lvo_offset` | 2 | 2 | Library vector offset |
| `func_id` | 4 | 2 | Function index within its library |
| `arg_count` | 6 | 2 | Number of register arguments |
| `enabled` | 8 | 4 | Per-function enable flag (`volatile`) |
| `use_count` | 12 | 4 | Active stub execution count (`volatile`) |
| `original` | 16 | 4 | Original function pointer (pre-patch) |
| `stub_code` | 20 | 4 | Pointer to generated stub code |
| `stub_size` | 24 | 4 | Stub allocation size in bytes |
| `arg_regs[8]` | 28 | 8 | Register indices for arguments |
| `string_args` | 36 | 1 | Bitmask of arguments that are C strings |
| `name_deref_type`| 37 | 1 | Indirect name capture type (see below) |
| `skip_null_arg` | 38 | 1 | Register for NULL-argument filtering |
| `padding_end` | 39 | 1 | Alignment |

The `enabled` and `use_count` fields are `volatile`. Stubs check
`enabled` on every invocation and increment/decrement `use_count` around
the critical section. The `QUIT` sequence waits for all `use_count`
values to drain to zero before deallocating the ring buffer.

### Function and Library Metadata

The `struct func_info` (20 bytes) and `struct lib_info` (12 bytes)
tables in `atrace/funcs.c` define the 99 traced functions across 7
libraries:

| Library | lib_id | Function Count |
|--------------------|--------|----------------|
| `exec.library` | 0 | 31 |
| `dos.library` | 1 | 26 |
| `intuition.library` | 2 | 14 |
| `bsdsocket.library` | 3 | 15 |
| `graphics.library` | 4 | 2 |
| `icon.library` | 5 | 5 |
| `workbench.library` | 6 | 6 |

The daemon maintains a parallel `func_table[]` (in `daemon/trace.c`)
with the same function ordering. This table adds error classification
(`ERR_CHECK_*` constants) and return value formatting (`RET_*` constants)
that are daemon-specific concerns.

## Stub Code Generation

Each traced function gets a dynamically generated 68k machine code stub,
allocated in `MEMF_PUBLIC` memory and installed via `SetFunction()` to
replace the original library vector entry.

A stub has three regions:

1. **Prefix** (216 bytes standard, 224 bytes with NULL-argument filter):
   Fast-path checks (`enabled`, `global_enable`, `filter_task`,
   `daemon_task`), optional NULL-argument short-circuit, register save
   (`MOVEM d0-d7/a0-a4/a6`),
   ring buffer slot reservation under `Disable()`/`Enable()`, EClock
   timestamp capture via `ReadEClock()`, and event header population
   (`lib_id`, `lvo_offset`, `sequence`, `caller_task`, `eclock_lo`,
   `eclock_hi`).

2. **Variable region** (size varies per function): Argument copy
   instructions (reading from the MOVEM frame on the stack into the event
   entry), `arg_count` immediate, `flags` write, and optional string
   capture code. String capture handles direct C string arguments
   (`string_args` bitmask), indirect name dereference (11 types including
   `ln_Name`, IORequest device names, TextAttr font names, Lock volume
   names, Window/Screen titles, and `sockaddr_in` capture for bsdsocket
   calls), and CLI command name extraction
   (which also writes the resolved name into `task_name`). Ends by
   setting `valid` to 2 (in-progress).

3. **Suffix** (126 bytes standard, 196 bytes for OpenLibrary, 152 bytes
   for accept): MOVEM restore, trampoline to the original function
   (pushes original address and executes `RTS`), post-call handler that
   writes `retval` and optionally calls `IoErr()` for `dos.library`
   functions with zero return values, sets `valid` to 1, decrements
   `use_count`, and returns to the original caller. Also contains the
   disabled fast path (transparent tail-call to the original) and the
   overflow path (increments the overflow counter and tail-calls the
   original). The OpenLibrary suffix includes a 70-byte bsdsocket
   per-opener patching block that calls `SetFunction` for each
   bsdsocket LVO on a newly opened `bsdsocket.library` base. The
   accept suffix includes a 26-byte `sockaddr_in` capture block.

Total stub size = prefix + variable region + suffix, rounded up to a
4-byte (ULONG) boundary.

See [68k Assembly Stub Generation](stub-mechanism.md) for
instruction-level detail.

## IPC: Named Semaphore Discovery

The installer program and the daemon communicate through a named Exec
semaphore. After completing all allocations and patch installations, the
loader calls `AddSemaphore()` to register the anchor under the name
`"atrace_patches"`.

The daemon locates the anchor via `FindSemaphore("atrace_patches")`:

- At daemon startup (`trace_init()`).
- Lazily on each `TRACE START` or `TRACE RUN` command
  (`trace_discover()`).
- After auto-loading `C:atrace_loader` (`trace_auto_load()`).

After locating the semaphore, the daemon validates the `magic` field
(`0x41545243`) to confirm it is a genuine atrace anchor rather than an
unrelated semaphore with the same name.

The semaphore also serves as a coordination mechanism during shutdown:

- `trace_poll_events()` obtains the semaphore shared before reading the
  ring buffer, and releases it after each polling batch.
- The `QUIT` command obtains the semaphore exclusively before removing it
  from the system list and freeing the ring buffer, blocking until the
  daemon releases its shared hold.

## Daemon Integration

### Auto-Loading

When a client sends `TRACE START` or `TRACE RUN` and the atrace
semaphore has not been found, the daemon attempts auto-loading:

1. Call `trace_discover()` -- if the semaphore exists, done.
2. Execute `C:atrace_loader` synchronously via `SystemTags()` with
   output redirected to `NIL:`.
3. Call `trace_discover()` again to pick up the newly registered
   semaphore.

If auto-loading fails, the daemon returns an error to the client.

### Event Polling

`trace_poll_events()` is called once per event loop iteration when any
client has `trace.active == 1`. Each call:

1. Obtains the anchor semaphore shared (`AttemptSemaphoreShared`). If the
   semaphore cannot be obtained and `global_enable` is 0, atrace is
   shutting down -- all trace sessions are terminated.
2. Reads up to 512 events per batch from the ring buffer starting at
   `read_pos`.
3. For each event:
   - Waits for in-progress events (`valid == 2`) to complete
     (`valid == 1`). After 3 consecutive polls at the same position
     (`INFLIGHT_PATIENCE`), the event is consumed as-is to prevent
     stalls from blocking functions.
   - Filters out the daemon's own library calls (by comparing
     `caller_task` against the daemon's task pointer).
   - At Basic tier, suppresses successful `OpenLibrary` calls with
     version 0 and successful `Lock` calls (high-volume noise from
     normal OS operation).
   - Resolves the calling task's name via a task cache (refreshed every
     ~20 poll cycles by walking the system task lists under `Forbid()`).
     Falls back to the event's embedded `task_name` for exited processes.
   - Formats the event into a tab-separated text line.
   - Broadcasts to all tracing clients that pass the per-client filter.
   - Clears `valid` to release the slot, advances `read_pos`.
4. Reports any ring buffer overflow as a `# OVERFLOW` comment to all
   tracing clients.
5. Releases the anchor semaphore.

### Task Name Resolution

The daemon maintains two caches for mapping task pointers to names:

- **Task cache** (64 entries): Rebuilt periodically by walking
  `ExecBase->TaskReady` and `ExecBase->TaskWait` under `Forbid()`. For
  CLI processes (`pr_TaskNum > 0`), extracts the command basename from
  `cli_CommandName` (e.g., `"[3] atrace_test"` instead of
  `"Background CLI"`).
- **Task history** (32-entry ring): Records previously resolved names so
  that events from short-lived processes that have already exited can
  still be identified and matched by `PROC=` filters.

### Value Caches

The daemon builds several value caches during a trace session to enrich
events with contextual information:

- **Lock cache**: Maps lock BPTR values to path strings (populated from
  `Lock` and `CreateDir` events). Used to annotate `CurrentDir` events
  with the directory path instead of a raw BPTR.
- **File handle cache**: Maps file handle BPTR values to filenames
  (populated from `Open` events). Used to annotate `Close`, `Read`,
  `Write`, and `Seek` events.
- **DiskObject cache**: Maps DiskObject pointers to filenames (populated
  from `GetDiskObject` events). Used to annotate `FreeDiskObject`.
- **Segment cache**: Maps segment list values to filenames (populated
  from `LoadSeg` and `NewLoadSeg` events). Used to annotate `UnLoadSeg`
  and `RunCommand`.

These caches are cleared at the start of each trace session.

## Wire Protocol (Trace Streaming)

Trace events are transmitted over the amigactl TCP protocol (default port
6800) using the DATA/END streaming pattern.

### Session Lifecycle

```
Client                          Daemon
  |                               |
  |  TRACE START [filters]  --->  |
  |                               |  (auto-load atrace if needed)
  |                               |  (drain stale events)
  |                               |  (capture EClock epoch)
  |  <--- OK                      |
  |  <--- DATA <len>\n<header>    |  (header comment with filter info)
  |  <--- DATA <len>\n<event>     |  (per-event, ongoing)
  |  <--- DATA <len>\n<event>     |
  |  ...                          |
  |  STOP  --->                   |  (client requests end)
  |  <--- DATA <len>\n<event>     |  (remaining buffered events)
  |  <--- END                     |
  |  <--- .                       |  (sentinel -- back to command mode)
  |                               |
```

For `TRACE RUN`, the flow is similar but the stream auto-terminates when
the launched process exits. The daemon sends a
`# PROCESS EXITED rc=<N>` comment followed by `END` and the sentinel.

### Event Payload Format

Each trace event is transmitted as a single `DATA` chunk containing a
tab-separated line with 7 fields:

```
<seq>\t<time>\t<lib.func>\t<task>\t<args>\t<retval>\t<status>
```

| Field | Example | Description |
|---------|-------------------------------|----------------------------------------|
| seq | `42` | Global sequence number |
| time | `14:23:07.123456` | Timestamp (EClock-derived or DateStamp) |
| lib.func| `dos.Open` | Library and function name |
| task | `[3] List` | Task identifier |
| args | `"SYS:C", MODE_OLDFILE` | Formatted arguments |
| retval | `0x001A2B3C` | Formatted return value |
| status | `O` | `O` = ok, `E` = error, `-` = neutral |

Comments (overflow notifications, session metadata, process exit
notifications) use `DATA` chunks with payloads beginning with `#`.

### Server-Side Filters

Filters are specified as space-separated tokens after `TRACE START`:

- `LIB=<name>` -- restrict to a specific library (e.g., `LIB=dos`)
- `FUNC=<name>` -- restrict to a specific function or comma-separated
  list (e.g., `FUNC=Open,Close`)
- `PROC=<name>` -- restrict to tasks whose name contains the substring
  (case-insensitive)
- `ERRORS` -- only pass events with error return values

For `TRACE RUN`, a `PROC=` filter is not accepted because process
filtering is automatic (the daemon matches on the exact task pointer of
the launched process).

The daemon also supports mid-stream filter changes via the `FILTER`
command and output tier switching via `TRACE TIER <1|2|3>`.

See [Filtering](filtering.md) for complete filter reference.

## Data Flow: Event Lifecycle

The complete path of a trace event from function call to client display:

```
1. Application          2. Library Jump Table    3. Stub Code
   calls Open()  --->      LVO redirected  --->     [prefix]
                           by SetFunction           check enabled
                                                    check global_enable
                                                    check filter_task
                                                    MOVEM save registers
                                                    Disable()
                                                    reserve ring slot
                                                    increment use_count
                                                    Enable()
                                                    ReadEClock -> eclock_lo/hi
                                                    write lib_id, lvo, seq,
                                                      caller_task, task_name
                                                    [variable]
                                                    copy args from MOVEM frame
                                                    capture string arguments
                                                    set valid=2 (in-progress)
                                                    [suffix]
                                                    MOVEM restore
                                                    push original address
                                                    RTS (trampoline)

4. Original Function    5. Stub Post-Call        6. Daemon
   executes    --->        write retval  --->       poll ring buffer
   returns via             (IoErr if dos+fail)      resolve task name
   trampoline RTS          set valid=1              format event line
                           decrement use_count      apply per-client filters
                           return to caller         send DATA chunk

7. Python Client
   parse tab-separated fields
   apply client-side filters (TUI noise suppression, search)
   render in terminal (color-coded, scrollable)
```

### Valid Flag State Machine

The `valid` field in each event entry drives the producer-consumer
protocol:

```
        0 (empty)
            |
   stub pre-call sets valid=2
            |
            v
        2 (in-progress)
            |
   stub post-call sets valid=1
            |
            v
        1 (complete)
            |
   daemon clears valid=0
            |
            v
        0 (empty, reusable)
```

The `valid=2` state exists to handle blocking functions (e.g.,
`RunCommand`, `Execute`, `WaitSelect`). Without it, the daemon would
stall the ring buffer waiting for a function that might block for
seconds. After `INFLIGHT_PATIENCE` (3) consecutive polls, the daemon
consumes `valid=2` events as-is, accepting incomplete return value data.

## Output Tiers

Functions are organized into four progressive verbosity tiers. At install
time, only Basic-tier functions are enabled; the others are auto-disabled.

| Tier | Functions | Characteristic |
|----------|-----------|---------------------------------------------|
| Basic | 57 | Core operations: Open, Lock, OpenLibrary... |
| Detail | 13 | Deeper debugging: AllocSignal, UnLock, Seek...|
| Verbose | 3 | High-volume burst: ExNext, OpenFont, CloseFont|
| Manual | 26 | Extreme rate: AllocMem, GetMsg, Wait, recv...|

Tier switching is available via the `TRACE TIER` command (daemon-side) or
interactively via the `1`/`2`/`3` keys in the TUI viewer. The tier level
also controls daemon-side content filtering (e.g., successful
`OpenLibrary` version-0 calls are suppressed at Basic tier).

See [Output Tiers](output-tiers.md) for tier membership details.

## Limitations

- **No uninstall without reboot.** `QUIT` disables tracing and frees the
  ring buffer, but stub code and patch descriptors remain allocated in
  `MEMF_PUBLIC` memory. The library jump table entries still point to
  stubs, which act as transparent pass-throughs when `global_enable` is
  0. Restoring the original jump table entries is unsafe because other
  SetFunction patches may have chained on top of atrace. A reboot is
  required for complete removal.

- **Maximum 4 captured arguments.** The event entry has space for 4
  argument values. Functions with more arguments (e.g., `sendto` with 6,
  `AddAppIconA` with 7) have their full `arg_count` recorded but only
  the first 4 register values are captured.

- **Single consumer.** The ring buffer supports one consumer (the
  daemon). Multiple clients receive the same events via the daemon's
  broadcast, but the ring buffer itself has a single `read_pos`.

- **EClock timestamp resolution.** Timestamps use the Amiga's EClock
  (typically 709,379 Hz on PAL systems). The 48-bit EClock value
  (32-bit `eclock_lo` + 16-bit `eclock_hi`) wraps approximately every
  4.4 days at this frequency. Trace sessions spanning longer periods may
  exhibit timestamp discontinuities.

- **Task name length.** The embedded `task_name` field is 22 bytes
  (21 characters + NUL). Longer task names are truncated.

- **String data length.** The `string_data` field is 64 bytes
  (63 characters + NUL). Longer strings are truncated. The stub copies
  strings byte-by-byte with a length limit.

- **Ring buffer overflow.** When the ring buffer is full and a new event
  arrives, the event is dropped and the `overflow` counter is
  incremented. The daemon reports overflow to clients as a comment. No
  backpressure mechanism exists; the stub simply drops the event and
  calls the original function normally.

- **IoErr truncation.** The `ioerr` field stores only the low byte (UBYTE) of the 32-bit `IoErr()` return value. Standard AmigaOS error codes (103--233) fit within this range, but values above 255 would be truncated.
