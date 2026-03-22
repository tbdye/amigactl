/*
 * atrace -- stub template and generalized code generator
 *
 * Parameterized code generator that emits per-function
 * argument copy and string capture instructions based on metadata
 * from the patch descriptor.
 *
 * The stub consists of three regions:
 *   1. Prefix (216 bytes standard, 224 with NULL-argument filter):
 *      fast-path checks, task filter, daemon task exclusion,
 *      optional NULL-arg skip,
 *      register save, ring buffer slot reservation, EClock capture,
 *      event header fields.
 *   2. Variable region: per-function argument copy, arg_count immediate,
 *      flags write, and optional string capture. Size varies by function.
 *   3. Suffix (156 bytes, 226 for OpenLibrary, 182 for accept):
 *      MOVEM restore, trampoline with sequence push, post-call handler
 *      with sequence guard and IoErr capture, disabled path, circular
 *      overflow path.  OpenLibrary suffix includes a 70-byte bsdsocket
 *      per-opener patching block.  Accept suffix includes a 26-byte
 *      sockaddr capture block.  Byte offsets shift based on variable
 *      region size.
 */

#include "atrace.h"

#include <proto/exec.h>

#include <string.h>
#include <stddef.h>  /* offsetof */

/*
 * Prefix template -- bytes 0-195, 98 UWORD values.
 * Identical for all patched functions.
 *
 * 26-byte task filter check inserted at bytes 30-55.
 * 28-byte EClock capture block inserted at bytes 136-163,
 * shifting event fill instructions by +28 bytes (168 -> 196 bytes).
 *
 * Contains placeholder 0x0000 values at PATCH_ADDR, ANCHOR_ADDR,
 * RING_ENTRIES_ADDR, TIMER_BASE_ADDR, struct displacement, and
 * branch displacement slots.
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

    /* === Task filter check === */
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
    /*126: */ 0xEF82,                   /* asl.l #7, d2                         */
    /*128: */ 0x2A7C, 0x0000, 0x0000,   /* movea.l #RING_ENTRIES_ADDR, a5      */
    /*134: */ 0xDBC2,                   /* adda.l d2, a5                        */

    /* === EClock capture (28 bytes, 14 words) === */
    /*136: */ 0x2C7C, 0x0000, 0x0000,   /* movea.l #TIMER_BASE, a6             */
    /*142: */ 0x518F,                   /* subq.l #8, sp                        */
    /*144: */ 0x204F,                   /* movea.l a7, a0                       */
    /*146: */ 0x4EAE, 0xFFC4,           /* jsr -60(a6)  (ReadEClock)            */
    /*150: */ 0x2B6F, 0x0004, 0x0064,   /* move.l 4(sp), 100(a5)  eclock_lo    */
    /*156: */ 0x3B6F, 0x0002, 0x0068,   /* move.w 2(sp), 104(a5)  eclock_hi    */
    /*162: */ 0x508F,                   /* addq.l #8, sp                        */

    /* === Event header fill (shifted +28 from original) === */
    /*164: */ 0x2B43, 0x0004,           /* move.l d3, 4(a5)  entry->sequence   */
    /*168: */ 0x207C, 0x0000, 0x0000,   /* movea.l #PATCH_ADDR, a0   [3]       */
    /*174: */ 0x1B68, 0x0000, 0x0001,   /* move.b 0(a0), 1(a5)  lib_id        */
    /*180: */ 0x3B68, 0x0002, 0x0002,   /* move.w 2(a0), 2(a5)  lvo_offset    */
    /*186: */ 0x2C78, 0x0004,           /* movea.l 4.w, a6  (SysBase)          */
    /*190: */ 0x2B6E, 0x0114, 0x0008,   /* move.l 276(a6), 8(a5) caller_task  */
};

#define STUB_PREFIX_BYTES  216  /* 108 words (196 template + 20 daemon task check) */

/*
 * Suffix template -- MOVEM restore, trampoline construction with
 * sequence push, post-call handler with sequence guard and IoErr
 * capture, disabled path, circular overflow path.
 * 78 UWORD values, 156 bytes.
 *
 * The trampoline uses a stack-based approach to pass the entry pointer
 * (a5) and the event's sequence number through the original function
 * call WITHOUT clobbering a0.
 * After MOVEM restore, saved_a5 is on top of stack:
 *   1. Push entry->sequence onto stack (sequence guard)
 *   2. Duplicate saved_a5 lower on the stack (past the sequence)
 *   3. Overwrite original saved_a5 slot with entry pointer (a5)
 *   4. Pop the duplicate to restore a5
 * This leaves [saved_seq][entry_ptr] on the stack, accessible after
 * the original function returns via .post_call.
 *
 * Post-call sequence guard:
 * Before writing retval/ioerr/valid=1, compares saved_seq against
 * entry->sequence. If mismatched (daemon consumed and slot reused),
 * skips all writes but still decrements use_count.
 *
 * Circular overflow:
 * Instead of dropping new events, advances read_pos by 1 (discards
 * oldest event) and branches back to the write path in the prefix.
 *
 * All byte offsets below are suffix-relative (0 = first byte of suffix).
 */
static const UWORD stub_suffix[] = {
    /* === MOVEM restore + trampoline (30 bytes) === */
    /*  0: */ 0x4CDF, 0x5FFF,           /* movem.l (sp)+, d0-d7/a0-a4/a6       */
    /*  4: */ 0x2F2D, 0x0004,           /* move.l 4(a5), -(sp)  push entry->seq */
    /*  8: */ 0x2F2F, 0x0004,           /* move.l 4(sp), -(sp)  dup saved_a5    */
    /* 12: */ 0x2F4D, 0x0008,           /* move.l a5, 8(sp)     entry ptr       */
    /* 16: */ 0x2A5F,                   /* movea.l (sp)+, a5    restore a5      */
    /* 18: */ 0x487A, 0x000A,           /* pea 10(pc)           push .post_call */
    /* 22: */ 0x2F3C, 0x0000, 0x0000,   /* move.l #ORIG_ADDR, -(sp)  [1]       */
    /* 28: */ 0x4E75,                   /* rts                  jump to original*/

    /* === Post-call handler with sequence guard (82 bytes) === */
    /* .post_call: (suffix offset 30) */
    /* 30: */ 0x2F00,                   /* move.l d0, -(sp)     save retval     */
    /* 32: */ 0x206F, 0x0008,           /* movea.l 8(sp), a0    entry ptr       */
    /* 36: */ 0x222F, 0x0004,           /* move.l 4(sp), d1     saved seq       */
    /* 40: */ 0xB2A8, 0x0004,           /* cmp.l 4(a0), d1      entry->sequence */
    /* 44: */ 0x6600, 0x0032,           /* bne.w .guard_skip    disp=50         */
    /* 48: */ 0x2140, 0x001C,           /* move.l d0, 28(a0)    entry->retval   */

    /* === IoErr capture (dos.library only) === */
    /* 52: */ 0x0C28, 0x0001, 0x0001,   /* cmp.b #LIB_DOS, 1(a0)              */
    /* 58: */ 0x6620,                   /* bne.s +32            .skip_ioerr     */
    /* 60: */ 0x4A80,                   /* tst.l d0             retval == 0?    */
    /* 62: */ 0x661C,                   /* bne.s +28            success->skip   */
    /* 64: */ 0x2F0E,                   /* move.l a6, -(sp)     save caller a6  */
    /* 66: */ 0x2C7C, 0x0000, 0x0000,   /* movea.l #DOS_BASE, a6 [patched]    */
    /* 72: */ 0x4EAE, 0xFF7C,           /* jsr -132(a6)         IoErr()         */
    /* 76: */ 0x2C5F,                   /* movea.l (sp)+, a6    restore a6      */
    /* 78: */ 0x206F, 0x0008,           /* movea.l 8(sp), a0    reload entry    */
    /* 82: */ 0x1140, 0x0062,           /* move.b d0, 98(a0)    entry->ioerr    */
    /* 86: */ 0x0028, 0x0002, 0x0021,   /* or.b #2, 33(a0)     FLAG_HAS_IOERR  */

    /* .skip_ioerr: */
    /* 92: */ 0x10BC, 0x0001,           /* move.b #1, (a0)      entry->valid=1  */
    /* .guard_skip: */
    /* 96: */ 0x207C, 0x0000, 0x0000,   /* movea.l #PATCH_ADDR, a0  [4]        */
    /*102: */ 0x53A8, 0x0000,           /* subq.l #1, OFS_USE_COUNT(a0)        */
    /*106: */ 0x201F,                   /* move.l (sp)+, d0     restore retval  */
    /*108: */ 0x508F,                   /* addq.l #8, sp        pop seq+entry   */
    /*110: */ 0x4E75,                   /* rts                  return to caller*/

    /* === DISABLED fast path === */
    /* .disabled: (suffix offset 112) */
    /*112: */ 0x2A5F,                   /* movea.l (sp)+, a5    restore a5      */
    /*114: */ 0x2F3C, 0x0000, 0x0000,   /* move.l #ORIG_ADDR, -(sp)  [2]       */
    /*120: */ 0x4E75,                   /* rts                  tail-call orig  */

    /* === OVERFLOW path (circular, 34 bytes) === */
    /* .overflow: (suffix offset 122) */
    /*122: */ 0x52A8, 0x0000,           /* addq.l #1, OFS_OVERFLOW(a0)         */
    /*126: */ 0x5281,                   /* addq.l #1, d1        advance read_pos*/
    /*128: */ 0xB2A8, 0x0000,           /* cmp.l OFS_CAPACITY(a0), d1          */
    /*132: */ 0x6502,                   /* bcs.s +2             no wrap         */
    /*134: */ 0x7200,                   /* moveq #0, d1         wrap to 0       */
    /*136: */ 0x2141, 0x0000,           /* move.l d1, OFS_READ_POS(a0)         */
    /*140: */ 0x2200,                   /* move.l d0, d1        new_write_pos   */
    /*142: */ 0x5281,                   /* addq.l #1, d1                        */
    /*144: */ 0xB2A8, 0x0000,           /* cmp.l OFS_CAPACITY(a0), d1          */
    /*148: */ 0x6502,                   /* bcs.s +2             no wrap         */
    /*150: */ 0x7200,                   /* moveq #0, d1         wrap to 0       */
    /*152: */ 0x6000, 0x0000,           /* bra.w <disp>         -> write_pos wr */
};

