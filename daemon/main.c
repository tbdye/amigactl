/*
 * amigactld -- Amiga remote access daemon
 *
 * Entry point, startup (CLI + Workbench dual-mode), WaitSelect event
 * loop, command dispatch for Phase 1-4 commands.
 */

#include "daemon.h"
#include "config.h"
#include "net.h"
#include "file.h"
#include "exec.h"
#include "sysinfo.h"
#include "arexx.h"
#include "tail.h"

#include <proto/exec.h>
#include <proto/dos.h>
#include <dos/dosextens.h>
#include <proto/icon.h>
#include <proto/bsdsocket.h>
#include <workbench/startup.h>

#include <stdio.h>
#include <string.h>

/* Ensure sufficient stack for buffers and nested calls.
 * libnix startup code checks this and expands the stack if needed. */
unsigned long __stack = 65536;

/* Override libnix default CON: window for Workbench launches.
 * libnix opens this window before main() when argc==0. */
const char *__stdiowin = "CON:0/20/640/200/amigactld/AUTO/CLOSE/WAIT";

/* icon.library base -- needed by proto/icon.h inline stubs */
struct Library *IconBase = NULL;

/* ReadArgs template */
#define TEMPLATE "PORT/N,CONFIG/K"

enum {
    ARG_PORT,
    ARG_CONFIG,
    ARG_COUNT
};

/* Default config file path */
#define DEFAULT_CONFIG_PATH "S:amigactld.conf"

/* Forward declarations */
static void handle_accept(struct daemon_state *d);
static void handle_client(struct daemon_state *d, int idx);
static void dispatch_command(struct daemon_state *d, int idx, char *cmd);
static void disconnect_client(struct daemon_state *d, int idx);

