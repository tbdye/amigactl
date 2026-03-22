# Command Reference

Commands are invoked as `amigactl <command> [arguments]`. Global options
(`--host`, `--port`, `--config`) are documented in
[Configuration](../configuration.md). Run `amigactl <command> --help` for
quick help on any command.

Running `amigactl` with no subcommand enters the
[interactive shell](../shell/index.md).


## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success. |
| 1 | Error: connection failure, protocol error, invalid arguments, or daemon-reported error. |

**Special cases:**

- **`exec`** mirrors the remote command's AmigaOS return code, capped at 255.
  Exit code 0 means the remote command succeeded.
- **`arexx`** mirrors the ARexx return code, capped at 255.
- **`trace run`** mirrors the traced command's return code, capped at 255.


## Error Handling

All error messages are printed to **stderr**. Normal command output goes to
stdout, making it safe to redirect or pipe output without error messages
contaminating the data stream.

Errors fall into four categories:

| Error type | stderr message | Cause |
|------------|----------------|-------|
| Connection refused | `Error: could not connect to <host>:<port>` | Daemon not running or wrong host/port. |
| OS/network error | `Error: <OS message>` | Socket timeout, broken pipe, DNS failure. |
| Daemon error | `Error: <daemon message>` | The daemon rejected the command (file not found, permission denied, etc.). |
| Protocol error | `Error: <description>` | Unexpected response framing from the daemon. |

Daemon error codes map to these exception types:

| Code | Type | Typical causes |
|------|------|----------------|
| 100 | Command syntax error | Unknown command, malformed arguments. |
| 200 | Not found | File, directory, path, ARexx port, or process ID not found. |
| 201 | Permission denied | Operation not permitted (e.g., delete-protected file, remote shutdown disabled). |
| 202 | Already exists | Target already exists (e.g., `cp --no-replace` to existing file). |
| 300 | I/O error | Filesystem I/O failure on the Amiga. |
| 400 | Timeout | Operation timed out on the Amiga (e.g., ARexx 30-second reply timeout). |
| 500 | Internal error | Unexpected daemon-side failure. |


## Output Format Conventions

CLI output is designed for both human reading and scripting:

- **Tab-separated columns** are used by listing commands (`ps`, `volumes`,
  `tasks`, `devices`). The first line is a header row with column names.
- **Tab-separated columns without a header row** are used by `ls` and
  `assigns`.
- **`key=value` pairs**, one per line, are used by metadata commands (`stat`,
  `sysinfo`, `status`, `capabilities`, `checksum`, `libver`, `chmod`). No
  header row.
- **Plain text** is used by simple commands (`version`, `ping`, `env`,
  `ports`).
- **Raw bytes** are written to stdout by `cat` (binary-safe; use redirection
  to capture).
- All text output uses the terminal's default encoding. The daemon protocol
  uses ISO-8859-1 internally.
- Commands that produce no output on success (`rm`, `mv`, `mkdir`,
  `setcomment`) print a brief confirmation message (e.g., `Deleted`, `Renamed`,
  `Comment set`).
- Commands that transfer data report byte counts (e.g.,
  `Downloaded 1234 bytes to file.txt`).
- Some output is colorized when connected to a terminal. Color can be
  controlled with the `AMIGACTL_COLOR` and `NO_COLOR` environment variables.
  See [Configuration](../configuration.md) for details.

**Commands with header rows:** `ps`, `volumes`, `tasks`, `devices`.

**Commands without header rows:** `ls`, `assigns`, `stat`, `sysinfo`, `status`,
`capabilities`, `checksum`, `libver`, `chmod`.


---


## Connection and Daemon

### version

Print the daemon's version string.

```
amigactl version
```

**Arguments:** None.

**Output:** Single line with the daemon version identifier.

```
amigactld
```

---

### ping

Verify the daemon is responding.

```
amigactl ping
```

**Arguments:** None.

