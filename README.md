# amigactl

Remote access daemon for AmigaOS.

Traditional remote access tools give you a shell session or a file transfer
channel -- not structured, programmatic access to AmigaOS internals. amigactl
is a lightweight TCP daemon that exposes file operations, CLI command execution,
ARexx dispatch, and system introspection (assigns, volumes, ports, tasks) over
a simple text protocol with machine-parseable responses. A Python client library
provides first-class scripting support for automation on trusted LANs and
emulator setups.

## Architecture

```
Host                              Amiga
+-----------------+               +------------------+
| amigactl CLI    |--TCP:6800---->| amigactld        |
| (Python)        |               | (m68k C daemon)  |
+-----------------+               |                  |
                                  | - File I/O (DOS) |
+-----------------+               | - EXEC / Proc    |
| Python library  |--TCP:6800---->| - ARexx dispatch |
| (host)          |               | - System queries |
+-----------------+               +------------------+
```

- **amigactld**: Amiga daemon, C, cross-compiled with m68k-amigaos-gcc.
  Multi-client via WaitSelect event loop (up to 8 simultaneous clients).
- **amigactl**: Python client library and CLI tool (host-side).
- **Protocol**: Text commands, dot-stuffed sentinel termination, length-prefixed
  binary for file data. ISO-8859-1 encoding.
- **Security**: IP-based ACL from `S:amigactld.conf`

## Current Status

**Phase 3 -- EXEC, Process Management, and System Info.** The daemon accepts
TCP connections, checks IP ACLs, sends a banner, and handles lifecycle commands
(VERSION, PING, QUIT, SHUTDOWN), eight file commands (DIR, STAT, READ, WRITE,
DELETE, RENAME, MAKEDIR, PROTECT), synchronous and asynchronous command execution
(EXEC, EXEC ASYNC), process management (PROCLIST, PROCSTAT, SIGNAL, KILL),
system introspection (SYSINFO, ASSIGNS, PORTS, VOLUMES, TASKS), and datestamp
setting (SETDATE). The Python client library and CLI support all commands. ARexx
dispatch is planned for a later phase.

## Requirements

### Amiga (daemon)

- AmigaOS 2.0 or later
- 68020 or later CPU
- A TCP/IP stack providing `bsdsocket.library` (Roadshow, AmiTCP, Miami, or
  emulator bsdsocket emulation)

### Host (client)

- Python 3.8 or later

## Building the Daemon

### Prerequisites

