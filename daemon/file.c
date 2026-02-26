/*
 * amigactld -- File operation command handlers
 *
 * Implements DIR, STAT, READ, WRITE, DELETE, RENAME, MAKEDIR, PROTECT,
 * SETDATE, COPY, APPEND, CHECKSUM, SETCOMMENT.
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

/* Join an Amiga directory path with a child filename.
 * If path ends with ':', omit the '/' separator (volume root).
 * Returns the number of characters written (excluding NUL), or
 * -1 if the result would exceed bufsize. */
static int join_amiga_path(char *buf, int bufsize,
                           const char *path, const char *child)
{
    int n;
    int len = strlen(path);

    if (len > 0 && path[len - 1] == ':')
        n = snprintf(buf, bufsize, "%s%s", path, child);
    else
        n = snprintf(buf, bufsize, "%s/%s", path, child);

    if (n < 0 || n >= bufsize)
        return -1;
    return n;
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
            n = join_amiga_path(w->newpath, sizeof(w->newpath),
                                path, (const char *)fib->fib_FileName);
            if (n < 0)
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
            n = join_amiga_path(subpath, sizeof(subpath),
                                path, (const char *)fib->fib_FileName);
            if (n < 0)
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
    LONG actual_bytes;
    LONG remaining;
    LONG chunk;
    LONG n;
    char info[32];
    /* Static: single-threaded, non-recursive handler -- safe to reuse */
    static char path[MAX_CMD_LEN + 1];
    static char read_buf[4096];
    long offset_val = 0;
    long length_val = -1; /* -1 = not specified */
    int have_offset = 0;
    int have_length = 0;
    int pathlen;

    if (args[0] == '\0') {
        send_error(c->fd, ERR_SYNTAX, "Missing path argument");
        send_sentinel(c->fd);
        return 0;
    }

    /* Copy args so we can parse and modify */
    strncpy(path, args, sizeof(path) - 1);
    path[sizeof(path) - 1] = '\0';
    pathlen = strlen(path);

    /* Parse optional trailing LENGTH <n> */
    {
        int i;
        const char *last_space = NULL;
        const char *token;

        /* Find last whitespace */
        for (i = pathlen - 1; i >= 0; i--) {
            if (path[i] == ' ' || path[i] == '\t') {
                last_space = &path[i];
                break;
            }
        }

        if (last_space) {
            char *endp;
            token = last_space + 1;
            length_val = strtol(token, &endp, 10);
            if (*endp == '\0' && endp != token) {
                /* Last token is numeric -- check if preceding token is LENGTH */
                int new_end = (int)(last_space - path);
                const char *prev_space = NULL;

                /* Trim whitespace before the number */
                while (new_end > 0 &&
                       (path[new_end - 1] == ' ' || path[new_end - 1] == '\t'))
                    new_end--;

                /* Find the whitespace before the keyword */
                for (i = new_end - 1; i >= 0; i--) {
                    if (path[i] == ' ' || path[i] == '\t') {
                        prev_space = &path[i];
                        break;
                    }
                }

                if (prev_space) {
                    token = prev_space + 1;
                    /* Check if the keyword between prev_space and new_end is LENGTH */
                    {
                        int kw_len = new_end - (int)(token - path);
                        if (kw_len == 6 && strnicmp(token, "LENGTH", 6) == 0) {
                            have_length = 1;
                            /* Trim path to before the keyword */
                            pathlen = (int)(prev_space - path);
                            while (pathlen > 0 &&
                                   (path[pathlen - 1] == ' ' ||
                                    path[pathlen - 1] == '\t'))
                                pathlen--;
                            path[pathlen] = '\0';
                        }
                    }
                } else {
                    /* No space before -- check if the whole remaining string is LENGTH */
                    token = path;
                    {
                        int kw_len = new_end - 0;
                        if (kw_len == 6 && strnicmp(token, "LENGTH", 6) == 0) {
                            /* Keyword is the entire path -- invalid (no path left) */
                            have_length = 0;
                        }
                    }
                }
            }
        }
    }

    /* Parse optional trailing OFFSET <n> from (possibly trimmed) path */
    pathlen = strlen(path);
    {
        int i;
        const char *last_space = NULL;
        const char *token;

        for (i = pathlen - 1; i >= 0; i--) {
            if (path[i] == ' ' || path[i] == '\t') {
                last_space = &path[i];
                break;
            }
        }

        if (last_space) {
            char *endp;
            token = last_space + 1;
            offset_val = strtol(token, &endp, 10);
            if (*endp == '\0' && endp != token) {
                int new_end = (int)(last_space - path);
                const char *prev_space = NULL;

                while (new_end > 0 &&
                       (path[new_end - 1] == ' ' || path[new_end - 1] == '\t'))
                    new_end--;

                for (i = new_end - 1; i >= 0; i--) {
                    if (path[i] == ' ' || path[i] == '\t') {
                        prev_space = &path[i];
                        break;
                    }
                }

                if (prev_space) {
                    token = prev_space + 1;
                    {
                        int kw_len = new_end - (int)(token - path);
                        if (kw_len == 6 && strnicmp(token, "OFFSET", 6) == 0) {
                            have_offset = 1;
                            pathlen = (int)(prev_space - path);
                            while (pathlen > 0 &&
                                   (path[pathlen - 1] == ' ' ||
                                    path[pathlen - 1] == '\t'))
                                pathlen--;
                            path[pathlen] = '\0';
                        }
                    }
                }
            }
        }
    }

    /* Trim trailing whitespace from path */
    pathlen = strlen(path);
    while (pathlen > 0 &&
           (path[pathlen - 1] == ' ' || path[pathlen - 1] == '\t'))
        pathlen--;
    path[pathlen] = '\0';

    if (path[0] == '\0') {
        send_error(c->fd, ERR_SYNTAX, "Missing path argument");
        send_sentinel(c->fd);
        return 0;
    }

    /* Validate parsed values */
    if (have_offset && offset_val < 0) {
        send_error(c->fd, ERR_SYNTAX, "Invalid offset");
        send_sentinel(c->fd);
        return 0;
    }
    if (have_length && length_val < 0) {
        send_error(c->fd, ERR_SYNTAX, "Invalid length");
        send_sentinel(c->fd);
        return 0;
    }

    /* Examine the file to get size and type */
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

    /* Calculate actual bytes to send */
    if (have_offset && offset_val >= file_size) {
        actual_bytes = 0;
    } else if (have_offset) {
        actual_bytes = file_size - (LONG)offset_val;
    } else {
        actual_bytes = file_size;
    }
    if (have_length && length_val < actual_bytes) {
        actual_bytes = (LONG)length_val;
    }

    /* Open the file for reading */
    fh = Open((STRPTR)path, MODE_OLDFILE);
    if (!fh) {
        send_dos_error(c->fd, "Open failed");
        return 0;
    }

    /* Seek to offset if specified */
    if (have_offset && offset_val > 0 && actual_bytes > 0) {
        LONG seek_result;
        seek_result = Seek(fh, (LONG)offset_val, OFFSET_BEGINNING);
        if (seek_result == -1) {
            send_dos_error(c->fd, "Seek failed");
            Close(fh);
            return 0;
        }
    }

    sprintf(info, "%ld", (long)actual_bytes);
    send_ok(c->fd, info);

    /* Send file contents in chunks */
    remaining = actual_bytes;
    while (remaining > 0) {
        chunk = remaining;
        if (chunk > (LONG)sizeof(read_buf))
            chunk = (LONG)sizeof(read_buf);
        n = Read(fh, (STRPTR)read_buf, chunk);
        if (n <= 0)
            break;
        if (send_data_chunk(c->fd, read_buf, (int)n) < 0) {
            Close(fh);
            return 0;
        }
        remaining -= n;
    }

    if (remaining > 0 && n < 0) {
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

/* ---- CRC32 table (IEEE 802.3, reflected polynomial 0xEDB88320) ---- */

static const ULONG crc32_table[256] = {
    0x00000000UL, 0x77073096UL, 0xEE0E612CUL, 0x990951BAUL,
    0x076DC419UL, 0x706AF48FUL, 0xE963A535UL, 0x9E6495A3UL,
    0x0EDB8832UL, 0x79DCB8A4UL, 0xE0D5E91EUL, 0x97D2D988UL,
    0x09B64C2BUL, 0x7EB17CBDUL, 0xE7B82D07UL, 0x90BF1D91UL,
    0x1DB71064UL, 0x6AB020F2UL, 0xF3B97148UL, 0x84BE41DEUL,
    0x1ADAD47DUL, 0x6DDDE4EBUL, 0xF4D4B551UL, 0x83D385C7UL,
    0x136C9856UL, 0x646BA8C0UL, 0xFD62F97AUL, 0x8A65C9ECUL,
    0x14015C4FUL, 0x63066CD9UL, 0xFA0F3D63UL, 0x8D080DF5UL,
    0x3B6E20C8UL, 0x4C69105EUL, 0xD56041E4UL, 0xA2677172UL,
    0x3C03E4D1UL, 0x4B04D447UL, 0xD20D85FDUL, 0xA50AB56BUL,
    0x35B5A8FAUL, 0x42B2986CUL, 0xDBBBC9D6UL, 0xACBCF940UL,
    0x32D86CE3UL, 0x45DF5C75UL, 0xDCD60DCFUL, 0xABD13D59UL,
    0x26D930ACUL, 0x51DE003AUL, 0xC8D75180UL, 0xBFD06116UL,
    0x21B4F4B5UL, 0x56B3C423UL, 0xCFBA9599UL, 0xB8BDA50FUL,
    0x2802B89EUL, 0x5F058808UL, 0xC60CD9B2UL, 0xB10BE924UL,
    0x2F6F7C87UL, 0x58684C11UL, 0xC1611DABUL, 0xB6662D3DUL,
    0x76DC4190UL, 0x01DB7106UL, 0x98D220BCUL, 0xEFD5102AUL,
    0x71B18589UL, 0x06B6B51FUL, 0x9FBFE4A5UL, 0xE8B8D433UL,
    0x7807C9A2UL, 0x0F00F934UL, 0x9609A88EUL, 0xE10E9818UL,
    0x7F6A0DBBUL, 0x086D3D2DUL, 0x91646C97UL, 0xE6635C01UL,
    0x6B6B51F4UL, 0x1C6C6162UL, 0x856530D8UL, 0xF262004EUL,
    0x6C0695EDUL, 0x1B01A57BUL, 0x8208F4C1UL, 0xF50FC457UL,
    0x65B0D9C6UL, 0x12B7E950UL, 0x8BBEB8EAUL, 0xFCB9887CUL,
    0x62DD1DDFUL, 0x15DA2D49UL, 0x8CD37CF3UL, 0xFBD44C65UL,
    0x4DB26158UL, 0x3AB551CEUL, 0xA3BC0074UL, 0xD4BB30E2UL,
    0x4ADFA541UL, 0x3DD895D7UL, 0xA4D1C46DUL, 0xD3D6F4FBUL,
    0x4369E96AUL, 0x346ED9FCUL, 0xAD678846UL, 0xDA60B8D0UL,
    0x44042D73UL, 0x33031DE5UL, 0xAA0A4C5FUL, 0xDD0D7CC9UL,
    0x5005713CUL, 0x270241AAUL, 0xBE0B1010UL, 0xC90C2086UL,
    0x5768B525UL, 0x206F85B3UL, 0xB966D409UL, 0xCE61E49FUL,
    0x5EDEF90EUL, 0x29D9C998UL, 0xB0D09822UL, 0xC7D7A8B4UL,
    0x59B33D17UL, 0x2EB40D81UL, 0xB7BD5C3BUL, 0xC0BA6CADUL,
    0xEDB88320UL, 0x9ABFB3B6UL, 0x03B6E20CUL, 0x74B1D29AUL,
    0xEAD54739UL, 0x9DD277AFUL, 0x04DB2615UL, 0x73DC1683UL,
    0xE3630B12UL, 0x94643B84UL, 0x0D6D6A3EUL, 0x7A6A5AA8UL,
    0xE40ECF0BUL, 0x9309FF9DUL, 0x0A00AE27UL, 0x7D079EB1UL,
    0xF00F9344UL, 0x8708A3D2UL, 0x1E01F268UL, 0x6906C2FEUL,
    0xF762575DUL, 0x806567CBUL, 0x196C3671UL, 0x6E6B06E7UL,
    0xFED41B76UL, 0x89D32BE0UL, 0x10DA7A5AUL, 0x67DD4ACCUL,
    0xF9B9DF6FUL, 0x8EBEEFF9UL, 0x17B7BE43UL, 0x60B08ED5UL,
    0xD6D6A3E8UL, 0xA1D1937EUL, 0x38D8C2C4UL, 0x4FDFF252UL,
    0xD1BB67F1UL, 0xA6BC5767UL, 0x3FB506DDUL, 0x48B2364BUL,
    0xD80D2BDAUL, 0xAF0A1B4CUL, 0x36034AF6UL, 0x41047A60UL,
    0xDF60EFC3UL, 0xA867DF55UL, 0x316E8EEFUL, 0x4669BE79UL,
    0xCB61B38CUL, 0xBC66831AUL, 0x256FD2A0UL, 0x5268E236UL,
    0xCC0C7795UL, 0xBB0B4703UL, 0x220216B9UL, 0x5505262FUL,
    0xC5BA3BBEUL, 0xB2BD0B28UL, 0x2BB45A92UL, 0x5CB36A04UL,
    0xC2D7FFA7UL, 0xB5D0CF31UL, 0x2CD99E8BUL, 0x5BDEAE1DUL,
    0x9B64C2B0UL, 0xEC63F226UL, 0x756AA39CUL, 0x026D930AUL,
    0x9C0906A9UL, 0xEB0E363FUL, 0x72076785UL, 0x05005713UL,
    0x95BF4A82UL, 0xE2B87A14UL, 0x7BB12BAEUL, 0x0CB61B38UL,
    0x92D28E9BUL, 0xE5D5BE0DUL, 0x7CDCEFB7UL, 0x0BDBDF21UL,
    0x86D3D2D4UL, 0xF1D4E242UL, 0x68DDB3F8UL, 0x1FDA836EUL,
    0x81BE16CDUL, 0xF6B9265BUL, 0x6FB077E1UL, 0x18B74777UL,
    0x88085AE6UL, 0xFF0F6A70UL, 0x66063BCAUL, 0x11010B5CUL,
    0x8F659EFFUL, 0xF862AE69UL, 0x616BFFD3UL, 0x166CCF45UL,
    0xA00AE278UL, 0xD70DD2EEUL, 0x4E048354UL, 0x3903B3C2UL,
    0xA7672661UL, 0xD06016F7UL, 0x4969474DUL, 0x3E6E77DBUL,
    0xAED16A4AUL, 0xD9D65ADCUL, 0x40DF0B66UL, 0x37D83BF0UL,
    0xA9BCAE53UL, 0xDEBB9EC5UL, 0x47B2CF7FUL, 0x30B5FFE9UL,
    0xBDBDF21CUL, 0xCABAC28AUL, 0x53B39330UL, 0x24B4A3A6UL,
    0xBAD03605UL, 0xCDD70693UL, 0x54DE5729UL, 0x23D967BFUL,
    0xB3667A2EUL, 0xC4614AB8UL, 0x5D681B02UL, 0x2A6F2B94UL,
    0xB40BBE37UL, 0xC30C8EA1UL, 0x5A05DF1BUL, 0x2D02EF8DUL
};

static ULONG crc32_update(ULONG crc, const char *buf, int len)
{
    int i;
    for (i = 0; i < len; i++) {
        crc = crc32_table[((unsigned char)crc ^ (unsigned char)buf[i]) & 0xFF]
              ^ (crc >> 8);
    }
    return crc;
}

/* ---- Additional file command handlers ---- */

int cmd_copy(struct client *c, const char *args)
{
    /* Static: single-threaded, non-recursive handler -- safe to reuse */
    static char src_path[MAX_CMD_LEN + 1];
    static char dst_path[MAX_CMD_LEN + 1];
    static char copy_buf[4096];
    static char src_comment[80];
    BPTR src_lock;
    BPTR dst_lock;
    struct FileInfoBlock *fib;
    LONG src_prot;
    struct DateStamp src_date;
    BPTR src_fh;
    BPTR dst_fh;
    LONG n;
    LONG written;
    int noclone = 0;
    int noreplace = 0;
    const char *p;

    /* Parse flags from args */
    p = args;
    while (*p) {
        const char *tok_start;
        int tok_len;

        while (*p == ' ' || *p == '\t')
            p++;
        if (*p == '\0')
            break;

        tok_start = p;
        while (*p && *p != ' ' && *p != '\t')
            p++;
        tok_len = (int)(p - tok_start);

        if (tok_len == 7 && strnicmp(tok_start, "NOCLONE", 7) == 0) {
            noclone = 1;
        } else if (tok_len == 9 && strnicmp(tok_start, "NOREPLACE", 9) == 0) {
            noreplace = 1;
        } else {
            send_error(c->fd, ERR_SYNTAX, "Unknown flag");
            send_sentinel(c->fd);
            return 0;
        }
    }

    /* Read source path */
    if (recv_line_blocking(c, src_path, sizeof(src_path)) < 0)
        return -1;

    /* Read destination path */
    if (recv_line_blocking(c, dst_path, sizeof(dst_path)) < 0)
        return -1;

    if (src_path[0] == '\0' || dst_path[0] == '\0') {
        send_error(c->fd, ERR_SYNTAX, "Missing path");
        send_sentinel(c->fd);
        return 0;
    }

    /* Lock source to examine it */
    src_lock = Lock((STRPTR)src_path, ACCESS_READ);
    if (!src_lock) {
        send_dos_error(c->fd, "Lock source failed");
        return 0;
    }

    /* Check if source and destination are the same */
    dst_lock = Lock((STRPTR)dst_path, ACCESS_READ);
    if (dst_lock) {
        LONG same = SameLock(src_lock, dst_lock);
        UnLock(dst_lock);
        if (same == LOCK_SAME) {
            UnLock(src_lock);
            send_error(c->fd, ERR_IO,
                       "Source and destination are the same file");
            send_sentinel(c->fd);
            return 0;
        }
    }

    /* Examine source */
    fib = AllocDosObject(DOS_FIB, NULL);
    if (!fib) {
        UnLock(src_lock);
        send_error(c->fd, ERR_INTERNAL, "Out of memory");
        send_sentinel(c->fd);
        return 0;
    }

    if (!Examine(src_lock, fib)) {
        send_dos_error(c->fd, "Examine source failed");
        FreeDosObject(DOS_FIB, fib);
        UnLock(src_lock);
        return 0;
    }

    if (fib->fib_DirEntryType > 0) {
        send_error(c->fd, ERR_IO, "Source is a directory");
        send_sentinel(c->fd);
        FreeDosObject(DOS_FIB, fib);
        UnLock(src_lock);
        return 0;
    }

    /* Save metadata for cloning */
    src_prot = fib->fib_Protection;
    src_date = fib->fib_Date;
    strncpy(src_comment, (const char *)fib->fib_Comment,
            sizeof(src_comment) - 1);
    src_comment[sizeof(src_comment) - 1] = '\0';

    FreeDosObject(DOS_FIB, fib);
    UnLock(src_lock);

    /* NOREPLACE: check if destination exists */
    if (noreplace) {
        BPTR test_lock;
        test_lock = Lock((STRPTR)dst_path, ACCESS_READ);
        if (test_lock) {
            UnLock(test_lock);
            send_error(c->fd, ERR_EXISTS, "Destination already exists");
            send_sentinel(c->fd);
            return 0;
        }
    }

    /* Open source for reading */
    src_fh = Open((STRPTR)src_path, MODE_OLDFILE);
    if (!src_fh) {
        send_dos_error(c->fd, "Open source failed");
        return 0;
    }

    /* Open destination for writing */
    dst_fh = Open((STRPTR)dst_path, MODE_NEWFILE);
    if (!dst_fh) {
        LONG err = IoErr();
        Close(src_fh);
        SetIoErr(err);
        send_dos_error(c->fd, "Open destination failed");
        return 0;
    }

    /* Copy loop */
    for (;;) {
        n = Read(src_fh, (STRPTR)copy_buf, sizeof(copy_buf));
        if (n == 0)
            break; /* EOF */
        if (n < 0) {
            LONG err = IoErr();
            Close(src_fh);
            Close(dst_fh);
            DeleteFile((STRPTR)dst_path);
            SetIoErr(err);
            send_dos_error(c->fd, "Read source failed");
            return 0;
        }
        written = Write(dst_fh, (STRPTR)copy_buf, n);
        if (written != n) {
            LONG err = IoErr();
            Close(src_fh);
            Close(dst_fh);
            DeleteFile((STRPTR)dst_path);
            SetIoErr(err);
            send_dos_error(c->fd, "Write destination failed");
            return 0;
        }
    }

    Close(src_fh);
    Close(dst_fh);

    /* Clone metadata unless NOCLONE */
    if (!noclone) {
        SetProtection((STRPTR)dst_path, src_prot);
        SetFileDate((STRPTR)dst_path, &src_date);
        if (src_comment[0] != '\0')
            SetComment((STRPTR)dst_path, (STRPTR)src_comment);
    }

    send_ok(c->fd, NULL);
    send_sentinel(c->fd);
    return 0;
}

int cmd_append(struct client *c, const char *args)
{
    /* Static: single-threaded, non-recursive handler -- safe to reuse */
    static char append_path[MAX_CMD_LEN + 1];
    static char chunk_buf[4096];
    const char *size_str;
    const char *last_space;
    unsigned long declared_size;
    unsigned long total_received;
    char *endp;
    BPTR lock;
    struct FileInfoBlock *fib;
    BPTR fh;
    LONG seek_result;
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
        send_error(c->fd, ERR_SYNTAX, "Usage: APPEND <path> <size>");
        send_sentinel(c->fd);
        return 0;
    }

    size_str = last_space + 1;
    if (*size_str == '\0') {
        send_error(c->fd, ERR_SYNTAX, "Usage: APPEND <path> <size>");
        send_sentinel(c->fd);
        return 0;
    }

    /* Copy the path portion */
    {
        int pathlen = (int)(last_space - args);
        if (pathlen >= (int)sizeof(append_path))
            pathlen = (int)sizeof(append_path) - 1;
        memcpy(append_path, args, pathlen);

        /* Trim trailing whitespace from path */
        while (pathlen > 0 && (append_path[pathlen - 1] == ' ' ||
                               append_path[pathlen - 1] == '\t'))
            pathlen--;
        append_path[pathlen] = '\0';
    }

    if (append_path[0] == '\0') {
        send_error(c->fd, ERR_SYNTAX, "Usage: APPEND <path> <size>");
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

    /* Verify file exists and is not a directory */
    lock = Lock((STRPTR)append_path, ACCESS_READ);
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

    FreeDosObject(DOS_FIB, fib);
    UnLock(lock);

    /* Open for append: MODE_OLDFILE + Seek to end */
    fh = Open((STRPTR)append_path, MODE_OLDFILE);
    if (!fh) {
        send_dos_error(c->fd, "Open failed");
        return 0;
    }

    seek_result = Seek(fh, 0, OFFSET_END);
    if (seek_result == -1) {
        send_dos_error(c->fd, "Seek failed");
        Close(fh);
        return 0;
    }

    send_ready(c->fd);

    /* Receive DATA/END lines (same pattern as cmd_write) */
    total_received = 0;
    while (1) {
        int result;

        result = recv_line_blocking(c, line, sizeof(line));
        if (result < 0) {
            Close(fh);
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
                return -1;
            }

            if (recv_exact_from_client(c, chunk_buf, (int)chunk_len) < 0) {
                Close(fh);
                return -1;
            }

            written = Write(fh, chunk_buf, (LONG)chunk_len);
            if (written != (LONG)chunk_len) {
                Close(fh);
                return -1;
            }

            total_received += chunk_len;

            /* Safety cap: disconnect if far too much data received */
            if (total_received > declared_size + 4096) {
                Close(fh);
                return -1;
            }
        } else {
            /* Malformed line */
            Close(fh);
            return -1;
        }
    }

    Close(fh);

    /* Verify size */
    if (total_received != declared_size) {
        send_error(c->fd, ERR_IO, "Size mismatch");
        send_sentinel(c->fd);
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

int cmd_checksum(struct client *c, const char *args)
{
    BPTR lock;
    struct FileInfoBlock *fib;
    BPTR fh;
    LONG file_size;
    ULONG crc;
    LONG n;
    /* Static: single-threaded, non-recursive handler -- safe to reuse */
    static char csum_buf[4096];
    static char line[64];

    if (args[0] == '\0') {
        send_error(c->fd, ERR_SYNTAX, "Missing path argument");
        send_sentinel(c->fd);
        return 0;
    }

    /* Lock and examine */
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

    /* Open file for reading */
    fh = Open((STRPTR)args, MODE_OLDFILE);
    if (!fh) {
        send_dos_error(c->fd, "Open failed");
        return 0;
    }

    /* Compute CRC32 */
    crc = 0xFFFFFFFFUL;
    for (;;) {
        n = Read(fh, (STRPTR)csum_buf, sizeof(csum_buf));
        if (n == 0)
            break;
        if (n < 0) {
            send_dos_error(c->fd, "Read failed");
            Close(fh);
            return 0;
        }
        crc = crc32_update(crc, csum_buf, (int)n);
    }
    crc ^= 0xFFFFFFFFUL;

    Close(fh);

    send_ok(c->fd, NULL);
    sprintf(line, "crc32=%08lx", (unsigned long)crc);
    send_payload_line(c->fd, line);
    sprintf(line, "size=%ld", (long)file_size);
    send_payload_line(c->fd, line);
    send_sentinel(c->fd);
    return 0;
}

int cmd_setcomment(struct client *c, const char *args)
{
    /* Static: single-threaded, non-recursive handler -- safe to reuse */
    static char sc_path[MAX_CMD_LEN + 1];
    const char *tab;
    const char *comment;
    int pathlen;

    if (args[0] == '\0') {
        send_error(c->fd, ERR_SYNTAX, "Missing arguments");
        send_sentinel(c->fd);
        return 0;
    }

    /* Find first TAB separator */
    tab = strchr(args, '\t');
    if (!tab) {
        send_error(c->fd, ERR_SYNTAX, "Missing tab separator");
        send_sentinel(c->fd);
        return 0;
    }

    /* Extract path */
    pathlen = (int)(tab - args);
    if (pathlen == 0) {
        send_error(c->fd, ERR_SYNTAX, "Missing path");
        send_sentinel(c->fd);
        return 0;
    }
    if (pathlen >= (int)sizeof(sc_path))
        pathlen = (int)sizeof(sc_path) - 1;
    memcpy(sc_path, args, pathlen);
    sc_path[pathlen] = '\0';

    /* Comment is everything after the TAB */
    comment = tab + 1;

    if (!SetComment((STRPTR)sc_path, (STRPTR)comment)) {
        send_dos_error(c->fd, "SetComment failed");
        return 0;
    }

    send_ok(c->fd, NULL);
    send_sentinel(c->fd);
    return 0;
}
