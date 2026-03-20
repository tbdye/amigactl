# CLI Quick Start

This guide gets you from zero to productive with the `amigactl` command-line
interface in about five minutes. It covers connectivity, file operations,
command execution, and scripting.

## Prerequisites

- The `amigactld` daemon is running on your Amiga.
- Your machine can reach the Amiga over the network (default port 6800).
- The `amigactl` Python package is installed and available on your PATH.

## Verify Connectivity

Test that you can reach the daemon with `version` and `ping`:

```
$ amigactl --host 192.168.6.200 version
amigactld 0.8.0

$ amigactl --host 192.168.6.200 ping
OK
```

`version` prints the daemon's version string. `ping` prints `OK` if the
daemon is reachable. Both exit with code 0 on success. If the connection
fails, you will see an error message. For example:

```
Error: could not connect to 192.168.6.200:6800
```

## Save Connection Settings

To avoid typing `--host` on every command, create a config file at
`client/amigactl.conf` (next to the Python package):

```ini
[connection]
host = 192.168.6.200
port = 6800
```

If no config file exists when amigactl starts, it auto-creates one from the
bundled example. If `--host` or `--port` are passed on that first run, their
values are written into the generated file; otherwise the example defaults
are kept.

You can also use environment variables:

```
$ export AMIGACTL_HOST=192.168.6.200
$ export AMIGACTL_PORT=6800
```

Priority order: CLI flags > environment variables > config file > defaults.
See [Configuration](../configuration.md) for full details.

The remaining examples in this guide assume the host is configured and omit
`--host`.

## Basic File Operations

### List a directory

```
$ amigactl ls SYS:C
FILE	Copy	7824	00000000	1993-05-19 12:00:00
FILE	Delete	5368	00000000	1993-05-19 12:00:00
DIR	ExtBin	0	00000000	1993-05-19 12:00:00
```

Output is tab-separated with five columns: type (`FILE` or `DIR`), name,
size in bytes, protection bits (8-digit hex), and datestamp.

Add `-r` for recursive listing:

```
$ amigactl ls -r SYS:Devs
```

### View a file

```
$ amigactl cat SYS:S/Startup-Sequence
```

`cat` writes the raw file contents to stdout. It supports `--offset` and
`--length` for partial reads:

```
$ amigactl cat --offset 0 --length 128 SYS:S/Startup-Sequence
```

### Download a file

```
$ amigactl get SYS:S/Startup-Sequence
Downloaded 342 bytes to Startup-Sequence

$ amigactl get SYS:S/Startup-Sequence local-copy.txt
Downloaded 342 bytes to local-copy.txt
```

The first form saves to the current directory using the remote filename.
The second form specifies an explicit local path.

### Upload a file

```
$ amigactl put myprog RAM:myprog
Uploaded 10240 bytes to RAM:myprog

$ amigactl put myprog
Uploaded 10240 bytes to myprog
```

The first form specifies the remote path. The second form sends just the
local filename (without any directory component) as the remote path. The
daemon resolves this relative to the directory where `amigactld` was
launched.

## Running Commands

### Synchronous execution

`exec` runs a CLI command on the Amiga and waits for it to finish. Output is
printed to stdout. The exit code mirrors the AmigaOS return code (capped at
255):

```
$ amigactl exec -- Version
Kickstart 47.96, Workbench 47.2

$ echo $?
0
```

Use `--` before the remote command to prevent its flags from being parsed
locally. Use `-C` to set a working directory:

```
$ amigactl exec -C SYS:C -- List
```

If the remote command fails, amigactl exits with the same return code:

```
$ amigactl exec -- NonExistentCommand
Unknown command NonExistentCommand
$ echo $?
20
```

### Asynchronous execution

For long-running commands, use `run` to launch in the background. It prints
the daemon-assigned process ID immediately:

```
$ amigactl run -- MyLongTask
1
```

Check on running processes with `ps`:

```
$ amigactl ps
ID	COMMAND	STATUS	RC
1	MyLongTask	RUNNING	-
```

The output is tab-separated. `RC` shows `-` while the process is still
running.

Get detailed status of a specific process with `status`:

```
$ amigactl status 1
id=1
command=MyLongTask
status=EXITED
rc=0
```

## System Information

### System overview

```
$ amigactl sysinfo
exec_version=47.96
kickstart=47.96
chip_free=1823456
fast_free=14208000
total_free=16031456
chip_total=2097152
fast_total=16777216
chip_largest=1802240
fast_largest=14192640
```

Output is `key=value`, one pair per line.

### Mounted volumes

```
$ amigactl volumes
NAME	USED	FREE	CAPACITY	BLOCKSIZE
System:	52428800	478150656	530579456	512
Work:	1048576	104857600	105906176	512
RAM Disk:	0	0	0	0
```

Output is tab-separated with a header row.

## Scripting Example

A complete bash script that deploys a program to the Amiga and runs it:

```bash
#!/usr/bin/env bash
#
# deploy-and-test.sh -- Upload a binary, run it, check the result.

AMIGA_HOST="192.168.6.200"
BINARY="build/myprog"
REMOTE_PATH="RAM:myprog"

# Verify connectivity
if ! amigactl --host "$AMIGA_HOST" ping > /dev/null 2>&1; then
    echo "Error: cannot reach amigactld at $AMIGA_HOST" >&2
    exit 1
fi

# Upload the binary
echo "Uploading $BINARY..."
if ! amigactl --host "$AMIGA_HOST" put "$BINARY" "$REMOTE_PATH"; then
    echo "Error: upload failed" >&2
    exit 1
fi

# Run it and capture output
echo "Running $REMOTE_PATH..."
output=$(amigactl --host "$AMIGA_HOST" exec -- "$REMOTE_PATH")
rc=$?

if [ $rc -ne 0 ]; then
    echo "Program exited with rc=$rc" >&2
    echo "$output"
    exit $rc
fi

echo "Success:"
echo "$output"

# Download the results file if it was created
amigactl --host "$AMIGA_HOST" get "RAM:results.txt" 2>/dev/null
if [ $? -eq 0 ]; then
    echo "Results saved to results.txt"
fi
```

Key patterns used:

- `ping` with stdout/stderr suppressed for a connectivity check.
- `put` to upload, with inline exit code check.
- `exec` to run, capturing output in a variable and the exit code in `$?`.
- `get` to download, with stderr suppressed to silently handle the case
  where the file does not exist.

## Next Steps

- [Command Reference](commands.md) -- full details on every CLI command
- [Configuration](../configuration.md) -- config file format, environment
  variables, precedence rules
- [Interactive Shell](../shell/index.md) -- REPL-style interactive use
- [Library Call Tracing](../atrace/index.md) -- trace Amiga library calls
  in real time
