/*
 * atrace -- stub template and generalized code generator
 *
 * Phase 2: parameterized code generator that emits per-function
 * argument copy and string capture instructions based on metadata
 * from the patch descriptor.
 *
 * The stub consists of three regions:
 *   1. Prefix (168 bytes): fast-path checks, task filter, register save,
 *      ring buffer slot reservation, event header fields. Identical for
 *      all functions.
 *   2. Variable region: per-function argument copy, arg_count immediate,
 *      and optional string capture. Size varies by function.
 *   3. Suffix (86 bytes): MOVEM restore, trampoline, post-call handler,
 *      disabled path, overflow path. Identical for all functions except
 *      that byte offsets shift based on variable region size.
 */

#include "atrace.h"

#include <proto/exec.h>

#include <string.h>
#include <stddef.h>  /* offsetof */

/*
 * Prefix template -- bytes 0-167, 84 UWORD values.
 * Identical for all patched functions.
 *
 * Phase 4: 26-byte task filter check inserted at bytes 30-55,
 * shifting the MOVEM and all subsequent instructions by +26 bytes
 * relative to the Phase 3 prefix (142 -> 168 bytes).
 *
 * Contains placeholder 0x0000 values at PATCH_ADDR, ANCHOR_ADDR,
 * RING_ENTRIES_ADDR, struct displacement, and branch displacement slots.
 */
static const UWORD stub_prefix[] = {
    /* === Fast path checks === */
    /*  0: */ 0x2F0D,                   /* move.l a5, -(sp)                     */
    /*  2: */ 0x2A7C, 0x0000, 0x0000,   /* movea.l #PATCH_ADDR, a5   [1]       */
    /*  8: */ 0x4AAD, 0x0000,           /* tst.l OFS_ENABLED(a5)               */
    /* 12: */ 0x6700, 0x0000,           /* beq.w .disabled                      */
    /* 16: */ 0x2A7C, 0x0000, 0x0000,   /* movea.l #ANCHOR_ADDR, a5            */
    /* 22: */ 0x4AAD, 0x0000,           /* tst.l OFS_GLOBAL_ENABLE(a5)         */
    /* 26: */ 0x6700, 0x0000,           /* beq.w .disabled                      */

    /* === Phase 4: Task filter check === */
    /* 30: */ 0x4AAD, 0x0000,           /* tst.l OFS_FILTER_TASK(a5)           */
    /* 34: */ 0x6714,                   /* beq.s .no_filter (+20)               */
    /* 36: */ 0x2F0E,                   /* move.l a6, -(sp)                     */
    /* 38: */ 0x2C78, 0x0004,           /* movea.l 4.w, a6  (SysBase)          */
    /* 42: */ 0x2C6E, 0x0114,           /* movea.l 276(a6), a6 (ThisTask)      */
    /* 46: */ 0xBDED, 0x0000,           /* cmpa.l OFS_FILTER_TASK(a5), a6      */
    /* 50: */ 0x2C5F,                   /* movea.l (sp)+, a6  (restore)        */
    /* 52: */ 0x6600, 0x0000,           /* bne.w .disabled  (mismatch)         */
    /* .no_filter: */

    /* === Save all volatile registers === */
    /* 56: */ 0x48E7, 0xFFFA,           /* movem.l d0-d7/a0-a4/a6, -(sp)       */

    /* === Ring buffer slot reservation === */
    /* 60: */ 0x2C78, 0x0004,           /* movea.l 4.w, a6  (SysBase)          */
    /* 64: */ 0x4EAE, 0xFF88,           /* jsr _LVODisable(a6)  = -120         */
    /* 68: */ 0x206D, 0x0000,           /* movea.l OFS_RING(a5), a0            */
    /* 72: */ 0x2028, 0x0000,           /* move.l OFS_WRITE_POS(a0), d0        */
    /* 76: */ 0x2200,                   /* move.l d0, d1                        */
    /* 78: */ 0x5281,                   /* addq.l #1, d1                        */
    /* 80: */ 0xB2A8, 0x0000,           /* cmp.l OFS_CAPACITY(a0), d1          */
    /* 84: */ 0x6502,                   /* bcs.s .nowrap (+2)                   */
    /* 86: */ 0x7200,                   /* moveq #0, d1                         */
    /* .nowrap: */
    /* 88: */ 0xB2A8, 0x0000,           /* cmp.l OFS_READ_POS(a0), d1          */
    /* 92: */ 0x6700, 0x0000,           /* beq.w .overflow                      */
    /* 96: */ 0x2141, 0x0000,           /* move.l d1, OFS_WRITE_POS(a0)        */
    /*100: */ 0x207C, 0x0000, 0x0000,   /* movea.l #PATCH_ADDR, a0   [2]       */
    /*106: */ 0x52A8, 0x0000,           /* addq.l #1, OFS_USE_COUNT(a0)        */
    /*110: */ 0x222D, 0x0000,           /* move.l OFS_EVENT_SEQ(a5), d1        */
    /*114: */ 0x52AD, 0x0000,           /* addq.l #1, OFS_EVENT_SEQ(a5)        */
    /*118: */ 0x2400,                   /* move.l d0, d2                        */
    /*120: */ 0x2601,                   /* move.l d1, d3                        */
    /*122: */ 0x4EAE, 0xFF82,           /* jsr _LVOEnable(a6)  = -126          */

    /* === Fill event entry === */
    /*126: */ 0xED82,                   /* asl.l #6, d2                         */
    /*128: */ 0x2A7C, 0x0000, 0x0000,   /* movea.l #RING_ENTRIES_ADDR, a5      */
    /*134: */ 0xDBC2,                   /* adda.l d2, a5                        */
    /*136: */ 0x2B43, 0x0004,           /* move.l d3, 4(a5)  entry->sequence   */
    /*140: */ 0x207C, 0x0000, 0x0000,   /* movea.l #PATCH_ADDR, a0   [3]       */
    /*146: */ 0x1B68, 0x0000, 0x0001,   /* move.b 0(a0), 1(a5)  lib_id        */
    /*152: */ 0x3B68, 0x0002, 0x0002,   /* move.w 2(a0), 2(a5)  lvo_offset    */
    /*158: */ 0x2C78, 0x0004,           /* movea.l 4.w, a6  (SysBase)          */
    /*162: */ 0x2B6E, 0x0114, 0x0008,   /* move.l 276(a6), 8(a5) caller_task  */
};

