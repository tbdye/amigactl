# Tracing a Specific Program (TRACE RUN)

TRACE RUN launches a program on the Amiga and traces only that
program's library calls, from startup to exit.  It combines process
creation with trace streaming and automatic task-scoped filtering in a
single operation.  When the program exits, the trace stream terminates
automatically and the program's return code is propagated back to the
caller.

This is the primary tool for debugging a specific program.  Unlike
`trace start`, which captures calls from every process on the system,
`trace run` isolates the target program so you see only what it does.


## Usage

### CLI

```
amigactl trace run [--lib LIB] [--func FUNC] [--errors]
                   [--cd DIR] [--basic|--detail|--verbose]
                   -- <command>
```

The `--` separator is required when using the CLI.  It separates
amigactl's own options from the Amiga command to execute.  Without it,
argparse may misinterpret command arguments as amigactl flags.

Everything after `--` is joined into a single command string and sent
to the daemon.

### Wire Protocol

```
TRACE RUN [LIB=<name>] [FUNC=<name>] [ERRORS] [CD=<dir>] -- <command>
```

At the wire protocol level, the `--` separator is also required.  The
daemon scans for `--` to split the filter options (before) from the
command (after).  If the separator is missing, the daemon returns
`ERR 100 Missing -- separator`.

### Python API

```python
with AmigaConnection("192.168.6.200") as conn:
    result = conn.trace_run("List SYS:", callback, lib="dos")
    result = conn.trace_run("List SYS:", callback, lib="dos", preset="file-io")
```

The Python client inserts the `--` separator automatically when
constructing the wire command.  The caller does not include it.

### Options

| Option | CLI Flag | Wire Syntax | Description |
|--------|----------|-------------|-------------|
| Library filter | `--lib LIB` | `LIB=<name>` | Show only calls to this library |
| Function filter | `--func FUNC` | `FUNC=<name>` | Show only this function |
| Errors only | `--errors` | `ERRORS` | Show only calls that returned errors |
| Working directory | `--cd DIR` | `CD=<dir>` | Set the program's current directory |
| Output tier | `--basic`, `--detail`, `--verbose` | N/A (use TIER command) | Control output detail level |

When multiple filters are specified, they are AND-combined: all must
match for an event to be shown.  See [filtering.md](filtering.md) for
full filter syntax details.

Note: `--proc` is not accepted with `trace run`.  Process filtering is
automatic.  If `PROC=` appears in the wire command, the daemon returns
`ERR 100 PROC filter not valid for TRACE RUN`.


## Examples

### Basic Usage

Trace all library calls made by the `List` command:

```
$ amigactl trace run -- List SYS:C
SEQ     TIME          FUNCTION              TASK              ARGS                    RESULT  S
1001    14:30:01.000  dos.Lock              List              "SYS:C",Shared          0x03c1a0b8  O
1002    14:30:01.020  dos.Open              List              "*",Read                0x1a3c0040  O
...
```

### With Library Filter

Trace only exec.library calls from an application:

```
$ amigactl trace run --lib exec -- Work:myapp
```

### With Working Directory

Set the program's current directory before launch:

```
$ amigactl trace run --cd Work:projects -- myprog
```

This is equivalent to `CD Work:projects` followed by running `myprog`
in an Amiga shell.  Useful for programs that access files via relative
paths.

### With Error Filter

Show only calls that returned error values:

```
$ amigactl trace run --errors -- Work:myapp
```

### With Output Tier

Use the Detail tier for deeper debugging:

```
$ amigactl trace run --detail -- Work:myapp
```

If the selected tier is above Basic, the CLI enables the additional
functions (via `TRACE ENABLE`) before starting the stream.  See
[output-tiers.md](output-tiers.md) for tier membership details.

### Combined Filters

Trace only DOS file operations that return errors, from a specific
working directory:

```
$ amigactl trace run --lib dos --errors --cd Work:projects -- myprog
```


## Working Directory (CD=)

The `--cd` option (wire: `CD=<dir>`) sets the current directory of the
launched process.  The daemon obtains a shared lock on the directory
path via `Lock()`.  If the directory does not exist, the command fails
with `ERR 200 Directory not found` before the process is created.

The `CD=` token is parsed from the filter portion of the wire command
and blanked out so it does not interfere with the filter parser.  The
lock is stored in the process slot and applied as the working directory when the process wrapper begins execution.


## Lifecycle

TRACE RUN follows a precise sequence from command parsing through
process exit.  Understanding this sequence is useful for diagnosing
timing-related issues and knowing what happens at each stage.

### 1. Precondition Checks

The daemon validates several preconditions before doing any work:

- No TAIL session is active on this connection.
- No TRACE session is active on this connection.
- atrace is loaded (auto-loads `C:atrace_loader` if needed).
- atrace global_enable is set (tracing is not disabled).
- The async execution subsystem is available.

If any check fails, the daemon returns an error and the connection
stays in normal command processing mode.

### 2. Command Parsing

The daemon scans the arguments for the `--` separator.  Everything
before it is the filter/option portion; everything after it is the
command string.  The `CD=` option is extracted and consumed from the
filter portion.  The remaining filter text is validated -- if `PROC=`
is present, the command is rejected.

