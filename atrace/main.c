/*
 * atrace -- system library call tracing for AmigaOS
 *
 * Loader binary: installs patches, allocates ring buffer,
 * registers named semaphore for IPC with amigactld.
 */

#include "atrace.h"

#include <proto/exec.h>
#include <proto/dos.h>

#include <stdio.h>
#include <string.h>
#include <stddef.h>  /* offsetof */

/* Stack size for libnix */
unsigned long __stack = 8192;

/* Compile-time struct size verification */
typedef char assert_event_size [(sizeof(struct atrace_event) == 64) ? 1 : -1];
typedef char assert_patch_size [(sizeof(struct atrace_patch) == 40) ? 1 : -1];
typedef char assert_ringbuf_hdr[(sizeof(struct atrace_ringbuf) == 16) ? 1 : -1];
typedef char assert_anchor_size[(sizeof(struct atrace_anchor) == 84) ? 1 : -1];

/* ReadArgs template */
#define TEMPLATE "BUFSZ/K/N,DISABLE/S,STATUS/S,ENABLE/S,QUIT/S,FUNCS/M"

enum {
    ARG_BUFSZ,
    ARG_DISABLE,
    ARG_STATUS,
    ARG_ENABLE,
    ARG_QUIT,
    ARG_FUNCS,
    ARG_COUNT
};

/* From ringbuf.c */
extern struct atrace_ringbuf *ringbuf_alloc(ULONG capacity);

/* From stub_gen.c */
extern int stub_generate_and_install(
    struct atrace_anchor *anchor,
    struct atrace_patch *patch,
    struct Library *libbase,
    struct atrace_event *entries);

/* Functions that are auto-disabled by default due to high frequency.
 * These are enabled automatically when filter_task is set (TRACE RUN),
 * because per-task volume is manageable. The user can also manually
 * enable them via "atrace_loader ENABLE <funcname>".
 *
 * MUST match the noise_func_names table in daemon/trace.c exactly. */
static const char *noise_func_names[] = {
    "FindPort",
    "FindSemaphore",
    "FindTask",
    "GetMsg",
    "PutMsg",
    "ObtainSemaphore",
    "ReleaseSemaphore",
    "AllocMem",
    NULL  /* sentinel */
};

/* Local functions */
static int find_patch_by_name(struct atrace_anchor *anchor, const char *name);
static int do_install(ULONG capacity, int start_disabled, STRPTR *funcs);
static int do_status(struct atrace_anchor *anchor);
static int do_enable(struct atrace_anchor *anchor, STRPTR *funcs);
static int do_disable(struct atrace_anchor *anchor, STRPTR *funcs);
static int do_quit(struct atrace_anchor *anchor);
static struct atrace_anchor *find_anchor(void);

int main(int argc, char **argv)
{
    struct RDArgs *rdargs;
    LONG args[ARG_COUNT];
    struct atrace_anchor *anchor;
    ULONG capacity;

    (void)argc;
    (void)argv;

    memset(args, 0, sizeof(args));

    rdargs = ReadArgs((STRPTR)TEMPLATE, args, NULL);
    if (!rdargs) {
        printf("Usage: atrace_loader [BUFSZ <n>] [DISABLE] [STATUS] [ENABLE] [QUIT] [func ...]\n");
        return RETURN_FAIL;
    }

    /* Determine buffer capacity */
    capacity = ATRACE_DEFAULT_BUFSZ;
    if (args[ARG_BUFSZ])
        capacity = (ULONG)(*(LONG *)args[ARG_BUFSZ]);
    if (capacity < 16)
        capacity = 16;

    /* Check for existing installation */
    anchor = find_anchor();

    if (anchor) {
        /* Already loaded -- handle reconfiguration commands.
         * FreeArgs is deferred because FUNCS/M pointers are owned
         * by ReadArgs and become invalid after FreeArgs. */
        int rc;
        if (args[ARG_STATUS]) {
            rc = do_status(anchor);
            FreeArgs(rdargs);
            return rc;
        }
        if (args[ARG_ENABLE]) {
            rc = do_enable(anchor, (STRPTR *)args[ARG_FUNCS]);
            FreeArgs(rdargs);
            return rc;
        }
        if (args[ARG_DISABLE]) {
            rc = do_disable(anchor, (STRPTR *)args[ARG_FUNCS]);
            FreeArgs(rdargs);
            return rc;
        }
        if (args[ARG_QUIT]) {
            rc = do_quit(anchor);
            FreeArgs(rdargs);
            return rc;
        }
        printf("atrace already loaded. Use STATUS, ENABLE, DISABLE, or QUIT.\n");
        FreeArgs(rdargs);
        return RETURN_WARN;
    }

    /* Not loaded -- STATUS/ENABLE/QUIT without installation is an error */
    if (args[ARG_STATUS] || args[ARG_ENABLE] || args[ARG_QUIT]) {
        printf("atrace is not loaded.\n");
        FreeArgs(rdargs);
        return RETURN_WARN;
    }

    /* Fresh install -- FreeArgs deferred because FUNCS/M pointers
     * are owned by ReadArgs. */
    {
        int rc;
        int start_disabled = args[ARG_DISABLE] != 0;
        rc = do_install(capacity, start_disabled,
                        (STRPTR *)args[ARG_FUNCS]);
        FreeArgs(rdargs);
        return rc;
    }
}

