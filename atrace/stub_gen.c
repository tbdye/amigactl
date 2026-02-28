/*
 * atrace -- stub template and code generator
 *
 * Phase 1: hardcoded stub for exec.OpenLibrary.
 * The stub template is a UWORD array of big-endian 68k opcodes.
 * The code generator copies the template, patches in runtime
 * addresses and struct field offsets, then installs via SetFunction.
 */

#include "atrace.h"

#include <proto/exec.h>

#include <string.h>
#include <stddef.h>  /* offsetof */

/*
 * Stub template -- UWORD array, big-endian 68k opcodes.
 *
 * Placeholder values:
 *   0x0000 0x0000 at PATCH_ADDR slots  (4 occurrences)
 *   0x0000 0x0000 at ANCHOR_ADDR slot  (1 occurrence)
 *   0x0000 0x0000 at RING_ENTRIES slot (1 occurrence)
 *   0x0000 0x0000 at ORIG_ADDR slots   (3 occurrences)
 *   0x0000 at struct offset displacement slots (patched from offsetof)
 *   0x0000 at branch displacement slots (computed from label offsets)
 */
static const UWORD stub_template[] = {
    /* === PHASE 1: Fast path checks === */
    /*  0: */ 0x2F0D,                   /* move.l a5, -(sp)                     */
    /*  2: */ 0x2A7C, 0x0000, 0x0000,   /* movea.l #PATCH_ADDR, a5   [1]       */
    /*  8: */ 0x4AAD, 0x0000,           /* tst.l OFS_ENABLED(a5)               */
    /* 12: */ 0x6700, 0x0000,           /* beq.w .disabled                      */
    /* 16: */ 0x2A7C, 0x0000, 0x0000,   /* movea.l #ANCHOR_ADDR, a5            */
    /* 22: */ 0x4AAD, 0x0000,           /* tst.l OFS_GLOBAL_ENABLE(a5)         */
    /* 26: */ 0x6700, 0x0000,           /* beq.w .disabled                      */

    /* === PHASE 2: Save all volatile registers === */
    /* 30: */ 0x48E7, 0xFFFA,           /* movem.l d0-d7/a0-a4/a6, -(sp)       */

    /* === PHASE 3: Reserve ring buffer slot === */
    /* 34: */ 0x2C78, 0x0004,           /* movea.l 4.w, a6  (SysBase)          */
    /* 38: */ 0x4EAE, 0xFF88,           /* jsr _LVODisable(a6)  = -120         */
    /* 42: */ 0x206D, 0x0000,           /* movea.l OFS_RING(a5), a0            */
    /* 46: */ 0x2028, 0x0000,           /* move.l OFS_WRITE_POS(a0), d0        */
    /* 50: */ 0x2200,                   /* move.l d0, d1                        */
    /* 52: */ 0x5281,                   /* addq.l #1, d1                        */
    /* 54: */ 0xB2A8, 0x0000,           /* cmp.l OFS_CAPACITY(a0), d1          */
    /* 58: */ 0x6502,                   /* bcs.s .nowrap (+2)                   */
    /* 60: */ 0x7200,                   /* moveq #0, d1                         */
    /* .nowrap: */
    /* 62: */ 0xB2A8, 0x0000,           /* cmp.l OFS_READ_POS(a0), d1          */
    /* 66: */ 0x6700, 0x0000,           /* beq.w .overflow                      */
    /* 70: */ 0x2141, 0x0000,           /* move.l d1, OFS_WRITE_POS(a0)        */
    /* 74: */ 0x207C, 0x0000, 0x0000,   /* movea.l #PATCH_ADDR, a0   [2]       */
    /* 80: */ 0x52A8, 0x0000,           /* addq.l #1, OFS_USE_COUNT(a0)        */
    /* 84: */ 0x222D, 0x0000,           /* move.l OFS_EVENT_SEQ(a5), d1        */
    /* 88: */ 0x52AD, 0x0000,           /* addq.l #1, OFS_EVENT_SEQ(a5)        */
    /* 92: */ 0x2400,                   /* move.l d0, d2                        */
    /* 94: */ 0x2601,                   /* move.l d1, d3                        */
    /* 96: */ 0x4EAE, 0xFF82,           /* jsr _LVOEnable(a6)  = -126          */

    /* === PHASE 4: Fill event entry === */
    /*100: */ 0xED82,                   /* asl.l #6, d2                         */
    /*102: */ 0x2A7C, 0x0000, 0x0000,   /* movea.l #RING_ENTRIES_ADDR, a5      */
    /*108: */ 0xDBC2,                   /* adda.l d2, a5                        */
    /*110: */ 0x2B43, 0x0004,           /* move.l d3, 4(a5)  entry->sequence   */
    /*114: */ 0x207C, 0x0000, 0x0000,   /* movea.l #PATCH_ADDR, a0   [3]       */
    /*120: */ 0x1B68, 0x0000, 0x0001,   /* move.b 0(a0), 1(a5)  lib_id        */
    /*126: */ 0x3B68, 0x0002, 0x0002,   /* move.w 2(a0), 2(a5)  lvo_offset    */
    /*132: */ 0x2C78, 0x0004,           /* movea.l 4.w, a6  (SysBase)          */
    /*136: */ 0x2B6E, 0x0114, 0x0008,   /* move.l 276(a6), 8(a5) caller_task  */

    /* --- Argument copy (OpenLibrary: a1@frame+36, d0@frame+0) --- */
    /*142: */ 0x2B6F, 0x0024, 0x000C,   /* move.l 36(sp), 12(a5) args[0]=a1   */
    /*148: */ 0x2B6F, 0x0000, 0x0010,   /* move.l 0(sp), 16(a5)  args[1]=d0   */

    /* --- arg_count --- */
    /*154: */ 0x1B7C, 0x0002, 0x0020,   /* move.b #2, 32(a5)  arg_count=2     */

    /* --- String capture (arg0 = libName from a1@frame+36) --- */
    /*160: */ 0x206F, 0x0024,           /* movea.l 36(sp), a0   source string  */
    /*164: */ 0x43ED, 0x0022,           /* lea 34(a5), a1       dest=string_data*/
    /*168: */ 0x7016,                   /* moveq #22, d0        max iterations  */
    /* .strcopy: */
    /*170: */ 0x12D8,                   /* move.b (a0)+, (a1)+                  */
    /*172: */ 0x57C8, 0xFFFC,           /* dbeq d0, .strcopy (-4)              */
    /*176: */ 0x4211,                   /* clr.b (a1)           NUL terminate   */

    /* === PHASE 5: Call original function === */
    /*178: */ 0x4CDF, 0x5FFF,           /* movem.l (sp)+, d0-d7/a0-a4/a6       */
    /*182: */ 0x204D,                   /* movea.l a5, a0       a0=entry ptr    */
    /*184: */ 0x2A5F,                   /* movea.l (sp)+, a5    restore a5      */
    /*186: */ 0x2F08,                   /* move.l a0, -(sp)     push entry ptr  */
    /*188: */ 0x487A, 0x000A,           /* pea 10(pc)           push .post_call */
    /*192: */ 0x2F3C, 0x0000, 0x0000,   /* move.l #ORIG_ADDR, -(sp)  [1]       */
    /*198: */ 0x4E75,                   /* rts                  jump to original*/

    /* === PHASE 6: Post-call === */
    /* .post_call: */
    /*200: */ 0x2F00,                   /* move.l d0, -(sp)     save retval     */
    /*202: */ 0x206F, 0x0004,           /* movea.l 4(sp), a0    entry ptr       */
    /*206: */ 0x2140, 0x001C,           /* move.l d0, 28(a0)    entry->retval   */
    /*210: */ 0x10BC, 0x0001,           /* move.b #1, (a0)      entry->valid=1  */
    /*214: */ 0x207C, 0x0000, 0x0000,   /* movea.l #PATCH_ADDR, a0  [4]        */
    /*220: */ 0x53A8, 0x0000,           /* subq.l #1, OFS_USE_COUNT(a0)        */
    /*224: */ 0x201F,                   /* move.l (sp)+, d0     restore retval  */
    /*226: */ 0x588F,                   /* addq.l #4, sp        pop entry ptr   */
    /*228: */ 0x4E75,                   /* rts                  return to caller*/

    /* === DISABLED fast path === */
    /* .disabled: */
    /*230: */ 0x2A5F,                   /* movea.l (sp)+, a5    restore a5      */
    /*232: */ 0x2F3C, 0x0000, 0x0000,   /* move.l #ORIG_ADDR, -(sp)  [2]       */
    /*238: */ 0x4E75,                   /* rts                  tail-call orig  */

    /* === OVERFLOW path === */
    /* .overflow: */
    /*240: */ 0x52A8, 0x0000,           /* addq.l #1, OFS_OVERFLOW(a0)         */
    /*244: */ 0x4EAE, 0xFF82,           /* jsr _LVOEnable(a6)                  */
    /*248: */ 0x4CDF, 0x5FFF,           /* movem.l (sp)+, d0-d7/a0-a4/a6       */
    /*252: */ 0x2A5F,                   /* movea.l (sp)+, a5    restore a5      */
    /*254: */ 0x2F3C, 0x0000, 0x0000,   /* move.l #ORIG_ADDR, -(sp)  [3]       */
    /*260: */ 0x4E75,                   /* rts                  tail-call orig  */
};

