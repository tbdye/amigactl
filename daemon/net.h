/*
 * amigactld -- Socket helpers and protocol I/O
 */

#ifndef AMIGACTLD_NET_H
#define AMIGACTLD_NET_H

#include "daemon.h"

/* Open bsdsocket.library and register errno pointers.
 * Returns 0 on success, -1 on failure (diagnostic printed). */
int net_init(void);

/* Close bsdsocket.library. */
void net_cleanup(void);

/* Create a TCP listener socket on the given port.
 * Binds to INADDR_ANY with SO_REUSEADDR, listen backlog 5.
 * Returns fd on success, -1 on failure (diagnostic printed). */
LONG net_listen(int port);

/* Accept a connection on a listener socket.
 * Fills *peer_addr with the peer's IP in network byte order.
 * Returns the new fd on success, -1 on failure. */
LONG net_accept(LONG listener, ULONG *peer_addr);

/* Set a socket to non-blocking mode via IoctlSocket(FIONBIO).
 * Returns 0 on success, -1 on failure. */
int net_set_nonblocking(LONG fd);

/* Set a socket to blocking mode via IoctlSocket(FIONBIO).
 * Returns 0 on success, -1 on failure. */
int net_set_blocking(LONG fd);

/* Close a socket if fd >= 0. */
void net_close(LONG fd);

/* ---- Protocol I/O ---- */

/* Send a string followed by \n.  Loops on partial send().
 * Returns 0 on success, -1 on error. */
int send_line(LONG fd, const char *line);

/* Send "OK\n" (info==NULL) or "OK <info>\n" (info!=NULL).
 * Does NOT send sentinel -- caller must follow with payload
 * lines (if any) and then send_sentinel(). */
int send_ok(LONG fd, const char *info);

/* Send "ERR <code> <message>\n".
 * Does NOT send sentinel -- caller must follow with send_sentinel(). */
int send_error(LONG fd, int code, const char *message);

/* Send the connection banner: "AMIGACTL <version>\n". */
int send_banner(LONG fd);

/* Send a payload line with dot-stuffing.
 * If line starts with '.', prepends an extra '.'.
 * Appends \n. Returns 0 on success, -1 on error. */
int send_payload_line(LONG fd, const char *line);

/* Send the sentinel: ".\n".
 * Every command handler must call this as its final action. */
int send_sentinel(LONG fd);

/* Send exactly len bytes, looping on partial send().
 * Returns 0 on success, -1 on error. */
int send_all(LONG fd, const char *buf, int len);

/* Send a DATA chunk: "DATA <len>\n" header followed by exactly len
 * raw bytes.  Returns 0 on success, -1 on error. */
int send_data_chunk(LONG fd, const char *data, int len);

/* Send "END\n".  Returns 0 on success, -1 on error. */
int send_end(LONG fd);

/* Send "READY\n".  Returns 0 on success, -1 on error. */
int send_ready(LONG fd);

/* Receive data into a client's recv_buf at offset recv_len.
 * Returns bytes received (>0), 0 for EOF, -1 for error. */
int recv_into_buf(struct client *c);

/* Extract a complete command from a client's recv_buf.
 * Scans for \n, strips trailing \r, copies into cmd (NUL-terminated),
 * shifts remaining data in recv_buf.
 * Returns 1 if a command was extracted, 0 if no complete line yet,
 * -1 on overflow (buffer full with no \n -- sets c->discarding). */
int extract_command(struct client *c, char *cmd, int cmd_max);

/* Receive exactly len bytes, first draining c->recv_buf, then from
 * the socket.  Returns 0 on success, -1 on error/EOF. */
int recv_exact_from_client(struct client *c, char *buf, int len);

/* Block until a complete line is available in c->recv_buf, extract it
 * into cmd (NUL-terminated).  Calls extract_command() before recv to
 * avoid deadlock when data is already buffered.
 * Returns 0 on success, -1 on error/EOF/overflow. */
int recv_line_blocking(struct client *c, char *cmd, int cmd_max);

#endif /* AMIGACTLD_NET_H */
