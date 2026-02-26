/*
 * amigactld -- System information command handlers
 *
 * Implements SYSINFO, ASSIGNS, ASSIGN, PORTS, VOLUMES, TASKS,
 * LIBVER, ENV, SETENV, DEVICES, CAPABILITIES.
 * All handlers are read-only queries that always return 0 (ASSIGN
 * is the exception -- it modifies the assign list).
 *
 * Critical safety rules:
 * - No I/O between Forbid/Permit (copy data to local buffers only)
 * - No I/O under DosList lock (two-phase collect/resolve pattern)
 * - InfoData must be AllocMem'd for alignment (never stack-allocated)
 */

#include "sysinfo.h"
#include "net.h"

#include <proto/exec.h>
#include <proto/dos.h>
#include <proto/bsdsocket.h>
#include <dos/dosextens.h>
#include <exec/execbase.h>

#include <stdio.h>
#include <string.h>

/* MEMF_TOTAL was added in Kickstart 3.0 (V39) but may not be in all
 * cross-compiler header sets. */
#ifndef MEMF_TOTAL
#define MEMF_TOTAL (1L << 19)
#endif

/* MEMF_LARGEST may not be in all cross-compiler header sets. */
#ifndef MEMF_LARGEST
#define MEMF_LARGEST (1L << 17)
#endif

/* ---- Assigns collection limits ---- */

#define MAX_ASSIGNS       128
#define MAX_ASSIGN_NAME    64
#define MAX_ASSIGN_DIRS     8
#define MAX_ASSIGN_STR    256

/* Per-assign entry collected under DosList lock */
struct assign_entry {
    char name[MAX_ASSIGN_NAME];
    LONG type;                          /* DLT_DIRECTORY, DLT_LATE, DLT_NONBINDING */
    BPTR locks[MAX_ASSIGN_DIRS];        /* DLT_DIRECTORY: primary + multi-dir */
    int lock_count;
    char assign_str[MAX_ASSIGN_STR];    /* DLT_LATE/DLT_NONBINDING: path string */
};

/* ---- Ports collection limits ---- */

#define MAX_PORTS     256
#define MAX_PORT_NAME  64

/* ---- Volumes collection limits ---- */

#define MAX_VOLUMES    32
#define MAX_VOL_NAME   64

/* ---- Tasks collection limits ---- */

#define MAX_TASKS      256
#define MAX_TASK_NAME   64

struct task_entry {
    char name[MAX_TASK_NAME];
    char type[8];           /* "TASK" or "PROCESS" */
    int priority;
    char state[8];          /* "run", "ready", or "wait" */
    unsigned long stacksize;
};

/* ---- Static helpers ---- */

/* Map AmigaOS IoErr() codes to wire protocol error codes.
 * Duplicated from file.c because both are static translation-unit helpers. */
static int map_dos_error(LONG ioerr)
{
    switch (ioerr) {
    case ERROR_OBJECT_NOT_FOUND:         /* 205 */
    case ERROR_DIR_NOT_FOUND:            /* 204 */
    case ERROR_DEVICE_NOT_MOUNTED:       /* 218 */
        return ERR_NOT_FOUND;

    case ERROR_OBJECT_IN_USE:            /* 202 */
    case ERROR_DISK_WRITE_PROTECTED:     /* 214 */
    case ERROR_READ_PROTECTED:           /* 224 */
    case ERROR_DELETE_PROTECTED:         /* 222 */
    case ERROR_DIRECTORY_NOT_EMPTY:      /* 216 */
        return ERR_PERMISSION;

    case ERROR_OBJECT_EXISTS:            /* 203 */
        return ERR_EXISTS;

    case ERROR_DISK_FULL:                /* 221 */
        return ERR_IO;

    default:
        return ERR_IO;
    }
}

/* Send an ERR response derived from the current IoErr().
 * msg_prefix is prepended to the Fault() text (which starts with ": "). */
static int send_dos_error(LONG fd, const char *msg_prefix)
{
    LONG ioerr;
    int code;
    static char fault_buf[128];
    static char msg_buf[256];

    ioerr = IoErr();
    code = map_dos_error(ioerr);
    Fault(ioerr, (STRPTR)"", (STRPTR)fault_buf, sizeof(fault_buf));
    sprintf(msg_buf, "%s%s", msg_prefix, fault_buf);

    send_error(fd, code, msg_buf);
    send_sentinel(fd);
    return 0;
}