int main(int argc, char **argv)
{
    /* Static: 33KB struct must live in BSS, not on the stack.
     * CLI default stack is 4-8KB; even with __stack expansion,
     * stack-allocating this caused Guru Meditations on 68k. */
    static struct daemon_state daemon;
    struct RDArgs *rdargs = NULL;
    LONG args[ARG_COUNT];
    const char *config_path;
    int i;
    int exit_code = RETURN_OK;

    /* Workbench startup variables */
    struct RDArgs wb_rda;
    char argbuf[256];

    /* WaitSelect variables */
    fd_set rfds;
    LONG nfds;
    struct timeval tv;
    ULONG sigmask;
    int rc;

    memset(args, 0, sizeof(args));
    memset(&daemon, 0, sizeof(daemon));

    /* Suppress "Please insert volume" and similar system requesters.
     * A daemon must never block on UI prompts. */
    {
        struct Process *pr = (struct Process *)FindTask(NULL);
        pr->pr_WindowPtr = (APTR)-1;
    }

    daemon.listener_fd = -1;
    daemon.running = 1;
    daemon.next_proc_id = 1;
    DateStamp(&daemon.startup_stamp);

    /* Initialize all client slots */
    for (i = 0; i < MAX_CLIENTS; i++) {
        daemon.clients[i].fd = -1;
        daemon.clients[i].recv_len = 0;
        daemon.clients[i].discarding = 0;
    }

    /* ---- Argument parsing ---- */

    if (argc == 0) {
        /* Workbench launch: build CLI-style arg string from Tool Types.
         * libnix has already: waited for WBStartup, opened our CON:
         * window via __stdiowin, and called CurrentDir() to the
         * program's directory. */
        struct WBStartup *wbmsg = (struct WBStartup *)argv;
        struct DiskObject *dobj = NULL;
        CONST_STRPTR *tt;
        char *p = argbuf;
        UBYTE *val;

        IconBase = OpenLibrary((STRPTR)"icon.library", 36);
        if (IconBase) {
            dobj = GetDiskObject(wbmsg->sm_ArgList[0].wa_Name);
            if (dobj && dobj->do_ToolTypes) {
                tt = (CONST_STRPTR *)dobj->do_ToolTypes;
                val = FindToolType(tt, (STRPTR)"PORT");
                if (val)
                    p += sprintf(p, "PORT %s ", (char *)val);
                val = FindToolType(tt, (STRPTR)"CONFIG");
                if (val)
                    p += sprintf(p, "CONFIG %s ", (char *)val);
            }
            if (dobj)
                FreeDiskObject(dobj);
            CloseLibrary(IconBase);
            IconBase = NULL;
        }
        *p++ = '\n';
        *p = '\0';

        /* Feed argbuf to ReadArgs via RDA_Source */
        memset(&wb_rda, 0, sizeof(wb_rda));
        wb_rda.RDA_Source.CS_Buffer = (STRPTR)argbuf;
        wb_rda.RDA_Source.CS_Length = strlen(argbuf);
        rdargs = ReadArgs((STRPTR)TEMPLATE, args, &wb_rda);
    } else {
        /* CLI launch: standard ReadArgs from command line */
        rdargs = ReadArgs((STRPTR)TEMPLATE, args, NULL);
    }

    if (!rdargs) {
        printf("Usage: amigactld [PORT <port>] [CONFIG <path>]\n");
        return RETURN_FAIL;
    }

    /* ---- Configuration ---- */

    config_defaults(&daemon.config);

    config_path = args[ARG_CONFIG]
                  ? (const char *)args[ARG_CONFIG]
                  : DEFAULT_CONFIG_PATH;

    if (config_load(&daemon.config, config_path) < 0) {
        exit_code = RETURN_FAIL;
        goto cleanup;
    }

    /* Override port from ReadArgs/ToolType if specified */
    if (args[ARG_PORT])
        daemon.config.port = (int)(*(LONG *)args[ARG_PORT]);

    /* ---- Network initialization ---- */

    if (net_init() < 0) {
        exit_code = RETURN_FAIL;
        goto cleanup;
    }

    daemon.listener_fd = net_listen(daemon.config.port);
    if (daemon.listener_fd < 0) {
        exit_code = RETURN_FAIL;
        goto cleanup;
    }

    /* Listener must be non-blocking so accept() doesn't block
     * the event loop on spurious readability notifications */
    if (net_set_nonblocking(daemon.listener_fd) < 0) {
        printf("Failed to set listener to non-blocking mode\n");
        exit_code = RETURN_FAIL;
        goto cleanup;
    }

    printf("amigactld %s listening on port %d\n",
           AMIGACTLD_VERSION, daemon.config.port);
    fflush(stdout);

    /* ---- Phase 3 initialization ---- */

    g_daemon_state = &daemon;

    if (exec_init() < 0) {
        printf("Warning: EXEC ASYNC unavailable (no signal bit)\n");
        fflush(stdout);
    }

    exec_cleanup_temp_files();

    /* ---- Phase 4 initialization ---- */

    arexx_init();
    if (g_arexx_sigbit < 0) {
        printf("Warning: AREXX unavailable (rexxsyslib.library not found)\n");
        fflush(stdout);
    }

    if (tail_init() < 0) {
        printf("Failed to allocate TAIL resources\n");
        fflush(stdout);
        exit_code = RETURN_FAIL;
        goto cleanup;
    }

    /* ---- Event loop ---- */

    while (daemon.running) {
        FD_ZERO(&rfds);
        FD_SET(daemon.listener_fd, &rfds);
        nfds = daemon.listener_fd;

        for (i = 0; i < MAX_CLIENTS; i++) {
            if (daemon.clients[i].fd >= 0 &&
                !daemon.clients[i].arexx_pending) {
                FD_SET(daemon.clients[i].fd, &rfds);
                if (daemon.clients[i].fd > nfds)
                    nfds = daemon.clients[i].fd;
            }
        }
        nfds++;

        tv.tv_secs = 1;
        tv.tv_micro = 0;
        sigmask = SIGBREAKF_CTRL_C;
        if (g_proc_sigbit >= 0)
            sigmask |= (1L << g_proc_sigbit);
        if (g_arexx_sigbit >= 0)
            sigmask |= (1L << g_arexx_sigbit);

        rc = WaitSelect(nfds, &rfds, NULL, NULL, &tv, &sigmask);

        /* Check for Ctrl-C */
        if (sigmask & SIGBREAKF_CTRL_C) {
            printf("Ctrl-C received, shutting down.\n");
            fflush(stdout);
            daemon.running = 0;
            break;
        }

        /* Check for async process completion */
        if (g_proc_sigbit >= 0 &&
            (sigmask & (1L << g_proc_sigbit))) {
            exec_scan_completed(&daemon);
        }

        /* Check for ARexx reply */
        if (g_arexx_sigbit >= 0 &&
            (sigmask & (1L << g_arexx_sigbit))) {
            arexx_handle_replies(&daemon);
        }

        if (rc < 0) {
            /* WaitSelect error -- could be spurious, continue */
            continue;
        }

        /* Check listener for new connections */
        if (FD_ISSET(daemon.listener_fd, &rfds))
            handle_accept(&daemon);

        /* Process each client based on its current mode */
        for (i = 0; i < MAX_CLIENTS; i++) {
            if (daemon.clients[i].fd < 0)
                continue;

            if (daemon.clients[i].tail.active) {
                /* TAIL mode: check for STOP, poll file */
                if (FD_ISSET(daemon.clients[i].fd, &rfds)) {
                    if (tail_handle_input(&daemon, i) < 0) {
                        disconnect_client(&daemon, i);
                        continue;
                    }
                }
                if (daemon.clients[i].tail.active)
                    if (tail_poll_file(&daemon, i) < 0) {
                        disconnect_client(&daemon, i);
                        continue;
                    }
            } else if (daemon.clients[i].arexx_pending) {
                /* Waiting for ARexx reply -- skip command processing */
            } else {
                /* Normal command processing */
                if (FD_ISSET(daemon.clients[i].fd, &rfds))
                    handle_client(&daemon, i);
            }
        }

        /* ARexx timeout housekeeping */
        arexx_check_timeouts(&daemon);
    }

    /* ---- Cleanup ---- */

cleanup:
    /* Drain outstanding ARexx replies before closing connections */
    arexx_shutdown_wait(&daemon);

    /* Safely terminate tracked async processes */
    exec_shutdown_procs(&daemon);

    /* Close all client sockets */
    for (i = 0; i < MAX_CLIENTS; i++) {
        if (daemon.clients[i].fd >= 0) {
            net_close(daemon.clients[i].fd);
            daemon.clients[i].fd = -1;
        }
    }

    /* Close listener */
    if (daemon.listener_fd >= 0) {
        net_close(daemon.listener_fd);
        daemon.listener_fd = -1;
    }

    /* Close bsdsocket.library */
    net_cleanup();

    /* Free module resources (reverse order of init) */
    tail_cleanup();
    arexx_cleanup();
    exec_cleanup();

    if (rdargs)
        FreeArgs(rdargs);

    printf("amigactld stopped.\n");
    fflush(stdout);
    return exit_code;
}

