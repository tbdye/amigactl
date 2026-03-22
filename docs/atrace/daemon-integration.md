# Daemon Integration

The amigactld daemon is the bridge between atrace's in-kernel ring buffer
and remote clients. It discovers the atrace anchor via a named Exec
semaphore, polls the ring buffer for events, resolves task names,
formats events into human-readable text, applies per-client filters, and
streams the result over TCP as DATA/END chunks. This document covers the
daemon-side trace module: discovery, auto-loading, the polling loop, task
name resolution, value caches, event formatting, noise filtering, tier
management, and the TRACE command set.

For the ring buffer's memory layout and producer/consumer protocol, see
[ring-buffer.md](ring-buffer.md). For the 128-byte event structure, see
[event-format.md](event-format.md). For the overall system architecture,
see [architecture.md](architecture.md).


## Discovery and Auto-Loading

The daemon locates atrace through the named Exec semaphore
`"atrace_patches"` (`ATRACE_SEM_NAME` in `atrace/atrace.h`). The
semaphore's address is the start of the `struct atrace_anchor`, which
contains pointers to the ring buffer and patch array.

### Startup Discovery (trace_init)

At daemon startup, `trace_init()` calls `FindSemaphore()` under
Forbid/Permit. If the semaphore exists, it validates the magic field
(`anchor->magic == 0x41545243`, the ASCII bytes `'ATRC'`). On success it
caches two pointers in module globals:

- `g_anchor` -- the anchor structure itself (104 bytes).
- `g_ring_entries` -- a pointer to the first event slot in the ring
  buffer, computed as `(UBYTE *)g_anchor->ring + sizeof(struct
  atrace_ringbuf)` (the 16-byte header is skipped).

If the semaphore is not found at startup, both pointers remain NULL. This
is not an error -- atrace can be loaded at any time.

### Lazy Re-discovery (trace_discover)

Each time a client issues `TRACE START`, `TRACE STATUS`, or `TRACE RUN`,
the daemon calls `trace_discover()` before proceeding. If `g_anchor` is
already set, it returns immediately. Otherwise it repeats the
FindSemaphore/magic validation sequence.

On a successful discovery, `trace_discover()` also validates the
`noise_func_names[]` array against the loaded patch table. Every name in
the noise table is looked up with `find_patch_index_by_name()`. A warning
is printed for any name that does not match a loaded patch, catching
mismatches between the loader and daemon noise lists at runtime rather
than silently misidentifying functions.

### Auto-Loading (trace_auto_load)

If `trace_discover()` fails (atrace is not resident), the daemon
attempts to load it automatically by running `C:atrace_loader` via
`SystemTags()`:

```c
rc = SystemTags((STRPTR)"C:atrace_loader",
                SYS_Output, fh_nil,
                SYS_Input, 0,
                TAG_DONE);
```

The output is discarded to `NIL:`. If the loader returns rc=0, the
daemon retries `trace_discover()` to pick up the newly created semaphore.
A diagnostic message (`"Auto-loading C:atrace_loader"`) is printed to the
daemon's console.

Auto-loading is triggered by TRACE START, TRACE STATUS, TRACE RUN, TRACE
ENABLE, and TRACE DISABLE. It is a synchronous, blocking operation -- the
daemon does not process other client commands until the loader finishes.


## Session Lifecycle

A trace session progresses through well-defined phases.

### TRACE START

1. Mutual exclusion: reject if a TAIL session or another TRACE session
   is already active on the same connection.
2. Auto-load atrace if not resident.
3. Verify `global_enable != 0` (atrace must be enabled).
4. Parse filter arguments from the command line via `parse_filters()`.
5. Clear all value caches (lock, file handle, DiskObject, segment).
6. Reset `g_current_tier` to 1 (Basic).
7. Set `c->trace.active = 1` to enter streaming mode.
8. Under Forbid: call `drain_stale_events()` to flush leftover ring
   buffer content from prior sessions. This advances `read_pos` to
   `write_pos` and clears the `valid`, `retval`, `ioerr`, and `flags`
   fields of every slot.
9. Call `eclock_capture_epoch()` to snapshot the wall-clock time and
   prepare for EClock-to-timestamp conversion.
10. Send `OK` (no sentinel -- the response is a streaming continuation).
11. Emit header comment lines via `emit_trace_header()`.

The client now receives DATA chunks as events arrive, until it sends
`STOP` or disconnects.

### TRACE RUN

TRACE RUN follows the same initial steps as TRACE START, with additions:

1. Find the `--` separator in the command line. Everything before it is
   filter arguments; everything after is the command to execute.
2. Reject `PROC=` filters (task filtering is automatic, returns
   ERR_SYNTAX).
3. Parse `CD=<dir>` from the filter portion; Lock the directory.
4. Clear value caches and reset tier.
5. Allocate a process slot from the daemon's tracked-process table
   (`MAX_TRACKED_PROCS` = 16 slots).
