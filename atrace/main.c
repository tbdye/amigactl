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
typedef char assert_anchor_size[(sizeof(struct atrace_anchor) == 80) ? 1 : -1];

/* ReadArgs template */
#define TEMPLATE "BUFSZ/K/N,DISABLE/S,STATUS/S,ENABLE/S,QUIT/S"

enum {
    ARG_BUFSZ,
    ARG_DISABLE,
    ARG_STATUS,
    ARG_ENABLE,
    ARG_QUIT,
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

/* Local functions */
static int do_install(ULONG capacity, int start_disabled);
static int do_status(struct atrace_anchor *anchor);
static int do_enable(struct atrace_anchor *anchor);
static int do_disable(struct atrace_anchor *anchor);
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
        printf("Usage: atrace_loader [BUFSZ <n>] [DISABLE] [STATUS] [ENABLE] [QUIT]\n");
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
        /* Already loaded -- handle reconfiguration commands */
        if (args[ARG_STATUS]) {
            FreeArgs(rdargs);
            return do_status(anchor);
        }
        if (args[ARG_ENABLE]) {
            FreeArgs(rdargs);
            return do_enable(anchor);
        }
        if (args[ARG_DISABLE]) {
            FreeArgs(rdargs);
            return do_disable(anchor);
        }
        if (args[ARG_QUIT]) {
            FreeArgs(rdargs);
            return do_quit(anchor);
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

    /* Fresh install */
    FreeArgs(rdargs);
    return do_install(capacity, args[ARG_DISABLE] != 0);
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

/* ---- Fresh installation ---- */

static int do_install(ULONG capacity, int start_disabled)
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
    anchor->sem.ss_Link.ln_Name = (char *)ATRACE_SEM_NAME;
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

    /* 6. Register the semaphore -- makes atrace discoverable */
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

    return RETURN_OK;
}

/* ---- ENABLE: activate tracing ---- */

static int do_enable(struct atrace_anchor *anchor)
{
    anchor->global_enable = 1;
    printf("atrace tracing ENABLED\n");
    return RETURN_OK;
}

/* ---- DISABLE: deactivate tracing and drain in-flight events ---- */

static int do_disable(struct atrace_anchor *anchor)
{
    int polls;
    int i;
    int all_drained;

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
    do_disable(anchor);

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
