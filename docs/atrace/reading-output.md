# Reading Trace Output

This document explains how to interpret the formatted event lines that
atrace produces. Every traced library call generates a single line of
output containing seven tab-separated fields. Understanding what each
field means, how return values are classified, and what the various
formatting conventions indicate is essential for effective debugging.

For the raw binary layout of events in the ring buffer, see
[event-format.md](event-format.md). For per-function details
(which arguments are captured, error classification, tier assignment),
see [traced-functions.md](traced-functions.md).


## Event Line Format

Each event is transmitted as a single line with seven tab-separated
fields:

```
SEQ \t TIME \t LIB.FUNC \t TASK \t ARGS \t RETVAL \t STATUS
```

A concrete example:

```
42	10:30:15.123456	dos.Open	[3] Shell Process	"RAM:test",Write	0x1c16daf0	O
```

| Position | Field | Description |
|----------|-------|-------------|
| 1 | SEQ | Monotonic sequence number |
| 2 | TIME | Timestamp (HH:MM:SS.uuuuuu) |
| 3 | LIB.FUNC | Library and function name, dot-separated |
| 4 | TASK | Calling task or process identifier |
| 5 | ARGS | Formatted function arguments |
| 6 | RETVAL | Formatted return value (may include IoErr) |
| 7 | STATUS | Single character: `O`, `E`, or `-` |

The daemon formats these fields in `trace_format_event()` and sends
them to connected clients as DATA chunks. The Python client parses
them in `_parse_trace_event()`, which splits on tabs and produces a
dictionary with keys `seq`, `time`, `lib`, `func`, `task`, `args`,
`retval`, and `status`.


## Sequence Number (SEQ)

The first field is a monotonically increasing 32-bit counter assigned
from `anchor->event_sequence`. Every traced library call receives a
unique sequence number, starting from whatever value was current when
the trace session began (stale events from before the session are
drained at startup).

Key properties:

- **Ordering.** Events are numbered in the order their ring buffer
  slots were reserved, which corresponds to the order the library calls
  entered the stub code. This is not necessarily the order the calls
  completed -- a blocking function that entered the stub first may
  complete after a non-blocking function that entered second.

- **Gap detection.** A gap in sequence numbers indicates events
  discarded by ring buffer overflow. The ring buffer uses circular
  overwrite, discarding the oldest unread events when full. The
  overflow count is reported in the session-end summary and in
  TRACE STATUS output.

- **Wrapping.** The counter wraps at 2^32 (approximately 4.3 billion
  events), which is far beyond any practical tracing session.


## Timestamps

The TIME field shows wall-clock time derived from the Amiga's EClock
hardware counter. The format is `HH:MM:SS.uuuuuu` -- hours, minutes,
seconds, and microseconds, using a 24-hour clock.

### How Timestamps Are Computed

The daemon captures a wall-clock epoch when the trace session starts
(via the AmigaOS `DateStamp()` function, which provides time since
midnight in ticks). The EClock baseline is captured lazily from the
first event that arrives with `FLAG_HAS_ECLOCK` set. For each
subsequent event, the daemon computes:

1. A 48-bit subtraction: event EClock minus baseline EClock.
2. Conversion of elapsed ticks to seconds and microseconds using the
   EClock frequency stored in `anchor->eclock_freq`.
3. Addition of the elapsed time to the wall-clock epoch.

The EClock frequency depends on the Amiga's video standard: 709,379 Hz
for PAL systems, 715,909 Hz for NTSC systems. The actual frequency is
read from `ReadEClock()`'s return value during `atrace_loader`
initialization.

### Precision and Limitations

Each event carries a 48-bit EClock value (32-bit `eclock_lo` plus
16-bit `eclock_hi`). The daemon uses signed WORD subtraction for the
`eclock_hi` difference, giving an effective range of approximately
2,296 days (~6.3 years) at PAL frequency before overflow. The
microsecond digits in the output are
genuine hardware-derived values, not interpolated. The conversion uses
integer arithmetic:

```
elapsed_us = (remainder * 1000) / (eclock_freq / 1000)
```

This avoids 32-bit overflow while maintaining microsecond precision.

The timestamp reflects when the stub reserved the ring buffer slot
(pre-call), not when the function returned. For blocking functions
like `RunCommand` or `WaitSelect`, the actual call duration is the
difference between this event's timestamp and the next event from
the same task.

### Fallback Timestamps

If EClock data is unavailable (`eclock_freq` is zero), the daemon falls back to per-batch `DateStamp()` timestamps
with millisecond resolution in the format `HH:MM:SS.mmm`. All events
consumed in the same polling cycle share the same fallback timestamp.
Events with EClock timestamps always use their own per-event time.

