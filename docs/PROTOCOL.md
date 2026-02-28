# amigactl Wire Protocol Specification

This document is the authoritative specification for the amigactl wire
protocol.  Daemon and client implementations MUST conform to the behavior
described here.  Where COMMANDS.md specifies per-command semantics, this
document specifies framing, encoding, connection lifecycle, error codes,
and binary transfer conventions that apply to all commands.

## Encoding

All text on the wire is **ISO-8859-1** (Latin-1).  This is the native
character encoding of AmigaOS.  Clients MUST encode outgoing text as
ISO-8859-1 and decode incoming text as ISO-8859-1.

Bytes 0x00 through 0x1F (except 0x0A LF and 0x0D CR) have no defined
meaning in the protocol and MUST NOT appear in command arguments or
text payload data.  Binary DATA chunk bodies are exempt from this
restriction (see Binary Data Framing).  Their presence in a text line
is not an error -- the daemon processes the line as-is -- but behavior
is undefined.

## Transport

The protocol runs over a single TCP connection.  The default listening
port is **6800**.  The daemon accepts up to **8 simultaneous clients**.

Connections beyond the limit are closed immediately by the server with no
banner and no error message.

## Connection Lifecycle

### 1. Connect

The client opens a TCP connection to the daemon's listening port.

### 2. ACL Check

The daemon checks the client's IP address against its access control list
(configured via `ALLOW` directives in `S:amigactld.conf`).  If the ACL is
non-empty and the client's IP is not listed, the daemon closes the
connection immediately.  No banner is sent.  No error message is sent.

If the ACL is empty (no `ALLOW` directives), all IPs are permitted.

### 3. Banner

On successful connection, the daemon sends a banner line:

```
AMIGACTL <version>\n
```

`<version>` is a dotted version string (e.g., `0.7.0`).  The client
SHOULD read and validate the banner before sending any commands.  The
banner is not followed by a sentinel -- it is a single line, not a
response envelope.

### 4. Command/Response

The client sends commands and reads responses, one at a time.  See the
Request Format and Response Format sections below.

### 5. Disconnect

Either side may close the connection at any time:

- **Client-initiated (graceful):** The client sends `QUIT\n`.  The daemon
  responds with `OK Goodbye\n.\n` and then closes the connection.

- **Client-initiated (abrupt):** The client closes the TCP connection
  without sending QUIT.  The daemon detects EOF on the next read, cleans
  up client state, and frees the slot.

- **Server-initiated:** The daemon closes the connection after processing
  a QUIT command, after a SHUTDOWN sequence, or on Ctrl-C shutdown.  The
  client detects EOF on the next read.

## Line Endings

The canonical line ending is **LF** (`\n`, 0x0A).

For telnet compatibility, the daemon also accepts **CR LF** (`\r\n`,
0x0D 0x0A).  When the daemon encounters a CR immediately preceding a LF
in a request line, it strips the CR before processing.  CR characters in
other positions are not stripped and are treated as part of the line
content.

The daemon MUST send responses using bare LF line endings.  Clients
SHOULD accept bare LF.  Clients MAY also tolerate CR LF in responses for
robustness, but the daemon will never send CR LF.

## Empty Lines

Request lines that are empty or contain only whitespace (spaces and tabs)
before the line ending are silently ignored.  The daemon sends no
response for such lines.  This allows clients to send blank lines as
keepalives or for readability without triggering errors.

## Request Format

A request is a single line of text:

```
COMMAND [arguments]\n
```

- **COMMAND** is a verb (e.g., `VERSION`, `PING`, `DIR`).  Commands are
  **case-insensitive**: `ping`, `Ping`, and `PING` are equivalent.

- **Arguments** follow the command, separated by a space.  Argument
  syntax is command-specific and defined in COMMANDS.md.

- The maximum request line length is **4096 bytes**, including the
  command, arguments, and any trailing CR, but excluding the terminating
  LF.

