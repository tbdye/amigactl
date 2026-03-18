# Limitations

Every tool has boundaries. Understanding the amigactl shell's
constraints helps you work effectively within them and avoid surprises
during file management, command execution, and remote administration.


## Connection

### Single Connection per Shell Session

Each shell session uses one TCP connection to the amigactld daemon.
All commands -- file operations, execution, system queries -- are
serialized over this connection. Only one request can be in flight at a
time (except ARexx, which has its own non-blocking reply mechanism).

This means a long-running synchronous `exec` blocks the shell until the
command completes. You cannot issue other commands while waiting.

**Workaround:** Open a second shell session for concurrent operations,
or use `run` for background execution to avoid blocking the shell.

### Maximum 8 Concurrent Daemon Connections

The daemon supports at most 8 simultaneous client connections
(`MAX_CLIENTS` in `daemon.h`). This limit covers all amigactld
connections, not just shell sessions -- TRACE START sessions, TAIL
sessions, file transfers, and shell sessions each consume one slot.
When all slots are occupied, new connections are silently refused.

**Workaround:** Disconnect idle sessions. Use `exit` to cleanly release
a slot rather than letting a connection time out.

### No Connection Multiplexing

The protocol is strictly request-response. Streaming commands (TAIL,
TRACE START) occupy the connection for their duration, preventing any
other commands on that connection until the stream ends.

**Workaround:** Use a separate shell session for interactive work while
a streaming session is active.


## Path Handling

### ISO-8859-1 Only

The wire protocol encodes all strings -- paths, command output,
filenames -- as ISO-8859-1 (Latin-1). Paths containing characters
outside this encoding (such as non-Latin Unicode characters) are
rejected before the command is sent.

This covers the full range of characters that AmigaOS filesystems
support. In practice, Amiga filenames are almost always plain ASCII.

### CD= Does Not Support Spaces in Directory Names

The `CD=<path>` prefix used to set a working directory for remote
command execution terminates at the first whitespace character. Directory
names containing spaces cannot be specified this way.

This is a protocol-level constraint: the daemon's `parse_cd_prefix()`
scans for the next space or tab to find the end of the path.

There is currently no workaround for executing commands in directories
whose paths contain spaces. While `cd` can navigate to such directories
(it uses STAT validation, not the CD= prefix parser), subsequent `exec`
or `run` commands will fail because the shell generates the `CD=<cwd>`
prefix from the current working directory, and the daemon's
`parse_cd_prefix()` splits on whitespace when parsing it.

### No Wildcard Expansion on the Command Line

The shell does not expand glob patterns (`*`, `?`) in command arguments
for most commands. If you type `rm *.info`, the literal string `*.info`
is sent to the daemon, which will fail because no file has that name.

The `ls` command is an exception -- it supports glob patterns by
filtering directory listings client-side.

**Workaround:** Use the `find` command for pattern matching (`find SYS:
*.info`), or use `ls *.info` to identify matching files and then operate
on them individually.


## Command Execution

### Synchronous Exec Blocks All Clients

When any client runs a synchronous `exec` command, the daemon's
single-threaded event loop is blocked until the command completes. During
this time, no other clients can send or receive data. A long-running
command (e.g., a compilation or disk operation) blocks the entire daemon.

This is an inherent consequence of the single-threaded event loop design
-- the daemon cannot process other clients while waiting for
`SystemTags()` to return.

**Workaround:** Use `run` (async exec) for long-running commands. Async
execution creates a separate AmigaOS process that runs independently of
the daemon's event loop.

### No Standard Input for Executed Commands

Both synchronous and asynchronous commands have their standard input
connected to `NIL:`. Commands that read from stdin will receive
immediate EOF.

This is a design choice -- there is no mechanism to relay interactive
input over the network protocol.

### No Output Capture for Async Commands

Commands launched with `run` (async exec) have both stdout and stderr
connected to `NIL:`. Their output is discarded. Only the return code is
captured when the process exits.

**Workaround:** Redirect output within the command itself (e.g.,
`run mycommand >RAM:output.txt`) and retrieve the output file with
`get` or `cat`.

### Process Table Limited to 16 Slots

The daemon tracks at most 16 async processes (`MAX_TRACKED_PROCS`).
When all slots are in use by running processes, new `run` commands fail
with "Process table full." Exited processes are automatically evicted
(oldest first) to make room.

### No Pipe or Redirection Support in the Shell

The amigactl shell does not support Unix-style pipes (`|`) or
redirection (`>`, `<`, `>>`) between commands. Each command is sent to
the daemon as a single request.

**Workaround:** For pipes and redirection, pass the full command line to
`exec` and let the AmigaOS shell handle it: `exec type SYS:S/Startup-Sequence`.
AmigaOS itself supports redirection with `>` in its CLI.

