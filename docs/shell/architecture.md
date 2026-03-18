# Architecture

The amigactl interactive shell is a `cmd.Cmd`-based REPL that provides an
interactive interface to the amigactld daemon running on an Amiga. It
maintains a persistent TCP connection, tracks a current working directory
on the remote filesystem, resolves paths locally before sending them to
the daemon, and provides tab completion, command history, and colorized
output.

This document describes the shell's internal design. For user-facing
command documentation, see the other files in this directory. For the
wire protocol and daemon architecture, see the main amigactl
documentation.


## Shell Design

### Class Hierarchy

`AmigaShell` extends Python's `cmd.Cmd` from the standard library. The
`cmd.Cmd` base class provides the core REPL mechanics: reading input via
`readline`, dispatching to `do_*` methods by command name, calling
`complete_*` methods for tab completion, and formatting `help_*` output
from docstrings.

`AmigaShell` adds:

- A persistent `AmigaConnection` instance (`self.conn`) for daemon
  communication.
- A `ColorWriter` instance (`self.cw`) for ANSI-colorized output.
- A current working directory (`self.cwd`) for path resolution.
- A `_DirCache` instance for tab completion performance.
- An error-handling wrapper (`_run`) around all daemon calls.

The class is instantiated in `__main__.py` with host, port, and an
optional editor path from the config file:

```python
sh = AmigaShell(host, port, editor=editor)
sh.cmdloop()
```

### Main Loop

The main loop is `cmd.Cmd.cmdloop()`, invoked from `__main__.main()`.
Before the loop begins, the `preloop()` hook establishes the connection,
configures readline, and sets the initial CWD. After the loop exits,
`postloop()` closes the connection.

`KeyboardInterrupt` (Ctrl-C) is not caught by `cmd.Cmd` -- it
propagates out of `cmdloop()` to the caller in `__main__.py`, which
prints a newline and exits cleanly.

Ctrl-D sends EOF, which triggers `do_EOF()`. This method prints a
newline (to avoid a dangling prompt) and returns `True`, which tells
`cmd.Cmd` to exit the loop. The `exit` and `quit` commands also return
`True` to exit.

The `emptyline()` method is overridden to do nothing. Without this
override, `cmd.Cmd` would repeat the previous command on blank input.

### Command Dispatch

`cmd.Cmd` dispatches commands by looking up `do_<name>` methods on the
class. When the user types `ls SYS:S`, the framework calls
`do_ls("SYS:S")`. Each `do_*` method handles its own argument parsing
(using `shlex.split` for commands with multiple arguments or flags) and
calls the daemon through `self.conn`.

Several commands have aliases. Four are simple class-level assignments:

- `do_copy` = `do_cp`
- `do_comment` = `do_setcomment`
- `do_getenv` = `do_env`
- `do_caps` = `do_capabilities`

`do_dir` is a separate method that delegates to `do_ls` (rather than a
direct assignment) so it can carry its own docstring for `help dir`.

These aliases are hidden from the `help` listing via `get_names()`,
which filters out a set of internal command names (`do_EOF`, `do_quit`,
`do_dir`, `do_copy`, `do_comment`, `do_getenv`, `do_caps`).

The `default()` method handles input that does not match any `do_*`
method. It has one special behavior: if the input is a single token
containing a colon or ending with a slash (i.e., it looks like a path),
it is treated as an implicit `cd`. Otherwise, it prints an "Unknown
command" message.

### Error Handling Wrapper

Most commands route their daemon calls through `_run()`, which wraps
a callable in a try/except block that catches three exception families:

| Exception | Handling |
|-----------------|-----------------------------------------------------|
| `AmigactlError` | Prints the daemon error message in red. Returns `None`. |
| `ProtocolError` | Prints a protocol error message. Returns `None`. |
| `OSError` | Prints a connection error. Sets `self.conn = None` and updates the prompt to reflect the disconnected state. Returns `None`. |

On success, `_run()` returns the function's return value. For functions
that return `None` on success (e.g., `delete`, `makedir`), it returns
the sentinel string `"ok"` so callers can distinguish success from
error by checking `if result is not None`.

The `OSError` case is the connection loss detector. When a socket
operation raises `OSError` (which includes `ConnectionResetError`,
`BrokenPipeError`, and timeout errors), `_run` clears the connection
reference and updates the prompt. All subsequent commands will fail
at `_check_connected()` until the user runs `reconnect`.


## Connection Lifecycle

### Establishing a Connection

The connection is established in `preloop()`, which runs once before
the REPL loop starts. It creates an `AmigaConnection` with the host,
port, and timeout provided at shell construction, then calls
`connect()`.

`AmigaConnection.connect()` opens a TCP socket, sets the timeout, and
reads the daemon's banner line. The banner must start with `"AMIGACTL "`
-- if it does not, the connection is rejected with a `ProtocolError`.
The banner includes the daemon version string and is stored for later
retrieval.

After a successful connection, `preloop()` reads the daemon version,
prints a welcome message, and attempts to set the initial CWD to
`SYS:` by issuing a `STAT SYS:` command. If `SYS:` is unreachable
(e.g., the boot volume has a different name), the CWD is left as
`None`.

