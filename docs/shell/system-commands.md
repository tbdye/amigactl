# System Commands

The amigactl shell provides commands for checking daemon health,
querying Amiga system state, managing environment variables and
logical assigns, and performing administrative operations like
shutdown and reboot. These commands do not involve the filesystem --
for file and directory operations, see
[file-operations.md](file-operations.md).


## Daemon Health

### version

Print the daemon's version string.

```
version
```

Returns the daemon name and version number. This is the same string
shown when the shell first connects.

```bash
amiga@192.168.6.228:SYS:> version
amigactld 0.8.0
```

### ping

Check that the daemon is responding.

```
ping
```

Sends a lightweight request to the daemon and prints `OK` if it
replies. Useful for verifying that the connection is still alive
after a period of inactivity.

```bash
amiga@192.168.6.228:SYS:> ping
OK
```

If the connection has been lost, the shell prints a connection error
and clears the prompt. Use `reconnect` to re-establish the session.

### uptime

Show how long the daemon has been running.

```
uptime
```

Prints the daemon uptime as a human-readable duration. The format
uses `d` (days), `h` (hours), `m` (minutes), and `s` (seconds),
omitting zero-valued units. Seconds are always included when no
larger unit is present (i.e., uptime under one minute shows only
seconds).

```bash
amiga@192.168.6.228:SYS:> uptime
2d 5h 13m 47s
```

A freshly started daemon:

```bash
amiga@192.168.6.228:SYS:> uptime
12s
```

### capabilities

Show daemon capabilities and supported protocol commands.

```
capabilities
```

Displays the daemon version, protocol version, connection limits,
and the full list of wire-protocol commands the daemon supports.
The command list is printed one command per line for readability.

```bash
amiga@192.168.6.228:SYS:> capabilities
version=0.8.0
protocol=1.0
max_clients=8
max_cmd_len=4096
commands=
  APPEND
  AREXX
  ASSIGN
  ASSIGNS
  CAPABILITIES
  DELETE
  DEVICES
  DIR
  ...
```

`caps` is an alias for `capabilities`.


## System Information

### sysinfo

Show Amiga system information.

```
sysinfo
```

Displays key system details as `key=value` pairs. The output
includes memory statistics (chip and fast RAM, both free and total
amounts, total free memory across all types, and the largest
contiguous free block of each type), the Exec library version, the
Kickstart version, and the TCP/IP stack version.

```bash
amiga@192.168.6.228:SYS:> sysinfo
chip_free=1587424
fast_free=115867648
total_free=117455072
chip_total=2097152
fast_total=134217728
chip_largest=1571792
fast_largest=115830784
exec_version=47.3
kickstart=47
bsdsocket=4.307
```

Memory values are raw byte counts (integers). The `exec_version`
and `bsdsocket` fields are dot-separated version strings. The
`kickstart` field is a single integer. The `bsdsocket` field
identifies the TCP/IP stack -- Roadshow reports its own version,
while AmiTCP-based stacks report theirs.

### libver

Get the version of an Amiga library or device.

```
libver NAME
```

Queries the version and revision of a named system library or
device driver. The name must include the `.library` or `.device`
suffix.

| Argument | Description |
|----------|-------------|
| `NAME`   | Full library or device name (e.g., `exec.library`, `dos.library`, `timer.device`). |

```bash
amiga@192.168.6.228:SYS:> libver exec.library
name=exec.library
version=47.3

amiga@192.168.6.228:SYS:> libver dos.library
name=dos.library
version=47.4

amiga@192.168.6.228:SYS:> libver timer.device
name=timer.device
version=50.1
```

The version string is in `major.minor` format. If the named library
cannot be opened or the device is not loaded in memory, the daemon
returns an error.

### volumes

List mounted volumes on the Amiga.

```
volumes
```

Prints a table of all mounted volumes with their space usage. Four
columns are shown: volume name, used space, free space, and total
capacity. Sizes are formatted in human-readable units: values under 1024 are
shown as plain integers, and larger values use K, M, G, or T suffixes.

```bash
amiga@192.168.6.228:SYS:> volumes
NAME         USED   FREE  CAPACITY
System:     42.3M  87.7M     130M
Work:      285.6M  714M     1000M
RAM:         8.2K  1.0M      1.0M
```

Columns are right-aligned for numeric values and left-aligned for
names. The column widths adjust dynamically to fit the longest
entry.

### tasks

