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
#define ERR_CHECK_NULL      0   /* error when retval == 0 (NULL/FALSE) -- most functions */
#define ERR_CHECK_NZERO     1   /* error when retval != 0 (OpenDevice: 0=success) */
#define ERR_CHECK_VOID      2   /* void function -- never shown in ERRORS mode */
#define ERR_CHECK_ANY       3   /* no clear error convention -- always show */
#define ERR_CHECK_NONE      4   /* never an error (GetMsg NULL is normal) */
#define ERR_CHECK_RC        5   /* return code: error when rc != 0 */
#define ERR_CHECK_NEGATIVE  6   /* error when (LONG)retval < 0 (GetVar: -1=fail, >=0=count) */

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
    /* exec.library functions (12) */
    { "exec", "FindPort",         LIB_EXEC, -390, ERR_CHECK_NULL,  1, RET_PTR      },
    { "exec", "FindResident",     LIB_EXEC,  -96, ERR_CHECK_NULL,  1, RET_PTR      },
    { "exec", "FindSemaphore",    LIB_EXEC, -594, ERR_CHECK_NULL,  1, RET_PTR      },
    { "exec", "FindTask",         LIB_EXEC, -294, ERR_CHECK_NULL,  1, RET_PTR      },
    { "exec", "OpenDevice",       LIB_EXEC, -444, ERR_CHECK_NZERO, 1, RET_NZERO_ERR},
    { "exec", "OpenLibrary",      LIB_EXEC, -552, ERR_CHECK_NULL,  1, RET_PTR      },
    { "exec", "OpenResource",     LIB_EXEC, -498, ERR_CHECK_NULL,  1, RET_PTR      },
    { "exec", "GetMsg",           LIB_EXEC, -372, ERR_CHECK_NONE,  0, RET_MSG_PTR  },
    { "exec", "PutMsg",           LIB_EXEC, -366, ERR_CHECK_VOID,  0, RET_VOID     },
    { "exec", "ObtainSemaphore",  LIB_EXEC, -564, ERR_CHECK_VOID,  0, RET_VOID     },
    { "exec", "ReleaseSemaphore", LIB_EXEC, -570, ERR_CHECK_VOID,  0, RET_VOID     },
    { "exec", "AllocMem",         LIB_EXEC, -198, ERR_CHECK_NULL,  0, RET_PTR      },
    /* dos.library functions (18) */
    { "dos", "Open",              LIB_DOS,   -30, ERR_CHECK_NULL,  1, RET_PTR      },
    { "dos", "Close",             LIB_DOS,   -36, ERR_CHECK_NULL,  0, RET_BOOL_DOS },
    { "dos", "Lock",              LIB_DOS,   -84, ERR_CHECK_NULL,  1, RET_LOCK     },
    { "dos", "DeleteFile",        LIB_DOS,   -72, ERR_CHECK_NULL,  1, RET_BOOL_DOS },
    { "dos", "Execute",           LIB_DOS,  -222, ERR_CHECK_NULL,  1, RET_BOOL_DOS },
    { "dos", "GetVar",            LIB_DOS,  -906, ERR_CHECK_NEGATIVE, 1, RET_LEN   },
    { "dos", "FindVar",           LIB_DOS,  -918, ERR_CHECK_NULL,  1, RET_PTR      },
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
    { "dos", "CurrentDir",        LIB_DOS,  -126, ERR_CHECK_VOID,  0, RET_OLD_LOCK },
};

#define FUNC_TABLE_SIZE  (sizeof(func_table) / sizeof(func_table[0]))

/* ---- Module globals ---- */

/* Cached anchor pointer -- NULL if atrace not found */
static struct atrace_anchor *g_anchor = NULL;

/* Cached ring entries pointer (avoids repeated offset math) */
static struct atrace_event *g_ring_entries = NULL;

/* Running total of dropped events (from ring->overflow) */
static ULONG g_events_dropped = 0;
static ULONG g_poll_count = 0;

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

/* ---- Noise function table ---- */

/* Noise function names -- auto-enabled when filter_task is set,
 * restored when filter_task is cleared.
 *
 * MUST match the noise_func_names table in atrace/main.c exactly.
 * If a name is misspelled here, it will silently fail to match
 * and that function will not be auto-enabled during TRACE RUN. */