/* ---- Accept handler ---- */

static void handle_accept(struct daemon_state *d)
{
    LONG fd;
    ULONG peer_addr;
    int i;
    int slot;

    fd = net_accept(d->listener_fd, &peer_addr);
    if (fd < 0)
        return; /* spurious readability, nothing to accept */

    /* ACL check -- if denied, close immediately with no banner */
    if (!acl_check(&d->config, peer_addr)) {
        net_close(fd);
        return;
    }

    /* Find an empty client slot */
    slot = -1;
    for (i = 0; i < MAX_CLIENTS; i++) {
        if (d->clients[i].fd < 0) {
            slot = i;
            break;
        }
    }

    if (slot < 0) {
        /* No room -- close silently (same as ACL reject) */
        net_close(fd);
        return;
    }

    /* Ensure the accepted socket is blocking -- on some stacks,
     * accept() inherits the listener's non-blocking flag. */
    net_set_blocking(fd);

    /* Initialize client state.  Socket stays blocking. */
    d->clients[slot].fd = fd;
    d->clients[slot].addr = peer_addr;
    d->clients[slot].recv_len = 0;
    d->clients[slot].discarding = 0;
    d->clients[slot].arexx_pending = 0;
    d->clients[slot].tail.active = 0;

    send_banner(fd);
}

/* ---- Client data handler ---- */

