# Common Use Cases and Recipes

Practical cookbook-style examples for common debugging scenarios with
atrace. Each recipe shows the commands to run, what to look for in the
output, and tips for interpreting the results.

## Conventions Used in This Document

- Shell commands prefixed with `$` run on the client (Linux/macOS).
- Commands prefixed with `amigactl>` run inside the interactive shell
  (`amigactl shell`).
- Example output is fabricated but realistic, based on actual event
  formatting from the daemon.
- All CLI examples assume `--host` is configured via config file or
  environment variable. Add `--host <ip>` if needed.

**Important:** The CLI `--func` flag accepts only a single function
name. Comma-separated values are treated as a literal name and will
not match anything. For multi-function filtering from the command line,
use the interactive shell's wire protocol syntax (e.g.,
`FUNC=Open,Lock,Close`) or the Python API presets. See
[filtering.md](filtering.md) for details.


## File I/O Debugging

Track file operations to understand how a program accesses the
filesystem -- which files it opens, what locks it acquires, and where
failures occur.

### Approach 1: All DOS library calls (CLI)

```
$ amigactl trace start --lib dos
```

This shows every dos.library call in the Basic tier: Open, Close, Lock,
DeleteFile, Execute, GetVar, FindVar, LoadSeg, NewLoadSeg, CreateDir,
MakeLink, Rename, RunCommand, SetVar, DeleteVar, SystemTagList,
AddDosEntry, CurrentDir, and SetProtection.

### Approach 2: Focused file operations (shell)

For just the core file I/O functions without the noise of GetVar/SetVar
shell initialization, use the interactive shell with comma-separated
FUNC= syntax:

```
amigactl> trace start FUNC=Open,Close,Lock,DeleteFile,CreateDir,Rename
```

### Approach 3: Trace a specific program's file access

```
$ amigactl trace run --verbose --lib dos -- List SYS:C
```

This launches `List SYS:C`, traces only its dos.library calls, and
stops when the program exits.

### Example output

The following output shows Verbose-tier tracing from Approach 3, which
includes Examine and ExNext events not visible at Basic tier.

```
SEQ          TIME  FUNCTION                     TASK                 ARGS                                     RESULT
42       0:00.003  dos.Open                     [4] List             "SYS:C",MODE_OLDFILE                     0x001e4a80
43       0:00.003  dos.Lock                     [4] List             "SYS:C",SHARED_LOCK                      0x001b3c44
44       0:00.004  dos.Examine                  [4] List             lock=0x1b3c44,fib=0x1e4b00                OK
45       0:00.004  dos.ExNext                   [4] List             lock=0x1b3c44,fib=0x1e4b00                OK
...
127      0:00.031  dos.ExNext                   [4] List             lock=0x1b3c44,fib=0x1e4b00                FAIL (232, no more entries)
128      0:00.031  dos.UnLock                   [4] List             "SYS:C"                                   (void)
129      0:00.031  dos.Close                    [4] List             "SYS:C" [fh from seq 42]                  OK
```

### What to look for

- **Open returning NULL**: Failed file open. The IoErr value tells you
  why (e.g., `(205, object not found)` or `(224, file is read protected)`).
- **Lock returning NULL**: Directory or file not found.
- **ExNext returning FAIL with error 232**: Normal end-of-directory.
  This is not a real error -- the `no more entries` code signals the
  directory scan is complete.
- **Unmatched Open without Close**: Potential file handle leak. The
  handle resolver in the interactive viewer annotates Close events with
  the original filename from the Open event, making it easy to match
  pairs.

### Finding failed file operations

```
$ amigactl trace start --errors --lib dos
```

This combines the `--errors` and `--lib` filters (AND-combined) to show
only DOS calls that returned error values. Useful for finding why a
program cannot find a file or why a write fails.

### Tips

- **Detail tier adds Examine, UnLock, Seek, UnLoadSeg. Verbose tier
  adds ExNext.** Switch to `--detail` or `--verbose` if you need to see
  directory scans and lock lifecycles.
