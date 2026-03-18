# File Operations

The amigactl shell provides commands for listing, inspecting, creating,
modifying, and deleting files and directories on the Amiga. All path
arguments follow the same resolution rules described in
[navigation.md](navigation.md) -- relative paths are joined with the
current working directory, `..` is translated to Amiga `/` parent
navigation, and paths are validated for ISO-8859-1 encoding before
being sent to the daemon.

Every command in this document supports tab completion for Amiga paths.
Type a partial path and press Tab to see matching entries from the live
filesystem.


## Directory Listing

### ls

List directory contents.

```
ls [PATH] [-l] [-r]
```

`PATH` is a directory or file path, or a glob pattern. If omitted, the
current working directory is listed.

| Option | Description |
|--------|-------------|
| `-l` | Long format: displays type tag, name, protection bits, size, and datestamp for each entry. |
| `-r` | Recursive listing. Implies `-l`. Lists all entries in the directory tree. |

Options can be combined (e.g., `-rl`).

**Short format** (default) displays names in multi-column layout sized
to the terminal width. Directories are shown with a trailing `/` to
distinguish them from files:

```bash
amiga@192.168.6.228:SYS:> ls
C/          Devs/       Expansion/  L/          Libs/       Locale/
Prefs/      S/          Storage/    T/          Utilities/  WBStartup/
Disk.info   System/     Tools/
```

**Long format** (`-l`) shows a type tag (`DIR` for directories, blank
for files), the entry name, protection bits in `hsparwed` notation,
the file size (human-readable), and the datestamp:

```bash
amiga@192.168.6.228:SYS:> ls -l S
  DIR  S                     ----rwed       2026-01-15 10:30:00
       Startup-Sequence      ----rwed  512  2026-01-10 08:00:00
       User-Startup          ----rwed  128  2026-02-01 14:22:00
       Shell-Startup         ----rwed   96  2025-12-20 09:00:00
```

Size values use a compact format: values under 1024 bytes are shown as
plain integers, larger values use K, M, G, or T suffixes (e.g., `1.5M`).
Directories do not display a size.

**Recursive** (`-r`) lists all entries in the tree with relative paths:

```bash
amiga@192.168.6.228:SYS:> ls -r S
  DIR  S                     ----rwed             2026-01-15 10:30:00
       Startup-Sequence      ----rwed       512   2026-01-10 08:00:00
       User-Startup          ----rwed       128   2026-02-01 14:22:00
```

**Glob patterns** filter entries by name. The `*` and `?` wildcards
match against the basename of each entry. Glob matching is
case-insensitive, consistent with AmigaOS filesystem behavior:

```bash
amiga@192.168.6.228:SYS:> ls S*
S/        Storage/  System/
amiga@192.168.6.228:SYS:> ls *.info
Disk.info
```

When the glob matches nothing, `ls` prints "No match."

`dir` is an alias for `ls` -- they are identical commands.

**Single-file listing:** If the path names a file rather than a
directory, `ls` shows information for that single file:

```bash
amiga@192.168.6.228:SYS:> ls -l S/Startup-Sequence
       Startup-Sequence      ----rwed  512  2026-01-10 08:00:00
```

### tree

Display a directory tree with box-drawing structure.

```
tree [PATH] [-d] [--ascii]
```

`PATH` is the directory to display. If omitted, the current working
directory is used.

| Option | Description |
|--------|-------------|
| `-d` | Show directories only. Files are omitted from the tree. |
| `--ascii` | Use ASCII box-drawing characters instead of Unicode. Useful for terminals that do not support Unicode or when piping output. |

The tree is built from a recursive directory listing. Entries are sorted
alphabetically at each level:

```bash
amiga@192.168.6.228:SYS:> tree S
SYS:S
├── Shell-Startup
├── Startup-Sequence
└── User-Startup

0 directories, 3 files
```

A larger tree with subdirectories:

```bash
amiga@192.168.6.228:SYS:> tree Devs
SYS:Devs
├── DOSDrivers
│   ├── PIPE
│   └── PORT
├── Keymaps
│   └── usa
├── Monitors
│   └── VGAOnly
└── system-configuration

3 directories, 5 files
```

With `--ascii`:

```bash
amiga@192.168.6.228:SYS:> tree --ascii S
SYS:S
|-- Shell-Startup
|-- Startup-Sequence
`-- User-Startup

0 directories, 3 files
```

With `-d` (directories only):

```bash
amiga@192.168.6.228:SYS:> tree -d Devs
SYS:Devs
├── DOSDrivers
├── Keymaps
└── Monitors

