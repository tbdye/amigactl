# Python API Reference

Programmatic access to atrace trace functionality through the amigactl
Python client library. This document covers every trace-related method on
`AmigaConnection`, the `FILTER_PRESETS` dictionary, the low-level
`TraceStreamReader` class, event parsing, and the tier utility functions.

For CLI usage, see [cli-reference.md](cli-reference.md). For the
interactive TUI viewer, see [interactive-viewer.md](interactive-viewer.md).


## Connection Setup

All trace operations use the `AmigaConnection` class from the `amigactl`
package:

```python
from amigactl import AmigaConnection, FILTER_PRESETS

conn = AmigaConnection("192.168.6.228", port=6800, timeout=30)
conn.connect()
```

As a context manager:

```python
with AmigaConnection("192.168.6.228") as conn:
    status = conn.trace_status()
    print(status)
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `host` | str | (required) | Amiga IP address or hostname |
| `port` | int | 6800 | amigactld TCP port |
| `timeout` | int | 30 | Socket timeout in seconds |

The connection can also be managed manually with `conn.connect()` and
`conn.close()`.


## trace_status()

Query the current state of the atrace subsystem.

```python
status = conn.trace_status()
```

**Returns:** `dict` with the following keys:

| Key | Type | Description |
|-----|------|-------------|
| `loaded` | bool | Whether atrace_loader has been run |
| `enabled` | bool | Whether global tracing is active |
| `patches` | int | Total number of patches installed |
| `events_produced` | int | Cumulative events written to ring buffer |
| `events_consumed` | int | Cumulative events read by daemon |
| `events_dropped` | int | Oldest events overwritten due to ring buffer overflow |
| `buffer_capacity` | int | Ring buffer slot count |
| `buffer_used` | int | Slots currently occupied |
| `filter_task` | str | Hex Task pointer for TRACE RUN filter (e.g. `"0x0e300200"`), or `"0x00000000"` if none. Only present when loaded. |
| `noise_disabled` | int | Count of noise functions currently disabled. Only present when loaded. |
| `anchor_version` | int | atrace kernel module version. Only present when loaded. |
| `eclock_freq` | int | EClock frequency in Hz (e.g. 709379 for PAL). Only present when loaded. |
| `patch_list` | list | List of `{"name": "lib.func", "enabled": bool}` dicts. Only present when loaded. |

Integer fields are only present when atrace is loaded. When `loaded` is
`False`, the dict contains only `loaded` and `enabled`.

**Example:**

```python
with AmigaConnection("192.168.6.228") as conn:
    status = conn.trace_status()
    if status.get("loaded"):
        print("Patches:", status["patches"])
        print("Events produced:", status["events_produced"])
        print("Buffer usage: {}/{}".format(
            status["buffer_used"], status["buffer_capacity"]))

        # List enabled functions
        for patch in status.get("patch_list", []):
            if patch["enabled"]:
                print("  enabled:", patch["name"])
```


## trace_start(callback, ...)

Start a blocking trace event stream. Enters a read loop and calls the
provided callback for each trace event until the stream ends.

```python
def trace_start(self, callback, lib=None, func=None, proc=None,
                errors_only=None, preset=None)
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `callback` | callable | (required) | Called with each event dict |
| `lib` | str or None | None | Library filter (e.g. `"dos"`) |
| `func` | str or None | None | Function filter (e.g. `"Open"` or `"Open,Lock"`) |
| `proc` | str or None | None | Process name filter (substring match) |
| `errors_only` | bool or None | None | If True, only stream error events |
| `preset` | str or None | None | Filter preset name from `FILTER_PRESETS` |

**Returns:** None. The method blocks until the stream ends (via
`stop_trace()` from another connection or thread).

