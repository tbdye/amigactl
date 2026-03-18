# Recipes

Practical workflows combining shell commands for common tasks. Each
recipe shows the commands to type, expected output where helpful, and
tips for getting the most out of the shell.


## Conventions Used in This Document

- All commands are typed at the amigactl shell prompt. The prompt
  format is `amiga@<host>:<cwd>>`.
- Example output is fabricated but realistic.
- Paths use plausible Amiga filesystem layouts. Your volume names and
  directory structures will differ.


## File Management

### Finding and Cleaning Up Temp Files

Locate `.tmp` and `.bak` files scattered across a volume, review them,
and remove the ones you no longer need.

```bash
amiga@192.168.6.228:SYS:> find Work: *.tmp
Projects/build/output.tmp
Projects/build/link.tmp
Temp/scratch.tmp

amiga@192.168.6.228:SYS:> find Work: *.bak
Documents/readme.bak
Projects/src/main.c.bak
```

Check what a file contains before deleting it:

```bash
amiga@192.168.6.228:SYS:> cat Work:Temp/scratch.tmp
(temporary build output)

amiga@192.168.6.228:SYS:> rm Work:Temp/scratch.tmp
Deleted: Work:Temp/scratch.tmp
```

To narrow the search to just files (excluding any directories that
happen to match the glob):

```bash
amiga@192.168.6.228:SYS:> find Work: -type f *.tmp
```

**Tip:** `rm` only deletes one file at a time and cannot delete
non-empty directories. There is no recursive delete -- you must remove
files individually, then remove the empty directory.


### Comparing Config File Versions

Check whether a startup sequence has been modified from its backup:

```bash
amiga@192.168.6.228:SYS:> diff SYS:S/Startup-Sequence SYS:S/Startup-Sequence.bak
--- SYS:S/Startup-Sequence
+++ SYS:S/Startup-Sequence.bak
@@ -12,7 +12,6 @@
 SetPatch QUIET
 AddBuffers DH0: 50
-C:amigactld
 BindDrivers
```

The output is a unified diff with color: additions in green, deletions
in red, and hunk headers in bold. If the files are identical, it says
so. Binary files are detected (by the presence of null bytes) and
reported without attempting a text diff.


### Checking Disk Space

Get an overview of space usage across all mounted volumes:

```bash
amiga@192.168.6.228:SYS:> volumes
NAME     USED    FREE  CAPACITY
System   42M     18M       60M
Work    180M     70M      250M
RAM       0       8M        8M
```

For a deeper look at which directories are consuming space on a
specific volume:

```bash
amiga@192.168.6.228:SYS:> du -h Work:
12K     Documents
145M    Projects
145M    Projects/build
820     Projects/src
22M     Temp
180M    Work:
```

Use `-s` for just the total without the per-directory breakdown:

```bash
amiga@192.168.6.228:SYS:> du -sh Work:
180M    Work:
```

**Tip:** `du` performs a recursive directory listing behind the scenes,
so it can be slow on large directory trees. Use it on specific
subdirectories rather than an entire volume when possible.


## System Administration

### Monitoring a Log File

Watch new lines appear in a log file in real time, similar to
`tail -f` on Unix:

```bash
amiga@192.168.6.228:SYS:> tail RAM:server.log
[12:05:01] Connection from 192.168.6.100
[12:05:02] Request: GET /index.html
[12:05:03] Response: 200 OK
[12:05:15] Connection from 192.168.6.101
...
```

Press Ctrl-C to stop following. The command detects file truncation and
deletion, so it handles log rotation gracefully.

**Tip:** `tail` streams data as it is appended. It does not show
existing file contents on startup -- only new data written after the
command begins. Use `cat` first if you need to see the current
contents.


### Checking What's Running

List all tasks and processes on the Amiga:

