# Troubleshooting

This document covers common problems encountered when using atrace, organized
by symptom. Each section describes the symptom, possible causes, diagnostic
steps, and solutions.


## Diagnostic Tools

Before diving into specific symptoms, familiarize yourself with the two
primary diagnostic tools.

### trace status (CLI)

The `amigactl trace status` command queries atrace state remotely from the
host machine:

```
$ amigactl --host 192.168.6.228 trace status
loaded=1
enabled=1
patches=99
events_produced=48271
events_consumed=48271
events_dropped=0
events_self_filtered=0
buffer_capacity=8192
buffer_used=0
poll_count=312
```

Key fields:

| Field | Meaning |
|-------|---------|
| `loaded` | Whether atrace_loader is resident in Amiga memory. `0` means no patches are installed. |
| `enabled` | Global tracing switch. `0` means all stubs take the disabled fast path. |
| `patches` | Total number of installed function patches (99 at full installation). |
| `events_produced` | Cumulative events written to the ring buffer by stubs. |
| `events_consumed` | Cumulative events read from the ring buffer by the daemon. |
| `events_dropped` | Cumulative events lost to ring buffer overflow. |
| `events_self_filtered` | Cumulative events filtered by daemon-side self-filtering and content-based suppression. |
| `buffer_capacity` | Ring buffer size in entries (default: 8192). |
| `buffer_used` | Number of entries currently occupied in the ring buffer. |
| `poll_count` | Number of polling cycles performed by the daemon consumer. |
| `ioerr_capture` | Whether IoErr capture is active (1 when anchor version >= 4). |

When atrace is loaded, additional fields appear: `noise_disabled` (count of
non-Basic tier functions currently disabled), `anchor_version` (currently 4),
`eclock_freq` (EClock frequency in Hz, e.g., 709379 for PAL systems),
`filter_task` (hex address of the stub-level task filter pointer, or
`0x00000000` when no TRACE RUN is active), `poll_count` (daemon consumer
polling cycles), and `ioerr_capture` (whether IoErr capture is active,
emitted when anchor version >= 4).

Per-patch status lines (`patch_N=lib.func enabled=0/1`) show which individual
functions are enabled or disabled.

See [cli-reference.md](cli-reference.md) for full command syntax.

### atrace_loader STATUS (Amiga CLI)

On the Amiga itself, run:

```
C:atrace_loader STATUS
```

This prints the same information directly to the Amiga console, including
per-patch enabled/disabled state. Useful when the network connection itself
is the problem being diagnosed.

If atrace is not loaded, this prints `atrace is not loaded.` and exits with
a warning return code.


## atrace Not Loaded

**Symptom:** `amigactl trace status` reports `loaded=0`, or `trace start`
fails with `atrace not loaded`.

**Cause:** The atrace_loader binary has not been run, and auto-loading
failed.

**Diagnostic steps:**

1. Check that `C:atrace_loader` exists on the Amiga. The daemon auto-loads
   it by executing `C:atrace_loader` via `SystemTags()`. If the binary is
   not at that path, auto-loading fails silently from the client's
   perspective (the daemon console prints `Auto-load: C:atrace_loader
   returned <rc>`).

2. Check the daemon console output. If auto-loading was attempted, you will
   see one of:
   - `Auto-loading C:atrace_loader` followed by success (discovery of the
     semaphore).
   - `Auto-load: C:atrace_loader returned <rc>` -- the loader exited with
     a non-zero return code. Common causes: insufficient memory for the ring
     buffer, or `timer.device` could not be opened.
   - `Auto-load: cannot open NIL:` -- an unusual system-level failure.

3. Try loading manually from the Amiga CLI:
   ```
   C:atrace_loader
   ```
   This prints detailed output including which libraries were patched and
   any errors encountered.

**Solutions:**

- Deploy atrace_loader to `C:` on the Amiga:
  ```
  $ amigactl put build/atrace_loader C:atrace_loader
  ```
- If manual loading reports memory allocation failures, free memory on the
  Amiga or reduce the buffer size: `C:atrace_loader BUFSZ 2048`.
