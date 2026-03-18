# Command Execution

The amigactl shell can execute AmigaOS CLI commands on the Amiga
remotely. Commands can run synchronously (blocking until completion with
captured output) or asynchronously (returning immediately with a process
ID for later management). The shell also provides commands for listing,
inspecting, signaling, and force-terminating background processes.


## Synchronous Execution

### exec

Execute an AmigaOS CLI command and wait for it to finish.

```
exec COMMAND...
```

The entire argument string is passed to the daemon as a single command
line. The daemon executes it via `SystemTags()`, captures stdout to a
temporary file, and sends the output back when the command exits.
Sync commands use `SystemTags()` rather than `RunCommand()` because
there is no process ID to manage -- the entire command runs and
completes within a single daemon event loop iteration. The shell
displays the output followed by the return code.

```bash
amiga@192.168.6.228:SYS:> exec list SYS:S
S/Startup-Sequence
S/Shell-Startup
S/User-Startup
Return code: 0
```

```bash
amiga@192.168.6.228:SYS:> exec avail FLUSH
Return code: 0
```

```bash
amiga@192.168.6.228:SYS:> exec CD=NoSuchDir: list
Error: Directory not found
```

**Working directory.** If the shell has a current working directory
(set via `cd`), it is automatically prepended to the command as a
`CD=<path>` prefix. The daemon locks the specified directory and
changes to it before executing the command, then restores the original
directory afterward. This means commands that use relative paths will
resolve them against the shell's CWD:

```bash
amiga@192.168.6.228:Work:Projects> exec list
myproject
testdata
Return code: 0
```

**Blocking behavior.** `exec` is synchronous -- the shell prompt does
not return until the command finishes. More importantly, the daemon
processes the command in its main event loop, which means a
long-running `exec` blocks the daemon from servicing any other
connected clients. All other sessions are stalled until the command
completes. For commands that take more than a moment, use `run` instead.

**Standard input.** The command's standard input is connected to
`NIL:` (the AmigaOS equivalent of `/dev/null`). Interactive commands
that read from stdin will see immediate end-of-file.

**Tab completion:** The command argument supports tab completion against
the Amiga filesystem.


## Asynchronous Execution

### run

Launch an AmigaOS CLI command in the background.

```
run COMMAND...
```

The daemon creates a new AmigaOS process to run the command and returns
a process ID immediately. The command runs independently -- its output
is directed to `NIL:` and is not captured or returned to the client.
The shell prompt returns as soon as the process is created.

```bash
amiga@192.168.6.228:SYS:> run wait 30
Process ID: 1
amiga@192.168.6.228:SYS:> run execute SYS:S/MyScript
Process ID: 2
```

**Working directory.** Like `exec`, the shell's current working
directory is passed via the `CD=` prefix. The daemon locks the
directory and passes it to the child process, which changes to it
before executing the command.

**Process lookup.** The daemon first tries to locate the command binary
on disk (checking the resident list, the CLI command path, `C:`, and
the current directory) and runs it with `RunCommand()`. This executes
the command within the wrapper process context, which means break
signals sent via `signal` are directly visible to the running command.
If the binary cannot be found (for shell built-ins, script files, etc.),
the daemon falls back to `SystemTags()`, which creates a child shell
process -- in this case, signals sent to the wrapper may not reach the
actual command.

**Process table.** The daemon tracks up to 16 concurrent background
processes. If all 16 slots are occupied by running processes, `run`
returns an error. If some slots hold exited processes, the oldest
exited slot is reused.

**No output capture.** Unlike `exec`, async commands produce no visible
output. Both stdin and stdout are connected to `NIL:`. To see results,
the command must write to a file or produce side effects that can be
observed through other means.

**Tab completion:** The command argument supports tab completion against
the Amiga filesystem.


## Process Management

### ps

List all processes tracked by the daemon.

```
ps
```

Displays a table of all tracked processes -- both running and exited.
Only processes started via `run` appear in this list; `exec` commands
are not tracked.

```bash
amiga@192.168.6.228:SYS:> ps
ID  COMMAND                STATUS   RC
1   wait 30                RUNNING  -
2   execute SYS:S/MyScript EXITED   0
3   copy RAM:test Work:    EXITED   0
```

**Columns:**

| Column    | Description |
|-----------|-------------|
| `ID`      | Daemon-assigned process ID, monotonically increasing starting from 1. |
| `COMMAND` | The command string as passed to `run`. |
| `STATUS`  | `RUNNING` if the process is still executing, `EXITED` if it has finished. |
| `RC`      | The AmigaOS return code. Shown as `-` while the process is still running. A value of `-1` indicates the process was killed or failed to execute. |

If no processes have been launched (or all tracked slots have been
reused), `ps` prints "No tracked processes."

### status

Show detailed status of a single tracked process.

```
status ID
```

**Arguments:**

| Argument | Description |
|----------|-------------|
| `ID`     | Process ID returned by `run`. |

Displays key=value pairs for the specified process:

```bash
amiga@192.168.6.228:SYS:> status 1
id=1
command=wait 30
status=RUNNING
rc=-
```

```bash
amiga@192.168.6.228:SYS:> status 2
id=2
command=execute SYS:S/MyScript
status=EXITED
rc=0
```

**Fields:**