6. Create the target process via `CreateNewProcTags()` under Forbid.
7. Set `g_anchor->filter_task` to the new process's Task pointer (if
   not already owned by another TRACE RUN session). This enables
   stub-level task filtering: stubs compare `SysBase->ThisTask` against
   `filter_task` and skip non-matching tasks entirely, keeping the ring
   buffer clean.
8. Capture `event_sequence` under Forbid as `run_start_seq` -- events
   with sequence numbers below this threshold are stale and ignored.
9. Drain stale events under Forbid.
10. Permit (the new process can now run).
11. Parse remaining filters, capture EClock epoch, enter streaming mode.
12. Send `OK <proc_id>` and emit the header.

The session ends when the target process exits. `trace_check_run_completed()`
detects this (called after `exec_scan_completed()` in the daemon's event
loop), performs a final drain of remaining target-process events from the
ring buffer, sends `# PROCESS EXITED rc=<N>`, `END`, and the sentinel.

### STOP

During an active stream, the client sends `STOP` as a bare line. The
daemon's `trace_handle_input()` recognizes it and responds with `END`
followed by the sentinel. For TRACE RUN sessions, `trace_run_cleanup()`
clears `filter_task` and resets per-client trace state.

### Disconnect Cleanup

If a tracing client disconnects without sending STOP,
`trace_run_disconnect_cleanup()` clears `filter_task` (if this client
owned it) and resets trace state. This prevents orphaned task filters
from blocking subsequent TRACE RUN sessions.

### Shutdown Detection

During polling, if the daemon cannot obtain the anchor semaphore (shared)
and `global_enable` is 0, it interprets this as atrace shutting down
(`atrace_loader QUIT` in progress). It sends `# ATRACE SHUTDOWN` to all
active trace clients, followed by `END` and sentinel, then clears
`g_anchor` and `g_ring_entries`.


## Event Polling

### The Polling Loop (trace_poll_events)

`trace_poll_events()` is called once per iteration of the daemon's main
event loop, but only when at least one client has `trace.active == 1`
(checked by `trace_any_active()`).

The function:

1. Obtains the anchor semaphore in shared mode via
   `AttemptSemaphoreShared()`. If the semaphore is busy and
   `global_enable` is 0, shutdown is detected (see above). If the
   semaphore is merely busy, the poll cycle is skipped.

2. Captures a batch DateStamp for timestamp fallback (used when EClock
   is unavailable).

3. Iterates from `ring->read_pos` to `ring->write_pos`, processing up
   to **512 events per poll cycle** (the batch limit). Only events
   actually sent to at least one subscriber count against this limit --
   self-filtered, noise-suppressed, and content-filtered events are
   consumed without counting, preventing a full buffer of filtered
   events from stalling real event delivery.

4. For each event slot:
   - Checks the `valid` field (0=empty, 1=complete, 2=in-progress).
   - Applies in-progress patience logic (see below).
   - Applies self-filtering and noise filtering (see below).
   - Resolves the task name.
   - Formats the event line via `trace_format_event()`.
   - Broadcasts to all active trace clients, applying per-client filters.
   - Clears the `valid` field (releases the slot for reuse).
   - Advances `read_pos`.

5. Updates `events_consumed` on the anchor.

6. Checks `ring->overflow`. If non-zero, atomically reads and clears it
   under Disable/Enable and accumulates into `g_events_dropped`. The
   overflow count is reported in the session-end summary rather than
   as per-poll comments. The per-session counter is reset at session
   start by `drain_stale_events()`. TRACE STATUS also reports the
   cumulative count.

7. Releases the anchor semaphore.

### In-Progress Event Handling (INFLIGHT_PATIENCE)

Stubs set `valid=2` ("in-progress") before calling the original library
function, then set `valid=1` ("complete") after the function returns.
For non-blocking functions this transition takes microseconds. For
blocking functions (RunCommand, Execute, WaitSelect), the slot may
remain at `valid=2` for seconds.

The daemon uses a patience mechanism controlled by the
`INFLIGHT_PATIENCE` constant (3). When the consumer encounters
`valid=2` at a given ring position:

- **First encounter:** Records the position in `g_inflight_stall_pos`,
  sets `g_inflight_stall_count = 1`, and breaks out of the loop. The
  event is not consumed yet.
- **Subsequent encounters at the same position:** Increments the stall
  count. If it reaches `INFLIGHT_PATIENCE` (3 polls, approximately
  ~60ms at the 20ms poll interval), the event is consumed
  as-is. The `retval`, `ioerr`, and `flags` fields may not have been
  filled by the post-call handler, but the `ev->valid == 1` guard in
  `format_retval()` prevents display of stale IoErr data.

This mechanism prevents blocking functions from freezing the entire
ring buffer while still giving non-blocking functions time to complete.
Stall tracking state is reset whenever the consumer advances past a
slot, and also at session start by `drain_stale_events()`.

### Self-Filtering

