/*
 * amigactld -- Library call tracing
 *
 * TRACE STATUS, TRACE START [filters], TRACE STOP,
 * TRACE ENABLE, TRACE DISABLE.
 * Discovers atrace via named semaphore, polls ring buffer,
 * streams events to subscribed clients as DATA chunks.
 * Supports per-client filters: LIB, FUNC, PROC, ERRORS.
 */

#ifndef AMIGACTLD_TRACE_H
#define AMIGACTLD_TRACE_H

#include "daemon.h"

/* Initialize trace module.
 * Attempts to find atrace semaphore (not an error if missing).
 * Returns 0 always. */
int trace_init(void);

/* Cleanup trace module. Nothing to free (atrace owns all memory). */
void trace_cleanup(void);

/* Command handler for TRACE verb.
 * Dispatches to STATUS, START, ENABLE, DISABLE subcommands.
 * Uses (daemon_state*, idx, args) signature like cmd_arexx,
 * because trace needs daemon-wide state for broadcasting.
 * Returns 0 on success, -1 on send failure (disconnect client). */
int cmd_trace(struct daemon_state *d, int idx, const char *args);

/* Called from event loop when a trace-active client has recv data.
 * Checks for STOP command.
 * Returns 0 on success, -1 if client should be disconnected. */
int trace_handle_input(struct daemon_state *d, int idx);

/* Called from event loop once per iteration when any client has
 * trace.active == 1. Reads events from ring buffer, broadcasts
 * to all tracing clients. */
void trace_poll_events(struct daemon_state *d);

/* Returns 1 if any client has an active trace session. */
int trace_any_active(struct daemon_state *d);

#endif /* AMIGACTLD_TRACE_H */
