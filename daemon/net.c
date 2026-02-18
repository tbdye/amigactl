/*
 * amigactld -- Socket helpers and protocol I/O
 *
 * Manages bsdsocket.library lifecycle, TCP listener/accept operations,
 * and the wire protocol framing (send_ok, send_error, dot-stuffing,
 * sentinel, recv buffering, command extraction).
 */

#include "net.h"

#include <proto/exec.h>
#include <proto/bsdsocket.h>

#include <sys/socket.h>
#include <sys/filio.h>
#include <netinet/in.h>

#include <stdio.h>
#include <string.h>

/* ---- Library state ---- */

/* Storage for the extern declared by <proto/bsdsocket.h> */
struct Library *SocketBase = NULL;

static LONG bsd_errno;
static LONG bsd_h_errno;

/* ---- Library management ---- */

int net_init(void)
{
    LONG result;

    SocketBase = OpenLibrary((STRPTR)"bsdsocket.library", 4);
    if (!SocketBase) {
        printf("Could not open bsdsocket.library v4\n");
        printf("A TCP/IP stack (e.g. Roadshow, Miami, AmiTCP) must be "
               "running.\n");
        return -1;
    }

    result = SocketBaseTags(
        SBTM_SETVAL(SBTC_ERRNOLONGPTR), (ULONG)&bsd_errno,
        SBTM_SETVAL(SBTC_HERRNOLONGPTR), (ULONG)&bsd_h_errno,
        TAG_DONE);

    if (result != 0) {
        printf("Warning: SocketBaseTags errno registration failed\n");
    }

    return 0;
}

void net_cleanup(void)
{
    if (SocketBase) {
        CloseLibrary(SocketBase);
        SocketBase = NULL;
    }
}

/* ---- Socket operations ---- */

LONG net_listen(int port)
{
    LONG fd;
    struct sockaddr_in addr;
    LONG one = 1;

    fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) {
        printf("socket() failed, errno=%ld\n", (long)bsd_errno);
        return -1;
    }

#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wincompatible-pointer-types"
    if (setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one)) < 0)
        printf("Warning: setsockopt(SO_REUSEADDR) failed\n");
#pragma GCC diagnostic pop

    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(port);
    addr.sin_addr.s_addr = htonl(INADDR_ANY);

    if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        printf("bind() failed on port %d, errno=%ld\n",
               port, (long)bsd_errno);
        CloseSocket(fd);
        return -1;
    }

    if (listen(fd, 5) < 0) {
        printf("listen() failed, errno=%ld\n", (long)bsd_errno);
        CloseSocket(fd);
        return -1;
    }

    return fd;
}

LONG net_accept(LONG listener, ULONG *peer_addr)
{
    struct sockaddr_in addr;
    LONG addrlen = sizeof(addr);
    LONG fd;

#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wincompatible-pointer-types"
    fd = accept(listener, (struct sockaddr *)&addr, &addrlen);
#pragma GCC diagnostic pop
    if (fd < 0)
        return -1;

    if (peer_addr)
        *peer_addr = addr.sin_addr.s_addr;

    return fd;
}

int net_set_nonblocking(LONG fd)
{
    LONG one = 1;
    return IoctlSocket(fd, FIONBIO, (char *)&one);
}

void net_close(LONG fd)
{
    if (fd >= 0)
        CloseSocket(fd);
}

/* ---- Low-level send helper ---- */

/* Send exactly len bytes, looping on partial send().
 * Returns 0 on success, -1 on error. */
static int send_all(LONG fd, const char *buf, int len)
{
    int sent;
    LONG n;

    sent = 0;
    while (sent < len) {
        n = send(fd, (STRPTR)(buf + sent), len - sent, 0);
        if (n <= 0)
            return -1;
        sent += n;
    }
    return 0;
}

/* ---- Protocol I/O ---- */

int send_line(LONG fd, const char *line)
{
    int len;

    len = strlen(line);
    if (len > 0) {
        if (send_all(fd, line, len) < 0)
            return -1;
    }
    return send_all(fd, "\n", 1);
}

int send_ok(LONG fd, const char *info)
{
    if (info) {
        if (send_all(fd, "OK ", 3) < 0)
            return -1;
        if (send_all(fd, info, strlen(info)) < 0)
            return -1;
        return send_all(fd, "\n", 1);
    }
    return send_all(fd, "OK\n", 3);
}

int send_error(LONG fd, int code, const char *message)
{
    char buf[16];
    int len;

    len = sprintf(buf, "ERR %d ", code);
    if (send_all(fd, buf, len) < 0)
        return -1;
    if (send_all(fd, message, strlen(message)) < 0)
        return -1;
    return send_all(fd, "\n", 1);
}

int send_banner(LONG fd)
{
    return send_line(fd, "AMIGACTL " AMIGACTLD_VERSION);
}

int send_payload_line(LONG fd, const char *line)
{
    /* Dot-stuff: if line starts with '.', prepend an extra '.' */
    if (line[0] == '.') {
        if (send_all(fd, ".", 1) < 0)
            return -1;
    }
    return send_line(fd, line);
}

int send_sentinel(LONG fd)
{
    return send_all(fd, ".\n", 2);
}

int recv_into_buf(struct client *c)
{
    LONG n;

    n = recv(c->fd, (STRPTR)(c->recv_buf + c->recv_len),
             RECV_BUF_SIZE - c->recv_len, 0);
    if (n > 0)
        c->recv_len += n;

    return (int)n;
}

int extract_command(struct client *c, char *cmd, int cmd_max)
{
    int i;
    int cmd_len;

    /* Scan for newline in recv_buf */
    for (i = 0; i < c->recv_len; i++) {
        if (c->recv_buf[i] == '\n') {
            /* Found a complete line: bytes 0..i-1 are the command,
             * byte i is the newline */
            cmd_len = i;

            /* Strip trailing \r for telnet compatibility */
            if (cmd_len > 0 && c->recv_buf[cmd_len - 1] == '\r')
                cmd_len--;

            /* RECV_BUF_SIZE is MAX_CMD_LEN + 1 (for the LF), so a full buffer
             * holds at most a 4096-byte command + newline.  Defense-in-depth. */
            if (cmd_len >= cmd_max)
                cmd_len = cmd_max - 1;
            memcpy(cmd, c->recv_buf, cmd_len);
            cmd[cmd_len] = '\0';

            /* Shift remaining data in recv_buf */
            i++; /* skip past the newline */
            if (i < c->recv_len) {
                memmove(c->recv_buf, c->recv_buf + i, c->recv_len - i);
            }
            c->recv_len -= i;

            return 1;
        }
    }

    /* No newline found.  If buffer is full, that's an overflow. */
    if (c->recv_len >= RECV_BUF_SIZE) {
        c->discarding = 1;
        return -1;
    }

    return 0;
}
