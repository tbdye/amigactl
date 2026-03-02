/*
 * amigactld -- Amiga remote access daemon
 *
 * Central header: constants, error codes, structures.
 */

#ifndef AMIGACTLD_DAEMON_H
#define AMIGACTLD_DAEMON_H

#include <exec/types.h>
#include <exec/tasks.h>
#include <dos/dos.h>

/* Version string -- single source of truth */
#define AMIGACTLD_VERSION "0.7.0"

/* Limits */
#define MAX_CLIENTS      8
#define MAX_ACL_ENTRIES  16
#define MAX_CMD_LEN      4096
#define RECV_BUF_SIZE    4097  /* MAX_CMD_LEN + LF terminator */
#define DEFAULT_PORT     6800
#define CONFIG_LINE_MAX  256

/* Process table */
#define MAX_TRACKED_PROCS 16
#define PROC_RUNNING 0
#define PROC_EXITED  1

/* ARexx */
#define MAX_AREXX_PENDING MAX_CLIENTS  /* one per client */
#define AREXX_TIMEOUT_SECS 30

/* Error codes (wire protocol) */
#define ERR_SYNTAX       100
#define ERR_NOT_FOUND    200
#define ERR_PERMISSION   201
#define ERR_EXISTS       202
#define ERR_IO           300
#define ERR_TIMEOUT      400
#define ERR_INTERNAL     500

/* ARexx pending slot (one outstanding message) */
struct arexx_pending {
    int active;              /* 1 = msg outstanding */
    int client_idx;          /* client who initiated, -1 = orphaned */
    ULONG epoch;             /* slot reuse safety counter */
    void *msg;               /* struct RexxMsg * (void to avoid header dep) */
    struct DateStamp send_time;  /* for timeout detection */
};

/* TAIL streaming state (per-client) */
struct tail_state {
    int active;              /* 1 = TAIL in progress */
    char path[512];          /* file being tailed */
    LONG last_size;          /* last known file size */
    LONG last_pos;           /* current read position */
};

/* TRACE mode constants */
#define TRACE_MODE_START  0  /* normal TRACE START */
#define TRACE_MODE_RUN    1  /* TRACE RUN (auto-terminate on process exit) */

/* TRACE streaming state (per-client) */
struct trace_state {
    int active;              /* 1 = TRACE in progress */

    /* Filters (applied daemon-side, AND-combined) */
    int filter_lib_id;       /* -1 = all libs, or specific lib_id */
    WORD filter_lvo;         /* 0 = all functions, or specific LVO */
    int filter_errors_only;  /* 1 = only events where retval indicates error */
    char filter_procname[64]; /* "" = all, or substring match on task name */

    /* TRACE RUN state (only used when mode == TRACE_MODE_RUN) */
    int mode;                /* TRACE_MODE_START or TRACE_MODE_RUN */
    int run_proc_slot;       /* index into daemon_state.procs[], -1 if none */
    APTR run_task_ptr;       /* Task pointer for exact process matching */
    ULONG run_start_seq;     /* event_sequence at TRACE RUN start; skip older */

    /* Phase 4: noise function save/restore during TRACE RUN.
     * noise_saved is the order-independent trigger for cleanup --
     * set only when we took ownership of filter_task, cleared only
     * by trace_run_cleanup(). */
    int noise_saved;                    /* 1 = save state is valid */
    int noise_saved_count;              /* number of entries in saved arrays */
    int noise_patch_indices[16];        /* patch indices of noise functions */
    ULONG noise_saved_enabled[16];     /* saved enabled state per noise func */
};

/* Per-client state */
struct client {
    LONG fd;                       /* socket fd, -1 = unused */
    ULONG addr;                    /* peer IP, network byte order */
    char recv_buf[RECV_BUF_SIZE];  /* incoming data buffer */
    int recv_len;                  /* bytes currently in recv_buf */
    int discarding;                /* overflow discard mode flag */
    int arexx_pending;             /* 1 = waiting for ARexx reply */
    struct tail_state tail;        /* TAIL tracking */
    struct trace_state trace;      /* TRACE tracking */
};

/* IP access control list entry */
struct acl_entry {
    ULONG addr;                    /* allowed IP, network byte order */
};

/* Daemon configuration (parsed from S:amigactld.conf) */
struct daemon_config {
    int port;
    int allow_remote_shutdown;
    int allow_remote_reboot;
    struct acl_entry acl[MAX_ACL_ENTRIES];
    int acl_count;
};

/* Tracked async process */
struct tracked_proc {
    int id;                      /* daemon-assigned, monotonic, starts at 1 */
    struct Task *task;           /* pointer to child process */
    char command[256];           /* command string copy */
    int status;                  /* PROC_RUNNING or PROC_EXITED */
    int rc;                      /* return code (valid when EXITED) */
    int completed;               /* set by wrapper under Forbid */
    BPTR cd_lock;                /* optional CD lock for async */
    char proc_name[32];          /* NP_Name buffer (per-slot, outlives process) */
};

/* Top-level daemon state */
struct daemon_state {
    LONG listener_fd;
    struct client clients[MAX_CLIENTS];
    struct daemon_config config;
    int running;
    struct tracked_proc procs[MAX_TRACKED_PROCS];
    int next_proc_id;               /* monotonically incrementing, starts at 1 */
    struct DateStamp startup_stamp; /* recorded at startup for UPTIME */
    struct arexx_pending arexx_slots[MAX_AREXX_PENDING];
    ULONG arexx_epoch;              /* monotonic counter */
};


/* ---- Startup output routing ---- */

/* In Workbench mode, startup messages go to a manually-managed CON: window.
 * In CLI mode, they go to stdout.  daemon_msg() routes to the right place.
 * Runtime messages (event loop, shutdown) always use printf/stdout. */
extern BPTR g_wb_console;
void daemon_msg(const char *fmt, ...);

#endif /* AMIGACTLD_DAEMON_H */