**Output:** Prints `OK` on success.

---

### shutdown

Shut down the amigactld daemon process.

```
amigactl shutdown
```

**Arguments:** None.

Sends `SHUTDOWN CONFIRM` to the daemon. The daemon must have remote shutdown
enabled in its configuration, or the command fails with a permission denied
error.

**Output:** Prints the daemon's response message, or `Shutdown initiated` if
the daemon returns no message.

---

### reboot

Reboot the Amiga.

```
amigactl reboot
```

**Arguments:** None.

Sends `REBOOT CONFIRM` to the daemon, which calls AmigaOS `ColdReboot()`. The
daemon must have remote reboot enabled in its configuration. Because
`ColdReboot()` may kill the TCP stack before the response arrives, connection
reset errors are treated as success.

**Output:** Prints the daemon's response message, or `Reboot initiated` if no
message is received.

---

### uptime

Show how long the daemon has been running.

```
amigactl uptime
```

**Arguments:** None.

**Output:** Human-readable duration with components separated by spaces. Zero
components are omitted. Seconds is always shown when it is the only non-zero
component or when all components are zero (i.e., `0s`).

```
2d 5h 33m 12s
```

```
0s
```


---


## File Operations

### ls

List directory contents.

```
amigactl ls [-r] <path>
```

| Argument | Type | Required | Description |
|----------|------|----------|-------------|
| `path` | string | yes | Amiga directory path to list. |
| `-r`, `--recursive` | flag | no | Recurse into subdirectories. |

**Output:** Tab-separated fields, one entry per line, with no header row.
Fields are: type, name, size, protection, datestamp.

```
DIR	S	0	00000000	2026-01-15 10:30:00
FILE	Startup-Sequence	1234	00000000	2026-01-15 10:30:00
```

- `type` is `FILE` or `DIR`.
- `size` is in bytes (0 for directories).
- `protection` is an 8-digit hexadecimal value representing AmigaOS protection
  bits.
- `datestamp` is in `YYYY-MM-DD HH:MM:SS` format (Amiga local time).

With `--recursive`, names include the relative path from the listed directory
(e.g., `S/Startup-Sequence`).

---

### stat

Show file or directory metadata.

```
amigactl stat <path>
```

| Argument | Type | Required | Description |
|----------|------|----------|-------------|
| `path` | string | yes | Amiga path. |

**Output:** Key=value pairs, one per line. Fields are: type, name, size,
protection, datestamp, comment.

```
type=FILE
name=Startup-Sequence
size=1234
protection=00000000
datestamp=2026-01-15 10:30:00
comment=Boot script
```

The `comment` field is included even when empty.

Protection bits are displayed as 8-digit hex values. The interactive shell's
`stat` command displays the same bits in `hsparwed` notation instead.

---

### cat

Print a remote file's contents to stdout.

```
amigactl cat [--offset N] [--length N] <path>
```

| Argument | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `path` | string | yes | | Amiga file path. |
| `--offset` | int | no | None (start of file) | Start reading at this byte offset. |
| `--length` | int | no | None (entire file) | Read at most this many bytes. |

**Output:** Raw file bytes written directly to `sys.stdout.buffer`. No
trailing newline is added. Binary-safe; suitable for piping or redirection.

---

### get

Download a file from the Amiga to the local filesystem.

```
amigactl get [--offset N] [--length N] <remote> [<local>]
```

| Argument | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `remote` | string | yes | | Amiga file path. |
| `local` | string | no | Basename of remote path | Local destination file path. |
| `--offset` | int | no | None (start of file) | Start reading at this byte offset. |
| `--length` | int | no | None (entire file) | Read at most this many bytes. |

When `local` is omitted, the file is saved in the current local directory
using the basename extracted from the Amiga path. The extraction handles both
`/` and `:` separators (e.g., `SYS:C/Dir` saves as `Dir`).

**Output:**

