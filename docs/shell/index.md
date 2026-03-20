# Interactive Shell

The amigactl interactive shell provides remote access to an Amiga
running the amigactld daemon. It maintains a persistent TCP connection
and tracks a current working directory on the remote filesystem, giving
it the feel of a local shell session despite operating over the network.
Paths are resolved using Amiga conventions (`:` volume separators, `/`
parent navigation, assigns) before being sent to the daemon.

The shell exposes over 50 commands spanning file management, command
execution, process control, system administration, ARexx messaging,
library call tracing, and search utilities. It provides tab
completion for remote paths and command names with a caching layer to
minimize round-trips, persistent command history via readline, and
colorized output for directory listings, diffs, and trace events.


## Key Features

- **Full filesystem access.** List, read, create, copy, move, delete,
  and inspect files and directories on the Amiga with familiar
  Unix-style commands.
- **Bidirectional file transfer.** Download, upload, append, and
  edit-in-place with automatic conflict detection.
- **Remote command execution.** Run AmigaOS CLI commands synchronously
  or in the background, with process listing, signaling, and
  force-termination.
- **ARexx integration.** Send ARexx commands to any named message port
  on the Amiga, bridging client-side scripts into the Amiga IPC ecosystem.
- **Library call tracing.** Start, stop, filter, and view atrace
  sessions from within the shell, including a full-screen interactive
  TUI viewer.
- **Search utilities.** `find`, `grep`, `tree`, `diff`, `du`, `watch`,
  and `tail` combine basic operations into powerful search and monitoring
  tools.
- **Tab completion and history.** Remote path completion with caching
  and persistent readline history across sessions.
- **System introspection.** Query volumes, assigns, devices, tasks,
  ports, environment variables, library versions, and system info.


## Getting Started

New to the shell? The [Quick Start](quickstart.md) guide walks through
connecting to your Amiga and performing the essential workflow --
navigate, inspect, execute, transfer, and exit -- in 5 minutes.


## Document Index

### User Guide

| Document | Description |
|----------|-------------|
| [Quick Start](quickstart.md) | Connect and get productive in 5 minutes -- navigate, inspect, execute, transfer, and exit. |
| [Navigation](navigation.md) | Current working directory, `cd`, `pwd`, Amiga path syntax, volume separators, assigns, and parent traversal. |
| [File Operations](file-operations.md) | Listing, inspecting, creating, copying, moving, deleting, and modifying files and directories. |
| [File Transfer](file-transfer.md) | Downloading, uploading, appending, and editing remote files with conflict detection. |
| [Command Execution](command-execution.md) | Synchronous and asynchronous command execution, process listing, signaling, and termination. |
| [System Commands](system-commands.md) | Daemon health, system state, environment variables, assigns, shutdown, and reboot. |
| [ARexx](arexx.md) | Sending ARexx commands to Amiga message ports for application scripting and automation. |
| [Search Utilities](search-utilities.md) | `find`, `grep`, `diff`, `du`, and `watch` commands. |
| [Recipes](recipes.md) | Cookbook-style workflows combining commands for common tasks. |

### Technical Reference

| Document | Description |
|----------|-------------|
| [Architecture](architecture.md) | Shell internals: `cmd.Cmd` design, connection management, path resolution, completion caching, colorized output. |
| [Execution Internals](execution-internals.md) | Daemon-side execution engine: synchronous/async modes, process table, signal-based completion detection. |

### Operational

| Document | Description |
|----------|-------------|
| [Tab Completion](tab-completion.md) | Path completion with caching, command name completion, and persistent readline history. |
| [Troubleshooting](troubleshooting.md) | Symptom-based diagnostic guide: connection failures, transfer errors, execution problems, and more. |
| [Limitations](limitations.md) | Known constraints, design trade-offs, and platform-specific considerations. |


## Command Quick Reference

Every shell command, sorted alphabetically. Aliases are listed
separately and point to the same documentation as their target command.