#define STUB_SUFFIX_BYTES   156  /* 78 words */

/* ---- Suffix-relative byte offsets ---- */

/* PATCH_ADDR occurrence 4 (high word of address in suffix) */
#define PATCH_SUFFIX_REL            98   /* was 80, +18 */

/* DOS_BASE_ADDR (high word of DOSBase address in suffix IoErr block) */
#define DOS_BASE_SUFFIX_REL         68   /* was 50, +18 */

/* Struct field displacement patches within the suffix */
#define SUFFIX_DISP_USE_COUNT_DEC  104   /* was 86, +18 -- subq.l #1, OFS_USE_COUNT(a0) */
#define SUFFIX_DISP_OVERFLOW       124   /* was 106, +18 -- addq.l #1, OFS_OVERFLOW(a0) */

/* Label offsets within the suffix (used for branch displacement calc) */
#define SUFFIX_LABEL_DISABLED      112   /* was 94, +18 -- .disabled label */
#define SUFFIX_LABEL_OVERFLOW      122   /* was 104, +18 -- .overflow label */

/* ORIG_ADDR occurrences (suffix-relative high word offsets) */
#define ORIG_SUFFIX_REL_1           24   /* was 18, +6 (trampoline push) */
#define ORIG_SUFFIX_REL_2          116   /* was 98, +18 -- .disabled push */
/* ORIG_SUFFIX_REL_3 deleted: overflow path no longer pushes ORIG_ADDR */

/* Overflow path displacement patches (suffix-relative byte offsets) */
#define OVF_DISP_CAPACITY_1        130   /* cmp.l OFS_CAPACITY in overflow read_pos advance */
#define OVF_DISP_READ_POS_WR      138   /* move.l d1, OFS_READ_POS in overflow */
#define OVF_DISP_CAPACITY_2        146   /* cmp.l OFS_CAPACITY in overflow write_pos recalc */
#define OVF_BRA_DISP               154   /* bra.w displacement word in overflow */

/* BSD patching block inserted into OpenLibrary suffix (Section 5 of plan) */
#define BSD_PATCH_BLOCK_BYTES       70   /* 35 words */
#define BSD_TABLE_SUFFIX_REL        76   /* was 58, +18 -- suffix-relative offset of bsd_table addr */

/* Accept sockaddr capture block inserted into accept suffix (Phase 3) */
#define ACCEPT_BLOCK_BYTES          26   /* 13 words */

/* ---- Prefix address byte offsets (high word of each 32-bit address) ---- */

/* PATCH_ADDR -- 3 occurrences in prefix */
#define PATCH_OFF_1     4    /* per-patch enable check                  */
#define PATCH_OFF_2   122    /* use_count increment (was 102, +20 for daemon task check) */
#define PATCH_OFF_3   190    /* lib_id/lvo_offset copy (was 170, +20 for daemon task check) */

/* ANCHOR_ADDR -- 1 occurrence in prefix */
#define ANCHOR_OFF_1   18    /* global enable check                     */

/* RING_ENTRIES_ADDR -- 1 occurrence in prefix */
#define ENTRIES_OFF_1  150   /* entry base address (was 130, +20 for daemon task check) */

/* TIMER_BASE_ADDR -- 1 occurrence in prefix */
#define TIMER_BASE_OFF 158   /* EClock block: movea.l #TIMER_BASE, a6 (was 138, +20) */

/* ---- Prefix struct field displacement patches ---- */

/* atrace_patch field displacements */
#define DISP_ENABLED       10   /* tst.l OFS_ENABLED(a5): word at byte 10  */
#define DISP_USE_COUNT_INC 128  /* addq.l #1, OFS_USE_COUNT(a0): byte 128 (was 108, +20) */

/* atrace_anchor field displacements */
#define DISP_GLOBAL_ENABLE 24   /* tst.l OFS_GLOBAL_ENABLE(a5): byte 24    */
#define DISP_FILTER_TASK_1 32   /* tst.l OFS_FILTER_TASK(a5): byte 32      */
#define DISP_FILTER_TASK_2 48   /* cmpa.l OFS_FILTER_TASK(a5), a6: byte 48 */
#define DISP_DAEMON_TASK_1 68   /* cmpa.l OFS_DAEMON_TASK(a5), a6: byte 68 (daemon check block) */
#define DISP_RING          90   /* movea.l OFS_RING(a5), a0: byte 90 (was 70, +20) */
#define DISP_EVENT_SEQ_RD 132   /* move.l OFS_EVENT_SEQ(a5), d1: byte 132 (was 112, +20) */
#define DISP_EVENT_SEQ_WR 136   /* addq.l #1, OFS_EVENT_SEQ(a5): byte 136 (was 116, +20) */

/* atrace_ringbuf field displacements */
#define DISP_WRITE_POS_RD  94   /* move.l OFS_WRITE_POS(a0), d0: byte 94 (was 74, +20) */
#define DISP_CAPACITY     102   /* cmp.l OFS_CAPACITY(a0), d1: byte 102 (was 82, +20) */
#define DISP_READ_POS     110   /* cmp.l OFS_READ_POS(a0), d1: byte 110 (was 90, +20) */
#define DISP_WRITE_POS_WR 118   /* move.l d1, OFS_WRITE_POS(a0): byte 118 (was 98, +20) */

/* ---- Branch displacement byte offsets (word containing displacement) ---- */

