# Quick Start

Get connected to your Amiga and productive in 5 minutes. This guide
covers the essential workflow: connect, navigate, inspect, execute,
transfer, and exit.


## Prerequisites

1. **amigactld** is running on the Amiga (typically installed at
   `C:amigactld` and launched from `S:User-Startup`). It listens on TCP
   port 6800 by default.

2. **Python client** is available on your machine (Python 3.8+).
   Either run directly via the wrapper script (`client/amigactl.sh`) or
   install with pip:

       pip install -e client/

3. **Network connectivity** between the client and the Amiga. Verify with:

       amigactl --host 192.168.6.200 ping

   You should see `OK`.


## Connecting

### Default Connection

Running `amigactl` with no subcommand launches the interactive shell:

    amigactl --host 192.168.6.200

This connects and drops you into the shell immediately.

### Specifying a Host

The host can be provided three ways, in order of precedence:

- **CLI flag:** `--host 192.168.6.200`
- **Environment variable:** `AMIGACTL_HOST=192.168.6.200`
- **Config file:** set `host` under `[connection]` in
  `client/amigactl.conf`

Once configured via environment or config file, you can launch the
shell with just `amigactl`.

### What You See

On successful connection, the shell prints a banner and sets the
initial working directory to `SYS:`:

```
Connected to 192.168.6.200 (amigactld 0.8.0)
Type "help" for a list of commands, "exit" to disconnect.
amiga@192.168.6.200:SYS:>
```

The prompt format is `amiga@<host>:<cwd>>`, so your current location
on the Amiga is always visible.


## First Commands

### Navigating

```bash
amiga@192.168.6.200:SYS:> ls
C/          Classes/    Devs/       Expansion/  Fonts/      L/
Libs/       Locale/     Prefs/      Rexxc/      S/          Storage/
System/     Tools/      Utilities/  WBStartup/

amiga@192.168.6.200:SYS:> cd S
amiga@192.168.6.200:SYS:S> pwd
SYS:S

amiga@192.168.6.200:SYS:S> cd Work:
amiga@192.168.6.200:Work:>
```

`cd` with no arguments returns to `SYS:`. Tab completion works for
Amiga paths.

### Viewing Files

```bash
amiga@192.168.6.200:SYS:> cat S/Startup-Sequence
; $VER: Startup-Sequence 40.14 (18.10.93)
...

amiga@192.168.6.200:SYS:> stat S/Startup-Sequence
type=FILE
name=Startup-Sequence
size=1234
protection=----rwed
datestamp=1993-10-18 00:00:00
```

If the file has a comment set, a `comment` field is also shown.

### Running Commands

```bash
amiga@192.168.6.200:SYS:> exec avail
Type   Available    In-Use   Maximum   Largest
chip     1234567    234567   2097152   1048576
fast    12345678   1234567  16777216   8388608
total   13580245   1469134  18874368   8388608
Return code: 0
```

`exec` runs a command synchronously and displays its output. The
shell blocks until the command finishes.


## Transferring Files

### Download

```bash
amiga@192.168.6.200:SYS:> get S/Startup-Sequence /tmp/startup.txt
Downloaded 1234 bytes to /tmp/startup.txt
```

If you omit the local path, the file is saved under its Amiga filename
in your current local directory.

### Upload

```bash
amiga@192.168.6.200:SYS:> put /tmp/myprog Work:myprog
Uploaded 5678 bytes to Work:myprog
```

If you omit the remote path, the file is uploaded to the current Amiga
directory with the same filename.


## Getting Help

List all available commands:

```bash
amiga@192.168.6.200:SYS:> help
```

Get detailed help for a specific command:

```bash
amiga@192.168.6.200:SYS:> help ls
```

Each command's help text shows its usage, options, and examples.


## Exiting

Any of these will disconnect and exit the shell:

```bash
amiga@192.168.6.200:SYS:> exit
```

`quit` and **Ctrl-D** also work.


## Next Steps

- [navigation.md](navigation.md) -- Directory navigation, Amiga path
  syntax, and path resolution.
- [file-operations.md](file-operations.md) -- File and directory
  management (mkdir, rm, mv, cp, chmod, and more).
- [file-transfer.md](file-transfer.md) -- Transferring files between
  the client and the Amiga (get, put, append).
- [command-execution.md](command-execution.md) -- Synchronous and
  asynchronous command execution (exec, run, ps, kill).
- [tab-completion.md](tab-completion.md) -- Tab completion and readline
  integration.
- [system-commands.md](system-commands.md) -- System information,
  volumes, assigns, tasks, and ports.
- [troubleshooting.md](troubleshooting.md) -- Common issues and
  solutions.
