# amigactl Command Reference

This document is the authoritative specification for all amigactl commands.
Code is written to satisfy this spec. Reviewers validate implementations
against it. Tests verify the documented behavior.

**Version**: 0.1.0 (Phase 1 -- connection lifecycle commands only)

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

The version string matches the daemon version (currently `0.1.0`).

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
S> AMIGACTL 0.1.0
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
S> amigactld 0.1.0
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

### Phase 2: File Operations

| Command | Description |
|---------|-------------|
| `DIR <path> [RECURSIVE]` | List directory contents (RECURSIVE includes subdirectories) |
| `STAT <path>` | File/directory metadata |
| `READ <path>` | Download file (chunked binary) |
| `WRITE <path> <size>` | Upload file (chunked binary, atomic via temp+rename) |
| `DELETE <path>` | Delete file or empty directory |
| `RENAME` | Rename/move (multi-line: verb, old path, new path) |
| `MAKEDIR <path>` | Create directory |
| `PROTECT <path> [<hex>]` | Get or set protection bits |

### Phase 3: Execution and System Info

| Command | Description |
|---------|-------------|
| `EXEC [CD=<path>] <command>` | Execute CLI command, capture output (CD= sets working directory) |
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