/* ---- Command handlers ---- */

int cmd_sysinfo(struct client *c, const char *args)
{
    ULONG chip_free, fast_free, total_free;
    ULONG chip_total, fast_total;
    ULONG chip_largest, fast_largest;
    char line[128];

    (void)args;

    chip_free = AvailMem(MEMF_CHIP);
    fast_free = AvailMem(MEMF_FAST);
    total_free = AvailMem(MEMF_ANY);

    chip_total = 0;
    fast_total = 0;
    if (SysBase->LibNode.lib_Version >= 39) {
        chip_total = AvailMem(MEMF_CHIP | MEMF_TOTAL);
        fast_total = AvailMem(MEMF_FAST | MEMF_TOTAL);
    }

    chip_largest = AvailMem(MEMF_CHIP | MEMF_LARGEST);
    fast_largest = AvailMem(MEMF_FAST | MEMF_LARGEST);

    send_ok(c->fd, NULL);

    sprintf(line, "chip_free=%lu", (unsigned long)chip_free);
    send_payload_line(c->fd, line);

    sprintf(line, "fast_free=%lu", (unsigned long)fast_free);
    send_payload_line(c->fd, line);

    sprintf(line, "total_free=%lu", (unsigned long)total_free);
    send_payload_line(c->fd, line);

    sprintf(line, "chip_total=%lu", (unsigned long)chip_total);
    send_payload_line(c->fd, line);

    sprintf(line, "fast_total=%lu", (unsigned long)fast_total);
    send_payload_line(c->fd, line);

    sprintf(line, "chip_largest=%lu", (unsigned long)chip_largest);
    send_payload_line(c->fd, line);

    sprintf(line, "fast_largest=%lu", (unsigned long)fast_largest);
    send_payload_line(c->fd, line);

    sprintf(line, "exec_version=%d.%d",
            (int)SysBase->LibNode.lib_Version,
            (int)SysBase->LibNode.lib_Revision);
    send_payload_line(c->fd, line);

    sprintf(line, "kickstart=%d", (int)SysBase->SoftVer);
    send_payload_line(c->fd, line);

    sprintf(line, "bsdsocket=%d.%d",
            (int)SocketBase->lib_Version,
            (int)SocketBase->lib_Revision);
    send_payload_line(c->fd, line);

    send_sentinel(c->fd);
    return 0;
}

int cmd_assigns(struct client *c, const char *args)
{
    static struct assign_entry assigns[MAX_ASSIGNS];
    int assign_count;
    struct DosList *dl;
    int i, j;

    (void)args;

    /* Phase 1: collect under DosList lock */
    assign_count = 0;

    dl = LockDosList(LDF_ASSIGNS | LDF_READ);
    while ((dl = NextDosEntry(dl, LDF_ASSIGNS)) != NULL) {
        struct assign_entry *ae;
        char *bstr;
        int len;

        if (assign_count >= MAX_ASSIGNS)
            break;

        ae = &assigns[assign_count];
        memset(ae, 0, sizeof(*ae));

        /* Copy BSTR name */
        bstr = (char *)BADDR(dl->dol_Name);
        len = (unsigned char)bstr[0];
        if (len >= MAX_ASSIGN_NAME)
            len = MAX_ASSIGN_NAME - 1;
        memcpy(ae->name, bstr + 1, len);
        ae->name[len] = '\0';

        ae->type = dl->dol_Type;

        if (dl->dol_Type == DLT_DIRECTORY) {
            struct AssignList *al;

            /* Primary lock */
            if (dl->dol_Lock) {
                ae->locks[0] = DupLock(dl->dol_Lock);
                ae->lock_count = 1;
            }

            /* Multi-dir chain */
            al = dl->dol_misc.dol_assign.dol_List;
            while (al && ae->lock_count < MAX_ASSIGN_DIRS) {
                ae->locks[ae->lock_count] = DupLock(al->al_Lock);
                ae->lock_count++;
                al = al->al_Next;
            }
        } else {
            /* DLT_LATE or DLT_NONBINDING: copy the path string */
            char *aname = (char *)dl->dol_misc.dol_assign.dol_AssignName;
            if (aname) {
                strncpy(ae->assign_str, aname, MAX_ASSIGN_STR - 1);
                ae->assign_str[MAX_ASSIGN_STR - 1] = '\0';
            }
        }

        assign_count++;
    }
    UnLockDosList(LDF_ASSIGNS | LDF_READ);

    /* Phase 2: resolve locks and send (unlocked, safe for I/O) */
    send_ok(c->fd, NULL);

    for (i = 0; i < assign_count; i++) {
        struct assign_entry *ae = &assigns[i];
        char line[512];
        char path_buf[512];
        int pos;

        if (ae->type == DLT_DIRECTORY) {
            /* Resolve each lock via NameFromLock */
            pos = 0;
            for (j = 0; j < ae->lock_count; j++) {
                char nbuf[256];

                if (!ae->locks[j])
                    continue;

                if (NameFromLock(ae->locks[j], (STRPTR)nbuf, sizeof(nbuf))) {
                    int nlen;

                    if (pos > 0 && pos < (int)sizeof(path_buf) - 1) {
                        path_buf[pos++] = ';';
                    }
                    nlen = strlen(nbuf);
                    if (pos + nlen < (int)sizeof(path_buf)) {
                        memcpy(path_buf + pos, nbuf, nlen);
                        pos += nlen;
                    }
                }
                UnLock(ae->locks[j]);
                ae->locks[j] = 0;
            }
            path_buf[pos] = '\0';
        } else {
            /* DLT_LATE or DLT_NONBINDING */
            strncpy(path_buf, ae->assign_str, sizeof(path_buf) - 1);
            path_buf[sizeof(path_buf) - 1] = '\0';
        }

        snprintf(line, sizeof(line), "%s:\t%s", ae->name, path_buf);
        send_payload_line(c->fd, line);
    }

    send_sentinel(c->fd);
    return 0;
}