#define STUB_PREFIX_BYTES  168  /* 84 words */

/*
 * Suffix template -- MOVEM restore, trampoline construction,
 * post-call handler, disabled path, and overflow path.
 * 43 UWORD values, 86 bytes.
 *
 * The trampoline uses a stack-based approach to pass the entry pointer
 * (a5) through the original function call WITHOUT clobbering a0.
 * After MOVEM restore, saved_a5 is on top of stack:
 *   1. Duplicate saved_a5 lower on the stack
 *   2. Overwrite original saved_a5 slot with entry pointer (a5)
 *   3. Pop the duplicate to restore a5
 * This leaves entry_ptr on the stack, accessible after the original
 * function returns via .post_call.
 *
 * All byte offsets below are suffix-relative (0 = first byte of suffix).
 */
static const UWORD stub_suffix[] = {
    /* === MOVEM restore + trampoline === */
    /*  0: */ 0x4CDF, 0x5FFF,           /* movem.l (sp)+, d0-d7/a0-a4/a6       */
    /*  4: */ 0x2F17,                   /* move.l (sp), -(sp)   dup saved_a5    */
    /*  6: */ 0x2F4D, 0x0004,           /* move.l a5, 4(sp)     entry ptr       */
    /* 10: */ 0x2A5F,                   /* movea.l (sp)+, a5    restore a5      */
    /* 12: */ 0x487A, 0x000A,           /* pea 10(pc)           push .post_call */
    /* 16: */ 0x2F3C, 0x0000, 0x0000,   /* move.l #ORIG_ADDR, -(sp)  [1]       */
    /* 22: */ 0x4E75,                   /* rts                  jump to original*/

    /* === Post-call handler === */
    /* .post_call: (suffix offset 24) */
    /* 24: */ 0x2F00,                   /* move.l d0, -(sp)     save retval     */
    /* 26: */ 0x206F, 0x0004,           /* movea.l 4(sp), a0    entry ptr       */
    /* 30: */ 0x2140, 0x001C,           /* move.l d0, 28(a0)    entry->retval   */
    /* 34: */ 0x10BC, 0x0001,           /* move.b #1, (a0)      entry->valid=1  */
    /* 38: */ 0x207C, 0x0000, 0x0000,   /* movea.l #PATCH_ADDR, a0  [4]        */
    /* 44: */ 0x53A8, 0x0000,           /* subq.l #1, OFS_USE_COUNT(a0)        */
    /* 48: */ 0x201F,                   /* move.l (sp)+, d0     restore retval  */
    /* 50: */ 0x588F,                   /* addq.l #4, sp        pop entry ptr   */
    /* 52: */ 0x4E75,                   /* rts                  return to caller*/

    /* === DISABLED fast path === */
    /* .disabled: (suffix offset 54) */
    /* 54: */ 0x2A5F,                   /* movea.l (sp)+, a5    restore a5      */
    /* 56: */ 0x2F3C, 0x0000, 0x0000,   /* move.l #ORIG_ADDR, -(sp)  [2]       */
    /* 62: */ 0x4E75,                   /* rts                  tail-call orig  */

    /* === OVERFLOW path === */
    /* .overflow: (suffix offset 64) */
    /* 64: */ 0x52A8, 0x0000,           /* addq.l #1, OFS_OVERFLOW(a0)         */
    /* 68: */ 0x4EAE, 0xFF82,           /* jsr _LVOEnable(a6)                  */
    /* 72: */ 0x4CDF, 0x5FFF,           /* movem.l (sp)+, d0-d7/a0-a4/a6       */
    /* 76: */ 0x2A5F,                   /* movea.l (sp)+, a5    restore a5      */
    /* 78: */ 0x2F3C, 0x0000, 0x0000,   /* move.l #ORIG_ADDR, -(sp)  [3]       */
    /* 84: */ 0x4E75,                   /* rts                  tail-call orig  */
};

