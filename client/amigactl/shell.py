"""Interactive shell for amigactl."""

import cmd
import os
import shlex
import sys

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

    def __init__(self, host, port, timeout=30):
        super().__init__()
        self.host = host
        self.port = port
        self.timeout = timeout
        self.conn = None
        self.cw = ColorWriter()
        self.cwd = None  # Current working directory (Amiga path)
        self._dir_cache = _DirCache()

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
        self._update_prompt()
        print("Connected to {} ({})".format(self.host, ver))
        print('Type "help" for a list of commands, "exit" to disconnect.')

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
        """Change the current working directory. Usage: cd [PATH]

        Use an absolute Amiga path (e.g., cd SYS:S) or a relative path
        if a CWD is already set (e.g., cd S from SYS:). Use 'cd /' to go
        up one level. Use 'cd' with no arguments to clear the CWD."""
        path = arg.strip()

        # cd with no arguments -- clear CWD
        if not path:
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
        """Print the current working directory."""
        if self.cwd is None:
            print("No current directory set. Use 'cd' to set one.")
        else:
            print(self.cwd)

    # -- File listing and info ---------------------------------------------

    def do_ls(self, arg):
        """List directory contents. Usage: ls [PATH] [-r] [-l]"""
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

        entries = self._run(self.conn.dir, path, recursive=recursive)
        if entries is None:
            return

        if not entries:
            return

        # Calculate column widths
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
            if long_format:
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

            if long_format:
                prot_str = _format_protection(entry["protection"])
                if is_dir:
                    # Directories: no size column
                    print("{tag}  {name:<{nw}}  {prot:>{pw}}  {blank:>{sw}}  "
                          "{date}".format(
                              tag=tag,
                              name=name, nw=max_name,
                              prot=prot_str, pw=max_prot,
                              blank="", sw=max_size,
                              date=date))
                else:
                    size_str = format_size(entry["size"])
                    print("{tag}  {name:<{nw}}  {prot:>{pw}}  {size:>{sw}}  "
                          "{date}".format(
                              tag=tag,
                              name=name, nw=max_name,
                              prot=prot_str, pw=max_prot,
                              size=size_str, sw=max_size,
                              date=date))
            else:
                if is_dir:
                    print("{tag}  {name:<{nw}}  {blank:>{sw}}  "
                          "{date}".format(
                              tag=tag,
                              name=name, nw=max_name,
                              blank="", sw=max_size,
                              date=date))
                else:
                    size_str = format_size(entry["size"])
                    print("{tag}  {name:<{nw}}  {size:>{sw}}  "
                          "{date}".format(
                              tag=tag,
                              name=name, nw=max_name,
                              size=size_str, sw=max_size,
                              date=date))

    def do_stat(self, arg):
        """Show file/directory metadata. Usage: stat PATH"""
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
        """Print file contents to stdout. Usage: cat PATH"""
        path = arg.strip()
        if not path:
            print("Usage: cat PATH")
            return
        if not self._check_connected():
            return
        path = self._resolve_path(path)
        if path is None:
            return

        data = self._run(self.conn.read, path)
        if data is None:
            return

        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()

    # -- File transfer -----------------------------------------------------

    def do_get(self, arg):
        """Download a file. Usage: get REMOTE LOCAL"""
        if not self._check_connected():
            return
        try:
            parts = shlex.split(arg)
        except ValueError as e:
            print("Parse error: {}".format(e))
            return
        if len(parts) != 2:
            print("Usage: get REMOTE LOCAL")
            return

        remote = self._resolve_path(parts[0])
        if remote is None:
            return
        local = parts[1]

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
        """Upload a file. Usage: put LOCAL REMOTE"""
        if not self._check_connected():
            return
        try:
            parts = shlex.split(arg)
        except ValueError as e:
            print("Parse error: {}".format(e))
            return
        if len(parts) != 2:
            print("Usage: put LOCAL REMOTE")
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

        written = self._run(self.conn.write, remote, data)
        if written is None:
            return

        self._dir_cache.invalidate()
        print(self.cw.success(
            "Uploaded {} bytes to {}".format(written, remote)))

    def do_edit(self, arg):
        """Edit a remote file locally. Usage: edit PATH"""
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
            editor = (os.environ.get("VISUAL")
                      or os.environ.get("EDITOR")
                      or "vi")
            subprocess.call([editor, tmpfile])

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
        """Delete a file or empty directory. Usage: rm PATH"""
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
        """Rename/move a file or directory. Usage: mv OLD NEW"""
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
        """Create a directory. Usage: mkdir PATH"""
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

    def do_chmod(self, arg):
        """Get or set protection bits. Usage: chmod PATH [HEX]"""
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
        """Set file datestamp. Usage: touch PATH DATE TIME

        DATE is YYYY-MM-DD, TIME is HH:MM:SS."""
        if not self._check_connected():
            return
        try:
            parts = shlex.split(arg)
        except ValueError as e:
            print("Parse error: {}".format(e))
            return
        if len(parts) != 3:
            print("Usage: touch PATH DATE TIME")
            return

        path = self._resolve_path(parts[0])
        if path is None:
            return
        datestamp = "{} {}".format(parts[1], parts[2])

        result = self._run(self.conn.setdate, path, datestamp)
        if result is not None:
            self._dir_cache.invalidate()
            print("{}={}".format(
                self.cw.key("datestamp"), result))

    # -- Execution and process management ----------------------------------

    def do_exec(self, arg):
        """Execute a CLI command synchronously. Usage: exec CMD..."""
        command = arg.strip()
        if not command:
            print("Usage: exec CMD...")
            return
        if not self._check_connected():
            return

        result = self._run(self.conn.execute, command)
        if result is None:
            return

        rc, output = result
        if output:
            sys.stdout.write(output)
        print("Return code: {}".format(rc))

    def do_run(self, arg):
        """Launch a command asynchronously. Usage: run CMD..."""
        command = arg.strip()
        if not command:
            print("Usage: run CMD...")
            return
        if not self._check_connected():
            return

        proc_id = self._run(self.conn.execute_async, command)
        if proc_id is None:
            return

        print("Process ID: {}".format(proc_id))

    def do_ps(self, arg):
        """List daemon-launched processes. Usage: ps"""
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
        """Show status of a tracked process. Usage: status ID"""
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
        """Send a break signal to a process. Usage: signal ID [SIG]

        Default signal is CTRL_C."""
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
        """Force-terminate a tracked process. Usage: kill ID"""
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
        """Print daemon version. Usage: version"""
        if not self._check_connected():
            return

        ver = self._run(self.conn.version)
        if ver is not None:
            print(ver)

    def do_ping(self, arg):
        """Ping the daemon. Usage: ping"""
        if not self._check_connected():
            return

        result = self._run(self.conn.ping)
        if result is not None:
            print(self.cw.success("OK"))

    def do_uptime(self, arg):
        """Show daemon uptime. Usage: uptime"""
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
        """Show system information. Usage: sysinfo"""
        if not self._check_connected():
            return

        info = self._run(self.conn.sysinfo)
        if info is None:
            return

        for key, value in info.items():
            print("{}={}".format(self.cw.key(key), value))

    def do_assigns(self, arg):
        """List logical assigns. Usage: assigns"""
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

    def do_ports(self, arg):
        """List active Exec message ports. Usage: ports"""
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
        """List mounted volumes. Usage: volumes"""
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
        """List running tasks/processes. Usage: tasks"""
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

    # -- ARexx -------------------------------------------------------------

    def do_arexx(self, arg):
        """Send ARexx command to a named port. Usage: arexx PORT CMD..."""
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

    # -- File streaming ----------------------------------------------------

    def do_tail(self, arg):
        """Stream file appends. Usage: tail PATH (Ctrl-C to stop)"""
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
        """Re-establish the connection after a disconnect. Usage: reconnect"""
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
        """Shut down the Amiga. Usage: shutdown"""
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
        """Reboot the Amiga. Usage: reboot"""
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
        """Exit the shell. Usage: exit"""
        return True

    def do_quit(self, arg):
        """Exit the shell. Usage: quit"""
        return True

    def do_EOF(self, arg):
        """Exit the shell (Ctrl-D)."""
        print()  # newline after ^D
        return True

    # -- Suppress cmd.Cmd noise -------------------------------------------

    def emptyline(self):
        """Do nothing on empty input (override cmd.Cmd's default repeat)."""
        pass

    def default(self, line):
        """Handle unknown commands."""
        cmd_word = line.split()[0] if line.strip() else line
        print("Unknown command: {}. Type 'help' for a list of commands."
              .format(cmd_word))
