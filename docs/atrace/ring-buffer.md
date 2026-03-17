# Ring Buffer Implementation

The ring buffer is the shared data structure that connects atrace stubs
(producers) to the amigactld daemon (consumer). Every traced library call
writes a 128-byte event into the ring buffer; the daemon reads events out
and streams them to connected clients. The buffer is a single contiguous
block of Amiga public memory, allocated once at install time and never
reallocated.

This document covers memory layout, allocation, the producer and consumer
protocols, overflow handling, and capacity configuration. For the
internal layout of each 128-byte event slot, see
[event-format.md](event-format.md). For how the ring buffer fits into
the overall system, see [architecture.md](architecture.md).


## Structure

The ring buffer consists of a 16-byte header (`struct atrace_ringbuf`)
immediately followed by an array of event entries. The header and entries
are allocated as a single contiguous block.

```
Offset  Size   Field       Type             Description
------  -----  ----------  ---------------  --------------------------------
0       4      capacity    ULONG            Number of event slots
4       4      write_pos   volatile ULONG   Next slot the producer will write
8       4      read_pos    volatile ULONG   Next slot the consumer will read
12      4      overflow    volatile ULONG   Dropped event counter
16      128*N  entries[]   atrace_event[]   Event slot array (N = capacity)
```

Total allocation size: `16 + (128 * capacity)` bytes.

The `write_pos`, `read_pos`, and `overflow` fields are declared
`volatile` because they are accessed by both interrupt-disabled stub code
(producers) and the daemon's polling loop (consumer) without any lock
beyond the Disable()/Enable() critical section around slot reservation.

A compile-time assertion in `atrace/main.c` enforces that the header is
exactly 16 bytes:

```c
typedef char assert_ringbuf_hdr[(sizeof(struct atrace_ringbuf) == 16) ? 1 : -1];
```

### Entries Array

The entries array begins at byte offset 16, immediately after the
header. Each entry is exactly 128 bytes (`ATRACE_EVENT_SIZE`), enforced
by a separate compile-time assertion:

```c
typedef char assert_event_size [(sizeof(struct atrace_event) == 128) ? 1 : -1];
```

The power-of-two entry size is a deliberate design choice. It allows
stubs to compute a slot's byte offset from its index using a single
68000 arithmetic shift instruction (`asl.l #7, d2`) rather than a
multiply. In the stub prefix at byte 126:

```asm
asl.l  #7, d2        ; slot_byte_offset = write_pos << 7
```

The stub then adds this offset to the precomputed entries base address to
obtain the target slot pointer.


## Allocation

The ring buffer is allocated by `ringbuf_alloc()` in `atrace/ringbuf.c`.
It performs a single `AllocMem()` call for the combined header and
entries array:

```c
alloc_size = sizeof(struct atrace_ringbuf) + ATRACE_EVENT_SIZE * capacity;
ring = AllocMem(alloc_size, MEMF_PUBLIC | MEMF_CLEAR);
```

Two memory flags are significant:

- **MEMF_PUBLIC**: The allocation survives the loader process's exit.
  The atrace_loader program runs, sets up all data structures, registers
  the named semaphore, and terminates. All allocated memory --
  including the ring buffer, anchor, patches, and stub code -- must
  persist after the loader's process context is freed. `MEMF_PUBLIC`
  ensures this.

- **MEMF_CLEAR**: All bytes are zeroed at allocation time. This means
  every event slot starts with `valid=0` (empty) and `overflow=0`,
  `write_pos=0`, `read_pos=0`. No explicit initialization loop is
  needed.

### Default Capacity

The default capacity is 8192 slots, defined as `ATRACE_DEFAULT_BUFSZ` in
`atrace/atrace.h`:

```c
#define ATRACE_DEFAULT_BUFSZ 8192
```

This produces a default allocation of `16 + (128 * 8192) = 1,048,592`
bytes, approximately 1 MB.

### Configuring Capacity

The capacity is configurable at load time through the `BUFSZ` argument
to `atrace_loader`:

```
atrace_loader BUFSZ 16384
```

The minimum accepted capacity is 16 slots. Values below 16 are silently
clamped upward. There is no defined maximum, but the allocation must
fit in available Amiga memory. The memory formula for planning:

```
total_bytes = 16 + (128 * capacity)
```

| Capacity | Memory      |
|----------|-------------|
| 16       | 2,064 B     |
| 1024     | 131,088 B   |
| 8192     | 1,048,592 B |
| 16384    | 2,097,168 B |
| 65536    | 8,388,624 B |

