# Event Filtering

atrace generates a high volume of events -- every traced library call
across all running processes produces an event. Without filtering, a
busy system can generate hundreds of events per second, making it
difficult to find the calls that matter. Filtering narrows the event
stream to show only what you need.

This document covers all filtering mechanisms: daemon-side wire protocol
filters, client-side noise suppression, filter presets, mid-stream
filter changes, and automatic task filtering for TRACE RUN. For
CLI syntax details, see [cli-reference.md](cli-reference.md). For
per-function error classification and tier membership, see
[traced-functions.md](traced-functions.md).


## Filter Architecture

Filtering operates at two layers, each serving a different purpose:

**Daemon-side filters** are applied before events are transmitted over
the network. They reduce network traffic and client processing load.
These filters are set when the trace session starts (via `TRACE START`
or `TRACE RUN` arguments) and can be changed mid-stream with the
`FILTER` command. The daemon evaluates each event against the client's
filter state in `trace_filter_match()` and only transmits events that
pass all criteria.

**Client-side filters** are applied after events are received. They
provide instant toggling without a protocol round-trip. The interactive
TUI viewer (available in `amigactl shell`) uses client-side filtering
for its toggle grid and noise suppression. Changes take effect
immediately on the next rendered frame.

Daemon-side filtering is strictly preferable when you know what you
want in advance: it prevents unwanted events from consuming ring buffer
bandwidth and network capacity. Client-side filtering is useful for
interactive exploration where you want to toggle visibility without
restarting the stream.

### How Filters Combine

All daemon-side filters are AND-combined: every active filter must
match for an event to pass. Within a multi-value filter (e.g.,
`LIB=dos,exec`), values are OR-combined: the event's library must
match any one of the listed values.

For example, `LIB=dos ERRORS` means "show dos.library calls that
returned errors." Both conditions must be satisfied. An exec.library
call, even one that returned an error, is filtered out because it does
not match the LIB filter.


## Library Filter (LIB=)

Filters events by the originating library.

### Syntax

| Form | Meaning |
|------|---------|
| `LIB=dos` | Show only dos.library calls |
| `LIB=dos,exec` | Show dos.library and exec.library calls |
| `-LIB=bsdsocket` | Show all libraries except bsdsocket.library |

### Behavior

- Library names are case-insensitive.
- The `.library`, `.device`, and `.resource` suffixes are automatically
  stripped. `LIB=dos.library` is equivalent to `LIB=dos`.
- Valid library names: `exec`, `dos`, `intuition`, `bsdsocket`,
  `graphics`, `icon`, `workbench`.
- Unknown library names match nothing (the filter silently produces an
  empty result set).
- A single-value `LIB=` uses the simple filter path. Comma-separated
  values or the `-LIB=` blacklist prefix activate the extended filter
  path. Both produce the same results; the distinction is an internal
  optimization.

**Note:** The comma-separated form is only supported via the FILTER inline command, which uses the extended parser. TRACE START and TRACE RUN use the simple parser, which reads the entire value including commas as a single token and will match nothing. Use the FILTER command or the Python API for multi-library filtering.

### CLI

```
amigactl trace start --lib dos
amigactl trace start --lib bsdsocket
```

The `--lib` flag accepts a single library name. For multi-library
filtering, use the Python API or the TUI toggle grid.


## Function Filter (FUNC=)

Filters events by function name.

### Syntax

| Form | Meaning |
|------|---------|
| `FUNC=Open` | Show only calls to Open (any library) |
| `FUNC=dos.Open` | Show only calls to dos.Open specifically |
| `FUNC=Open,Lock,Close` | Show calls to Open, Lock, or Close |
| `-FUNC=AllocMem,GetMsg` | Show all functions except AllocMem and GetMsg |

**Note:** The comma-separated form is only supported via the FILTER inline command, which uses the extended parser. TRACE START and TRACE RUN use the simple parser, which reads the entire value including commas as a single token and will match nothing. Use the FILTER command or the Python API for multi-function filtering.

### Library-Scoped Names

The `lib.func` dot-separated syntax disambiguates functions that share
a name or LVO offset across libraries. While no currently traced
functions share a name, LVO collisions do exist: `exec.OpenDevice` and
`dos.MakeLink` both use LVO -444. When `FUNC=` matches by function
name, it resolves both the LVO and the library ID internally, so this
collision does not cause incorrect matches. The library-scoped syntax
is primarily useful for clarity and for the toggle grid's internal
FILTER commands.