int cmd_assign(struct client *c, const char *args)
{
    static char name[MAX_ASSIGN_NAME];
    static char apath[MAX_CMD_LEN + 1];
    const char *rest;
    const char *colon;
    int name_len;
    int mode; /* 0=lock, 1=late, 2=add */
    BPTR lock;

    if (args[0] == '\0') {
        send_error(c->fd, ERR_SYNTAX, "Usage: ASSIGN [LATE|ADD] NAME: [PATH]");
        send_sentinel(c->fd);
        return 0;
    }

    rest = args;
    mode = 0;

    /* Check for LATE or ADD modifier */
    if (strnicmp(rest, "LATE ", 5) == 0) {
        mode = 1;
        rest += 5;
        while (*rest == ' ' || *rest == '\t') rest++;
    } else if (strnicmp(rest, "ADD ", 4) == 0) {
        mode = 2;
        rest += 4;
        while (*rest == ' ' || *rest == '\t') rest++;
    }

    if (*rest == '\0') {
        send_error(c->fd, ERR_SYNTAX, "Missing assign name");
        send_sentinel(c->fd);
        return 0;
    }

    /* Find the colon in the assign name */
    colon = strchr(rest, ':');
    if (!colon) {
        send_error(c->fd, ERR_SYNTAX, "Assign name must include colon");
        send_sentinel(c->fd);
        return 0;
    }

    name_len = (int)(colon - rest);
    if (name_len <= 0 || name_len >= (int)sizeof(name)) {
        send_error(c->fd, ERR_SYNTAX, "Invalid assign name");
        send_sentinel(c->fd);
        return 0;
    }
    memcpy(name, rest, name_len);
    name[name_len] = '\0';

    /* Extract path (after colon, trimmed) */
    {
        const char *p = colon + 1;
        while (*p == ' ' || *p == '\t') p++;

        if (*p == '\0') {
            /* Remove assign — AssignLock with NULL lock removes it */
            if (!AssignLock((STRPTR)name, 0)) {
                send_error(c->fd, ERR_NOT_FOUND, "Assign not found");
                send_sentinel(c->fd);
                return 0;
            }
            send_ok(c->fd, NULL);
            send_sentinel(c->fd);
            return 0;
        }

        strncpy(apath, p, sizeof(apath) - 1);
        apath[sizeof(apath) - 1] = '\0';
        /* Trim trailing whitespace */
        {
            int plen = strlen(apath);
            while (plen > 0 && (apath[plen - 1] == ' ' || apath[plen - 1] == '\t'))
                plen--;
            apath[plen] = '\0';
        }
    }

    /* Execute based on mode */
    if (mode == 1) {
        /* LATE: no lock needed */
        if (!AssignLate((STRPTR)name, (STRPTR)apath)) {
            send_error(c->fd, ERR_IO, "AssignLate failed");
            send_sentinel(c->fd);
            return 0;
        }
    } else {
        /* LOCK or ADD: need a lock on the path */
        lock = Lock((STRPTR)apath, ACCESS_READ);
        if (!lock) {
            send_dos_error(c->fd, "Lock failed");
            return 0;
        }

        if (mode == 0) {
            /* AssignLock: replaces existing. Consumes lock on success. */
            if (!AssignLock((STRPTR)name, lock)) {
                UnLock(lock);
                send_dos_error(c->fd, "AssignLock failed");
                return 0;
            }
        } else {
            /* AssignAdd: adds to multi-dir. Consumes lock on success. */
            if (!AssignAdd((STRPTR)name, lock)) {
                UnLock(lock);
                send_error(c->fd, ERR_IO,
                           "AssignAdd failed (assign may not exist; "
                           "create with ASSIGN NAME: PATH first)");
                send_sentinel(c->fd);
                return 0;
            }
        }
    }

    send_ok(c->fd, NULL);
    send_sentinel(c->fd);
    return 0;
}

