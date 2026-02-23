# Agent Guide: Using amigactl for Amiga Automation

Practical reference for AI agents interacting with AmigaOS via amigactl.
For full command specifications (arguments, responses, error conditions,
wire format examples), see [COMMANDS.md](COMMANDS.md). For wire protocol
details, see [PROTOCOL.md](PROTOCOL.md).

## Quick Start

```python
from amigactl import AmigaConnection

with AmigaConnection("192.168.6.200") as amiga:
    print(amiga.version())
    data = amiga.read("SYS:S/Startup-Sequence")
    print(data.decode("iso-8859-1"))
```

`AmigaConnection(host, port=6800, timeout=30)` -- port and timeout are
optional. Always use the context manager for automatic cleanup.

If running from the repository without installing:

    PYTHONPATH=/path/to/amigactl/client python3 your_script.py

## Method Reference

Every Python method maps to a daemon command documented in
[COMMANDS.md](COMMANDS.md). Consult COMMANDS.md for argument formats,
response fields, error conditions, and example transcripts.

### Connection

| Method | Returns | Docs |
|--------|---------|------|
| `version()` | `str` -- version string | [VERSION](COMMANDS.md#version) |
| `ping()` | None | [PING](COMMANDS.md#ping) |
| `uptime()` | `int` -- seconds | [UPTIME](COMMANDS.md#uptime) |
| `shutdown()` | `str` -- info string | [SHUTDOWN](COMMANDS.md#shutdown) |
| `reboot()` | `str` -- info string | [REBOOT](COMMANDS.md#reboot) |

### Files

| Method | Returns | Docs |
|--------|---------|------|
| `read(path)` | `bytes` | [READ](COMMANDS.md#read) |
| `write(path, data: bytes)` | `int` -- bytes written | [WRITE](COMMANDS.md#write) |
| `dir(path, recursive=False)` | `list[dict]` | [DIR](COMMANDS.md#dir) |
| `stat(path)` | `dict` | [STAT](COMMANDS.md#stat) |
| `delete(path)` | None | [DELETE](COMMANDS.md#delete) |
| `rename(old_path, new_path)` | None | [RENAME](COMMANDS.md#rename) |
| `makedir(path)` | None | [MAKEDIR](COMMANDS.md#makedir) |
| `protect(path, value=None)` | `str` -- hex bits | [PROTECT](COMMANDS.md#protect) |
| `setdate(path, datestamp=None)` | `str` -- new datestamp | [SETDATE](COMMANDS.md#setdate) |

### Execution

| Method | Returns | Docs |
|--------|---------|------|
| `execute(command, timeout=None, cd=None)` | `(int, str)` -- rc, output | [EXEC](COMMANDS.md#exec) |
| `execute_async(command, cd=None)` | `int` -- process ID | [EXEC](COMMANDS.md#exec) |
| `proclist()` | `list[dict]` | [PROCLIST](COMMANDS.md#proclist) |
| `procstat(proc_id)` | `dict` -- id, command, status, rc | [PROCSTAT](COMMANDS.md#procstat) |
| `signal(proc_id, sig="CTRL_C")` | None | [SIGNAL](COMMANDS.md#signal) |
| `kill(proc_id)` | None -- **dangerous, see Gotchas** | [KILL](COMMANDS.md#kill) |

### System

| Method | Returns | Docs |
|--------|---------|------|
| `sysinfo()` | `dict` -- all values are `str` | [SYSINFO](COMMANDS.md#sysinfo) |
| `assigns()` | `dict` -- name -> path | [ASSIGNS](COMMANDS.md#assigns) |
| `assign(name, path=None, mode=None)` | None | [ASSIGN](COMMANDS.md#assign) |
| `volumes()` | `list[dict]` -- int values for sizes | [VOLUMES](COMMANDS.md#volumes) |
| `ports()` | `list[str]` | [PORTS](COMMANDS.md#ports) |
| `tasks()` | `list[dict]` | [TASKS](COMMANDS.md#tasks) |

### ARexx and Streaming

| Method | Returns | Docs |
|--------|---------|------|
| `arexx(port, command, timeout=35)` | `(int, str)` -- rc, result | [AREXX](COMMANDS.md#arexx) |
| `tail(path, callback)` | None -- **blocks, see Gotchas** | [TAIL](COMMANDS.md#tail) |
| `stop_tail()` | None | [TAIL](COMMANDS.md#tail) |

## Amiga Domain Knowledge

Context that an AI agent likely does not have. Understanding this is
essential for correct interaction with AmigaOS.

### Path conventions

- Paths use `volume:path/to/file` format (e.g., `SYS:S/Startup-Sequence`)
- `/` alone after a path component means parent directory (like `..`)
- The daemon does NOT translate `..` -- use `/` or resolve client-side
- Path matching is case-insensitive on most Amiga filesystems

### Common volumes

| Volume | Purpose | Unix analogy |
|--------|---------|--------------|
| `SYS:` | Boot volume | `/` |
| `RAM:` | RAM disk (fast, volatile, lost on reboot) | tmpfs |
| `T:` | Temporary directory (assign, usually `RAM:T`) | `/tmp` |
| `S:` | Startup scripts (usually `SYS:S`) | `/etc/init.d` |
| `C:` | System commands (usually `SYS:C`) | `/usr/bin` |
| `LIBS:` | Shared libraries (usually `SYS:Libs`) | `/usr/lib` |
| `DEVS:` | Device drivers (usually `SYS:Devs`) | `/dev` |
| `WORK:` | Secondary storage (convention) | `/home` |

### Text encoding

AmigaOS uses ISO-8859-1 (Latin-1), not UTF-8. This applies to
filenames, file contents, command output, and ARexx results.

- `read()` and `tail()` callbacks return raw `bytes` -- decode with
  `"iso-8859-1"`
- `execute()` and `arexx()` return pre-decoded `str`
- `write()` takes `bytes` -- encode with `"iso-8859-1"`

### Return codes

AmigaOS uses different severity levels than Unix:

| Code | Meaning | Equivalent |
|------|---------|------------|
| 0 | Success | same as Unix |
| 5 | WARN -- non-fatal warning | -- |
| 10 | ERROR -- operation failed | non-zero exit |
| 20 | FAIL -- serious failure | non-zero exit |

`execute()` returns the rc as-is. Check for `rc != 0` or `rc >= 10`
depending on how strict you want to be.

### Protection bits

AmigaOS protection bits are inverted for RWED: a SET bit means DENIED
(opposite of Unix). The `protect()` method returns/accepts raw hex.
See [PROTECT](COMMANDS.md#protect) for the bit layout.

## Key Patterns

Patterns that are non-obvious from the method signatures.

### Text file round-trip

```python
data = amiga.read(path)
text = data.decode("iso-8859-1")
# ... modify text ...
amiga.write(path, text.encode("iso-8859-1"))
```

### Async process polling

```python
pid = amiga.execute_async("copy SYS:C WORK:backup ALL")
import time
while True:
    info = amiga.procstat(pid)
    if info["status"] != "running":
        break
    time.sleep(2)
print("Exit code:", info["rc"])
```

### Tail streaming

```python
def on_data(chunk):
    print(chunk.decode("iso-8859-1"), end="")

amiga.tail("RAM:logfile.txt", on_data)
# Blocks until file deletion or stop_tail() from another thread.
# Use KeyboardInterrupt (Ctrl-C) to break out.
```

### Error recovery

```python
from amigactl import AmigaConnection, AmigactlError, NotFoundError

try:
    data = amiga.read(path)
except NotFoundError:
    amiga.write(path, b"")  # create if missing
except AmigactlError as e:
    print(f"Amiga error: {e.message} (code {e.code})")
```

## Error Handling

All exception classes are importable from `amigactl`.

- `AmigactlError` -- base class (`.code: int`, `.message: str`)
  - `CommandSyntaxError` (100) -- malformed/unknown command
  - `NotFoundError` (200) -- file/path/port not found
  - `PermissionDeniedError` (201) -- access denied
  - `AlreadyExistsError` (202) -- already exists
  - `RemoteIOError` (300) -- I/O error on Amiga side
  - `RemoteTimeoutError` (400) -- operation timed out
  - `InternalError` (500) -- daemon internal error
- `ProtocolError` -- wire protocol violation (client-side)
  - `ServerError` -- server returned ERR status
  - `BinaryTransferError` -- ERR during binary transfer

## Gotchas

1. **ISO-8859-1, not UTF-8.** All text on AmigaOS is Latin-1. See
   "Text encoding" above.

2. **Synchronous exec blocks the connection.** `execute()` blocks the
   daemon's event loop for that client. Set `timeout` for safety.

3. **KILL is dangerous.** `kill()` uses RemTask() which leaks resources
   (open files, memory). Always try `signal(proc_id, "CTRL_C")` first.

4. **TAIL occupies the connection.** No other commands can be sent while
   `tail()` is active (except `stop_tail()`). Use a separate connection
   for concurrent work. `tail()` blocks the calling thread.

5. **Max 8 simultaneous clients.** Close connections when done.

6. **No .. in paths.** Use `/` for parent directory (Amiga convention).
