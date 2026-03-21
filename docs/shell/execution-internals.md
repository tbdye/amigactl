# Execution Internals

The daemon's execution engine (`daemon/exec.c`) runs AmigaOS commands on
behalf of remote clients. It provides two execution modes -- synchronous
and asynchronous -- along with a process table for tracking background
processes and a signal-based mechanism for detecting their completion.

This document covers the daemon-side implementation. For the user-facing
shell commands (`exec`, `run`, `ps`, `status`, `signal`, `kill`), see
[command-execution.md](command-execution.md).


## Synchronous Execution

### How It Works

`exec_sync()` executes a command using `SystemTags()` and captures its
output to a temporary file. The sequence is:

1. Parse an optional `CD=<path>` prefix from the command string.
2. Create a temp file in `T:` for stdout capture.
3. Open `NIL:` as stdin.
4. If a `CD=` prefix was given, `Lock()` the directory and `CurrentDir()`
   to it.
5. Call `SystemTags()` with `SYS_Output` set to the temp file handle and
   `SYS_Input` set to the `NIL:` handle. `SYS_Asynch` is not used, so
   the daemon retains ownership of both handles and must `Close()` them
   after the call returns.
6. Restore the original directory (if changed) and `UnLock()` the
   `CD=` lock.
7. Close the output and input handles.
8. If `SystemTags()` returned `-1` (command not found or execution
   failure), delete the temp file and send an error response.
9. Re-open the temp file for reading, send an `OK rc=<N>` response,
   then stream the file contents to the client in 4096-byte chunks
   using a static `read_buf` buffer. Delete the temp file when done.

The return code from `SystemTags()` is the AmigaOS return code of the
executed command. A return of `-1` means `SystemTags()` itself failed
(e.g., the command could not be found by the shell).

### Temp File Management

Temp files are named `T:amigactld_exec_N.tmp`, where `N` is a
monotonically increasing sequence counter (`exec_seq`). The counter is
a module-level static integer that starts at zero and increments on
every synchronous execution. It is not reset across the daemon's
lifetime, so file names do not collide even if a previous deletion
failed.

At daemon startup, `exec_cleanup_temp_files()` scans the `T:` directory
using `Examine()`/`ExNext()` and deletes any files whose names begin
with `amigactld_exec_`. This cleans up leftovers from a previous daemon
run that may have crashed or been killed before deleting its temp files.

### Blocking Behavior

`SystemTags()` without `SYS_Asynch` is a blocking call. While it runs,
the daemon's event loop is stalled. No other client can send or receive
data. All connected sessions are frozen until the command completes.

This is an inherent limitation of the synchronous execution path. The
daemon is single-threaded and processes commands inline in the event
loop. Long-running commands should use asynchronous execution instead.

### Working Directory

Both synchronous and asynchronous execution support a `CD=<path>` prefix
parsed by `parse_cd_prefix()`. The parser:

1. Checks whether the argument string begins with `CD=` (case-insensitive
   via `strnicmp`).
2. Extracts the path (everything between `CD=` and the next whitespace
   or end of string).
3. Calls `Lock()` on the path. If the lock fails, sends an `ERR 200
   Directory not found` error and returns `NULL`.
4. Advances the returned pointer past the `CD=` token and any trailing
   whitespace.

The caller then uses `CurrentDir()` to switch to the locked directory,
saving the old lock. After command execution, `CurrentDir()` restores
the saved lock and `UnLock()` releases the `CD=` directory.

For async execution, the `cd_lock` is stored in the process table slot
and passed to the child process, which performs the `CurrentDir()` /
`UnLock()` itself.


## Asynchronous Execution

### Process Creation

`exec_async()` creates a new AmigaOS process via `CreateNewProcTags()`
to run the command independently of the daemon's event loop. The key
tags are:

| Tag         | Value                         |
|-------------|-------------------------------|
| `NP_Entry`  | `async_wrapper` function      |
| `NP_Name`   | Command basename (from a per-slot `proc_name` buffer) |
| `NP_StackSize` | 16384 bytes                |
| `NP_Cli`    | `TRUE` (creates a CLI context for the process) |