### Display Modes in the TUI

The interactive TUI viewer (via `amigactl shell`) supports three
timestamp display modes, cycled with the `t` key:

- **Absolute** (default): wall-clock time as described above.
- **Relative**: elapsed time since the trace session started.
- **Delta**: time difference from the previous displayed event.


## Function Identification (LIB.FUNC)

The third field identifies the traced function in `library.Function`
format. The library name is the short name (without the `.library`
suffix), and the function name is the AmigaOS API name exactly as
documented in the NDK:

| Library Name | Examples |
|--------------|----------|
| exec | `exec.OpenLibrary`, `exec.AllocMem`, `exec.FindTask` |
| dos | `dos.Open`, `dos.Lock`, `dos.Execute` |
| intuition | `intuition.OpenWindow`, `intuition.CloseScreen` |
| bsdsocket | `bsdsocket.socket`, `bsdsocket.connect` |
| graphics | `graphics.OpenFont` |
| icon | `icon.GetDiskObject`, `icon.FindToolType` |
| workbench | `workbench.AddAppIconA` |

The daemon resolves functions by matching the event's `lib_id` and
`lvo_offset` against its internal `func_table[]`. If a function cannot
be resolved (which should not happen for properly installed patches),
both library and function display as `?`.

In the TUI's narrow terminal modes (below 80 columns), library names
are abbreviated to their first letter (`d.Open` instead of `dos.Open`)
to conserve horizontal space.


## Task Identification (TASK)

The fourth field identifies which AmigaOS task or process made the
library call. The format depends on the calling context:

- **CLI processes**: `[N] command_name` -- where N is the CLI number
  (from `pr_TaskNum`) and `command_name` is the basename of the
  currently executing command, extracted from the CLI structure's
  `cli_CommandName` BSTR. Path prefixes are stripped: `SYS:C/Dir`
  becomes `Dir`.

- **Non-CLI processes**: The task's `tc_Node.ln_Name` string. This
  includes background processes, Workbench-launched programs, and
  system tasks. No CLI number prefix is shown.

- **Fallback**: `<task 0xNNNNNNNN>` -- when the task pointer cannot
  be resolved to a name. This happens when the process has already
  exited and is not in the history cache.

### Name Resolution Mechanism

The daemon maintains a task name cache of 64 entries, refreshed every
~400ms (20 poll cycles) by walking the system task lists under
`Forbid()`. For CLI processes, `resolve_cli_name()` reads the command
name from `pr_CLI->cli_CommandName` and extracts the basename.

A supplementary task history cache (32 entries, ring buffer) records
the last-known name for each task pointer. This allows events from
short-lived processes (e.g., commands launched via `SystemTags`) to be
displayed correctly even after the process exits.

When a cache miss occurs, the daemon attempts a direct dereference of
the task pointer under `Forbid()`. If even that yields no name, the
embedded `task_name` field from the event itself (captured by the stub
from `tc_Node.ln_Name` or `cli_CommandName`) is used as a final
fallback. The embedded name is limited to 21 characters and may
contain the raw process name (e.g., "Background CLI") rather than the
CLI command name.


## Arguments (ARGS)

The fifth field contains the function's arguments, formatted according
to per-function rules in the daemon's `format_args()` function. The
formatting is designed to be human-readable rather than
machine-parseable.

### String Arguments

Functions that take C string arguments display them in double quotes.
A truncation indicator (`...`) is appended when the captured string
filled all 63 available bytes in the event's `string_data` field,
indicating the original string was likely longer:

```
"LIBS:mathieeesingbas.library..."
```

A NULL string pointer is handled gracefully (the stub stores an empty
string), and the daemon falls back to displaying the raw argument as
a hex pointer.

### Dual-String Arguments

Two functions capture two strings, split across the 64-byte
`string_data` buffer (31 characters plus NUL terminator, stored in
32 bytes each):

- **Rename**: `"oldname" -> "newname"`
- **MakeLink**: `"linkname" -> "destpath" soft` or `"linkname" -> "destpath" hard`

Each half has independent truncation detection at 31 characters.

### Argument Formatting by Library

The following subsections summarize the formatting conventions. Not
every function is listed -- only those with noteworthy formatting.

#### exec.library

