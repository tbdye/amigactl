/*
 * amigactld -- File streaming (Phase 4)
 *
 * TAIL: stream file appends to a client until STOP.
 */

#ifndef AMIGACTLD_TAIL_H
#define AMIGACTLD_TAIL_H

#include "daemon.h"

/* Allocate static FIB for tail operations.
 * Returns 0 on success, -1 on failure (AllocDosObject). */
int tail_init(void);

/* Free the static FIB. */
void tail_cleanup(void);

/* Command handler: start tailing a file.
 * Sends OK <current_size> and enters streaming mode.
 * Returns 0 always (errors sent inline). */
int cmd_tail(struct client *c, const char *args);

/* Called from event loop when a tail client has recv data.
 * Checks for STOP command, handles disconnect.
 * Returns 0 on success, -1 if client should be disconnected. */
int tail_handle_input(struct daemon_state *d, int idx);

/* Called from event loop every iteration for active tail clients.
 * Polls the file for new data and sends DATA chunks.
 * Returns 0 on success, -1 if client should be disconnected. */
int tail_poll_file(struct daemon_state *d, int idx);

#endif /* AMIGACTLD_TAIL_H */
