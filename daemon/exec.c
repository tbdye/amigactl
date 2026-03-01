/*
 * amigactld -- Execution and process management
 *
 * Implements EXEC (sync + async), PROCLIST, PROCSTAT, SIGNAL, KILL
 * and the supporting process table infrastructure.
 *
 * Async processes run in child tasks created via CreateNewProcTags.
 * The child signals the daemon on completion; the main event loop
 * calls exec_scan_completed() to harvest exit codes.
 */

#include "exec.h"
#include "daemon.h"
#include "net.h"

#include <proto/exec.h>
#include <proto/dos.h>
#include <dos/dos.h>
#include <dos/dostags.h>
#include <dos/dosextens.h>

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* ---- Globals ---- */

struct daemon_state *g_daemon_state = NULL;
struct Task *g_daemon_task = NULL;
LONG g_proc_sigbit = -1;

/* Sequence counter for temp file naming (sync exec) */
static int exec_seq = 0;

/* Temp file read buffer -- static to avoid stack allocation */
static char read_buf[4096];

/* ---- Forward declarations ---- */

static int exec_sync(struct client *c, const char *args);
static int exec_async(struct client *c, const char *args);
static const char *parse_cd_prefix(const char *args, BPTR *cd_lock,
                                   struct client *c);
static BPTR find_command_segment(const char *cmdname, int *is_resident);
static void release_command_segment(BPTR seg, const char *cmdname,
                                    int is_resident);

/* ---- Initialization / cleanup ---- */

int exec_init(void)
{
    g_daemon_task = FindTask(NULL);
    g_proc_sigbit = AllocSignal(-1L);
    if (g_proc_sigbit == -1) {
        daemon_msg("Warning: no free signal bits for async exec\n");
        return -1;
    }
    return 0;
}

void exec_cleanup(void)
{
    /* Intentionally does NOT call FreeSignal.
     *
     * A slow async wrapper may still be running when the daemon
     * shuts down.  If we freed the signal bit, the wrapper's
     * Signal() call would corrupt an unrelated signal.  Leaving
     * the bit allocated is harmless -- it dies with the task. */
}

void exec_cleanup_temp_files(void)
{
    BPTR lock;
    struct FileInfoBlock *fib;

    lock = Lock((STRPTR)"T:", ACCESS_READ);
    if (!lock)
        return;

    fib = AllocDosObject(DOS_FIB, NULL);
    if (!fib) {
        UnLock(lock);
        return;
    }

    if (!Examine(lock, fib)) {
        FreeDosObject(DOS_FIB, fib);
        UnLock(lock);
        return;
    }

    while (ExNext(lock, fib)) {
        if (strncmp((char *)fib->fib_FileName, "amigactld_exec_", 15) == 0) {
            char path[128];
            sprintf(path, "T:%s", (char *)fib->fib_FileName);
            DeleteFile((STRPTR)path);
        }
    }

    FreeDosObject(DOS_FIB, fib);
    UnLock(lock);
}

void exec_scan_completed(struct daemon_state *d)
{
    int i;

    for (i = 0; i < MAX_TRACKED_PROCS; i++) {
        if (d->procs[i].status == PROC_RUNNING &&
            d->procs[i].completed == 1) {
            d->procs[i].status = PROC_EXITED;
            d->procs[i].completed = 0;
            d->procs[i].task = NULL;
        }
    }
}

void exec_shutdown_procs(struct daemon_state *d)
{
    int i;

    Forbid();
    for (i = 0; i < MAX_TRACKED_PROCS; i++) {
        if (d->procs[i].id == 0)
            continue;  /* never-used slot */
        if (d->procs[i].status == PROC_RUNNING) {
            if (d->procs[i].completed == 1) {
                /* Already finished, just mark it */
                d->procs[i].status = PROC_EXITED;
                d->procs[i].task = NULL;
            } else {
                /* Still running -- ask it to stop */
                Signal(d->procs[i].task, SIGBREAKF_CTRL_C);
                d->procs[i].status = PROC_EXITED;
                d->procs[i].rc = -1;
                d->procs[i].task = NULL;
            }
        }
    }
    Permit();

    /* Note: wrappers that received CTRL_C but haven't exited yet may
     * still write to slot->rc, slot->completed, and call Signal() on
     * the daemon.  This is harmless -- the daemon is about to exit. */
}

/* ---- Static helpers ---- */