static const char *noise_func_names[] = {
    "FindPort",
    "FindSemaphore",
    "FindTask",
    "GetMsg",
    "PutMsg",
    "ObtainSemaphore",
    "ReleaseSemaphore",
    "AllocMem",
    NULL
};

#define MAX_NOISE_FUNCS 16  /* room for growth in future phases */

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
static int trace_auto_load(void);
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
static char format_retval(struct atrace_event *ev,
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
static void trace_run_cleanup(struct client *c);

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
                LONG cli_num = pr->pr_TaskNum;
                if (cli_num > 0) {
                    snprintf(task_cache[idx].name, 64, "[%ld] %s",
                             (long)cli_num, node->ln_Name);
                } else {
                    strncpy(task_cache[idx].name, node->ln_Name, 63);
                    task_cache[idx].name[63] = '\0';
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
                LONG cli_num = pr->pr_TaskNum;
                if (cli_num > 0) {
                    snprintf(task_cache[idx].name, 64, "[%ld] %s",
                             (long)cli_num, node->ln_Name);
                } else {
                    strncpy(task_cache[idx].name, node->ln_Name, 63);
                    task_cache[idx].name[63] = '\0';
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
                LONG cli_num = pr->pr_TaskNum;
                if (cli_num > 0) {
                    snprintf(task_cache[idx].name, 64, "[%ld] %s",
                             (long)cli_num, this_task->tc_Node.ln_Name);
                } else {
                    strncpy(task_cache[idx].name,
                            this_task->tc_Node.ln_Name, 63);
                    task_cache[idx].name[63] = '\0';
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
        /* Check if this is a Process with a CLI number */
        if (task->tc_Node.ln_Type == NT_PROCESS) {
            struct Process *pr = (struct Process *)task;
            LONG cli_num = pr->pr_TaskNum;
            if (cli_num > 0) {
                snprintf(fallback, sizeof(fallback), "[%ld] %s",
                         (long)cli_num, name);
            } else {
                strncpy(fallback, name, sizeof(fallback) - 1);
                fallback[sizeof(fallback) - 1] = '\0';
            }
        } else {
            strncpy(fallback, name, sizeof(fallback) - 1);
            fallback[sizeof(fallback) - 1] = '\0';
        }
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
             * dos.MakeLink both have LVO -444). */
            char fname[32];
            int fi = 0;
            int found = 0;
            args += 5;
            while (*args && *args != ' ' && *args != '\t' &&
                   fi < (int)sizeof(fname) - 1)
                fname[fi++] = *args++;
            fname[fi] = '\0';

            for (fi = 0; fi < (int)FUNC_TABLE_SIZE; fi++) {
                if (stricmp(fname, func_table[fi].func_name) == 0) {
                    ts->filter_lvo = func_table[fi].lvo_offset;
                    ts->filter_lib_id = func_table[fi].lib_id;
                    found = 1;
                    break;
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
        if (p != buf) { *p++ = '|'; remaining--; } \
        p += snprintf(p, remaining, name); \
        remaining = bufsz - (int)(p - buf); \
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
        if (p != buf) { *p++ = '|'; remaining--; }
        snprintf(p, remaining, "0x%lx", (unsigned long)(flags & ~known));
    }
}

/* Check if a string_data value was likely truncated.
 * string_data is 24 bytes; the stub copies at most 23 chars
 * (leaving room for NUL). If strlen == 23, truncation likely. */
static int string_likely_truncated(const char *s)
{
    return (strlen(s) >= 23);
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
 * The cache is small (32 entries) and uses FIFO eviction.
 * Lock values are opaque 32-bit integers (BPTRs shifted << 2). */
#define LOCK_CACHE_SIZE  32

struct lock_cache_entry {
    ULONG lock_val;     /* retval from Lock/CreateDir */
    char  path[64];     /* path string */
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

/* Generic argument formatter -- dispatches to per-function formatters */
static void format_args(struct atrace_event *ev,
                        const struct trace_func_entry *fe,
                        char *buf, int bufsz)
{
    int i;
    char *p = buf;
    int remaining = bufsz;
    const char *trunc;

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

    trunc = (fe->has_string && string_likely_truncated(ev->string_data))
            ? "..." : "";

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
            p += snprintf(p, remaining, "\"%s%s\",unit=%lu,0x%lx,0x%lx",
                          ev->string_data, trunc,
                          (unsigned long)ev->args[1],
                          (unsigned long)ev->args[2],
                          (unsigned long)ev->args[3]);
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
            p += snprintf(p, remaining, "port=0x%lx",
                          (unsigned long)ev->args[0]);
            return;

        case -366:  /* PutMsg(port, msg) */
            p += snprintf(p, remaining, "port=0x%lx,msg=0x%lx",
                          (unsigned long)ev->args[0],
                          (unsigned long)ev->args[1]);
            return;

        case -564:  /* ObtainSemaphore(sem) */
            p += snprintf(p, remaining, "sem=0x%lx",
                          (unsigned long)ev->args[0]);
            return;

        case -570:  /* ReleaseSemaphore(sem) */
            p += snprintf(p, remaining, "sem=0x%lx",
                          (unsigned long)ev->args[0]);
            return;

        case -198:  /* AllocMem(byteSize, requirements) */
        {
            char flags_buf[64];
            format_memf_flags(ev->args[1], flags_buf, sizeof(flags_buf));
            p += snprintf(p, remaining, "%lu,%s",
                          (unsigned long)ev->args[0], flags_buf);
            return;
        }

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
            p += snprintf(p, remaining, "fh=0x%lx",
                          (unsigned long)ev->args[0]);
            return;

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
            /* Show NULL for zero file handles instead of 0x0 */
            if (ev->args[1] == 0 && ev->args[2] == 0)
                p += snprintf(p, remaining, "\"%s%s\",in=NULL,out=NULL",
                              ev->string_data, trunc);
            else if (ev->args[1] == 0)
                p += snprintf(p, remaining, "\"%s%s\",in=NULL,out=0x%lx",
                              ev->string_data, trunc,
                              (unsigned long)ev->args[2]);
            else if (ev->args[2] == 0)
                p += snprintf(p, remaining, "\"%s%s\",in=0x%lx,out=NULL",
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
            p += snprintf(p, remaining, "\"%s%s\",buf=0x%lx,%lu,%s",
                          ev->string_data, trunc,
                          (unsigned long)ev->args[1],
                          (unsigned long)ev->args[2],
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
            p += snprintf(p, remaining, "\"%s%s\",tags=0x%lx",
                          ev->string_data, trunc,
                          (unsigned long)ev->args[1]);
            return;

        case -120:  /* CreateDir(name) */
            p += snprintf(p, remaining, "\"%s%s\"",
                          ev->string_data, trunc);
            return;

        case -444:  /* MakeLink(name, dest, soft) */
            p += snprintf(p, remaining, "\"%s%s\",dest=0x%lx,%s",
                          ev->string_data, trunc,
                          (unsigned long)ev->args[1],
                          ev->args[2] ? "soft" : "hard");
            return;

        case -78:   /* Rename(oldName, newName) */
            /* Only arg0 (oldName) captured as string_data.
             * arg1 (newName) is a pointer -- only one string fits in the event. */
            p += snprintf(p, remaining, "\"%s%s\",new=0x%lx",
                          ev->string_data, trunc,
                          (unsigned long)ev->args[1]);
            return;

        case -504:  /* RunCommand(seg, stack, paramptr, paramlen) */
            p += snprintf(p, remaining, "seg=0x%lx,stack=%lu,params=0x%lx,%lu",
                          (unsigned long)ev->args[0],
                          (unsigned long)ev->args[1],
                          (unsigned long)ev->args[2],
                          (unsigned long)ev->args[3]);
            return;

        case -900:  /* SetVar(name, buffer, size, flags) */
        {
            const char *scope;
            ULONG f = ev->args[3];
            if (f & 0x100)      scope = "GLOBAL";
            else if (f & 0x200) scope = "LOCAL";
            else                scope = "ANY";
            p += snprintf(p, remaining, "\"%s%s\",buf=0x%lx,%lu,%s",
                          ev->string_data, trunc,
                          (unsigned long)ev->args[1],
                          (unsigned long)ev->args[2],
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
            p += snprintf(p, remaining, "\"%s%s\",tags=0x%lx",
                          ev->string_data, trunc,
                          (unsigned long)ev->args[1]);
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
                if (path)
                    p += snprintf(p, remaining, "\"%s\"", path);
                else
                    p += snprintf(p, remaining, "lock=0x%lx",
                                  (unsigned long)ev->args[0]);
            }
            return;

        }  /* end dos switch */
    }

    /* Fallback: should not reach here for known functions,
     * but handle gracefully */
    if (fe->has_string && ev->string_data[0] != '\0') {
        p += snprintf(p, remaining, "\"%s%s\"", ev->string_data, trunc);
        remaining = bufsz - (int)(p - buf);
        for (i = 1; i < ev->arg_count && i < 4; i++) {
            p += snprintf(p, remaining, ",%lu",
                          (unsigned long)ev->args[i]);
            remaining = bufsz - (int)(p - buf);
        }
    } else {
        for (i = 0; i < ev->arg_count && i < 4; i++) {
            if (i > 0) { *p++ = ','; remaining--; }
            p += snprintf(p, remaining, "0x%lx",
                          (unsigned long)ev->args[i]);
            remaining = bufsz - (int)(p - buf);
        }
    }
}

/* Format return value with per-function semantics.
 * Writes the formatted retval string to buf, and returns a status
 * character ('O', 'E', or '-') for the wire protocol. */
static char format_retval(struct atrace_event *ev,
                           const struct trace_func_entry *fe,
                           char *buf, int bufsz)
{
    ULONG rv = ev->retval;
    LONG srv = (LONG)rv;  /* signed interpretation */

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
        return '-';

    case RET_PTR:
        /* Pointer: NULL=fail, non-zero=hex addr */
        if (rv == 0) {
            snprintf(buf, bufsz, "NULL");
            return 'E';
        }
        snprintf(buf, bufsz, "0x%08lx", (unsigned long)rv);
        return 'O';

    case RET_BOOL_DOS:
        /* DOS boolean: non-zero=success, 0=fail.
         * DOSTRUE is defined as -1, but some DOS functions return
         * other non-zero values for success (e.g. Execute). Apps
         * check with if(result), not if(result == DOSTRUE). */
        if (rv == 0) {
            snprintf(buf, bufsz, "FAIL");
            return 'E';
        }
        snprintf(buf, bufsz, "OK");
        return 'O';

    case RET_NZERO_ERR:
        /* 0=success, non-zero=error code (OpenDevice) */
        if (rv == 0) {
            snprintf(buf, bufsz, "OK");
            return 'O';
        }
        snprintf(buf, bufsz, "err=%ld", (long)srv);
        return 'E';

    case RET_MSG_PTR:
        /* Message pointer: NULL=empty (normal), non-zero=addr */
        if (rv == 0) {
            snprintf(buf, bufsz, "(empty)");
            return '-';
        }
        snprintf(buf, bufsz, "0x%08lx", (unsigned long)rv);
        return 'O';

    case RET_RC:
        /* Return code: signed decimal, 0=success */
        snprintf(buf, bufsz, "rc=%ld", (long)srv);
        if (srv == 0)
            return 'O';
        return 'E';

    case RET_LOCK:
        /* BPTR lock: NULL=fail, non-zero=hex addr */
        if (rv == 0) {
            snprintf(buf, bufsz, "NULL");
            return 'E';
        }
        snprintf(buf, bufsz, "0x%08lx", (unsigned long)rv);
        return 'O';

    case RET_LEN:
        /* Byte count: -1=fail, >=0=decimal count */
        if (srv == -1) {
            snprintf(buf, bufsz, "-1");
            return 'E';
        }
        snprintf(buf, bufsz, "%ld", (long)srv);
        return 'O';

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
        return '-';

    default:
        /* Fallback */
        if (rv == 0)
            snprintf(buf, bufsz, "NULL");
        else
            snprintf(buf, bufsz, "0x%08lx", (unsigned long)rv);
        return '-';
    }
}

/* ---- Event formatting ---- */

/* Format a single trace event as a text line.
 *
 * Format (7 tab-separated fields):
 *   <seq>\t<time>\t<lib>.<func>\t<task>\t<args>\t<retval>\t<status>
 *
 * The status field is a single character from format_retval():
 *   'O' = OK (success), 'E' = Error, '-' = Neutral/void.
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
    char status;

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
    }

    snprintf(buf, bufsz, "%lu\t%s\t%s.%s\t%s\t%s\t%s\t%c",
             (unsigned long)ev->sequence,
             timestr,
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
                c = &d->clients[i];
                if (c->fd >= 0 && c->trace.active) {
                    send_trace_data_chunk(c->fd, "# ATRACE SHUTDOWN");
                    send_end(c->fd);
                    send_sentinel(c->fd);
                    /* Restore filter_task and noise before clearing state.
                     * g_anchor is still valid at this point. */
                    trace_run_cleanup(c);
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
                    /* Restore filter_task and noise before clearing state.
                     * g_anchor is still valid at this point. */
                    trace_run_cleanup(c);
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
            /* TRACE RUN: exact Task pointer match + skip stale events */
            if (c->trace.mode == TRACE_MODE_RUN) {
                if (ev->caller_task != c->trace.run_task_ptr)
                    continue;
                if (ev->sequence < c->trace.run_start_seq)
                    continue;
            }
            if (!trace_filter_match(&c->trace, ev, task_name))
                continue;
            if (send_trace_data_chunk(c->fd, trace_line_buf) < 0) {
                /* Restore filter_task/noise, then disconnect
                 * immediately so stale data can't arrive on
                 * the next event loop iteration. */
                trace_run_cleanup(c);
                net_close(c->fd);
                c->fd = -1;
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

    /* Phase 4: filter_task status */
    if (g_anchor->version >= 2) {
        snprintf(line, sizeof(line), "filter_task=0x%08lx",
                 (unsigned long)g_anchor->filter_task);
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

    /* Clear lock-to-path cache for the new session */
    lock_cache_clear();

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

    /* Clear lock-to-path cache for the new session.
     * Must be before process creation -- a timer interrupt between
     * Permit() and a later clear could let the new process call
     * Lock(), caching with stale session data. */
    lock_cache_clear();

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

    /* Look up noise function patch indices (before Forbid) */
    {
        int ni = 0;
        const char **np;
        for (np = noise_func_names; *np && ni < MAX_NOISE_FUNCS; np++) {
            int pidx = find_patch_index_by_name(*np);
            if (pidx >= 0 && pidx < (int)g_anchor->patch_count) {
                c->trace.noise_patch_indices[ni] = pidx;
                ni++;
            }
        }
        c->trace.noise_saved_count = ni;
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

    /* Set filter_task and auto-enable noise only if we have exclusive
     * ownership of the stub-level filter.
     *
     * Design note: the filter_task field is a single global value in
     * the anchor struct. Only one TRACE RUN can use stub-level
     * filtering at a time. If another TRACE RUN is already active
     * (filter_task != NULL), we skip the filter_task write and the
     * noise auto-enable, falling back to daemon-side filtering only
     * (the existing run_task_ptr check in trace_poll_events). The
     * ring buffer may overflow in this case, same as Phase 3.
     *
     * Noise auto-enable without stub-level filtering would immediately
     * overflow the ring buffer (high-frequency functions producing
     * 10K+ events/sec system-wide with no task filter to limit them),
     * so noise save/enable is gated on filter_task ownership. */
    if (g_anchor->version >= 2 && g_anchor->filter_task == NULL) {
        int ni;
        /* Save noise enable states and enable all noise functions */
        for (ni = 0; ni < c->trace.noise_saved_count; ni++) {
            int pidx = c->trace.noise_patch_indices[ni];
            c->trace.noise_saved_enabled[ni] =
                g_anchor->patches[pidx].enabled;
            g_anchor->patches[pidx].enabled = 1;
        }
        c->trace.noise_saved = 1;
        g_anchor->filter_task = (APTR)proc;
    } else {
        /* Another TRACE RUN or manual filter is active, or anchor
         * version < 2. Fall back to daemon-side filtering only. */
        c->trace.noise_saved = 0;
    }

    /* Capture event_sequence under Forbid() -- the new process cannot
     * run until Permit(), so this value is guaranteed to precede any
     * events from the traced process. */
    c->trace.run_start_seq = g_anchor->event_sequence;

    Permit();

    /* Parse trace filters (after successful process creation) */
    parse_filters(filter_buf, &c->trace);

    /* Enter TRACE RUN streaming mode. */
    c->trace.mode = TRACE_MODE_RUN;
    c->trace.run_proc_slot = slot;
    c->trace.run_task_ptr = (APTR)g_daemon_state->procs[slot].task;
    c->trace.active = 1;

    sprintf(info, "%d", g_daemon_state->procs[slot].id);
    send_ok(c->fd, info);
    return 0;
}

/* ---- TRACE RUN cleanup helper ---- */

/* Restore noise function enable states and clear filter_task.
 * Called when TRACE RUN ends (process exit, STOP, disconnect,
 * send failure, or atrace shutdown).
 *
 * Uses noise_saved as the trigger (not mode), so this is safe
 * to call after trace.mode has already been cleared. noise_saved
 * is only set to 1 when we successfully took ownership of
 * filter_task in trace_cmd_run(). */
static void trace_run_cleanup(struct client *c)
{
    /* Restore noise enable states */
    if (c->trace.noise_saved && g_anchor) {
        int ni;
        for (ni = 0; ni < c->trace.noise_saved_count; ni++) {
            int pidx = c->trace.noise_patch_indices[ni];
            if (pidx >= 0 && pidx < (int)g_anchor->patch_count)
                g_anchor->patches[pidx].enabled =
                    c->trace.noise_saved_enabled[ni];
        }
        c->trace.noise_saved = 0;

        /* Clear task filter (we definitely own it if noise was saved) */
        if (g_anchor->version >= 2)
            g_anchor->filter_task = NULL;
    } else if (g_anchor && g_anchor->version >= 2 &&
               c->trace.run_task_ptr != NULL &&
               g_anchor->filter_task == c->trace.run_task_ptr) {
        /* No noise was saved, but filter_task matches our task --
         * clear it defensively to prevent stuck filter_task from
         * edge cases where noise_saved was cleared without clearing
         * filter_task. */
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
    /* Trigger cleanup if noise was saved (we owned filter_task) OR
     * if this client has an active TRACE RUN (catches cases where
     * noise_saved is 0 but filter_task still needs clearing). */
    if (c->trace.noise_saved ||
        (c->trace.active && c->trace.mode == TRACE_MODE_RUN)) {
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

        /* Final drain: read remaining target events from the ring
         * buffer BEFORE cleanup clears filter_task.  The stubs may
         * have written events that trace_poll_events() hasn't
         * consumed yet (the process exited between poll cycles).
         *
         * Non-target events are broadcast to other active TRACE START
         * clients, matching the pattern in trace_poll_events(). */
        if (g_anchor && g_anchor->ring && g_ring_entries) {
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
                run_client_ok = 1;  /* Track send status for TRACE RUN client */

                /* Drain remaining events, bounded by ring capacity
                 * (can't have more valid entries than capacity). */
                while (batch < (int)ring->capacity &&
                       g_ring_entries[pos].valid) {
                    ev = &g_ring_entries[pos];
                    task_name = resolve_task_name(ev->caller_task);
                    trace_format_event(ev, drain_timestr,
                                       trace_line_buf,
                                       sizeof(trace_line_buf));

                    /* Send target-task events to the TRACE RUN client */
                    if (run_client_ok &&
                        ev->caller_task == c->trace.run_task_ptr &&
                        ev->sequence >= c->trace.run_start_seq) {
                        if (trace_filter_match(&c->trace, ev, task_name)) {
                            if (send_trace_data_chunk(c->fd, trace_line_buf) < 0)
                                run_client_ok = 0;  /* Client disconnected */
                        }
                    }

                    /* Broadcast to other active TRACE START/RUN clients,
                     * matching trace_poll_events() broadcast pattern. */
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
                        if (!trace_filter_match(&oc->trace, ev, task_name))
                            continue;
                        if (send_trace_data_chunk(oc->fd, trace_line_buf) < 0) {
                            trace_run_cleanup(oc);
                            net_close(oc->fd);
                            oc->fd = -1;
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
            send_trace_data_chunk(c->fd, comment);
            send_end(c->fd);
            send_sentinel(c->fd);
        } else {
            /* Client disconnected during drain; close and clean up */
            net_close(c->fd);
            c->fd = -1;
        }

        /* Restore noise states, clear filter_task, clear trace state */
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
            /* Restore noise states, clear filter_task, clear trace state */
            trace_run_cleanup(c);
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
