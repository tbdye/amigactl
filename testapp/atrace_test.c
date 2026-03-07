/*
 * atrace_test -- Test execution app for atrace validation
 *
 * Calls all traced functions with known, distinctive inputs.
 * Used by test_trace_app.py to verify that atrace captures and
 * formats every function correctly.
 *
 * All file operations use RAM: only.  No printf (no stdio dependency).
 * Cross-compiled with m68k-amigaos-gcc -noixemul.
 */

#include <proto/exec.h>
#include <proto/dos.h>

#include <exec/memory.h>
#include <exec/ports.h>
#include <exec/semaphores.h>
#include <exec/io.h>
#include <exec/nodes.h>

#include <dos/dos.h>
#include <dos/dostags.h>
#include <dos/dosextens.h>

#include <intuition/intuition.h>
#include <intuition/screens.h>
#include <proto/intuition.h>

#include <devices/timer.h>

#include <string.h>

/* Ensure sufficient stack for buffers and nested calls */
unsigned long __stack = 65536;

/* ---- File cleanup ---- */

/* Delete all RAM: artifacts this app creates.
 * Called at start (handle interrupted prior runs) and end.
 * Ignores return values -- files may not exist on pre-clean. */
static void cleanup_files(void)
{
    DeleteFile((STRPTR)"RAM:atrace_test_read");
    DeleteFile((STRPTR)"RAM:atrace_test_write");
    DeleteFile((STRPTR)"RAM:atrace_test_delete");
    DeleteFile((STRPTR)"RAM:atrace_test_link");
    DeleteFile((STRPTR)"RAM:atrace_test_link_tgt");
    DeleteFile((STRPTR)"RAM:atrace_test_ren_old");
    DeleteFile((STRPTR)"RAM:atrace_test_ren_new");
    DeleteFile((STRPTR)"RAM:atrace_test_dir");
    DeleteFile((STRPTR)"RAM:atrace_test_readfile");
}

/* ---- Main ---- */

