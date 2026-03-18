# Search and Analysis Utilities

The amigactl shell provides several commands for searching, comparing,
and analyzing files on the Amiga. Unlike most shell commands, which map
directly to a single protocol operation, these are **shell-only
commands** -- they are implemented entirely on the client side by
combining basic protocol operations (DIR for directory listings, READ
for file contents) and processing the results locally. The Amiga daemon
has no knowledge of these commands; it simply serves directory listings
and file data on request.

This client-side approach means these commands work with any version of
the amigactld daemon without requiring protocol extensions. The
trade-off is that commands like `grep` must download file contents over
the network before searching, and `find` must fetch a full recursive
directory listing before filtering. For large directory trees or many
files, this can be slow compared to native Amiga utilities.

All path arguments follow the same resolution rules described in
[navigation.md](navigation.md) -- relative paths are joined with the
current working directory, `..` is translated to Amiga `/` parent
navigation, and paths are validated for ISO-8859-1 encoding before
being sent to the daemon.


## find

Search for files and directories by name pattern.

### Syntax

```
find PATH PATTERN
find PATH -name PATTERN
find PATH -type f PATTERN
find PATH -type d PATTERN
```

| Argument | Description |
|----------|-------------|
| `PATH` | Directory to search recursively. |
| `PATTERN` | Glob pattern to match against entry names. |

### Options

| Option | Description |
|--------|-------------|
| `-name PATTERN` | Explicit pattern flag. Equivalent to providing the pattern as a positional argument -- useful for clarity when combining with other flags. |
| `-type f` | Match files only. Directories are excluded from results. |
| `-type d` | Match directories only. Files are excluded from results. |

Options and the pattern can appear in any order after the path.

### Pattern Matching

`find` uses glob patterns, not regular expressions. The supported
wildcards are:

| Wildcard | Meaning |
|----------|---------|
| `*` | Match zero or more characters. |
| `?` | Match exactly one character. |
| `[seq]` | Match one character from the set. |
| `[!seq]` | Match one character not in the set. |

Matching is **case-insensitive**, consistent with AmigaOS filesystem
behavior. The pattern `*.INFO` matches `Disk.info`, `icon.INFO`, and
`README.Info` equally.

The pattern is matched against the **basename only** (the final path
component), not the full relative path. A pattern of `*.c` matches
`src/main.c` because the basename `main.c` matches, even though the
full relative path does not start with `*`.

Internally, `find` fetches a complete recursive directory listing from
the daemon in a single request, then filters the results locally using
Python's `fnmatch` module. No file contents are transferred -- only
metadata.

### Examples

Find all `.info` files anywhere under `SYS:`:

```bash
amiga@192.168.6.200:SYS:> find SYS: *.info
Disk.info
Tools/Calculator.info
Prefs/Pointer.info
```

Find only directories matching a pattern:

```bash
amiga@192.168.6.200:SYS:> find SYS: -type d S*
S
Storage
System
```

Find only files with an explicit `-name` flag:

```bash
amiga@192.168.6.200:SYS:> find Work: -type f -name *.c
Projects/hello/main.c
Projects/hello/util.c
```

Directories in the output are displayed with color highlighting (if
the terminal supports it) to distinguish them from files.


## grep

Search file contents for a text pattern.

### Syntax

```
grep PATTERN FILE
grep [OPTIONS] PATTERN FILE
grep -r PATTERN PATH
```

| Argument | Description |
|----------|-------------|
| `PATTERN` | Search string. Literal by default; regex with `-E`. |
| `FILE` | File to search (single-file mode). |
| `PATH` | Directory to search recursively (with `-r`). |

### Options

| Option | Description |
|--------|-------------|
| `-E` | Treat `PATTERN` as a regular expression instead of a literal string. |
| `-i` | Case-insensitive matching. |
| `-r` | Recursive mode: search all files under `PATH`. |
| `-n` | Show line numbers alongside matching lines. |
| `-c` | Show only the count of matching lines, not the lines themselves. |
| `-l` | Show only the names of files that contain matches. Implies `-r`. |

Options can be combined into a single flag group (e.g., `-rni`,
`-Eil`).

### Pattern Matching

