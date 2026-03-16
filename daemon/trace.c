/*
 * amigactld -- Library call tracing
 *
 * Implements TRACE STATUS, TRACE START/STOP streaming,
 * TRACE ENABLE/DISABLE, and per-client event filtering.
 * Follows the TAIL module pattern (tail.c).
 *
 * Function name lookup, task name cache, per-function
 * argument formatting, per-patch STATUS reporting, server-side
 * filters (LIB, FUNC, PROC, ERRORS), ENABLE/DISABLE commands.
 */

#include "trace.h"
#include "daemon.h"
#include "net.h"
#include "exec.h"
#include "../atrace/atrace.h"

#include <proto/exec.h>
#include <proto/dos.h>
#include <proto/bsdsocket.h>
#include <exec/execbase.h>
#include <dos/dostags.h>
#include <dos/dosextens.h>
#include <sys/socket.h>
#include <sys/errno.h>

#include <stdio.h>
#include <string.h>

/* ---- Function/library name lookup table ---- */

/* Must match the function table in atrace/funcs.c exactly.
 * The daemon cannot access atrace's table directly (separate binary),
 * so it maintains its own static copy for name resolution. */

/* Error classification for the ERRORS filter.
 * Determines what return values constitute an error for each function. */
#define ERR_CHECK_NULL      0   /* error when retval == 0 (NULL/FALSE) -- most functions */
#define ERR_CHECK_NZERO     1   /* error when retval != 0 (OpenDevice: 0=success) */
#define ERR_CHECK_VOID      2   /* void function -- never shown in ERRORS mode */
#define ERR_CHECK_ANY       3   /* no clear error convention -- always show */
#define ERR_CHECK_NONE      4   /* never an error (GetMsg NULL is normal) */
#define ERR_CHECK_RC        5   /* return code: error when rc != 0 */
#define ERR_CHECK_NEGATIVE  6   /* error when (LONG)retval < 0 (GetVar: -1=fail, >=0=count) */
#define ERR_CHECK_NEG1      7   /* error when retval == 0xFFFFFFFF (-1 unsigned) */

/* Return value semantics -- how to display and classify the result */
#define RET_PTR         0   /* pointer: NULL=fail, non-zero=hex addr */
#define RET_BOOL_DOS    1   /* DOS boolean: DOSTRUE(-1)=success, 0=fail */
#define RET_NZERO_ERR   2   /* 0=success, non-zero=error code */
#define RET_VOID        3   /* void function, show "(void)" */
#define RET_MSG_PTR     4   /* message pointer: NULL=empty, non-zero=addr */
#define RET_RC          5   /* return code: signed decimal, 0=success */
#define RET_LOCK        6   /* BPTR lock: NULL=fail, non-zero=hex addr */
#define RET_LEN         7   /* byte count: -1=fail, >=0=decimal count */
#define RET_OLD_LOCK    8   /* old lock from CurrentDir: NULL=ok, non-zero=hex */
#define RET_PTR_OPAQUE  9   /* opaque pointer: OK for non-NULL, NULL for fail */
#define RET_EXECUTE    10  /* Execute(): show raw value, neutral status */
#define RET_IO_LEN     11   /* I/O byte count: -1=error, 0=EOF(Read), >0=bytes */
/* Next available: 12 */

struct trace_func_entry {
    const char *lib_name;
    const char *func_name;
    UBYTE lib_id;
    WORD  lvo_offset;
    UBYTE error_check;   /* ERR_CHECK_* for ERRORS filter */
    UBYTE has_string;    /* 1 if stub captures string_data, 0 if not */
    UBYTE result_type;   /* RET_* constant for return value formatting */
};

static const struct trace_func_entry func_table[] = {
    /* exec.library functions (31) */
    { "exec", "FindPort",         LIB_EXEC, -390, ERR_CHECK_NULL,  1, RET_PTR_OPAQUE},
    { "exec", "FindResident",     LIB_EXEC,  -96, ERR_CHECK_NULL,  1, RET_PTR_OPAQUE},
    { "exec", "FindSemaphore",    LIB_EXEC, -594, ERR_CHECK_NULL,  1, RET_PTR_OPAQUE},
    { "exec", "FindTask",         LIB_EXEC, -294, ERR_CHECK_NULL,  1, RET_PTR_OPAQUE},
    { "exec", "OpenDevice",       LIB_EXEC, -444, ERR_CHECK_NZERO, 1, RET_NZERO_ERR},
    { "exec", "OpenLibrary",      LIB_EXEC, -552, ERR_CHECK_NULL,  1, RET_PTR      },
    { "exec", "OpenResource",     LIB_EXEC, -498, ERR_CHECK_NULL,  1, RET_PTR_OPAQUE},
    { "exec", "GetMsg",           LIB_EXEC, -372, ERR_CHECK_NONE,  1, RET_MSG_PTR  },
    { "exec", "PutMsg",           LIB_EXEC, -366, ERR_CHECK_VOID,  1, RET_VOID     },
    { "exec", "ObtainSemaphore",  LIB_EXEC, -564, ERR_CHECK_VOID,  1, RET_VOID     },
    { "exec", "ReleaseSemaphore", LIB_EXEC, -570, ERR_CHECK_VOID,  1, RET_VOID     },
    { "exec", "AllocMem",         LIB_EXEC, -198, ERR_CHECK_NULL,  0, RET_PTR      },
    { "exec", "DoIO",             LIB_EXEC, -456, ERR_CHECK_NZERO, 1, RET_NZERO_ERR},
    { "exec", "SendIO",           LIB_EXEC, -462, ERR_CHECK_VOID,  1, RET_VOID     },
    { "exec", "WaitIO",           LIB_EXEC, -474, ERR_CHECK_NZERO, 1, RET_NZERO_ERR},
    { "exec", "AbortIO",          LIB_EXEC, -480, ERR_CHECK_ANY,   1, RET_NZERO_ERR},
    { "exec", "CheckIO",          LIB_EXEC, -468, ERR_CHECK_ANY,   1, RET_PTR      },
    { "exec", "FreeMem",          LIB_EXEC, -210, ERR_CHECK_VOID,  0, RET_VOID     },
    { "exec", "AllocVec",         LIB_EXEC, -684, ERR_CHECK_NULL,  0, RET_PTR      },
    { "exec", "FreeVec",          LIB_EXEC, -690, ERR_CHECK_VOID,  0, RET_VOID     },
    /* Extended exec.library */
    { "exec", "Wait",           LIB_EXEC, -318, ERR_CHECK_ANY,   0, RET_PTR      },
    { "exec", "Signal",         LIB_EXEC, -324, ERR_CHECK_VOID,  0, RET_VOID     },
    { "exec", "AllocSignal",    LIB_EXEC, -330, ERR_CHECK_NEG1,  0, RET_IO_LEN   },
    { "exec", "FreeSignal",     LIB_EXEC, -336, ERR_CHECK_VOID,  0, RET_VOID     },
    { "exec", "CreateMsgPort",  LIB_EXEC, -666, ERR_CHECK_NULL,  0, RET_PTR      },
    { "exec", "DeleteMsgPort",  LIB_EXEC, -672, ERR_CHECK_VOID,  1, RET_VOID     },
    /* Lifecycle closers - exec.library */
    { "exec", "CloseLibrary",   LIB_EXEC, -414, ERR_CHECK_VOID,  1, RET_VOID     },
    { "exec", "CloseDevice",    LIB_EXEC, -450, ERR_CHECK_VOID,  1, RET_VOID     },
    { "exec", "ReplyMsg",       LIB_EXEC, -378, ERR_CHECK_VOID,  0, RET_VOID     },
    /* exec.library additions */
    { "exec", "AddPort",    LIB_EXEC, -354, ERR_CHECK_VOID,  1, RET_VOID     },
    { "exec", "WaitPort",   LIB_EXEC, -384, ERR_CHECK_ANY,   1, RET_MSG_PTR  },
    /* dos.library functions (26) */
    { "dos", "Open",              LIB_DOS,   -30, ERR_CHECK_NULL,  1, RET_PTR      },
    { "dos", "Close",             LIB_DOS,   -36, ERR_CHECK_NULL,  0, RET_BOOL_DOS },
    { "dos", "Lock",              LIB_DOS,   -84, ERR_CHECK_NULL,  1, RET_LOCK     },
    { "dos", "DeleteFile",        LIB_DOS,   -72, ERR_CHECK_NULL,  1, RET_BOOL_DOS },
    { "dos", "Execute",           LIB_DOS,  -222, ERR_CHECK_NONE,  1, RET_EXECUTE },
    { "dos", "GetVar",            LIB_DOS,  -906, ERR_CHECK_NEGATIVE, 1, RET_LEN   },
    { "dos", "FindVar",           LIB_DOS,  -918, ERR_CHECK_NULL,  1, RET_PTR_OPAQUE},
    { "dos", "LoadSeg",           LIB_DOS,  -150, ERR_CHECK_NULL,  1, RET_PTR      },
    { "dos", "NewLoadSeg",        LIB_DOS,  -768, ERR_CHECK_NULL,  1, RET_PTR      },
    { "dos", "CreateDir",         LIB_DOS,  -120, ERR_CHECK_NULL,  1, RET_LOCK     },
    { "dos", "MakeLink",          LIB_DOS,  -444, ERR_CHECK_NULL,  1, RET_BOOL_DOS },
    { "dos", "Rename",            LIB_DOS,   -78, ERR_CHECK_NULL,  1, RET_BOOL_DOS },
    { "dos", "RunCommand",        LIB_DOS,  -504, ERR_CHECK_RC,    0, RET_RC       },
    { "dos", "SetVar",            LIB_DOS,  -900, ERR_CHECK_NULL,  1, RET_BOOL_DOS },
    { "dos", "DeleteVar",         LIB_DOS,  -912, ERR_CHECK_NULL,  1, RET_BOOL_DOS },
    { "dos", "SystemTagList",     LIB_DOS,  -606, ERR_CHECK_RC,    1, RET_RC       },
    { "dos", "AddDosEntry",       LIB_DOS,  -678, ERR_CHECK_NULL,  0, RET_BOOL_DOS },
    { "dos", "CurrentDir",        LIB_DOS,  -126, ERR_CHECK_VOID,  1, RET_OLD_LOCK },
    { "dos", "Read",              LIB_DOS,   -42, ERR_CHECK_NEG1,  0, RET_IO_LEN   },
    { "dos", "Write",             LIB_DOS,   -48, ERR_CHECK_NEG1,  0, RET_IO_LEN   },
    /* dos.library additions */
    { "dos", "UnLock",          LIB_DOS,   -90, ERR_CHECK_VOID,  0, RET_VOID     },
    { "dos", "Examine",         LIB_DOS,  -102, ERR_CHECK_NULL,  0, RET_BOOL_DOS },
    { "dos", "ExNext",          LIB_DOS,  -108, ERR_CHECK_NULL,  0, RET_BOOL_DOS },
    { "dos", "Seek",            LIB_DOS,   -66, ERR_CHECK_NEG1,  0, RET_IO_LEN   },
    /* dos.library additions */
    { "dos", "SetProtection", LIB_DOS, -186, ERR_CHECK_NULL, 1, RET_BOOL_DOS },
    { "dos", "UnLoadSeg",     LIB_DOS, -156, ERR_CHECK_VOID, 0, RET_VOID     },
    /* intuition.library functions (14) */
    { "intuition", "OpenWindow",     LIB_INTUITION, -204, ERR_CHECK_NULL, 1, RET_PTR      },
    { "intuition", "CloseWindow",    LIB_INTUITION,  -72, ERR_CHECK_VOID, 1, RET_VOID     },
    { "intuition", "OpenScreen",     LIB_INTUITION, -198, ERR_CHECK_NULL, 1, RET_PTR      },
    { "intuition", "CloseScreen",    LIB_INTUITION,  -66, ERR_CHECK_VOID, 1, RET_VOID     },
    { "intuition", "ActivateWindow", LIB_INTUITION, -450, ERR_CHECK_VOID, 1, RET_VOID     },
    { "intuition", "WindowToFront",  LIB_INTUITION, -312, ERR_CHECK_VOID, 1, RET_VOID     },
    { "intuition", "WindowToBack",   LIB_INTUITION, -306, ERR_CHECK_VOID, 1, RET_VOID     },
    { "intuition", "ModifyIDCMP",    LIB_INTUITION, -150, ERR_CHECK_VOID, 0, RET_VOID     },
    { "intuition", "OpenWorkBench",  LIB_INTUITION, -210, ERR_CHECK_ANY,  0, RET_PTR      },
    { "intuition", "CloseWorkBench", LIB_INTUITION,  -78, ERR_CHECK_ANY,  0, RET_BOOL_DOS },
    /* intuition.library addition */
    { "intuition", "LockPubScreen", LIB_INTUITION, -510, ERR_CHECK_NULL, 1, RET_PTR },
    /* intuition.library additions */
    { "intuition", "OpenWindowTagList",  LIB_INTUITION, -606, ERR_CHECK_NULL, 1, RET_PTR  },
    { "intuition", "OpenScreenTagList",  LIB_INTUITION, -612, ERR_CHECK_NULL, 1, RET_PTR  },
    { "intuition", "UnlockPubScreen",    LIB_INTUITION, -516, ERR_CHECK_VOID, 1, RET_VOID },
    /* bsdsocket.library functions */
    { "bsdsocket", "socket",       LIB_BSDSOCKET,  -30, ERR_CHECK_NEG1, 0, RET_IO_LEN },
    { "bsdsocket", "bind",         LIB_BSDSOCKET,  -36, ERR_CHECK_NEG1, 0, RET_IO_LEN },
    { "bsdsocket", "listen",       LIB_BSDSOCKET,  -42, ERR_CHECK_NEG1, 0, RET_IO_LEN },
    { "bsdsocket", "accept",       LIB_BSDSOCKET,  -48, ERR_CHECK_NEG1, 0, RET_IO_LEN },
    { "bsdsocket", "connect",      LIB_BSDSOCKET,  -54, ERR_CHECK_NEG1, 0, RET_IO_LEN },
    { "bsdsocket", "sendto",       LIB_BSDSOCKET,  -60, ERR_CHECK_NEG1, 0, RET_IO_LEN },
    { "bsdsocket", "send",         LIB_BSDSOCKET,  -66, ERR_CHECK_NEG1, 0, RET_IO_LEN },
    { "bsdsocket", "recvfrom",     LIB_BSDSOCKET,  -72, ERR_CHECK_NEG1, 0, RET_IO_LEN },
    { "bsdsocket", "recv",         LIB_BSDSOCKET,  -78, ERR_CHECK_NEG1, 0, RET_IO_LEN },
    { "bsdsocket", "shutdown",     LIB_BSDSOCKET,  -84, ERR_CHECK_NEG1, 0, RET_IO_LEN },
    { "bsdsocket", "setsockopt",   LIB_BSDSOCKET,  -90, ERR_CHECK_NEG1, 0, RET_IO_LEN },
    { "bsdsocket", "getsockopt",   LIB_BSDSOCKET,  -96, ERR_CHECK_NEG1, 0, RET_IO_LEN },
    { "bsdsocket", "IoctlSocket",  LIB_BSDSOCKET, -114, ERR_CHECK_NEG1, 0, RET_IO_LEN },
    { "bsdsocket", "CloseSocket",  LIB_BSDSOCKET, -120, ERR_CHECK_NEG1, 0, RET_IO_LEN },
    { "bsdsocket", "WaitSelect",   LIB_BSDSOCKET, -126, ERR_CHECK_NEG1, 0, RET_IO_LEN },
    /* graphics.library functions (2) */
    { "graphics", "OpenFont",     LIB_GRAPHICS,  -72, ERR_CHECK_NULL,  1, RET_PTR },
    /* graphics.library addition */
    { "graphics", "CloseFont", LIB_GRAPHICS, -78, ERR_CHECK_VOID, 0, RET_VOID },
    /* icon.library functions */
    { "icon", "GetDiskObject",  LIB_ICON, -78, ERR_CHECK_NULL, 1, RET_PTR      },
    { "icon", "PutDiskObject",  LIB_ICON, -84, ERR_CHECK_NULL, 1, RET_BOOL_DOS },
    { "icon", "FreeDiskObject", LIB_ICON, -90, ERR_CHECK_VOID, 0, RET_VOID     },
    { "icon", "FindToolType",   LIB_ICON, -96, ERR_CHECK_NULL, 1, RET_PTR      },
    { "icon", "MatchToolValue", LIB_ICON, -102, ERR_CHECK_NONE, 0, RET_BOOL_DOS },
    /* MatchToolValue returns BOOL (TRUE=match, FALSE=no match).
     * FALSE is a normal expected result, not an error condition.
     * ERR_CHECK_NONE prevents FALSE returns from appearing in
     * ERRORS-only filtered output. */
    /* workbench.library functions
     * Note: RemoveAppIcon, RemoveAppWindow, RemoveAppMenuItem return
     * standard BOOL (TRUE=1), not DOSTRUE (-1). RET_BOOL_DOS handles
     * both correctly: it formats any non-zero return as "OK". */
    { "workbench", "AddAppIconA",     LIB_WORKBENCH, -60, ERR_CHECK_NULL, 1, RET_PTR      },
    { "workbench", "RemoveAppIcon",   LIB_WORKBENCH, -66, ERR_CHECK_NULL, 0, RET_BOOL_DOS },
    { "workbench", "AddAppWindowA",   LIB_WORKBENCH, -48, ERR_CHECK_NULL, 0, RET_PTR      },
    { "workbench", "RemoveAppWindow", LIB_WORKBENCH, -54, ERR_CHECK_NULL, 0, RET_BOOL_DOS },
    { "workbench", "AddAppMenuItemA", LIB_WORKBENCH, -72, ERR_CHECK_NULL, 1, RET_PTR      },
    { "workbench", "RemoveAppMenuItem", LIB_WORKBENCH, -78, ERR_CHECK_NULL, 0, RET_BOOL_DOS },
};

#define FUNC_TABLE_SIZE  (sizeof(func_table) / sizeof(func_table[0]))

/* ---- DOS error code lookup ---- */

/* Standard AmigaOS DOS error code names.
 * Returns NULL for unknown codes. */
static const char *dos_error_name(int code)
{
    switch (code) {
    case 103: return "insufficient free store";
    case 105: return "task table full";
    case 114: return "bad template";
    case 116: return "required argument missing";
    case 117: return "value after keyword missing";
    case 120: return "argument line invalid or too long";
    case 121: return "file is not an object module";
    case 122: return "invalid resident library";
    case 202: return "object in use";
    case 203: return "object already exists";
    case 204: return "directory not found";
    case 205: return "object not found";
    case 206: return "invalid window description";
    case 207: return "object too large";
    case 208: return "action not known";
    case 209: return "packet request type unknown";
    case 210: return "object name invalid";
    case 211: return "invalid lock";
    case 212: return "object not of required type";
    case 213: return "disk not validated";
    case 214: return "disk write-protected";
    case 215: return "rename across devices";
    case 216: return "directory not empty";
    case 218: return "device not mounted";
    case 219: return "seek error";
    case 220: return "comment too big";
    case 221: return "disk full";
    case 222: return "file is protected from deletion";
    case 223: return "file is write protected";
    case 224: return "file is read protected";
    case 225: return "not a DOS disk";
    case 226: return "no disk in drive";
    case 232: return "no more entries";
    case 233: return "buffer overflow";
    default:  return NULL;
    }
}

/* ---- Module globals ---- */

/* Cached anchor pointer -- NULL if atrace not found */
static struct atrace_anchor *g_anchor = NULL;

/* Cached ring entries pointer (avoids repeated offset math) */
static struct atrace_event *g_ring_entries = NULL;

/* Running total of dropped events (from ring->overflow) */
static ULONG g_events_dropped = 0;
static ULONG g_self_filtered = 0;  /* Includes self-filter + content-filter suppression */
static ULONG g_poll_count = 0;

/* Current output tier level (1=Basic, 2=Detail, 3=Verbose).
 * Set via TRACE TIER command from the client.
 * Used for content-based event filtering (e.g., OpenLibrary v0
 * suppression at Basic tier). Initialized to TIER_BASIC at
 * session start. */
static int g_current_tier = 1;

/* Command extraction buffer (static, single-threaded) */
static char trace_cmd_buf[MAX_CMD_LEN + 1];

/* Event formatting buffer (static, single-threaded) */
static char trace_line_buf[512];

/* In-progress (valid=2) event patience tracking.
 * When the consumer encounters an event with valid=2, it waits up to
 * INFLIGHT_PATIENCE consecutive polls for the post-call handler to
 * complete (set valid=1).  After that, it consumes the event as-is.
 * This prevents ring buffer stalls from blocking functions while
 * giving non-blocking functions time to complete and fill in retval,
 * ioerr, and FLAG_HAS_IOERR. */
#define INFLIGHT_PATIENCE 3  /* 3 encounters = ~200ms at 100ms poll rate */
static ULONG g_inflight_stall_pos = 0xFFFFFFFF;
static int g_inflight_stall_count = 0;

/* ---- EClock session epoch ----
 * Captured at TRACE START / TRACE RUN to convert per-event EClock
 * ticks into wall-clock timestamps.  Only valid when g_anchor->version >= 3
 * and g_anchor->eclock_freq != 0. */
static int g_eclock_valid = 0;       /* 1 = epoch captured, EClock available */
static int g_eclock_baseline_set = 0; /* 1 = baseline captured from first event */
static ULONG g_eclock_freq = 0;     /* EClock frequency in Hz */
static ULONG g_start_eclock_lo = 0; /* EClock lo at session start */
static WORD  g_start_eclock_hi = 0; /* EClock hi at session start */
static LONG  g_start_secs = 0;      /* wall-clock seconds since midnight */
static LONG  g_start_us = 0;        /* wall-clock microseconds */
/* Precomputed constants for 48-bit EClock conversion */
static ULONG g_secs_per_hi = 0;     /* (2^32 - 1) / freq */
static ULONG g_rem_per_hi = 0;      /* (2^32 - 1) % freq */

/* ---- bsdsocket.library per-opener tracking ---- */

/* bsdsocket.library uses per-opener bases -- each OpenLibrary returns
 * a unique base with its own jump table.  Track patched bases to avoid
 * redundant SetFunction calls. */
#define MAX_BSD_BASES 16
static APTR g_patched_bsd_bases[MAX_BSD_BASES];
static int g_patched_bsd_count = 0;

/* Drain stale events from ring buffer and clear valid flags.
 * Must be called under Forbid(). */
static void drain_stale_events(void)
{
    struct atrace_ringbuf *ring;
    ULONG stale;
    ULONG ci;

    /* Reset in-progress stall tracking -- a new trace session starts
     * with a clean slate.  Without this, stale stall state from a
     * prior session could cause the first valid=2 event in the new
     * session to be consumed prematurely (if it happens to land at
     * the same ring position as the old stall). */
    g_inflight_stall_pos = 0xFFFFFFFF;
    g_inflight_stall_count = 0;

    if (!g_anchor || !g_anchor->ring || !g_ring_entries)
        return;

    ring = g_anchor->ring;

    stale = (ring->write_pos - ring->read_pos
             + ring->capacity) % ring->capacity;
    if (stale > 0) {
        g_anchor->events_consumed += stale;
        ring->read_pos = ring->write_pos;
    }
    if (ring->overflow > 0) {
        g_events_dropped += ring->overflow;
        ring->overflow = 0;
    }

    /* Clear valid flags, retval, ioerr, and flags to prevent
     * trace_poll_events() from consuming stale entries.  Stubs set
     * valid=2 ("in-progress") before calling the original function
     * (needed for blocking calls -- the consumer must not stall on
     * those slots).  The post-call handler writes retval, ioerr,
     * FLAG_HAS_IOERR, and sets valid=1 ("complete").  If the daemon
     * polls while the function is executing, it sees valid=2 and
     * stale field values from prior ring buffer usage.  Clearing
     * here ensures those stale values are zero, not misleading. */
    for (ci = 0; ci < ring->capacity; ci++) {
        g_ring_entries[ci].valid = 0;
        g_ring_entries[ci].retval = 0;
        g_ring_entries[ci].ioerr = 0;
        g_ring_entries[ci].flags = 0;
    }
}

/* Look up a patch index by function name (case-insensitive).
 * Returns the func_table[] index, which IS the global patch index
 * because func_table[] ordering matches the installation order
 * in atrace/funcs.c (exec functions first, then dos functions).
 * Returns -1 if not found. */
static int find_patch_index_by_name(const char *name)
{
    int i;
    for (i = 0; i < (int)FUNC_TABLE_SIZE; i++) {
        if (stricmp(name, func_table[i].func_name) == 0)
            return i;
    }
    return -1;
}

/* ---- Noise function table ---- */

/* Noise function names -- non-Basic tier functions that are disabled
 * by default at install time.  Union of Detail + Verbose + Manual
 * tiers (42 functions: 13 Detail + 3 Verbose + 26 Manual).
 *
 * MUST match the union of tier_detail_funcs + tier_verbose_funcs +
 * tier_manual_funcs in atrace/main.c.
 * Source of truth: trace_tiers.py.
 *
 * Used by trace_discover() to validate names at startup and by
 * trace_cmd_status() to report the noise_disabled count. */