/* ---- Find existing atrace installation via named semaphore ---- */

static struct atrace_anchor *find_anchor(void)
{
    struct SignalSemaphore *sem;

    Forbid();
    sem = FindSemaphore((STRPTR)ATRACE_SEM_NAME);
    Permit();

    if (!sem)
        return NULL;

    /* Validate magic to avoid matching a random same-named semaphore */
    {
        struct atrace_anchor *a = (struct atrace_anchor *)sem;
        if (a->magic != ATRACE_MAGIC)
            return NULL;
    }

    return (struct atrace_anchor *)sem;
}

/* ---- Patch name lookup ---- */

/* Search atrace_libs[]/func_info[] for a case-insensitive match on
 * function name. Returns the global patch index (0-29), or -1 if
 * not found. The global index is computed sequentially through all
 * libraries' functions in order, matching the installation order
 * in do_install(). The anchor parameter is unused but kept for
 * consistency with the API -- the lookup uses the static func_info
 * tables from funcs.c. */
static int find_patch_by_name(struct atrace_anchor *anchor, const char *name)
{
    int li, fi;
    int idx;

    (void)anchor;

    idx = 0;
    for (li = 0; li < atrace_lib_count; li++) {
        struct lib_info *lib = &atrace_libs[li];
        for (fi = 0; fi < (int)lib->func_count; fi++) {
            if (stricmp(name, lib->funcs[fi].name) == 0)
                return idx;
            idx++;
        }
    }
    return -1;
}

/* ---- Fresh installation ---- */