| Command | Description | Documentation |
|---------|-------------|---------------|
| `append` | Append a local file to a remote file | [file-transfer.md](file-transfer.md) |
| `arexx` | Send an ARexx command to a named message port | [arexx.md](arexx.md) |
| `assign` | Create, modify, or remove a logical assign | [system-commands.md](system-commands.md) |
| `assigns` | List all logical assigns | [system-commands.md](system-commands.md) |
| `capabilities` | Show daemon capabilities and supported commands | [system-commands.md](system-commands.md) |
| `caps` | Show daemon capabilities (alias for `capabilities`) | [system-commands.md](system-commands.md) |
| `cat` | Print a remote file's contents to the terminal | [file-operations.md](file-operations.md) |
| `cd` | Change the current working directory | [navigation.md](navigation.md) |
| `checksum` | Compute CRC32 checksum of a remote file | [file-operations.md](file-operations.md) |
| `chmod` | Get or set AmigaOS protection bits | [file-operations.md](file-operations.md) |
| `comment` | Set file comment (alias for `setcomment`) | [file-operations.md](file-operations.md) |
| `copy` | Copy a file on the Amiga (alias for `cp`) | [file-operations.md](file-operations.md) |
| `cp` | Copy a file on the Amiga | [file-operations.md](file-operations.md) |
| `devices` | List Exec devices | [system-commands.md](system-commands.md) |
| `diff` | Compare two remote files | [search-utilities.md](search-utilities.md) |
| `dir` | List directory contents (alias for `ls`) | [file-operations.md](file-operations.md) |
| `du` | Show disk usage for a directory | [search-utilities.md](search-utilities.md) |
| `edit` | Edit a remote file in a local editor | [file-transfer.md](file-transfer.md) |
| `env` | Get an AmigaOS environment variable | [system-commands.md](system-commands.md) |
| `exec` | Execute an AmigaOS CLI command synchronously | [command-execution.md](command-execution.md) |
| `exit` | Disconnect and exit the shell | [quickstart.md](quickstart.md) |
| `find` | Search for files and directories by name pattern | [search-utilities.md](search-utilities.md) |
| `get` | Download a file from the Amiga | [file-transfer.md](file-transfer.md) |
| `getenv` | Get an environment variable (alias for `env`) | [system-commands.md](system-commands.md) |
| `grep` | Search file contents for a pattern | [search-utilities.md](search-utilities.md) |
| `kill` | Force-terminate a background process | [command-execution.md](command-execution.md) |
| `libver` | Get the version of an Amiga library or device | [system-commands.md](system-commands.md) |
| `ls` | List directory contents | [file-operations.md](file-operations.md) |
| `mkdir` | Create a directory | [file-operations.md](file-operations.md) |
| `mv` | Rename or move a file or directory | [file-operations.md](file-operations.md) |
| `ping` | Check that the daemon is responding | [system-commands.md](system-commands.md) |
| `ports` | List active Exec message ports | [system-commands.md](system-commands.md) |
| `ps` | List processes launched by the daemon | [command-execution.md](command-execution.md) |
| `put` | Upload a file to the Amiga | [file-transfer.md](file-transfer.md) |
| `pwd` | Print the current working directory | [navigation.md](navigation.md) |
| `quit` | Disconnect and exit the shell (alias for `exit`) | [quickstart.md](quickstart.md) |
| `reboot` | Reboot the Amiga (requires confirmation) | [system-commands.md](system-commands.md) |
| `reconnect` | Re-establish the connection after a disconnect | [troubleshooting.md](troubleshooting.md) |
| `rm` | Delete a file or empty directory | [file-operations.md](file-operations.md) |
| `run` | Launch an AmigaOS CLI command in the background | [command-execution.md](command-execution.md) |
| `setcomment` | Set the file comment on a remote file | [file-operations.md](file-operations.md) |
| `setenv` | Set or delete an AmigaOS environment variable | [system-commands.md](system-commands.md) |
| `shutdown` | Shut down the amigactld daemon (requires confirmation) | [system-commands.md](system-commands.md) |
| `signal` | Send a break signal to a background process | [command-execution.md](command-execution.md) |
| `stat` | Show file or directory metadata | [file-operations.md](file-operations.md) |
| `status` | Show the status of a tracked background process | [command-execution.md](command-execution.md) |
| `sysinfo` | Show Amiga system information | [system-commands.md](system-commands.md) |
| `tail` | Stream new data appended to a file in real time | [file-operations.md](file-operations.md) |
| `tasks` | List all running tasks and processes | [system-commands.md](system-commands.md) |
| `touch` | Set datestamp or create an empty file | [file-operations.md](file-operations.md) |
| `trace` | Control library call tracing (start, run, stop, status, enable, disable) | [../atrace/index.md](../atrace/index.md) |
| `tree` | Display a directory tree | [file-operations.md](file-operations.md) |
| `uptime` | Show how long the daemon has been running | [system-commands.md](system-commands.md) |
| `version` | Print the amigactld daemon version string | [system-commands.md](system-commands.md) |
| `volumes` | List mounted volumes | [system-commands.md](system-commands.md) |
| `watch` | Repeatedly run a command and display its output | [search-utilities.md](search-utilities.md) |


## Related Documentation

The shell is one component of the amigactl toolkit. These documents
cover related areas:

- [atrace Documentation](../atrace/index.md) -- Library call tracing
  system, including the `trace` subcommands accessible from within the
  shell.
- [CLI Reference](../cli/index.md) -- Non-interactive, scriptable
  command-line interface for one-shot operations.
- [Configuration](../configuration.md) -- Global options, config file,
  and environment variables shared across all amigactl modes.
- [Protocol Commands Reference](../protocol-commands.md) -- Complete command reference for the
  amigactld daemon wire protocol.
- [Wire Protocol Specification](../protocol.md) -- Wire protocol specification for
  client-daemon communication.
