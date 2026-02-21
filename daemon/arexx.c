/*
 * amigactld -- ARexx command dispatch (Phase 4)
 *
 * Implements AREXX command: send ARexx commands to named ports
 * with non-blocking reply handling via signal-driven message port.
 *
 * Pattern follows exec.c: module globals, init/cleanup lifecycle,
 * signal-driven event handling in the main event loop.
 */

#include "arexx.h"
#include "net.h"

#include <proto/rexxsyslib.h>
#include <rexx/storage.h>
#include <rexx/errors.h>
#include <proto/exec.h>
#include <proto/dos.h>

#include <stdio.h>
#include <string.h>

/* ---- Module globals ---- */

/* RexxSysBase must be struct RxsLib *, not struct Library *.
 * The proto/rexxsyslib.h inline stubs require this exact type. */
struct RxsLib *RexxSysBase = NULL;

/* Private reply port for receiving ARexx replies */
static struct MsgPort *reply_port = NULL;

/* Signal bit from reply port, exported for main.c event loop */
LONG g_arexx_sigbit = -1;

/* Result buffer -- static to avoid stack allocation on 68k.
 * Single-threaded daemon, safe to reuse across calls. */
static char result_buf[4096];

/* ---- Initialization / cleanup ---- */

int arexx_init(void)
{
    RexxSysBase = (struct RxsLib *)OpenLibrary(
        (STRPTR)"rexxsyslib.library", 0);
    if (!RexxSysBase) {
        /* Non-fatal: AREXX commands will return ERR 500 */
        g_arexx_sigbit = -1;
        return 0;
    }

    reply_port = CreateMsgPort();
    if (!reply_port) {
        CloseLibrary((struct Library *)RexxSysBase);
        RexxSysBase = NULL;
        g_arexx_sigbit = -1;
        return 0;
    }

    g_arexx_sigbit = reply_port->mp_SigBit;
    return 0;
}

void arexx_cleanup(void)
{
    if (reply_port) {
        DeleteMsgPort(reply_port);
        reply_port = NULL;
    }

    if (RexxSysBase) {
        CloseLibrary((struct Library *)RexxSysBase);
        RexxSysBase = NULL;
    }

    g_arexx_sigbit = -1;
}

void arexx_shutdown_wait(struct daemon_state *d)
{
    int i;
    int pending;
    int wait_count;
    struct RexxMsg *rmsg;

    if (!reply_port)
        return;

    /* Mark all active slots as orphaned */
    for (i = 0; i < MAX_AREXX_PENDING; i++) {
        if (d->arexx_slots[i].active)
            d->arexx_slots[i].client_idx = -1;
    }

    /* Drain pending replies for up to 10 seconds.
     * Must not delete the port while messages are outstanding --
     * the target would reply to a dead port, crashing AmigaOS. */
    wait_count = 0;
    while (wait_count < 200) {  /* 200 * 50ms = 10 seconds */
        /* Consume all available replies */
        while ((rmsg = (struct RexxMsg *)GetMsg(reply_port)) != NULL) {
            /* Free the result string if present.
             * Only valid as argstring when rc == 0; otherwise it is
             * a numeric secondary error code. */
            if (rmsg->rm_Result1 == 0 && rmsg->rm_Result2)
                DeleteArgstring((UBYTE *)rmsg->rm_Result2);
            ClearRexxMsg(rmsg, 1);
            DeleteRexxMsg(rmsg);

            /* Clear the matching slot */
            for (i = 0; i < MAX_AREXX_PENDING; i++) {
                if (d->arexx_slots[i].active &&
                    d->arexx_slots[i].msg == rmsg) {
                    d->arexx_slots[i].active = 0;
                    d->arexx_slots[i].msg = NULL;
                    break;
                }
            }
        }

        /* Check if any slots are still active */
        pending = 0;
        for (i = 0; i < MAX_AREXX_PENDING; i++) {
            if (d->arexx_slots[i].active)
                pending = 1;
        }

        if (!pending)
            break;

        /* Wait 50ms (1 tick) before checking again */
        Delay(1);
        wait_count++;
    }

    /* Clean up port and library.  If messages are still outstanding
     * after 10 seconds, we have no choice but to leak the port --
     * deleting it would crash when the reply arrives.  In practice
     * this means the daemon leaks a MsgPort on unclean shutdown,
     * which is acceptable. */
    if (!pending) {
        DeleteMsgPort(reply_port);
    }
    reply_port = NULL;  /* Prevent arexx_cleanup() from deleting port with outstanding msgs */

    if (RexxSysBase) {
        CloseLibrary((struct Library *)RexxSysBase);
        RexxSysBase = NULL;
    }

    g_arexx_sigbit = -1;
}