### Oversized Request Handling

If the daemon's receive buffer fills (4096 bytes) without encountering a
LF, the request is oversized.  The daemon:

1. Sends `ERR 100 Command too long\n.\n`.
2. Enters **discard mode** for this client: all subsequent incoming data
   is discarded until a LF is found.
3. Once a LF is found, the daemon exits discard mode.  The connection is
   now ready for the next command.

The connection is NOT closed.  The client can recover by ensuring its
oversized data is terminated with LF, then resuming normal commands.

## Response Format

Every command produces exactly one response.  Every response is
terminated by a **sentinel line** (see below).  There are two response
types:

### Success Response

```
OK [info]\n
[payload line 1]\n
[payload line 2]\n
...
.\n
```

- The status line begins with `OK`.  It may optionally include
  additional information after a space (e.g., `OK Goodbye`,
  `OK rc=0`, `OK 14832`).  If there is no additional information, the
  line is just `OK\n`.

- Zero or more **payload lines** follow.  Payload content is
  command-specific and defined in COMMANDS.md.  Payload lines are
  subject to dot-stuffing (see below).

- The response is terminated by the **sentinel**: a line consisting of a
  single period followed by LF (`.\n`).

### Error Response

```
ERR <code> <message>\n
.\n
```

- The status line begins with `ERR`, followed by a space, a numeric
  error code, a space, and a human-readable error message.

- Error responses have **no payload lines**.  The sentinel immediately
  follows the status line.

### The Sentinel Invariant

**Every response -- both OK and ERR -- is terminated by a sentinel line
(`.\n`).  No exceptions.**

This invariant simplifies client implementations: a client reads lines
until it encounters a line that is exactly `.\n` (after dot-unstuffing).
At that point, the response is complete.

The sentinel is always the last line of a response.  Nothing follows it
until the client sends the next command.

**Exception: Streaming responses.**  The TAIL and TRACE commands produce
ongoing streaming responses where DATA chunks may arrive at any time
after the OK status line, for an indefinite duration.  The sentinel is
sent only when the stream terminates (via client STOP or server error).
During a TAIL or TRACE stream, the client MAY send `STOP` to request
termination.  See the ARexx and Streaming Wire Formats section for
details.

## Dot-Stuffing

Payload lines may contain arbitrary text, including lines that begin with
a period.  To prevent ambiguity with the sentinel, the protocol uses
**dot-stuffing** (the same mechanism used by SMTP):

- **Sending (daemon):** If a payload line begins with `.` (0x2E), the
  daemon prepends an additional `.` before sending.  A payload line
  `.foo` is sent as `..foo\n`.  A payload line consisting of a single `.`
  is sent as `..\n`.

- **Receiving (client):** When the client reads a line that begins with
  `.`, it checks whether the line is exactly `.\n` (the sentinel).  If
  the line begins with `..`, the client removes the leading `.` to
  recover the original payload line.

The sentinel `.\n` is never dot-stuffed.  It is always exactly two bytes:
0x2E 0x0A.

Dot-stuffing applies only to **payload lines** in OK responses.  The
status line (`OK ...` or `ERR ...`) and the banner are never dot-stuffed.

## Binary Data Framing

Some commands (READ, WRITE, APPEND, EXEC, AREXX, TAIL, TRACE) transfer
binary or large data that cannot be reliably represented as dot-stuffed
text
lines.  These commands use **DATA/END chunked framing** within the
response envelope.

### READ Response (Server to Client)

```
OK <filesize>\n
DATA <chunk_len>\n
<raw bytes: exactly chunk_len bytes>
DATA <chunk_len>\n
<raw bytes: exactly chunk_len bytes>
...
END\n
.\n
```

- The OK status line includes the total file size in bytes as its info
  field.

- Each `DATA` line specifies the number of raw bytes that immediately
  follow.  The chunk length is a decimal integer.  The maximum chunk
  size is **4096 bytes**.

