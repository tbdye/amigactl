# Output Tiers: Basic, Detail, Verbose

atrace organizes its 99 traced functions into four tiers that control
which functions generate events. Tiers are a coarse-grained verbosity
control: lower tiers produce fewer events by limiting which functions
are active, while higher tiers add more functions for deeper
investigation. The fourth category, Manual, contains high-frequency
functions that are never activated by any tier and must be enabled
individually.

The canonical source of truth for tier membership is
`client/amigactl/trace_tiers.py`. The loader (`atrace/main.c`) and
daemon (`daemon/trace.c`) maintain parallel tables that must stay in
sync with the Python definitions.

For per-function documentation (arguments, return values, error
classification), see [traced-functions.md](traced-functions.md). For
CLI syntax of tier-related flags, see
[cli-reference.md](cli-reference.md).


## Tier System Overview

The tier system operates on two levels:

1. **Patch-level enable/disable.** Each of the 99 function patches in
   the Amiga-side atrace installation has an `enabled` flag. When a
   patch is disabled, its stub is a transparent pass-through: the
   original library function executes with no event recording and
   negligible overhead. Tier switching changes which patches are
   enabled.

2. **Content-based suppression.** At the Basic tier, the daemon applies
   additional content-aware filtering that suppresses certain
   high-noise successful calls (described in [Basic-Tier Noise
   Suppression](#basic-tier-noise-suppression) below). These
   suppressions are lifted at Detail tier and above.

Tiers are cumulative. Each higher tier includes all functions from
the tiers below it:

| Tier | Level | Functions Enabled | Total Active |
|------|-------|-------------------|--------------|
| Basic | 1 | 57 | 57 |
| Detail | 2 | Basic + 13 | 70 |
| Verbose | 3 | Basic + Detail + 3 | 73 |
| Manual | -- | Never auto-enabled | 0 (unless individually enabled) |

Manual-tier functions are never included in any tier-based activation.
They must be enabled individually through the toggle grid, CLI
commands, or Python API. When a Manual-tier function is individually
enabled, it operates alongside whatever tier is currently active.

At installation time, the loader enables all 57 Basic-tier patches and
disables the remaining 42 (Detail + Verbose + Manual). The loader
prints a summary:

```
Auto-disabled 42 non-basic functions (tiers: detail=13, verbose=3, manual=26)
```


## Tier 1 -- Basic (Default)

The Basic tier contains 57 functions selected for immediate diagnostic
value: each event tells you something meaningful about program
behavior without requiring correlation with other events. These are the
functions most useful for general-purpose debugging -- file operations,
library loading, window management, network connections.

Basic is the default tier. It activates automatically when atrace loads
and when a new trace session starts.

### dos.library (19 functions)

| Function | Purpose |
|----------|---------|
| Open | Open a file or device |
| Close | Close a file handle |
| Lock | Obtain a filesystem lock |
| DeleteFile | Delete a file |
| Execute | Execute a CLI command string |
| GetVar | Read an environment/local variable |
| FindVar | Look up a local variable |
| LoadSeg | Load an executable segment list |
| NewLoadSeg | Load segments with tag options |
| CreateDir | Create a directory |
| MakeLink | Create a hard or soft link |
| Rename | Rename a file or directory |
| RunCommand | Run a loaded segment as a command |
| SetVar | Set an environment/local variable |
| DeleteVar | Delete an environment/local variable |
| SystemTagList | Execute a command via System() |
| AddDosEntry | Add a DOS list entry (assign, volume) |
| CurrentDir | Change the current directory lock |
| SetProtection | Set file protection bits |

### exec.library (5 functions)

| Function | Purpose |
|----------|---------|
| OpenDevice | Open an Exec device |
| CloseDevice | Close an Exec device |
| OpenLibrary | Open a shared library |
| OpenResource | Open a system resource |
| FindResident | Find a resident module by name |

### intuition.library (12 functions)

| Function | Purpose |
|----------|---------|
| OpenWindow | Open a window (legacy struct) |
| CloseWindow | Close a window |
| OpenScreen | Open a screen (legacy struct) |
| CloseScreen | Close a screen |
| ActivateWindow | Bring a window to input focus |
| WindowToFront | Move a window to the front |
| WindowToBack | Move a window behind others |
| OpenWorkBench | Open the Workbench screen |
| CloseWorkBench | Close the Workbench screen |
| LockPubScreen | Lock a public screen by name |
| OpenWindowTagList | Open a window (taglist API) |
| OpenScreenTagList | Open a screen (taglist API) |

### bsdsocket.library (10 functions)

| Function | Purpose |
|----------|---------|
| socket | Create a socket |
| bind | Bind a socket to an address |
| listen | Mark a socket as listening |
| accept | Accept an incoming connection |
| connect | Connect to a remote address |
| shutdown | Shut down part of a connection |
| CloseSocket | Close a socket |
| setsockopt | Set a socket option |
| getsockopt | Get a socket option |
| IoctlSocket | Socket I/O control |

### icon.library (5 functions)

| Function | Purpose |
|----------|---------|
| GetDiskObject | Read a .info icon file |
| PutDiskObject | Write a .info icon file |
| FreeDiskObject | Free a DiskObject structure |
| FindToolType | Search tool types for a key |
| MatchToolValue | Match a tool type value |

### workbench.library (6 functions)

| Function | Purpose |
|----------|---------|
| AddAppIconA | Register an application icon |
| RemoveAppIcon | Remove an application icon |
| AddAppWindowA | Register an application window |
| RemoveAppWindow | Remove an application window |
| AddAppMenuItemA | Register an application menu item |
| RemoveAppMenuItem | Remove an application menu item |


## Basic-Tier Noise Suppression

At the Basic tier (and only at the Basic tier), the daemon applies two
content-based suppression rules that filter out high-volume successful
calls. These suppressions reduce noise without hiding any failures.
When the tier is raised to Detail or above, these suppressions are
lifted and all matching events pass through.

### OpenLibrary version 0 suppression

Successful `OpenLibrary` calls where the requested version is 0 are
suppressed. Version-0 opens are AmigaOS internal housekeeping: CLI
startup sequences, library interdependencies, and runtime initialization
all open libraries with version 0 (meaning "any version"). These
generate substantial noise with no diagnostic value.

Failed `OpenLibrary` calls (return value 0/NULL) are never suppressed,
regardless of the requested version. A failed library open is always
diagnostically significant.

At Detail tier and above, all `OpenLibrary` events pass through
unfiltered.

### Lock success suppression

Successful `Lock` calls (return value non-zero) are suppressed. Locks
are the filesystem's fundamental path-resolution primitive, and AmigaOS
issues dozens of Lock calls during routine operations -- every
`LoadSeg`, `Execute`, `SystemTagList`, and path lookup triggers
multiple internal Lock calls. Showing every successful Lock at the
Basic tier would bury the events that actually matter.

Failed `Lock` calls (return value 0, indicating the path was not found
or the lock could not be obtained) are never suppressed. A failed Lock
is a common source of bugs and is always shown.

Even when successful Lock events are suppressed from the output stream,
the daemon still caches the lock-to-path mapping so that subsequent
events referencing that lock handle (such as `CurrentDir` or
`UnLock`) can resolve the path correctly.

At Detail tier and above, all `Lock` events pass through unfiltered.

### Implementation

The daemon maintains a `g_current_tier` variable (initialized to 1 at
session start) that controls whether these suppressions are active. The
suppression checks run in the event consumer loop, after the event is
read from the ring buffer but before it is formatted and sent to the
client. Suppressed events are counted in the `g_self_filtered` statistic.

When the client switches tiers, it sends a `TIER <n>` inline command
to the daemon, which updates `g_current_tier`. This is separate from
the `ENABLE=/DISABLE=` commands that change which patches are active --
the TIER command only controls content-based suppression behavior.


## Tier 2 -- Detail

The Detail tier adds 13 functions to the Basic set, bringing the total
to 70 active functions. These functions provide deeper visibility into
resource lifecycle management -- signal allocation, message port
creation, lock release, segment unloading. They are separated from
Basic because they generate moderate event volume during normal
operation and are primarily useful when investigating specific resource
management issues.

### exec.library (5 functions)

| Function | Purpose |
|----------|---------|
| AllocSignal | Allocate a signal bit |
| FreeSignal | Free a signal bit |
| CreateMsgPort | Create a message port |
| DeleteMsgPort | Delete a message port |
| CloseLibrary | Close a shared library |

### dos.library (4 functions)

| Function | Purpose |
|----------|---------|
| UnLock | Release a filesystem lock |
| Examine | Examine a locked file/directory |
| Seek | Seek within an open file |
| UnLoadSeg | Unload an executable segment list |

### intuition.library (2 functions)

| Function | Purpose |
|----------|---------|
| ModifyIDCMP | Change a window's IDCMP flags |
| UnlockPubScreen | Release a public screen lock |

### bsdsocket.library (2 functions)

| Function | Purpose |
|----------|---------|
| sendto | Send data to a specific address |
| recvfrom | Receive data with sender address |


## Tier 3 -- Verbose

The Verbose tier adds 3 functions to the Detail set, bringing the total
to 73 active functions. These functions produce high-volume bursts tied
to specific user actions -- directory enumeration and font operations.
A single directory listing can generate hundreds of `ExNext` events; a
program opening a requester may trigger several `OpenFont`/`CloseFont`
pairs. They are in their own tier because their burst behavior can
temporarily dominate the event stream.

### dos.library (1 function)

| Function | Purpose |
|----------|---------|
| ExNext | Iterate to the next directory entry |

### graphics.library (2 functions)

| Function | Purpose |
|----------|---------|
| OpenFont | Open a font by attributes |
| CloseFont | Close a font |


## Manual Tier

The Manual tier contains 26 functions that are never auto-enabled by
any tier. These are extreme-event-rate functions that fire continuously
during normal system operation -- memory allocation, message passing,
I/O operations, semaphore management. Enabling any of them system-wide
produces thousands of events per second that overwhelm the ring buffer
and make the output stream unreadable.

Manual-tier functions are designed for targeted investigation using
task-scoped filtering (`trace run`), where only a single program's
calls are captured. Even with task filtering, some of these functions
(particularly `AllocMem`/`FreeMem` and `Wait`/`Signal`) produce
substantial output.

To enable a Manual-tier function, use the toggle grid in the
interactive viewer (Tab key, then navigate to the function), the CLI
`trace enable` command, or the Python API `trace_enable()` method.

### exec.library (21 functions)

| Function | Purpose |
|----------|---------|
| FindPort | Find a named message port |
| FindSemaphore | Find a named semaphore |
| FindTask | Find a task by name |
| PutMsg | Send a message to a port |
| GetMsg | Retrieve a message from a port |
| ObtainSemaphore | Acquire a semaphore (blocking) |
| ReleaseSemaphore | Release a semaphore |
| AllocMem | Allocate memory |
| FreeMem | Free memory |
| AllocVec | Allocate memory with size tracking |
| FreeVec | Free AllocVec-allocated memory |
| Wait | Wait for signals |
| Signal | Send signals to a task |
| DoIO | Synchronous device I/O |
| SendIO | Asynchronous device I/O (start) |
| WaitIO | Wait for asynchronous I/O completion |
| AbortIO | Abort a pending I/O request |
| CheckIO | Check I/O request completion |
| ReplyMsg | Reply to a message |
| AddPort | Add a message port to the system list |
| WaitPort | Wait for a message on a port |

### dos.library (2 functions)

| Function | Purpose |
|----------|---------|
| Read | Read bytes from a file handle |
| Write | Write bytes to a file handle |

### bsdsocket.library (3 functions)

| Function | Purpose |
|----------|---------|
| send | Send data on a connected socket |
| recv | Receive data from a connected socket |
| WaitSelect | Wait for socket activity (select) |


## Switching Tiers

There are three ways to change the active tier during a trace session.

### CLI flags

The `trace start` and `trace run` subcommands accept mutually
exclusive tier flags:

```
amigactl trace start --basic          # Tier 1 (default)
amigactl trace start --detail         # Tier 2
amigactl trace start --verbose        # Tier 3
```

```
amigactl trace run --detail -- List SYS:C
```

When a tier flag higher than Basic is specified, the client sends
`ENABLE` commands for the additional functions before starting the
trace stream. The `--basic` flag is accepted for explicitness but has
no effect since Basic is the default.

### TUI keys

In the interactive trace viewer (launched via `amigactl shell`, then
`trace start`), press a number key to switch tiers instantly:

| Key | Tier | Effect |
|-----|------|--------|
| `1` | Basic | Disable Detail and Verbose functions |
| `2` | Detail | Enable Detail functions, disable Verbose |
| `3` | Verbose | Enable all tiered functions |

Tier switching in the TUI computes the minimal delta between the
current effective function set (including any manual overrides) and the
target tier's clean function set. Only the functions that need to
change state are sent in `ENABLE=/DISABLE=` filter commands. This
minimizes the protocol traffic and avoids unnecessary patch state
churn.

Manual overrides are reset on tier switch. If you have manually enabled
or disabled individual functions through the toggle grid, pressing a
tier key returns to the clean tier state.

### Python API

The `trace_tiers` module provides programmatic access to tier
definitions and switching logic:

```python
from amigactl.trace_tiers import (
    TIER_BASIC, TIER_DETAIL, TIER_VERBOSE, TIER_MANUAL,
    functions_for_tier, compute_tier_switch, tier_for_function,
)

# Get the cumulative function set for a tier
detail_funcs = functions_for_tier(2)   # Basic + Detail = 70 functions
verbose_funcs = functions_for_tier(3)  # Basic + Detail + Verbose = 73

# Compute what to enable/disable when switching tiers
to_enable, to_disable = compute_tier_switch(
    old_level=1, new_level=2)

# Check which tier a function belongs to
tier_for_function("Open")           # 1 (Basic)
tier_for_function("UnLock")         # 2 (Detail)
tier_for_function("ExNext")         # 3 (Verbose)
tier_for_function("AllocMem")       # None (Manual)
```

The `compute_tier_switch()` function also accepts `manual_additions`
and `manual_removals` sets to account for individual function overrides
when computing the delta.


## Choosing the Right Tier

**Basic** is the right starting point for nearly all investigations.
It covers the most diagnostically valuable functions -- file access,
library loading, window and screen management, network connections --
while keeping event volume manageable. The noise suppression rules
further reduce clutter by hiding routine successful Lock and
OpenLibrary calls. Start here and move to a higher tier only if you
need visibility that Basic does not provide.

**Detail** is appropriate when investigating resource lifecycle issues:
signal exhaustion, message port leaks, lock leaks (UnLock without
matching Lock), segment loading problems, or library reference counting
(CloseLibrary without matching OpenLibrary). It is also useful for
socket debugging that involves sendto/recvfrom (datagram-oriented
communication). Expect roughly 20-50% more events than Basic during
typical interactive use.

**Verbose** is for targeted analysis of directory enumeration (ExNext
produces one event per directory entry) or font loading behavior.
Enable this tier when investigating slow directory listings, font
selection problems, or programs that scan directories extensively.
Expect burst-heavy output during directory operations.

**Manual** functions should be enabled individually, not as a group.
Pick the specific function you need (e.g., `AllocMem` to track memory
allocation patterns, `Wait` to understand task scheduling, `send`/`recv`
to trace socket data flow) and enable it for a targeted session,
preferably with task filtering via `trace run`. Enabling more than a
few Manual-tier functions simultaneously will likely overflow the ring
buffer.