By default, the pattern is treated as a **literal string**. Special
regex characters like `.`, `*`, and `+` have no special meaning and are
matched verbatim. This is the safest mode for searching for exact text.

With `-E`, the pattern is compiled as a Python regular expression. The
full `re` module syntax is supported, including character classes
(`[a-z]`), alternation (`error|warn`), quantifiers (`+`, `*`, `?`),
and groups.

If the pattern fails to compile as a valid regex, an error is displayed
and the search is not performed.

### Recursive Search

Without `-r`, `grep` searches a single file. The file's contents are
downloaded from the Amiga and searched locally.

With `-r`, `grep` fetches a recursive directory listing of `PATH`, then
downloads and searches each file individually. Directories in the
listing are skipped -- only files are searched. Each matching line is
prefixed with the file's relative path.

Because `grep` must download the contents of every file it searches,
recursive searches over large directory trees can be slow and transfer
significant data. For targeted searches, specify the most specific
directory possible.

File contents are decoded as ISO-8859-1 (Latin-1) for pattern matching.

### Examples

Search for a literal string in a single file:

```bash
amiga@192.168.6.200:SYS:> grep SetPatch S/Startup-Sequence
C:SetPatch QUIET
```

Case-insensitive search with line numbers:

```bash
amiga@192.168.6.200:SYS:> grep -ni setpatch S/Startup-Sequence
3:C:SetPatch QUIET
```

Regex search for alternatives:

```bash
amiga@192.168.6.200:SYS:> grep -Ei "error|warning" RAM:build.log
Warning: implicit declaration of printf
Error: undefined symbol _main
```

Recursive search showing matching filenames only:

```bash
amiga@192.168.6.200:SYS:> grep -rl AddBuffers SYS:S
Startup-Sequence
```

Count matches per file in a recursive search:

```bash
amiga@192.168.6.200:SYS:> grep -rc TODO Work:src
main.c:3
util.c:1
```

Recursive search with line numbers:

```bash
amiga@192.168.6.200:SYS:> grep -rn include Work:src
main.c:1:#include <stdio.h>
main.c:2:#include "util.h"
util.c:1:#include "util.h"
```

Count matches in a single file (prints a bare number):

```bash
amiga@192.168.6.200:SYS:> grep -c SetPatch S/Startup-Sequence
1
```


## diff

Compare two remote files and display the differences.

### Syntax

```
diff FILE1 FILE2
```

| Argument | Description |
|----------|-------------|
| `FILE1` | First file (the "original"). |
| `FILE2` | Second file (the "modified"). |

Both files are downloaded from the Amiga and compared locally. No flags
or options are accepted -- the output format is always unified diff.

### Output Format

`diff` produces unified diff output, the same format used by
`diff -u` on Unix systems. The output includes:

- A header showing the two file paths (prefixed with `---` and `+++`).
- Hunk headers (`@@`) showing the line ranges that differ.
- Lines beginning with `-` that are present in FILE1 but not FILE2.
- Lines beginning with `+` that are present in FILE2 but not FILE1.
- Context lines (unchanged) with a space prefix.

Output is colorized when the terminal supports it: deletions in red,
additions in green, and hunk headers in bold.

If the files are identical, `diff` prints "Files are identical" instead
of producing empty output.

### Binary Detection

Before comparing, `diff` checks both files for null bytes (`\x00`). If
either file contains a null byte, it is treated as binary. Binary files
are not diffed -- instead, `diff` reports either "Binary files are
identical" or "Binary files differ".

This is a simple heuristic. Text files that happen to contain null
bytes will be treated as binary. Conversely, binary files without null
bytes (rare but possible) will produce meaningless diff output.

### Examples

Compare a startup sequence with its backup:

```bash
amiga@192.168.6.200:SYS:> diff S/Startup-Sequence S/Startup-Sequence.bak
--- SYS:S/Startup-Sequence
+++ SYS:S/Startup-Sequence.bak
@@ -3,7 +3,6 @@
 C:SetPatch QUIET
 C:AddBuffers DH0: 50
-C:Run >NIL: amigactld
 FailAt 21
```

When files are identical:

```bash
amiga@192.168.6.200:SYS:> diff RAM:copy.txt RAM:copy2.txt
Files are identical
```