/* ---- Command handler ---- */

int cmd_arexx(struct daemon_state *d, int client_idx, const char *args)
{
    struct client *c = &d->clients[client_idx];
    const char *port_name;
    const char *cmd_string;
    int port_len;
    int slot;
    int i;
    struct MsgPort *target_port;
    struct RexxMsg *rmsg;
    /* Static: single-threaded, non-recursive handler */
    static char port_buf[128];

    /* Parse port name (first whitespace-delimited token) */
    if (args[0] == '\0') {
        send_error(c->fd, ERR_SYNTAX, "Usage: AREXX <port> <command>");
        send_sentinel(c->fd);
        return 0;
    }

    port_name = args;
    cmd_string = args;
    while (*cmd_string && *cmd_string != ' ' && *cmd_string != '\t')
        cmd_string++;

    port_len = (int)(cmd_string - port_name);
    if (port_len <= 0 || port_len >= (int)sizeof(port_buf)) {
        send_error(c->fd, ERR_SYNTAX, "Usage: AREXX <port> <command>");
        send_sentinel(c->fd);
        return 0;
    }

    memcpy(port_buf, port_name, port_len);
    port_buf[port_len] = '\0';

    /* Skip whitespace to find command */
    while (*cmd_string == ' ' || *cmd_string == '\t')
        cmd_string++;

    if (*cmd_string == '\0') {
        send_error(c->fd, ERR_SYNTAX, "Usage: AREXX <port> <command>");
        send_sentinel(c->fd);
        return 0;
    }

    /* Check if ARexx is available */
    if (!RexxSysBase) {
        send_error(c->fd, ERR_INTERNAL, "ARexx not available");
        send_sentinel(c->fd);
        return 0;
    }

    /* Find a free arexx_pending slot */
    slot = -1;
    for (i = 0; i < MAX_AREXX_PENDING; i++) {
        if (!d->arexx_slots[i].active) {
            slot = i;
            break;
        }
    }

    if (slot < 0) {
        send_error(c->fd, ERR_INTERNAL, "ARexx busy");
        send_sentinel(c->fd);
        return 0;
    }

    /* Create the ARexx message */
    rmsg = CreateRexxMsg(reply_port, NULL, NULL);
    if (!rmsg) {
        send_error(c->fd, ERR_INTERNAL, "Failed to create ARexx message");
        send_sentinel(c->fd);
        return 0;
    }

    rmsg->rm_Args[0] = (STRPTR)CreateArgstring(
        (STRPTR)cmd_string, strlen(cmd_string));
    if (!rmsg->rm_Args[0]) {
        DeleteRexxMsg(rmsg);
        send_error(c->fd, ERR_INTERNAL, "Failed to create ARexx argstring");
        send_sentinel(c->fd);
        return 0;
    }

    rmsg->rm_Action = RXCOMM | RXFF_RESULT | RXFF_STRING;

    /* Find the target port under Forbid to prevent it from
     * disappearing between FindPort and PutMsg */
    Forbid();
    target_port = FindPort((STRPTR)port_buf);
    if (!target_port) {
        Permit();
        ClearRexxMsg(rmsg, 1);
        DeleteRexxMsg(rmsg);
        send_error(c->fd, ERR_NOT_FOUND, "ARexx port not found");
        send_sentinel(c->fd);
        return 0;
    }

    PutMsg(target_port, (struct Message *)rmsg);
    Permit();

    /* Record in pending slot */
    d->arexx_slots[slot].active = 1;
    d->arexx_slots[slot].client_idx = client_idx;
    d->arexx_slots[slot].epoch = d->arexx_epoch++;
    d->arexx_slots[slot].msg = rmsg;
    DateStamp(&d->arexx_slots[slot].send_time);

    /* Mark client as waiting for ARexx reply */
    c->arexx_pending = 1;

    /* Deferred response -- no OK/sentinel sent yet */
    return 0;
}

/* ---- Reply handling ---- */

