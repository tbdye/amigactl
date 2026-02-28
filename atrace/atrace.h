/*
 * atrace -- shared structures for atrace resident module and daemon
 *
 * Included by both atrace/main.c and daemon/trace.c.
 */

#ifndef ATRACE_H
#define ATRACE_H

#include <exec/types.h>
#include <exec/semaphores.h>

/* ---- Constants ---- */

#define ATRACE_MAGIC        0x41545243  /* 'ATRC' */
#define ATRACE_VERSION      1
#define ATRACE_SEM_NAME     "atrace_patches"
#define ATRACE_DEFAULT_BUFSZ 8192

/* Library IDs (index into lib_info table) */
#define LIB_EXEC    0

/* Event entry size -- must be 64 bytes for shift-based indexing */
#define ATRACE_EVENT_SIZE   64

/* ---- struct atrace_anchor ----
 *
 * Top-level structure, found via named semaphore.
 *
 * Byte offsets (explicit sem_padding field aligns magic to
 * 4-byte boundary after 46-byte SignalSemaphore):
 *
 *   sem:              offset   0,  46 bytes (SignalSemaphore)
 *   sem_padding:      offset  46,   2 bytes (alignment to 4-byte boundary)
 *   magic:            offset  48,   4 bytes (ULONG)
 *   version:          offset  52,   2 bytes (UWORD)
 *   flags:            offset  54,   2 bytes (UWORD)
 *   global_enable:    offset  56,   4 bytes (ULONG)
 *   ring:             offset  60,   4 bytes (APTR)
 *   patch_count:      offset  64,   2 bytes (UWORD)
 *   padding1:         offset  66,   2 bytes
 *   patches:          offset  68,   4 bytes (APTR)
 *   event_sequence:   offset  72,   4 bytes (ULONG)
 *   events_consumed:  offset  76,   4 bytes (ULONG)
 *   Total: 80 bytes
 */
struct atrace_anchor {
    struct SignalSemaphore sem;
    UWORD sem_padding;            /* Alignment padding (46 -> 48 for ULONG) */
    ULONG magic;
    UWORD version;
    UWORD flags;
    volatile ULONG global_enable;
    struct atrace_ringbuf *ring;
    UWORD patch_count;
    UWORD padding1;
    struct atrace_patch *patches;
    volatile ULONG event_sequence;
    volatile ULONG events_consumed;
};

/* ---- struct atrace_ringbuf ----
 *
 *   capacity:   offset  0,  4 bytes (ULONG)
 *   write_pos:  offset  4,  4 bytes (volatile ULONG)
 *   read_pos:   offset  8,  4 bytes (volatile ULONG)
 *   overflow:   offset 12,  4 bytes (volatile ULONG)
 *   Total header: 16 bytes
 *   Followed by entries[capacity] at offset 16.
 */
struct atrace_ringbuf {
    ULONG capacity;
    volatile ULONG write_pos;
    volatile ULONG read_pos;
    volatile ULONG overflow;
    /* struct atrace_event entries[] follows in memory */
};

/* ---- struct atrace_event ----
 *
 *   valid:        offset  0,  1 byte  (volatile UBYTE)
 *   lib_id:       offset  1,  1 byte  (UBYTE)
 *   lvo_offset:   offset  2,  2 bytes (WORD)
 *   sequence:     offset  4,  4 bytes (ULONG)
 *   caller_task:  offset  8,  4 bytes (APTR)
 *   args[4]:      offset 12, 16 bytes (ULONG * 4)
 *   retval:       offset 28,  4 bytes (ULONG)
 *   arg_count:    offset 32,  1 byte  (UBYTE)
 *   padding:      offset 33,  1 byte  (UBYTE)
 *   string_data:  offset 34, 24 bytes (char[24])
 *   reserved:     offset 58,  6 bytes (UBYTE[6])
 *   Total: 64 bytes
 */
struct atrace_event {
    volatile UBYTE valid;
    UBYTE lib_id;
    WORD  lvo_offset;
    ULONG sequence;
    APTR  caller_task;
    ULONG args[4];
    ULONG retval;
    UBYTE arg_count;
    UBYTE padding;
    char  string_data[24];
    UBYTE reserved[6];
};

