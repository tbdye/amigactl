# Performance and Overhead

atrace is designed to be lightweight enough for always-on tracing on a
stock Amiga system. This document quantifies the overhead at each level
of the system -- stub execution, memory consumption, ring buffer sizing,
daemon polling, and network bandwidth -- and provides guidance for
capacity planning and overhead reduction.

For the ring buffer internals and overflow mechanics, see
[ring-buffer.md](ring-buffer.md). For how stubs are structured and
installed, see [stub-mechanism.md](stub-mechanism.md). For the overall
system architecture, see [architecture.md](architecture.md).


## Stub Overhead

Every patched function call passes through a generated 68k stub. The
cost of that stub depends on which code path the call takes. There are
three progressively more expensive paths: disabled, filtered-out, and
full capture.

### Disabled Fast Path (Per-Patch Disabled)

When a function's per-patch `enabled` flag is 0, the stub takes the
fastest possible exit. The entire path executes 7 instructions:

**Prefix (4 instructions):**

```
move.l  a5, -(sp)              ; save a5
movea.l #PATCH_ADDR, a5        ; load patch descriptor pointer
tst.l   enabled(a5)            ; test per-patch enabled flag
beq.w   .disabled              ; branch to disabled path
```

**Suffix .disabled (3 instructions):**

```
movea.l (sp)+, a5              ; restore a5
move.l  #ORIG_ADDR, -(sp)     ; push original function address
rts                            ; tail-call to original function
```

This is a register save, a memory test, a branch, a register restore,
and a tail-call. No Disable/Enable pair, no ring buffer access, no
string capture. The overhead is negligible -- a handful of memory
accesses and one conditional branch.

### Disabled Fast Path (Global Disable)

When the per-patch `enabled` flag is 1 but `global_enable` is 0 (global
tracing disabled via `atrace_loader DISABLE`), the prefix executes 3
additional instructions before reaching the same `.disabled` path:

```
movea.l #ANCHOR_ADDR, a5       ; load anchor pointer
tst.l   global_enable(a5)      ; test global enable flag
beq.w   .disabled              ; branch to disabled path
```

Total: 10 instructions (7 prefix + 3 suffix). Still very fast -- two
pointer loads, two memory tests, two branches, then the standard
3-instruction disabled exit.

### Filtered-Out Path (Task Filter Mismatch)

When both `enabled` and `global_enable` are set but the task filter is
active and the current task does not match, the prefix takes the task
filter branch. This adds a SysBase dereference, a ThisTask read, a
pointer comparison, and a register save/restore for a6:

```
tst.l   filter_task(a5)        ; is task filter active?
beq.s   .no_filter             ; skip if NULL (trace all)
move.l  a6, -(sp)              ; save a6
movea.l 4.w, a6                ; load SysBase
movea.l 276(a6), a6            ; read ThisTask
cmpa.l  filter_task(a5), a6    ; compare against filter
movea.l (sp)+, a6              ; restore a6
bne.w   .disabled              ; mismatch -> disabled path
```

Total: 18 instructions (15 prefix + 3 suffix). Still fast -- no
interrupts disabled, no ring buffer access. This is the common path
during TRACE RUN when most system activity comes from other tasks.

### Full Capture Path

When all checks pass, the stub executes the complete capture sequence:

1. **Register save**: `movem.l d0-d7/a0-a4/a6, -(sp)` saves 14
   registers (56 bytes) to the stack.

2. **Ring buffer reservation**: Disable(), read write_pos, increment
   with wrap, check against read_pos for overflow, write new write_pos,
   increment use_count, read and increment event_sequence, Enable().
   This is the critical section -- interrupts are disabled for
   approximately 16 instructions.

3. **EClock capture**: Calls ReadEClock() through timer.device for
   microsecond-precision timestamps. This is a device library call
   (one JSR through the library jump table).

4. **Event header fill**: Writes sequence number, lib_id, lvo_offset,
   and caller_task pointer into the event slot.

5. **Argument copy**: 3 words (6 bytes) per argument, up to 4
   arguments. Copies register values from the MOVEM save frame into
   the event's args[] array.

6. **String capture**: If the function has string arguments or indirect
   name deref, the stub copies up to 63 bytes of string data into the
   event's string_data field. Dual-string functions (Rename, MakeLink)
   copy two 31-byte strings. Indirect deref functions (IORequest device
   name, window title, etc.) follow pointer chains with NULL checks at
   each level.

7. **Task name capture**: Resolves the current task's name by checking
   whether it is an AmigaOS Process (NT_PROCESS). For CLI processes,
   follows the pr_CLI -> cli_CommandName BSTR chain. Falls back to
   tc_Node.ln_Name for plain tasks. This is 47 words (94 bytes) of
   generated code, executed for every captured event.

8. **Valid flag**: Sets `entry->valid = 2` (in-progress) before calling
   the original function.

9. **Register restore + trampoline**: Restores all saved registers,
   pushes the original function address onto the stack, and executes
   RTS to tail-call the original.