| Function | Format | Example |
|----------|--------|---------|
| OpenLibrary | `"name",vN` | `"dos.library",v37` |
| OpenDevice | `"name",unit=N[,flags=0xN]` | `"timer.device",unit=0` |
| AllocMem / AllocVec | `size,flags` | `65536,MEMF_PUBLIC\|MEMF_CLEAR` |
| FreeMem | `0xaddr,size` | `0x07c42a00,1024` |
| FreeVec | `0xaddr` | `0x07c42a00` |
| FindTask | `"name"` or `NULL (self)` | `NULL (self)` |
| DoIO / SendIO / WaitIO / AbortIO / CheckIO | `"device" CMD N` or `io=0xaddr` | `"timer.device" CMD 9` |
| ObtainSemaphore / ReleaseSemaphore | `"name"` or `sem=0xaddr` | `"LayerInfo"` |
| GetMsg / PutMsg | `"portname"` or `port=0xaddr` | `"AREXX"` |
| Wait | `0xNNNNNNNN (CTRL_C\|CTRL_D)` | `0x00005000 (CTRL_C\|CTRL_D)` |
| Signal | `"taskname",0xNNNNNNNN (CTRL_C)` | `"[3] Shell Process",0x00001000 (CTRL_C)` |
| AllocSignal / FreeSignal | `sig=N` | `sig=-1` |
| CloseLibrary | `"name"` or `lib=0xaddr` | `"dos.library"` |
| AddPort / DeleteMsgPort / WaitPort | `"name"` or `port=0xaddr` | `"AMITCP"` |
| ReplyMsg | `msg=0xaddr` | `msg=0x07c10040` |
| CreateMsgPort | (no arguments) | |

Memory flag names are decoded from the `MEMF_*` constants: `MEMF_ANY`
(flags=0), `MEMF_PUBLIC`, `MEMF_CHIP`, `MEMF_FAST`, `MEMF_LOCAL`,
`MEMF_KICK`, `MEMF_24BITDMA`, `MEMF_CLEAR`, `MEMF_LARGEST`,
`MEMF_REVERSE`, `MEMF_TOTAL`. Unknown bits are shown as hex. Multiple
flags are separated by `|`.

Signal set formatting shows the raw hex value followed by recognized
signal names in parentheses: `CTRL_C` (bit 12), `CTRL_D` (bit 13),
`CTRL_E` (bit 14), `CTRL_F` (bit 15).

For I/O request functions (DoIO, SendIO, etc.), when the stub captures
an indirect device name via `DEREF_IOREQUEST`, the output shows the
device name and the `io_Command` value. When the indirect capture
fails (NULL pointer in the chain), the raw I/O request pointer is
shown instead.

#### dos.library

| Function | Format | Example |
|----------|--------|---------|
| Open | `"path",mode` | `"RAM:test",Write` |
| Close | `"path"` or `fh=0xaddr` | `"S:Startup-Sequence"` |
| Lock | `"path",type` | `"LIBS:",Shared` |
| DeleteFile | `"path"` | `"T:tempfile"` |
| Execute | `"command"[,in=0xN][,out=0xN]` | `"Dir SYS:"` |
| GetVar | `"name",scope` | `"Workbench",GLOBAL` |
| SetVar / DeleteVar | `"name",scope` | `"RC",ANY` |
| FindVar | `"name",type` | `"RC",LV_VAR` |
| LoadSeg / NewLoadSeg | `"path"` | `"C:Dir"` |
| CreateDir | `"path"` | `"RAM:NewDir"` |
| SystemTagList | `"command"` | `"Dir SYS: ALL"` |
| CurrentDir | `"path"`, `"volume:?"`, or `lock=0xN` | `"RAM:T"` |
| Read / Write | `fh=0xaddr,len=N` | `fh=0x1c16daf0,len=4096` |
| Seek | `fh=0xaddr,pos=N,mode` | `fh=0x1c16daf0,pos=0,OFFSET_BEGINNING` |
| Examine / ExNext | `lock=0xaddr,fib=0xaddr` | `lock=0x07c00040,fib=0x07c42a00` |
| UnLock | `"path"` or `lock=0xaddr` | `"LIBS:"` |
| RunCommand | `"prog",stack=N,len` or `seg=0xN,...` | `"Dir",stack=4096,12` |
| SetProtection | `"path",rwed` | `"RAM:test",----rwed` |
| UnLoadSeg | `"prog"` or `seg=0xaddr` | `"C:Dir"` |

Access modes for Open: `Read` (MODE_OLDFILE, 1005), `Write`
(MODE_NEWFILE, 1006), `Read/Write` (MODE_READWRITE, 1004).

Lock types: `Shared` (ACCESS_READ, -2), `Exclusive` (ACCESS_WRITE, -1).