```
Downloaded 1234 bytes to Dir
```

---

### put

Upload a local file to the Amiga.

```
amigactl put <local> [<remote>]
```

| Argument | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `local` | string | yes | | Local file path. |
| `remote` | string | no | Basename of local path | Amiga destination file path. |

When `remote` is omitted, the file is uploaded with the same basename as the
local file.

**Output:**

```
Uploaded 1234 bytes to SYS:C/myfile
```

---

### append

Append the contents of a local file to an existing remote file.

```
amigactl append <local> <remote>
```

| Argument | Type | Required | Description |
|----------|------|----------|-------------|
| `local` | string | yes | Local file to read and append. |
| `remote` | string | yes | Amiga file to append to. |

The remote file must already exist.

**Output:**

```
Appended 256 bytes to RAM:logfile.txt
```

---

### rm

Delete a file or empty directory on the Amiga.

```
amigactl rm <path>
```

| Argument | Type | Required | Description |
|----------|------|----------|-------------|
| `path` | string | yes | Amiga path to delete. |

There is no recursive delete. Directories must be empty.

**Output:** Prints `Deleted` on success.

---

### mv

Rename or move a file or directory on the Amiga.

```
amigactl mv <old> <new>
```

| Argument | Type | Required | Description |
|----------|------|----------|-------------|
| `old` | string | yes | Current Amiga path. |
| `new` | string | yes | New Amiga path. |

Both paths must be on the same volume (AmigaOS limitation).

**Output:** Prints `Renamed` on success.

---

### cp

Copy a file on the Amiga.

```
amigactl cp [-P] [-n] <source> <dest>
```

| Argument | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `source` | string | yes | | Source Amiga path. |
| `dest` | string | yes | | Destination Amiga path. |
| `-P`, `--no-clone` | flag | no | off | Do not copy metadata (protection bits, datestamp, comment). |
| `-n`, `--no-replace` | flag | no | off | Fail if destination already exists. |

By default, the copy preserves metadata (protection bits, datestamp, comment).

**Output:** Prints `Copied` on success.

---

### mkdir

Create a directory on the Amiga.

```
amigactl mkdir <path>
```

| Argument | Type | Required | Description |
|----------|------|----------|-------------|
| `path` | string | yes | Amiga path for the new directory. |

Parent directories must already exist.

**Output:** Prints `Created` on success.

---

### chmod

Get or set AmigaOS protection bits.

```
amigactl chmod <path> [<value>]
```

| Argument | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `path` | string | yes | | Amiga path. |
| `value` | string | no | None (get mode) | Hex protection value to set. |

When `value` is omitted, the command reads and displays the current protection
bits. When provided, it sets the protection bits to the given value.

AmigaOS protection bits use inverted semantics for the lower 4 bits (RWED): a
set bit means the operation is *denied*.

**Output:** Key=value format with the current or new protection value:

```
protection=00000000
```

---

### checksum

Compute the CRC32 checksum of a remote file.

```
amigactl checksum <path>
```

| Argument | Type | Required | Description |
|----------|------|----------|-------------|
| `path` | string | yes | Amiga file path. |

The checksum is computed on the daemon side. The CRC32 value matches Python's
`zlib.crc32() & 0xFFFFFFFF`.

**Output:**

```
crc32=a1b2c3d4
size=1234
```

---

### touch

Set a file's datestamp, creating the file if it does not exist.

```
amigactl touch <path> [<date> <time>]
```

| Argument | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `path` | string | yes | | Amiga path. |
| `date` | string | no | Current time | Date in `YYYY-MM-DD` format. |
| `time` | string | no | Current time | Time in `HH:MM:SS` format. |

Both `date` and `time` must be provided together, or both omitted. Providing
only one is an error (exit code 1, message to stderr).

If the file does not exist, it is created as an empty file (Unix `touch`
semantics). If a datestamp is specified, it is applied after creation.

**Output:**

