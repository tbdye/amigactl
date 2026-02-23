/*
 * amigactld -- File streaming
 *
 * Implements TAIL command: stream file appends to a client.
 * The client sends STOP to terminate the stream, returning
 * to normal command processing.
 *
 * Uses static FIB and read buffer (single-threaded daemon, safe).
 * Pattern follows file.c for Lock/Examine/UnLock and static buffers.
 */

#include "tail.h"
#include "daemon.h"
#include "net.h"

#include <proto/dos.h>
#include <proto/exec.h>
#include <dos/dos.h>

#include <stdio.h>
#include <string.h>

/* ---- Module globals ---- */

/* Static FIB allocated once at init -- avoids per-poll AllocDosObject */
static struct FileInfoBlock *tail_fib = NULL;

/* Read buffer for file polling -- static to avoid stack allocation */
static char tail_read_buf[4096];

/* Command extraction buffer -- static to avoid stack allocation */
static char tail_cmd_buf[MAX_CMD_LEN + 1];

/* ---- Forward declarations ---- */

static int tail_stop(struct daemon_state *d, int idx);

/* ---- Initialization / cleanup ---- */

int tail_init(void)
{
    tail_fib = AllocDosObject(DOS_FIB, NULL);
    if (!tail_fib) {
        daemon_msg("TAIL: failed to allocate FileInfoBlock\n");
        return -1;
    }
    return 0;
}

void tail_cleanup(void)
{
    if (tail_fib) {
        FreeDosObject(DOS_FIB, tail_fib);
        tail_fib = NULL;
    }
}

/* ---- Command handler ---- */

int cmd_tail(struct client *c, const char *args)
{
    BPTR lock;
    LONG current_size;
    static char info[32];

    if (args[0] == '\0') {
        send_error(c->fd, ERR_SYNTAX, "Missing path argument");
        send_sentinel(c->fd);
        return 0;
    }

    lock = Lock((STRPTR)args, ACCESS_READ);
    if (!lock) {
        /* Map IoErr to appropriate error code and message */
        LONG ioerr = IoErr();
        static char fault_buf[128];
        static char msg_buf[256];
        int code;

        switch (ioerr) {
        case ERROR_OBJECT_NOT_FOUND:
        case ERROR_DIR_NOT_FOUND:
        case ERROR_DEVICE_NOT_MOUNTED:
            code = ERR_NOT_FOUND;
            break;
        default:
            code = ERR_IO;
            break;
        }

        Fault(ioerr, (STRPTR)"", (STRPTR)fault_buf, sizeof(fault_buf));
        sprintf(msg_buf, "Lock failed%s", fault_buf);
        send_error(c->fd, code, msg_buf);
        send_sentinel(c->fd);
        return 0;
    }

    if (!Examine(lock, tail_fib)) {
        UnLock(lock);
        send_error(c->fd, ERR_IO, "Cannot examine file");
        send_sentinel(c->fd);
        return 0;
    }

    /* Check if it's a directory */
    if (tail_fib->fib_DirEntryType > 0) {
        UnLock(lock);
        send_error(c->fd, ERR_IO, "TAIL requires a file, not a directory");
        send_sentinel(c->fd);
        return 0;
    }

    current_size = tail_fib->fib_Size;
    UnLock(lock);

    /* Send OK with current size */
    sprintf(info, "%ld", (long)current_size);
    send_ok(c->fd, info);

    /* Enter streaming mode */
    c->tail.active = 1;
    strncpy(c->tail.path, args, sizeof(c->tail.path) - 1);
    c->tail.path[sizeof(c->tail.path) - 1] = '\0';
    c->tail.last_pos = current_size;
    c->tail.last_size = current_size;

    /* Response is ongoing -- no sentinel sent */
    return 0;
}

/* ---- File polling ---- */