int cmd_ports(struct client *c, const char *args)
{
    static char port_names[MAX_PORTS][MAX_PORT_NAME];
    int port_count;
    struct Node *node;
    int i;

    (void)args;

    /* Collect under Forbid -- no I/O allowed */
    port_count = 0;

    Forbid();
    for (node = SysBase->PortList.lh_Head;
         node->ln_Succ;
         node = node->ln_Succ) {
        int j, len;

        if (!node->ln_Name)
            continue;
        if (port_count >= MAX_PORTS)
            break;

        len = strlen(node->ln_Name);
        if (len >= MAX_PORT_NAME)
            len = MAX_PORT_NAME - 1;
        memcpy(port_names[port_count], node->ln_Name, len);
        port_names[port_count][len] = '\0';

        /* Replace control characters with '?' */
        for (j = 0; j < len; j++) {
            unsigned char ch = (unsigned char)port_names[port_count][j];
            if (ch < 0x20)
                port_names[port_count][j] = '?';
        }

        port_count++;
    }
    Permit();

    /* Send results (safe for I/O now) */
    send_ok(c->fd, NULL);

    for (i = 0; i < port_count; i++) {
        send_payload_line(c->fd, port_names[i]);
    }

    send_sentinel(c->fd);
    return 0;
}

int cmd_volumes(struct client *c, const char *args)
{
    static char vol_names[MAX_VOLUMES][MAX_VOL_NAME];
    int vol_count;
    struct DosList *dl;
    int i;

    (void)args;

    /* Phase 1: collect volume names under DosList lock */
    vol_count = 0;

    dl = LockDosList(LDF_VOLUMES | LDF_READ);
    while ((dl = NextDosEntry(dl, LDF_VOLUMES)) != NULL) {
        char *bstr;
        int len;

        if (!dl->dol_Task)
            continue;  /* not mounted */
        if (vol_count >= MAX_VOLUMES)
            break;

        /* Copy BSTR name and append ':' */
        bstr = (char *)BADDR(dl->dol_Name);
        len = (unsigned char)bstr[0];
        if (len >= MAX_VOL_NAME - 2)
            len = MAX_VOL_NAME - 2;  /* room for ':' and NUL */
        memcpy(vol_names[vol_count], bstr + 1, len);
        vol_names[vol_count][len] = ':';
        vol_names[vol_count][len + 1] = '\0';

        vol_count++;
    }
    UnLockDosList(LDF_VOLUMES | LDF_READ);

    /* Phase 2: probe each volume and send (unlocked, safe for I/O) */
    send_ok(c->fd, NULL);

    for (i = 0; i < vol_count; i++) {
        BPTR lock;
        struct InfoData *info;
        unsigned long used, free_bytes, capacity;
        LONG blocksize;
        char line[256];

        lock = Lock((STRPTR)vol_names[i], ACCESS_READ);
        if (!lock)
            continue;  /* volume unmounted between phases */

        info = (struct InfoData *)AllocMem(sizeof(struct InfoData),
                                           MEMF_PUBLIC | MEMF_CLEAR);
        if (!info) {
            UnLock(lock);
            continue;
        }

        if (!Info(lock, info)) {
            FreeMem(info, sizeof(struct InfoData));
            UnLock(lock);
            continue;
        }

        used = (unsigned long)info->id_NumBlocksUsed *
               (unsigned long)info->id_BytesPerBlock;
        free_bytes = ((unsigned long)info->id_NumBlocks -
                      (unsigned long)info->id_NumBlocksUsed) *
                     (unsigned long)info->id_BytesPerBlock;
        capacity = (unsigned long)info->id_NumBlocks *
                   (unsigned long)info->id_BytesPerBlock;
        blocksize = info->id_BytesPerBlock;

        FreeMem(info, sizeof(struct InfoData));
        UnLock(lock);

        snprintf(line, sizeof(line), "%s\t%lu\t%lu\t%lu\t%ld",
                 vol_names[i], used, free_bytes, capacity,
                 (long)blocksize);
        send_payload_line(c->fd, line);
    }

    send_sentinel(c->fd);
    return 0;
}