static int do_install(ULONG capacity, int start_disabled, STRPTR *funcs)
{
    struct atrace_anchor *anchor;
    struct atrace_ringbuf *ring;
    struct atrace_patch *patches;
    struct atrace_event *entries;
    int total_patches;
    int li, fi;
    int patch_idx;

    total_patches = 0;
    for (li = 0; li < atrace_lib_count; li++)
        total_patches += atrace_libs[li].func_count;

    /* Validate function names before allocating anything */
    if (funcs) {
        STRPTR *fp;
        for (fp = funcs; *fp; fp++) {
            if (find_patch_by_name(NULL, (const char *)*fp) < 0) {
                printf("Unknown function: %s\n", (const char *)*fp);
                return RETURN_FAIL;
            }
        }
    }

    /* 1. Allocate anchor */
    anchor = (struct atrace_anchor *)AllocMem(
        sizeof(struct atrace_anchor), MEMF_PUBLIC | MEMF_CLEAR);
    if (!anchor) {
        printf("Failed to allocate anchor (%ld bytes)\n",
               (long)sizeof(struct atrace_anchor));
        return RETURN_FAIL;
    }

    /* 2. Allocate ring buffer */
    ring = ringbuf_alloc(capacity);
    if (!ring) {
        printf("Failed to allocate ring buffer (%lu entries, %lu bytes)\n",
               (unsigned long)capacity,
               (unsigned long)(sizeof(struct atrace_ringbuf) +
                               ATRACE_EVENT_SIZE * capacity));
        /* anchor is leaked -- acceptable, see shutdown design */
        return RETURN_FAIL;
    }

    /* 3. Allocate patch descriptor array */
    patches = (struct atrace_patch *)AllocMem(
        sizeof(struct atrace_patch) * total_patches,
        MEMF_PUBLIC | MEMF_CLEAR);
    if (!patches) {
        printf("Failed to allocate patch array (%d entries)\n", total_patches);
        FreeMem(ring, sizeof(struct atrace_ringbuf) +
                ATRACE_EVENT_SIZE * capacity);
        return RETURN_FAIL;
    }

    /* 4. Fill anchor */
    InitSemaphore(&anchor->sem);

    /* Semaphore name must persist after the loader process exits.
     * String literals live in the loader's data segment which is
     * freed when the seglist is unloaded.  Copy to MEMF_PUBLIC. */
    {
        ULONG name_len = strlen(ATRACE_SEM_NAME) + 1;
        char *sem_name = (char *)AllocMem(name_len, MEMF_PUBLIC);
        if (!sem_name) {
            printf("Failed to allocate semaphore name\n");
            return RETURN_FAIL;
        }
        CopyMem((APTR)ATRACE_SEM_NAME, (APTR)sem_name, name_len);
        anchor->sem.ss_Link.ln_Name = sem_name;
    }
    anchor->sem.ss_Link.ln_Type = NT_SIGNALSEM;
    anchor->sem.ss_Link.ln_Pri = 0;
    anchor->magic = ATRACE_MAGIC;
    anchor->version = ATRACE_VERSION;
    anchor->flags = 0;
    anchor->global_enable = start_disabled ? 0 : 1;
    anchor->ring = ring;
    anchor->patch_count = (UWORD)total_patches;
    anchor->patches = patches;
    anchor->event_sequence = 0;
    anchor->events_consumed = 0;
    anchor->filter_task = NULL;

    /* Compute entries base address */
    entries = (struct atrace_event *)
        ((UBYTE *)ring + sizeof(struct atrace_ringbuf));

    /* 5. Open target libraries and install patches */
    patch_idx = 0;
    for (li = 0; li < atrace_lib_count; li++) {
        struct lib_info *lib = &atrace_libs[li];
        struct Library *libbase;

        libbase = OpenLibrary((STRPTR)lib->name, 0);
        if (!libbase) {
            printf("Cannot open %s -- skipping\n", lib->name);
            continue;
        }
        /* Do NOT close the library -- keep it in memory
         * because patches point into it. */

        for (fi = 0; fi < lib->func_count; fi++) {
            struct func_info *func = &lib->funcs[fi];
            struct atrace_patch *p = &patches[patch_idx];
            int ri;

            /* Fill patch descriptor */
            p->lib_id = lib->lib_id;
            p->lvo_offset = func->lvo_offset;
            p->func_id = (UWORD)fi;
            p->arg_count = func->arg_count;
            p->enabled = 1;
            p->use_count = 0;
            for (ri = 0; ri < 8; ri++)
                p->arg_regs[ri] = func->arg_regs[ri];
            p->string_args = func->string_args;

            if (stub_generate_and_install(anchor, p, libbase, entries) < 0) {
                printf("Failed to install patch for %s/%s\n",
                       lib->name, func->name);
                /* Continue with remaining patches */
            } else {
                printf("Patched %s/%s (LVO %d)\n",
                       lib->name, func->name, (int)func->lvo_offset);
            }

            patch_idx++;
        }
    }

    /* 6. If FUNCS specified, disable all then enable only named ones.
     *    Names were already validated before allocation. */
    if (funcs) {
        STRPTR *fp;
        int idx;

        for (fi = 0; fi < total_patches; fi++)
            patches[fi].enabled = 0;
        for (fp = funcs; *fp; fp++) {
            idx = find_patch_by_name(NULL, (const char *)*fp);
            patches[idx].enabled = 1;
        }
    } else {
        /* Auto-disable noise functions for system-wide usability.
         * These are auto-enabled when filter_task is set (TRACE RUN). */
        const char **np;
        int noise_count = 0;
        for (np = noise_func_names; *np; np++) {
            int idx = find_patch_by_name(NULL, *np);
            if (idx >= 0 && idx < total_patches) {
                patches[idx].enabled = 0;
                noise_count++;
            }
        }
        if (noise_count > 0)
            printf("Auto-disabled %d noise functions "
                   "(use ENABLE to override)\n", noise_count);
    }

    /* 7. Register the semaphore -- makes atrace discoverable */
    AddSemaphore(&anchor->sem);

    printf("atrace loaded: %d patches, %lu-entry ring buffer (%luKB)\n",
           patch_idx, (unsigned long)capacity,
           (unsigned long)(ATRACE_EVENT_SIZE * capacity / 1024));
    if (start_disabled)
        printf("Tracing is DISABLED (use ENABLE to activate)\n");
    else
        printf("Tracing is ACTIVE\n");

    return RETURN_OK;
}

/* ---- STATUS: print current state ---- */

