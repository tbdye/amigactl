/*
 * amigactld -- Amiga remote access daemon
 *
 * Central header: constants, error codes, structures.
 */

#ifndef AMIGACTLD_DAEMON_H
#define AMIGACTLD_DAEMON_H

#include <exec/types.h>

/* Version string -- single source of truth */
#define AMIGACTLD_VERSION "0.1.0"

/* Limits */
#define MAX_CLIENTS      8
#define MAX_ACL_ENTRIES  16
#define MAX_CMD_LEN      4096
#define RECV_BUF_SIZE    4097  /* MAX_CMD_LEN + LF terminator */
#define DEFAULT_PORT     6800
#define CONFIG_LINE_MAX  256

/* Error codes (wire protocol) */
#define ERR_SYNTAX       100
#define ERR_NOT_FOUND    200
#define ERR_PERMISSION   201
#define ERR_EXISTS       202
#define ERR_IO           300
#define ERR_TIMEOUT      400
#define ERR_INTERNAL     500

/* Per-client state */
struct client {
    LONG fd;                       /* socket fd, -1 = unused */
    ULONG addr;                    /* peer IP, network byte order */
    char recv_buf[RECV_BUF_SIZE];  /* incoming data buffer */
    int recv_len;                  /* bytes currently in recv_buf */
    int discarding;                /* overflow discard mode flag */
};

/* IP access control list entry */
struct acl_entry {
    ULONG addr;                    /* allowed IP, network byte order */
};

/* Daemon configuration (parsed from S:amigactld.conf) */
struct daemon_config {
    int port;
    int allow_remote_shutdown;
    struct acl_entry acl[MAX_ACL_ENTRIES];
    int acl_count;
};

/* Top-level daemon state */
struct daemon_state {
    LONG listener_fd;
    struct client clients[MAX_CLIENTS];
    struct daemon_config config;
    int running;
};

#endif /* AMIGACTLD_DAEMON_H */