int cmd_tasks(struct client *c, const char *args)
{
    static struct task_entry tasks[MAX_TASKS];
    int task_count;
    struct Node *node;
    struct Task *current;
    int i;

    (void)args;

    /* Collect under Forbid -- no I/O allowed */
    task_count = 0;

    Forbid();

    /* Currently running task */
    current = FindTask(NULL);
    if (current && task_count < MAX_TASKS) {
        struct task_entry *te = &tasks[task_count];
        const char *name;
        int len;

        name = current->tc_Node.ln_Name;
        if (!name)
            name = "<unnamed>";
        len = strlen(name);
        if (len >= MAX_TASK_NAME)
            len = MAX_TASK_NAME - 1;
        memcpy(te->name, name, len);
        te->name[len] = '\0';

        if (current->tc_Node.ln_Type == NT_PROCESS)
            strcpy(te->type, "PROCESS");
        else
            strcpy(te->type, "TASK");

        te->priority = (int)current->tc_Node.ln_Pri;
        strcpy(te->state, "run");
        te->stacksize = (unsigned long)((char *)current->tc_SPUpper -
                                        (char *)current->tc_SPLower);
        task_count++;
    }

    /* Ready tasks */
    for (node = SysBase->TaskReady.lh_Head;
         node->ln_Succ;
         node = node->ln_Succ) {
        struct Task *task;
        struct task_entry *te;
        const char *name;
        int len;

        if (task_count >= MAX_TASKS)
            break;

        task = (struct Task *)node;
        te = &tasks[task_count];

        name = task->tc_Node.ln_Name;
        if (!name)
            name = "<unnamed>";
        len = strlen(name);
        if (len >= MAX_TASK_NAME)
            len = MAX_TASK_NAME - 1;
        memcpy(te->name, name, len);
        te->name[len] = '\0';

        if (task->tc_Node.ln_Type == NT_PROCESS)
            strcpy(te->type, "PROCESS");
        else
            strcpy(te->type, "TASK");

        te->priority = (int)task->tc_Node.ln_Pri;
        strcpy(te->state, "ready");
        te->stacksize = (unsigned long)((char *)task->tc_SPUpper -
                                        (char *)task->tc_SPLower);
        task_count++;
    }

    /* Waiting tasks */
    for (node = SysBase->TaskWait.lh_Head;
         node->ln_Succ;
         node = node->ln_Succ) {
        struct Task *task;
        struct task_entry *te;
        const char *name;
        int len;

        if (task_count >= MAX_TASKS)
            break;

        task = (struct Task *)node;
        te = &tasks[task_count];

        name = task->tc_Node.ln_Name;
        if (!name)
            name = "<unnamed>";
        len = strlen(name);
        if (len >= MAX_TASK_NAME)
            len = MAX_TASK_NAME - 1;
        memcpy(te->name, name, len);
        te->name[len] = '\0';

        if (task->tc_Node.ln_Type == NT_PROCESS)
            strcpy(te->type, "PROCESS");
        else
            strcpy(te->type, "TASK");

        te->priority = (int)task->tc_Node.ln_Pri;
        strcpy(te->state, "wait");
        te->stacksize = (unsigned long)((char *)task->tc_SPUpper -
                                        (char *)task->tc_SPLower);
        task_count++;
    }

    Permit();

    /* Send results (safe for I/O now) */
    send_ok(c->fd, NULL);

    for (i = 0; i < task_count; i++) {
        struct task_entry *te = &tasks[i];
        char line[256];

        snprintf(line, sizeof(line), "%s\t%s\t%d\t%s\t%lu",
                 te->name, te->type, te->priority,
                 te->state, te->stacksize);
        send_payload_line(c->fd, line);
    }

    send_sentinel(c->fd);
    return 0;
}