#define STUB_SUFFIX_BYTES   86  /* 43 words */

/* ---- Suffix-relative byte offsets ---- */

/* PATCH_ADDR occurrence 4 (high word of address in suffix) */
#define PATCH_SUFFIX_REL            40

/* Struct field displacement patches within the suffix */
#define SUFFIX_DISP_USE_COUNT_DEC   46   /* subq.l #1, OFS_USE_COUNT(a0) */
#define SUFFIX_DISP_OVERFLOW        66   /* addq.l #1, OFS_OVERFLOW(a0)  */

/* Label offsets within the suffix (used for branch displacement calc) */
#define SUFFIX_LABEL_DISABLED       54   /* .disabled label */
#define SUFFIX_LABEL_OVERFLOW       64   /* .overflow label */

/* ORIG_ADDR occurrences (suffix-relative high word offsets) */
#define ORIG_SUFFIX_REL_1           18   /* trampoline push   */
#define ORIG_SUFFIX_REL_2           58   /* .disabled push    */
#define ORIG_SUFFIX_REL_3           80   /* .overflow push    */

/* ---- Prefix address byte offsets (high word of each 32-bit address) ---- */

/* PATCH_ADDR -- 3 occurrences in prefix */
#define PATCH_OFF_1     4    /* per-patch enable check                  */
#define PATCH_OFF_2   102    /* use_count increment                     */
#define PATCH_OFF_3   142    /* lib_id/lvo_offset copy                  */

/* ANCHOR_ADDR -- 1 occurrence in prefix */
#define ANCHOR_OFF_1   18    /* global enable check                     */

/* RING_ENTRIES_ADDR -- 1 occurrence in prefix */
#define ENTRIES_OFF_1  130   /* entry base address                      */

/* ---- Prefix struct field displacement patches ---- */

/* atrace_patch field displacements */
#define DISP_ENABLED       10   /* tst.l OFS_ENABLED(a5): word at byte 10  */
#define DISP_USE_COUNT_INC 108  /* addq.l #1, OFS_USE_COUNT(a0): byte 108  */

