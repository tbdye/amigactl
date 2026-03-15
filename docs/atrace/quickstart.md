# Quick Start: First Trace in Under 5 Minutes

This guide walks through starting your first atrace session. By the end
you will have captured live system library calls from a running Amiga.

## Prerequisites

Before starting, verify four things are in place:

1. **amigactld** is running on the Amiga (typically installed at `C:amigactld`
   and launched from `S:User-Startup`). It listens on TCP port 6800 by
   default.

2. **atrace_loader** is installed at `C:atrace_loader` on the Amiga. The
   daemon auto-loads it when the first trace session starts, so no manual
   loading is required.

3. **Python client** is available on the host machine (Python 3.8+). Either
   run directly via the wrapper script (`client/amigactl.sh`) or install
   with pip:

       pip install -e client/

4. **Network connectivity** between host and Amiga. Verify with:

       amigactl --host 192.168.6.200 ping

   Replace `192.168.6.200` with your Amiga's IP address. You should see
   `OK`. If you set `AMIGACTL_HOST` in your environment or configured
   `client/amigactl.conf`, you can omit the `--host` flag from all
   commands below.

## Step 1 -- Start Tracing (CLI Plain Text Output)

The fastest way to see trace events is the CLI `trace start` command. It
prints formatted events to stdout as plain text:

    amigactl --host 192.168.6.200 trace start

Output streams immediately as library calls happen on the Amiga. Press
**Ctrl+C** to stop.

Example output:

```
SEQ              TIME  FUNCTION                     TASK                 ARGS                                     RESULT
1          0.000123  dos.Open                     Shell Process [1]    "S:Startup-Sequence",1005                0x1a2b3c4d
2          0.000456  dos.Lock                     Shell Process [1]    "SYS:",ACCESS_READ                       0x0a1b2c3d
3          0.001002  exec.OpenLibrary             Shell Process [1]    "dos.library",0                          0x07c3e4f0
4          0.001234  dos.Close                    Shell Process [1]    0x1a2b3c4d                               -1
5          0.002100  exec.OpenDevice               MyApp                "timer.device",0,ioreq,0                 0
```

This output is illustrative; actual field values vary based on the
system state and traced application. Each line shows the sequence
number, timestamp (seconds since trace start), library and function
name, calling task, arguments, and return value. Error returns are highlighted in red when color is supported.

This is plain text output to stdout -- it is **not** the interactive TUI
viewer. The output can be piped or redirected like any other command.

## Step 2 -- Interactive TUI Viewer

For a richer experience with pause, scrollback, filtering, and
statistics, use the interactive shell:

    amigactl --host 192.168.6.200 shell

At the shell prompt, start tracing:

    amiga@192.168.6.200:SYS:> trace start

This opens the TUI viewer with a status bar, scrollable event list, and
hotkey bar. The TUI is only available through the interactive shell -- it
does not activate from the CLI `amigactl trace start` command.

Key TUI controls:

| Key     | Action                            |
|---------|-----------------------------------|
| q       | Quit viewer and stop the trace    |
| p       | Pause/resume event scrolling      |
| ?       | Show help overlay                 |
| Tab     | Open the filter toggle grid       |
| /       | Search events                     |
| e       | Toggle errors-only mode           |
| 1/2/3   | Switch output tier (basic/detail/verbose) |

Press **q** to stop the trace and return to the shell prompt.

See [interactive-viewer.md](interactive-viewer.md) for the full TUI
reference.

## Step 3 -- Trace a Specific Program

To trace only the library calls made by a single program, use
`trace run`. The trace starts when the program launches and stops
automatically when it exits:

    amigactl --host 192.168.6.200 trace run -- List SYS:C

The `--` separator before the command is required in the interactive
shell and recommended in the CLI to avoid ambiguity with option flags.
Only calls from the launched process are captured -- system-wide background activity is
filtered out.

From the interactive shell, the same command opens the TUI viewer:

    amiga@192.168.6.200:SYS:> trace run -- List SYS:C

When the program exits, the trace stream ends. The CLI prints an exit
status message only if the return code is non-zero; a successful (rc=0)
exit ends the stream silently. Press Ctrl+C to detach early (the program
continues running on the Amiga but tracing stops).

## Step 4 -- Filter by Library

To focus on a single library's calls, use the `--lib` flag:

    amigactl --host 192.168.6.200 trace start --lib dos

This shows only dos.library calls (Open, Close, Lock, Read, Write, etc.)
and suppresses all other libraries. Use `--func Open` to filter to a
single function. See [Filtering](filtering.md) for more filter options.

The seven traceable libraries are: exec, dos, intuition, bsdsocket,
graphics, icon, and workbench.

From the interactive shell, pass the filter as a parameter:

    amiga@192.168.6.200:SYS:> trace start LIB=dos

See [filtering.md](filtering.md) for the full filtering reference,
including function filters, process filters, and extended filter syntax.

## Step 5 -- Show Only Errors

To see only calls that returned error values:

    amigactl --host 192.168.6.200 trace start --errors

This suppresses successful calls and shows only failures -- useful for
diagnosing why a program is not working. Combine with other filters:

    amigactl --host 192.168.6.200 trace run --lib dos --errors -- Work:MyApp

From the interactive shell:

    amiga@192.168.6.200:SYS:> trace start ERRORS
    amiga@192.168.6.200:SYS:> trace run LIB=dos ERRORS -- Work:MyApp

## Next Steps

- [cli-reference.md](cli-reference.md) -- Complete reference for all
  `amigactl trace` subcommands and options.
- [interactive-viewer.md](interactive-viewer.md) -- Full TUI viewer
  documentation: layout, keybindings, search, toggle grid.
- [filtering.md](filtering.md) -- All filtering mechanisms: library,
  function, process, error, tier-based, and mid-stream filter changes.
- [output-tiers.md](output-tiers.md) -- Output tier system (basic,
  detail, verbose) and per-function tier membership.
- [reading-output.md](reading-output.md) -- How to interpret trace event
  fields, status codes, and argument formatting.
- [trace-run.md](trace-run.md) -- Detailed guide to TRACE RUN, task
  filtering, working directories, and limitations.