static const char *noise_func_names[] = {
    /* Detail tier: exec.library */
    "AllocSignal", "FreeSignal", "CreateMsgPort", "DeleteMsgPort",
    "CloseLibrary",
    /* Detail tier: dos.library */
    "UnLock", "Examine", "Seek",
    /* Detail tier: intuition.library */
    "ModifyIDCMP",
    /* Detail tier: bsdsocket.library */
    "sendto", "recvfrom",
    /* Detail tier additions */
    "UnLoadSeg", "UnlockPubScreen",
    /* Verbose tier */
    "ExNext",
    /* Verbose tier: graphics.library */
    "OpenFont",
    /* Verbose tier addition */
    "CloseFont",
    /* Manual tier: exec.library */
    "FindPort", "FindSemaphore", "FindTask",
    "PutMsg", "GetMsg", "ObtainSemaphore", "ReleaseSemaphore",
    "AllocMem", "FreeMem", "AllocVec", "FreeVec",
    "Wait", "Signal",
    "DoIO", "SendIO", "WaitIO", "AbortIO", "CheckIO",
    "ReplyMsg",
    /* Manual tier: dos.library */
    "Read", "Write",
    /* Manual tier: bsdsocket.library */
    "send", "recv", "WaitSelect",
    /* Manual tier additions */
    "AddPort", "WaitPort",
    NULL
};

/* ---- Task name cache ---- */

#define TASK_CACHE_SIZE   64
#define TASK_CACHE_REFRESH_INTERVAL  20  /* polls = ~2 seconds */

struct task_cache_entry {
    APTR task_ptr;
    char name[64];
};

static struct task_cache_entry task_cache[TASK_CACHE_SIZE];
static int task_cache_count = 0;
static int task_cache_polls = 0;

/* ---- Task name history (for exited processes) ----
 *
 * The task cache is rebuilt from scratch on each refresh, so entries
 * for exited processes are lost.  The history cache preserves the last
 * resolved name for each task pointer so that events from short-lived
 * processes (e.g., CLI commands launched via SystemTags) can still be
 * matched by PROC= filters and displayed correctly after the process
 * exits.  This is a simple ring buffer; older entries are overwritten
 * when full. */

#define TASK_HISTORY_SIZE  32

static struct task_cache_entry task_history[TASK_HISTORY_SIZE];
static int task_history_count = 0;
static int task_history_next = 0;   /* next write slot (ring) */

/* Record a resolved task name in the history cache.
 * Called when resolve_task_name() successfully resolves a name
 * (not a generic "<task 0x...>" fallback). */
static void task_history_record(APTR task_ptr, const char *name)
{
    int i;

    /* Don't record generic fallback names */
    if (name[0] == '<')
        return;

    /* Check if already recorded with same name */
    for (i = 0; i < task_history_count; i++) {
        if (task_history[i].task_ptr == task_ptr) {
            /* Update if name changed (e.g., CLI ran a new command) */
            if (strcmp(task_history[i].name, name) != 0) {
                strncpy(task_history[i].name, name, 63);
                task_history[i].name[63] = '\0';
            }
            return;
        }
    }

    /* New entry: write to ring slot */
    task_history[task_history_next].task_ptr = task_ptr;
    strncpy(task_history[task_history_next].name, name, 63);
    task_history[task_history_next].name[63] = '\0';
    task_history_next = (task_history_next + 1) % TASK_HISTORY_SIZE;
    if (task_history_count < TASK_HISTORY_SIZE)
        task_history_count++;
}

/* Look up a task pointer in the history cache.
 * Returns the last-known name, or NULL if not found. */
static const char *task_history_lookup(APTR task_ptr)
{
    int i;
    for (i = 0; i < task_history_count; i++) {
        if (task_history[i].task_ptr == task_ptr)
            return task_history[i].name;
    }
    return NULL;
}

/* Extract the CLI command name for a Process, if available.
 * Returns 1 if a command name was extracted, 0 if falling back to
 * tc_Node.ln_Name is needed.
 *
 * For CLI processes (pr_TaskNum > 0), reads the command name BSTR
 * from the CLI structure. The basename is extracted (path prefix
 * stripped).
 *
 * Must be called under Forbid() -- the CLI structure is owned by
 * the process and could be freed if the process exits. */
static int resolve_cli_name(struct Process *pr, char *buf, int bufsz)
{
    struct CommandLineInterface *cli;
    UBYTE *bstr;
    int len;
    char cmd[64];
    const char *base;
    const char *slash;
    const char *colon;

    if (pr->pr_TaskNum <= 0)
        return 0;

    cli = (struct CommandLineInterface *)BADDR(pr->pr_CLI);
    if (!cli)
        return 0;

    if (!cli->cli_CommandName)
        return 0;

    bstr = (UBYTE *)BADDR(cli->cli_CommandName);
    if (!bstr)
        return 0;

    len = bstr[0];
    if (len == 0)
        return 0;

    /* Cap to buffer size */
    if (len > 63)
        len = 63;

    memcpy(cmd, &bstr[1], len);
    cmd[len] = '\0';

    /* Extract basename: strip everything before the last '/' or ':' */
    base = cmd;
    slash = strrchr(cmd, '/');
    colon = strrchr(cmd, ':');
    if (slash && colon)
        base = (slash > colon) ? slash + 1 : colon + 1;
    else if (slash)
        base = slash + 1;
    else if (colon)
        base = colon + 1;

    /* If basename is empty after stripping (e.g., "SYS:"), fall back */
    if (!base[0])
        return 0;

    snprintf(buf, bufsz, "[%ld] %s", (long)pr->pr_TaskNum, base);
    return 1;
}

/* ---- Forward declarations ---- */

static int trace_discover(void);
static int trace_auto_load(void);
static void refresh_task_cache(void);
static const char *resolve_task_name(APTR task_ptr);
static const struct trace_func_entry *lookup_func(UBYTE lib_id, WORD lvo);
static const char *stristr(const char *haystack, const char *needle);
static int parse_filters(const char *args, struct trace_state *ts);
static void parse_extended_filter(const char *args, struct trace_state *ts);
static int trace_filter_match(struct trace_state *ts,
                               struct atrace_event *ev,
                               const char *task_name);
static const char *format_access_mode(LONG mode);
static const char *format_lock_type(LONG type);
static void format_memf_flags(ULONG flags, char *buf, int bufsz);
static void format_idcmp_flags(ULONG flags, char *buf, int bufsz);
static const char *format_af(LONG domain);
static const char *format_socktype(LONG type);
static const char *format_shutdown_how(LONG how);
static const char *format_seek_offset(LONG offset);
static int format_signal_set(ULONG sigs, char *buf, int bufsz);
static void format_args(struct atrace_event *ev,
                        const struct trace_func_entry *fe,
                        char *buf, int bufsz);
static char format_retval(struct atrace_event *ev,
                           const struct trace_func_entry *fe,
                           char *buf, int bufsz);
static void trace_format_event(struct atrace_event *ev,
                                const char *timestr,
                                const char *resolved_name,
                                char *buf, int bufsz);
static int send_trace_data_chunk(LONG fd, const char *line);
static int trace_cmd_status(struct client *c);
static int trace_cmd_start(struct daemon_state *d, int idx,
                            const char *args);
static int trace_cmd_run(struct daemon_state *d, int idx,
                          const char *args);
static int trace_cmd_enable(struct client *c, const char *args);
static int trace_cmd_disable(struct client *c, const char *args);
static void trace_run_cleanup(struct client *c);
static int build_filter_desc(struct trace_state *ts, char *buf, int bufsz);
static int emit_trace_header(LONG fd, struct trace_state *ts,
                              const char *run_command);
static void eclock_capture_epoch(void);
static int eclock_format_time(struct atrace_event *ev,
                               char *buf, int bufsz);
static void patch_bsdsocket_base(struct daemon_state *d, APTR base);
static void sendbuf_init(struct trace_sendbuf *sb);
static int sendbuf_append_data_chunk(struct trace_sendbuf *sb,
                                      const char *line);
static int sendbuf_drain(struct trace_sendbuf *sb, LONG fd);

/* ---- Initialization / cleanup ---- */

int trace_init(void)
{
    struct SignalSemaphore *sem;

    Forbid();
    sem = FindSemaphore((STRPTR)ATRACE_SEM_NAME);
    Permit();

    if (sem) {
        g_anchor = (struct atrace_anchor *)sem;
        if (g_anchor->magic != ATRACE_MAGIC) {
            g_anchor = NULL;
        } else if (g_anchor->ring) {
            g_ring_entries = (struct atrace_event *)
                ((UBYTE *)g_anchor->ring + sizeof(struct atrace_ringbuf));
        } else {
            g_ring_entries = NULL;
        }
    }

    /* Register daemon task for stub-level self-event filtering */
    if (g_anchor && g_anchor->version >= 6) {
        g_anchor->daemon_task = FindTask(NULL);
    }

    return 0;  /* always succeeds */
}

void trace_cleanup(void)
{
    /* Clear daemon task filter before releasing anchor reference */
    if (g_anchor && g_anchor->version >= 6) {
        g_anchor->daemon_task = NULL;
    }

    /* Nothing to free -- atrace owns all shared memory */
    g_anchor = NULL;
    g_ring_entries = NULL;
    g_events_dropped = 0;
    g_self_filtered = 0;
    g_current_tier = 1;
    task_cache_count = 0;
    task_cache_polls = 0;
    g_inflight_stall_pos = 0xFFFFFFFF;
    g_inflight_stall_count = 0;
    g_eclock_valid = 0;
    g_eclock_freq = 0;
    g_patched_bsd_count = 0;
    memset(g_patched_bsd_bases, 0, sizeof(g_patched_bsd_bases));
}

/* ---- Lazy discovery helper ---- */

/* Re-check for atrace semaphore if not yet found.
 * Called on each TRACE START so atrace can be loaded after amigactld. */
static int trace_discover(void)
{
    struct SignalSemaphore *sem;

    if (g_anchor)
        return 1;  /* already found */

    Forbid();
    sem = FindSemaphore((STRPTR)ATRACE_SEM_NAME);
    Permit();

    if (!sem)
        return 0;

    g_anchor = (struct atrace_anchor *)sem;
    if (g_anchor->magic != ATRACE_MAGIC) {
        g_anchor = NULL;
        return 0;
    }

    if (g_anchor->ring) {
        g_ring_entries = (struct atrace_event *)
            ((UBYTE *)g_anchor->ring + sizeof(struct atrace_ringbuf));
    } else {
        g_ring_entries = NULL;
    }

    /* Register daemon task for stub-level self-event filtering */
    if (g_anchor->version >= 6) {
        g_anchor->daemon_task = FindTask(NULL);
    }

    /* Validate noise function names against the loaded patch table.
     * Log a warning for any that don't match -- catches typos and
     * mismatches between the loader and daemon noise lists. */
    {
        const char **np;
        for (np = noise_func_names; *np; np++) {
            if (find_patch_index_by_name(*np) < 0) {
                printf("WARNING: noise function '%s' not found "
                       "in patch table\n", *np);
            }
        }
    }

    return 1;
}

/* ---- Auto-load helper ---- */

/* Attempt to load atrace_loader if atrace is not already resident.
 * Executes "C:atrace_loader" synchronously via SystemTags, then
 * retries discovery.  Returns 1 on success, 0 on failure.
 *
 * On success, the caller should note auto-loading happened so the
 * user sees feedback (e.g. via the OK info field or a payload line).
 * The daemon console also gets a log message. */
static int trace_auto_load(void)
{
    BPTR fh_nil;
    LONG rc;

    /* Already loaded? */
    if (trace_discover())
        return 1;

    printf("Auto-loading C:atrace_loader\n");

    /* Run the loader with output discarded */
    fh_nil = Open((STRPTR)"NIL:", MODE_OLDFILE);
    if (!fh_nil) {
        printf("Auto-load: cannot open NIL:\n");
        return 0;
    }

    rc = SystemTags((STRPTR)"C:atrace_loader",
                    SYS_Output, fh_nil,
                    SYS_Input, 0,
                    TAG_DONE);

    Close(fh_nil);

    if (rc != 0) {
        printf("Auto-load: C:atrace_loader returned %ld\n", (long)rc);
        return 0;
    }

    /* Retry discovery now that the semaphore should exist */
    return trace_discover();
}

/* ---- Function name lookup ---- */

/* Look up function entry by lib_id and lvo_offset.
 * Returns pointer to the static func_table entry, or NULL if not found. */
static const struct trace_func_entry *lookup_func(UBYTE lib_id, WORD lvo)
{
    int i;
    for (i = 0; i < (int)FUNC_TABLE_SIZE; i++) {
        if (func_table[i].lib_id == lib_id && func_table[i].lvo_offset == lvo)
            return &func_table[i];
    }
    return NULL;
}

/* ---- Task name cache ---- */

/* Refresh the task cache by walking the system task lists under Forbid.
 * This is O(N) in the number of tasks but runs only every ~5 seconds. */
static void refresh_task_cache(void)
{
    struct ExecBase *eb = (struct ExecBase *)(*((APTR *)4));
    struct Node *node;
    int idx = 0;

    Forbid();

    /* Walk the ready list */
    for (node = eb->TaskReady.lh_Head;
         node->ln_Succ && idx < TASK_CACHE_SIZE;
         node = node->ln_Succ) {
        task_cache[idx].task_ptr = (APTR)node;
        if (node->ln_Name) {
            if (node->ln_Type == NT_PROCESS) {
                struct Process *pr = (struct Process *)node;
                if (!resolve_cli_name(pr, task_cache[idx].name, 64)) {
                    /* No CLI command name -- use tc_Node.ln_Name with
                     * optional CLI number prefix */
                    LONG cli_num = pr->pr_TaskNum;
                    if (cli_num > 0) {
                        snprintf(task_cache[idx].name, 64, "[%ld] %s",
                                 (long)cli_num, node->ln_Name);
                    } else {
                        strncpy(task_cache[idx].name, node->ln_Name, 63);
                        task_cache[idx].name[63] = '\0';
                    }
                }
            } else {
                strncpy(task_cache[idx].name, node->ln_Name, 63);
                task_cache[idx].name[63] = '\0';
            }
        } else {
            task_cache[idx].name[0] = '\0';
        }
        idx++;
    }

    /* Walk the wait list */
    for (node = eb->TaskWait.lh_Head;
         node->ln_Succ && idx < TASK_CACHE_SIZE;
         node = node->ln_Succ) {
        task_cache[idx].task_ptr = (APTR)node;
        if (node->ln_Name) {
            if (node->ln_Type == NT_PROCESS) {
                struct Process *pr = (struct Process *)node;
                if (!resolve_cli_name(pr, task_cache[idx].name, 64)) {
                    /* No CLI command name -- use tc_Node.ln_Name with
                     * optional CLI number prefix */
                    LONG cli_num = pr->pr_TaskNum;
                    if (cli_num > 0) {
                        snprintf(task_cache[idx].name, 64, "[%ld] %s",
                                 (long)cli_num, node->ln_Name);
                    } else {
                        strncpy(task_cache[idx].name, node->ln_Name, 63);
                        task_cache[idx].name[63] = '\0';
                    }
                }
            } else {
                strncpy(task_cache[idx].name, node->ln_Name, 63);
                task_cache[idx].name[63] = '\0';
            }
        } else {
            task_cache[idx].name[0] = '\0';
        }
        idx++;
    }

    /* Current task (ourselves) */
    if (idx < TASK_CACHE_SIZE) {
        struct Task *this_task = FindTask(NULL);
        task_cache[idx].task_ptr = (APTR)this_task;
        if (this_task->tc_Node.ln_Name) {
            if (this_task->tc_Node.ln_Type == NT_PROCESS) {
                struct Process *pr = (struct Process *)this_task;
                if (!resolve_cli_name(pr, task_cache[idx].name, 64)) {
                    /* No CLI command name -- use tc_Node.ln_Name with
                     * optional CLI number prefix */
                    LONG cli_num = pr->pr_TaskNum;
                    if (cli_num > 0) {
                        snprintf(task_cache[idx].name, 64, "[%ld] %s",
                                 (long)cli_num, this_task->tc_Node.ln_Name);
                    } else {
                        strncpy(task_cache[idx].name,
                                this_task->tc_Node.ln_Name, 63);
                        task_cache[idx].name[63] = '\0';
                    }
                }
            } else {
                strncpy(task_cache[idx].name,
                        this_task->tc_Node.ln_Name, 63);
                task_cache[idx].name[63] = '\0';
            }
        } else {
            task_cache[idx].name[0] = '\0';
        }
        idx++;
    }

    Permit();

    task_cache_count = idx;
    task_cache_polls = 0;
}

/* Resolve a Task pointer to a name string.
 * Uses a cached task-name table refreshed every ~2 seconds.
 * Falls back to direct dereference under Forbid for cache misses. */
static const char *resolve_task_name(APTR task_ptr)
{
    static char fallback[64];
    struct Task *task;
    char *name;
    int i;

    if (!task_ptr)
        return "<null>";

    /* Refresh cache periodically */
    if (task_cache_polls++ >= TASK_CACHE_REFRESH_INTERVAL ||
        task_cache_count == 0)
        refresh_task_cache();

    /* Search cache */
    for (i = 0; i < task_cache_count; i++) {
        if (task_cache[i].task_ptr == task_ptr) {
            task_history_record(task_ptr, task_cache[i].name);
            return task_cache[i].name;
        }
    }

    /* Cache miss -- attempt on-demand refresh.
     * The task may be alive but the periodic refresh hasn't caught it.
     * Refresh now and re-search. This adds a Forbid/Permit per miss
     * but misses are rare (only for tasks first seen since last refresh).
     *
     * Use minimum gap: require at least 3 periodic polls between
     * on-demand refreshes. This prevents O(N/2) refresh storms when
     * burst events arrive from a new task (each miss would trigger a
     * refresh, but they all resolve after the first one). */
    if (task_cache_polls >= 3) {
        refresh_task_cache();

        /* Re-search after refresh */
        for (i = 0; i < task_cache_count; i++) {
            if (task_cache[i].task_ptr == task_ptr) {
                task_history_record(task_ptr, task_cache[i].name);
                return task_cache[i].name;
            }
        }
    }

    /* Cache miss after refresh -- attempt direct dereference under Forbid.
     * This handles short-lived tasks that started and exited between
     * cache refreshes. The Forbid prevents the task from being
     * removed while we read its name.
     * If the task has already exited, the pointer may be stale --
     * this is acceptable on
     * single-CPU 68k where Forbid blocks FreeMem completions. */
    task = (struct Task *)task_ptr;
    Forbid();
    name = (char *)task->tc_Node.ln_Name;
    if (name) {
        /* Check if this is a Process with a CLI number */
        if (task->tc_Node.ln_Type == NT_PROCESS) {
            struct Process *pr = (struct Process *)task;
            if (!resolve_cli_name(pr, fallback, sizeof(fallback))) {
                LONG cli_num = pr->pr_TaskNum;
                if (cli_num > 0) {
                    snprintf(fallback, sizeof(fallback), "[%ld] %s",
                             (long)cli_num, name);
                } else {
                    strncpy(fallback, name, sizeof(fallback) - 1);
                    fallback[sizeof(fallback) - 1] = '\0';
                }
            }
        } else {
            strncpy(fallback, name, sizeof(fallback) - 1);
            fallback[sizeof(fallback) - 1] = '\0';
        }
    } else {
        /* ln_Name is NULL -- task memory may have been freed.
         * Check the history cache for the last-known name before
         * falling back to the generic "<task 0x...>" string. */
        const char *hist = task_history_lookup(task_ptr);
        if (hist) {
            Permit();
            return hist;
        }
        sprintf(fallback, "<task 0x%08lx>", (unsigned long)task_ptr);
    }
    Permit();

    task_history_record(task_ptr, fallback);
    return fallback;
}

/* ---- Case-insensitive substring search ---- */

/* Returns pointer to the first occurrence of needle in haystack,
 * or NULL if not found. Empty needle matches everything. */
static const char *stristr(const char *haystack, const char *needle)
{
    const char *h;
    const char *n;
    const char *start;

    if (!needle || !needle[0])
        return haystack;
    if (!haystack)
        return NULL;

    for (start = haystack; *start; start++) {
        h = start;
        n = needle;
        while (*h && *n) {
            char hc = *h;
            char nc = *n;
            /* ASCII-only tolower */
            if (hc >= 'A' && hc <= 'Z') hc += 32;
            if (nc >= 'A' && nc <= 'Z') nc += 32;
            if (hc != nc)
                break;
            h++;
            n++;
        }
        if (!*n)
            return start;  /* full needle matched */
    }
    return NULL;
}

/* ---- Filter parsing and matching ---- */

/* Parse filter arguments from the TRACE START command line.
 * Initializes all filter fields to "match everything" defaults,
 * then overrides from any recognized filter keywords.
 * Returns 0 always (unknown keywords are silently skipped). */
static int parse_filters(const char *args, struct trace_state *ts)
{
    /* Initialize to "match everything" */
    ts->filter_lib_id = -1;
    ts->filter_lvo = 0;
    ts->filter_errors_only = 0;
    ts->filter_procname[0] = '\0';

    /* Clear extended filter state (safe because trace_filter_match checks
     * use_extended_filter before accessing these fields) */
    ts->use_extended_filter = 0;

    while (*args) {
        while (*args == ' ' || *args == '\t')
            args++;
        if (*args == '\0')
            break;

        if (strnicmp(args, "LIB=", 4) == 0) {
            /* Extract library name, look up lib_id */
            char lname[32];
            int llen = 0;
            args += 4;
            while (*args && *args != ' ' && *args != '\t' &&
                   llen < (int)sizeof(lname) - 1)
                lname[llen++] = *args++;
            lname[llen] = '\0';

            /* Strip common library suffixes so "dos.library" matches "dos" */
            {
                static const char *suffixes[] = {
                    ".library", ".device", ".resource", NULL
                };
                const char **sfx;
                for (sfx = suffixes; *sfx; sfx++) {
                    int slen = strlen(*sfx);
                    if (llen > slen && stricmp(&lname[llen - slen], *sfx) == 0) {
                        lname[llen - slen] = '\0';
                        llen -= slen;
                        break;
                    }
                }
            }

            /* Match against known library short names */
            {
                int found = 0;
                int idx;
                for (idx = 0; idx < (int)FUNC_TABLE_SIZE; idx++) {
                    if (stricmp(lname, func_table[idx].lib_name) == 0) {
                        ts->filter_lib_id = func_table[idx].lib_id;
                        found = 1;
                        break;
                    }
                }
                if (!found) {
                    /* Unknown library name -- match nothing.
                     * Use sentinel 255 (no real lib_id is that high). */
                    ts->filter_lib_id = 255;
                }
            }
        } else if (strnicmp(args, "FUNC=", 5) == 0) {
            /* Extract function name, look up LVO and lib_id.
             * Setting both filter_lvo AND filter_lib_id prevents
             * cross-library LVO collisions (e.g. exec.OpenDevice and
             * dos.MakeLink both have LVO -444).
             *
             * Supports library-scoped syntax: FUNC=dos.Open
             * (dot separator between library name and function name).
             * Plain FUNC=Open still works (global search). */
            char fname[32];
            int fi = 0;
            int found = 0;
            char *dot;
            args += 5;
            while (*args && *args != ' ' && *args != '\t' &&
                   fi < (int)sizeof(fname) - 1)
                fname[fi++] = *args++;
            fname[fi] = '\0';

            dot = strchr(fname, '.');
            if (dot) {
                /* Library-scoped: split at dot, match both fields */
                const char *fn = dot + 1;
                *dot = '\0';
                for (fi = 0; fi < (int)FUNC_TABLE_SIZE; fi++) {
                    if (stricmp(fname, func_table[fi].lib_name) == 0 &&
                        stricmp(fn, func_table[fi].func_name) == 0) {
                        ts->filter_lvo = func_table[fi].lvo_offset;
                        ts->filter_lib_id = func_table[fi].lib_id;
                        found = 1;
                        break;
                    }
                }
            } else {
                /* Global search by function name only */
                for (fi = 0; fi < (int)FUNC_TABLE_SIZE; fi++) {
                    if (stricmp(fname, func_table[fi].func_name) == 0) {
                        ts->filter_lvo = func_table[fi].lvo_offset;
                        ts->filter_lib_id = func_table[fi].lib_id;
                        found = 1;
                        break;
                    }
                }
            }
            if (!found) {
                /* Unknown function name -- match nothing.
                 * Use sentinel LVO value 1 (no real LVO is positive). */
                ts->filter_lvo = 1;
            }
        } else if (strnicmp(args, "PROC=", 5) == 0) {
            /* Extract process name substring */
            int pi = 0;
            args += 5;
            while (*args && *args != ' ' && *args != '\t' &&
                   pi < (int)sizeof(ts->filter_procname) - 1)
                ts->filter_procname[pi++] = *args++;
            ts->filter_procname[pi] = '\0';
        } else if (strnicmp(args, "ERRORS", 6) == 0) {
            ts->filter_errors_only = 1;
            args += 6;
        } else {
            /* Unknown filter keyword -- skip to next space */
            while (*args && *args != ' ' && *args != '\t')
                args++;
        }
    }
    return 0;
}

/* ---- Extended filter parsing ---- */

/* Advance past the current token to the next whitespace.
 * Returns pointer to the first space/tab, or end of string. */
static const char *skip_to_space(const char *p)
{
    while (*p && *p != ' ' && *p != '\t')
        p++;
    return p;
}

/* Parse a comma-separated list of library names into output arrays.
 *
 * Strips ".library"/".device"/".resource" suffixes, looks up lib_id
 * in func_table. Populates out_ids[].
 *
 * Unknown names are silently skipped (robust against typos).
 * Parsing stops at space, tab, or NUL. */
