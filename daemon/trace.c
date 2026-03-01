/*
 * amigactld -- Library call tracing
 *
 * Implements TRACE STATUS, TRACE START/STOP streaming,
 * TRACE ENABLE/DISABLE, and per-client event filtering.
 * Follows the TAIL module pattern (tail.c).
 *
 * Phase 2: function name lookup, task name cache, per-function
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
#include <exec/execbase.h>
#include <dos/dostags.h>
#include <dos/dosextens.h>

#include <stdio.h>
#include <string.h>

/* ---- Function/library name lookup table ---- */

/* Must match the function table in atrace/funcs.c exactly.
 * The daemon cannot access atrace's table directly (separate binary),
 * so it maintains its own static copy for name resolution. */

/* Error classification for the ERRORS filter.
 * Determines what return values constitute an error for each function. */
#define ERR_CHECK_NULL    0   /* error when retval == 0 (NULL/FALSE) -- most functions */
#define ERR_CHECK_NZERO   1   /* error when retval != 0 (OpenDevice: 0=success) */
#define ERR_CHECK_VOID    2   /* void function -- never shown in ERRORS mode */
#define ERR_CHECK_ANY     3   /* no clear error convention -- always show */

struct trace_func_entry {
    const char *lib_name;
    const char *func_name;
    UBYTE lib_id;
    WORD  lvo_offset;
    UBYTE error_check;   /* ERR_CHECK_NULL, ERR_CHECK_NZERO, ERR_CHECK_VOID, or ERR_CHECK_ANY */
};

static const struct trace_func_entry func_table[] = {
    /* exec.library functions (12)
     * Error conventions:
     *   FindPort/FindResident/FindSemaphore/FindTask: NULL=not found
     *   OpenDevice: 0=success, non-zero=error (ERR_CHECK_NZERO)
     *   OpenLibrary/OpenResource: NULL=failure
     *   GetMsg: NULL=no message (not strictly an error)
     *   PutMsg/ObtainSemaphore/ReleaseSemaphore: void
     *   AllocMem: NULL=allocation failed */
    { "exec", "FindPort",          LIB_EXEC, -390, ERR_CHECK_NULL  },
    { "exec", "FindResident",      LIB_EXEC,  -96, ERR_CHECK_NULL  },
    { "exec", "FindSemaphore",     LIB_EXEC, -594, ERR_CHECK_NULL  },
    { "exec", "FindTask",          LIB_EXEC, -294, ERR_CHECK_NULL  },
    { "exec", "OpenDevice",        LIB_EXEC, -444, ERR_CHECK_NZERO },
    { "exec", "OpenLibrary",       LIB_EXEC, -552, ERR_CHECK_NULL  },
    { "exec", "OpenResource",      LIB_EXEC, -498, ERR_CHECK_NULL  },
    { "exec", "GetMsg",            LIB_EXEC, -372, ERR_CHECK_NULL  },
    { "exec", "PutMsg",            LIB_EXEC, -366, ERR_CHECK_VOID  },
    { "exec", "ObtainSemaphore",   LIB_EXEC, -564, ERR_CHECK_VOID  },
    { "exec", "ReleaseSemaphore",  LIB_EXEC, -570, ERR_CHECK_VOID  },
    { "exec", "AllocMem",          LIB_EXEC, -198, ERR_CHECK_NULL  },
    /* dos.library functions (18)
     * Error conventions:
     *   Open/Lock/CreateDir/FindVar/LoadSeg/NewLoadSeg: NULL=failure
     *   Close: DOSTRUE(-1)=success, DOSFALSE(0)=failure -> ERR_CHECK_NULL
     *   DeleteFile/Execute/MakeLink/Rename: DOSTRUE=success, 0=failure -> ERR_CHECK_NULL
     *   GetVar: -1=failure (ERR_CHECK_ANY since -1 is the error indicator)
     *   RunCommand: return code (any value valid, -1=couldn't run)
     *   SetVar/DeleteVar/AddDosEntry: DOSTRUE=success, 0=failure -> ERR_CHECK_NULL
     *   SystemTagList: return code (-1=couldn't run)
     *   CurrentDir: returns old lock (no error convention) */
    { "dos", "Open",               LIB_DOS,   -30, ERR_CHECK_NULL  },
    { "dos", "Close",              LIB_DOS,   -36, ERR_CHECK_NULL  },
    { "dos", "Lock",               LIB_DOS,   -84, ERR_CHECK_NULL  },
    { "dos", "DeleteFile",         LIB_DOS,   -72, ERR_CHECK_NULL  },
    { "dos", "Execute",            LIB_DOS,  -222, ERR_CHECK_NULL  },
    { "dos", "GetVar",             LIB_DOS,  -906, ERR_CHECK_ANY   },
    { "dos", "FindVar",            LIB_DOS,  -918, ERR_CHECK_NULL  },
    { "dos", "LoadSeg",            LIB_DOS,  -150, ERR_CHECK_NULL  },
    { "dos", "NewLoadSeg",         LIB_DOS,  -768, ERR_CHECK_NULL  },
    { "dos", "CreateDir",          LIB_DOS,  -120, ERR_CHECK_NULL  },
    { "dos", "MakeLink",           LIB_DOS,  -444, ERR_CHECK_NULL  },
    { "dos", "Rename",             LIB_DOS,   -78, ERR_CHECK_NULL  },
    { "dos", "RunCommand",         LIB_DOS,  -504, ERR_CHECK_ANY   },
    { "dos", "SetVar",             LIB_DOS,  -900, ERR_CHECK_NULL  },
    { "dos", "DeleteVar",          LIB_DOS,  -912, ERR_CHECK_NULL  },
    { "dos", "SystemTagList",      LIB_DOS,  -606, ERR_CHECK_ANY   },
    { "dos", "AddDosEntry",        LIB_DOS,  -678, ERR_CHECK_NULL  },
    { "dos", "CurrentDir",         LIB_DOS,  -126, ERR_CHECK_VOID  },
};