static void handle_client(struct daemon_state *d, int idx)
{
    struct client *c = &d->clients[idx];
    static char cmd[MAX_CMD_LEN + 1];
    int n;
    int result;
    int i;

    n = recv_into_buf(c);
    if (n <= 0) {
        /* EOF or error -- disconnect */
        disconnect_client(d, idx);
        return;
    }

    /* If in discard mode, scan for newline to exit it */
    if (c->discarding) {
        for (i = 0; i < c->recv_len; i++) {
            if (c->recv_buf[i] == '\n') {
                /* Found newline -- exit discard mode.
                 * Data after the newline is the start of next command. */
                i++; /* skip past newline */
                if (i < c->recv_len) {
                    memmove(c->recv_buf, c->recv_buf + i, c->recv_len - i);
                }
                c->recv_len -= i;
                c->discarding = 0;
                break;
            }
        }
        if (c->discarding) {
            /* No newline found -- discard everything */
            c->recv_len = 0;
            return;
        }
        /* Fall through to command extraction with whatever remains */
    }

    /* Extract and dispatch complete commands */
    while ((result = extract_command(c, cmd, sizeof(cmd))) == 1) {
        /* Skip empty or whitespace-only lines */
        {
            char *cp = cmd;
            while (*cp == ' ' || *cp == '\t')
                cp++;
            if (*cp == '\0')
                continue;
        }

        dispatch_command(d, idx, cmd);

        /* Client may have been disconnected by QUIT */
        if (c->fd < 0)
            return;
    }

    /* Check for overflow */
    if (result == -1) {
        send_error(c->fd, ERR_SYNTAX, "Command too long");
        send_sentinel(c->fd);
        /* discarding flag already set by extract_command() */
        c->recv_len = 0;  /* Clear buffer so recv can receive new data */
    }
}

/* ---- Command dispatch ---- */

/* Note: send failures are intentionally unchecked in command handlers.
 * A broken connection will be detected by recv_into_buf() on the next
 * event loop iteration and the client will be disconnected then. */