static void parse_name_list_lib(const char *csv,
                                 int *out_ids, int *count, int max)
{
    char name[32];
    int nlen;
    *count = 0;

    while (*csv && *csv != ' ' && *csv != '\t' && *count < max) {
        nlen = 0;
        while (*csv && *csv != ',' && *csv != ' ' && *csv != '\t'
               && nlen < (int)sizeof(name) - 1)
            name[nlen++] = *csv++;
        name[nlen] = '\0';

        if (*csv == ',')
            csv++;

        /* Strip .library/.device/.resource suffix */
        {
            static const char *suffixes[] = {
                ".library", ".device", ".resource", NULL
            };
            const char **sfx;
            for (sfx = suffixes; *sfx; sfx++) {
                int slen = strlen(*sfx);
                if (nlen > slen &&
                    stricmp(&name[nlen - slen], *sfx) == 0) {
                    name[nlen - slen] = '\0';
                    nlen -= slen;
                    break;
                }
            }
        }

        /* Look up lib_id (first match in func_table) */
        {
            int idx;
            for (idx = 0; idx < (int)FUNC_TABLE_SIZE; idx++) {
                if (stricmp(name, func_table[idx].lib_name) == 0) {
                    /* Deduplicate: skip if lib_id already present */
                    int dup = 0, j;
                    for (j = 0; j < *count; j++) {
                        if (out_ids[j] == func_table[idx].lib_id) {
                            dup = 1;
                            break;
                        }
                    }
                    if (!dup) {
                        out_ids[*count] = func_table[idx].lib_id;
                        (*count)++;
                    }
                    break;
                }
            }
            /* Unknown library: silently skip */
        }
    }
}

/* Parse a comma-separated list of function names into output arrays.
 *
 * Looks up (lib_id, lvo) pairs in func_table. Populates
 * out_lib_ids[] and out_lvos[].
 *
 * Supports library-scoped names: "dos.Open,exec.AllocMem"
 * (dot separator between library and function name).
 * Plain names "Open,Lock" also work (global search).
 *
 * Unknown names are silently skipped (robust against typos).
 * Parsing stops at space, tab, or NUL. */
static void parse_name_list_func(const char *csv,
                                  int *out_lib_ids,
                                  WORD *out_lvos,
                                  int *count, int max)
{
    char name[32];
    int nlen;
    *count = 0;

    while (*csv && *csv != ' ' && *csv != '\t' && *count < max) {
        nlen = 0;
        while (*csv && *csv != ',' && *csv != ' ' && *csv != '\t'
               && nlen < (int)sizeof(name) - 1)
            name[nlen++] = *csv++;
        name[nlen] = '\0';

        if (*csv == ',')
            csv++;

        /* Look up (lib_id, lvo) pair from func_table.
         * Check for dot-separator (library-scoped syntax). */
        {
            int idx;
            char *dot = strchr(name, '.');
            if (dot) {
                /* Library-scoped: split at dot, match both fields */
                const char *fn = dot + 1;
                *dot = '\0';
                for (idx = 0; idx < (int)FUNC_TABLE_SIZE; idx++) {
                    if (stricmp(name, func_table[idx].lib_name) == 0 &&
                        stricmp(fn, func_table[idx].func_name) == 0) {
                        /* Deduplicate: skip if (lib_id, lvo) already present */
                        int dup = 0, j;
                        for (j = 0; j < *count; j++) {
                            if (out_lib_ids[j] == func_table[idx].lib_id &&
                                out_lvos[j] == func_table[idx].lvo_offset) {
                                dup = 1;
                                break;
                            }
                        }
                        if (!dup) {
                            out_lib_ids[*count] = func_table[idx].lib_id;
                            out_lvos[*count] = func_table[idx].lvo_offset;
                            (*count)++;
                        }
                        break;
                    }
                }
            } else {
                /* Global search by function name only */
                for (idx = 0; idx < (int)FUNC_TABLE_SIZE; idx++) {
                    if (stricmp(name, func_table[idx].func_name) == 0) {
                        /* Deduplicate: skip if (lib_id, lvo) already present */
                        int dup = 0, j;
                        for (j = 0; j < *count; j++) {
                            if (out_lib_ids[j] == func_table[idx].lib_id &&
                                out_lvos[j] == func_table[idx].lvo_offset) {
                                dup = 1;
                                break;
                            }
                        }
                        if (!dup) {
                            out_lib_ids[*count] = func_table[idx].lib_id;
                            out_lvos[*count] = func_table[idx].lvo_offset;
                            (*count)++;
                        }
                        break;
                    }
                }
            }
            /* Unknown function: silently skip */
        }
    }
}

/* Parse extended FILTER with comma-separated lists and blacklists.
 * Examples:
 *   FILTER LIB=dos,exec
 *   FILTER -FUNC=AllocMem,GetMsg
 *   FILTER LIB=dos -FUNC=Close ERRORS
 *   FILTER             (empty = clear all)
 *
 * Handles both simple and extended filter syntax. When arguments
 * contain no commas or blacklist prefixes, delegates to parse_filters()
 * for exact backward compatibility. */
static void parse_extended_filter(const char *args,
                                   struct trace_state *ts)
{
    /* Reset all filter state (both simple and extended) */
    ts->use_extended_filter = 0;
    ts->filter_lib_id = -1;
    ts->filter_lvo = 0;
    ts->filter_errors_only = 0;
    ts->filter_procname[0] = '\0';
    ts->lib_filter_mode = 0;
    ts->lib_filter_count = 0;
    ts->func_filter_mode = 0;
    ts->func_filter_count = 0;

    if (!args || !args[0]) {
        /* Empty = clear all filters */
        return;
    }

    /* Check if this needs extended mode (commas or blacklist
     * prefixes). S4 fix: check specifically for -LIB= and -FUNC=
     * prefixes rather than scanning for any '-' character, because
     * '-' appears legitimately in process names (e.g. PROC=my-app). */
    {
        const char *scan = args;
        int needs_extended = 0;
        while (*scan) {
            if (*scan == ',')
                needs_extended = 1;
            /* Check for blacklist prefix at word boundary.
             * A '-' at the start of the string or after whitespace
             * followed by LIB= or FUNC= indicates blacklist mode. */
            if (*scan == '-' && (scan == args || scan[-1] == ' '
                    || scan[-1] == '\t')) {
                if (strnicmp(scan, "-LIB=", 5) == 0 ||
                    strnicmp(scan, "-FUNC=", 6) == 0)
                    needs_extended = 1;
            }
            /* ENABLE= and DISABLE= always require extended mode */
            if (scan == args || scan[-1] == ' ' || scan[-1] == '\t') {
                if (strnicmp(scan, "ENABLE=", 7) == 0 ||
                    strnicmp(scan, "DISABLE=", 8) == 0)
                    needs_extended = 1;
            }
            scan++;
        }

        if (!needs_extended) {
            /* Simple single-value syntax -- delegate to
             * parse_filters() for exact backward compatibility */
            parse_filters(args, ts);
            return;
        }
    }

    ts->use_extended_filter = 1;

    while (*args) {
        while (*args == ' ' || *args == '\t')
            args++;
        if (*args == '\0')
            break;

        if (strnicmp(args, "-LIB=", 5) == 0) {
            ts->lib_filter_mode = -1;  /* blacklist */
            args += 5;
            parse_name_list_lib(args, ts->lib_filter_ids,
                                &ts->lib_filter_count,
                                MAX_FILTER_NAMES);
            args = skip_to_space(args);
        } else if (strnicmp(args, "LIB=", 4) == 0) {
            ts->lib_filter_mode = 1;   /* whitelist */
            args += 4;
            parse_name_list_lib(args, ts->lib_filter_ids,
                                &ts->lib_filter_count,
                                MAX_FILTER_NAMES);
            args = skip_to_space(args);
        } else if (strnicmp(args, "-FUNC=", 6) == 0) {
            ts->func_filter_mode = -1;
            args += 6;
            parse_name_list_func(args,
                                 ts->func_filter_lib_ids,
                                 ts->func_filter_lvos,
                                 &ts->func_filter_count,
                                 MAX_FILTER_NAMES);
            args = skip_to_space(args);
        } else if (strnicmp(args, "FUNC=", 5) == 0) {
            ts->func_filter_mode = 1;
            args += 5;
            parse_name_list_func(args,
                                 ts->func_filter_lib_ids,
                                 ts->func_filter_lvos,
                                 &ts->func_filter_count,
                                 MAX_FILTER_NAMES);
            args = skip_to_space(args);
        } else if (strnicmp(args, "ENABLE=", 7) == 0) {
            /* NOTE: ENABLE=/DISABLE= modify g_anchor->patches[].enabled which
             * is GLOBAL state -- all connected trace clients are affected.
             * This differs from LIB=/FUNC=/PROC= which are per-session. */
            /* Enable specific patches (fire-and-forget during streaming) */
            char name[32];
            int nlen;
            args += 7;
            while (*args && *args != ' ' && *args != '\t') {
                nlen = 0;
                while (*args && *args != ',' && *args != ' ' && *args != '\t'
                       && nlen < (int)sizeof(name) - 1)
                    name[nlen++] = *args++;
                name[nlen] = '\0';
                if (*args == ',')
                    args++;

                {
                    int pidx = find_patch_index_by_name(name);
                    if (pidx >= 0 && pidx < (int)g_anchor->patch_count)
                        g_anchor->patches[pidx].enabled = 1;
                }
            }
            /* Force extended mode so we don't fall through to simple parser */
            ts->use_extended_filter = 1;
        } else if (strnicmp(args, "DISABLE=", 8) == 0) {
            /* Disable specific patches (fire-and-forget during streaming) */
            char name[32];
            int nlen;
            args += 8;
            while (*args && *args != ' ' && *args != '\t') {
                nlen = 0;
                while (*args && *args != ',' && *args != ' ' && *args != '\t'
                       && nlen < (int)sizeof(name) - 1)
                    name[nlen++] = *args++;
                name[nlen] = '\0';
                if (*args == ',')
                    args++;

                {
                    int pidx = find_patch_index_by_name(name);
                    if (pidx >= 0 && pidx < (int)g_anchor->patch_count)
                        g_anchor->patches[pidx].enabled = 0;
                }
            }
            ts->use_extended_filter = 1;
        } else if (strnicmp(args, "PROC=", 5) == 0) {
            /* Single proc filter (substring match).
             * C5 note: Multi-value PROC is not supported
             * server-side. Process filtering in the toggle grid
             * is client-side only. */
            int pi = 0;
            args += 5;
            while (*args && *args != ' ' && *args != '\t' &&
                   pi < (int)sizeof(ts->filter_procname) - 1)
                ts->filter_procname[pi++] = *args++;
            ts->filter_procname[pi] = '\0';
        } else if (strnicmp(args, "ERRORS", 6) == 0) {
            ts->filter_errors_only = 1;
            args += 6;
        } else {
            /* Unknown keyword: skip to next space */
            while (*args && *args != ' ' && *args != '\t')
                args++;
        }
    }
}

/* Check if an event matches a client's filter criteria.
 * All filters are AND-combined: all must match for the event to pass.
 * Returns 1 if event matches (should be sent), 0 if filtered out. */
static int trace_filter_match(struct trace_state *ts,
                               struct atrace_event *ev,
                               const char *task_name)
{
    if (ts->use_extended_filter) {
        /* Extended lib filter */
        if (ts->lib_filter_mode == 1) {
            /* Whitelist: event lib_id must be in the list */
            int found = 0, j;
            for (j = 0; j < ts->lib_filter_count; j++) {
                if (ev->lib_id == ts->lib_filter_ids[j]) {
                    found = 1;
                    break;
                }
            }
            if (!found) return 0;
        } else if (ts->lib_filter_mode == -1) {
            /* Blacklist: event lib_id must NOT be in the list */
            int j;
            for (j = 0; j < ts->lib_filter_count; j++) {
                if (ev->lib_id == ts->lib_filter_ids[j])
                    return 0;
            }
        }

        /* Extended func filter using (lib_id, lvo) pairs (M2 fix).
         * No func_table scan needed -- pairs were resolved at parse
         * time by parse_name_list_func(). */
        if (ts->func_filter_mode != 0) {
            int found = 0, j;
            for (j = 0; j < ts->func_filter_count; j++) {
                if (ev->lib_id == ts->func_filter_lib_ids[j] &&
                    ev->lvo_offset == ts->func_filter_lvos[j]) {
                    found = 1;
                    break;
                }
            }
            if (ts->func_filter_mode == 1 && !found)
                return 0;  /* whitelist: not in list */
            if (ts->func_filter_mode == -1 && found)
                return 0;  /* blacklist: in list */
        }

        /* PROC and ERRORS checks are below, outside the if/else.
         * Both simple and extended paths use the same fields. */

    } else {
        /* Simple filters (original, for TRACE START compatibility
         * and FILTER commands without comma/blacklist syntax) */

        /* LIB filter */
        if (ts->filter_lib_id >= 0 &&
            ev->lib_id != ts->filter_lib_id)
            return 0;

        /* FUNC filter (by LVO + lib_id) */
        if (ts->filter_lvo != 0 &&
            ev->lvo_offset != ts->filter_lvo)
            return 0;
    }

    /* ---- Shared checks (both simple and extended paths) ---- */

    /* PROC filter (case-insensitive substring match on task name).
     * Match against the base name only, stripping the [N] CLI number
     * prefix if present, so PROC=7 doesn't match [7] Shell Process. */
    if (ts->filter_procname[0] != '\0') {
        const char *match_name = task_name;
        /* Skip "[N] " prefix if present */
        if (match_name[0] == '[') {
            const char *bracket_end = strchr(match_name, ']');
            if (bracket_end && bracket_end[1] == ' ')
                match_name = bracket_end + 2;
        }
        if (stristr(match_name, ts->filter_procname) == NULL)
            return 0;
    }

    /* ERRORS filter -- per-function error classification */
    if (ts->filter_errors_only) {
        const struct trace_func_entry *fe;
        fe = lookup_func(ev->lib_id, ev->lvo_offset);
        if (fe) {
            switch (fe->error_check) {
            case ERR_CHECK_VOID:
                /* Void function -- never show in ERRORS mode */
                return 0;
            case ERR_CHECK_NULL:
                /* Error when retval == 0 (NULL/FALSE) */
                if (ev->retval != 0)
                    return 0;
                break;
            case ERR_CHECK_NZERO:
                /* Error when retval != 0 (e.g. OpenDevice: 0=success) */
                if (ev->retval == 0)
                    return 0;
                break;
            case ERR_CHECK_ANY:
                /* No clear error convention -- always show */
                break;
            case ERR_CHECK_NONE:
                /* Never an error (e.g. GetMsg NULL is normal) */
                return 0;
            case ERR_CHECK_RC:
                /* Return code: error when rc != 0 */
                if (ev->retval == 0)
                    return 0;
                break;
            case ERR_CHECK_NEGATIVE:
                /* Error when (LONG)retval < 0 (e.g. GetVar: -1=fail, >=0=count) */
                if ((LONG)ev->retval >= 0)
                    return 0;
                break;
            case ERR_CHECK_NEG1:
                /* Error when retval == -1 (0xFFFFFFFF) */
                if (ev->retval != 0xFFFFFFFF)
                    return 0;
                break;
            }
        }
        /* Unknown function: show unconditionally in ERRORS mode */
    }

    return 1;
}

/* ---- Argument format helpers ---- */

/* Format a dos.Open access mode value to a named constant */
static const char *format_access_mode(LONG mode)
{
    switch (mode) {
    case 1005: return "Read";
    case 1006: return "Write";
    case 1004: return "Read/Write";
    default:   return NULL;
    }
}

/* Format a Lock type value */
static const char *format_lock_type(LONG type)
{
    switch (type) {
    case -2: return "Shared";
    case -1: return "Exclusive";
    default:  return NULL;
    }
}

/* Safe flag-name appender for format_*_flags functions.
 * Appends "|name" (or just "name" if at start of buffer).
 * Returns 1 if anything was written, 0 if buffer is full. */
static int append_flag(char *buf, char **pp, int *rem, const char *name)
{
    char *p = *pp;
    int remaining = *rem;

    if (remaining <= 1)
        return 0;

    if (p != buf) {
        *p++ = '|';
        remaining--;
        if (remaining <= 1) {
            *pp = p;
            *rem = remaining;
            return 0;
        }
    }

    {
        int n = snprintf(p, remaining, "%s", name);
        if (n >= remaining)
            n = remaining - 1;
        if (n > 0) {
            p += n;
            remaining -= n;
        }
    }

    *pp = p;
    *rem = remaining;
    return 1;
}

/* Format AllocMem requirements flags */
static void format_memf_flags(ULONG flags, char *buf, int bufsz)
{
    char *p = buf;
    int remaining = bufsz;
    ULONG known = 0;

    buf[0] = '\0';

    /* Show MEMF_ANY when flags == 0 */
    if (flags == 0) {
        snprintf(buf, bufsz, "MEMF_ANY");
        return;
    }

#define MEMF_FLAG(bit, name) \
    if (flags & (bit)) { \
        append_flag(buf, &p, &remaining, name); \
        known |= (bit); \
    }

    MEMF_FLAG(0x00001, "MEMF_PUBLIC")
    MEMF_FLAG(0x00002, "MEMF_CHIP")
    MEMF_FLAG(0x00004, "MEMF_FAST")
    MEMF_FLAG(0x00200, "MEMF_LOCAL")
    MEMF_FLAG(0x00400, "MEMF_KICK")
    MEMF_FLAG(0x00800, "MEMF_24BITDMA")
    MEMF_FLAG(0x10000, "MEMF_CLEAR")
    MEMF_FLAG(0x20000, "MEMF_LARGEST")
    MEMF_FLAG(0x40000, "MEMF_REVERSE")
    MEMF_FLAG(0x80000, "MEMF_TOTAL")

#undef MEMF_FLAG

    /* Show any unknown bits */
    if (flags & ~known) {
        char tmp[20];
        snprintf(tmp, sizeof(tmp), "0x%lx", (unsigned long)(flags & ~known));
        append_flag(buf, &p, &remaining, tmp);
    }
}

/* Format IDCMP (Intuition Direct Communication Message Port) flags */
static void format_idcmp_flags(ULONG flags, char *buf, int bufsz)
{
    char *p = buf;
    int remaining = bufsz;
    ULONG known = 0;

    buf[0] = '\0';

    if (flags == 0) {
        snprintf(buf, bufsz, "0");
        return;
    }

#define IDCMP_FLAG(bit, name) \
    if (flags & (bit)) { \
        append_flag(buf, &p, &remaining, name); \
        known |= (bit); \
    }

    /* Values from NDK intuition/intuition.h */
    IDCMP_FLAG(0x00000001, "SIZEVERIFY")
    IDCMP_FLAG(0x00000002, "NEWSIZE")
    IDCMP_FLAG(0x00000004, "REFRESHWINDOW")
    IDCMP_FLAG(0x00000008, "MOUSEBUTTONS")
    IDCMP_FLAG(0x00000010, "MOUSEMOVE")
    IDCMP_FLAG(0x00000020, "GADGETDOWN")
    IDCMP_FLAG(0x00000040, "GADGETUP")
    IDCMP_FLAG(0x00000080, "REQSET")
    IDCMP_FLAG(0x00000100, "MENUPICK")
    IDCMP_FLAG(0x00000200, "CLOSEWINDOW")
    IDCMP_FLAG(0x00000400, "RAWKEY")
    IDCMP_FLAG(0x00000800, "REQVERIFY")
    IDCMP_FLAG(0x00001000, "REQCLEAR")
    IDCMP_FLAG(0x00002000, "MENUVERIFY")
    IDCMP_FLAG(0x00004000, "NEWPREFS")
    IDCMP_FLAG(0x00008000, "DISKINSERTED")
    IDCMP_FLAG(0x00010000, "DISKREMOVED")
    IDCMP_FLAG(0x00020000, "WBENCHMESSAGE")
    IDCMP_FLAG(0x00040000, "ACTIVEWINDOW")
    IDCMP_FLAG(0x00080000, "INACTIVEWINDOW")
    IDCMP_FLAG(0x00100000, "DELTAMOVE")
    IDCMP_FLAG(0x00200000, "VANILLAKEY")
    IDCMP_FLAG(0x00400000, "INTUITICKS")
    IDCMP_FLAG(0x00800000, "IDCMPUPDATE")
    IDCMP_FLAG(0x01000000, "MENUHELP")
    IDCMP_FLAG(0x02000000, "CHANGEWINDOW")
    IDCMP_FLAG(0x04000000, "GADGETHELP")

#undef IDCMP_FLAG

    /* Show any unknown bits */
    if (flags & ~known) {
        char tmp[20];
        snprintf(tmp, sizeof(tmp), "0x%lx", (unsigned long)(flags & ~known));
        append_flag(buf, &p, &remaining, tmp);
    }
}

/* Check if a string_data value was likely truncated.
 * string_data is 64 bytes; the stub copies at most 63 chars
 * (leaving room for NUL). If strlen == 63, truncation likely. */
static int string_likely_truncated(const char *s)
{
    return (strlen(s) >= 63);
}

/* Lock-to-path cache: maps BPTR lock values to path strings.
 * Populated when Lock() or CreateDir() returns a non-NULL lock with
 * string_data containing the path. Used by CurrentDir to resolve
 * lock arguments to readable paths.
 *
 * Open() is NOT cached here: Open() returns a BPTR to a FileHandle,
 * not a FileLock. CurrentDir() takes a FileLock, so Open() return
 * values will never produce valid cache hits. Storing them would
 * waste slots and risk false matches if AmigaOS reuses the BPTR
 * address for a different type.
 *
 * The cache is 128 entries with FIFO eviction.
 * Lock values are opaque 32-bit integers (BPTRs shifted << 2). */
#define LOCK_CACHE_SIZE  128

struct lock_cache_entry {
    ULONG lock_val;     /* retval from Lock/CreateDir */
    char  path[64];     /* path string (59 chars + NUL fits in 64) */
};

static struct lock_cache_entry lock_cache[LOCK_CACHE_SIZE];
static int lock_cache_next = 0;  /* FIFO write index */

static void lock_cache_add(ULONG lock_val, const char *path)
{
    if (lock_val == 0 || !path || !path[0])
        return;
    lock_cache[lock_cache_next].lock_val = lock_val;
    strncpy(lock_cache[lock_cache_next].path, path, 63);
    lock_cache[lock_cache_next].path[63] = '\0';
    lock_cache_next = (lock_cache_next + 1) % LOCK_CACHE_SIZE;
}

static const char *lock_cache_lookup(ULONG lock_val)
{
    int i;
    if (lock_val == 0)
        return NULL;
    for (i = 0; i < LOCK_CACHE_SIZE; i++) {
        if (lock_cache[i].lock_val == lock_val)
            return lock_cache[i].path;
    }
    return NULL;
}

/* Clear the lock cache. Must be called at the start of each trace
 * session to prevent stale mappings from a previous session.
 * AmigaOS can reuse BPTR addresses after locks are freed, so
 * cross-session cache entries could resolve the wrong path. */
static void lock_cache_clear(void)
{
    memset(lock_cache, 0, sizeof(lock_cache));
    lock_cache_next = 0;
}

/* Remove a lock from the cache. Called when UnLock() consumes a lock.
 * Prevents stale entries from causing incorrect path resolution when
 * AmigaOS reuses BPTR addresses for different locks. */
static void lock_cache_remove(ULONG lock_val)
{
    int i;
    if (lock_val == 0)
        return;
    for (i = 0; i < LOCK_CACHE_SIZE; i++) {
        if (lock_cache[i].lock_val == lock_val) {
            lock_cache[i].lock_val = 0;
            lock_cache[i].path[0] = '\0';
            return;
        }
    }
}


/* ---- File handle cache ----
 *
 * Maps Open() return values (BPTR file handles) to their path strings.
 * Used to resolve Close() arguments to human-readable paths.
 *
 * Unlike lock_cache (FIFO eviction only), fh_cache has explicit
 * remove on Close -- AmigaOS reuses file handle addresses, so stale
 * entries could cause incorrect path attribution.
 *
 * Populated from successful Open() events (retval != 0).
 * Looked up and removed by Close() events. */

#define FH_CACHE_SIZE  128

struct fh_cache_entry {
    ULONG fh_val;       /* retval BPTR from Open() */
    char  path[64];     /* path string */
};

static struct fh_cache_entry fh_cache[FH_CACHE_SIZE];
static int fh_cache_next = 0;

static void fh_cache_add(ULONG fh_val, const char *path)
{
    if (fh_val == 0 || !path || !path[0])
        return;
    fh_cache[fh_cache_next].fh_val = fh_val;
    strncpy(fh_cache[fh_cache_next].path, path, 63);
    fh_cache[fh_cache_next].path[63] = '\0';
    fh_cache_next = (fh_cache_next + 1) % FH_CACHE_SIZE;
}

static const char *fh_cache_lookup(ULONG fh_val)
{
    int i;
    if (fh_val == 0)
        return NULL;
    for (i = 0; i < FH_CACHE_SIZE; i++) {
        if (fh_cache[i].fh_val == fh_val)
            return fh_cache[i].path;
    }
    return NULL;
}

static void fh_cache_remove(ULONG fh_val)
{
    int i;
    if (fh_val == 0)
        return;
    for (i = 0; i < FH_CACHE_SIZE; i++) {
        if (fh_cache[i].fh_val == fh_val) {
            fh_cache[i].fh_val = 0;
            fh_cache[i].path[0] = '\0';
            return;
        }
    }
}