10. **Post-call handler**: After the original function returns, captures
    the return value (d0) into the event. For dos.library functions,
    also calls IoErr() and stores the result. Sets `valid = 1`
    (complete), decrements use_count.

The full capture path is the most expensive. The dominant costs are the
Disable/Enable pair (ring buffer reservation), the ReadEClock call
(timestamp capture), and the IoErr call (DOS functions only). String
capture and task name resolution involve memory reads but no system
calls.


## Memory Usage

atrace allocates all memory at install time from Amiga public memory
(MEMF_PUBLIC). Nothing is allocated during tracing. The major
allocations are:

### Fixed Structures

| Structure      | Size              | Notes                            |
|----------------|-------------------|----------------------------------|
| Anchor         | 92 bytes          | `struct atrace_anchor`, one only |
| Semaphore name | 15 bytes          | "atrace_patches" + NUL           |
| Patch array    | 40 x 99 = 3,960  | `struct atrace_patch` per function |

### Stub Code

Each function gets a generated stub allocated from MEMF_PUBLIC. Stub
size is the sum of three regions:

- **Prefix**: 196 bytes (standard) or 204 bytes (functions with
  NULL-argument filter: FindTask, UnLock, LockPubScreen,
  UnlockPubScreen)
- **Variable region**: depends on argument count, string capture mode,
  and indirect deref type
- **Suffix**: 126 bytes (identical for all functions)

Representative stub sizes (prefix + variable + suffix):

| Function type                        | Variable region | Total stub |
|--------------------------------------|-----------------|------------|
| 0-arg, no string (CreateMsgPort)     | 110 bytes       | 432 bytes  |
| 1-arg, single string (OpenLibrary)   | 138 bytes       | 460 bytes  |
| 1-arg, indirect deref (ObtainSem.)   | 152 bytes       | 474 bytes  |
| 2-arg, no string (AllocMem)          | 122 bytes       | 444 bytes  |
| 2-arg, dual string (Rename)          | 178 bytes       | 500 bytes  |
| 1-arg, IORequest deref (DoIO)        | 170 bytes       | 492 bytes  |
| 1-arg, Lock volume deref (CurrentDir)| 186 bytes       | 508 bytes  |
| 1-arg, NULL-arg filter (FindTask)    | 138 bytes       | 468 bytes  |

All 99 stubs together consume approximately 44-48 KB of Amiga memory.
Every stub includes the 94-byte task name capture sequence (47 words)
in its variable region.

### Ring Buffer

The ring buffer is a single contiguous allocation:

```
Total = 16 + (128 * capacity) bytes
```

| Capacity (events) | Ring buffer size | Total with fixed structures |
|--------------------|------------------|-----------------------------|
| 16 (minimum)       | 2,064 bytes      | ~52 KB                     |
| 1,024              | 131,088 bytes    | ~180 KB                    |
| 4,096              | 524,304 bytes    | ~570 KB                    |
| 8,192 (default)    | 1,048,592 bytes  | ~1.1 MB                    |
| 16,384             | 2,097,168 bytes  | ~2.1 MB                    |
| 65,536             | 8,388,624 bytes  | ~8.4 MB                    |

The default configuration (8,192 events) uses approximately 1.1 MB of
Amiga memory total (ring buffer + stubs + fixed structures).


## Ring Buffer Capacity

The ring buffer is the bridge between the 68k stubs (producers) and the
daemon (consumer). Its capacity determines how many events can
accumulate before the consumer must drain them. When the buffer is full,
new events are silently dropped and the `overflow` counter increments.

### Configuring Capacity

Set the capacity at install time:

```
atrace_loader BUFSZ 16384
```

The minimum capacity is 16 events. There is no maximum, but each event
slot is 128 bytes, so large capacities consume significant memory. The
default of 8,192 events (1 MB) is a reasonable balance for systems with
several megabytes of free memory.

### Event Rate Estimates

The event rate depends on which functions are enabled and what the
system is doing. Some rough guidelines:

| Scenario                              | Approximate event rate |
|---------------------------------------|------------------------|
| Idle Workbench, basic tier            | 10-50 events/sec       |
| Active program, basic tier            | 100-500 events/sec     |
| Active program, detail tier           | 500-2,000 events/sec   |
| Directory scan (ExNext loop)          | 1,000-5,000 events/sec |
| Network I/O (send/recv enabled)       | 1,000-10,000 events/sec|
| Manual tier (AllocMem, GetMsg, etc.)  | 10,000+ events/sec     |

### Buffer Duration

At a given event rate, the buffer provides this much breathing room
before overflow:

```
duration = capacity / event_rate
```

With the default 8,192-entry buffer:

| Event rate     | Buffer duration |
|----------------|-----------------|
| 100 events/sec | ~82 seconds     |
| 1,000 events/sec | ~8 seconds   |
| 5,000 events/sec | ~1.6 seconds |
| 10,000 events/sec | <1 second   |

If you see overflow counts climbing in TRACE STATUS or the TUI status
bar, either increase the buffer capacity or reduce the number of enabled
functions.

### Overflow Behavior

