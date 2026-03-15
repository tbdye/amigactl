# atrace -- Library Call Tracing for AmigaOS

## What is atrace?

atrace is a library call tracing system for AmigaOS, part of the
[amigactl](../../README.md) remote access toolkit. It intercepts calls to
system library functions in real time, records them as 128-byte events with
microsecond EClock timestamps, and streams them over TCP to a remote client
for display and analysis. Think of it as strace for the Amiga: you see
every library call a program makes, with arguments, return values, and
error status, without modifying the program itself.

atrace traces 99 functions across 7 AmigaOS libraries: exec, dos,
intuition, bsdsocket, graphics, icon, and workbench.


## Key Features

- **Zero-configuration install.** The daemon auto-loads `C:atrace_loader`
  when the first trace session starts. No manual setup required.
- **Per-function enable/disable.** Each of the 99 patches can be
  individually toggled without restarting.
- **Task-scoped filtering.** `trace run` launches a program and traces
  only its calls, from startup to exit.
- **Tiered output levels.** Basic (57 functions), Detail (70), and
  Verbose (73) tiers control verbosity with a single flag.
- **EClock microsecond timestamps.** Every event records the Amiga's
  hardware timer for precise timing analysis.
- **String argument capture.** Filenames, library names, device names,
  and variable names are captured directly in the event.
- **IoErr capture.** DOS library calls record the IoErr value alongside
  the return code.
- **Interactive TUI viewer.** Full-screen terminal interface with pause,
  scrollback, per-library/function/process filtering, text search, and
  statistics (via `amigactl shell`).
- **Python API with filter presets.** Programmatic access for scripting
  and automated analysis.


## Architecture Overview

atrace comprises three cooperating components:

1. **atrace_loader** -- An installer program that runs once on the Amiga
   to patch library jump tables with generated 68k stubs. The stubs
   intercept function calls, record events into a shared ring buffer, and
   call through to the original library code. All allocated memory is
   `MEMF_PUBLIC` and persists after the loader exits.

2. **amigactld** -- The daemon running on the Amiga. It discovers the
   atrace ring buffer via a named Exec semaphore, polls for events,
   resolves task names, formats events into human-readable text, applies
   per-client filters, and streams results over TCP port 6800.

3. **Python client** -- The `amigactl` CLI and library running on the
   host machine. It provides the `trace start`, `trace run`, and
   `trace status` commands, the interactive TUI viewer, and a
   programmatic API for scripted analysis.

See [architecture.md](architecture.md) for the full technical breakdown.


## Getting Started

New to atrace? The [Quick Start](quickstart.md) guide walks through
capturing your first trace in under five minutes. It covers prerequisites,
the plain-text CLI output, the interactive TUI viewer, and basic filtering.


## Document Index

### User Guide

| Document | Description |
|----------|-------------|
| [Quick Start](quickstart.md) | First trace in under 5 minutes -- prerequisites, basic commands, and first results. |
| [CLI Reference](cli-reference.md) | Complete syntax for all `amigactl trace` subcommands: start, run, stop, status, enable, disable. |
| [Event Filtering](filtering.md) | Daemon-side and client-side filtering: library, function, process, error-only, filter presets, mid-stream changes. |
| [Tracing a Specific Program](trace-run.md) | TRACE RUN: launching a program with automatic task-scoped filtering, from startup to exit. |
| [Interactive Trace Viewer](interactive-viewer.md) | Full-screen TUI: screen layout, keybindings, filter grid, search, statistics, scrollback export. |
| [Output Tiers](output-tiers.md) | Basic, Detail, Verbose, and Manual tiers -- which functions are active at each level. |
| [Reading Trace Output](reading-output.md) | Interpreting event lines: the seven tab-separated fields, return value classification, formatting conventions. |
| [Traced Functions Reference](traced-functions.md) | Per-function metadata for all 99 functions: arguments, registers, string capture, error classification, tier assignment. |
| [Recipes](recipes.md) | Cookbook-style examples for common debugging scenarios: file I/O, library loading, network debugging, and more. |

### Technical Reference

| Document | Description |
|----------|-------------|
| [System Architecture](architecture.md) | Three-component design, shared data structures, and the complete lifecycle of a trace event. |
| [68k Stub Mechanism](stub-mechanism.md) | Deep technical reference for stub generation: three-region structure, instruction sequences, register usage, trampoline. |
| [Ring Buffer](ring-buffer.md) | Shared memory layout, producer/consumer protocol, overflow handling, and capacity configuration. |
| [Event Format](event-format.md) | The 128-byte `struct atrace_event` binary layout: every field, offset, size, and type. |
| [Daemon Integration](daemon-integration.md) | How amigactld discovers, polls, formats, filters, and streams trace events. |
| [Adding Functions](adding-functions.md) | Step-by-step guide to adding new traced functions or entire libraries. |
| [Python API](python-api.md) | Programmatic access: `AmigaConnection` trace methods, `TraceStreamReader`, filter presets, event parsing. |

### Operational

| Document | Description |
|----------|-------------|
| [Performance](performance.md) | Overhead quantification at each level: stub execution, memory, ring buffer, daemon polling, network bandwidth. |
| [Troubleshooting](troubleshooting.md) | Symptom-based diagnostic guide: common problems, causes, and solutions. |
| [Limitations](limitations.md) | Known constraints, design trade-offs, and platform-specific considerations. |


## Related Documentation

The amigactl wire protocol and command set are documented separately:

- [COMMANDS.md](../COMMANDS.md) -- Complete command reference for the
  amigactld daemon, including the TRACE command family.
- [PROTOCOL.md](../PROTOCOL.md) -- Wire protocol specification for
  client-daemon communication.