static void fh_cache_clear(void)
{
    memset(fh_cache, 0, sizeof(fh_cache));
    fh_cache_next = 0;
}

/* ---- DiskObject pointer-to-name cache ----
 * Maps GetDiskObject() return values (DiskObject pointers) to their
 * name strings.  Used to resolve FreeDiskObject() arguments to
 * human-readable names.
 *
 * Like fh_cache, has explicit remove on FreeDiskObject -- AmigaOS
 * reuses DiskObject addresses after FreeDiskObject. */

#define DISKOBJ_CACHE_SIZE  128

struct diskobj_cache_entry {
    ULONG obj_val;      /* retval pointer from GetDiskObject() */
    char  name[64];     /* name string */
};

static struct diskobj_cache_entry diskobj_cache[DISKOBJ_CACHE_SIZE];
static int diskobj_cache_next = 0;

static void diskobj_cache_add(ULONG obj_val, const char *name)
{
    if (obj_val == 0 || !name || !name[0])
        return;
    diskobj_cache[diskobj_cache_next].obj_val = obj_val;
    strncpy(diskobj_cache[diskobj_cache_next].name, name, 63);
    diskobj_cache[diskobj_cache_next].name[63] = '\0';
    diskobj_cache_next = (diskobj_cache_next + 1) % DISKOBJ_CACHE_SIZE;
}

static const char *diskobj_cache_lookup(ULONG obj_val)
{
    int i;
    if (obj_val == 0)
        return NULL;
    for (i = 0; i < DISKOBJ_CACHE_SIZE; i++) {
        if (diskobj_cache[i].obj_val == obj_val)
            return diskobj_cache[i].name;
    }
    return NULL;
}

static void diskobj_cache_remove(ULONG obj_val)
{
    int i;
    if (obj_val == 0)
        return;
    for (i = 0; i < DISKOBJ_CACHE_SIZE; i++) {
        if (diskobj_cache[i].obj_val == obj_val) {
            diskobj_cache[i].obj_val = 0;
            diskobj_cache[i].name[0] = '\0';
            return;
        }
    }
}

static void diskobj_cache_clear(void)
{
    memset(diskobj_cache, 0, sizeof(diskobj_cache));
    diskobj_cache_next = 0;
}

/* ---- Segment-to-filename cache ----
 * Maps LoadSeg()/NewLoadSeg() return values (BPTR segment lists) to
 * their filename strings.  Used to resolve RunCommand() and
 * UnLoadSeg() arguments to human-readable program names.
 *
 * Like fh_cache, has explicit remove on UnLoadSeg -- AmigaOS reuses
 * segment list addresses after UnLoadSeg frees the memory. */

#define SEG_CACHE_SIZE  128

static struct { ULONG seg; char name[60]; } seg_cache[SEG_CACHE_SIZE];
static int seg_cache_next = 0;

static void seg_cache_add(ULONG seg, const char *name)
{
    if (seg == 0 || !name || !name[0])
        return;
    seg_cache[seg_cache_next].seg = seg;
    strncpy(seg_cache[seg_cache_next].name, name, 59);
    seg_cache[seg_cache_next].name[59] = '\0';
    seg_cache_next = (seg_cache_next + 1) % SEG_CACHE_SIZE;
}

static const char *seg_cache_lookup(ULONG seg)
{
    int i;
    if (seg == 0)
        return NULL;
    for (i = 0; i < SEG_CACHE_SIZE; i++) {
        if (seg_cache[i].seg == seg)
            return seg_cache[i].name;
    }
    return NULL;
}

static void seg_cache_remove(ULONG seg)
{
    int i;
    if (seg == 0)
        return;
    for (i = 0; i < SEG_CACHE_SIZE; i++) {
        if (seg_cache[i].seg == seg) {
            seg_cache[i].seg = 0;
            seg_cache[i].name[0] = '\0';
            return;
        }
    }
}

static void seg_cache_clear(void)
{
    memset(seg_cache, 0, sizeof(seg_cache));
    seg_cache_next = 0;
}

/* ---- Trace log header emission ---- */

/* Convert Amiga DateStamp days (epoch 1978-01-01) to year/month/day.
 * Simple day-counting algorithm: subtract days per year (accounting
 * for leap years), then days per month. No external library deps. */
static void amiga_days_to_ymd(LONG days, int *year, int *month, int *day)
{
    static const int mdays[12] = {31,28,31,30,31,30,31,31,30,31,30,31};
    int y = 1978;
    int m, leap, yd;

    /* Find year */
    for (;;) {
        leap = (y % 4 == 0 && (y % 100 != 0 || y % 400 == 0)) ? 1 : 0;
        yd = 365 + leap;
        if (days < yd)
            break;
        days -= yd;
        y++;
    }

    /* Find month */
    leap = (y % 4 == 0 && (y % 100 != 0 || y % 400 == 0)) ? 1 : 0;
    for (m = 0; m < 12; m++) {
        int md = mdays[m] + (m == 1 ? leap : 0);
        if (days < md)
            break;
        days -= md;
    }

    *year = y;
    *month = m + 1;
    *day = (int)days + 1;
}

/* Map a lib_id to its library name string using func_table[].
 * Returns "?" if not found. */
static const char *lib_id_to_name(int lib_id)
{
    int i;
    for (i = 0; i < (int)FUNC_TABLE_SIZE; i++) {
        if (func_table[i].lib_id == (UBYTE)lib_id)
            return func_table[i].lib_name;
    }
    return "?";
}

/* Emit trace header comment lines as DATA chunks at the start of a
 * TRACE START or TRACE RUN session. Returns 0 on success, -1 on
 * send failure (client disconnected).
 *
 * Header format:
 *   # atrace v2, 2026-03-06 19:33:38
 *   # command: C:atrace_test           (TRACE RUN only)
 *   # filter: tier=basic                (no explicit filters)
 *   # filter: tier=detail, PROC=control LIB=dos ERRORS
 *   # enabled: GetMsg, PutMsg (normally noise-disabled)
 *   # disabled: ModifyIDCMP (manually disabled)
 */

/* Return human-readable name for the current tier level. */
static const char *tier_name(int level)
{
    switch (level) {
    case 1:  return "basic";
    case 2:  return "detail";
    case 3:  return "verbose";
    default: return "?";
    }
}

/* Build a human-readable filter description string from trace_state.
 * Returns the length written to buf (0 if no filters active).
 * Used by emit_trace_header() and by the FILTER command handler to
 * emit a filter comment after mid-stream filter changes. */
static int build_filter_desc(struct trace_state *ts, char *buf, int bufsz)
{
    int flen = 0;

    buf[0] = '\0';

    if (ts->filter_procname[0] != '\0') {
        flen += snprintf(buf + flen, bufsz - flen,
                         "PROC=%s", ts->filter_procname);
    }

    if (ts->use_extended_filter) {
        /* Extended lib filter */
        if (ts->lib_filter_mode != 0) {
            int j;
            if (flen > 0 && flen < bufsz - 1)
                buf[flen++] = ' ';
            if (ts->lib_filter_mode == -1) {
                flen += snprintf(buf + flen, bufsz - flen, "-LIB=");
            } else {
                flen += snprintf(buf + flen, bufsz - flen, "LIB=");
            }
            for (j = 0; j < ts->lib_filter_count; j++) {
                if (j > 0 && flen < bufsz - 1)
                    buf[flen++] = ',';
                flen += snprintf(buf + flen, bufsz - flen,
                                 "%s",
                                 lib_id_to_name(ts->lib_filter_ids[j]));
            }
        }

        /* Extended func filter -- emit lib.func format */
        if (ts->func_filter_mode != 0) {
            int j;
            if (flen > 0 && flen < bufsz - 1)
                buf[flen++] = ' ';
            if (ts->func_filter_mode == -1) {
                flen += snprintf(buf + flen, bufsz - flen, "-FUNC=");
            } else {
                flen += snprintf(buf + flen, bufsz - flen, "FUNC=");
            }
            for (j = 0; j < ts->func_filter_count; j++) {
                const struct trace_func_entry *fe;
                if (j > 0 && flen < bufsz - 1)
                    buf[flen++] = ',';
                fe = lookup_func(
                    (UBYTE)ts->func_filter_lib_ids[j],
                    ts->func_filter_lvos[j]);
                if (fe) {
                    flen += snprintf(buf + flen, bufsz - flen,
                                     "%s.%s",
                                     fe->lib_name, fe->func_name);
                } else {
                    flen += snprintf(buf + flen, bufsz - flen,
                                     "?");
                }
            }
        }
    } else {
        /* Simple filters */
        if (ts->filter_lib_id >= 0) {
            if (flen > 0 && flen < bufsz - 1)
                buf[flen++] = ' ';
            flen += snprintf(buf + flen, bufsz - flen,
                             "LIB=%s",
                             lib_id_to_name(ts->filter_lib_id));
        }

        if (ts->filter_lvo != 0) {
            const struct trace_func_entry *fe;
            if (flen > 0 && flen < bufsz - 1)
                buf[flen++] = ' ';
            /* Simple filter uses filter_lib_id + filter_lvo.
             * Always emit lib.func format for unambiguous display. */
            fe = lookup_func(
                (UBYTE)(ts->filter_lib_id >= 0
                        ? ts->filter_lib_id : 0),
                ts->filter_lvo);
            if (fe) {
                flen += snprintf(buf + flen, bufsz - flen,
                                 "FUNC=%s.%s",
                                 fe->lib_name, fe->func_name);
            } else {
                flen += snprintf(buf + flen, bufsz - flen,
                                 "FUNC=?");
            }
        }
    }

    if (ts->filter_errors_only) {
        if (flen > 0 && flen < bufsz - 1)
            buf[flen++] = ' ';
        flen += snprintf(buf + flen, bufsz - flen, "ERRORS");
    }

    buf[bufsz - 1] = '\0';
    return flen;
}

static int emit_trace_header(LONG fd, struct trace_state *ts,
                              const char *run_command)
{
    char line[512];
    struct DateStamp ds;
    int yr, mo, dy, hr, mn, sc;
    int i;

    /* Line 1: version and timestamp */
    DateStamp(&ds);
    amiga_days_to_ymd(ds.ds_Days, &yr, &mo, &dy);
    hr = ds.ds_Minute / 60;
    mn = ds.ds_Minute % 60;
    sc = ds.ds_Tick / TICKS_PER_SECOND;

    snprintf(line, sizeof(line),
             "# atrace v%d, %04d-%02d-%02d %02d:%02d:%02d",
             (int)(g_anchor ? g_anchor->version : ATRACE_VERSION),
             yr, mo, dy, hr, mn, sc);
    if (send_trace_data_chunk(fd, line) < 0)
        return -1;

    /* EClock info (v3+ only) */
    if (g_eclock_valid && g_eclock_freq != 0) {
        snprintf(line, sizeof(line), "# eclock_freq: %lu Hz",
                 (unsigned long)g_eclock_freq);
        if (send_trace_data_chunk(fd, line) < 0)
            return -1;
        if (send_trace_data_chunk(fd, "# timestamp_precision: microsecond") < 0)
            return -1;
    }

    /* Line 2 (TRACE RUN only): command being traced */
    if (run_command && run_command[0]) {
        snprintf(line, sizeof(line), "# command: %s", run_command);
        if (send_trace_data_chunk(fd, line) < 0)
            return -1;
    }

    /* Line 3: active filter description (always includes tier) */
    {
        char filter_desc[384];
        int flen;

        flen = build_filter_desc(ts, filter_desc, sizeof(filter_desc));
        if (flen == 0) {
            snprintf(line, sizeof(line), "# filter: tier=%s",
                     tier_name(g_current_tier));
        } else {
            snprintf(line, sizeof(line), "# filter: tier=%s, %s",
                     tier_name(g_current_tier), filter_desc);
        }
        if (send_trace_data_chunk(fd, line) < 0)
            return -1;
    }

    /* Lines 4-5: deviations from default enable/disable state */
    if (g_anchor && g_anchor->patches) {
        char enabled_devs[512];
        char disabled_devs[512];
        int en_len = 0, dis_len = 0;
        int en_overflow = 0, dis_overflow = 0;
        int patch_count = (int)g_anchor->patch_count;

        enabled_devs[0] = '\0';
        disabled_devs[0] = '\0';

        for (i = 0; i < patch_count && i < (int)FUNC_TABLE_SIZE; i++) {
            struct atrace_patch *p = &g_anchor->patches[i];
            const struct trace_func_entry *fe =
                lookup_func(p->lib_id, p->lvo_offset);
            int is_noise = 0;
            const char **np;

            if (!fe)
                continue;

            for (np = noise_func_names; *np; np++) {
                if (strcmp(fe->func_name, *np) == 0) {
                    is_noise = 1;
                    break;
                }
            }

            if (is_noise && p->enabled) {
                /* Noise function manually enabled -- deviation */
                int nlen = strlen(fe->func_name);
                if (en_len + nlen + 2 < (int)sizeof(enabled_devs) - 32) {
                    if (en_len > 0) {
                        enabled_devs[en_len++] = ',';
                        enabled_devs[en_len++] = ' ';
                    }
                    memcpy(enabled_devs + en_len, fe->func_name, nlen);
                    en_len += nlen;
                    enabled_devs[en_len] = '\0';
                } else {
                    en_overflow++;
                }
            } else if (!is_noise && !p->enabled) {
                /* Non-noise function manually disabled -- deviation */
                int nlen = strlen(fe->func_name);
                if (dis_len + nlen + 2 < (int)sizeof(disabled_devs) - 32) {
                    if (dis_len > 0) {
                        disabled_devs[dis_len++] = ',';
                        disabled_devs[dis_len++] = ' ';
                    }
                    memcpy(disabled_devs + dis_len, fe->func_name, nlen);
                    dis_len += nlen;
                    disabled_devs[dis_len] = '\0';
                } else {
                    dis_overflow++;
                }
            }
        }

        if (en_overflow > 0) {
            snprintf(enabled_devs + en_len,
                     sizeof(enabled_devs) - en_len,
                     ", ... and %d more", en_overflow);
        }
        if (dis_overflow > 0) {
            snprintf(disabled_devs + dis_len,
                     sizeof(disabled_devs) - dis_len,
                     ", ... and %d more", dis_overflow);
        }

        if (en_len > 0 || en_overflow > 0) {
            snprintf(line, sizeof(line),
                     "# enabled: %s (normally noise-disabled)",
                     enabled_devs);
            if (send_trace_data_chunk(fd, line) < 0)
                return -1;
        }
        if (dis_len > 0 || dis_overflow > 0) {
            snprintf(line, sizeof(line),
                     "# disabled: %s (manually disabled)",
                     disabled_devs);
            if (send_trace_data_chunk(fd, line) < 0)
                return -1;
        }
    }

    return 0;
}

/* Socket domain (address family) names */
static const char *format_af(LONG domain)
{
    switch (domain) {
    case 0:  return "AF_UNSPEC";
    case 2:  return "AF_INET";
    default: return NULL;
    }
}

/* Socket type names */
static const char *format_socktype(LONG type)
{
    switch (type) {
    case 1: return "SOCK_STREAM";
    case 2: return "SOCK_DGRAM";
    case 3: return "SOCK_RAW";
    default: return NULL;
    }
}

/* Shutdown how names */
static const char *format_shutdown_how(LONG how)
{
    switch (how) {
    case 0: return "SHUT_RD";
    case 1: return "SHUT_WR";
    case 2: return "SHUT_RDWR";
    default: return NULL;
    }
}

/* Seek offset mode names */
static const char *format_seek_offset(LONG offset)
{
    switch (offset) {
    case -1: return "OFFSET_BEGINNING";
    case  0: return "OFFSET_CURRENT";
    case  1: return "OFFSET_END";
    default: return NULL;
    }
}

/* Signal set bit names for exec.Wait/Signal display.
 * Returns the number of characters written to buf. */
static int format_signal_set(ULONG sigs, char *buf, int bufsz)
{
    int n = 0;
    int first = 1;
    /* Check for well-known signal bits.
     * static const: avoid reconstructing on every call (68k stack/CPU). */
    static const struct { ULONG bit; const char *name; } known[] = {
        { 1UL << 12, "CTRL_C" },
        { 1UL << 13, "CTRL_D" },
        { 1UL << 14, "CTRL_E" },
        { 1UL << 15, "CTRL_F" },
    };
    int ki;

    n = snprintf(buf, bufsz, "0x%08lx", (unsigned long)sigs);
    if (n >= bufsz) return n;

    /* Append known signal names in parentheses */
    for (ki = 0; ki < 4; ki++) {
        if (sigs & known[ki].bit) {
            if (first) {
                int m = snprintf(buf + n, bufsz - n, " (");
                if (m > 0) n += m;
                first = 0;
            } else {
                int m = snprintf(buf + n, bufsz - n, "|");
                if (m > 0) n += m;
            }
            {
                int m = snprintf(buf + n, bufsz - n, "%s", known[ki].name);
                if (m > 0) n += m;
            }
        }
    }
    if (!first && n < bufsz - 1) {
        buf[n++] = ')';
        buf[n] = '\0';
    }
    return n;
}

