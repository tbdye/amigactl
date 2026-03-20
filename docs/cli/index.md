# CLI Reference

The amigactl command-line interface provides non-interactive, one-shot
access to the Amiga. Each invocation connects to the daemon, executes a
single command, prints the result, and exits with an appropriate status
code. This makes it suitable for shell scripts, cron jobs, CI pipelines,
and any situation where you need a quick operation without entering an
interactive session.

When invoked without a subcommand, amigactl launches the interactive
shell instead. The CLI and shell share the same connection settings
(`--host`, `--port`, `--config`, environment variables) and expose the
same core operations, but the CLI is stateless -- there is no working
directory, no history, and no persistent connection between invocations.


## CLI vs Shell vs Trace

amigactl offers three interfaces, each suited to different workflows:

| | CLI | Shell | Trace |
|----------------------|--------------------------------------|--------------------------------------|--------------------------------------------------|
| **Invocation** | `amigactl <command> [args]` | `amigactl` (no subcommand) | `amigactl trace <subcommand>` |
| **Mode** | Non-interactive, one-shot | Interactive REPL | Streaming event viewer |
| **Connection** | Opens and closes per invocation | Persistent for the session | Persistent for the trace session |
| **Exit codes** | Yes -- scriptable | N/A | Yes (`trace run` mirrors traced program) |
| **Tab completion** | No | Yes, with remote path caching | No |
| **Command history** | No | Yes, persisted across sessions | No |
| **Working directory** | No (`cd`/`pwd` are shell-only) | Yes, tracks remote CWD | N/A |
| **Extra commands** | -- | `cd`, `pwd`, `edit`, `find`, `grep`, `tree`, `diff`, `du`, `watch`, `reconnect` | Interactive TUI viewer (shell-only) |

**Use the CLI when** you need a single operation from a script, want to
chain amigactl into a Unix pipeline, or are automating a task with cron
or a Makefile.

**Use the shell when** you are exploring the filesystem, debugging
interactively, or performing a sequence of related operations where a
persistent connection and working directory save time.

**Use trace when** you need to observe library calls made by Amiga
programs in real time -- debugging, profiling, or understanding how
software interacts with the operating system.


## Getting Started

New to the CLI? The [Quick Start](quickstart.md) guide walks through
connecting to your Amiga and running your first commands in about five
minutes.


## Documents in This Section

| Document | Description |
|----------|-------------|
| [Quick Start](quickstart.md) | Getting started with CLI usage -- connecting, running your first commands, and reading output. |
| [Commands](commands.md) | Complete subcommand reference with syntax, options, and examples for every CLI command. |
| [Configuration](../configuration.md) | Global options (`--host`, `--port`, `--config`), the config file, and environment variables. |


## Related Documentation

- [Interactive Shell](../shell/index.md) -- The interactive REPL with
  tab completion, command history, working directory tracking, and
  additional commands not available in the CLI.
- [atrace](../atrace/index.md) -- Library call tracing system for
  observing AmigaOS function calls in real time.