- After the `DATA` line's LF, exactly `chunk_len` raw bytes follow.
  These bytes are **not** line-oriented and are **not** dot-stuffed.
  They may contain any byte value including 0x0A, 0x0D, and 0x2E.

- The receiver MUST read exactly `chunk_len` bytes by looping on
  `recv()` before expecting the next `DATA` or `END` line.  TCP does
  not guarantee delivery boundaries.

- After the last chunk, the daemon sends `END\n` followed by the
  sentinel `.\n`.

- A zero-length file produces: `OK 0\nEND\n.\n` (no DATA chunks).

### WRITE Request (Client to Server)

```
Client: WRITE <path> <total_size>\n
Server: READY\n
Client: DATA <chunk_len>\n<raw bytes>
Client: DATA <chunk_len>\n<raw bytes>
...
Client: END\n
Server: OK <bytes_written>\n.\n
```

- The client sends the WRITE command with the target path and total file
  size.

- The server validates the path and responds with `READY\n` to indicate
  it is prepared to receive data.  `READY` is not an OK/ERR response --
  it is a handshake signal and is not followed by a sentinel.

- The client then sends DATA chunks in the same format as READ but in
  the client-to-server direction.  The same chunk size limit (4096
  bytes) applies.

- After the last chunk, the client sends `END\n`.

- The server writes the data to a temporary file on the same volume as
  the target path (`<path>.amigactld.tmp`), then atomically renames it
  to the target.  On success, the server responds with
  `OK <bytes_written>\n.\n`.

- **Error during transfer:** If the server encounters an error during
  the data transfer phase (disk full, I/O error), it sends
  `ERR <code> <message>\n.\n`.  The client MUST be prepared to receive
  an ERR response instead of expecting more READY or OK messages.

- **Partial WRITE on disconnect:** If the client disconnects before
  sending END, the server deletes the temporary file.

### APPEND Request (Client to Server)

APPEND uses the same READY handshake as WRITE.  The command line is
`APPEND <path> <size>\n` where `<path>` is the file to append to and
`<size>` is the number of bytes to append.  The file must already exist.

```
Client: APPEND <path> <size>\n
Server: READY\n
Client: DATA <chunk_len>\n<raw bytes>
Client: DATA <chunk_len>\n<raw bytes>
...
Client: END\n
Server: OK <bytes_appended>\n.\n
```

The handshake, DATA/END chunking, and error handling are identical to
WRITE.  The only difference is that APPEND opens the file for appending
rather than creating a new file via a temporary rename.

### EXEC Response

EXEC uses the same DATA/END framing for captured command output:

```
OK rc=<return_code>\n
DATA <chunk_len>\n
<raw bytes>
...
END\n
.\n
```

The `rc` field in the OK line is the AmigaOS return code from the
executed command.  Because synchronous EXEC blocks until the command
completes, the return code and all captured output are available when
the response begins.

**EXEC ASYNC** does not use DATA/END framing.  It returns immediately
with `OK <id>\n.\n` where `<id>` is the daemon-assigned process ID.
No output is captured for asynchronous commands.

AREXX uses the same response framing as EXEC.  TAIL uses ongoing
DATA/END streaming.  See ARexx and Streaming Wire Formats below for
details.

## Pipelining

**Pipelining is not supported.**  The client MUST wait for the complete
response to a command (up to and including the `.\n` sentinel) before
sending the next command.  The daemon processes one command at a time per
client.

Sending a second command before the first response is complete produces
undefined behavior.  The daemon may interpret the second command as part
of the first command's input (for commands that accept multi-line input
like RENAME), or it may be buffered and processed after the first
response -- no guarantee is made.

**Exception: TAIL and TRACE streaming.**  During an active TAIL or TRACE
stream, the client sends `STOP\n` to terminate the stream, even though
the response sentinel has not yet been received.  These are the only
cases where the client sends data before a response is complete.  See
the TAIL and TRACE command specifications in COMMANDS.md for details.

