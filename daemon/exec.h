/*
 * amigactld -- Execution and process management (Phase 3)
 *
 * EXEC (sync + async), PROCLIST, PROCSTAT, SIGNAL, KILL.
 */

#ifndef AMIGACTLD_EXEC_H
#define AMIGACTLD_EXEC_H

#include "daemon.h"

/* Command handlers */
int cmd_exec(struct client *c, const char *args);
int cmd_proclist(struct client *c, const char *args);
int cmd_procstat(struct client *c, const char *args);
int cmd_signal_proc(struct client *c, const char *args);
int cmd_kill(struct client *c, const char *args);

/* Called from main.c at startup to allocate the process completion
 * signal bit and store the daemon's Task pointer.
 * Returns 0 on success, -1 if AllocSignal fails (non-fatal: EXEC ASYNC
 * will return ERR 500 but other commands work). */
int exec_init(void);

/* Called from main.c at shutdown to free the signal bit.
 * Intentionally does NOT call FreeSignal -- a slow async wrapper
 * may still Signal() the daemon after cleanup. The bit dies with
 * the daemon's task. */
void exec_cleanup(void);

/* Called from main.c event loop when proc_complete_signal fires.
 * Scans process table for completed entries, transitions to EXITED. */
void exec_scan_completed(struct daemon_state *d);

/* Called from main.c at startup to delete stale T:amigactld_exec*.tmp
 * files from previous daemon runs. */
void exec_cleanup_temp_files(void);

/* Called from main.c shutdown to signal CTRL_C to all RUNNING procs
 * and mark them EXITED under Forbid. */
void exec_shutdown_procs(struct daemon_state *d);

/* Globals needed by async wrapper and main.c event loop */
extern struct daemon_state *g_daemon_state;
extern struct Task *g_daemon_task;
extern LONG g_proc_sigbit;

#endif /* AMIGACTLD_EXEC_H */
