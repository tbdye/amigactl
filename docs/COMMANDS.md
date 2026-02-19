# amigactl Command Reference

This document is the authoritative specification for all amigactl commands.
Code is written to satisfy this spec. Reviewers validate implementations
against it. Tests verify the documented behavior.

**Version**: 0.2.0 (Phase 2 -- file operations)

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

The version string matches the daemon version (currently `0.2.0`).

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
S> AMIGACTL 0.2.0
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
S> amigactld 0.2.0
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

The following commands are planned for future phases. Sending any of them
in the current version produces `ERR 100 Unknown command`.

### Phase 3: Execution and System Info

| Command | Description |
|---------|-------------|
| `EXEC [CD=<path>] <command>` | Execute CLI command, capture output (CD= sets working directory) |
| `EXEC ASYNC [CD=<path>] <command>` | Launch command asynchronously, return process ID |
| `PROCLIST` | List daemon-launched processes (tab-separated: id, command, status, rc) |
| `PROCSTAT <id>` | Status of a specific tracked process (key=value pairs: id, command, status, rc) |
| `SIGNAL <id> [CTRL_C\|CTRL_D\|CTRL_E\|CTRL_F]` | Send break signal to tracked process (default CTRL_C) |
| `KILL <id>` | Force-terminate tracked process via RemTask() (requires `ALLOW_REMOTE_SHUTDOWN YES`) |
| `SETDATE <path> <datestamp>` | Set file/directory datestamp (`YYYY-MM-DD HH:MM:SS` format) |
| `SYSINFO` | System information (key=value pairs) |
| `ASSIGNS` | List logical assigns |
| `PORTS` | List active Exec message ports |
| `VOLUMES` | List mounted volumes with free space and capacity |
| `TASKS` | List running tasks/processes |

### Phase 4: ARexx

| Command | Description |
|---------|-------------|
| `AREXX <port> <command>` | Send ARexx command to named port |
| `TAIL <path>` | Stream new content appended to a file (ongoing response) |
| `STOP` | Terminate an active TAIL stream (contextual, only valid during TAIL) |

Phase 5 (Polish) adds no new server commands. It focuses on client-side
improvements including an interactive shell mode and distribution packaging.

These commands will be fully specified in this document as each phase is
implemented. Until then, the server has no knowledge of them and they are
treated identically to any other unknown command.