When files are binary:

```bash
amiga@192.168.6.200:SYS:> diff C:Dir C:List
Binary files differ
```


## du

Show disk usage for a directory tree.

### Syntax

```
du [PATH]
du [OPTIONS] [PATH]
```

| Argument | Description |
|----------|-------------|
| `PATH` | Directory to analyze. If omitted, the current working directory is used. |

### Options

| Option | Description |
|--------|-------------|
| `-s` | Summary only. Show just the grand total instead of per-directory subtotals. |
| `-h` | Human-readable sizes. Format byte counts with K, M, G, T suffixes instead of raw byte values. |

Options can be combined (e.g., `-sh`).

`du` works by fetching a complete recursive directory listing from the
daemon and accumulating file sizes locally. Only file sizes reported in
the directory metadata are counted -- directory entries themselves have
zero size. Subdirectory totals are propagated upward so that each
directory's reported size includes all of its descendants.

The output format is tab-separated: size followed by the directory
path. Per-directory subtotals are listed alphabetically, with the
root total appearing last.

### Examples

Show per-directory breakdown:

```bash
amiga@192.168.6.200:SYS:> du S
640	SubDir
1152	SYS:S
```

The last line shows the grand total for the directory. In this
example, `SubDir` contains 640 bytes, the remaining 512 bytes are
files directly in `S`, and the total across the entire tree is 1152
bytes.

Summary only with human-readable sizes:

```bash
amiga@192.168.6.200:SYS:> du -sh Work:
2.5M	Work:
```

Default (current directory, raw byte counts):

```bash
amiga@192.168.6.200:SYS:Devs> du
1024	DOSDrivers
4096	Keymaps
2048	Monitors
7168	SYS:Devs
```

Human-readable without summary:

```bash
amiga@192.168.6.200:SYS:> du -h Libs
45.2K	SYS:Libs
```


## watch

Repeatedly execute a shell command and display its output.

### Syntax

```
watch [-n SECONDS] COMMAND
```

| Argument | Description |
|----------|-------------|
| `COMMAND` | Any amigactl shell command to execute repeatedly. |

### Options

| Option | Description |
|--------|-------------|
| `-n SECONDS` | Set the refresh interval in seconds. Accepts decimal values (e.g., `-n 0.5`). Default: **2 seconds**. The interval must be positive. |

`watch` clears the terminal before each execution and displays a header
line showing the interval and command. The command is then executed
exactly as if you had typed it at the prompt. After the command
completes, `watch` sleeps for the specified interval, then repeats.

Press **Ctrl-C** to stop watching and return to the shell prompt.

`COMMAND` can be any shell command, including those with flags and
arguments. The entire remainder of the line after the interval (or
after `watch` if no `-n` is given) is treated as the command string.

If the connection to the daemon is lost during execution, `watch`
stops automatically.

### Examples

Monitor a process list, refreshing every 2 seconds (default):

```bash
amiga@192.168.6.200:SYS:> watch ps
Every 2.0s: ps

ID  COMMAND                STATUS   RC
1   myserver               RUNNING  -
^C
```

Watch a directory for new files with a 5-second interval:

```bash
amiga@192.168.6.200:SYS:> watch -n 5 ls -l RAM:T
Every 5.0s: ls -l RAM:T

       pipe_1            ----rwed  128  2026-03-16 14:00:00
       pipe_2            ----rwed  256  2026-03-16 14:00:05
^C
```

Monitor available memory with a fast refresh:

```bash
amiga@192.168.6.200:SYS:> watch -n 0.5 exec avail
Every 0.5s: exec avail

Type   Available    In-Use   Maximum   Largest
chip     438272    610016   1048576    390144
fast    15204352    573216  15777568  15101952
total   15642624   1183232  16826144  15101952
^C
```


## Related Documentation

- [navigation.md](navigation.md) -- Path syntax, `cd`/`pwd`, and path
  resolution rules.
- [file-operations.md](file-operations.md) -- File and directory
  management commands (`ls`, `stat`, `cat`, `tree`).
- [file-transfer.md](file-transfer.md) -- Transferring files between
  the client and the Amiga.
- [command-execution.md](command-execution.md) -- Running AmigaDOS and
  ARexx commands.