Seek offset modes: `OFFSET_BEGINNING` (-1), `OFFSET_CURRENT` (0),
`OFFSET_END` (1).

GetVar/SetVar/DeleteVar scope is decoded from `GVF_*` flags:
`GLOBAL` (GVF_GLOBAL_ONLY, 0x100), `LOCAL` (GVF_LOCAL_ONLY, 0x200),
`ANY` (neither flag).

FindVar type is decoded from `LV_*` constants: `LV_VAR` (0),
`LV_ALIAS` (1).

Protection bits for SetProtection are formatted as an `hsparwed`
string where each letter appears if the corresponding permission is
active. The RWED bits (3-0) use inverted logic per AmigaOS convention:
bit clear means the action is allowed. For example, `----rwed` means
all permissions granted, `----r---` means only read is allowed.

The daemon maintains internal caches (lock-to-path, file-handle-to-path,
seglist-to-name) that allow Close, UnLock, CurrentDir, FreeDiskObject,
RunCommand, and UnLoadSeg to display human-readable paths instead of
raw hex addresses. If a handle was opened before tracing started, the
cache will not contain it, and the raw hex address is shown instead.

For CurrentDir, when the lock argument is not in the lock cache, the
stub's `DEREF_LOCK_VOLUME` indirect capture provides the volume name
from the lock's `fl_Volume` field (e.g., `"RAM:"`). A `?` suffix is
appended to indicate that only the volume is known, not the
subdirectory path: `"RAM:?"`.

#### intuition.library

| Function | Format | Example |
|----------|--------|---------|
| OpenWindow / OpenWindowTagList | `"title"` or `nw=0xaddr[,tags=0xaddr]` | `"CON:0/0/640/200/Shell"` |
| CloseWindow / ActivateWindow / WindowToFront / WindowToBack | `"title"` or `win=0xaddr` | `"Shell"` |
| OpenScreen / OpenScreenTagList | `"title"` or `ns=0xaddr[,tags=0xaddr]` | `"Workbench Screen"` |
| CloseScreen | `"title"` or `scr=0xaddr` | `"Workbench Screen"` |
| ModifyIDCMP | `win=0xaddr,flags` | `win=0x07c42a00,CLOSEWINDOW\|RAWKEY\|VANILLAKEY` |
| LockPubScreen | `"name"` or `NULL (default)` | `NULL (default)` |
| UnlockPubScreen | `"name"` or `screen=0xaddr` | `"Workbench"` |

Window and screen titles are captured via indirect dereferencing of
the appropriate struct fields (`DEREF_NW_TITLE`, `DEREF_WIN_TITLE`,
`DEREF_NS_TITLE`, `DEREF_SCR_TITLE`). If the title pointer is NULL,
the raw struct address is shown.

IDCMP flag names are decoded from the standard NDK constants:
`SIZEVERIFY`, `NEWSIZE`, `REFRESHWINDOW`, `MOUSEBUTTONS`,
`MOUSEMOVE`, `GADGETDOWN`, `GADGETUP`, `REQSET`, `MENUPICK`,
`CLOSEWINDOW`, `RAWKEY`, `VANILLAKEY`, `INTUITICKS`, `CHANGEWINDOW`,
and others. Multiple flags are separated by `|`.

#### bsdsocket.library

| Function | Format | Example |
|----------|--------|---------|
| socket | `domain,type,proto=N` | `AF_INET,SOCK_STREAM,proto=0` |
| bind / connect | `fd=N,IP:port` or `fd=N,addr=0xaddr` | `fd=0,192.168.1.5:8080` |
| listen | `fd=N,backlog=N` | `fd=0,backlog=5` |
| accept | `fd=N` | `fd=0` |
| send / recv | `fd=N,len=N[,flags]` | `fd=0,len=1024,MSG_OOB` |
| sendto | `fd=N[,IP:port],len=N[,flags]` | `fd=0,192.168.1.5:9000,len=512` |
| recvfrom | `fd=N,len=N[,flags]` | `fd=0,len=1024` |
| shutdown | `fd=N,how` | `fd=0,SHUT_RDWR` |
| setsockopt / getsockopt | `fd=N,level=N,opt=N` | `fd=0,level=65535,opt=4` |
| IoctlSocket | `fd=N,req=0xN` | `fd=0,req=0x8004667e` |
| CloseSocket | `fd=N` | `fd=0` |
| WaitSelect | `nfds=N,sigs=0xN (signals)` | `nfds=2,sigs=0x00001000 (CTRL_C)` |

Socket domain names: `AF_UNSPEC` (0), `AF_INET` (2). Unknown domains
show as `domain=N`.