/* Generic argument formatter -- dispatches to per-function formatters. */
static void format_args(struct atrace_event *ev,
                        const struct trace_func_entry *fe,
                        char *buf, int bufsz)
{
    int i;
    char *p = buf;
    int remaining = bufsz;
    const char *trunc = "";

    buf[0] = '\0';

    if (fe && fe->has_string && string_likely_truncated(ev->string_data)) {
        trunc = "...";
    }

    if (!fe) {
        /* Unknown function -- dump raw args */
        for (i = 0; i < ev->arg_count && i < 4; i++) {
            int n;
            if (i > 0) {
                if (remaining <= 1) break;
                *p++ = ','; remaining--;
            }
            if (remaining <= 1) break;
            n = snprintf(p, remaining, "0x%lx", (unsigned long)ev->args[i]);
            if (n >= remaining) n = remaining - 1;
            if (n > 0) { p += n; remaining -= n; }
        }
        return;
    }

    /* --- exec.library --- */

    if (fe->lib_id == LIB_EXEC) {
        switch (fe->lvo_offset) {

        case -390:  /* FindPort(name) */
            p += snprintf(p, remaining, "\"%s%s\"",
                          ev->string_data, trunc);
            return;

        case -96:   /* FindResident(name) */
            p += snprintf(p, remaining, "\"%s%s\"",
                          ev->string_data, trunc);
            return;

        case -594:  /* FindSemaphore(name) */
            p += snprintf(p, remaining, "\"%s%s\"",
                          ev->string_data, trunc);
            return;

        case -294:  /* FindTask(name) */
            if (ev->args[0] == 0) {
                p += snprintf(p, remaining, "NULL (self)");
            } else {
                p += snprintf(p, remaining, "\"%s%s\"",
                              ev->string_data, trunc);
            }
            return;

        case -444:  /* OpenDevice(devName, unit, ioReq, flags) */
            if (ev->args[3] != 0)
                p += snprintf(p, remaining, "\"%s%s\",unit=%lu,flags=0x%lx",
                              ev->string_data, trunc,
                              (unsigned long)ev->args[1],
                              (unsigned long)ev->args[3]);
            else
                p += snprintf(p, remaining, "\"%s%s\",unit=%lu",
                              ev->string_data, trunc,
                              (unsigned long)ev->args[1]);
            return;

        case -552:  /* OpenLibrary(libName, version) */
            p += snprintf(p, remaining, "\"%s%s\",v%lu",
                          ev->string_data, trunc,
                          (unsigned long)ev->args[1]);
            return;

        case -498:  /* OpenResource(resName) */
            p += snprintf(p, remaining, "\"%s%s\"",
                          ev->string_data, trunc);
            return;

        case -372:  /* GetMsg(port) */
            if (ev->string_data[0])
                p += snprintf(p, remaining, "\"%s%s\"",
                              ev->string_data, trunc);
            else
                p += snprintf(p, remaining, "port=0x%lx",
                              (unsigned long)ev->args[0]);
            return;

        case -366:  /* PutMsg(port, msg) */
            if (ev->string_data[0])
                p += snprintf(p, remaining, "\"%s%s\",msg=0x%lx",
                              ev->string_data, trunc,
                              (unsigned long)ev->args[1]);
            else
                p += snprintf(p, remaining, "port=0x%lx,msg=0x%lx",
                              (unsigned long)ev->args[0],
                              (unsigned long)ev->args[1]);
            return;

        case -564:  /* ObtainSemaphore(sem) */
            if (ev->string_data[0])
                p += snprintf(p, remaining, "\"%s%s\"",
                              ev->string_data, trunc);
            else
                p += snprintf(p, remaining, "sem=0x%lx",
                              (unsigned long)ev->args[0]);
            return;

        case -570:  /* ReleaseSemaphore(sem) */
            if (ev->string_data[0])
                p += snprintf(p, remaining, "\"%s%s\"",
                              ev->string_data, trunc);
            else
                p += snprintf(p, remaining, "sem=0x%lx",
                              (unsigned long)ev->args[0]);
            return;

        case -198:  /* AllocMem(byteSize, requirements) */
        {
            char flags_buf[128];
            format_memf_flags(ev->args[1], flags_buf, sizeof(flags_buf));
            p += snprintf(p, remaining, "%lu,%s",
                          (unsigned long)ev->args[0], flags_buf);
            return;
        }

        case -456:  /* DoIO(ioRequest) */
        case -462:  /* SendIO(ioRequest) */
        case -474:  /* WaitIO(ioRequest) */
        case -480:  /* AbortIO(ioRequest) */
        case -468:  /* CheckIO(ioRequest) */
        case -450:  /* CloseDevice(ioRequest) */
            if (ev->string_data[0])
                p += snprintf(p, remaining, "\"%s%s\" CMD %lu",
                              ev->string_data, trunc,
                              (unsigned long)ev->args[1]);
            else
                p += snprintf(p, remaining, "io=0x%lx",
                              (unsigned long)ev->args[0]);
            return;

        case -210:  /* FreeMem(memoryBlock, byteSize) */
            p += snprintf(p, remaining, "0x%lx,%lu",
                          (unsigned long)ev->args[0],
                          (unsigned long)ev->args[1]);
            return;

        case -684:  /* AllocVec(byteSize, requirements) */
        {
            char flags_buf[128];
            format_memf_flags(ev->args[1], flags_buf, sizeof(flags_buf));
            p += snprintf(p, remaining, "%lu,%s",
                          (unsigned long)ev->args[0], flags_buf);
            return;
        }

        case -690:  /* FreeVec(memoryBlock) */
            p += snprintf(p, remaining, "0x%lx",
                          (unsigned long)ev->args[0]);
            return;

        case -318:  /* Wait(signalSet) */
        {
            char sig_buf[128];
            format_signal_set(ev->args[0], sig_buf, sizeof(sig_buf));
            p += snprintf(p, remaining, "%s", sig_buf);
            return;
        }

        case -324:  /* Signal(task, signalSet) */
        {
            char sig_buf[128];
            const char *task_name;
            format_signal_set(ev->args[1], sig_buf, sizeof(sig_buf));
            task_name = resolve_task_name((APTR)ev->args[0]);
            if (task_name)
                p += snprintf(p, remaining, "\"%s\",%s",
                              task_name, sig_buf);
            else
                p += snprintf(p, remaining, "task=0x%lx,%s",
                              (unsigned long)ev->args[0], sig_buf);
            return;
        }

        case -330:  /* AllocSignal(signalNum) */
            p += snprintf(p, remaining, "sig=%ld",
                          (long)(LONG)ev->args[0]);
            return;

        case -336:  /* FreeSignal(signalNum) */
            p += snprintf(p, remaining, "sig=%ld",
                          (long)(LONG)ev->args[0]);
            return;

        case -666:  /* CreateMsgPort() -- no args */
            return;

        case -672:  /* DeleteMsgPort(port) */
            if (ev->string_data[0])
                p += snprintf(p, remaining, "\"%s%s\"",
                              ev->string_data, trunc);
            else
                p += snprintf(p, remaining, "port=0x%lx",
                              (unsigned long)ev->args[0]);
            return;

        case -414:  /* CloseLibrary(library) */
            if (ev->string_data[0])
                p += snprintf(p, remaining, "\"%s%s\"",
                              ev->string_data, trunc);
            else
                p += snprintf(p, remaining, "lib=0x%lx",
                              (unsigned long)ev->args[0]);
            return;

        case -378:  /* ReplyMsg(message) */
            p += snprintf(p, remaining, "msg=0x%lx",
                          (unsigned long)ev->args[0]);
            return;

        case -354:  /* AddPort(port) */
            if (ev->string_data[0])
                p += snprintf(p, remaining, "\"%s%s\"",
                              ev->string_data, trunc);
            else
                p += snprintf(p, remaining, "port=0x%lx",
                              (unsigned long)ev->args[0]);
            return;

        case -384:  /* WaitPort(port) */
            if (ev->string_data[0])
                p += snprintf(p, remaining, "\"%s%s\"",
                              ev->string_data, trunc);
            else
                p += snprintf(p, remaining, "port=0x%lx",
                              (unsigned long)ev->args[0]);
            return;

        }  /* end exec switch */
    }

    /* --- dos.library --- */

    if (fe->lib_id == LIB_DOS) {
        switch (fe->lvo_offset) {

        case -30:   /* Open(name, accessMode) */
        {
            const char *mode_name = format_access_mode((LONG)ev->args[1]);
            p += snprintf(p, remaining, "\"%s%s\",%s",
                          ev->string_data, trunc,
                          mode_name ? mode_name : "?");
            return;
        }

        case -36:   /* Close(fileHandle) */
        {
            const char *path = fh_cache_lookup(ev->args[0]);
            if (path) {
                p += snprintf(p, remaining, "\"%s\"", path);
                /* Remove from cache -- AmigaOS reuses file handle
                 * addresses after Close. */
                fh_cache_remove(ev->args[0]);
            } else {
                p += snprintf(p, remaining, "fh=0x%lx",
                              (unsigned long)ev->args[0]);
            }
            return;
        }

        case -84:   /* Lock(name, type) */
        {
            const char *type_name = format_lock_type((LONG)ev->args[1]);
            p += snprintf(p, remaining, "\"%s%s\",%s",
                          ev->string_data, trunc,
                          type_name ? type_name : "?");
            return;
        }

        case -72:   /* DeleteFile(name) */
            p += snprintf(p, remaining, "\"%s%s\"",
                          ev->string_data, trunc);
            return;

        case -222:  /* Execute(string, input, output) */
        {
            if (ev->args[1] == 0 && ev->args[2] == 0)
                p += snprintf(p, remaining, "\"%s%s\"",
                              ev->string_data, trunc);
            else if (ev->args[1] == 0)
                p += snprintf(p, remaining, "\"%s%s\",out=0x%lx",
                              ev->string_data, trunc,
                              (unsigned long)ev->args[2]);
            else if (ev->args[2] == 0)
                p += snprintf(p, remaining, "\"%s%s\",in=0x%lx",
                              ev->string_data, trunc,
                              (unsigned long)ev->args[1]);
            else
                p += snprintf(p, remaining, "\"%s%s\",in=0x%lx,out=0x%lx",
                              ev->string_data, trunc,
                              (unsigned long)ev->args[1],
                              (unsigned long)ev->args[2]);
            return;
        }

        case -906:  /* GetVar(name, buffer, size, flags) */
        {
            const char *scope;
            ULONG f = ev->args[3];
            /* GVF_GLOBAL_ONLY=0x100, GVF_LOCAL_ONLY=0x200 */
            if (f & 0x100)      scope = "GLOBAL";
            else if (f & 0x200) scope = "LOCAL";
            else                scope = "ANY";
            p += snprintf(p, remaining, "\"%s%s\",%s",
                          ev->string_data, trunc,
                          scope);
            return;
        }

        case -918:  /* FindVar(name, type) */
        {
            /* FindVar takes a LocalVar type code (LV_VAR=0, LV_ALIAS=1),
             * NOT GVF scope flags. LVF_IGNORE (0x80) is an optional flag. */
            const char *type_name;
            ULONG t = ev->args[1] & 0x7F;  /* mask off LVF_IGNORE */
            switch (t) {
            case 0:  type_name = "LV_VAR"; break;
            case 1:  type_name = "LV_ALIAS"; break;
            default: type_name = NULL; break;
            }
            if (type_name)
                p += snprintf(p, remaining, "\"%s%s\",%s",
                              ev->string_data, trunc, type_name);
            else
                p += snprintf(p, remaining, "\"%s%s\",type=0x%lx",
                              ev->string_data, trunc,
                              (unsigned long)ev->args[1]);
            return;
        }

        case -150:  /* LoadSeg(name) */
            p += snprintf(p, remaining, "\"%s%s\"",
                          ev->string_data, trunc);
            return;

        case -768:  /* NewLoadSeg(file, tags) */
            p += snprintf(p, remaining, "\"%s%s\"",
                          ev->string_data, trunc);
            return;

        case -120:  /* CreateDir(name) */
            p += snprintf(p, remaining, "\"%s%s\"",
                          ev->string_data, trunc);
            return;

        case -444:  /* MakeLink(name, dest, soft) */
        {
            /* Dual-string capture: linkName in string_data[0..31],
             * destPath in string_data[32..63]. */
            const char *link_name = ev->string_data;
            const char *dest_path = &ev->string_data[32];
            const char *link_trunc = (strlen(link_name) >= 31) ? "..." : "";
            const char *dest_trunc = (strlen(dest_path) >= 31) ? "..." : "";
            const char *link_type = ev->args[2] ? "soft" : "hard";
            if (dest_path[0] != '\0') {
                p += snprintf(p, remaining, "\"%s%s\" -> \"%s%s\" %s",
                              link_name, link_trunc,
                              dest_path, dest_trunc, link_type);
            } else {
                /* Second string missing -- fall back to hex */
                p += snprintf(p, remaining, "\"%s%s\" -> 0x%lx %s",
                              link_name, link_trunc,
                              (unsigned long)ev->args[1], link_type);
            }
            return;
        }

        case -78:   /* Rename(oldName, newName) */
        {
            /* Dual-string capture: oldName in string_data[0..31],
             * newName in string_data[32..63]. */
            const char *old_name = ev->string_data;
            const char *new_name = &ev->string_data[32];
            const char *old_trunc = (strlen(old_name) >= 31) ? "..." : "";
            const char *new_trunc = (strlen(new_name) >= 31) ? "..." : "";
            if (new_name[0] != '\0') {
                p += snprintf(p, remaining, "\"%s%s\" -> \"%s%s\"",
                              old_name, old_trunc, new_name, new_trunc);
            } else {
                /* Second string missing -- fall back to hex */
                p += snprintf(p, remaining, "\"%s%s\" -> 0x%lx",
                              old_name, old_trunc,
                              (unsigned long)ev->args[1]);
            }
            return;
        }

        case -504:  /* RunCommand(seg, stack, paramptr, paramlen) */
        {
            const char *prog = seg_cache_lookup(ev->args[0]);
            if (prog)
                p += snprintf(p, remaining, "\"%s\",stack=%lu,%lu",
                              prog,
                              (unsigned long)ev->args[1],
                              (unsigned long)ev->args[3]);
            else
                p += snprintf(p, remaining, "seg=0x%lx,stack=%lu,%lu",
                              (unsigned long)ev->args[0],
                              (unsigned long)ev->args[1],
                              (unsigned long)ev->args[3]);
            return;
        }

        case -900:  /* SetVar(name, buffer, size, flags) */
        {
            const char *scope;
            ULONG f = ev->args[3];
            if (f & 0x100)      scope = "GLOBAL";
            else if (f & 0x200) scope = "LOCAL";
            else                scope = "ANY";
            p += snprintf(p, remaining, "\"%s%s\",%s",
                          ev->string_data, trunc,
                          scope);
            return;
        }

        case -912:  /* DeleteVar(name, flags) */
        {
            const char *scope;
            ULONG f = ev->args[1];
            if (f & 0x100)      scope = "GLOBAL";
            else if (f & 0x200) scope = "LOCAL";
            else                scope = "ANY";
            p += snprintf(p, remaining, "\"%s%s\",%s",
                          ev->string_data, trunc, scope);
            return;
        }

        case -606:  /* SystemTagList(command, tags) */
            p += snprintf(p, remaining, "\"%s%s\"",
                          ev->string_data, trunc);
            return;

        case -678:  /* AddDosEntry(dlist) */
            p += snprintf(p, remaining, "dlist=0x%lx",
                          (unsigned long)ev->args[0]);
            return;

        case -126:  /* CurrentDir(lock) */
            if (ev->args[0] == 0) {
                p += snprintf(p, remaining, "lock=NULL");
            } else {
                const char *path = lock_cache_lookup(ev->args[0]);
                if (path) {
                    p += snprintf(p, remaining, "\"%s\"", path);
                } else if (fe && fe->has_string &&
                           ev->string_data[0] != '\0') {
                    /* Volume name from DEREF_LOCK_VOLUME (e.g., "RAM:").
                     * The "?" suffix indicates that the volume is known
                     * from the lock's fl_Volume field but the subdirectory
                     * path within that volume is not available from the
                     * stub data alone. */
                    p += snprintf(p, remaining, "\"%s?\"",
                                  ev->string_data);
                } else {
                    p += snprintf(p, remaining, "lock=0x%lx",
                                  (unsigned long)ev->args[0]);
                }
            }
            return;

        case -42:   /* Read(file, buffer, length) */
            p += snprintf(p, remaining, "fh=0x%lx,len=%lu",
                          (unsigned long)ev->args[0],
                          (unsigned long)ev->args[2]);
            return;

        case -48:   /* Write(file, buffer, length) */
            p += snprintf(p, remaining, "fh=0x%lx,len=%lu",
                          (unsigned long)ev->args[0],
                          (unsigned long)ev->args[2]);
            return;

        case -90:   /* UnLock(lock) */
            /* lock_cache_lookup() is populated by the Lock() return
             * value formatter when Lock() succeeds.  If the
             * corresponding Lock() happened before tracing started,
             * the cache will miss and we fall through to hex display.
             * This is expected behavior, not a bug. */
            if (ev->args[0] == 0) {
                p += snprintf(p, remaining, "lock=NULL");
            } else {
                const char *path = lock_cache_lookup(ev->args[0]);
                if (path)
                    p += snprintf(p, remaining, "\"%s\"", path);
                else
                    p += snprintf(p, remaining, "lock=0x%lx",
                                  (unsigned long)ev->args[0]);
            }
            /* Remove lock from cache -- AmigaOS reuses BPTR addresses
             * after UnLock, so stale entries could cause incorrect
             * path attribution for new Lock calls. */
            lock_cache_remove(ev->args[0]);
            return;

        case -102:  /* Examine(lock, fib) */
            p += snprintf(p, remaining, "lock=0x%lx,fib=0x%lx",
                          (unsigned long)ev->args[0],
                          (unsigned long)ev->args[1]);
            return;

        case -108:  /* ExNext(lock, fib) */
            p += snprintf(p, remaining, "lock=0x%lx,fib=0x%lx",
                          (unsigned long)ev->args[0],
                          (unsigned long)ev->args[1]);
            return;

        case -66:   /* Seek(file, position, offset) */
        {
            const char *ofs_name = format_seek_offset((LONG)ev->args[2]);
            if (ofs_name)
                p += snprintf(p, remaining, "fh=0x%lx,pos=%ld,%s",
                              (unsigned long)ev->args[0],
                              (long)(LONG)ev->args[1],
                              ofs_name);
            else
                p += snprintf(p, remaining, "fh=0x%lx,pos=%ld,mode=%ld",
                              (unsigned long)ev->args[0],
                              (long)(LONG)ev->args[1],
                              (long)(LONG)ev->args[2]);
            return;
        }

        case -186:  /* SetProtection(name, protect) */
        {
            /* Format protection bits as rwed string.
             * AmigaOS dos/dos.h bit positions:
             *   FIBB_HOLD=7, FIBB_SCRIPT=6, FIBB_PURE=5, FIBB_ARCHIVE=4
             *   FIBB_READ=3, FIBB_WRITE=2, FIBB_EXECUTE=1, FIBB_DELETE=0
             * HSPA bits (7-4): 1 = flag active. RWED bits (3-0): 0 = allowed, 1 = denied. */
            ULONG prot = ev->args[1];
            char prot_buf[16];
            prot_buf[0] = (prot & (1<<7)) ? 'h' : '-';  /* FIBB_HOLD */
            prot_buf[1] = (prot & (1<<6)) ? 's' : '-';  /* FIBB_SCRIPT */
            prot_buf[2] = (prot & (1<<5)) ? 'p' : '-';  /* FIBB_PURE */
            prot_buf[3] = (prot & (1<<4)) ? 'a' : '-';  /* FIBB_ARCHIVE */
            prot_buf[4] = (prot & (1<<3)) ? '-' : 'r';  /* FIBB_READ (inverted) */
            prot_buf[5] = (prot & (1<<2)) ? '-' : 'w';  /* FIBB_WRITE (inverted) */
            prot_buf[6] = (prot & (1<<1)) ? '-' : 'e';  /* FIBB_EXECUTE (inverted) */
            prot_buf[7] = (prot & (1<<0)) ? '-' : 'd';  /* FIBB_DELETE (inverted) */
            prot_buf[8] = '\0';
            p += snprintf(p, remaining, "\"%s%s\",%s",
                          ev->string_data, trunc, prot_buf);
            return;
        }

        case -156:  /* UnLoadSeg(seglist) */
        {
            const char *prog = seg_cache_lookup(ev->args[0]);
            if (prog) {
                p += snprintf(p, remaining, "\"%s\"", prog);
                seg_cache_remove(ev->args[0]);
            } else {
                p += snprintf(p, remaining, "seg=0x%lx",
                              (unsigned long)ev->args[0]);
            }
            return;
        }

        }  /* end dos switch */
    }

    /* --- intuition.library --- */

    if (fe->lib_id == LIB_INTUITION) {
        switch (fe->lvo_offset) {

        case -204:  /* OpenWindow(newWindow) */
            if (ev->string_data[0])
                p += snprintf(p, remaining, "\"%s%s\"",
                              ev->string_data, trunc);
            else
                p += snprintf(p, remaining, "nw=0x%lx",
                              (unsigned long)ev->args[0]);
            return;

        case -72:   /* CloseWindow(window) */
            if (ev->string_data[0])
                p += snprintf(p, remaining, "\"%s%s\"",
                              ev->string_data, trunc);
            else
                p += snprintf(p, remaining, "win=0x%lx",
                              (unsigned long)ev->args[0]);
            return;

        case -198:  /* OpenScreen(newScreen) */
            if (ev->string_data[0])
                p += snprintf(p, remaining, "\"%s%s\"",
                              ev->string_data, trunc);
            else
                p += snprintf(p, remaining, "ns=0x%lx",
                              (unsigned long)ev->args[0]);
            return;

        case -66:   /* CloseScreen(screen) */
            if (ev->string_data[0])
                p += snprintf(p, remaining, "\"%s%s\"",
                              ev->string_data, trunc);
            else
                p += snprintf(p, remaining, "scr=0x%lx",
                              (unsigned long)ev->args[0]);
            return;

        case -450:  /* ActivateWindow(window) */
            if (ev->string_data[0])
                p += snprintf(p, remaining, "\"%s%s\"",
                              ev->string_data, trunc);
            else
                p += snprintf(p, remaining, "win=0x%lx",
                              (unsigned long)ev->args[0]);
            return;

        case -312:  /* WindowToFront(window) */
            if (ev->string_data[0])
                p += snprintf(p, remaining, "\"%s%s\"",
                              ev->string_data, trunc);
            else
                p += snprintf(p, remaining, "win=0x%lx",
                              (unsigned long)ev->args[0]);
            return;

        case -306:  /* WindowToBack(window) */
            if (ev->string_data[0])
                p += snprintf(p, remaining, "\"%s%s\"",
                              ev->string_data, trunc);
            else
                p += snprintf(p, remaining, "win=0x%lx",
                              (unsigned long)ev->args[0]);
            return;

        case -150:  /* ModifyIDCMP(window, flags) */
        {
            char idcmp_buf[384];
            format_idcmp_flags(ev->args[1], idcmp_buf, sizeof(idcmp_buf));
            p += snprintf(p, remaining, "win=0x%lx,%s",
                          (unsigned long)ev->args[0], idcmp_buf);
            return;
        }

        case -210:  /* OpenWorkBench() -- no args */
            return;

        case -78:   /* CloseWorkBench() -- no args */
            return;

        case -510:  /* LockPubScreen(name) */
            if (ev->args[0] == 0 || ev->string_data[0] == '\0') {
                p += snprintf(p, remaining, "NULL (default)");
            } else {
                p += snprintf(p, remaining, "\"%s%s\"",
                              ev->string_data, trunc);
            }
            return;

        case -606:  /* OpenWindowTagList(newWindow, tagList) */
            if (ev->string_data[0])
                p += snprintf(p, remaining, "\"%s%s\",tags=0x%lx",
                              ev->string_data, trunc,
                              (unsigned long)ev->args[1]);
            else
                p += snprintf(p, remaining, "nw=0x%lx,tags=0x%lx",
                              (unsigned long)ev->args[0],
                              (unsigned long)ev->args[1]);
            return;

        case -612:  /* OpenScreenTagList(newScreen, tagList) */
            if (ev->string_data[0])
                p += snprintf(p, remaining, "\"%s%s\",tags=0x%lx",
                              ev->string_data, trunc,
                              (unsigned long)ev->args[1]);
            else
                p += snprintf(p, remaining, "ns=0x%lx,tags=0x%lx",
                              (unsigned long)ev->args[0],
                              (unsigned long)ev->args[1]);
            return;

        case -516:  /* UnlockPubScreen(name, screen) */
            if (ev->string_data[0])
                p += snprintf(p, remaining, "\"%s%s\"",
                              ev->string_data, trunc);
            else
                p += snprintf(p, remaining, "screen=0x%lx",
                              (unsigned long)ev->args[1]);
            return;

        }  /* end intuition switch */
    }

    /* --- bsdsocket.library --- */

    if (fe->lib_id == LIB_BSDSOCKET) {
        switch (fe->lvo_offset) {

        case -30:   /* socket(domain, type, protocol) */
        {
            const char *af = format_af((LONG)ev->args[0]);
            const char *st = format_socktype((LONG)ev->args[1]);
            if (af && st)
                p += snprintf(p, remaining, "%s,%s,proto=%lu",
                              af, st, (unsigned long)ev->args[2]);
            else
                p += snprintf(p, remaining, "domain=%lu,type=%lu,proto=%lu",
                              (unsigned long)ev->args[0],
                              (unsigned long)ev->args[1],
                              (unsigned long)ev->args[2]);
            return;
        }

        case -36:   /* bind(fd, name, namelen) */
            p += snprintf(p, remaining, "fd=%ld,addr=0x%lx,len=%lu",
                          (long)(LONG)ev->args[0],
                          (unsigned long)ev->args[1],
                          (unsigned long)ev->args[2]);
            return;

        case -42:   /* listen(fd, backlog) */
            p += snprintf(p, remaining, "fd=%ld,backlog=%lu",
                          (long)(LONG)ev->args[0],
                          (unsigned long)ev->args[1]);
            return;

        case -48:   /* accept(fd, addr, addrlen) */
            p += snprintf(p, remaining, "fd=%ld,addr=0x%lx",
                          (long)(LONG)ev->args[0],
                          (unsigned long)ev->args[1]);
            return;

        case -54:   /* connect(fd, name, namelen) */
            p += snprintf(p, remaining, "fd=%ld,addr=0x%lx,len=%lu",
                          (long)(LONG)ev->args[0],
                          (unsigned long)ev->args[1],
                          (unsigned long)ev->args[2]);
            return;

        case -60:   /* sendto(fd, buf, len, flags) -- first 4 of 6 */
            p += snprintf(p, remaining, "fd=%ld,len=%lu,flags=0x%lx",
                          (long)(LONG)ev->args[0],
                          (unsigned long)ev->args[2],
                          (unsigned long)ev->args[3]);
            return;

        case -66:   /* send(fd, buf, len, flags) */
            p += snprintf(p, remaining, "fd=%ld,len=%lu,flags=0x%lx",
                          (long)(LONG)ev->args[0],
                          (unsigned long)ev->args[2],
                          (unsigned long)ev->args[3]);
            return;

        case -72:   /* recvfrom(fd, buf, len, flags) -- first 4 of 6 */
            p += snprintf(p, remaining, "fd=%ld,len=%lu,flags=0x%lx",
                          (long)(LONG)ev->args[0],
                          (unsigned long)ev->args[2],
                          (unsigned long)ev->args[3]);
            return;

        case -78:   /* recv(fd, buf, len, flags) */
            p += snprintf(p, remaining, "fd=%ld,len=%lu,flags=0x%lx",
                          (long)(LONG)ev->args[0],
                          (unsigned long)ev->args[2],
                          (unsigned long)ev->args[3]);
            return;

        case -84:   /* shutdown(fd, how) */
        {
            const char *how_name = format_shutdown_how((LONG)ev->args[1]);
            if (how_name)
                p += snprintf(p, remaining, "fd=%ld,%s",
                              (long)(LONG)ev->args[0], how_name);
            else
                p += snprintf(p, remaining, "fd=%ld,how=%ld",
                              (long)(LONG)ev->args[0],
                              (long)(LONG)ev->args[1]);
            return;
        }

        case -90:   /* setsockopt(fd, level, optname, optval) -- first 4 of 5 */
            p += snprintf(p, remaining, "fd=%ld,level=%ld,opt=%ld",
                          (long)(LONG)ev->args[0],
                          (long)(LONG)ev->args[1],
                          (long)(LONG)ev->args[2]);
            return;

        case -96:   /* getsockopt(fd, level, optname, optval) -- first 4 of 5 */
            p += snprintf(p, remaining, "fd=%ld,level=%ld,opt=%ld",
                          (long)(LONG)ev->args[0],
                          (long)(LONG)ev->args[1],
                          (long)(LONG)ev->args[2]);
            return;

        case -114:  /* IoctlSocket(fd, request, argp) */
            p += snprintf(p, remaining, "fd=%ld,req=0x%lx",
                          (long)(LONG)ev->args[0],
                          (unsigned long)ev->args[1]);
            return;

        case -120:  /* CloseSocket(fd) */
            p += snprintf(p, remaining, "fd=%ld",
                          (long)(LONG)ev->args[0]);
            return;

        case -126:  /* WaitSelect(nfds, sigmask, readfds, writefds) */
        {
            char sig_buf[128];
            format_signal_set(ev->args[1], sig_buf, sizeof(sig_buf));
            p += snprintf(p, remaining, "nfds=%lu,sigs=%s",
                          (unsigned long)ev->args[0], sig_buf);
            return;
        }

        }  /* end bsdsocket switch */
    }

    /* --- graphics.library --- */

    if (fe->lib_id == LIB_GRAPHICS) {
        switch (fe->lvo_offset) {

        case -72:   /* OpenFont(textAttr) */
            if (ev->string_data[0])
                p += snprintf(p, remaining, "\"%s%s\",%lu",
                              ev->string_data, trunc,
                              (unsigned long)ev->args[1]);
            else
                p += snprintf(p, remaining, "attr=0x%lx",
                              (unsigned long)ev->args[0]);
            return;

        case -78:  /* CloseFont(textFont) */
            p += snprintf(p, remaining, "font=0x%lx",
                          (unsigned long)ev->args[0]);
            return;

        }  /* end graphics switch */
    }

    /* --- icon.library --- */

    if (fe->lib_id == LIB_ICON) {
        switch (fe->lvo_offset) {

        case -78:  /* GetDiskObject(name) */
            p += snprintf(p, remaining, "\"%s%s\"",
                          ev->string_data, trunc);
            return;

        case -84:  /* PutDiskObject(name, diskObj) */
            p += snprintf(p, remaining, "\"%s%s\",obj=0x%lx",
                          ev->string_data, trunc,
                          (unsigned long)ev->args[1]);
            return;

        case -90:  /* FreeDiskObject(diskObj) */
        {
            const char *name = diskobj_cache_lookup(ev->args[0]);
            if (name) {
                p += snprintf(p, remaining, "\"%s\"", name);
                /* Remove from cache -- AmigaOS reuses DiskObject
                 * addresses after FreeDiskObject. */
                diskobj_cache_remove(ev->args[0]);
            } else {
                p += snprintf(p, remaining, "obj=0x%lx",
                              (unsigned long)ev->args[0]);
            }
            return;
        }

        case -96:  /* FindToolType(toolTypeArray, typeName) */
            p += snprintf(p, remaining, "\"%s%s\"",
                          ev->string_data, trunc);
            return;

        case -102:  /* MatchToolValue(typeString, value) */
            p += snprintf(p, remaining, "str=0x%lx,val=0x%lx",
                          (unsigned long)ev->args[0],
                          (unsigned long)ev->args[1]);
            return;
        }
    }

    /* --- workbench.library --- */

    if (fe->lib_id == LIB_WORKBENCH) {
        switch (fe->lvo_offset) {

        case -60:  /* AddAppIconA(id, userdata, text, msgport) */
            if (ev->string_data[0])
                p += snprintf(p, remaining, "id=%lu,\"%s%s\"",
                              (unsigned long)ev->args[0],
                              ev->string_data, trunc);
            else
                p += snprintf(p, remaining, "id=%lu,text=0x%lx",
                              (unsigned long)ev->args[0],
                              (unsigned long)ev->args[2]);
            return;

        case -66:  /* RemoveAppIcon(appIcon) */
            p += snprintf(p, remaining, "icon=0x%lx",
                          (unsigned long)ev->args[0]);
            return;

        case -48:  /* AddAppWindowA(id, userdata, window, msgport) */
            p += snprintf(p, remaining, "id=%lu,win=0x%lx",
                          (unsigned long)ev->args[0],
                          (unsigned long)ev->args[2]);
            return;

        case -54:  /* RemoveAppWindow(appWindow) */
            p += snprintf(p, remaining, "win=0x%lx",
                          (unsigned long)ev->args[0]);
            return;

        case -72:  /* AddAppMenuItemA(id, userdata, text, msgport) */
            if (ev->string_data[0])
                p += snprintf(p, remaining, "id=%lu,\"%s%s\"",
                              (unsigned long)ev->args[0],
                              ev->string_data, trunc);
            else
                p += snprintf(p, remaining, "id=%lu,text=0x%lx",
                              (unsigned long)ev->args[0],
                              (unsigned long)ev->args[2]);
            return;

        case -78:  /* RemoveAppMenuItem(appMenuItem) */
            p += snprintf(p, remaining, "item=0x%lx",
                          (unsigned long)ev->args[0]);
            return;
        }
    }

    /* Fallback: should not reach here for known functions,
     * but handle gracefully */
    if (fe->has_string && ev->string_data[0] != '\0') {
        { int n = snprintf(p, remaining, "\"%s%s\"", ev->string_data, trunc);
          if (n >= remaining) n = remaining - 1;
          if (n > 0) { p += n; remaining -= n; }
        }
        for (i = 1; i < ev->arg_count && i < 4; i++) {
            int n;
            if (remaining <= 1) break;
            n = snprintf(p, remaining, ",%lu",
                          (unsigned long)ev->args[i]);
            if (n >= remaining) n = remaining - 1;
            if (n > 0) { p += n; remaining -= n; }
        }
    } else {
        for (i = 0; i < ev->arg_count && i < 4; i++) {
            int n;
            if (i > 0) {
                if (remaining <= 1) break;
                *p++ = ','; remaining--;
            }
            if (remaining <= 1) break;
            n = snprintf(p, remaining, "0x%lx",
                          (unsigned long)ev->args[i]);
            if (n >= remaining) n = remaining - 1;
            if (n > 0) { p += n; remaining -= n; }
        }
    }
}