```bash
amiga@192.168.6.228:SYS:> tasks
NAME              TYPE      PRI  STATE    STACK
exec.library      TASK        0  ready     4096
input.device      TASK       20  wait      4096
ramlib            PROCESS     0  wait      4096
amigactld         PROCESS     0  wait     65536
Shell Process     PROCESS     0  wait      4096
myserver          PROCESS     0  wait      8192
```

To see only the processes launched through the daemon (via `run`):

```bash
amiga@192.168.6.228:SYS:> ps
ID  COMMAND              STATUS    RC
1   myserver             RUNNING   -
2   wait 60              EXITED    0
```

**Tip:** `tasks` shows the system-wide Exec task list (all tasks and
processes), while `ps` shows only processes launched through amigactl's
`run` command. Use `tasks` for a system overview; use `ps` to manage
your own background jobs.


### Managing Background Tasks

Launch a command in the background, monitor it, and stop it when done:

```bash
amiga@192.168.6.228:SYS:> run execute SYS:S/MyScript
Process ID: 3

amiga@192.168.6.228:SYS:> ps
ID  COMMAND                STATUS    RC
3   execute SYS:S/MyScript RUNNING   -

amiga@192.168.6.228:SYS:> signal 3
Signal sent.

amiga@192.168.6.228:SYS:> ps
ID  COMMAND                STATUS    RC
3   execute SYS:S/MyScript EXITED    0
```

If the process does not respond to `signal` (which sends CTRL_C), you
can force-terminate it:

```bash
amiga@192.168.6.228:SYS:> kill 3
Process terminated.
```

You can also send other break signals:

```bash
amiga@192.168.6.228:SYS:> signal 3 CTRL_D
Signal sent.
```

**Tip:** Always try `signal` before `kill`. The `signal` command sends
a break signal that well-behaved programs handle gracefully. The `kill`
command forcibly removes the process from tracking without giving it a
chance to clean up.


### Restarting a Service

Stop a running daemon, start it again, and verify it came back up. This
example restarts a hypothetical server process:

```bash
amiga@192.168.6.228:SYS:> exec break myserver C
Return code: 0

amiga@192.168.6.228:SYS:> run C:myserver
Process ID: 4

amiga@192.168.6.228:SYS:> ps
ID  COMMAND     STATUS    RC
4   C:myserver  RUNNING   -
```

Confirm it is visible in the system task list:

```bash
amiga@192.168.6.228:SYS:> tasks
NAME              TYPE      PRI  STATE    STACK
...
myserver          PROCESS     0  wait      8192
...
```

And confirm its message port is registered:

```bash
amiga@192.168.6.228:SYS:> ports
REXX
amigactld
myserver.port
```


## Debugging

### Searching for a String in Source Files

Find all files under a project directory containing a specific string:

```bash
amiga@192.168.6.228:SYS:> grep -rn TODO Work:Projects/src
main.c:42:/* TODO: handle error case */
utils.c:17:/* TODO: optimize this loop */
network.c:88:/* TODO: add timeout */
```

Search case-insensitively with a regex pattern:

```bash
amiga@192.168.6.228:SYS:> grep -Erin "error|warning" Work:Projects/src
main.c:55:    printf("Error: failed to open file\n");
main.c:91:    printf("Warning: buffer nearly full\n");
network.c:103:    printf("Error: connection refused\n");
```

List only filenames that contain matches (without showing the lines
themselves):

```bash
amiga@192.168.6.228:SYS:> grep -rl printf Work:Projects/src
main.c
utils.c
network.c
```

Get match counts per file:

```bash
amiga@192.168.6.228:SYS:> grep -rc TODO Work:Projects/src
main.c:1
utils.c:1
network.c:1
```

**Tip:** Recursive grep downloads each file to search it locally. On
large directory trees with many files, this can be slow. Narrow the
search to a specific subdirectory when possible.


### Watching a Command Repeatedly

Monitor memory usage over time by running `exec avail` every 5 seconds:

```bash
amiga@192.168.6.228:SYS:> watch -n 5 exec avail
Every 5.0s: exec avail

Type   Available    In-Use   Maximum   Largest
chip     4832728   3355688   8388608   4702208
fast    12058624   4329792  16777216  11894784
total   16891352   7685480  25165824  16597024
```

The screen clears and refreshes on each iteration. Press Ctrl-C to
stop.

You can watch any shell command -- not just `exec`. For example,
monitor the process list:

```bash
amiga@192.168.6.228:SYS:> watch ps
```

Or watch a directory for new files:

```bash
amiga@192.168.6.228:SYS:> watch -n 10 ls -l RAM:T
```


### Examining Library Versions

Check the version of a specific library:

```bash
amiga@192.168.6.228:SYS:> libver exec.library
name=exec.library
version=47.3
```

Check several libraries in sequence to understand the system's
software baseline:

```bash
amiga@192.168.6.228:SYS:> libver dos.library
name=dos.library
version=47.1

amiga@192.168.6.228:SYS:> libver intuition.library
name=intuition.library
version=47.1

amiga@192.168.6.228:SYS:> libver bsdsocket.library
name=bsdsocket.library
version=4.307
```

For a broader system overview that includes CPU, Kickstart version, and
RAM totals:

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

**Tip:** `libver` queries a library by name and returns its version
string. The library must be in `LIBS:` or already resident. Use
`devices` to list loaded device drivers, or `sysinfo` for a
one-command system summary.


## Environment Setup

### Checking and Setting Environment Variables

Read the current value of an environment variable:

```bash
amiga@192.168.6.228:SYS:> env Workbench
47.1

amiga@192.168.6.228:SYS:> env Language
english
```

Set a persistent environment variable (written to both `ENV:` and
`ENVARC:` so it survives reboot):

```bash
amiga@192.168.6.228:SYS:> setenv MyApp_Debug 1
Set: MyApp_Debug=1
```

Set a volatile variable (session only, not persisted to `ENVARC:`):

```bash
amiga@192.168.6.228:SYS:> setenv -v TempPath RAM:T
Set: TempPath=RAM:T
```

Delete an environment variable:

```bash
amiga@192.168.6.228:SYS:> setenv MyApp_Debug
Deleted: MyApp_Debug
```

**Tip:** Without `-v`, `setenv` writes to both `ENV:` (current
session) and `ENVARC:` (persistent across reboots). Use `-v` for
temporary variables that should not survive a reboot.


### Creating Temporary Assigns

Create a quick assign pointing to a project directory:

```bash
amiga@192.168.6.228:SYS:> assign PROJ: Work:Projects/MyApp
Assigned: PROJ: -> Work:Projects/MyApp

amiga@192.168.6.228:SYS:> cd PROJ:
amiga@192.168.6.228:PROJ:> ls
src/        docs/       Makefile
```

Create a late-binding assign (resolved on first access, not
immediately):

```bash
amiga@192.168.6.228:SYS:> assign late PROJ: Work:Projects/MyApp
Assigned: PROJ: -> Work:Projects/MyApp
```

Add an additional directory to an existing multi-directory assign:

```bash
amiga@192.168.6.228:SYS:> assign add LIBS: Work:MyLibs
Assigned: LIBS: -> Work:MyLibs
```

View all current assigns:

```bash
amiga@192.168.6.228:SYS:> assigns
C:          SYS:C
DEVS:       SYS:Devs
FONTS:      SYS:Fonts
L:          SYS:L
LIBS:       SYS:Libs
PROJ:       Work:Projects/MyApp
S:          SYS:S
```

Remove an assign when you are done:

```bash
amiga@192.168.6.228:SYS:> assign PROJ:
Removed: PROJ:
```

**Tip:** Late-binding assigns (`assign late`) are useful when the
target directory might not exist yet at the time of assignment. The
path is not validated until first access.


## File Transfer Workflows

### Deploying a Binary

