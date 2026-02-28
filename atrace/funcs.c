/*
 * atrace -- function table
 *
 * Phase 2: 30 functions (12 exec.library + 18 dos.library).
 */

#include "atrace.h"

/* exec.library functions (12) */
static struct func_info exec_funcs[] = {
    /* 0: FindPort(a1=name) -> d0=port */
    {
        "FindPort", -390, 1,
        { REG_A1, 0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x01, 0  /* string_args: bit 0 = arg0 (name) */
    },
    /* 1: FindResident(a1=name) -> d0=resident */
    {
        "FindResident", -96, 1,
        { REG_A1, 0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x01, 0
    },
    /* 2: FindSemaphore(a1=name) -> d0=sem */
    {
        "FindSemaphore", -594, 1,
        { REG_A1, 0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x01, 0
    },
    /* 3: FindTask(a1=name) -> d0=task */
    {
        "FindTask", -294, 1,
        { REG_A1, 0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x01, 0  /* name can be NULL (find self) */
    },
    /* 4: OpenDevice(a0=devName, d0=unit, a1=ioReq, d1=flags) -> d0=error */
    {
        "OpenDevice", -444, 4,
        { REG_A0, REG_D0, REG_A1, REG_D1, 0, 0, 0, 0 },
        REG_D0, 0x01, 0  /* string_args: bit 0 = arg0 (devName) */
    },
    /* 5: OpenLibrary(a1=libName, d0=version) -> d0=libBase */
    {
        "OpenLibrary", -552, 2,
        { REG_A1, REG_D0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x01, 0
    },
    /* 6: OpenResource(a1=resName) -> d0=resBase */
    {
        "OpenResource", -498, 1,
        { REG_A1, 0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x01, 0
    },
    /* 7: GetMsg(a0=port) -> d0=msg */
    {
        "GetMsg", -372, 1,
        { REG_A0, 0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, 0  /* no string args */
    },
    /* 8: PutMsg(a0=port, a1=msg) -> void */
    {
        "PutMsg", -366, 2,
        { REG_A0, REG_A1, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, 0
    },
    /* 9: ObtainSemaphore(a0=sem) -> void */
    {
        "ObtainSemaphore", -564, 1,
        { REG_A0, 0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, 0
    },
    /* 10: ReleaseSemaphore(a0=sem) -> void */
    {
        "ReleaseSemaphore", -570, 1,
        { REG_A0, 0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, 0
    },
    /* 11: AllocMem(d0=byteSize, d1=requirements) -> d0=memBlock */
    {
        "AllocMem", -198, 2,
        { REG_D0, REG_D1, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, 0
    }
};

/* dos.library functions (18) */
static struct func_info dos_funcs[] = {
    /* 0: Open(d1=name, d2=accessMode) -> d0=fileHandle */
    {
        "Open", -30, 2,
        { REG_D1, REG_D2, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x01, 0  /* arg0 is string */
    },
    /* 1: Close(d1=fileHandle) -> d0=success */
    {
        "Close", -36, 1,
        { REG_D1, 0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, 0
    },
    /* 2: Lock(d1=name, d2=type) -> d0=lock */
    {
        "Lock", -84, 2,
        { REG_D1, REG_D2, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x01, 0
    },
    /* 3: DeleteFile(d1=name) -> d0=success */
    {
        "DeleteFile", -72, 1,
        { REG_D1, 0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x01, 0
    },
    /* 4: Execute(d1=string, d2=file, d3=file) -> d0=success */
    {
        "Execute", -222, 3,
        { REG_D1, REG_D2, REG_D3, 0, 0, 0, 0, 0 },
        REG_D0, 0x01, 0
    },
    /* 5: GetVar(d1=name, d2=buffer, d3=size, d4=flags) -> d0=len */
    {
        "GetVar", -906, 4,
        { REG_D1, REG_D2, REG_D3, REG_D4, 0, 0, 0, 0 },
        REG_D0, 0x01, 0
    },
    /* 6: FindVar(d1=name, d2=type) -> d0=localVar */
    {
        "FindVar", -918, 2,
        { REG_D1, REG_D2, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x01, 0
    },
    /* 7: LoadSeg(d1=name) -> d0=segList */
    {
        "LoadSeg", -150, 1,
        { REG_D1, 0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x01, 0
    },
    /* 8: NewLoadSeg(d1=file, d2=tags) -> d0=segList */
    {
        "NewLoadSeg", -768, 2,
        { REG_D1, REG_D2, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x01, 0
    },
    /* 9: CreateDir(d1=name) -> d0=lock */
    {
        "CreateDir", -120, 1,
        { REG_D1, 0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x01, 0
    },
    /* 10: MakeLink(d1=name, d2=dest, d3=soft) -> d0=success */
    {
        "MakeLink", -444, 3,
        { REG_D1, REG_D2, REG_D3, 0, 0, 0, 0, 0 },
        REG_D0, 0x01, 0
    },
    /* 11: Rename(d1=oldName, d2=newName) -> d0=success */
    {
        "Rename", -78, 2,
        { REG_D1, REG_D2, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x01, 0  /* only arg0 (oldName) captured as string */
    },
    /* 12: RunCommand(d1=seg, d2=stack, d3=paramptr, d4=paramlen) -> d0=rc */
    {
        "RunCommand", -504, 4,
        { REG_D1, REG_D2, REG_D3, REG_D4, 0, 0, 0, 0 },
        REG_D0, 0x00, 0  /* no string args */
    },
    /* 13: SetVar(d1=name, d2=buffer, d3=size, d4=flags) -> d0=success */
    {
        "SetVar", -900, 4,
        { REG_D1, REG_D2, REG_D3, REG_D4, 0, 0, 0, 0 },
        REG_D0, 0x01, 0
    },
    /* 14: DeleteVar(d1=name, d2=flags) -> d0=success */
    {
        "DeleteVar", -912, 2,
        { REG_D1, REG_D2, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x01, 0
    },
    /* 15: SystemTagList(d1=command, d2=tags) -> d0=rc */
    {
        "SystemTagList", -606, 2,
        { REG_D1, REG_D2, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x01, 0
    },
    /* 16: AddDosEntry(d1=dlist) -> d0=success */
    {
        "AddDosEntry", -678, 1,
        { REG_D1, 0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, 0
    },
    /* 17: CurrentDir(d1=lock) -> d0=oldLock */
    {
        "CurrentDir", -126, 1,
        { REG_D1, 0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, 0
    }
};

/* Library table */
struct lib_info atrace_libs[] = {
    {
        "exec.library",     /* name */
        exec_funcs,         /* funcs */
        12,                 /* func_count */
        LIB_EXEC,           /* lib_id = 0 */
        0                   /* padding */
    },
    {
        "dos.library",      /* name */
        dos_funcs,          /* funcs */
        18,                 /* func_count */
        LIB_DOS,            /* lib_id = 1 */
        0                   /* padding */
    }
};

int atrace_lib_count = 2;