- The `"file-io"` preset is available in the Python API
  (`FILTER_PRESETS["file-io"]`). See [python-api.md](python-api.md).
- Cross-reference: [reading-output.md](reading-output.md) explains
  return value formatting for DOS functions, including IoErr display.


## Memory Tracking

Track memory allocation and deallocation to identify leaks, understand
allocation patterns, or measure memory consumption.

### Important: Manual tier functions

AllocMem, FreeMem, AllocVec, and FreeVec are in the Manual tier because
they fire at extreme rates during normal system operation. They are
never auto-enabled by any output tier. You **must** explicitly enable
them, and you should almost always combine them with task filtering
(TRACE RUN) to keep output volume manageable.

### Approach 1: Trace a program's memory usage (CLI)

```
$ amigactl trace enable AllocMem FreeMem AllocVec FreeVec
$ amigactl trace run --func AllocMem -- MyProgram
```

The `--func` flag on the CLI accepts only a single function name. To
see all four memory functions for a specific program, use the shell:

```
amigactl> trace enable AllocMem FreeMem AllocVec FreeVec
amigactl> trace run FUNC=AllocMem,FreeMem,AllocVec,FreeVec -- MyProgram
```

### Approach 2: System-wide memory monitoring (shell)

```
amigactl> trace enable AllocMem FreeMem AllocVec FreeVec
amigactl> trace start FUNC=AllocMem,FreeMem,AllocVec,FreeVec PROC=myapp
```

The `PROC=` filter limits output to a specific process by name
(case-insensitive substring match).

### Example output

```
SEQ          TIME  FUNCTION                     TASK                 ARGS                                     RESULT
201      0:00.015  exec.AllocMem                [5] MyProgram        1024,MEMF_PUBLIC                         0x00234000
202      0:00.015  exec.AllocMem                [5] MyProgram        256,MEMF_CLEAR                           0x00234800
203      0:00.016  exec.AllocVec                [5] MyProgram        4096,MEMF_PUBLIC|MEMF_CLEAR              0x00235000
...
315      0:00.042  exec.FreeMem                 [5] MyProgram        0x234800,256                             (void)
316      0:00.042  exec.FreeVec                 [5] MyProgram        0x235000                                 (void)
```

### Leak detection approach

1. Run `trace run` with all four memory functions enabled.
2. After the program exits, review the output in the interactive
   viewer. Use the pause and scrollback features to examine the trace.
3. Look for AllocMem/AllocVec calls whose returned addresses never
   appear in a subsequent FreeMem/FreeVec call.
4. Note the allocation sizes and MEMF flags to characterize the leak.

This is a manual process -- atrace does not automatically detect leaks.
But the per-program isolation from TRACE RUN makes the analysis
tractable because you only see one program's allocations.

### What to look for

- **AllocMem/AllocVec returning NULL**: Memory exhaustion. The
  requested size and flags are shown in the ARGS column.
- **MEMF flags**: `MEMF_PUBLIC` (shared), `MEMF_CHIP` (chip RAM for
  DMA), `MEMF_CLEAR` (zero-filled). These reveal what kind of memory
  the program needs.
- **Allocation sizes**: Unusually large allocations or many small
  allocations in a tight loop can indicate problems.

### Tips

- The `"memory"` preset is available in the Python API
  (`FILTER_PRESETS["memory"]`).
- FreeMem shows the address and size; FreeVec shows only the address
  (the size is stored in the allocation header by AllocVec).
- See [output-tiers.md](output-tiers.md) for the complete tier
  membership table explaining why memory functions are Manual tier.


## IPC Analysis

Trace inter-process communication through Exec message ports and
semaphores to understand how processes coordinate.

### Important: Manual tier functions

All IPC functions (FindPort, GetMsg, PutMsg, ObtainSemaphore,
ReleaseSemaphore, ReplyMsg, AddPort, WaitPort) are Manual tier. Enable
them explicitly and use process filtering to manage volume.

### Approach: Trace message passing for a program (shell)