## System Query and Execution Wire Formats

The following commands handle system queries, process management, and
command execution.  The wire formats for these commands use three
patterns already defined in this protocol:

### Key=Value Payload (Text Lines, Dot-Stuffed)

Used by **PROCSTAT**, **SYSINFO**, **SETDATE**, **CHECKSUM**, **LIBVER**,
**ENV**, **CAPABILITIES**, **TRACE STATUS**: the payload consists of
`key=value` lines in a fixed order, one per line, subject to
dot-stuffing.  These follow the same framing as other text-payload
commands (STAT, PROTECT).

### Tab-Separated Payload (Text Lines, Dot-Stuffed)

Used by **PROCLIST**, **ASSIGNS**, **VOLUMES**, **TASKS**, **DEVICES**:
the payload consists of lines with tab-separated fields, subject to
dot-stuffing.  These follow the same framing as DIR.

**PORTS** uses one port name per payload line (no tabs), dot-stuffed.

### Simple OK/ERR (No Payload)

Used by **SIGNAL**, **KILL**, **COPY**, **SETCOMMENT**, **SETENV**,
**TRACE ENABLE**, **TRACE DISABLE**: the response is `OK\n.\n` on
success or `ERR <code> <message>\n.\n` on failure.  No payload lines.
These follow the same framing as DELETE and MAKEDIR.

### DATA/END Binary Framing

**EXEC** (synchronous) uses DATA/END chunked binary framing as
described in the EXEC Response section above.  **EXEC ASYNC** does not
use binary framing -- it returns `OK <id>\n.\n` with no payload.

See COMMANDS.md for the specific fields and semantics of each command.

## ARexx and Streaming Wire Formats

AREXX, TAIL, and TRACE use the following wire format patterns:

**AREXX** uses DATA/END binary framing for the result string, identical
to EXEC.  The OK status line includes `rc=<N>` where N is the ARexx
return code.  See COMMANDS.md for details.

**TAIL** uses an ongoing DATA/END streaming response.  Unlike READ
(where the total size is known upfront) or EXEC (where the command
completes before the response begins), TAIL's response has no
predetermined end.  The stream is terminated by the client sending
`STOP\n`, after which the server sends END and the sentinel.  During
the stream, the server may send DATA chunks at any time (when the
monitored file grows).  The receiver must be prepared for an indefinite
stream of DATA chunks interspersed with arbitrary delays.

If the server encounters an error during the stream (e.g., file
deleted), it sends `ERR <code> <message>\n.\n`, terminating the stream.

**TRACE** uses the same ongoing DATA/END streaming pattern as TAIL.
Each DATA chunk contains a single tab-separated event line.  The stream
is terminated by the client sending `STOP\n`.  If the atrace module is
unloaded during streaming, the server sends a comment line
(`# ATRACE SHUTDOWN`) as a DATA chunk, followed by END and the sentinel.
See COMMANDS.md for the full TRACE command specification.

## Error Codes

| Code | Name              | Meaning                                        |
|------|-------------------|------------------------------------------------|
| 100  | Syntax Error      | Malformed command, unknown command, missing or invalid arguments, command too long |
| 200  | Not Found         | File, directory, path, or ARexx port does not exist |
| 201  | Permission Denied | ACL rejection, operation not permitted (e.g., remote shutdown disabled) |
| 202  | Already Exists    | Target already exists (e.g., MAKEDIR on existing directory) |
| 300  | I/O Error         | Filesystem I/O failure, disk full, read/write error |
| 400  | Timeout           | Operation timed out (e.g., ARexx reply not received within deadline) |
| 500  | Internal Error    | Unexpected daemon error, resource exhaustion     |

Error codes are stable.  New codes may be added in future versions but
existing codes will not change meaning.

Clients SHOULD handle unknown error codes gracefully (e.g., treat any
unrecognized code as a generic error).

## Multi-Line Commands