Socket types: `SOCK_STREAM` (1), `SOCK_DGRAM` (2), `SOCK_RAW` (3).
Unknown types show as `type=N`.

Shutdown modes: `SHUT_RD` (0), `SHUT_WR` (1), `SHUT_RDWR` (2).

For `bind` and `connect`, the stub captures the `sockaddr_in` contents
from the caller's address argument. When the capture succeeds and the
address family is `AF_INET`, the IP and port are shown inline
(e.g., `fd=0,192.168.1.5:8080`). `INADDR_ANY` is displayed as `*`
(e.g., `fd=0,*:8080`). If the capture fails (non-AF_INET family or
NULL pointer), the raw address pointer is shown as a fallback.

For `accept`, the arguments show only the file descriptor. On success,
the daemon appends the accepted peer's address to the return value:
`N [from 192.168.1.5:4321]`. The peer address is captured by the
post-call suffix block from the `sockaddr_in` output parameter.

For `sendto`, the destination `sockaddr_in` is captured and shown
before the length when available (e.g., `fd=0,192.168.1.5:9000,len=512`).

Message flags for `send`, `sendto`, `recv`, and `recvfrom` are decoded
symbolically when non-zero: `MSG_OOB` (0x01), `MSG_PEEK` (0x02),
`MSG_DONTROUTE` (0x04), `MSG_WAITALL` (0x40), `MSG_DONTWAIT` (0x80).
Multiple flags are separated by `|`. When flags are zero, no flags
field is shown.

#### graphics.library

| Function | Format | Example |
|----------|--------|---------|
| OpenFont | `"fontname",size` | `"topaz.font",8` |
| CloseFont | `font=0xaddr` | `font=0x07c42a00` |

OpenFont uses `DEREF_TEXTATTR` to capture the font name from the
`TextAttr` structure (offset 0) and the font size (`ta_YSize`) from
offset 4.

#### icon.library

| Function | Format | Example |
|----------|--------|---------|
| GetDiskObject | `"name"` | `"SYS:Utilities/Clock"` |
| PutDiskObject | `"name",obj=0xaddr` | `"RAM:test",obj=0x07c42a00` |
| FreeDiskObject | `"name"` or `obj=0xaddr` | `"SYS:Utilities/Clock"` |
| FindToolType | `"typename"` | `"WINDOW"` |
| MatchToolValue | `str=0xaddr,val=0xaddr` | `str=0x07c42a00,val=0x07c42a04` |

FreeDiskObject uses the daemon's disk object cache (populated by
GetDiskObject) to resolve object pointers back to names.

#### workbench.library

| Function | Format | Example |
|----------|--------|---------|
| AddAppIconA | `id=N,"text"` | `id=0,"MyApp"` |
| RemoveAppIcon | `icon=0xaddr` | `icon=0x07c42a00` |
| AddAppWindowA | `id=N,win=0xaddr` | `id=0,win=0x07c42a00` |
| RemoveAppWindow | `win=0xaddr` | `win=0x07c42a00` |
| AddAppMenuItemA | `id=N,"text"` | `id=0,"Quit"` |
| RemoveAppMenuItem | `item=0xaddr` | `item=0x07c42a00` |

### Indirect Name Deref Types

Several functions take struct pointers rather than strings. The stub
dereferences these pointers to extract a human-readable name. The
deref types and what they produce:

| Type | What It Captures | Used By |
|------|------------------|---------|
| DEREF_LN_NAME | `struct->ln_Name` (offset 10) | ObtainSemaphore, ReleaseSemaphore, GetMsg, PutMsg, CloseLibrary, DeleteMsgPort, AddPort, WaitPort |
| DEREF_IOREQUEST | `IORequest->io_Device->ln_Name` + `io_Command` | DoIO, SendIO, WaitIO, AbortIO, CheckIO, CloseDevice |
| DEREF_TEXTATTR | `TextAttr->ta_Name` + `ta_YSize` | OpenFont |
| DEREF_LOCK_VOLUME | Lock BPTR -> `fl_Volume` -> `dol_Name` BSTR + `:` | CurrentDir |
| DEREF_NW_TITLE | `NewWindow->Title` (offset 26) | OpenWindow, OpenWindowTagList |
| DEREF_WIN_TITLE | `Window->Title` (offset 32) | CloseWindow, ActivateWindow, WindowToFront, WindowToBack |
| DEREF_NS_TITLE | `NewScreen->DefaultTitle` (offset 20) | OpenScreen, OpenScreenTagList |
| DEREF_SCR_TITLE | `Screen->Title` (offset 22) | CloseScreen |

