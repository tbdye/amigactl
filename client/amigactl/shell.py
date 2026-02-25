"""Interactive shell for amigactl."""

import cmd
import difflib
import fnmatch
import os
import re
import shlex
import shutil
import sys
import time

from . import (
    AmigaConnection, AmigactlError, NotFoundError, ProtocolError,
)
from .colors import ColorWriter


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def format_size(nbytes):
    """Format a byte count as a human-readable string.

    Returns the value with a suffix: B, K, M, G, T.
    Values under 1024 are shown as plain integers.
    Values >= 999.5 in a given unit roll over to the next unit
    (e.g. 999.5K displays as 1.0M, not 1000K).
    """
    if nbytes < 1024:
        return str(nbytes)
    for unit in ("K", "M", "G", "T"):
        nbytes = nbytes / 1024.0
        if nbytes < 999.95 or unit == "T":
            if nbytes == int(nbytes):
                return "{:.0f}{}".format(int(nbytes), unit)
            return "{:.1f}{}".format(nbytes, unit)
    return str(nbytes)


def _format_protection(hex_str):
    """Convert raw fib_Protection hex to hsparwed display string.

    AmigaOS protection bits (bit positions):
      bit 7: h (hold/hidden)
      bit 6: s (script)
      bit 5: p (pure)
      bit 4: a (archive)
      bit 3: r (read)     -- INVERTED: set = denied
      bit 2: w (write)    -- INVERTED: set = denied
      bit 1: e (execute)  -- INVERTED: set = denied
      bit 0: d (delete)   -- INVERTED: set = denied
    """
    try:
        bits = int(hex_str, 16)
    except (ValueError, TypeError):
        return hex_str  # Can't parse, show raw

    flags = "hspa"
    result = []
    for i, ch in enumerate(flags):
        if bits & (1 << (7 - i)):
            result.append(ch)
        else:
            result.append("-")
    # RWED bits are inverted
    inv_flags = "rwed"
    for i, ch in enumerate(inv_flags):
        if bits & (1 << (3 - i)):
            result.append("-")  # set = denied
        else:
            result.append(ch)  # clear = allowed
    return "".join(result)


def _join_amiga_path(base, relative):
    """Join an Amiga base directory path with a relative path.

    Handles Amiga conventions:
    - base must end with ':' or '/' (it's a directory)
    - leading '/' in relative means "go up one level"
    - multiple leading '/' means go up multiple levels

    Examples:
        _join_amiga_path("SYS:S", "Startup-Sequence")
            -> "SYS:S/Startup-Sequence"
        _join_amiga_path("SYS:", "S")
            -> "SYS:S"
        _join_amiga_path("Work:Projects/foo", "/bar")
            -> "Work:Projects/bar"
        _join_amiga_path("Work:Projects", "//test")
            -> "Work:test"
        _join_amiga_path("Work:", "/test")
            -> "Work:test"    (can't go above volume root)
    """
    # Normalize base: ensure it ends with ':' or '/'
    if not base.endswith(":") and not base.endswith("/"):
        base = base + "/"

    # Handle leading '/' (parent directory navigation)
    while relative.startswith("/"):
        relative = relative[1:]
        # Strip one path component from base
        if base.endswith("/"):
            base = base[:-1]  # remove trailing /
            # Find the previous separator
            slash = base.rfind("/")
            colon = base.rfind(":")
            sep = max(slash, colon)
            if sep >= 0:
                base = base[:sep + 1]
            # else: already at volume root, can't go higher
        # If base ends with ':', we're at volume root -- stay there

    if not relative:
        # Pure parent navigation, return the base
        if base.endswith(":"):
            return base
        return base.rstrip("/")

    # Join
    if base.endswith(":") or base.endswith("/"):
        return base + relative
    return base + "/" + relative


def _amiga_basename(path):
    """Extract filename from an Amiga path.

    Examples:
        SYS:S/Startup-Sequence -> Startup-Sequence
        RAM:test.txt -> test.txt
        Work: -> Work  (volume root -- unusual but handle it)
    """
    if "/" in path:
        return path.rsplit("/", 1)[1]
    if ":" in path:
        return path.rsplit(":", 1)[1] or path.rstrip(":")
    return path


def _normalize_dotdot(path):
    """Resolve .. segments in a relative path for Amiga compatibility.

    Leading .. segments become Amiga / parent navigation.
    Mid-path .. segments are resolved by popping the preceding segment.
    Single . segments are removed.
    """
    parts = path.split("/")
    resolved = []
    leading_parents = 0
    in_leading = True

    for part in parts:
        if part == "..":
            if in_leading:
                leading_parents += 1
            elif resolved:
                resolved.pop()
            else:
                leading_parents += 1
        elif part == ".":
            continue
        else:
            in_leading = False
            if part:
                resolved.append(part)

    prefix = "/" * leading_parents
    return prefix + "/".join(resolved)


_ANSI_RE = re.compile(r'\033\[[0-9;]*m')


def _visible_len(s):
    """Return display width of string, ignoring ANSI escape codes."""
    return len(_ANSI_RE.sub('', s))


def _find_filter(entries, pattern, type_filter=None):
    """Filter directory entries by glob pattern and optional type.

    entries     List of dir entry dicts (from conn.dir).
    pattern     Glob pattern string matched against basename only.
    type_filter 'f' for files only, 'd' for directories only, or None.

    Returns matching entries. Matching is case-insensitive.
    """
    pattern_lower = pattern.lower()
    result = []
    for entry in entries:
        if type_filter == "f" and entry["type"].lower() == "dir":
            continue
        if type_filter == "d" and entry["type"].lower() != "dir":
            continue
        name = entry["name"]
        # Match against basename only (last component)
        if "/" in name:
            basename = name.rsplit("/", 1)[1]
        else:
            basename = name
        if fnmatch.fnmatch(basename.lower(), pattern_lower):
            result.append(entry)
    return result


def _build_tree(entries):
    """Build a nested tree structure from recursive DIR entries.

    entries     List of dir entry dicts with relative path names.

    Returns a list of root-level nodes. Each node is a dict:
        {'name': str, 'type': str, 'children': []}
    """
    root_children = []
    # Map from directory path to its children list for fast lookup
    dir_map = {}
    dir_map[""] = root_children

    # Sort entries so directories come before their contents
    sorted_entries = sorted(entries, key=lambda e: e["name"].lower())

    for entry in sorted_entries:
        name = entry["name"]
        if "/" in name:
            parent_path, basename = name.rsplit("/", 1)
        else:
            parent_path = ""
            basename = name

        node = {
            "name": basename,
            "type": entry["type"].lower(),
            "children": [],
        }

        # Ensure parent exists in dir_map
        if parent_path not in dir_map:
            # Create intermediate directory nodes as needed
            parts = parent_path.split("/")
            current = ""
            for part in parts:
                prev = current
                current = part if not current else current + "/" + part
                if current not in dir_map:
                    intermediate = {
                        "name": part,
                        "type": "dir",
                        "children": [],
                    }
                    dir_map[current] = intermediate["children"]
                    parent_list = dir_map.get(prev, root_children)
                    parent_list.append(intermediate)

        parent_list = dir_map.get(parent_path, root_children)
        parent_list.append(node)

        if node["type"] == "dir":
            dir_map[name] = node["children"]

    return root_children


def _format_tree(root_name, tree, dirs_only=False, ascii_mode=False):
    """Render a tree structure as a list of display lines.

    root_name   Name of the root directory to display.
    tree        List of nodes from _build_tree().
    dirs_only   If True, only show directories.
    ascii_mode  If True, use ASCII box-drawing characters.

    Returns (lines, dir_count, file_count) where lines is a list of
    strings and the counts are totals across the entire tree.
    """
    if ascii_mode:
        branch = "|-- "
        last_branch = "`-- "
        vertical = "|   "
        blank = "    "
    else:
        branch = "\u251c\u2500\u2500 "
        last_branch = "\u2514\u2500\u2500 "
        vertical = "\u2502   "
        blank = "    "

    lines = [root_name]
    dir_count = 0
    file_count = 0

    def _walk(children, prefix):
        nonlocal dir_count, file_count
        # Filter if dirs_only
        if dirs_only:
            visible = [c for c in children if c["type"] == "dir"]
        else:
            visible = list(children)

        for i, node in enumerate(visible):
            is_last = (i == len(visible) - 1)
            connector = last_branch if is_last else branch

            lines.append(prefix + connector + node["name"])

            if node["type"] == "dir":
                dir_count += 1
                child_prefix = prefix + (blank if is_last else vertical)
                _walk(node["children"], child_prefix)
            else:
                file_count += 1

    _walk(tree, "")
    return lines, dir_count, file_count


def _grep_lines(text, pattern, is_regex=False, ignore_case=False):
    """Search text for lines matching a pattern.

    text         Content as a string.
    pattern      Search pattern (literal string or regex).
    is_regex     If True, compile pattern as regex; otherwise escape it.
    ignore_case  If True, match case-insensitively.

    Returns list of (line_number, line_text) tuples. line_number is
    1-based.
    """
    flags = re.IGNORECASE if ignore_case else 0
    if is_regex:
        compiled = re.compile(pattern, flags)
    else:
        compiled = re.compile(re.escape(pattern), flags)

    results = []
    for i, line in enumerate(text.splitlines(), 1):
        if compiled.search(line):
            results.append((i, line))
    return results


def _du_accumulate(entries):
    """Accumulate per-directory sizes from recursive DIR entries.

    entries     List of dir entry dicts (from recursive DIR).

    Returns (dir_sizes, total) where dir_sizes is a list of
    (directory_path, size) tuples with per-directory subtotals
    (including all descendants), and total is the grand total.
    """
    dir_totals = {}  # directory path -> accumulated size
    dir_totals["."] = 0  # root directory

    for entry in entries:
        name = entry["name"]
        size = entry["size"]

        if "/" in name:
            parent = name.rsplit("/", 1)[0]
        else:
            parent = "."

        if parent not in dir_totals:
            dir_totals[parent] = 0
        dir_totals[parent] += size

    # Build result: propagate subdirectory sizes upward
    # Sort by depth (deepest first) so children are processed before parents
    sorted_dirs = sorted(dir_totals.keys(),
                         key=lambda d: d.count("/"), reverse=True)

    # Propagate: add each dir's total into its parent
    propagated = dict(dir_totals)
    for d in sorted_dirs:
        if d == ".":
            continue
        if "/" in d:
            parent = d.rsplit("/", 1)[0]
        else:
            parent = "."
        if parent not in propagated:
            propagated[parent] = 0
        propagated[parent] += propagated[d]

    # Build output list sorted alphabetically, "." first
    result = []
    for d in sorted(propagated.keys()):
        if d == ".":
            continue
        result.append((d, propagated[d]))
    # Append root as "." at the end
    total = propagated["."]
    result.append((".", total))

    return result, total


