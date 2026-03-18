# Navigation

The amigactl interactive shell maintains a current working directory
(CWD) on the Amiga, resolves relative paths against it, and provides
`cd` and `pwd` commands for directory navigation. Understanding how the
shell handles paths is important because Amiga path syntax differs from
Unix in several ways -- parent directory navigation, volume separators,
and the role of assigns all work differently than their Unix
counterparts.


## Amiga Path Syntax

### Volumes and Assigns

Every absolute Amiga path begins with a volume or assign name followed
by a colon. The colon is the volume separator -- it serves the same
structural role as the leading `/` in a Unix path, but it appears after
the device name rather than before the first directory.

```bash
amiga@192.168.6.228:SYS:> ls SYS:C
amiga@192.168.6.228:SYS:> ls Work:Projects
amiga@192.168.6.228:SYS:> cat DEVS:Monitors/VGAOnly
```

`SYS:` is the boot volume. `Work:`, `RAM:`, and `DEVS:` are other
common volumes. Assigns are logical names that point to one or more
physical directories (e.g., `LIBS:` typically points to `SYS:Libs`).
From the shell's perspective, volumes and assigns are interchangeable --
any name followed by a colon is treated as an absolute path.

A bare volume name with just the colon refers to the root of that
volume:

```bash
amiga@192.168.6.228:SYS:> cd Work:
amiga@192.168.6.228:Work:> cd RAM:
amiga@192.168.6.228:RAM:> pwd
RAM:
```

### Relative Paths

A path without a colon is relative. The shell resolves it against the
current working directory before sending it to the daemon. Subdirectory
components are separated by `/`, just as in Unix:

```bash
amiga@192.168.6.228:SYS:> cd S
amiga@192.168.6.228:SYS:S> ls Startup-Sequence
amiga@192.168.6.228:SYS:S> cd ../Devs
amiga@192.168.6.228:SYS:Devs>
```

When the CWD ends with `:` (the volume root), relative paths are
appended directly without an additional separator. When the CWD is a
subdirectory, a `/` separator is inserted. This distinction matters
because `SYS:/C` is an invalid Amiga path while `SYS:C` is correct.

### Parent Directory Navigation

AmigaOS uses a leading `/` to mean "go up one directory level" -- the
opposite of what `/` means in Unix. Multiple leading slashes go up
multiple levels:

| Syntax | Meaning |
|--------|----------------------------------------|
| `/`    | Go up one level (like Unix `..`)       |
| `//`   | Go up two levels (like Unix `../..`)   |
| `///`  | Go up three levels                     |

The shell also accepts `..` as a convenience for Unix users. When the
path contains `.` or `..` segments, they are translated into the Amiga
`/` convention before resolution. Mid-path `..` segments pop the
preceding directory component, and `.` segments are silently removed.

```bash
amiga@192.168.6.228:SYS:S> cd /
amiga@192.168.6.228:SYS:> cd S
amiga@192.168.6.228:SYS:S> cd ..
amiga@192.168.6.228:SYS:> cd Devs/Monitors/../DOSDrivers
amiga@192.168.6.228:SYS:Devs/DOSDrivers>
```

Parent navigation stops at the volume root. Attempting to go above the
volume root is silently clamped:

```bash
amiga@192.168.6.228:SYS:S> cd //////////
amiga@192.168.6.228:SYS:>
```


## Commands

### cd

Change the current working directory on the Amiga.

```
cd [PATH]
```