The process name is derived from the command's basename -- the portion
after the last `/` or `:` in the first whitespace-delimited word. This
is truncated to 31 characters and stored in the slot's `proc_name`
buffer. AmigaOS stores the `NP_Name` pointer rather than copying the
string, so the buffer must outlive the process. The per-slot buffer in
`daemon_state` satisfies this requirement since the daemon state persists
for the daemon's lifetime. If the basename is empty (e.g., the command
path ends with `:` or `/`), the name defaults to `amigactld-exec`.

The slot is populated before `CreateNewProcTags()` is called, and the
creation is wrapped in `Forbid()`/`Permit()` to prevent the child from
running (and scanning for its slot) before the parent has stored the
`task` pointer. If process creation fails, the slot is cleaned up and
an error is returned.

After successful creation, the daemon immediately responds with `OK`
and the assigned process ID. It does not wait for the command to start
or complete.

### Command Resolution

The `async_wrapper()` entry point parses the command string into a
command name and argument string, then calls `find_command_segment()` to
locate a loadable binary. The search order is:

1. **Resident list.** `FindSegment()` searches the system's resident
   command list. The function respects the `seg_UC` (use count) field:
   - `CMD_DISABLED` (`-999`): Disabled segment. Skipped; falls through
     to the path search.
   - `CMD_SYSTEM` (`-1`) or `CMD_INTERNAL` (`-2`): Permanent system or
     internal command. Used directly without modifying the use count.
     `is_resident` is set to `2`.
   - `>= 0`: User-loaded resident command. The use count is incremented
     under `Forbid()` to prevent unloading during execution.
     `is_resident` is set to `1`.

2. **CLI command path.** If the daemon process has a CLI context
   (`pr_CLI`), the `cli_CommandDir` linked list is walked. For each
   path entry, `CurrentDir()` switches to that directory and
   `LoadSeg()` attempts to load the command by name. The original
   directory is restored after each attempt.

3. **C: directory.** The command name is prefixed with `C:` and passed
   to `LoadSeg()`.

4. **Current directory.** `LoadSeg()` is called with the bare command
   name, resolving against the process's current directory.

If any step produces a segment, it is returned immediately (the later
steps are not tried). If no segment is found, `find_command_segment()`
returns zero.

After execution, `release_command_segment()` cleans up based on the
`is_resident` value:
- `0`: Disk-loaded segment. `UnLoadSeg()` frees it.
- `1`: User-loaded resident. The use count is decremented under
  `Forbid()`.
- `2`: Permanent resident. No action needed.

### Execution Strategy

When `find_command_segment()` returns a segment, the wrapper uses
`RunCommand()` to execute it. When no segment is found, it falls back
to `SystemTags()`.

Before either path is taken, the wrapper sets `cli_CommandName` on the
process's CLI structure to a BSTR of the command basename. This allows
tools like atrace's `resolve_cli_name()` to identify the process by its
actual command name rather than the generic "Background CLI" label. The
BSTR is constructed in a LONG-aligned buffer to satisfy the `MKBADDR`
alignment requirement. The original `cli_CommandName` is saved and
restored after execution completes, regardless of which execution path
was used.

**RunCommand (preferred).** `RunCommand()` executes a loaded segment
within the current process context -- it does not create a child process.
This has a critical advantage: break signals delivered to the wrapper
task via `Signal()` are directly visible to the running command through
`CheckSignal()`. The `signal` command works reliably because the
signaled task is the same task running the command.

The argument string is formatted as a newline-terminated buffer, as
`RunCommand()` expects. `SelectInput()` and `SelectOutput()` redirect
the process's standard I/O to `NIL:` handles for the duration of the
call.

**SystemTags (fallback).** When the command binary cannot be found on
disk -- because it is a shell built-in, a script file, or uses a path
that `find_command_segment()` does not resolve -- the wrapper falls back
to `SystemTags()`. This creates a child shell process to interpret the
command line. The trade-off is that signals sent to the wrapper process
do not reach the child shell, so `signal` may not be effective.

**Exit() caveat.** If a command executed via `RunCommand()` calls the
DOS `Exit()` function, the wrapper process terminates without cleanup.
The process table slot remains in `PROC_RUNNING` state with no task
to signal. This is inherent to `RunCommand()` and cannot be worked
around.

### Output Handling