### Behavior

- Function name matching is case-insensitive.
- Unknown function names match nothing (silently filtered out).
- In the simple parser (TRACE START / TRACE RUN), `FUNC=` auto-resolves
  the function's library and overwrites any earlier `LIB=` setting, so
  `LIB=exec FUNC=Open` actually matches `dos.Open` -- the FUNC lookup
  determines the library, and the explicit `LIB=exec` is silently
  replaced. This overwrite also occurs in FILTER commands when the
  string contains no commas or blacklist syntax, since such strings
  are delegated to the simple parser. The independent AND combination
  of LIB= and FUNC= only exists when the extended parser is active
  (triggered by commas, `-LIB=`, `-FUNC=`, `ENABLE=`, or
  `DISABLE=` in the filter string). For example,
  `FILTER LIB=exec FUNC=Open,Lock` uses the extended parser because
  of the comma, and correctly matches nothing because neither Open nor
  Lock belongs to exec.library.
- A single-value `FUNC=` (without commas or blacklist prefix) uses the
  simple filter path. Comma-separated values or `-FUNC=` activate the
  extended filter path.

### CLI

```
amigactl trace start --func Open
```

The `--func` flag accepts a single function name. The simple parser
treats commas as part of the value, so `--func Open,Lock,Close` would
be sent as `FUNC=Open,Lock,Close` and match nothing. For multi-function
filtering, use the Python API or send a FILTER command during an active
session.


## Process Filter (PROC=)

Filters events by the name of the calling task or process.

### Syntax

| Form | Meaning |
|------|---------|
| `PROC=myapp` | Show only events from tasks whose name contains "myapp" |

### Behavior

- The match is a case-insensitive substring search on the process name.
- CLI process names include a `[N]` prefix (e.g., `[3] Shell Process`).
  The PROC filter matches against the base name only, stripping the
  `[N]` prefix before comparison. `PROC=Shell` matches
  `[3] Shell Process`, but `PROC=3` does not.
- Multi-value PROC (comma-separated) is not supported on the daemon
  side. The TUI toggle grid provides client-side process filtering
  with multi-process support.