Events generated by the daemon's own task (`g_daemon_task` from
`exec.c`) are unconditionally skipped. This prevents feedback loops
(the daemon's own Open, Lock, etc. calls generating trace events)
and keeps output focused on user processes. The filtered count is
tracked in `g_self_filtered`.

### Content-Based Noise Filtering at Basic Tier

At Basic tier (`g_current_tier == 1`), two content-based suppression
rules apply:

1. **OpenLibrary v0 success suppression:** Successful `OpenLibrary()`
   calls with `version == 0` are suppressed. These come from AmigaOS
   internal library management (CLI startup, library interdependencies)
   and are pure noise at the Basic tier. Failed opens of any version
   always pass through for diagnostic value. At Detail tier and above,
   all OpenLibrary events are shown.

2. **Lock success suppression:** Successful `Lock()` calls (`retval != 0`)
   are suppressed. Path resolution during normal operation generates a
   high volume of Lock calls. Failed Locks always pass through. Even
   when a successful Lock is suppressed, the lock-to-path cache is still
   populated so that subsequent `CurrentDir()` or `UnLock()` events can
   resolve the path.

Both types of suppressed events increment `g_self_filtered` (shared
with the self-filter counter).


## Task Name Resolution

### Overview

Every event carries a `caller_task` pointer (4 bytes at event offset 8)
identifying the Exec Task/Process that made the library call. The daemon
resolves this pointer to a human-readable name for display and PROC
filter matching.

### Task Cache

The task cache is a flat array of 64 entries (`TASK_CACHE_SIZE`), each
storing a task pointer and a 64-character name buffer:

```c
#define TASK_CACHE_SIZE   64
#define TASK_CACHE_REFRESH_INTERVAL  20  /* polls = ~2 seconds */

struct task_cache_entry {
    APTR task_ptr;
    char name[64];
};
```

The cache is rebuilt from scratch every 20 poll cycles (approximately
400ms at the 20ms poll interval). Rebuilding walks
`ExecBase->TaskReady` and `ExecBase->TaskWait` linked lists under
Forbid, plus the current task (the daemon itself). For each task:

- If the task is an `NT_PROCESS` and has a positive `pr_TaskNum` (CLI
  process), `resolve_cli_name()` extracts the CLI command name from
  `pr_CLI->cli_CommandName` (a BSTR). The path prefix is stripped to
  yield the basename (e.g., `"SYS:C/List"` becomes `"List"`), and the
  result is formatted as `"[N] basename"` (e.g., `"[3] List"`).
- If CLI name extraction fails (no CLI structure, empty command name),
  the `tc_Node.ln_Name` is used with an optional `[N]` prefix.
- Plain Tasks (not Processes) use `tc_Node.ln_Name` directly.

### Resolution Algorithm (resolve_task_name)

When `resolve_task_name()` is called with a task pointer:

1. If the pointer is NULL, returns `"<null>"`.
2. Checks if the cache needs refreshing (counter >= 20 or cache empty).
3. Searches the cache linearly. On hit, records the name in the
   history cache and returns it.
4. On cache miss, attempts an on-demand refresh (if at least 3 polls
   have elapsed since the last refresh, to prevent refresh storms from
   burst events). Re-searches after refresh.
5. On continued miss, performs a direct dereference of the task pointer
   under Forbid. This handles short-lived tasks that started and exited
   between cache refreshes. Reads `tc_Node.ln_Name` and, for Processes,
   attempts CLI name resolution.
6. If `ln_Name` is NULL (task memory may have been freed), checks the
   history cache for the last-known name.
7. As a final fallback, returns `"<task 0x08300200>"` (hex address).

### Task History Cache

The history cache is a 32-entry ring buffer (`TASK_HISTORY_SIZE`) that
preserves the last-known name for each resolved task pointer:

```c
#define TASK_HISTORY_SIZE  32
```

Entries are recorded by `task_history_record()` whenever
`resolve_task_name()` successfully resolves a name (generic `"<task ...>"`
fallback names are not recorded). If a task pointer already exists in the
history with the same name, it is left unchanged. If the name differs
(e.g., a CLI process ran a new command), the entry is updated. New entries
overwrite the oldest when the ring is full.

The history cache serves two purposes:

- Resolving events from short-lived processes that have already exited
  and been removed from the system task lists.
- Providing names for PROC filter matching when the task pointer is no
  longer in the main cache.

### Embedded Task Name

Events include a 22-byte `task_name`
field (offset 106) captured by the stub at call time. If
`resolve_task_name()` returns a generic `"<task 0x...>"` string (process
already exited, not in any cache), the daemon falls back to this
embedded name. However, the embedded name contains the raw
`tc_Node.ln_Name` (e.g., `"Background CLI"`), not the CLI command name.
It is used only as a last resort because PROC filters need the command
basename for meaningful matching.


## Value Caches

The daemon maintains four value caches that map opaque return values
(BPTRs, pointers) back to their associated names or paths. These enable
human-readable formatting of Close, UnLock, FreeDiskObject, RunCommand,
and UnLoadSeg events.

All caches are cleared at the start of each trace session
(`trace_cmd_start()` and `trace_cmd_run()` both call `*_cache_clear()`).
This prevents stale mappings from prior sessions -- AmigaOS reuses BPTR
addresses after resources are freed.

### Lock Cache

Maps `Lock()` and `CreateDir()` return values (BPTR file locks) to their
path strings.

| Property     | Value   |
|------------- |---------|
| Size         | 128 entries (`LOCK_CACHE_SIZE`) |
| Eviction     | FIFO (circular write index) |
| Key          | `retval` from Lock/CreateDir |
| Value        | Path string (64-byte buffer, 63 chars max) |
| Populated by | `trace_format_event()` on successful Lock/CreateDir |
| Looked up by | `format_args()` for CurrentDir and UnLock |
| Removal      | `lock_cache_remove()` on UnLock events |

Design note: `Open()` return values are not stored in the lock cache.
Open returns a BPTR to a `FileHandle`, not a `FileLock`. `CurrentDir()`
takes a `FileLock`, so Open return values would never produce valid cache
hits and would waste slots.

### File Handle Cache

Maps `Open()` return values (BPTR file handles) to their path strings.

| Property     | Value   |
|------------- |---------|
| Size         | 128 entries (`FH_CACHE_SIZE`) |
| Eviction     | FIFO + explicit removal |
| Key          | `retval` from Open |
| Value        | Path string (64-byte buffer) |
| Populated by | `trace_format_event()` on successful Open |
| Looked up by | `format_args()` for Close |
| Removal      | `fh_cache_remove()` on Close events |

Both caches use FIFO eviction and explicit removal (the lock cache
removes entries on UnLock via `lock_cache_remove()`; the file handle
cache removes entries on Close via `fh_cache_remove()`). Explicit
removal is important because AmigaOS reuses addresses -- a stale entry
could cause incorrect path attribution for a subsequent Open or Lock.

### DiskObject Cache

Maps `GetDiskObject()` return values (DiskObject pointers) to their
name strings.

| Property     | Value   |
|------------- |---------|
| Size         | 128 entries (`DISKOBJ_CACHE_SIZE`) |
| Eviction     | FIFO + explicit removal |
| Key          | `retval` from GetDiskObject |
| Value        | Name string (64-byte buffer) |
| Populated by | `trace_format_event()` on successful GetDiskObject |
| Looked up by | `format_args()` for FreeDiskObject |
| Removal      | `diskobj_cache_remove()` on FreeDiskObject events |

### Segment Cache

Maps `LoadSeg()` and `NewLoadSeg()` return values (BPTR segment lists)
to their filename strings.

| Property     | Value   |
|------------- |---------|
| Size         | 128 entries (`SEG_CACHE_SIZE`) |
| Eviction     | FIFO + explicit removal |
| Key          | `retval` from LoadSeg/NewLoadSeg |
| Value        | Filename string (60-byte buffer, 59 chars max) |
| Populated by | `trace_format_event()` on successful LoadSeg/NewLoadSeg |
| Looked up by | `format_args()` for RunCommand and UnLoadSeg |
| Removal      | `seg_cache_remove()` on UnLoadSeg events |

### Limitations

All four caches use linear search for lookups (O(N) per lookup, N=128).
This is acceptable because the daemon processes events sequentially and
typical cache populations are well below 128 entries. However, programs
that rapidly create and release hundreds of locks, file handles, or
segments within a single trace session will see FIFO eviction, and
later Close/UnLock/FreeDiskObject events may fall back to hex address
display.

Cache entries are populated during `trace_format_event()`, which runs
after per-client filtering. If a successful Lock event is filtered out
(e.g., by a FUNC filter that excludes Lock), the lock-to-path mapping is
not cached, and a subsequent CurrentDir or UnLock referencing that lock
will display a hex address. The one exception is Basic-tier Lock success
suppression, which explicitly populates the lock cache even for suppressed
events.


## Event Formatting

### Function Lookup (lookup_func)

The daemon cannot access atrace's function metadata directly (separate
binary), so it maintains its own static copy in `func_table[]`. This
table has 99 entries, one per traced function, matching the installation
order in `atrace/funcs.c`. Each entry stores:

```c
struct trace_func_entry {
    const char *lib_name;   /* "exec", "dos", etc. */
    const char *func_name;  /* "Open", "AllocMem", etc. */
    UBYTE lib_id;           /* LIB_EXEC=0, LIB_DOS=1, ... */
    WORD  lvo_offset;       /* negative LVO (-30, -84, etc.) */
    UBYTE error_check;      /* ERR_CHECK_* constant */
    UBYTE has_string;       /* 1 if stub captures string_data */
    UBYTE result_type;      /* RET_* constant */
};
```

`lookup_func()` searches `func_table[]` linearly by `(lib_id, lvo_offset)`
pair and returns a pointer to the matching entry, or NULL if not found.

### Argument Formatting (format_args)

`format_args()` dispatches to per-function formatting logic based on the
function's library ID and LVO offset. Every traced function has a custom
format case (or falls back to a generic formatter). Highlights:

- **String arguments** are quoted: `"RAM:test"`. If the string_data
  field was likely truncated (length >= 63 characters), `"..."` is
  appended.
- **Dual-string capture** (MakeLink, Rename): the 64-byte string_data
  is split into two 32-byte halves, each formatted as a quoted string
  with separate truncation detection at 31 characters.
- **Named constants**: Open access modes (Read, Write, Read/Write),
  Lock types (Shared/Exclusive), MEMF flags (MEMF_PUBLIC|MEMF_CLEAR),
  IDCMP flags, socket address families (AF_INET), socket types
  (SOCK_STREAM), shutdown modes (SHUT_RDWR), Seek offsets
  (OFFSET_BEGINNING/CURRENT/END), signal bits (CTRL_C, CTRL_D,
  CTRL_E, CTRL_F), GetVar/SetVar/DeleteVar scope flags
  (GLOBAL/LOCAL/ANY), FindVar type codes (LV_VAR/LV_ALIAS), and
  SetProtection bits (`hspa rwed` notation).