#define FUNC_TABLE_SIZE  (sizeof(func_table) / sizeof(func_table[0]))

/* ---- Module globals ---- */

/* Cached anchor pointer -- NULL if atrace not found */
static struct atrace_anchor *g_anchor = NULL;

/* Cached ring entries pointer (avoids repeated offset math) */
static struct atrace_event *g_ring_entries = NULL;

/* Running total of dropped events (from ring->overflow) */
static ULONG g_events_dropped = 0;

/* Command extraction buffer (static, single-threaded) */
static char trace_cmd_buf[MAX_CMD_LEN + 1];

/* Event formatting buffer (static, single-threaded) */
static char trace_line_buf[512];

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

/* ---- Task name cache ---- */

#define TASK_CACHE_SIZE   64
#define TASK_CACHE_REFRESH_INTERVAL  50  /* polls = ~5 seconds */

struct task_cache_entry {
    APTR task_ptr;
    char name[64];
};

static struct task_cache_entry task_cache[TASK_CACHE_SIZE];
static int task_cache_count = 0;
static int task_cache_polls = 0;

/* ---- Forward declarations ---- */

static int trace_discover(void);
static void refresh_task_cache(void);
static const char *resolve_task_name(APTR task_ptr);
static const struct trace_func_entry *lookup_func(UBYTE lib_id, WORD lvo);
static const char *stristr(const char *haystack, const char *needle);
static int parse_filters(const char *args, struct trace_state *ts);
static int trace_filter_match(struct trace_state *ts,
                               struct atrace_event *ev,
                               const char *task_name);
static const char *format_access_mode(LONG mode);
static const char *format_lock_type(LONG type);
static void format_memf_flags(ULONG flags, char *buf, int bufsz);
static void format_args(struct atrace_event *ev,
                        const struct trace_func_entry *fe,
                        char *buf, int bufsz);
static void format_retval(struct atrace_event *ev,
                           const struct trace_func_entry *fe,
                           char *buf, int bufsz);
static void trace_format_event(struct atrace_event *ev,
                                const char *timestr,
                                char *buf, int bufsz);
static int send_trace_data_chunk(LONG fd, const char *line);
static int trace_cmd_status(struct client *c);
static int trace_cmd_start(struct daemon_state *d, int idx,
                            const char *args);
static int trace_cmd_run(struct daemon_state *d, int idx,
                          const char *args);
static int trace_cmd_enable(struct client *c, const char *args);
static int trace_cmd_disable(struct client *c, const char *args);

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

    return 0;  /* always succeeds */
}