static int do_status(struct atrace_anchor *anchor)
{
    struct atrace_ringbuf *ring = anchor->ring;
    ULONG used;

    printf("atrace status:\n");
    printf("  Version:          %d\n", (int)anchor->version);
    printf("  Global enable:    %s\n",
           anchor->global_enable ? "ACTIVE" : "DISABLED");
    printf("  Patches:          %d\n", (int)anchor->patch_count);
    printf("  Events produced:  %lu\n",
           (unsigned long)anchor->event_sequence);
    printf("  Events consumed:  %lu\n",
           (unsigned long)anchor->events_consumed);

    if (ring) {
        used = (ring->write_pos - ring->read_pos + ring->capacity)
               % ring->capacity;
        printf("  Buffer capacity:  %lu\n",
               (unsigned long)ring->capacity);
        printf("  Buffer used:      %lu\n", (unsigned long)used);
        printf("  Buffer overflow:  %lu\n",
               (unsigned long)ring->overflow);
    } else {
        printf("  Ring buffer:      (freed -- QUIT was called)\n");
    }

    /* Per-patch listing */
    {
        int li, fi;
        int idx = 0;
        printf("\n");
        for (li = 0; li < atrace_lib_count; li++) {
            struct lib_info *lib = &atrace_libs[li];
            for (fi = 0; fi < (int)lib->func_count; fi++) {
                if (idx < (int)anchor->patch_count) {
                    printf("  Patch %2d: %s.%-18s %s\n",
                           idx,
                           li == LIB_EXEC ? "exec" : "dos",
                           lib->funcs[fi].name,
                           anchor->patches[idx].enabled ?
                               "ENABLED" : "DISABLED");
                }
                idx++;
            }
        }
    }

    return RETURN_OK;
}

/* ---- ENABLE: activate tracing ---- */

static int do_enable(struct atrace_anchor *anchor, STRPTR *funcs)
{
    if (funcs) {
        STRPTR *fp;
        int idx;

        /* Validate all names first -- all-or-nothing */
        for (fp = funcs; *fp; fp++) {
            idx = find_patch_by_name(anchor, (const char *)*fp);
            if (idx < 0) {
                printf("Unknown function: %s\n", (const char *)*fp);
                return RETURN_FAIL;
            }
        }
        /* Apply: enable named patches only */
        for (fp = funcs; *fp; fp++) {
            idx = find_patch_by_name(anchor, (const char *)*fp);
            anchor->patches[idx].enabled = 1;
            printf("Enabled %s\n", (const char *)*fp);
        }
    } else {
        anchor->global_enable = 1;
        printf("atrace tracing ACTIVE\n");
    }
    return RETURN_OK;
}

/* ---- DISABLE: deactivate tracing and drain in-flight events ---- */

static int do_disable(struct atrace_anchor *anchor, STRPTR *funcs)
{
    int polls;
    int i;
    int all_drained;

    if (funcs) {
        STRPTR *fp;
        int idx;

        /* Validate all names first -- all-or-nothing */
        for (fp = funcs; *fp; fp++) {
            idx = find_patch_by_name(anchor, (const char *)*fp);
            if (idx < 0) {
                printf("Unknown function: %s\n", (const char *)*fp);
                return RETURN_FAIL;
            }
        }
        /* Apply: disable named patches only.
         * No global_enable change, no use_count drain needed --
         * the stub checks enabled atomically. */
        for (fp = funcs; *fp; fp++) {
            idx = find_patch_by_name(anchor, (const char *)*fp);
            anchor->patches[idx].enabled = 0;
            printf("Disabled %s\n", (const char *)*fp);
        }
        return RETURN_OK;
    }

    /* Global disable */
    Disable();
    anchor->global_enable = 0;
    Enable();

    /* Wait for all use_count values to drain */
    for (polls = 0; polls < 50; polls++) {
        all_drained = 1;
        for (i = 0; i < (int)anchor->patch_count; i++) {
            if (anchor->patches[i].use_count > 0) {
                all_drained = 0;
                break;
            }
        }
        if (all_drained)
            break;
        Delay(1);  /* 20ms */
    }

    if (!all_drained)
        printf("Warning: use counts did not fully drain\n");

    printf("atrace tracing DISABLED\n");
    return RETURN_OK;
}

/* ---- QUIT: disable, detach semaphore, free ring buffer ---- */

static int do_quit(struct atrace_anchor *anchor)
{
    struct atrace_ringbuf *ring;
    ULONG ring_size;

    /* 1. Disable tracing and drain use counts */
    do_disable(anchor, NULL);

    /* 2. Obtain semaphore exclusively -- blocks until daemon releases */
    ObtainSemaphore(&anchor->sem);

    /* 3. Remove semaphore from system list */
    RemSemaphore(&anchor->sem);

    /* 4. Free ring buffer */
    ring = anchor->ring;
    if (ring) {
        ring_size = sizeof(struct atrace_ringbuf) +
                    ATRACE_EVENT_SIZE * ring->capacity;
        anchor->ring = NULL;
        FreeMem(ring, ring_size);
    }

    /* 5. Release semaphore (even though it's removed from the list,
     *    the structure is still valid in memory) */
    ReleaseSemaphore(&anchor->sem);

    printf("atrace unloaded. Patches remain as transparent pass-throughs.\n");
    printf("Reboot to fully remove.\n");

    /* Anchor, patches, and stub code remain allocated forever.
     * Stubs are transparent because global_enable=0. */
    return RETURN_OK;
}