When setting the datestamp on an existing file:
```
datestamp=2026-02-19 12:00:00
```

When creating a new file without a specific datestamp:
```
Created RAM:test.txt
```

---

### setcomment

Set the file comment (filenote) on an Amiga file.

```
amigactl setcomment <path> <comment>
```

| Argument | Type | Required | Description |
|----------|------|----------|-------------|
| `path` | string | yes | Amiga file path. |
| `comment` | string | yes | Comment string. Use `''` (empty string) to clear. |

**Output:** Prints `Comment set` on success.

---

### tail

Stream new data appended to a file in real time.

```
amigactl tail <path>
```

| Argument | Type | Required | Description |
|----------|------|----------|-------------|
| `path` | string | yes | Amiga file path to tail. |

Continuously streams new data as it is written to the file, similar to Unix
`tail -f`. Press Ctrl-C to stop. The raw bytes are written to
`sys.stdout.buffer` as they arrive.


---


## Command Execution

### exec

Execute an AmigaOS CLI command synchronously and display its output.

```
amigactl exec [-C DIR] [--timeout SECS] [--] <command...>
```

| Argument | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `command` | remainder | yes | | Command to execute. Use `--` before any flags intended for the remote command. |
| `-C` | string | no | None | Working directory for the command on the Amiga. |
| `--timeout` | int | no | None (socket default) | Socket timeout in seconds for this command. |

The command tokens are joined with spaces and sent to the daemon. The
connection blocks until the command completes.

**Output:** The captured stdout from the remote command is written to stdout.
No additional output is printed on success.

**Exit codes:** The remote command's AmigaOS return code is used as the exit
code, capped at 255. Exit code 0 means the remote command returned 0.

If no command is specified (empty remainder), the error
`Error: no command specified` is printed to stderr and the exit code is 1.

---

### run

Launch an AmigaOS CLI command asynchronously.

```
amigactl run [-C DIR] [--] <command...>
```

| Argument | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `command` | remainder | yes | | Command to launch. Use `--` before any flags intended for the remote command. |
| `-C` | string | no | None | Working directory for the command on the Amiga. |

Returns immediately after the daemon acknowledges the launch. The launched
process is tracked by the daemon and can be inspected with `ps`, `status`,
`signal`, and `kill`.

**Output:** The daemon-assigned process ID (integer) on a single line:

```
1
```

---

### ps

List processes launched through the daemon.

```
amigactl ps
```

**Arguments:** None.

Only processes started via `run` (or `EXEC ASYNC` at the protocol level) are
listed.

**Output:** Tab-separated columns with a header row. Columns: ID, COMMAND,
STATUS, RC. The RC column shows `-` for processes that have not yet finished.

```
ID	COMMAND	STATUS	RC
1	wait 30	RUNNING	-
2	echo hello	EXITED	0
```

If there are no tracked processes, no output is produced.

---

### status

Get the status of a specific tracked process.

```
amigactl status <id>
```

| Argument | Type | Required | Description |
|----------|------|----------|-------------|
| `id` | int | yes | Process ID returned by `run`. |

**Output:** Key=value pairs, one per line. The RC field shows `-` for
processes that have not finished.

```
id=1
command=wait 30
status=RUNNING
rc=-
```

---

### signal

Send a break signal to a tracked process.

```
amigactl signal <id> [<sig>]
```

| Argument | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `id` | int | yes | | Process ID returned by `run`. |
| `sig` | string | no | `CTRL_C` | Signal name: `CTRL_C`, `CTRL_D`, `CTRL_E`, or `CTRL_F`. |

**Output:** Prints `OK` on success.

---

### kill

Force-terminate a tracked process.

```
amigactl kill <id>
```

| Argument | Type | Required | Description |
|----------|------|----------|-------------|
| `id` | int | yes | Process ID returned by `run`. |

Forcibly removes the process from the daemon's tracking. Prefer `signal`
for graceful shutdown.