The [m68k-amigaos-gcc](https://github.com/AmigaPorts/m68k-amigaos-gcc)
cross-compiler must be installed at `/opt/amiga`. The build uses `-noixemul`
(libnix) and targets 68020.

### Compile

```
make
```

This produces the `amigactld` binary in the project root. Copy it to the Amiga
(e.g., via SMB share, FTP, or any file transfer method available).

## Daemon Configuration

amigactld reads its configuration from `S:amigactld.conf` on the Amiga. If the
file is missing, the daemon starts with default settings (port 6800, all IPs
allowed, remote shutdown disabled).

### Config file format

```
# amigactld configuration
#
# Lines starting with # are comments.
# Keywords are case-insensitive.

# TCP port to listen on (default: 6800)
PORT 6800

# Allowed client IPs. One per line. If no ALLOW lines are present,
# all IPs are permitted (development mode).
ALLOW 192.168.6.100
ALLOW 192.168.6.50

# Allow the SHUTDOWN command from remote clients (default: NO).
# When NO, SHUTDOWN CONFIRM returns ERR 201.
ALLOW_REMOTE_SHUTDOWN NO
```

An example config is provided in `dist/amigactld.conf.example`.

## Running the Daemon

### From the CLI

```
amigactld
amigactld PORT 6900
amigactld CONFIG S:my_custom.conf
```

ReadArgs template: `PORT/N,CONFIG/K`

The daemon prints a startup message and listens for connections. Press Ctrl-C
to shut down cleanly.

### From Workbench

Double-click the amigactld icon. The daemon opens a console window
(`CON:0/20/640/200/amigactld/AUTO/CLOSE/WAIT`) and runs until the window is
closed or a SHUTDOWN command is received. Configuration can be set via Tool
Types (`PORT`, `CONFIG`) in the icon's Info window.

## Python Client

### Installation

```
pip install -e client/
```

This installs the `amigactl` library and CLI tool. No external dependencies
are required (stdlib only).

### Library usage

```python
from amigactl import AmigaConnection

with AmigaConnection("192.168.6.200") as amiga:
    print(amiga.version())          # "amigactld 0.3.0"
    amiga.ping()

    # File operations
    entries = amiga.dir("SYS:S")
    data = amiga.read("SYS:S/Startup-Sequence")
    amiga.write("RAM:test.txt", b"hello world")
    info = amiga.stat("RAM:test.txt")
    amiga.rename("RAM:test.txt", "RAM:renamed.txt")
    amiga.delete("RAM:renamed.txt")
    amiga.makedir("RAM:mydir")
    prot = amiga.protect("RAM:mydir")

    # Command execution
    rc, output = amiga.execute("list SYS:S")
    proc_id = amiga.execute_async("wait 30")
    procs = amiga.proclist()
    info = amiga.procstat(proc_id)
    amiga.signal(proc_id)

    # System info
    sysinfo = amiga.sysinfo()
    assigns = amiga.assigns()
    volumes = amiga.volumes()
    ports = amiga.ports()
    tasks = amiga.tasks()
    amiga.setdate("RAM:test.txt", "2026-02-19 12:00:00")
```

The host and port can also be set via the `AMIGACTL_HOST` and `AMIGACTL_PORT`
environment variables.

### CLI usage

```
amigactl --host 192.168.6.200 version
amigactl --host 192.168.6.200 ping
amigactl --host 192.168.6.200 shutdown    # sends SHUTDOWN CONFIRM
amigactl --host 192.168.6.200 ls SYS:S
amigactl --host 192.168.6.200 stat SYS:S/Startup-Sequence
amigactl --host 192.168.6.200 cat SYS:S/Startup-Sequence > startup.txt
amigactl --host 192.168.6.200 get SYS:S/Startup-Sequence startup.txt
amigactl --host 192.168.6.200 put localfile.txt RAM:test.txt
amigactl --host 192.168.6.200 rm RAM:test.txt
amigactl --host 192.168.6.200 mv RAM:old.txt RAM:new.txt
amigactl --host 192.168.6.200 mkdir RAM:newdir
amigactl --host 192.168.6.200 chmod RAM:file.txt
amigactl --host 192.168.6.200 chmod RAM:file.txt 0f
amigactl --host 192.168.6.200 touch RAM:file.txt 2026-02-19 12:00:00
amigactl --host 192.168.6.200 exec echo hello
amigactl --host 192.168.6.200 run wait 30
amigactl --host 192.168.6.200 ps
amigactl --host 192.168.6.200 status 1
amigactl --host 192.168.6.200 signal 1
amigactl --host 192.168.6.200 sysinfo
amigactl --host 192.168.6.200 assigns
amigactl --host 192.168.6.200 volumes
amigactl --host 192.168.6.200 ports
amigactl --host 192.168.6.200 tasks
```

The `--host` flag defaults to the `AMIGACTL_HOST` environment variable, or
`192.168.6.200` if unset. `--port` defaults to `AMIGACTL_PORT` or `6800`.

## Protocol Overview

amigactl uses a text-based protocol over TCP. Clients send one command per
line (max 4096 bytes, excluding the terminating newline). The daemon accepts
both LF and CR LF line endings for telnet compatibility. The server responds
with a status line (`OK [info]\n` or `ERR <code> <message>\n`), optional
payload lines, and
a dot-on-a-line sentinel (`.\n`) that terminates every response. Payload lines
starting with `.` are dot-stuffed (SMTP-style). Binary file data uses
length-prefixed DATA/END chunking within the sentinel-terminated envelope.

Full details are in [docs/PROTOCOL.md](docs/PROTOCOL.md) (wire format, framing,
encoding, binary transfer) and [docs/COMMANDS.md](docs/COMMANDS.md) (per-command
syntax, responses, error conditions, and example transcripts).

## Testing

Tests are a pytest suite that exercises the daemon over a live TCP connection.
Start amigactld on the Amiga, then run:

```
pytest tests/ --host 192.168.6.200 -v
```

The `--host` and `--port` options (or `AMIGACTL_HOST` / `AMIGACTL_PORT`
environment variables) specify the daemon to test against.

Some tests require manual execution (ACL rejection from a non-allowed IP,
Ctrl-C shutdown) and are marked as skipped with instructions in their
docstrings.

## Repository Layout

```
amigactl/
+-- README.md
+-- LICENSE                          # GPL v3
+-- Makefile                         # m68k cross-compilation
+-- daemon/
|   +-- main.c                       # Entry, startup, event loop
|   +-- daemon.h                     # Shared structures, constants, error codes
|   +-- net.c / net.h                # Socket helpers, protocol I/O
|   +-- config.c / config.h          # Config file parsing, ACL
|   +-- file.c / file.h              # File operation command handlers
|   +-- exec.c / exec.h              # EXEC and process management
|   +-- sysinfo.c / sysinfo.h        # System info command handlers
+-- client/
|   +-- amigactl/
|   |   +-- __init__.py              # AmigaConnection class
|   |   +-- __main__.py              # CLI tool
|   |   +-- protocol.py              # Wire protocol helpers
|   +-- pyproject.toml
+-- tests/
|   +-- conftest.py                  # Fixtures, CLI options
|   +-- test_connection.py           # Connection, auth, lifecycle
|   +-- test_file.py                 # File operation tests
|   +-- test_exec.py                 # Exec and process management tests
|   +-- test_sysinfo.py              # System info tests
+-- dist/
|   +-- amigactld.conf.example       # Config template
+-- docs/
|   +-- PROTOCOL.md                  # Wire protocol spec
|   +-- COMMANDS.md                  # Per-command spec
```

## Roadmap

### Phase 1: Connection Skeleton (complete)

TCP server with WaitSelect event loop, IP ACL, banner, and lifecycle commands
(VERSION, PING, QUIT, SHUTDOWN). Python client library and CLI. Documentation
and test suite.

### Phase 2: File Operations (complete)

DIR, STAT, READ, WRITE, DELETE, RENAME, MAKEDIR, and PROTECT commands. Chunked
binary transfer for READ/WRITE. Atomic writes via temp file and rename.

### Phase 3: EXEC, Process Management, and System Info (complete)

CLI command execution with captured output (EXEC). Asynchronous process
launching with signal and kill support (EXEC ASYNC, PROCLIST, PROCSTAT, SIGNAL,
KILL). System introspection (SYSINFO, ASSIGNS, PORTS, VOLUMES, TASKS).
Datestamp setting (SETDATE).

### Phase 4: ARexx (next)

Non-blocking ARexx command dispatch to named ports, with timeout handling and
reply matching via WaitSelect signal integration.

### Phase 5: Polish and Interactive Shell

Interactive shell mode with persistent connection, readline support, and
human-friendly command names. LHA packaging for Amiga distribution, Workbench
icon, final documentation pass.

## License

This project is licensed under the GNU General Public License v3.0. See
[LICENSE](LICENSE) for details.