- If `timer.device` fails to open, this indicates a fundamental system
  problem -- `timer.device` is a core OS component present on every
  AmigaOS 2.0+ system.


## No Events Appearing

**Symptom:** `trace start` or `trace run` connects successfully but no
events are displayed, even though activity is happening on the Amiga.

**Possible causes and solutions:**

### Global tracing is disabled

Check `trace status` -- if `enabled=0`, tracing is globally disabled.
All stubs take the disabled fast path and produce no events.

Fix from the host:
```
$ amigactl trace enable
```

Or from the Amiga CLI:
```
C:atrace_loader ENABLE
```

### All relevant patches are disabled

Even with global tracing enabled, individual functions can be disabled.
The default installation auto-disables 42 non-Basic tier functions (Detail,
Verbose, and Manual tiers). If the function you are interested in is in one
of these tiers, it will not produce events.

Check per-patch status:
```
$ amigactl trace status
```

Look for `patch_N=lib.func enabled=0` lines. Enable specific functions:
```
$ amigactl trace enable AllocMem FreeMem
```

Or use a higher output tier when starting a trace session. In the
interactive viewer (via `amigactl shell`), press `2` for Detail tier or
`3` for Verbose tier. From the CLI:
```
$ amigactl trace start --detail
$ amigactl trace start --verbose
```

See [output-tiers.md](output-tiers.md) for tier membership details.

### Filters are too restrictive

Server-side filters (`--lib`, `--func`, `--proc`, `--errors`) suppress
events before they reach the client. A `--lib dos` filter hides all
exec.library, intuition.library, and other non-DOS events. A `--func Open`
filter shows only `Open` calls.

Try removing all filters to confirm events are flowing:
```
$ amigactl trace start
```

If events appear without filters but not with them, adjust the filter
parameters. See [filtering.md](filtering.md) for filter syntax details.

### TRACE RUN task filter is too narrow

`trace run` applies a task filter that only shows events from the
launched process. Child processes spawned by the traced program have
different Task pointers and are **not** included. If the program you
are tracing delegates its work to child processes, those calls are
invisible.

Workaround: use `trace start --proc <name>` with a substring match
that catches both the parent and child process names.

### The target library is not installed

If a library is not present when atrace_loader runs, patches for that
library are skipped entirely. For example, if `bsdsocket.library` is
not available (no TCP/IP stack running), all bsdsocket function patches
are skipped. The loader prints `Cannot open bsdsocket.library -- skipping
N patches` during installation.

Fix: ensure the required library/TCP stack is loaded before running
`atrace_loader`. If atrace was auto-loaded before the library was
available, use `atrace_loader QUIT` followed by a manual reload after
the library is available (or reboot).


## Events Missing or Gaps

**Symptom:** Some events appear but the sequence numbers have gaps, or
certain function calls are known to have occurred but do not appear in
the output.

**Possible causes:**

### Ring buffer overflow

When events are produced faster than the daemon can consume them, the
ring buffer overflows. Overflowed events are lost permanently. The
overflow is reported in the trace stream as a comment line:

```
# OVERFLOW 147 events dropped
```

The `events_dropped` field in `trace status` shows the cumulative count.