### 3. Process Creation

The daemon finds a free process slot (the same slot table used by
EXEC ASYNC), extracts a basename from the command for the process
name, and creates a new process via `CreateNewProcTags()`:

- `NP_Entry`: the daemon's async wrapper function
- `NP_Name`: command basename (truncated to 31 characters)
- `NP_StackSize`: 16384 bytes
- `NP_Cli`: TRUE

The process is created inside a `Forbid()` block so it cannot run
until the daemon finishes setup.  The process's Task pointer is
captured at this point.

If process creation fails, the daemon cleans up and returns
`ERR 500 Failed to create process`.

### 4. Task Filter Setup

With the new process created (but not yet running, thanks to
`Forbid()`), the daemon sets up stub-level task filtering:

1. Checks for orphaned `filter_task` values: if `filter_task` is
   non-NULL but no connected client owns it, clears it.

2. If `filter_task` is NULL, writes the new process's Task pointer
   into `anchor->filter_task` (offset 80 in the anchor struct).

3. Captures `event_sequence` as `run_start_seq` -- this value is
   guaranteed to precede any events from the new process since the
   process cannot run until `Permit()`.

4. Drains stale buffer content and clears valid flags to make room
   for the new process's events.

5. Calls `Permit()`, allowing the new process to begin executing.

### 5. Event Streaming

The daemon enters streaming mode:

- `trace.mode` is set to `TRACE_MODE_RUN`
- `trace.run_proc_slot` records the process slot index
- `trace.run_task_ptr` stores the process's Task pointer
- `trace.active` is set to 1

The daemon sends `OK <proc_id>` followed by comment headers
(version, EClock frequency, command name, active filters).  Events
are then streamed as DATA chunks following the same format as TRACE
START.

During streaming, `trace_poll_events()` applies a two-level filter
for TRACE RUN clients:

1. **Task pointer match**: `ev->caller_task` must equal
   `c->trace.run_task_ptr`.
2. **Sequence check**: `ev->sequence` must be >= `c->trace.run_start_seq`
   (skips stale events from before the process started).
3. **User filters**: LIB, FUNC, ERRORS filters are applied after the
   task match.

### 6. Process Exit Detection

The daemon calls `trace_check_run_completed()` in its event loop,
after `exec_scan_completed()` has updated process slot status.  When
the process slot transitions to `PROC_EXITED`:

1. **Final drain**: reads remaining target events from the ring buffer
   that `trace_poll_events()` has not yet consumed.  Non-target events
   are broadcast to other active TRACE START clients.

2. **Exit notification**: sends a `# PROCESS EXITED rc=<N>` comment
   as a DATA chunk, where N is the process's return code.

3. **Stream termination**: sends END followed by the sentinel (`.`).

4. **Cleanup**: calls `trace_run_cleanup()` to clear `filter_task`
   and reset the client's trace state.

The client does not need to send STOP -- the stream terminates
automatically.

### 7. Return Code Propagation

The Python client parses the `# PROCESS EXITED rc=<N>` comment to
extract the return code.  `trace_run()` returns a dict:

```python
{
    "proc_id": 5,          # daemon-assigned process ID
    "rc": 0,               # process exit code (int or None)
    "stats": {
        "total_events": 47,
        "by_function": {"Lock": 12, "Open": 8, "Close": 8, ...},
        "errors": 2,
        "error_functions": {"Open": 1, "Lock": 1},
    },
}
```

The CLI exits with the traced program's return code (capped at 255)
if it is non-zero, printing a diagnostic to stderr:

```
Process 5 exited with rc=10
```


## How Task Filtering Works

TRACE RUN's most important feature is automatic task-scoped filtering.
Only events from the launched process are shown, even though atrace
patches are system-wide.

### Stub-Level Filtering

Each atrace stub contains a 26-byte task filter check at bytes 30-55
of its prefix code.  The assembly sequence is:

1. Test `anchor->filter_task` -- if NULL, skip the check (trace all
   tasks, normal TRACE START behavior).
2. Load `SysBase->ThisTask` (the currently executing task).
3. Compare against `anchor->filter_task`.
4. If mismatch, branch to the `.disabled` path -- the stub passes
   through to the original library function with zero overhead beyond
   the comparison.

This filtering happens at the stub level, before any event is written
to the ring buffer.  When a TRACE RUN is active, only the target
process's calls consume ring buffer slots.  This dramatically reduces
buffer pressure compared to filtering at the daemon level.

### The filter_task Field

The `filter_task` field lives at offset 80 in the `atrace_anchor`
struct.  It is declared `volatile APTR` because stubs read it from
interrupt-level code paths.

- **NULL** means "trace all tasks" (the default, used by TRACE START).
- **Non-NULL** means "trace only the task whose pointer matches this
  value."

The daemon writes this field inside `Forbid()` when creating the
TRACE RUN process, and clears it when the process exits, the client
sends STOP, or the client disconnects.

### Daemon-Level Filtering (Fallback)