Once allocated, the capacity cannot be changed without unloading
(`atrace_loader QUIT`) and reinstalling atrace.


## Producer Protocol (Stubs)

Every patched library function has a generated stub that acts as a
ring buffer producer. The critical slot reservation is implemented in 68000
machine code in the stub prefix (bytes 60--125 of `stub_gen.c`'s
`stub_prefix[]` template). It runs on the calling task's context --
any AmigaOS task or process that calls a traced library function.

### Slot Reservation

Slot reservation is the critical operation that claims the next available
ring buffer entry. It runs inside a Disable()/Enable() critical section
that inhibits all interrupts and task switching on the 68000, providing
atomicity without needing a mutex or semaphore.

The sequence within the critical section:

1. **Disable interrupts**: `jsr _LVODisable(a6)` via SysBase.
2. **Read write_pos**: Load the current producer position from
   `ring->write_pos` into d0.
3. **Compute new_pos**: `d1 = write_pos + 1`. If `d1 >= capacity`,
   wrap to 0 (the `bcs.s .nowrap` / `moveq #0, d1` pair handles this).
4. **Full check**: Compare `new_pos` against `ring->read_pos`. If equal,
   the buffer is full -- branch to the overflow path.
5. **Commit write_pos**: Store `new_pos` back to `ring->write_pos`.
6. **Increment use_count**: `patch->use_count += 1`. This tracks
   in-flight stubs for safe disable/quiesce.
7. **Read and increment sequence**: Load `anchor->event_sequence` into
   d1, then increment it in place. The pre-increment value becomes
   this event's sequence number.
8. **Enable interrupts**: `jsr _LVOEnable(a6)`.

The entire reservation (steps 2--7) executes under Disable(), so no
other task or interrupt handler can interleave. On a single-processor
68000, this guarantees that `write_pos` advances atomically and no two
stubs claim the same slot.

### Event Fill

After Enable(), the stub holds the reserved slot index in d2 and the
sequence number in d3. It computes the slot address:

```asm
asl.l  #7, d2                     ; byte offset = index << 7
movea.l #RING_ENTRIES_ADDR, a5     ; entries base (patched at install)
adda.l  d2, a5                     ; a5 = pointer to this event slot
```

The stub then fills the event fields in order: EClock timestamp,
sequence number, lib_id, lvo_offset, caller_task, function arguments,
arg_count, flags, string data, and task name. All of this happens
outside the critical section -- the slot is exclusively owned by this
stub because no other producer can claim it (write_pos has already
advanced past it) and the consumer will not read it until `valid`
becomes non-zero.

### Setting valid=2 (In-Progress)

As the final step before calling the original library function, the
stub sets `valid=2`:

```asm
move.b  #2, (a5)     ; entry->valid = 2
```

The value 2 means "in-progress" -- the event's pre-call fields (sequence,
arguments, timestamps, task name) are filled, but `retval`, `ioerr`, and
`FLAG_HAS_IOERR` are not yet written because the original function has
not returned.

This is set *before* the trampoline calls the original function, not
after. The reason is blocking functions. A call like `dos.RunCommand` or
`dos.Execute` may not return for seconds or minutes. If the slot had
`valid=0` during that time, the consumer could never advance past it,
freezing all event consumption system-wide (the ring buffer would fill
and every new event would overflow). With `valid=2`, the consumer knows
the slot is occupied and can eventually consume it, even without complete
post-call data.

### Setting valid=1 (Complete)

After the original function returns, the stub's post-call handler in the
suffix writes the return value to `entry->retval`, optionally captures
IoErr for dos.library failures, and then sets `valid=1`:

```asm
move.b  #1, (a0)     ; entry->valid = 1
```

If the daemon has already consumed the event while it was `valid=2`, this
write is harmless -- the daemon has already advanced `read_pos` past this
slot, and the slot will be reused by a future producer.


## Consumer Protocol (Daemon)

The daemon is the sole consumer of ring buffer events. There is no
support for multiple consumers -- only one daemon process reads the
buffer. The consumption logic lives in `trace_poll_events()` in
`daemon/trace.c`.

### Poll Timing

The daemon's main event loop uses `WaitSelect()` with a 20ms timeout
when tracing is active (1-second timeout otherwise). Each iteration
calls `trace_poll_events()` if any client has an active trace session.
This means the consumer polls the ring buffer approximately 50 times per
second under ideal conditions, though actual frequency depends on network
activity and processing load.

### Consumption Loop

Within a single poll, the consumer processes events until 512 have been
sent to at least one subscriber. Skipped events (self-filtered,
tier-suppressed, per-client filtered) do not count toward this limit,
preventing a full buffer of irrelevant events from blocking real events
across many poll cycles.