- **Indirect deref display**: when string_data contains a name captured
  via `DEREF_LN_NAME` (port names, semaphore names, device names), the
  name is shown quoted. When the deref field is empty (NULL ln_Name),
  the raw pointer is shown (e.g., `port=0x1a2b3c`).
- **I/O request display**: `DEREF_IOREQUEST` captures
  `io_Device->ln_Name` and `io_Command`. These are formatted as
  `"devicename" CMD N`.
- **Value cache lookups**: Close resolves file handle to path via
  `fh_cache_lookup()`, CurrentDir and UnLock resolve lock to path via
  `lock_cache_lookup()`, FreeDiskObject resolves pointer to name via
  `diskobj_cache_lookup()`, RunCommand and UnLoadSeg resolve segment to
  filename via `seg_cache_lookup()`.

### Return Value Formatting (format_retval)

`format_retval()` formats the return value according to the function's
`result_type` field and returns a single-character status code:

| Constant       | Display                        | Status |
|--------------- |------------------------------- |--------|
| `RET_PTR`      | Hex address or `"NULL"`        | O/E    |
| `RET_BOOL_DOS` | `"OK"` or `"FAIL"`            | O/E    |
| `RET_NZERO_ERR`| `"OK"` or `"err=N"`           | O/E    |
| `RET_VOID`     | `"(void)"`                     | -      |
| `RET_MSG_PTR`  | Hex address or `"(empty)"`     | O/-    |
| `RET_RC`       | `"rc=N"` (signed decimal)      | O/E    |
| `RET_LOCK`     | Hex BPTR or `"NULL"`           | O/E    |
| `RET_LEN`      | Decimal count or `"-1"`        | O/E    |
| `RET_OLD_LOCK` | Hex BPTR, lock path, or `"(none)"` | -  |
| `RET_PTR_OPAQUE`| `"OK"` or `"NULL"`            | O/E    |
| `RET_EXECUTE`  | `"rc=0"`, `"OK"`, or `"rc=N"` | -      |
| `RET_IO_LEN`   | Decimal, `"-1"`, or `"0"`      | O/E    |

Status characters: `O` = success, `E` = error, `-` = neutral/void.

After formatting the return value, an IoErr epilogue appends DOS error
information for dos.library failures. This fires only when all of the
following are true:

- Status is `E` (error).
- `ev->valid == 1` (post-call handler completed).
- `FLAG_HAS_IOERR` (0x02) is set in `ev->flags`.
- `ev->ioerr != 0`.
- The function's library is dos.library (`lib_id == LIB_DOS`).

The IoErr value is decoded to a human-readable name by `dos_error_name()`,
which covers standard AmigaOS error codes 103-233. Unknown codes are
shown as `"(err N)"`.

### EClock Timestamp Formatting (eclock_format_time)

For events with `FLAG_HAS_ECLOCK` set, the daemon
formats per-event timestamps with microsecond precision as
`HH:MM:SS.uuuuuu`. The conversion uses a session epoch captured at
TRACE START/RUN:

1. `eclock_capture_epoch()` captures the wall-clock time via
   `DateStamp()` and stores it as seconds since midnight plus
   microseconds. It precomputes constants for 48-bit EClock conversion:
   `g_secs_per_hi = (2^32 - 1) / freq` and
   `g_rem_per_hi = (2^32 - 1) % freq`.

2. The EClock baseline is captured lazily from the first event with
   `FLAG_HAS_ECLOCK`. The daemon cannot call `ReadEClock()` directly
   (it does not open timer.device), so it uses the first event's
   EClock values as the reference point.