```
amigactl> trace enable FindPort GetMsg PutMsg ReplyMsg AddPort WaitPort
amigactl> trace run FUNC=FindPort,GetMsg,PutMsg,ReplyMsg,AddPort,WaitPort -- MyProgram
```

For semaphore analysis:

```
amigactl> trace enable ObtainSemaphore ReleaseSemaphore FindSemaphore
amigactl> trace start FUNC=ObtainSemaphore,ReleaseSemaphore,FindSemaphore PROC=myapp
```

### Example output

```
SEQ          TIME  FUNCTION                     TASK                 ARGS                                     RESULT
88       0:01.220  exec.FindPort                [5] MyProgram        "REXX"                                   OK
89       0:01.221  exec.PutMsg                  [5] MyProgram        "REXX",msg=0x001a5000                    (void)
90       0:01.221  exec.WaitPort                [5] MyProgram        "MyApp.port"                             0x001a5080
91       0:01.250  exec.GetMsg                  [5] MyProgram        "MyApp.port"                             0x001a5080
92       0:01.250  exec.ReplyMsg                [5] MyProgram        msg=0x001a5000                           (void)
```

### What to look for

- **FindPort returning NULL**: The target port does not exist. The
  program is trying to communicate with a process that is not running.
- **Port names in quotes**: Named ports (e.g., `"REXX"`, `"MyApp.port"`)
  reveal the communication topology between processes.
- **Semaphore names**: Named semaphores show shared resource contention.
  If `ObtainSemaphore` calls are frequent for the same semaphore, that
  resource may be a bottleneck.
- **AddPort**: Shows when a process registers a new public port, making
  itself available for IPC.

### Tips

- The `"ipc"` preset is available in the Python API
  (`FILTER_PRESETS["ipc"]`).
- Combine IPC tracing with `--proc` to focus on one side of a
  conversation.
- See [traced-functions.md](traced-functions.md) for details on how
  port and semaphore names are resolved from the Node structure.


## Network Debugging

Trace bsdsocket.library calls to debug network applications --
connection failures, socket lifecycle, and data transfer problems.

### Approach 1: All network calls (CLI)

```
$ amigactl trace start --lib bsdsocket
```

This shows all 10 Basic-tier bsdsocket functions: socket, bind, listen,
accept, connect, shutdown, CloseSocket, setsockopt, getsockopt, and
IoctlSocket.

### Approach 2: Trace a specific program's networking (CLI)

```
$ amigactl trace run --lib bsdsocket -- MyNetApp
```

### Approach 3: Include data transfer functions (shell)

The send, recv, and WaitSelect functions are Manual tier due to high
event rates. Enable them for detailed data flow analysis:

```
amigactl> trace enable send recv WaitSelect
amigactl> trace start --detail LIB=bsdsocket PROC=mynetapp
```

send, recv, and WaitSelect are Manual tier and must be explicitly
enabled as shown above.

### Example output

```
SEQ          TIME  FUNCTION                     TASK                 ARGS                                     RESULT
15       0:00.001  bsdsocket.socket             [3] AmiTCP           AF_INET,SOCK_STREAM,proto=0              3
16       0:00.002  bsdsocket.setsockopt         [3] AmiTCP           fd=3,level=65535,opt=4                    0
17       0:00.002  bsdsocket.bind               [3] AmiTCP           fd=3,addr=0x1e3000,len=16                 0
18       0:00.002  bsdsocket.listen             [3] AmiTCP           fd=3,backlog=5                            0
19       0:05.441  bsdsocket.accept             [3] AmiTCP           fd=3,addr=0x1e3100                        4
20       0:05.442  bsdsocket.recv               [3] AmiTCP           fd=4,len=1024,flags=0x0                   128
21       0:05.443  bsdsocket.send               [3] AmiTCP           fd=4,len=128,flags=0x0                    128
22       0:05.500  bsdsocket.CloseSocket        [3] AmiTCP           fd=4                                      0
```

### What to look for

