/*
 * atrace -- ring buffer allocation and initialization
 */

#include "atrace.h"

#include <proto/exec.h>

/* Allocate and initialize a ring buffer with the given capacity.
 * Returns pointer to the ringbuf header, or NULL on failure.
 *
 * The allocation is a single contiguous block:
 *   [struct atrace_ringbuf header (16 bytes)]
 *   [struct atrace_event entries[capacity] (64 * capacity bytes)]
 *
 * Total size: 16 + 64 * capacity.
 *
 * MEMF_PUBLIC: survives loader process exit.
 * MEMF_CLEAR:  all entries start with valid=0.
 */
struct atrace_ringbuf *ringbuf_alloc(ULONG capacity)
{
    ULONG alloc_size;
    struct atrace_ringbuf *ring;

    alloc_size = sizeof(struct atrace_ringbuf) + ATRACE_EVENT_SIZE * capacity;

    ring = (struct atrace_ringbuf *)AllocMem(alloc_size,
                                              MEMF_PUBLIC | MEMF_CLEAR);
    if (!ring)
        return NULL;

    ring->capacity = capacity;
    /* write_pos, read_pos, overflow are all 0 from MEMF_CLEAR */

    return ring;
}