/* Parse an optional CD=<path> prefix from args.
 * If present, locks the directory and advances the returned pointer
 * past the CD= token.  On lock failure, sends ERR 200 and returns NULL.
 * If no CD= prefix, *cd_lock is set to 0 and args is returned as-is. */
static const char *parse_cd_prefix(const char *args, BPTR *cd_lock,
                                   struct client *c)
{
    const char *p;
    const char *end;
    char cd_path[512];
    int pathlen;

    *cd_lock = 0;

    if (strnicmp(args, "CD=", 3) != 0)
        return args;

    /* Find the end of the CD path (next whitespace or end of string) */
    p = args + 3;
    end = p;
    while (*end && *end != ' ' && *end != '\t')
        end++;

    pathlen = (int)(end - p);
    if (pathlen <= 0 || pathlen >= (int)sizeof(cd_path)) {
        send_error(c->fd, ERR_NOT_FOUND, "Directory not found");
        send_sentinel(c->fd);
        return NULL;
    }

    memcpy(cd_path, p, pathlen);
    cd_path[pathlen] = '\0';

    *cd_lock = Lock((STRPTR)cd_path, ACCESS_READ);
    if (!*cd_lock) {
        send_error(c->fd, ERR_NOT_FOUND, "Directory not found");
        send_sentinel(c->fd);
        return NULL;
    }

    /* Advance past CD= token and any trailing whitespace */
    p = end;
    while (*p == ' ' || *p == '\t')
        p++;

    return p;
}

/* Find and load a command segment by name.
 * Search order: resident list, CLI command path, C:, current directory.
 * Sets *is_resident to indicate how to release the segment:
 *   0 = loaded from disk (UnLoadSeg to release)
 *   1 = resident with incremented use count (decrement to release)
 *   2 = permanent resident, seg_UC < 0 (no action needed to release)
 * Returns segment (BPTR) or 0 if not found. */
static BPTR find_command_segment(const char *cmdname, int *is_resident)
{
    BPTR seg;
    struct Segment *rseg;
    struct Process *me;
    struct CommandLineInterface *cli;
    LONG *path_entry;
    BPTR old;
    char path[256];

    *is_resident = 0;

    /* Check the resident list.
     * seg_UC special values:
     *   CMD_SYSTEM (-1)   -- permanent system command, never unloaded
     *   CMD_INTERNAL (-2) -- internal command, never unloaded
     *   CMD_DISABLED (-999) -- disabled segment, must not be used
     *   >= 0 -- user-loaded, reference counted */
    Forbid();
    rseg = FindSegment((STRPTR)cmdname, NULL, 0);
    if (rseg) {
        if (rseg->seg_UC == CMD_DISABLED) {
            /* Disabled segment -- skip, fall through to path search */
        } else if (rseg->seg_UC < 0) {
            /* System or internal command -- use directly, don't touch
             * the use count.  These are permanent and never unloaded. */
            seg = rseg->seg_Seg;
            *is_resident = 2;
            Permit();
            return seg;
        } else {
            /* User-loaded resident -- increment use count */
            seg = rseg->seg_Seg;
            rseg->seg_UC++;
            *is_resident = 1;
            Permit();
            return seg;
        }
    }
    Permit();

    /* Walk the CLI command path */
    me = (struct Process *)FindTask(NULL);
    if (me->pr_CLI) {
        cli = (struct CommandLineInterface *)BADDR(me->pr_CLI);
        path_entry = (LONG *)BADDR(cli->cli_CommandDir);
        while (path_entry) {
            old = CurrentDir((BPTR)path_entry[1]);
            seg = LoadSeg((STRPTR)cmdname);
            CurrentDir(old);
            if (seg)
                return seg;
            path_entry = (LONG *)BADDR(path_entry[0]);
        }
    }

    /* Try C: */
    if (strlen(cmdname) < sizeof(path) - 2) {
        sprintf(path, "C:%s", cmdname);
        seg = LoadSeg((STRPTR)path);
        if (seg)
            return seg;
    }

    /* Try bare name (current directory) */
    seg = LoadSeg((STRPTR)cmdname);
    return seg;
}

/* Release a segment obtained from find_command_segment.
 * is_resident: 0=UnLoadSeg, 1=decrement use count, 2=permanent (no-op). */