/* Format return value with per-function semantics.
 * Writes the formatted retval string to buf, and returns a status
 * character ('O', 'E', or '-') for the wire protocol.
 *
 * Uses single-return pattern so the IoErr append epilogue
 * executes for all code paths. */
static char format_retval(struct atrace_event *ev,
                           const struct trace_func_entry *fe,
                           char *buf, int bufsz)
{
    ULONG rv = ev->retval;
    LONG srv = (LONG)rv;  /* signed interpretation */
    char status = '-';

    if (!fe) {
        /* Unknown function */
        if (rv == 0)
            snprintf(buf, bufsz, "NULL");
        else
            snprintf(buf, bufsz, "0x%08lx", (unsigned long)rv);
        return '-';
    }

    switch (fe->result_type) {
    case RET_VOID:
        snprintf(buf, bufsz, "(void)");
        status = '-';
        break;

    case RET_PTR:
        /* Pointer: NULL=fail, non-zero=hex addr */
        if (rv == 0) {
            snprintf(buf, bufsz, "NULL");
            status = 'E';
        } else {
            snprintf(buf, bufsz, "0x%08lx", (unsigned long)rv);
            status = 'O';
        }
        break;

    case RET_BOOL_DOS:
        /* DOS boolean: non-zero=success, 0=fail.
         * DOSTRUE is defined as -1, but some DOS functions return
         * other non-zero values for success (e.g. Execute). Apps
         * check with if(result), not if(result == DOSTRUE). */
        if (rv == 0) {
            snprintf(buf, bufsz, "FAIL");
            status = 'E';
        } else {
            snprintf(buf, bufsz, "OK");
            status = 'O';
        }
        break;

    case RET_NZERO_ERR:
        /* 0=success, non-zero=error code (OpenDevice) */
        if (rv == 0) {
            snprintf(buf, bufsz, "OK");
            status = 'O';
        } else {
            snprintf(buf, bufsz, "err=%ld", (long)srv);
            status = 'E';
        }
        break;

    case RET_MSG_PTR:
        /* Message pointer: NULL=empty (normal), non-zero=addr */
        if (rv == 0) {
            snprintf(buf, bufsz, "(empty)");
            status = '-';
        } else {
            snprintf(buf, bufsz, "0x%08lx", (unsigned long)rv);
            status = 'O';
        }
        break;

    case RET_RC:
        /* Return code: signed decimal, 0=success */
        snprintf(buf, bufsz, "rc=%ld", (long)srv);
        status = (srv == 0) ? 'O' : 'E';
        break;

    case RET_LOCK:
        /* BPTR lock: NULL=fail, non-zero=hex addr */
        if (rv == 0) {
            snprintf(buf, bufsz, "NULL");
            status = 'E';
        } else {
            snprintf(buf, bufsz, "0x%08lx", (unsigned long)rv);
            status = 'O';
        }
        break;

    case RET_LEN:
        /* Byte count: -1=fail, >=0=decimal count */
        if (srv == -1) {
            snprintf(buf, bufsz, "-1");
            status = 'E';
        } else {
            snprintf(buf, bufsz, "%ld", (long)srv);
            status = 'O';
        }
        break;

    case RET_PTR_OPAQUE:
        /* Opaque pointer: non-zero means success, show OK not hex */
        if (rv == 0) {
            snprintf(buf, bufsz, "NULL");
            status = 'E';
        } else {
            snprintf(buf, bufsz, "OK");
            status = 'O';
        }
        break;

    case RET_EXECUTE:
        /* Execute(): return value is ambiguous between shell rc and DOS boolean.
         * Show raw value without OK/FAIL interpretation. rc=0 means the shell
         * completed (success in shell convention). Non-zero is the shell rc. */
        if (rv == 0)
            snprintf(buf, bufsz, "rc=0");
        else if (rv == (ULONG)-1)
            snprintf(buf, bufsz, "OK");      /* DOSTRUE = genuine success */
        else
            snprintf(buf, bufsz, "rc=%ld", (long)srv);
        status = '-';   /* Neutral status -- never classified as error */
        break;

    case RET_IO_LEN:
        /* I/O byte count: -1=error, 0=EOF(Read), >0=bytes transferred */
        if (srv == -1) {
            snprintf(buf, bufsz, "-1");
            status = 'E';
        } else {
            snprintf(buf, bufsz, "%ld", (long)srv);
            status = 'O';
        }
        break;

    case RET_OLD_LOCK:
        /* Old lock from CurrentDir: informational */
        if (rv == 0) {
            snprintf(buf, bufsz, "(none)");
        } else {
            const char *path = lock_cache_lookup(rv);
            if (path)
                snprintf(buf, bufsz, "\"%s\"", path);
            else
                snprintf(buf, bufsz, "0x%08lx", (unsigned long)rv);
        }
        status = '-';
        break;

    default:
        /* Fallback */
        if (rv == 0)
            snprintf(buf, bufsz, "NULL");
        else
            snprintf(buf, bufsz, "0x%08lx", (unsigned long)rv);
        status = '-';
        break;
    }

    /* Append IoErr info for dos.library failures.
     * The status == 'E' check is essential -- the stub captures IoErr
     * whenever retval==0, which includes valid returns (CurrentDir,
     * GetVar, RunCommand, etc.). The daemon suppresses display for
     * those via the status check.
     *
     * The ev->valid == 1 check handles the pre-call valid race: stubs
     * set valid=2 before calling the original function (so blocking
     * functions don't freeze the consumer).  If the daemon polls while
     * the function is executing, it sees valid=2 and retval/ioerr are
     * stale (zero from MEMF_CLEAR).  The post-call handler sets
     * valid=1 after writing retval, ioerr, and FLAG_HAS_IOERR.
     * Checking valid==1 ensures we only append IoErr text for events
     * whose post-call handler has completed. */
    if (g_anchor && g_anchor->version >= 4 &&
        status == 'E' && ev->valid == 1 &&
        (ev->flags & FLAG_HAS_IOERR) && ev->ioerr != 0 &&
        fe->lib_id == LIB_DOS) {
        int cur_len = (int)strlen(buf);
        int ioerr_code = (int)ev->ioerr;
        const char *err_name = dos_error_name(ioerr_code);
        if (err_name) {
            snprintf(buf + cur_len, bufsz - cur_len,
                     " (%d, %s)", ioerr_code, err_name);
        } else {
            snprintf(buf + cur_len, bufsz - cur_len,
                     " (err %d)", ioerr_code);
        }
    }

    return status;
}

/* ---- Event formatting ---- */

/* Format a single trace event as a text line.
 *
 * ---- EClock timestamp support ---- */

/* Capture the EClock epoch at trace session start.
 * Must be called with g_anchor valid and version >= 3.
 * Captures both wall-clock (DateStamp) and EClock values. */
static void eclock_capture_epoch(void)
{
    struct DateStamp ds;
    LONG ds_secs;

    if (!g_anchor || g_anchor->version < 3 ||
        g_anchor->eclock_freq == 0) {
        g_eclock_valid = 0;
        return;
    }

    g_eclock_freq = g_anchor->eclock_freq;

    /* Capture wall-clock time */
    DateStamp(&ds);
    ds_secs = ds.ds_Minute * 60 + ds.ds_Tick / TICKS_PER_SECOND;
    g_start_secs = ds_secs;
    g_start_us = (ds.ds_Tick % TICKS_PER_SECOND) * (1000000 / TICKS_PER_SECOND);

    /* The daemon cannot call ReadEClock() (no timer.device open), so
     * the EClock baseline is captured lazily from the first event with
     * FLAG_HAS_ECLOCK.  Setting start_eclock_lo/hi to 0 signals
     * eclock_format_time() to grab the baseline from that first event.
     * Elapsed ticks from the baseline are then added to the wall-clock
     * epoch captured above. */
    g_start_eclock_lo = 0;
    g_start_eclock_hi = 0;
    g_eclock_baseline_set = 0;

    /* Precompute hi-unit conversion constants */
    g_secs_per_hi = 0xFFFFFFFFUL / g_eclock_freq;
    g_rem_per_hi = 0xFFFFFFFFUL % g_eclock_freq;

    g_eclock_valid = 1;
}

/* Format an EClock timestamp as HH:MM:SS.uuuuuu wall-clock time.
 * Uses the session epoch (g_start_*) and the event's eclock_lo/hi.
 *
 * On the first event with FLAG_HAS_ECLOCK after session start, captures
 * the baseline EClock value.  Subsequent events compute elapsed ticks
 * relative to this baseline and add them to the wall-clock epoch.
 *
 * Returns 1 if EClock timestamp was formatted, 0 if caller should
 * fall back to DateStamp. */
static int eclock_format_time(struct atrace_event *ev,
                               char *buf, int bufsz)
{
    ULONG elapsed_lo;
    WORD  elapsed_hi;
    ULONG elapsed_secs;
    ULONG remainder;
    ULONG elapsed_us;
    LONG  total_secs;
    LONG  total_us;
    LONG  hours, mins, secs;

    if (!g_eclock_valid || g_eclock_freq == 0)
        return 0;

    if (!(ev->flags & FLAG_HAS_ECLOCK))
        return 0;

    /* Capture baseline from first EClock event */
    if (!g_eclock_baseline_set) {
        g_start_eclock_lo = ev->eclock_lo;
        g_start_eclock_hi = (WORD)ev->eclock_hi;
        g_eclock_baseline_set = 1;
    }

    /* 48-bit subtraction: elapsed = event - start */
    elapsed_lo = ev->eclock_lo - g_start_eclock_lo;
    elapsed_hi = (WORD)ev->eclock_hi - g_start_eclock_hi;
    if (ev->eclock_lo < g_start_eclock_lo)
        elapsed_hi--;  /* borrow from lo */

    /* Convert elapsed ticks to seconds + remainder.
     * elapsed = elapsed_hi * 2^32 + elapsed_lo ticks.
     * For hi contribution: each hi unit = (2^32 / freq) seconds. */
    elapsed_secs = elapsed_lo / g_eclock_freq;
    remainder = elapsed_lo % g_eclock_freq;

    if (elapsed_hi > 0) {
        ULONG hi_secs = (ULONG)elapsed_hi * g_secs_per_hi;
        ULONG hi_rem  = (ULONG)elapsed_hi * g_rem_per_hi;
        elapsed_secs += hi_secs;
        remainder += hi_rem;
    }

    /* Handle carry from remainder overflow */
    if (remainder >= g_eclock_freq) {
        elapsed_secs += remainder / g_eclock_freq;
        remainder = remainder % g_eclock_freq;
    }

    /* Convert remainder to microseconds.
     * remainder * 1000000 could overflow 32 bits, so split:
     * elapsed_us = (remainder * 1000) / (freq / 1000)
     * For freq ~710000: freq/1000 = 710, remainder < 710000.
     * remainder * 1000 < 710000000, fits in 32 bits. */
    elapsed_us = (remainder * 1000) / (g_eclock_freq / 1000);

    /* Add elapsed to wall-clock epoch */
    total_secs = g_start_secs + (LONG)elapsed_secs;
    total_us = g_start_us + (LONG)elapsed_us;
    if (total_us >= 1000000) {
        total_secs++;
        total_us -= 1000000;
    }

    hours = total_secs / 3600;
    mins = (total_secs % 3600) / 60;
    secs = total_secs % 60;

    snprintf(buf, bufsz, "%02ld:%02ld:%02ld.%06ld",
             (long)hours, (long)mins, (long)secs, (long)total_us);
    return 1;
}

/* ---- Event formatting ----
 *
 * Format (7 tab-separated fields):
 *   <seq>\t<time>\t<lib>.<func>\t<task>\t<args>\t<retval>\t<status>
 *
 * The status field is a single character from format_retval():
 *   'O' = OK (success), 'E' = Error, '-' = Neutral/void.
 *
 * For v3 events with FLAG_HAS_ECLOCK, the time field uses per-event
 * EClock timestamps with microsecond precision (HH:MM:SS.uuuuuu).
 * For v2 events or when EClock is unavailable, falls back to per-batch
 * DateStamp timestamps (HH:MM:SS.mmm). */
static void trace_format_event(struct atrace_event *ev,
                                const char *timestr,
                                const char *resolved_name,
                                char *buf, int bufsz)
{
    const struct trace_func_entry *fe;
    const char *task_name;
    const char *lib_name;
    const char *func_name;
    static char args_buf[384];
    static char retval_buf[80];
    static char eclock_timestr[20];
    char status;
    const char *effective_timestr;

    /* Determine timestamp: per-event EClock or batch DateStamp */
    if (eclock_format_time(ev, eclock_timestr, sizeof(eclock_timestr)))
        effective_timestr = eclock_timestr;
    else
        effective_timestr = timestr;

    /* Use caller-provided resolved name.  If the caller got a generic
     * "<task 0x...>" string and the event has an embedded task_name
     * (v3 events), prefer the embedded name (the process may have
     * exited since the task cache was last refreshed). */
    if (resolved_name[0] == '<' &&
        g_anchor && g_anchor->version >= 3 && ev->task_name[0] != '\0') {
        task_name = ev->task_name;
    } else {
        task_name = resolved_name;
    }

    fe = lookup_func(ev->lib_id, ev->lvo_offset);
    if (fe) {
        lib_name = fe->lib_name;
        func_name = fe->func_name;
    } else {
        lib_name = "?";
        func_name = "?";
    }

    /* Format arguments */
    format_args(ev, fe, args_buf, sizeof(args_buf));

    /* Format return value and get status classification */
    status = format_retval(ev, fe, retval_buf, sizeof(retval_buf));

    /* Populate lock-to-path cache for Lock and CreateDir.
     * Both return BPTR to FileLock. Open() is excluded because it
     * returns BPTR to FileHandle (different type, not valid for
     * CurrentDir). */
    if (fe && ev->retval != 0 && fe->has_string &&
        ev->string_data[0] != '\0') {
        if ((fe->lib_id == LIB_DOS && fe->lvo_offset == -84) ||    /* Lock */
            (fe->lib_id == LIB_DOS && fe->lvo_offset == -120)) {   /* CreateDir */
            lock_cache_add(ev->retval, ev->string_data);
        }
        /* fh_cache population for Open */
        if (fe->lib_id == LIB_DOS && fe->lvo_offset == -30) {      /* Open */
            fh_cache_add(ev->retval, ev->string_data);
        }
        /* diskobj_cache population for GetDiskObject */
        if (fe->lib_id == LIB_ICON && fe->lvo_offset == -78) {     /* GetDiskObject */
            diskobj_cache_add(ev->retval, ev->string_data);
        }
        /* seg_cache population for LoadSeg and NewLoadSeg */
        if (fe->lib_id == LIB_DOS &&
            (fe->lvo_offset == -150 || fe->lvo_offset == -768)) {
            seg_cache_add(ev->retval, ev->string_data);
        }
    }

    snprintf(buf, bufsz, "%lu\t%s\t%s.%s\t%s\t%s\t%s\t%c",
             (unsigned long)ev->sequence,
             effective_timestr,
             lib_name, func_name,
             task_name,
             args_buf,
             retval_buf,
             status);
}

/* ---- Send helper ---- */

static int send_trace_data_chunk(LONG fd, const char *line)
{
    return send_data_chunk(fd, line, strlen(line));
}

/* ---- bsdsocket.library per-opener patching ---- */

/* Patch a newly opened bsdsocket.library base with all bsdsocket stubs.
 *
 * bsdsocket.library uses per-opener bases -- each OpenLibrary returns a
 * unique base with its own jump table.  The atrace loader only patches the
 * single base it opens during installation.  This function patches additional
 * bases discovered at runtime via OpenLibrary event detection.
 *
 * All bsdsocket patches are installed regardless of the per-function enabled
 * flag.  Disabled stubs are transparent pass-throughs with negligible
 * overhead, and the user may enable them later via tier switching. */
static void patch_bsdsocket_base(struct daemon_state *d, APTR base)
{
    int i, j, count, scan_limit;
    struct atrace_patch *p;
    APTR old;
    char msg[80];
    struct client *c;

    if (!g_anchor || !g_anchor->patches || !base)
        return;

    /* Check if already tracked */
    scan_limit = g_patched_bsd_count < MAX_BSD_BASES
                 ? g_patched_bsd_count : MAX_BSD_BASES;
    for (i = 0; i < scan_limit; i++) {
        if (g_patched_bsd_bases[i] == base)
            return;
    }

    /* Patch all bsdsocket LVOs */
    count = 0;
    for (i = 0; i < g_anchor->patch_count; i++) {
        p = &g_anchor->patches[i];

        if (p->lib_id != LIB_BSDSOCKET)
            continue;
        if (!p->stub_code)
            continue;

        Disable();
        old = SetFunction((struct Library *)base, p->lvo_offset,
                          (APTR)((ULONG)p->stub_code));
        Enable();

        if (old == p->stub_code) {
            /* Already patched (stubs persist across trace sessions) */
            count++;
            continue;
        }

        if (old != p->original) {
            /* Unexpected original -- log warning but proceed.
             * The stub's embedded ORIG_ADDR is the shared implementation
             * address, which is correct for all per-opener bases. */
            snprintf(msg, sizeof(msg),
                     "# WARNING: bsdsocket LVO %d old=%lx expected=%lx",
                     (int)p->lvo_offset,
                     (unsigned long)old,
                     (unsigned long)p->original);
            for (j = 0; j < MAX_CLIENTS; j++) {
                c = &d->clients[j];
                if (c->fd >= 0 && c->trace.active)
                    sendbuf_append_data_chunk(&c->trace.sendbuf, msg);
            }
            printf("%s\n", msg);
            fflush(stdout);
        }

        count++;
    }

    /* Defensive cache flush after all SetFunction calls */
    CacheClearU();

    /* Track the patched base (circular overwrite when full) */
    g_patched_bsd_bases[g_patched_bsd_count % MAX_BSD_BASES] = base;
    g_patched_bsd_count++;

    /* Diagnostic: send trace comment to all active subscribers */
    snprintf(msg, sizeof(msg),
             "# Patched bsdsocket base 0x%08lx (%d LVOs)",
             (unsigned long)base, count);
    for (i = 0; i < MAX_CLIENTS; i++) {
        c = &d->clients[i];
        if (c->fd >= 0 && c->trace.active)
            sendbuf_append_data_chunk(&c->trace.sendbuf, msg);
    }

    /* Also log to daemon console */
    printf("Patched bsdsocket base 0x%08lx (%d LVOs)\n",
           (unsigned long)base, count);
    fflush(stdout);
}

/* ---- Non-blocking send buffer helpers ---- */

/* Initialize a send buffer (called when trace session starts). */
static void sendbuf_init(struct trace_sendbuf *sb)
{
    sb->len = 0;
    sb->events_dropped = 0;
}

/* Append a formatted DATA chunk ("DATA <len>\n<payload>") to the buffer.
 * Handles drop notification: if events_dropped > 0 and there is room,
 * injects a "# DROPPED N events" comment first, then resets the counter.
 * Returns 0 on success, -1 if buffer full (event should be dropped). */
static int sendbuf_append_data_chunk(struct trace_sendbuf *sb,
                                      const char *line)
{
    char hdr[20];
    int hdr_len;
    int line_len;
    int total;

    /* Drop notification: inject before the new event */
    if (sb->events_dropped > 0) {
        char drop_msg[64];
        char drop_hdr[20];
        int drop_msg_len;
        int drop_hdr_len;
        int drop_total;

        drop_msg_len = snprintf(drop_msg, sizeof(drop_msg),
                                "# DROPPED %lu events",
                                (unsigned long)sb->events_dropped);
        drop_hdr_len = sprintf(drop_hdr, "DATA %d\n", drop_msg_len);
        drop_total = drop_hdr_len + drop_msg_len;

        /* Only inject if there is room; otherwise defer to next append */
        if (sb->len + drop_total <= TRACE_SENDBUF_SIZE) {
            memcpy(sb->buf + sb->len, drop_hdr, drop_hdr_len);
            sb->len += drop_hdr_len;
            memcpy(sb->buf + sb->len, drop_msg, drop_msg_len);
            sb->len += drop_msg_len;
            sb->events_dropped = 0;
        }
    }

    line_len = strlen(line);
    hdr_len = sprintf(hdr, "DATA %d\n", line_len);
    total = hdr_len + line_len;

    if (sb->len + total > TRACE_SENDBUF_SIZE)
        return -1;

    memcpy(sb->buf + sb->len, hdr, hdr_len);
    sb->len += hdr_len;
    memcpy(sb->buf + sb->len, line, line_len);
    sb->len += line_len;
    return 0;
}

/* Attempt to drain the buffer via non-blocking send().
 * Returns:  0 = buffer empty (fully drained)
 *           1 = partial drain (data remains, EWOULDBLOCK)
 *          -1 = send error (client should be disconnected) */
static int sendbuf_drain(struct trace_sendbuf *sb, LONG fd)
{
    LONG n;

    if (sb->len == 0)
        return 0;

    n = send(fd, (STRPTR)sb->buf, sb->len, 0);
    if (n > 0) {
        if (n >= sb->len) {
            sb->len = 0;
            return 0;
        }
        /* Partial send: shift remaining data */
        memmove(sb->buf, sb->buf + n, sb->len - n);
        sb->len -= (int)n;
        return 1;
    }

    /* n <= 0: check for EWOULDBLOCK */
    if (n < 0 && net_get_errno() == EWOULDBLOCK)
        return 1;

    /* Real error or connection closed */
    return -1;
}

/* Public wrapper: drain buffered trace data for a client.
 * Returns 0 if buffer empty, 1 if data remains, -1 on error. */
int trace_drain_client(struct daemon_state *d, int idx)
{
    struct client *c = &d->clients[idx];
    if (c->fd < 0 || !c->trace.active)
        return 0;
    return sendbuf_drain(&c->trace.sendbuf, c->fd);
}

/* Returns 1 if any trace client has buffered send data. */
int trace_any_buffered(struct daemon_state *d)
{
    int i;
    for (i = 0; i < MAX_CLIENTS; i++) {
        if (d->clients[i].fd >= 0 &&
            d->clients[i].trace.active &&
            d->clients[i].trace.sendbuf.len > 0)
            return 1;
    }
    return 0;
}

/* ---- Ring buffer polling ---- */