**Output:** Prints `OK` on success.


---


## System Information

### sysinfo

Display Amiga system information.

```
amigactl sysinfo
```

**Arguments:** None.

**Output:** Key=value pairs, one per line. Fields include chip/fast RAM (free,
total, largest block), exec version, Kickstart version, and bsdsocket library
version.

```
chip_free=1048576
fast_free=16000000
total_free=17048576
chip_total=2097152
fast_total=16777216
chip_largest=1000000
fast_largest=15000000
exec_version=47.2
kickstart=3.2
bsdsocket=4.307
```

---

### assigns

List all logical assigns.

```
amigactl assigns
```

**Arguments:** None.

**Output:** Tab-separated fields, one per line, with no header row. Fields
are: name (with trailing colon), path.

```
SYS:	DH0:
S:	DH0:S
C:	DH0:C
LIBS:	DH0:Libs
```

---

### assign

Create, modify, or remove a logical assign.

```
amigactl assign [--late] [--add] <name> [<path>]
```

| Argument | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `name` | string | yes | | Assign name with trailing colon (e.g., `TEST:`). |
| `path` | string | no | None (remove) | Target path. Omit to remove the assign. |
| `--late` | flag | no | off | Create a late-binding assign (path resolved on access). |
| `--add` | flag | no | off | Add path to an existing multi-directory assign. |

`--late` and `--add` are mutually exclusive. Without either flag, a standard
lock-based assign is created (path resolved immediately).

When `path` is omitted, the assign is removed.

**Output:**

```
OK
```

When removing an assign:
```
Removed
```

---

### ports

List active Exec message ports.

```
amigactl ports
```

**Arguments:** None.

**Output:** One port name per line, no header.

```
REXX
amigactld
ARexx
```

---

### volumes

List mounted volumes with space information.

```
amigactl volumes
```

**Arguments:** None.

**Output:** Tab-separated columns with a header row. Columns: NAME, USED,
FREE, CAPACITY, BLOCKSIZE. All numeric values are in bytes.

```
NAME	USED	FREE	CAPACITY	BLOCKSIZE
DH0:	52428800	157286400	209715200	512
RAM:	1048576	15728640	16777216	512
```

If no volumes are found, no output is produced.

---

### tasks

List all running tasks and processes on the Amiga.

```
amigactl tasks
```

**Arguments:** None.

**Output:** Tab-separated columns with a header row. Columns: NAME, TYPE,
PRI, STATE, STACK.

```
NAME	TYPE	PRI	STATE	STACK
amigactld	process	0	ready	65536
input.device	task	20	waiting	4096
```

- `TYPE` is `task` or `process`.
- `PRI` is the task priority (integer, may be negative).
- `STATE` is the scheduling state (e.g., `ready`, `waiting`, `running`).
- `STACK` is the stack size in bytes.

If no tasks are found, no output is produced.

---

### devices

List Exec devices loaded in the system.

```
amigactl devices
```

**Arguments:** None.

**Output:** Tab-separated columns with a header row. Columns: NAME, VERSION.

```
NAME	VERSION
timer.device	47.1
input.device	47.1
```

If no devices are found, no output is produced.

---

### capabilities

Show daemon capabilities and supported commands.

```
amigactl capabilities
```

**Arguments:** None.

**Output:** Key=value pairs, one per line. Fields include version, protocol,
max_clients, max_cmd_len, and commands (comma-separated list).

```
version=<version>
protocol=1.0
max_clients=8
max_cmd_len=4096
commands=APPEND,AREXX,ASSIGN,ASSIGNS,...
```

---

### libver

Get the version of an Amiga library or device.

```
amigactl libver <name>
```

| Argument | Type | Required | Description |
|----------|------|----------|-------------|
| `name` | string | yes | Library or device name (e.g., `exec.library`, `timer.device`). |

**Output:**

```
name=exec.library
version=47.2
```