static void release_command_segment(BPTR seg, const char *cmdname,
                                    int is_resident)
{
    struct Segment *rseg;

    if (is_resident == 1) {
        Forbid();
        rseg = FindSegment((STRPTR)cmdname, NULL, 0);
        if (rseg && rseg->seg_UC > 0)
            rseg->seg_UC--;
        Permit();
    } else if (is_resident == 0) {
        UnLoadSeg(seg);
    }
    /* is_resident == 2: permanent resident, nothing to do */
}

/* ---- exec_sync ---- */

static int exec_sync(struct client *c, const char *args)
{
    const char *command;
    BPTR cd_lock;
    BPTR old_lock;
    BPTR fh_out;
    BPTR fh_in;
    char temp_path[64];
    LONG rc;
    LONG n;
    char info[32];

    /* Parse optional CD= prefix */
    command = parse_cd_prefix(args, &cd_lock, c);
    if (!command)
        return 0; /* error already sent */

    /* Validate non-empty command */
    if (*command == '\0') {
        if (cd_lock)
            UnLock(cd_lock);
        send_error(c->fd, ERR_SYNTAX, "Missing command");
        send_sentinel(c->fd);
        return 0;
    }

    /* Create temp file for capturing output */
    exec_seq++;
    sprintf(temp_path, "T:amigactld_exec_%d.tmp", exec_seq);

    fh_out = Open((STRPTR)temp_path, MODE_NEWFILE);
    if (!fh_out) {
        if (cd_lock)
            UnLock(cd_lock);
        send_error(c->fd, ERR_INTERNAL, "Cannot create temp file");
        send_sentinel(c->fd);
        return 0;
    }

    fh_in = Open((STRPTR)"NIL:", MODE_OLDFILE);
    if (!fh_in) {
        Close(fh_out);
        DeleteFile((STRPTR)temp_path);
        if (cd_lock)
            UnLock(cd_lock);
        send_error(c->fd, ERR_INTERNAL, "Cannot open NIL:");
        send_sentinel(c->fd);
        return 0;
    }

    /* Change directory if requested */
    old_lock = 0;
    if (cd_lock)
        old_lock = CurrentDir(cd_lock);

    /* Execute the command synchronously.
     * SYS_Asynch is NOT used, so we retain ownership of the handles
     * and must Close() them ourselves. */
    rc = SystemTags((STRPTR)command,
                    SYS_Output, fh_out,
                    SYS_Input, fh_in,
                    TAG_DONE);

    /* Restore directory */
    if (cd_lock) {
        CurrentDir(old_lock);
        UnLock(cd_lock);
    }

    Close(fh_out);
    Close(fh_in);

    if (rc == -1) {
        DeleteFile((STRPTR)temp_path);
        send_error(c->fd, ERR_INTERNAL, "Command execution failed");
        send_sentinel(c->fd);
        return 0;
    }

    /* Re-open temp file to read output */
    fh_out = Open((STRPTR)temp_path, MODE_OLDFILE);
    if (!fh_out) {
        DeleteFile((STRPTR)temp_path);
        send_error(c->fd, ERR_INTERNAL, "Cannot read command output");
        send_sentinel(c->fd);
        return 0;
    }

    /* Rewind to start of temp file */
    Seek(fh_out, 0, OFFSET_END);
    (void)Seek(fh_out, 0, OFFSET_BEGINNING);

    sprintf(info, "rc=%ld", (long)rc);
    send_ok(c->fd, info);

    /* Send output in chunks */
    n = Read(fh_out, (STRPTR)read_buf, sizeof(read_buf));
    while (n > 0) {
        if (send_data_chunk(c->fd, read_buf, (int)n) < 0) {
            Close(fh_out);
            DeleteFile((STRPTR)temp_path);
            return 0;
        }
        n = Read(fh_out, (STRPTR)read_buf, sizeof(read_buf));
    }

    send_end(c->fd);
    send_sentinel(c->fd);

    Close(fh_out);
    DeleteFile((STRPTR)temp_path);
    return 0;
}

/* ---- exec_async ---- */