#define STUB_TEMPLATE_WORDS  (sizeof(stub_template) / sizeof(UWORD))
#define STUB_TEMPLATE_BYTES  (sizeof(stub_template))
/* STUB_TEMPLATE_BYTES should be 262. Allocation pads to 264. */
#define STUB_ALLOC_SIZE      ((STUB_TEMPLATE_BYTES + 3) & ~3)  /* ULONG-align */

/* ---- Patch address byte offsets (high word of each 32-bit address) ---- */

/* PATCH_ADDR -- 4 occurrences (byte offsets of the high word) */
#define PATCH_OFF_1     4    /* Phase 1: per-patch enable check        */
#define PATCH_OFF_2    76    /* Phase 3: use_count increment           */
#define PATCH_OFF_3   116    /* Phase 4: lib_id/lvo_offset copy        */
#define PATCH_OFF_4   216    /* Phase 6: use_count decrement           */

/* ANCHOR_ADDR -- 1 occurrence */
#define ANCHOR_OFF_1   18    /* Phase 1: global enable check           */

/* RING_ENTRIES_ADDR -- 1 occurrence */
#define ENTRIES_OFF_1  104   /* Phase 4: entry base address            */

/* ORIG_ADDR -- 3 occurrences */
#define ORIG_OFF_1     194   /* Phase 5: trampoline push               */
#define ORIG_OFF_2     234   /* .disabled: tail-call push              */
#define ORIG_OFF_3     256   /* .overflow: tail-call push              */

