/*
 * atrace -- function table
 *
 * Phase 9: 80 functions (29 exec.library + 24 dos.library +
 *   11 intuition.library + 15 bsdsocket.library + 1 graphics.library).
 */

#include "atrace.h"

/* exec.library functions (29) */
static struct func_info exec_funcs[] = {
    /* 0: FindPort(a1=name) -> d0=port */
    {
        "FindPort", -390, 1,
        { REG_A1, 0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x01, DEREF_NONE, 0, 0  /* string_args: bit 0 = arg0 (name) */
    },
    /* 1: FindResident(a1=name) -> d0=resident */
    {
        "FindResident", -96, 1,
        { REG_A1, 0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x01, DEREF_NONE, 0, 0
    },
    /* 2: FindSemaphore(a1=name) -> d0=sem */
    {
        "FindSemaphore", -594, 1,
        { REG_A1, 0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x01, DEREF_NONE, 0, 0
    },
    /* 3: FindTask(a1=name) -> d0=task */
    {
        "FindTask", -294, 1,
        { REG_A1, 0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x01, DEREF_NONE, REG_A1, 0  /* skip_null_arg = a1 */
    },
    /* 4: OpenDevice(a0=devName, d0=unit, a1=ioReq, d1=flags) -> d0=error */
    {
        "OpenDevice", -444, 4,
        { REG_A0, REG_D0, REG_A1, REG_D1, 0, 0, 0, 0 },
        REG_D0, 0x01, DEREF_NONE, 0, 0  /* string_args: bit 0 = arg0 (devName) */
    },
    /* 5: OpenLibrary(a1=libName, d0=version) -> d0=libBase */
    {
        "OpenLibrary", -552, 2,
        { REG_A1, REG_D0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x01, DEREF_NONE, 0, 0
    },
    /* 6: OpenResource(a1=resName) -> d0=resBase */
    {
        "OpenResource", -498, 1,
        { REG_A1, 0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x01, DEREF_NONE, 0, 0
    },
    /* 7: GetMsg(a0=port) -> d0=msg */
    {
        "GetMsg", -372, 1,
        { REG_A0, 0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_LN_NAME, 0, 0
    },
    /* 8: PutMsg(a0=port, a1=msg) -> void */
    {
        "PutMsg", -366, 2,
        { REG_A0, REG_A1, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_LN_NAME, 0, 0
    },
    /* 9: ObtainSemaphore(a0=sem) -> void */
    {
        "ObtainSemaphore", -564, 1,
        { REG_A0, 0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_LN_NAME, 0, 0
    },
    /* 10: ReleaseSemaphore(a0=sem) -> void */
    {
        "ReleaseSemaphore", -570, 1,
        { REG_A0, 0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_LN_NAME, 0, 0
    },
    /* 11: AllocMem(d0=byteSize, d1=requirements) -> d0=memBlock */
    {
        "AllocMem", -198, 2,
        { REG_D0, REG_D1, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_NONE, 0, 0
    },
    /* 12: DoIO(a1=ioRequest) -> d0=error */
    {
        "DoIO", -456, 1,
        { REG_A1, 0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_IOREQUEST, 0, 0
    },
    /* 13: SendIO(a1=ioRequest) -> void */
    {
        "SendIO", -462, 1,
        { REG_A1, 0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_IOREQUEST, 0, 0
    },
    /* 14: WaitIO(a1=ioRequest) -> d0=error */
    {
        "WaitIO", -474, 1,
        { REG_A1, 0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_IOREQUEST, 0, 0
    },
    /* 15: AbortIO(a1=ioRequest) -> d0=result */
    {
        "AbortIO", -480, 1,
        { REG_A1, 0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_IOREQUEST, 0, 0
    },
    /* 16: CheckIO(a1=ioRequest) -> d0=ioRequest_or_NULL */
    {
        "CheckIO", -468, 1,
        { REG_A1, 0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_IOREQUEST, 0, 0
    },
    /* 17: FreeMem(a1=memoryBlock, d0=byteSize) -> void */
    {
        "FreeMem", -210, 2,
        { REG_A1, REG_D0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_NONE, 0, 0
    },
    /* 18: AllocVec(d0=byteSize, d1=requirements) -> d0=memoryBlock */
    {
        "AllocVec", -684, 2,
        { REG_D0, REG_D1, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_NONE, 0, 0
    },
    /* 19: FreeVec(a1=memoryBlock) -> void */
    {
        "FreeVec", -690, 1,
        { REG_A1, 0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_NONE, 0, 0
    },
    /* 20: Wait(d0=signalSet) -> d0=signals */
    {
        "Wait", -318, 1,
        { REG_D0, 0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_NONE, 0, 0
    },
    /* 21: Signal(a1=task, d0=signalSet) -> void */
    {
        "Signal", -324, 2,
        { REG_A1, REG_D0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_NONE, 0, 0
    },
    /* 22: AllocSignal(d0=signalNum) -> d0=signalNum */
    {
        "AllocSignal", -330, 1,
        { REG_D0, 0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_NONE, 0, 0
    },
    /* 23: FreeSignal(d0=signalNum) -> void */
    {
        "FreeSignal", -336, 1,
        { REG_D0, 0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_NONE, 0, 0
    },
    /* 24: CreateMsgPort() -> d0=port */
    {
        "CreateMsgPort", -666, 0,
        { 0, 0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_NONE, 0, 0
    },
    /* 25: DeleteMsgPort(a0=port) -> void */
    {
        "DeleteMsgPort", -672, 1,
        { REG_A0, 0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_LN_NAME, 0, 0
    },
    /* 26: CloseLibrary(a1=library) -> void */
    {
        "CloseLibrary", -414, 1,
        { REG_A1, 0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_LN_NAME, 0, 0
    },
    /* 27: CloseDevice(a1=ioRequest) -> void */
    {
        "CloseDevice", -450, 1,
        { REG_A1, 0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_IOREQUEST, 0, 0
    },
    /* 28: ReplyMsg(a1=message) -> void */
    {
        "ReplyMsg", -378, 1,
        { REG_A1, 0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_NONE, 0, 0
    }
};

/* dos.library functions (24) */
static struct func_info dos_funcs[] = {
    /* 0: Open(d1=name, d2=accessMode) -> d0=fileHandle */
    {
        "Open", -30, 2,
        { REG_D1, REG_D2, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x01, DEREF_NONE, 0, 0  /* arg0 is string */
    },
    /* 1: Close(d1=fileHandle) -> d0=success */
    {
        "Close", -36, 1,
        { REG_D1, 0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_NONE, 0, 0
    },
    /* 2: Lock(d1=name, d2=type) -> d0=lock */
    {
        "Lock", -84, 2,
        { REG_D1, REG_D2, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x01, DEREF_NONE, 0, 0
    },
    /* 3: DeleteFile(d1=name) -> d0=success */
    {
        "DeleteFile", -72, 1,
        { REG_D1, 0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x01, DEREF_NONE, 0, 0
    },
    /* 4: Execute(d1=string, d2=file, d3=file) -> d0=success */
    {
        "Execute", -222, 3,
        { REG_D1, REG_D2, REG_D3, 0, 0, 0, 0, 0 },
        REG_D0, 0x01, DEREF_NONE, 0, 0
    },
    /* 5: GetVar(d1=name, d2=buffer, d3=size, d4=flags) -> d0=len */
    {
        "GetVar", -906, 4,
        { REG_D1, REG_D2, REG_D3, REG_D4, 0, 0, 0, 0 },
        REG_D0, 0x01, DEREF_NONE, 0, 0
    },
    /* 6: FindVar(d1=name, d2=type) -> d0=localVar */
    {
        "FindVar", -918, 2,
        { REG_D1, REG_D2, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x01, DEREF_NONE, 0, 0
    },
    /* 7: LoadSeg(d1=name) -> d0=segList */
    {
        "LoadSeg", -150, 1,
        { REG_D1, 0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x01, DEREF_NONE, 0, 0
    },
    /* 8: NewLoadSeg(d1=file, d2=tags) -> d0=segList */
    {
        "NewLoadSeg", -768, 2,
        { REG_D1, REG_D2, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x01, DEREF_NONE, 0, 0
    },
    /* 9: CreateDir(d1=name) -> d0=lock */
    {
        "CreateDir", -120, 1,
        { REG_D1, 0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x01, DEREF_NONE, 0, 0
    },
    /* 10: MakeLink(d1=name, d2=dest, d3=soft) -> d0=success */
    {
        "MakeLink", -444, 3,
        { REG_D1, REG_D2, REG_D3, 0, 0, 0, 0, 0 },
        REG_D0, 0x03, DEREF_NONE, 0, 0  /* arg0 + arg1 both captured as strings */
    },
    /* 11: Rename(d1=oldName, d2=newName) -> d0=success */
    {
        "Rename", -78, 2,
        { REG_D1, REG_D2, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x03, DEREF_NONE, 0, 0  /* arg0 + arg1 both captured as strings */
    },
    /* 12: RunCommand(d1=seg, d2=stack, d3=paramptr, d4=paramlen) -> d0=rc */
    {
        "RunCommand", -504, 4,
        { REG_D1, REG_D2, REG_D3, REG_D4, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_NONE, 0, 0  /* no string args */
    },
    /* 13: SetVar(d1=name, d2=buffer, d3=size, d4=flags) -> d0=success */
    {
        "SetVar", -900, 4,
        { REG_D1, REG_D2, REG_D3, REG_D4, 0, 0, 0, 0 },
        REG_D0, 0x01, DEREF_NONE, 0, 0
    },
    /* 14: DeleteVar(d1=name, d2=flags) -> d0=success */
    {
        "DeleteVar", -912, 2,
        { REG_D1, REG_D2, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x01, DEREF_NONE, 0, 0
    },
    /* 15: SystemTagList(d1=command, d2=tags) -> d0=rc */
    {
        "SystemTagList", -606, 2,
        { REG_D1, REG_D2, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x01, DEREF_NONE, 0, 0
    },
    /* 16: AddDosEntry(d1=dlist) -> d0=success */
    {
        "AddDosEntry", -678, 1,
        { REG_D1, 0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_NONE, 0, 0
    },
    /* 17: CurrentDir(d1=lock) -> d0=oldLock */
    {
        "CurrentDir", -126, 1,
        { REG_D1, 0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_LOCK_VOLUME, 0, 0
    },
    /* 18: Read(d1=file, d2=buffer, d3=length) -> d0=actualLength */
    {
        "Read", -42, 3,
        { REG_D1, REG_D2, REG_D3, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_NONE, 0, 0  /* no string args */
    },
    /* 19: Write(d1=file, d2=buffer, d3=length) -> d0=actualLength */
    {
        "Write", -48, 3,
        { REG_D1, REG_D2, REG_D3, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_NONE, 0, 0
    },
    /* 20: UnLock(d1=lock) -> void */
    {
        "UnLock", -90, 1,
        { REG_D1, 0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_NONE, REG_D1, 0  /* skip_null_arg = d1 */
    },
    /* 21: Examine(d1=lock, d2=fib) -> d0=success */
    {
        "Examine", -102, 2,
        { REG_D1, REG_D2, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_NONE, 0, 0
    },
    /* 22: ExNext(d1=lock, d2=fib) -> d0=success */
    {
        "ExNext", -108, 2,
        { REG_D1, REG_D2, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_NONE, 0, 0
    },
    /* 23: Seek(d1=file, d2=position, d3=offset) -> d0=oldPosition */
    {
        "Seek", -66, 3,
        { REG_D1, REG_D2, REG_D3, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_NONE, 0, 0
    }
};

/* intuition.library functions (11) */
static struct func_info intuition_funcs[] = {
    /* 0: OpenWindow(a0=newWindow) -> d0=window */
    {
        "OpenWindow", -204, 1,
        { REG_A0, 0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_NONE, 0, 0
    },
    /* 1: CloseWindow(a0=window) -> void */
    {
        "CloseWindow", -72, 1,
        { REG_A0, 0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_NONE, 0, 0
    },
    /* 2: OpenScreen(a0=newScreen) -> d0=screen */
    {
        "OpenScreen", -198, 1,
        { REG_A0, 0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_NONE, 0, 0
    },
    /* 3: CloseScreen(a0=screen) -> void */
    {
        "CloseScreen", -66, 1,
        { REG_A0, 0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_NONE, 0, 0
    },
    /* 4: ActivateWindow(a0=window) -> void */
    {
        "ActivateWindow", -450, 1,
        { REG_A0, 0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_NONE, 0, 0
    },
    /* 5: WindowToFront(a0=window) -> void */
    {
        "WindowToFront", -312, 1,
        { REG_A0, 0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_NONE, 0, 0
    },
    /* 6: WindowToBack(a0=window) -> void */
    {
        "WindowToBack", -306, 1,
        { REG_A0, 0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_NONE, 0, 0
    },
    /* 7: ModifyIDCMP(a0=window, d0=flags) -> void */
    {
        "ModifyIDCMP", -150, 2,
        { REG_A0, REG_D0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_NONE, 0, 0
    },
    /* 8: OpenWorkBench() -> d0=result */
    {
        "OpenWorkBench", -210, 0,
        { 0, 0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_NONE, 0, 0
    },
    /* 9: CloseWorkBench() -> d0=result */
    {
        "CloseWorkBench", -78, 0,
        { 0, 0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_NONE, 0, 0
    },
    /* 10: LockPubScreen(a0=name) -> d0=screen */
    {
        "LockPubScreen", -510, 1,
        { REG_A0, 0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x01, DEREF_NONE, REG_A0, 0  /* skip_null_arg = a0 */
    }
};

/* bsdsocket.library functions (15) */
static struct func_info bsdsocket_funcs[] = {
    /* 0: socket(d0=domain, d1=type, d2=protocol) -> d0=fd */
    {
        "socket", -30, 3,
        { REG_D0, REG_D1, REG_D2, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_NONE, 0, 0
    },
    /* 1: bind(d0=fd, a0=name, d1=namelen) -> d0=result */
    {
        "bind", -36, 3,
        { REG_D0, REG_A0, REG_D1, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_NONE, 0, 0
    },
    /* 2: listen(d0=fd, d1=backlog) -> d0=result */
    {
        "listen", -42, 2,
        { REG_D0, REG_D1, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_NONE, 0, 0
    },
    /* 3: accept(d0=fd, a0=addr, a1=addrlen) -> d0=fd */
    {
        "accept", -48, 3,
        { REG_D0, REG_A0, REG_A1, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_NONE, 0, 0
    },
    /* 4: connect(d0=fd, a0=name, d1=namelen) -> d0=result */
    {
        "connect", -54, 3,
        { REG_D0, REG_A0, REG_D1, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_NONE, 0, 0
    },
    /* 5: sendto(d0=fd, a0=buf, d1=len, d2=flags, ...) -> d0=sent
     * 6 args total, only first 4 captured: fd, buf, len, flags */
    {
        "sendto", -60, 6,
        { REG_D0, REG_A0, REG_D1, REG_D2, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_NONE, 0, 0
    },
    /* 6: send(d0=fd, a0=buf, d1=len, d2=flags) -> d0=sent */
    {
        "send", -66, 4,
        { REG_D0, REG_A0, REG_D1, REG_D2, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_NONE, 0, 0
    },
    /* 7: recvfrom(d0=fd, a0=buf, d1=len, d2=flags, ...) -> d0=received
     * 6 args total, only first 4 captured: fd, buf, len, flags */
    {
        "recvfrom", -72, 6,
        { REG_D0, REG_A0, REG_D1, REG_D2, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_NONE, 0, 0
    },
    /* 8: recv(d0=fd, a0=buf, d1=len, d2=flags) -> d0=received */
    {
        "recv", -78, 4,
        { REG_D0, REG_A0, REG_D1, REG_D2, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_NONE, 0, 0
    },
    /* 9: shutdown(d0=fd, d1=how) -> d0=result */
    {
        "shutdown", -84, 2,
        { REG_D0, REG_D1, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_NONE, 0, 0
    },
    /* 10: setsockopt(d0=fd, d1=level, d2=optname, a0=optval, ...) -> d0=result
     * 5 args total, only first 4 captured: fd, level, optname, optval */
    {
        "setsockopt", -90, 5,
        { REG_D0, REG_D1, REG_D2, REG_A0, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_NONE, 0, 0
    },
    /* 11: getsockopt(d0=fd, d1=level, d2=optname, a0=optval, ...) -> d0=result
     * 5 args total, only first 4 captured: fd, level, optname, optval */
    {
        "getsockopt", -96, 5,
        { REG_D0, REG_D1, REG_D2, REG_A0, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_NONE, 0, 0
    },
    /* 12: IoctlSocket(d0=fd, d1=request, a0=argp) -> d0=result */
    {
        "IoctlSocket", -114, 3,
        { REG_D0, REG_D1, REG_A0, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_NONE, 0, 0
    },
    /* 13: CloseSocket(d0=fd) -> d0=result */
    {
        "CloseSocket", -120, 1,
        { REG_D0, 0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_NONE, 0, 0
    },
    /* 14: WaitSelect(d0=nfds, a0=readfds, a1=writefds, a2=exceptfds, ...) -> d0=result
     * 6 args total, only first 4 captured.
     * Custom arg_regs order: {d0, d1, a0, a1} to capture nfds and
     * sigmask (d1) as the two most debugging-relevant values. */
    {
        "WaitSelect", -126, 6,
        { REG_D0, REG_D1, REG_A0, REG_A1, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_NONE, 0, 0
    }
};

/* graphics.library functions (1) */
static struct func_info graphics_funcs[] = {
    /* 0: OpenFont(a0=textAttr) -> d0=font */
    {
        "OpenFont", -72, 1,
        { REG_A0, 0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_TEXTATTR, 0, 0
    }
};

/* Library table */
struct lib_info atrace_libs[] = {
    {
        "exec.library",     /* name */
        exec_funcs,         /* funcs */
        29,                 /* func_count (was 20) */
        LIB_EXEC,           /* lib_id = 0 */
        0                   /* padding */
    },
    {
        "dos.library",      /* name */
        dos_funcs,          /* funcs */
        24,                 /* func_count (was 20) */
        LIB_DOS,            /* lib_id = 1 */
        0                   /* padding */
    },
    {
        "intuition.library", /* name */
        intuition_funcs,     /* funcs */
        11,                  /* func_count (was 10) */
        LIB_INTUITION,       /* lib_id = 2 */
        0                    /* padding */
    },
    {
        "bsdsocket.library", /* name */
        bsdsocket_funcs,     /* funcs */
        15,                  /* func_count */
        LIB_BSDSOCKET,       /* lib_id = 3 */
        0                    /* padding */
    },
    {
        "graphics.library",  /* name */
        graphics_funcs,      /* funcs */
        1,                   /* func_count */
        LIB_GRAPHICS,        /* lib_id = 4 */
        0                    /* padding */
    }
};

int atrace_lib_count = 5;