void trace_cleanup(void)
{
    /* Nothing to free -- atrace owns all shared memory */
    g_anchor = NULL;
    g_ring_entries = NULL;
    g_events_dropped = 0;
    task_cache_count = 0;
    task_cache_polls = 0;
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
    return 1;
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
            strncpy(task_cache[idx].name, node->ln_Name, 63);
            task_cache[idx].name[63] = '\0';
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
            strncpy(task_cache[idx].name, node->ln_Name, 63);
            task_cache[idx].name[63] = '\0';
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
            strncpy(task_cache[idx].name, this_task->tc_Node.ln_Name, 63);
            task_cache[idx].name[63] = '\0';
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
 * Uses a cached task-name table refreshed every ~5 seconds.
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
        if (task_cache[i].task_ptr == task_ptr)
            return task_cache[i].name;
    }

    /* Cache miss -- attempt direct dereference under Forbid.
     * This handles short-lived tasks that started and exited between
     * cache refreshes. The Forbid prevents the task from being
     * removed while we read its name (same approach as Phase 1).
     * If the task has already exited, the pointer may be stale --
     * this is the same risk as Phase 1 and is acceptable on
     * single-CPU 68k where Forbid blocks FreeMem completions. */
    task = (struct Task *)task_ptr;
    Forbid();
    name = (char *)task->tc_Node.ln_Name;
    if (name) {
        strncpy(fallback, name, sizeof(fallback) - 1);
        fallback[sizeof(fallback) - 1] = '\0';
    } else {
        sprintf(fallback, "<task 0x%08lx>", (unsigned long)task_ptr);
    }
    Permit();

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

    while (*args) {
        while (*args == ' ' || *args == '\t')
            args++;
        if (*args == '\0')
            break;

        if (strnicmp(args, "LIB=", 4) == 0) {
            /* Extract library name, look up lib_id */
            char lname[32];
            int li = 0;
            args += 4;
            while (*args && *args != ' ' && *args != '\t' &&
                   li < (int)sizeof(lname) - 1)
                lname[li++] = *args++;
            lname[li] = '\0';

            /* Match against known library short names */
            for (li = 0; li < (int)FUNC_TABLE_SIZE; li++) {
                if (stricmp(lname, func_table[li].lib_name) == 0) {
                    ts->filter_lib_id = func_table[li].lib_id;
                    break;
                }
            }
        } else if (strnicmp(args, "FUNC=", 5) == 0) {
            /* Extract function name, look up LVO and lib_id.
             * Setting both filter_lvo AND filter_lib_id prevents
             * cross-library LVO collisions (e.g. exec.OpenDevice and
             * dos.MakeLink both have LVO -444). */
            char fname[32];
            int fi = 0;
            args += 5;
            while (*args && *args != ' ' && *args != '\t' &&
                   fi < (int)sizeof(fname) - 1)
                fname[fi++] = *args++;
            fname[fi] = '\0';

            for (fi = 0; fi < (int)FUNC_TABLE_SIZE; fi++) {
                if (stricmp(fname, func_table[fi].func_name) == 0) {
                    ts->filter_lvo = func_table[fi].lvo_offset;
                    ts->filter_lib_id = func_table[fi].lib_id;
                    break;
                }
            }
            /* If not found, filter_lvo stays 0 (match all) */
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

/* Check if an event matches a client's filter criteria.
 * All filters are AND-combined: all must match for the event to pass.
 * Returns 1 if event matches (should be sent), 0 if filtered out. */
static int trace_filter_match(struct trace_state *ts,
                               struct atrace_event *ev,
                               const char *task_name)
{
    /* LIB filter */
    if (ts->filter_lib_id >= 0 && ev->lib_id != ts->filter_lib_id)
        return 0;

    /* FUNC filter (by LVO + lib_id) */
    if (ts->filter_lvo != 0 && ev->lvo_offset != ts->filter_lvo)
        return 0;

    /* PROC filter (case-insensitive substring match on task name) */
    if (ts->filter_procname[0] != '\0') {
        if (stristr(task_name, ts->filter_procname) == NULL)
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
    case 1005: return "MODE_OLDFILE";
    case 1006: return "MODE_NEWFILE";
    case 1004: return "MODE_READWRITE";
    default:   return NULL;
    }
}

/* Format a Lock type value */
static const char *format_lock_type(LONG type)
{
    switch (type) {
    case -2: return "ACCESS_READ";
    case -1: return "ACCESS_WRITE";
    default:  return NULL;
    }
}

/* Format AllocMem requirements flags */
static void format_memf_flags(ULONG flags, char *buf, int bufsz)
{
    char *p = buf;
    int remaining = bufsz;

    buf[0] = '\0';

    if (flags & 0x00001) {
        p += snprintf(p, remaining, "MEMF_PUBLIC");
        remaining = bufsz - (int)(p - buf);
    }
    if (flags & 0x00002) {
        if (p != buf) { *p++ = '|'; remaining--; }
        p += snprintf(p, remaining, "MEMF_CHIP");
        remaining = bufsz - (int)(p - buf);
    }
    if (flags & 0x00004) {
        if (p != buf) { *p++ = '|'; remaining--; }
        p += snprintf(p, remaining, "MEMF_FAST");
        remaining = bufsz - (int)(p - buf);
    }
    if (flags & 0x10000) {
        if (p != buf) { *p++ = '|'; remaining--; }
        p += snprintf(p, remaining, "MEMF_CLEAR");
        remaining = bufsz - (int)(p - buf);
    }

    if (p == buf)
        snprintf(buf, bufsz, "0x%lx", (unsigned long)flags);
}

/* Generic argument formatter -- dispatches to per-function formatters */
static void format_args(struct atrace_event *ev,
                        const struct trace_func_entry *fe,
                        char *buf, int bufsz)
{
    int i;
    char *p = buf;
    int remaining = bufsz;

    buf[0] = '\0';

    if (!fe) {
        /* Unknown function -- dump raw args */
        for (i = 0; i < ev->arg_count && i < 4; i++) {
            if (i > 0) { *p++ = ','; remaining--; }
            p += snprintf(p, remaining, "0x%lx", (unsigned long)ev->args[i]);
            remaining = bufsz - (int)(p - buf);
        }
        return;
    }

    /* Per-function formatting based on lib_id and LVO */
    if (fe->lib_id == LIB_DOS && fe->lvo_offset == -30) {
        /* dos.Open: "name", mode */
        const char *mode_name = format_access_mode((LONG)ev->args[1]);
        p += snprintf(p, remaining, "\"%s\",%s",
                      ev->string_data,
                      mode_name ? mode_name : "?");
    } else if (fe->lib_id == LIB_DOS && fe->lvo_offset == -84) {
        /* dos.Lock: "name", type */
        const char *type_name = format_lock_type((LONG)ev->args[1]);
        p += snprintf(p, remaining, "\"%s\",%s",
                      ev->string_data,
                      type_name ? type_name : "?");
    } else if (fe->lib_id == LIB_EXEC && fe->lvo_offset == -552) {
        /* exec.OpenLibrary: "name", version */
        p += snprintf(p, remaining, "\"%s\",%lu",
                      ev->string_data,
                      (unsigned long)ev->args[1]);
    } else if (fe->lib_id == LIB_EXEC && fe->lvo_offset == -444) {
        /* exec.OpenDevice: "name", unit, ioReq, flags */
        p += snprintf(p, remaining, "\"%s\",%lu,0x%lx,%lu",
                      ev->string_data,
                      (unsigned long)ev->args[1],
                      (unsigned long)ev->args[2],
                      (unsigned long)ev->args[3]);
    } else if (fe->lib_id == LIB_EXEC && fe->lvo_offset == -198) {
        /* exec.AllocMem: size, memf flags */
        char flags_buf[64];
        format_memf_flags(ev->args[1], flags_buf, sizeof(flags_buf));
        p += snprintf(p, remaining, "%lu,%s",
                      (unsigned long)ev->args[0], flags_buf);
    } else if (ev->string_data[0] != '\0') {
        /* Has a string arg -- show it quoted, then remaining args as hex */
        p += snprintf(p, remaining, "\"%s\"", ev->string_data);
        remaining = bufsz - (int)(p - buf);
        for (i = 1; i < ev->arg_count && i < 4; i++) {
            p += snprintf(p, remaining, ",%lu",
                          (unsigned long)ev->args[i]);
            remaining = bufsz - (int)(p - buf);
        }
    } else {
        /* No string arg -- all hex */
        for (i = 0; i < ev->arg_count && i < 4; i++) {
            if (i > 0) { *p++ = ','; remaining--; }
            p += snprintf(p, remaining, "0x%lx",
                          (unsigned long)ev->args[i]);
            remaining = bufsz - (int)(p - buf);
        }
    }
}

/* Format return value with special handling for void functions */
static void format_retval(struct atrace_event *ev,
                           const struct trace_func_entry *fe,
                           char *buf, int bufsz)
{
    /* Void functions */
    if (fe) {
        if ((fe->lib_id == LIB_EXEC && fe->lvo_offset == -366) ||  /* PutMsg */
            (fe->lib_id == LIB_EXEC && fe->lvo_offset == -564) ||  /* ObtainSemaphore */
            (fe->lib_id == LIB_EXEC && fe->lvo_offset == -570)) {  /* ReleaseSemaphore */
            snprintf(buf, bufsz, "(void)");
            return;
        }
    }

    if (ev->retval == 0) {
        snprintf(buf, bufsz, "NULL");
    } else if (ev->retval == (ULONG)-1) {
        snprintf(buf, bufsz, "-1");
    } else {
        snprintf(buf, bufsz, "0x%08lx", (unsigned long)ev->retval);
    }
}

/* ---- Event formatting ---- */

/* Format a single trace event as a text line.
 *
 * Format:
 *   <seq>\t<time>\t<lib>.<func>\t<task>\t<args>\t<retval>
 *
 * The time field comes from DateStamp at poll time (not per-event).
 * The caller pre-computes the timestamp once per poll batch. */
static void trace_format_event(struct atrace_event *ev,
                                const char *timestr,
                                char *buf, int bufsz)
{
    const struct trace_func_entry *fe;
    const char *task_name;
    const char *lib_name;
    const char *func_name;
    static char args_buf[128];
    static char retval_buf[32];

    task_name = resolve_task_name(ev->caller_task);

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

    /* Format return value */
    format_retval(ev, fe, retval_buf, sizeof(retval_buf));

    snprintf(buf, bufsz, "%lu\t%s\t%s.%s\t%s\t%s\t%s",
             (unsigned long)ev->sequence,
             timestr,
             lib_name, func_name,
             task_name,
             args_buf,
             retval_buf);
}

/* ---- Send helper ---- */

static int send_trace_data_chunk(LONG fd, const char *line)
{
    return send_data_chunk(fd, line, strlen(line));
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
    int i;
    struct DateStamp ds;
    static char timestr[16];
    LONG hours, mins, secs, ticks_rem;
    const char *task_name;

    if (!g_anchor || !g_anchor->ring)
        return;

    /* Check if atrace is shutting down.
     * Try a shared obtain -- if it fails AND global_enable is 0,
     * atrace QUIT is in progress. */
    if (!AttemptSemaphoreShared(&g_anchor->sem)) {
        if (g_anchor->global_enable == 0) {
            /* Shutdown detected -- stop all trace sessions */
            for (i = 0; i < MAX_CLIENTS; i++) {
                c = &d->clients[i];
                if (c->fd >= 0 && c->trace.active) {
                    send_trace_data_chunk(c->fd, "# ATRACE SHUTDOWN");
                    send_end(c->fd);
                    send_sentinel(c->fd);
                    c->trace.active = 0;
                    c->trace.mode = TRACE_MODE_START;
                    c->trace.run_proc_slot = -1;
                    c->trace.run_task_ptr = NULL;
                }
            }
            g_anchor = NULL;
            g_ring_entries = NULL;
            g_events_dropped = 0;
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
                c = &d->clients[i];
                if (c->fd >= 0 && c->trace.active) {
                    send_trace_data_chunk(c->fd, "# ATRACE SHUTDOWN");
                    send_end(c->fd);
                    send_sentinel(c->fd);
                    c->trace.active = 0;
                    c->trace.mode = TRACE_MODE_START;
                    c->trace.run_proc_slot = -1;
                    c->trace.run_task_ptr = NULL;
                }
            }
            g_anchor = NULL;
            g_ring_entries = NULL;
            g_events_dropped = 0;
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

    while (batch < 64 && g_ring_entries[pos].valid) {
        ev = &g_ring_entries[pos];

        /* Resolve task name once per event (used by both formatting
         * and PROC filter matching) */
        task_name = resolve_task_name(ev->caller_task);

        /* Format event */
        trace_format_event(ev, timestr, trace_line_buf,
                           sizeof(trace_line_buf));

        /* Broadcast to all tracing clients (with per-client filtering) */
        for (i = 0; i < MAX_CLIENTS; i++) {
            c = &d->clients[i];
            if (c->fd < 0 || !c->trace.active)
                continue;
            /* TRACE RUN: exact Task pointer match */
            if (c->trace.mode == TRACE_MODE_RUN) {
                if (ev->caller_task != c->trace.run_task_ptr)
                    continue;
            }
            if (!trace_filter_match(&c->trace, ev, task_name))
                continue;
            if (send_trace_data_chunk(c->fd, trace_line_buf) < 0) {
                /* Mark for disconnect; main loop handles it */
                c->trace.active = 0;
                c->trace.mode = TRACE_MODE_START;
                c->trace.run_proc_slot = -1;
                c->trace.run_task_ptr = NULL;
            }
        }

        /* Release slot */
        ev->valid = 0;

        /* Advance consumer */
        pos = (pos + 1) % ring->capacity;
        ring->read_pos = pos;
        batch++;
    }

    /* Update lifetime counter */
    g_anchor->events_consumed += batch;

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
                send_trace_data_chunk(c->fd, trace_line_buf);
        }
    }

    ReleaseSemaphore(&g_anchor->sem);
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

    send_error(c->fd, ERR_SYNTAX, "Unknown TRACE subcommand");
    send_sentinel(c->fd);
    return 0;
}

/* ---- TRACE STATUS ---- */

static int trace_cmd_status(struct client *c)
{
    static char line[128];

    if (!trace_discover()) {
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
            }
            ReleaseSemaphore(&g_anchor->sem);
        }
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

    /* Check for atrace */
    if (!trace_discover()) {
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

    /* Enter streaming mode */
    c->trace.active = 1;

    /* Send OK -- no sentinel (streaming response) */
    send_ok(c->fd, NULL);
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

    /* Check for atrace */
    if (!trace_discover()) {
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
        NP_Name, (ULONG)"amigactld-exec",
        NP_StackSize, 16384,
        NP_Cli, TRUE,
        TAG_DONE);
    if (proc)
        g_daemon_state->procs[slot].task = (struct Task *)proc;
    Permit();

    if (!proc) {
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

    /* Parse trace filters (after successful process creation) */
    parse_filters(filter_buf, &c->trace);

    /* Enter TRACE RUN streaming mode */
    c->trace.mode = TRACE_MODE_RUN;
    c->trace.run_proc_slot = slot;
    c->trace.run_task_ptr = (APTR)g_daemon_state->procs[slot].task;
    c->trace.active = 1;

    sprintf(info, "%d", g_daemon_state->procs[slot].id);
    send_ok(c->fd, info);
    return 0;
}

/* ---- TRACE RUN completion check ---- */

void trace_check_run_completed(struct daemon_state *d)
{
    int i;
    struct client *c;
    struct tracked_proc *slot;
    static char comment[64];

    for (i = 0; i < MAX_CLIENTS; i++) {
        c = &d->clients[i];
        if (c->fd < 0 || !c->trace.active)
            continue;
        if (c->trace.mode != TRACE_MODE_RUN)
            continue;
        if (c->trace.run_proc_slot < 0)
            continue;

        slot = &d->procs[c->trace.run_proc_slot];

        if (slot->status != PROC_EXITED)
            continue;

        /* Send exit notification */
        sprintf(comment, "# PROCESS EXITED rc=%d", slot->rc);
        send_trace_data_chunk(c->fd, comment);
        send_end(c->fd);
        send_sentinel(c->fd);

        /* Clear trace state */
        c->trace.active = 0;
        c->trace.mode = TRACE_MODE_START;
        c->trace.run_proc_slot = -1;
        c->trace.run_task_ptr = NULL;
    }
}

/* ---- TRACE ENABLE / TRACE DISABLE ---- */

static int trace_cmd_enable(struct client *c, const char *args)
{
    if (!trace_discover()) {
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
    if (!trace_discover()) {
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
            /* Send END + sentinel, return to normal mode */
            send_end(c->fd);
            send_sentinel(c->fd);
            c->trace.active = 0;
            c->trace.mode = TRACE_MODE_START;
            c->trace.run_proc_slot = -1;
            c->trace.run_task_ptr = NULL;
            return 0;
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