#define BEQ_DISABLED_1     14   /* beq.w .disabled at prefix byte 12 */
#define BEQ_DISABLED_2     28   /* beq.w .disabled at prefix byte 26 */
#define BNE_DISABLED_3     54   /* bne.w .disabled at prefix byte 52 */
#define BEQ_DISABLED_4     74   /* beq.w .disabled at daemon check byte 72 (56+16), disp at 74 */
#define BEQ_OVERFLOW      114   /* beq.w .overflow at prefix byte 112 (was 94, +20) */

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
 *   1. Fixed prefix (216 bytes) - task filter, daemon check, register save, ring buffer,
 *      EClock capture, event header
 *   2. Variable region - argument copy, flags, string capture, built from metadata
 *   3. Fixed suffix (156 bytes, 226 for OpenLibrary, 182 for accept) -
 *      post-call handler with sequence guard and IoErr capture, disabled
 *      path, circular overflow path
 *
 * Parameters:
 *   anchor    -- pointer to the atrace_anchor (already allocated)
 *   patch     -- pointer to the atrace_patch descriptor (pre-filled
 *                with lib_id, lvo_offset, func_id, arg_count,
 *                arg_regs, string_args, enabled=1)
 *   libbase   -- the library base pointer for SetFunction
 *   entries   -- pointer to ring buffer entries array
 *   dos_base  -- dos.library base pointer for IoErr()
 *   out_suffix_start -- output: byte offset where suffix begins in
 *                the assembled stub (for late-patching by caller).
 *                May be NULL if caller does not need it.
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
    struct atrace_event *entries,
    ULONG dos_base,
    int *out_suffix_start)
{
    UBYTE *stub_mem;
    UWORD *p;
    UWORD var_buf[120];   /* Worst case: 4-arg(12) + arg_count(3) + flags(3) +
                           *   dual-string(28) + cli_CommandName(53) + valid(2) = 101
                           *   (DEREF_LOCK_VOLUME path: 99 words -- CurrentDir is 1-arg)
                           *   120 provides ample margin.
                           *   (cli_CommandName + DEREF_LOCK_VOLUME) */
    int var_words;
    int prefix_bytes;     /* 216 standard, 224 with NULL-argument filter */
    int total_bytes;
    int alloc_size;
    int suffix_start;     /* byte offset where suffix begins in assembled stub */
    int i;
    APTR old_addr;

    /* ---- 1. Build variable region ---- */

    /* NULL-argument filter extends the prefix by 8 bytes */
    prefix_bytes = STUB_PREFIX_BYTES;
    if (patch->skip_null_arg != 0) {
        prefix_bytes = STUB_PREFIX_BYTES + 8;  /* 224 */
    }

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

    /* arg_count immediate: move.b #<count>, 32(a5)
     * IORequest and TextAttr deref capture an extra field in
     * args[1], so arg_count is forced to 2 for those functions. */
    {
        UBYTE actual_count;
        if (patch->name_deref_type == DEREF_IOREQUEST ||
            patch->name_deref_type == DEREF_TEXTATTR) {
            actual_count = 2;
        } else {
            actual_count = (patch->arg_count > 4) ? 4 : patch->arg_count;
        }
        var_buf[var_words++] = 0x1B7C;                /* move.b #imm, d16(a5) */
        var_buf[var_words++] = (UWORD)actual_count;   /* immediate byte value */
        var_buf[var_words++] = (UWORD)offsetof(struct atrace_event, arg_count);
    }

    /* flags = FLAG_HAS_ECLOCK: move.b #1, 33(a5) */
    var_buf[var_words++] = 0x1B7C;                    /* move.b #imm, d16(a5) */
    var_buf[var_words++] = 0x0001;                    /* FLAG_HAS_ECLOCK      */
    var_buf[var_words++] = (UWORD)offsetof(struct atrace_event, flags);

    /* Indirect string capture and direct string capture
     * are mutually exclusive. A function has one or the other, never both. */
    if (patch->string_args != 0 && patch->name_deref_type != 0) {
        /* Programming error -- should never happen */
        return -1;
    }

    /* Indirect string capture (Groups A/B/C).
     * Dereferences struct pointers to capture human-readable names
     * (e.g. library name, device name, font name) into string_data. */
    if (patch->name_deref_type != 0) {
        WORD frame_ofs = reg_to_frame_offset(patch->arg_regs[0]);

        switch (patch->name_deref_type) {
        case DEREF_LN_NAME:
            /* Group A: one-level deref, struct->ln_Name (offset 10)
             * 36 bytes, 18 words.
             * Used by: CloseLibrary, ObtainSemaphore, ReleaseSemaphore,
             *          GetMsg, PutMsg, DeleteMsgPort */
            var_buf[var_words++] = 0x206F;      /* movea.l d16(sp), a0   */
            var_buf[var_words++] = (UWORD)frame_ofs;
            var_buf[var_words++] = 0x4A88;      /* tst.l a0              */
            var_buf[var_words++] = 0x6718;      /* beq.s +24 .skip_name  */
            var_buf[var_words++] = 0x2068;      /* movea.l 10(a0), a0    */
            var_buf[var_words++] = 0x000A;      /* [10 = ln_Name offset] */
            var_buf[var_words++] = 0x4A88;      /* tst.l a0              */
            var_buf[var_words++] = 0x6710;      /* beq.s +16 .skip_name  */
            var_buf[var_words++] = 0x43ED;      /* lea d16(a5), a1       */
            var_buf[var_words++] = (UWORD)offsetof(struct atrace_event, string_data);
            var_buf[var_words++] = 0x703E;      /* moveq #62, d0         */
            var_buf[var_words++] = 0x12D8;      /* move.b (a0)+, (a1)+   */
            var_buf[var_words++] = 0x57C8;      /* dbeq d0, .copy        */
            var_buf[var_words++] = 0xFFFC;      /* displacement -4       */
            var_buf[var_words++] = 0x4211;      /* clr.b (a1)            */
            var_buf[var_words++] = 0x6004;      /* bra.s +4 .done        */
            var_buf[var_words++] = 0x422D;      /* clr.b d16(a5)         */
            var_buf[var_words++] = (UWORD)offsetof(struct atrace_event, string_data);
            break;

        case DEREF_IOREQUEST:
            /* Group B: IORequest two-level deref + io_Command capture
             * 54 bytes, 27 words.
             * ioReq -> io_Device (offset 20) -> ln_Name (offset 10)
             * Also captures io_Command (UWORD at offset 28) into args[1].
             * Used by: DoIO, SendIO, WaitIO, AbortIO, CheckIO, CloseDevice */
            var_buf[var_words++] = 0x206F;      /* movea.l d16(sp), a0   */
            var_buf[var_words++] = (UWORD)frame_ofs;
            var_buf[var_words++] = 0x4A88;      /* tst.l a0              */
            var_buf[var_words++] = 0x672A;      /* beq.s +42 .skip_name  */
            /* io_Command capture (UWORD at IORequest+28 -> args[1]) */
            var_buf[var_words++] = 0x7000;      /* moveq #0, d0          */
            var_buf[var_words++] = 0x3028;      /* move.w d16(a0), d0    */
            var_buf[var_words++] = 0x001C;      /* [28 = io_Command ofs] */
            var_buf[var_words++] = 0x2B40;      /* move.l d0, d16(a5)    */
            var_buf[var_words++] = (UWORD)(offsetof(struct atrace_event, args) + 4);
            /* First deref: io_Device at offset 20 */
            var_buf[var_words++] = 0x2068;      /* movea.l d16(a0), a0   */
            var_buf[var_words++] = 0x0014;      /* [20 = io_Device ofs]  */
            var_buf[var_words++] = 0x4A88;      /* tst.l a0              */
            var_buf[var_words++] = 0x6718;      /* beq.s +24 .skip_name  */
            /* Second deref: ln_Name at offset 10 */
            var_buf[var_words++] = 0x2068;      /* movea.l d16(a0), a0   */
            var_buf[var_words++] = 0x000A;      /* [10 = ln_Name ofs]    */
            var_buf[var_words++] = 0x4A88;      /* tst.l a0              */
            var_buf[var_words++] = 0x6710;      /* beq.s +16 .skip_name  */
            /* String copy */
            var_buf[var_words++] = 0x43ED;      /* lea d16(a5), a1       */
            var_buf[var_words++] = (UWORD)offsetof(struct atrace_event, string_data);
            var_buf[var_words++] = 0x703E;      /* moveq #62, d0         */
            var_buf[var_words++] = 0x12D8;      /* move.b (a0)+, (a1)+   */
            var_buf[var_words++] = 0x57C8;      /* dbeq d0, .copy        */
            var_buf[var_words++] = 0xFFFC;      /* displacement -4       */
            var_buf[var_words++] = 0x4211;      /* clr.b (a1)            */
            var_buf[var_words++] = 0x6004;      /* bra.s +4 .done        */
            /* .skip_name: */
            var_buf[var_words++] = 0x422D;      /* clr.b d16(a5)         */
            var_buf[var_words++] = (UWORD)offsetof(struct atrace_event, string_data);
            /* .done: */
            break;

        case DEREF_TEXTATTR:
            /* Group C: TextAttr->ta_Name (offset 0) + ta_YSize capture
             * 44 bytes, 22 words.
             * Also captures ta_YSize (UWORD at offset 4) into args[1].
             * Used by: OpenFont */
            var_buf[var_words++] = 0x206F;      /* movea.l d16(sp), a0   */
            var_buf[var_words++] = (UWORD)frame_ofs;
            var_buf[var_words++] = 0x4A88;      /* tst.l a0              */
            var_buf[var_words++] = 0x6720;      /* beq.s +32 .skip_name  */
            /* ta_YSize capture (UWORD at TextAttr+4 -> args[1]) */
            var_buf[var_words++] = 0x7000;      /* moveq #0, d0          */
            var_buf[var_words++] = 0x3028;      /* move.w d16(a0), d0    */
            var_buf[var_words++] = 0x0004;      /* [4 = ta_YSize offset] */
            var_buf[var_words++] = 0x2B40;      /* move.l d0, d16(a5)    */
            var_buf[var_words++] = (UWORD)(offsetof(struct atrace_event, args) + 4);
            /* ta_Name deref: offset 0, use (a0) mode */
            var_buf[var_words++] = 0x2050;      /* movea.l (a0), a0      */
            var_buf[var_words++] = 0x4A88;      /* tst.l a0              */
            var_buf[var_words++] = 0x6710;      /* beq.s +16 .skip_name  */
            /* String copy */
            var_buf[var_words++] = 0x43ED;      /* lea d16(a5), a1       */
            var_buf[var_words++] = (UWORD)offsetof(struct atrace_event, string_data);
            var_buf[var_words++] = 0x703E;      /* moveq #62, d0         */
            var_buf[var_words++] = 0x12D8;      /* move.b (a0)+, (a1)+   */
            var_buf[var_words++] = 0x57C8;      /* dbeq d0, .copy        */
            var_buf[var_words++] = 0xFFFC;      /* displacement -4       */
            var_buf[var_words++] = 0x4211;      /* clr.b (a1)            */
            var_buf[var_words++] = 0x6004;      /* bra.s +4 .done        */
            /* .skip_name: */
            var_buf[var_words++] = 0x422D;      /* clr.b d16(a5)         */
            var_buf[var_words++] = (UWORD)offsetof(struct atrace_event, string_data);
            /* .done: */
            break;

        case DEREF_LOCK_VOLUME:
            /* Lock BPTR -> fl_Volume -> dol_Name BSTR -> volume name string
             * Three BPTR dereferences with NULL checks at each level.
             * Appends ":" to volume name for AmigaOS convention.
             *
             * Offsets (verified from NDK dos/dosextens.h):
             *   FileLock->fl_Volume at offset 16 (BPTR)
             *   DosList->dol_Name at offset 40 (BPTR to BSTR)
             *
             * 35 words = 70 bytes.
             * Uses a0, a1, d0 as scratch (saved by MOVEM). */

            /* Load lock BPTR from arg0 frame slot */
            var_buf[var_words++] = 0x206F;      /* movea.l d16(sp), a0   */
            var_buf[var_words++] = (UWORD)frame_ofs;
            var_buf[var_words++] = 0x2008;      /* move.l a0, d0         */
            var_buf[var_words++] = 0x673A;      /* beq.s +58 .skip_vol   */

            /* BADDR the lock: BPTR -> FileLock* */
            var_buf[var_words++] = 0xE580;      /* asl.l #2, d0          */
            var_buf[var_words++] = 0x2040;      /* movea.l d0, a0        */

            /* Read fl_Volume (BPTR at FileLock offset 16) */
            var_buf[var_words++] = 0x2028;      /* move.l 16(a0), d0     */
            var_buf[var_words++] = 0x0010;
            var_buf[var_words++] = 0x6730;      /* beq.s +48 .skip_vol   */

            /* BADDR the volume DosList entry */
            var_buf[var_words++] = 0xE580;      /* asl.l #2, d0          */
            var_buf[var_words++] = 0x2040;      /* movea.l d0, a0        */

            /* Read dol_Name (BPTR to BSTR at DosList offset 40) */
            var_buf[var_words++] = 0x2028;      /* move.l 40(a0), d0     */
            var_buf[var_words++] = 0x0028;
            var_buf[var_words++] = 0x6726;      /* beq.s +38 .skip_vol   */

            /* BADDR the BSTR */
            var_buf[var_words++] = 0xE580;      /* asl.l #2, d0          */
            var_buf[var_words++] = 0x2040;      /* movea.l d0, a0        */

            /* Read BSTR: first byte is length */
            var_buf[var_words++] = 0x7000;      /* moveq #0, d0          */
            var_buf[var_words++] = 0x1018;      /* move.b (a0)+, d0      */
            var_buf[var_words++] = 0x671C;      /* beq.s +28 .skip_vol   */

            /* Clamp to 61 chars (64 - ":" - NUL - 1 safety) */
            var_buf[var_words++] = 0xB07C;      /* cmp.w #61, d0         */
            var_buf[var_words++] = 0x003D;
            var_buf[var_words++] = 0x6302;      /* bls.s +2 .len_ok      */
            var_buf[var_words++] = 0x703D;      /* moveq #61, d0         */
            /* .len_ok: */

            /* Copy volume name into string_data (offset 34 from a5) */
            var_buf[var_words++] = 0x43ED;      /* lea 34(a5), a1        */
            var_buf[var_words++] = (UWORD)offsetof(struct atrace_event, string_data);
            var_buf[var_words++] = 0x5340;      /* subq.w #1, d0         */
            /* .vol_copy: */
            var_buf[var_words++] = 0x12D8;      /* move.b (a0)+, (a1)+   */
            var_buf[var_words++] = 0x51C8;      /* dbf d0, .vol_copy     */
            var_buf[var_words++] = 0xFFFC;      /* displacement -4       */

            /* Append ":" and NUL-terminate */
            var_buf[var_words++] = 0x12FC;      /* move.b #':', (a1)+    */
            var_buf[var_words++] = 0x003A;      /* ':' = 0x3A            */
            var_buf[var_words++] = 0x4211;      /* clr.b (a1)            */
            var_buf[var_words++] = 0x6004;      /* bra.s +4 .vol_done    */

            /* .skip_vol: */
            var_buf[var_words++] = 0x422D;      /* clr.b 34(a5)          */
            var_buf[var_words++] = (UWORD)offsetof(struct atrace_event, string_data);
            /* .vol_done: */
            break;

        case DEREF_NW_TITLE:
            /* NewWindow->Title (offset 26) with tag list fallback.
             * 92 bytes, 46 words.
             *
             * When a0 (NewWindow) is non-NULL, dereferences nw_Title at
             * offset 26 as before.  When a0 is NULL and a1 (tagList) is
             * non-NULL, walks the tag list looking for WA_Title
             * (0x8000006E) and captures the ti_Data string pointer.
             * Safety limit of 64 iterations prevents hangs on corrupted
             * tag lists.
             *
             * Layout:
             *   byte  0: load a0 from MOVEM frame, NULL check
             *   byte  8: NewWindow path (deref nw_Title at offset 26)
             *   byte 18: .try_tags -- load a1 from frame, walk tag list
             *   byte 64: .found_title -- load ti_Data into a0
             *   byte 72: .copy_str -- shared string copy (max 63 chars)
             *   byte 88: .skip_name -- clear string_data[0]
             *   byte 92: .done
             */
        {
            WORD tag_frame_ofs = reg_to_frame_offset(patch->arg_regs[1]);

            /* Load a0 = NewWindow from MOVEM frame */
            var_buf[var_words++] = 0x206F;      /* movea.l d16(sp), a0   */
            var_buf[var_words++] = (UWORD)frame_ofs;
            var_buf[var_words++] = 0x4A88;      /* tst.l a0              */
            var_buf[var_words++] = 0x670A;      /* beq.s +10 .try_tags   */

            /* -- NewWindow non-NULL path -- */
            var_buf[var_words++] = 0x2068;      /* movea.l 26(a0), a0    */
            var_buf[var_words++] = 0x001A;      /* [26 = nw_Title offset]*/
            var_buf[var_words++] = 0x4A88;      /* tst.l a0              */
            var_buf[var_words++] = 0x6638;      /* bne.s +56 .copy_str   */
            var_buf[var_words++] = 0x6046;      /* bra.s +70 .skip_name  */

            /* -- .try_tags (byte 18): load a1 from MOVEM frame -- */
            var_buf[var_words++] = 0x226F;      /* movea.l d16(sp), a1   */
            var_buf[var_words++] = (UWORD)tag_frame_ofs;
            var_buf[var_words++] = 0x4A89;      /* tst.l a1              */
            var_buf[var_words++] = 0x673E;      /* beq.s +62 .skip_name  */
            var_buf[var_words++] = 0x723F;      /* moveq #63, d1 (64 iterations) */

            /* -- .tag_loop (byte 28): walk tag list -- */
            var_buf[var_words++] = 0x2011;      /* move.l (a1), d0       */
            var_buf[var_words++] = 0x6738;      /* beq.s +56 .skip_name (TAG_DONE) */
            var_buf[var_words++] = 0x0C80;      /* cmpi.l #WA_Title, d0  */
            var_buf[var_words++] = 0x8000;      /* high word of 0x8000006E */
            var_buf[var_words++] = 0x006E;      /* low word of 0x8000006E */
            var_buf[var_words++] = 0x6718;      /* beq.s +24 .found_title */
            var_buf[var_words++] = 0x0C80;      /* cmpi.l #TAG_MORE, d0  */
            var_buf[var_words++] = 0x0000;      /* high word of 2        */
            var_buf[var_words++] = 0x0002;      /* low word of 2         */
            var_buf[var_words++] = 0x6728;      /* beq.s +40 .skip_name  */
            var_buf[var_words++] = 0x0C80;      /* cmpi.l #TAG_SKIP, d0  */
            var_buf[var_words++] = 0x0000;      /* high word of 3        */
            var_buf[var_words++] = 0x0003;      /* low word of 3         */
            var_buf[var_words++] = 0x6720;      /* beq.s +32 .skip_name  */
            /* TAG_IGNORE (1) and normal tags fall through to .tag_next */
            var_buf[var_words++] = 0x5089;      /* addq.l #8, a1 (next TagItem) */
            var_buf[var_words++] = 0x51C9;      /* dbf d1, .tag_loop     */
            var_buf[var_words++] = 0xFFE0;      /* displacement -32      */
            var_buf[var_words++] = 0x6018;      /* bra.s +24 .skip_name (limit reached) */

            /* -- .found_title (byte 64): ti_Data -> string pointer -- */
            var_buf[var_words++] = 0x2069;      /* movea.l 4(a1), a0     */
            var_buf[var_words++] = 0x0004;      /* [4 = ti_Data offset]  */
            var_buf[var_words++] = 0x4A88;      /* tst.l a0              */
            var_buf[var_words++] = 0x6710;      /* beq.s +16 .skip_name  */

            /* -- .copy_str (byte 72): shared string copy -- */
            var_buf[var_words++] = 0x43ED;      /* lea d16(a5), a1       */
            var_buf[var_words++] = (UWORD)offsetof(struct atrace_event, string_data);
            var_buf[var_words++] = 0x703E;      /* moveq #62, d0         */
            /* .copy: */
            var_buf[var_words++] = 0x12D8;      /* move.b (a0)+, (a1)+   */
            var_buf[var_words++] = 0x57C8;      /* dbeq d0, .copy        */
            var_buf[var_words++] = 0xFFFC;      /* displacement -4       */
            var_buf[var_words++] = 0x4211;      /* clr.b (a1)            */
            var_buf[var_words++] = 0x6004;      /* bra.s +4 .done        */

            /* -- .skip_name (byte 88) -- */
            var_buf[var_words++] = 0x422D;      /* clr.b d16(a5)         */
            var_buf[var_words++] = (UWORD)offsetof(struct atrace_event, string_data);
            /* -- .done (byte 92) -- */
        }
            break;

        case DEREF_WIN_TITLE:
            /* Window->Title (offset 32)
             * 36 bytes, 18 words.
             * Same pattern as DEREF_LN_NAME but with offset 32. */
            var_buf[var_words++] = 0x206F;      /* movea.l d16(sp), a0   */
            var_buf[var_words++] = (UWORD)frame_ofs;
            var_buf[var_words++] = 0x4A88;      /* tst.l a0              */
            var_buf[var_words++] = 0x6718;      /* beq.s +24 .skip_name  */
            var_buf[var_words++] = 0x2068;      /* movea.l 32(a0), a0    */
            var_buf[var_words++] = 0x0020;      /* [32 = win_Title ofs]  */
            var_buf[var_words++] = 0x4A88;      /* tst.l a0              */
            var_buf[var_words++] = 0x6710;      /* beq.s +16 .skip_name  */
            var_buf[var_words++] = 0x43ED;      /* lea d16(a5), a1       */
            var_buf[var_words++] = (UWORD)offsetof(struct atrace_event, string_data);
            var_buf[var_words++] = 0x703E;      /* moveq #62, d0         */
            var_buf[var_words++] = 0x12D8;      /* move.b (a0)+, (a1)+   */
            var_buf[var_words++] = 0x57C8;      /* dbeq d0, .copy        */
            var_buf[var_words++] = 0xFFFC;      /* displacement -4       */
            var_buf[var_words++] = 0x4211;      /* clr.b (a1)            */
            var_buf[var_words++] = 0x6004;      /* bra.s +4 .done        */
            var_buf[var_words++] = 0x422D;      /* clr.b d16(a5)         */
            var_buf[var_words++] = (UWORD)offsetof(struct atrace_event, string_data);
            break;

        case DEREF_NS_TITLE:
            /* NewScreen->DefaultTitle (offset 20)
             * 36 bytes, 18 words.
             * Same pattern as DEREF_LN_NAME but with offset 20. */
            var_buf[var_words++] = 0x206F;      /* movea.l d16(sp), a0   */
            var_buf[var_words++] = (UWORD)frame_ofs;
            var_buf[var_words++] = 0x4A88;      /* tst.l a0              */
            var_buf[var_words++] = 0x6718;      /* beq.s +24 .skip_name  */
            var_buf[var_words++] = 0x2068;      /* movea.l 20(a0), a0    */
            var_buf[var_words++] = 0x0014;      /* [20 = ns_DefaultTitle]*/
            var_buf[var_words++] = 0x4A88;      /* tst.l a0              */
            var_buf[var_words++] = 0x6710;      /* beq.s +16 .skip_name  */
            var_buf[var_words++] = 0x43ED;      /* lea d16(a5), a1       */
            var_buf[var_words++] = (UWORD)offsetof(struct atrace_event, string_data);
            var_buf[var_words++] = 0x703E;      /* moveq #62, d0         */
            var_buf[var_words++] = 0x12D8;      /* move.b (a0)+, (a1)+   */
            var_buf[var_words++] = 0x57C8;      /* dbeq d0, .copy        */
            var_buf[var_words++] = 0xFFFC;      /* displacement -4       */
            var_buf[var_words++] = 0x4211;      /* clr.b (a1)            */
            var_buf[var_words++] = 0x6004;      /* bra.s +4 .done        */
            var_buf[var_words++] = 0x422D;      /* clr.b d16(a5)         */
            var_buf[var_words++] = (UWORD)offsetof(struct atrace_event, string_data);
            break;

        case DEREF_SCR_TITLE:
            /* Screen->Title (offset 22)
             * 36 bytes, 18 words.
             * Same pattern as DEREF_LN_NAME but with offset 22. */
            var_buf[var_words++] = 0x206F;      /* movea.l d16(sp), a0   */
            var_buf[var_words++] = (UWORD)frame_ofs;
            var_buf[var_words++] = 0x4A88;      /* tst.l a0              */
            var_buf[var_words++] = 0x6718;      /* beq.s +24 .skip_name  */
            var_buf[var_words++] = 0x2068;      /* movea.l 22(a0), a0    */
            var_buf[var_words++] = 0x0016;      /* [22 = scr_Title ofs]  */
            var_buf[var_words++] = 0x4A88;      /* tst.l a0              */
            var_buf[var_words++] = 0x6710;      /* beq.s +16 .skip_name  */
            var_buf[var_words++] = 0x43ED;      /* lea d16(a5), a1       */
            var_buf[var_words++] = (UWORD)offsetof(struct atrace_event, string_data);
            var_buf[var_words++] = 0x703E;      /* moveq #62, d0         */
            var_buf[var_words++] = 0x12D8;      /* move.b (a0)+, (a1)+   */
            var_buf[var_words++] = 0x57C8;      /* dbeq d0, .copy        */
            var_buf[var_words++] = 0xFFFC;      /* displacement -4       */
            var_buf[var_words++] = 0x4211;      /* clr.b (a1)            */
            var_buf[var_words++] = 0x6004;      /* bra.s +4 .done        */
            var_buf[var_words++] = 0x422D;      /* clr.b d16(a5)         */
            var_buf[var_words++] = (UWORD)offsetof(struct atrace_event, string_data);
            break;

        case DEREF_SOCKADDR:
        case DEREF_SOCKADDR_3:
            /* sockaddr_in dereference: copy 8 bytes to string_data[0..7]
             * 28 bytes, 14 words.
             * DEREF_SOCKADDR dereferences arg_regs[1] (bind, connect).
             * DEREF_SOCKADDR_3 dereferences arg_regs[3] (sendto). */
        {
            int sa_arg_idx = (patch->name_deref_type == DEREF_SOCKADDR) ? 1 : 3;
            WORD sa_frame_ofs = reg_to_frame_offset(patch->arg_regs[sa_arg_idx]);

            var_buf[var_words++] = 0x206F;      /* movea.l d16(sp), a0       */
            var_buf[var_words++] = (UWORD)sa_frame_ofs;
            var_buf[var_words++] = 0x4A88;      /* tst.l a0                  */
            var_buf[var_words++] = 0x6710;      /* beq.s +16 (.skip_sa)      */
            var_buf[var_words++] = 0x2010;      /* move.l (a0), d0           */
            var_buf[var_words++] = 0x2B40;      /* move.l d0, 34(a5)         */
            var_buf[var_words++] = (UWORD)offsetof(struct atrace_event, string_data);
            var_buf[var_words++] = 0x2028;      /* move.l 4(a0), d0          */
            var_buf[var_words++] = 0x0004;
            var_buf[var_words++] = 0x2B40;      /* move.l d0, 38(a5)         */
            var_buf[var_words++] = (UWORD)(offsetof(struct atrace_event, string_data) + 4);
            var_buf[var_words++] = 0x6004;      /* bra.s +4 (.done)          */
            /* .skip_sa: */
            var_buf[var_words++] = 0x422D;      /* clr.b 34(a5)              */
            var_buf[var_words++] = (UWORD)offsetof(struct atrace_event, string_data);
            /* .done: */
            break;
        }
        }
    } else if (patch->string_args != 0) {
        /* Count string arguments */
        int str_count = 0;
        int bit;
        for (bit = 0; bit < (int)patch->arg_count; bit++) {
            if (patch->string_args & (1 << bit))
                str_count++;
        }

        if (str_count == 1) {
            /* Single string: full 63-byte capture (existing behavior) */
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
            var_buf[var_words++] = 0x703E;                /* moveq #62, d0 */
            var_buf[var_words++] = 0x12D8;                /* move.b (a0)+, (a1)+ */
            var_buf[var_words++] = 0x57C8;                /* dbeq d0, .strcopy */
            var_buf[var_words++] = 0xFFFC;                /* displacement -4 */
            var_buf[var_words++] = 0x4211;                /* clr.b (a1) */
        } else if (str_count == 2) {
            /* Dual string: split string_data into two 32-byte halves.
             * First string: string_data[0..31]  (offset 34(a5), max 31 chars)
             * Second string: string_data[32..63] (offset 66(a5), max 31 chars)
             *
             * Each half has: load arg, lea dest, tst NULL, beq.s .null,
             * moveq #30 d0, copy loop (move.b + dbeq), clr.b NUL,
             * bra.s .next, .null: clr.b field.
             *
             * Branch displacements (verified):
             *   beq.s +12: skips moveq(2) + move.b(2) + dbeq(4) + clr.b(2)
             *              + bra.s(2) = 12 bytes -> lands on .null clr.b
             *   bra.s +4:  skips .null block = clr.b d16(a5) (4 bytes)
             *              -> lands on next string block or .done */
            int args_found = 0;
            WORD str_frame_offsets[2];

            for (bit = 0; bit < (int)patch->arg_count && args_found < 2; bit++) {
                if (patch->string_args & (1 << bit)) {
                    str_frame_offsets[args_found] = reg_to_frame_offset(
                        patch->arg_regs[bit]);
                    args_found++;
                }
            }

            /* First string capture: arg0 -> string_data[0..31] */
            var_buf[var_words++] = 0x206F;                        /* movea.l d16(sp), a0 */
            var_buf[var_words++] = (UWORD)str_frame_offsets[0];
            var_buf[var_words++] = 0x43ED;                        /* lea 34(a5), a1 */
            var_buf[var_words++] = (UWORD)offsetof(struct atrace_event, string_data);
            var_buf[var_words++] = 0x4A88;                        /* tst.l a0 */
            var_buf[var_words++] = 0x670C;                        /* beq.s +12 (.null1) */
            var_buf[var_words++] = 0x701E;                        /* moveq #30, d0 */
            var_buf[var_words++] = 0x12D8;                        /* move.b (a0)+, (a1)+ */
            var_buf[var_words++] = 0x57C8;                        /* dbeq d0, .copy1 */
            var_buf[var_words++] = 0xFFFC;                        /* displacement -4 */
            var_buf[var_words++] = 0x4211;                        /* clr.b (a1) */
            var_buf[var_words++] = 0x6004;                        /* bra.s +4 (.str2) */
            /* .null1: */
            var_buf[var_words++] = 0x422D;                        /* clr.b 34(a5) */
            var_buf[var_words++] = (UWORD)offsetof(struct atrace_event, string_data);

            /* Second string capture: arg1 -> string_data[32..63] */
            /* .str2: */
            var_buf[var_words++] = 0x206F;                        /* movea.l d16(sp), a0 */
            var_buf[var_words++] = (UWORD)str_frame_offsets[1];
            var_buf[var_words++] = 0x43ED;                        /* lea 66(a5), a1 */
            var_buf[var_words++] = (UWORD)(offsetof(struct atrace_event, string_data) + 32);
            var_buf[var_words++] = 0x4A88;                        /* tst.l a0 */
            var_buf[var_words++] = 0x670C;                        /* beq.s +12 (.null2) */
            var_buf[var_words++] = 0x701E;                        /* moveq #30, d0 */
            var_buf[var_words++] = 0x12D8;                        /* move.b (a0)+, (a1)+ */
            var_buf[var_words++] = 0x57C8;                        /* dbeq d0, .copy2 */
            var_buf[var_words++] = 0xFFFC;                        /* displacement -4 */
            var_buf[var_words++] = 0x4211;                        /* clr.b (a1) */
            var_buf[var_words++] = 0x6004;                        /* bra.s +4 (.done2) */
            /* .null2: */
            var_buf[var_words++] = 0x422D;                        /* clr.b 66(a5) */
            var_buf[var_words++] = (UWORD)(offsetof(struct atrace_event, string_data) + 32);
            /* .done2: */
        }
    }

    /* BSD flag check for OpenLibrary (exec, LVO -552) only.
     * After string capture has filled string_data, check for
     * "bsdsocke" prefix (8-byte match) and set bsd_flag (offset 99)
     * to 0xFF if matched.  The post-call suffix handler reads this
     * byte to decide whether to patch the returned library base.
     * 30 bytes = 15 words. */
    if (patch->lib_id == LIB_EXEC && patch->lvo_offset == -552) {
        var_buf[var_words++] = 0x422D;  /* clr.b 99(a5)                         */
        var_buf[var_words++] = 0x0063;  /* offset 99 = bsd_flag                 */
        var_buf[var_words++] = 0x0CAD;  /* cmpi.l #$62736473, 34(a5)  "bsds"   */
        var_buf[var_words++] = 0x6273;
        var_buf[var_words++] = 0x6473;
        var_buf[var_words++] = 0x0022;  /* offset 34 = string_data              */
        var_buf[var_words++] = 0x6610;  /* bne.s +16  -> .skip_flag             */
        var_buf[var_words++] = 0x0CAD;  /* cmpi.l #$6F636B65, 38(a5)  "ocke"   */
        var_buf[var_words++] = 0x6F63;
        var_buf[var_words++] = 0x6B65;
        var_buf[var_words++] = 0x0026;  /* offset 38 = string_data + 4          */
        var_buf[var_words++] = 0x6606;  /* bne.s +6   -> .skip_flag             */
        var_buf[var_words++] = 0x1B7C;  /* move.b #$FF, 99(a5)                  */
        var_buf[var_words++] = 0x00FF;
        var_buf[var_words++] = 0x0063;  /* offset 99 = bsd_flag                 */
        /* .skip_flag: */
    }

    /* Accept: pre-clear string_data[0..7] to eliminate stale sockaddr
     * from ring buffer slot reuse.  The post-call suffix handler only
     * writes here on success; failed accepts must see zeros. */
    if (patch->lib_id == LIB_BSDSOCKET && patch->lvo_offset == -48) {
        var_buf[var_words++] = 0x42AD;      /* clr.l 34(a5)          */
        var_buf[var_words++] = (UWORD)offsetof(struct atrace_event, string_data);
        var_buf[var_words++] = 0x42AD;      /* clr.l 38(a5)          */
        var_buf[var_words++] = (UWORD)(offsetof(struct atrace_event, string_data) + 4);
    }

    /* Task name capture: cli_CommandName resolution with fallback.
     * Tries pr_CLI -> cli_CommandName (BSTR) first for CLI processes,
     * falls back to tc_Node.ln_Name for non-CLI processes/tasks.
     * a6 = SysBase (from prefix byte 186), a5 = entry pointer.
     * Uses a0, a1, d0 as scratch (all saved by MOVEM).
     *
     * All memory reads are in the pre-call variable region where
     * reads work correctly (see MEMORY.md note about post-call
     * handler memory read freezes).
     *
     * BSTR handling: AmigaOS BSTRs are BPTRs (longword-aligned
     * pointers divided by 4). lsl.l #2 converts to real address.
     * First byte is length, followed by string data. No NUL
     * terminator in the BSTR itself.
     *
     * NT_PROCESS guard: A plain Task struct is only 92 bytes.
     * pr_CLI is at Process offset 172, which is 80 bytes past the
     * end of a plain Task.  We must verify tc_Node.ln_Type == 13
     * (NT_PROCESS) before reading pr_CLI, otherwise a plain Task
     * calling a patched exec.library function would read garbage.
     *
     * Offsets:
     *   276(a6) = SysBase->ThisTask (Process*)
     *   8(a0)   = tc_Node.ln_Type (UBYTE, NT_PROCESS=13)
     *   172(a0) = pr_CLI (BPTR to CommandLineInterface)
     *   16(a1)  = cli_CommandName (BPTR to BSTR)
     *   10(a0)  = tc_Node.ln_Name (C string pointer)
     *   106(a5) = entry->task_name (22-byte field)
     *
     * 53 words = 106 bytes. Net growth: +37 words (+74 bytes) per stub.
     * (was 47 words = 94 bytes before stale name validation, +6 per path)
     */

    /* --- Try CLI path first --- */
    var_buf[var_words++] = 0x206E;  /* movea.l 276(a6), a0  ThisTask            */
    var_buf[var_words++] = 0x0114;

    /* Guard: only Processes have pr_CLI at offset 172 */
    var_buf[var_words++] = 0x0C28;  /* cmpi.b #13, 8(a0)    NT_PROCESS check    */
    var_buf[var_words++] = 0x000D;  /* #13 = NT_PROCESS                         */
    var_buf[var_words++] = 0x0008;  /* offset 8 = tc_Node.ln_Type               */
    var_buf[var_words++] = 0x6638;  /* bne.s +56 (.use_task_name)  was +50, +6  */

    var_buf[var_words++] = 0x2028;  /* move.l 172(a0), d0   pr_CLI (BPTR)       */
    var_buf[var_words++] = 0x00AC;
    var_buf[var_words++] = 0x6732;  /* beq.s +50 (.use_task_name)  was +44, +6  */

    /* BADDR: BPTR -> real pointer */
    var_buf[var_words++] = 0xE580;  /* asl.l #2, d0                             */
    var_buf[var_words++] = 0x2240;  /* movea.l d0, a1       a1 = CLI struct     */

    var_buf[var_words++] = 0x2029;  /* move.l 16(a1), d0    cli_CommandName BPTR */
    var_buf[var_words++] = 0x0010;
    var_buf[var_words++] = 0x6728;  /* beq.s +40 (.use_task_name)  was +34, +6  */

    /* BADDR the BSTR */
    var_buf[var_words++] = 0xE580;  /* asl.l #2, d0                             */
    var_buf[var_words++] = 0x2240;  /* movea.l d0, a1       a1 = BSTR pointer   */

    /* Read BSTR: first byte is length */
    var_buf[var_words++] = 0x7000;  /* moveq #0, d0                             */
    var_buf[var_words++] = 0x1019;  /* move.b (a1)+, d0     d0 = string length  */
    var_buf[var_words++] = 0x671E;  /* beq.s +30 (.use_task_name)  was +24, +6  */

    /* Validate first BSTR data byte is printable (>= 0x20) */
    var_buf[var_words++] = 0x0C11;  /* cmpi.b #0x20, (a1)   first char < space? */
    var_buf[var_words++] = 0x0020;
    var_buf[var_words++] = 0x6518;  /* bcs.s +24 (.use_task_name)  stale BSTR   */

    /* Clamp to 21 chars (task_name field is 22 bytes with NUL) */
    var_buf[var_words++] = 0xB07C;  /* cmp.w #21, d0                            */
    var_buf[var_words++] = 0x0015;
    var_buf[var_words++] = 0x6302;  /* bls.s +2 (.cli_len_ok)                   */
    var_buf[var_words++] = 0x7015;  /* moveq #21, d0                            */
    /* .cli_len_ok: */

    /* Copy BSTR data into entry->task_name */
    var_buf[var_words++] = 0x41ED;  /* lea 106(a5), a0      &entry->task_name   */
    var_buf[var_words++] = 0x006A;
    var_buf[var_words++] = 0x5340;  /* subq.w #1, d0        adjust for dbf      */
    /* .cli_copy: */
    var_buf[var_words++] = 0x10D9;  /* move.b (a1)+, (a0)+                      */
    var_buf[var_words++] = 0x51C8;  /* dbf d0, .cli_copy                        */
    var_buf[var_words++] = 0xFFFC;  /* displacement -4                           */
    var_buf[var_words++] = 0x4210;  /* clr.b (a0)           NUL-terminate       */
    var_buf[var_words++] = 0x6026;  /* bra.s +38 (.name_done)  was +32, +6      */

    /* --- Fallback: tc_Node.ln_Name --- */
    /* .use_task_name: */
    var_buf[var_words++] = 0x206E;  /* movea.l 276(a6), a0  reload ThisTask     */
    var_buf[var_words++] = 0x0114;
    var_buf[var_words++] = 0x2068;  /* movea.l 10(a0), a0   ln_Name             */
    var_buf[var_words++] = 0x000A;
    var_buf[var_words++] = 0x2008;  /* move.l a0, d0        NULL check          */
    var_buf[var_words++] = 0x6716;  /* beq.s +22 (.name_clear)  was +16, +6     */

    /* Validate first ln_Name byte is printable (>= 0x20) */
    var_buf[var_words++] = 0x0C10;  /* cmpi.b #0x20, (a0)   first byte < space? */
    var_buf[var_words++] = 0x0020;
    var_buf[var_words++] = 0x6510;  /* bcs.s +16 (.name_clear)  stale name      */

    var_buf[var_words++] = 0x43ED;  /* lea 106(a5), a1      &entry->task_name   */
    var_buf[var_words++] = 0x006A;
    var_buf[var_words++] = 0x7014;  /* moveq #20, d0        max 21 chars        */
    /* .tn_copy: */
    var_buf[var_words++] = 0x12D8;  /* move.b (a0)+, (a1)+                      */
    var_buf[var_words++] = 0x57C8;  /* dbeq d0, .tn_copy                        */
    var_buf[var_words++] = 0xFFFC;  /* displacement -4                           */
    var_buf[var_words++] = 0x4211;  /* clr.b (a1)           NUL-terminate       */
    var_buf[var_words++] = 0x6004;  /* bra.s +4 (.name_done)  unchanged         */
    /* .name_clear: */
    var_buf[var_words++] = 0x422D;  /* clr.b 106(a5)        empty task_name     */
    var_buf[var_words++] = 0x006A;
    /* .name_done: */

    /* Set valid=2 BEFORE the suffix's trampoline calls the original function.
     * This must happen pre-call because blocking functions (e.g. dos.RunCommand)
     * can block indefinitely.  With valid=0 during the block, the consumer
     * cannot advance past this slot, freezing ALL event consumption system-wide.
     *
     * The value 2 ("in-progress") distinguishes pre-call events from
     * post-call events (valid=1, set by the suffix post-call handler).
     * The daemon uses this to suppress IoErr display for events consumed
     * mid-flight: if the daemon polls while the original function is still
     * executing (e.g. Lock waiting for a DOS packet reply), retval and
     * ioerr fields are not yet filled.  The daemon sees valid=2 and skips
     * IoErr append.  After the function returns, the post-call handler
     * overwrites valid with 1, but the daemon has already consumed the
     * event -- the write is harmless. */
    var_buf[var_words++] = 0x1ABC;    /* move.b #2, (a5)  entry->valid = 2 */
    var_buf[var_words++] = 0x0002;    /* immediate byte 2, word-aligned    */

    /* ---- 2. Calculate total size and allocate ---- */

    /* Post-retval suffix insertion: 70-byte BSD patching block for
     * OpenLibrary, or 26-byte accept sockaddr capture block. */
    {
    int post_retval_insert = 0;
    if (patch->lib_id == LIB_EXEC && patch->lvo_offset == -552)
        post_retval_insert = BSD_PATCH_BLOCK_BYTES;
    else if (patch->lib_id == LIB_BSDSOCKET && patch->lvo_offset == -48)
        post_retval_insert = ACCEPT_BLOCK_BYTES;

    total_bytes = prefix_bytes + (var_words * 2) + STUB_SUFFIX_BYTES + post_retval_insert;
    alloc_size = (total_bytes + 3) & ~3;  /* ULONG-align */

    stub_mem = (UBYTE *)AllocMem(alloc_size, MEMF_PUBLIC | MEMF_CLEAR);
    if (!stub_mem)
        return -1;

    /* ---- 3. Assemble: prefix + variable + suffix ---- */

    /* The prefix is assembled from the 196-byte template with two
     * inline insertions at byte 56 (between task filter and MOVEM save):
     *
     *   1. Daemon task exclusion check (20 bytes, always emitted).
     *      Uses only a6 (saved/restored via stack), no data registers.
     *      When daemon_task is NULL, cmpa.l #0 never matches because
     *      ThisTask is always a valid non-zero pointer.
     *
     *   2. NULL-argument filter (8 bytes, conditional on skip_null_arg).
     *      Inserted after the daemon check at byte 76.
     *
     * Both must fire BEFORE the MOVEM push because the .disabled path
     * only pops saved_a5.
     *
     * Layout:
     *   bytes 0-55:    template fast-path checks + task filter
     *   bytes 56-75:   daemon task exclusion check (20 bytes, always)
     *   bytes 76-83:   NULL-argument check (8 bytes, conditional)
     *   bytes 76/84+:  template bytes 56-195 (MOVEM save through event header)
     */
#define DAEMON_INSERT_POINT  56  /* byte offset where daemon check is inserted */
#define DAEMON_INSERT_BYTES  20  /* size of daemon task check block */
#define STUB_PREFIX_BYTES_TEMPLATE 196  /* original template size before daemon insert */
#define NULL_INSERT_POINT  (DAEMON_INSERT_POINT + DAEMON_INSERT_BYTES)  /* 76 */

    /* Stage 1: Copy template bytes 0-55 (fast-path checks + task filter) */
    CopyMem((APTR)stub_prefix, (APTR)stub_mem, DAEMON_INSERT_POINT);

    /* Stage 2: Emit 20-byte daemon task exclusion check at byte 56 */
    {
        UWORD *dt = (UWORD *)(stub_mem + DAEMON_INSERT_POINT);
        dt[0] = 0x2F0E;  /* move.l a6, -(sp)              */
        dt[1] = 0x2C78;  /* movea.l 4.w, a6               */
        dt[2] = 0x0004;  /* SysBase at abs addr 4          */
        dt[3] = 0x2C6E;  /* movea.l 276(a6), a6  ThisTask  */
        dt[4] = 0x0114;  /* 276 = ExecBase.ThisTask        */
        dt[5] = 0xBDED;  /* cmpa.l d(a5), a6              */
        dt[6] = 0x0000;  /* displacement (patched below)   */
        dt[7] = 0x2C5F;  /* movea.l (sp)+, a6             */
        dt[8] = 0x6700;  /* beq.w .disabled                */
        dt[9] = 0x0000;  /* displacement (patched below)   */
        /* .no_daemon_check: byte 76 */
    }

    /* Stage 3: Handle NULL filter and copy rest of template */
    if (patch->skip_null_arg != 0) {
        UWORD *null_check;
        UWORD cmpa_opcode;

        /* Emit 8-byte NULL-argument check at byte 76 */
        null_check = (UWORD *)(stub_mem + NULL_INSERT_POINT);

        /* NULL-argument check: compare register to zero.
         * For address registers: cmpa.w #0, An (4 bytes)
         *   CMPA format: 1011 rrr 011 111 100
         *   a0: 0xB0FC, a1: 0xB2FC
         *   CMPA.W sign-extends immediate to 32 bits -- full-width test.
         * For data registers: tst.l Dn (2 bytes) + nop (2 bytes)
         *   TST.L format: 0100 1010 10 000 rrr
         *   d0: 0x4A80, d1: 0x4A81
         *   Must use .l -- .w would only test low 16 bits, causing
         *   false NULL matches for BPTRs like 0x00010000. */
        if (patch->skip_null_arg >= REG_A0) {
            /* Address register: cmpa.w #0, An (4 bytes: opcode + immediate) */
            cmpa_opcode = 0xB0FC | ((patch->skip_null_arg - REG_A0) << 9);
            null_check[0] = cmpa_opcode;  /* cmpa.w #0, An        */
            null_check[1] = 0x0000;       /* immediate 0           */
        } else {
            /* Data register: tst.l Dn (2 bytes) + nop (2 bytes) */
            cmpa_opcode = 0x4A80 | (patch->skip_null_arg - REG_D0);
            null_check[0] = cmpa_opcode;  /* tst.l Dn              */
            null_check[1] = 0x4E71;       /* nop (pad to 4 bytes)  */
        }
        null_check[2] = 0x6700;       /* beq.w .disabled      */
        null_check[3] = 0x0000;       /* displacement (patched below) */

        /* Copy template bytes 56-195 -> stub bytes 84-223 */
        CopyMem((APTR)((UBYTE *)stub_prefix + DAEMON_INSERT_POINT),
                (APTR)(stub_mem + NULL_INSERT_POINT + 8),
                STUB_PREFIX_BYTES_TEMPLATE - DAEMON_INSERT_POINT);
    } else {
        /* No NULL filter -- copy template bytes 56-195 -> stub bytes 76-215 */
        CopyMem((APTR)((UBYTE *)stub_prefix + DAEMON_INSERT_POINT),
                (APTR)(stub_mem + NULL_INSERT_POINT),
                STUB_PREFIX_BYTES_TEMPLATE - DAEMON_INSERT_POINT);
    }

    CopyMem((APTR)var_buf, (APTR)(stub_mem + prefix_bytes),
            var_words * 2);

    suffix_start = prefix_bytes + (var_words * 2);

    if (post_retval_insert > 0) {
        /* Insert a post-retval block between suffix bytes 48 and 52.
         * Copy suffix bytes 0-51 (26 words), emit the block,
         * then copy suffix bytes 52-155 (52 words). */
#define POST_RETVAL_INSERT_POINT 52  /* suffix byte offset of insertion point */

        /* Copy suffix bytes 0-51 */
        CopyMem((APTR)stub_suffix,
                (APTR)(stub_mem + suffix_start),
                POST_RETVAL_INSERT_POINT);

        if (patch->lib_id == LIB_EXEC && patch->lvo_offset == -552) {
        /* Emit 70-byte BSD patching block (35 words) for OpenLibrary */
        {
            UWORD *bsd = (UWORD *)(stub_mem + suffix_start + POST_RETVAL_INSERT_POINT);

            /* === BSD patching check (14 bytes) === */
            bsd[0]  = 0x4A28;  /*  +0: tst.b 99(a0)              */
            bsd[1]  = 0x0063;  /*       offset 99 = bsd_flag      */
            bsd[2]  = 0x6700;  /*  +4: beq.w .no_bsd_patch        */
            bsd[3]  = 0x0040;  /*       disp = 64                 */
            bsd[4]  = 0x4A80;  /*  +8: tst.l d0                   */
            bsd[5]  = 0x6700;  /* +10: beq.w .no_bsd_patch        */
            bsd[6]  = 0x003A;  /*       disp = 58                 */

            /* === Save registers (4 bytes) === */
            bsd[7]  = 0x48E7;  /* +14: movem.l d1-d2/a1-a3/a6, -(sp) */
            bsd[8]  = 0x6072;  /*       save mask                 */

            /* === Load SysBase and table (16 bytes) === */
            bsd[9]  = 0x2C78;  /* +18: movea.l 4.w, a6            */
            bsd[10] = 0x0004;  /*       SysBase at abs addr 4     */
            bsd[11] = 0x247C;  /* +22: movea.l #BSD_TABLE, a2     */
            bsd[12] = 0x0000;  /*       [high word - patched]     */
            bsd[13] = 0x0000;  /*       [low word - patched]      */
            bsd[14] = 0x240A;  /* +28: move.l a2, d2              */
            bsd[15] = 0x6700;  /* +30: beq.w .bsd_done            */
            bsd[16] = 0x001C;  /*       disp = 28                 */

            /* === Setup loop (4 bytes) === */
            bsd[17] = 0x2640;  /* +34: movea.l d0, a3             */
            bsd[18] = 0x740E;  /* +36: moveq #14, d2              */

            /* === SetFunction loop (18 bytes) === */
            /* .bsd_loop: */
            bsd[19] = 0x224B;  /* +38: movea.l a3, a1             */
            bsd[20] = 0x3052;  /* +40: movea.w (a2), a0           */
            bsd[21] = 0x202A;  /* +42: move.l 4(a2), d0           */
            bsd[22] = 0x0004;  /*       offset 4 = stub_code      */
            bsd[23] = 0x4EAE;  /* +46: jsr -420(a6)  SetFunction  */
            bsd[24] = 0xFE5C;  /*       -420 = 0xFE5C             */
            bsd[25] = 0x504A;  /* +50: addq.l #8, a2              */
            bsd[26] = 0x51CA;  /* +52: dbf d2, .bsd_loop          */
            bsd[27] = 0xFFF0;  /*       disp = -16                */

            /* === Cache flush (4 bytes) === */
            bsd[28] = 0x4EAE;  /* +56: jsr -636(a6)  CacheClearU  */
            bsd[29] = 0xFD84;  /*       -636 = 0xFD84             */

            /* === Restore registers and reload (10 bytes) === */
            /* .bsd_done: */
            bsd[30] = 0x4CDF;  /* +60: movem.l (sp)+, d1-d2/a1-a3/a6 */
            bsd[31] = 0x4E06;  /*       restore mask              */
            bsd[32] = 0x206F;  /* +64: movea.l 8(sp), a0          */
            bsd[33] = 0x0008;  /*       reload entry ptr          */
            bsd[34] = 0x2017;  /* +68: move.l (sp), d0            */
            /* .no_bsd_patch: (byte 70, continues to IoErr check) */
        }
        } else if (patch->lib_id == LIB_BSDSOCKET && patch->lvo_offset == -48) {
        /* Emit 26-byte accept sockaddr capture block (13 words).
         * Dereferences the sockaddr pointer (saved in args[1] during
         * variable region) and copies 8 bytes to string_data[0..7].
         * Only fires when accept succeeded (d0 != -1). */
        {
            UWORD *acc = (UWORD *)(stub_mem + suffix_start + POST_RETVAL_INSERT_POINT);

            acc[0]  = 0x0C80;  /*  +0: cmpi.l #-1, d0             */
            acc[1]  = 0xFFFF;  /*       high word of -1            */
            acc[2]  = 0xFFFF;  /*       low word of -1             */
            acc[3]  = 0x6712;  /*  +6: beq.s +18 (.skip_accept)   */
            acc[4]  = 0x2268;  /*  +8: movea.l 16(a0), a1         */
            acc[5]  = 0x0010;  /*       offset 16 = args[1]        */
            acc[6]  = 0x2209;  /* +12: move.l a1, d1 (NULL check) */
            acc[7]  = 0x670A;  /* +14: beq.s +10 (.skip_accept)   */
            acc[8]  = 0x2151;  /* +16: move.l (a1), 34(a0)        */
            acc[9]  = 0x0022;  /*       string_data offset         */
            acc[10] = 0x2169;  /* +20: move.l 4(a1), 38(a0)       */
            acc[11] = 0x0004;  /*       source offset 4            */
            acc[12] = 0x0026;  /*       dest offset 38             */
            /* .skip_accept: byte 26 */
        }
        }

        /* Copy suffix bytes 52-155 (shifted past post-retval block) */
        CopyMem((APTR)((UBYTE *)stub_suffix + POST_RETVAL_INSERT_POINT),
                (APTR)(stub_mem + suffix_start + POST_RETVAL_INSERT_POINT + post_retval_insert),
                STUB_SUFFIX_BYTES - POST_RETVAL_INSERT_POINT);
    } else {
        /* Standard suffix: copy full suffix template */
        CopyMem((APTR)stub_suffix, (APTR)(stub_mem + suffix_start),
                STUB_SUFFIX_BYTES);
    }

    p = (UWORD *)stub_mem;

    /* ---- 4. Patch addresses and displacements ---- */

    /* For NULL-filtered functions, all prefix byte offsets
     * at or after NULL_INSERT_POINT (76) are shifted by +8 because the
     * 8-byte NULL check was inserted there.  ns (null_shift) is 0 or 8.
     *
     * Byte ranges:
     *   0-55:   no shift (fast-path checks, task filter)
     *   56-75:  no shift (daemon task check block, always present)
     *   76+:    shifted by ns (0 or 8) */
    {
        int ns = (patch->skip_null_arg != 0) ? 8 : 0;

        /* PATCH_ADDR -- 4 occurrences:
         *   3 in prefix (fixed offsets), 1 in suffix (suffix-relative) */
        {
            ULONG pa = (ULONG)patch;
            patch_addr(p, PATCH_OFF_1, pa);          /* prefix byte 4: enable check (< 76, no shift) */
            patch_addr(p, PATCH_OFF_2 + ns, pa);     /* prefix byte 122: use_count inc (>= 76, shift) */
            patch_addr(p, PATCH_OFF_3 + ns, pa);     /* prefix byte 190: lib_id/lvo copy (>= 76, shift) */
            /* occurrence 4 is in suffix at suffix-relative offset PATCH_SUFFIX_REL
             * (shifted by post_retval_insert because it's after byte 34) */
            patch_addr(p, suffix_start + PATCH_SUFFIX_REL + post_retval_insert, pa);
        }

        /* ANCHOR_ADDR -- 1 occurrence (prefix byte 18, < 76, no shift) */
        patch_addr(p, ANCHOR_OFF_1, (ULONG)anchor);

        /* RING_ENTRIES_ADDR -- 1 occurrence (prefix byte 150, >= 76, shift) */
        patch_addr(p, ENTRIES_OFF_1 + ns, (ULONG)entries);

        /* TIMER_BASE_ADDR -- 1 occurrence (prefix byte 158, >= 76, shift) */
        patch_addr(p, TIMER_BASE_OFF + ns, (ULONG)anchor->timer_base);

        /* DOS_BASE_ADDR -- 1 occurrence (suffix, IoErr block -- after byte 34, shifted) */
        if (dos_base != 0) {
            patch_addr(p, suffix_start + DOS_BASE_SUFFIX_REL + post_retval_insert, dos_base);
        }

        /* Struct field displacements (prefix -- patched from offsetof).
         * Offsets < 76 are unshifted; offsets >= 76 are shifted by ns. */
        p[DISP_ENABLED / 2]            = (UWORD)offsetof(struct atrace_patch, enabled);       /* byte 10, no shift */
        p[(DISP_USE_COUNT_INC + ns) / 2] = (UWORD)offsetof(struct atrace_patch, use_count);   /* byte 128, shift */
        p[DISP_GLOBAL_ENABLE / 2]      = (UWORD)offsetof(struct atrace_anchor, global_enable); /* byte 24, no shift */
        p[DISP_FILTER_TASK_1 / 2]      = (UWORD)offsetof(struct atrace_anchor, filter_task);   /* byte 32, no shift */
        p[DISP_FILTER_TASK_2 / 2]      = (UWORD)offsetof(struct atrace_anchor, filter_task);   /* byte 48, no shift */
        /* DISP_DAEMON_TASK_1 is at byte 68 (within daemon block, no ns shift) */
        p[DISP_DAEMON_TASK_1 / 2]      = (UWORD)offsetof(struct atrace_anchor, daemon_task);
        p[(DISP_RING + ns) / 2]        = (UWORD)offsetof(struct atrace_anchor, ring);          /* byte 90, shift */
        p[(DISP_EVENT_SEQ_RD + ns) / 2] = (UWORD)offsetof(struct atrace_anchor, event_sequence); /* byte 132, shift */
        p[(DISP_EVENT_SEQ_WR + ns) / 2] = (UWORD)offsetof(struct atrace_anchor, event_sequence); /* byte 136, shift */
        p[(DISP_WRITE_POS_RD + ns) / 2] = (UWORD)offsetof(struct atrace_ringbuf, write_pos);  /* byte 94, shift */
        p[(DISP_CAPACITY + ns) / 2]    = (UWORD)offsetof(struct atrace_ringbuf, capacity);     /* byte 102, shift */
        p[(DISP_READ_POS + ns) / 2]    = (UWORD)offsetof(struct atrace_ringbuf, read_pos);     /* byte 110, shift */
        p[(DISP_WRITE_POS_WR + ns) / 2] = (UWORD)offsetof(struct atrace_ringbuf, write_pos);  /* byte 118, shift */

        /* Suffix displacement patches (suffix-relative offsets, shifted by post_retval_insert
         * for offsets after the BSD insertion point at suffix byte 52).
         * The sequence guard bne.w at suffix byte 44 jumps to .guard_skip
         * at suffix byte 96; when a post-retval block is inserted between
         * bytes 52 and 96, the displacement must grow by post_retval_insert. */
        {
            int s = suffix_start;

            /* Sequence guard bne.w displacement at suffix byte 46 */
            if (post_retval_insert > 0) {
                p[(s + 46) / 2] = (UWORD)(50 + post_retval_insert);
            }
            p[(s + SUFFIX_DISP_USE_COUNT_DEC + post_retval_insert) / 2] =
                (UWORD)offsetof(struct atrace_patch, use_count);
            p[(s + SUFFIX_DISP_OVERFLOW + post_retval_insert) / 2] =
                (UWORD)offsetof(struct atrace_ringbuf, overflow);

            /* Overflow path displacement patches (all after insertion point) */
            p[(s + OVF_DISP_CAPACITY_1 + post_retval_insert) / 2] =
                (UWORD)offsetof(struct atrace_ringbuf, capacity);
            p[(s + OVF_DISP_READ_POS_WR + post_retval_insert) / 2] =
                (UWORD)offsetof(struct atrace_ringbuf, read_pos);
            p[(s + OVF_DISP_CAPACITY_2 + post_retval_insert) / 2] =
                (UWORD)offsetof(struct atrace_ringbuf, capacity);

            /* bra.w displacement in overflow path: target is the
             * move.l d1, OFS_WRITE_POS(a0) opcode at prefix byte
             * (DISP_WRITE_POS_WR - 2 + ns) = 116+ns.
             * The bra.w opcode is at stub byte (s + 152 + post_retval_insert).
             * 68k bra.w displacement = target - (bra_opcode_addr + 2). */
            {
                int bra_opcode = s + 152 + post_retval_insert;
                int target = DISP_WRITE_POS_WR - 2 + ns;  /* opcode, not displacement */
                p[(s + OVF_BRA_DISP + post_retval_insert) / 2] =
                    (UWORD)(target - (bra_opcode + 2));
            }
        }

        /* Branch displacements (prefix to suffix).
         * Branches at bytes < 76 are unshifted in position but their
         * displacements grow because the target (.disabled/.overflow)
         * moved further away.  The beq.w .overflow at byte 112 shifts
         * to byte 112+ns. */
        {
            int disabled_byte = suffix_start + SUFFIX_LABEL_DISABLED + post_retval_insert;
            int overflow_byte = suffix_start + SUFFIX_LABEL_OVERFLOW + post_retval_insert;

            /* beq.w .disabled at byte 12: displacement word at byte 14 (< 76, no pos shift) */
            p[BEQ_DISABLED_1 / 2] = (UWORD)(disabled_byte - (12 + 2));
            /* beq.w .disabled at byte 26: displacement word at byte 28 (< 76, no pos shift) */
            p[BEQ_DISABLED_2 / 2] = (UWORD)(disabled_byte - (26 + 2));
            /* bne.w .disabled at byte 52: displacement word at byte 54 (< 76, no pos shift) */
            p[BNE_DISABLED_3 / 2] = (UWORD)(disabled_byte - (52 + 2));
            /* beq.w .disabled at byte 72 (daemon check): disp word at byte 74 (< 76, no pos shift) */
            p[BEQ_DISABLED_4 / 2] = (UWORD)(disabled_byte - (72 + 2));
            /* beq.w .overflow at byte 112 (>= 76, shifts to 112+ns): displacement word at 114+ns */
            p[(BEQ_OVERFLOW + ns) / 2] = (UWORD)(overflow_byte - (112 + ns + 2));

            /* NULL-argument filter beq.w .disabled at byte 76 */
            if (patch->skip_null_arg != 0) {
                /* beq.w at byte 80 (NULL_INSERT_POINT + 4), displacement at byte 82 */
                p[(NULL_INSERT_POINT + 6) / 2] =
                    (UWORD)(disabled_byte - (NULL_INSERT_POINT + 4 + 2));
            }
        }
    }

    /* ---- 5. Flush and install ---- */

    CacheClearU();

    Disable();
    old_addr = SetFunction(libbase, patch->lvo_offset,
                           (APTR)((ULONG)stub_mem));

    /* Patch ORIG_ADDR (2 occurrences, both in suffix).
     * ORIG_SUFFIX_REL_1 is before the BSD insertion point (byte 24).
     * ORIG_SUFFIX_REL_2 is after it, so add post_retval_insert.
     * (Old ORIG_SUFFIX_REL_3 in overflow path was deleted -- circular
     * overflow branches back to prefix instead of tail-calling original.) */
    {
        ULONG oa = (ULONG)old_addr;
        patch_addr(p, suffix_start + ORIG_SUFFIX_REL_1, oa);
        patch_addr(p, suffix_start + ORIG_SUFFIX_REL_2 + post_retval_insert, oa);
    }
    CacheClearU();
    Enable();

    }  /* end post_retval_insert scope */

    /* 6. Fill patch descriptor */
    patch->original = old_addr;
    patch->stub_code = stub_mem;
    patch->stub_size = alloc_size;

    /* Return suffix_start for late-patching by caller */
    if (out_suffix_start)
        *out_suffix_start = suffix_start;

    return 0;
}
