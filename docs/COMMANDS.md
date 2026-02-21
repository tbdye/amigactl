# amigactl Command Reference

This document is the authoritative specification for all amigactl commands.
Code is written to satisfy this spec. Reviewers validate implementations
against it. Tests verify the documented behavior.

**Version**: 0.4.0 (Phase 4 -- ARexx, file streaming)

**Conventions used in this document:**

- `C>` denotes a line sent by the client (bytes on the wire, excluding the
  trailing `\n` which is implicit on every line).
- `S>` denotes a line sent by the server.
- `\n` is the line terminator. The server always sends `\n` (not `\r\n`).
  The server accepts both `\n` and `\r\n` from the client (stripping any
  `\r` immediately preceding `\n`).
- `.\n` is the sentinel that terminates every response (both OK and ERR).
- Commands are case-insensitive. `PING`, `ping`, and `Ping` are equivalent.
- Empty lines (whitespace-only before the newline) are silently ignored by
  the server -- no response is sent.
- The maximum command line length is 4096 bytes (including the command verb,
  all arguments, and any trailing `\r`, but excluding the terminating `\n`).
  See
  [Oversized Command Lines](#oversized-command-lines) for overflow behavior.
- All text uses ISO-8859-1 encoding (native AmigaOS).

For wire-level framing details (dot-stuffing, binary data chunking, error
code table), see [PROTOCOL.md](PROTOCOL.md).

---

## Table of Contents

- [Connection Banner](#connection-banner)
- [VERSION](#version)
- [PING](#ping)
- [QUIT](#quit)
- [SHUTDOWN](#shutdown)
- [DIR](#dir)
- [STAT](#stat)
- [READ](#read)
- [WRITE](#write)
- [DELETE](#delete)
- [RENAME](#rename)
- [MAKEDIR](#makedir)
- [PROTECT](#protect)
- [SETDATE](#setdate)
- [EXEC](#exec)
  - [EXEC (Synchronous)](#exec-synchronous)
  - [EXEC ASYNC](#exec-async)
- [PROCLIST](#proclist)
- [PROCSTAT](#procstat)
- [SIGNAL](#signal)
- [KILL](#kill)
- [SYSINFO](#sysinfo)
- [ASSIGNS](#assigns)
- [PORTS](#ports)
- [VOLUMES](#volumes)
- [TASKS](#tasks)
- [REBOOT](#reboot)
- [UPTIME](#uptime)
- [AREXX](#arexx)
- [TAIL](#tail)
- [STOP](#stop)
- [Error Handling](#error-handling)
  - [Unknown Command](#unknown-command)
  - [Oversized Command Lines](#oversized-command-lines)
- [Not Yet Implemented](#not-yet-implemented)

---

## Connection Banner

The banner is not a command. The server sends it immediately upon accepting
a new TCP connection, before the client sends anything.

### Format

```
AMIGACTL <version>
```

The version string matches the daemon version (currently `0.4.0`).

### Behavior

- The banner is the first data the client receives after TCP connect.
- If the client's IP fails the ACL check, the server closes the connection
  immediately without sending a banner.
- If the server has reached its maximum client limit (8), the connection is
  closed immediately without sending a banner.
- The banner is NOT followed by a sentinel (`.\n`). It is a single line,
  not a command response.

### Example

```
S> AMIGACTL 0.4.0
```

---

## VERSION

Returns the daemon's name and version string.

### Syntax

```
VERSION
```

No arguments. Any trailing text after `VERSION` is ignored.

### Response

```
OK
<version_string>
.
```

The payload is a single line containing the daemon identifier and version
in the format `amigactld <version>`.

### Error Conditions

None. This command always succeeds.

### Example

```
C> VERSION
S> OK
S> amigactld 0.4.0
S> .
```

---

## PING

A no-op keepalive command. Returns OK with no payload.

### Syntax

```
PING
```

No arguments. Any trailing text after `PING` is ignored.

### Response

```
OK
.
```

No payload lines between the status line and the sentinel.

### Error Conditions

None. This command always succeeds.

### Example

```
C> PING
S> OK
S> .
```

---

## QUIT

Requests a graceful disconnect. The server sends a farewell response and
then closes the connection.

### Syntax

```
QUIT
```

No arguments. Any trailing text after `QUIT` is ignored.

### Response

```
OK Goodbye
.
```

After sending the sentinel, the server closes the client's TCP connection.
The client should expect `recv()` to return 0 (EOF) after reading the
sentinel.

### Error Conditions

None. This command always succeeds.

### Example

```
C> QUIT
S> OK Goodbye
S> .
(server closes connection)
(client recv returns EOF)
```

---

## SHUTDOWN

Requests a complete daemon shutdown. Requires the `CONFIRM` keyword as a
safety measure and must be enabled in the daemon configuration.

### Syntax

```
SHUTDOWN CONFIRM
```

The first whitespace-delimited token after `SHUTDOWN` must be `CONFIRM`
(case-insensitive). Additional tokens after `CONFIRM` are ignored.

### Response (success)

```
OK Shutting down
.
```

After sending the response, the daemon:

1. Closes all client connections (including the one that sent SHUTDOWN).
2. Closes the listener socket.
3. Exits cleanly.

### Error Conditions

| Condition | Response |
|-----------|----------|
| `CONFIRM` keyword missing or wrong | `ERR 100 SHUTDOWN requires CONFIRM keyword` |
| `ALLOW_REMOTE_SHUTDOWN` is not `YES` in config | `ERR 201 Remote shutdown not permitted` |

Error checking order: the `CONFIRM` keyword is validated first. If the
keyword is missing, the server returns ERR 100 regardless of the
`ALLOW_REMOTE_SHUTDOWN` setting.

### Examples

**Successful shutdown (ALLOW_REMOTE_SHUTDOWN YES in config):**

```
C> SHUTDOWN CONFIRM
S> OK Shutting down
S> .
(server closes all connections and exits)
```

Note: both the command verb and the `CONFIRM` keyword are case-insensitive.
`shutdown confirm`, `Shutdown CONFIRM`, and `SHUTDOWN Confirm` are all
equivalent.

**Missing CONFIRM keyword:**

```
C> SHUTDOWN
S> ERR 100 SHUTDOWN requires CONFIRM keyword
S> .
```

**Wrong keyword:**

```
C> SHUTDOWN NOW
S> ERR 100 SHUTDOWN requires CONFIRM keyword
S> .
```

**Remote shutdown not permitted (default configuration):**

```
C> SHUTDOWN CONFIRM
S> ERR 201 Remote shutdown not permitted
S> .
```

---

## DIR

Lists the contents of a directory. Optionally recurses into subdirectories.

### Syntax

```
DIR <path> [RECURSIVE]
```

`<path>` is mandatory. `RECURSIVE` is an optional keyword (case-insensitive)
that causes the listing to include all subdirectories and their contents.
Any trailing text after the path (or after `RECURSIVE`) is ignored.

### Response

```
OK
<type>\t<name>\t<size>\t<protection>\t<datestamp>
<type>\t<name>\t<size>\t<protection>\t<datestamp>
...
.
```

Each payload line contains five tab-separated fields with no trailing
whitespace after the last field:

| Field | Description |
|-------|-------------|
| `type` | `FILE` or `DIR` |
| `name` | Entry name (non-recursive) or relative path from the base directory (recursive) |
| `size` | Size in bytes. 0 for directories. |
| `protection` | 8 lowercase hex digits, zero-padded (raw AmigaOS `fib_Protection` value) |
| `datestamp` | `YYYY-MM-DD HH:MM:SS` (local Amiga time) |

Payload lines are dot-stuffed per [PROTOCOL.md](PROTOCOL.md). If an entry
name begins with `.`, the line will be dot-stuffed on the wire.

An empty directory returns OK with no payload lines (just the sentinel).

**RECURSIVE behavior**: When `RECURSIVE` is specified, entries from
subdirectories use relative paths from the base directory as the name field
(e.g., `S/Startup-Sequence`). Directory entries are listed before their
contents. The base directory itself is NOT listed as an entry.

### Error Conditions

| Condition | Response |
|-----------|----------|
| Missing path argument | `ERR 100 Missing path argument` |
| Path not found | `ERR 200 <dos error message>` |
| Path is a file (not a directory) | `ERR 200 Not a directory` |

### Examples

**Non-recursive listing of SYS:S:**

```
C> DIR SYS:S
S> OK
S> FILE	Startup-Sequence	1842	00000004	2024-06-15 14:30:00
S> FILE	Shell-Startup	523	00000004	2024-06-15 14:30:00
S> FILE	User-Startup	0	00000000	2024-06-15 14:30:00
S> .
```

**Recursive listing:**

```
C> DIR SYS:S RECURSIVE
S> OK
S> FILE	Startup-Sequence	1842	00000004	2024-06-15 14:30:00
S> FILE	Shell-Startup	523	00000004	2024-06-15 14:30:00
S> FILE	User-Startup	0	00000000	2024-06-15 14:30:00
S> DIR	Network	0	00000000	2024-06-15 14:30:00
S> FILE	Network/Setup	384	00000004	2024-06-15 14:30:00
S> .
```

**Nonexistent path:**

```
C> DIR SYS:NoSuchDir
S> ERR 200 Object not found
S> .
```

**Empty directory:**

```
C> DIR RAM:EmptyDir
S> OK
S> .
```

---

## STAT

Returns metadata for a file or directory.

### Syntax

```
STAT <path>
```

`<path>` is mandatory. Any trailing text after the path is ignored.

### Response

```
OK
type=<type>
name=<name>
size=<size>
protection=<protection>
datestamp=<datestamp>
comment=<comment>
.
```

The payload consists of key=value lines in a fixed order. Payload lines
are dot-stuffed per [PROTOCOL.md](PROTOCOL.md).

| Key | Description |
|-----|-------------|
| `type` | `file` or `dir` (lowercase) |
| `name` | Entry name (base name from `fib_FileName`) |
| `size` | Size in bytes (integer). 0 for directories. |
| `protection` | 8 lowercase hex digits, zero-padded (raw AmigaOS `fib_Protection` value) |
| `datestamp` | `YYYY-MM-DD HH:MM:SS` (local Amiga time) |
| `comment` | File comment from `fib_Comment`. May be empty; the key is still sent as `comment=`. |

### Error Conditions

| Condition | Response |
|-----------|----------|
| Missing path argument | `ERR 100 Missing path argument` |
| Path not found | `ERR 200 <dos error message>` |

### Examples

**STAT on a file:**

```
C> STAT SYS:S/Startup-Sequence
S> OK
S> type=file
S> name=Startup-Sequence
S> size=1842
S> protection=00000004
S> datestamp=2024-06-15 14:30:00
S> comment=
S> .
```

**STAT on a directory:**

```
C> STAT SYS:S
S> OK
S> type=dir
S> name=S
S> size=0
S> protection=00000000
S> datestamp=2024-06-15 14:30:00
S> comment=
S> .
```

**STAT on nonexistent path:**

```
C> STAT SYS:NoSuchFile
S> ERR 200 Object not found
S> .
```

---

## READ

Downloads a file from the Amiga. The response uses DATA/END chunked binary
framing as described in [PROTOCOL.md](PROTOCOL.md).

### Syntax

```
READ <path>
```

`<path>` is mandatory.

### Response

```
OK <filesize>
DATA <chunk_len>
<raw bytes: exactly chunk_len bytes>
DATA <chunk_len>
<raw bytes: exactly chunk_len bytes>
...
END
.
```

The OK status line includes the total file size in bytes. Each DATA chunk
contains up to 4096 bytes of raw file content. The last chunk may be
shorter. After all chunks, `END` is sent followed by the sentinel.

A zero-length file produces:

```
OK 0
END
.
```

No DATA chunks are sent for an empty file.

### Error Conditions

| Condition | Response |
|-----------|----------|
| Missing path argument | `ERR 100 Missing path argument` |
| File not found | `ERR 200 <dos error message>` |
| Path is a directory | `ERR 300 Is a directory` |
| Open failure | `ERR <code> <dos error message>` |
| Read I/O error mid-transfer | `ERR 300 <message>` (after truncated DATA stream) |

If a Read() I/O error occurs during data transfer, the server sends the
ERR response and sentinel after whatever DATA chunks have already been sent.
The client receives a partial DATA stream followed by the ERR line and
sentinel. The client can detect this because the total bytes received in
DATA chunks will be less than the file size declared in the OK line.

### Examples

**Read a small file:**

```
C> READ SYS:S/Startup-Sequence
S> OK 1842
S> DATA 1842
S> <1842 bytes of raw file content>
S> END
S> .
```

**Read an empty file:**

```
C> READ RAM:empty.txt
S> OK 0
S> END
S> .
```

**Nonexistent file:**

```
C> READ SYS:NoSuchFile
S> ERR 200 Object not found
S> .
```

---

## WRITE

Uploads a file to the Amiga. Uses a READY handshake and DATA/END chunked
binary framing as described in [PROTOCOL.md](PROTOCOL.md). The file is
written atomically via a temporary file and rename.

### Syntax

```
WRITE <path> <total_size>
```

Both `<path>` and `<total_size>` are mandatory. `<total_size>` is a decimal
integer representing the total number of bytes to be transferred.

### Handshake

After validating the command, the server sends either:

- `READY` -- the server is prepared to receive data. The client proceeds to
  send DATA/END chunks. `READY` is NOT an OK/ERR response and is NOT
  followed by a sentinel.
- `ERR <code> <message>` followed by sentinel -- validation failed. The
  client must not send data.

The validation sequence before READY is:

1. Missing arguments: `ERR 100 Usage: WRITE <path> <size>`
2. Invalid size (non-numeric, negative, or exceeds 32-bit signed integer range): `ERR 100 Invalid size`
3. Path too long (exceeds 497 characters; path plus `.amigactld.tmp` suffix
   must fit in a 512-byte buffer): `ERR 300 Path too long`
4. Cannot open temporary file: `ERR <code> <dos error message>`

### Data Transfer

After receiving READY, the client sends DATA/END chunks per
[PROTOCOL.md](PROTOCOL.md). The maximum chunk size is 4096 bytes.

A zero-byte file sends no DATA chunks -- just `END` immediately after
receiving READY.

### Response (success)

```
OK <bytes_written>
.
```

The `<bytes_written>` field confirms the number of bytes written.

### Atomic Write

Data is written to a temporary file (`<path>.amigactld.tmp`) on the same
volume as the target. On successful completion, the temporary file is
renamed to the target path. If the target already exists, it is deleted
before the rename.

### Error Handling During Transfer

**Before END is received** (data transfer in progress): If the server
encounters a write failure, receive failure, malformed DATA header, or
oversized DATA chunk (exceeding 4096 bytes), it deletes the temporary file
and **disconnects the client** (closes the
connection with no response). Sending an ERR response at this point would
corrupt protocol framing because the client's send buffer still contains
unread DATA/END frames.

**After END is received** (data transfer complete, framing is clean): The
server can safely send an error response:

| Condition | Response |
|-----------|----------|
| Size mismatch (received bytes != declared total_size) | `ERR 300 Size mismatch` |
| Rename failure | `ERR <code> <dos error message>` |

The connection remains open after post-END errors.

**Client disconnect during transfer**: The temporary file is deleted. No
response is sent (connection is dead).

### Examples

**Successful write:**

```
C> WRITE RAM:test.txt 13
S> READY
C> DATA 13
C> <13 bytes of raw content>
C> END
S> OK 13
S> .
```

**Zero-byte write:**

```
C> WRITE RAM:empty.txt 0
S> READY
C> END
S> OK 0
S> .
```

**Nonexistent volume:**

```
C> WRITE NOSUCH:file.txt 100
S> ERR 200 Device not mounted
S> .
```

---

## DELETE

Deletes a file or an empty directory.

### Syntax

```
DELETE <path>
```

`<path>` is mandatory.

### Response

```
OK
.
```

### Error Conditions

| Condition | Response |
|-----------|----------|
| Missing path argument | `ERR 100 Missing path argument` |
| Path not found | `ERR 200 <dos error message>` |
| Directory not empty | `ERR 201 <dos error message>` |
| Permission denied (delete-protected) | `ERR 201 <dos error message>` |

### Examples

**Delete a file:**

```
C> DELETE RAM:test.txt
S> OK
S> .
```

**Delete nonexistent file:**

```
C> DELETE RAM:NoSuchFile
S> ERR 200 Object not found
S> .
```

**Delete non-empty directory:**

```
C> DELETE RAM:NonEmptyDir
S> ERR 201 Directory not empty
S> .
```

---

## RENAME

Renames or moves a file or directory. Uses a three-line format: the command
verb is on the first line, and the old and new paths are on subsequent
lines.

### Syntax

```
RENAME
<old_path>
<new_path>
```

The verb line takes NO arguments. If arguments are present after `RENAME`,
the server returns an error. The old and new paths are read from subsequent
lines, one path per line.

### Response

```
OK
.
```

### Error Conditions

| Condition | Response |
|-----------|----------|
| Arguments on verb line | `ERR 100 RENAME takes no arguments; use three-line format` |
| Empty path line (blank old or new path) | ERR 200 (mapped from AmigaOS error) |
| Old path not found | `ERR 200 <dos error message>` |
| Rename across volumes | `ERR 300 <dos error message>` |
| Client disconnect before paths arrive | Connection closed, no response |

If the client disconnects after sending the RENAME verb but before both
path lines arrive, the server discards the partial command and closes the
connection (per [PROTOCOL.md](PROTOCOL.md) multi-line command rules).

### Examples

**Rename a file:**

```
C> RENAME
C> RAM:oldname.txt
C> RAM:newname.txt
S> OK
S> .
```

**Nonexistent source:**

```
C> RENAME
C> RAM:NoSuchFile
C> RAM:newname.txt
S> ERR 200 Object not found
S> .
```

**Rename across volumes (error):**

```
C> RENAME
C> RAM:file.txt
C> WORK:file.txt
S> ERR 300 Rename across devices
S> .
```

**Arguments on verb line:**

```
C> RENAME RAM:old RAM:new
S> ERR 100 RENAME takes no arguments; use three-line format
S> .
```

---

## MAKEDIR

Creates a new directory.

### Syntax

```
MAKEDIR <path>
```

`<path>` is mandatory.

### Response

```
OK
.
```

### Error Conditions

| Condition | Response |
|-----------|----------|
| Missing path argument | `ERR 100 Missing path argument` |
| Already exists | `ERR 202 <dos error message>` |
| Parent not found | `ERR 200 <dos error message>` |
| Permission denied | `ERR 201 <dos error message>` |

### Examples

**Create a directory:**

```
C> MAKEDIR RAM:NewDir
S> OK
S> .
```

**Already exists:**

```
C> MAKEDIR RAM:NewDir
S> ERR 202 Object already exists
S> .
```

---

## PROTECT

Gets or sets the AmigaOS protection bits on a file or directory.

### Syntax

```
PROTECT <path> [<hex>]
```

`<path>` is mandatory. `<hex>` is optional.

- If `<hex>` is absent: **GET mode** -- query the current protection bits.
- If `<hex>` is present: **SET mode** -- set the protection bits, then echo
  the new value.

**Hex format**: 1 to 8 hexadecimal digits (`0-9`, `a-f`, `A-F`), no `0x`
prefix. Leading zeros are optional on input. Output is always 8 lowercase
hex digits, zero-padded. The value represents the raw AmigaOS
`fib_Protection` value (note: owner RWED bits are inverted in this raw
representation).

**Parsing rule**: The hex value, if present, is the LAST whitespace-
delimited token in the arguments. The daemon finds the last token, validates
it as a hex value (1-8 characters, all hex digits), and checks that there
is at least one prior token (the path). If the last token is valid hex and
there is a preceding path, it is treated as the hex value and everything
before it (trimmed) is the path. Otherwise, the entire argument string is
the path (GET mode).

**Ambiguity note**: If a path's final component consists solely of
hexadecimal characters (e.g., `WORK:deadbeef`), the daemon cannot
distinguish it from a hex value. In this case, use GET mode first (the
single-token arguments will be treated as a path), and then use SET mode
with an explicit value to change protection bits.

### Response (GET and SET)

```
OK
protection=<8-hex>
.
```

Both GET and SET return the same response format. SET echoes the newly
applied protection value.

### Error Conditions

| Condition | Response |
|-----------|----------|
| Missing path argument | `ERR 100 Missing path argument` |
| Path not found | `ERR 200 <dos error message>` |
| SetProtection or Examine failure (e.g., write-protected disk) | `ERR <code> <dos error message>` |

### Examples

**GET protection bits on a file:**

```
C> PROTECT SYS:S/Startup-Sequence
S> OK
S> protection=00000004
S> .
```

**SET protection bits and verify:**

```
C> PROTECT RAM:test.txt 0000000f
S> OK
S> protection=0000000f
S> .
```

```
C> PROTECT RAM:test.txt f
S> OK
S> protection=0000000f
S> .
```

**Nonexistent path:**

```
C> PROTECT SYS:NoSuchFile
S> ERR 200 Object not found
S> .
```

---

## SETDATE

Sets the AmigaOS datestamp (modification time) on a file or directory.

### Syntax

```
SETDATE <path> <datestamp>
```

Both `<path>` and `<datestamp>` are mandatory. The datestamp format is
`YYYY-MM-DD HH:MM:SS` (always exactly 19 characters).

**Parsing rule**: The daemon extracts the datestamp as the last 19
characters of the argument string. Everything before those 19 characters
(trimmed of trailing whitespace) is the path. This means the path and
datestamp are separated by at least one space, and the datestamp always
occupies a fixed-width suffix.

### Response

```
OK
datestamp=<YYYY-MM-DD HH:MM:SS>
.
```

The payload is a single key=value line echoing the applied datestamp.
The echoed datestamp is re-formatted from the internal DateStamp
representation for consistency (confirming what was actually applied).

### Error Conditions

| Condition | Response |
|-----------|----------|
| Missing arguments (no path or datestamp) | `ERR 100 Missing arguments` |
| Invalid datestamp format (not `YYYY-MM-DD HH:MM:SS`, out-of-range values) | `ERR 100 Invalid datestamp format` |
| Path not found | `ERR 200 <dos error message>` |
| SetFileDate failure (e.g., write-protected disk) | `ERR <code> <dos error message>` |

Error checking order: argument presence is validated first, then the
datestamp string is parsed and validated, then the DOS call is attempted.

**Datestamp validation**: Year must be >= 1978 (AmigaOS epoch). Month
must be 1-12. Day must be 1 through the number of days in the given
month (accounting for leap years). Hours must be 0-23, minutes 0-59,
seconds 0-59.

### Edge Cases / Notes

- SETDATE precision is limited to 1 second. AmigaOS DateStamp supports
  1/50th second resolution (ticks), but the `YYYY-MM-DD HH:MM:SS`
  format does not carry sub-second precision. Ticks are set to
  `seconds * TICKS_PER_SECOND`.
- SETDATE works on both files and directories.
- The path may contain spaces only in the portion before the final 19
  characters (which are always the datestamp).

### Examples

**Set datestamp on a file:**

```
C> SETDATE RAM:test.txt 2024-06-15 14:30:00
S> OK
S> datestamp=2024-06-15 14:30:00
S> .
```

**Verify via STAT:**

```
C> STAT RAM:test.txt
S> OK
S> type=file
S> name=test.txt
S> size=100
S> protection=00000000
S> datestamp=2024-06-15 14:30:00
S> comment=
S> .
```

**Nonexistent path:**

```
C> SETDATE RAM:NoSuchFile 2024-06-15 14:30:00
S> ERR 200 Object not found
S> .
```

**Invalid datestamp format:**

```
C> SETDATE RAM:test.txt 2024-13-01 00:00:00
S> ERR 100 Invalid datestamp format
S> .
```

**Missing arguments:**

```
C> SETDATE
S> ERR 100 Missing arguments
S> .
```

---

## EXEC

Executes a CLI command on the Amiga. EXEC supports two modes:
synchronous (blocking, with output capture) and asynchronous
(non-blocking, no output capture). The mode is selected by the presence
of the `ASYNC` keyword.

The daemon first checks if the argument text after `EXEC` starts with
the keyword `ASYNC` (case-insensitive). If so, the remainder is passed
to the async handler. Otherwise, the text is parsed for an optional
`CD=` prefix and treated as a synchronous command.

### EXEC (Synchronous)

Executes a CLI command synchronously, captures stdout, and returns the
output along with the command's return code. **This blocks the daemon's
event loop** -- all other clients are blocked until the command
completes. Commands from other clients queue in the TCP receive buffer
and execute sequentially when the event loop resumes.

#### Syntax

```
EXEC [CD=<path>] <command>
```

`<command>` is mandatory. `CD=<path>` is an optional prefix that sets
the working directory for the executed command.

**CD= parsing**: The path extends from `=` to the next whitespace
character. Paths containing spaces are NOT supported with the `CD=`
prefix. The `CD=` prefix is case-insensitive (`cd=`, `Cd=`, and `CD=`
are equivalent).

The `CD=` path affects only the child command's working directory. The
daemon's own current directory is saved before the command and restored
afterward. Subsequent commands (with or without `CD=`) are not affected.

#### Response

```
OK rc=<return_code>
DATA <chunk_len>
<raw bytes: exactly chunk_len bytes>
...
END
.
```

The OK status line includes `rc=<N>` where N is the AmigaOS return code
from the command (0 for success, 5 for WARN, 10 for ERROR, 20 for FAIL).
The captured output follows using DATA/END chunked binary framing (same
framing as READ). Output encoding is ISO-8859-1.

If the command produces no output, the response contains no DATA chunks:

```
OK rc=<return_code>
END
.
```

#### Error Conditions

| Condition | Response |
|-----------|----------|
| Missing command (no text after EXEC or after CD=path) | `ERR 100 Missing command` |
| CD= path not found | `ERR 200 Directory not found` |
| Command execution failed (SystemTags returned -1, e.g., shell unavailable) | `ERR 500 Command execution failed` |

**Note on non-zero return codes**: A command that runs but returns a
non-zero return code (e.g., `failat 1` then a failing command) is NOT an
error from the daemon's perspective. The daemon returns `OK rc=<N>` with
the non-zero rc. Only a failure to launch the command at all produces an
ERR response.

#### Edge Cases / Notes

- The event loop is blocked for the duration of the command. If a
  command hangs, all clients are blocked. The Python client enforces a
  configurable timeout (default 30s) and can reconnect. For
  long-running commands, use `EXEC ASYNC` instead.
- Output is captured to a temporary file (`T:amigactld_exec_<seq>.tmp`)
  and read back after the command completes. Stale temp files from
  previous daemon runs are cleaned up at daemon startup.
- A command that does not exist (e.g., `EXEC nosuchcommand`) does NOT
  produce an ERR response. AmigaOS returns a non-zero rc (typically 20)
  with an error message in stdout (e.g., "Unknown command nosuchcommand").
  The daemon returns `OK rc=20` with the shell's error output.
- The command string is limited by the 4096-byte request line maximum
  (see [PROTOCOL.md](PROTOCOL.md)), minus the `EXEC ` prefix and any
  `CD=<path> ` prefix.

#### Examples

**Simple command:**

```
C> EXEC echo hello
S> OK rc=0
S> DATA 6
S> hello
S> END
S> .
```

(The DATA body is 6 raw bytes: `hello\n`.)

**Multi-line output:**

```
C> EXEC list SYS:S
S> OK rc=0
S> DATA 247
S> <247 bytes of directory listing>
S> END
S> .
```

**Non-zero return code:**

```
C> EXEC search SYS:S nosuchpattern
S> OK rc=5
S> END
S> .
```

**Empty output:**

```
C> EXEC cd SYS:
S> OK rc=0
S> END
S> .
```

**With CD= working directory:**

```
C> EXEC CD=SYS:S list
S> OK rc=0
S> DATA 247
S> <247 bytes of SYS:S listing>
S> END
S> .
```

**CD= path not found:**

```
C> EXEC CD=RAM:NoSuchDir echo hello
S> ERR 200 Directory not found
S> .
```

**Missing command:**

```
C> EXEC
S> ERR 100 Missing command
S> .
```

```
C> EXEC CD=SYS:S
S> ERR 100 Missing command
S> .
```

---

### EXEC ASYNC

Launches a CLI command asynchronously and returns immediately with a
daemon-assigned process ID. The command runs in a separate AmigaOS
process. No output is captured for asynchronous commands.

#### Syntax

```
EXEC ASYNC [CD=<path>] <command>
```

`<command>` is mandatory. `CD=<path>` follows the same parsing rules as
synchronous EXEC (path extends to the next whitespace, spaces in path
not supported, case-insensitive prefix).

#### Response

```
OK <id>
.
```

The OK status line includes the daemon-assigned process ID (a
monotonically incrementing integer, starting at 1, never reused within
a daemon session). No payload lines follow.

#### Error Conditions

| Condition | Response |
|-----------|----------|
| Missing command (no text after ASYNC or after CD=path) | `ERR 100 Missing command` |
| CD= path not found | `ERR 200 Directory not found` |
| Process table full (all 16 slots are RUNNING) | `ERR 500 Process table full` |
| Async exec unavailable (no signal bit allocated at daemon startup) | `ERR 500 Async exec unavailable` |

#### Edge Cases / Notes

- Async processes have no output capture. Stdout and stderr are directed
  to `NIL:`.
- The process table holds up to 16 tracked processes. EXITED entries are
  evicted (oldest first by ID) to make room for new launches. If all 16
  slots are RUNNING, the launch fails.
- Process IDs are session-scoped -- they reset when the daemon restarts.
- Use PROCLIST or PROCSTAT to check on async processes. Use SIGNAL to
  send break signals or KILL as a last resort for hung processes.

#### Examples

**Launch an async command:**

```
C> EXEC ASYNC wait 10
S> OK 1
S> .
```

**Launch with CD=:**

```
C> EXEC ASYNC CD=SYS:S list >T:listing.txt
S> OK 2
S> .
```

**Missing command:**

```
C> EXEC ASYNC
S> ERR 100 Missing command
S> .
```

**Process table full:**

```
C> EXEC ASYNC wait 60
S> ERR 500 Process table full
S> .
```

---

## PROCLIST

Lists all daemon-launched asynchronous processes (both running and
exited). This includes all entries in the process table, regardless of
status.

### Syntax

```
PROCLIST
```

No arguments. Any trailing text after `PROCLIST` is ignored.

### Response

```
OK
<id>\t<command>\t<status>\t<rc>
<id>\t<command>\t<status>\t<rc>
...
.
```

Each payload line contains four tab-separated fields:

| Field | Description |
|-------|-------------|
| `id` | Daemon-assigned process ID (integer) |
| `command` | The command string that was launched |
| `status` | `RUNNING` or `EXITED` |
| `rc` | Return code (integer) when EXITED; `-` when RUNNING |

Payload lines are dot-stuffed per [PROTOCOL.md](PROTOCOL.md).

If no processes have been launched (empty process table), the response
contains no payload lines (just OK and sentinel).

### Error Conditions

None. This command always succeeds.

### Examples

**Processes in various states:**

```
C> PROCLIST
S> OK
S> 1	echo hello	EXITED	0
S> 2	wait 60	RUNNING	-
S> 3	search SYS:S pattern	EXITED	5
S> .
```

**No processes:**

```
C> PROCLIST
S> OK
S> .
```

---

## PROCSTAT

Returns detailed status information for a single daemon-launched
process.

### Syntax

```
PROCSTAT <id>
```

`<id>` is mandatory. It must be a valid integer corresponding to a
tracked process.

### Response

```
OK
id=<id>
command=<command>
status=<status>
rc=<rc>
.
```

The payload consists of key=value lines in a fixed order:

| Key | Description |
|-----|-------------|
| `id` | Daemon-assigned process ID |
| `command` | The command string that was launched |
| `status` | `RUNNING` or `EXITED` |
| `rc` | Return code (integer) when EXITED; `-` when RUNNING |

### Error Conditions

| Condition | Response |
|-----------|----------|
| Missing process ID | `ERR 100 Missing process ID` |
| Invalid process ID (non-numeric) | `ERR 100 Invalid process ID` |
| Process not found (no such ID in table) | `ERR 200 Process not found` |

### Examples

**Running process:**

```
C> PROCSTAT 2
S> OK
S> id=2
S> command=wait 60
S> status=RUNNING
S> rc=-
S> .
```

**Exited process:**

```
C> PROCSTAT 1
S> OK
S> id=1
S> command=echo hello
S> status=EXITED
S> rc=0
S> .
```

**Invalid ID:**

```
C> PROCSTAT abc
S> ERR 100 Invalid process ID
S> .
```

**Process not found:**

```
C> PROCSTAT 999
S> ERR 200 Process not found
S> .
```

---

## SIGNAL

Sends an AmigaOS break signal to a daemon-launched asynchronous process.
This is the standard cooperative mechanism for requesting a process to
stop (equivalent to pressing Ctrl-C in a shell).

### Syntax

```
SIGNAL <id> [<signal>]
```

`<id>` is mandatory. `<signal>` is optional and defaults to `CTRL_C`.

Valid signal names (case-insensitive):

| Signal | AmigaOS Flag |
|--------|-------------|
| `CTRL_C` | `SIGBREAKF_CTRL_C` (default) |
| `CTRL_D` | `SIGBREAKF_CTRL_D` |
| `CTRL_E` | `SIGBREAKF_CTRL_E` |
| `CTRL_F` | `SIGBREAKF_CTRL_F` |

### Response

```
OK
.
```

No payload lines. The signal has been delivered.

### Error Conditions

| Condition | Response |
|-----------|----------|
| Missing process ID | `ERR 100 Missing process ID` |
| Invalid process ID (non-numeric) | `ERR 100 Invalid process ID` |
| Invalid signal name | `ERR 100 Invalid signal name` |
| Process not found (no such ID in table) | `ERR 200 Process not found` |
| Process not running (already EXITED) | `ERR 200 Process not running` |

Error checking order: process ID is validated first (presence, format,
existence), then status (must be RUNNING), then signal name (if
provided). This ensures the most actionable error is reported first --
a bad process ID or a non-running process is reported before an
invalid signal name.

### Edge Cases / Notes

- Signal delivery is asynchronous with respect to the target process's
  execution. The process may not act on the signal immediately.
- Sending a signal to a process does not guarantee it will stop. The
  process may ignore break signals.
- After signaling, use PROCSTAT to poll for the process to transition to
  EXITED state.

### Examples

**Signal with default CTRL_C:**

```
C> SIGNAL 2
S> OK
S> .
```

**Signal with explicit signal name:**

```
C> SIGNAL 2 CTRL_D
S> OK
S> .
```

**Process not running:**

```
C> SIGNAL 1
S> ERR 200 Process not running
S> .
```

**Invalid signal name:**

```
C> SIGNAL 2 HUP
S> ERR 100 Invalid signal name
S> .
```

---

## KILL

Force-terminates a daemon-launched asynchronous process using
`RemTask()`. This is a **last-resort** operation for processes that do
not respond to break signals.

### Syntax

```
KILL <id>
```

`<id>` is mandatory.

### Response

```
OK
.
```

No payload lines. The process has been removed.

### Error Conditions

| Condition | Response |
|-----------|----------|
| Missing process ID | `ERR 100 Missing process ID` |
| Invalid process ID (non-numeric) | `ERR 100 Invalid process ID` |
| Process not found (no such ID in table) | `ERR 200 Process not found` |
| Process not running (already EXITED) | `ERR 200 Process not running` |
| Remote kill not permitted (`ALLOW_REMOTE_SHUTDOWN` is `NO` in config) | `ERR 201 Remote kill not permitted` |

Error checking order: permission is validated first (the
`ALLOW_REMOTE_SHUTDOWN` configuration flag must be `YES`). Then process
ID is validated (presence, format, existence), then status (must be
RUNNING).

### Edge Cases / Notes

- KILL is gated behind the `ALLOW_REMOTE_SHUTDOWN YES` configuration
  flag (the same flag that controls SHUTDOWN). When the flag is `NO`
  (the default), KILL returns `ERR 201` regardless of the process ID.
- **RemTask does not clean up resources.** The killed process's memory,
  file handles, library bases, and other resources are leaked. If the
  process was inside `SystemTags()`, the shell child process may
  continue running as an orphan.
- **Orphan shell processes** launched by `SystemTags()` may later
  attempt to signal their destroyed parent, which can crash AmigaOS.
  This is inherent to RemTask's unsafety and cannot be prevented.
- After KILL, the process table entry transitions to EXITED with
  `rc=-1`.
- If the process completed naturally between the SIGNAL attempt and the
  KILL request (race condition), the daemon detects this via the
  completion flag and simply transitions the slot to EXITED without
  calling RemTask.

### Examples

**Kill a hung process:**

```
C> KILL 2
S> OK
S> .
```

**Verify via PROCSTAT:**

```
C> PROCSTAT 2
S> OK
S> id=2
S> command=wait 60
S> status=EXITED
S> rc=-1
S> .
```

**Not permitted (default configuration):**

```
C> KILL 2
S> ERR 201 Remote kill not permitted
S> .
```

**Process not found (assumes ALLOW_REMOTE_SHUTDOWN YES; otherwise
the response would be ERR 201 Remote kill not permitted):**

```
C> KILL 999
S> ERR 200 Process not found
S> .
```

---

## SYSINFO

Returns system information about the Amiga as key=value pairs. This
includes memory statistics, OS version information, and the bsdsocket
library version.

### Syntax

```
SYSINFO
```

No arguments. Any trailing text after `SYSINFO` is ignored.

### Response

```
OK
chip_free=<bytes>
fast_free=<bytes>
total_free=<bytes>
chip_total=<bytes>
fast_total=<bytes>
exec_version=<major.revision>
kickstart=<revision>
bsdsocket=<major.revision>
.
```

The payload consists of key=value lines in a fixed order:

| Key | Description |
|-----|-------------|
| `chip_free` | Free chip memory in bytes (`AvailMem(MEMF_CHIP)`) |
| `fast_free` | Free fast memory in bytes (`AvailMem(MEMF_FAST)`) |
| `total_free` | Total free memory in bytes (`AvailMem(MEMF_ANY)`) |
| `chip_total` | Total chip memory in bytes (requires exec v39+; omitted on older systems) |
| `fast_total` | Total fast memory in bytes (requires exec v39+; omitted on older systems) |
| `exec_version` | exec.library version, dot-separated (e.g., `40.68`) |
| `kickstart` | Kickstart revision number (e.g., `40`) |
| `bsdsocket` | bsdsocket.library version, dot-separated (e.g., `4.364`) |

Memory values are decimal integers (bytes). Version strings are
dot-separated (major.revision) or plain integers (kickstart).

### Error Conditions

None. This command always succeeds.

### Edge Cases / Notes

- `chip_total` and `fast_total` use the `MEMF_TOTAL` flag, which was
  introduced in AmigaOS 3.1 (exec.library v39). On systems running
  exec.library older than v39, these keys are omitted from the response.
- Memory values are a snapshot at the time of the call and may change
  between calls.
- The daemon requires bsdsocket.library at startup; the `bsdsocket`
  key is always present.

### Examples

**Typical response on an AmigaOS 3.1+ system:**

```
C> SYSINFO
S> OK
S> chip_free=1843200
S> fast_free=12582912
S> total_free=14426112
S> chip_total=2097152
S> fast_total=16777216
S> exec_version=40.68
S> kickstart=40
S> bsdsocket=4.364
S> .
```

---

## ASSIGNS

Lists all logical assigns (device-name-to-path mappings) known to the
system.

### Syntax

```
ASSIGNS
```

No arguments. Any trailing text after `ASSIGNS` is ignored.

### Response

```
OK
<name>:\t<path>
<name>:\t<path>
...
.
```

Each payload line contains two tab-separated fields:

| Field | Description |
|-------|-------------|
| `name:` | Assign name including the trailing colon (e.g., `SYS:`, `S:`, `FONTS:`) |
| `path` | Resolved path for the assign |

Payload lines are dot-stuffed per [PROTOCOL.md](PROTOCOL.md).

**Multi-directory assigns**: If an assign points to multiple directories,
the paths are separated by semicolons within the path field (e.g.,
`LIBS:\tSYS:Libs;WORK:Libs`).

**Late and nonbinding assigns**: For late-binding and nonbinding assigns,
the path field contains the unresolved assignment string rather than a
resolved filesystem path.

### Error Conditions

None. This command always succeeds.

### Examples

**Typical response:**

```
C> ASSIGNS
S> OK
S> SYS:	DH0:
S> S:	DH0:S
S> C:	DH0:C
S> L:	DH0:L
S> LIBS:	DH0:Libs
S> DEVS:	DH0:Devs
S> FONTS:	DH0:Fonts
S> .
```

**Multi-directory assign:**

```
C> ASSIGNS
S> OK
S> LIBS:	DH0:Libs;WORK:Libs
S> ...
S> .
```

---

## PORTS

Lists all active Exec message ports on the system.

### Syntax

```
PORTS
```

No arguments. Any trailing text after `PORTS` is ignored.

### Response

```
OK
<port_name>
<port_name>
...
.
```

Each payload line contains a single port name. Payload lines are
dot-stuffed per [PROTOCOL.md](PROTOCOL.md).

Ports with NULL names are skipped. Control characters (bytes 0x00-0x1F)
in port names are replaced with `?` before sending.

### Error Conditions

None. This command always succeeds.

### Examples

**Typical response:**

```
C> PORTS
S> OK
S> REXX
S> AREXX
S> amigactld
S> .
```

---

## VOLUMES

Lists all currently mounted volumes with disk usage statistics.

### Syntax

```
VOLUMES
```

No arguments. Any trailing text after `VOLUMES` is ignored.

### Response

```
OK
<name>\t<used>\t<free>\t<capacity>\t<blocksize>
<name>\t<used>\t<free>\t<capacity>\t<blocksize>
...
.
```

Each payload line contains five tab-separated fields:

| Field | Description |
|-------|-------------|
| `name` | Volume name (e.g., `System`, `Work`, `RAM Disk`) |
| `used` | Used space in bytes |
| `free` | Free space in bytes |
| `capacity` | Total capacity in bytes |
| `blocksize` | Block size in bytes (e.g., 512) |

Payload lines are dot-stuffed per [PROTOCOL.md](PROTOCOL.md).

Only mounted volumes (those with an active filesystem handler) are
listed. Unmounted volumes are omitted.

All numeric values are decimal integers.

### Error Conditions

None. This command always succeeds.

### Edge Cases / Notes

- A volume that becomes unmounted between the list scan and the
  subsequent Info() probe is silently skipped.
- RAM Disk always has a blocksize but the exact value is implementation-
  dependent.
- Volume names are returned without a trailing colon. To use a volume
  name as a path, append `:` (e.g., `System:`).

### Examples

**Typical response:**

```
C> VOLUMES
S> OK
S> System	42991616	225738752	268730368	512
S> Work	1073741824	3221225472	4294967296	512
S> RAM Disk	0	1048576	1048576	512
S> .
```

---

## TASKS

Lists all running tasks and processes on the system. This is the
AmigaOS equivalent of a Unix `ps` command.

### Syntax

```
TASKS
```

No arguments. Any trailing text after `TASKS` is ignored.

### Response

```
OK
<name>\t<type>\t<priority>\t<state>\t<stacksize>
<name>\t<type>\t<priority>\t<state>\t<stacksize>
...
.
```

Each payload line contains five tab-separated fields:

| Field | Description |
|-------|-------------|
| `name` | Task/process name from `tc_Node.ln_Name`. Tasks with a NULL name are shown as `<unnamed>`. |
| `type` | `TASK` or `PROCESS` (from `tc_Node.ln_Type`: NT_TASK=1 or NT_PROCESS=13) |
| `priority` | Signed integer priority (from `tc_Node.ln_Pri`) |
| `state` | `run` (currently executing), `ready` (ready to run), or `wait` (waiting for a signal) |
| `stacksize` | Stack size in bytes (`tc_SPUpper - tc_SPLower`) |

Payload lines are dot-stuffed per [PROTOCOL.md](PROTOCOL.md).

### Error Conditions

None. This command always succeeds.

### Edge Cases / Notes

- The task list is a snapshot taken under `Forbid()`. All data is copied
  to local buffers before `Permit()` is called and the response is sent.
  The actual task states may have changed by the time the client reads
  the response.
- The currently executing task (the daemon itself, since it calls
  `FindTask(NULL)`) is listed with state `run`.
- Tasks from the `TaskReady` list have state `ready`. Tasks from the
  `TaskWait` list have state `wait`.

### Examples

**Typical response:**

```
C> TASKS
S> OK
S> exec.library	TASK	126	ready	4096
S> input.device	TASK	20	wait	4096
S> amigactld	PROCESS	0	run	65536
S> ramlib	PROCESS	0	wait	4096
S> Shell Process	PROCESS	0	wait	16384
S> .
```

---

## REBOOT

Requests a system reboot. Like SHUTDOWN, this requires the `CONFIRM`
keyword as a safety measure and must be enabled in the daemon
configuration. After sending the response, the daemon calls
`ColdReboot()`, which immediately reboots the AmigaOS system. The daemon
does not perform any cleanup (closing sockets, freeing resources)
because `ColdReboot()` is instantaneous and never returns.

### Syntax

```
REBOOT CONFIRM
```

The first whitespace-delimited token after `REBOOT` must be `CONFIRM`
(case-insensitive). Additional tokens after `CONFIRM` are ignored.

### Response (success)

```
OK Rebooting
.
```

After sending the response, the daemon calls `ColdReboot()`. The client
should expect the TCP connection to be dropped (the remote system is
rebooting).

### Error Conditions

| Condition | Response |
|-----------|----------|
| `CONFIRM` keyword missing or wrong | `ERR 100 REBOOT requires CONFIRM keyword` |
| `ALLOW_REMOTE_REBOOT` is not `YES` in config | `ERR 201 Remote reboot not permitted` |

Error checking order: the `CONFIRM` keyword is validated first. If the
keyword is missing, the server returns ERR 100 regardless of the
`ALLOW_REMOTE_REBOOT` setting.

### Examples

**Successful reboot (ALLOW_REMOTE_REBOOT YES in config):**

```
C> REBOOT CONFIRM
S> OK Rebooting
S> .
(system reboots; TCP connection drops)
```

Note: both the command verb and the `CONFIRM` keyword are case-insensitive.
`reboot confirm`, `Reboot CONFIRM`, and `REBOOT Confirm` are all
equivalent.

**Missing CONFIRM keyword:**

```
C> REBOOT
S> ERR 100 REBOOT requires CONFIRM keyword
S> .
```

**Remote reboot not permitted (default configuration):**

```
C> REBOOT CONFIRM
S> ERR 201 Remote reboot not permitted
S> .
```

---

## UPTIME

Returns the daemon's uptime -- how long since the daemon process started.

### Syntax

```
UPTIME
```

No arguments. Any trailing text after `UPTIME` is ignored.

### Response

```
OK
seconds=<total_seconds>
.
```

The payload is a single key=value line with the total uptime in seconds
as an unsigned integer.

### Error Conditions

None. This command always succeeds.

### Example

```
C> UPTIME
S> OK
S> seconds=3661
S> .
```

---

## AREXX

Sends an ARexx command to a named ARexx port and returns the result.
The command is dispatched asynchronously -- it does NOT block the
daemon's event loop.

### Syntax

```
AREXX <port> <command>
```

`<port>` is the target ARexx port name (e.g., `REXX`, `CNET`).
Case-sensitive (AmigaOS port names are case-sensitive). `<command>` is
the ARexx command string. Everything after the first whitespace-
delimited port name is the command, including any internal whitespace.
The command must be non-empty after trimming whitespace. `AREXX REXX`
(port name with no command text) returns ERR 100.

### Response

```
OK rc=<N>
DATA <chunk_len>
<raw bytes: exactly chunk_len bytes>
...
END
.
```

The OK status line includes `rc=<N>` where N is the return code from
the target port's reply. Standard ARexx conventions use 0 (RC_OK),
5 (RC_WARN), 10 (RC_ERROR), 20 (RC_FATAL), but target ports may
return any integer value.

When rc=0, the DATA body is the ARexx RESULT string returned by the
target port. If the command set a result string, it appears in DATA
chunks. If no result string was set, no DATA chunks are sent (just
END immediately after the OK line). Result strings may contain any
content including embedded newlines.

When rc is non-zero, no DATA chunks are sent (just END immediately
after the OK line). Per the ARexx API, `rm_Result2` is a secondary
error code (a numeric value) when the return code is non-zero, not a
result string pointer.

### Non-Blocking Behavior

AREXX does NOT block the daemon's event loop. The daemon dispatches
the ARexx message to the target port and immediately returns to
servicing other clients. The requesting client is suspended (excluded
from command processing) until the ARexx reply arrives or a timeout
occurs. Other clients can send commands (PING, DIR, etc.) normally
while an AREXX request is pending.

### Timeout

If the target port does not reply within 30 seconds, the daemon
returns ERR 400 to the requesting client and resumes normal service
for that client. The outstanding ARexx message is cleaned up when the
reply eventually arrives.

### Error Conditions

| Condition | Response |
|-----------|----------|
| Missing port name or command | `ERR 100 Usage: AREXX <port> <command>` |
| ARexx not available (rexxsyslib.library not found) | `ERR 500 ARexx not available` |
| No free ARexx slot (all slots pending) | `ERR 500 ARexx busy` |
| Target port not found | `ERR 200 ARexx port not found` |
| Timeout (30 seconds, no reply) | `ERR 400 ARexx command timed out` |

Error codes 500 and 200 are returned synchronously before the daemon
enters the asynchronous wait. Error 400 is returned asynchronously
when the timeout fires.

### Edge Cases / Notes

- Only one AREXX request per client at a time. A client cannot send a
  second AREXX command while waiting for the first reply.
- If the client disconnects while its AREXX is pending, the daemon
  marks the pending slot as orphaned. When the reply eventually
  arrives, it is consumed and freed silently -- never delivered to a
  different client that may have connected to the same slot.
- The maximum concurrent AREXX requests across all clients equals the
  maximum client count (one per client slot).
- Port names are case-sensitive. `REXX` and `rexx` are different
  ports.
- The built-in ARexx interpreter port is typically named `REXX`. It
  can evaluate expressions and run scripts directly (e.g.,
  `return 1+2`).
- A non-zero rc from the target is NOT a daemon-level error. The
  daemon returns `OK rc=<N>` -- the error is in the ARexx execution,
  not in the daemon's handling of the command.

### Examples

**Simple expression:**

```
C> AREXX REXX return 42
S> OK rc=0
S> DATA 2
S> 42
S> END
S> .
```

(The DATA body is 2 raw bytes: `42`.)

**Arithmetic:**

```
C> AREXX REXX return 1+2
S> OK rc=0
S> DATA 1
S> 3
S> END
S> .
```

**No result string (command that doesn't return a value):**

```
C> AREXX REXX call delay(50)
S> OK rc=0
S> END
S> .
```

**ARexx error (non-zero rc, no result string):**

```
C> AREXX REXX x = (
S> OK rc=10
S> END
S> .
```

**Port not found:**

```
C> AREXX NOSUCHPORT hello
S> ERR 200 ARexx port not found
S> .
```

**Missing arguments:**

```
C> AREXX
S> ERR 100 Usage: AREXX <port> <command>
S> .
```

```
C> AREXX REXX
S> ERR 100 Usage: AREXX <port> <command>
S> .
```

---

## TAIL

Streams new content appended to a file. The response is an ongoing
DATA/END stream that continues until the client sends STOP or the file
is deleted.

### Syntax

```
TAIL <path>
```

`<path>` is mandatory. Must be a file, not a directory.

### Response

```
OK <current_size>
DATA <chunk_len>
<raw bytes: exactly chunk_len bytes>
DATA <chunk_len>
<raw bytes: exactly chunk_len bytes>
...
(client sends STOP)
DATA <chunk_len>
<raw bytes: exactly chunk_len bytes>
END
.
```

The OK status line includes the file's current size in bytes at the
time TAIL starts. TAIL begins monitoring from the current end of the
file. No existing file content is sent. The current_size value is
informational only -- it does NOT define how many bytes will be
streamed.

After the OK line, the daemon monitors the file for growth. When new
content is appended, it sends one or more DATA chunks containing the
new bytes. The maximum chunk size is 4096 bytes.

The daemon polls the file each event loop iteration (1-second
resolution). If more than 4096 bytes of new content appear between
polls, the server sends multiple DATA chunks.

The stream continues until the client sends `STOP` or the file is
deleted.

### STOP Handling

When the client sends `STOP\n` (case-insensitive), the daemon:

1. Performs one final file poll to capture any remaining new data.
2. Sends any remaining DATA chunks.
3. Sends `END\n`.
4. Sends the sentinel `.\n`.

After the sentinel, the connection returns to normal command
processing.

### Truncation Detection

If the file size decreases (e.g., the file is overwritten with smaller
content), the daemon resets its read position to the new file end. No
error is generated. Subsequent growth is streamed from the new end.

### File Deletion During Stream

If the file is deleted or becomes inaccessible during streaming, the
daemon sends:

```
ERR 300 File no longer accessible
.
```

No END marker is sent before the ERR line. The ERR line and sentinel
replace the END + sentinel that would normally terminate the stream.
The stream is terminated. The connection returns to normal command
processing.

### Input During TAIL

Any input from the client other than `STOP` (case-insensitive) is
silently discarded. Normal commands cannot be sent while a TAIL stream
is active.

### Error Conditions

| Condition | Response |
|-----------|----------|
| Missing path argument | `ERR 100 Missing path argument` |
| Path not found | `ERR 200 <dos error message>` |
| Path is a directory | `ERR 300 TAIL requires a file, not a directory` |

These errors are returned synchronously (before the streaming phase
begins). The connection remains in normal command processing mode.

### Edge Cases / Notes

- Only one TAIL per client at a time. A client in TAIL mode cannot
  send other commands until STOP is sent.
- If the client disconnects during an active TAIL stream, the daemon
  cleans up the tracking state silently. No error is logged.
- TAIL does not lock the file exclusively. Other processes can write
  to, truncate, or delete the file freely.
- The receiver MUST read exactly `chunk_len` bytes by looping on
  `recv()` before expecting the next DATA, END, or ERR line. TCP does
  not guarantee delivery boundaries. (Same rule as READ.)
- TAIL starts streaming from the current end of the file. Existing
  content is NOT sent. To read existing content, use READ first, then
  TAIL.
- Polling resolution is 1 second (the daemon's event loop timeout).
  Sub-second appends are batched into the next poll.
- If a read or send failure occurs during a DATA chunk transfer, the
  daemon disconnects the client. No ERR response is sent because the
  protocol framing may already be corrupted (partial DATA chunk sent).
  The client should handle unexpected connection closure during TAIL
  streaming.

### Examples

**Start TAIL, receive data, then stop:**

```
C> TAIL RAM:logfile.txt
S> OK 1024
(file grows by 50 bytes)
S> DATA 50
S> <50 bytes of new content>
(file grows by another 100 bytes)
S> DATA 100
S> <100 bytes of new content>
C> STOP
S> END
S> .
```

**TAIL on empty file, then data arrives:**

```
C> TAIL RAM:newlog.txt
S> OK 0
(50 bytes written to file)
S> DATA 50
S> <50 bytes>
C> STOP
S> END
S> .
```

**File deleted during TAIL:**

```
C> TAIL RAM:volatile.txt
S> OK 512
(file is deleted)
S> ERR 300 File no longer accessible
S> .
```

**Nonexistent file:**

```
C> TAIL RAM:NoSuchFile
S> ERR 200 Object not found
S> .
```

**Directory:**

```
C> TAIL SYS:S
S> ERR 300 TAIL requires a file, not a directory
S> .
```

**Missing path:**

```
C> TAIL
S> ERR 100 Missing path argument
S> .
```

**STOP with no new data:**

```
C> TAIL RAM:logfile.txt
S> OK 1024
C> STOP
S> END
S> .
```

---

## STOP

Terminates an active TAIL stream. STOP is a contextual command: it is
only valid during an active TAIL stream.

### Syntax

```
STOP
```

No arguments. Case-insensitive.

### Behavior During TAIL

Terminates the active stream. The server sends any remaining DATA
chunks, then END, then the sentinel. After the sentinel, the
connection returns to normal command processing. See the
[TAIL](#tail) command for full details.

### Behavior Outside TAIL

STOP is not recognized as a command outside of an active TAIL stream.
Since STOP is not in the normal command dispatch table, it produces:

```
ERR 100 Unknown command
.
```

This is not a special case -- it is the standard behavior for any
unrecognized command verb.

### Examples

**During TAIL:**

```
(TAIL is active)
C> STOP
S> END
S> .
C> PING
S> OK
S> .
```

**Outside TAIL:**

```
C> STOP
S> ERR 100 Unknown command
S> .
```

---

## Error Handling

### Unknown Command

Any command verb that the server does not recognize produces a syntax error.
This includes commands from future phases that have not yet been
implemented.

#### Response

```
ERR 100 Unknown command
.
```

The connection remains open. The client may send further commands.

#### Example

```
C> FOOBAR
S> ERR 100 Unknown command
S> .
C> PING
S> OK
S> .
```

### Oversized Command Lines

If the client sends 4096 or more bytes without a newline, the server
treats this as a protocol violation.

#### Behavior

1. The server sends an error response:

   ```
   ERR 100 Command too long
   .
   ```

2. The server enters discard mode: all incoming bytes are discarded until
   a newline (`\n`) is received.

3. After discarding through the newline, the connection returns to normal
   operation. The client may send further commands.

This means the oversized "command" is never executed, but the connection
is not terminated. The client can recover by ensuring its next transmission
after the error includes a newline.

#### Example

```
C> AAAA....(4096+ bytes without newline)
S> ERR 100 Command too long
S> .
C> ....(remaining overflow bytes)....\n
(server discards everything up to and including the newline)
C> PING
S> OK
S> .
```

The server sends the error response as soon as its receive buffer fills,
even if the client has not finished sending. Remaining bytes are discarded
as they arrive, up to and including the next newline.

---

## Not Yet Implemented

Phase 5 (Polish) adds no new server commands. It focuses on client-side
improvements including an interactive REPL and distribution packaging.