When the ring buffer is full (write_pos + 1 == read_pos after wrapping),
the stub increments the `overflow` counter, calls Enable() to leave the
critical section, restores registers, and tail-calls the original
function normally. The traced function call still executes -- only the
trace event is lost.

Overflow events are reported in the TUI status bar and in
`TRACE STATUS` output. See [ring-buffer.md](ring-buffer.md) for the
full overflow protocol.


## Daemon Polling

The daemon (amigactld) polls the ring buffer on a fixed schedule. When
any trace session is active, the main event loop's WaitSelect timeout
drops from 1 second to 20 milliseconds:

```c
if (trace_any_active(&daemon)) {
    tv.tv_secs = 0;
    tv.tv_micro = 20000;  /* 20ms */
}
```

Each poll cycle reads up to 512 events from the ring buffer before
yielding. This batch limit prevents the trace consumer from starving
the daemon's other responsibilities (command processing, file
transfers, ARexx handling).

### Daemon-Side Caches

The daemon maintains several caches to avoid repeated system lookups
during event formatting:

| Cache          | Size       | Refresh interval              |
|----------------|------------|-------------------------------|
| Task names     | 64 entries | Every 20 polls (~400ms)       |
| Task history   | 32 entries | LRU, never expires            |
| Lock paths     | 128 entries | Populated on Lock, cleared on UnLock |
| File handle paths | 128 entries | Populated on Open, cleared on Close |
| Segment names  | 128 entries | Populated on LoadSeg          |
| DiskObject names | 128 entries | Populated on GetDiskObject, cleared on FreeDiskObject |

The task name cache uses event-driven refresh with a minimum gap of 3
polls between refreshes. A full refresh walks the exec task and ready
lists under Forbid/Permit, resolving CLI command names for Process
nodes.

### In-Flight Event Handling

When the daemon encounters an event with `valid=2` (in-progress -- the
original function has not yet returned), it waits up to 3 consecutive
poll cycles (~60ms at 20ms polling) for the post-call handler to set
`valid=1`. After 3 cycles, the event is consumed as-is to prevent ring
buffer stalls from blocking functions like RunCommand or Execute. Events
consumed in-flight have incomplete return value and IoErr data, which
the daemon handles by suppressing IoErr display.


## Network Bandwidth

Each formatted trace event produces approximately 80-200 bytes of text
on the TCP connection (DATA framing + formatted event line). The exact
size depends on function name length, argument formatting, and string
data.

| Event rate      | Approximate bandwidth |
|-----------------|------------------------|
| 100 events/sec  | 10-20 KB/sec           |
| 1,000 events/sec | 100-200 KB/sec        |
| 5,000 events/sec | 500 KB - 1 MB/sec     |

The DATA/END framing protocol transmits events individually (one DATA
line per event), so there is no batching optimization at the wire level.
Network bandwidth is rarely the bottleneck -- ring buffer overflow on
the Amiga side is the more common limiting factor.


## Reducing Overhead

### Use Output Tiers

The default Basic tier enables 57 of 99 functions. The remaining 42
functions are in Detail (13), Verbose (3), and Manual (26) tiers. Keep
the tier at Basic unless you need deeper visibility. See
[output-tiers.md](output-tiers.md) for tier membership.

### Disable Unused Functions

Individual functions can be disabled at install time or at runtime:

```
atrace_loader DISABLE OpenFont CloseFont
atrace_loader ENABLE AllocMem FreeMem
```

Disabled functions take the 7-instruction fast path on every call.

### Use Task Filtering (TRACE RUN)

TRACE RUN sets the stub-level task filter so that only the target
process's calls are captured. All other tasks hit the 18-instruction
filtered-out path, producing no events and consuming no ring buffer
slots. This dramatically reduces event volume and virtually eliminates
overflow for single-program debugging:

```
amigactl --host <ip> trace run -- List SYS:C
```

### Use the ERRORS Filter

The ERRORS filter causes the daemon to suppress non-error events before
transmission. This does not reduce stub overhead or ring buffer
consumption (all events are still captured), but it reduces network
traffic and client-side processing to only the events that indicate
failures.

### Manual-Tier Functions

The 26 Manual-tier functions (AllocMem, FreeMem, GetMsg, PutMsg, Wait,
Signal, and other high-frequency primitives) can generate tens of
thousands of events per second on an active system. These functions are
never auto-enabled. Enable them only with a task filter active, and
consider increasing the buffer capacity:

```
atrace_loader BUFSZ 32768
amigactl --host <ip> trace run --verbose -- MyProgram
```

### Capacity Planning Summary

| Use case                         | Recommended BUFSZ | Tier    |
|----------------------------------|---------------------|---------|
| Casual system monitoring         | 8,192 (default)     | Basic   |
| Debugging a specific program     | 8,192 (default)     | Basic   |
| Detailed I/O analysis            | 16,384              | Detail  |
| Memory allocation tracing        | 32,768              | Manual  |
| High-frequency IPC tracing       | 65,536              | Manual  |

For TRACE RUN sessions with a task filter, the default 8,192 is usually
sufficient even at Detail tier, because only one process's events enter
the ring buffer.