# ---------------------------------------------------------------------------
# Directory cache (used by tab completion in Step 4)
# ---------------------------------------------------------------------------

class _DirCache:
    """Brief cache for DIR results to avoid repeated round-trips."""

    def __init__(self, ttl=5.0, max_entries=100):
        self.ttl = ttl
        self.max_entries = max_entries
        self._cache = {}  # resolved_path -> (timestamp, entries)

    def get(self, conn, resolved_path):
        """Return DIR entries for resolved_path, using cache if fresh.

        resolved_path must be an absolute Amiga path (volume-qualified).
        """
        import time
        now = time.monotonic()
        if resolved_path in self._cache:
            ts, entries = self._cache[resolved_path]
            if now - ts < self.ttl:
                return entries
        try:
            entries = conn.dir(resolved_path)
            # Evict oldest entry if cache is full
            if len(self._cache) >= self.max_entries:
                oldest_key = min(
                    self._cache, key=lambda k: self._cache[k][0])
                del self._cache[oldest_key]
            self._cache[resolved_path] = (now, entries)
            return entries
        except Exception:
            return []

    def invalidate(self):
        """Clear the entire cache."""
        self._cache.clear()


# ---------------------------------------------------------------------------
# Interactive shell
# ---------------------------------------------------------------------------

class AmigaShell(cmd.Cmd):
    """Interactive shell for communicating with an amigactld daemon."""

    intro = ""  # Set dynamically after connect
    prompt = "amiga> "

    def __init__(self, host, port, timeout=30, editor=None):
        super().__init__()
        self.host = host
        self.port = port
        self.timeout = timeout
        self.conn = None
        self.cw = ColorWriter()
        self.cwd = None  # Current working directory (Amiga path)
        self._dir_cache = _DirCache()
        self._editor = editor  # from config file; None = use env/default

    # -- Lifecycle ---------------------------------------------------------

    def preloop(self):
        """Connect to the daemon and configure readline before the REPL."""
        self.conn = AmigaConnection(self.host, self.port, self.timeout)
        try:
            self.conn.connect()
        except Exception as e:
            print("Failed to connect to {}:{}: {}".format(
                self.host, self.port, e), file=sys.stderr)
            raise SystemExit(1)

        # Configure readline
        try:
            import readline
            readline.set_completer_delims(" \t\n")
            # Load history
            histfile = os.path.expanduser("~/.amigactl_history")
            try:
                readline.read_history_file(histfile)
            except (FileNotFoundError, OSError):
                pass
            import atexit
            atexit.register(readline.write_history_file, histfile)
        except ImportError:
            pass

        ver = self.conn.version()
        print("Connected to {} ({})".format(self.host, ver))
        print('Type "help" for a list of commands, "exit" to disconnect.')

        # Set initial CWD to SYS: (standard boot volume)
        try:
            info = self.conn.stat("SYS:")
            if info.get("type", "").lower() == "dir":
                self.cwd = "SYS:"
        except Exception:
            pass  # Leave cwd as None if SYS: unreachable
        self._update_prompt()

    def postloop(self):
        """Disconnect when the REPL exits."""
        if self.conn is not None:
            self.conn.close()
            self.conn = None
        print("Disconnected.")

    def _update_prompt(self):
        """Update the prompt to reflect connection state and CWD."""
        if self.conn is None:
            self.prompt = "amiga> "
        elif self.cwd is not None:
            self.prompt = "amiga@{}:{}> ".format(self.host, self.cwd)
        else:
            self.prompt = "amiga@{}> ".format(self.host)

    # -- Error handling wrapper --------------------------------------------

    def _run(self, func, *args, **kwargs):
        """Run a command function, catching amigactl exceptions.

        Returns the function's return value on success, or None on error.
        For functions that return None on success (e.g. delete, makedir),
        returns the sentinel string "ok" to distinguish from error.
        """
        try:
            result = func(*args, **kwargs)
            if result is None:
                return "ok"
            return result
        except AmigactlError as e:
            print(self.cw.error("Error: {}".format(e.message)))
            return None
        except ProtocolError as e:
            print(self.cw.error("Protocol error: {}".format(e)))
            return None
        except OSError as e:
            print(self.cw.error("Connection error: {}".format(e)))
            # Connection is likely dead
            self.conn = None
            self._update_prompt()
            return None

    # -- Helpers -----------------------------------------------------------

    def _check_connected(self):
        """Check if the connection is still alive.

        Returns True if connected, False otherwise. Prints a message on
        failure so the caller can simply return.
        """
        if self.conn is None:
            print("Not connected. Use 'reconnect' to re-establish or "
                  "'exit' to quit.")
            return False
        return True

    def _prepend_cd(self, command):
        """Prepend CD=<cwd> to a command for daemon exec with correct CWD."""
        if self.cwd:
            return "CD={} {}".format(self.cwd, command)
        return command

    def _validate_path(self, path):
        """Validate that a path can be encoded to ISO-8859-1."""
        try:
            path.encode("iso-8859-1")
        except UnicodeEncodeError as e:
            print(self.cw.error(
                "Path contains characters not representable in "
                "ISO-8859-1: {}".format(e)))
            return False
        return True

    def _resolve_path(self, user_path):
        """Resolve user_path against the shell's CWD.

        If user_path is absolute (contains ':'), return it unchanged.
        If user_path is relative and CWD is set, join them.
        If user_path is relative and CWD is not set, return it unchanged
        (the daemon will likely reject it, which is the correct behavior --
        the user hasn't cd'd anywhere yet).

        Returns None if the path contains characters outside ISO-8859-1.
        Callers must check for None and skip the command if so.
        """
        user_path = user_path.strip()
        if not user_path:
            return user_path

        # Validate ISO-8859-1 encoding before any resolution
        if not self._validate_path(user_path):
            return None

        # Absolute path -- contains volume separator
        if ":" in user_path:
            return user_path

        # Relative path -- needs CWD
        if self.cwd is None:
            return user_path  # Let daemon reject it

        # Translate Unix .. and . to Amiga path conventions
        segments = user_path.split("/")
        if any(s == ".." or s == "." for s in segments):
            user_path = _normalize_dotdot(user_path)

        return _join_amiga_path(self.cwd, user_path)

    # -- Tab completion ----------------------------------------------------

    def _complete_path(self, text, line, begidx, endidx):
        """Tab-complete an Amiga path argument.

        With whitespace-only delimiters, ``text`` is the full
        space-delimited token (e.g., "SYS:S/Star").  We split it into a
        directory prefix and a name prefix, query the daemon for the
        directory contents, filter, and return completions with the full
        directory prefix prepended.
        """
        if self.conn is None:
            return []

        # Split text into directory prefix and name prefix.
        # Find the last separator (/ or :).
        last_slash = text.rfind("/")
        last_colon = text.rfind(":")
        split_pos = max(last_slash, last_colon)

        if split_pos >= 0:
            dir_prefix = text[:split_pos + 1]  # includes trailing / or :
            name_prefix = text[split_pos + 1:]
            # Directory to query: strip trailing / but keep trailing :
            if dir_prefix.endswith("/"):
                dir_to_query = dir_prefix[:-1]
            else:
                dir_to_query = dir_prefix  # ends with :
        else:
            dir_prefix = ""
            dir_to_query = ""
            name_prefix = text

        # Resolve relative directory part against CWD for the DIR query.
        if dir_to_query and ":" not in dir_to_query:
            # Relative directory (no volume)
            if self.cwd is None:
                return []
            resolved_dir = _join_amiga_path(self.cwd, dir_to_query)
        elif not dir_to_query:
            # No directory part at all
            if self.cwd is None:
                return []
            resolved_dir = self.cwd
        else:
            # Absolute path
            resolved_dir = dir_to_query

        # Query directory (cache uses resolved absolute path as key)
        entries = self._dir_cache.get(self.conn, resolved_dir)

        # Filter by name prefix (case-insensitive, Amiga FS)
        prefix_lower = name_prefix.lower()
        results = []
        for entry in entries:
            name = entry.get("name", "")
            if name.lower().startswith(prefix_lower):
                if entry.get("type", "").lower() == "dir":
                    results.append(dir_prefix + name + "/")
                else:
                    results.append(dir_prefix + name)

        return results

    # Single-path commands: delegate directly to _complete_path
    complete_cd = _complete_path
    complete_ls = _complete_path
    complete_cat = _complete_path
    complete_stat = _complete_path
    complete_rm = _complete_path
    complete_mkdir = _complete_path
    complete_chmod = _complete_path
    complete_touch = _complete_path
    complete_tail = _complete_path
    complete_edit = _complete_path

    # mv: both arguments are Amiga paths
    complete_mv = _complete_path

    def complete_get(self, text, line, begidx, endidx):
        """Complete get: first arg is Amiga path, second is local."""
        # Count words before cursor to determine argument position
        prefix = line[:begidx]
        args = prefix.split()
        # args[0] is "get", so position 1 is the Amiga path
        if len(args) <= 1:
            return self._complete_path(text, line, begidx, endidx)
        return []

    def complete_put(self, text, line, begidx, endidx):
        """Complete put: first arg is local, second is Amiga path."""
        prefix = line[:begidx]
        args = prefix.split()
        # args[0] is "put", position 1 is local path, position 2+ is Amiga
        if len(args) >= 2:
            return self._complete_path(text, line, begidx, endidx)
        return []

    # -- Navigation --------------------------------------------------------

    def do_cd(self, arg):
        """Change the current working directory on the Amiga.

    Usage: cd [PATH]

    PATH    Absolute (e.g., SYS:S) or relative path. Omit to return
            to SYS: (the home directory).

    Amiga path conventions:
        /       Go up one directory level (like Unix ..).
        //      Go up two levels, etc.
        ..      Also supported as a convenience (translated to /).

    Examples:
        cd SYS:S
        cd Devs
        cd /
        cd ..
        cd"""
        path = arg.strip()

        # cd with no arguments -- return to SYS: (home directory)
        if not path:
            if not self._check_connected():
                return
            try:
                info = self.conn.stat("SYS:")
                if info.get("type", "").lower() == "dir":
                    self.cwd = "SYS:"
                    self._update_prompt()
                    return
            except Exception:
                pass
            self.cwd = None
            self._update_prompt()
            return

        if not self._check_connected():
            return

        # Resolve relative paths
        resolved = self._resolve_path(path)
        if resolved is None:
            return

        # Validate the directory exists via STAT
        try:
            info = self.conn.stat(resolved)
        except NotFoundError:
            print(self.cw.error(
                "Directory not found: {}".format(resolved)))
            return
        except AmigactlError as e:
            print(self.cw.error("Error: {}".format(e.message)))
            return
        except OSError as e:
            print(self.cw.error("Connection error: {}".format(e)))
            self.conn = None
            self._update_prompt()
            return

        if info.get("type", "").lower() != "dir":
            print(self.cw.error("Not a directory: {}".format(resolved)))
            return

        # Strip trailing '/' for cleanliness (but keep trailing ':')
        if resolved.endswith("/"):
            resolved = resolved[:-1]

        self.cwd = resolved
        self._update_prompt()

    def do_pwd(self, arg):
        """Print the current working directory on the Amiga.

    Usage: pwd"""
        if self.cwd is None:
            print("No current directory set. Use 'cd' to set one.")
        else:
            print(self.cwd)

    # -- File listing and info ---------------------------------------------

    def do_ls(self, arg):
        """List directory contents.

    Usage: ls [PATH] [-l] [-r]

    PATH    Directory to list (default: current working directory).
            Glob patterns (* and ?) are supported.
    -l      Long format: type, name, protection bits, size, date.
    -r      Recursive (implies -l).

    Without -l, names are shown in multi-column format. Directories
    are marked with a trailing /.

    Examples:
        ls
        ls SYS:S
        ls -l
        ls -rl Work:
        ls *.info
        ls S*"""
        if not self._check_connected():
            return
        parts = shlex.split(arg) if arg.strip() else []
        recursive = False
        long_format = False
        path_parts = []
        for part in parts:
            if part.startswith("-") and len(part) > 1:
                for ch in part[1:]:
                    if ch == "r":
                        recursive = True
                    elif ch == "l":
                        long_format = True
                    else:
                        print("Unknown flag: -{}".format(ch))
                        return
            else:
                path_parts.append(part)

        # Recursive always implies long format
        if recursive:
            long_format = True

        if not path_parts:
            if self.cwd is not None:
                path = self.cwd
            else:
                print("Usage: ls PATH [-r] [-l]"
                      " (or set a directory with 'cd' first)")
                return
        else:
            path = self._resolve_path(path_parts[0])
            if path is None:
                return

        # Check for glob characters in the user-supplied argument
        raw_arg = path_parts[0] if path_parts else ""
        has_glob = any(c in raw_arg for c in ("*", "?"))

        if has_glob:
            # Glob mode: split resolved path into parent directory + pattern
            # and filter the parent's contents.
            if "/" in path:
                parent, pattern = path.rsplit("/", 1)
            elif ":" in path:
                parent, pattern = path.split(":", 1)
                parent += ":"
            else:
                # Relative pattern with no separator -- list CWD
                parent = self.cwd if self.cwd is not None else ""
                pattern = path

            all_entries = self._run(self.conn.dir, parent)
            if all_entries is None:
                return
            pattern_lower = pattern.lower()
            entries = [
                e for e in all_entries
                if fnmatch.fnmatch(e["name"].lower(), pattern_lower)
            ]
            if not entries:
                print("No match.")
                return
        else:
            # Try dir() first (normal directory listing)
            try:
                entries = self.conn.dir(path, recursive=recursive)
            except Exception:
                # dir() failed -- try stat() for single-file fallback
                try:
                    info = self.conn.stat(path)
                    if info.get("type", "").lower() == "file":
                        # Construct a single-entry list matching dir format.
                        # Ensure basename for display (_amiga_basename is
                        # defensive; stat already returns fib_FileName).
                        info["name"] = _amiga_basename(info["name"])
                        entries = [info]
                    else:
                        # Not a file (e.g., broken path) -- show original
                        # dir error via _run for proper error display
                        entries = self._run(
                            self.conn.dir, path, recursive=recursive)
                        if entries is None:
                            return
                except Exception:
                    # stat() also failed -- show original dir error
                    entries = self._run(
                        self.conn.dir, path, recursive=recursive)
                    if entries is None:
                        return

        if not entries:
            return

        if long_format:
            # Detailed format (existing behavior)
            max_name = 0
            max_size = 0
            max_prot = 0
            for entry in entries:
                name_len = len(entry["name"])
                if name_len > max_name:
                    max_name = name_len
                size_str = format_size(entry["size"])
                size_len = len(size_str)
                if size_len > max_size:
                    max_size = size_len
                prot_str = _format_protection(entry["protection"])
                prot_len = len(prot_str)
                if prot_len > max_prot:
                    max_prot = prot_len

            for entry in entries:
                is_dir = entry["type"].lower() == "dir"
                name = entry["name"]
                date = entry["datestamp"]

                if is_dir:
                    tag = self.cw.directory("  DIR")
                else:
                    tag = "     "

                prot_str = _format_protection(entry["protection"])
                if is_dir:
                    print("{tag}  {name:<{nw}}  {prot:>{pw}}  "
                          "{blank:>{sw}}  {date}".format(
                              tag=tag,
                              name=name, nw=max_name,
                              prot=prot_str, pw=max_prot,
                              blank="", sw=max_size,
                              date=date))
                else:
                    size_str = format_size(entry["size"])
                    print("{tag}  {name:<{nw}}  {prot:>{pw}}  "
                          "{size:>{sw}}  {date}".format(
                              tag=tag,
                              name=name, nw=max_name,
                              prot=prot_str, pw=max_prot,
                              size=size_str, sw=max_size,
                              date=date))
        else:
            # Multi-column names-only format
            display_names = []
            for entry in entries:
                is_dir = entry["type"].lower() == "dir"
                name = entry["name"]
                if is_dir:
                    display_names.append(
                        self.cw.directory(name + "/"))
                else:
                    display_names.append(name)

            # Calculate column layout
            term_width = shutil.get_terminal_size((80, 24)).columns
            max_vis = max(
                (_visible_len(n) for n in display_names), default=0)
            col_width = max_vis + 2  # 2-char gutter
            if col_width <= 0:
                col_width = 1
            num_cols = max(1, term_width // col_width)
            num_rows = (len(display_names) + num_cols - 1) // num_cols

            for row in range(num_rows):
                parts = []
                for col in range(num_cols):
                    idx = row + col * num_rows
                    if idx < len(display_names):
                        name = display_names[idx]
                        vis = _visible_len(name)
                        if col < num_cols - 1:
                            padding = col_width - vis
                            parts.append(name + " " * padding)
                        else:
                            parts.append(name)
                print("".join(parts))

    def do_dir(self, arg):
        """List directory contents (alias for ls). See: help ls"""
        self.do_ls(arg)

    complete_dir = _complete_path

    def do_stat(self, arg):
        """Show file or directory metadata.

    Usage: stat PATH

    Displays type, name, size, protection bits, datestamp, and
    comment for the given path.

    Examples:
        stat SYS:S/Startup-Sequence
        stat RAM:"""
        path = arg.strip()
        if not path:
            print("Usage: stat PATH")
            return
        if not self._check_connected():
            return
        path = self._resolve_path(path)
        if path is None:
            return

        info = self._run(self.conn.stat, path)
        if info is None:
            return

        for key in ("type", "name", "size", "protection",
                     "datestamp", "comment"):
            if key in info:
                value = info[key]
                if key == "protection":
                    value = _format_protection(str(value))
                print("{}={}".format(self.cw.key(key), value))

    def do_cat(self, arg):
        """Print a remote file's contents to the terminal.

    Usage: cat [--offset N] [--length N] PATH

    --offset N  Start reading at byte offset N.
    --length N  Read at most N bytes.

    Outputs the raw bytes of the file to stdout. Use 'get' to save
    to a local file instead.

    Examples:
        cat SYS:S/Startup-Sequence
        cat --offset 100 --length 50 RAM:test.txt
        cat User-Startup"""
        if not self._check_connected():
            return
        try:
            parts = shlex.split(arg) if arg.strip() else []
        except ValueError as e:
            print("Parse error: {}".format(e))
            return
        if not parts:
            print("Usage: cat [--offset N] [--length N] PATH")
            return

        offset = None
        length = None
        path_parts = []
        i = 0
        while i < len(parts):
            if parts[i] == "--offset" and i + 1 < len(parts):
                try:
                    offset = int(parts[i + 1])
                except ValueError:
                    print("Invalid offset: {}".format(parts[i + 1]))
                    return
                i += 2
            elif parts[i] == "--length" and i + 1 < len(parts):
                try:
                    length = int(parts[i + 1])
                except ValueError:
                    print("Invalid length: {}".format(parts[i + 1]))
                    return
                i += 2
            else:
                path_parts.append(parts[i])
                i += 1

        if not path_parts:
            print("Usage: cat [--offset N] [--length N] PATH")
            return

        path = self._resolve_path(path_parts[0])
        if path is None:
            return

        data = self._run(self.conn.read, path, offset=offset, length=length)
        if data is None:
            return

        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()

    # -- File transfer -----------------------------------------------------

    def do_get(self, arg):
        """Download a file from the Amiga.

    Usage: get REMOTE [LOCAL]

    REMOTE  Amiga file path (absolute, or relative to current directory).
    LOCAL   Destination on this computer (default: current directory,
            same filename as the remote file).

    Examples:
        get SYS:C/WhichAmiga
        get Startup-Sequence /tmp/startup.txt"""
        if not self._check_connected():
            return
        try:
            parts = shlex.split(arg)
        except ValueError as e:
            print("Parse error: {}".format(e))
            return
        if len(parts) == 1:
            remote = self._resolve_path(parts[0])
            if remote is None:
                return
            local = _amiga_basename(parts[0])
        elif len(parts) == 2:
            remote = self._resolve_path(parts[0])
            if remote is None:
                return
            local = parts[1]
        else:
            print("Usage: get REMOTE [LOCAL]")
            return

        data = self._run(self.conn.read, remote)
        if data is None:
            return

        try:
            with open(local, "wb") as f:
                f.write(data)
        except IOError as e:
            print(self.cw.error("Local write error: {}".format(e)))
            return

        print(self.cw.success(
            "Downloaded {} bytes to {}".format(len(data), local)))

    def do_put(self, arg):
        """Upload a file to the Amiga.

    Usage: put LOCAL [REMOTE]

    LOCAL   File on this computer to upload.
    REMOTE  Destination Amiga path (default: current Amiga directory,
            same filename as the local file).

    Examples:
        put startup.txt SYS:S/Startup-Sequence
        put localfile.txt"""
        if not self._check_connected():
            return
        try:
            parts = shlex.split(arg)
        except ValueError as e:
            print("Parse error: {}".format(e))
            return
        if len(parts) == 1:
            local = parts[0]
            remote = self._resolve_path(os.path.basename(local))
            if remote is None:
                return
        elif len(parts) == 2:
            local = parts[0]
            remote = self._resolve_path(parts[1])
            if remote is None:
                return
        else:
            print("Usage: put LOCAL [REMOTE]")
            return

        try:
            with open(local, "rb") as f:
                data = f.read()
        except IOError as e:
            print(self.cw.error("Local read error: {}".format(e)))
            return

        written = self._run(self.conn.write, remote, data)
        if written is None:
            return

        self._dir_cache.invalidate()
        print(self.cw.success(
            "Uploaded {} bytes to {}".format(written, remote)))

    def do_append(self, arg):
        """Append a local file to a remote file.

    Usage: append LOCAL REMOTE

    LOCAL   File on this computer to append.
    REMOTE  Amiga file to append to (must already exist).

    Examples:
        append extra.txt RAM:logfile.txt"""
        if not self._check_connected():
            return
        try:
            parts = shlex.split(arg)
        except ValueError as e:
            print("Parse error: {}".format(e))
            return
        if len(parts) != 2:
            print("Usage: append LOCAL REMOTE")
            return

        local = parts[0]
        remote = self._resolve_path(parts[1])
        if remote is None:
            return

        try:
            with open(local, "rb") as f:
                data = f.read()
        except IOError as e:
            print(self.cw.error("Local read error: {}".format(e)))
            return

        written = self._run(self.conn.append, remote, data)
        if written is None:
            return

        print(self.cw.success(
            "Appended {} bytes to {}".format(written, remote)))

    def complete_append(self, text, line, begidx, endidx):
        """Complete append: first arg is local, second is Amiga path."""
        prefix = line[:begidx]
        args = prefix.split()
        if len(args) >= 2:
            return self._complete_path(text, line, begidx, endidx)
        return []

    def do_edit(self, arg):
        """Edit a remote file in a local editor.

    Usage: edit PATH

    Downloads the file, opens it in $VISUAL, $EDITOR, or the editor
    configured in the config file (default: vi on Linux/macOS, notepad
    on Windows), and uploads changes on save.
    Detects remote modifications made while editing and prompts
    before overwriting.

    Examples:
        edit SYS:S/User-Startup
        edit Startup-Sequence"""
        import subprocess
        import tempfile

        path = arg.strip()
        if not path:
            print("Usage: edit PATH")
            return
        if not self._check_connected():
            return
        path = self._resolve_path(path)
        if path is None:
            return

        # 1. Get remote datestamp
        original_datestamp = None
        try:
            info = self.conn.stat(path)
            original_datestamp = info.get("datestamp", "")
        except NotFoundError:
            pass  # New file
        except (AmigactlError, ProtocolError, OSError) as e:
            print(self.cw.error("Error checking file: {}".format(e)))
            return

        # 2. Download
        original_data = b""
        try:
            original_data = self.conn.read(path)
        except NotFoundError:
            print("File does not exist. Creating new file.")
        except AmigactlError as e:
            print(self.cw.error("Error reading file: {}".format(e)))
            return
        except ProtocolError as e:
            print(self.cw.error("Error reading file: {}".format(e)))
            return
        except OSError as e:
            print(self.cw.error("Error reading file: {}".format(e)))
            self.conn = None
            self._update_prompt()
            return

        # 3. Write to temp file
        filename = _amiga_basename(path)
        tmpdir = tempfile.mkdtemp(prefix="amigactl_edit_")
        tmpfile = os.path.join(tmpdir, filename)
        try:
            with open(tmpfile, "wb") as f:
                f.write(original_data)

            saved_mtime = os.path.getmtime(tmpfile)

            # 5. Launch editor
            if sys.platform == "win32":
                default_editor = "notepad"
            else:
                default_editor = "vi"
            editor = (os.environ.get("VISUAL")
                      or os.environ.get("EDITOR")
                      or self._editor
                      or default_editor)
            editor_cmd = shlex.split(editor) + [tmpfile]
            subprocess.call(editor_cmd)

            # 6. Check for local modifications
            if os.path.getmtime(tmpfile) == saved_mtime:
                print("No changes detected.")
                try:
                    os.remove(tmpfile)
                    os.rmdir(tmpdir)
                except OSError:
                    pass
                return

            with open(tmpfile, "rb") as f:
                new_data = f.read()

            old_size = len(original_data)
            new_size = len(new_data)
            print("File changed: {} -> {} bytes".format(old_size, new_size))

            # 9. Check remote conflict
            if not self._check_connected():
                print("Local copy preserved at: {}".format(tmpfile))
                return

            try:
                current_info = self.conn.stat(path)
                current_datestamp = current_info.get("datestamp", "")
                if original_datestamp is not None:
                    # Existing file -- check for modification
                    if current_datestamp != original_datestamp:
                        print(self.cw.error(
                            "Warning: file was modified remotely "
                            "while editing."))
                        print("  Remote datestamp was: {}".format(
                            original_datestamp))
                        print("  Remote datestamp now: {}".format(
                            current_datestamp))
                        try:
                            answer = input(
                                "Upload anyway? [y/N] ").strip().lower()
                        except EOFError:
                            answer = "n"
                        if answer != "y":
                            print("Upload cancelled.")
                            print("Local copy saved at: {}".format(tmpfile))
                            return  # Don't clean up tmpdir
                else:
                    # New file -- but it now exists (created by another
                    # process)
                    print(self.cw.error(
                        "Warning: file was created remotely "
                        "while editing."))
                    print("  Remote datestamp: {}".format(
                        current_datestamp))
                    try:
                        answer = input(
                            "Overwrite? [y/N] ").strip().lower()
                    except EOFError:
                        answer = "n"
                    if answer != "y":
                        print("Upload cancelled.")
                        print("Local copy saved at: {}".format(tmpfile))
                        return  # Don't clean up tmpdir
            except NotFoundError:
                pass  # File still doesn't exist (new file) -- safe to upload
            except (AmigactlError, ProtocolError):
                pass  # Can't check -- proceed with upload
            except OSError as e:
                print(self.cw.error("Connection lost: {}".format(e)))
                self.conn = None
                self._update_prompt()
                print("Local copy preserved at: {}".format(tmpfile))
                return

            # 10. Upload
            try:
                written = self.conn.write(path, new_data)
                print(self.cw.success(
                    "Uploaded {} bytes to {}".format(written, path)))
                self._dir_cache.invalidate()
            except AmigactlError as e:
                print(self.cw.error(
                    "Upload failed: {}".format(e.message)))
                print("Local copy preserved at: {}".format(tmpfile))
                return
            except (ProtocolError, OSError) as e:
                print(self.cw.error("Upload failed: {}".format(e)))
                self.conn = None
                self._update_prompt()
                print("Local copy preserved at: {}".format(tmpfile))
                return

        except Exception as e:
            print(self.cw.error("Error: {}".format(e)))
            if os.path.exists(tmpfile):
                print("Local copy preserved at: {}".format(tmpfile))
            return  # Don't clean up on error

        # 12. Clean up
        try:
            os.remove(tmpfile)
            os.rmdir(tmpdir)
        except OSError:
            pass

    # -- File manipulation -------------------------------------------------

    def do_rm(self, arg):
        """Delete a file or empty directory on the Amiga.

    Usage: rm PATH

    The directory must be empty to be deleted. There is no recursive
    delete or confirmation prompt.

    Examples:
        rm RAM:test.txt
        rm Work:emptydir"""
        path = arg.strip()
        if not path:
            print("Usage: rm PATH")
            return
        if not self._check_connected():
            return
        path = self._resolve_path(path)
        if path is None:
            return

        result = self._run(self.conn.delete, path)
        if result is not None:
            self._dir_cache.invalidate()
            print(self.cw.success("Deleted: {}".format(path)))

    def do_mv(self, arg):
        """Rename or move a file or directory on the Amiga.

    Usage: mv OLD NEW

    Both paths must be on the same volume (AmigaOS limitation).

    Examples:
        mv RAM:test.txt RAM:renamed.txt
        mv oldname newname"""
        if not self._check_connected():
            return
        try:
            parts = shlex.split(arg)
        except ValueError as e:
            print("Parse error: {}".format(e))
            return
        if len(parts) != 2:
            print("Usage: mv OLD NEW")
            return

        old = self._resolve_path(parts[0])
        if old is None:
            return
        new = self._resolve_path(parts[1])
        if new is None:
            return

        result = self._run(self.conn.rename, old, new)
        if result is not None:
            self._dir_cache.invalidate()
            print(self.cw.success("Renamed: {} -> {}".format(old, new)))

    def do_mkdir(self, arg):
        """Create a directory on the Amiga.

    Usage: mkdir PATH

    Parent directories must already exist.

    Examples:
        mkdir RAM:newdir
        mkdir subdir"""
        path = arg.strip()
        if not path:
            print("Usage: mkdir PATH")
            return
        if not self._check_connected():
            return
        path = self._resolve_path(path)
        if path is None:
            return

        result = self._run(self.conn.makedir, path)
        if result is not None:
            self._dir_cache.invalidate()
            print(self.cw.success("Created: {}".format(path)))

    def do_cp(self, arg):
        """Copy a file on the Amiga.

    Usage: cp [-P] [-n] SOURCE DEST

    SOURCE  File to copy.
    DEST    Destination path.
    -P      Do not clone metadata (protection, date, comment).
    -n      Do not replace if destination exists.

    Examples:
        cp RAM:file.txt RAM:backup.txt
        cp -n SYS:S/Startup-Sequence RAM:"""
        if not self._check_connected():
            return
        try:
            parts = shlex.split(arg) if arg.strip() else []
        except ValueError as e:
            print("Parse error: {}".format(e))
            return
        noclone = False
        noreplace = False
        path_parts = []
        for part in parts:
            if part.startswith("-") and len(part) > 1:
                for ch in part[1:]:
                    if ch == "P":
                        noclone = True
                    elif ch == "n":
                        noreplace = True
                    else:
                        print("Unknown flag: -{}".format(ch))
                        return
            else:
                path_parts.append(part)

        if len(path_parts) != 2:
            print("Usage: cp [-P] [-n] SOURCE DEST")
            return

        src = self._resolve_path(path_parts[0])
        if src is None:
            return
        dst = self._resolve_path(path_parts[1])
        if dst is None:
            return

        result = self._run(self.conn.copy, src, dst,
                           noclone=noclone, noreplace=noreplace)
        if result is not None:
            self._dir_cache.invalidate()
            print(self.cw.success("Copied: {} -> {}".format(src, dst)))

    do_copy = do_cp

    complete_cp = _complete_path
    complete_copy = _complete_path

    def do_chmod(self, arg):
        """Get or set AmigaOS protection bits.

    Usage: chmod PATH [BITS]

    PATH    File or directory path.
    BITS    Raw protection value in hexadecimal. Omit to display
            the current protection bits.

    Protection bits are displayed as hsparwed (hold, script, pure,
    archive, read, write, execute, delete). Owner RWED bits are
    inverted: a set bit means access is denied.

    Examples:
        chmod SYS:C/Dir
        chmod RAM:test.txt 0f"""
        if not self._check_connected():
            return
        try:
            parts = shlex.split(arg)
        except ValueError as e:
            print("Parse error: {}".format(e))
            return
        if len(parts) < 1 or len(parts) > 2:
            print("Usage: chmod PATH [HEX]")
            return

        path = self._resolve_path(parts[0])
        if path is None:
            return
        value = parts[1] if len(parts) == 2 else None

        result = self._run(self.conn.protect, path, value)
        if result is not None:
            self._dir_cache.invalidate()
            display = _format_protection(result)
            print("{}={}".format(
                self.cw.key("protection"), display))

    def do_touch(self, arg):
        """Set the datestamp on a file or directory, creating the file
    if it does not exist.

    Usage: touch PATH [DATE TIME]

    PATH    Amiga file or directory path.
    DATE    Date in YYYY-MM-DD format.
    TIME    Time in HH:MM:SS format.

    If DATE and TIME are omitted, the current time is used.

    Examples:
        touch RAM:test.txt
        touch RAM:test.txt 2026-02-19 12:00:00"""
        if not self._check_connected():
            return
        try:
            parts = shlex.split(arg)
        except ValueError as e:
            print("Parse error: {}".format(e))
            return
        if len(parts) == 1:
            path = self._resolve_path(parts[0])
            if path is None:
                return
            datestamp = None
        elif len(parts) == 3:
            path = self._resolve_path(parts[0])
            if path is None:
                return
            datestamp = "{} {}".format(parts[1], parts[2])
        else:
            print("Usage: touch PATH [DATE TIME]")
            return

        try:
            result = self.conn.setdate(path, datestamp)
        except NotFoundError:
            # File doesn't exist -- create it (Unix touch semantics)
            try:
                self.conn.write(path, b"")
            except AmigactlError as e:
                print(self.cw.error("Error: {}".format(e.message)))
                return
            except ProtocolError as e:
                print(self.cw.error("Protocol error: {}".format(e)))
                return
            except OSError as e:
                print(self.cw.error("Connection error: {}".format(e)))
                self.conn = None
                self._update_prompt()
                return
            if datestamp is not None:
                # User specified a datestamp -- apply it to the new file
                try:
                    result = self.conn.setdate(path, datestamp)
                except AmigactlError as e:
                    print(self.cw.error("Error: {}".format(e.message)))
                    return
                except ProtocolError as e:
                    print(self.cw.error("Protocol error: {}".format(e)))
                    return
                except OSError as e:
                    print(self.cw.error("Connection error: {}".format(e)))
                    self.conn = None
                    self._update_prompt()
                    return
            else:
                result = None
        except AmigactlError as e:
            print(self.cw.error("Error: {}".format(e.message)))
            return
        except ProtocolError as e:
            print(self.cw.error("Protocol error: {}".format(e)))
            return
        except OSError as e:
            print(self.cw.error("Connection error: {}".format(e)))
            self.conn = None
            self._update_prompt()
            return

        self._dir_cache.invalidate()
        if result is not None:
            print("{}={}".format(
                self.cw.key("datestamp"), result))
        else:
            print("Created {}".format(path))

    def do_checksum(self, arg):
        """Compute CRC32 checksum of a remote file.

    Usage: checksum PATH

    Displays the CRC32 hash and file size.

    Examples:
        checksum SYS:C/Dir
        checksum RAM:test.txt"""
        path = arg.strip()
        if not path:
            print("Usage: checksum PATH")
            return
        if not self._check_connected():
            return
        path = self._resolve_path(path)
        if path is None:
            return

        result = self._run(self.conn.checksum, path)
        if result is None:
            return

        print("{}={}".format(
            self.cw.key("crc32"), result.get("crc32", "")))
        print("{}={}".format(
            self.cw.key("size"), result.get("size", "")))

    complete_checksum = _complete_path

    def do_setcomment(self, arg):
        """Set the file comment on a remote file.

    Usage: setcomment PATH COMMENT
           setcomment PATH ""

    An empty comment clears the existing comment.

    Examples:
        setcomment RAM:test.txt "Important file"
        setcomment RAM:test.txt ""
        setcomment Startup-Sequence "Modified 2026-02-24\""""
        if not self._check_connected():
            return
        try:
            parts = shlex.split(arg)
        except ValueError as e:
            print("Parse error: {}".format(e))
            return
        if len(parts) < 2:
            print("Usage: setcomment PATH COMMENT")
            return

        path = self._resolve_path(parts[0])
        if path is None:
            return
        comment = " ".join(parts[1:])

        result = self._run(self.conn.setcomment, path, comment)
        if result is not None:
            print(self.cw.success("Comment set on {}".format(path)))

    do_comment = do_setcomment

    complete_setcomment = _complete_path
    complete_comment = _complete_path

    # -- Execution and process management ----------------------------------

    def do_exec(self, arg):
        """Execute an AmigaOS CLI command and wait for it to finish.

    Usage: exec COMMAND...

    Runs the command synchronously. Output is displayed as it is
    captured, followed by the return code. The shell blocks until
    the command completes.

    Use 'run' for asynchronous execution.

    Examples:
        exec list SYS:S
        exec echo hello world
        exec avail FLUSH"""
        command = arg.strip()
        if not command:
            print("Usage: exec CMD...")
            return
        if not self._check_connected():
            return

        result = self._run(self.conn.execute, self._prepend_cd(command))
        if result is None:
            return

        rc, output = result
        if output:
            sys.stdout.write(output)
        print("Return code: {}".format(rc))

    complete_exec = _complete_path
    complete_run = _complete_path

    def do_run(self, arg):
        """Launch an AmigaOS CLI command in the background.

    Usage: run COMMAND...

    Starts the command asynchronously and returns its process ID
    immediately. Use 'ps' to list running processes, 'status ID'
    to check on one, and 'signal ID' or 'kill ID' to stop it.

    Examples:
        run wait 30
        run execute SYS:S/MyScript"""
        command = arg.strip()
        if not command:
            print("Usage: run CMD...")
            return
        if not self._check_connected():
            return

        proc_id = self._run(self.conn.execute_async, self._prepend_cd(command))
        if proc_id is None:
            return

        print("Process ID: {}".format(proc_id))

    def do_ps(self, arg):
        """List processes launched by the daemon.

    Usage: ps

    Shows the ID, command, status (running/finished), and return
    code of each tracked process. Only processes started via 'run'
    are tracked."""
        if not self._check_connected():
            return

        procs = self._run(self.conn.proclist)
        if procs is None:
            return

        if not procs:
            print("No tracked processes.")
            return

        # Calculate column widths
        max_id = len("ID")
        max_cmd = len("COMMAND")
        max_status = len("STATUS")
        max_rc = len("RC")
        for p in procs:
            id_len = len(str(p["id"]))
            if id_len > max_id:
                max_id = id_len
            cmd_len = len(p["command"])
            if cmd_len > max_cmd:
                max_cmd = cmd_len
            status_len = len(p["status"])
            if status_len > max_status:
                max_status = status_len
            rc_str = str(p["rc"]) if p["rc"] is not None else "-"
            rc_len = len(rc_str)
            if rc_len > max_rc:
                max_rc = rc_len

        header = "{:<{}}  {:<{}}  {:<{}}  {:<{}}".format(
            "ID", max_id, "COMMAND", max_cmd,
            "STATUS", max_status, "RC", max_rc)
        print(self.cw.bold(header))
        for p in procs:
            rc_str = str(p["rc"]) if p["rc"] is not None else "-"
            print("{:<{}}  {:<{}}  {:<{}}  {:<{}}".format(
                p["id"], max_id, p["command"], max_cmd,
                p["status"], max_status, rc_str, max_rc))

    def do_status(self, arg):
        """Show the status of a tracked background process.

    Usage: status ID

    ID      Process ID returned by 'run'.

    Displays the process ID, command, status (running/finished),
    and return code.

    Examples:
        status 1"""
        arg = arg.strip()
        if not arg:
            print("Usage: status ID")
            return
        if not self._check_connected():
            return

        try:
            proc_id = int(arg)
        except ValueError:
            print("Error: ID must be an integer")
            return

        info = self._run(self.conn.procstat, proc_id)
        if info is None:
            return

        for key in ("id", "command", "status", "rc"):
            if key in info:
                val = info[key]
                if val is None:
                    val = "-"
                print("{}={}".format(self.cw.key(key), val))

    def do_signal(self, arg):
        """Send a break signal to a background process.

    Usage: signal ID [SIG]

    ID      Process ID returned by 'run'.
    SIG     Signal name (default: CTRL_C). Also: CTRL_D, CTRL_E,
            CTRL_F.

    Examples:
        signal 1
        signal 1 CTRL_D"""
        if not self._check_connected():
            return
        try:
            parts = shlex.split(arg)
        except ValueError as e:
            print("Parse error: {}".format(e))
            return
        if len(parts) < 1 or len(parts) > 2:
            print("Usage: signal ID [SIG]")
            return

        try:
            proc_id = int(parts[0])
        except ValueError:
            print("Error: ID must be an integer")
            return
        sig = parts[1] if len(parts) == 2 else "CTRL_C"

        result = self._run(self.conn.signal, proc_id, sig)
        if result is not None:
            print(self.cw.success("Signal sent."))

    def do_kill(self, arg):
        """Force-terminate a background process.

    Usage: kill ID

    ID      Process ID returned by 'run'.

    Forcibly removes the process from the daemon's tracking. Use
    'signal' first for a graceful shutdown.

    Examples:
        kill 1"""
        arg = arg.strip()
        if not arg:
            print("Usage: kill ID")
            return
        if not self._check_connected():
            return

        try:
            proc_id = int(arg)
        except ValueError:
            print("Error: ID must be an integer")
            return

        result = self._run(self.conn.kill, proc_id)
        if result is not None:
            print(self.cw.success("Process terminated."))

    # -- System information ------------------------------------------------

    def do_version(self, arg):
        """Print the amigactld daemon version string.

    Usage: version"""
        if not self._check_connected():
            return

        ver = self._run(self.conn.version)
        if ver is not None:
            print(ver)

    def do_ping(self, arg):
        """Check that the daemon is responding.

    Usage: ping

    Prints OK if the daemon responds."""
        if not self._check_connected():
            return

        result = self._run(self.conn.ping)
        if result is not None:
            print(self.cw.success("OK"))

    def do_uptime(self, arg):
        """Show how long the daemon has been running.

    Usage: uptime

    Output format: Xd Xh Xm Xs."""
        if not self._check_connected():
            return

        seconds = self._run(self.conn.uptime)
        if seconds is None:
            return

        days = seconds // 86400
        seconds %= 86400
        hours = seconds // 3600
        seconds %= 3600
        minutes = seconds // 60
        secs = seconds % 60
        parts = []
        if days:
            parts.append("{}d".format(days))
        if hours:
            parts.append("{}h".format(hours))
        if minutes:
            parts.append("{}m".format(minutes))
        # Always show seconds (even 0) if nothing else, or if non-zero
        if secs or not parts:
            parts.append("{}s".format(secs))
        print(" ".join(parts))

    def do_sysinfo(self, arg):
        """Show Amiga system information.

    Usage: sysinfo

    Displays CPU type, chip/fast RAM, Kickstart version, and other
    system details."""
        if not self._check_connected():
            return

        info = self._run(self.conn.sysinfo)
        if info is None:
            return

        for key, value in info.items():
            print("{}={}".format(self.cw.key(key), value))

    def do_libver(self, arg):
        """Get the version of an Amiga library or device.

    Usage: libver NAME

    NAME    Library or device name (e.g. exec.library,
            timer.device).

    Examples:
        libver exec.library
        libver dos.library
        libver timer.device"""
        name = arg.strip()
        if not name:
            print("Usage: libver NAME")
            return
        if not self._check_connected():
            return

        result = self._run(self.conn.libver, name)
        if result is None:
            return

        print("{}={}".format(
            self.cw.key("name"), result.get("name", "")))
        print("{}={}".format(
            self.cw.key("version"), result.get("version", "")))

    def do_env(self, arg):
        """Get an AmigaOS environment variable.

    Usage: env NAME

    Prints the value of the named global environment variable.

    Examples:
        env Workbench
        env Language"""
        name = arg.strip()
        if not name:
            print("Usage: env NAME")
            return
        if not self._check_connected():
            return

        result = self._run(self.conn.env, name)
        if result is None:
            return

        print(result)

    do_getenv = do_env

    def do_setenv(self, arg):
        """Set or delete an AmigaOS environment variable.

    Usage: setenv NAME VALUE
           setenv -v NAME VALUE
           setenv NAME

    NAME    Variable name.
    VALUE   Value to set. Omit to delete the variable.
    -v      Volatile only (ENV: only, not persisted to ENVARC:).

    Examples:
        setenv MyVar hello
        setenv -v TempVar 42
        setenv MyVar"""
        if not self._check_connected():
            return
        try:
            parts = shlex.split(arg) if arg.strip() else []
        except ValueError as e:
            print("Parse error: {}".format(e))
            return
        if not parts:
            print("Usage: setenv [-v] NAME [VALUE]")
            return

        volatile = False
        idx = 0

        if parts[0] == "-v":
            volatile = True
            idx = 1

        if idx >= len(parts):
            print("Usage: setenv [-v] NAME [VALUE]")
            return

        name = parts[idx]
        idx += 1

        value = None
        if idx < len(parts):
            value = " ".join(parts[idx:])

        result = self._run(self.conn.setenv, name, value=value,
                           volatile=volatile)
        if result is not None:
            if value is not None:
                print(self.cw.success("Set: {}={}".format(name, value)))
            else:
                print(self.cw.success("Deleted: {}".format(name)))

    def do_assigns(self, arg):
        """List all logical assigns on the Amiga.

    Usage: assigns

    Shows each assign name and its target path. Use 'assign' to
    create, modify, or remove individual assigns."""
        if not self._check_connected():
            return

        assigns = self._run(self.conn.assigns)
        if assigns is None:
            return

        if not assigns:
            print("No assigns.")
            return

        # Calculate column widths
        max_name = 0
        for name in assigns:
            if len(name) > max_name:
                max_name = len(name)

        for name, path in assigns.items():
            print("{:<{}}  {}".format(name, max_name, path))

    def do_assign(self, arg):
        """Create, modify, or remove a logical assign.

    Usage: assign NAME: [PATH]
           assign late NAME: PATH
           assign add NAME: PATH

    Create:     assign TEST: RAM:         (lock-based, immediate)
    Late-bind:  assign late TEST: RAM:    (resolved on first access)
    Add path:   assign add TEST: RAM:T    (multi-directory assign)
    Remove:     assign TEST:
    List all:   use 'assigns' command"""
        if not self._check_connected():
            return
        try:
            parts = shlex.split(arg)
        except ValueError as e:
            print("Parse error: {}".format(e))
            return
        if not parts:
            print("Usage: assign NAME: [PATH]\n"
                  "       assign late NAME: PATH\n"
                  "       assign add NAME: PATH")
            return

        mode = None
        idx = 0

        # Check for mode keyword (case-insensitive)
        if parts[0].lower() in ("late", "add"):
            mode = parts[0].lower()
            idx = 1

        if idx >= len(parts):
            print("Usage: assign [late|add] NAME: [PATH]")
            return

        name = parts[idx]
        idx += 1

        path = None
        if idx < len(parts):
            path = parts[idx]

        result = self._run(self.conn.assign, name, path, mode)
        if result is not None:
            if path is not None:
                print(self.cw.success("Assigned: {} -> {}".format(
                    name, path)))
            else:
                print(self.cw.success("Removed: {}".format(name)))

    complete_assign = _complete_path

    def do_ports(self, arg):
        """List active Exec message ports on the Amiga.

    Usage: ports

    Shows the names of all public message ports (e.g., REXX,
    amigactld). Useful for finding ARexx port names."""
        if not self._check_connected():
            return

        ports = self._run(self.conn.ports)
        if ports is None:
            return

        if not ports:
            print("No ports.")
            return

        for port in ports:
            print(port)

    def do_volumes(self, arg):
        """List mounted volumes on the Amiga.

    Usage: volumes

    Shows each volume's name, used space, free space, and total
    capacity."""
        if not self._check_connected():
            return

        vols = self._run(self.conn.volumes)
        if vols is None:
            return

        if not vols:
            print("No volumes.")
            return

        # Calculate column widths for name
        max_name = len("NAME")
        for v in vols:
            if len(v["name"]) > max_name:
                max_name = len(v["name"])

        # Format sizes
        rows = []
        max_used = len("USED")
        max_free = len("FREE")
        max_cap = len("CAPACITY")
        for v in vols:
            used = format_size(v["used"])
            free = format_size(v["free"])
            cap = format_size(v["capacity"])
            if len(used) > max_used:
                max_used = len(used)
            if len(free) > max_free:
                max_free = len(free)
            if len(cap) > max_cap:
                max_cap = len(cap)
            rows.append((v["name"], used, free, cap))

        header = "{:<{}}  {:>{}}  {:>{}}  {:>{}}".format(
            "NAME", max_name,
            "USED", max_used,
            "FREE", max_free,
            "CAPACITY", max_cap)
        print(self.cw.bold(header))
        for name, used, free, cap in rows:
            print("{:<{}}  {:>{}}  {:>{}}  {:>{}}".format(
                name, max_name,
                used, max_used,
                free, max_free,
                cap, max_cap))

    def do_tasks(self, arg):
        """List all running tasks and processes on the Amiga.

    Usage: tasks

    Shows name, type (task/process), priority, state, and stack
    size for every task in the system."""
        if not self._check_connected():
            return

        tasks = self._run(self.conn.tasks)
        if tasks is None:
            return

        if not tasks:
            print("No tasks.")
            return

        # Calculate column widths
        max_name = len("NAME")
        max_type = len("TYPE")
        max_pri = len("PRI")
        max_state = len("STATE")
        max_stack = len("STACK")
        for t in tasks:
            if len(t["name"]) > max_name:
                max_name = len(t["name"])
            if len(t["type"]) > max_type:
                max_type = len(t["type"])
            pri_str = str(t["priority"])
            if len(pri_str) > max_pri:
                max_pri = len(pri_str)
            if len(t["state"]) > max_state:
                max_state = len(t["state"])
            stack_str = str(t["stacksize"])
            if len(stack_str) > max_stack:
                max_stack = len(stack_str)

        header = "{:<{}}  {:<{}}  {:>{}}  {:<{}}  {:>{}}".format(
            "NAME", max_name,
            "TYPE", max_type,
            "PRI", max_pri,
            "STATE", max_state,
            "STACK", max_stack)
        print(self.cw.bold(header))
        for t in tasks:
            print("{:<{}}  {:<{}}  {:>{}}  {:<{}}  {:>{}}".format(
                t["name"], max_name,
                t["type"], max_type,
                t["priority"], max_pri,
                t["state"], max_state,
                t["stacksize"], max_stack))

    def do_devices(self, arg):
        """List Exec devices on the Amiga.

    Usage: devices

    Shows the name and version of each device driver loaded
    in the system."""
        if not self._check_connected():
            return

        devs = self._run(self.conn.devices)
        if devs is None:
            return

        if not devs:
            print("No devices.")
            return

        # Calculate column widths
        max_name = len("NAME")
        max_ver = len("VERSION")
        for d in devs:
            if len(d["name"]) > max_name:
                max_name = len(d["name"])
            if len(d["version"]) > max_ver:
                max_ver = len(d["version"])

        header = "{:<{}}  {:>{}}".format(
            "NAME", max_name, "VERSION", max_ver)
        print(self.cw.bold(header))
        for d in devs:
            print("{:<{}}  {:>{}}".format(
                d["name"], max_name,
                d["version"], max_ver))

    def do_capabilities(self, arg):
        """Show daemon capabilities and supported commands.

    Usage: capabilities

    Displays daemon version, protocol version, limits, and the
    full list of supported commands."""
        if not self._check_connected():
            return

        caps = self._run(self.conn.capabilities)
        if caps is None:
            return

        for key, value in caps.items():
            if key == "commands":
                # Format command list in columns
                print("{}=".format(self.cw.key(key)))
                commands = value.split(",")
                for cmd in commands:
                    print("  {}".format(cmd))
            else:
                print("{}={}".format(self.cw.key(key), value))

    do_caps = do_capabilities

    # -- ARexx -------------------------------------------------------------

    def do_arexx(self, arg):
        """Send an ARexx command to a named message port.

    Usage: arexx PORT COMMAND...

    PORT        Name of the ARexx port (use 'ports' to list them).
    COMMAND     ARexx command string to send.

    The return code and any result string are displayed.

    Examples:
        arexx REXX return 1+2
        arexx REXX "say 'hello'"
        arexx CNet_AREXX WHO"""
        if not self._check_connected():
            return
        arg = arg.strip()
        if not arg:
            print("Usage: arexx PORT CMD...")
            return

        # First word is port name, rest is command
        parts = arg.split(None, 1)
        port = parts[0]
        if len(parts) < 2 or not parts[1].strip():
            print("Usage: arexx PORT CMD...")
            return
        command = parts[1]

        result = self._run(self.conn.arexx, port, command)
        if result is None:
            return

        rc, output = result
        if output:
            print(output)
        print("Return code: {}".format(rc))

    # -- Search and analysis -----------------------------------------------

    def do_find(self, arg):
        """Search for files and directories by name pattern.

    Usage: find PATH PATTERN
           find PATH -name PATTERN
           find PATH -type f PATTERN
           find PATH -type d PATTERN

    PATH      Directory to search recursively.
    PATTERN   Glob pattern (e.g., *.info, S*). Case-insensitive.
    -name     Explicit pattern flag (optional).
    -type f   Files only.
    -type d   Directories only.

    Examples:
        find SYS: *.info
        find Work: -type f *.c
        find RAM: -name test*"""
        if not self._check_connected():
            return
        try:
            parts = shlex.split(arg) if arg.strip() else []
        except ValueError as e:
            print("Parse error: {}".format(e))
            return
        if not parts:
            print("Usage: find PATH PATTERN")
            return

        path = None
        pattern = None
        type_filter = None
        i = 0

        # First non-flag argument is the path
        if i < len(parts) and not parts[i].startswith("-"):
            path = parts[i]
            i += 1

        # Parse remaining arguments
        while i < len(parts):
            if parts[i] == "-name":
                i += 1
                if i < len(parts):
                    pattern = parts[i]
                    i += 1
                else:
                    print("Usage: find PATH -name PATTERN")
                    return
            elif parts[i] == "-type":
                i += 1
                if i < len(parts) and parts[i] in ("f", "d"):
                    type_filter = parts[i]
                    i += 1
                else:
                    print("Usage: find PATH -type f|d PATTERN")
                    return
            elif not parts[i].startswith("-"):
                pattern = parts[i]
                i += 1
            else:
                print("Unknown flag: {}".format(parts[i]))
                return

        if path is None or pattern is None:
            print("Usage: find PATH PATTERN")
            return

        path = self._resolve_path(path)
        if path is None:
            return

        entries = self._run(self.conn.dir, path, recursive=True)
        if entries is None:
            return

        matches = _find_filter(entries, pattern, type_filter)
        for entry in matches:
            name = entry["name"]
            if entry["type"].lower() == "dir":
                print(self.cw.directory(name))
            else:
                print(name)

    complete_find = _complete_path

    def do_tree(self, arg):
        """Display a directory tree.

    Usage: tree [PATH]
           tree -d [PATH]
           tree --ascii [PATH]

    PATH      Directory to display (default: current directory).
    -d        Show directories only.
    --ascii   Use ASCII box-drawing characters instead of Unicode.

    Examples:
        tree
        tree SYS:S
        tree -d Work:
        tree --ascii RAM:"""
        if not self._check_connected():
            return
        try:
            parts = shlex.split(arg) if arg.strip() else []
        except ValueError as e:
            print("Parse error: {}".format(e))
            return
        dirs_only = False
        ascii_mode = False
        path_parts = []

        for part in parts:
            if part == "--ascii":
                ascii_mode = True
            elif part.startswith("-") and len(part) > 1 and part != "--ascii":
                for ch in part[1:]:
                    if ch == "d":
                        dirs_only = True
                    else:
                        print("Unknown flag: -{}".format(ch))
                        return
            else:
                path_parts.append(part)

        if not path_parts:
            if self.cwd is not None:
                path = self.cwd
            else:
                print("Usage: tree [PATH]"
                      " (or set a directory with 'cd' first)")
                return
        else:
            path = self._resolve_path(path_parts[0])
            if path is None:
                return

        entries = self._run(self.conn.dir, path, recursive=True)
        if entries is None:
            return

        tree = _build_tree(entries)
        lines, dir_count, file_count = _format_tree(
            path, tree, dirs_only=dirs_only, ascii_mode=ascii_mode)

        for line in lines:
            print(line)

        print("\n{} directories, {} files".format(dir_count, file_count))

    complete_tree = _complete_path

    def do_grep(self, arg):
        """Search file contents for a pattern.

    Usage: grep PATTERN FILE
           grep -i PATTERN FILE
           grep -E PATTERN FILE
           grep -r PATTERN PATH
           grep -n PATTERN FILE
           grep -c PATTERN FILE
           grep -l PATTERN PATH

    PATTERN   Search string (literal by default).
    FILE      File to search.
    PATH      Directory to search (with -r).

    -E        Treat PATTERN as a regular expression.
    -i        Case-insensitive matching.
    -r        Recursive: search all files under PATH.
    -n        Show line numbers.
    -c        Show match count only.
    -l        Show matching filenames only (implies -r).

    Examples:
        grep hello SYS:S/Startup-Sequence
        grep -rn TODO Work:src
        grep -Ei "error|warn" RAM:logfile
        grep -rl password SYS:"""
        if not self._check_connected():
            return
        try:
            parts = shlex.split(arg) if arg.strip() else []
        except ValueError as e:
            print("Parse error: {}".format(e))
            return
        if not parts:
            print("Usage: grep PATTERN FILE")
            return

        is_regex = False
        ignore_case = False
        recursive = False
        show_lines = False
        count_only = False
        list_only = False
        positional = []

        for part in parts:
            if part.startswith("-") and len(part) > 1:
                for ch in part[1:]:
                    if ch == "E":
                        is_regex = True
                    elif ch == "i":
                        ignore_case = True
                    elif ch == "r":
                        recursive = True
                    elif ch == "n":
                        show_lines = True
                    elif ch == "c":
                        count_only = True
                    elif ch == "l":
                        list_only = True
                        recursive = True
                    else:
                        print("Unknown flag: -{}".format(ch))
                        return
            else:
                positional.append(part)

        if len(positional) < 2:
            print("Usage: grep PATTERN FILE")
            return

        pattern = positional[0]
        target = self._resolve_path(positional[1])
        if target is None:
            return

        # Validate pattern compiles
        try:
            flags = re.IGNORECASE if ignore_case else 0
            if is_regex:
                re.compile(pattern, flags)
            else:
                re.compile(re.escape(pattern), flags)
        except re.error as e:
            print(self.cw.error("Invalid pattern: {}".format(e)))
            return

        if recursive:
            entries = self._run(self.conn.dir, target, recursive=True)
            if entries is None:
                return

            files = [e for e in entries if e["type"].lower() != "dir"]
            for entry in files:
                full_path = _join_amiga_path(target, entry["name"])
                data = self._run(self.conn.read, full_path)
                if data is None:
                    continue
                text = data.decode("iso-8859-1")
                matches = _grep_lines(text, pattern,
                                      is_regex=is_regex,
                                      ignore_case=ignore_case)
                if not matches:
                    continue

                if list_only:
                    print(entry["name"])
                    continue

                if count_only:
                    print("{}:{}".format(entry["name"], len(matches)))
                    continue

                for lineno, line in matches:
                    if show_lines:
                        print("{}:{}:{}".format(
                            entry["name"], lineno, line))
                    else:
                        print("{}:{}".format(entry["name"], line))
        else:
            data = self._run(self.conn.read, target)
            if data is None:
                return
            text = data.decode("iso-8859-1")
            matches = _grep_lines(text, pattern,
                                  is_regex=is_regex,
                                  ignore_case=ignore_case)

            if count_only:
                print(len(matches))
                return

            for lineno, line in matches:
                if show_lines:
                    print("{}:{}".format(lineno, line))
                else:
                    print(line)

    complete_grep = _complete_path

    def do_diff(self, arg):
        """Compare two remote files.

    Usage: diff FILE1 FILE2

    Downloads both files and displays a unified diff. Binary files
    are detected (presence of null bytes) and reported without
    attempting a text diff.

    Output is colorized: additions in green, deletions in red,
    hunk headers in bold.

    Examples:
        diff SYS:S/Startup-Sequence SYS:S/Startup-Sequence.bak
        diff RAM:old.txt RAM:new.txt"""
        if not self._check_connected():
            return
        try:
            parts = shlex.split(arg)
        except ValueError as e:
            print("Parse error: {}".format(e))
            return
        if len(parts) != 2:
            print("Usage: diff FILE1 FILE2")
            return

        path1 = self._resolve_path(parts[0])
        if path1 is None:
            return
        path2 = self._resolve_path(parts[1])
        if path2 is None:
            return

        data1 = self._run(self.conn.read, path1)
        if data1 is None:
            return
        data2 = self._run(self.conn.read, path2)
        if data2 is None:
            return

        # Binary detection
        if b"\x00" in data1 or b"\x00" in data2:
            if data1 == data2:
                print("Binary files are identical")
            else:
                print("Binary files differ")
            return

        text1 = data1.decode("iso-8859-1")
        text2 = data2.decode("iso-8859-1")
        lines1 = text1.splitlines(True)
        lines2 = text2.splitlines(True)

        diff_lines = difflib.unified_diff(
            lines1, lines2, fromfile=path1, tofile=path2)

        has_output = False
        for line in diff_lines:
            has_output = True
            # Strip trailing newline for print
            display = line.rstrip("\n")
            if line.startswith("@@"):
                print(self.cw.bold(display))
            elif line.startswith("+"):
                print(self.cw.success(display))
            elif line.startswith("-"):
                print(self.cw.error(display))
            else:
                print(display)

        if not has_output:
            print("Files are identical")

    complete_diff = _complete_path

    def do_du(self, arg):
        """Show disk usage for a directory.

    Usage: du [PATH]
           du -s [PATH]
           du -h [PATH]

    PATH    Directory to analyze (default: current directory).
    -s      Summary only (just the total).
    -h      Human-readable sizes (K, M, G).

    Shows per-directory subtotals and a grand total.

    Examples:
        du
        du -sh Work:
        du SYS:S"""
        if not self._check_connected():
            return
        try:
            parts = shlex.split(arg) if arg.strip() else []
        except ValueError as e:
            print("Parse error: {}".format(e))
            return
        summary_only = False
        human_readable = False
        path_parts = []

        for part in parts:
            if part.startswith("-") and len(part) > 1:
                for ch in part[1:]:
                    if ch == "s":
                        summary_only = True
                    elif ch == "h":
                        human_readable = True
                    else:
                        print("Unknown flag: -{}".format(ch))
                        return
            else:
                path_parts.append(part)

        if not path_parts:
            if self.cwd is not None:
                path = self.cwd
            else:
                print("Usage: du [PATH]"
                      " (or set a directory with 'cd' first)")
                return
        else:
            path = self._resolve_path(path_parts[0])
            if path is None:
                return

        entries = self._run(self.conn.dir, path, recursive=True)
        if entries is None:
            return

        dir_sizes, total = _du_accumulate(entries)

        def fmt(size):
            if human_readable:
                return format_size(size)
            return str(size)

        if summary_only:
            print("{}\t{}".format(fmt(total), path))
        else:
            for dirname, size in dir_sizes:
                if dirname == ".":
                    display = path
                else:
                    display = dirname
                print("{}\t{}".format(fmt(size), display))

    complete_du = _complete_path

    def do_watch(self, arg):
        """Repeatedly run a command and display its output.

    Usage: watch [-n SECONDS] COMMAND

    SECONDS   Refresh interval (default: 2).
    COMMAND   Any shell command to repeat.

    Press Ctrl-C to stop.

    Examples:
        watch ps
        watch -n 5 ls -l RAM:
        watch exec avail"""
        if not self._check_connected():
            return
        raw = arg.strip()
        if not raw:
            print("Usage: watch [-n SECONDS] COMMAND")
            return

        interval = 2.0
        if raw.startswith("-n ") or raw.startswith("-n\t"):
            rest = raw[3:].lstrip()
            space = None
            for j, ch in enumerate(rest):
                if ch in (" ", "\t"):
                    space = j
                    break
            if space is None:
                print("Usage: watch [-n SECONDS] COMMAND")
                return
            try:
                interval = float(rest[:space])
            except ValueError:
                print("Invalid interval: {}".format(rest[:space]))
                return
            if interval <= 0:
                print("Interval must be positive")
                return
            command = rest[space:].lstrip()
        else:
            command = raw

        if not command:
            print("Usage: watch [-n SECONDS] COMMAND")
            return

        try:
            while True:
                # Clear screen and move cursor to top-left
                sys.stdout.write("\033[2J\033[H")
                sys.stdout.flush()
                print("Every {:.1f}s: {}".format(interval, command))
                print()
                self.onecmd(command)
                if self.conn is None:
                    return
                time.sleep(interval)
        except KeyboardInterrupt:
            print()

    # -- File streaming ----------------------------------------------------

    def do_tail(self, arg):
        """Stream new data appended to a file in real time.

    Usage: tail PATH

    Continuously displays new data as it is written to the file,
    similar to Unix 'tail -f'. Press Ctrl-C to stop.

    Detects file truncation and deletion.

    Examples:
        tail RAM:logfile.txt"""
        path = arg.strip()
        if not path:
            print("Usage: tail PATH")
            return
        if not self._check_connected():
            return
        path = self._resolve_path(path)
        if path is None:
            return

        def write_chunk(chunk):
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()

        try:
            self.conn.tail(path, write_chunk)
        except KeyboardInterrupt:
            try:
                self.conn.stop_tail()
            except Exception:
                pass
        except AmigactlError as e:
            print(self.cw.error("Error: {}".format(e.message)))
        except ProtocolError as e:
            print(self.cw.error("Protocol error: {}".format(e)))
        except OSError as e:
            print(self.cw.error("Connection error: {}".format(e)))
            self.conn = None
            self._update_prompt()

    # -- Connection management ---------------------------------------------

    def do_reconnect(self, arg):
        """Re-establish the connection after a disconnect.

    Usage: reconnect

    Use this after the connection drops (e.g., network error,
    daemon restart). The current working directory is preserved."""
        if self.conn is not None:
            print("Already connected.")
            return
        self.conn = AmigaConnection(self.host, self.port, self.timeout)
        try:
            self.conn.connect()
        except Exception as e:
            print(self.cw.error("Reconnect failed: {}".format(e)))
            self.conn = None
            return
        try:
            ver = self.conn.version()
        except Exception as e:
            print(self.cw.error("Connected but VERSION failed: {}".format(e)))
            self.conn.close()
            self.conn = None
            return
        self._update_prompt()
        print("Reconnected to {} ({})".format(self.host, ver))

    # -- Destructive operations --------------------------------------------

    def do_shutdown(self, arg):
        """Shut down the Amiga (requires confirmation).

    Usage: shutdown

    Sends a shutdown command to the Amiga. The daemon must have
    ALLOW_REMOTE_SHUTDOWN enabled in its configuration. You will
    be prompted to confirm."""
        if not self._check_connected():
            return
        try:
            answer = input(
                "Shut down the Amiga. Are you sure? [y/N] "
            ).strip().lower()
        except EOFError:
            answer = "n"
        if answer != "y":
            print("Cancelled.")
            return
        result = self._run(self.conn.shutdown)
        if result is not None:
            print(self.cw.success("Shutdown command sent."))
            self.conn = None
            self._update_prompt()

    def do_reboot(self, arg):
        """Reboot the Amiga (requires confirmation).

    Usage: reboot

    Sends a reboot command to the Amiga. The daemon must have
    ALLOW_REMOTE_REBOOT enabled in its configuration. You will
    be prompted to confirm."""
        if not self._check_connected():
            return
        try:
            answer = input(
                "Reboot the Amiga. Are you sure? [y/N] "
            ).strip().lower()
        except EOFError:
            answer = "n"
        if answer != "y":
            print("Cancelled.")
            return
        result = self._run(self.conn.reboot)
        if result is not None:
            print(self.cw.success("Reboot command sent."))
            self.conn = None
            self._update_prompt()

    # -- Session control ---------------------------------------------------

    def do_exit(self, arg):
        """Disconnect and exit the shell.

    Usage: exit

    You can also press Ctrl-D to exit."""
        return True

    def do_quit(self, arg):
        """Disconnect and exit the shell (alias for exit)."""
        return True

    def do_EOF(self, arg):
        """Exit the shell (Ctrl-D)."""
        print()  # newline after ^D
        return True

    # -- Suppress cmd.Cmd noise -------------------------------------------

    def get_names(self):
        """Hide internal commands from help listing."""
        hidden = {'do_EOF', 'do_quit', 'do_dir', 'do_copy',
                  'do_comment', 'do_getenv', 'do_caps'}
        return [n for n in super().get_names() if n not in hidden]

    def emptyline(self):
        """Do nothing on empty input (override cmd.Cmd's default repeat)."""
        pass

    def default(self, line):
        """Handle unknown commands."""
        stripped = line.strip()
        # Single token that looks like a path: treat as cd
        if " " not in stripped and (":" in stripped or stripped.endswith("/")):
            self.do_cd(stripped)
            return
        cmd_word = stripped.split()[0] if stripped else line
        print("Unknown command: {}. Type 'help' for a list of commands."
              .format(cmd_word))