### Command Length Limit

Individual commands (including the `CD=` prefix) are limited to 4096
bytes (`MAX_CMD_LEN`). Commands exceeding this limit are rejected.

### Async Command String Truncated at 255 Characters

Async commands (`run`) have their command string stored in a 256-byte
buffer in the process table slot. Commands longer than 255 characters
are silently truncated before execution. This is more restrictive than
the 4096-byte `MAX_CMD_LEN` wire protocol limit and affects actual
command execution, not just the command name shown by `ps` and `status`.

**Workaround:** For long commands, write the command to a script file
on the Amiga and execute the script instead.


## File Transfer

### No Recursive Directory Transfer

The `get` and `put` commands operate on individual files. There is no
built-in mechanism to download or upload an entire directory tree in one
operation.

**Workaround:** Use `find` to list files, then transfer them
individually with scripting on the client side. For bulk transfers,
consider using the amigactl Python API directly.

### No Resume for Interrupted Transfers

If a file transfer is interrupted (network error, Ctrl-C), there is no
way to resume from where it left off. The transfer must be restarted
from the beginning.

### No Text Encoding Conversion

Files are transferred as raw bytes. The shell does not convert line
endings (LF vs CR/LF) or character encodings between the client and the
Amiga. AmigaOS text files typically use LF line endings (same as Unix),
so this is rarely an issue in practice.


## ARexx

### Result String Truncated at 4 KB

ARexx command results are stored in a 4096-byte static buffer on the
daemon side (`result_buf[4096]` in `arexx.c`). Result strings longer
than 4096 bytes are silently truncated.

### One Outstanding Request per Client

Each client connection can have at most one pending ARexx request.
Attempting to send a second ARexx command while the first is still
waiting for a reply results in an error. The shell enforces this
naturally since it waits for each command to complete before accepting
the next.

### 30-Second Non-Configurable Timeout

If an ARexx target port does not reply within 30 seconds
(`AREXX_TIMEOUT_SECS`), the daemon reports a timeout error to the
client. This timeout is a compile-time constant and cannot be adjusted
at runtime.

After a timeout, the daemon keeps the ARexx message slot active
(orphaned) until the reply eventually arrives and can be safely freed.
If the target application is slow but will eventually respond, the
response is silently discarded.


## Protocol

### Text-Based Protocol with Line-Length Limits

The daemon uses a text-based, line-oriented protocol. Each command line
is limited to 4096 bytes. Binary file data is framed within `DATA`/`END`
markers with explicit length headers, so binary content itself is not
subject to line-length constraints.

### No Encryption or Authentication

All communication between the shell and daemon is unencrypted plaintext
over TCP. There is no authentication mechanism -- any client that can
reach the daemon's port can issue commands, subject only to optional
IP-based access control lists configured in the daemon.

**Workaround:** Restrict access using the daemon's ACL configuration
(`ALLOW` directives in `S:amigactld.conf`), or use network-level
controls (firewall rules, VPN) to limit who can reach the daemon port.


## Design Trade-offs

The amigactl shell prioritizes simplicity, reliability, and minimal
resource consumption on the Amiga side. Several of the limitations above
are deliberate trade-offs:

- **Single-threaded daemon.** The daemon runs as a single AmigaOS
  process using a `WaitSelect()` event loop. This avoids the complexity
  and resource overhead of multi-threaded programming on AmigaOS, where
  inter-task synchronization is manual and error-prone. The cost is that
  synchronous exec blocks all clients.

- **Static resource limits.** Fixed-size arrays for clients (8),
  processes (16), and ARexx slots (one per client) keep memory usage deterministic
  and avoid dynamic allocation on a platform where memory is scarce and
  fragmentation is permanent.

- **Text protocol.** A human-readable protocol simplifies debugging and
  makes it possible to interact with the daemon using a raw TCP client
  (e.g., `nc` or `telnet`) for troubleshooting. The trade-off is
  slightly higher overhead compared to a binary protocol, and no
  built-in security.

- **Client-side path resolution.** The shell resolves relative paths and
  `..` segments locally before sending commands to the daemon. This
  keeps the daemon simpler (it receives only absolute paths) but means
  the shell must track its own CWD state, which can diverge from the
  Amiga filesystem state if directories are renamed or deleted
  externally.


## Related Documentation

- [navigation.md](navigation.md) -- Path resolution rules and CWD
  management.
- [architecture.md](architecture.md) -- Shell architecture and
  connection lifecycle.
- [file-transfer.md](file-transfer.md) -- File upload and download
  commands.
- [file-operations.md](file-operations.md) -- File and directory
  management commands.