static int exec_async(struct client *c, const char *args)
{
    const char *command;
    BPTR cd_lock;
    int slot;
    int i;
    int oldest_slot;
    int oldest_id;
    struct Process *proc;
    char info[16];

    /* Check that async exec is available */
    if (g_proc_sigbit < 0) {
        send_error(c->fd, ERR_INTERNAL, "Async exec unavailable");
        send_sentinel(c->fd);
        return 0;
    }

    /* Parse optional CD= prefix */
    command = parse_cd_prefix(args, &cd_lock, c);
    if (!command)
        return 0; /* error already sent */

    /* Validate non-empty command */
    if (*command == '\0') {
        if (cd_lock)
            UnLock(cd_lock);
        send_error(c->fd, ERR_SYNTAX, "Missing command");
        send_sentinel(c->fd);
        return 0;
    }

    /* Find a free slot.  Prefer the first non-RUNNING slot.
     * If all are RUNNING, find the oldest EXITED to evict. */
    slot = -1;
    oldest_slot = -1;
    oldest_id = 0x7FFFFFFF;

    for (i = 0; i < MAX_TRACKED_PROCS; i++) {
        if (g_daemon_state->procs[i].id == 0) {
            /* Never-used slot -- best choice */
            slot = i;
            break;
        }
        if (g_daemon_state->procs[i].status != PROC_RUNNING) {
            /* Exited slot -- candidate for eviction */
            if (g_daemon_state->procs[i].id < oldest_id) {
                oldest_id = g_daemon_state->procs[i].id;
                oldest_slot = i;
            }
        }
    }

    if (slot < 0)
        slot = oldest_slot;

    if (slot < 0) {
        /* All slots are RUNNING, none exited */
        if (cd_lock)
            UnLock(cd_lock);
        send_error(c->fd, ERR_INTERNAL, "Process table full");
        send_sentinel(c->fd);
        return 0;
    }

    /* Populate the slot before creating the process.
     * The Forbid/Permit around CreateNewProcTags ensures the child
     * cannot run (and look for its slot) until we've stored task. */
    strncpy(g_daemon_state->procs[slot].command, command, 255);
    g_daemon_state->procs[slot].command[255] = '\0';
    g_daemon_state->procs[slot].status = PROC_RUNNING;
    g_daemon_state->procs[slot].completed = 0;
    g_daemon_state->procs[slot].rc = 0;
    g_daemon_state->procs[slot].id = g_daemon_state->next_proc_id++;
    g_daemon_state->procs[slot].cd_lock = cd_lock;

    Forbid();
    proc = CreateNewProcTags(
        NP_Entry, (ULONG)async_wrapper,
        NP_Name, (ULONG)"amigactld-exec",
        NP_StackSize, 16384,
        NP_Cli, TRUE,
        TAG_DONE);
    if (proc)
        g_daemon_state->procs[slot].task = (struct Task *)proc;
    Permit();

    if (!proc) {
        /* Creation failed -- clean up the slot */
        g_daemon_state->procs[slot].id = 0;
        g_daemon_state->procs[slot].status = PROC_EXITED;
        g_daemon_state->procs[slot].task = NULL;
        if (cd_lock)
            UnLock(cd_lock);
        g_daemon_state->procs[slot].cd_lock = 0;
        send_error(c->fd, ERR_INTERNAL, "Failed to create process");
        send_sentinel(c->fd);
        return 0;
    }

    sprintf(info, "%d", g_daemon_state->procs[slot].id);
    send_ok(c->fd, info);
    send_sentinel(c->fd);
    return 0;
}

/* ---- Async wrapper (runs in child process) ---- */

/* __saveds is not needed: the large data model (-noixemul) uses absolute
 * addressing for globals, so the a4 small-data base register setup that
 * __saveds provides is redundant and the compiler warns about it. */
