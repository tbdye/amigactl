# Installation

amigactl has two components that must be installed separately:

- **amigactl** (Python client) -- runs on your machine (Linux, macOS, or
  Windows). Provides the CLI tool, interactive shell, and Python library.
- **amigactld** (C daemon) -- runs on the Amiga. Listens for TCP connections
  and executes commands on behalf of the client.

Both communicate over TCP port 6800 by default.

## Requirements

### Client

- Python 3.8 or later (3.8 through 3.13 are tested)
- No external dependencies (stdlib only)
- Linux, macOS, or Windows

### Daemon (Amiga)

- AmigaOS 2.0 or later
- 68020 or later CPU
- A TCP/IP stack providing `bsdsocket.library` (Roadshow, AmiTCP, Miami, or
  emulator bsdsocket emulation)
- Approximately 120 KB of free disk space for the daemon binary
- Approximately 26 KB additional if using atrace (function call tracing)

## Client Installation

### Option 1: From a GitHub Release

Download the latest `.lha` archive from the
[Releases](https://github.com/tbdye/amigactl/releases) page and extract it.
The `client/` directory inside the archive contains everything needed.

**Linux / macOS:**

```
sh client/amigactl.sh --host 192.168.6.200 version
```

If you extracted from an LHA archive, the execute bit is not preserved. Either
invoke with `sh` as shown above, or restore the bit once:

```
chmod +x client/amigactl.sh
./client/amigactl.sh --host 192.168.6.200 version
```

**Windows (PowerShell):**

```
client\amigactl.ps1 --host 192.168.6.200 version
```

If script execution is blocked, set the execution policy:

```
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

If `python` launches the Microsoft Store instead of running the script,
disable the App execution aliases for `python.exe` and `python3.exe` in
Settings > Apps > Advanced app settings > App execution aliases.

### Option 2: pip install from source

Clone the repository or extract a release archive, then install with pip:

```
cd amigactl
pip install client/
```

Or, for a development install (editable):

```
pip install -e client/
```

Using a virtual environment is recommended:

```
python3 -m venv .venv
. .venv/bin/activate
pip install -e client/
```

After a pip install, the `amigactl` command is available directly:

```
amigactl --host 192.168.6.200 version
```

### Verifying the Client

Run any of the following to confirm the client is working:

```
amigactl --help
amigactl --host 192.168.6.200 ping
amigactl --host 192.168.6.200 version
```

The `ping` and `version` commands require a running daemon on the Amiga. If
you have not installed the daemon yet, `--help` will confirm the client itself
is installed correctly.

### Initial Configuration

On first run, amigactl auto-creates a configuration file at
`client/amigactl.conf` from the included `client/amigactl.conf.example`
template. The host and port you specify on the first invocation are written
into the generated config, so subsequent runs use them as defaults.

You can also create or edit the config file manually:

```ini
[connection]
host = 192.168.6.200
port = 6800

[editor]
command = vi
```

You can also specify an alternative config file location with `--config`:

```
amigactl --config /path/to/my.conf --host 192.168.6.200 ping
```

Settings are resolved in this order: CLI flags > environment variables
(`AMIGACTL_HOST`, `AMIGACTL_PORT`) > config file > built-in defaults.

See [configuration.md](configuration.md) for full details on all settings.

## Daemon Installation

### Downloading a Release

Download the latest `.lha` archive from the
[Releases](https://github.com/tbdye/amigactl/releases) page. Each release
contains:

| File | Description |
|------|-------------|
| `amigactld` | The daemon binary (m68k, 68020+) |
| `atrace_loader` | Function call tracing module (optional) |
| `amigactld.info` | Workbench icon for the daemon |
| `amigactld.conf.example` | Example configuration file |
| `client/` | Python client (see above) |
| `docs/` | Documentation |
| `README.md` | Project readme |
| `amigactld.readme` | Aminet-format readme |
| `LICENSE` | GPL v3 license |

Extract the archive on the Amiga (or on your machine and transfer the files).

### Placing Files on the Amiga

1. Copy `amigactld` to a location on the Amiga. Common choices are `C:` (the
   system command directory) or a dedicated directory such as `SYS:Tools/`.

2. If you want function call tracing, also copy `atrace_loader` to the same
   location.

3. Optionally copy `amigactld.info` alongside `amigactld` for a Workbench
   icon.

### Configuring the Daemon

The daemon reads its configuration from `S:amigactld.conf`. If the file is
absent, the daemon starts with default settings: port 6800, all IP addresses
allowed, remote shutdown and reboot disabled.

To configure:

1. Copy `amigactld.conf.example` to `S:amigactld.conf`:

   ```
   Copy amigactld.conf.example S:amigactld.conf
   ```

2. Edit `S:amigactld.conf` to match your network. At minimum, set `ALLOW`
   lines to restrict access to your client IP:

   ```
   # TCP port to listen on (default: 6800)
   PORT 6800

   # IP addresses allowed to connect. If no ALLOW lines are present,
   # all IPs are permitted. Add one ALLOW line per permitted host.
   ALLOW 192.168.6.100

   # Uncomment to allow remote SHUTDOWN CONFIRM commands.
   # ALLOW_REMOTE_SHUTDOWN YES

   # Uncomment to allow remote REBOOT CONFIRM commands.
   # ALLOW_REMOTE_REBOOT YES
   ```

   If no `ALLOW` lines are present, all IPs are permitted (development mode).

See [configuration.md](configuration.md) for details on all daemon settings.

### Starting the Daemon

**From the CLI:**

```
amigactld
```

The daemon prints a startup banner and listens for connections. Press Ctrl-C
to shut it down.

Command-line options use AmigaOS ReadArgs syntax:

```
amigactld PORT 6900
amigactld CONFIG S:my_custom.conf
```

**From Workbench:**

Double-click the `amigactld` icon. Configuration can be set via Tool Types
(`PORT`, `CONFIG`) in the icon's Info window.

**Auto-start on boot:**

Add the following to `S:User-Startup`:

```
RUN >NIL: C:amigactld
```

Adjust the path if you placed the binary elsewhere.

### Loading atrace (Optional)

To use library call tracing, place `atrace_loader` at `C:atrace_loader`.
The daemon automatically loads it via `SystemTags()` when a client first
requests tracing (e.g., `TRACE START`). No manual execution or setup is
needed.

If you want to pre-load atrace before any client connects, you can run
`C:atrace_loader` from the CLI. This is optional -- the daemon handles it
on demand.

### Verifying the Daemon

From the Amiga CLI, confirm the daemon is running by checking that it printed
its startup banner (e.g., `amigactld 0.8.0 listening on port 6800`).

From the client machine:

```
amigactl --host 192.168.6.200 ping
```

A response of `OK` confirms the daemon is reachable and accepting connections.

## Building from Source

This section is for developers who want to compile the daemon from source or
modify the client.

### Client

The client is pure Python with no build step. Install it in editable mode:

```
pip install -e client/
```

### Daemon

The daemon is cross-compiled using
[m68k-amigaos-gcc](https://github.com/AmigaPorts/m68k-amigaos-gcc), which
must be installed at `/opt/amiga`.

From the project root:

```
make
```

This produces three binaries in the project root:

- `amigactld` -- the daemon
- `atrace_loader` -- the tracing module
- `atrace_test` -- validation tool for atrace (not included in releases)

The build uses `-noixemul` (libnix) and targets 68020. Key compiler flags
include `-noixemul -O2 -Wall -Wextra -m68020 -fomit-frame-pointer`; see the
Makefile for the full set.

To clean build artifacts:

```
make clean
```

To build a distributable LHA archive:

```
sh dist/build_lha.sh
```

This runs a clean build, generates the Workbench icon, and packages everything
into an `amigactld-<version>.lha` archive.

### Running Tests

Tests require a running amigactld on an Amiga (or emulator) and the client
package installed. From the project root:

```
pip install -e client/
pip install pytest
pytest tests/ --host 192.168.6.200 -v
```

## Verifying the Connection

Once both components are installed, confirm end-to-end connectivity:

```
$ amigactl --host 192.168.6.200 ping
OK

$ amigactl --host 192.168.6.200 version
amigactld 0.8.0

$ amigactl --host 192.168.6.200 sysinfo
chip_free=1234567
fast_free=8765432
...
```

If the connection is refused, verify:

1. The daemon is running on the Amiga (`amigactld` process is listed).
2. The Amiga has a TCP/IP stack running with `bsdsocket.library` available.
3. The client IP is permitted by the daemon's `ALLOW` configuration (or no
   `ALLOW` lines are set).
4. The host and port match between client and daemon.

## Next Steps

- [Configuration](configuration.md) -- full reference for client and daemon
  settings, precedence rules, and environment variables.
- [CLI Reference](cli/) -- documentation for all CLI subcommands.
- [Interactive Shell](shell/) -- shell-specific commands and navigation.
- [Protocol Specification](protocol.md) -- wire protocol details for
  integration and debugging.
- [Agent Guide](agent-guide.md) -- guide for AI agents automating tasks via
  the Python library.

Note: the release archive includes a subset of the documentation (protocol
spec, agent guide, and atrace docs). The full documentation set is available
in the [GitHub repository](https://github.com/tbdye/amigactl).