void arexx_handle_replies(struct daemon_state *d)
{
    struct RexxMsg *rmsg;
    int i;
    LONG rc;
    int result_len;
    int slot_idx;
    int cidx;
    struct client *c;

    while ((rmsg = (struct RexxMsg *)GetMsg(reply_port)) != NULL) {
        /* Find the matching slot by msg pointer */
        slot_idx = -1;
        for (i = 0; i < MAX_AREXX_PENDING; i++) {
            if (d->arexx_slots[i].active &&
                d->arexx_slots[i].msg == rmsg) {
                slot_idx = i;
                break;
            }
        }

        /* Extract return code */
        rc = rmsg->rm_Result1;

        /* Extract result string if present.
         * rm_Result2 is only a pointer to an argstring when rc == 0.
         * When rc != 0, rm_Result2 is a secondary error code (numeric),
         * NOT a pointer -- calling LengthArgstring/DeleteArgstring on it
         * would read/write arbitrary memory. */
        result_len = 0;
        if (rc == 0 && rmsg->rm_Result2) {
            int len;
            char *src;

            src = (char *)rmsg->rm_Result2;
            len = LengthArgstring((UBYTE *)rmsg->rm_Result2);

            /* Copy to local buffer before freeing */
            if (len > (int)sizeof(result_buf))
                len = (int)sizeof(result_buf);
            memcpy(result_buf, src, len);
            result_len = len;

            DeleteArgstring((UBYTE *)rmsg->rm_Result2);
        }

        /* Free the message */
        ClearRexxMsg(rmsg, 1);
        DeleteRexxMsg(rmsg);

        if (slot_idx < 0) {
            /* No matching slot -- shouldn't happen, but safe to ignore */
            continue;
        }

        cidx = d->arexx_slots[slot_idx].client_idx;

        /* Clear the slot */
        d->arexx_slots[slot_idx].active = 0;
        d->arexx_slots[slot_idx].msg = NULL;

        /* If orphaned, discard silently */
        if (cidx < 0)
            continue;

        c = &d->clients[cidx];

        /* Verify client is still connected and waiting */
        if (c->fd < 0 || !c->arexx_pending)
            continue;

        /* Send the response: OK rc=N, DATA chunks, END, sentinel.
         * Same framing as EXEC sync. */
        {
            static char info[32];
            sprintf(info, "rc=%ld", (long)rc);
            send_ok(c->fd, info);
        }

        if (result_len > 0)
            send_data_chunk(c->fd, result_buf, result_len);

        send_end(c->fd);
        send_sentinel(c->fd);

        /* Resume normal command processing for this client */
        c->arexx_pending = 0;
    }
}

/* ---- Timeout checking ---- */

void arexx_check_timeouts(struct daemon_state *d)
{
    struct DateStamp now;
    LONG elapsed;
    int i;
    int cidx;
    struct client *c;

    DateStamp(&now);

    for (i = 0; i < MAX_AREXX_PENDING; i++) {
        if (!d->arexx_slots[i].active)
            continue;

        /* Compute elapsed seconds using integer DateStamp arithmetic.
         * Naive field subtraction is safe: tick truncation error < 1s,
         * negligible vs 30s timeout. Day/minute carry propagates correctly. */
        elapsed = (now.ds_Days - d->arexx_slots[i].send_time.ds_Days)
                  * 86400
                + (now.ds_Minute - d->arexx_slots[i].send_time.ds_Minute)
                  * 60
                + (now.ds_Tick - d->arexx_slots[i].send_time.ds_Tick)
                  / 50;

        if (elapsed <= AREXX_TIMEOUT_SECS)
            continue;

        cidx = d->arexx_slots[i].client_idx;

        if (cidx >= 0) {
            /* Notify the client of timeout */
            c = &d->clients[cidx];
            if (c->fd >= 0 && c->arexx_pending) {
                send_error(c->fd, ERR_TIMEOUT,
                           "ARexx command timed out");
                send_sentinel(c->fd);
                c->arexx_pending = 0;
            }
        }

        /* Mark slot as orphaned -- keep active so the reply can be
         * consumed and freed when it eventually arrives.  The slot
         * cannot be reused until the reply is received. */
        d->arexx_slots[i].client_idx = -1;
    }
}

/* ---- Client disconnect handling ---- */

void arexx_orphan_client(struct daemon_state *d, int client_idx)
{
    int i;

    for (i = 0; i < MAX_AREXX_PENDING; i++) {
        if (d->arexx_slots[i].active &&
            d->arexx_slots[i].client_idx == client_idx) {
            d->arexx_slots[i].client_idx = -1;
        }
    }
}