3 directories, 0 files
```

The summary line at the bottom always reports the total count of
directories and files in the tree. With `-d`, the file count is
reported as 0 since files are excluded from the traversal.


## File Information

### stat

Show file or directory metadata.

```
stat PATH
```

Displays the type, name, size, protection bits, datestamp, and comment
for a single path. This is the most detailed view of a single entry --
it includes the comment field, which `ls -l` does not show.

```bash
amiga@192.168.6.228:SYS:> stat S/Startup-Sequence
type=FILE
name=Startup-Sequence
size=512
protection=----rwed
datestamp=2026-01-10 08:00:00
comment=Boot script
```

For directories, the type is `DIR` and size is reported as zero:

```bash
amiga@192.168.6.228:SYS:> stat Devs
type=DIR
name=Devs
size=0
protection=----rwed
datestamp=2026-01-15 10:30:00
comment=
```

Protection bits are displayed in `hsparwed` notation (see
[chmod](#chmod) for details on the bit layout).

### checksum

Compute a CRC32 checksum of a remote file.

```
checksum PATH
```

Displays the CRC32 hash as an 8-character lowercase hex string, along
with the file size in bytes. The checksum is computed on the Amiga side,
so no file data is transferred over the network -- useful for verifying
file integrity after a transfer without downloading the file again.

```bash
amiga@192.168.6.228:SYS:> checksum C/Dir
crc32=a1b2c3d4
size=5384
```

The CRC32 value matches Python's `zlib.crc32()` masked with
`0xFFFFFFFF`, so you can compare directly:

```bash
# On the host, after downloading:
python3 -c "import zlib; print(format(zlib.crc32(open('Dir','rb').read()) & 0xFFFFFFFF, '08x'))"
```


## Viewing File Contents

### cat

Display a remote file's contents on the terminal.

```
cat [--offset N] [--length N] PATH
```

Reads the file and writes its raw bytes to stdout. For text files the
content is displayed directly; for binary files you will see raw bytes
(consider piping through `xxd` on the host side).

| Option | Description |
|--------|-------------|
| `--offset N` | Start reading at byte offset `N` (default: 0). |
| `--length N` | Read at most `N` bytes (default: entire file). |

```bash
amiga@192.168.6.228:SYS:> cat S/Startup-Sequence
; Startup-Sequence for Amiga
C:SetPatch QUIET
C:AddBuffers DH0: 50
...
```

Read a specific range of bytes:

```bash
amiga@192.168.6.228:SYS:> cat --offset 100 --length 50 RAM:test.txt
his is a fragment starting at byte 100 and reading
```

To save file contents to a local file instead of displaying them, use
the `get` command (see [file-transfer.md](file-transfer.md)).

### tail

Stream new data appended to a file in real time.

```
tail PATH
```

Continuously displays new data as it is written to the file, similar to
Unix `tail -f`. The command blocks and streams output until you press
Ctrl-C to stop.

```bash
amiga@192.168.6.228:SYS:> tail RAM:logfile.txt
2026-03-16 12:00:01 Connection accepted
2026-03-16 12:00:05 Data received: 128 bytes
^C
```

`tail` detects file truncation and deletion. If the file is truncated
while tailing, it resets to the new end. If the file is deleted, an
error is reported.


## Creating and Modifying

### mkdir

Create a directory on the Amiga.

```
mkdir PATH
```

Creates a single directory. Parent directories must already exist --
there is no `-p` flag for recursive creation.

```bash
amiga@192.168.6.228:SYS:> mkdir RAM:newdir
Created: RAM:newdir
amiga@192.168.6.228:SYS:> mkdir RAM:newdir/sub
Created: RAM:newdir/sub
```

### touch

Set the datestamp on a file or directory, or create an empty file.

```
touch PATH [DATE TIME]
```

If the file exists, its datestamp is updated. If the file does not
exist, an empty file is created (Unix `touch` semantics).

| Argument | Description |
|----------|-------------|
| `PATH` | Amiga file or directory path. |
| `DATE` | Date in `YYYY-MM-DD` format. |
| `TIME` | Time in `HH:MM:SS` format. |

When `DATE` and `TIME` are omitted, the current Amiga system time is
used.

```bash
amiga@192.168.6.228:SYS:> touch RAM:test.txt
Created RAM:test.txt
amiga@192.168.6.228:SYS:> touch RAM:test.txt 2026-02-19 12:00:00
datestamp=2026-02-19 12:00:00
```

When creating a new file with a specific datestamp, `touch` first
creates the empty file, then applies the datestamp as a separate step.

### chmod

Get or set AmigaOS protection bits.

```
chmod PATH [BITS]
```

When called with just a path, displays the current protection bits.
When called with a hex value, sets the protection bits and displays the
new value.

| Argument | Description |
|----------|-------------|
| `PATH` | File or directory path. |
| `BITS` | Raw protection value in hexadecimal (e.g., `0f`, `05`). |

Protection bits are displayed in `hsparwed` notation:

| Position | Letter | Meaning | Set = |
|----------|--------|---------|-------|
| 7 | `h` | Hold (keep in memory) | Active |
| 6 | `s` | Script | Active |
| 5 | `p` | Pure (re-entrant) | Active |
| 4 | `a` | Archive | Active |
| 3 | `r` | Read | **Denied** |
| 2 | `w` | Write | **Denied** |
| 1 | `e` | Execute | **Denied** |
| 0 | `d` | Delete | **Denied** |

The owner RWED bits (bits 0--3) use **inverted** semantics: a set bit
means the operation is *denied*, the opposite of Unix permissions. A
dash in the display means the operation is allowed.

```bash
amiga@192.168.6.228:SYS:> chmod C/Dir
protection=----rwed
amiga@192.168.6.228:SYS:> chmod RAM:test.txt 05
protection=----r-e-
```

In the second example, `05` sets bits 0 (delete) and 2 (write), denying
both delete and write access. The display shows dashes for `d` and `w`
(denied) and letters for `r` and `e` (allowed).

Common hex values:

| Value | Display | Effect |
|-------|---------|--------|
| `00` | `----rwed` | All operations allowed (default). |
| `0f` | `--------` | All RWED operations denied. |
| `05` | `----r-e-` | Write and delete denied (read-only, executable). |
| `01` | `----rwe-` | Delete denied only. |

### setcomment

Set or clear the file comment on a remote file.

```
setcomment PATH COMMENT
```

The comment is a free-text string stored in the filesystem metadata.
Enclose multi-word comments in quotes. An empty string (`""`) clears
the existing comment.

```bash
amiga@192.168.6.228:SYS:> setcomment RAM:test.txt "Important file"
Comment set on RAM:test.txt
amiga@192.168.6.228:SYS:> setcomment RAM:test.txt ""
Comment set on RAM:test.txt
```

`comment` is an alias for `setcomment` -- they are identical commands.

Use `stat` to view the current comment on a file.


## Moving and Copying

### mv

Rename or move a file or directory on the Amiga.

```
mv OLD NEW
```

Both paths are resolved against the current working directory. Both
paths must reside on the same volume -- this is an AmigaOS limitation.
Cross-volume moves are not supported.

```bash
amiga@192.168.6.228:SYS:> mv RAM:test.txt RAM:renamed.txt
Renamed: RAM:test.txt -> RAM:renamed.txt
amiga@192.168.6.228:RAM:> mv oldname newname
Renamed: RAM:oldname -> RAM:newname
```

### cp

Copy a file on the Amiga.

```
cp [-P] [-n] SOURCE DEST
```

Copies a file from `SOURCE` to `DEST`. By default, metadata
(protection bits, datestamp, comment) is cloned from the source to the
destination.

| Option | Description |
|--------|-------------|
| `-P` | Do not clone metadata (protection bits, datestamp, comment). The destination file gets default values. |
| `-n` | No-replace mode. If the destination already exists, the copy fails with an error instead of overwriting. |

Options can be combined (e.g., `-Pn`).

```bash
amiga@192.168.6.228:SYS:> cp RAM:file.txt RAM:backup.txt
Copied: RAM:file.txt -> RAM:backup.txt
amiga@192.168.6.228:SYS:> cp -n SYS:S/Startup-Sequence RAM:Startup-Sequence
Copied: SYS:S/Startup-Sequence -> RAM:Startup-Sequence
```

`copy` is an alias for `cp` -- they are identical commands.

The copy is performed entirely on the Amiga side. No file data is
transferred over the network, making this much faster than downloading
and re-uploading.


## Deleting

### rm

Delete a file or empty directory on the Amiga.

```
rm PATH
```

Deletes the named file or directory. If the target is a directory, it
must be empty -- there is no recursive delete and no confirmation
prompt.

```bash
amiga@192.168.6.228:SYS:> rm RAM:test.txt
Deleted: RAM:test.txt
amiga@192.168.6.228:SYS:> rm RAM:emptydir
Deleted: RAM:emptydir
```

Attempting to delete a non-empty directory produces an error from the
daemon. To remove a directory tree, delete the contents first (files,
then subdirectories from the bottom up).


## Related Documentation

- [navigation.md](navigation.md) -- Path syntax, `cd`/`pwd`, and path
  resolution rules.
- [file-transfer.md](file-transfer.md) -- Transferring files between
  host and Amiga (`get`, `put`, `append`, `edit`).
- [tab-completion.md](tab-completion.md) -- Tab completion and readline
  integration.
- [architecture.md](architecture.md) -- Shell architecture and path
  resolution internals.