3. For each subsequent event, 48-bit subtraction computes elapsed ticks,
   which are converted to seconds and microseconds, then added to the
   wall-clock epoch.

When EClock is unavailable (`FLAG_HAS_ECLOCK` not
set), the daemon falls back to a per-batch DateStamp timestamp at
millisecond resolution (`HH:MM:SS.mmm`).

### Event Line Assembly (trace_format_event)

`trace_format_event()` combines all formatting steps into a single
tab-separated output line:

```
<seq>\t<time>\t<lib>.<func>\t<task>\t<args>\t<retval>\t<status>
```

Seven fields separated by tab characters. For example:

```
42\t10:30:15.123456\tdos.Open\t[3] Shell Process\t"RAM:test",Write\t0x1c16daf0\tO
```

Task name selection: the caller provides a resolved name from
`resolve_task_name()`. If that name is a generic `"<task 0x...>"` string
and the event has an embedded `task_name`, the embedded name is
used instead.

Cache population is also performed during formatting: successful Lock,
CreateDir, Open, GetDiskObject, LoadSeg, and NewLoadSeg events populate
their respective caches from the `retval` and `string_data` fields.

### Sending Events (send_trace_data_chunk)

Each formatted event line is sent to clients as a DATA chunk using the
amigactld wire protocol:

```
DATA <length>\n<payload>
```

Comments (lines starting with `#`) use the same framing. The stream
ends with:

```
END\n.\n
```


## Per-Client Filtering

### Filter Types

Each connected client has its own `struct trace_state` with independent
filter settings. All filters are AND-combined: an event must pass all
active filters to be sent to that client.

**Simple filters** (set by TRACE START arguments):

| Filter      | Field               | Behavior                            |
|------------ |-------------------- |-------------------------------------|
| `LIB=`      | `filter_lib_id`     | Match single library ID (-1 = all)  |
| `FUNC=`     | `filter_lvo`        | Match single LVO (0 = all)          |
| `PROC=`     | `filter_procname`   | Case-insensitive substring match    |
| `ERRORS`    | `filter_errors_only`| Only show error returns             |

**Extended filters** (set by FILTER command during streaming):

| Filter      | Mode field           | Arrays                          | Max entries |
|------------ |--------------------- |-------------------------------- |-------------|
| `LIB=`      | `lib_filter_mode`    | `lib_filter_ids[]`              | 32          |
| `-LIB=`     | `lib_filter_mode=-1` | `lib_filter_ids[]`              | 32          |
| `FUNC=`     | `func_filter_mode`   | `func_filter_lib_ids[]`, `func_filter_lvos[]` | 32 |
| `-FUNC=`    | `func_filter_mode=-1`| `func_filter_lib_ids[]`, `func_filter_lvos[]` | 32 |
| `ENABLE=`   | (global effect)      | Modifies `patches[].enabled`    | N/A         |
| `DISABLE=`  | (global effect)      | Modifies `patches[].enabled`    | N/A         |

The maximum number of entries in each filter list is 32
(`MAX_FILTER_NAMES`). Library names automatically strip `.library`,
`.device`, and `.resource` suffixes. Function names support
library-scoped syntax (`dos.Open`) to prevent LVO collisions.

### Filter Matching (trace_filter_match)

The filter matching function checks in this order:

1. **Library filter** (simple or extended whitelist/blacklist).
2. **Function filter** (simple LVO match or extended (lib_id, lvo)
   pair match).
3. **PROC filter**: case-insensitive substring match on the task name.
   The `[N]` CLI number prefix is stripped before matching, so
   `PROC=List` matches `[3] List` but `PROC=3` does not.
4. **ERRORS filter**: classifies the return value using the function's
   `error_check` type. Events from void functions are always excluded.
   Events from `ERR_CHECK_NONE` functions (e.g., GetMsg) are always
   excluded. Unknown functions are always included.

### Error Classification (ERR_CHECK_* Types)

The ERRORS filter uses eight classification types, assigned per-function
in `func_table[]`:

| Constant            | Code | Condition for "error"       | Example functions |
|-------------------- |----- |---------------------------- |-------------------|
| `ERR_CHECK_NULL`    | 0    | `retval == 0`               | Open, Lock, OpenLibrary |
| `ERR_CHECK_NZERO`   | 1    | `retval != 0`               | OpenDevice, DoIO |
| `ERR_CHECK_VOID`    | 2    | Never shown (void)          | FreeMem, PutMsg |
| `ERR_CHECK_ANY`     | 3    | Always shown                | AbortIO, CheckIO |
| `ERR_CHECK_NONE`    | 4    | Never an error              | GetMsg, MatchToolValue |
| `ERR_CHECK_RC`      | 5    | `retval != 0`               | RunCommand, SystemTagList |
| `ERR_CHECK_NEGATIVE`| 6    | `(LONG)retval < 0`          | GetVar |
| `ERR_CHECK_NEG1`    | 7    | `retval == 0xFFFFFFFF (-1)` | socket, recv, send |