List all running tasks and processes on the Amiga.

```
tasks
```

Prints a table of every task and process in the system. Five columns
are shown: task name, type (task or process), scheduling priority,
current state, and stack size in bytes.

```bash
amiga@192.168.6.228:SYS:> tasks
NAME                TYPE      PRI  STATE    STACK
exec.library        TASK        0  ready     4096
Workbench           PROCESS     1  wait     16384
Shell Process       PROCESS     0  wait      8192
amigactld           PROCESS     0  wait     65536
tcp.handler         PROCESS     5  wait     16384
input.device        TASK       20  wait      4096
```

Task states reflect the AmigaOS scheduler state at the moment of
the query (e.g., `ready`, `wait`, `run`). Priority is a signed
integer -- higher values mean the task is scheduled more
aggressively. The currently executing task will show `run`.

### devices

List Exec devices on the Amiga.

```
devices
```

Prints a table of all device drivers loaded in the system, showing
each device's name and version number.

```bash
amiga@192.168.6.228:SYS:> devices
NAME               VERSION
timer.device          50.1
keyboard.device       40.1
gameport.device       40.1
input.device          50.1
trackdisk.device      40.1
console.device        44.2
```

These are Exec-level device drivers (the I/O subsystem), not DOS
devices or volume names. To see mounted filesystem volumes, use
`volumes` instead.

### ports

List active Exec message ports on the Amiga.

```
ports
```

Prints the name of every public message port currently registered
with Exec. Ports are the inter-process communication mechanism in
AmigaOS -- applications, device drivers, and system services create
named ports to receive messages.

```bash
amiga@192.168.6.228:SYS:> ports
REXX
AMIGACTLD
WORKBENCH
```

This command is useful for discovering ARexx port names before using
the `arexx` command to send commands to an application.


## Environment Variables

AmigaOS stores environment variables in two locations: `ENV:` (the
current session, stored in RAM) and `ENVARC:` (the persistent
archive, stored on disk). By default, `setenv` writes to both
locations so that variables survive a reboot. The `-v` flag
restricts the write to `ENV:` only.

### env

Get the value of an AmigaOS environment variable.

```
env NAME
```

Prints the value of the named global environment variable. The
command reads from `ENV:` (the active session copy).

| Argument | Description |
|----------|-------------|
| `NAME`   | Variable name (case-sensitive). |

```bash
amiga@192.168.6.228:SYS:> env Workbench
3.2
amiga@192.168.6.228:SYS:> env Language
english
```

If the variable does not exist, the daemon returns a "not found"
error.

`getenv` is an alias for `env`.

### setenv

Set or delete an AmigaOS environment variable.

```
setenv NAME VALUE
setenv -v NAME VALUE
setenv NAME
```

Creates, updates, or deletes a named environment variable. When a
value is provided, the variable is set. When the value is omitted,
the variable is deleted.

| Argument / Flag | Description |
|-----------------|-------------|
| `NAME`          | Variable name (case-sensitive). |
| `VALUE`         | Value to assign. Omit to delete the variable. If the value contains spaces, quote it. |
| `-v`            | Volatile only -- write to `ENV:` but not `ENVARC:`. The variable will be lost on reboot. |

Setting a persistent variable (written to both `ENV:` and
`ENVARC:`):

```bash
amiga@192.168.6.228:SYS:> setenv MyVar hello
Set: MyVar=hello
```

Setting a volatile variable (session only, lost on reboot):

```bash
amiga@192.168.6.228:SYS:> setenv -v TempVar 42
Set: TempVar=42
```

Deleting a variable:

```bash
amiga@192.168.6.228:SYS:> setenv MyVar
Deleted: MyVar
```

When deleting without `-v`, both the `ENV:` and `ENVARC:` copies
are removed. With `-v`, only the `ENV:` copy is removed (the
`ENVARC:` copy, if any, remains and will be restored on reboot).


## Logical Assigns

Logical assigns are AmigaOS name aliases that map a short name
(followed by a colon) to one or more physical directory paths.
Assigns are a central part of how AmigaOS locates files --
`LIBS:`, `DEVS:`, `FONTS:`, and many application-specific names
are all assigns rather than physical volumes.

### assigns

List all logical assigns.

```
assigns
```

Prints every currently defined assign and its target path. Each
line shows the assign name (with trailing colon) and the path it
resolves to.

