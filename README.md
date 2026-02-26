# amigactl

Remote access toolkit for AmigaOS.

amigactl provides structured, programmatic remote access to AmigaOS over TCP.
It consists of a lightweight C daemon (amigactld) running on the Amiga and a
Python client library and CLI tool on the client side. Together they expose
35 daemon commands spanning file operations (copy, checksum, streaming), CLI
command execution, ARexx dispatch, environment variables, library version
queries, and system introspection (assigns, volumes, ports, tasks, devices)
through a simple text protocol with machine-parseable responses. The interactive
shell adds 6 client-side search and navigation commands (find, tree, grep, diff,
du, watch). Designed for trusted LANs and emulator setups.

## Architecture

```
Client                            Amiga
+-----------------+               +------------------+
| amigactl CLI    |--TCP:6800---->| amigactld        |
| (Python)        |               | (m68k C daemon)  |
+-----------------+               |                  |
                                  | - File I/O (DOS) |
+-----------------+               | - EXEC / Proc    |
| Python library  |--TCP:6800---->| - ARexx dispatch |
| (client)        |               | - File streaming |
+-----------------+               | - System queries |
                                  +------------------+
```

- **amigactld**: Amiga daemon, C, cross-compiled with m68k-amigaos-gcc.
  Multi-client via WaitSelect event loop (up to 8 simultaneous clients).
- **amigactl**: Python client library and CLI tool (client-side).
- **Protocol**: Text commands, dot-stuffed sentinel termination, length-prefixed
  binary for file data. ISO-8859-1 encoding.
- **Security**: IP-based ACL from `S:amigactld.conf`

## Requirements

### Amiga (daemon)

- AmigaOS 2.0 or later
- 68020 or later CPU
- A TCP/IP stack providing `bsdsocket.library` (Roadshow, AmiTCP, Miami, or
  emulator bsdsocket emulation)

### Client

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

# Allow the REBOOT command from remote clients (default: NO).
# When NO, REBOOT CONFIRM returns ERR 201.
ALLOW_REMOTE_REBOOT NO
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

Double-click the amigactld icon. A console window briefly appears showing the
startup banner, then auto-dismisses once the daemon is running. If startup
fails (e.g., port already in use), the window stays open with an error message.
The daemon runs until a SHUTDOWN command is received or the system shuts down.
Configuration can be set via Tool Types (`PORT`, `CONFIG`) in the icon's Info
window.

## Running the Client

### Linux / macOS

    sh client/amigactl.sh --host 192.168.6.200

With no subcommand, this launches the interactive shell. If you extracted
from the LHA archive, the execute bit is not preserved. Use `sh` as shown
above, or run `chmod +x client/amigactl.sh` once to invoke it directly. You can also run
one-off commands:

    client/amigactl.sh --host 192.168.6.200 ls SYS:S
    client/amigactl.sh --host 192.168.6.200 exec list SYS:S
    client/amigactl.sh --host 192.168.6.200 get SYS:S/Startup-Sequence

### Interactive Shell

The interactive shell starts automatically when no subcommand is given, or
explicitly with the `shell` subcommand:

    $ client/amigactl.sh --host 192.168.6.200
    Connected to 192.168.6.200 (amigactld 0.7.0)
    Type "help" for a list of commands, "exit" to disconnect.
    amiga@192.168.6.200:SYS:> ls
    C/  Devs/  Expansion/  L/  Libs/  Locale/  Prefs/  S/  System/  T/
    Utilities/  WBStartup/  Disk.info  Startup-Sequence  User-Startup
    amiga@192.168.6.200:SYS:> cd S
    amiga@192.168.6.200:SYS:S> cat Startup-Sequence
    ; Startup-Sequence
    ...
    amiga@192.168.6.200:SYS:S> cd ..
    amiga@192.168.6.200:SYS:> exec avail FLUSH
    Type   Available    In-Use   Maximum   Largest
    chip      477752    571528   1048576    460488
    fast    13298680    895176  14680064  13036136
    total   13776432   1466704  15728640  13036136
    amiga@192.168.6.200:SYS:> sysinfo
    cpu=MC68020
    ...
    amiga@192.168.6.200:SYS:> cp C/Dir RAM:Dir.bak
    amiga@192.168.6.200:SYS:> checksum C/Dir
    crc32=a1b2c3d4  size=12345  C/Dir
    amiga@192.168.6.200:SYS:> libver dos.library
    dos.library 40.3
    amiga@192.168.6.200:SYS:> env Workbench
    USE1MAP.16
    amiga@192.168.6.200:SYS:> find C #?.info
    C/Ed.info
    C/IconX.info
    ...
    amiga@192.168.6.200:SYS:> grep "Pattern" S
    S/Startup-Sequence:14: Pattern
    ...
    amiga@192.168.6.200:SYS:> exit
    Disconnected.