See the [Buffer Overflow](#buffer-overflow) section below for detailed
remediation.

### Non-Basic tier functions not enabled

Functions in the Detail, Verbose, or Manual tiers are disabled by
default. If you expect to see `AllocMem`/`FreeMem` calls but they are
in the Manual tier, you must enable them explicitly. See
[output-tiers.md](output-tiers.md) for tier membership.

### Content-based filtering at Basic tier

At the Basic output tier, the daemon applies content-based suppression
rules. For example, `OpenLibrary` calls that return version 0 (probing
calls) may be suppressed. Switching to Detail or Verbose tier disables
these suppression rules.

### In-flight event patience

Events with `valid=2` (the function has been entered but has not yet
returned) are given up to 3 consecutive encounters (~200ms at the default
100ms poll rate) before being consumed as-is. If a function blocks for
exactly this duration, the event may appear with incomplete return
value information (retval=0, no IoErr). This is by design -- it
prevents blocking functions (e.g., `WaitSelect`) from stalling the
entire event stream.


## Buffer Overflow

**Symptom:** The trace stream contains `# OVERFLOW N events dropped`
comments, or `trace status` shows a non-zero `events_dropped` value.

**Cause:** Events are being produced faster than the daemon can format,
filter, and send them to the client over the network. The ring buffer
is a fixed-size circular buffer (default 8192 entries, each 128 bytes =
1 MB). When the producer (stubs) catches up to the consumer (daemon),
new events increment the overflow counter and are discarded.

**Diagnostic steps:**

1. Check `trace status` for `events_dropped` and `buffer_used`. If
   `buffer_used` is consistently near `buffer_capacity`, the consumer
   cannot keep up.

2. Check which functions are enabled. Manual-tier functions like
   `AllocMem`, `FreeMem`, `GetMsg`, `PutMsg`, `Wait`, and `Signal`
   fire at extremely high rates (thousands per second) and can
   overwhelm the buffer in under a second.

**Solutions (from least to most disruptive):**

1. **Use task filtering.** `trace run` applies stub-level task filtering
   (`filter_task` in the anchor struct), which prevents non-target
   processes from writing to the ring buffer at all. This is the most
   effective single mitigation. See [trace-run.md](trace-run.md).

2. **Disable high-frequency functions.** If you do not need Manual-tier
   functions, ensure they remain disabled (the default). If you
   explicitly enabled them, disable them:
   ```
   $ amigactl trace disable AllocMem FreeMem AllocVec FreeVec
   ```

3. **Use server-side filters.** `--lib`, `--func`, and `--errors`
   filters reduce the number of events the daemon must format and
   transmit, though they do not prevent stubs from writing to the ring
   buffer.

4. **Increase buffer size.** Reload atrace_loader with a larger buffer:
   ```
   C:atrace_loader QUIT
   C:atrace_loader BUFSZ 32768
   ```
   The buffer must have at least 16 entries. Each entry is 128 bytes,
   so `BUFSZ 32768` allocates 4 MB. The default of 8192 entries (1 MB)
   is sufficient for most use cases.

See [ring-buffer.md](ring-buffer.md) and [performance.md](performance.md)
for deeper analysis.


## Connection Errors

### Cannot connect to the daemon

**Symptom:**
```
Error: could not connect to 192.168.6.228:6800
```

**Causes and solutions:**

- **amigactld is not running.** Start it on the Amiga: `Run >NIL: C:amigactld`.
- **Wrong IP address or port.** Verify with `--host` and `--port` flags, or
  check `amigactl.conf`. The default port is 6800.
- **Network connectivity.** Ping the Amiga from the host. Check that the
  Amiga's TCP/IP stack (e.g., Roadshow) is running and the network interface
  is configured.
- **Firewall or routing.** Ensure port 6800/TCP is not blocked between the
  host and the Amiga.

### Connection drops during trace session

**Symptom:** The trace stream stops abruptly, possibly with a broken pipe
or connection reset error.

**Causes:**

- **Network instability.** TCP connections over emulated Amiga networking
  (e.g., Amiberry's TAP bridge) can be fragile under high load.
- **Daemon restart or crash.** If amigactld exits or crashes, all active
  connections are dropped. Check the Amiga console for error messages.
- **Client timeout.** The Python client sets the socket to blocking mode
  (no timeout) during trace streaming, so idle timeouts should not occur.
  However, if the daemon stops sending events for an extended period and
  the OS-level TCP keepalive expires, the connection may drop.

**Recovery:** Simply restart the trace session. The daemon drains stale
events from the ring buffer at the start of each new session, so no manual
cleanup is needed.


## TRACE RUN Failures

### "atrace not loaded" after auto-load attempt

**Symptom:** `trace run` returns `ERR 500 atrace not loaded`.

This means both discovery and auto-loading failed. The daemon attempted
to run `C:atrace_loader` but either the binary was not found, it returned
a non-zero exit code, or it ran but the named semaphore
(`atrace_patches`) was not found afterward.

See the [atrace Not Loaded](#atrace-not-loaded) section for resolution.

### "atrace is disabled"

**Symptom:** `trace run` returns `ERR 500 atrace is disabled (run:
atrace_loader ENABLE)`.

Global tracing was explicitly disabled. Enable it:
```
$ amigactl trace enable
```

### "TRACE session already active"

**Symptom:** `trace run` or `trace start` returns `ERR 500 TRACE session
already active`.

The same connection already has an active trace stream. Each connection
supports only one trace session at a time. Stop the existing session
(Ctrl-C, or send `STOP`) before starting a new one. If using the Python
API, call `stop_trace()` on the existing session first.

### "TAIL session active"

**Symptom:** `trace start` or `trace run` returns `ERR 500 TAIL session
active`.

The same connection has an active `TAIL` file-streaming session. TAIL and
TRACE are mutually exclusive on a single connection. Stop the tail session
before starting a trace.

### "Missing -- separator"

**Symptom:** `trace run` returns `ERR 100 Missing -- separator`.

The `--` separator between filter options and the command was omitted.
Correct syntax:
```
$ amigactl trace run -- List SYS:C
$ amigactl trace run --lib dos -- Dir SYS:
```

### "PROC filter not valid for TRACE RUN"

**Symptom:** `trace run` returns `ERR 100 PROC filter not valid for
TRACE RUN`.

`trace run` automatically filters to the launched process. Specifying an
additional `PROC=` filter conflicts with this. Use `trace start --proc`
instead if you need to filter by process name.

### "Process table full"

**Symptom:** `trace run` returns `ERR 500 Process table full`.

The daemon's tracked process table is full. This table is shared with
`amigactl exec`/`run` commands. Wait for existing processes to complete,
or use `amigactl kill <id>` to clean up stale entries.

### "Failed to create process"

**Symptom:** `trace run` returns `ERR 500 Failed to create process`.

The Amiga system could not create a new process via `CreateNewProcTags()`.
This typically indicates insufficient memory. Free memory on the Amiga and
retry.

### "Directory not found"

**Symptom:** `trace run --cd <path>` returns `ERR 200 Directory not found`.

The working directory specified with `--cd` does not exist on the Amiga.
Verify the path is correct and the volume is mounted.

### "Async exec unavailable"

**Symptom:** `trace run` returns `ERR 500 Async exec unavailable`.

The daemon's async execution subsystem failed to initialize (`g_proc_sigbit
< 0`), meaning it cannot launch processes. This prevents `trace run` from
creating the target process.

**Solution:** Restart amigactld. The async execution subsystem is initialized
at daemon startup; there is no way to reinitialize it at runtime.


## atrace_loader Issues

### Installation errors

When running `atrace_loader` manually, common errors include:

**"Failed to allocate anchor (92 bytes)"** -- System memory is critically
low. Free memory before retrying.

**"Failed to allocate ring buffer (N entries, M bytes)"** -- Insufficient
contiguous memory for the ring buffer. The default buffer (8192 entries)
requires approximately 1 MB. Reduce the size: `C:atrace_loader BUFSZ 2048`.

**"Failed to allocate patch array (N entries)"** -- Insufficient memory for
patch descriptors (99 entries x 40 bytes = ~4 KB). Very unusual.

**"Cannot open timer.device: error N"** -- timer.device could not be opened.
This is a FATAL error -- without it, stubs cannot record EClock timestamps
and would crash when called. This indicates a severe system problem.

**"FATAL: Cannot open timer.device (required for EClock timestamps)"** --
Same as above, printed as a follow-up message.

**"Cannot open <library> -- skipping N patches"** -- A target library was
not available. Patches for that library's functions are not installed.
This is a warning, not an error -- other libraries are still patched.
Common for `bsdsocket.library` if no TCP/IP stack is running.

**"Failed to install patch for <lib>/<func>"** -- Stub generation or
`SetFunction()` failed for a specific function. This is unusual and may
indicate memory exhaustion. The loader continues with remaining patches.

**"Unknown function: <name>"** -- A function name passed on the command
line was not found in the patch table. Check spelling (function names are
case-insensitive).

### "atrace already loaded"

Running `atrace_loader` when atrace is already resident prints:

```
atrace already loaded. Use STATUS, ENABLE, DISABLE, or QUIT.
```

This is not an error. Use the listed subcommands to manage the existing
installation.

### "atrace is not loaded"

Running `atrace_loader STATUS`, `ENABLE`, or `QUIT` when atrace is not
loaded prints:

```
atrace is not loaded.
```

Note: `DISABLE` does not exhibit this behavior. Running `atrace_loader
DISABLE` when atrace is not loaded performs a fresh installation with
tracing globally disabled (equivalent to loading then immediately
disabling).

Run `atrace_loader` without arguments to perform a fresh installation.


## Removing atrace (QUIT)

To remove atrace from the running system:

```
C:atrace_loader QUIT
```

This performs the following steps:

1. Sets `global_enable` to 0 (disables all stubs).
2. Waits for all in-flight stub executions to drain (`use_count` values
   reach 0, polled up to 50 times at 20ms intervals).
3. Obtains the anchor semaphore exclusively (blocks until the daemon
   releases it).
4. Removes the semaphore from the system list (`RemSemaphore`).
5. Frees the ring buffer memory.
6. Releases the semaphore.

After QUIT:

- **Stubs remain in memory** as transparent pass-throughs. They check
  `global_enable`, find it 0, and jump directly to the original function.
  There is a small (single-digit nanosecond) overhead per call.
- **The anchor, patch descriptors, and stub code are never freed.** They
  are allocated with `MEMF_PUBLIC` and persist until reboot. Freeing them
  would be unsafe because other code may have chained `SetFunction()`
  patches on top of atrace's stubs.
- **The named semaphore is removed**, so subsequent `trace status` reports
  `loaded=0` and auto-loading will install a fresh copy.
- **Full removal requires a reboot.** This is the only way to restore the
  original library jump table entries.

If `QUIT` reports `Warning: use counts did not fully drain`, some stubs
were still executing when the timeout expired. This is rare and generally
harmless -- the stubs will complete and become transparent on their next
invocation.


## Stale Events After Restart

**Symptom:** After stopping and restarting a trace session, old events
from the previous session appear briefly before new events.

**Explanation:** Between trace sessions, background system activity
continues to fill the ring buffer. When a new session starts, the daemon
calls `drain_stale_events()` which:

1. Resets in-flight stall tracking state.
2. Advances `read_pos` to `write_pos` (discards all buffered events).
3. Accumulates the overflow counter into the running total and resets it.
4. Clears the `valid`, `retval`, `ioerr`, and `flags` fields on every
   ring buffer slot to prevent stale data from being misinterpreted.

This happens automatically at the start of every `TRACE START` and
`TRACE RUN` session. You should not see stale events under normal
operation.

**If stale events persist:** This would indicate a bug in the drain
logic. As a workaround, unload and reload atrace:

```
C:atrace_loader QUIT
C:atrace_loader
```

This allocates a fresh ring buffer with all slots zeroed.


## Amiberry-Specific Issues

### JIT Compatibility

atrace works with Amiberry's JIT compiler enabled. The stubs are
allocated in `MEMF_PUBLIC` memory and use standard 68k instructions
that the JIT handles correctly. No special JIT configuration is needed.

### Post-Call Memory Read Issue (Historical)

Early versions of atrace experienced a freeze in Amiberry when the
post-call handler (suffix code, executed after the original function
returns via a trampoline `RTS`) performed data memory reads. Register
operations and memory writes worked correctly, but reads from data
memory (e.g., dereferencing `SysBase` to find `ThisTask`, traversing
pointers to copy the task name) caused the Amiberry UI to hang when
high-frequency functions were traced.

**Root cause:** Amiberry's JIT has specific behavior with code reached
via trampoline `RTS` from dynamically allocated memory. Data memory
reads in this context can trigger a JIT stall.

**Resolution:** All memory-reading code (SysBase dereference, pointer
traversal for task name capture) was moved from the post-call handler
to the pre-call variable region of the stub, where memory reads work
correctly. The task name is the same before and after the call (the
same task is executing), so this relocation has no semantic effect.

This issue is **fixed in the current codebase** and is documented here
only for historical reference. If you are running an older version of
atrace and experience UI freezes correlated with high trace event
rates, update to the current version.

### MEMF_PUBLIC Requirement

All atrace memory allocations (anchor, ring buffer, patch descriptors,
stub code) use `MEMF_PUBLIC`. This is required because:

- Stubs execute in the context of any process that calls the patched
  library function, not in atrace_loader's process context.
- If `MEMF_ANY` were used, the memory could be associated with
  atrace_loader's process and freed when that process exits.
- `MEMF_PUBLIC` memory persists until explicitly freed or until reboot.

This is not configurable and is handled automatically by the loader.


## Error Messages Reference

### Daemon console messages

| Message | Meaning |
|---------|---------|
| `Auto-loading C:atrace_loader` | Daemon is attempting auto-load because atrace was not found. |
| `Auto-load: C:atrace_loader returned N` | Auto-load failed; the loader exited with return code N. |
| `Auto-load: cannot open NIL:` | System-level failure opening the null device (very unusual). |
| `WARNING: noise function 'X' not found in patch table` | A function in the daemon's noise list does not match any installed patch. Indicates a mismatch between the daemon and loader function tables. |

### Protocol error messages

| Error | Code | Context |
|-------|------|---------|
| `atrace not loaded` | 500 | TRACE START, RUN, ENABLE, DISABLE when auto-load fails. |
| `atrace is disabled (run: atrace_loader ENABLE)` | 500 | TRACE START, RUN when global_enable is 0. |
| `TRACE session already active` | 500 | Starting a second trace on the same connection. |
| `TAIL session active` | 500 | Starting trace when a TAIL session is active. |
| `Missing -- separator` | 100 | TRACE RUN without the `--` between filters and command. |
| `Missing command` | 100 | TRACE RUN with `--` but no command after it. |
| `PROC filter not valid for TRACE RUN` | 100 | TRACE RUN with an explicit PROC= filter. |
| `Process table full` | 500 | No free slots in the daemon's tracked process table. |
| `Failed to create process` | 500 | CreateNewProcTags() failed (usually out of memory). |
| `Directory not found` | 200 | TRACE RUN --cd with a non-existent path. |
| `Async exec unavailable` | 500 | The daemon's async process infrastructure is not initialized. |
| `Unknown function: X` | 100 | TRACE ENABLE/DISABLE with an unrecognized function name. |
| `Unknown TRACE subcommand` | 100 | Unrecognized TRACE subcommand (not START/RUN/STATUS/ENABLE/DISABLE/TIER). |
| `Usage: TRACE TIER <1\|2\|3>` | 100 | TRACE TIER without an argument. |
| `Invalid tier level (1-3)` | 100 | TRACE TIER with an out-of-range value. |

### Client-side exceptions

| Exception | Condition |
|-----------|-----------|
| `ConnectionRefusedError` | Daemon is not running or port is wrong. |
| `OSError` | Network-level failure (timeout, reset, unreachable). |
| `ProtocolError("Not connected")` | Calling trace methods on a closed connection. |
| `ValueError("Unknown preset: ...")` | Invalid filter preset name passed to `trace_start()` or `trace_run()`. |

See [python-api.md](python-api.md) for the full exception hierarchy.


## Related Documents

- [architecture.md](architecture.md) -- System architecture and component interactions
- [cli-reference.md](cli-reference.md) -- Complete CLI command reference
- [ring-buffer.md](ring-buffer.md) -- Ring buffer design and overflow mechanics
- [output-tiers.md](output-tiers.md) -- Tier system and function membership
- [filtering.md](filtering.md) -- Filter syntax and behavior
- [trace-run.md](trace-run.md) -- TRACE RUN task-scoped tracing
- [daemon-integration.md](daemon-integration.md) -- Daemon-side trace processing
- [stub-mechanism.md](stub-mechanism.md) -- Stub code generation and disabled path
- [python-api.md](python-api.md) -- Python client API and exception handling
- [performance.md](performance.md) -- Performance characteristics and tuning
