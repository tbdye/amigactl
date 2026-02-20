/*
 * amigactld -- System information command handlers (Phase 3)
 *
 * Implements SYSINFO, ASSIGNS, PORTS, VOLUMES, TASKS.
 * All handlers are read-only queries that always return 0.
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

/* ---- Command handlers ---- */

int cmd_sysinfo(struct client *c, const char *args)
{
    ULONG chip_free, fast_free, total_free;
    ULONG chip_total, fast_total;
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