If the connection cannot be established at all, `preloop()` prints an
error and raises `SystemExit(1)`.

### Reconnection

Connection loss is detected by `_run()` when a daemon call raises
`OSError`. At that point, `self.conn` is set to `None` and the prompt
changes to `amiga>` (no host, no CWD).

In the disconnected state, all commands that call `_check_connected()`
will print a message directing the user to run `reconnect` or `exit`.
The CWD is preserved across disconnection so it does not need to be
re-established after reconnecting.

The `reconnect` command creates a new `AmigaConnection`, calls
`connect()`, and verifies the connection by issuing a `VERSION` command.
If either step fails, the connection is cleaned up and the shell remains
in disconnected state. On success, the prompt is updated to show the
host and preserved CWD.

### Disconnection

The shell can be exited by:

- Typing `exit` or `quit` (both return `True` from their `do_*` method).
- Pressing Ctrl-D (triggers `do_EOF`, which prints a newline and
  returns `True`).
- Pressing Ctrl-C (caught in `__main__.py`, exits without cleanup
  message).

On normal exit, `postloop()` runs. If `self.conn is not None`, it
calls `AmigaConnection.close()`, which sends a `QUIT` command to the
daemon (best-effort, errors ignored) so the daemon can release the
client slot, then closes the socket. If the connection was already
lost (e.g., after `shutdown` or `reboot` set `conn` to `None`),
`close()` is not called and no `QUIT` is sent. After cleanup, it
prints "Disconnected."

The `shutdown` and `reboot` commands also disconnect after sending their
respective commands. They set `self.conn = None` and update the prompt,
leaving the shell in disconnected state. The user can then `exit`, or
`reconnect` once the daemon is running again (after restarting it
manually, or after the Amiga finishes rebooting in the `reboot` case).


## Path Resolution

### Pipeline Overview

Every command that accepts an Amiga path calls `_resolve_path()` to
convert the user's input into an absolute path before sending it to the
daemon. Resolution is a three-step pipeline:

1. **Validate** -- `_validate_path()` checks that all characters in
   the path are representable in ISO-8859-1 (the wire protocol
   encoding). If validation fails, the method returns `None` and the
   command is aborted.

2. **Normalize dots** -- If the path is relative and contains `.` or
   `..` segments, `_normalize_dotdot()` translates them into Amiga
   conventions. Leading `..` segments become leading `/` characters
   (the Amiga parent directory syntax). Mid-path `..` segments pop the
   preceding component. Single `.` segments are removed.

3. **Join with CWD** -- `_join_amiga_path()` merges the normalized
   relative path with the current working directory. This function is
   colon-aware: if the base path ends with `:` (volume root), the
   relative part is appended directly; if it ends with `/` or has no
   trailing separator, a `/` is inserted. Leading `/` characters in the
   relative path consume parent components from the base, stopping at
   the volume root.

Absolute paths (those containing a colon) skip steps 2 and 3 -- they
are returned as-is after validation.

If no CWD is set and the path is relative, it is passed through
unmodified. The daemon will reject it, which is the correct behavior --
it prompts the user to set a directory with `cd` first.

### Implementation Details

For the full user-facing behavior of path resolution, including worked
examples of dot normalization, colon-aware joining, and parent
navigation clamping at the volume root, see
[navigation.md](navigation.md).


## Directory Cache

### Purpose

Tab completion requires querying the daemon for directory listings.
Without caching, every Tab keypress would send a `DIR` command over
the network, introducing noticeable latency. The `_DirCache` class
reduces these round-trips by caching recent directory listings.

### TTL and Eviction

Each cache entry stores a resolved absolute path mapped to a tuple of
`(timestamp, entries)`. Entries are considered fresh for 5 seconds
(`ttl=5.0`). After the TTL expires, the next access re-queries the
daemon.

The cache holds at most 100 entries (`max_entries=100`). When the cache
is full and a new entry must be added, the oldest entry (by timestamp)
is evicted. This is not strictly LRU (it evicts by insertion/refresh
time rather than access time), but the effect is similar given the
short TTL.

Cache lookups use `time.monotonic()` for timestamps, so they are immune
to system clock changes.

### Invalidation

The cache is cleared entirely (`_dir_cache.invalidate()`) after any
operation that modifies the filesystem:

- `put` (file upload)
- `append` (file append)
- `rm` (delete)
- `mv` (rename)
- `mkdir` (create directory)
- `cp` / `copy` (copy file)
- `chmod` (protection bits change)
- `touch` (datestamp/create)
- `setcomment` / `comment` (file comment change)
- `edit` (file edit and upload)

Read-only operations (`ls`, `cat`, `stat`, `get`, `find`, `grep`,
`tree`, `du`) do not invalidate the cache. Neither does `cd`, since
changing the CWD does not modify the filesystem.

Cache misses (e.g., querying a nonexistent directory) return an empty
list without storing a cache entry, so they are retried on the next
completion attempt.


## Tab Completion

### How It Works

Tab completion is implemented by `_complete_path()`, which is bound
to individual commands via class-level assignments:

```python
complete_cd = _complete_path
complete_ls = _complete_path
complete_cat = _complete_path
# ... and so on for all path-accepting commands
```

When the user presses Tab, `cmd.Cmd` calls the appropriate `complete_*`
method with the current token text. `_complete_path()` splits the text
into a directory prefix and a name prefix at the last `/` or `:`, then:

1. Resolves the directory prefix against the CWD to get an absolute
   Amiga path.
2. Queries the daemon for that directory's contents via
   `_dir_cache.get()` (which uses the cache or issues a `DIR` command).
3. Filters entries whose names start with the name prefix
   (case-insensitive, matching Amiga filesystem behavior).
4. Returns completion strings with the original directory prefix
   prepended.

Some commands have position-aware completion. `complete_get()` only
completes the first argument as an Amiga path (the second is a local
path). `complete_put()` and `complete_append()` only complete the
second argument as an Amiga path (the first is a local path).

### Completion Behavior

Directories are returned with a trailing `/` appended to their name,
while files are returned without one. This visual distinction helps
users navigate deeper into the directory tree by pressing Tab again.

The readline completer delimiters are configured in `preloop()` to
`" \t\n"` -- only whitespace characters act as delimiters. This means
path characters like `/` and `:` are not treated as word boundaries,
so the full path token (e.g., `SYS:S/Star`) is passed to
`_complete_path()` as a single `text` argument. Without this
configuration, readline would split on `:` and `/`, breaking Amiga
path completion.

Readline space suppression is not explicitly configured; `cmd.Cmd`
handles this by default. When a single completion match is found,
readline appends a space after it (standard behavior). When the match
is a directory ending with `/`, this means the user can immediately
start typing the next path component.


## Terminal Output

### Color and Formatting

The `ColorWriter` class (`colors.py`) provides ANSI color support with
automatic detection. Color is enabled when all of the following are
true:

- The `NO_COLOR` environment variable is not set.
- `AMIGACTL_COLOR` is not set to `"never"`.
- `sys.stdout.isatty()` returns `True`.

Color can be forced on with `AMIGACTL_COLOR=always`, overriding the
TTY check.

`ColorWriter` wraps text in ANSI escape sequences and appends a reset
code. When color is disabled, it returns the text unmodified. The
semantic methods and their colors are:

| Method | Color | Usage |
|---------------|-----------|-----------------------------------------------|
| `error()` | Red | Error messages |
| `success()` | Green | Confirmation messages (e.g., "Uploaded") |
| `directory()` | Blue | Directory names in `ls` output |
| `key()` | Cyan | Metadata keys in `stat`, `sysinfo` output |
| `bold()` | Bold | Table headers |
| `warning()` | Yellow | Warning messages |
| `dim()` | Dim | De-emphasized text |
| `reverse()` | Reverse | Highlighted selections (e.g., active items in trace grid) |

The shell creates a single `ColorWriter` instance in `__init__` and
uses it throughout the session. All user-facing output passes through
`cw` methods for consistent styling.

### Output Encoding

The amigactl wire protocol uses ISO-8859-1 (Latin-1) encoding, defined
as `ENCODING = "iso-8859-1"` in `protocol.py`. All strings received
from the daemon -- file names, directory listings, command output, error
messages -- are decoded from ISO-8859-1 to Python strings.

Path validation (`_validate_path()`) ensures that paths typed by the
user can be encoded back to ISO-8859-1 before being sent to the daemon.
Characters outside the ISO-8859-1 range (code points above U+00FF,
such as emoji or CJK characters) are rejected with an error message.

Terminal display uses Python's default encoding (typically UTF-8). Since
ISO-8859-1 is a subset of Unicode, all characters received from the
daemon display correctly in any UTF-8 terminal. The reverse direction
(user input to daemon) is where the encoding constraint applies.

### Command History

The shell uses readline's persistent history. In `preloop()`, the
history file `~/.amigactl_history` is loaded if it exists. An `atexit`
handler is registered to save the history when the Python process exits,
ensuring command history survives across sessions.


## Prompt Format

The prompt dynamically reflects the connection state and current
directory. It is updated by `_update_prompt()` after every CWD change,
connection, or disconnection:

| State | Prompt |
|-------------------------------|-------------------------------------|
| Connected, CWD set | `amiga@192.168.6.200:SYS:S> ` |
| Connected, CWD at volume root| `amiga@192.168.6.200:SYS:> ` |
| Connected, no CWD | `amiga@192.168.6.200> ` |
| Disconnected | `amiga> ` |


## Related Documentation

- [navigation.md](navigation.md) -- Path resolution behavior and
  navigation commands from the user's perspective.
- [file-operations.md](file-operations.md) -- File and directory
  management commands.
- [file-transfer.md](file-transfer.md) -- Transferring files between
  the client and the Amiga.
- [command-execution.md](command-execution.md) -- Running commands on
  the Amiga.
- [system-commands.md](system-commands.md) -- System information and
  control commands.
- [search-utilities.md](search-utilities.md) -- find, grep, tree, du,
  and other analysis commands.
- [arexx.md](arexx.md) -- ARexx command dispatch.