int main(int argc, char **argv)
{
    /* Suppress unused parameter warnings */
    (void)argc;
    (void)argv;

    cleanup_files();

    /* ================================================================
     * exec.library tests (blocks 1-12)
     * ================================================================ */

    /* Block 1: FindPort
     * Look up the AMITCP port.  May or may not exist; we just need
     * atrace to capture the call with the distinctive name. */
    {
        FindPort((STRPTR)"AMITCP");
    }

    Delay(1);

    /* Block 2: FindResident
     * dos.library is always resident on any AmigaOS 2.0+ system. */
    {
        FindResident((STRPTR)"dos.library");
    }

    Delay(1);

    /* Block 3: FindSemaphore
     * atrace_patches semaphore exists because atrace must be loaded
     * for this test to run (it is invoked via TRACE RUN). */
    {
        FindSemaphore((STRPTR)"atrace_patches");
    }

    Delay(1);

    /* Block 4: FindTask
     * NULL argument = self-lookup, always succeeds. */
    {
        FindTask(NULL);
    }

    Delay(1);

    /* Block 5: OpenDevice
     * Open timer.device with UNIT_MICROHZ, then close it. */
    {
        struct MsgPort *port;
        struct timerequest *tr;

        port = CreateMsgPort();
        if (port) {
            tr = (struct timerequest *)CreateIORequest(port,
                                            sizeof(struct timerequest));
            if (tr) {
                OpenDevice((STRPTR)"timer.device", UNIT_MICROHZ,
                           (struct IORequest *)tr, 0);
                CloseDevice((struct IORequest *)tr);
                DeleteIORequest((struct IORequest *)tr);
            }
            DeleteMsgPort(port);
        }
    }

    Delay(1);

    /* Block 6: OpenLibrary
     * Open dos.library version 0, then close it. */
    {
        struct Library *lib;
        lib = OpenLibrary((STRPTR)"dos.library", 0);
        if (lib)
            CloseLibrary(lib);
    }

    Delay(1);

    /* Block 7: OpenResource
     * FileSystem.resource is always present on OS 2.0+. */
    {
        OpenResource((STRPTR)"FileSystem.resource");
    }

    Delay(1);

    /* Block 8: GetMsg (empty port)
     * Create a port, call GetMsg with no messages queued, delete port. */
    {
        struct MsgPort *port;
        port = CreateMsgPort();
        if (port) {
            GetMsg(port);
            DeleteMsgPort(port);
        }
    }

    Delay(1);

    /* Block 9: PutMsg + GetMsg (with message)
     * Create two ports, send a message from one to the other,
     * retrieve it, then clean up. */
    {
        struct MsgPort *recv_port;
        struct MsgPort *reply_port;
        struct Message msg;
        struct Message *got;

        recv_port = CreateMsgPort();
        reply_port = CreateMsgPort();
        if (recv_port && reply_port) {
            memset(&msg, 0, sizeof(msg));
            msg.mn_Node.ln_Type = NT_MESSAGE;
            msg.mn_ReplyPort = reply_port;
            msg.mn_Length = sizeof(struct Message);

            PutMsg(recv_port, &msg);
            got = GetMsg(recv_port);
            (void)got;
        }
        if (reply_port)
            DeleteMsgPort(reply_port);
        if (recv_port)
            DeleteMsgPort(recv_port);
    }

    Delay(1);

    /* Block 10: ObtainSemaphore + ReleaseSemaphore
     * Stack-allocated semaphore, initialized with InitSemaphore. */
    {
        struct SignalSemaphore sem;
        memset(&sem, 0, sizeof(sem));
        InitSemaphore(&sem);

        ObtainSemaphore(&sem);
        ReleaseSemaphore(&sem);
    }

    Delay(1);

    /* Block 11: AllocMem
     * Allocate 1234 bytes with MEMF_PUBLIC|MEMF_CLEAR.
     * The distinctive size 1234 is used by tests to identify this event. */
    {
        APTR mem;
        mem = AllocMem(1234, MEMF_PUBLIC | MEMF_CLEAR);
        if (mem)
            FreeMem(mem, 1234);
    }

    Delay(1);

    /* ================================================================
     * dos.library tests (blocks 12-29, 31 -- block 30 AddDosEntry skipped)
     * ================================================================ */

    /* Block 12: Open (Read, success)
     * Create a file first, then open it for reading. */
    {
        BPTR fh;

        /* Setup: create the file */
        fh = Open((STRPTR)"RAM:atrace_test_read", MODE_NEWFILE);
        if (fh)
            Close(fh);

        /* Target call: open for reading */
        fh = Open((STRPTR)"RAM:atrace_test_read", MODE_OLDFILE);
        if (fh)
            Close(fh);
    }

    Delay(1);

    /* Block 13: Open (Read, failure)
     * Open a non-existent file -- should return NULL. */
    {
        Open((STRPTR)"RAM:atrace_test_nofile", MODE_OLDFILE);
    }

    Delay(1);

    /* Block 14: Open (Write, success)
     * Open a new file for writing, then close and delete it. */
    {
        BPTR fh;
        fh = Open((STRPTR)"RAM:atrace_test_write", MODE_NEWFILE);
        if (fh)
            Close(fh);
        DeleteFile((STRPTR)"RAM:atrace_test_write");
    }

    Delay(1);

    /* Block 15: Close
     * The Close calls from blocks 12 and 14 already exercise Close.
     * No separate block needed -- the test identifies Close events
     * by sequence position relative to their paired Open events. */

    /* Block 16: Lock
     * Lock RAM: with shared (read) access, then unlock. */
    {
        BPTR lock;
        lock = Lock((STRPTR)"RAM:", ACCESS_READ);
        if (lock)
            UnLock(lock);
    }

    Delay(1);

    /* Block 17: DeleteFile
     * Create a file, then delete it. */
    {
        BPTR fh;
        fh = Open((STRPTR)"RAM:atrace_test_delete", MODE_NEWFILE);
        if (fh)
            Close(fh);

        DeleteFile((STRPTR)"RAM:atrace_test_delete");
    }

    Delay(1);

    /* Block 18: Execute
     * Run a simple echo command.  Both input and output handles
     * are NULL (0) to use defaults. */
    {
        Execute((STRPTR)"Echo >NIL: atrace_exec", (BPTR)0, (BPTR)0);
    }

    Delay(1);

    /* Block 19: LoadSeg
     * Load C:Echo, then unload it. */
    {
        BPTR seg;
        seg = LoadSeg((STRPTR)"C:Echo");
        if (seg)
            UnLoadSeg(seg);
    }

    Delay(1);

    /* Block 20: NewLoadSeg
     * Load C:Echo with NULL tags, then unload. */
    {
        BPTR seg;
        seg = NewLoadSeg((STRPTR)"C:Echo", NULL);
        if (seg)
            UnLoadSeg(seg);
    }

    Delay(1);

    /* Block 21: GetVar
     * Set a variable first, then read it back with GetVar. */
    {
        UBYTE buf[64];
        SetVar((STRPTR)"atrace_test_var", (STRPTR)"hello", 5, 0);
        GetVar((STRPTR)"atrace_test_var", buf, sizeof(buf), 0);
    }

    Delay(1);

    /* Block 22: FindVar
     * Look up the variable set in block 21. */
    {
        FindVar((STRPTR)"atrace_test_var", 0);
    }

    Delay(1);

    /* Block 23: SetVar
     * Set a distinctively-named variable. */
    {
        SetVar((STRPTR)"atrace_test_setvar", (STRPTR)"val42", 5, 0);
        DeleteVar((STRPTR)"atrace_test_setvar", 0);
    }

    Delay(1);

    /* Block 24: DeleteVar
     * Create a variable, then delete it. */
    {
        SetVar((STRPTR)"atrace_test_delvar", (STRPTR)"x", 1, 0);
        DeleteVar((STRPTR)"atrace_test_delvar", 0);
    }

    Delay(1);

    /* Block 25: CreateDir
     * Create a directory in RAM:, then clean up. */
    {
        BPTR lock;
        lock = CreateDir((STRPTR)"RAM:atrace_test_dir");
        if (lock)
            UnLock(lock);
        DeleteFile((STRPTR)"RAM:atrace_test_dir");
    }

    Delay(1);

    /* Block 26: MakeLink
     * Create a soft link.  May fail on FFS (no soft link support). */
    {
        BPTR fh;

        /* Setup: create target file */
        fh = Open((STRPTR)"RAM:atrace_test_link_tgt", MODE_NEWFILE);
        if (fh)
            Close(fh);

        /* Target call: create soft link */
        MakeLink((STRPTR)"RAM:atrace_test_link",
                 (LONG)"RAM:atrace_test_link_tgt", LINK_SOFT);

        /* Cleanup */
        DeleteFile((STRPTR)"RAM:atrace_test_link");
        DeleteFile((STRPTR)"RAM:atrace_test_link_tgt");
    }

    Delay(1);

    /* Block 27: Rename
     * Create a file, rename it, delete the renamed file. */
    {
        BPTR fh;
        fh = Open((STRPTR)"RAM:atrace_test_ren_old", MODE_NEWFILE);
        if (fh)
            Close(fh);

        Rename((STRPTR)"RAM:atrace_test_ren_old",
               (STRPTR)"RAM:atrace_test_ren_new");

        DeleteFile((STRPTR)"RAM:atrace_test_ren_new");
    }

    Delay(1);

    /* Block 28: RunCommand
     * Load C:Echo, run it with "hello\n" as params, unload. */
    {
        BPTR seg;
        seg = LoadSeg((STRPTR)"C:Echo");
        if (seg) {
            RunCommand(seg, 4096, (STRPTR)"hello\n", 6);
            UnLoadSeg(seg);
        }
    }

    Delay(1);

    /* Block 29: SystemTagList
     * Run a simple echo command via SystemTags.
     * Open NIL: for output to prevent interference with daemon I/O. */
    {
        BPTR fh_nil;
        fh_nil = Open((STRPTR)"NIL:", MODE_NEWFILE);
        if (fh_nil) {
            SystemTags((STRPTR)"Echo >NIL: systest",
                       SYS_Output, fh_nil,
                       TAG_DONE);
            Close(fh_nil);
        }
    }

    Delay(1);

    /* Block 31: CurrentDir
     * Lock RAM:, wait for daemon to process the Lock event
     * (populating the lock-to-path cache), then change directory
     * to RAM: and back. */
    {
        BPTR lock;
        BPTR old;

        lock = Lock((STRPTR)"RAM:", ACCESS_READ);
        if (lock) {
            /* Delay ensures the daemon polls and formats the Lock
             * event, populating the lock-to-path cache, before the
             * CurrentDir event arrives. */
            Delay(1);

            old = CurrentDir(lock);
            CurrentDir(old);
            UnLock(lock);
        }
    }

    /* ================================================================
     * Phase 5: Device I/O tests (blocks 32-34)
     * ================================================================ */

    /* Block 32: DoIO
     * Open timer.device, send a DoIO request (TR_ADDREQUEST for 1 tick),
     * then close. */
    {
        struct MsgPort *port;
        struct timerequest *tr;

        port = CreateMsgPort();
        if (port) {
            tr = (struct timerequest *)CreateIORequest(port,
                                            sizeof(struct timerequest));
            if (tr) {
                if (OpenDevice((STRPTR)"timer.device", UNIT_MICROHZ,
                               (struct IORequest *)tr, 0) == 0) {
                    tr->tr_node.io_Command = TR_ADDREQUEST;
                    tr->tr_time.tv_secs = 0;
                    tr->tr_time.tv_micro = 20000;  /* 20ms */
                    DoIO((struct IORequest *)tr);
                    CloseDevice((struct IORequest *)tr);
                }
                DeleteIORequest((struct IORequest *)tr);
            }
            DeleteMsgPort(port);
        }
    }

    Delay(1);

    /* Block 33: SendIO + CheckIO + WaitIO
     * Use SendIO for an async request, check with CheckIO, then WaitIO. */
    {
        struct MsgPort *port;
        struct timerequest *tr;

        port = CreateMsgPort();
        if (port) {
            tr = (struct timerequest *)CreateIORequest(port,
                                            sizeof(struct timerequest));
            if (tr) {
                if (OpenDevice((STRPTR)"timer.device", UNIT_MICROHZ,
                               (struct IORequest *)tr, 0) == 0) {
                    tr->tr_node.io_Command = TR_ADDREQUEST;
                    tr->tr_time.tv_secs = 0;
                    tr->tr_time.tv_micro = 100000;  /* 100ms */
                    SendIO((struct IORequest *)tr);
                    CheckIO((struct IORequest *)tr);
                    WaitIO((struct IORequest *)tr);
                    CloseDevice((struct IORequest *)tr);
                }
                DeleteIORequest((struct IORequest *)tr);
            }
            DeleteMsgPort(port);
        }
    }

    Delay(1);

    /* Block 34: AbortIO
     * Start a long timer request, abort it immediately. */
    {
        struct MsgPort *port;
        struct timerequest *tr;

        port = CreateMsgPort();
        if (port) {
            tr = (struct timerequest *)CreateIORequest(port,
                                            sizeof(struct timerequest));
            if (tr) {
                if (OpenDevice((STRPTR)"timer.device", UNIT_MICROHZ,
                               (struct IORequest *)tr, 0) == 0) {
                    tr->tr_node.io_Command = TR_ADDREQUEST;
                    tr->tr_time.tv_secs = 60;  /* long timeout */
                    tr->tr_time.tv_micro = 0;
                    SendIO((struct IORequest *)tr);
                    AbortIO((struct IORequest *)tr);
                    WaitIO((struct IORequest *)tr);
                    CloseDevice((struct IORequest *)tr);
                }
                DeleteIORequest((struct IORequest *)tr);
            }
            DeleteMsgPort(port);
        }
    }

    Delay(1);

    /* ================================================================
     * Phase 5: Memory tests (blocks 35-36)
     * ================================================================ */

    /* Block 35: FreeMem
     * Allocate 2345 bytes, then free. Distinctive size for matching. */
    {
        APTR mem;
        mem = AllocMem(2345, MEMF_PUBLIC);
        if (mem)
            FreeMem(mem, 2345);
    }

    Delay(1);

    /* Block 36: AllocVec + FreeVec
     * Allocate 3456 bytes with MEMF_PUBLIC|MEMF_CLEAR, then free.
     * Distinctive size for matching. */
    {
        APTR mem;
        mem = AllocVec(3456, MEMF_PUBLIC | MEMF_CLEAR);
        if (mem)
            FreeVec(mem);
    }

    Delay(1);

    /* ================================================================
     * Phase 5: Intuition tests (blocks 37-40)
     * Open IntuitionBase once, exercise OpenWindow/CloseWindow,
     * OpenScreen/CloseScreen, ActivateWindow/WindowToFront/WindowToBack,
     * and ModifyIDCMP, then close IntuitionBase.
     * ================================================================ */
    {
        struct Library *IntuitionBase;
        IntuitionBase = OpenLibrary((STRPTR)"intuition.library", 37);
        if (IntuitionBase) {

            /* Block 37: OpenWindow + CloseWindow
             * Open a small, minimal window on the Workbench screen, then close.
             * Use static NewWindow to avoid large stack allocation. */
            {
                static struct NewWindow nw;
                struct Window *win;

                memset(&nw, 0, sizeof(nw));
                nw.LeftEdge = 0;
                nw.TopEdge = 0;
                nw.Width = 64;
                nw.Height = 32;
                nw.DetailPen = 0;
                nw.BlockPen = 1;
                nw.IDCMPFlags = 0;
                nw.Flags = WFLG_SMART_REFRESH | WFLG_NOCAREREFRESH
                         | WFLG_BORDERLESS | WFLG_BACKDROP;
                nw.Title = NULL;
                nw.Screen = NULL;  /* Workbench screen */
                nw.Type = WBENCHSCREEN;

                win = OpenWindow(&nw);
                if (win) {
                    Delay(2);  /* Let daemon see the open event */
                    CloseWindow(win);
                }
            }

            Delay(1);

            /* Block 38: OpenScreen + CloseScreen
             * Open a minimal custom screen, then close it immediately.
             * Use static NewScreen to avoid stack allocation. */
            {
                static struct NewScreen ns;
                struct Screen *scr;

                memset(&ns, 0, sizeof(ns));
                ns.LeftEdge = 0;
                ns.TopEdge = 0;
                ns.Width = 320;
                ns.Height = 200;
                ns.Depth = 1;
                ns.DetailPen = 0;
                ns.BlockPen = 1;
                ns.ViewModes = 0;
                ns.Type = CUSTOMSCREEN;
                ns.DefaultTitle = (UBYTE *)"atrace_test_screen";

                scr = OpenScreen(&ns);
                if (scr)
                    CloseScreen(scr);
            }

            Delay(1);

            /* Block 39: ActivateWindow + WindowToFront + WindowToBack
             * Open a window, activate it, bring to front, send to back, close. */
            {
                static struct NewWindow nw;
                struct Window *win;

                memset(&nw, 0, sizeof(nw));
                nw.Width = 64;
                nw.Height = 32;
                nw.Flags = WFLG_SMART_REFRESH | WFLG_NOCAREREFRESH
                         | WFLG_BORDERLESS | WFLG_BACKDROP;
                nw.Type = WBENCHSCREEN;

                win = OpenWindow(&nw);
                if (win) {
                    ActivateWindow(win);
                    Delay(1);
                    WindowToFront(win);
                    Delay(1);
                    WindowToBack(win);
                    Delay(1);
                    CloseWindow(win);
                }
            }

            Delay(1);

            /* Block 40: ModifyIDCMP
             * Open a window with no IDCMP, modify to add CLOSEWINDOW, close. */
            {
                static struct NewWindow nw;
                struct Window *win;

                memset(&nw, 0, sizeof(nw));
                nw.Width = 64;
                nw.Height = 32;
                nw.IDCMPFlags = 0;
                nw.Flags = WFLG_SMART_REFRESH | WFLG_NOCAREREFRESH
                         | WFLG_BORDERLESS | WFLG_BACKDROP;
                nw.Type = WBENCHSCREEN;

                win = OpenWindow(&nw);
                if (win) {
                    ModifyIDCMP(win, IDCMP_CLOSEWINDOW);  /* 0x00000200 */
                    Delay(1);
                    CloseWindow(win);
                }
            }

            CloseLibrary(IntuitionBase);
        }
    }

    Delay(1);

    /* ================================================================
     * Phase 5b: String resolution test (block 42)
     * ================================================================ */

    /* Block 42: Long path string capture
     * Lock a path > 23 chars to exercise expanded string_data.
     * With 59-char string_data (128-byte events), this 42-char
     * path fits entirely without truncation.
     * The lock will fail (file doesn't exist) but atrace still
     * captures the full path argument. */
    {
        BPTR lock;
        /* Path is 42 chars -- fits within 59-char string_data */
        lock = Lock((STRPTR)"PROGDIR:atrace_test_long_path_verification",
                     ACCESS_READ);
        if (lock)
            UnLock(lock);
    }

    Delay(1);

    /* ================================================================
     * Phase 5: File I/O tests (block 41)
     * ================================================================ */

    /* Block 41: Read + Write (dos.library)
     * Create a file, write 42 bytes, close, reopen, read back, close. */
    {
        BPTR fh;
        static char write_buf[42];
        static char read_buf[42];
        LONG actual;

        memset(write_buf, 'A', 42);

        /* Write phase */
        fh = Open((STRPTR)"RAM:atrace_test_readfile", MODE_NEWFILE);
        if (fh) {
            Write(fh, write_buf, 42);
            Close(fh);
        }

        /* Read phase */
        fh = Open((STRPTR)"RAM:atrace_test_readfile", MODE_OLDFILE);
        if (fh) {
            actual = Read(fh, read_buf, 42);
            (void)actual;
            Close(fh);
        }

        DeleteFile((STRPTR)"RAM:atrace_test_readfile");
    }

    Delay(1);

    cleanup_files();
    return 0;
}
