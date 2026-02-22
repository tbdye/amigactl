/*
 * amigactld -- File operation command handlers (Phase 2)
 *
 * Implements DIR, STAT, READ, WRITE, DELETE, RENAME, MAKEDIR, PROTECT, SETDATE.
 * All handlers follow the protocol framing conventions from net.h:
 * send OK/ERR + payload lines + sentinel, return 0 on success or
 * -1 to disconnect the client.
 */

#include "file.h"
#include "net.h"

#include <proto/dos.h>
#include <proto/exec.h>
#include <dos/dos.h>
#include <dos/dosextens.h>
#include <dos/datetime.h>

#include <stdio.h>
#include <string.h>
#include <stdlib.h>

/* ---- Static helpers ---- */

/* Map AmigaOS IoErr() codes to wire protocol error codes. */
static int map_dos_error(LONG ioerr)
{
    switch (ioerr) {
    case ERROR_OBJECT_NOT_FOUND:         /* 205 */
    case ERROR_DIR_NOT_FOUND:            /* 204 */
    case ERROR_DEVICE_NOT_MOUNTED:       /* 218 */
        return ERR_NOT_FOUND;

    case ERROR_OBJECT_IN_USE:            /* 202 */
    case ERROR_DISK_WRITE_PROTECTED:     /* 214 */
        /* ERROR_WRITE_PROTECTED is also 214 (alias) */
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
    /* Static: single-threaded, non-recursive -- safe to reuse across calls */
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

/* Convert an AmigaOS DateStamp to "YYYY-MM-DD HH:MM:SS".
 * buf must be at least 20 bytes. */
static void format_datestamp(const struct DateStamp *ds, char *buf)
{
    static const int mdays[12] = {
        31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31
    };
    long remaining;
    int year, month, day;
    int hours, minutes, seconds;
    int leap, dim;

    remaining = ds->ds_Days;
    year = 1978;

    for (;;) {
        leap = ((year % 4 == 0) && (year % 100 != 0)) ||
               (year % 400 == 0);
        dim = leap ? 366 : 365;
        if (remaining < dim)
            break;
        remaining -= dim;
        year++;
    }

    leap = ((year % 4 == 0) && (year % 100 != 0)) ||
           (year % 400 == 0);
    month = 0;
    while (month < 11) {
        dim = mdays[month];
        if (month == 1 && leap)
            dim = 29;
        if (remaining < dim)
            break;
        remaining -= dim;
        month++;
    }
    day = (int)remaining + 1;
    month++; /* 1-based */

    hours = ds->ds_Minute / 60;
    minutes = ds->ds_Minute % 60;
    seconds = ds->ds_Tick / TICKS_PER_SECOND;

    sprintf(buf, "%04d-%02d-%02d %02d:%02d:%02d",
            year, month, day, hours, minutes, seconds);
}

/* Parse "YYYY-MM-DD HH:MM:SS" into an AmigaOS DateStamp.
 * Returns 0 on success, -1 on parse/range failure.
 * This is the inverse of format_datestamp(). */
static int parse_datestamp(const char *str, struct DateStamp *ds)
{
    static const int mdays[12] = {
        31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31
    };
    int year, month, day, hours, minutes, seconds;
    int leap, dim, total_days;
    int i;
    char trailing;

    /* Parse and reject trailing garbage via %c */
    if (sscanf(str, "%d-%d-%d %d:%d:%d%c",
               &year, &month, &day, &hours, &minutes, &seconds,
               &trailing) != 6) {
        return -1;
    }

    /* Validate ranges */
    if (year < 1978)
        return -1;
    if (month < 1 || month > 12)
        return -1;
    if (hours < 0 || hours > 23)
        return -1;
    if (minutes < 0 || minutes > 59)
        return -1;
    if (seconds < 0 || seconds > 59)
        return -1;

    /* Determine max days for this month */
    leap = ((year % 4 == 0) && (year % 100 != 0)) ||
           (year % 400 == 0);
    dim = mdays[month - 1];
    if (month == 2 && leap)
        dim = 29;
    if (day < 1 || day > dim)
        return -1;

    /* Calculate days since 1978-01-01 */
    total_days = 0;
    for (i = 1978; i < year; i++) {
        leap = ((i % 4 == 0) && (i % 100 != 0)) ||
               (i % 400 == 0);
        total_days += leap ? 366 : 365;
    }
    for (i = 0; i < month - 1; i++) {
        total_days += mdays[i];
        if (i == 1) {
            leap = ((year % 4 == 0) && (year % 100 != 0)) ||
                   (year % 400 == 0);
            if (leap)
                total_days++;
        }
    }
    total_days += day - 1;

    ds->ds_Days = total_days;
    ds->ds_Minute = hours * 60 + minutes;
    ds->ds_Tick = seconds * TICKS_PER_SECOND;
    return 0;
}

/* Format a FileInfoBlock as a tab-separated directory entry.
 * Format: <type>\t<prefix><name>\t<size>\t<protection>\t<datestamp>
 * Returns length written, or -1 if buffer too small. */
static int format_dir_entry(const struct FileInfoBlock *fib,
                            const char *prefix, char *buf, int bufsize)
{
    const char *type;
    char datebuf[20];
    int len;

    type = (fib->fib_DirEntryType > 0) ? "DIR" : "FILE";
    format_datestamp(&fib->fib_Date, datebuf);

    len = snprintf(buf, bufsize, "%s\t%s%s\t%ld\t%08lx\t%s",
                   type, prefix, fib->fib_FileName,
                   (long)fib->fib_Size,
                   (unsigned long)fib->fib_Protection,
                   datebuf);

    if (len < 0 || len >= bufsize)
        return -1;
    return len;
}

/* Maximum recursion depth for dir_recurse() to prevent runaway descent */
#define DIR_MAX_DEPTH 32

/* Heap-allocated work buffers for dir_recurse().
 * Stack allocation would add 1536 bytes per recursion level, which
 * overflows small stacks (e.g., 4096-byte RUN default). */
struct dir_work {
    char entry_buf[512];
    char newpath[512];
    char newprefix[512];
};

/* Recursive directory listing helper.
 * Sends formatted entries for all contents of path, prepending prefix
 * to each name.  Recurses into subdirectories up to DIR_MAX_DEPTH levels.
 * Called after OK is already sent -- must never send ERR or sentinel.
 * Returns 0 on success, -1 on send failure. */
static int dir_recurse(LONG fd, const char *path, const char *prefix,
                       int depth)
{
    BPTR lock;
    struct FileInfoBlock *fib;
    struct dir_work *w;
    int rc = 0;
    int n;

    if (depth >= DIR_MAX_DEPTH)
        return 0; /* silently skip -- too deep */

    lock = Lock((STRPTR)path, ACCESS_READ);
    if (!lock)
        return 0; /* silently skip -- OK already sent */

    fib = AllocDosObject(DOS_FIB, NULL);
    if (!fib) {
        UnLock(lock);
        return 0; /* silently skip -- OK already sent */
    }

    /* Heap-allocate work buffers: recursive, so static is unsafe */
    w = (struct dir_work *)AllocMem(sizeof(struct dir_work), MEMF_ANY);
    if (!w) {
        FreeDosObject(DOS_FIB, fib);
        UnLock(lock);
        return 0; /* silently skip -- OK already sent */
    }

    if (!Examine(lock, fib)) {
        FreeMem(w, sizeof(struct dir_work));
        FreeDosObject(DOS_FIB, fib);
        UnLock(lock);
        return 0; /* silently skip -- OK already sent */
    }

    while (ExNext(lock, fib)) {
        if (format_dir_entry(fib, prefix, w->entry_buf,
                             sizeof(w->entry_buf)) < 0)
            continue; /* entry too long, skip */
        if (send_payload_line(fd, w->entry_buf) < 0) {
            rc = -1;
            break;
        }

        if (fib->fib_DirEntryType > 0) {
            /* Subdirectory -- recurse */
            n = snprintf(w->newprefix, sizeof(w->newprefix), "%s%s/",
                         prefix, fib->fib_FileName);
            if (n < 0 || n >= (int)sizeof(w->newprefix))
                continue; /* path too long, skip entry */
            n = snprintf(w->newpath, sizeof(w->newpath), "%s/%s",
                         path, fib->fib_FileName);
            if (n < 0 || n >= (int)sizeof(w->newpath))
                continue; /* path too long, skip entry */
            rc = dir_recurse(fd, w->newpath, w->newprefix, depth + 1);
            if (rc < 0)
                break;
        }
    }

    if (rc == 0) {
        /* Check if ExNext ended normally or with an error */
        LONG err = IoErr();
        if (err != ERROR_NO_MORE_ENTRIES) {
            /* Silently ignore -- OK already sent, cannot send ERR */
        }
    }

    FreeMem(w, sizeof(struct dir_work));
    FreeDosObject(DOS_FIB, fib);
    UnLock(lock);
    return rc;
}

/* ---- Command handlers ---- */

int cmd_dir(struct client *c, const char *args)
{
    BPTR lock;
    struct FileInfoBlock *fib;
    /* Static: single-threaded, non-recursive handler -- safe to reuse */
    static char path[MAX_CMD_LEN + 1];
    static char entry_buf[512];
    int recursive = 0;
    const char *last_space;
    int pathlen;

    /* Copy args so we can modify it */
    if (args[0] == '\0') {
        send_error(c->fd, ERR_SYNTAX, "Missing path argument");
        send_sentinel(c->fd);
        return 0;
    }

    strncpy(path, args, sizeof(path) - 1);
    path[sizeof(path) - 1] = '\0';

    /* Check if the last token is RECURSIVE (case-insensitive) */
    pathlen = strlen(path);
    last_space = NULL;
    {
        int i;
        for (i = pathlen - 1; i >= 0; i--) {
            if (path[i] == ' ' || path[i] == '\t') {
                last_space = &path[i];
                break;
            }
        }
    }

    if (last_space) {
        const char *token = last_space + 1;
        if (stricmp((char *)token, "RECURSIVE") == 0) {
            recursive = 1;
            /* Trim the RECURSIVE keyword and trailing whitespace */
            pathlen = (int)(last_space - path);
            while (pathlen > 0 &&
                   (path[pathlen - 1] == ' ' || path[pathlen - 1] == '\t'))
                pathlen--;
            path[pathlen] = '\0';
        }
    }

    if (path[0] == '\0') {
        send_error(c->fd, ERR_SYNTAX, "Missing path argument");
        send_sentinel(c->fd);
        return 0;
    }

    lock = Lock((STRPTR)path, ACCESS_READ);
    if (!lock) {
        send_dos_error(c->fd, "Lock failed");
        return 0;
    }

    fib = AllocDosObject(DOS_FIB, NULL);
    if (!fib) {
        UnLock(lock);
        send_error(c->fd, ERR_INTERNAL, "Out of memory");
        send_sentinel(c->fd);
        return 0;
    }

    if (!Examine(lock, fib)) {
        send_dos_error(c->fd, "Examine failed");
        FreeDosObject(DOS_FIB, fib);
        UnLock(lock);
        return 0;
    }

    /* Verify it's a directory */
    if (fib->fib_DirEntryType <= 0) {
        send_error(c->fd, ERR_NOT_FOUND, "Not a directory");
        send_sentinel(c->fd);
        FreeDosObject(DOS_FIB, fib);
        UnLock(lock);
        return 0;
    }

    send_ok(c->fd, NULL);

    while (ExNext(lock, fib)) {
        if (format_dir_entry(fib, "", entry_buf, sizeof(entry_buf)) < 0)
            continue; /* entry too long, skip */
        if (send_payload_line(c->fd, entry_buf) < 0) {
            FreeDosObject(DOS_FIB, fib);
            UnLock(lock);
            return -1;
        }

        if (recursive && fib->fib_DirEntryType > 0) {
            /* Static: single-threaded, non-recursive context */
            static char subpath[512];
            static char subprefix[512];
            int n;

            n = snprintf(subprefix, sizeof(subprefix), "%s/",
                         fib->fib_FileName);
            if (n < 0 || n >= (int)sizeof(subprefix))
                continue; /* path too long, skip entry */
            n = snprintf(subpath, sizeof(subpath), "%s/%s",
                         path, fib->fib_FileName);
            if (n < 0 || n >= (int)sizeof(subpath))
                continue; /* path too long, skip entry */
            if (dir_recurse(c->fd, subpath, subprefix, 0) < 0) {
                FreeDosObject(DOS_FIB, fib);
                UnLock(lock);
                return -1;
            }
        }
    }

    /* Check if ExNext ended normally */
    {
        LONG err = IoErr();
        if (err != ERROR_NO_MORE_ENTRIES) {
            /* Send the error as a payload line -- OK already sent.
             * Static: single-threaded, non-recursive handler */
            static char fault_buf[128];
            static char msg_buf[256];
            Fault(err, (STRPTR)"", (STRPTR)fault_buf, sizeof(fault_buf));
            sprintf(msg_buf, "ExNext failed%s", fault_buf);
            send_payload_line(c->fd, msg_buf);
        }
    }

    FreeDosObject(DOS_FIB, fib);
    UnLock(lock);
    send_sentinel(c->fd);
    return 0;
}

int cmd_stat(struct client *c, const char *args)
{
    BPTR lock;
    struct FileInfoBlock *fib;
    const char *type;
    char datebuf[20];
    /* Static: single-threaded, non-recursive handler */
    static char line[256];

    if (args[0] == '\0') {
        send_error(c->fd, ERR_SYNTAX, "Missing path argument");
        send_sentinel(c->fd);
        return 0;
    }

    lock = Lock((STRPTR)args, ACCESS_READ);
    if (!lock) {
        send_dos_error(c->fd, "Lock failed");
        return 0;
    }

    fib = AllocDosObject(DOS_FIB, NULL);
    if (!fib) {
        UnLock(lock);
        send_error(c->fd, ERR_INTERNAL, "Out of memory");
        send_sentinel(c->fd);
        return 0;
    }

    if (!Examine(lock, fib)) {
        send_dos_error(c->fd, "Examine failed");
        FreeDosObject(DOS_FIB, fib);
        UnLock(lock);
        return 0;
    }

    type = (fib->fib_DirEntryType > 0) ? "dir" : "file";
    format_datestamp(&fib->fib_Date, datebuf);

    send_ok(c->fd, NULL);

    sprintf(line, "type=%s", type);
    send_payload_line(c->fd, line);

    sprintf(line, "name=%s", fib->fib_FileName);
    send_payload_line(c->fd, line);

    sprintf(line, "size=%ld", (long)fib->fib_Size);
    send_payload_line(c->fd, line);

    sprintf(line, "protection=%08lx", (unsigned long)fib->fib_Protection);
    send_payload_line(c->fd, line);

    sprintf(line, "datestamp=%s", datebuf);
    send_payload_line(c->fd, line);

    sprintf(line, "comment=%s", fib->fib_Comment);
    send_payload_line(c->fd, line);

    FreeDosObject(DOS_FIB, fib);
    UnLock(lock);
    send_sentinel(c->fd);
    return 0;
}

int cmd_read(struct client *c, const char *args)
{
    BPTR lock;
    struct FileInfoBlock *fib;
    BPTR fh;
    LONG file_size;
    LONG n;
    char info[16];
    static char read_buf[4096];

    if (args[0] == '\0') {
        send_error(c->fd, ERR_SYNTAX, "Missing path argument");
        send_sentinel(c->fd);
        return 0;
    }

    /* Examine the file to get size and type */
    lock = Lock((STRPTR)args, ACCESS_READ);
    if (!lock) {
        send_dos_error(c->fd, "Lock failed");
        return 0;
    }

    fib = AllocDosObject(DOS_FIB, NULL);
    if (!fib) {
        UnLock(lock);
        send_error(c->fd, ERR_INTERNAL, "Out of memory");
        send_sentinel(c->fd);
        return 0;
    }

    if (!Examine(lock, fib)) {
        send_dos_error(c->fd, "Examine failed");
        FreeDosObject(DOS_FIB, fib);
        UnLock(lock);
        return 0;
    }

    if (fib->fib_DirEntryType > 0) {
        send_error(c->fd, ERR_IO, "Is a directory");
        send_sentinel(c->fd);
        FreeDosObject(DOS_FIB, fib);
        UnLock(lock);
        return 0;
    }

    file_size = fib->fib_Size;
    FreeDosObject(DOS_FIB, fib);
    UnLock(lock);

    /* Open the file for reading */
    fh = Open((STRPTR)args, MODE_OLDFILE);
    if (!fh) {
        send_dos_error(c->fd, "Open failed");
        return 0;
    }

    sprintf(info, "%ld", (long)file_size);
    send_ok(c->fd, info);

    /* Send file contents in chunks */
    n = Read(fh, (STRPTR)read_buf, sizeof(read_buf));
    while (n > 0) {
        if (send_data_chunk(c->fd, read_buf, (int)n) < 0) {
            Close(fh);
            return 0;
        }
        n = Read(fh, (STRPTR)read_buf, sizeof(read_buf));
    }

    if (n < 0) {
        Close(fh);
        send_error(c->fd, ERR_IO, "Read failed");
        send_sentinel(c->fd);
        return 0;
    }

    send_end(c->fd);
    send_sentinel(c->fd);
    Close(fh);
    return 0;
}

int cmd_write(struct client *c, const char *args)
{
    /* Static: single-threaded, non-recursive handler -- safe to reuse */
    static char path[MAX_CMD_LEN + 1];
    static char temp_path[512];
    const char *size_str;
    const char *last_space;
    unsigned long declared_size;
    unsigned long total_received;
    char *endp;
    BPTR fh;
    static char chunk_buf[4096];
    char line[128];
    int i;

    /* Find last space to split path and size */
    last_space = NULL;
    for (i = strlen(args) - 1; i >= 0; i--) {
        if (args[i] == ' ' || args[i] == '\t') {
            last_space = &args[i];
            break;
        }
    }

    if (!last_space || last_space == args) {
        send_error(c->fd, ERR_SYNTAX, "Usage: WRITE <path> <size>");
        send_sentinel(c->fd);
        return 0;
    }

    size_str = last_space + 1;
    if (*size_str == '\0') {
        send_error(c->fd, ERR_SYNTAX, "Usage: WRITE <path> <size>");
        send_sentinel(c->fd);
        return 0;
    }

    /* Copy the path portion */
    {
        int pathlen = (int)(last_space - args);
        if (pathlen >= (int)sizeof(path))
            pathlen = (int)sizeof(path) - 1;
        memcpy(path, args, pathlen);

        /* Trim trailing whitespace from path */
        while (pathlen > 0 && (path[pathlen - 1] == ' ' ||
                               path[pathlen - 1] == '\t'))
            pathlen--;
        path[pathlen] = '\0';
    }

    if (path[0] == '\0') {
        send_error(c->fd, ERR_SYNTAX, "Usage: WRITE <path> <size>");
        send_sentinel(c->fd);
        return 0;
    }

    /* Parse size */
    if (*size_str == '-') {
        send_error(c->fd, ERR_SYNTAX, "Invalid size");
        send_sentinel(c->fd);
        return 0;
    }

    declared_size = strtoul(size_str, &endp, 10);
    if (*endp != '\0') {
        send_error(c->fd, ERR_SYNTAX, "Invalid size");
        send_sentinel(c->fd);
        return 0;
    }

    if (declared_size > 0x7FFFFFFFUL) {
        send_error(c->fd, ERR_SYNTAX, "Invalid size");
        send_sentinel(c->fd);
        return 0;
    }

    /* Check path length for temp file suffix */
    if (strlen(path) > 497) {
        send_error(c->fd, ERR_IO, "Path too long");
        send_sentinel(c->fd);
        return 0;
    }

    sprintf(temp_path, "%s.amigactld.tmp", path);

    /* Open temp file for writing */
    fh = Open((STRPTR)temp_path, MODE_NEWFILE);
    if (!fh) {
        send_dos_error(c->fd, "Open failed");
        return 0;
    }

    send_ready(c->fd);

    /* Receive DATA/END lines */
    total_received = 0;
    while (1) {
        int result;

        result = recv_line_blocking(c, line, sizeof(line));
        if (result < 0) {
            Close(fh);
            DeleteFile((STRPTR)temp_path);
            return -1;
        }

        if (strcmp(line, "END") == 0)
            break;

        if (strncmp(line, "DATA ", 5) == 0) {
            unsigned long chunk_len;
            LONG written;

            chunk_len = strtoul(line + 5, &endp, 10);
            if (*endp != '\0' || chunk_len == 0 || chunk_len > 4096) {
                Close(fh);
                DeleteFile((STRPTR)temp_path);
                return -1;
            }

            if (recv_exact_from_client(c, chunk_buf, (int)chunk_len) < 0) {
                Close(fh);
                DeleteFile((STRPTR)temp_path);
                return -1;
            }

            written = Write(fh, chunk_buf, (LONG)chunk_len);
            if (written != (LONG)chunk_len) {
                Close(fh);
                DeleteFile((STRPTR)temp_path);
                return -1;
            }

            total_received += chunk_len;
        } else {
            /* Malformed line */
            Close(fh);
            DeleteFile((STRPTR)temp_path);
            return -1;
        }
    }

    Close(fh);

    /* Verify size */
    if (total_received != declared_size) {
        DeleteFile((STRPTR)temp_path);
        send_error(c->fd, ERR_IO, "Size mismatch");
        send_sentinel(c->fd);
        return 0;
    }

    /* Delete existing target (OK if it doesn't exist) */
    if (!DeleteFile((STRPTR)path)) {
        LONG err = IoErr();
        if (err != ERROR_OBJECT_NOT_FOUND) {
            DeleteFile((STRPTR)temp_path);
            /* Restore IoErr for send_dos_error */
            SetIoErr(err);
            send_dos_error(c->fd, "Delete failed");
            return 0;
        }
    }

    /* Rename temp to target */
    if (!Rename((STRPTR)temp_path, (STRPTR)path)) {
        DeleteFile((STRPTR)temp_path);
        send_dos_error(c->fd, "Rename failed");
        return 0;
    }

    {
        char info[16];
        sprintf(info, "%lu", total_received);
        send_ok(c->fd, info);
    }
    send_sentinel(c->fd);
    return 0;
}

int cmd_delete(struct client *c, const char *args)
{
    if (args[0] == '\0') {
        send_error(c->fd, ERR_SYNTAX, "Missing path argument");
        send_sentinel(c->fd);
        return 0;
    }

    if (!DeleteFile((STRPTR)args)) {
        send_dos_error(c->fd, "Delete failed");
        return 0;
    }

    send_ok(c->fd, NULL);
    send_sentinel(c->fd);
    return 0;
}

int cmd_rename(struct client *c, const char *args)
{
    static char old_path[MAX_CMD_LEN + 1];
    static char new_path[MAX_CMD_LEN + 1];
    const char *p;

    /* RENAME takes no inline arguments; paths come on subsequent lines */
    p = args;
    while (*p == ' ' || *p == '\t')
        p++;
    if (*p != '\0') {
        send_error(c->fd, ERR_SYNTAX,
                   "RENAME takes no arguments; use three-line format");
        send_sentinel(c->fd);
        return 0;
    }

    /* Read old path */
    if (recv_line_blocking(c, old_path, sizeof(old_path)) < 0)
        return -1;

    /* Read new path */
    if (recv_line_blocking(c, new_path, sizeof(new_path)) < 0)
        return -1;

    if (!Rename((STRPTR)old_path, (STRPTR)new_path)) {
        send_dos_error(c->fd, "Rename failed");
        return 0;
    }

    send_ok(c->fd, NULL);
    send_sentinel(c->fd);
    return 0;
}

int cmd_makedir(struct client *c, const char *args)
{
    BPTR lock;

    if (args[0] == '\0') {
        send_error(c->fd, ERR_SYNTAX, "Missing path argument");
        send_sentinel(c->fd);
        return 0;
    }

    lock = CreateDir((STRPTR)args);
    if (!lock) {
        send_dos_error(c->fd, "CreateDir failed");
        return 0;
    }

    UnLock(lock);
    send_ok(c->fd, NULL);
    send_sentinel(c->fd);
    return 0;
}

int cmd_protect(struct client *c, const char *args)
{
    /* Static: single-threaded, non-recursive handler -- safe to reuse */
    static char path[MAX_CMD_LEN + 1];
    const char *last_space;
    const char *token;
    int set_mode = 0;
    unsigned long prot_value = 0;
    int pathlen;
    int i;

    if (args[0] == '\0') {
        send_error(c->fd, ERR_SYNTAX, "Missing path argument");
        send_sentinel(c->fd);
        return 0;
    }

    strncpy(path, args, sizeof(path) - 1);
    path[sizeof(path) - 1] = '\0';

    /* Find the last whitespace-delimited token */
    pathlen = strlen(path);
    last_space = NULL;
    for (i = pathlen - 1; i >= 0; i--) {
        if (path[i] == ' ' || path[i] == '\t') {
            last_space = &path[i];
            break;
        }
    }

    if (last_space) {
        char *endp;
        int token_len;

        token = last_space + 1;
        token_len = strlen(token);

        /* Check if it looks like a valid hex protection value:
         * 1-8 hex chars, all must be [0-9a-fA-F] (no 0x prefix) */
        if (token_len >= 1 && token_len <= 8) {
            int all_hex = 1;
            int j;
            for (j = 0; j < token_len; j++) {
                char ch = token[j];
                if (!((ch >= '0' && ch <= '9') ||
                      (ch >= 'a' && ch <= 'f') ||
                      (ch >= 'A' && ch <= 'F'))) {
                    all_hex = 0;
                    break;
                }
            }
            if (all_hex) {
                prot_value = strtoul(token, &endp, 16);
                if (*endp == '\0') {
                    /* Valid hex -- SET mode.  Path is everything before. */
                    set_mode = 1;
                    pathlen = (int)(last_space - path);
                    while (pathlen > 0 &&
                           (path[pathlen - 1] == ' ' ||
                            path[pathlen - 1] == '\t'))
                        pathlen--;
                    path[pathlen] = '\0';
                }
            }
        }
    }

    if (path[0] == '\0') {
        send_error(c->fd, ERR_SYNTAX, "Missing path argument");
        send_sentinel(c->fd);
        return 0;
    }

    if (set_mode) {
        BPTR lock;
        struct FileInfoBlock *fib;
        char line[32];

        /* Set the protection bits */
        if (!SetProtection((STRPTR)path, (LONG)prot_value)) {
            send_dos_error(c->fd, "SetProtection failed");
            return 0;
        }

        /* Read back the actual value via Examine */
        lock = Lock((STRPTR)path, ACCESS_READ);
        if (!lock) {
            send_dos_error(c->fd, "Lock failed");
            return 0;
        }

        fib = AllocDosObject(DOS_FIB, NULL);
        if (!fib) {
            UnLock(lock);
            send_error(c->fd, ERR_INTERNAL, "Out of memory");
            send_sentinel(c->fd);
            return 0;
        }

        if (!Examine(lock, fib)) {
            send_dos_error(c->fd, "Examine failed");
            FreeDosObject(DOS_FIB, fib);
            UnLock(lock);
            return 0;
        }

        sprintf(line, "protection=%08lx",
                (unsigned long)fib->fib_Protection);

        FreeDosObject(DOS_FIB, fib);
        UnLock(lock);

        send_ok(c->fd, NULL);
        send_payload_line(c->fd, line);
        send_sentinel(c->fd);
    } else {
        /* GET mode */
        BPTR lock;
        struct FileInfoBlock *fib;
        char line[32];

        lock = Lock((STRPTR)path, ACCESS_READ);
        if (!lock) {
            send_dos_error(c->fd, "Lock failed");
            return 0;
        }

        fib = AllocDosObject(DOS_FIB, NULL);
        if (!fib) {
            UnLock(lock);
            send_error(c->fd, ERR_INTERNAL, "Out of memory");
            send_sentinel(c->fd);
            return 0;
        }

        if (!Examine(lock, fib)) {
            send_dos_error(c->fd, "Examine failed");
            FreeDosObject(DOS_FIB, fib);
            UnLock(lock);
            return 0;
        }

        sprintf(line, "protection=%08lx",
                (unsigned long)fib->fib_Protection);

        FreeDosObject(DOS_FIB, fib);
        UnLock(lock);

        send_ok(c->fd, NULL);
        send_payload_line(c->fd, line);
        send_sentinel(c->fd);
    }

    return 0;
}

int cmd_setdate(struct client *c, const char *args)
{
    /* Static: single-threaded, non-recursive handler -- safe to reuse */
    static char path[MAX_CMD_LEN + 1];
    const char *ds_str;
    struct DateStamp ds;
    char datebuf[20];
    char line[64];
    int args_len;
    int pathlen;

    if (args[0] == '\0') {
        send_error(c->fd, ERR_SYNTAX, "Missing arguments");
        send_sentinel(c->fd);
        return 0;
    }

    args_len = strlen(args);

    /* Try to parse an explicit datestamp (last 19 chars: YYYY-MM-DD HH:MM:SS) */
    if (args_len >= 21 &&
        (args[args_len - 20] == ' ' || args[args_len - 20] == '\t')) {
        ds_str = args + args_len - 19;
        if (parse_datestamp(ds_str, &ds) == 0) {
            /* Valid datestamp -- extract path */
            pathlen = (int)(ds_str - 1 - args);
            while (pathlen > 0 && (args[pathlen - 1] == ' ' ||
                                    args[pathlen - 1] == '\t'))
                pathlen--;

            if (pathlen > 0 && pathlen < (int)sizeof(path)) {
                memcpy(path, args, pathlen);
                path[pathlen] = '\0';
                goto apply;
            }
        }
    }

    /* No valid datestamp suffix -- use current time */
    strncpy(path, args, sizeof(path) - 1);
    path[sizeof(path) - 1] = '\0';
    /* Trim trailing whitespace */
    {
        int plen = strlen(path);
        while (plen > 0 && (path[plen - 1] == ' ' || path[plen - 1] == '\t'))
            plen--;
        path[plen] = '\0';
    }
    DateStamp(&ds);

apply:
    if (!SetFileDate((STRPTR)path, &ds)) {
        send_dos_error(c->fd, "SetFileDate failed");
        return 0;
    }

    /* Success: echo back the applied datestamp */
    format_datestamp(&ds, datebuf);
    sprintf(line, "datestamp=%s", datebuf);

    send_ok(c->fd, NULL);
    send_payload_line(c->fd, line);
    send_sentinel(c->fd);
    return 0;
}