```bash
amiga@192.168.6.228:SYS:> assigns
SYS:       DH0:
C:         SYS:C
S:         SYS:S
L:         SYS:L
LIBS:      SYS:Libs
DEVS:      SYS:Devs
FONTS:     SYS:Fonts
REXX:      SYS:Rexxc
ENV:       RAM:Env
ENVARC:    SYS:Prefs/Env-Archive
T:         RAM:T
```

The names are left-aligned and padded to form columns. To create,
modify, or remove individual assigns, use the `assign` command.

### assign

Create, modify, or remove a logical assign.

```
assign NAME: PATH
assign late NAME: PATH
assign add NAME: PATH
assign NAME:
```

Manipulates a single logical assign. The assign name must include
its trailing colon.

| Syntax | Description |
|--------|-------------|
| `assign NAME: PATH`      | Create or replace a lock-based assign. The daemon obtains a filesystem lock on `PATH` immediately. If `PATH` does not exist, an error is returned. |
| `assign late NAME: PATH`  | Create a late-binding assign. The path is stored as a string and resolved only when first accessed. Useful for paths on removable media or volumes that may not be mounted yet. |
| `assign add NAME: PATH`   | Add `PATH` to an existing multi-directory assign. The new path is appended to its search list rather than replacing the old one. The assign must already exist -- if `NAME:` is not defined, the daemon returns an error (e.g., `AssignAdd failed`). |
| `assign NAME:`            | Remove the assign entirely. |

Creating a new assign:

```bash
amiga@192.168.6.228:SYS:> assign TEST: RAM:
Assigned: TEST: -> RAM:
```

Creating a late-binding assign:

```bash
amiga@192.168.6.228:SYS:> assign late MYSRC: Work:Source
Assigned: MYSRC: -> Work:Source
```

Adding a second directory to an existing assign:

```bash
amiga@192.168.6.228:SYS:> assign add LIBS: Work:ExtraLibs
Assigned: LIBS: -> Work:ExtraLibs
```

Removing an assign:

```bash
amiga@192.168.6.228:SYS:> assign TEST:
Removed: TEST:
```

Tab completion is available for the `PATH` argument, using the
standard Amiga path completion.


## Administrative Commands

These commands affect the entire Amiga system. Both require the
daemon to have the corresponding permission explicitly enabled in
its configuration file -- by default, remote shutdown and reboot
are disabled for safety.

### shutdown

Shut down the Amiga.

```
shutdown
```

Sends a shutdown command to the Amiga. Before executing, the shell
prompts for confirmation:

```bash
amiga@192.168.6.228:SYS:> shutdown
Shut down the Amiga. Are you sure? [y/N] y
Shutdown command sent.
amiga>
```

Typing anything other than `y` (or pressing Enter for the default
`N`) cancels the operation:

```bash
amiga@192.168.6.228:SYS:> shutdown
Shut down the Amiga. Are you sure? [y/N] n
Cancelled.
```

After a successful shutdown, the connection is closed and the
prompt reverts to the disconnected state (`amiga>`).

The daemon must have `ALLOW_REMOTE_SHUTDOWN` enabled in its
configuration. If remote shutdown is not permitted, the daemon
returns a permission denied error:

```bash
amiga@192.168.6.228:SYS:> shutdown
Shut down the Amiga. Are you sure? [y/N] y
Error: Remote shutdown not permitted
```

### reboot

Reboot the Amiga.

```
reboot
```

Sends a reboot command to the Amiga, triggering a ColdReboot().
Like `shutdown`, the shell prompts for confirmation before
proceeding:

```bash
amiga@192.168.6.228:SYS:> reboot
Reboot the Amiga. Are you sure? [y/N] y
Reboot command sent.
amiga>
```

Because ColdReboot() may terminate the TCP/IP stack before the
daemon can send its acknowledgment, a broken connection during
reboot is treated as success rather than an error.

After reboot, the connection is closed. Use `reconnect` (or restart
the shell) once the Amiga has finished booting and the daemon is
running again.

The daemon must have `ALLOW_REMOTE_REBOOT` enabled in its
configuration. If not permitted, a permission denied error is
returned.


## Related Documentation

- [navigation.md](navigation.md) -- Directory navigation and Amiga
  path syntax.
- [file-operations.md](file-operations.md) -- File and directory
  management commands.
- [file-transfer.md](file-transfer.md) -- Transferring files between
  host and Amiga.
- [command-execution.md](command-execution.md) -- Running AmigaDOS
  and ARexx commands.