---

### env

Get an AmigaOS environment variable.

```
amigactl env <name>
```

| Argument | Type | Required | Description |
|----------|------|----------|-------------|
| `name` | string | yes | Variable name. |

**Output:** The variable's value as a single line of text.

```
English
```

Raises a not-found error (exit code 1) if the variable does not exist.

---

### setenv

Set or delete an AmigaOS environment variable.

```
amigactl setenv [-v] <name> [<value>]
```

| Argument | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `name` | string | yes | | Variable name. |
| `value` | string | no | None (delete) | Value to set. Omit to delete the variable. |
| `-v`, `--volatile` | flag | no | off | Set in ENV: only (current session). Without this flag, the variable is also persisted to ENVARC: (survives reboot). |

**Output:**

When setting a value:
```
Set
```

When deleting a variable:
```
Deleted
```


---


## ARexx

### arexx

Send an ARexx command to a named message port.

```
amigactl arexx <port> [--] <command...>
```

| Argument | Type | Required | Description |
|----------|------|----------|-------------|
| `port` | string | yes | ARexx port name (use `ports` to discover available ports). |
| `command` | remainder | yes | ARexx command string. Use `--` before flags to avoid argument parsing conflicts. |

The daemon has a 30-second timeout for ARexx replies.

**Output:** If the ARexx command returns a result string, it is printed to
stdout. Nothing is printed if the result is empty.

**Exit codes:** The ARexx return code is used as the exit code, capped at 255.
Exit code 0 means the ARexx command succeeded.


---


## Interactive Shell

Running `amigactl` with no subcommand enters the interactive shell:

```
amigactl
```

The shell provides a persistent connection with a current working directory,
tab completion, command history, and additional commands not available from the
CLI. See [Shell Documentation](../shell/index.md) for details.

The `shell` subcommand is also accepted explicitly:

```
amigactl shell
```


---


## Trace Commands

Library call tracing is provided through the `trace` subcommand family. These
commands control [atrace](../atrace/index.md), the Amiga-side library call
tracing subsystem.

| Subcommand | Description |
|------------|-------------|
| `trace start` | Start streaming live trace events. Press Ctrl-C to stop. |
| `trace run` | Launch a command and trace its library calls until it exits. |
| `trace stop` | Not valid from CLI (use Ctrl-C to stop a running trace). |
| `trace status` | Show whether atrace is loaded, enabled, and current statistics. |
| `trace enable` | Enable tracing globally or for specific functions. |
| `trace disable` | Disable tracing globally or for specific functions. |

**Note:** `trace run` uses `--cd DIR` for the working directory option, while
`exec` and `run` use `-C DIR`. The underlying daemon operation is the same.

For complete syntax, arguments, filters, and output format details, see the
[atrace CLI Reference](../atrace/cli-reference.md).


---


## Shell-Only Commands

The following commands are available only in the [interactive shell](../shell/index.md)
and do not have CLI subcommand equivalents:

| Command | Description |
|---------|-------------|
| `cd` | Change the current working directory on the Amiga. |
| `pwd` | Print the current working directory. |
| `dir` | Alias for `ls`. |
| `copy` | Alias for `cp`. |
| `comment` | Alias for `setcomment`. |
| `getenv` | Alias for `env`. |
| `caps` | Alias for `capabilities`. |
| `edit` | Download a file, open it in a local editor, and upload changes. |
| `find` | Search recursively for files and directories by glob pattern. |
| `tree` | Display a directory tree with Unicode box-drawing characters. |
| `grep` | Search remote file contents for a string or regex pattern. |
| `diff` | Compare two remote files and display a unified diff. |
| `du` | Show disk usage (per-directory subtotals) for a directory tree. |
| `watch` | Repeatedly run a shell command at a configurable interval. |
| `reconnect` | Re-establish the connection after a disconnect. |
| `exit` / `quit` | Disconnect and exit the shell. |