void async_wrapper(void)
{
    struct Task *me;
    struct tracked_proc *slot;
    char command[256];
    char cmdname[128];
    char argbuf[256];
    const char *args_start;
    int cmdlen;
    int arglen;
    int is_resident;
    int used_runcommand;
    BPTR seg;
    BPTR cd_lock;
    BPTR old_lock;
    BPTR nil_in;
    BPTR nil_out;
    BPTR old_in;
    BPTR old_out;
    LONG rc;
    int i;

    me = FindTask(NULL);

    /* Find our slot in the process table */
    slot = NULL;
    for (i = 0; i < MAX_TRACKED_PROCS; i++) {
        if (g_daemon_state->procs[i].status == PROC_RUNNING &&
            g_daemon_state->procs[i].task == me) {
            slot = &g_daemon_state->procs[i];
            break;
        }
    }

    if (!slot)
        return; /* should never happen */

    /* Copy command and cd_lock from slot to local storage.
     * The slot's command buffer must not be read after we signal
     * completion, as the daemon may reuse the slot. */
    strncpy(command, slot->command, sizeof(command) - 1);
    command[sizeof(command) - 1] = '\0';
    cd_lock = slot->cd_lock;

    /* Open NIL: for both input and output */
    nil_in = Open((STRPTR)"NIL:", MODE_OLDFILE);
    nil_out = Open((STRPTR)"NIL:", MODE_NEWFILE);
    if (!nil_in || !nil_out) {
        if (nil_in) Close(nil_in);
        if (nil_out) Close(nil_out);
        if (cd_lock) {
            UnLock(cd_lock);
            slot->cd_lock = 0;
        }
        Forbid();
        slot->rc = -1;
        slot->completed = 1;
        Signal(g_daemon_task, 1L << g_proc_sigbit);
        return; /* returns under Forbid -- RemTask is safe */
    }

    /* Change directory if requested */
    old_lock = 0;
    if (cd_lock)
        old_lock = CurrentDir(cd_lock);

    /* Parse command name (first whitespace-delimited word) and arguments.
     * We need these separately for RunCommand, which takes a loaded
     * segment and an argument string rather than a shell command line. */
    args_start = command;
    cmdlen = 0;
    while (*args_start && *args_start != ' ' && *args_start != '\t' &&
           cmdlen < (int)sizeof(cmdname) - 1) {
        cmdname[cmdlen++] = *args_start++;
    }
    cmdname[cmdlen] = '\0';

    /* Skip whitespace between command name and arguments */
    while (*args_start == ' ' || *args_start == '\t')
        args_start++;

    /* Build newline-terminated argument string for RunCommand */
    arglen = (int)strlen(args_start);
    if (arglen >= (int)sizeof(argbuf) - 1)
        arglen = (int)sizeof(argbuf) - 2; /* leave room for \n */
    memcpy(argbuf, args_start, arglen);
    argbuf[arglen] = '\n';
    arglen++; /* include the newline in the length */

    /* Try to locate the command binary so we can use RunCommand.
     *
     * RunCommand executes a loaded segment in the CURRENT process
     * context (no child process).  This means CTRL_C signals delivered
     * to the wrapper task via cmd_signal_proc() are visible to the
     * running command through CheckSignal().  SystemTags creates a
     * child shell process, so signals sent to the wrapper never reach
     * the actual command.
     *
     * Fall back to SystemTags when the binary can't be found -- this
     * handles shell built-ins, script files, and other cases where
     * there's no loadable segment.
     *
     * Note: if the command calls Exit() (the DOS function), the wrapper
     * process terminates without cleanup.  The slot remains PROC_RUNNING.
     * This is inherent to RunCommand -- the same risk exists with
     * SystemTags if the child shell exits abnormally. */
    used_runcommand = 0;
    seg = find_command_segment(cmdname, &is_resident);
    if (seg) {
        old_in = SelectInput(nil_in);
        old_out = SelectOutput(nil_out);
        rc = RunCommand(seg, 16384, (STRPTR)argbuf, arglen);
        SelectInput(old_in);
        SelectOutput(old_out);
        release_command_segment(seg, cmdname, is_resident);
        used_runcommand = 1;
    }

    if (!used_runcommand) {
        /* Fallback: use SystemTags for shell built-ins, scripts, etc. */
        rc = SystemTags((STRPTR)command,
                        SYS_Input, nil_in,
                        SYS_Output, nil_out,
                        TAG_DONE);
    }

    /* Restore directory and release the lock */
    if (cd_lock) {
        CurrentDir(old_lock);
        UnLock(cd_lock);
    }

    Close(nil_in);
    Close(nil_out);

    /* Clear cd_lock in the slot (already unlocked above) to prevent
     * double-free if the slot is cleaned up during shutdown */
    slot->cd_lock = 0;

    /* Store return code */
    if (rc == -1)
        slot->rc = -1;
    else
        slot->rc = (int)rc;

    /* Signal completion under Forbid.
     * Forbid prevents the daemon from calling RemTask or reusing
     * the slot between setting completed and returning (which triggers
     * the system's RemTask). */
    Forbid();
    slot->completed = 1;
    Signal(g_daemon_task, 1L << g_proc_sigbit);
    /* Return under Forbid -- system's RemTask is safe under Forbid */
}

/* ---- Command handlers ---- */