`cd` accepts an absolute or relative path. If the path is relative, it
is resolved against the current CWD (see [Path Resolution](#path-resolution)
below). Before updating the CWD, the shell validates the target by
sending a `STAT` request to the daemon. If the path does not exist or
is not a directory, an error is printed and the CWD is unchanged.

```bash
amiga@192.168.6.228:SYS:> cd Work:Projects
amiga@192.168.6.228:Work:Projects> cd /
amiga@192.168.6.228:Work:> cd RAM:
amiga@192.168.6.228:RAM:>
```

**Default behavior:** Running `cd` with no arguments returns to `SYS:`
(the boot volume root), analogous to `cd ~` in Unix shells. If `SYS:`
is unreachable, the CWD is cleared entirely and the prompt reverts to
showing no directory.

```bash
amiga@192.168.6.228:Work:Projects/foo> cd
amiga@192.168.6.228:SYS:>
```

**Error handling:** If the target does not exist, the shell reports the
fully resolved path so you can see exactly what was attempted:

```bash
amiga@192.168.6.228:SYS:> cd NoSuchDir
Directory not found: SYS:NoSuchDir
amiga@192.168.6.228:SYS:> cd Work:Bogus
Directory not found: Work:Bogus
```

If the target exists but is a file rather than a directory:

```bash
amiga@192.168.6.228:SYS:> cd S/Startup-Sequence
Not a directory: SYS:S/Startup-Sequence
```

**Tab completion:** `cd` supports tab completion for Amiga paths. Type a
partial path and press Tab to see matching directories and files.
Directories are shown with a trailing `/` to distinguish them from
files. The completion queries the daemon for the directory contents, so
it reflects the live filesystem state.

### pwd

Print the current working directory.

```
pwd
```

Prints the absolute Amiga path of the current working directory. If no
directory has been set (e.g., the shell could not reach `SYS:` at
startup and the user has not yet used `cd`), a message is printed
instead.

```bash
amiga@192.168.6.228:SYS:S> pwd
SYS:S
amiga@192.168.6.228:SYS:> pwd
SYS:
```

The CWD is also always visible in the shell prompt. The prompt format is
`amiga@<host>:<cwd>>`, so `pwd` is primarily useful for scripts or when
you want to copy the path.


## Path Resolution

When you type a relative path (one without a `:`), the shell resolves
it into an absolute Amiga path before sending anything to the daemon.
This resolution happens in three steps:

1. **ISO-8859-1 validation.** The path is checked for encoding
   compatibility (see [Path Validation](#path-validation) below). If
   validation fails, the command is aborted immediately -- no further
   resolution is attempted.

2. **Dot normalization.** If the path contains `.` or `..` segments,
   they are converted to Amiga conventions. Leading `..` segments become
   leading `/` characters (Amiga parent navigation). Mid-path `..`
   segments pop the preceding component. Single `.` segments are
   removed. For example, `../Devs` becomes `/Devs`, and
   `S/../Devs/./Monitors` becomes `Devs/Monitors`.

3. **Joining with CWD.** The normalized relative path is joined with
   the current working directory using Amiga path rules. If the CWD ends
   with `:`, the relative path is appended directly (`SYS:` + `S` =
   `SYS:S`). If the CWD is a subdirectory, a `/` separator is inserted
   (`SYS:S` + `User-Startup` = `SYS:S/User-Startup`). Leading `/`
   characters in the relative path consume parent components from the
   CWD, stopping at the volume root. A pure parent navigation (e.g.,
   `cd /` from `SYS:Devs/Monitors`) strips the trailing component and
   returns the parent (`SYS:Devs`).

Absolute paths (those containing `:`) skip steps 2 and 3 -- they are
passed to the daemon as-is after validation.

Here is a worked example showing each step:

```bash
# CWD is SYS:Devs/Monitors
amiga@192.168.6.228:SYS:Devs/Monitors> cat ../../S/Startup-Sequence
```

1. All characters are in the ISO-8859-1 range, so validation passes.
2. `../../S/Startup-Sequence` normalizes to `//S/Startup-Sequence`
   (two leading `..` become two leading `/`).
3. Joining `SYS:Devs/Monitors` with `//S/Startup-Sequence`:
   - First `/`: strip `Monitors`, CWD becomes `SYS:Devs/`.
   - Second `/`: strip `Devs`, CWD becomes `SYS:`.
   - Append `S/Startup-Sequence`: result is `SYS:S/Startup-Sequence`.

If no CWD is set (the shell connected but `SYS:` was unreachable and no
`cd` has been issued), relative paths are passed to the daemon
unresolved -- dot normalization is also skipped, so the raw user input
(including any `..` segments) is sent through unmodified. The daemon
will reject them, producing an error. Use `cd` to set an absolute
starting point first.


## Path Validation

The amigactl wire protocol encodes paths as ISO-8859-1 (Latin-1). Paths
containing characters outside this encoding -- such as those from
non-Latin Unicode scripts -- are rejected before the command is sent to
the daemon.

```bash
amiga@192.168.6.228:SYS:> cd Emoji\U0001f600Dir
Path contains characters not representable in ISO-8859-1: ...
```

Standard ASCII characters, Western European accented characters, and
other Latin-1 code points (U+0000 through U+00FF) are all accepted.
This covers the full range of characters that AmigaOS filesystems
support. In practice this constraint is rarely encountered -- Amiga
filenames are almost always plain ASCII.

Note that this validation is purely an encoding check -- it does not
validate filesystem-level path legality. Characters such as null bytes
or control characters will pass validation but would fail at the
AmigaOS level.

Validation is applied to both absolute and relative paths, before
resolution. If a path fails validation, the command is not sent and the
CWD is not changed.


## Related Documentation

- [file-operations.md](file-operations.md) -- File and directory
  management commands.
- [file-transfer.md](file-transfer.md) -- Transferring files between
  host and Amiga.
- [tab-completion.md](tab-completion.md) -- Tab completion and readline
  integration.
- [architecture.md](architecture.md) -- Shell architecture and path
  resolution internals.