int tail_poll_file(struct daemon_state *d, int idx)
{
    struct client *c = &d->clients[idx];
    BPTR lock;
    LONG current_size;
    BPTR fh;
    LONG to_read;
    LONG n;

    lock = Lock((STRPTR)c->tail.path, ACCESS_READ);
    if (!lock) {
        /* File no longer accessible */
        send_error(c->fd, ERR_IO, "File no longer accessible");
        send_sentinel(c->fd);
        c->tail.active = 0;
        return 0;
    }

    if (!Examine(lock, tail_fib)) {
        UnLock(lock);
        send_error(c->fd, ERR_IO, "File no longer accessible");
        send_sentinel(c->fd);
        c->tail.active = 0;
        return 0;
    }

    current_size = tail_fib->fib_Size;
    UnLock(lock);

    /* Truncation detection: file shrank */
    if (current_size < c->tail.last_pos) {
        c->tail.last_pos = current_size;
        c->tail.last_size = current_size;
        return 0;
    }

    /* No new data */
    if (current_size == c->tail.last_pos) {
        c->tail.last_size = current_size;
        return 0;
    }

    /* New data available: current_size > last_pos */
    fh = Open((STRPTR)c->tail.path, MODE_OLDFILE);
    if (!fh) {
        /* Cannot open -- treat as file gone */
        send_error(c->fd, ERR_IO, "File no longer accessible");
        send_sentinel(c->fd);
        c->tail.active = 0;
        return 0;
    }

    /* Seek to where we left off */
    Seek(fh, c->tail.last_pos, OFFSET_BEGINNING);

    /* Read and send in chunks */
    while (c->tail.last_pos < current_size) {
        to_read = current_size - c->tail.last_pos;
        if (to_read > (LONG)sizeof(tail_read_buf))
            to_read = (LONG)sizeof(tail_read_buf);

        n = Read(fh, (STRPTR)tail_read_buf, to_read);
        if (n <= 0)
            break; /* Read error or EOF */

        if (send_data_chunk(c->fd, tail_read_buf, (int)n) < 0) {
            /* Send failure -- client likely disconnected.
             * Clear tail state; main loop will detect the broken pipe
             * and disconnect the client on the next recv attempt. */
            Close(fh);
            c->tail.active = 0;
            return -1;
        }

        c->tail.last_pos += n;
    }

    Close(fh);
    c->tail.last_size = current_size;
    return 0;
}

/* ---- Input handling during TAIL ---- */

int tail_handle_input(struct daemon_state *d, int idx)
{
    struct client *c = &d->clients[idx];
    int n;
    int result;

    n = recv_into_buf(c);
    if (n <= 0) {
        /* EOF or error -- client disconnected */
        c->tail.active = 0;
        return -1;
    }

    /* Extract and process commands from recv buffer */
    while ((result = extract_command(c, tail_cmd_buf,
                                     sizeof(tail_cmd_buf))) == 1) {
        /* Skip leading whitespace */
        char *p = tail_cmd_buf;
        while (*p == ' ' || *p == '\t')
            p++;

        /* Skip empty lines */
        if (*p == '\0')
            continue;

        if (stricmp(p, "STOP") == 0) {
            tail_stop(d, idx);
            return 0;
        }

        /* Silently discard any other input during TAIL */
    }

    /* Overflow during TAIL: discard and ignore (no error response,
     * the only valid input is "STOP") */
    if (result == -1) {
        c->recv_len = 0;
        c->discarding = 0;
    }

    return 0;
}

/* ---- STOP handler ---- */

/* Note: Uses module-global tail_fib and tail_read_buf. Safe because the
 * single-threaded event loop guarantees tail_stop() and tail_poll_file()
 * never execute concurrently for the same client. */
static int tail_stop(struct daemon_state *d, int idx)
{
    struct client *c = &d->clients[idx];

    (void)d; /* unused, kept for consistency */

    /* Perform one final poll to capture any remaining data.
     * Ignore return value -- even if send fails, we still
     * need to try sending END + sentinel. */
    if (c->tail.active) {
        BPTR lock;
        LONG current_size;

        lock = Lock((STRPTR)c->tail.path, ACCESS_READ);
        if (lock) {
            if (Examine(lock, tail_fib)) {
                current_size = tail_fib->fib_Size;
                UnLock(lock);

                if (current_size > c->tail.last_pos) {
                    BPTR fh;
                    LONG to_read;
                    LONG n;

                    fh = Open((STRPTR)c->tail.path, MODE_OLDFILE);
                    if (fh) {
                        Seek(fh, c->tail.last_pos, OFFSET_BEGINNING);
                        while (c->tail.last_pos < current_size) {
                            to_read = current_size - c->tail.last_pos;
                            if (to_read > (LONG)sizeof(tail_read_buf))
                                to_read = (LONG)sizeof(tail_read_buf);
                            n = Read(fh, (STRPTR)tail_read_buf, to_read);
                            if (n <= 0)
                                break;
                            if (send_data_chunk(c->fd, tail_read_buf,
                                                (int)n) < 0)
                                break;
                            c->tail.last_pos += n;
                        }
                        Close(fh);
                    }
                }
            } else {
                UnLock(lock);
            }
        }
    }

    send_end(c->fd);
    send_sentinel(c->fd);

    /* Return to normal command processing */
    c->tail.active = 0;
    return 0;
}