Async commands produce no captured output. Both stdin and stdout are
connected to `NIL:` (the AmigaOS equivalent of `/dev/null`). Two
separate `NIL:` file handles are opened -- one for input
(`MODE_OLDFILE`) and one for output (`MODE_NEWFILE`). If either open
fails, the wrapper signals completion with `rc=-1` and exits.

For `RunCommand()`, `SelectInput()` and `SelectOutput()` redirect the
process's standard handles to the `NIL:` handles. For the
`SystemTags()` fallback, `SYS_Input` and `SYS_Output` tags pass the
handles directly.

Both handles are closed by the wrapper after execution completes.


## Process Table

### Structure

The process table is an array of `MAX_TRACKED_PROCS` (16)
`tracked_proc` structures embedded in the `daemon_state`:

| Field       | Type            | Description |
|-------------|-----------------|-------------|
| `id`        | `int`           | Daemon-assigned process ID. Zero indicates a never-used slot. |
| `task`      | `struct Task *` | Pointer to the child task. `NULL` when exited. |
| `command`   | `char[256]`     | Copy of the command string. |
| `status`    | `int`           | `PROC_RUNNING` (0) or `PROC_EXITED` (1). |
| `rc`        | `int`           | Return code. Valid only when `PROC_EXITED`. |
| `completed` | `int`           | Set to 1 by the wrapper under `Forbid()` to signal completion. |
| `cd_lock`   | `BPTR`          | Optional directory lock. Cleared after the wrapper unlocks it. |
| `proc_name` | `char[32]`      | NP_Name buffer. Must outlive the process. |

### Slot Allocation

`exec_async()` searches for a slot using a two-pass preference order:

1. **Never-used slot** (`id == 0`): Best choice. Selected immediately.
2. **Exited slot** (any slot where `status != PROC_RUNNING`): Candidate
   for LRU eviction. Among all exited slots, the one with the lowest
   `id` (oldest) is selected.

If no never-used or exited slot is found, all 16 slots are occupied by
running processes. The daemon returns `ERR 500 Process table full` and
the request fails.

Note that exited processes remain in the table until their slot is
reclaimed. This allows clients to query exit status via `PROCSTAT`
after the process has finished. The slot is only reused when a new
async execution needs it.

### Process IDs

Process IDs are assigned from `daemon_state.next_proc_id`, which starts
at 1 and increments monotonically. IDs are never reused -- even when a
slot is reclaimed, the new process gets the next sequential ID.
Eventually the counter will wrap (at `INT_MAX`), but this requires
billions of process launches.


## Completion Signaling

### Signal Bit Mechanism

At daemon startup, `exec_init()` allocates a signal bit using
`AllocSignal(-1L)`, which requests any free bit. The bit number is
stored in the global `g_proc_sigbit` and the daemon's `Task` pointer
in `g_daemon_task`.

When an async wrapper finishes execution, it signals completion by:

1. Entering `Forbid()` (prevents task switching).
2. Setting `slot->completed = 1`.
3. Calling `Signal(g_daemon_task, 1L << g_proc_sigbit)`.
4. Returning from the function (still under `Forbid()`), which triggers
   the system's `RemTask()` to clean up the child.

The `Forbid()` is essential: it prevents the daemon from calling
`RemTask()` or reusing the slot between the moment `completed` is set
and the moment the wrapper's return triggers the implicit `RemTask()`.
Without it, the daemon could attempt to `RemTask()` a task that is
still executing its final instructions.

At shutdown, `exec_cleanup()` intentionally does not call `FreeSignal()`.
A slow async wrapper may still be running and could call `Signal()` on
the freed bit, corrupting an unrelated signal. The bit is harmlessly
reclaimed when the daemon task exits.

### Harvesting

The event loop in `main.c` includes the process completion signal in
the `sigmask` passed to `WaitSelect()`:

```c
if (g_proc_sigbit >= 0)
    sigmask |= (1L << g_proc_sigbit);
```

When `WaitSelect()` returns with this bit set, the event loop calls
`exec_scan_completed()`. This function iterates all 16 process table
slots and, for each slot that is both `PROC_RUNNING` and has
`completed == 1`:

1. Transitions `status` to `PROC_EXITED`.
2. Clears `completed` to zero.
3. Sets `task` to `NULL` (the task is already gone or about to be
   removed by the system).

This scan is non-blocking -- it reads the `completed` flag that was set
by the wrapper under `Forbid()` and does not call any blocking system
functions. Multiple processes can complete between event loop iterations;
the scan harvests all of them in a single pass.


## Process Control

### Break Signals

`cmd_signal_proc()` delivers AmigaOS break signals to running processes.
The supported signals are:

| Wire name | Flag               | Typical use |
|-----------|--------------------|-------------|
| `CTRL_C`  | `SIGBREAKF_CTRL_C` | Abort/interrupt (default) |
| `CTRL_D`  | `SIGBREAKF_CTRL_D` | Exit from interactive input |
| `CTRL_E`  | `SIGBREAKF_CTRL_E` | Application-defined |
| `CTRL_F`  | `SIGBREAKF_CTRL_F` | Application-defined |

If no signal name is specified, `CTRL_C` is used as the default.

Signal delivery is race-safe. The function performs an initial check
that the process is running (outside `Forbid()`), then re-checks both
`status` and `completed` under `Forbid()` before calling `Signal()`.
The second check catches the case where the wrapper set `completed`
between the initial check and the `Forbid()`.

Break signaling is cooperative. The target process must call
`CheckSignal()` or `SetSignal()` to detect and respond to break
signals. A program that does not check will continue running.

### Forced Termination

`cmd_kill()` forcibly removes a process using `RemTask()`. This
operation is guarded by the `ALLOW_REMOTE_SHUTDOWN` configuration
option -- if not enabled, the daemon rejects the request with
`ERR 201 Remote kill not permitted`.

The kill operation runs under `Forbid()` with a race check:

1. If `slot->completed == 1`, the wrapper has already finished. The
   slot is simply transitioned to `PROC_EXITED` (the task is gone and
   `RemTask()` would crash on a stale pointer).
2. If `slot->completed == 0`, the wrapper is still running. `RemTask()`
   removes it immediately. The slot is marked `PROC_EXITED` with
   `rc = -1`. If the slot still holds a `cd_lock` (the wrapper had
   not reached its unlock code), the lock is released to prevent a
   resource leak.

`RemTask()` is destructive -- the process gets no opportunity to
close files, free memory, or release locks. Any resources held by the
command (other than the `cd_lock` that the daemon manages) are leaked.


## Shutdown

### Graceful Shutdown

When the daemon exits (via `CTRL_C` or `SHUTDOWN CONFIRM`), the event
loop terminates and `exec_shutdown_procs()` is called. Under a single
`Forbid()`/`Permit()` pair, it iterates all process table slots:

- **Never-used slots** (`id == 0`): Skipped.
- **Already completed** (`completed == 1`): Transitioned to
  `PROC_EXITED` without signaling. The task is already gone.
- **Still running** (`status == PROC_RUNNING`, `completed == 0`):
  A `SIGBREAKF_CTRL_C` signal is sent to request graceful termination.
  The slot is immediately marked `PROC_EXITED` with `rc = -1` and
  `task = NULL`.

The `Forbid()` ensures that no wrapper can transition between the
status check and the signal delivery. Note that the daemon does not
wait for signaled processes to actually exit -- it marks them as exited
and proceeds with shutdown. Wrappers that have not yet exited may still
write to `slot->rc` and `slot->completed` and call `Signal()` on the
daemon. This is harmless because the daemon is about to exit and these
fields are no longer read.

### Temp File Cleanup

`exec_cleanup_temp_files()` runs at startup (not shutdown) to clean up
stale temp files from a previous daemon instance. It scans the `T:`
directory for files matching the `amigactld_exec_` prefix and deletes
them.

Temp files are cleaned up at startup rather than shutdown because:

- A crashing daemon cannot clean up after itself.
- A killed daemon (via `CTRL_C` during a sync exec) may leave a temp
  file behind from the interrupted `SystemTags()` call.
- Startup cleanup handles both cases without adding complexity to the
  shutdown path.


## Related Documentation

- [command-execution.md](command-execution.md) -- User-facing shell
  commands for execution and process management.
- [architecture.md](architecture.md) -- Shell architecture and design.