The shell supports `cd` and `pwd` for navigation (including `..` and `/`
for parent directory). Relative paths are resolved client-side before
sending to the daemon. Tab completion works with both absolute and
relative Amiga paths. Type `help` for a command list, or `help COMMAND`
for detailed usage of any command.

The shell also provides search and navigation commands that combine
multiple daemon operations client-side: `find` (recursive file search
with glob patterns), `tree` (directory tree display), `grep` (recursive
text search), `diff` (file comparison), `du` (disk usage), and `watch`
(periodic command re-execution). These are shell-only and not available
as CLI subcommands.

Tab completion requires Python's readline module (included on Linux/macOS).
On Windows, tab completion is not available by default. Installing the
optional `pyreadline3` package (`pip install pyreadline3`) adds tab
completion support.

The `edit` command downloads a file, opens it in your editor, and uploads
changes on save. It checks `$VISUAL`, `$EDITOR`, the config file `editor`
setting, then falls back to `vi` (Linux/macOS) or `notepad` (Windows).

    export EDITOR=nano                 # Linux/macOS
    $env:EDITOR = "code --wait"        # Windows (VS Code)

Colors are auto-detected (disable with `NO_COLOR=1` or
`AMIGACTL_COLOR=never`).

### Windows (PowerShell)

    client\amigactl.ps1 --host 192.168.6.200

If script execution is blocked, set the execution policy:

    Set-ExecutionPolicy -Scope CurrentUser RemoteSigned

If `python` launches the Microsoft Store instead of running the script,
disable the App execution aliases for `python.exe` and `python3.exe` in
Settings > Apps > Advanced app settings > App execution aliases.

### Alternative: pip install

For users who prefer a system-wide install:

    python3 -m venv .venv && . .venv/bin/activate
    pip install -e client/
    amigactl --host 192.168.6.200

No external dependencies are required (stdlib only).

### Directory layout

The wrapper scripts are in the `client/` directory alongside the Python package:

```
amigactl/
+-- client/
    +-- amigactl.sh        (Linux/macOS wrapper)
    +-- amigactl.ps1       (Windows wrapper)
    +-- amigactl/
        +-- __init__.py
        +-- __main__.py
        +-- protocol.py
        +-- shell.py
        +-- colors.py
```

### Environment variables

The `--host` flag defaults to the `AMIGACTL_HOST` environment variable, or
`192.168.6.200` if unset. `--port` defaults to `AMIGACTL_PORT` or `6800`.

### Configuration file

Settings can also be placed in `client/amigactl.conf`. On first run, this
file is auto-created from `client/amigactl.conf.example` with defaults filled
in. Edit it to match your setup:

```ini
[connection]
host = 192.168.6.200
port = 6800

[editor]
command = vi
```

CLI flags override environment variables, which override config file settings.
Use `--config PATH` to specify an alternative location.

### CLI usage

```
client/amigactl.sh --host 192.168.6.200 version
client/amigactl.sh --host 192.168.6.200 ping
client/amigactl.sh --host 192.168.6.200 uptime
client/amigactl.sh --host 192.168.6.200 ls SYS:S
client/amigactl.sh --host 192.168.6.200 stat SYS:S/Startup-Sequence
client/amigactl.sh --host 192.168.6.200 cat SYS:S/Startup-Sequence > startup.txt
client/amigactl.sh --host 192.168.6.200 get SYS:S/Startup-Sequence startup.txt
client/amigactl.sh --host 192.168.6.200 put localfile.txt RAM:test.txt
client/amigactl.sh --host 192.168.6.200 rm RAM:test.txt
client/amigactl.sh --host 192.168.6.200 mv RAM:old.txt RAM:new.txt
client/amigactl.sh --host 192.168.6.200 mkdir RAM:newdir
client/amigactl.sh --host 192.168.6.200 chmod RAM:file.txt
client/amigactl.sh --host 192.168.6.200 chmod RAM:file.txt 0f
client/amigactl.sh --host 192.168.6.200 touch RAM:file.txt
client/amigactl.sh --host 192.168.6.200 touch RAM:file.txt 2026-02-19 12:00:00
client/amigactl.sh --host 192.168.6.200 exec echo hello
client/amigactl.sh --host 192.168.6.200 run wait 30
client/amigactl.sh --host 192.168.6.200 ps
client/amigactl.sh --host 192.168.6.200 status 1
client/amigactl.sh --host 192.168.6.200 signal 1
client/amigactl.sh --host 192.168.6.200 kill 1
client/amigactl.sh --host 192.168.6.200 sysinfo
client/amigactl.sh --host 192.168.6.200 assigns
client/amigactl.sh --host 192.168.6.200 volumes
client/amigactl.sh --host 192.168.6.200 ports
client/amigactl.sh --host 192.168.6.200 tasks
client/amigactl.sh --host 192.168.6.200 arexx REXX -- return 1+2
client/amigactl.sh --host 192.168.6.200 cp SYS:C/Dir RAM:Dir
client/amigactl.sh --host 192.168.6.200 append RAM:logfile.txt localdata.txt
client/amigactl.sh --host 192.168.6.200 checksum SYS:C/Dir
client/amigactl.sh --host 192.168.6.200 setcomment RAM:test.txt "Important file"
client/amigactl.sh --host 192.168.6.200 libver exec.library
client/amigactl.sh --host 192.168.6.200 env Workbench
client/amigactl.sh --host 192.168.6.200 setenv MyVar hello
client/amigactl.sh --host 192.168.6.200 devices
client/amigactl.sh --host 192.168.6.200 capabilities
client/amigactl.sh --host 192.168.6.200 tail RAM:logfile.txt
client/amigactl.sh --host 192.168.6.200 shutdown
client/amigactl.sh --host 192.168.6.200 reboot
```

