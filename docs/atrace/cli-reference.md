# CLI Reference: amigactl trace Commands

All `amigactl trace` subcommands are invoked through the top-level
`amigactl` CLI. The general form is:

```
amigactl [--host HOST] [--port PORT] [--config PATH] trace <subcommand> [options]
```

Global options (`--host`, `--port`, `--config`) are described in
[Configuration](#configuration) at the end of this document.

There are six trace subcommands: `start`, `run`, `stop`, `status`,
`enable`, and `disable`.


## trace start

Stream trace events from all tasks to stdout as plain text. This is
**not** the interactive TUI viewer -- it prints formatted lines to
stdout with no scrollback, pausing, or keybindings. The TUI is only
available via the `amigactl shell` interactive mode (see
[Differences from the Interactive Shell](#differences-from-the-interactive-shell)).

The stream continues until interrupted with Ctrl-C.

### Syntax

```
amigactl trace start [--lib LIB] [--func FUNC] [--proc PROC]
                     [--errors] [--basic | --detail | --verbose]
```

### Options

| Option | Type | Description |
|--------|------|-------------|
| `--lib LIB` | string | Filter by library name. Accepts short names (`dos`, `exec`, `intuition`, `bsdsocket`, `graphics`, `icon`, `workbench`) or full names (`dos.library`). |
| `--func FUNC` | string | Filter by function name (e.g., `Open`). Supports comma-separated lists and library-scoped names (`dos.Open`). See [filtering.md](filtering.md) for full syntax. |
| `--proc PROC` | string | Filter by process name (case-insensitive substring match against the task name). |
| `--errors` | flag | Only show calls that returned error values. |
| `--basic` | flag | Use the Basic output tier (default). 57 core diagnostic functions. |
| `--detail` | flag | Use the Detail output tier. Adds 13 resource-lifecycle functions to Basic. |
| `--verbose` | flag | Use the Verbose output tier. Adds 3 high-volume functions to Detail. |

The tier flags (`--basic`, `--detail`, `--verbose`) are mutually
exclusive. If none is specified, Basic (tier 1) is used. See
[output-tiers.md](output-tiers.md) for the complete list of functions
in each tier.

When a tier above Basic is selected, the CLI enables the additional
tier functions (via `TRACE ENABLE`) before starting the stream.
Filters are AND-combined: specifying both `--lib dos` and `--errors`
shows only dos.library calls that returned errors.

### Output Format

Output begins with a column header line, followed by one line per
trace event:

```
SEQ                 TIME  FUNCTION                     TASK                 ARGS                                     RESULT
1            23.456789  dos.Open                     myapp                "SYS:Libs/test.library",1005             0x1e3c8a00
2            23.457102  dos.Close                    myapp                0x1e3c8a00                               -1
3            23.458200  exec.OpenLibrary             myapp                "utility.library",0                      0x1e200000
```

Columns are fixed-width. Comment lines (such as system messages)
are prefixed with `#`.

When the terminal supports color, error results are highlighted in
red and successful results in green.

### Examples

Stream all events (Basic tier):

```
amigactl trace start
```

Stream only dos.library calls:

```
amigactl trace start --lib dos
```

Stream only error returns from all libraries:

```
amigactl trace start --errors
```

Stream calls from a specific process at the Detail tier:

```
amigactl trace start --proc myapp --detail
```

Combine library and function filters:

```
amigactl trace start --lib exec --func OpenLibrary
```

### Stopping

Press Ctrl-C to stop the stream. The CLI sends a `STOP` command to
the daemon, drains any remaining events, and exits cleanly.


## trace run

Launch a program on the Amiga and trace only its library calls. The
stream starts when the program launches and ends automatically when
it exits. Only calls from the launched process are shown -- other
system activity is filtered out at the stub level.

### Syntax

```
amigactl trace run [--lib LIB] [--func FUNC] [--errors]
                   [--cd DIR] [--basic | --detail | --verbose]
                   -- <command>
```

The `--` separator is **required** between options and the command.

### Options

| Option | Type | Description |
|--------|------|-------------|
| `--lib LIB` | string | Filter by library name (same as `trace start`). |
| `--func FUNC` | string | Filter by function name (same as `trace start`). |
| `--errors` | flag | Only show error returns. |
| `--cd DIR` | string | Working directory for the launched command on the Amiga. |
| `--basic` | flag | Basic output tier (default). |
| `--detail` | flag | Detail output tier. |
| `--verbose` | flag | Verbose output tier. |

Note: `--proc` is **not available** with `trace run`. The process
filter is applied automatically to the launched program. Attempting
to use `--proc` with `trace run` would be meaningless since only the
launched process is traced.

### Output Format

Output is identical to `trace start`: a column header followed by
formatted event lines. When the process exits, the CLI prints the
exit status to stderr if the return code is non-zero:

```
Process 5 exited with rc=10
```

The CLI exits with the same return code as the traced program
(capped at 255).

### Examples

Trace a directory listing:

```
amigactl trace run -- List SYS:C
```

Trace only dos.library calls from a program:

```
amigactl trace run --lib dos -- Work:myapp
```

Trace a program with a specific working directory:

```
amigactl trace run --cd Work:projects -- myapp
```

Trace with Verbose tier to see all function activity:

```
amigactl trace run --verbose -- List SYS:
```

Show only errors from a program:

```
amigactl trace run --errors -- Work:myapp
```

### Interrupting

Press Ctrl-C to stop tracing before the program exits. The trace
stream stops, but the launched program continues running on the
Amiga. A message is printed to stderr:

```
Tracing stopped. Process continues running.
```

For more details on task-scoped tracing, see
[trace-run.md](trace-run.md).


## trace stop

The `trace stop` subcommand does **not** stop a remote trace session.
It prints an error message and exits with code 1:

```
$ amigactl trace stop
trace stop is only valid during an active trace stream.
Use Ctrl-C to stop a running trace.
```

To stop a running trace, press Ctrl-C in the terminal where
`trace start` or `trace run` is running. The Ctrl-C handler sends
the `STOP` protocol command internally.


## trace status

Query the current state of the atrace kernel module.

### Syntax

```
amigactl trace status
```

No arguments.

### Output Format

If atrace is not loaded:

```
atrace is not loaded.
```

If atrace is loaded, key=value pairs are printed:

```
loaded=1
enabled=1
patches=99
events_produced=14523
events_consumed=14520
events_dropped=0
buffer_capacity=2048
buffer_used=3
```

| Field | Description |
|-------|-------------|
| `loaded` | 1 if the atrace kernel module is resident. |
| `enabled` | 1 if global tracing is enabled, 0 if disabled. |
| `patches` | Number of function patches installed. |
| `events_produced` | Total events written to the ring buffer. |
| `events_consumed` | Total events read from the ring buffer by the daemon. |
| `events_dropped` | Oldest events overwritten due to ring buffer overflow. |
| `buffer_capacity` | Ring buffer size in event slots. |
| `buffer_used` | Current number of unconsumed events in the buffer. |

### Example

```
$ amigactl trace status
loaded=1
enabled=1
patches=99
events_produced=14523
events_consumed=14520
events_dropped=0
buffer_capacity=2048
buffer_used=3
```


## trace enable

Enable tracing globally or for specific functions.

### Syntax

```
amigactl trace enable [func1 func2 ...]
```

### Arguments

| Argument | Type | Description |
|----------|------|-------------|
| `func1 func2 ...` | positional, optional | Function names to enable. If omitted, enables global tracing. |

### Behavior

With no arguments, enables the global tracing flag. With arguments,
enables the named functions (sets their per-patch enable flag).
Functions are specified by name without library prefix (e.g.,
`OpenLibrary`, not `exec.OpenLibrary`).

### Output

With no arguments:

```
atrace tracing enabled.
```

With function names:

```
Enabled: AllocMem, FreeMem
```

### Examples

Enable global tracing:

```
amigactl trace enable
```

Enable specific functions:

```
amigactl trace enable AllocMem FreeMem AllocVec FreeVec
```


## trace disable

Disable tracing globally or for specific functions.

### Syntax

```
amigactl trace disable [func1 func2 ...]
```

### Arguments

| Argument | Type | Description |
|----------|------|-------------|
| `func1 func2 ...` | positional, optional | Function names to disable. If omitted, disables global tracing. |

### Behavior

With no arguments, disables the global tracing flag and drains the
event buffer. With arguments, disables the named functions (clears
their per-patch enable flag). Functions are specified by name without
library prefix.

### Output

With no arguments:

```
atrace tracing disabled.
```

With function names:

```
Disabled: Read, Write, WaitSelect
```

### Examples

Disable global tracing:

```
amigactl trace disable
```

Disable noisy functions:

```
amigactl trace disable AllocMem FreeMem GetMsg PutMsg
```


## CLI Equivalents for Common Filter Combinations

There is no `--preset` flag on the CLI. Presets are a Python API
feature only (see [python-api.md](python-api.md) for programmatic
usage). The same filtering can be achieved with CLI flags:

| Preset Name | CLI Equivalent |
|-------------|----------------|
| file-io | `--lib dos --func Open,Close,Lock,DeleteFile,CreateDir,Rename,MakeLink,SetProtection` |
| lib-load | `--func OpenLibrary,OpenDevice,OpenResource,CloseLibrary,CloseDevice` |
| network | `--lib bsdsocket` |
| ipc | `--func FindPort,GetMsg,PutMsg,ObtainSemaphore,ReleaseSemaphore,AddPort,WaitPort` |
| errors-only | `--errors` |
| memory | `--func AllocMem,FreeMem,AllocVec,FreeVec` |
| window | `--func OpenWindow,CloseWindow,OpenScreen,CloseScreen,OpenWindowTagList,OpenScreenTagList,ActivateWindow` |
| icon | `--lib icon` |

Example -- trace file I/O with errors only:

```
amigactl trace start --lib dos --func Open,Close,Lock,DeleteFile,CreateDir,Rename,MakeLink,SetProtection --errors
```

For full filter syntax (comma-separated lists, blacklists, extended
FILTER command), see [filtering.md](filtering.md).


## Differences from the Interactive Shell

The `amigactl trace` CLI commands and the interactive shell's `trace`
command share the same underlying protocol, but differ in several
ways:

| Feature | CLI (`amigactl trace`) | Shell (`amigactl shell` then `trace`) |
|---------|------------------------|---------------------------------------|
| Output mode | Plain text to stdout | Interactive TUI with scrollback, pause, search, filter grid (when stdout is a TTY) |
| Filter syntax | `--lib`, `--func`, `--proc`, `--errors` flags | `LIB=`, `FUNC=`, `PROC=`, `ERRORS` bare tokens |
| Tier switching | Selected at start only (`--basic`/`--detail`/`--verbose`) | Can switch during viewing with 1/2/3 keys |
| Working directory | Not applicable | `trace run` inherits the shell's current Amiga working directory if `CD=` is not specified |
| Process filter | `--proc NAME` | `PROC=NAME` |
| Stopping | Ctrl-C | Ctrl-C or `q` key (TUI) |

In the interactive shell, `trace start` opens the full-screen TUI
viewer when the terminal supports it (TTY on non-Windows platforms).
The TUI provides pause/resume, scrollback navigation, a toggle grid
for per-library and per-function filtering, statistics display, event
detail view, and save-to-file. These features are not available in
the CLI's plain-text output mode.

When the shell detects a non-TTY stdout (e.g., piped output), it
falls back to the same plain-text format as the CLI.

For full TUI documentation, see
[interactive-viewer.md](interactive-viewer.md).


## Configuration

amigactl uses a shared configuration system for connection settings,
environment variables, and config file management. See
[Configuration](../configuration.md) for complete details on:

- Global options (`--host`, `--port`, `--config`)
- Configuration file format and auto-creation
- Environment variables (`AMIGACTL_HOST`, `AMIGACTL_PORT`, `AMIGACTL_COLOR`)
- Resolution order (CLI flag > environment > config file > default)