The loop walks forward from `read_pos`:

1. **Bounds check**: If `read_pos >= capacity`, the position is
   corrupted. Reset `read_pos` to `write_pos` and abort the poll.
2. **Read valid flag**: Check `entries[pos].valid`.
   - `valid=0`: Slot is empty. The loop terminates -- there are no more
     events to consume (the producer has not yet written to this slot).
   - `valid=1`: Complete event. Consume it normally.
   - `valid=2`: In-progress event. Handle via the INFLIGHT_PATIENCE
     mechanism (see below).
3. **Self-filter**: Skip events from the daemon's own task
   (`caller_task == g_daemon_task`) to prevent feedback loops.
4. **Content filter**: Apply tier-based suppression (e.g., successful
   `OpenLibrary` with version 0 at Basic tier, successful `Lock` calls
   at Basic tier).
5. **Format and broadcast**: Format the event into a text line and send
   it to all connected tracing clients that pass per-client filters
   (library, function, process name, errors-only).
6. **Release slot**: Set `ev->valid = 0`, clearing the slot for reuse
   by future producers.
7. **Advance read_pos**: `pos = (pos + 1) % capacity`, then write back
   to `ring->read_pos`.
8. **Increment events_consumed**: After the loop, add the total
   consumed count to `anchor->events_consumed`.

### Semaphore Protection

Before accessing the ring buffer, the consumer obtains the anchor's
`SignalSemaphore` in shared mode via `AttemptSemaphoreShared()`. This
is not for producer/consumer synchronization (that is handled by the
valid flag protocol) -- it protects against `atrace_loader QUIT`
freeing the ring buffer while the daemon is reading it. The QUIT
command obtains the semaphore exclusively, so the daemon's shared obtain
will fail if QUIT is in progress, allowing the daemon to detect shutdown.

The semaphore is released at the end of each poll cycle via
`ReleaseSemaphore()`.


## The INFLIGHT_PATIENCE Mechanism

When the consumer encounters a slot with `valid=2`, it faces a dilemma.
For non-blocking functions, the post-call handler sets `valid=1` within
microseconds -- waiting one poll cycle lets the function complete so the
consumer gets full data (retval, ioerr). But for blocking functions like
`RunCommand` or `Execute`, `valid=2` can persist for seconds or longer.

The INFLIGHT_PATIENCE mechanism resolves this with a counter-based
patience system:

```c
#define INFLIGHT_PATIENCE 3
```

The consumer tracks the position and count of consecutive stalls:

- **First encounter** at a given position: Record the position in
  `g_inflight_stall_pos`, set `g_inflight_stall_count = 1`, and break
  out of the consumption loop (wait for next poll).
- **Subsequent encounters** at the same position: Increment the stall
  count. If `g_inflight_stall_count < INFLIGHT_PATIENCE`, break again.
- **Patience exhausted** (count reaches 3): Consume the event as-is.
  The retval field may contain stale or zero data, and IoErr is not
  available. The daemon's formatting logic handles this gracefully --
  it checks `ev->valid == 1` before displaying IoErr data.

With the daemon's 20ms WaitSelect timeout, 3 stalls represent
approximately 60ms of waiting. This is enough time for virtually all non-blocking functions
to complete, while keeping blocking functions from indefinitely stalling
the consumer.

When the consumer advances past a stalled position (whether by consuming
it or by finding that a previously stalled slot is now `valid=1`), it
resets the stall tracking:

```c
g_inflight_stall_pos = 0xFFFFFFFF;
g_inflight_stall_count = 0;
```

This reset is important for correctness: without it, a future event
landing in the same ring buffer slot would falsely inherit the stall
count from a previous wrap-around.


## Overflow Handling

When a stub's slot reservation finds the buffer full (`new_pos ==
read_pos`), it takes the overflow path instead of the normal event
recording path.

### Producer-Side Overflow

The overflow path in the stub suffix (bytes 104--125):

1. **Increment overflow counter**: `addq.l #1, ring->overflow`. This
   happens while still inside the Disable()/Enable() critical section,
   so the increment is atomic with respect to the full-check.
2. **Enable interrupts**: `jsr _LVOEnable(a6)`.
3. **Restore registers**: `movem.l (sp)+, d0-d7/a0-a4/a6` followed by
   restoring a5 from the stack.
4. **Tail-call original**: Push the original function address and `rts`,
   executing the library function with no tracing. The event is lost.