- **socket returning -1**: Cannot create socket. Usually means
  bsdsocket.library is not available or resources are exhausted.
- **connect returning -1**: Connection refused or timeout. The target
  host is not reachable or the port is not open.
- **bind returning -1**: Address already in use, or invalid address.
- **send/recv returning -1**: Connection error during data transfer.
- **Socket lifecycle**: A healthy connection follows
  socket -> connect -> send/recv -> CloseSocket (client) or
  socket -> bind -> listen -> accept -> send/recv -> CloseSocket
  (server). Deviations from this pattern indicate problems.

### Tips

- The `"network"` preset is available in the Python API
  (`FILTER_PRESETS["network"]`).
- The fd argument in bsdsocket calls is the socket descriptor (a small
  integer), not a pointer. Track it across calls to follow a single
  connection's lifecycle.
- bsdsocket return values use the -1 = error convention (displayed as
  `-1` with status `E` in the RESULT column).
- Cross-reference: [traced-functions.md](traced-functions.md) lists all
  15 bsdsocket functions with their argument formats.


## Startup Profiling

Analyze what a program does during launch: which libraries it opens,
what files it reads, which resources it allocates, and how it
initializes its UI.

### Approach 1: Basic startup trace (CLI)

```
$ amigactl trace run -- MyProgram
```

With the default Basic tier, this captures the most diagnostic events:
library opens, file access, window/screen creation, icon loading, and
Workbench integration. The trace automatically stops when the program
exits.

### Approach 2: Detailed startup with font loading (CLI)

```
$ amigactl trace run --verbose -- MyProgram
```

The Verbose tier adds OpenFont and CloseFont (graphics.library), showing
which fonts the program loads during initialization.

### Approach 3: With working directory (CLI)

```
$ amigactl trace run --cd "Work:MyApp" -- MyProgram
```

The `--cd` flag sets the current directory for the launched process,
which is important for programs that use relative paths.

### Example output

```
SEQ          TIME  FUNCTION                     TASK                 ARGS                                     RESULT
1        0:00.001  exec.OpenLibrary             [5] MyProgram        "dos.library",v37                        0x001a2000
2        0:00.001  exec.OpenLibrary             [5] MyProgram        "intuition.library",v37                  0x001a4000
3        0:00.001  exec.OpenLibrary             [5] MyProgram        "graphics.library",v37                   0x001a6000
4        0:00.002  exec.OpenLibrary             [5] MyProgram        "gadtools.library",v37                   0x001a8000
5        0:00.002  dos.Open                     [5] MyProgram        "PROGDIR:myapp.prefs",MODE_OLDFILE       0x001b0000
6        0:00.003  dos.Lock                     [5] MyProgram        "PROGDIR:data",SHARED_LOCK               0x001b2000
7        0:00.004  icon.GetDiskObject           [5] MyProgram        "PROGDIR:MyProgram"                      0x001b4000
8        0:00.005  icon.FindToolType            [5] MyProgram        0x1b4100,"PUBSCREEN"                     NULL
9        0:00.005  intuition.LockPubScreen      [5] MyProgram        NULL (default)                           0x001c0000
10       0:00.006  intuition.OpenWindowTagList   [5] MyProgram       "MyProgram v1.0"                         0x001d0000
11       0:00.007  graphics.OpenFont            [5] MyProgram        "topaz.font",8                           0x001d2000
```

### What to look for

- **Library open order**: Shows dependencies. OpenLibrary returning NULL
  means a required library is missing or the requested version is too
  high.
- **Configuration file access**: Open calls for preference files reveal
  where the program stores its settings.
- **Icon loading**: GetDiskObject and FindToolType show how the program
  reads its .info file and which Tool Types it checks.
- **Screen/window creation**: OpenWindowTagList and LockPubScreen show
  how the program sets up its display. Window titles are captured.
- **Font loading** (Verbose tier): OpenFont shows which fonts the
  program requests and at what sizes.

### Tips