/* atrace_anchor field displacements */
#define DISP_GLOBAL_ENABLE 24   /* tst.l OFS_GLOBAL_ENABLE(a5): byte 24    */
#define DISP_FILTER_TASK_1 32   /* tst.l OFS_FILTER_TASK(a5): byte 32      */
#define DISP_FILTER_TASK_2 48   /* cmpa.l OFS_FILTER_TASK(a5), a6: byte 48 */
#define DISP_RING          70   /* movea.l OFS_RING(a5), a0: byte 70       */
#define DISP_EVENT_SEQ_RD 112   /* move.l OFS_EVENT_SEQ(a5), d1: byte 112  */
#define DISP_EVENT_SEQ_WR 116   /* addq.l #1, OFS_EVENT_SEQ(a5): byte 116  */

/* atrace_ringbuf field displacements */
#define DISP_WRITE_POS_RD  74   /* move.l OFS_WRITE_POS(a0), d0: byte 74   */
#define DISP_CAPACITY      82   /* cmp.l OFS_CAPACITY(a0), d1: byte 82     */
#define DISP_READ_POS      90   /* cmp.l OFS_READ_POS(a0), d1: byte 90     */
#define DISP_WRITE_POS_WR  98   /* move.l d1, OFS_WRITE_POS(a0): byte 98   */

/* ---- Branch displacement byte offsets (word containing displacement) ---- */

#define BEQ_DISABLED_1     14   /* beq.w .disabled at prefix byte 12 */
#define BEQ_DISABLED_2     28   /* beq.w .disabled at prefix byte 26 */
#define BNE_DISABLED_3     54   /* bne.w .disabled at prefix byte 52 */
#define BEQ_OVERFLOW       94   /* beq.w .overflow at prefix byte 92 */

/* ---- Helper: patch a 32-bit address into the stub ---- */

static void patch_addr(UWORD *stub, int byte_offset, ULONG addr)
{
    stub[byte_offset / 2]     = (UWORD)(addr >> 16);
    stub[byte_offset / 2 + 1] = (UWORD)(addr);
}

/* ---- Generalized code generator ---- */

/* Generate and install a stub for one patched function.
 *
 * The stub is assembled from three pieces:
 *   1. Fixed prefix (168 bytes) - task filter, register save, ring buffer, event header
 *   2. Variable region - argument copy + string capture, built from metadata
 *   3. Fixed suffix (86 bytes) - post-call, disabled path, overflow path
 *
 * Parameters:
 *   anchor    -- pointer to the atrace_anchor (already allocated)
 *   patch     -- pointer to the atrace_patch descriptor (pre-filled
 *                with lib_id, lvo_offset, func_id, arg_count,
 *                arg_regs, string_args, enabled=1)
 *   libbase   -- the library base pointer for SetFunction
 *   entries   -- pointer to ring buffer entries array
 *
 * Returns 0 on success, -1 on failure (AllocMem failed).
 *
 * On success, patch->stub_code, patch->stub_size, and
 * patch->original are filled in. The stub is installed via
 * SetFunction under Disable/Enable.
 */
int stub_generate_and_install(
    struct atrace_anchor *anchor,
    struct atrace_patch *patch,
    struct Library *libbase,
    struct atrace_event *entries)
{
    UBYTE *stub_mem;
    UWORD *p;
    UWORD var_buf[28];    /* max variable region: 4 args + argcount + string w/ null check + valid = 28 words */
    int var_words;
    int total_bytes;
    int alloc_size;
    int suffix_start;     /* byte offset where suffix begins in assembled stub */
    int i;
    APTR old_addr;

    /* ---- 1. Build variable region ---- */

    var_words = 0;

    /* Argument copy instructions: move.l d16(sp), d16(a5) for each arg */
    for (i = 0; i < (int)patch->arg_count && i < 4; i++) {
        WORD frame_ofs = reg_to_frame_offset(patch->arg_regs[i]);
        UWORD entry_arg_ofs = (UWORD)(offsetof(struct atrace_event, args)
                                       + i * 4);
        var_buf[var_words++] = 0x2B6F;               /* move.l d16(sp), d16(a5) */
        var_buf[var_words++] = (UWORD)frame_ofs;      /* source frame offset */
        var_buf[var_words++] = entry_arg_ofs;          /* dest entry offset */
    }

