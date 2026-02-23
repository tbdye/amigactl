/*
 * amigactld -- ARexx command dispatch
 *
 * AREXX: send commands to named ARexx ports, non-blocking.
 * Reply matching via signal-driven message port.
 */

#ifndef AMIGACTLD_AREXX_H
#define AMIGACTLD_AREXX_H

#include "daemon.h"

/* Open rexxsyslib.library, create reply port, set g_arexx_sigbit.
 * Returns 0 always (library or port failure is non-fatal; AREXX commands
 * will return ERR 500). */
int arexx_init(void);

/* Close library, delete port if no messages outstanding. */
void arexx_cleanup(void);

/* Drain outstanding ARexx replies (up to 10s), then clean up.
 * Must be called before arexx_cleanup() during shutdown. */
void arexx_shutdown_wait(struct daemon_state *d);

/* Command handler: dispatch an ARexx message to a named port.
 * Response is deferred -- sent asynchronously when reply arrives.
 * Returns 0 always (errors are sent inline before returning). */
int cmd_arexx(struct daemon_state *d, int client_idx, const char *args);

/* Called from event loop when reply signal fires.
 * Processes all pending GetMsg replies. */
void arexx_handle_replies(struct daemon_state *d);

/* Called every event loop iteration.  Times out slots that have
 * been pending longer than AREXX_TIMEOUT_SECS. */
void arexx_check_timeouts(struct daemon_state *d);

/* Called when a client disconnects.  Marks any pending ARexx
 * slots for that client as orphaned (reply consumed silently). */
void arexx_orphan_client(struct daemon_state *d, int client_idx);

/* Signal bit for ARexx reply port, -1 if unavailable */
extern LONG g_arexx_sigbit;

#endif /* AMIGACTLD_AREXX_H */