When the deref fails (NULL pointer at any level of indirection), the
stub stores an empty string and the daemon falls back to showing the
raw struct address.


## Return Values (RETVAL)

The sixth field shows the function's return value, formatted according
to the function's `result_type` metadata. There are 12 return value
types, each with distinct display conventions:

### RET_PTR -- Pointer Return

Used by: `OpenLibrary`, `AllocMem`, `AllocVec`, `OpenWindow`,
`OpenScreen`, `LoadSeg`, `NewLoadSeg`, `Open`, `CheckIO`, `CreateMsgPort`,
`OpenFont`, `GetDiskObject`, `FindToolType`, `LockPubScreen`,
`OpenWindowTagList`, `OpenScreenTagList`, `AddAppIconA`,
`AddAppWindowA`, `AddAppMenuItemA`, `OpenWorkBench`, `Wait`.

- Non-zero: `0x07c42a00` (hex address) -- status `O`
- Zero: `NULL` -- status `E`

### RET_BOOL_DOS -- DOS Boolean

Used by: `Close`, `DeleteFile`, `Rename`, `SetVar`, `DeleteVar`,
`AddDosEntry`, `MakeLink`, `Examine`, `ExNext`, `SetProtection`,
`CloseWorkBench`, `PutDiskObject`, `MatchToolValue`,
`RemoveAppIcon`, `RemoveAppWindow`, `RemoveAppMenuItem`.

- Non-zero (any value, including DOSTRUE=-1): `OK` -- status `O`
- Zero: `FAIL` -- status `E`

### RET_NZERO_ERR -- Non-Zero Error Code

Used by: `OpenDevice`, `DoIO`, `WaitIO`, `AbortIO`.

- Zero: `OK` -- status `O`
- Non-zero: `err=N` (signed decimal) -- status `E`

### RET_VOID -- Void Function

Used by: `PutMsg`, `ObtainSemaphore`, `ReleaseSemaphore`, `FreeMem`,
`FreeVec`, `SendIO`, `Signal`, `FreeSignal`, `DeleteMsgPort`,
`CloseLibrary`, `CloseDevice`, `ReplyMsg`, `AddPort`, `CloseWindow`,
`CloseScreen`, `ActivateWindow`, `WindowToFront`, `WindowToBack`,
`ModifyIDCMP`, `UnlockPubScreen`, `FreeDiskObject`, `CloseFont`,
`UnLock`, `UnLoadSeg`.

- Always: `(void)` -- status `-`

### RET_MSG_PTR -- Message Pointer

Used by: `GetMsg`, `WaitPort`.

- Non-zero: `0x07c42a00` (hex address) -- status `O`
- Zero: `(empty)` -- status `-`

Note the distinction from RET_PTR: a NULL return from `GetMsg` is
normal (message port empty, not an error), so the status is `-`
(neutral) rather than `E`.

### RET_RC -- Return Code

Used by: `RunCommand`, `SystemTagList`.

- Always shown as: `rc=N` (signed decimal)
- Zero: status `O`
- Non-zero: status `E`

### RET_LOCK -- Lock BPTR

Used by: `Lock`, `CreateDir`.

- Non-zero: `0x07c42a00` (hex BPTR value) -- status `O`
- Zero: `NULL` -- status `E`

Functionally identical to RET_PTR in display, but semantically
distinct (the value is a BPTR, not a direct pointer).

### RET_LEN -- Byte Count

Used by: `GetVar`.

- -1: `-1` -- status `E`
- >= 0: decimal count -- status `O`

### RET_IO_LEN -- I/O Byte Count

Used by: `Read`, `Write`, `Seek`, `socket`, `bind`, `listen`,
`accept`, `connect`, `send`, `sendto`, `recv`, `recvfrom`,
`shutdown`, `setsockopt`, `getsockopt`, `IoctlSocket`, `CloseSocket`,
`WaitSelect`, `AllocSignal`.

- -1: `-1` -- status `E`
- >= 0: decimal count -- status `O`

For `Read`, a return of 0 indicates end-of-file (not an error).

### RET_OLD_LOCK -- Old Lock from CurrentDir

Used by: `CurrentDir`.

- Zero: `(none)` -- status `-`
- Non-zero: `"path"` (if in lock cache) or `0x07c42a00` -- status `-`

The status is always neutral because `CurrentDir` always succeeds.
The return value is the previous current directory lock, which is
informational.

### RET_PTR_OPAQUE -- Opaque Pointer