**Callback signature:** `callback(event_dict)` where `event_dict` is
either a trace event or a comment. See [Event Dict Format](#event-dict-format)
below.

**Important:** `trace_start()` does not catch `KeyboardInterrupt`. The
caller should catch it and call `stop_trace()` on a separate connection
to terminate cleanly:

```python
with AmigaConnection("192.168.6.228") as conn:
    events = []
    def collector(event):
        if event["type"] == "event":
            events.append(event)
            print("{}.{}: {}".format(event["lib"], event["func"], event["args"]))

    try:
        conn.trace_start(collector, lib="dos")
    except KeyboardInterrupt:
        # stop_trace() must come from a DIFFERENT connection
        # because this connection is occupied by the stream.
        with AmigaConnection("192.168.6.228") as stop_conn:
            stop_conn.stop_trace()
```

When `preset` is specified, its filter values are applied as defaults.
Explicit `lib`, `func`, and `errors_only` parameters override preset
values. See [Filter Presets](#filter-presets) for available presets.


## trace_run(command, callback, ...)

Launch a command on the Amiga and trace only its library calls. The stream
auto-terminates when the launched process exits.

```python
def trace_run(self, command, callback, lib=None, func=None,
              errors_only=None, cd=None, preset=None)
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `command` | str | (required) | AmigaOS command to execute |
| `callback` | callable | (required) | Called with each event dict |
| `lib` | str or None | None | Library filter |
| `func` | str or None | None | Function filter |
| `errors_only` | bool or None | None | If True, only stream error events |
| `cd` | str or None | None | Working directory for the command |
| `preset` | str or None | None | Filter preset name |

There is no `proc` parameter. TRACE RUN automatically filters by the
launched process's Task pointer -- a `proc` filter would be redundant
and the daemon rejects it with ERR 100.

**Returns:** `dict` with the following structure:

```python
{
    "proc_id": 3,           # int -- daemon-assigned process ID
    "rc": 0,                # int or None -- process exit code
    "stats": {
        "total_events": 47,         # int -- total events received
        "by_function": {            # dict -- per-function call counts
            "Open": 12,
            "Lock": 8,
            "Close": 11,
            "OpenLibrary": 5,
            ...
        },
        "errors": 2,                # int -- total error events
        "error_functions": {        # dict -- per-function error counts
            "Open": 1,
            "Lock": 1,
        },
    },
}
```

**Example:**

```python
with AmigaConnection("192.168.6.228") as conn:
    def on_event(event):
        if event["type"] == "event":
            print(event["func"], event["args"])

    result = conn.trace_run("List SYS:C", on_event, lib="dos")
    print("Exit code:", result["rc"])
    print("Total events:", result["stats"]["total_events"])
    for func, count in sorted(result["stats"]["by_function"].items(),
                               key=lambda x: -x[1]):
        print("  {}: {}".format(func, count))
```


## trace_events(...)

Generator that yields trace events from a TRACE START session. This is
the most convenient API for consuming events in a `for` loop.

```python
def trace_events(self, lib=None, func=None, proc=None,
                 errors_only=None, preset=None)
```

**Parameters:** Same as `trace_start()` except no `callback`.

**Yields:** Event dicts (same format as callback-based methods).

**Two-connection requirement:** `trace_events()` runs `trace_start()` in
a background thread and uses an internal queue. When the generator is
closed (loop break, garbage collection, or exception), it opens a *second*
`AmigaConnection` to the same host to call `stop_trace()`. This is
necessary because the primary connection is occupied by the blocking
stream. The daemon must be able to accept the additional connection.

If the stop connection fails (daemon unreachable, max clients reached) and
the background thread does not exit within 5 seconds, the primary socket
is closed as a fallback to unblock the recv.

**Example:**

```python
with AmigaConnection("192.168.6.228") as conn:
    for event in conn.trace_events(lib="dos"):
        if event["type"] == "comment":
            continue
        print("{}.{}: {} -> {}".format(
            event["lib"], event["func"],
            event["args"], event["retval"]))
        if event["func"] == "Open" and event["status"] == "E":
            print("  FAILED OPEN!")
            break  # Automatically stops the trace
```


## trace_analyze(command, ...)

Convenience method that combines `trace_run()` with post-processing to
produce a structured diagnostic summary. Collects events, extracts file
access patterns, and identifies library opens.

```python
def trace_analyze(self, command, max_events=10000, cd=None,
                  lib=None, func=None, errors_only=False, preset=None)
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `command` | str | (required) | AmigaOS command to trace |
| `max_events` | int | 10000 | Maximum events to collect (safety limit) |
| `cd` | str or None | None | Working directory |
| `lib` | str or None | None | Library filter |
| `func` | str or None | None | Function filter |
| `errors_only` | bool | False | Only capture error events |
| `preset` | str or None | None | Filter preset name |

**Returns:** `dict` with the following structure:

```python
{
    "events": [...],            # list of event dicts (up to max_events)
    "stats": {                  # same as trace_run stats
        "total_events": 47,
        "by_function": {"Open": 12, ...},
        "errors": 2,
        "error_functions": {"Open": 1, ...},
    },
    "errors": [...],            # list of error event dicts
    "file_access": [            # sorted list of unique file paths accessed
        "LIBS:bsdsocket.library",
        "RAM:test",
        ...
    ],
    "lib_opens": [              # sorted list of libraries opened
        "dos.library",
        "exec.library",
        ...
    ],
    "rc": 0,                    # process exit code (int or None)
}
```

File access is extracted from Open, Lock, DeleteFile, CreateDir,
SetProtection, and GetDiskObject events. Library opens are extracted from
OpenLibrary events. Both are identified by parsing quoted filenames from
the `args` field.

**Example:**

```python
with AmigaConnection("192.168.6.228") as conn:
    report = conn.trace_analyze("List SYS:C")
    print("Exit code:", report["rc"])
    print("Files accessed:")
    for path in report["file_access"]:
        print("  ", path)
    print("Libraries opened:")
    for lib in report["lib_opens"]:
        print("  ", lib)
    if report["errors"]:
        print("Errors:")
        for err in report["errors"]:
            print("  {}: {} -> {}".format(
                err["func"], err["args"], err["retval"]))
```


## trace_start_raw(...) / trace_run_raw(...)

Non-blocking variants that return a `RawTraceSession` instead of entering
a blocking read loop. Used by the interactive TUI viewer and custom
event-loop consumers.

### trace_start_raw()

```python
def trace_start_raw(self, lib=None, func=None, proc=None,
                    errors_only=False)
```

**Parameters:** Same filter parameters as `trace_start()`, except no
`callback` or `preset`. Note that `errors_only` defaults to `False`
here rather than `None`, since without preset support there is no
sentinel value to distinguish 'unset' from 'off'.

**Returns:** `RawTraceSession` context manager.

### trace_run_raw()

```python
def trace_run_raw(self, command, lib=None, func=None,
                  errors_only=False, cd=None)
```

**Returns:** `(RawTraceSession, proc_id)` tuple, where `proc_id` is the
daemon-assigned process ID (int or None).

### RawTraceSession

The `RawTraceSession` object provides:

| Attribute | Type | Description |
|-----------|------|-------------|
| `sock` | `socket.socket` | The underlying socket (set to non-blocking) |
| `reader` | `TraceStreamReader` | Stateful event parser for non-blocking reads |

Use as a context manager to ensure socket timeout is restored:

```python
import select

with conn.trace_start_raw(lib="dos") as session:
    while True:
        readable, _, _ = select.select([session.sock], [], [], 1.0)
        if readable:
            event = session.reader.try_read_event()
            if event is False:
                break  # Stream ended
            if event is not None:
                print(event)
        # Process buffered events (multiple may arrive in one recv)
        while session.reader.has_buffered_data():
            event = session.reader.drain_buffered()
            if event is False:
                break
            if event is not None:
                print(event)
```

See [TraceStreamReader](#tracestreamreader) for the full reader API.


## stop_trace()

Send STOP during an active trace stream and drain remaining DATA chunks
until the END sentinel.

```python
def stop_trace(self)
```

After this call, the connection returns to normal command mode. Uses a
10-second timeout for draining.

**Critical usage note:** `stop_trace()` sends a STOP command to the
daemon. It can be called on *any* connection — the daemon routes the stop
to whichever trace stream is active. Because `trace_start()` is
blocking, you typically need a second connection or thread to trigger the
stop:

```python
# Pattern 1: Two connections
with AmigaConnection("192.168.6.228") as trace_conn:
    with AmigaConnection("192.168.6.228") as control_conn:
        import threading

        def run_trace():
            trace_conn.trace_start(my_callback)

        t = threading.Thread(target=run_trace, daemon=True)
        t.start()
        # ... do other work ...
        control_conn.stop_trace()  # Stops the stream
        t.join()

# Pattern 2: Use trace_events() (handles this automatically)
with AmigaConnection("192.168.6.228") as conn:
    for event in conn.trace_events(lib="dos"):
        process(event)
        if done:
            break  # trace_events handles stop_trace internally
```


## send_filter(...)

Send a FILTER command during an active trace stream to change filters
without restarting the stream.

```python
def send_filter(self, lib=None, func=None, proc=None, raw=None)
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `lib` | str or None | None | Library filter |
| `func` | str or None | None | Function filter |
| `proc` | str or None | None | Process name filter |
| `raw` | str or None | None | Raw filter string (overrides lib/func/proc when provided) |

**Fire-and-forget:** No response is expected from the daemon. The method
handles non-blocking sockets by temporarily switching to blocking mode
with a 2-second timeout for the send, then restoring non-blocking mode.
If the send fails (e.g., TCP buffer full), the error is silently swallowed.

Call with no arguments to clear all filters:

```python
conn.send_filter()  # Clear all filters
```

The `raw` parameter allows sending arbitrary FILTER command syntax
including extended features like blacklists and ENABLE/DISABLE clauses:

```python
# Whitelist two libraries
conn.send_filter(raw="LIB=dos,exec")

# Blacklist specific functions
conn.send_filter(raw="-FUNC=AllocMem,GetMsg")

# Combined whitelist and blacklist
conn.send_filter(raw="LIB=dos -FUNC=Close ERRORS")

# Enable/disable individual functions (tier switching mechanism)
conn.send_filter(raw="ENABLE=UnLock,Examine DISABLE=OpenLibrary")
```

**Important:** The daemon's `parse_extended_filter()` resets all
per-session filter state at the start of every FILTER command. If you send
`FILTER ENABLE=UnLock`, any previously active LIB/FUNC/PROC filters are
cleared. To preserve existing filters alongside ENABLE/DISABLE, include
them in the same command.


## send_inline(cmd)

Send a bare inline command during an active trace stream. Like
`send_filter()`, this is fire-and-forget with no expected response.

```python
def send_inline(self, cmd)
```

**Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `cmd` | str | Command string to send (e.g. `"TIER 2"`) |

Handles non-blocking sockets the same way as `send_filter()`. Used for
commands that the daemon's `trace_handle_input()` processes alongside
STOP and FILTER.

**Example:**

```python
# Notify daemon of tier level (for content-based filtering)
conn.send_inline("TIER 2")
```


## Tier Switching

There is no `send_tier()` method on `AmigaConnection`. Tier switching is
performed through a combination of ENABLE/DISABLE function deltas and the
TIER inline command.

The process for programmatic tier switching:

1. Use `compute_tier_switch()` from `amigactl.trace_tiers` to calculate
   which functions need to be enabled and disabled.
2. Send the function deltas via `send_filter(raw=...)` with
   ENABLE=/DISABLE= clauses.
3. Optionally send `TIER <n>` via `send_inline()` to inform the daemon
   of the new tier level (enables content-based filtering like
   suppressing OpenLibrary version 0 events at the Basic tier).

**Example -- switching from tier 1 (Basic) to tier 2 (Detail):**

```python
from amigactl.trace_tiers import compute_tier_switch

to_enable, to_disable = compute_tier_switch(
    old_level=1, new_level=2)

# Build FILTER command with deltas
parts = []
if to_enable:
    parts.append("ENABLE=" + ",".join(sorted(to_enable)))
if to_disable:
    parts.append("DISABLE=" + ",".join(sorted(to_disable)))
if parts:
    conn.send_filter(raw=" ".join(parts))

# Notify daemon of new tier level
conn.send_inline("TIER 2")
```

**Alternative for non-streaming contexts:** When the connection is not in
streaming mode (no active TRACE START), use `trace_enable()` and
`trace_disable()` instead:

```python
from amigactl.trace_tiers import compute_tier_switch

to_enable, to_disable = compute_tier_switch(1, 2)
if to_enable:
    conn.trace_enable(funcs=sorted(to_enable))
if to_disable:
    conn.trace_disable(funcs=sorted(to_disable))
```

This approach uses the standard request-response protocol (`TRACE ENABLE`
/ `TRACE DISABLE` commands) which waits for an OK response. It does not
work during streaming because the socket is occupied by trace DATA chunks.


## trace_enable(funcs=None) / trace_disable(funcs=None)

Enable or disable tracing globally, or toggle specific functions.

```python
def trace_enable(self, funcs=None)
def trace_disable(self, funcs=None)
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `funcs` | list of str or None | None | Function names to enable/disable. If None, toggles the global enable flag. |

When `funcs` is None:
- `trace_enable()` sets `global_enable = 1` in the anchor.
- `trace_disable()` sets `global_enable = 0` and drains the ring buffer.

When `funcs` is a list:
- `trace_enable(["Open", "Lock"])` enables the named function patches.
- `trace_disable(["Open", "Lock"])` disables the named function patches.

**Raises:** `AmigactlError` if atrace is not loaded or a function name is
not recognized.

**Important:** These methods use the standard request-response protocol.
They cannot be called during an active trace stream (the socket is
occupied by DATA chunks). During streaming, use `send_filter()` with
ENABLE=/DISABLE= clauses instead.

**Example:**

```python
with AmigaConnection("192.168.6.228") as conn:
    # Enable tracing globally
    conn.trace_enable()

    # Enable specific Manual-tier functions
    conn.trace_enable(funcs=["AllocMem", "FreeMem", "AllocVec", "FreeVec"])

    # Disable a noisy function
    conn.trace_disable(funcs=["FindPort"])

    # Disable tracing globally
    conn.trace_disable()
```


## Filter Presets

The `FILTER_PRESETS` dictionary provides named filter configurations for
common tracing scenarios. Presets are a Python API feature only -- there
is no `--preset` CLI flag. For CLI equivalents, see
[cli-reference.md](cli-reference.md).

```python
from amigactl import FILTER_PRESETS
```

### Available Presets

| Preset | lib | func_list | errors_only | Description |
|--------|-----|-----------|-------------|-------------|
| `file-io` | `dos` | Open, Close, Lock, DeleteFile, CreateDir, Rename, MakeLink, SetProtection | -- | DOS file operations |
| `lib-load` | -- | OpenLibrary, OpenDevice, OpenResource, CloseLibrary, CloseDevice | -- | Library/device lifecycle |
| `network` | `bsdsocket` | -- | -- | All bsdsocket.library calls |
| `ipc` | -- | FindPort, GetMsg, PutMsg, ObtainSemaphore, ReleaseSemaphore, AddPort, WaitPort | -- | Inter-process communication |
| `errors-only` | -- | -- | True | Only error returns (all libraries) |
| `memory` | -- | AllocMem, FreeMem, AllocVec, FreeVec | -- | Memory allocation tracking |
| `window` | -- | OpenWindow, CloseWindow, OpenScreen, CloseScreen, OpenWindowTagList, OpenScreenTagList, ActivateWindow | -- | Window/screen lifecycle |
| `icon` | `icon` | -- | -- | All icon.library calls |

### Preset Resolution

When a preset is passed to `trace_start()`, `trace_run()`,
`trace_events()`, or `trace_analyze()`, its values are applied as
defaults. Explicit parameters override preset values:

```python
# Use the file-io preset
conn.trace_start(callback, preset="file-io")
# Equivalent to: conn.trace_start(callback, lib="dos",
#     func="Open,Close,Lock,DeleteFile,CreateDir,Rename,MakeLink,SetProtection")

# Override the preset's lib filter
conn.trace_start(callback, preset="file-io", lib="exec")
# Uses lib="exec" instead of "dos", but keeps the func_list from the preset

# Combine preset with errors_only
conn.trace_run("MyProgram", callback, preset="network", errors_only=True)
```

### Preset Data Structure

Each preset is a dict with optional keys:

- `lib` (str): Library name for the LIB= wire filter.
- `func_list` (list of str): Function names, joined with commas for the
  FUNC= wire filter.
- `errors_only` (bool): If True, adds the ERRORS flag.

**Example -- listing all presets:**

```python
from amigactl import FILTER_PRESETS

for name, spec in sorted(FILTER_PRESETS.items()):
    parts = []
    if "lib" in spec:
        parts.append("LIB=" + spec["lib"])
    if "func_list" in spec:
        parts.append("FUNC=" + ",".join(spec["func_list"]))
    if spec.get("errors_only"):
        parts.append("ERRORS")
    print("{}: {}".format(name, " ".join(parts)))
```


## Event Dict Format

All callback-based and generator-based trace methods produce event dicts
in one of two forms.

### Trace Events

Events of type `"event"` have these guaranteed fields:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `type` | str | `"event"` | Always `"event"` |
| `raw` | str | (input) | Raw tab-separated event line |
| `seq` | int | 0 | Sequence number (monotonically increasing) |
| `time` | str | `""` | Timestamp string (e.g. `"10:30:15.123"`) |
| `lib` | str | `""` | Library short name (e.g. `"dos"`, `"exec"`) |
| `func` | str | `""` | Function name (e.g. `"Open"`, `"AllocMem"`) |
| `task` | str | `""` | Task identifier (e.g. `"[3] Shell Process"`) |
| `args` | str | `""` | Formatted arguments |
| `retval` | str | `""` | Return value string |
| `status` | str | `"-"` | `"O"` (success), `"E"` (error), `"-"` (neutral/void) |

All keys are present regardless of whether the event line was well-formed.
Missing or unparseable fields use the defaults shown above. This contract
allows callers to access any key without existence checks.

### Comment Events

Comments have type `"comment"` with a single `text` field:

```python
{"type": "comment", "text": "PROCESS EXITED rc=0"}
```

Common comments include trace session headers (function list, filter
state) and the `"PROCESS EXITED rc=N"` notification at the end of a
TRACE RUN session.

### Parsing Details

Events are parsed by `_parse_trace_event()` in `amigactl.protocol`. The
wire format is tab-separated with 7 fields:

```
SEQ \t TIME \t LIB.FUNC \t TASK \t ARGS \t RETVAL \t STATUS
```

The `lib` and `func` fields are split from the `LIB.FUNC` column at the
first dot character. For more on interpreting field values, see
[reading-output.md](reading-output.md).


## TraceStreamReader

The `TraceStreamReader` class in `amigactl.protocol` provides non-blocking,
stateful event parsing for use with `select()`. It is the core reader
used by `RawTraceSession`.

```python
from amigactl.protocol import TraceStreamReader
```

### Constructor

```python
reader = TraceStreamReader(sock)
```

Takes a socket object. The socket should be set to non-blocking mode by
the caller.

### States

The reader maintains an internal state machine with four states:

| State | Description |
|-------|-------------|
| `"header"` | Accumulating bytes for the next line (DATA, END, ERR, or comment) |
| `"chunk"` | After seeing `DATA <len>`, accumulating the binary payload |
| `"sentinel"` | Waiting for the sentinel line after END |
| `"err_sentinel"` | Waiting for the sentinel line after ERR |

### Methods

**`try_read_event()`**

Attempt to read one complete trace event. Calls `recv(4096)` on the
socket, buffers the data, and attempts to parse a complete event.

Returns:
- `dict` -- A complete parsed event (type `"event"` or `"comment"`)
- `None` -- Incomplete data; call again when `select()` fires
- `False` -- Stream ended (END received and sentinel consumed)

Raises `ProtocolError` on framing errors and `ConnectionError` on socket
close.

**`drain_buffered()`**

Process buffered data without calling `recv()`. Call this after
`try_read_event()` when `has_buffered_data()` returns True, because
multiple events may arrive in a single `recv()` call.

Returns the same values as `try_read_event()`.

**`has_buffered_data()`**

Returns `True` if there is unprocessed data in the internal buffer.

### Usage Pattern

```python
import select

reader = TraceStreamReader(sock)
sock.setblocking(False)

while True:
    readable, _, _ = select.select([sock], [], [], 1.0)
    if readable:
        result = reader.try_read_event()
        if result is False:
            break  # Stream ended
        if result is not None:
            handle_event(result)
    # Always drain buffered data
    while reader.has_buffered_data():
        result = reader.drain_buffered()
        if result is False:
            break
        if result is not None:
            handle_event(result)
```


## _parse_trace_event(text)

Low-level parser that converts a tab-separated trace event line into a
dict. Used internally by `TraceStreamReader` and `read_one_trace_event()`.

```python
from amigactl.protocol import _parse_trace_event

event = _parse_trace_event("42\t10:30:15.123\tdos.Open\t[3] Shell\t\"RAM:test\",Read\t0x1c16daf\tO")
```

**Parameter:** `text` (str) -- The raw tab-separated event line.

**Returns:** `dict` with the fields documented in
[Event Dict Format](#event-dict-format). All keys are initialized to
defaults, even for malformed input.

The underscore prefix indicates this is a module-internal function, but it
is a stable API contract for agent consumers. The guaranteed field set will
not change without notice.


## Tier Utility Functions (trace_tiers)

The `amigactl.trace_tiers` module defines the three output tiers and
provides functions for computing tier transitions. It is the authoritative
source of truth for which functions belong to each tier.

```python
from amigactl.trace_tiers import (
    functions_for_tier, compute_tier_switch,
    tier_for_function, tier_name, detect_tier,
    TIER_BASIC, TIER_DETAIL, TIER_VERBOSE, TIER_MANUAL,
    TIER_BASIC_LEVEL, TIER_DETAIL_LEVEL, TIER_VERBOSE_LEVEL,
)
```

### Tier Constants

| Constant | Value | Description |
|----------|-------|-------------|
| `TIER_BASIC_LEVEL` | 1 | Basic tier level |
| `TIER_DETAIL_LEVEL` | 2 | Detail tier level |
| `TIER_VERBOSE_LEVEL` | 3 | Verbose tier level |
| `TIER_BASIC` | frozenset (57 functions) | Basic tier function names |
| `TIER_DETAIL` | frozenset (13 functions) | Detail-only function names |
| `TIER_VERBOSE` | frozenset (3 functions) | Verbose-only function names |
| `TIER_MANUAL` | frozenset (26 functions) | Manual-only function names (never auto-enabled) |

Total: 99 functions across all tiers. The module validates this at import
time with assertions.

### functions_for_tier(level)

Return the cumulative function set for a tier level. Tiers are cumulative:

- Level 1 (Basic): `TIER_BASIC` (57 functions)
- Level 2 (Detail): `TIER_BASIC | TIER_DETAIL` (70 functions)
- Level 3 (Verbose): `TIER_BASIC | TIER_DETAIL | TIER_VERBOSE` (73 functions)

Manual functions are never included in any tier.

**Parameters:** `level` (int) -- Tier level (1, 2, or 3).

**Returns:** `frozenset` of function name strings.

### compute_tier_switch(old_level, new_level, manual_additions=None, manual_removals=None)

Compute the ENABLE/DISABLE function deltas for a tier transition.
Calculates the difference between the old effective set (tier + manual
overrides) and the new clean tier set. Manual overrides are cleared on
tier switch.

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `old_level` | int | (required) | Current tier level (1, 2, or 3) |
| `new_level` | int | (required) | Target tier level (1, 2, or 3) |
| `manual_additions` | set or None | None | Function names manually enabled outside the current tier |
| `manual_removals` | set or None | None | Function names manually disabled within the current tier |

**Returns:** `(to_enable, to_disable)` -- two sets of function name strings.

**Example:**

```python
from amigactl.trace_tiers import compute_tier_switch

# Switch from Basic to Detail
to_enable, to_disable = compute_tier_switch(1, 2)
print("Enable:", sorted(to_enable))
# ['AllocSignal', 'CloseLibrary', 'CreateMsgPort', 'DeleteMsgPort',
#  'Examine', 'FreeSignal', 'ModifyIDCMP', 'Seek', 'UnLoadSeg',
#  'UnLock', 'UnlockPubScreen', 'recvfrom', 'sendto']
print("Disable:", sorted(to_disable))
# [] (Detail is a superset of Basic)

# Switch from Detail back to Basic with a manually-enabled function
to_enable, to_disable = compute_tier_switch(
    2, 1, manual_additions={"AllocMem"})
print("Disable:", sorted(to_disable))
# ['AllocMem', 'AllocSignal', 'CloseLibrary', ...] (includes manual addition)
```

### tier_for_function(func_name)

Return the tier level where a function is defined (not cumulative).

**Returns:** `int` (1, 2, or 3) for tiered functions, or `None` for
Manual-tier or unknown functions.

```python
from amigactl.trace_tiers import tier_for_function

tier_for_function("Open")        # 1 (Basic)
tier_for_function("UnLock")      # 2 (Detail)
tier_for_function("ExNext")      # 3 (Verbose)
tier_for_function("AllocMem")    # None (Manual)
tier_for_function("nonexistent") # None
```

### tier_name(level)

Return the display name for a tier level.

**Returns:** `"basic"`, `"detail"`, or `"verbose"` for levels 1-3.
Returns `"tier-N"` for unknown levels.

### detect_tier(enabled_set)

Detect which cumulative tier the enabled function set matches exactly.

**Parameters:** `enabled_set` -- set or frozenset of function name strings.

**Returns:** `int` (1, 2, or 3) if the set exactly matches a cumulative
tier, or `None` if it does not match any clean tier (indicating manual
overrides are present).

```python
from amigactl.trace_tiers import detect_tier, functions_for_tier

detect_tier(functions_for_tier(1))  # 1
detect_tier(functions_for_tier(2))  # 2
detect_tier({"Open", "Lock"})       # None (not a clean tier)
```


## Complete Examples

### Trace File I/O with Error Reporting

```python
from amigactl import AmigaConnection

with AmigaConnection("192.168.6.228") as conn:
    for event in conn.trace_events(preset="file-io"):
        if event["type"] == "comment":
            continue
        status = "[ERROR]" if event["status"] == "E" else "[ok]"
        print("{} {} {} -> {}".format(
            status, event["func"], event["args"], event["retval"]))
```

### Profile a Program's Library Usage

```python
from amigactl import AmigaConnection

with AmigaConnection("192.168.6.228") as conn:
    report = conn.trace_analyze("MyProgram", cd="Work:")
    print("Exit code:", report["rc"])
    print()
    print("Libraries opened:")
    for lib in report["lib_opens"]:
        print("  ", lib)
    print()
    print("Files accessed:")
    for path in report["file_access"]:
        print("  ", path)
    print()
    print("Call counts:")
    for func, count in sorted(
            report["stats"]["by_function"].items(),
            key=lambda x: -x[1])[:10]:
        print("  {:20s} {:5d}".format(func, count))
    if report["errors"]:
        print()
        print("Errors ({} total):".format(len(report["errors"])))
        for err in report["errors"]:
            print("  {}.{}: {} -> {}".format(
                err["lib"], err["func"], err["args"], err["retval"]))
```

### Memory Leak Detection

```python
from amigactl import AmigaConnection

with AmigaConnection("192.168.6.228") as conn:
    allocs = {}  # address -> (func, args, seq)

    def track_memory(event):
        if event["type"] == "comment":
            return
        func = event["func"]
        retval = event["retval"]
        args = event["args"]

        if func in ("AllocMem", "AllocVec") and retval != "NULL":
            allocs[retval] = (func, args, event["seq"])
        elif func in ("FreeMem", "FreeVec"):
            # First arg is the address being freed
            addr = args.split(",")[0] if "," in args else args
            allocs.pop(addr, None)

    result = conn.trace_run(
        "MyProgram", track_memory, preset="memory")

    if allocs:
        print("Potential leaks ({} unfreed):".format(len(allocs)))
        for addr, (func, args, seq) in sorted(
                allocs.items(), key=lambda x: x[1][2]):
            print("  seq={} {} {} -> {}".format(seq, func, args, addr))
    else:
        print("No leaks detected.")
```

### Programmatic Tier Switching During a Raw Session

```python
import select
from amigactl import AmigaConnection
from amigactl.trace_tiers import compute_tier_switch

with AmigaConnection("192.168.6.228") as conn:
    with conn.trace_start_raw() as session:
        current_tier = 1
        event_count = 0

        while True:
            readable, _, _ = select.select([session.sock], [], [], 0.5)
            if readable:
                event = session.reader.try_read_event()
                if event is False:
                    break
                if event is not None and event.get("type") == "event":
                    event_count += 1

                while session.reader.has_buffered_data():
                    event = session.reader.drain_buffered()
                    if event is False:
                        break
                    if event is not None and event.get("type") == "event":
                        event_count += 1

            # Switch to Detail tier after 100 events
            if event_count >= 100 and current_tier == 1:
                to_enable, to_disable = compute_tier_switch(1, 2)
                parts = []
                if to_enable:
                    parts.append("ENABLE=" + ",".join(sorted(to_enable)))
                if to_disable:
                    parts.append("DISABLE=" + ",".join(sorted(to_disable)))
                if parts:
                    conn.send_filter(raw=" ".join(parts))
                conn.send_inline("TIER 2")
                current_tier = 2
                print("Switched to Detail tier after {} events".format(
                    event_count))
```


## Error Handling

All trace methods can raise:

| Exception | Condition |
|-----------|-----------|
| `ProtocolError` | Not connected, wire protocol violation, unexpected EOF |
| `AmigactlError` | Server returned an error (ERR code + message) |
| `CommandSyntaxError` (code 100) | Unknown command, bad syntax, or invalid filter |
| `NotFoundError` (code 200) | atrace not loaded (for TRACE STATUS/ENABLE/DISABLE) |
| `InternalError` (code 500) | Unexpected daemon-side failure |
| `ValueError` | Unknown preset name, invalid parameter |

The `AmigactlError` subclasses map to specific daemon error codes. See
the amigactl package's exception hierarchy for the complete mapping.

```python
from amigactl import AmigaConnection, NotFoundError, CommandSyntaxError

with AmigaConnection("192.168.6.228") as conn:
    try:
        conn.trace_enable()
    except NotFoundError:
        print("atrace is not loaded -- run C:atrace_loader first")
    except CommandSyntaxError as e:
        print("Bad command:", e.message)
```


## Cross-References

- [cli-reference.md](cli-reference.md) -- CLI equivalents for trace operations
- [filtering.md](filtering.md) -- Wire protocol filter syntax (LIB=, FUNC=, PROC=, ERRORS)
- [output-tiers.md](output-tiers.md) -- Tier membership and switching
- [reading-output.md](reading-output.md) -- Interpreting event fields
- [trace-run.md](trace-run.md) -- TRACE RUN semantics and task filtering
- [interactive-viewer.md](interactive-viewer.md) -- TUI viewer (uses trace_start_raw internally)