static void dispatch_command(struct daemon_state *d, int idx, char *cmd)
{
    struct client *c = &d->clients[idx];
    char *verb;
    char *rest;
    int rc = 0;

    /* Split command into verb and rest */
    verb = cmd;
    rest = cmd;
    while (*rest && *rest != ' ' && *rest != '\t')
        rest++;
    if (*rest) {
        *rest++ = '\0';
        /* Skip whitespace after verb */
        while (*rest == ' ' || *rest == '\t')
            rest++;
    }

    if (stricmp(verb, "VERSION") == 0) {
        send_ok(c->fd, NULL);
        send_payload_line(c->fd, "amigactld " AMIGACTLD_VERSION);
        send_sentinel(c->fd);

    } else if (stricmp(verb, "PING") == 0) {
        send_ok(c->fd, NULL);
        send_sentinel(c->fd);

    } else if (stricmp(verb, "QUIT") == 0) {
        send_ok(c->fd, "Goodbye");
        send_sentinel(c->fd);
        disconnect_client(d, idx);

    } else if (stricmp(verb, "SHUTDOWN") == 0) {
        /* Validate CONFIRM keyword first (before checking permission).
         * Extract just the first word of rest for comparison --
         * trailing text after CONFIRM is ignored per spec. */
        {
            char keyword[16];
            int ki = 0;
            const char *rp = rest;

            while (*rp && *rp != ' ' && *rp != '\t' &&
                   ki < (int)sizeof(keyword) - 1)
                keyword[ki++] = *rp++;
            keyword[ki] = '\0';

            if (ki == 0 || stricmp(keyword, "CONFIRM") != 0) {
                send_error(c->fd, ERR_SYNTAX,
                           "SHUTDOWN requires CONFIRM keyword");
                send_sentinel(c->fd);
                return;
            }
        }

        if (!d->config.allow_remote_shutdown) {
            send_error(c->fd, ERR_PERMISSION,
                       "Remote shutdown not permitted");
            send_sentinel(c->fd);
            return;
        }

        /* Permitted shutdown */
        send_ok(c->fd, "Shutting down");
        send_sentinel(c->fd);
        d->running = 0;

    } else if (stricmp(verb, "REBOOT") == 0) {
        {
            char keyword[16];
            int ki = 0;
            const char *rp = rest;

            while (*rp && *rp != ' ' && *rp != '\t' &&
                   ki < (int)sizeof(keyword) - 1)
                keyword[ki++] = *rp++;
            keyword[ki] = '\0';

            if (ki == 0 || stricmp(keyword, "CONFIRM") != 0) {
                send_error(c->fd, ERR_SYNTAX,
                           "REBOOT requires CONFIRM keyword");
                send_sentinel(c->fd);
                return;
            }
        }

        if (!d->config.allow_remote_reboot) {
            send_error(c->fd, ERR_PERMISSION,
                       "Remote reboot not permitted");
            send_sentinel(c->fd);
            return;
        }

        /* Send response before rebooting -- ColdReboot() never returns */
        send_ok(c->fd, "Rebooting");
        send_sentinel(c->fd);
        ColdReboot();

    /* --- Phase 2 file handlers --- */

    } else if (stricmp(verb, "DIR") == 0) {
        rc = cmd_dir(c, rest);

    } else if (stricmp(verb, "STAT") == 0) {
        rc = cmd_stat(c, rest);

    } else if (stricmp(verb, "READ") == 0) {
        rc = cmd_read(c, rest);

    } else if (stricmp(verb, "WRITE") == 0) {
        rc = cmd_write(c, rest);

    } else if (stricmp(verb, "DELETE") == 0) {
        rc = cmd_delete(c, rest);

    } else if (stricmp(verb, "RENAME") == 0) {
        rc = cmd_rename(c, rest);

    } else if (stricmp(verb, "MAKEDIR") == 0) {
        rc = cmd_makedir(c, rest);

    } else if (stricmp(verb, "PROTECT") == 0) {
        rc = cmd_protect(c, rest);

    /* --- Phase 3 handlers --- */

    } else if (stricmp(verb, "EXEC") == 0) {
        rc = cmd_exec(c, rest);

    } else if (stricmp(verb, "PROCLIST") == 0) {
        rc = cmd_proclist(c, rest);

    } else if (stricmp(verb, "PROCSTAT") == 0) {
        rc = cmd_procstat(c, rest);

    } else if (stricmp(verb, "SIGNAL") == 0) {
        rc = cmd_signal_proc(c, rest);

    } else if (stricmp(verb, "KILL") == 0) {
        rc = cmd_kill(c, rest);

    } else if (stricmp(verb, "SETDATE") == 0) {
        rc = cmd_setdate(c, rest);

    } else if (stricmp(verb, "SYSINFO") == 0) {
        rc = cmd_sysinfo(c, rest);

    } else if (stricmp(verb, "ASSIGNS") == 0) {
        rc = cmd_assigns(c, rest);

    } else if (stricmp(verb, "PORTS") == 0) {
        rc = cmd_ports(c, rest);

    } else if (stricmp(verb, "VOLUMES") == 0) {
        rc = cmd_volumes(c, rest);

    } else if (stricmp(verb, "TASKS") == 0) {
        rc = cmd_tasks(c, rest);

    } else if (stricmp(verb, "UPTIME") == 0) {
        {
            struct DateStamp now;
            LONG days_diff;
            LONG mins_diff;
            LONG ticks_diff;
            LONG total_seconds;
            static char uptimebuf[32];

            DateStamp(&now);
            days_diff = now.ds_Days - d->startup_stamp.ds_Days;
            mins_diff = now.ds_Minute - d->startup_stamp.ds_Minute;
            ticks_diff = now.ds_Tick - d->startup_stamp.ds_Tick;
            total_seconds = days_diff * 86400 + mins_diff * 60
                            + ticks_diff / 50;

            sprintf(uptimebuf, "seconds=%ld", (long)total_seconds);
            send_ok(c->fd, NULL);
            send_payload_line(c->fd, uptimebuf);
            send_sentinel(c->fd);
        }

    /* --- Phase 4 handlers --- */

    } else if (stricmp(verb, "AREXX") == 0) {
        rc = cmd_arexx(d, idx, rest);

    } else if (stricmp(verb, "TAIL") == 0) {
        rc = cmd_tail(c, rest);

    } else {
        send_error(c->fd, ERR_SYNTAX, "Unknown command");
        send_sentinel(c->fd);
    }

    /* Disconnect client if a Phase 2 handler signaled failure */
    if (rc < 0) {
        disconnect_client(d, idx);
    }
}

/* ---- Client disconnect ---- */

static void disconnect_client(struct daemon_state *d, int idx)
{
    /* Clean up Phase 4 state before closing the connection */
    d->clients[idx].tail.active = 0;
    arexx_orphan_client(d, idx);
    d->clients[idx].arexx_pending = 0;

    net_close(d->clients[idx].fd);
    d->clients[idx].fd = -1;
    d->clients[idx].recv_len = 0;
    d->clients[idx].discarding = 0;
}