For the complete per-function error check type assignment, see
[traced-functions.md](traced-functions.md).

### TRACE RUN Filtering

In TRACE RUN mode, the polling loop applies an additional filter before
per-client filters: events are matched by exact task pointer comparison
(`ev->caller_task == c->trace.run_task_ptr`). Events with sequence
numbers below `run_start_seq` are also skipped (stale events from before
the process started).

This daemon-side check is a backup for the stub-level `filter_task`
comparison. If another TRACE RUN was already active when this session
started, `filter_task` could not be set, and the stubs would record
events from all tasks. The daemon-side check ensures only the target
process's events reach the client.


## TRACE Command Reference

All TRACE commands are dispatched by `cmd_trace()`, which extracts the
subcommand keyword and delegates to the appropriate handler.

### TRACE STATUS

Returns a multi-line payload with current state information:

| Field              | Description                                    |
|------------------- |------------------------------------------------|
| `loaded`           | 0 or 1, whether atrace semaphore was found     |
| `enabled`          | 0 or 1, `global_enable` state                  |
| `patches`          | Total patch count                               |
| `events_produced`  | `event_sequence` counter                        |
| `events_consumed`  | Consumer counter                                |
| `events_dropped`   | Cumulative count of old events discarded by overflow |
| `events_self_filtered` | Self-filter + content-filter suppression count |
| `buffer_capacity`  | Ring buffer slot count                          |
| `buffer_used`      | Current occupancy                               |
| `peek_N`           | Debug: first 4 pending events (pos, valid, lib_id, lvo, seq, task) |
| `poll_count`       | Total poll cycles since startup                 |
| `filter_task`      | Current stub-level task filter (hex)            |
| `anchor_version`   | Anchor struct version number                    |
| `eclock_freq`      | EClock frequency in Hz                          |
| `ioerr_capture`    | 1 (IoErr capture supported)                     |
| `noise_disabled`   | Count of currently disabled noise functions      |
| `patch_N`          | Per-patch status: `lib.func enabled=0/1`        |

TRACE STATUS triggers auto-loading. If atrace is not found after
auto-loading, it returns only `loaded=0`.

### TRACE START [filters]

Enters streaming mode. Filter arguments are parsed from the command tail:

- `LIB=<name>` -- filter by library
- `FUNC=<name>` or `FUNC=<lib>.<name>` -- filter by function
- `PROC=<substring>` -- filter by process name
- `ERRORS` -- only show error returns

Multiple filters can be combined (AND logic). Unknown keywords are
silently skipped.

### TRACE RUN [filters] -- <command>

Launches a command and enters streaming mode with automatic task
filtering. The `--` separator is required. Accepts the same filter
arguments as TRACE START except `PROC=` (returns ERR_SYNTAX). Also
accepts `CD=<dir>` to set the working directory.

### TRACE STOP

Sent by the client during an active stream (not as a top-level command).
Recognized by `trace_handle_input()`.

### TRACE ENABLE [func1 func2 ...]

With no arguments: sets `global_enable = 1` on the anchor (enables all
patches globally). With function names: enables specific per-patch
`enabled` flags. Function names are validated in a first pass before any
changes are applied; an unknown name returns ERR_SYNTAX without modifying
any state.

### TRACE DISABLE [func1 func2 ...]

With no arguments: sets `global_enable = 0` and drains the ring buffer.
With function names: disables specific per-patch `enabled` flags, using
the same two-pass validate-then-apply pattern as ENABLE.

Global disable is performed under Disable/Enable for atomicity. After
clearing `global_enable`, the ring buffer is drained: all valid entries
are cleared and `read_pos` is advanced to `write_pos`. This prevents a
full buffer from immediately overflowing when tracing is re-enabled.

### TRACE TIER <1|2|3>

Sets the daemon's tier level for content-based filtering. This affects
the Basic-tier noise suppression rules (OpenLibrary v0, Lock success).
Invalid levels return ERR_SYNTAX.

During an active stream, the TIER command can also be sent inline (same
framing as STOP and FILTER). `trace_handle_input()` recognizes it and
updates `g_current_tier`, emitting a `# tier changed: <name>` comment
to the stream. Invalid inline TIER values are silently ignored.

### FILTER [clauses]

Mid-stream filter change, sent during an active trace session (not as a
top-level command). Recognized by `trace_handle_input()`.

`parse_extended_filter()` handles the parsing. If the arguments contain
no commas, no `-LIB=`/`-FUNC=` prefixes, and no `ENABLE=`/`DISABLE=`
clauses, it delegates to `parse_filters()` for exact backward
compatibility with the simple single-value syntax.

Extended syntax examples:

```
FILTER LIB=dos,exec                  # whitelist two libraries
FILTER -FUNC=AllocMem,GetMsg          # blacklist two functions
FILTER LIB=dos -FUNC=Close ERRORS     # combine whitelist, blacklist, errors
FILTER ENABLE=GetMsg,PutMsg           # enable specific patches (global)
FILTER DISABLE=ModifyIDCMP            # disable specific patch (global)
FILTER                                # clear all filters (empty = match all)
```

Important: `ENABLE=` and `DISABLE=` clauses modify the global
`patches[].enabled` state, affecting all connected clients. This
contrasts with LIB/FUNC/PROC/ERRORS which are per-session. After
processing, a filter comment line is emitted to the stream showing the
new filter state and current tier level.


## Tier Management

### Server-Side Tier Level

The daemon tracks the current tier level in `g_current_tier` (1=Basic,
2=Detail, 3=Verbose), initialized to 1 at each session start.

The tier level controls content-based noise suppression in the polling
loop (OpenLibrary v0, Lock success -- described above). It does not
directly enable or disable individual patches. Patch enable/disable
state is managed by the client via ENABLE=/DISABLE= in FILTER commands
or via the TRACE ENABLE/TRACE DISABLE commands.

### How Tier Switching Works

Tier switching is a coordinated operation between client and daemon:

1. The client computes which functions need to be enabled or disabled
   to transition from the old tier to the new tier (using
   `compute_tier_switch()` from `trace_tiers.py`).
2. The client sends ENABLE=/DISABLE= clauses in a FILTER command to
   toggle the appropriate patch `enabled` flags.
3. The client sends a bare `TIER <N>` inline command to update the
   daemon's content-based filtering level.

The TUI performs both steps in `_switch_tier()`, sending the FILTER and
TIER commands as fire-and-forget writes on the streaming socket.

### Noise Function Table

The daemon maintains a `noise_func_names[]` array listing all non-Basic
tier functions (42 names: 13 Detail + 3 Verbose + 26 Manual). This table
serves two purposes:

1. **Validation at discovery time:** Each name is checked against the
   loaded patch table. Mismatches produce a warning, catching
   synchronization errors between the loader and daemon.
2. **STATUS reporting:** `trace_cmd_status()` counts how many noise
   functions are currently disabled and reports this as
   `noise_disabled=N`.

The noise function list must match the union of `tier_detail_funcs`,
`tier_verbose_funcs`, and `tier_manual_funcs` from `atrace/main.c`. The
source of truth for tier membership is `trace_tiers.py` on the client
side.


## Trace Log Header

At the start of each TRACE START or TRACE RUN session, the daemon emits
a series of comment lines (prefixed with `#`) as DATA chunks:

```
# atrace, 2026-03-14 19:33:38
# eclock_freq: 709379 Hz
# timestamp_precision: microsecond
# command: C:atrace_test               (TRACE RUN only)
# filter: tier=basic
# enabled: GetMsg, PutMsg (normally noise-disabled)
# disabled: ModifyIDCMP (manually disabled)
```

The header includes:

- **Timestamp**: wall-clock time at session start.
- **EClock info**: frequency in Hz and precision indicator.
- **Command** (TRACE RUN only): the command being traced.
- **Filter description**: current tier and any active filters.
- **Enable/disable deviations**: functions whose enable state differs
  from the default for their tier. Noise functions that are enabled
  (normally disabled) are listed as "enabled". Basic-tier functions that
  are disabled (normally enabled) are listed as "disabled".

The `build_filter_desc()` helper constructs the human-readable filter
description string, which is also reused when the FILTER command emits
a mid-stream filter change notification.


## Module Globals

Key module-global variables in `trace.c`:

| Variable                 | Type    | Description                           |
|------------------------- |-------- |---------------------------------------|
| `g_anchor`               | Pointer | Cached anchor, NULL if not found      |
| `g_ring_entries`         | Pointer | First event slot in ring buffer       |
| `g_events_dropped`       | ULONG   | Cumulative count of old events discarded |
| `g_self_filtered`        | ULONG   | Self-filter + content-filter count    |
| `g_poll_count`           | ULONG   | Total poll cycles                     |
| `g_current_tier`         | int     | 1=Basic, 2=Detail, 3=Verbose         |
| `g_inflight_stall_pos`   | ULONG   | Ring position of current valid=2 stall|
| `g_inflight_stall_count` | int     | Consecutive polls at stall position   |
| `g_eclock_valid`         | int     | 1 = EClock epoch captured             |
| `g_eclock_baseline_set`  | int     | 1 = baseline from first event         |
| `g_eclock_freq`          | ULONG   | EClock frequency in Hz                |
| `g_start_eclock_lo/hi`   | ULONG/WORD | EClock baseline values             |
| `g_start_secs/us`        | LONG    | Wall-clock epoch (secs since midnight)|
| `g_secs_per_hi/g_rem_per_hi` | ULONG | Precomputed conversion constants |

All globals are reset by `trace_cleanup()` and/or at session start.
The module is single-threaded (the daemon uses cooperative
multiplexing, not threads), so no synchronization is needed for these
variables.