The overflow path is carefully designed to have zero side effects beyond
incrementing the counter. The original function still executes normally --
only the trace event is dropped. `patch->use_count` is not incremented
(the stub never got past the reservation stage), so the DISABLE drain
logic is not affected.

### Consumer-Side Overflow Reporting

At the end of each poll cycle, the consumer checks `ring->overflow`:

```c
if (ring->overflow > 0) {
    Disable();
    ov = ring->overflow;
    ring->overflow = 0;
    Enable();
    g_events_dropped += ov;
    /* Format and broadcast overflow notification */
}
```

The read-and-clear of the overflow counter uses Disable()/Enable()
because in-flight stubs may be incrementing it concurrently. The
consumer accumulates the total in `g_events_dropped` and sends an
`# OVERFLOW <n> events dropped` notification to all connected tracing
clients. (The exception is `drain_stale_events()`, which reads and
clears the overflow counter under Forbid() rather than Disable/Enable,
since it runs during session setup when stubs are already quiesced.)

Overflow is not an error condition in the protocol sense -- it is an
expected consequence of sustained high event rates exceeding the
consumer's throughput. The client displays the overflow count in its
status bar so the user can decide whether to increase the buffer size
or reduce the number of enabled functions.


## Stale Event Draining

When a new trace session starts (TRACE START or TRACE RUN), the ring
buffer may contain events from a prior session or from background system
activity between sessions. The `drain_stale_events()` function clears
this stale data.

Called under `Forbid()`, it performs four operations:

1. **Advance read_pos**: Sets `read_pos = write_pos`, logically
   discarding all buffered events. The count of discarded events is
   added to `events_consumed`.

2. **Clear overflow counter**: Any accumulated overflow from the gap
   between sessions is absorbed into `g_events_dropped`.

3. **Clear all slot metadata**: Iterates over every slot in the buffer,
   setting `valid=0`, `retval=0`, `ioerr=0`, and `flags=0`. This is
   necessary because stubs set `valid=2` before calling the original
   function. If a slot contained `valid=2` from a prior session, the
   consumer would see it as an in-progress event and attempt to process
   stale field values. Clearing ensures those values are zero rather
   than misleading remnants from prior ring buffer usage.

4. **Reset stall tracking**: Sets `g_inflight_stall_pos = 0xFFFFFFFF`
   and `g_inflight_stall_count = 0`. Without this, stale stall state
   from a prior session could cause the first `valid=2` event in the
   new session to be consumed prematurely if it happens to land at the
   same ring position as the old stall.

The TRACE DISABLE command performs a similar drain: it advances
`read_pos` to `write_pos`, clears valid flags on occupied slots, and
accumulates the overflow counter. This prevents the buffer from
remaining full after disable, which would cause immediate overflow when
tracing is re-enabled.


## Design Constraints and Limitations

### Single Consumer

The ring buffer supports exactly one consumer (the daemon process).
The `read_pos` field is written only by the daemon. There is no
mechanism for multiple consumers to coordinate, and adding one would
require a fundamentally different design (per-consumer cursors or a
publish-subscribe model).

### No Per-Slot Locking

Individual slots have no lock or ownership flag beyond the `valid` byte.
The protocol relies on the invariant that a slot between `read_pos` and
`write_pos` is exclusively owned by either the producing stub (while
`valid=0` or being filled) or the consuming daemon (after `valid` becomes
non-zero and before the daemon clears it back to 0). This works because
`write_pos` advances atomically under Disable() and the consumer only
clears `valid` after finishing with the slot.

### Wrapping Arithmetic

Position values (`write_pos`, `read_pos`) are slot indices in the range
`[0, capacity)`, not byte offsets. Advancement uses modular arithmetic:
`(pos + 1) % capacity`. The buffer is considered full when `new_pos ==
read_pos` (one slot is always left empty to distinguish full from empty).
This means the usable capacity is `capacity - 1` slots.

### No Backpressure

Producers never block. If the buffer is full, the event is silently
dropped and the overflow counter is incremented. This is a deliberate
design choice: stubs run in the context of the calling task, potentially
at interrupt time or inside Forbid() sections. Blocking would risk
deadlocks or system hangs. The tradeoff is that sustained event rates
above the consumer's throughput cause data loss rather than slowdown.

### Memory Lifetime

The ring buffer is allocated with `MEMF_PUBLIC` and is never freed during
normal operation. `atrace_loader QUIT` frees it explicitly after
disabling tracing and draining all in-flight stubs. If the system is
rebooted without running QUIT, the memory is recovered by the OS reset.
There is no mechanism to resize the buffer without a full
unload/reinstall cycle.