- **Not valid with TRACE RUN.** The daemon returns
  `ERR 100 PROC filter not valid for TRACE RUN` if PROC= is included
  in a TRACE RUN command. Task filtering is automatic in TRACE RUN;
  see [Automatic Task Filtering (TRACE RUN)](#automatic-task-filtering-trace-run).

### CLI

```
amigactl trace start --proc myapp
```

The `--proc` flag is only available on `trace start`, not on
`trace run`.


## Error Filter (ERRORS)

Shows only calls that returned error values. This is one of the most
useful filters for debugging: it suppresses the majority of successful
calls and highlights failures.

### Syntax

| Form | Meaning |
|------|---------|
| `ERRORS` | Show only calls classified as errors |

### Error Classification

Not all functions define "error" the same way. A NULL return from
`Open()` is an error, but a NULL return from `GetMsg()` simply means
the message port is empty. The daemon classifies each function's
return value using one of eight error check types:

| Type | Condition | Error When | Example Functions |
|------|-----------|------------|-------------------|
| `ERR_CHECK_NULL` | `retval == 0` | Return is NULL/FALSE | Open, Lock, OpenLibrary |
| `ERR_CHECK_NZERO` | `retval != 0` | Return is non-zero | OpenDevice, DoIO, WaitIO |
| `ERR_CHECK_VOID` | -- | Never (void function) | PutMsg, FreeMem, CloseWindow |
| `ERR_CHECK_ANY` | -- | Always shown | Wait, CheckIO, AbortIO |
| `ERR_CHECK_NONE` | -- | Never an error | GetMsg, MatchToolValue, Execute |
| `ERR_CHECK_RC` | `retval != 0` | Non-zero return code | RunCommand, SystemTagList |
| `ERR_CHECK_NEGATIVE` | `(LONG)retval < 0` | Negative return | GetVar |
| `ERR_CHECK_NEG1` | `retval == -1` | Return is -1 | socket, bind, connect, recv, Read, Write, Seek |

These types are defined as constants `ERR_CHECK_*` in the daemon's
`trace.c` and assigned per-function in the `func_table[]` array.

**How each type works with the ERRORS filter:**

- **ERR_CHECK_NULL**: The most common type. Most AmigaOS functions
  return a pointer or a BOOL where zero indicates failure. Open,
  Lock, OpenLibrary, AllocMem, and many others use this convention.

- **ERR_CHECK_NZERO**: Inverse of NULL. Used by functions where zero
  means success (e.g., `OpenDevice()` returns 0 on success and a
  non-zero error code on failure).

- **ERR_CHECK_VOID**: Void functions have no return value to classify.
  They are always excluded from ERRORS output. Functions like PutMsg,
  FreeMem, CloseWindow, and ObtainSemaphore fall in this category.

- **ERR_CHECK_ANY**: Functions with no clear error convention are always
  included in ERRORS output. This ensures they are visible when
  debugging failures, even though not every call is necessarily an
  error.

- **ERR_CHECK_NONE**: Functions where a "zero" return is a normal,
  expected result. `GetMsg()` returning NULL just means no messages
  are pending. `MatchToolValue()` returning FALSE means the value
  did not match, which is a normal outcome. These are always excluded
  from ERRORS output.

- **ERR_CHECK_RC**: Return code convention where zero means success and
  any non-zero value is a failure code. Used by `RunCommand()` and
  `SystemTagList()`.

- **ERR_CHECK_NEGATIVE**: Functions where negative values indicate
  failure but zero and positive values are valid results. `GetVar()`
  returns the variable length on success (>= 0) or -1 on failure.

- **ERR_CHECK_NEG1**: Functions that return -1 on error and non-negative
  values on success. All bsdsocket.library functions use this BSD
  convention. DOS I/O functions `Read()`, `Write()`, and `Seek()` also
  use -1 as their error indicator.

For the complete mapping of each traced function to its error check
type, see [traced-functions.md](traced-functions.md).

Events from functions not present in the daemon `func_table[]` (e.g.,
functions added to stubs but not yet registered for error classification)
pass the ERRORS filter unconditionally -- they are always included in
errors-only output.

### CLI

```
amigactl trace start --errors
amigactl trace run --errors -- List SYS:C
```

The `--errors` flag can be combined with other filters:

```
amigactl trace start --lib dos --errors
```


## Extended Filter Syntax (FILTER Command)

The FILTER command changes filters during an active trace session
without restarting the stream. It supports the full extended filter
syntax including comma-separated lists, blacklists, and ENABLE/DISABLE
directives.

**Important:** Each FILTER command replaces the entire filter state. Sending `FILTER LIB=dos` clears any previous FUNC=, PROC=, or ERRORS filters. To combine multiple filter criteria, include all of them in a single FILTER command (e.g., `FILTER LIB=dos ERRORS`). A bare `FILTER` with no arguments clears all filters.

### Syntax

The FILTER command is sent as an inline command on the trace
connection (the same connection receiving DATA chunks). There is no OK/ERR response line. Instead, the daemon emits a
comment line confirming the new filter state.

| Form | Effect |
|------|--------|
| `FILTER LIB=dos,exec` | Whitelist: show only dos and exec calls |
| `FILTER -FUNC=AllocMem,GetMsg` | Blacklist: hide AllocMem and GetMsg |
| `FILTER LIB=dos -FUNC=Close ERRORS` | Combined: dos calls except Close, errors only |
| `FILTER PROC=myapp` | Process filter (single substring) |
| `FILTER` | Clear all filters (show everything) |

### Extended vs. Simple Parsing

The daemon auto-detects whether to use simple or extended parsing:

- If the arguments contain commas, `-LIB=`, `-FUNC=`, `ENABLE=`, or
  `DISABLE=`, the extended parser is used. The daemon checks for
  `-LIB=` and `-FUNC=` only at word boundaries (after whitespace or at
  the start of the string), so hyphens in process names (e.g.,
  `PROC=my-app`) do not accidentally trigger extended filter mode.
- Otherwise, the simple parser handles it for backward compatibility.
- The result is the same for simple cases; the auto-detection is
  transparent.

### ENABLE= and DISABLE= Directives

The FILTER command can also modify the global patch enable/disable
state:

| Form | Effect |
|------|--------|
| `ENABLE=AllocMem,FreeMem` | Enable specific function patches |
| `DISABLE=GetMsg,PutMsg` | Disable specific function patches |

**Important:** ENABLE= and DISABLE= modify the global `patches[].enabled`
state in the atrace anchor, which affects all connected trace clients.
This differs from LIB=, FUNC=, and PROC=, which are per-session
filters that only affect the client that sent the FILTER command.

### Confirmation

After processing a FILTER command, the daemon sends a comment line
to the stream showing the new filter state:

```
# filter: tier=basic, LIB=dos -FUNC=dos.Close ERRORS
```

### Python API

```python
# Simple filters
conn.send_filter(lib="dos")
conn.send_filter(func="Open")
conn.send_filter(proc="myapp")

# Extended syntax via raw parameter
conn.send_filter(raw="LIB=dos,exec -FUNC=AllocMem")

# Clear all filters
conn.send_filter()
```

The ERRORS flag can also be sent mid-stream using `send_filter()`:

```python
# Enable error-only filtering mid-stream
conn.send_filter(raw="ERRORS")

# Combine with library filter
conn.send_filter(raw="LIB=dos ERRORS")
```

The `send_filter()` method is fire-and-forget: it sends the command
and returns immediately without waiting for a response. On non-blocking
sockets (as used by the interactive viewer), it temporarily switches
to blocking mode with a 2-second timeout for the send.


## Tier-Based Filtering

Output tiers control which functions are enabled at the patch level.
Unlike LIB/FUNC/PROC filters (which suppress matching events after
they are generated), tiers control whether the stub code records
events at all.

### Tier Levels

| Level | Name | Functions | Description |
|-------|------|-----------|-------------|
| 1 | Basic | 57 | Core diagnostic events (default) |
| 2 | Detail | +13 (70 total) | Deeper debugging |
| 3 | Verbose | +3 (73 total) | High-volume burst events |
| -- | Manual | 26 | Never auto-enabled |

Tiers are cumulative: Detail includes all Basic functions plus its own.
Verbose includes Basic and Detail plus its own.

Manual-tier functions (26 functions including AllocMem, FreeMem, GetMsg,
PutMsg, Read, Write, send, recv, Wait, Signal, and others) fire at
extreme rates on an active system. They are never auto-enabled by any
tier and must be enabled explicitly using `trace enable <name>` or the
ENABLE= directive.

### Switching Tiers

- **CLI:** `--basic`, `--detail`, or `--verbose` flags on
  `trace start` or `trace run`.
- **TUI:** Press `1` (Basic), `2` (Detail), or `3` (Verbose) during
  a trace session.
- **Wire protocol:** The client sends an inline `TIER <1|2|3>` command
  on the trace connection.

For full tier membership lists and switching behavior, see
[output-tiers.md](output-tiers.md).


## Filter Presets (Python API Only)

The Python client library defines named filter presets that combine
lib, func, and errors_only parameters into convenient shortcuts.
Presets exist only in the Python API (`FILTER_PRESETS` dict in
`amigactl/__init__.py`). There is no `--preset` CLI flag.

### Available Presets

| Preset | Expands To | Purpose |
|--------|------------|---------|
| `file-io` | `LIB=dos FUNC=Open,Close,Lock,DeleteFile,CreateDir,Rename,MakeLink,SetProtection` | File system operations |
| `lib-load` | `FUNC=OpenLibrary,OpenDevice,OpenResource,CloseLibrary,CloseDevice` | Library lifecycle |
| `network` | `LIB=bsdsocket` | BSD socket calls |
| `ipc` | `FUNC=FindPort,GetMsg,PutMsg,ObtainSemaphore,ReleaseSemaphore,AddPort,WaitPort` | Inter-process communication |
| `errors-only` | `ERRORS` | Error returns across all functions |
| `memory` | `FUNC=AllocMem,FreeMem,AllocVec,FreeVec` | Memory allocation |
| `window` | `FUNC=OpenWindow,CloseWindow,OpenScreen,CloseScreen,OpenWindowTagList,OpenScreenTagList,ActivateWindow` | Window/screen management |
| `icon` | `LIB=icon` | Icon operations |

### Python API Usage

```python
from amigactl import AmigaConnection

with AmigaConnection("192.168.6.200") as conn:
    # Using a preset
    conn.trace_start(callback, preset="file-io")

    # Preset with trace_run
    conn.trace_run("List SYS:", callback, preset="network")

    # Preset with additional overrides (explicit args take precedence)
    conn.trace_start(callback, preset="file-io", errors_only=True)
```

Preset resolution works by filling in `lib`, `func`, and `errors_only`
parameters from the preset dict, but only when the caller has not
provided an explicit value. Explicit arguments always take precedence
over preset values.

### CLI Equivalents

Presets that map to a single `--lib` or `--errors` filter have direct
CLI equivalents. Multi-function presets (file-io, lib-load, ipc, memory,
window) have no direct CLI equivalent because the simple parser does
not support comma-separated `--func` values. Use the Python API for
those presets.

```bash
# network preset equivalent
amigactl trace start --lib bsdsocket

# errors-only preset equivalent
amigactl trace start --errors

# icon preset equivalent
amigactl trace start --lib icon
```

For programmatic preset usage, see [python-api.md](python-api.md).


## Automatic Task Filtering (TRACE RUN)

TRACE RUN combines program launch with trace streaming and provides
automatic task-level filtering so only events from the launched process
appear in the output. No PROC= filter is needed or accepted.

### How It Works

When the daemon processes a `TRACE RUN` command, it:

1. Launches the program via `CreateNewProcTags()` under `Forbid()`.
2. Records the new process's Task pointer in `trace_state.run_task_ptr`.
3. Sets `anchor->filter_task` to the launched process's Task pointer.
   This is a global field in the atrace anchor that the stub code checks
   before writing events to the ring buffer.

With `filter_task` set, the stubs compare `SysBase->ThisTask` against
the filter value and skip event recording entirely for non-matching
tasks. This is stub-level filtering -- events from other tasks never
enter the ring buffer, which is far more efficient than daemon-side
post-filtering.

### Limitations

- `filter_task` is a single global value. Only one TRACE RUN session
  can use stub-level filtering at a time. If a second TRACE RUN starts
  while the first is active, it falls back to daemon-side filtering
  only (the ring buffer receives events from all tasks, and the daemon
  matches events by the run_task_ptr field). This fallback is more
  susceptible to ring buffer overflow under heavy load.

- Manual-tier functions (AllocMem, GetMsg, Read, Write, etc.) are not
  auto-enabled during TRACE RUN. Even with stub-level task filtering,
  a single target process can generate ~10,000 events in 0.5 seconds
  from manual-tier functions, overwhelming the ring buffer. Enable
  specific manual-tier functions explicitly if needed.

### Cleanup

When the traced process exits, the daemon:

1. Drains remaining events from the ring buffer for that task.
2. Sends a `# PROCESS EXITED rc=N` comment and an END marker.
3. Clears `anchor->filter_task` so other TRACE RUN sessions can use
   stub-level filtering.

If the client disconnects before the process exits, the daemon clears
`filter_task` during disconnect cleanup. Orphaned `filter_task` values
(from daemon restarts or dead clients) are detected and cleared when
the next TRACE RUN starts.


## Client-Side Noise Filter

The interactive TUI viewer suppresses repetitive shell initialization
events that clutter trace output. These events are individually
toggleable in the NOISE category of the filter grid.

### Suppressed Items

| Item | Suppresses |
|------|------------|
| `process` | SetVar/GetVar for "process" |
| `echo` | SetVar/GetVar for "echo" |
| `debug` | SetVar/GetVar for "debug" |
| `oldredirect` | SetVar/GetVar for "oldredirect" |
| `interactive` | SetVar/GetVar for "interactive" |
| `simpleshell` | SetVar/GetVar for "simpleshell" |
| `RC` | SetVar/GetVar for "RC" |
| `Result2` | SetVar/GetVar for "Result2" |
| `LV_ALIAS` | FindVar calls with type LV_ALIAS (alias lookups) |

### Behavior

- All 9 noise items are suppressed by default.
- Noise filtering is client-side only. Suppressed events are still
  received from the daemon; they are hidden during rendering.
- Toggling noise items does not send a FILTER command to the daemon.
- Noise items are controlled via the NOISE category in the toggle grid
  (press Tab in the TUI, then navigate to the NOISE column).
- RC and Result2 are included because they duplicate information already
  visible in traced function return values. Post-command SetVar
  RC/Result2 calls are shell bookkeeping, not application library calls.

For toggle grid navigation and keybindings, see
[interactive-viewer.md](interactive-viewer.md).
