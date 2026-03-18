# ARexx

ARexx is the inter-process communication scripting language built into
AmigaOS. Applications expose named message ports that accept ARexx
commands, making it possible to script and automate almost any program
on the Amiga. The amigactl shell's `arexx` command sends ARexx
messages to these ports remotely, bridging the host machine into the
Amiga's IPC ecosystem.


## Overview

The `arexx` command sends a command string to a named ARexx message
port on the Amiga and waits for the reply. The daemon dispatches the
message asynchronously -- it does not block on the reply, so other
connected clients can continue issuing commands while one client waits
for an ARexx response. When the target application replies, the daemon
forwards the return code and any result string back to the requesting
client.

The built-in `REXX` port (provided by the ARexx interpreter) can
evaluate arbitrary ARexx expressions. Application-specific ports
(e.g., a text editor's port) accept whatever commands the application
defines. Use the `ports` command to discover which ARexx ports are
currently available.


## Usage

### Syntax

```
arexx PORT COMMAND...
```

### Arguments

| Argument | Description |
|----------|-------------|
| `PORT` | Name of the target ARexx message port. Case-sensitive. Use `ports` to list available ports. |
| `COMMAND` | The ARexx command string to send. Everything after the port name is passed verbatim. |

Both arguments are required. Omitting either one prints a usage
message and does nothing.

### Return Values

Every ARexx reply carries a return code (`rc`) and an optional result
string. The shell displays both:

```bash
amiga@192.168.6.228:SYS:> arexx REXX return 42
42
Return code: 0
```

The semantics of `rc` follow the ARexx convention:

| rc | Meaning |
|----|---------|
| 0 | Success. The result string, if any, is the value set by the target (e.g., the return value of an ARexx expression). |
| >0 | Error. The result string is empty. The numeric `rc` is set by the target application or by the ARexx interpreter (e.g., a syntax error). |

When `rc` is 0 and the target returned no result string (e.g., the
command had no `return` statement), only the return code line is
printed:

```bash
amiga@192.168.6.228:SYS:> arexx REXX nop
Return code: 0
```


## Examples

### Basic Expressions

The built-in `REXX` port evaluates ARexx expressions directly. This
is the simplest way to test that ARexx is working:

```bash
amiga@192.168.6.228:SYS:> arexx REXX return 1+2
3
Return code: 0
```

String results are returned verbatim:

```bash
amiga@192.168.6.228:SYS:> arexx REXX return "hello world"
hello world
Return code: 0
```

### Sending Commands to Applications

Many Amiga applications create an ARexx port when they start. The
port name is typically the application's name or a variation of it.
To find available ports:

```bash
amiga@192.168.6.228:SYS:> ports
REXX
amigactld
```

Then send commands to the port using whatever command vocabulary the
application defines:

```bash
amiga@192.168.6.228:SYS:> arexx MYEDITOR OPEN "Work:file.txt"
Return code: 0
```

### Handling Errors

A non-zero return code indicates that the target application or the
ARexx interpreter rejected the command. For example, a syntax error
in an expression sent to the `REXX` port:

```bash
amiga@192.168.6.228:SYS:> arexx REXX x = (
Return code: 15
```

If the target port does not exist, the daemon returns an error
immediately without waiting for a reply:

```bash
amiga@192.168.6.228:SYS:> arexx NONEXISTENT_PORT test
Error: ARexx port not found
```


## How It Works

### Non-Blocking Dispatch

When a client sends an `arexx` command, the daemon constructs an
ARexx message (`RexxMsg` with `RXCOMM | RXFF_RESULT | RXFF_STRING`
flags), locates the target port under `Forbid()`/`Permit()` to
prevent it from disappearing mid-lookup, and dispatches the message
via `PutMsg()`. The daemon then returns to its event loop immediately
-- it does not block waiting for the reply.

The requesting client is suspended (no further commands are accepted
on that connection) until the reply arrives, times out, or the client
disconnects. All other clients can send commands normally during this
time.

When the target application replies, the daemon's reply port signals
the event loop. The daemon extracts the return code from
`rm_Result1` and, if `rc` is 0, the result string from `rm_Result2`
(which is only valid as a pointer to an argstring when `rc` is 0 --
when `rc` is non-zero, `rm_Result2` is a numeric secondary error
code and must not be dereferenced as a pointer). The daemon then
sends the response to the client and resumes normal command
processing on that connection.

### Timeout Handling

The daemon enforces a 30-second timeout on ARexx replies. If the
target application does not reply within 30 seconds, the daemon
sends an error to the client and releases the connection for further
commands. The underlying ARexx message slot remains allocated until
the reply eventually arrives (or the daemon shuts down), because
the reply port must stay valid for the target to reply to.

### One Request Per Client

Each client connection can have at most one outstanding ARexx request
at a time. The daemon maintains a pool of pending slots equal to the
maximum number of clients (8). If a client disconnects while an
ARexx request is pending, the slot is marked as orphaned -- the
reply is consumed and discarded silently when it arrives, freeing
the slot for reuse.


## Error Reference

| Error | Code | Message | Cause |
|-------|------|---------|-------|
| Missing arguments | ERR 100 | `Usage: AREXX <port> <command>` | No port name, no command, or both missing. |
| Port not found | ERR 200 | `ARexx port not found` | The named port does not exist. The target application may not be running. |
| Timeout | ERR 400 | `ARexx command timed out` | The target did not reply within 30 seconds. |
| ARexx not available | ERR 500 | `ARexx not available` | The daemon could not open `rexxsyslib.library`. ARexx may not be installed. |
| All slots busy | ERR 500 | `ARexx busy` | All pending slots are occupied by outstanding requests from other clients. |
| Message creation failed | ERR 500 | `Failed to create ARexx message` | Memory allocation failure on the Amiga. |
| Argstring creation failed | ERR 500 | `Failed to create ARexx argstring` | Memory allocation failure on the Amiga. |


## Limitations

- **Result string truncation.** The daemon copies the result string
  into a 4 KB static buffer. Result strings longer than 4096 bytes
  are silently truncated.

- **One concurrent request per client.** A connection can only have
  one pending ARexx request. The shell enforces this naturally (it
  waits for the response before accepting the next command), but
  programmatic clients using the wire protocol must not send a second
  `AREXX` command while one is outstanding.

- **Port names are case-sensitive.** `REXX` and `rexx` are different
  ports. The daemon passes the port name to `FindPort()` exactly as
  given. Use the `ports` command to see the exact names.

- **No port discovery from this command.** The `arexx` command sends
  to a port you already know by name. To list available ports, use
  the separate `ports` command.

- **Timeout is not configurable.** The 30-second timeout is hardcoded
  in the daemon. Long-running ARexx scripts that take more than 30
  seconds to complete will always time out.


## Related Documentation

- [command-execution.md](command-execution.md) -- Synchronous and
  asynchronous CLI command execution (uses the same response framing
  as ARexx).
- [system-commands.md](system-commands.md) -- The `ports` command for
  listing available message ports.