Upload a cross-compiled binary from the host, set its protection bits
to allow execution, and verify:

```bash
amiga@192.168.6.228:SYS:> put /home/user/build/myapp C:myapp
Uploaded 28672 bytes to SYS:C/myapp

amiga@192.168.6.228:SYS:> chmod C:myapp
protection=----rwed

amiga@192.168.6.228:SYS:> chmod C:myapp 20
protection=--p-rwed
```

Verify the upload by comparing checksums:

```bash
amiga@192.168.6.228:SYS:> checksum C:myapp
crc32=a1b2c3d4
size=28672
```

Test the deployed binary:

```bash
amiga@192.168.6.228:SYS:> exec C:myapp --version
MyApp 1.0 (2026-03-15)
Return code: 0
```

**Tip:** The `put` command uploads the file with the same protection
bits as the existing file (if replacing), or default bits for a new
file. Use `chmod` afterward if you need specific bits like the Pure
(`p`) flag for resident-capable programs.


### Editing a Remote Config

Open a remote file in your local editor, make changes, and upload the
result -- all in one command:

```bash
amiga@192.168.6.228:SYS:> edit S/User-Startup
```

This downloads the file, opens it in your editor (`$VISUAL`,
`$EDITOR`, or `vi` by default), and uploads the modified version when
you save and quit. If you quit without saving, no upload occurs:

```
No changes detected.
```

If someone else modifies the file on the Amiga while you are editing,
the shell detects the conflict and asks before overwriting:

```
Warning: file was modified remotely while editing.
  Remote datestamp was: 2026-03-10 08:00:00
  Remote datestamp now: 2026-03-10 09:15:22
Upload anyway? [y/N]
```

**Tip:** If the upload fails (network error, disk full), the local
copy is preserved in a temporary directory. The path is printed so you
can recover your work.


### Backing Up a Directory

There is no recursive `get` command, but you can combine `find` with
individual `get` calls to back up files from a directory. First,
survey what needs backing up:

```bash
amiga@192.168.6.228:SYS:> find SYS:S -type f *
Startup-Sequence
User-Startup
Shell-Startup
SPat
```

Then download each file individually:

```bash
amiga@192.168.6.228:SYS:> cd S
amiga@192.168.6.228:SYS:S> get Startup-Sequence /tmp/amiga-backup/Startup-Sequence
Downloaded 1234 bytes to /tmp/amiga-backup/Startup-Sequence

amiga@192.168.6.228:SYS:S> get User-Startup /tmp/amiga-backup/User-Startup
Downloaded 567 bytes to /tmp/amiga-backup/User-Startup
```

For binary files, verify integrity with `checksum`:

```bash
amiga@192.168.6.228:SYS:S> checksum Startup-Sequence
crc32=1a2b3c4d
size=1234
```

**Tip:** For automated or large-scale backups, use the Python API or
host-side scripting rather than the interactive shell. The shell is
best suited for ad-hoc transfers of individual files.


## Related Documentation

- [navigation.md](navigation.md) -- Directory navigation, path syntax,
  and CWD management.
- [file-operations.md](file-operations.md) -- File and directory
  management commands (`ls`, `rm`, `mv`, `mkdir`, `cp`, `chmod`,
  `touch`, `tree`).
- [file-transfer.md](file-transfer.md) -- Transferring files between
  host and Amiga (`get`, `put`, `edit`, `append`).
- [search-utilities.md](search-utilities.md) -- Search and analysis
  commands (`find`, `grep`, `diff`, `du`, `watch`).
- [command-execution.md](command-execution.md) -- Running AmigaOS
  commands (`exec`, `run`, `ps`, `signal`, `kill`).
- [system-commands.md](system-commands.md) -- System information and
  environment commands.
- [arexx.md](arexx.md) -- ARexx interprocess communication.
- [limitations.md](limitations.md) -- Known limitations and
  workarounds.