int cmd_exec(struct client *c, const char *args)
{
    const char *p;

    if (args[0] == '\0') {
        send_error(c->fd, ERR_SYNTAX, "Missing command");
        send_sentinel(c->fd);
        return 0;
    }

    /* Check for ASYNC prefix (case-insensitive) */
    if (strnicmp(args, "ASYNC", 5) == 0 &&
        (args[5] == ' ' || args[5] == '\t' || args[5] == '\0')) {
        /* Advance past ASYNC and whitespace */
        p = args + 5;
        while (*p == ' ' || *p == '\t')
            p++;
        return exec_async(c, p);
    }

    return exec_sync(c, args);
}

int cmd_proclist(struct client *c, const char *args)
{
    int i;
    int found;
    char line[384];
    const char *status_str;
    const char *rc_str;
    char rc_buf[16];

    (void)args; /* unused */

    found = 0;
    send_ok(c->fd, NULL);

    for (i = 0; i < MAX_TRACKED_PROCS; i++) {
        if (g_daemon_state->procs[i].id > 0) {
            found = 1;

            if (g_daemon_state->procs[i].status == PROC_RUNNING) {
                status_str = "RUNNING";
                rc_str = "-";
            } else {
                status_str = "EXITED";
                sprintf(rc_buf, "%d", g_daemon_state->procs[i].rc);
                rc_str = rc_buf;
            }

            sprintf(line, "%d\t%s\t%s\t%s",
                    g_daemon_state->procs[i].id,
                    g_daemon_state->procs[i].command,
                    status_str,
                    rc_str);
            send_payload_line(c->fd, line);
        }
    }

    (void)found;
    send_sentinel(c->fd);
    return 0;
}

int cmd_procstat(struct client *c, const char *args)
{
    int target_id;
    char *endp;
    int i;
    struct tracked_proc *slot;
    char line[384];
    const char *status_str;

    if (args[0] == '\0') {
        send_error(c->fd, ERR_SYNTAX, "Missing process ID");
        send_sentinel(c->fd);
        return 0;
    }

    target_id = (int)strtol(args, &endp, 10);
    if (*endp != '\0' && *endp != ' ' && *endp != '\t') {
        send_error(c->fd, ERR_SYNTAX, "Invalid process ID");
        send_sentinel(c->fd);
        return 0;
    }

    if (target_id <= 0) {
        send_error(c->fd, ERR_SYNTAX, "Invalid process ID");
        send_sentinel(c->fd);
        return 0;
    }

    /* Find the slot */
    slot = NULL;
    for (i = 0; i < MAX_TRACKED_PROCS; i++) {
        if (g_daemon_state->procs[i].id == target_id) {
            slot = &g_daemon_state->procs[i];
            break;
        }
    }

    if (!slot) {
        send_error(c->fd, ERR_NOT_FOUND, "Process not found");
        send_sentinel(c->fd);
        return 0;
    }

    status_str = (slot->status == PROC_RUNNING) ? "RUNNING" : "EXITED";

    send_ok(c->fd, NULL);

    sprintf(line, "id=%d", slot->id);
    send_payload_line(c->fd, line);

    sprintf(line, "command=%s", slot->command);
    send_payload_line(c->fd, line);

    sprintf(line, "status=%s", status_str);
    send_payload_line(c->fd, line);

    if (slot->status == PROC_RUNNING) {
        send_payload_line(c->fd, "rc=-");
    } else {
        sprintf(line, "rc=%d", slot->rc);
        send_payload_line(c->fd, line);
    }

    send_sentinel(c->fd);
    return 0;
}

