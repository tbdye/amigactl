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
| `read(path, offset=None, length=None)` | `bytes` | [READ](COMMANDS.md#read) |
| `write(path, data: bytes)` | `int` -- bytes written | [WRITE](COMMANDS.md#write) |
| `append(path, data: bytes)` | `int` -- bytes appended | [APPEND](COMMANDS.md#append) |
| `dir(path, recursive=False)` | `list[dict]` | [DIR](COMMANDS.md#dir) |
| `stat(path)` | `dict` | [STAT](COMMANDS.md#stat) |
| `copy(src, dst, noclone=False, noreplace=False)` | None | [COPY](COMMANDS.md#copy) |
| `delete(path)` | None | [DELETE](COMMANDS.md#delete) |
| `rename(old_path, new_path)` | None | [RENAME](COMMANDS.md#rename) |
| `makedir(path)` | None | [MAKEDIR](COMMANDS.md#makedir) |
| `protect(path, value=None)` | `str` -- hex bits | [PROTECT](COMMANDS.md#protect) |
| `setdate(path, datestamp=None)` | `str` -- new datestamp | [SETDATE](COMMANDS.md#setdate) |
| `checksum(path)` | `dict` -- crc32, size | [CHECKSUM](COMMANDS.md#checksum) |
| `setcomment(path, comment)` | None | [SETCOMMENT](COMMANDS.md#setcomment) |

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
| `sysinfo()` | `dict` -- see keys below | [SYSINFO](COMMANDS.md#sysinfo) |
| `libver(name)` | `dict` -- name, version | [LIBVER](COMMANDS.md#libver) |
| `env(name)` | `str` -- the variable value | [ENV](COMMANDS.md#env) |
| `setenv(name, value=None, volatile=False)` | None | [SETENV](COMMANDS.md#setenv) |
| `assigns()` | `dict` -- name -> path | [ASSIGNS](COMMANDS.md#assigns) |
| `assign(name, path=None, mode=None)` | None | [ASSIGN](COMMANDS.md#assign) |
| `volumes()` | `list[dict]` -- int values for sizes | [VOLUMES](COMMANDS.md#volumes) |
| `ports()` | `list[str]` | [PORTS](COMMANDS.md#ports) |
| `tasks()` | `list[dict]` | [TASKS](COMMANDS.md#tasks) |
| `devices()` | `list[dict]` -- name, version | [DEVICES](COMMANDS.md#devices) |
| `capabilities()` | `dict` -- max\_clients and max\_cmd\_len are `int`, rest are `str` | [CAPABILITIES](COMMANDS.md#capabilities) |

**`sysinfo()` keys:**

| Key | Type | Description |
|-----|------|-------------|
| `chip_free` | `int` | Free chip memory (bytes) |
| `fast_free` | `int` | Free fast memory (bytes) |
| `total_free` | `int` | Total free memory (bytes) |
| `chip_total` | `int` | Total chip memory (bytes; omitted on exec < v39) |
| `fast_total` | `int` | Total fast memory (bytes; omitted on exec < v39) |
| `chip_largest` | `int` | Largest contiguous chip block (bytes) |
| `fast_largest` | `int` | Largest contiguous fast block (bytes) |
| `exec_version` | `str` | exec.library version (e.g., `"40.68"`) |
| `kickstart` | `str` | Kickstart revision (e.g., `"40"`) |
| `bsdsocket` | `str` | bsdsocket.library version (e.g., `"4.364"`) |

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
- Every absolute path starts with a volume or assign name followed by a
  colon. There is no leading `/` -- `SYS:C/Dir` is correct, `/C/Dir` is
  not
- `/` alone after a path component means parent directory (like `..`)
- The daemon does NOT translate `..` -- use `/` or resolve client-side
- Path matching is case-insensitive on most Amiga filesystems, but
  case-preserving. `SYS:s/startup-sequence` works, but `stat()` returns
  the name as stored on disk

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

### Environment variable persistence

AmigaOS has a two-tier environment variable system unlike anything on
Unix:

- `ENV:` -- RAM-based, per-session. Variables here are available to
  running programs but lost on reboot.
- `ENVARC:` -- Disk-based, persistent. Variables here survive reboots.
  On boot, `ENVARC:` is copied to `ENV:`.

`setenv(name, value)` writes to both `ENV:` and `ENVARC:` (persistent).
`setenv(name, value, volatile=True)` writes to `ENV:` only (lost on
reboot). The `volatile` flag is for temporary values you do not want to
survive a reboot.

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

**Important:** Protection bits on AmigaOS are advisory, not enforced
by the filesystem kernel. Any program can read or write any file
regardless of its protection bits. The only exception is the delete-
protect bit (bit 0), which the filesystem does enforce. Do not rely on
protection bits for access control.

### File comments

AmigaOS supports a per-file comment string (called a "filenote") stored
in the filesystem metadata. There is no Unix equivalent. Comments are
up to 79 characters, cannot contain tab characters (0x09), and are
returned by `stat()` in the `comment` key. Use `setcomment(path, text)`
to set and `setcomment(path, "")` to clear.

### Assign types

AmigaOS distinguishes between **volumes** and **assigns**. A volume
(e.g., `SYS:`, `RAM:`, `WORK:`) is a mounted filesystem.  An assign
(e.g., `C:`, `LIBS:`, `T:`) is a logical name that maps to one or more
directories on volumes. `volumes()` lists the former; `assigns()` lists
the latter.

The `assign()` method supports three modes:

| Mode | Usage | Behavior |
|------|-------|----------|
| Lock (default) | `assign("TEST:", "WORK:test")` | Locks the path immediately. Fails if path does not exist. |
| Late | `assign("TEST:", "WORK:test", mode="late")` | Path is not resolved until first access. Useful for paths on removable media. |
| Add | `assign("TEST:", "WORK:extra", mode="add")` | Adds a directory to an existing multi-directory assign. The assign must already exist. |

Remove an assign by calling `assign("TEST:")` with no path.

## Key Patterns

Patterns that are non-obvious from the method signatures.

### Text file round-trip

```python
data = amiga.read(path)
text = data.decode("iso-8859-1")
# ... modify text ...
amiga.write(path, text.encode("iso-8859-1"))
```

### Partial file read

```python
# Read 100 bytes starting at offset 1000
data = amiga.read(path, offset=1000, length=100)
```

The `offset` and `length` parameters are optional.  If only `offset` is
given, the read continues to end of file.  If only `length` is given,
the read starts from the beginning.

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

### Deploy and reboot

Writing a binary and rebooting is the standard deployment pattern. The
FFS buffer cache can lose recent writes if the reboot happens too
quickly (see Gotcha 10). Sleep before rebooting to let the cache flush.

```python
import time

amiga.write("C:amigactld", new_binary)
time.sleep(5)  # let FFS buffer cache flush to disk
amiga.reboot()
```

### Reconnect after reboot

After `reboot()`, the connection is dead. The Amiga takes 15-45 seconds
to boot depending on hardware and startup sequence. Poll `connect()`
in a retry loop.

```python
import time
from amigactl import AmigaConnection

amiga.reboot()
time.sleep(20)  # minimum boot time

for attempt in range(12):
    try:
        amiga = AmigaConnection(host)
        amiga.connect()
        break
    except OSError:
        time.sleep(5)
else:
    raise RuntimeError("Amiga did not come back after reboot")

print(amiga.version())
# Remember to call amiga.close() when done, or use a context manager.
```

### Verify file integrity after write

Use `checksum()` with Python's `zlib.crc32()` to verify a file was
written correctly.

```python
import zlib

data = open("local_file", "rb").read()
amiga.write("C:my_program", data)
result = amiga.checksum("C:my_program")
local_crc = "{:08x}".format(zlib.crc32(data) & 0xFFFFFFFF)
assert result["crc32"] == local_crc, "CRC mismatch"
assert result["size"] == len(data), "Size mismatch"
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

## Raw Protocol Access

The `amigactl.protocol` module exposes the wire protocol primitives used
internally by `AmigaConnection`. Import it for edge cases not covered
by the high-level API (e.g., reading the `truncated` field from ENV
responses, or implementing custom command sequences).

```python
from amigactl.protocol import (
    ENCODING,           # "iso-8859-1"
    read_line,          # read_line(sock) -> str
    read_response,      # read_response(sock) -> (status, info, payload)
    send_command,       # send_command(sock, cmd) -> None
    recv_exact,         # recv_exact(sock, nbytes) -> bytes
    read_binary_response,  # read DATA/END chunks after OK
    read_exec_response,    # read EXEC-style OK rc=N + DATA/END
    send_data_chunks,      # send DATA/END chunks to server
    ProtocolError,         # wire protocol violation
    ServerError,           # server returned ERR status
    BinaryTransferError,   # ERR during binary transfer
)
```

`read_response()` returns `(status, info, payload_lines)` where
`status` is `"OK"` or `"ERR"`, `info` is the remainder of the status
line, and `payload_lines` is a list of dot-unstuffed strings. This gives
access to fields that `AmigaConnection` methods may not expose (e.g.,
the `truncated=true` line in an ENV response).

See [PROTOCOL.md](PROTOCOL.md) for the wire format specification.

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

7. **SETENV VOLATILE is a keyword.** You cannot set a variable named
   "VOLATILE" via `setenv("VOLATILE", "value")`. The daemon interprets
   it as the volatile mode flag. Use `execute("setenv VOLATILE value")`
   as a workaround if this edge case matters.

8. **APPEND requires an existing file.** Unlike `write()`, which creates
   a new file, `append()` fails with `NotFoundError` if the file does
   not exist.  Create it with `write(path, b"")` first if needed.

9. **checksum() returns hex string, not int.** The `crc32` field is an
   8-character lowercase hex string (e.g., `"a1b2c3d4"`), not a Python
   integer.  Use `int(result["crc32"], 16)` to convert if needed.

10. **reboot() can lose recent writes.** AmigaOS FFS uses a buffer cache.
    Writes may not be flushed to disk when `reboot()` calls ColdReboot().
    Sleep at least 5 seconds between the last `write()` and `reboot()`.
    See the "Deploy and reboot" pattern above.

11. **RAM: is volatile.** Everything on `RAM:` (and `T:`, which usually
    points to `RAM:T`) is lost on reboot. Do not store anything there
    that must survive a restart.

12. **env() silently truncates large values.** The daemon reads
    environment variables into a 4096-byte buffer. Values longer than
    4095 characters are truncated. The response includes a
    `truncated=true` field when this happens, but `env()` returns only
    the value string and discards it. Use the raw protocol
    (`read_response()`) if you need to detect truncation.
