/*
 * atrace -- function table
 *
 * Phase 1: single entry (exec.OpenLibrary).
 */

#include "atrace.h"

/* exec.library functions */
static struct func_info exec_funcs[] = {
    {
        "OpenLibrary",      /* name */
        -552,               /* lvo_offset */
        2,                  /* arg_count: a1=libName, d0=version */
        { REG_A1, REG_D0,  /* arg_regs[0..1] */
          0, 0, 0, 0, 0, 0 },
        REG_D0,             /* ret_reg (d0 = libBase) */
        0x01,               /* string_args: bit 0 = arg0 is string */
        0                   /* padding */
    }
};

/* Library table */
struct lib_info atrace_libs[] = {
    {
        "exec.library",     /* name */
        exec_funcs,         /* funcs */
        1,                  /* func_count */
        LIB_EXEC,           /* lib_id = 0 */
        0                   /* padding */
    }
};

int atrace_lib_count = 1;