- Use the interactive viewer's statistics mode (`s` key) to see a
  summary of the most-called functions during startup.
- The `S` key in the interactive viewer saves the scrollback to a file
  for offline analysis.
- Cross-reference: [trace-run.md](trace-run.md) for complete TRACE RUN
  documentation, including process exit detection and restrictions.


## Library Dependency Analysis

Discover which shared libraries, devices, and resources a program
opens, useful for understanding dependencies or diagnosing missing
library errors.

### Approach 1: Single function (CLI)

```
$ amigactl trace run --func OpenLibrary -- MyProgram
```

This traces only OpenLibrary calls for the target program.

### Approach 2: Complete library lifecycle (shell)

To see opens, closes, and device access together:

```
amigactl> trace run --detail FUNC=OpenLibrary,CloseLibrary,OpenDevice,CloseDevice,OpenResource -- MyProgram
```

### Example output

```
SEQ          TIME  FUNCTION                     TASK                 ARGS                                     RESULT
1        0:00.001  exec.OpenLibrary             [5] MyProgram        "dos.library",v37                        0x001a2000
2        0:00.001  exec.OpenLibrary             [5] MyProgram        "intuition.library",v39                  0x001a4000
3        0:00.001  exec.OpenLibrary             [5] MyProgram        "graphics.library",v37                   0x001a6000
4        0:00.002  exec.OpenLibrary             [5] MyProgram        "gadtools.library",v37                   0x001a8000
5        0:00.002  exec.OpenLibrary             [5] MyProgram        "asl.library",v38                        0x001aa000
6        0:00.003  exec.OpenLibrary             [5] MyProgram        "bsdsocket.library",v4                   NULL
7        0:00.003  exec.OpenDevice              [5] MyProgram        "timer.device",unit=0                    OK
```

### What to look for

- **OpenLibrary returning NULL**: The library is not installed, or the
  requested version (shown as `v<n>` in ARGS) is higher than what is
  available. This is the most common startup failure for Amiga software.
- **Version numbers**: The `v<n>` argument shows the minimum version
  the program requires. `v0` means "any version" and is common for
  optional libraries.
- **OpenDevice errors**: OpenDevice uses non-zero return = error
  convention (opposite of most functions). An `err=<n>` result means
  the device could not be opened.
- **Library close order**: With CloseLibrary enabled, verify that all
  opened libraries are properly closed during shutdown.

### Tips

- The `"lib-load"` preset is available in the Python API
  (`FILTER_PRESETS["lib-load"]`).
- The daemon suppresses OpenLibrary v0 events at Basic tier to reduce
  noise from system-internal library probing. Switch to `--detail` to
  see all OpenLibrary calls.
- Cross-reference: [output-tiers.md](output-tiers.md) explains the
  v0 suppression behavior at Basic tier.


## Error Hunting

Find all failing system calls across all libraries to quickly identify
what is going wrong, without needing to know which library or function
is involved.

### Approach 1: All errors (CLI)

```
$ amigactl trace start --errors
```

This shows only calls that returned error values, across all libraries
and all enabled functions. Error classification is per-function: NULL
return for pointer functions, FAIL for DOS booleans, -1 for bsdsocket
calls, and so on.

### Approach 2: Errors for a specific process (CLI)

```
$ amigactl trace start --errors --proc myapp
```

The `--proc` and `--errors` flags are AND-combined: only error events
from processes whose name contains "myapp" are shown.

### Approach 3: Errors for a specific program run (CLI)

```
$ amigactl trace run --errors -- MyProgram
```

### Approach 4: Errors in one library (CLI)

```
$ amigactl trace start --errors --lib dos
```

Shows only failed dos.library calls. Combined with IoErr display, this
is the fastest way to find "file not found" and similar problems.

### Example output