Used by: `FindPort`, `FindResident`, `FindSemaphore`, `FindTask`,
`OpenResource`, `FindVar`.

- Non-zero: `OK` -- status `O`
- Zero: `NULL` -- status `E`

Unlike RET_PTR, this does not show the hex address -- the pointer
value itself is not useful to the user.

### RET_EXECUTE -- Execute Return

Used by: `Execute`.

- DOSTRUE (-1): `OK` -- status `-`
- Zero: `rc=0` -- status `-`
- Other non-zero: `rc=N` -- status `-`

The status is always neutral because `Execute()`'s return value is
ambiguous: it conflates the DOS boolean convention (DOSTRUE = -1 for
success) with shell return codes (0 = success in shell convention).
Classifying any particular return value as an error would be
misleading.


## Status Indicator

The seventh field is a single character that classifies the outcome
of the function call:

| Status | Meaning | CLI Color |
|--------|---------|-----------|
| `O` | Success / OK | Green |
| `E` | Error / failure | Red |
| `-` | Neutral (void, informational, or ambiguous) | Default |

The classification is determined by each function's `error_check` type
in the daemon's `func_table[]`. These types control which return
values are considered errors for the purpose of the `--errors` filter:

| Error Check Type | Error Condition | Used By |
|------------------|-----------------|---------|
| ERR_CHECK_NULL | retval == 0 | Most functions |
| ERR_CHECK_NZERO | retval != 0 | OpenDevice, DoIO, WaitIO |
| ERR_CHECK_VOID | Never (void function) | PutMsg, FreeMem, CloseWindow, etc. |
| ERR_CHECK_ANY | Always shown in ERRORS mode | Wait, CheckIO, OpenWorkBench, CloseWorkBench, AbortIO, WaitPort |
| ERR_CHECK_NONE | Never an error | GetMsg, Execute, MatchToolValue |
| ERR_CHECK_RC | retval != 0 | RunCommand, SystemTagList |
| ERR_CHECK_NEGATIVE | (LONG)retval < 0 | GetVar |
| ERR_CHECK_NEG1 | retval == 0xFFFFFFFF | socket, bind, send, recv, Read, Write, Seek, AllocSignal, etc. |

The `--errors` filter uses `error_check` to decide which events to
suppress. Void functions (`ERR_CHECK_VOID`) are always suppressed in
errors-only mode. Functions with `ERR_CHECK_NONE` are never shown in
errors-only mode because their "failure" returns are normal expected
behavior (e.g., `GetMsg` returning NULL means the port is empty).


## IoErr Capture

For dos.library functions, when the return value indicates failure,
the daemon appends the IoErr code and its human-readable name to the
RETVAL field:

```
NULL (205, object not found)
FAIL (222, file is protected from deletion)
```

### When IoErr Appears

IoErr information is displayed only when all of the following
conditions are met:

1. The function belongs to dos.library (`lib_id == LIB_DOS`).
2. The return value indicates an error (`status == 'E'`).
3. The event is complete (`ev->valid == 1`).
4. The `FLAG_HAS_IOERR` flag is set in the event.
5. The `ioerr` value is non-zero.

The `valid == 1` check is important: events from blocking functions
may be consumed while the function is still executing (`valid == 2`),
in which case the IoErr field may contain stale data. The daemon
suppresses IoErr display for such events.

### Error Code Names

The daemon decodes IoErr values to standard AmigaOS error names:

| Code | Name |
|------|------|
| 103 | insufficient free store |
| 105 | task table full |
| 114 | bad template |
| 116 | required argument missing |
| 117 | value after keyword missing |
| 120 | argument line invalid or too long |
| 121 | file is not an object module |
| 122 | invalid resident library |
| 202 | object in use |
| 203 | object already exists |
| 204 | directory not found |
| 205 | object not found |
| 206 | invalid window description |
| 207 | object too large |
| 208 | action not known |
| 209 | packet request type unknown |
| 210 | object name invalid |
| 211 | invalid lock |
| 212 | object not of required type |
| 213 | disk not validated |
| 214 | disk write-protected |
| 215 | rename across devices |
| 216 | directory not empty |
| 218 | device not mounted |
| 219 | seek error |
| 220 | comment too big |
| 221 | disk full |
| 222 | file is protected from deletion |
| 223 | file is write protected |
| 224 | file is read protected |
| 225 | not a DOS disk |
| 226 | no disk in drive |
| 232 | no more entries |
| 233 | buffer overflow |

Unknown error codes are displayed as `(err N)` without a name.

### UBYTE Truncation