/* ---- LIBVER ---- */

int cmd_libver(struct client *c, const char *args)
{
    struct Library *lib;
    int version;
    int revision;
    static char name_line[MAX_CMD_LEN + 8];
    char line[128];
    int len;

    if (args[0] == '\0') {
        send_error(c->fd, ERR_SYNTAX, "Missing library name");
        send_sentinel(c->fd);
        return 0;
    }

    if (stricmp(args, "exec.library") == 0) {
        version = (int)SysBase->LibNode.lib_Version;
        revision = (int)SysBase->LibNode.lib_Revision;
    } else {
        /* Check if name ends with ".device" */
        len = strlen(args);
        if (len > 7 && stricmp(args + len - 7, ".device") == 0) {
            /* Device: find in device list under Forbid */
            Forbid();
            lib = (struct Library *)FindName(&SysBase->DeviceList,
                                             (STRPTR)args);
            if (lib) {
                version = (int)lib->lib_Version;
                revision = (int)lib->lib_Revision;
            }
            Permit();

            if (!lib) {
                send_error(c->fd, ERR_NOT_FOUND, "Device not found");
                send_sentinel(c->fd);
                return 0;
            }
        } else {
            /* Library: open, read version, close */
            lib = OpenLibrary((STRPTR)args, 0);
            if (!lib) {
                send_error(c->fd, ERR_NOT_FOUND, "Library not found");
                send_sentinel(c->fd);
                return 0;
            }
            version = (int)lib->lib_Version;
            revision = (int)lib->lib_Revision;
            CloseLibrary(lib);
        }
    }

    send_ok(c->fd, NULL);

    snprintf(name_line, sizeof(name_line), "name=%s", args);
    send_payload_line(c->fd, name_line);

    sprintf(line, "version=%d.%d", version, revision);
    send_payload_line(c->fd, line);

    send_sentinel(c->fd);
    return 0;
}

/* ---- ENV ---- */

static char env_buf[4096];

int cmd_env(struct client *c, const char *args)
{
    LONG result;
    static char line[4128];  /* "value=" + 4096 + NUL */

    if (args[0] == '\0') {
        send_error(c->fd, ERR_SYNTAX, "Missing variable name");
        send_sentinel(c->fd);
        return 0;
    }

    result = GetVar((STRPTR)args, (STRPTR)env_buf, sizeof(env_buf),
                    GVF_GLOBAL_ONLY);
    if (result == -1) {
        send_error(c->fd, ERR_NOT_FOUND, "Variable not found");
        send_sentinel(c->fd);
        return 0;
    }

    send_ok(c->fd, NULL);

    sprintf(line, "value=%s", env_buf);
    send_payload_line(c->fd, line);

    if (result == (LONG)(sizeof(env_buf) - 1)) {
        send_payload_line(c->fd, "truncated=true");
    }

    send_sentinel(c->fd);
    return 0;
}

/* ---- SETENV ---- */

static char env_name[256];
static char env_path[512];