### Python library

```python
from amigactl import AmigaConnection

with AmigaConnection("192.168.6.200") as amiga:
    print(amiga.version())
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

    # File operations (new in 0.7)
    amiga.copy("SYS:C/Dir", "RAM:Dir")
    data = amiga.read("SYS:C/Dir", offset=100, length=50)
    amiga.append("RAM:logfile.txt", b"new log entry\n")
    csum = amiga.checksum("SYS:C/Dir")
    amiga.setcomment("RAM:test.txt", "Important file")

    # Command execution
    rc, output = amiga.execute("list SYS:S")
    proc_id = amiga.execute_async("wait 30")
    procs = amiga.proclist()
    info = amiga.procstat(proc_id)
    amiga.signal(proc_id)

    # ARexx
    rc, result = amiga.arexx("REXX", "return 1+2")

    # File streaming (Ctrl-C or stop_tail() to end)
    amiga.tail("RAM:logfile.txt", lambda chunk: print(chunk))

    # Lifecycle
    uptime_secs = amiga.uptime()

    # System info
    sysinfo = amiga.sysinfo()
    assigns = amiga.assigns()
    volumes = amiga.volumes()
    ports = amiga.ports()
    tasks = amiga.tasks()
    amiga.setdate("RAM:test.txt", "2026-02-19 12:00:00")

    # System queries (new in 0.7)
    ver = amiga.libver("exec.library")
    val = amiga.env("Workbench")
    amiga.setenv("MyVar", "hello")
    devs = amiga.devices()
    caps = amiga.capabilities()
```

## Amiga Installation

Download the latest `.lha` archive from the Releases page, or build it:

    sh dist/build_lha.sh

Extract to any location on the Amiga. Copy `amigactld.conf.example` to
`S:amigactld.conf` and edit the ALLOW lines for your network.

To auto-start, add to `S:User-Startup`:

    RUN >NIL: <path>/amigactld

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
syntax, responses, error conditions, and example transcripts). For AI agents
automating tasks via the Python library, see
[docs/AGENT_GUIDE.md](docs/AGENT_GUIDE.md).

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
|   +-- arexx.c / arexx.h            # ARexx dispatch
|   +-- tail.c / tail.h              # File streaming (TAIL)
+-- client/
|   +-- amigactl.sh                  # Linux/macOS wrapper script
|   +-- amigactl.ps1                 # Windows wrapper script
|   +-- amigactl/
|   |   +-- __init__.py              # AmigaConnection class
|   |   +-- __main__.py              # CLI tool
|   |   +-- protocol.py              # Wire protocol helpers
|   |   +-- shell.py                 # Interactive shell
|   |   +-- colors.py                # ANSI color support
|   +-- pyproject.toml
+-- tests/
|   +-- conftest.py                  # Fixtures, CLI options
|   +-- test_connection.py           # Connection, auth, lifecycle
|   +-- test_file.py                 # File operation tests
|   +-- test_exec.py                 # Exec and process management tests
|   +-- test_sysinfo.py              # System info tests
|   +-- test_arexx.py                # ARexx dispatch tests
|   +-- test_tail.py                 # File streaming tests
+-- tools/
|   +-- mkicon.py                    # Workbench icon generator
+-- dist/
|   +-- amigactld.conf.example       # Config template
|   +-- amigactld.info               # Workbench icon
|   +-- build_lha.sh                 # LHA packaging script
+-- docs/
|   +-- PROTOCOL.md                  # Wire protocol spec
|   +-- COMMANDS.md                  # Per-command spec
|   +-- AGENT_GUIDE.md               # AI agent automation guide
```

## License

This project is licensed under the GNU General Public License v3.0. See
[LICENSE](LICENSE) for details.