/* ---- Struct field displacement patches (byte offset -> offsetof) ---- */

/* atrace_patch field displacements */
#define DISP_ENABLED       10   /* tst.l OFS_ENABLED(a5): word at byte 10 */
#define DISP_USE_COUNT_INC 82   /* addq.l #1, OFS_USE_COUNT(a0): byte 82  */
#define DISP_USE_COUNT_DEC 222  /* subq.l #1, OFS_USE_COUNT(a0): byte 222 */

/* atrace_anchor field displacements */
#define DISP_GLOBAL_ENABLE 24   /* tst.l OFS_GLOBAL_ENABLE(a5): byte 24   */
#define DISP_RING          44   /* movea.l OFS_RING(a5), a0: byte 44      */
#define DISP_EVENT_SEQ_RD  86   /* move.l OFS_EVENT_SEQ(a5), d1: byte 86  */
#define DISP_EVENT_SEQ_WR  90   /* addq.l #1, OFS_EVENT_SEQ(a5): byte 90  */

/* atrace_ringbuf field displacements */
#define DISP_WRITE_POS_RD  48   /* move.l OFS_WRITE_POS(a0), d0: byte 48  */
#define DISP_CAPACITY      56   /* cmp.l OFS_CAPACITY(a0), d1: byte 56    */
#define DISP_READ_POS      64   /* cmp.l OFS_READ_POS(a0), d1: byte 64    */
#define DISP_WRITE_POS_WR  72   /* move.l d1, OFS_WRITE_POS(a0): byte 72  */
#define DISP_OVERFLOW      242  /* addq.l #1, OFS_OVERFLOW(a0): byte 242  */

/* ---- Branch displacement patches ---- */

/* Branch displacements (byte offset of the 16-bit displacement word).
 * Displacement = target_byte - (branch_instruction_byte + 2).
 * The +2 accounts for the opcode word; the displacement is relative
 * to the address of the extension word. */

/* beq.w .disabled at byte 12: target .disabled = byte 230.
 * Displacement = 230 - (12 + 2) = 216 = 0x00D8 */
#define BEQ_DISABLED_1     14   /* displacement word at byte 14 */
#define BEQ_DISABLED_1_VAL 216

/* beq.w .disabled at byte 26: target .disabled = byte 230.
 * Displacement = 230 - (26 + 2) = 202 = 0x00CA */
#define BEQ_DISABLED_2     28   /* displacement word at byte 28 */
#define BEQ_DISABLED_2_VAL 202

/* beq.w .overflow at byte 66: target .overflow = byte 240.
 * Displacement = 240 - (66 + 2) = 172 = 0x00AC */
#define BEQ_OVERFLOW       68   /* displacement word at byte 68 */
#define BEQ_OVERFLOW_VAL   172

