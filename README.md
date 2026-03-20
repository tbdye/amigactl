# amigactl

A remote access toolkit for AmigaOS -- manage files, execute commands, inspect system state, and trace library calls over TCP.

## What is amigactl?

amigactl gives you full remote access to an Amiga from any modern workstation. A lightweight C daemon (`amigactld`) runs on the Amiga, while a Python CLI tool and library on the host side let you manage files, run commands, query system state, and automate workflows -- all over a simple TCP connection.

The toolkit spans the full range of daily Amiga development tasks: file transfers, remote command execution with process management, environment variable and assign manipulation, ARexx dispatch, and deep system introspection including running tasks, mounted volumes, message ports, and device drivers. An interactive shell with tab completion and Amiga-aware path navigation makes ad-hoc work feel native.

What sets amigactl apart is **atrace**, its library call tracing system. atrace patches 99 functions across 7 AmigaOS libraries (exec, dos, intuition, bsdsocket, icon, workbench, and graphics) at the machine code level, capturing every call with arguments, return values, caller task names, and timing data. Events stream to the host in real time and can be viewed through a live TUI with tier-based filtering, per-task isolation, and handle tracking. It is, as far as we know, the only tool of its kind for AmigaOS.

## Feature Highlights

**File Management** -- `ls`, `stat`, `cat`, `get`, `put`, `cp`, `mv`, `rm`, `mkdir`, `chmod`, `touch`, `checksum`, `append`, `setcomment`, `tail` (live file streaming), with recursive listing, byte-range reads, and CRC32 checksums.

**Remote Command Execution** -- Synchronous (`exec`) and asynchronous (`run`) command execution with working directory control, process listing (`ps`), status queries, signal delivery, and forced termination.

**Interactive Shell** -- Tab completion for Amiga paths, `cd`/`pwd` navigation with `..` support, colorized output, inline `edit` with local editor integration, and shell-only utilities: `find`, `tree`, `grep`, `diff`, `du`, `watch`.

**Library Call Tracing** -- 99 functions across 7 libraries, organized into three progressive tiers (Basic, Detail, Verbose) plus Manual-only functions for high-volume tracing. Filter by library, function, process name, or error-only. Stream events live or trace a single program's execution with `trace run`. Full TUI viewer with interactive controls.

**System Introspection** -- `sysinfo` (memory stats, Exec/Kickstart/bsdsocket versions), `tasks` (running tasks/processes with stack sizes), `volumes` (mounted filesystems with free space), `ports` (Exec message ports), `assigns`/`assign` (list, create, modify, and remove logical assignments), `devices` (Exec device drivers), `libver` (library/device versions), `env`/`setenv` (environment variables), `uptime` (daemon uptime).

**ARexx Integration** -- Send commands to any ARexx port with return code and result capture.

**Python Library** -- Every daemon capability is available as a method on `AmigaConnection` for scripting and automation. No external dependencies (stdlib only).

**Wire Protocol** -- A documented text protocol with dot-stuffed framing and length-prefixed binary transfers, suitable for custom integrations in any language.

## Quick Example

```
$ amigactl --host 192.168.6.200 sysinfo
chip_free=477752
fast_free=13298680
total_free=13776432
chip_total=1048576
fast_total=14680064
chip_largest=460488
fast_largest=13036136
exec_version=40.68
kickstart=68
bsdsocket=4.1

$ amigactl --host 192.168.6.200 ls SYS:C
file	Dir	4588	0	1994-10-20 00:00:00
file	List	7808	0	1994-10-20 00:00:00
file	Copy	5044	0	1994-10-20 00:00:00
...

$ amigactl --host 192.168.6.200 get SYS:S/Startup-Sequence startup.txt
Downloaded 1234 bytes to startup.txt

$ amigactl --host 192.168.6.200 exec avail FLUSH
Type   Available    In-Use   Maximum   Largest
chip      477752    571528   1048576    460488
fast    13298680    895176  14680064  13036136

$ amigactl --host 192.168.6.200 tasks
NAME	TYPE	PRI	STATE	STACK
input.device	TASK	20	wait	4096
amigactld	PROCESS	0	run	65536
...

$ amigactl --host 192.168.6.200 trace run -- list SYS:S
SEQ                 TIME  FUNCTION                     TASK                 ARGS                                     RESULT
1                 0.001s  dos.Lock                     List                 "SYS:S" SHARED_LOCK                      0x12345678
2                 0.002s  dos.Examine                  List                 0x12345678 0x...                         1
3                 0.003s  dos.ExNext                   List                 0x12345678 0x...                         1
...
8                 0.005s  dos.UnLock                   List                 0x12345678                               (void)

$ amigactl --host 192.168.6.200
amiga@192.168.6.200:SYS:> find C *.info
C/Ed.info
C/IconX.info
amiga@192.168.6.200:SYS:> exit
```

## Installation

The quickest path:

1. Download the latest `.lha` release from the [Releases](https://github.com/tbdye/amigactl/releases) page.
2. Extract on the Amiga. Copy `amigactld` to `C:` or any convenient location.
3. On the host, install the Python client:

```
pip install -e client/
amigactl --host <amiga-ip>
```

No external Python dependencies are required (stdlib only, Python 3.8+).

See [docs/installation.md](docs/installation.md) for the full guide, including daemon configuration, Workbench icon setup, auto-start, and cross-compiler build instructions.

## Documentation

| Guide | Description |
|-------|-------------|
| [Installation](docs/installation.md) | Daemon build, Amiga setup, client install |
| [Configuration](docs/configuration.md) | Daemon config file, client config, ACLs |
| [CLI Reference](docs/cli/index.md) | CLI subcommand index and usage |
| [CLI Quickstart](docs/cli/quickstart.md) | Get up and running fast |
| [CLI Commands](docs/cli/commands.md) | Complete command reference |
| [Interactive Shell](docs/shell/index.md) | Shell features, navigation, tab completion |
| [Shell Quickstart](docs/shell/quickstart.md) | Shell tutorial |
| [Library Tracing (atrace)](docs/atrace/index.md) | Architecture, traced functions, viewer, recipes |
| [atrace Quickstart](docs/atrace/quickstart.md) | Start tracing in minutes |
| [Agent/Automation Guide](docs/agent-guide.md) | Python library API for scripting and AI agents |
| [Wire Protocol](docs/protocol.md) | Protocol framing, encoding, binary transfers |
| [Protocol Commands](docs/protocol-commands.md) | Per-command syntax and response format |

The `docs/atrace/` directory contains 20 pages covering the tracing system in depth: architecture, stub mechanism, ring buffer, event format, output tiers, filtering, interactive viewer, traced function catalog, recipes, performance, and troubleshooting.

The `docs/shell/` directory contains 15 pages covering the interactive shell: navigation, file operations, file transfers, search utilities, command execution, system commands, ARexx, tab completion, recipes, and troubleshooting.

## Repository Layout

```
amigactl/
+-- README.md
+-- LICENSE                              # GPL v3
+-- Makefile                             # m68k cross-compilation
+-- daemon/
|   +-- main.c                           # Entry, startup, event loop
|   +-- daemon.h                         # Shared structures, constants
|   +-- net.c / net.h                    # Socket helpers, protocol I/O
|   +-- config.c / config.h              # Config file parsing, ACL
|   +-- file.c / file.h                  # File operation handlers
|   +-- exec.c / exec.h                  # Command execution, process mgmt
|   +-- sysinfo.c / sysinfo.h            # System info handlers
|   +-- arexx.c / arexx.h                # ARexx dispatch
|   +-- tail.c / tail.h                  # File streaming (TAIL)
|   +-- trace.c / trace.h                # Library call tracing (TRACE)
+-- atrace/
|   +-- main.c                           # atrace_loader entry point
|   +-- atrace.h                         # Shared data structures
|   +-- funcs.c                          # Function table (99 functions)
|   +-- stub_gen.c                       # 68k stub code generator
|   +-- ringbuf.c                        # Lock-free ring buffer
+-- client/
|   +-- amigactl.sh                      # Linux/macOS wrapper script
|   +-- amigactl.ps1                     # Windows wrapper script
|   +-- pyproject.toml                   # Package metadata
|   +-- amigactl.conf.example            # Client config template
|   +-- amigactl/
|       +-- __init__.py                  # AmigaConnection library API
|       +-- __main__.py                  # CLI tool
|       +-- protocol.py                  # Wire protocol helpers
|       +-- shell.py                     # Interactive shell
|       +-- colors.py                    # ANSI color support
|       +-- trace_tiers.py              # Output tier definitions
|       +-- trace_ui.py                  # Interactive trace viewer (TUI)
|       +-- trace_grid.py               # Toggle grid for trace viewer
+-- tests/                              # pytest suite (live daemon tests)
+-- testapp/                            # Test helper programs (Amiga-side)
+-- tools/
|   +-- mkicon.py                        # Workbench icon generator
+-- dist/
|   +-- amigactld.conf.example           # Daemon config template
|   +-- amigactld.info                   # Workbench icon
|   +-- amigactld.readme                 # Aminet-format readme
|   +-- build_lha.sh                     # LHA packaging script
+-- docs/
    +-- installation.md                  # Installation guide
    +-- configuration.md                 # Configuration reference
    +-- agent-guide.md                   # AI agent automation guide
    +-- protocol.md                      # Wire protocol spec
    +-- protocol-commands.md             # Per-command spec
    +-- cli/                             # CLI documentation (3 pages)
    +-- shell/                           # Shell documentation (15 pages)
    +-- atrace/                          # Tracing documentation (20 pages)
```

## License

This project is licensed under the GNU General Public License v3.0. See [LICENSE](LICENSE) for details.