    /* arg_count immediate: move.b #<count>, 32(a5) */
    {
        UBYTE actual_count = (patch->arg_count > 4) ? 4 : patch->arg_count;
        var_buf[var_words++] = 0x1B7C;                /* move.b #imm, d16(a5) */
        var_buf[var_words++] = (UWORD)actual_count;   /* immediate byte value */
        var_buf[var_words++] = (UWORD)offsetof(struct atrace_event, arg_count);
    }

    /* String capture (if any string argument) */
    if (patch->string_args != 0) {
        /* Find the first string argument (lowest set bit) */
        int str_arg_idx;
        WORD str_frame_ofs;

        for (str_arg_idx = 0; str_arg_idx < (int)patch->arg_count; str_arg_idx++) {
            if (patch->string_args & (1 << str_arg_idx))
                break;
        }
        str_frame_ofs = reg_to_frame_offset(patch->arg_regs[str_arg_idx]);

        var_buf[var_words++] = 0x206F;                /* movea.l d16(sp), a0 */
        var_buf[var_words++] = (UWORD)str_frame_ofs;  /* source frame offset */
        var_buf[var_words++] = 0x43ED;                /* lea d16(a5), a1 */
        var_buf[var_words++] = (UWORD)offsetof(struct atrace_event, string_data);
        /* NULL check: if a0 == 0, skip to clr.b (displacement = 8 bytes) */
        var_buf[var_words++] = 0x4A88;                /* tst.l a0 */
        var_buf[var_words++] = 0x6708;                /* beq.s +8 (skip to clr.b) */
        var_buf[var_words++] = 0x7016;                /* moveq #22, d0 */
        var_buf[var_words++] = 0x12D8;                /* move.b (a0)+, (a1)+ */
        var_buf[var_words++] = 0x57C8;                /* dbeq d0, .strcopy */
        var_buf[var_words++] = 0xFFFC;                /* displacement -4 */
        var_buf[var_words++] = 0x4211;                /* clr.b (a1) */
    }

    /* Set valid=1 BEFORE the suffix's trampoline calls the original function.
     * This must happen pre-call because blocking functions (e.g. dos.RunCommand)
     * can block indefinitely.  With valid=0 during the block, the consumer
     * cannot advance past this slot, freezing ALL event consumption system-wide.
     * The suffix post-call handler also writes valid=1 (now redundant but harmless). */
    var_buf[var_words++] = 0x1ABC;    /* move.b #1, (a5)  entry->valid = 1 */
    var_buf[var_words++] = 0x0001;    /* immediate byte 1, word-aligned    */

    /* ---- 2. Calculate total size and allocate ---- */

    total_bytes = STUB_PREFIX_BYTES + (var_words * 2) + STUB_SUFFIX_BYTES;
    alloc_size = (total_bytes + 3) & ~3;  /* ULONG-align */

    stub_mem = (UBYTE *)AllocMem(alloc_size, MEMF_PUBLIC | MEMF_CLEAR);
    if (!stub_mem)
        return -1;

    /* ---- 3. Assemble: prefix + variable + suffix ---- */

    CopyMem((APTR)stub_prefix, (APTR)stub_mem, STUB_PREFIX_BYTES);
    CopyMem((APTR)var_buf, (APTR)(stub_mem + STUB_PREFIX_BYTES),
            var_words * 2);

    suffix_start = STUB_PREFIX_BYTES + (var_words * 2);
    CopyMem((APTR)stub_suffix, (APTR)(stub_mem + suffix_start),
            STUB_SUFFIX_BYTES);

    p = (UWORD *)stub_mem;

    /* ---- 4. Patch addresses and displacements ---- */

    /* PATCH_ADDR -- 4 occurrences:
     *   3 in prefix (fixed offsets), 1 in suffix (suffix-relative) */
    {
        ULONG pa = (ULONG)patch;
        patch_addr(p, PATCH_OFF_1, pa);     /* prefix: enable check */
        patch_addr(p, PATCH_OFF_2, pa);     /* prefix: use_count inc */
        patch_addr(p, PATCH_OFF_3, pa);     /* prefix: lib_id/lvo copy */
        /* occurrence 4 is in suffix at suffix-relative offset PATCH_SUFFIX_REL */
        patch_addr(p, suffix_start + PATCH_SUFFIX_REL, pa);
    }