Most commands are single-line.  A small number of commands require
additional input lines after the command verb:

- **RENAME** uses a three-line format: the verb line is followed by the
  old path and the new path on separate lines.  Path lines follow the
  same line-ending and max-length rules as request lines.  See
  COMMANDS.md for details.

- **COPY** uses the same three-line format as RENAME: the verb line
  (with optional flags such as `NOCLONE` and `NOREPLACE`) is followed
  by the source path and destination path on separate lines.  See
  COMMANDS.md for details.

If the client disconnects mid-command (after sending the verb but before
all required input lines), the server discards the partial command and
closes the connection.

## Tab-Separated Arguments

Most commands separate arguments with spaces.  **SETCOMMENT** uses a
tab character (0x09) to separate the path from the comment:

```
SETCOMMENT <path>\t<comment>\n
```

The tab delimiter is required because file comments may contain spaces.
An empty comment (tab followed by nothing) clears the existing comment.

## Protocol Versioning

The banner line (`AMIGACTL <version>`) communicates the daemon version.

Since version 0.7.0, the daemon also supports a `CAPABILITIES` command
that returns a `protocol=1.0` field for explicit protocol version
negotiation.  Clients can use `CAPABILITIES` to discover supported
commands and protocol limits at runtime.

Clients SHOULD parse the version from the banner and use it to determine
feature availability.  Unrecognized commands always produce
`ERR 100 Unknown command\n.\n`, so a client can safely attempt commands
from a newer protocol version against an older daemon.

## Summary of Framing Rules

| Element            | Format                        | Dot-stuffed? | Followed by sentinel? |
|--------------------|-------------------------------|--------------|----------------------|
| Banner             | `AMIGACTL <ver>\n`            | No           | No                   |
| Request            | `COMMAND [args]\n`            | N/A          | N/A                  |
| OK status line     | `OK [info]\n`                 | No           | --                   |
| ERR status line    | `ERR <code> <message>\n`      | No           | --                   |
| Payload line       | `<text>\n`                    | Yes          | --                   |
| DATA header        | `DATA <len>\n`                | No           | --                   |
| DATA body          | `<raw bytes>`                 | No           | --                   |
| END marker         | `END\n`                       | No           | --                   |
| READY handshake    | `READY\n`                     | No           | No                   |
| Sentinel           | `.\n`                         | N/A          | (is the sentinel)    |

## Example Session

The following transcript shows a complete session.  `C:` denotes bytes
sent by the client; `S:` denotes bytes sent by the server.  `\n`
represents a single LF byte (0x0A).

```
[TCP connection established]
S: AMIGACTL 0.7.0\n

C: PING\n
S: OK\n
S: .\n

C: VERSION\n
S: OK\n
S: amigactld 0.7.0\n
S: .\n

C: SYSINFO\n
S: OK\n
S: chip_free=1843200\n
S: fast_free=12582912\n
S: total_free=14426112\n
S: chip_total=2097152\n
S: fast_total=16777216\n
S: chip_largest=460488\n
S: fast_largest=13036136\n
S: exec_version=40.68\n
S: kickstart=40\n
S: bsdsocket=4.364\n
S: .\n

C: EXEC echo hello\n
S: OK rc=0\n
S: DATA 6\n
S: hello\n
S: END\n
S: .\n

C: AREXX REXX return 6*7\n
S: OK rc=0\n
S: DATA 2\n
S: 42
S: END\n
S: .\n

C: TAIL RAM:server.log\n
S: OK 1024\n
S: DATA 53\n
S: [2026-02-20 14:30:01] Client connected from 10.0.0.5\n
S: DATA 37\n
S: [2026-02-20 14:30:05] User logged in\n
C: STOP\n
S: END\n
S: .\n

C: FOOBAR\n
S: ERR 100 Unknown command\n
S: .\n

C: QUIT\n
S: OK Goodbye\n
S: .\n
[TCP connection closed by server]
```