int cmd_signal_proc(struct client *c, const char *args)
{
    int target_id;
    char *endp;
    const char *sig_name;
    ULONG sigflag;
    int i;
    struct tracked_proc *slot;

    if (args[0] == '\0') {
        send_error(c->fd, ERR_SYNTAX, "Missing process ID");
        send_sentinel(c->fd);
        return 0;
    }

    target_id = (int)strtol(args, &endp, 10);
    if (*endp != '\0' && *endp != ' ' && *endp != '\t') {
        send_error(c->fd, ERR_SYNTAX, "Invalid process ID");
        send_sentinel(c->fd);
        return 0;
    }

    if (target_id <= 0) {
        send_error(c->fd, ERR_SYNTAX, "Invalid process ID");
        send_sentinel(c->fd);
        return 0;
    }

    /* Find the slot */
    slot = NULL;
    for (i = 0; i < MAX_TRACKED_PROCS; i++) {
        if (g_daemon_state->procs[i].id == target_id) {
            slot = &g_daemon_state->procs[i];
            break;
        }
    }

    if (!slot) {
        send_error(c->fd, ERR_NOT_FOUND, "Process not found");
        send_sentinel(c->fd);
        return 0;
    }

    /* Check status before signal name (per spec error checking order) */
    if (slot->status != PROC_RUNNING) {
        send_error(c->fd, ERR_NOT_FOUND, "Process not running");
        send_sentinel(c->fd);
        return 0;
    }

    /* Parse optional signal name (default: CTRL_C) */
    sig_name = endp;
    while (*sig_name == ' ' || *sig_name == '\t')
        sig_name++;

    if (*sig_name == '\0') {
        sigflag = SIGBREAKF_CTRL_C;
    } else if (stricmp((char *)sig_name, "CTRL_C") == 0) {
        sigflag = SIGBREAKF_CTRL_C;
    } else if (stricmp((char *)sig_name, "CTRL_D") == 0) {
        sigflag = SIGBREAKF_CTRL_D;
    } else if (stricmp((char *)sig_name, "CTRL_E") == 0) {
        sigflag = SIGBREAKF_CTRL_E;
    } else if (stricmp((char *)sig_name, "CTRL_F") == 0) {
        sigflag = SIGBREAKF_CTRL_F;
    } else {
        send_error(c->fd, ERR_SYNTAX, "Invalid signal name");
        send_sentinel(c->fd);
        return 0;
    }

    Forbid();
    if (slot->status != PROC_RUNNING || slot->completed) {
        Permit();
        send_error(c->fd, ERR_NOT_FOUND, "Process not running");
        send_sentinel(c->fd);
        return 0;
    }

    Signal(slot->task, sigflag);
    Permit();

    send_ok(c->fd, NULL);
    send_sentinel(c->fd);
    return 0;
}

int cmd_kill(struct client *c, const char *args)
{
    int target_id;
    char *endp;
    int i;
    struct tracked_proc *slot;

    if (!g_daemon_state->config.allow_remote_shutdown) {
        send_error(c->fd, ERR_PERMISSION, "Remote kill not permitted");
        send_sentinel(c->fd);
        return 0;
    }

    if (args[0] == '\0') {
        send_error(c->fd, ERR_SYNTAX, "Missing process ID");
        send_sentinel(c->fd);
        return 0;
    }

    target_id = (int)strtol(args, &endp, 10);
    if (*endp != '\0' && *endp != ' ' && *endp != '\t') {
        send_error(c->fd, ERR_SYNTAX, "Invalid process ID");
        send_sentinel(c->fd);
        return 0;
    }

    if (target_id <= 0) {
        send_error(c->fd, ERR_SYNTAX, "Invalid process ID");
        send_sentinel(c->fd);
        return 0;
    }

    /* Find the slot */
    slot = NULL;
    for (i = 0; i < MAX_TRACKED_PROCS; i++) {
        if (g_daemon_state->procs[i].id == target_id) {
            slot = &g_daemon_state->procs[i];
            break;
        }
    }

    if (!slot) {
        send_error(c->fd, ERR_NOT_FOUND, "Process not found");
        send_sentinel(c->fd);
        return 0;
    }

    if (slot->status != PROC_RUNNING) {
        send_error(c->fd, ERR_NOT_FOUND, "Process not running");
        send_sentinel(c->fd);
        return 0;
    }

    /* Kill under Forbid to prevent race with async_wrapper completion.
     * Must check the completed flag: if the wrapper already set it,
     * the task is gone and RemTask would crash. */
    Forbid();
    if (slot->completed == 1) {
        /* Already finished -- just transition to EXITED normally */
        slot->status = PROC_EXITED;
        slot->completed = 0;
        slot->task = NULL;
    } else {
        /* Still running -- forcibly remove the task */
        RemTask(slot->task);
        slot->status = PROC_EXITED;
        slot->rc = -1;
        slot->task = NULL;
        /* Clean up cd_lock if the wrapper hadn't gotten to it yet */
        if (slot->cd_lock) {
            UnLock(slot->cd_lock);
            slot->cd_lock = 0;
        }
    }
    Permit();

    send_ok(c->fd, NULL);
    send_sentinel(c->fd);
    return 0;
}