```
SEQ          TIME  FUNCTION                     TASK                 ARGS                                     RESULT
12       0:00.005  dos.Open                     [5] MyProgram        "ENV:myapp.prefs",MODE_OLDFILE            NULL (205, object not found)
34       0:00.018  dos.Lock                     [5] MyProgram        "LIBS:mylib.library",SHARED_LOCK          NULL (205, object not found)
67       0:00.025  exec.OpenLibrary             [5] MyProgram        "mylib.library",v1                        NULL
89       0:01.200  bsdsocket.connect            [5] MyProgram        fd=3,addr=0x1e3000,len=16                 -1
```

### What to look for

- **IoErr codes on DOS functions**: When a dos.library function fails
  and the stub captures IoErr, the error code and its name are appended
  to the RESULT column. Common codes:
  - 205: object not found
  - 202: object in use
  - 221: disk full
  - 222: file is protected from deletion
  - 224: file is read protected
- **NULL on OpenLibrary**: Missing or wrong-version library. The
  version argument shows what was requested.
- **-1 on bsdsocket functions**: Network operation failed.
- **Error classification**: Not all NULL returns are errors. GetMsg
  returning NULL (empty port) and MatchToolValue returning FALSE (no
  match) are normal and are excluded from `--errors` output by design.
  See [traced-functions.md](traced-functions.md) for the error check
  type of each function.

### Tips

- The `"errors-only"` preset is available in the Python API
  (`FILTER_PRESETS["errors-only"]`).
- In the interactive viewer, press `e` to toggle the errors-only filter
  at any time without restarting the trace.
- Some error events are expected during normal operation (e.g., probing
  for optional files). Focus on errors that correlate with the
  misbehavior you are investigating.
- Cross-reference: [filtering.md](filtering.md) explains the 8
  different error check types and how error classification works.


## Window and Screen Operations

Trace the Intuition windowing system to understand how a program
creates, manages, and destroys windows and screens.

### Approach 1: All Intuition calls (CLI)

```
$ amigactl trace start --lib intuition
```

This shows all Basic-tier Intuition functions: OpenWindow, CloseWindow,
OpenScreen, CloseScreen, ActivateWindow, WindowToFront, WindowToBack,
OpenWorkBench, CloseWorkBench, LockPubScreen, OpenWindowTagList, and
OpenScreenTagList.

### Approach 2: Trace a program's UI setup (CLI)

```
$ amigactl trace run --lib intuition -- MyProgram
```

### Example output

```
SEQ          TIME  FUNCTION                     TASK                 ARGS                                     RESULT
5        0:00.003  intuition.LockPubScreen      [5] MyProgram        NULL (default)                           0x001c0000
6        0:00.004  intuition.OpenScreenTagList   [5] MyProgram       "MyApp Screen"                           0x001d0000
7        0:00.005  intuition.OpenWindowTagList   [5] MyProgram       "Main Window"                            0x001e0000
8        0:00.006  intuition.OpenWindowTagList   [5] MyProgram       "Tool Palette"                           0x001e2000
9        0:00.007  intuition.ActivateWindow     [5] MyProgram        "Main Window"                            (void)
...
45       0:05.100  intuition.CloseWindow        [5] MyProgram        "Tool Palette"                           (void)
46       0:05.101  intuition.CloseWindow        [5] MyProgram        "Main Window"                            (void)
47       0:05.102  intuition.CloseScreen        [5] MyProgram        "MyApp Screen"                           (void)
```

### What to look for

- **Window and screen titles**: Captured from the NewWindow/NewScreen
  structures via pointer dereference. These identify which window is
  being operated on.
- **OpenWindow/OpenScreen returning NULL**: UI creation failed,
  typically due to insufficient memory or an invalid screen mode.
- **LockPubScreen**: Shows which public screen the program targets.
  `NULL (default)` means the Workbench screen.
- **Window ordering**: WindowToFront and WindowToBack events show how
  the program manages window Z-order.
- **Unmatched opens/closes**: Windows opened but never closed indicate
  cleanup problems.

### Detail tier additions

Switch to `--detail` to additionally see:
- **ModifyIDCMP**: IDCMP flag changes, showing which input events a
  window is listening for.
- **UnlockPubScreen**: Public screen unlock events.