| Field     | Description |
|-----------|-------------|
| `id`      | The process ID. |
| `command` | The command string. |
| `status`  | `RUNNING` or `EXITED`. |
| `rc`      | Return code, or `-` if still running. |

If the process ID does not exist in the daemon's tracking table, an
error is printed:

```bash
amiga@192.168.6.228:SYS:> status 99
Error: Process not found
```


## Process Control

### signal

Send a break signal to a running background process.

```
signal ID [SIG]
```

**Arguments:**

| Argument | Description |
|----------|-------------|
| `ID`     | Process ID returned by `run`. |
| `SIG`    | Signal name (optional, defaults to `CTRL_C`). |

AmigaOS break signals are the standard mechanism for requesting that a
program stop or change behavior. The available signals are:

| Signal   | Typical meaning |
|----------|-----------------|
| `CTRL_C` | Abort/interrupt (default). Most programs treat this as a request to exit gracefully. |
| `CTRL_D` | Exit from interactive input (similar to Unix EOF). |
| `CTRL_E` | Application-defined. |
| `CTRL_F` | Application-defined. |

```bash
amiga@192.168.6.228:SYS:> signal 1
Signal sent.
amiga@192.168.6.228:SYS:> signal 1 CTRL_D
Signal sent.
```

Signaling is cooperative -- the target command must check for and
respond to break signals. A program that ignores signals will continue
running after `signal` is issued. The signal is delivered to the
wrapper process that `run` created. When the command was loaded via
`RunCommand()` (the common case for binary commands), the signal is
directly visible to the command through `CheckSignal()`. When the
fallback `SystemTags()` path was used, signals reach the wrapper but
may not propagate to the child shell.

If the process ID does not exist in the daemon's tracking table:

```bash
amiga@192.168.6.228:SYS:> signal 99
Error: Process not found
```

If the process has already exited:

```bash
amiga@192.168.6.228:SYS:> signal 1
Error: Process not running
```

### kill

Force-terminate a running background process.

```
kill ID
```

**Arguments:**

| Argument | Description |
|----------|-------------|
| `ID`     | Process ID returned by `run`. |

Immediately and forcibly removes the process using `RemTask()`. This
is not a graceful shutdown -- the process is destroyed without any
opportunity to clean up resources (close files, free memory, release
locks). The process's return code is set to `-1`.

```bash
amiga@192.168.6.228:SYS:> kill 1
Process terminated.
```

**Permission required.** The `kill` command requires the
`ALLOW_REMOTE_SHUTDOWN YES` option in the daemon's configuration file.
If this option is not enabled, the daemon rejects the request:

```bash
amiga@192.168.6.228:SYS:> kill 1
Error: Remote kill not permitted
```

**Use `signal` first.** Always try `signal ID` (which sends `CTRL_C`)
before resorting to `kill`. A signaled process can close files, free
memory, and exit cleanly. `kill` should be a last resort for processes
that are unresponsive to break signals.


## Understanding Return Codes

AmigaOS uses a convention of four return code severity levels:

| Return code | Severity | Meaning |
|-------------|----------|---------|
| 0           | OK       | The command completed successfully. |
| 5           | WARN     | The command completed with warnings. Some operations may have been skipped. |
| 10          | ERROR    | The command encountered an error. The primary operation likely failed. |
| 20          | FAIL     | The command failed completely. A serious or unrecoverable error occurred. |

These are conventions, not strict requirements -- individual programs
may return any integer value. A return code of `-1` in `ps` or `status`
output indicates that the background command could not be executed at
all or that the process was forcibly killed via `kill`. Synchronous
`exec` reports execution failures as error messages instead.

```bash
amiga@192.168.6.228:SYS:> exec type SYS:S/Startup-Sequence
; Startup-Sequence
...
Return code: 0

amiga@192.168.6.228:SYS:> exec type SYS:NoSuchFile
Return code: 20

amiga@192.168.6.228:SYS:> exec search SYS:S "pattern" QUIET
Return code: 5
```


## Sync vs Async Trade-offs

The choice between `exec` and `run` depends on what you need from the
command.

**Use `exec` when:**

- You need to see the command's output.
- The command runs quickly (a few seconds or less).
- You are the only client connected, or other clients can tolerate a
  brief pause.
- You want to check the return code before proceeding.

**Use `run` when:**

- The command takes a long time (copies, compilations, network
  operations).
- You do not need to see stdout output.
- Other clients are connected and must remain responsive.
- You want to launch multiple commands in parallel.

The key behavioral differences:

| Behavior              | `exec`                  | `run`                   |
|-----------------------|-------------------------|-------------------------|
| Blocks the shell      | Yes, until completion   | No, returns immediately |
| Blocks other clients  | Yes, daemon is occupied | No, daemon stays responsive |
| Captures output       | Yes, displayed in shell | No, output goes to NIL: |
| Returns return code   | Immediately, on completion | Later, via `status` or `ps` |
| Can be signaled       | No (no process ID)      | Yes, via `signal` or `kill` |
| Tracked in process table | No                   | Yes                     |


## Related Documentation

- [navigation.md](navigation.md) -- Directory navigation and Amiga
  path syntax (relevant to how `CD=` prefixing works).
- [file-operations.md](file-operations.md) -- File and directory
  management commands.
- [file-transfer.md](file-transfer.md) -- Transferring files between
  host and Amiga.