void trace_poll_events(struct daemon_state *d)
{
    struct atrace_ringbuf *ring;
    struct atrace_event *ev;
    struct client *c;
    ULONG pos;
    ULONG ov;
    int batch;
    int total_consumed;
    int sent_any;
    int i;
    struct DateStamp ds;
    static char timestr[16];
    LONG hours, mins, secs, ticks_rem;
    const char *task_name;

    g_poll_count++;

    if (!g_anchor || !g_anchor->ring)
        return;

    /* Check if atrace is shutting down.
     * Try a shared obtain -- if it fails AND global_enable is 0,
     * atrace QUIT is in progress. */
    if (!AttemptSemaphoreShared(&g_anchor->sem)) {
        if (g_anchor->global_enable == 0) {
            /* Shutdown detected -- stop all trace sessions */
            for (i = 0; i < MAX_CLIENTS; i++) {
                struct timeval sndtv;
                c = &d->clients[i];
                if (c->fd < 0 || !c->trace.active)
                    continue;

                /* Set 2-second send timeout before blocking flush */
                sndtv.tv_secs = 2;
                sndtv.tv_micro = 0;
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wincompatible-pointer-types"
                setsockopt(c->fd, SOL_SOCKET, SO_SNDTIMEO,
                           &sndtv, sizeof(sndtv));
#pragma GCC diagnostic pop
                net_set_blocking(c->fd);

                /* Flush remaining buffered data */
                if (c->trace.sendbuf.len > 0) {
                    if (send_all(c->fd, c->trace.sendbuf.buf,
                                 c->trace.sendbuf.len) < 0) {
                        trace_run_cleanup(c);
                        continue;
                    }
                    c->trace.sendbuf.len = 0;
                }
                send_trace_data_chunk(c->fd, "# ATRACE SHUTDOWN");
                send_end(c->fd);
                send_sentinel(c->fd);

                /* Clear SO_SNDTIMEO (set to 0 for normal blocking) */
                sndtv.tv_secs = 0;
                sndtv.tv_micro = 0;
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wincompatible-pointer-types"
                setsockopt(c->fd, SOL_SOCKET, SO_SNDTIMEO,
                           &sndtv, sizeof(sndtv));
#pragma GCC diagnostic pop

                /* Clear filter_task before clearing state.
                 * g_anchor is still valid at this point. */
                trace_run_cleanup(c);
            }
            g_anchor = NULL;
            g_ring_entries = NULL;
            g_events_dropped = 0;
            g_self_filtered = 0;
            g_current_tier = 1;
            return;
        }
        /* Semaphore busy but tracing still enabled -- skip this cycle */
        return;
    }

    /* Semaphore obtained (shared) -- safe to read ring buffer */
    ring = g_anchor->ring;

    if (!ring) {
        ReleaseSemaphore(&g_anchor->sem);
        /* Ring freed -- atrace was QUIT'd */
        if (g_anchor->global_enable == 0) {
            for (i = 0; i < MAX_CLIENTS; i++) {
                struct timeval sndtv;
                c = &d->clients[i];
                if (c->fd < 0 || !c->trace.active)
                    continue;

                /* Set 2-second send timeout before blocking flush */
                sndtv.tv_secs = 2;
                sndtv.tv_micro = 0;
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wincompatible-pointer-types"
                setsockopt(c->fd, SOL_SOCKET, SO_SNDTIMEO,
                           &sndtv, sizeof(sndtv));
#pragma GCC diagnostic pop
                net_set_blocking(c->fd);

                /* Flush remaining buffered data */
                if (c->trace.sendbuf.len > 0) {
                    if (send_all(c->fd, c->trace.sendbuf.buf,
                                 c->trace.sendbuf.len) < 0) {
                        trace_run_cleanup(c);
                        continue;
                    }
                    c->trace.sendbuf.len = 0;
                }
                send_trace_data_chunk(c->fd, "# ATRACE SHUTDOWN");
                send_end(c->fd);
                send_sentinel(c->fd);

                /* Clear SO_SNDTIMEO (set to 0 for normal blocking) */
                sndtv.tv_secs = 0;
                sndtv.tv_micro = 0;
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wincompatible-pointer-types"
                setsockopt(c->fd, SOL_SOCKET, SO_SNDTIMEO,
                           &sndtv, sizeof(sndtv));
#pragma GCC diagnostic pop

                /* Clear filter_task before clearing state.
                 * g_anchor is still valid at this point. */
                trace_run_cleanup(c);
            }
            g_anchor = NULL;
            g_ring_entries = NULL;
            g_events_dropped = 0;
            g_self_filtered = 0;
            g_current_tier = 1;
        }
        return;
    }

    /* Get current time for this batch */
    DateStamp(&ds);
    hours = ds.ds_Minute / 60;
    mins = ds.ds_Minute % 60;
    secs = ds.ds_Tick / 50;
    ticks_rem = ds.ds_Tick % 50;
    sprintf(timestr, "%02ld:%02ld:%02ld.%03ld",
            (long)hours, (long)mins, (long)secs,
            (long)(ticks_rem * 20));  /* 1 tick = 20ms */

    pos = ring->read_pos;

    if (pos >= ring->capacity) {
        /* Corrupted read position -- reset to write position */
        ring->read_pos = ring->write_pos;
        ReleaseSemaphore(&g_anchor->sem);
        return;
    }
    batch = 0;
    total_consumed = 0;

    while (batch < 512 && g_ring_entries[pos].valid) {
        const char *filter_name;
        ev = &g_ring_entries[pos];

        /* Wait for in-progress events (valid=2) to complete (valid=1).
         * Stubs set valid=2 pre-call so the consumer can advance past
         * blocking functions.  But for non-blocking functions, the
         * post-call handler sets valid=1 within microseconds.  Waiting
         * one poll cycle (100ms) lets those functions finish so we get
         * complete data (retval, ioerr, FLAG_HAS_IOERR).
         *
         * For truly blocking functions (RunCommand, Execute), valid=2
         * persists for seconds.  After INFLIGHT_PATIENCE consecutive
         * polls at the same position, we consume the event as-is to
         * prevent ring buffer stalls.  The IoErr guard (ev->valid==1)
         * in format_retval suppresses stale IoErr data. */
        if (ev->valid == 2) {
            if (pos == g_inflight_stall_pos) {
                g_inflight_stall_count++;
                if (g_inflight_stall_count < INFLIGHT_PATIENCE) {
                    break;  /* Wait for post-call handler */
                }
                /* Patience expired -- consume as-is */
            } else {
                g_inflight_stall_pos = pos;
                g_inflight_stall_count = 1;
                break;  /* First time seeing this slot as in-progress */
            }
        }

        /* Skip amigactld's own library calls to prevent noise and feedback.
         * All daemon events are filtered regardless of library -- EXEC paths
         * create separate processes with distinct task pointers, so user
         * commands are never suppressed.
         * Uses g_daemon_task from exec.c (initialized at daemon startup). */
        if (ev->caller_task == (APTR)g_daemon_task) {
            ev->valid = 0;
            if (pos == g_inflight_stall_pos) {
                g_inflight_stall_pos = 0xFFFFFFFF;
                g_inflight_stall_count = 0;
            }
            pos = (pos + 1) % ring->capacity;
            ring->read_pos = pos;
            total_consumed++;
            g_self_filtered++;
            continue;
        }

        /* bsdsocket.library per-opener dynamic patching.
         * Detect successful OpenLibrary("bsdsocket.library") events
         * and patch the newly returned base with all bsdsocket stubs.
         * Must run BEFORE v0 suppression -- patching is a side-effect
         * that must fire regardless of whether the event is displayed. */
        if (ev->caller_task != (APTR)g_daemon_task &&
            ev->lib_id == LIB_EXEC &&
            ev->lvo_offset == -552 &&            /* OpenLibrary */
            ev->retval != 0 &&                   /* success */
            stricmp(ev->string_data, "bsdsocket.library") == 0) {
            patch_bsdsocket_base(d, (APTR)ev->retval);
        }

        /* OpenLibrary v0 success suppression at Basic tier.
         * At Basic tier, successful OpenLibrary calls with version==0
         * are pure noise from AmigaOS internal library management
         * (CLI startup, library interdependencies). Failed opens of
         * any version are always shown (diagnostic value). At Detail
         * tier and above, all OpenLibrary events pass through. */
        if (g_current_tier == 1 &&
            ev->lib_id == LIB_EXEC &&
            ev->lvo_offset == -552 &&       /* OpenLibrary */
            ev->retval != 0 &&              /* success */
            ev->args[1] == 0) {             /* version 0 */
            ev->valid = 0;
            if (pos == g_inflight_stall_pos) {
                g_inflight_stall_pos = 0xFFFFFFFF;
                g_inflight_stall_count = 0;
            }
            pos = (pos + 1) % ring->capacity;
            ring->read_pos = pos;
            total_consumed++;
            g_self_filtered++;  /* Reuse counter for all daemon-side suppression */
            continue;
        }

        /* dos.Lock success suppression at Basic tier.
         * At Basic tier, successful Lock calls (retval != 0) are
         * high-volume noise from path resolution during normal
         * operation. Failed Locks (retval == 0) always pass through
         * for diagnostic value. At Detail tier and above, all Lock
         * events are shown. */
        if (g_current_tier == 1 &&
            ev->lib_id == LIB_DOS &&
            ev->lvo_offset == -84 &&        /* Lock */
            ev->retval != 0) {              /* success */
            ev->valid = 0;
            if (pos == g_inflight_stall_pos) {
                g_inflight_stall_pos = 0xFFFFFFFF;
                g_inflight_stall_count = 0;
            }
            pos = (pos + 1) % ring->capacity;
            ring->read_pos = pos;
            total_consumed++;
            g_self_filtered++;
            /* Cache lock-to-path even when suppressed */
            if (ev->string_data[0] != '\0') {
                lock_cache_add(ev->retval, ev->string_data);
            }
            continue;
        }

        /* Resolve task name for formatting and PROC filter matching.
         * resolve_task_name() uses resolve_cli_name() to extract the
         * CLI command basename (e.g. "atrace_test") for CLI processes,
         * which is what PROC= filters need to match against.
         *
         * The embedded ev->task_name (from tc_Node.ln_Name captured by
         * the stub) contains the raw process name (e.g. "Background CLI")
         * which is NOT the command name.  Only use it as fallback when
         * resolve_task_name() returns a generic "<task 0x...>" string
         * (process already exited and not in cache). */
        task_name = resolve_task_name(ev->caller_task);
        if (task_name[0] == '<' &&
            g_anchor->version >= 3 && ev->task_name[0] != '\0') {
            filter_name = ev->task_name;
        } else {
            filter_name = task_name;
        }

        /* Format event */
        trace_format_event(ev, timestr, task_name, trace_line_buf,
                           sizeof(trace_line_buf));

        /* Broadcast to all tracing clients (with per-client filtering) */
        sent_any = 0;
        for (i = 0; i < MAX_CLIENTS; i++) {
            c = &d->clients[i];
            if (c->fd < 0 || !c->trace.active)
                continue;
            /* TRACE RUN: exact Task pointer match + skip stale events */
            if (c->trace.mode == TRACE_MODE_RUN) {
                if (ev->caller_task != c->trace.run_task_ptr)
                    continue;
                if (ev->sequence < c->trace.run_start_seq)
                    continue;
            }
            if (!trace_filter_match(&c->trace, ev, filter_name))
                continue;
            sent_any = 1;
            if (sendbuf_append_data_chunk(&c->trace.sendbuf,
                                           trace_line_buf) < 0) {
                /* Buffer full -- drop this event for this client */
                c->trace.sendbuf.events_dropped++;
            }
        }

        /* Release slot */
        ev->valid = 0;

        /* Reset in-progress stall tracking now that we've advanced
         * past this position.  Without this reset, wrapping back to
         * the same ring slot would falsely inherit the stall count. */
        if (pos == g_inflight_stall_pos) {
            g_inflight_stall_pos = 0xFFFFFFFF;
            g_inflight_stall_count = 0;
        }

        /* Advance consumer */
        pos = (pos + 1) % ring->capacity;
        ring->read_pos = pos;
        /* Only count events actually sent to a subscriber toward the
         * batch limit.  Skipped events (stale, filtered out) are free
         * to consume — this prevents a full buffer of stale events
         * from blocking real events for 128+ poll cycles. */
        if (sent_any)
            batch++;
        total_consumed++;
    }

    g_anchor->events_consumed += total_consumed;

    /* Report overflow */
    if (ring->overflow > 0) {
        Disable();
        ov = ring->overflow;
        ring->overflow = 0;
        Enable();
        g_events_dropped += ov;
        sprintf(trace_line_buf, "# OVERFLOW %lu events dropped",
                (unsigned long)ov);
        for (i = 0; i < MAX_CLIENTS; i++) {
            c = &d->clients[i];
            if (c->fd >= 0 && c->trace.active)
                sendbuf_append_data_chunk(&c->trace.sendbuf,
                                          trace_line_buf);
        }
    }

    ReleaseSemaphore(&g_anchor->sem);

    /* Opportunistic drain -- push buffered data toward the network.
     * Runs after ReleaseSemaphore() to minimize semaphore hold time.
     * The drain only needs c->trace.sendbuf and c->fd, not the ring. */
    for (i = 0; i < MAX_CLIENTS; i++) {
        c = &d->clients[i];
        if (c->fd >= 0 && c->trace.active && c->trace.sendbuf.len > 0) {
            if (sendbuf_drain(&c->trace.sendbuf, c->fd) < 0) {
                /* Socket broken -- do NOT send END/sentinel, just close */
                c->trace.sendbuf.len = 0;
                c->trace.sendbuf.events_dropped = 0;
                trace_run_cleanup(c);
                net_close(c->fd);
                c->fd = -1;
            }
        }
    }
}

/* ---- Command handler ---- */

int cmd_trace(struct daemon_state *d, int idx, const char *args)
{
    struct client *c = &d->clients[idx];
    char *sub;
    static char sub_buf[32];

    /* Extract subcommand */
    sub = sub_buf;
    {
        int si = 0;
        while (*args && *args != ' ' && *args != '\t' &&
               si < (int)sizeof(sub_buf) - 1) {
            sub_buf[si++] = *args++;
        }
        sub_buf[si] = '\0';
        while (*args == ' ' || *args == '\t')
            args++;
        /* args now points past the subcommand, at filter arguments
         * (or '\0' if none). Passed to trace_cmd_start(). */
    }

    if (stricmp(sub, "STATUS") == 0) {
        return trace_cmd_status(c);
    }

    if (stricmp(sub, "START") == 0) {
        return trace_cmd_start(d, idx, args);
    }

    if (stricmp(sub, "RUN") == 0) {
        return trace_cmd_run(d, idx, args);
    }

    if (stricmp(sub, "ENABLE") == 0) {
        return trace_cmd_enable(c, args);
    }

    if (stricmp(sub, "DISABLE") == 0) {
        return trace_cmd_disable(c, args);
    }

    if (stricmp(sub, "TIER") == 0) {
        int level;
        if (!args || !args[0]) {
            send_error(c->fd, ERR_SYNTAX, "Usage: TRACE TIER <1|2|3>");
            send_sentinel(c->fd);
            return 0;
        }
        level = args[0] - '0';
        if (level < 1 || level > 3) {
            send_error(c->fd, ERR_SYNTAX, "Invalid tier level (1-3)");
            send_sentinel(c->fd);
            return 0;
        }
        g_current_tier = level;
        send_ok(c->fd, NULL);
        send_sentinel(c->fd);
        return 0;
    }

    send_error(c->fd, ERR_SYNTAX, "Unknown TRACE subcommand");
    send_sentinel(c->fd);
    return 0;
}

/* ---- TRACE STATUS ---- */

static int trace_cmd_status(struct client *c)
{
    static char line[128];

    if (!trace_auto_load()) {
        send_ok(c->fd, NULL);
        send_payload_line(c->fd, "loaded=0");
        send_sentinel(c->fd);
        return 0;
    }

    send_ok(c->fd, NULL);
    send_payload_line(c->fd, "loaded=1");

    snprintf(line, sizeof(line), "enabled=%d",
             g_anchor->global_enable ? 1 : 0);
    send_payload_line(c->fd, line);

    snprintf(line, sizeof(line), "patches=%d",
             (int)g_anchor->patch_count);
    send_payload_line(c->fd, line);

    snprintf(line, sizeof(line), "events_produced=%lu",
             (unsigned long)g_anchor->event_sequence);
    send_payload_line(c->fd, line);

    snprintf(line, sizeof(line), "events_consumed=%lu",
             (unsigned long)g_anchor->events_consumed);
    send_payload_line(c->fd, line);

    snprintf(line, sizeof(line), "events_dropped=%lu",
             (unsigned long)g_events_dropped);
    send_payload_line(c->fd, line);

    snprintf(line, sizeof(line), "events_self_filtered=%lu",
             (unsigned long)g_self_filtered);
    send_payload_line(c->fd, line);

    if (g_anchor->ring) {
        if (AttemptSemaphoreShared(&g_anchor->sem)) {
            struct atrace_ringbuf *ring = g_anchor->ring;
            if (ring) {
                ULONG used;

                snprintf(line, sizeof(line), "buffer_capacity=%lu",
                         (unsigned long)ring->capacity);
                send_payload_line(c->fd, line);

                used = (ring->write_pos - ring->read_pos
                        + ring->capacity) % ring->capacity;
                snprintf(line, sizeof(line), "buffer_used=%lu",
                         (unsigned long)used);
                send_payload_line(c->fd, line);

                if (used > 0) {
                    struct atrace_event *entries =
                        (struct atrace_event *)((UBYTE *)ring
                            + sizeof(struct atrace_ringbuf));
                    ULONG peek;
                    int n;
                    for (n = 0; n < 4; n++) {
                        peek = (ring->read_pos + (ULONG)n) % ring->capacity;
                        if ((ULONG)n >= used)
                            break;
                        snprintf(line, sizeof(line),
                                 "peek_%d=pos=%lu valid=%u lib_id=%u"
                                 " lvo=%d seq=%lu task=0x%08lx",
                                 n,
                                 (unsigned long)peek,
                                 (unsigned)entries[peek].valid,
                                 (unsigned)entries[peek].lib_id,
                                 (int)entries[peek].lvo_offset,
                                 (unsigned long)entries[peek].sequence,
                                 (unsigned long)entries[peek].caller_task);
                        send_payload_line(c->fd, line);
                    }
                }
            }
            ReleaseSemaphore(&g_anchor->sem);
        }
    }

    snprintf(line, sizeof(line), "poll_count=%lu",
             (unsigned long)g_poll_count);
    send_payload_line(c->fd, line);

    /* filter_task status */
    if (g_anchor->version >= 2) {
        snprintf(line, sizeof(line), "filter_task=0x%08lx",
                 (unsigned long)g_anchor->filter_task);
        send_payload_line(c->fd, line);
    }

    /* Anchor version and EClock info */
    snprintf(line, sizeof(line), "anchor_version=%d",
             (int)g_anchor->version);
    send_payload_line(c->fd, line);

    if (g_anchor->version >= 3 && g_anchor->eclock_freq != 0) {
        snprintf(line, sizeof(line), "eclock_freq=%lu",
                 (unsigned long)g_anchor->eclock_freq);
        send_payload_line(c->fd, line);
    }

    /* IoErr capture capability */
    if (g_anchor->version >= 4) {
        send_payload_line(c->fd, "ioerr_capture=1");
    }

    /* Daemon task filter status */
    if (g_anchor->version >= 6) {
        snprintf(line, sizeof(line), "daemon_task_filter=%s",
                 g_anchor->daemon_task ? "active" : "inactive");
        send_payload_line(c->fd, line);
    }

    /* Count noise-disabled functions */
    {
        int noise_disabled = 0;
        const char **np;
        for (np = noise_func_names; *np; np++) {
            int pidx = find_patch_index_by_name(*np);
            if (pidx >= 0 && pidx < (int)g_anchor->patch_count) {
                if (!g_anchor->patches[pidx].enabled)
                    noise_disabled++;
            }
        }
        snprintf(line, sizeof(line), "noise_disabled=%d", noise_disabled);
        send_payload_line(c->fd, line);
    }

    /* Per-patch status listing */
    {
        int pi;
        for (pi = 0; pi < (int)g_anchor->patch_count; pi++) {
            struct atrace_patch *p = &g_anchor->patches[pi];
            const struct trace_func_entry *fe;
            fe = lookup_func(p->lib_id, p->lvo_offset);
            if (fe) {
                snprintf(line, sizeof(line), "patch_%d=%s.%s enabled=%d",
                         pi, fe->lib_name, fe->func_name,
                         p->enabled ? 1 : 0);
            } else {
                snprintf(line, sizeof(line), "patch_%d=lib%d/lvo%d enabled=%d",
                         pi, (int)p->lib_id, (int)p->lvo_offset,
                         p->enabled ? 1 : 0);
            }
            send_payload_line(c->fd, line);
        }
    }

    send_sentinel(c->fd);
    return 0;
}

/* ---- TRACE START ---- */

static int trace_cmd_start(struct daemon_state *d, int idx,
                            const char *args)
{
    struct client *c = &d->clients[idx];

    /* Mutual exclusion with TAIL */
    if (c->tail.active) {
        send_error(c->fd, ERR_INTERNAL, "TAIL session active");
        send_sentinel(c->fd);
        return 0;
    }

    /* Mutual exclusion: reject if already tracing */
    if (c->trace.active) {
        send_error(c->fd, ERR_INTERNAL, "TRACE session already active");
        send_sentinel(c->fd);
        return 0;
    }

    /* Check for atrace (auto-load if needed) */
    if (!trace_auto_load()) {
        send_error(c->fd, ERR_INTERNAL, "atrace not loaded");
        send_sentinel(c->fd);
        return 0;
    }

    /* Check atrace is enabled */
    if (!g_anchor->global_enable) {
        send_error(c->fd, ERR_INTERNAL,
                   "atrace is disabled (run: atrace_loader ENABLE)");
        send_sentinel(c->fd);
        return 0;
    }

    /* Parse filters from remaining args */
    parse_filters(args, &c->trace);

    /* Clear caches for the new session */
    lock_cache_clear();
    fh_cache_clear();
    diskobj_cache_clear();
    seg_cache_clear();
    g_current_tier = 1;

    /* Enter streaming mode */
    c->trace.active = 1;

    /* Initialize send buffer for non-blocking delivery */
    sendbuf_init(&c->trace.sendbuf);

    /* Drain stale buffer content and clear valid flags.
     * Without a subscriber, background activity fills the ring buffer.
     * A user starting TRACE START wants new activity, not history. */
    Forbid();
    drain_stale_events();
    Permit();

    /* Capture EClock epoch for per-event timestamps (v3+) */
    eclock_capture_epoch();

    /* Send OK and header using blocking sends (before non-blocking switch) */
    send_ok(c->fd, NULL);
    if (emit_trace_header(c->fd, &c->trace, NULL) < 0) {
        c->trace.active = 0;
        return 0;
    }

    /* Switch to non-blocking mode for streaming event delivery */
    net_set_nonblocking(c->fd);

    return 0;
}

/* ---- TRACE RUN ---- */