### Tips

- The `"window"` preset is available in the Python API
  (`FILTER_PRESETS["window"]`).
- Cross-reference: [traced-functions.md](traced-functions.md) documents
  the dereference types used to extract window and screen titles.


## Icon and Workbench Integration

Trace how programs interact with the icon system and Workbench, useful
for debugging .info file access, AppIcon registration, and Workbench
menu items.

### Approach 1: Icon library calls (CLI)

```
$ amigactl trace start --lib icon
```

This shows GetDiskObject, PutDiskObject, FreeDiskObject, FindToolType,
and MatchToolValue.

### Approach 2: Combined icon and Workbench tracing (shell)

To see both icon.library and workbench.library together:

```
amigactl> trace start LIB=icon
```

There is no way to filter by two libraries simultaneously using a
single LIB= value. To see both, use the interactive viewer's toggle
grid (Tab key) to enable both icon and workbench libraries, or omit the
library filter and watch for functions from both libraries.

For Workbench-specific functions:

```
amigactl> trace start FUNC=AddAppIconA,RemoveAppIcon,AddAppWindowA,RemoveAppWindow
```

### Example output

```
SEQ          TIME  FUNCTION                     TASK                 ARGS                                     RESULT
12       0:00.004  icon.GetDiskObject           [5] MyProgram        "PROGDIR:MyProgram"                      0x001b4000
13       0:00.004  icon.FindToolType            [5] MyProgram        0x1b4100,"PUBSCREEN"                     0x001b4200
14       0:00.005  icon.FindToolType            [5] MyProgram        0x1b4100,"CX_POPUP"                      NULL
15       0:00.005  icon.FindToolType            [5] MyProgram        0x1b4100,"CX_PRIORITY"                   0x001b4220
16       0:00.006  icon.FreeDiskObject          [5] MyProgram        0x1b4000                                 (void)
17       0:00.007  workbench.AddAppIconA        [5] MyProgram        id=1,data=0x0,"MyApp",port=0x1a5000      0x001c0000
```

### What to look for

- **GetDiskObject returning NULL**: The .info file does not exist or
  cannot be read. The filename argument shows which icon was requested.
- **FindToolType returning NULL**: The Tool Type is not defined in the
  .info file. This is often normal (the program checks for optional
  Tool Types).
- **FindToolType returning a pointer**: The Tool Type exists. The
  value string is at the returned address.
- **AddAppIconA**: Registers an AppIcon on the Workbench. The text
  argument (third parameter) is the icon label.
- **Unmatched AddAppIconA/RemoveAppIcon**: If the program creates an
  AppIcon but never removes it, the icon persists after the program
  exits.

### Tips

- The `"icon"` preset is available in the Python API
  (`FILTER_PRESETS["icon"]`).
- GetDiskObject expects the program name without the `.info` extension.
  The filename in the ARGS column reflects this convention.
- Cross-reference: [traced-functions.md](traced-functions.md) for
  workbench.library function details including argument formats.


## Advanced Techniques

### Combining multiple recipes

The toggle grid in the interactive viewer (Tab key) lets you enable
and disable individual libraries and functions on the fly. Start with
a broad trace and narrow down:

1. Start an unfiltered trace in the interactive viewer:
   ```
   amigactl> trace start
   ```
2. Press `p` to pause when you see interesting activity.
3. Press Tab to open the toggle grid.
4. Use Left/Right arrows to switch between LIB, FUNC, PROC, and NOISE
   categories. Use Space to toggle individual items.
5. Press Enter to apply the filter.

This is often faster than constructing precise filter arguments
up front.

### Saving traces for offline analysis

In the interactive viewer, press `S` to save the current scrollback
buffer to a file. The saved file contains the raw event text, suitable
for grepping or scripting analysis.

### Two-stage error investigation

A useful pattern for investigating intermittent failures:

1. Start a broad error trace:
   ```
   $ amigactl trace start --errors
   ```
