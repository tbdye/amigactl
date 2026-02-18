# amigactl

Remote access daemon for AmigaOS.

There is no SSH equivalent for AmigaOS. Managing an emulated Amiga (or a
real one) remotely requires stitching together VNC, FTP, and telnet -- none of
which provide programmatic access to the filesystem, ARexx, or system
introspection. amigactl fills this gap: a single TCP daemon that exposes file
operations, CLI command execution, ARexx dispatch, and system queries over a
clean, simple protocol.

## Architecture

```
Host                              Amiga
+-----------------+               +------------------+
| amigactl CLI    |--TCP:6800---->| amigactld        |
| (Python)        |               | (m68k C daemon)  |
+-----------------+               |                  |
                                  | - File I/O (DOS) |
+-----------------+               | - EXEC (System)  |
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

**Phase 1 -- Connection Skeleton.** The daemon accepts TCP connections, checks
IP ACLs, sends a banner, and handles four lifecycle commands: VERSION, PING,
QUIT, and SHUTDOWN. The Python client library and CLI support these commands.
File operations, EXEC, ARexx, and system queries are planned for later phases.

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
    print(amiga.version())   # "amigactld 0.1.0"
    amiga.ping()
```

The host and port can also be set via the `AMIGACTL_HOST` and `AMIGACTL_PORT`
environment variables.

### CLI usage

```
amigactl --host 192.168.6.200 version
amigactl --host 192.168.6.200 ping
amigactl --host 192.168.6.200 shutdown    # sends SHUTDOWN CONFIRM
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
+-- client/
|   +-- amigactl/
|   |   +-- __init__.py              # AmigaConnection class
|   |   +-- __main__.py              # CLI tool
|   |   +-- protocol.py              # Wire protocol helpers
|   +-- pyproject.toml
+-- tests/
|   +-- conftest.py                  # Fixtures, CLI options
|   +-- test_connection.py           # Connection, auth, lifecycle
+-- dist/
|   +-- amigactld.conf.example       # Config template
+-- docs/
|   +-- PROTOCOL.md                  # Wire protocol spec
|   +-- COMMANDS.md                  # Per-command spec
```

## Roadmap

### Phase 1: Connection Skeleton (current)

TCP server with WaitSelect event loop, IP ACL, banner, and lifecycle commands
(VERSION, PING, QUIT, SHUTDOWN). Python client library and CLI. Documentation
and test suite.

### Phase 2: File Operations

DIR, STAT, READ, WRITE, DELETE, RENAME, MAKEDIR, and PROTECT commands. Chunked
binary transfer for READ/WRITE. Atomic writes via temp file and rename.

### Phase 3: EXEC and System Info

CLI command execution with captured output (EXEC). System introspection
(SYSINFO, ASSIGNS).

### Phase 4: ARexx

Non-blocking ARexx command dispatch to named ports, with timeout handling and
reply matching via WaitSelect signal integration.

### Phase 5: Polish

Interactive shell mode, LHA packaging for Amiga distribution, Workbench icon,
final documentation pass.

## License

This project is licensed under the GNU General Public License v3.0. See
[LICENSE](LICENSE) for details.