int cmd_setenv(struct client *c, const char *args)
{
    const char *p;
    const char *name_start;
    const char *name_end;
    const char *value;
    int volatile_mode;
    int name_len;

    p = args;
    volatile_mode = 0;

    /* Check for VOLATILE keyword */
    if (strnicmp(p, "VOLATILE", 8) == 0 &&
        (p[8] == ' ' || p[8] == '\t' || p[8] == '\0')) {
        volatile_mode = 1;
        p += 8;
        while (*p == ' ' || *p == '\t') p++;

        if (*p == '\0') {
            send_error(c->fd, ERR_SYNTAX,
                       "VOLATILE is a reserved keyword");
            send_sentinel(c->fd);
            return 0;
        }
    }

    if (*p == '\0') {
        send_error(c->fd, ERR_SYNTAX, "Missing variable name");
        send_sentinel(c->fd);
        return 0;
    }

    /* Extract variable name (first token) */
    name_start = p;
    while (*p && *p != ' ' && *p != '\t') p++;
    name_end = p;
    name_len = (int)(name_end - name_start);

    if (name_len <= 0 || name_len >= (int)sizeof(env_name)) {
        send_error(c->fd, ERR_SYNTAX, "Variable name too long");
        send_sentinel(c->fd);
        return 0;
    }
    memcpy(env_name, name_start, name_len);
    env_name[name_len] = '\0';

    /* Skip whitespace to find value */
    while (*p == ' ' || *p == '\t') p++;
    value = p;

    if (*value == '\0') {
        /* DELETE mode: no value provided */
        DeleteVar((STRPTR)env_name, GVF_GLOBAL_ONLY);

        if (!volatile_mode) {
            /* Remove persistent copy from ENVARC: */
            sprintf(env_path, "ENVARC:%s", env_name);
            DeleteFile((STRPTR)env_path);
        }

        send_ok(c->fd, NULL);
        send_sentinel(c->fd);
        return 0;
    }

    /* WRITE mode */
    if (volatile_mode) {
        if (!SetVar((STRPTR)env_name, (STRPTR)value, strlen(value),
                    GVF_GLOBAL_ONLY)) {
            send_error(c->fd, ERR_IO, "SetVar failed");
            send_sentinel(c->fd);
            return 0;
        }
    } else {
        if (!SetVar((STRPTR)env_name, (STRPTR)value, strlen(value),
                    GVF_GLOBAL_ONLY | GVF_SAVE_VAR)) {
            send_error(c->fd, ERR_IO, "SetVar failed");
            send_sentinel(c->fd);
            return 0;
        }
    }

    send_ok(c->fd, NULL);
    send_sentinel(c->fd);
    return 0;
}

/* ---- DEVICES ---- */

#define MAX_DEVICES 128

struct device_entry {
    char name[64];
    int version;
    int revision;
};

static struct device_entry devices[MAX_DEVICES];

int cmd_devices(struct client *c, const char *args)
{
    int dev_count;
    struct Node *node;
    int i;

    (void)args;

    /* Collect under Forbid -- no I/O allowed */
    dev_count = 0;

    Forbid();
    for (node = SysBase->DeviceList.lh_Head;
         node->ln_Succ;
         node = node->ln_Succ) {
        struct Library *dev;
        struct device_entry *de;
        const char *name;
        int len;

        if (dev_count >= MAX_DEVICES)
            break;

        dev = (struct Library *)node;
        de = &devices[dev_count];

        name = node->ln_Name;
        if (!name)
            name = "<unnamed>";
        len = strlen(name);
        if (len >= (int)sizeof(de->name))
            len = (int)sizeof(de->name) - 1;
        memcpy(de->name, name, len);
        de->name[len] = '\0';

        de->version = (int)dev->lib_Version;
        de->revision = (int)dev->lib_Revision;

        dev_count++;
    }
    Permit();

    /* Send results (safe for I/O now) */
    send_ok(c->fd, NULL);

    for (i = 0; i < dev_count; i++) {
        struct device_entry *de = &devices[i];
        char line[128];

        sprintf(line, "%s\t%d.%d", de->name, de->version, de->revision);
        send_payload_line(c->fd, line);
    }

    send_sentinel(c->fd);
    return 0;
}

/* ---- CAPABILITIES ---- */

/* Sorted list of all supported commands.
 * IMPORTANT: update this string when adding new commands. */
#define CAPABILITIES_COMMANDS \
    "APPEND,AREXX,ASSIGN,ASSIGNS,CAPABILITIES,CHECKSUM,COPY,DELETE," \
    "DEVICES,DIR,ENV,EXEC,KILL,LIBVER,MAKEDIR,PING,PORTS,PROCLIST," \
    "PROCSTAT,PROTECT,READ,REBOOT,RENAME,SETCOMMENT,SETDATE,SETENV," \
    "SHUTDOWN,SIGNAL,STAT,SYSINFO,TAIL,TASKS,UPTIME,VERSION,VOLUMES,WRITE"

int cmd_capabilities(struct client *c, const char *args)
{
    char line[128];

    (void)args;

    send_ok(c->fd, NULL);

    sprintf(line, "version=%s", AMIGACTLD_VERSION);
    send_payload_line(c->fd, line);

    send_payload_line(c->fd, "protocol=1.0");

    sprintf(line, "max_clients=%d", MAX_CLIENTS);
    send_payload_line(c->fd, line);

    sprintf(line, "max_cmd_len=%d", MAX_CMD_LEN);
    send_payload_line(c->fd, line);

    send_payload_line(c->fd, "commands=" CAPABILITIES_COMMANDS);

    send_sentinel(c->fd);
    return 0;
}