The `ioerr` field in the event structure is a single `UBYTE` (1 byte),
limiting it to values 0--255. All standard AmigaOS DOS error codes
(103--233) fit within this range. However, any custom or third-party
error codes exceeding 255 are truncated to their low byte. This is a
deliberate space optimization within the 128-byte fixed-size event
structure. See [event-format.md](event-format.md) for the full
binary layout.


## In-Progress Events

Events from blocking functions may be consumed by the daemon before
the function returns. The stub sets `valid=2` in the ring buffer slot
before calling the original function, allowing the consumer to advance
past slots that may block indefinitely.

When the daemon encounters a `valid=2` event, it waits up to 3
consecutive polls (approximately 60ms at the default 20ms poll
interval) for the post-call handler to complete and set `valid=1`.
If the function is still executing after this patience interval, the
event is consumed as-is.

For in-progress events:

- The `retval` field may not reflect the actual return value (it could
  be zero from `MEMF_CLEAR` initialization or stale from a prior ring
  buffer occupant).
- IoErr display is suppressed (the `valid == 1` guard prevents it).
- The status indicator may not be accurate.

This is most commonly visible with `RunCommand`, `Execute`,
`SystemTagList`, and `WaitSelect`, which can block for seconds or
longer. The event appears in the output when the function is called,
with the return value populated by whatever was in the slot at the
time of consumption.


## Noise Filtering at Basic Tier

At the default Basic output tier, the daemon suppresses two categories
of high-volume, low-information events before they reach any client:

### Successful OpenLibrary with version 0

AmigaOS internally calls `OpenLibrary("name", 0)` frequently during
CLI startup and library interdependency resolution. These successful
opens (retval != 0) with version 0 are noise. Failed opens of any
version are always shown because they have diagnostic value.

### Successful Lock calls

`Lock()` is called frequently during AmigaOS path resolution. At
Basic tier, successful Lock calls (retval != 0) are suppressed. Failed
Locks (retval == 0) always pass through.

Both suppression rules are applied on the daemon side, not the client
side, to reduce network bandwidth and ring buffer consumer overhead.
Lock values from suppressed successful Lock calls are still added to
the lock-to-path cache, so subsequent CurrentDir and UnLock events can
resolve their arguments even when the corresponding Lock was
suppressed.

At Detail tier (tier 2) and above, these suppressions are disabled and
all events pass through.

### Self-Filtering

Events from the daemon's own task are always silently dropped,
regardless of tier. This prevents feedback loops where the daemon's
own library calls (for formatting, network I/O, etc.) would generate
trace events.


## Annotated Example Output

The following shows realistic trace output from running `List SYS:C`
via `amigactl trace run -- List SYS:C`, annotated with explanations:

```
SEQ          TIME  FUNCTION                     TASK                 ARGS                                     RESULT
150   09:42:31.004821  dos.Open        [4] List        "SYS:C",Read                             0x1c16daf0    O
151   09:42:31.005103  dos.Lock        [4] List        "SYS:C",Shared                           NULL          E
```

- Event 150: `dos.Open` called by CLI process 4 (the List command),
  opening `SYS:C` for reading. Returned file handle `0x1c16daf0` --
  success (`O`).

- Event 151: `dos.Lock` called by the same process, attempting a
  shared lock on `SYS:C`. If the Lock at Basic tier was a failure,
  it would appear with `NULL` return and status `E`. (At Basic tier,
  successful Locks are suppressed.)

```
152   09:42:31.005987  exec.OpenLibrary  [4] List    "locale.library",v38           0x07906734    O
```

- Event 152: `exec.OpenLibrary` with version 38 (not version 0, so
  not suppressed at Basic tier). Successfully opened locale.library.

```
153   09:42:31.009214  dos.Open        [4] List        "ENV:Sys/locale.prefs",Read              NULL (205, object not found)    E
```

- Event 153: An `Open` that failed -- returned NULL with IoErr 205
  ("object not found"). The IoErr is appended to the return value
  because this is a dos.library function that returned an error.

```
154   09:42:31.015822  dos.Close       [4] List        "SYS:C"                                  OK            O
```

- Event 154: `Close` call. The daemon resolved the file handle back to
  the path `"SYS:C"` using its internal handle-to-path cache
  (populated when Open succeeded at event 150).

```
155   09:42:31.016001  exec.CloseLibrary [4] List    "locale.library"                         (void)          -
```

- Event 155: `CloseLibrary` is a void function. The indirect
  `DEREF_LN_NAME` captured the library name from the Library struct's
  `ln_Name` field. Status is `-` (neutral) because void functions
  have no success/failure concept.
