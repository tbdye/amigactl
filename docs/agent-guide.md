# Agent Guide: Using amigactl for Amiga Automation

Practical reference for AI agents interacting with AmigaOS via amigactl.
For full command specifications (arguments, responses, error conditions,
wire format examples), see [Protocol Commands Reference](protocol-commands.md). For wire protocol
details, see [Wire Protocol Specification](protocol.md).

## Quick Start

```python
from amigactl import AmigaConnection

with AmigaConnection("192.168.6.200") as amiga:
    print(amiga.version())
    data = amiga.read("SYS:S/Startup-Sequence")
    print(data.decode("iso-8859-1"))
```

`AmigaConnection(host, port=6800, timeout=30)` -- port and timeout are
optional. Always use the context manager for automatic cleanup.

If running from the repository without installing:

    PYTHONPATH=/path/to/amigactl/client python3 your_script.py

## Method Reference

Every Python method maps to a daemon command documented in
[Protocol Commands Reference](protocol-commands.md). Consult protocol-commands.md for argument formats,
response fields, error conditions, and example transcripts.

### Connection

| Method | Returns | Docs |
|--------|---------|------|
| `version()` | `str` -- version string | [VERSION](protocol-commands.md#version) |
| `ping()` | None | [PING](protocol-commands.md#ping) |
| `uptime()` | `int` -- seconds | [UPTIME](protocol-commands.md#uptime) |
| `shutdown()` | `str` -- info string | [SHUTDOWN](protocol-commands.md#shutdown) |
| `reboot()` | `str` -- info string | [REBOOT](protocol-commands.md#reboot) |
| `quit()` | None -- calls `close()` | [QUIT](protocol-commands.md#quit) |

### Files

| Method | Returns | Docs |
|--------|---------|------|
| `read(path, offset=None, length=None)` | `bytes` | [READ](protocol-commands.md#read) |
| `write(path, data: bytes)` | `int` -- bytes written | [WRITE](protocol-commands.md#write) |
| `append(path, data: bytes)` | `int` -- bytes appended | [APPEND](protocol-commands.md#append) |
| `dir(path, recursive=False)` | `list[dict]` | [DIR](protocol-commands.md#dir) |
| `stat(path)` | `dict` | [STAT](protocol-commands.md#stat) |
| `copy(src, dst, noclone=False, noreplace=False)` | None | [COPY](protocol-commands.md#copy) |
| `delete(path)` | None | [DELETE](protocol-commands.md#delete) |
| `rename(old_path, new_path)` | None | [RENAME](protocol-commands.md#rename) |
| `makedir(path)` | None | [MAKEDIR](protocol-commands.md#makedir) |
| `protect(path, value=None)` | `str` -- hex bits | [PROTECT](protocol-commands.md#protect) |
| `setdate(path, datestamp=None)` | `str` -- new datestamp | [SETDATE](protocol-commands.md#setdate) |
| `checksum(path)` | `dict` -- crc32, size | [CHECKSUM](protocol-commands.md#checksum) |
| `setcomment(path, comment)` | None | [SETCOMMENT](protocol-commands.md#setcomment) |

### Execution

| Method | Returns | Docs |
|--------|---------|------|
| `execute(command, timeout=None, cd=None)` | `(int, str)` -- rc, output | [EXEC](protocol-commands.md#exec) |
| `execute_async(command, cd=None)` | `int` -- process ID | [EXEC](protocol-commands.md#exec) |
| `proclist()` | `list[dict]` | [PROCLIST](protocol-commands.md#proclist) |
| `procstat(proc_id)` | `dict` -- id, command, status, rc | [PROCSTAT](protocol-commands.md#procstat) |
| `signal(proc_id, sig="CTRL_C")` | None | [SIGNAL](protocol-commands.md#signal) |
| `kill(proc_id)` | None -- **dangerous, see Gotchas** | [KILL](protocol-commands.md#kill) |

### System

| Method | Returns | Docs |
|--------|---------|------|
| `sysinfo()` | `dict` -- see keys below | [SYSINFO](protocol-commands.md#sysinfo) |
| `libver(name)` | `dict` -- name, version | [LIBVER](protocol-commands.md#libver) |
| `env(name)` | `str` -- the variable value | [ENV](protocol-commands.md#env) |
| `setenv(name, value=None, volatile=False)` | None | [SETENV](protocol-commands.md#setenv) |
| `assigns()` | `dict` -- name -> path | [ASSIGNS](protocol-commands.md#assigns) |
| `assign(name, path=None, mode=None)` | None | [ASSIGN](protocol-commands.md#assign) |
| `volumes()` | `list[dict]` -- int values for sizes | [VOLUMES](protocol-commands.md#volumes) |
| `ports()` | `list[str]` | [PORTS](protocol-commands.md#ports) |
| `tasks()` | `list[dict]` | [TASKS](protocol-commands.md#tasks) |
| `devices()` | `list[dict]` -- name, version | [DEVICES](protocol-commands.md#devices) |
| `capabilities()` | `dict` -- max\_clients and max\_cmd\_len are `int`, rest are `str` | [CAPABILITIES](protocol-commands.md#capabilities) |

**`sysinfo()` keys:**

| Key | Type | Description |
|-----|------|-------------|
| `chip_free` | `int` | Free chip memory (bytes) |
| `fast_free` | `int` | Free fast memory (bytes) |
| `total_free` | `int` | Total free memory (bytes) |
| `chip_total` | `int` | Total chip memory (bytes; omitted on exec < v39) |
| `fast_total` | `int` | Total fast memory (bytes; omitted on exec < v39) |
| `chip_largest` | `int` | Largest contiguous chip block (bytes) |
| `fast_largest` | `int` | Largest contiguous fast block (bytes) |
| `exec_version` | `str` | exec.library version (e.g., `"40.68"`) |
| `kickstart` | `str` | Kickstart revision (e.g., `"40"`) |
| `bsdsocket` | `str` | bsdsocket.library version (e.g., `"4.364"`) |

### ARexx and Streaming

| Method | Returns | Docs |
|--------|---------|------|
| `arexx(port, command, timeout=35)` | `(int, str)` -- rc, result | [AREXX](protocol-commands.md#arexx) |
| `tail(path, callback)` | None -- **blocks, see Gotchas** | [TAIL](protocol-commands.md#tail) |
| `stop_tail()` | None | [TAIL](protocol-commands.md#tail) |

### Library Call Tracing (atrace)

amigactl includes a library call tracing system (atrace) that intercepts
and records AmigaOS library function calls in real time. This is the
project's most distinctive feature -- there is no equivalent tool for
AmigaOS.

Tracing requires the atrace kernel module to be loaded on the Amiga
(the daemon loads it automatically). The trace methods fall into three
tiers: control methods that configure tracing, streaming methods that
deliver trace events, and convenience methods that combine both.

#### Control Methods

| Method | Returns | Docs |
|--------|---------|------|
| `trace_status()` | `dict` -- see keys below | [TRACE STATUS](protocol-commands.md#trace-status) |
| `trace_enable(funcs=None)` | None | [TRACE ENABLE](protocol-commands.md#trace-enable) |
| `trace_disable(funcs=None)` | None | [TRACE DISABLE](protocol-commands.md#trace-disable) |

#### Streaming Methods

| Method | Returns | Docs |
|--------|---------|------|
| `trace_start(callback, ...)` | None -- **blocks, see below** | [TRACE START](protocol-commands.md#trace-start) |
| `trace_run(command, callback, ...)` | `dict` -- proc\_id, rc, stats | [TRACE RUN](protocol-commands.md#trace-run) |
| `stop_trace()` | None | [STOP](protocol-commands.md#stop) |
| `trace_events(...)` | generator of `dict` | [TRACE START](protocol-commands.md#trace-start) |

#### Convenience Methods

| Method | Returns | Docs |
|--------|---------|------|
| `trace_analyze(command, ...)` | `dict` -- events, stats, errors, file\_access, lib\_opens, rc | [TRACE RUN](protocol-commands.md#trace-run) |

#### Advanced / Raw Methods

These are for building interactive UIs (like the built-in trace viewer).
Most agents should use the callback-based or generator methods above.
Raw methods do not accept a `preset` parameter -- specify `lib` and
`func` filters individually instead.

| Method | Returns | Docs |
|--------|---------|------|
| `trace_start_raw(...)` | `RawTraceSession` | [TRACE START](protocol-commands.md#trace-start) |
| `trace_run_raw(command, ...)` | `(RawTraceSession, int or None)` | [TRACE RUN](protocol-commands.md#trace-run) |
| `send_filter(...)` | None -- fire-and-forget | [TRACE START](protocol-commands.md#trace-start) |
| `send_inline(cmd)` | None -- fire-and-forget | [TRACE START](protocol-commands.md#trace-start) |

---

#### `trace_status()`

Query atrace status. Works whether or not a trace stream is active.

```python
status = amiga.trace_status()
```

Returns a dict with these keys:

| Key | Type | When Present |
|-----|------|--------------|
| `loaded` | `bool` | Always |
| `enabled` | `bool` | Always |
| `patches` | `int` | When loaded |
| `events_produced` | `int` | When loaded |
| `events_consumed` | `int` | When loaded |
| `events_dropped` | `int` | When loaded |
| `buffer_capacity` | `int` | When loaded |
| `buffer_used` | `int` | When loaded |
| `filter_task` | `str` | When anchor version >= 2 |
| `noise_disabled` | `int` | When loaded |
| `anchor_version` | `int` | When loaded |
| `eclock_freq` | `int` | When anchor version >= 3 |
| `patch_list` | `list[dict]` | When loaded (list of `{"name": str, "enabled": bool}`) |

`filter_task` is a hex string like `"0x0e300200"` when a task filter is
active (during TRACE RUN), or `"0x00000000"` when no filter is set.

---

#### `trace_enable(funcs=None)`

Enable atrace globally, or enable specific functions by name.

```python
# Enable tracing globally
amiga.trace_enable()

# Enable specific functions only
amiga.trace_enable(funcs=["Open", "Close", "Lock"])
```

**Parameters:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `funcs` | `list[str]` or None | None | Function names to enable. If None, toggles global enable on. |

Raises `AmigactlError` if atrace is not loaded or a function name is not
recognized.

---

#### `trace_disable(funcs=None)`

Disable atrace globally, or disable specific functions by name.

```python
# Disable tracing globally (also drains the event buffer)
amiga.trace_disable()

# Disable specific functions only
amiga.trace_disable(funcs=["AllocMem", "FreeMem"])
```

**Parameters:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `funcs` | `list[str]` or None | None | Function names to disable. If None, toggles global enable off and drains buffer. |

Raises `AmigactlError` if atrace is not loaded or a function name is not
recognized.

---

#### `trace_start(callback, lib=None, func=None, proc=None, errors_only=None, preset=None)`

Start a trace event stream. **This method blocks the calling thread**
until the stream ends (via `stop_trace()` from another connection or
thread) or raises an exception.

The callback is called once per trace event with a dict argument.

```python
def on_event(event):
    if event.get("type") == "comment":
        print("# " + event["text"])
        return
    print("{} {} {} -> {}".format(
        event["time"], event["lib"], event["func"], event["retval"]))

try:
    amiga.trace_start(on_event, lib="dos")
except KeyboardInterrupt:
    amiga.stop_trace()
```

**Parameters:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `callback` | callable | required | Called with each event dict. |
| `lib` | `str` or None | None | Filter by library name (e.g. `"dos"`, `"exec"`). |
| `func` | `str` or None | None | Filter by function name (e.g. `"Open"`). Comma-separated for multiple. |
| `proc` | `str` or None | None | Filter by process name (e.g. `"myapp"`). |
| `errors_only` | `bool` or None | None | If True, only deliver events with error return values. |
| `preset` | `str` or None | None | Filter preset name from `FILTER_PRESETS` (see below). |

**Event dict keys:**

| Key | Type | Description |
|-----|------|-------------|
| `type` | `str` | `"event"` for trace events, `"comment"` for comment lines |
| `raw` | `str` | Raw event line from the server |
| `seq` | `int` | Sequence number (0 if unparseable) |
| `time` | `str` | Timestamp |
| `lib` | `str` | Library name (e.g. `"dos"`) |
| `func` | `str` | Function name (e.g. `"Open"`) |
| `task` | `str` | Task/process name |
| `args` | `str` | Function arguments (formatted) |
| `retval` | `str` | Return value (formatted) |
| `status` | `str` | `"O"` for success, `"E"` for error, `"-"` for neutral/void |

Comment events have only `type` and `text` keys.

**Critical behavioral notes:**
- `trace_start()` blocks the connection. No other commands can be sent
  on this connection while the stream is active (except inline commands
  like STOP and FILTER).
- To stop the stream, call `stop_trace()` **from a different connection**
  or catch `KeyboardInterrupt` and call `stop_trace()` on the same
  connection (the stream loop exits on interrupt before stop_trace sends
  STOP).
- Does NOT catch `KeyboardInterrupt` -- the caller must handle it.

---

#### `trace_run(command, callback, lib=None, func=None, errors_only=None, cd=None, preset=None)`

Launch a program on the Amiga and trace its library calls. The stream
auto-terminates when the process exits. **This method blocks** until the
process finishes or an error occurs.

Unlike `trace_start()`, this method automatically filters events to only
the launched process (no `proc` parameter needed).

```python
events = []

def collector(event):
    if event.get("type") != "comment":
        events.append(event)

result = amiga.trace_run("Dir SYS:", collector, lib="dos")
print("Exit code:", result["rc"])
print("Total events:", result["stats"]["total_events"])
print("Errors:", result["stats"]["errors"])
```

**Parameters:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `command` | `str` | required | AmigaOS command to execute. |
| `callback` | callable | required | Called with each event dict (same format as `trace_start`). |
| `lib` | `str` or None | None | Filter by library name. |
| `func` | `str` or None | None | Filter by function name. |
| `errors_only` | `bool` or None | None | If True, only deliver error events. |
| `cd` | `str` or None | None | Working directory for the command. |
| `preset` | `str` or None | None | Filter preset name from `FILTER_PRESETS`. |

**Return value (`dict`):**

| Key | Type | Description |
|-----|------|-------------|
| `proc_id` | `int` or None | Daemon-assigned process ID. |
| `rc` | `int` or None | Process exit code (parsed from the `PROCESS EXITED rc=N` comment). |
| `stats` | `dict` | Summary statistics (see below). |

**Stats dict:**

| Key | Type | Description |
|-----|------|-------------|
| `total_events` | `int` | Total trace events received (excluding comments). |
| `by_function` | `dict` | `{func_name: count}` -- call counts per function. |
| `errors` | `int` | Total error events. |
| `error_functions` | `dict` | `{func_name: error_count}` -- error counts per function. |

**Critical behavioral notes:**
- Does NOT accept a `proc` parameter. The daemon filters by the launched
  process automatically.
- Does NOT catch `KeyboardInterrupt`. Catch it and call `stop_trace()` to
  stop the trace and leave the process running.

---

#### `stop_trace()`

Send STOP during an active trace stream and drain remaining events.
After this call, the connection is back in normal command mode.

```python
# From a separate connection or after KeyboardInterrupt:
amiga.stop_trace()
```

**Important:** If the trace was started with `trace_start()`, you
typically need a **separate connection** to call `stop_trace()`, because
the first connection is blocked in the streaming loop. The exception is
calling it from a `KeyboardInterrupt` handler, where the stream loop has
already exited.

---

#### `trace_events(lib=None, func=None, proc=None, errors_only=None, preset=None)`

Generator that yields trace events as dicts. This is the most Pythonic
way to consume trace events -- use it in a `for` loop.

Internally, this spawns a background thread running `trace_start()` and
yields events from a queue. When the generator is closed (by breaking
out of the loop or garbage collection), it automatically stops the
trace using a **second connection** to the daemon.

```python
with AmigaConnection("192.168.6.200") as amiga:
    for event in amiga.trace_events(lib="dos"):
        if event.get("type") == "comment":
            continue
        print(event["func"], event["args"])
        if event["func"] == "Open" and "myfile" in event["args"]:
            break  # auto-stops the trace
```

**Parameters:** Same as `trace_start()` (lib, func, proc, errors_only,
preset).

**Important:** This method uses a second connection internally for
cleanup. The daemon must have a free client slot (max 8 simultaneous
clients by default).

---

#### `trace_analyze(command, max_events=10000, cd=None, lib=None, func=None, errors_only=False, preset=None)`

Run a command under trace and return a structured analysis. This is a
convenience method that combines `trace_run()` with post-processing to
produce a diagnostic summary. Best for programmatic analysis of what a
program does.

```python
result = amiga.trace_analyze("Dir SYS:")
print("Files accessed:", result["file_access"])
print("Libraries opened:", result["lib_opens"])
print("Errors:", len(result["errors"]))
print("Exit code:", result["rc"])
```

**Parameters:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `command` | `str` | required | AmigaOS command to trace. |
| `max_events` | `int` | 10000 | Maximum events to capture (safety limit). |
| `cd` | `str` or None | None | Working directory for the command. |
| `lib` | `str` or None | None | Filter by library name. |
| `func` | `str` or None | None | Filter by function name. |
| `errors_only` | `bool` | False | If True, only capture error events. |
| `preset` | `str` or None | None | Filter preset name from `FILTER_PRESETS`. |

**Return value (`dict`):**

| Key | Type | Description |
|-----|------|-------------|
| `events` | `list[dict]` | Captured event dicts (up to `max_events`). |
| `stats` | `dict` | Summary statistics (same as `trace_run`). |
| `errors` | `list[dict]` | Error event dicts only. |
| `file_access` | `list[str]` | Sorted list of unique files accessed (from Open, Lock, DeleteFile, CreateDir, SetProtection, GetDiskObject). |
| `lib_opens` | `list[str]` | Sorted list of unique libraries opened (from OpenLibrary calls). |
| `rc` | `int` or None | Process exit code. |

---

#### `trace_start_raw(lib=None, func=None, proc=None, errors_only=False)`

Start a trace stream and return a `RawTraceSession` without entering a
read loop. The caller uses `select()` on the session's socket and the
session's `TraceStreamReader` for non-blocking event parsing. This is
for building interactive UIs.

```python
with amiga.trace_start_raw(lib="dos") as session:
    # session.sock -- the socket, for select()
    # session.reader.try_read_event() -- returns dict, None, or False
    import select
    while True:
        readable, _, _ = select.select([session.sock], [], [], 0.1)
        if readable:
            event = session.reader.try_read_event()
            if event is False:
                break  # END received, stream finished
            if event is not None:
                print(event)
            # event is None means incomplete data, keep polling
        # Also drain any buffered events from previous recv()
        while session.reader.has_buffered_data():
            event = session.reader.drain_buffered()
            if event is False:
                break
            if event is not None:
                print(event)
```

Returns a `RawTraceSession` context manager that restores the socket
timeout on exit.

---

#### `trace_run_raw(command, lib=None, func=None, errors_only=False, cd=None)`

Start a TRACE RUN stream and return `(RawTraceSession, proc_id)` without
entering a read loop. Same non-blocking pattern as `trace_start_raw()`.

```python
session, proc_id = amiga.trace_run_raw("Dir SYS:", lib="dos")
with session:
    print("Tracing process", proc_id)
    # ... non-blocking event loop ...
```

---

#### `send_filter(lib=None, func=None, proc=None, raw=None)`

Send a FILTER command during an active trace stream to change which
events are delivered. Fire-and-forget -- no response is expected.

```python
# Change filter to only show dos.library calls
amiga.send_filter(lib="dos")

# Clear all filters
amiga.send_filter()

# Raw filter string for advanced syntax
amiga.send_filter(raw="LIB=dos,exec -FUNC=AllocMem")
```

**Parameters:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `lib` | `str` or None | None | Library name filter. |
| `func` | `str` or None | None | Function name filter. |
| `proc` | `str` or None | None | Process name filter. |
| `raw` | `str` or None | None | Raw filter string. When provided, lib/func/proc are ignored. |

Handles non-blocking sockets: temporarily switches to blocking mode for
the send, then restores non-blocking mode.

---

#### `send_inline(cmd)`

Send a bare inline command during an active trace stream (e.g.,
`"TIER 2"`). Fire-and-forget -- no response is expected.

```python
amiga.send_inline("TIER 2")
```

Handles non-blocking sockets the same way as `send_filter()`.

---

#### `FILTER_PRESETS`

The `FILTER_PRESETS` dict (importable from `amigactl`) provides named
filter configurations for common tracing scenarios. Pass a preset name
as the `preset` parameter to `trace_start()`, `trace_run()`,
`trace_events()`, or `trace_analyze()`.

```python
from amigactl import FILTER_PRESETS

# See available presets
print(list(FILTER_PRESETS.keys()))
```

| Preset | Traces | Key Functions |
|--------|--------|---------------|
| `"file-io"` | dos.library file operations | Open, Close, Lock, DeleteFile, CreateDir, Rename, MakeLink, SetProtection |
| `"lib-load"` | Library/device loading | OpenLibrary, OpenDevice, OpenResource, CloseLibrary, CloseDevice |
| `"network"` | All bsdsocket.library calls | (all functions in bsdsocket) |
| `"ipc"` | Inter-process communication | FindPort, GetMsg, PutMsg, ObtainSemaphore, ReleaseSemaphore, AddPort, WaitPort |
| `"errors-only"` | All libraries, errors only | (any function that returns an error) |
| `"memory"` | Memory allocation | AllocMem, FreeMem, AllocVec, FreeVec |
| `"window"` | Window/screen operations | OpenWindow, CloseWindow, OpenScreen, CloseScreen, OpenWindowTagList, OpenScreenTagList, ActivateWindow |
| `"icon"` | All icon.library calls | (all functions in icon) |

Usage with trace methods:

```python
# Trace only file I/O operations
result = amiga.trace_analyze("Dir SYS:", preset="file-io")

# Stream only error returns
for event in amiga.trace_events(preset="errors-only"):
    print(event["func"], event["retval"])
```

Presets can be combined with explicit parameters. Explicit `lib`, `func`,
or `errors_only` parameters take precedence over the preset values.

## Amiga Domain Knowledge

Context that an AI agent likely does not have. Understanding this is
essential for correct interaction with AmigaOS.

### Path conventions

- Paths use `volume:path/to/file` format (e.g., `SYS:S/Startup-Sequence`)
- Every absolute path starts with a volume or assign name followed by a
  colon. There is no leading `/` -- `SYS:C/Dir` is correct, `/C/Dir` is
  not
- `/` alone after a path component means parent directory (like `..`)
- The daemon does NOT translate `..` -- use `/` or resolve client-side
- Path matching is case-insensitive on most Amiga filesystems, but
  case-preserving. `SYS:s/startup-sequence` works, but `stat()` returns
  the name as stored on disk

### Common volumes

| Volume | Purpose | Unix analogy |
|--------|---------|--------------|
| `SYS:` | Boot volume | `/` |
| `RAM:` | RAM disk (fast, volatile, lost on reboot) | tmpfs |
| `T:` | Temporary directory (assign, usually `RAM:T`) | `/tmp` |
| `S:` | Startup scripts (usually `SYS:S`) | `/etc/init.d` |
| `C:` | System commands (usually `SYS:C`) | `/usr/bin` |
| `LIBS:` | Shared libraries (usually `SYS:Libs`) | `/usr/lib` |
| `DEVS:` | Device drivers (usually `SYS:Devs`) | `/dev` |
| `WORK:` | Secondary storage (convention) | `/home` |

### Environment variable persistence

AmigaOS has a two-tier environment variable system unlike anything on
Unix:

- `ENV:` -- RAM-based, per-session. Variables here are available to
  running programs but lost on reboot.
- `ENVARC:` -- Disk-based, persistent. Variables here survive reboots.
  On boot, `ENVARC:` is copied to `ENV:`.

`setenv(name, value)` writes to both `ENV:` and `ENVARC:` (persistent).
`setenv(name, value, volatile=True)` writes to `ENV:` only (lost on
reboot). The `volatile` flag is for temporary values you do not want to
survive a reboot.

### Text encoding

AmigaOS uses ISO-8859-1 (Latin-1), not UTF-8. This applies to
filenames, file contents, command output, and ARexx results.

- `read()` and `tail()` callbacks return raw `bytes` -- decode with
  `"iso-8859-1"`
- `execute()` and `arexx()` return pre-decoded `str`
- `write()` takes `bytes` -- encode with `"iso-8859-1"`
- Trace event dicts contain pre-decoded `str` values

### Return codes

AmigaOS uses different severity levels than Unix:

| Code | Meaning | Equivalent |
|------|---------|------------|
| 0 | Success | same as Unix |
| 5 | WARN -- non-fatal warning | -- |
| 10 | ERROR -- operation failed | non-zero exit |
| 20 | FAIL -- serious failure | non-zero exit |

`execute()` returns the rc as-is. Check for `rc != 0` or `rc >= 10`
depending on how strict you want to be.

### Protection bits

AmigaOS protection bits are inverted for RWED: a SET bit means DENIED
(opposite of Unix). The `protect()` method returns/accepts raw hex.
See [PROTECT](protocol-commands.md#protect) for the bit layout.

**Important:** Protection bits on AmigaOS are advisory, not enforced
by the filesystem kernel. Any program can read or write any file
regardless of its protection bits. The only exception is the delete-
protect bit (bit 0), which the filesystem does enforce. Do not rely on
protection bits for access control.

### File comments

AmigaOS supports a per-file comment string (called a "filenote") stored
in the filesystem metadata. There is no Unix equivalent. Comments are
up to 79 characters, cannot contain tab characters (0x09), and are
returned by `stat()` in the `comment` key. Use `setcomment(path, text)`
to set and `setcomment(path, "")` to clear.

### Assign types

AmigaOS distinguishes between **volumes** and **assigns**. A volume
(e.g., `SYS:`, `RAM:`, `WORK:`) is a mounted filesystem.  An assign
(e.g., `C:`, `LIBS:`, `T:`) is a logical name that maps to one or more
directories on volumes. `volumes()` lists the former; `assigns()` lists
the latter.

The `assign()` method supports three modes:

| Mode | Usage | Behavior |
|------|-------|----------|
| Lock (default) | `assign("TEST:", "WORK:test")` | Locks the path immediately. Fails if path does not exist. |
| Late | `assign("TEST:", "WORK:test", mode="late")` | Path is not resolved until first access. Useful for paths on removable media. |
| Add | `assign("TEST:", "WORK:extra", mode="add")` | Adds a directory to an existing multi-directory assign. The assign must already exist. |

Remove an assign by calling `assign("TEST:")` with no path.

## Key Patterns

Patterns that are non-obvious from the method signatures.

### Text file round-trip

```python
data = amiga.read(path)
text = data.decode("iso-8859-1")
# ... modify text ...
amiga.write(path, text.encode("iso-8859-1"))
```

### Partial file read

```python
# Read 100 bytes starting at offset 1000
data = amiga.read(path, offset=1000, length=100)
```

The `offset` and `length` parameters are optional.  If only `offset` is
given, the read continues to end of file.  If only `length` is given,
the read starts from the beginning.

### Async process polling

```python
pid = amiga.execute_async("copy SYS:C WORK:backup ALL")
import time
while True:
    info = amiga.procstat(pid)
    if info["status"] != "running":
        break
    time.sleep(2)
print("Exit code:", info["rc"])
```

### Tail streaming

```python
def on_data(chunk):
    print(chunk.decode("iso-8859-1"), end="")

amiga.tail("RAM:logfile.txt", on_data)
# Blocks until file deletion or stop_tail() from another thread.
# Use KeyboardInterrupt (Ctrl-C) to break out.
```

### Trace a program and analyze results

```python
result = amiga.trace_analyze("Dir SYS:")
print("Exit code:", result["rc"])
print("Total calls:", result["stats"]["total_events"])
for func, count in sorted(result["stats"]["by_function"].items(),
                           key=lambda x: -x[1]):
    print("  {} -- {} calls".format(func, count))
if result["errors"]:
    print("Errors:")
    for err in result["errors"]:
        print("  {} {} -> {}".format(
            err["lib"], err["func"], err["retval"]))
print("Files accessed:", result["file_access"])
print("Libraries opened:", result["lib_opens"])
```

### Trace streaming with generator

```python
with AmigaConnection("192.168.6.200") as amiga:
    for event in amiga.trace_events(preset="file-io"):
        if event.get("type") == "comment":
            continue
        print("{} {}.{} -> {}".format(
            event["task"], event["lib"], event["func"],
            event["retval"]))
```

### Trace with two connections

When using `trace_start()` (not `trace_events()`), you need two
connections: one for the blocking trace stream, one for control.
Unlike TAIL's STOP (which must be sent on the same connection),
a trace STOP can be sent from any connection to the daemon.

```python
import threading

trace_conn = AmigaConnection("192.168.6.200")
trace_conn.connect()

ctrl_conn = AmigaConnection("192.168.6.200")
ctrl_conn.connect()

def on_event(event):
    if event.get("type") != "comment":
        print(event["func"])

# Start trace in a background thread
thread = threading.Thread(
    target=trace_conn.trace_start,
    args=(on_event,),
    kwargs={"lib": "dos"})
thread.start()

import time
time.sleep(10)  # collect events for 10 seconds

# Stop from the control connection
ctrl_conn.stop_trace()
thread.join()

trace_conn.close()
ctrl_conn.close()
```

### Error recovery

```python
from amigactl import AmigaConnection, AmigactlError, NotFoundError

try:
    data = amiga.read(path)
except NotFoundError:
    amiga.write(path, b"")  # create if missing
except AmigactlError as e:
    print(f"Amiga error: {e.message} (code {e.code})")
```

### Deploy and reboot

Writing a binary and rebooting is the standard deployment pattern. The
FFS buffer cache can lose recent writes if the reboot happens too
quickly (see Gotcha 10). Sleep before rebooting to let the cache flush.

```python
import time

amiga.write("C:amigactld", new_binary)
time.sleep(5)  # let FFS buffer cache flush to disk
amiga.reboot()
```

### Reconnect after reboot

After `reboot()`, the connection is dead. The Amiga takes 15-45 seconds
to boot depending on hardware and startup sequence. Poll `connect()`
in a retry loop.

```python
import time
from amigactl import AmigaConnection

amiga.reboot()
time.sleep(20)  # minimum boot time

for attempt in range(12):
    try:
        amiga = AmigaConnection(host)
        amiga.connect()
        break
    except OSError:
        time.sleep(5)
else:
    raise RuntimeError("Amiga did not come back after reboot")

print(amiga.version())
# Remember to call amiga.close() when done, or use a context manager.
```

### Verify file integrity after write

Use `checksum()` with Python's `zlib.crc32()` to verify a file was
written correctly.

```python
import zlib

data = open("local_file", "rb").read()
amiga.write("C:my_program", data)
result = amiga.checksum("C:my_program")
local_crc = "{:08x}".format(zlib.crc32(data) & 0xFFFFFFFF)
assert result["crc32"] == local_crc, "CRC mismatch"
assert result["size"] == len(data), "Size mismatch"
```

## Error Handling

All exception classes are importable from `amigactl`.

- `AmigactlError` -- base class (`.code: int`, `.message: str`)
  - `CommandSyntaxError` (100) -- malformed/unknown command
  - `NotFoundError` (200) -- file/path/port not found
  - `PermissionDeniedError` (201) -- access denied
  - `AlreadyExistsError` (202) -- already exists
  - `RemoteIOError` (300) -- I/O error on Amiga side
  - `RemoteTimeoutError` (400) -- operation timed out
  - `InternalError` (500) -- daemon internal error
- `ProtocolError` -- wire protocol violation (client-side)
  - `ServerError` -- server returned ERR status
  - `BinaryTransferError` -- ERR during binary transfer

## Raw Protocol Access

The `amigactl.protocol` module exposes the wire protocol primitives used
internally by `AmigaConnection`. Import it for edge cases not covered
by the high-level API (e.g., reading the `truncated` field from ENV
responses, or implementing custom command sequences).

```python
from amigactl.protocol import (
    ENCODING,           # "iso-8859-1"
    read_line,          # read_line(sock) -> str
    read_response,      # read_response(sock) -> (status, info, payload)
    send_command,       # send_command(sock, cmd) -> None
    recv_exact,         # recv_exact(sock, nbytes) -> bytes
    read_binary_response,  # read DATA/END chunks after OK
    read_exec_response,    # read EXEC-style OK rc=N + DATA/END
    send_data_chunks,      # send DATA/END chunks to server
    ProtocolError,         # wire protocol violation
    ServerError,           # server returned ERR status
    BinaryTransferError,   # ERR during binary transfer
)
```

`read_response()` returns `(status, info, payload_lines)` where
`status` is `"OK"` or `"ERR"`, `info` is the remainder of the status
line, and `payload_lines` is a list of dot-unstuffed strings. This gives
access to fields that `AmigaConnection` methods may not expose (e.g.,
the `truncated=true` line in an ENV response).

See [Wire Protocol Specification](protocol.md) for the wire format specification.

## Gotchas

1. **ISO-8859-1, not UTF-8.** All text on AmigaOS is Latin-1. See
   "Text encoding" above.

2. **Synchronous exec blocks the connection.** `execute()` blocks the
   daemon's event loop for that client. Set `timeout` for safety.

3. **KILL is dangerous.** `kill()` uses RemTask() which leaks resources
   (open files, memory). Always try `signal(proc_id, "CTRL_C")` first.

4. **TAIL occupies the connection.** No other commands can be sent while
   `tail()` is active (except `stop_tail()`). Use a separate connection
   for concurrent work. `tail()` blocks the calling thread.

5. **TRACE START occupies the connection.** Like `tail()`,
   `trace_start()` blocks the connection for the duration of the stream.
   Only inline commands (STOP, FILTER) can be sent. Use `stop_trace()`
   from a **different connection** to end the stream. `trace_events()`
   handles this automatically with a second connection. `trace_run()`
   and `trace_analyze()` auto-terminate when the process exits.

6. **Max 8 simultaneous clients.** Close connections when done.
   `trace_events()` uses two connections internally.

7. **No .. in paths.** Use `/` for parent directory (Amiga convention).

8. **SETENV VOLATILE is a keyword.** You cannot set a variable named
   "VOLATILE" via `setenv("VOLATILE", "value")`. The daemon interprets
   it as the volatile mode flag. Use `execute("setenv VOLATILE value")`
   as a workaround if this edge case matters.

9. **APPEND requires an existing file.** Unlike `write()`, which creates
   a new file, `append()` fails with `NotFoundError` if the file does
   not exist.  Create it with `write(path, b"")` first if needed.

10. **checksum() returns hex string, not int.** The `crc32` field is an
    8-character lowercase hex string (e.g., `"a1b2c3d4"`), not a Python
    integer.  Use `int(result["crc32"], 16)` to convert if needed.

11. **reboot() can lose recent writes.** AmigaOS FFS uses a buffer cache.
    Writes may not be flushed to disk when `reboot()` calls ColdReboot().
    Sleep at least 5 seconds between the last `write()` and `reboot()`.
    See the "Deploy and reboot" pattern above.

12. **RAM: is volatile.** Everything on `RAM:` (and `T:`, which usually
    points to `RAM:T`) is lost on reboot. Do not store anything there
    that must survive a restart.

13. **env() silently truncates large values.** The daemon reads
    environment variables into a 4096-byte buffer. Values longer than
    4095 characters are truncated. The response includes a
    `truncated=true` field when this happens, but `env()` returns only
    the value string and discards it. Use the raw protocol
    (`read_response()`) if you need to detect truncation.

14. **stop_trace() is not stop_tail().** These are separate methods for
    separate streaming protocols. `stop_tail()` stops a TAIL stream;
    `stop_trace()` stops a TRACE stream. Using the wrong one will cause
    protocol errors.