    /* ANCHOR_ADDR -- 1 occurrence (prefix, fixed offset) */
    patch_addr(p, ANCHOR_OFF_1, (ULONG)anchor);

    /* RING_ENTRIES_ADDR -- 1 occurrence (prefix, fixed offset) */
    patch_addr(p, ENTRIES_OFF_1, (ULONG)entries);

    /* Struct field displacements (prefix -- patched from offsetof) */
    p[DISP_ENABLED / 2]       = (UWORD)offsetof(struct atrace_patch, enabled);
    p[DISP_USE_COUNT_INC / 2] = (UWORD)offsetof(struct atrace_patch, use_count);
    p[DISP_GLOBAL_ENABLE / 2] = (UWORD)offsetof(struct atrace_anchor, global_enable);
    p[DISP_FILTER_TASK_1 / 2] = (UWORD)offsetof(struct atrace_anchor, filter_task);
    p[DISP_FILTER_TASK_2 / 2] = (UWORD)offsetof(struct atrace_anchor, filter_task);
    p[DISP_RING / 2]          = (UWORD)offsetof(struct atrace_anchor, ring);
    p[DISP_EVENT_SEQ_RD / 2]  = (UWORD)offsetof(struct atrace_anchor, event_sequence);
    p[DISP_EVENT_SEQ_WR / 2]  = (UWORD)offsetof(struct atrace_anchor, event_sequence);
    p[DISP_WRITE_POS_RD / 2]  = (UWORD)offsetof(struct atrace_ringbuf, write_pos);
    p[DISP_CAPACITY / 2]      = (UWORD)offsetof(struct atrace_ringbuf, capacity);
    p[DISP_READ_POS / 2]      = (UWORD)offsetof(struct atrace_ringbuf, read_pos);
    p[DISP_WRITE_POS_WR / 2]  = (UWORD)offsetof(struct atrace_ringbuf, write_pos);

    /* Suffix displacement patches (suffix-relative offsets) */
    {
        int s = suffix_start;
        p[(s + SUFFIX_DISP_USE_COUNT_DEC) / 2] =
            (UWORD)offsetof(struct atrace_patch, use_count);
        p[(s + SUFFIX_DISP_OVERFLOW) / 2] =
            (UWORD)offsetof(struct atrace_ringbuf, overflow);
    }

    /* Branch displacements (prefix to suffix) */
    {
        int disabled_byte = suffix_start + SUFFIX_LABEL_DISABLED;
        int overflow_byte = suffix_start + SUFFIX_LABEL_OVERFLOW;

        /* beq.w .disabled at byte 12: displacement word at byte 14 */
        p[BEQ_DISABLED_1 / 2] = (UWORD)(disabled_byte - (12 + 2));
        /* beq.w .disabled at byte 26: displacement word at byte 28 */
        p[BEQ_DISABLED_2 / 2] = (UWORD)(disabled_byte - (26 + 2));
        /* bne.w .disabled at byte 52: displacement word at byte 54 */
        p[BNE_DISABLED_3 / 2] = (UWORD)(disabled_byte - (52 + 2));
        /* beq.w .overflow at byte 92: displacement word at byte 94 */
        p[BEQ_OVERFLOW / 2]   = (UWORD)(overflow_byte - (92 + 2));
    }

    /* ---- 5. Flush and install ---- */

    CacheClearU();

    Disable();
    old_addr = SetFunction(libbase, patch->lvo_offset,
                           (APTR)((ULONG)stub_mem));

    /* Patch ORIG_ADDR (3 occurrences, all in suffix) */
    {
        ULONG oa = (ULONG)old_addr;
        patch_addr(p, suffix_start + ORIG_SUFFIX_REL_1, oa);
        patch_addr(p, suffix_start + ORIG_SUFFIX_REL_2, oa);
        patch_addr(p, suffix_start + ORIG_SUFFIX_REL_3, oa);
    }
    CacheClearU();
    Enable();

    /* 6. Fill patch descriptor */
    patch->original = old_addr;
    patch->stub_code = stub_mem;
    patch->stub_size = alloc_size;

    return 0;
}