/* ---- Helper: patch a 32-bit address into the stub ---- */

static void patch_addr(UWORD *stub, int byte_offset, ULONG addr)
{
    stub[byte_offset / 2]     = (UWORD)(addr >> 16);
    stub[byte_offset / 2 + 1] = (UWORD)(addr);
}

/* ---- Code generator ---- */

/* Generate and install a stub for one patched function.
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
    APTR old_addr;
    ULONG pa;
    ULONG aa;
    ULONG ea;
    ULONG oa;

    /* 1. Allocate stub memory */
    stub_mem = (UBYTE *)AllocMem(STUB_ALLOC_SIZE, MEMF_PUBLIC | MEMF_CLEAR);
    if (!stub_mem)
        return -1;

    /* 2. Copy the template */
    CopyMem((APTR)stub_template, (APTR)stub_mem, STUB_TEMPLATE_BYTES);

    p = (UWORD *)stub_mem;

    /* 3. Patch PATCH_ADDR (4 occurrences) */
    pa = (ULONG)patch;
    patch_addr(p, PATCH_OFF_1, pa);
    patch_addr(p, PATCH_OFF_2, pa);
    patch_addr(p, PATCH_OFF_3, pa);
    patch_addr(p, PATCH_OFF_4, pa);

    /* 4. Patch ANCHOR_ADDR (1 occurrence) */
    aa = (ULONG)anchor;
    patch_addr(p, ANCHOR_OFF_1, aa);

    /* 5. Patch RING_ENTRIES_ADDR (1 occurrence) */
    ea = (ULONG)entries;
    patch_addr(p, ENTRIES_OFF_1, ea);

    /* 6. Patch struct field displacements using offsetof() */
    p[DISP_ENABLED / 2]       = (UWORD)offsetof(struct atrace_patch, enabled);
    p[DISP_USE_COUNT_INC / 2] = (UWORD)offsetof(struct atrace_patch, use_count);
    p[DISP_USE_COUNT_DEC / 2] = (UWORD)offsetof(struct atrace_patch, use_count);
    p[DISP_GLOBAL_ENABLE / 2] = (UWORD)offsetof(struct atrace_anchor, global_enable);
    p[DISP_RING / 2]          = (UWORD)offsetof(struct atrace_anchor, ring);
    p[DISP_EVENT_SEQ_RD / 2]  = (UWORD)offsetof(struct atrace_anchor, event_sequence);
    p[DISP_EVENT_SEQ_WR / 2]  = (UWORD)offsetof(struct atrace_anchor, event_sequence);
    p[DISP_WRITE_POS_RD / 2]  = (UWORD)offsetof(struct atrace_ringbuf, write_pos);
    p[DISP_CAPACITY / 2]      = (UWORD)offsetof(struct atrace_ringbuf, capacity);
    p[DISP_READ_POS / 2]      = (UWORD)offsetof(struct atrace_ringbuf, read_pos);
    p[DISP_WRITE_POS_WR / 2]  = (UWORD)offsetof(struct atrace_ringbuf, write_pos);
    p[DISP_OVERFLOW / 2]      = (UWORD)offsetof(struct atrace_ringbuf, overflow);

    /* 7. Patch branch displacements */
    p[BEQ_DISABLED_1 / 2] = (UWORD)BEQ_DISABLED_1_VAL;
    p[BEQ_DISABLED_2 / 2] = (UWORD)BEQ_DISABLED_2_VAL;
    p[BEQ_OVERFLOW / 2]   = (UWORD)BEQ_OVERFLOW_VAL;

    /* 8. Flush CPU instruction cache */
    CacheClearU();

    /* 9. Install under Disable/Enable (race-free).
     *    SetFunction returns the old function pointer, which we need
     *    to patch into the stub's ORIG_ADDR slots. The stub is already
     *    in memory, so we patch in-place under Disable. */
    Disable();
    old_addr = SetFunction(libbase, patch->lvo_offset,
                           (APTR)((ULONG)stub_mem));

    /* Patch all 3 ORIG_ADDR occurrences */
    oa = (ULONG)old_addr;
    patch_addr(p, ORIG_OFF_1, oa);
    patch_addr(p, ORIG_OFF_2, oa);
    patch_addr(p, ORIG_OFF_3, oa);

    CacheClearU();
    Enable();

    /* 10. Fill in patch descriptor fields */
    patch->original = old_addr;
    patch->stub_code = stub_mem;
    patch->stub_size = STUB_ALLOC_SIZE;

    return 0;
}