Even without stub-level filtering, TRACE RUN still works.
`trace_poll_events()` checks `ev->caller_task` against
`c->trace.run_task_ptr` for every event.  Non-matching events are
skipped.  The sequence number check (`ev->sequence >= run_start_seq`)
ensures stale events from before process creation are never delivered.

This fallback is used when:

- Another TRACE RUN already owns `filter_task` (only one TRACE RUN
  can set the stub-level filter at a time).

The cost of daemon-level-only filtering is higher ring buffer pressure:
all processes' events fill the buffer, and only the daemon discards
non-matching ones.  This may cause buffer overflow under heavy system
load.


## Stopping Early (Ctrl-C)

The user can press Ctrl-C during a TRACE RUN stream.  The CLI catches
`KeyboardInterrupt`, sends a STOP command to the daemon, and prints:

```
Tracing stopped. Process continues running.
```

The daemon sends END + sentinel and clears `filter_task`.  The
launched process is **not** terminated -- it continues running.  The
process can be managed afterward using the `proc_id` returned in the
OK line:

```
$ amigactl status <proc_id>
$ amigactl signal <proc_id>
$ amigactl kill <proc_id>
```

This behavior is intentional: tracing is observation, not control.
Stopping the trace should not affect the program being observed.


## Concurrency Limitation

The `filter_task` field in the anchor struct is a single global value.
Only one TRACE RUN can use stub-level task filtering at a time.

If a second TRACE RUN starts while the first is still active (on a
different connection), the second TRACE RUN skips the `filter_task`
write.  It still works -- daemon-level filtering ensures only the
correct process's events reach each client -- but the ring buffer
receives events from all processes, increasing overflow risk.

There is no protocol-level rejection of concurrent TRACE RUN sessions.
The second session simply falls back to daemon-level filtering
silently.  For best results, run one TRACE RUN at a time.

### Orphan Detection

If a client disconnects unexpectedly during TRACE RUN (network
failure, client crash), its `filter_task` value may be left behind.
The next TRACE RUN checks for this condition: if `filter_task` is
non-NULL but no connected client owns that value, it is cleared before
the new session takes ownership.


## Error Cases

| Condition | Error | Notes |
|-----------|-------|-------|
| Missing `--` separator | `ERR 100` | Syntax error |
| Missing command after `--` | `ERR 100` | Nothing to execute |
| `PROC=` filter specified | `ERR 100` | Process filtering is automatic |
| atrace not loaded | `ERR 500` | Auto-load failed |
| atrace is disabled | `ERR 500` | atrace is disabled (run: atrace_loader ENABLE) |
| TRACE session already active | `ERR 500` | One trace per connection |
| TAIL session active | `ERR 500` | Mutual exclusion with TAIL |
| CD= directory not found | `ERR 200` | Lock() failed on the path |
| Async exec unavailable | `ERR 500` | Daemon signal bit not allocated |
| Process table full | `ERR 500` | MAX_TRACKED_PROCS slots exhausted |
| Failed to create process | `ERR 500` | CreateNewProcTags() returned NULL |

All errors are returned synchronously before streaming begins.  The
connection remains in normal command processing mode and can accept
further commands.

### Command Not Found

When the command executable is not found on the Amiga, the process
still starts (the daemon creates a wrapper process), but it exits
immediately with `rc=-1`.  The client receives:

```
# PROCESS EXITED rc=-1
```

This is not a protocol error -- it is a normal exit with a non-zero
return code.  The CLI passes the return code to `sys.exit()`, and POSIX
unsigned-byte conversion produces exit status 255 for -1.


## Interaction with TRACE START

TRACE RUN and TRACE START can coexist on different connections.  A
TRACE START session on one connection sees events from all processes
(or filtered by PROC=), while a TRACE RUN session on another
connection sees only its target process's events.

Events from the TRACE RUN target process are visible to both sessions.
A TRACE START client can observe the same program by using
`--proc List` (the command basename used as the task name).

During the final drain when a TRACE RUN process exits, non-target
events in the ring buffer are broadcast to active TRACE START clients,
ensuring they do not miss events that arrived between poll cycles.


## Differences from TRACE START

| Aspect | TRACE START | TRACE RUN |
|--------|-------------|-----------|
| Process scope | System-wide (or PROC= filter) | Single launched process |
| Termination | Manual (Ctrl-C / STOP) | Automatic on process exit |
| PROC= filter | Accepted | Rejected (automatic) |
| CD= option | Not available | Sets working directory |
| Return code | N/A | Propagated to caller |
| Task filtering | Daemon-side only | Stub-level + daemon-side |
| Statistics | Not collected | Accumulated by Python client |


## Further Reading

- [filtering.md](filtering.md) -- Full filter reference (LIB, FUNC,
  ERRORS, extended syntax, presets)
- [cli-reference.md](cli-reference.md) -- Complete CLI option reference
  for all `amigactl trace` subcommands
- [architecture.md](architecture.md) -- Three-component architecture
  and data flow overview
- [reading-output.md](reading-output.md) -- How to interpret trace
  event fields and status indicators
- [output-tiers.md](output-tiers.md) -- Tier membership and switching