2. Watch for the error events that correspond to the misbehavior.
3. Note the function name and library.
4. Stop the trace (Ctrl-C) and restart with a targeted filter:
   ```
   $ amigactl trace start --lib dos --proc myapp
   ```
5. Now you see the full context of DOS calls around the failure, not
   just the errors.

### Tier switching during a live trace

In the interactive viewer, press `1`, `2`, or `3` to switch between
Basic, Detail, and Verbose tiers without stopping the trace. This
dynamically enables or disables functions at the daemon level:

- **1 (Basic)**: 57 functions -- core diagnostic events (default).
- **2 (Detail)**: 70 functions -- adds resource lifecycle (UnLock,
  Examine, CloseLibrary, etc.).
- **3 (Verbose)**: 73 functions -- adds high-volume events (ExNext,
  OpenFont, CloseFont).

Manual-tier functions (memory, IPC, Read/Write, send/recv) are never
included in any tier and must always be enabled explicitly with
`trace enable`.

See [output-tiers.md](output-tiers.md) for complete tier membership.

### Python API for scripted analysis

For automated trace collection and analysis, use the Python API
directly:

```python
from amigactl import AmigaConnection, FILTER_PRESETS

events = []

def collect(event):
    events.append(event)

with AmigaConnection("192.168.6.200") as conn:
    result = conn.trace_run("MyProgram", collect,
                            preset="file-io")

# Analyze results
for ev in events:
    if ev.get("status") == "E":
        print("FAIL: {}.{} {} -> {}".format(
            ev["lib"], ev["func"], ev["args"], ev["retval"]))

print("Exit code:", result.get("rc"))
```

The `FILTER_PRESETS` dict provides named shortcut configurations:
`"file-io"`, `"lib-load"`, `"network"`, `"ipc"`, `"errors-only"`,
`"memory"`, `"window"`, `"icon"`. Pass the preset name via the
`preset=` parameter; `trace_run()` maps it to the corresponding
wire protocol filters.

See [python-api.md](python-api.md) for the full API reference.


## Quick Reference: Recipe to Command Mapping

| Use Case | CLI Command | Shell Equivalent |
|----------|------------|------------------|
| All file I/O | `trace start --lib dos` | `trace start LIB=dos` |
| Specific file ops | (single function only) | `trace start FUNC=Open,Close,Lock` |
| Failed file access | `trace start --errors --lib dos` | `trace start LIB=dos ERRORS` |
| Memory tracking | `trace enable AllocMem FreeMem AllocVec FreeVec` then `trace run --func AllocMem -- prog` | `trace run FUNC=AllocMem,FreeMem,AllocVec,FreeVec -- prog` |
| IPC analysis | (enable, then single func) | `trace start FUNC=FindPort,GetMsg,PutMsg` |
| Network calls | `trace start --lib bsdsocket` | `trace start LIB=bsdsocket` |
| Program startup | `trace run -- MyProgram` | `trace run -- MyProgram` |
| Library deps | `trace run --func OpenLibrary -- prog` | `trace run FUNC=OpenLibrary,CloseLibrary -- prog` |
| All errors | `trace start --errors` | `trace start ERRORS` |
| Process errors | `trace start --errors --proc myapp` | `trace start ERRORS PROC=myapp` |
| Window activity | `trace start --lib intuition` | `trace start LIB=intuition` |
| Icon access | `trace start --lib icon` | `trace start LIB=icon` |

Note: The CLI `--func` flag accepts only a single function name. For
multi-function filtering, use the shell syntax or the Python API.

---

Cross-references:
- [cli-reference.md](cli-reference.md) -- complete CLI option reference
- [filtering.md](filtering.md) -- filter architecture and syntax details
- [trace-run.md](trace-run.md) -- TRACE RUN documentation
- [output-tiers.md](output-tiers.md) -- tier membership and switching
- [interactive-viewer.md](interactive-viewer.md) -- TUI keybindings and features
- [reading-output.md](reading-output.md) -- how to read event output
- [traced-functions.md](traced-functions.md) -- per-function argument and return value reference