/* ---- struct atrace_patch ----
 *
 *   lib_id:       offset  0,  1 byte  (UBYTE)
 *   padding0:     offset  1,  1 byte  (UBYTE)
 *   lvo_offset:   offset  2,  2 bytes (WORD)
 *   func_id:      offset  4,  2 bytes (UWORD)
 *   arg_count:    offset  6,  2 bytes (UWORD)
 *   enabled:      offset  8,  4 bytes (ULONG)
 *   use_count:    offset 12,  4 bytes (ULONG)
 *   original:     offset 16,  4 bytes (APTR)
 *   stub_code:    offset 20,  4 bytes (APTR)
 *   stub_size:    offset 24,  4 bytes (ULONG)
 *   arg_regs[8]:  offset 28,  8 bytes (UBYTE * 8)
 *   string_args:  offset 36,  1 byte  (UBYTE)
 *   padding[3]:   offset 37,  3 bytes
 *   Total: 40 bytes
 */
struct atrace_patch {
    UBYTE lib_id;
    UBYTE padding0;
    WORD  lvo_offset;
    UWORD func_id;
    UWORD arg_count;
    volatile ULONG enabled;
    volatile ULONG use_count;
    APTR  original;
    APTR  stub_code;
    ULONG stub_size;
    UBYTE arg_regs[8];
    UBYTE string_args;
    UBYTE padding_end[3];
};

/* ---- struct func_info ----
 *
 *   name:         offset  0,  4 bytes (const char *)
 *   lvo_offset:   offset  4,  2 bytes (WORD)
 *   arg_count:    offset  6,  1 byte  (UBYTE)
 *   arg_regs[8]:  offset  7,  8 bytes (UBYTE * 8)
 *   ret_reg:      offset 15,  1 byte  (UBYTE)
 *   string_args:  offset 16,  1 byte  (UBYTE)
 *   padding:      offset 17,  1 byte  (UBYTE)
 *   Total: 18 bytes
 */
struct func_info {
    const char *name;
    WORD lvo_offset;
    UBYTE arg_count;
    UBYTE arg_regs[8];
    UBYTE ret_reg;
    UBYTE string_args;
    UBYTE padding;
};

/* ---- struct lib_info ----
 *
 *   name:         offset  0,  4 bytes (const char *)
 *   funcs:        offset  4,  4 bytes (struct func_info *)
 *   func_count:   offset  8,  2 bytes (UWORD)
 *   lib_id:       offset 10,  1 byte  (UBYTE)
 *   padding:      offset 11,  1 byte  (UBYTE)
 *   Total: 12 bytes
 */
struct lib_info {
    const char *name;
    struct func_info *funcs;
    UWORD func_count;
    UBYTE lib_id;
    UBYTE padding;
};

/* ---- Register encoding ----
 *
 * Register indices used in arg_regs[]:
 *   0=d0, 1=d1, 2=d2, 3=d3, 4=d4, 5=d5, 6=d6, 7=d7,
 *   8=a0, 9=a1, 10=a2, 11=a3, 12=a4, 13=a5, 14=a6, 15=a7
 *
 * MOVEM frame offsets (d0-d7/a0-a4/a6 = 14 regs, 56 bytes):
 *   d0= 0, d1= 4, d2= 8, d3=12, d4=16, d5=20, d6=24, d7=28,
 *   a0=32, a1=36, a2=40, a3=44, a4=48, a6=52
 *
 * Note: a5 is NOT in the MOVEM frame (it is saved separately).
 *       a6 is at frame offset 52 (after a4, skipping a5).
 */
#define REG_D0  0
#define REG_D1  1
#define REG_D2  2
#define REG_D3  3
#define REG_D4  4
#define REG_D5  5
#define REG_D6  6
#define REG_D7  7
#define REG_A0  8
#define REG_A1  9
#define REG_A2 10
#define REG_A3 11
#define REG_A4 12
#define REG_A5 13
#define REG_A6 14

/* Map register index to MOVEM frame offset (bytes from sp).
 * a5 is not in the frame -- returns -1 as sentinel. */
static __inline WORD reg_to_frame_offset(UBYTE reg)
{
    /* d0-d7 are at offsets 0,4,8,...,28 */
    if (reg <= REG_D7)
        return (WORD)(reg * 4);
    /* a0-a4 are at offsets 32,36,40,44,48 */
    if (reg >= REG_A0 && reg <= REG_A4)
        return (WORD)(32 + (reg - REG_A0) * 4);
    /* a6 is at offset 52 */
    if (reg == REG_A6)
        return 52;
    /* a5 is not in the MOVEM frame */
    return -1;
}

/* ---- External declarations for funcs.c ---- */

extern struct lib_info atrace_libs[];
extern int atrace_lib_count;

#endif /* ATRACE_H */
