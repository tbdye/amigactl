# Tab Completion and Command History

The amigactl shell provides tab completion for Amiga paths and command
names, backed by a caching layer that reduces round-trips to the
daemon. It also provides persistent command history across sessions
using readline. This document covers how to use these features
effectively.


## Path Completion

### How It Works

When you type a partial path and press Tab, the shell queries the
daemon for a directory listing, filters the results against what you
have typed so far, and fills in the match. If there are multiple
matches, pressing Tab twice displays all candidates.

For example, starting from `SYS:`:

```
amiga@192.168.6.200:SYS:> cat S/Start<Tab>
```

The shell splits `S/Start` into a directory portion (`S/`) and a name
prefix (`Start`), resolves `S` against the current working directory to
get `SYS:S`, queries the daemon for the contents of `SYS:S`, and
returns entries whose names begin with `Start` (case-insensitive). If
`Startup-Sequence` is the only match, the line is completed to:

```
amiga@192.168.6.200:SYS:> cat S/Startup-Sequence
```

If there are multiple matches (e.g., `Startup-Sequence` and
`StartupII`), pressing Tab twice lists them all, and you can type more
characters to disambiguate.

### Completion Behavior

Directories are returned with a trailing `/` appended to their name,
while files are not. This lets you visually distinguish directories
from files in the candidate list and immediately continue typing into
a completed directory without pressing any additional keys:

```
amiga@192.168.6.200:SYS:> ls Dev<Tab>
Devs/
amiga@192.168.6.200:SYS:> ls Devs/<Tab><Tab>
DOSDrivers/    DataTypes/     Monitors/      Printers/
```

Matching is case-insensitive, consistent with how Amiga filesystems
handle file names. Typing `sys:s/start` will match `Startup-Sequence`
just as well as `S/Start`.

Absolute paths work too. You can type a volume or assign name followed
by a colon and then press Tab to browse that volume:

```
amiga@192.168.6.200:SYS:> cat RAM:<Tab><Tab>
Clipboards/    ENV/           T/
```

### Multi-Argument Commands

Several commands accept both local (client-side) and remote (Amiga-side)
path arguments. The shell only offers tab completion for the Amiga
path argument, since it cannot complete local paths over the daemon
connection.

- **get** -- The first argument is an Amiga path (completed), the
  second is a local destination (not completed).
- **put** -- The first argument is a local path (not completed), the
  second is an Amiga destination (completed).
- **append** -- Same as `put`: the first argument is local, the second
  is completed as an Amiga path.
- **mv**, **cp**, **copy**, **diff** -- Both arguments are Amiga
  paths, and both are completed.

For `get`, completion activates only when you are typing the first
argument. For `put` and `append`, completion activates only on the
second argument. This prevents the shell from trying to resolve a
local file path against the Amiga filesystem.


## Command Completion

On the first word of a line, Tab completes against available command
names. This is built into Python's `cmd.Cmd` framework and requires
no daemon communication:

```
amiga@192.168.6.200:SYS:> ch<Tab><Tab>
checksum  chmod
amiga@192.168.6.200:SYS:> check<Tab>
amiga@192.168.6.200:SYS:> checksum
```


## Commands with Tab Completion

The following commands support Amiga path tab completion:

| Command | Completion behavior |
|-------------|------------------------------------------------|
| `assign` | Amiga path |
| `cat` | Amiga path |
| `cd` | Amiga path |
| `checksum` | Amiga path |
| `chmod` | Amiga path |
| `comment` | Amiga path |
| `copy` | Both arguments (alias for `cp`) |
| `cp` | Both arguments (Amiga source and destination) |
| `diff` | Both arguments (two Amiga files to compare) |
| `dir` | Amiga path (alias for `ls`) |
| `du` | Amiga path |
| `edit` | Amiga path |
| `exec` | Amiga path |
| `find` | Amiga path |
| `get` | First argument only (Amiga source path) |
| `grep` | Amiga path |
| `ls` | Amiga path |
| `mkdir` | Amiga path |
| `mv` | Both arguments (Amiga source and destination) |
| `put` | Second argument only (Amiga destination path) |
| `append` | Second argument only (Amiga destination path) |
| `rm` | Amiga path |
| `run` | Amiga path |
| `setcomment` | Amiga path |
| `stat` | Amiga path |
| `tail` | Amiga path |
| `touch` | Amiga path |
| `tree` | Amiga path |