static int trace_cmd_run(struct daemon_state *d, int idx,
                          const char *args)
{
    struct client *c = &d->clients[idx];
    const char *p;
    const char *command;
    char filter_buf[256];
    int filter_len;
    BPTR cd_lock;
    int slot;
    int i;
    int oldest_slot;
    int oldest_id;
    struct Process *proc;
    char info[16];

    /* Mutual exclusion with TAIL */
    if (c->tail.active) {
        send_error(c->fd, ERR_INTERNAL, "TAIL session active");
        send_sentinel(c->fd);
        return 0;
    }

    /* Mutual exclusion: reject if already tracing */
    if (c->trace.active) {
        send_error(c->fd, ERR_INTERNAL, "TRACE session already active");
        send_sentinel(c->fd);
        return 0;
    }

    /* Check for atrace (auto-load if needed) */
    if (!trace_auto_load()) {
        send_error(c->fd, ERR_INTERNAL, "atrace not loaded");
        send_sentinel(c->fd);
        return 0;
    }

    /* Check atrace is enabled */
    if (!g_anchor->global_enable) {
        send_error(c->fd, ERR_INTERNAL,
                   "atrace is disabled (run: atrace_loader ENABLE)");
        send_sentinel(c->fd);
        return 0;
    }

    /* Check async exec is available */
    if (g_proc_sigbit < 0) {
        send_error(c->fd, ERR_INTERNAL, "Async exec unavailable");
        send_sentinel(c->fd);
        return 0;
    }

    /* Find the "--" separator */
    p = args;
    command = NULL;
    while (*p) {
        if (p[0] == '-' && p[1] == '-' &&
            (p == args || p[-1] == ' ' || p[-1] == '\t') &&
            (p[2] == ' ' || p[2] == '\t' || p[2] == '\0')) {
            command = p + 2;
            while (*command == ' ' || *command == '\t')
                command++;
            break;
        }
        p++;
    }

    if (!command) {
        send_error(c->fd, ERR_SYNTAX, "Missing -- separator");
        send_sentinel(c->fd);
        return 0;
    }

    if (*command == '\0') {
        send_error(c->fd, ERR_SYNTAX, "Missing command");
        send_sentinel(c->fd);
        return 0;
    }

    /* Extract filter portion (everything before "--") */
    filter_len = (int)(p - args);
    if (filter_len >= (int)sizeof(filter_buf))
        filter_len = (int)sizeof(filter_buf) - 1;
    memcpy(filter_buf, args, filter_len);
    filter_buf[filter_len] = '\0';

    /* Reject PROC= filter (process filtering is automatic) */
    if (stristr(filter_buf, "PROC=") != NULL) {
        send_error(c->fd, ERR_SYNTAX,
                   "PROC filter not valid for TRACE RUN");
        send_sentinel(c->fd);
        return 0;
    }

    /* Parse CD= from filter portion */
    cd_lock = 0;
    {
        char *scan = filter_buf;
        while (*scan) {
            char *tok_start;
            while (*scan == ' ' || *scan == '\t')
                scan++;
            if (*scan == '\0')
                break;
            tok_start = scan;
            while (*scan && *scan != ' ' && *scan != '\t')
                scan++;
            if (strnicmp(tok_start, "CD=", 3) == 0) {
                char cd_path[512];
                int ci = 0;
                const char *cp = tok_start + 3;
                while (cp < scan && ci < (int)sizeof(cd_path) - 1)
                    cd_path[ci++] = *cp++;
                cd_path[ci] = '\0';

                if (ci > 0) {
                    cd_lock = Lock((STRPTR)cd_path, ACCESS_READ);
                    if (!cd_lock) {
                        send_error(c->fd, ERR_NOT_FOUND,
                                   "Directory not found");
                        send_sentinel(c->fd);
                        return 0;
                    }
                }

                /* Blank out the CD= token so parse_filters()
                 * does not see it as an unknown keyword. */
                while (tok_start < scan)
                    *tok_start++ = ' ';
                break;
            }
        }
    }

    /* Clear caches for the new session.
     * Must be before process creation -- a timer interrupt between
     * Permit() and a later clear could let the new process call
     * Lock(), caching with stale session data. */
    lock_cache_clear();
    fh_cache_clear();
    diskobj_cache_clear();
    seg_cache_clear();
    g_current_tier = 1;

    /* Find a proc_slot (same logic as exec_async) */
    slot = -1;
    oldest_slot = -1;
    oldest_id = 0x7FFFFFFF;

    for (i = 0; i < MAX_TRACKED_PROCS; i++) {
        if (g_daemon_state->procs[i].id == 0) {
            slot = i;
            break;
        }
        if (g_daemon_state->procs[i].status != PROC_RUNNING) {
            if (g_daemon_state->procs[i].id < oldest_id) {
                oldest_id = g_daemon_state->procs[i].id;
                oldest_slot = i;
            }
        }
    }

    if (slot < 0)
        slot = oldest_slot;

    if (slot < 0) {
        if (cd_lock)
            UnLock(cd_lock);
        send_error(c->fd, ERR_INTERNAL, "Process table full");
        send_sentinel(c->fd);
        return 0;
    }

    /* Extract command basename for process name.
     * Written into the per-slot proc_name buffer so each process has
     * its own stable NP_Name storage.  AmigaOS stores the NP_Name
     * pointer, not a copy, so the buffer must outlive the process --
     * daemon_state slots persist for the daemon's lifetime. */
    {
        const char *cmd_start = command;
        const char *basename;
        int word_len = 0;
        int name_len;
        char *namebuf = g_daemon_state->procs[slot].proc_name;

        /* Skip leading spaces */
        while (*cmd_start == ' ') cmd_start++;

        /* Find end of first word */
        while (cmd_start[word_len] && cmd_start[word_len] != ' ')
            word_len++;

        /* Find basename: last '/' or ':' in first word */
        basename = cmd_start;
        {
            int bi;
            for (bi = 0; bi < word_len; bi++) {
                if (cmd_start[bi] == '/' || cmd_start[bi] == ':')
                    basename = &cmd_start[bi + 1];
            }
        }

        /* Copy basename, truncate to 31 chars */
        name_len = word_len - (int)(basename - cmd_start);
        if (name_len > 31) name_len = 31;
        memcpy(namebuf, basename, name_len);
        namebuf[name_len] = '\0';

        /* Fallback for empty basename (path ends with ':' or '/') */
        if (namebuf[0] == '\0')
            strcpy(namebuf, "amigactld-exec");
    }

    /* Populate the proc_slot and launch */
    strncpy(g_daemon_state->procs[slot].command, command, 255);
    g_daemon_state->procs[slot].command[255] = '\0';
    g_daemon_state->procs[slot].status = PROC_RUNNING;
    g_daemon_state->procs[slot].completed = 0;
    g_daemon_state->procs[slot].rc = 0;
    g_daemon_state->procs[slot].id = g_daemon_state->next_proc_id++;
    g_daemon_state->procs[slot].cd_lock = cd_lock;

    Forbid();
    proc = CreateNewProcTags(
        NP_Entry, (ULONG)async_wrapper,
        NP_Name, (ULONG)g_daemon_state->procs[slot].proc_name,
        NP_StackSize, 16384,
        NP_Cli, TRUE,
        TAG_DONE);

    if (!proc) {
        Permit();
        g_daemon_state->procs[slot].id = 0;
        g_daemon_state->procs[slot].status = PROC_EXITED;
        g_daemon_state->procs[slot].task = NULL;
        if (cd_lock) {
            UnLock(cd_lock);
            g_daemon_state->procs[slot].cd_lock = 0;
        }
        send_error(c->fd, ERR_INTERNAL, "Failed to create process");
        send_sentinel(c->fd);
        return 0;
    }

    g_daemon_state->procs[slot].task = (struct Task *)proc;

    /* Detect orphaned filter_task: if non-NULL but no connected
     * client owns it (dead client, daemon restart, etc.), clear it
     * so this TRACE RUN can take ownership. */
    if (g_anchor->version >= 2 && g_anchor->filter_task != NULL) {
        int fi;
        int orphaned = 1;
        for (fi = 0; fi < MAX_CLIENTS; fi++) {
            struct client *fc = &g_daemon_state->clients[fi];
            if (fc->fd >= 0 && fc->trace.active &&
                fc->trace.mode == TRACE_MODE_RUN &&
                fc->trace.run_task_ptr == g_anchor->filter_task) {
                orphaned = 0;
                break;
            }
        }
        if (orphaned)
            g_anchor->filter_task = NULL;
    }

    /* Set filter_task for stub-level task filtering if available.
     *
     * Design note: the filter_task field is a single global value in
     * the anchor struct. Only one TRACE RUN can use stub-level
     * filtering at a time. If another TRACE RUN is already active
     * (filter_task != NULL), we skip the filter_task write, falling
     * back to daemon-side filtering only (the existing run_task_ptr
     * check in trace_poll_events). The ring buffer may overflow in
     * this case.
     *
     * Noise functions are left at their current enable/disable state.
     * Even with stub-level task filtering, auto-enabling noise
     * overwhelms the ring buffer (~10K events in 0.5s from a single
     * target process). Users who want noise events can enable them
     * explicitly before TRACE RUN. */
    if (g_anchor->version >= 2 && g_anchor->filter_task == NULL) {
        g_anchor->filter_task = (APTR)proc;
    }

    /* Capture event_sequence under Forbid() -- the new process cannot
     * run until Permit(), so this value is guaranteed to precede any
     * events from the traced process. */
    c->trace.run_start_seq = g_anchor->event_sequence;

    /* Drain stale buffer content and clear valid flags.
     * Without a subscriber, background activity fills the ring buffer.
     * The target process (which cannot run until Permit()) would find
     * no free slots for its events.  Clearing valid flags prevents
     * trace_poll_events() from racing past the producer through stale
     * entries with valid=1 from prior activity. */
    drain_stale_events();

    Permit();

    /* Parse trace filters (after successful process creation) */
    parse_filters(filter_buf, &c->trace);

    /* Capture EClock epoch for per-event timestamps (v3+) */
    eclock_capture_epoch();

    /* Enter TRACE RUN streaming mode. */
    c->trace.mode = TRACE_MODE_RUN;
    c->trace.run_proc_slot = slot;
    c->trace.run_task_ptr = (APTR)g_daemon_state->procs[slot].task;
    c->trace.active = 1;

    /* Initialize send buffer for non-blocking delivery */
    sendbuf_init(&c->trace.sendbuf);

    sprintf(info, "%d", g_daemon_state->procs[slot].id);
    send_ok(c->fd, info);
    if (emit_trace_header(c->fd, &c->trace, command) < 0) {
        trace_run_cleanup(c);
        return 0;
    }

    /* Switch to non-blocking mode for streaming event delivery */
    net_set_nonblocking(c->fd);

    return 0;
}

/* ---- TRACE RUN cleanup helper ---- */

/* Clear filter_task and TRACE RUN state.
 * Called when TRACE RUN ends (process exit, STOP, disconnect,
 * send failure, or atrace shutdown). */
static void trace_run_cleanup(struct client *c)
{
    /* Clear stub-level task filter if we own it */
    if (g_anchor && g_anchor->version >= 2 &&
        c->trace.run_task_ptr != NULL &&
        g_anchor->filter_task == c->trace.run_task_ptr) {
        g_anchor->filter_task = NULL;
    }

    /* Clear TRACE RUN state */
    c->trace.active = 0;
    c->trace.mode = TRACE_MODE_START;
    c->trace.run_proc_slot = -1;
    c->trace.run_task_ptr = NULL;
}

/* ---- TRACE RUN disconnect cleanup ---- */

void trace_run_disconnect_cleanup(struct daemon_state *d, int idx)
{
    struct client *c = &d->clients[idx];
    if (c->trace.active && c->trace.mode == TRACE_MODE_RUN) {
        trace_run_cleanup(c);
    }
}

/* ---- TRACE RUN completion check ---- */

void trace_check_run_completed(struct daemon_state *d)
{
    int i;
    struct client *c;
    struct tracked_proc *tp;
    static char comment[64];

    for (i = 0; i < MAX_CLIENTS; i++) {
        int run_client_ok = 1;  /* Track send status during drain */
        struct timeval sndtv;
        c = &d->clients[i];
        if (c->fd < 0 || !c->trace.active)
            continue;
        if (c->trace.mode != TRACE_MODE_RUN)
            continue;
        if (c->trace.run_proc_slot < 0)
            continue;

        tp = &d->procs[c->trace.run_proc_slot];

        if (tp->status != PROC_EXITED)
            continue;

        /* Switch the TRACE RUN client to blocking mode with SO_SNDTIMEO
         * for the final drain.  This ensures all remaining events are
         * delivered to a responsive client rather than being dropped. */
        sndtv.tv_secs = 2;
        sndtv.tv_micro = 0;
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wincompatible-pointer-types"
        setsockopt(c->fd, SOL_SOCKET, SO_SNDTIMEO,
                   &sndtv, sizeof(sndtv));
#pragma GCC diagnostic pop
        net_set_blocking(c->fd);

        /* Flush any existing buffered data before the final drain */
        if (c->trace.sendbuf.len > 0) {
            if (send_all(c->fd, c->trace.sendbuf.buf,
                         c->trace.sendbuf.len) < 0) {
                run_client_ok = 0;
            } else {
                c->trace.sendbuf.len = 0;
            }
        }

        /* Final drain: read remaining target events from the ring
         * buffer BEFORE cleanup clears filter_task.  The stubs may
         * have written events that trace_poll_events() hasn't
         * consumed yet (the process exited between poll cycles).
         *
         * Non-target events are broadcast to other active TRACE START
         * clients using their respective send buffers (non-blocking). */
        if (run_client_ok && g_anchor && g_anchor->ring && g_ring_entries) {
            struct atrace_ringbuf *ring = g_anchor->ring;
            struct atrace_event *ev;
            ULONG pos;
            int batch;
            int j;
            struct DateStamp ds;
            static char drain_timestr[16];
            LONG hours, mins, secs, ticks_rem;
            const char *task_name;
            struct client *oc;

            if (AttemptSemaphoreShared(&g_anchor->sem)) {
                DateStamp(&ds);
                hours = ds.ds_Minute / 60;
                mins = ds.ds_Minute % 60;
                secs = ds.ds_Tick / 50;
                ticks_rem = ds.ds_Tick % 50;
                sprintf(drain_timestr, "%02ld:%02ld:%02ld.%03ld",
                        (long)hours, (long)mins, (long)secs,
                        (long)(ticks_rem * 20));

                pos = ring->read_pos;

                /* Bounds check -- reset corrupted read_pos, matching
                 * the guard in trace_poll_events(). */
                if (pos >= ring->capacity) {
                    ring->read_pos = ring->write_pos;
                    pos = ring->read_pos;
                }

                batch = 0;

                /* Drain remaining events, bounded by ring capacity
                 * (can't have more valid entries than capacity). */
                while (batch < (int)ring->capacity &&
                       g_ring_entries[pos].valid) {
                    const char *filter_name;
                    ev = &g_ring_entries[pos];

                    /* Skip in-progress events -- they belong to
                     * still-running processes and will be consumed
                     * by the next trace_poll_events() call. */
                    if (ev->valid == 2)
                        break;

                    /* Use resolved name for PROC filter matching
                     * (same logic as trace_poll_events).
                     * resolve_task_name() gets CLI command basename;
                     * ev->task_name is raw tc_Node.ln_Name fallback. */
                    task_name = resolve_task_name(ev->caller_task);
                    if (task_name[0] == '<' &&
                        g_anchor->version >= 3 &&
                        ev->task_name[0] != '\0') {
                        filter_name = ev->task_name;
                    } else {
                        filter_name = task_name;
                    }
                    trace_format_event(ev, drain_timestr, task_name,
                                       trace_line_buf,
                                       sizeof(trace_line_buf));

                    /* Buffer target-task events for the TRACE RUN client.
                     * Use sendbuf to batch events, flushing synchronously
                     * when the buffer fills. */
                    if (run_client_ok &&
                        ev->caller_task == c->trace.run_task_ptr &&
                        ev->sequence >= c->trace.run_start_seq) {
                        if (trace_filter_match(&c->trace, ev, filter_name)) {
                            if (sendbuf_append_data_chunk(&c->trace.sendbuf,
                                                          trace_line_buf) < 0) {
                                /* Buffer full -- flush synchronously */
                                if (send_all(c->fd, c->trace.sendbuf.buf,
                                             c->trace.sendbuf.len) < 0) {
                                    run_client_ok = 0;
                                } else {
                                    c->trace.sendbuf.len = 0;
                                    /* Retry the append after flush */
                                    sendbuf_append_data_chunk(
                                        &c->trace.sendbuf, trace_line_buf);
                                }
                            }
                        }
                    }

                    /* Broadcast to other active TRACE START/RUN clients
                     * using their respective send buffers (non-blocking). */
                    for (j = 0; j < MAX_CLIENTS; j++) {
                        oc = &d->clients[j];
                        if (j == i)
                            continue;  /* skip the completing TRACE RUN client */
                        if (oc->fd < 0 || !oc->trace.active)
                            continue;
                        if (oc->trace.mode == TRACE_MODE_RUN) {
                            if (ev->caller_task != oc->trace.run_task_ptr)
                                continue;
                            if (ev->sequence < oc->trace.run_start_seq)
                                continue;
                        }
                        if (!trace_filter_match(&oc->trace, ev, filter_name))
                            continue;
                        if (sendbuf_append_data_chunk(&oc->trace.sendbuf,
                                                      trace_line_buf) < 0) {
                            oc->trace.sendbuf.events_dropped++;
                        }
                    }

                    /* Release slot and advance */
                    ev->valid = 0;
                    pos = (pos + 1) % ring->capacity;
                    ring->read_pos = pos;
                    batch++;
                }

                g_anchor->events_consumed += batch;
                ReleaseSemaphore(&g_anchor->sem);
            }
        }

        /* Send exit notification */
        sprintf(comment, "# PROCESS EXITED rc=%d", tp->rc);
        if (run_client_ok) {
            /* Flush remaining buffered data */
            if (c->trace.sendbuf.len > 0) {
                if (send_all(c->fd, c->trace.sendbuf.buf,
                             c->trace.sendbuf.len) < 0) {
                    run_client_ok = 0;
                } else {
                    c->trace.sendbuf.len = 0;
                }
            }
        }
        if (run_client_ok) {
            send_trace_data_chunk(c->fd, comment);
            send_end(c->fd);
            send_sentinel(c->fd);

            /* Clear SO_SNDTIMEO (set to 0 for normal blocking) */
            sndtv.tv_secs = 0;
            sndtv.tv_micro = 0;
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wincompatible-pointer-types"
            setsockopt(c->fd, SOL_SOCKET, SO_SNDTIMEO,
                       &sndtv, sizeof(sndtv));
#pragma GCC diagnostic pop
        } else {
            /* Client disconnected during drain; close and clean up */
            net_close(c->fd);
            c->fd = -1;
        }

        /* Clear filter_task, clear trace state */
        trace_run_cleanup(c);
    }
}

/* ---- TRACE ENABLE / TRACE DISABLE ---- */

static int trace_cmd_enable(struct client *c, const char *args)
{
    if (!trace_auto_load()) {
        send_error(c->fd, ERR_INTERNAL, "atrace not loaded");
        send_sentinel(c->fd);
        return 0;
    }

    /* Skip leading whitespace */
    while (*args == ' ' || *args == '\t')
        args++;

    if (*args == '\0') {
        /* No function names -- global enable */
        g_anchor->global_enable = 1;
        send_ok(c->fd, NULL);
        send_sentinel(c->fd);
        return 0;
    }

    /* Per-function enable: parse and validate all names first */
    {
        const char *p;
        const char *tok_start;
        char name[32];
        int len;
        int idx;

        /* First pass: validate all names */
        p = args;
        while (*p) {
            while (*p == ' ' || *p == '\t')
                p++;
            if (*p == '\0')
                break;
            tok_start = p;
            while (*p && *p != ' ' && *p != '\t')
                p++;
            len = (int)(p - tok_start);
            if (len >= (int)sizeof(name))
                len = (int)sizeof(name) - 1;
            memcpy(name, tok_start, len);
            name[len] = '\0';

            idx = find_patch_index_by_name(name);
            if (idx < 0) {
                static char errbuf[64];
                snprintf(errbuf, sizeof(errbuf),
                         "Unknown function: %s", name);
                send_error(c->fd, ERR_SYNTAX, errbuf);
                send_sentinel(c->fd);
                return 0;
            }
        }

        /* Second pass: apply enables */
        p = args;
        while (*p) {
            while (*p == ' ' || *p == '\t')
                p++;
            if (*p == '\0')
                break;
            tok_start = p;
            while (*p && *p != ' ' && *p != '\t')
                p++;
            len = (int)(p - tok_start);
            if (len >= (int)sizeof(name))
                len = (int)sizeof(name) - 1;
            memcpy(name, tok_start, len);
            name[len] = '\0';

            idx = find_patch_index_by_name(name);
            g_anchor->patches[idx].enabled = 1;
        }
    }

    send_ok(c->fd, NULL);
    send_sentinel(c->fd);
    return 0;
}

static int trace_cmd_disable(struct client *c, const char *args)
{
    if (!trace_auto_load()) {
        send_error(c->fd, ERR_INTERNAL, "atrace not loaded");
        send_sentinel(c->fd);
        return 0;
    }

    /* Skip leading whitespace */
    while (*args == ' ' || *args == '\t')
        args++;

    if (*args != '\0') {
        /* Per-function disable: parse and validate all names first */
        const char *p;
        const char *tok_start;
        char name[32];
        int len;
        int idx;

        /* First pass: validate all names */
        p = args;
        while (*p) {
            while (*p == ' ' || *p == '\t')
                p++;
            if (*p == '\0')
                break;
            tok_start = p;
            while (*p && *p != ' ' && *p != '\t')
                p++;
            len = (int)(p - tok_start);
            if (len >= (int)sizeof(name))
                len = (int)sizeof(name) - 1;
            memcpy(name, tok_start, len);
            name[len] = '\0';

            idx = find_patch_index_by_name(name);
            if (idx < 0) {
                static char errbuf[64];
                snprintf(errbuf, sizeof(errbuf),
                         "Unknown function: %s", name);
                send_error(c->fd, ERR_SYNTAX, errbuf);
                send_sentinel(c->fd);
                return 0;
            }
        }

        /* Second pass: apply disables.
         * No global_enable change, no buffer drain -- other functions
         * may still be producing events. */
        p = args;
        while (*p) {
            while (*p == ' ' || *p == '\t')
                p++;
            if (*p == '\0')
                break;
            tok_start = p;
            while (*p && *p != ' ' && *p != '\t')
                p++;
            len = (int)(p - tok_start);
            if (len >= (int)sizeof(name))
                len = (int)sizeof(name) - 1;
            memcpy(name, tok_start, len);
            name[len] = '\0';

            idx = find_patch_index_by_name(name);
            g_anchor->patches[idx].enabled = 0;
        }

        send_ok(c->fd, NULL);
        send_sentinel(c->fd);
        return 0;
    }

    /* Global disable */
    /* Set global_enable = 0 under Disable/Enable for atomicity */
    Disable();
    g_anchor->global_enable = 0;
    Enable();
    /* Do NOT drain use_counts here -- that would block the daemon
     * event loop. The stubs drain within one timeslice (~20ms). */

    /* Drain remaining events from ring buffer.
     * Without this, the buffer stays full after disable. Re-enabling
     * would start with a full buffer that immediately overflows.
     *
     * Safety: after global_enable = 0, all new stubs take the disabled
     * fast path. In-flight stubs (already past the global_enable check)
     * will complete and write to slots behind the new read_pos -- those
     * writes are silently discarded, which is intended. */
    if (g_anchor->ring) {
        struct atrace_ringbuf *ring = g_anchor->ring;
        ULONG pos;
        ULONG end;
        struct atrace_event *entries;

        entries = (struct atrace_event *)
            ((UBYTE *)ring + sizeof(struct atrace_ringbuf));

        /* Clear valid flags on occupied slots (belt-and-suspenders --
         * the read_pos advance alone is sufficient) */
        pos = ring->read_pos;
        end = ring->write_pos;
        while (pos != end) {
            entries[pos].valid = 0;
            pos = (pos + 1) % ring->capacity;
        }

        /* Advance read_pos to write_pos (atomic drain) */
        ring->read_pos = ring->write_pos;

        /* Accumulate overflow counter under Disable/Enable because
         * in-flight stubs may be incrementing overflow concurrently */
        if (ring->overflow > 0) {
            Disable();
            g_events_dropped += ring->overflow;
            ring->overflow = 0;
            Enable();
        }
    }

    send_ok(c->fd, NULL);
    send_sentinel(c->fd);
    return 0;
}

/* ---- Input handling during TRACE ---- */

int trace_handle_input(struct daemon_state *d, int idx)
{
    struct client *c = &d->clients[idx];
    int n;
    int result;

    n = recv_into_buf(c);
    if (n <= 0) {
        /* Non-blocking socket: EWOULDBLOCK means no data available, not
         * disconnect.  Only treat as disconnect if recv returned 0 (EOF)
         * or a real error. */
        if (n < 0 && net_get_errno() == EWOULDBLOCK)
            return 0;  /* No data available -- not a disconnect */
        c->trace.active = 0;
        c->trace.mode = TRACE_MODE_START;
        c->trace.run_proc_slot = -1;
        c->trace.run_task_ptr = NULL;
        return -1;
    }

    while ((result = extract_command(c, trace_cmd_buf,
                                      sizeof(trace_cmd_buf))) == 1) {
        char *p = trace_cmd_buf;
        while (*p == ' ' || *p == '\t')
            p++;
        if (*p == '\0')
            continue;

        if (stricmp(p, "STOP") == 0) {
            /* Set 2-second send timeout before blocking flush */
            struct timeval sndtv;
            sndtv.tv_secs = 2;
            sndtv.tv_micro = 0;
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wincompatible-pointer-types"
            setsockopt(c->fd, SOL_SOCKET, SO_SNDTIMEO,
                       &sndtv, sizeof(sndtv));
#pragma GCC diagnostic pop

            net_set_blocking(c->fd);
            /* Flush remaining buffered data */
            if (c->trace.sendbuf.len > 0) {
                if (send_all(c->fd, c->trace.sendbuf.buf,
                             c->trace.sendbuf.len) < 0) {
                    /* Socket broken -- skip terminal framing */
                    trace_run_cleanup(c);
                    return 0;
                }
                c->trace.sendbuf.len = 0;
            }
            send_end(c->fd);
            send_sentinel(c->fd);

            /* Clear SO_SNDTIMEO (set to 0 for normal blocking) */
            sndtv.tv_secs = 0;
            sndtv.tv_micro = 0;
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wincompatible-pointer-types"
            setsockopt(c->fd, SOL_SOCKET, SO_SNDTIMEO,
                       &sndtv, sizeof(sndtv));
#pragma GCC diagnostic pop

            /* Clear filter_task, clear trace state */
            trace_run_cleanup(c);
            return 0;
        }

        /* FILTER command during active trace stream */
        if (strnicmp(p, "FILTER", 6) == 0 &&
            (p[6] == '\0' || p[6] == ' ' || p[6] == '\t')) {
            char *filter_args = p + 6;
            while (*filter_args == ' ' || *filter_args == '\t')
                filter_args++;
            /* Parse extended FILTER. Handles both simple
             * and extended syntax (commas, blacklists). When no
             * commas or -LIB=/-FUNC= prefixes, delegates to
             * parse_filters() for backward compatibility. Resets
             * all filter fields including extended state. */
            parse_extended_filter(filter_args, &c->trace);

            /* Emit filter comment so the client sees the change */
            if (c->trace.active) {
                char filter_desc[384];
                char fline[512];
                int flen;
                flen = build_filter_desc(&c->trace, filter_desc,
                                          sizeof(filter_desc));
                if (flen > 0) {
                    snprintf(fline, sizeof(fline),
                             "# filter: tier=%s, %s",
                             tier_name(g_current_tier), filter_desc);
                } else {
                    snprintf(fline, sizeof(fline),
                             "# filter: tier=%s",
                             tier_name(g_current_tier));
                }
                sendbuf_append_data_chunk(&c->trace.sendbuf, fline);
            }
            continue;
        }

        /* TIER command during active trace stream.
         * Sets the daemon's tier level for content-based filtering
         * (e.g., OpenLibrary v0 suppression at Basic tier).
         * Sent by the client as a bare "TIER <1|2|3>" inline command,
         * following the same pattern as STOP and FILTER. */
        if (strnicmp(p, "TIER", 4) == 0 &&
            (p[4] == '\0' || p[4] == ' ' || p[4] == '\t')) {
            char *tier_arg = p + 4;
            while (*tier_arg == ' ' || *tier_arg == '\t')
                tier_arg++;
            if (*tier_arg >= '1' && *tier_arg <= '3') {
                int new_tier = *tier_arg - '0';
                if (new_tier != g_current_tier) {
                    char tline[128];
                    g_current_tier = new_tier;
                    /* Emit tier change notification to the stream */
                    snprintf(tline, sizeof(tline),
                             "# tier changed: %s",
                             tier_name(g_current_tier));
                    sendbuf_append_data_chunk(&c->trace.sendbuf, tline);
                }
            }
            /* Invalid values silently ignored. */
            continue;
        }

        /* Silently discard other input during trace */
    }

    if (result == -1) {
        c->recv_len = 0;
        c->discarding = 0;
    }

    return 0;
}

/* ---- Active session query ---- */

int trace_any_active(struct daemon_state *d)
{
    int i;
    for (i = 0; i < MAX_CLIENTS; i++) {
        if (d->clients[i].fd >= 0 && d->clients[i].trace.active)
            return 1;
    }
    return 0;
}
