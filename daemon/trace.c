/*
 * amigactld -- Library call tracing
 *
 * Implements TRACE STATUS and TRACE START/STOP streaming.
 * Follows the TAIL module pattern (tail.c).
 */

#include "trace.h"
#include "daemon.h"
#include "net.h"
#include "../atrace/atrace.h"

#include <proto/exec.h>
#include <proto/dos.h>

#include <stdio.h>
#include <string.h>

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

/* Function name lookup -- Phase 1 has only OpenLibrary */
static const char *phase1_func_name = "OpenLibrary";
static const char *phase1_lib_name = "exec";

/* ---- Forward declarations ---- */

static int trace_discover(void);
static const char *resolve_task_name(APTR task_ptr);
static void trace_format_event(struct atrace_event *ev,
                                const char *timestr,
                                char *buf, int bufsz);
static int send_trace_data_chunk(LONG fd, const char *line);
static int trace_cmd_status(struct client *c);
static int trace_cmd_start(struct daemon_state *d, int idx);

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

/* ---- Task name resolution ---- */

/* Resolve a Task pointer to a name string.
 * Uses Forbid/Permit to safely read the task name.
 * Returns the name in a static buffer, or "<task 0xHHHHHHHH>" if
 * the task is no longer in the system task lists.
 *
 * Phase 1 simplification: always reads tc_Node.ln_Name under Forbid.
 * This is safe because Forbid prevents the task from exiting while
 * we read the name. A full implementation would use a cached table
 * refreshed periodically.
 *
 * PHASE 1 LIMITATION: This function directly dereferences the Task
 * pointer from the ring buffer event. If the task has exited between
 * event production and consumption, this is a use-after-free. Under
 * Forbid(), FreeMem completions from other tasks are blocked, so the
 * risk is low on single-CPU 68k. However, interrupt handlers could
 * theoretically reallocate the freed memory.
 *
 * Phase 2 MUST replace this with a cached task-name table populated
 * by walking the system task lists. Stale entries should display as
 * "<task 0xHHHHHHHH>".
 */
static const char *resolve_task_name(APTR task_ptr)
{
    static char name_buf[64];
    struct Task *task = (struct Task *)task_ptr;
    char *name;

    if (!task_ptr) {
        return "<null>";
    }

    Forbid();
    /* Under Forbid, no task can be removed from the system lists,
     * so task->tc_Node.ln_Name is safe to read. */
    name = (char *)task->tc_Node.ln_Name;
    if (name) {
        strncpy(name_buf, name, sizeof(name_buf) - 1);
        name_buf[sizeof(name_buf) - 1] = '\0';
    } else {
        sprintf(name_buf, "<task 0x%08lx>", (unsigned long)task_ptr);
    }
    Permit();

    return name_buf;
}

/* ---- Event formatting ---- */

/* Format a single trace event as a text line.
 *
 * Phase 1 format (OpenLibrary only):
 *   <seq>\t<time>\texec.OpenLibrary\t<task>\t"<libname>",<version>\t<retval>
 *
 * The time field comes from DateStamp at poll time (not per-event).
 * Phase 1 passes a pre-formatted time string.
 *
 * Phase 1 adds a timestr parameter to avoid computing DateStamp per-event
 * -- the caller pre-computes the timestamp once per poll batch.
 */
static void trace_format_event(struct atrace_event *ev,
                                const char *timestr,
                                char *buf, int bufsz)
{
    const char *task_name;
    const char *retval_str;
    char retval_buf[16];

    task_name = resolve_task_name(ev->caller_task);

    /* Return value formatting */
    if (ev->retval == 0) {
        retval_str = "NULL";
    } else {
        sprintf(retval_buf, "0x%08lx", (unsigned long)ev->retval);
        retval_str = retval_buf;
    }

    /* Phase 1: only OpenLibrary, so lib.func is always exec.OpenLibrary.
     * args[0] = libName (string in string_data), args[1] = version. */
    snprintf(buf, bufsz,
             "%lu\t%s\t%s.%s\t%s\t\"%s\",%lu\t%s",
             (unsigned long)ev->sequence,
             timestr,
             phase1_lib_name,
             phase1_func_name,
             task_name,
             ev->string_data,
             (unsigned long)ev->args[1],
             retval_str);
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

        /* Format event */
        trace_format_event(ev, timestr, trace_line_buf,
                           sizeof(trace_line_buf));

        /* Broadcast to all tracing clients */
        for (i = 0; i < MAX_CLIENTS; i++) {
            c = &d->clients[i];
            if (c->fd < 0 || !c->trace.active)
                continue;
            /* Phase 1: no filters -- send everything */
            if (send_trace_data_chunk(c->fd, trace_line_buf) < 0) {
                /* Mark for disconnect; main loop handles it */
                c->trace.active = 0;
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
        /* Phase 2 will use remaining args for filter parsing */
    }

    if (stricmp(sub, "STATUS") == 0) {
        return trace_cmd_status(c);
    }

    if (stricmp(sub, "START") == 0) {
        return trace_cmd_start(d, idx);
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

    send_sentinel(c->fd);
    return 0;
}

/* ---- TRACE START ---- */

static int trace_cmd_start(struct daemon_state *d, int idx)
{
    struct client *c = &d->clients[idx];

    /* Mutual exclusion with TAIL */
    if (c->tail.active) {
        send_error(c->fd, ERR_INTERNAL, "TAIL session active");
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

    /* Enter streaming mode */
    c->trace.active = 1;

    /* Send OK -- no sentinel (streaming response) */
    send_ok(c->fd, NULL);
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