Commands not listed here (e.g., `pwd`, `sysinfo`, `reconnect`,
`ports`, `version`, `arexx`) do not accept Amiga path arguments and
have no path completion.


## Directory Cache

### Purpose

Each Tab press triggers a directory listing from the daemon. Without
caching, rapid Tab presses or cycling through candidates would send
repeated `DIR` commands over the network, adding noticeable latency.
The directory cache stores recent listings to make completion feel
responsive.

### Behavior

- **TTL**: Each cached listing is valid for 5 seconds. After the TTL
  expires, the next completion request re-queries the daemon.
- **Capacity**: The cache holds up to 100 directory listings. When
  full, the oldest entry is evicted.
- **Automatic invalidation**: Any command that modifies the Amiga
  filesystem (`put`, `append`, `rm`, `mv`, `mkdir`, `cp`, `chmod`,
  `touch`, `setcomment`, `edit`) clears the entire cache. This ensures
  that completion results reflect the current state of the filesystem
  after a mutation. Read-only commands (`ls`, `cat`, `stat`, `get`,
  `find`, `grep`, `tree`, `du`) do not invalidate the cache.
- **Errors**: If the daemon cannot list a directory (e.g., the path
  does not exist), no entry is cached, so the next attempt will
  re-query.

The 5-second TTL is short enough that the cache stays consistent with
the real filesystem during interactive use, while long enough to
eliminate redundant queries during a burst of Tab presses within the
same directory.

For implementation details, see the `_DirCache` class description in
[architecture.md](architecture.md#directory-cache).


## Command History

### History File

The shell saves all commands you type to `~/.amigactl_history`. This
file is loaded when the shell starts and saved automatically when it
exits. History persists across sessions -- commands from previous
sessions are available immediately.

### History Navigation

Standard readline key bindings are available:

| Key | Action |
|----------|------------------------------------------|
| Up | Previous command in history |
| Down | Next command in history |
| Ctrl-R | Reverse incremental search through history |
| Ctrl-S | Forward incremental search (if terminal allows) |
| Ctrl-P | Previous command (same as Up) |
| Ctrl-N | Next command (same as Down) |

Ctrl-R is particularly useful for recalling long paths or complex
commands. Type Ctrl-R followed by a few characters of a previous
command, and readline will jump to the most recent match. Press
Ctrl-R again to cycle through older matches.

```
(reverse-i-search)`star': cat S/Startup-Sequence
```

All other standard readline editing shortcuts (Ctrl-A to move to start
of line, Ctrl-E to end, Ctrl-W to delete word, etc.) work as expected.


## Readline Configuration

### Custom Delimiters

The shell configures readline's completer delimiters to whitespace
only (`" \t\n"`). By default, readline treats many punctuation
characters -- including `:` and `/` -- as word boundaries.

### Why This Matters

Amiga paths contain colons and slashes as structural elements:
`SYS:Devs/Monitors`. If readline used its default delimiters, it
would split `SYS:Devs/Monitors` into three separate tokens (`SYS`,
`Devs`, `Monitors`) and pass only the fragment after the last
delimiter to the completion function. The completion function would
receive `Monitors` with no knowledge of the `SYS:Devs/` prefix,
making it impossible to resolve the correct directory.

With whitespace-only delimiters, the entire path `SYS:Devs/Mon` is
passed as a single token. The completion function can then split it
at the last `/` or `:` itself, resolve the directory portion, and
return properly prefixed completions.

This is configured once during shell startup and requires no user
action.


## Related Documentation

- [architecture.md](architecture.md) -- Shell internals, including
  the `_DirCache` class and `_complete_path` implementation details.
- [navigation.md](navigation.md) -- Path syntax, resolution, and
  navigation commands.
- [file-operations.md](file-operations.md) -- File and directory
  management commands.
- [file-transfer.md](file-transfer.md) -- File transfer commands
  (`get`, `put`, `append`).
