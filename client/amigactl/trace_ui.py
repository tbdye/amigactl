"""Interactive trace event viewer using ANSI escape sequences.

Provides a terminal UI for streaming trace events from atrace.
Uses DECSTBM (DEC Set Top and Bottom Margins) scroll regions for
a three-region layout: status bar, scroll region, hotkey bar.

No external TUI libraries (curses, etc.) -- pure ANSI escape codes.
"""

import atexit
import copy
from collections import deque
from datetime import datetime
import os
import re
import select
import signal
import socket
import sys
import time as _time
try:
    import tty
    import termios
except ImportError:
    tty = None
    termios = None  # Windows -- not supported for interactive viewer

from .colors import RESET, format_trace_event, get_lib_color, strip_ansi
from .protocol import ProtocolError, send_command

# Shell variables suppressed by the noise filter grid items.
# Includes RC and Result2: these duplicate information already visible
# in traced function return values. Post-command SetVar RC/Result2 are
# shell bookkeeping, not application library calls. Users debugging
# shell behavior can enable individual noise items in the toggle grid.
_SHELL_INIT_VARS = frozenset({
    "process",
    "RC", "Result2",
    "echo", "debug", "oldredirect",
    "interactive", "simpleshell",
})

# All noise item names (shell init vars + LV_ALIAS for FindVar filter).
# Used to build the default noise_suppressed set.
_ALL_NOISE_NAMES = frozenset(_SHELL_INIT_VARS | {"LV_ALIAS"})


class TerminalState:
    """Manage raw terminal mode with guaranteed cleanup.

    Handles:
    - Saving/restoring terminal attributes (cooked mode)
    - DECSTBM scroll region setup and teardown
    - Cursor visibility
    - Signal handlers for SIGINT, SIGWINCH, SIGTSTP
    - atexit handler for crash recovery
    - Flow control (IXON/IXOFF) disabling

    Usage:
        with TerminalState() as term:
            term.setup_regions(rows, cols)
            # ... interactive loop ...
    """

    def __init__(self, stdin_fd=None, stdout=None):
        self.stdin_fd = sys.stdin.fileno() if stdin_fd is None else stdin_fd
        self.stdout = stdout or sys.stdout
        self._saved_attrs = None
        self._saved_sigint = None
        self._saved_sigwinch = None
        self._saved_sigtstp = None
        self._atexit_registered = False
        self.rows = 24
        self.cols = 80

    def __enter__(self):
        # Save terminal attributes
        self._saved_attrs = termios.tcgetattr(self.stdin_fd)
        # Enter cbreak mode: characters available immediately,
        # no echo, but signals (Ctrl-C) still work
        tty.setcbreak(self.stdin_fd)

        # Disable flow control (C1 fix): Ctrl-S/Ctrl-Q should not
        # freeze/resume output. setcbreak() already enables ISIG
        # (so Ctrl-C still delivers SIGINT). Ctrl-Z is read as a
        # regular character by read_key() since cbreak mode does
        # not perform job control processing.
        attrs_cbreak = termios.tcgetattr(self.stdin_fd)
        attrs_cbreak[0] &= ~(termios.IXON | termios.IXOFF)
        termios.tcsetattr(self.stdin_fd, termios.TCSANOW, attrs_cbreak)

        # Register atexit cleanup
        atexit.register(self._cleanup)
        self._atexit_registered = True
        # Detect terminal size
        self._update_size()
        return self

    def __exit__(self, *exc_info):
        self._cleanup()

    def _cleanup(self):
        """Restore terminal to original state."""
        if self._saved_attrs is not None:
            # Reset scroll region to full screen
            self._write("\033[r")
            # Show cursor
            self._write("\033[?25h")
            # Move to bottom of screen, clear line
            self._write("\033[{};1H\033[2K".format(self.rows))
            # Restore terminal attributes (restores flow control,
            # ISIG, echo, etc.)
            try:
                termios.tcsetattr(self.stdin_fd,
                                  termios.TCSADRAIN,
                                  self._saved_attrs)
            except termios.error:
                pass
            self._saved_attrs = None
        if self._atexit_registered:
            try:
                atexit.unregister(self._cleanup)
            except Exception:
                pass
            self._atexit_registered = False

    def _update_size(self):
        try:
            size = os.get_terminal_size()
            self.rows = size.lines
            self.cols = size.columns
        except OSError:
            self.rows = 24
            self.cols = 80

    def _write(self, s):
        self.stdout.write(s)
        self.stdout.flush()

    def clear_screen(self):
        """Clear entire screen and move cursor to home position."""
        self._write("\033[2J\033[H")

    def setup_regions(self):
        """Configure DECSTBM four-region layout.

        Line 1:          Status bar (fixed)
        Line 2:          Column header (fixed)
        Lines 3..rows-1: Scroll region (events)
        Line rows:        Hotkey bar (fixed)
        """
        if self.rows < 5:
            # Terminal too short for a valid scroll region; skip DECSTBM
            # to avoid emitting invalid escape sequences.
            return
        # Set scroll region: rows 3 through rows-1
        self._write("\033[3;{}r".format(self.rows - 1))
        # Move cursor into scroll region
        self._write("\033[3;1H")

    def write_status_bar(self, text):
        """Write text to the fixed top line (line 1)."""
        # Save cursor, move to line 1, clear line, write, restore
        self._write("\0337")       # save cursor
        self._write("\033[1;1H")   # move to row 1, col 1
        self._write("\033[2K")     # clear entire line
        # Truncate to terminal width (ANSI-aware)
        visible = _truncate_to_visible(text, self.cols)
        self._write(visible)
        self._write("\0338")       # restore cursor

    def write_hotkey_bar(self, text):
        """Write text to the fixed bottom line."""
        self._write("\0337")
        self._write("\033[{};1H".format(self.rows))
        self._write("\033[2K")
        visible = _truncate_to_visible(text, self.cols)
        self._write(visible)
        self._write("\0338")

    def write_event(self, text):
        """Write an event line into the scroll region.

        The terminal's scroll region handles scrolling automatically.
        We position at the bottom of the scroll region and write.
        Text is truncated to terminal width to prevent wrapping
        into the hotkey bar (S2 fix).
        """
        # Truncate to terminal width (strip ANSI for length check)
        truncated = _truncate_to_visible(text, self.cols)

        # Move to bottom of scroll region, write line
        self._write("\033[{};1H".format(self.rows - 1))
        self._write("\033[2K")
        self._write(truncated)
        self._write("\n")  # triggers scroll within DECSTBM region

    def clear_scroll_region(self):
        """Clear the scroll region content."""
        self._write("\0337")
        for row in range(3, self.rows):
            self._write("\033[{};1H\033[2K".format(row))
        self._write("\0338")

    def write_at(self, row, text):
        """Write text at a specific row, clearing the line first.

        Public API for scroll-back rendering and grid display (S2 fix).
        Positions at (row, col 1), clears the entire line, then writes
        the text truncated to terminal width.
        """
        truncated = _truncate_to_visible(text, self.cols)
        self._write("\033[{};1H\033[2K{}".format(row, truncated))

    def read_key(self):
        """Read a single keypress (non-blocking check).

        Returns the key character, or None if no input available.
        For escape sequences (arrows, etc.), returns a tuple.
        """
        readable, _, _ = select.select([self.stdin_fd], [], [], 0)
        if not readable:
            return None
        ch = os.read(self.stdin_fd, 1).decode("utf-8", errors="replace")
        if ch == "\033":
            # Possible escape sequence -- read more if available
            readable2, _, _ = select.select([self.stdin_fd], [], [], 0.05)
            if readable2:
                seq = os.read(self.stdin_fd, 8).decode(
                    "utf-8", errors="replace")
                return ("esc", seq)
            return ("esc", "")
        return ch


def _visible_len(s):
    """Return the visible length of a string, ignoring ANSI escapes."""
    return len(re.sub(r'\033\[[0-9;]*m', '', s))


def _truncate_to_visible(s, max_width):
    """Truncate a string with ANSI codes to max_width visible chars.

    Preserves ANSI escape sequences but limits visible character
    count. Appends RESET if any escape was active.
    """
    visible = 0
    result = []
    i = 0
    has_active_escape = False
    while i < len(s) and visible < max_width:
        # Check for ANSI escape sequence
        m = re.match(r'\033\[[0-9;]*m', s[i:])
        if m:
            result.append(m.group())
            has_active_escape = (m.group() != "\033[0m")
            i += len(m.group())
        else:
            result.append(s[i])
            visible += 1
            i += 1
    text = "".join(result)
    if has_active_escape:
        text += "\033[0m"
    return text


class ColumnLayout:
    """Compute adaptive column widths based on terminal width.

    Priority system:
      P1: lib.func  (min 10, preferred 16)
      P2: result    (min 4, preferred 6)
      P3: process   (min 8, preferred 14)
      P4: args      (min 10, remainder)
      P5: timestamp (0 or 12)

    Width breakpoints:
      120+ (wide), 80-119 (standard), 60-79 (narrow), <60 (cramped)
    """

    def __init__(self, cols):
        self.cols = cols
        # Seq column is fixed at 6
        self.seq_width = 6
        # Compute layout
        self._compute()

    def _compute(self):
        if self.cols >= 120:
            self.time_width = 12
            self.func_width = 20
            self.result_width = 12
            self.proc_width = 16
            self.abbrev_lib = False
            n_parts = 6  # seq, time, func, proc, args, result
        elif self.cols >= 80:
            self.time_width = 12
            self.func_width = 16
            self.result_width = 8
            self.proc_width = 14
            self.abbrev_lib = False
            n_parts = 6
        elif self.cols >= 60:
            self.time_width = 8   # sub-second only: SS.mmm
            self.func_width = 12
            self.result_width = 6
            self.proc_width = 10
            self.abbrev_lib = True  # "d.Open" instead of "dos.Open"
            n_parts = 6
        else:
            self.time_width = 0    # drop timestamp
            self.func_width = 10
            self.result_width = 4
            self.proc_width = 8
            self.abbrev_lib = True
            n_parts = 5  # no timestamp column

        sep_total = (n_parts - 1) * 2  # "  ".join() inserts 2 chars
        remaining = self.cols - self.seq_width - sep_total
        fixed = (self.time_width + self.func_width
                 + self.result_width + self.proc_width)
        self.args_width = max(remaining - fixed, 10)

    def format_event(self, event, cw, time_str=None):
        """Format an event dict using adaptive column widths.

        Uses ANSI-aware padding (S5 fix): _visible_len() strips
        ANSI escape codes before computing padding. This ensures
        columns stay aligned when colors are enabled.

        Args:
            event: Parsed event dict.
            cw: ColorWriter instance.
            time_str: Pre-formatted timestamp string from the viewer.
                The viewer calls _format_timestamp(event) and passes
                the result here, because timestamp formatting depends
                on viewer state (timestamp_mode, start_time,
                last_event_time) that ColumnLayout does not have
                access to (M2 fix from R3).
                If None, falls back to the raw event time.
        """
        seq = str(event.get("seq", ""))[:self.seq_width]
        if time_str is None:
            time_str = event.get("time", "")
        lib = event.get("lib", "")
        func = event.get("func", "")
        proc = event.get("task", "")
        args = event.get("args", "")
        retval = event.get("retval", "")
        status = event.get("status", "-")

        if self.abbrev_lib and lib:
            lib = lib[0]  # "dos" -> "d"

        lib_func = "{}.{}".format(lib, func)
        if len(lib_func) > self.func_width:
            lib_func = lib_func[:self.func_width - 1] + "~"

        if len(proc) > self.proc_width:
            proc = proc[:self.proc_width - 1] + "~"

        if len(args) > self.args_width:
            args = args[:self.args_width - 3] + "..."

        if len(retval) > self.result_width:
            retval = retval[:self.result_width - 1] + "~"

        # Apply colors
        lib_color = get_lib_color(event.get("lib", ""))
        lib_func_colored = "{}{}{}".format(
            lib_color, lib_func, RESET) if cw.enabled else lib_func

        if status == "E":
            retval_colored = cw.error(retval)
        elif status == "O":
            retval_colored = cw.success(retval)
        else:
            retval_colored = retval

        seq_colored = cw.dim(seq)
        proc_colored = cw.green(proc)

        # ANSI-aware padding (S5 fix).
        # _visible_len() counts only visible characters (no ANSI
        # escapes). Pad each field to its column width using
        # the visible width, not the string length.
        def _pad(s, width, align="left"):
            vis = _visible_len(s)
            pad = max(0, width - vis)
            if align == "right":
                return " " * pad + s
            return s + " " * pad

        parts = [_pad(seq_colored, self.seq_width)]
        if self.time_width > 0:
            parts.append(time_str[:self.time_width].rjust(self.time_width))
        parts.append(_pad(lib_func_colored, self.func_width))
        parts.append(_pad(proc_colored, self.proc_width))
        parts.append(_pad(args, self.args_width))
        parts.append(retval_colored)

        return "  ".join(parts)

    def format_header(self, cw):
        """Format a column header string matching format_event() widths.

        Uses the same column widths and separator style as format_event()
        so the header aligns with event data. The entire header is
        rendered in dim text to distinguish it from events.

        Args:
            cw: ColorWriter instance.
        """
        def _pad(s, width, align="left"):
            pad = max(0, width - len(s))
            if align == "right":
                return " " * pad + s
            return s + " " * pad

        parts = [_pad("SEQ", self.seq_width)]
        if self.time_width > 0:
            parts.append("TIME".rjust(self.time_width))
        parts.append(_pad("FUNCTION", self.func_width))
        parts.append(_pad("TASK", self.proc_width))
        parts.append(_pad("ARGS", self.args_width))
        parts.append("RESULT")

        header = "  ".join(parts)
        return cw.dim(header)


class HandleResolver:
    """Track Open/Lock return values and resolve handles to paths.

    Maintains a bounded cache mapping hex handle values to file paths.
    Populated by tracking Open and Lock events; consumed by Close and
    CurrentDir annotation.

    The cache is keyed by the normalized hex string representation of
    the handle (e.g., "0x1c16daf"), not the integer value.
    """

    def __init__(self, max_size=256):
        self._cache = {}       # hex_str -> path
        self._order = []       # insertion order for FIFO eviction
        self._close_annotations = {}  # seq -> path (pre-recorded at Close time)
        self._close_order = []        # insertion order for FIFO eviction
        self._max_size = max_size

    def clear(self):
        """Clear all cached handles. Called at trace session start."""
        self._cache.clear()
        self._order.clear()
        self._close_annotations.clear()
        self._close_order.clear()

    def track(self, event):
        """Track an event, updating the handle-to-path cache.

        Call this for every event, before formatting.

        - Open/Lock with non-NULL return values populate the cache.
        - Close events snapshot the annotation (handle -> path) into
          ``_close_annotations`` keyed by sequence number, then evict
          the handle from the live cache so subsequent Opens at the
          same address create a fresh mapping.

        Args:
            event: Parsed event dict with keys: func, retval, args,
                   status, seq.
        """
        func = event.get("func", "")
        retval = event.get("retval", "")
        status = event.get("status", "-")
        args = event.get("args", "")

        # --- Close: snapshot annotation, then evict handle ---
        if func == "Close":
            hex_val = self._extract_hex(args, "fh=")
            if hex_val and hex_val in self._cache:
                seq = event.get("seq", "")
                path = self._cache[hex_val]

                # FIFO eviction on close_annotations
                if len(self._close_annotations) >= self._max_size and \
                        seq not in self._close_annotations:
                    oldest = self._close_order.pop(0)
                    self._close_annotations.pop(oldest, None)

                self._close_annotations[seq] = path
                if seq not in self._close_order:
                    self._close_order.append(seq)

                # Evict from live cache
                del self._cache[hex_val]
                if hex_val in self._order:
                    self._order.remove(hex_val)
            return

        # --- Open/Lock: populate cache ---
        if func not in ("Open", "Lock"):
            return
        if status != "O":
            return
        if not retval.startswith("0x"):
            return

        # Extract path from args: Open("path",mode) or Lock("path",type)
        path = self._extract_path(args)
        if not path:
            return

        key = self._normalize_hex(retval)

        # Evict oldest if at capacity
        if len(self._cache) >= self._max_size and \
                key not in self._cache:
            oldest = self._order.pop(0)
            self._cache.pop(oldest, None)

        self._cache[key] = path
        if key not in self._order:
            self._order.append(key)

    def annotate(self, event, consume=False):
        """Return annotation string for Close/CurrentDir, or None.

        For Close events, looks up the pre-recorded annotation saved
        by ``track()`` (keyed by sequence number) first, then falls
        back to the live cache for backwards compatibility with events
        that were not fed through ``track()``.

        Args:
            event: Parsed event dict.
            consume: If True, remove the cache entry for Close events.
                     Retained for backwards compatibility but largely
                     superseded by track()-based eviction.
        """
        func = event.get("func", "")
        args = event.get("args", "")

        if func == "Close":
            # Primary path: pre-recorded annotation from track()
            seq = event.get("seq", "")
            if seq and seq in self._close_annotations:
                return self._close_annotations[seq]

            # Fallback: live cache lookup (for events not tracked)
            hex_val = self._extract_hex(args, "fh=")
            if hex_val and hex_val in self._cache:
                path = self._cache[hex_val]
                if consume:
                    del self._cache[hex_val]
                    if hex_val in self._order:
                        self._order.remove(hex_val)
                return path

        if func == "CurrentDir":
            # Only annotate if the daemon sent a bare hex value
            # (no quotes = not already resolved by daemon lock_cache)
            if args.startswith('"'):
                return None  # daemon already resolved it
            hex_val = self._extract_hex(args, "lock=")
            if hex_val and hex_val in self._cache:
                return self._cache[hex_val]

        return None

    @staticmethod
    def _extract_path(args):
        """Extract quoted path from args like '"RAM:foo",Write'.

        Returns the path string without quotes, or None.
        """
        if not args.startswith('"'):
            return None
        end = args.find('"', 1)
        if end < 0:
            return None
        return args[1:end]

    @staticmethod
    def _extract_hex(args, prefix):
        """Extract hex value after a prefix like 'fh=' or 'lock='.

        Handles formats like:
        - 'fh=0x1c16daf'
        - 'lock=0x1c16daf'
        - 'lock=NULL' (returns None)

        Returns the normalized hex string (e.g., '0x1c16daf') or None.
        """
        idx = args.find(prefix)
        if idx < 0:
            return None
        start = idx + len(prefix)
        if args[start:start + 2] != "0x":
            return None
        # Find end of hex value (next comma, space, or end of string)
        end = start
        while end < len(args) and args[end] not in (',', ' ', ')'):
            end += 1
        raw = args[start:end]
        return HandleResolver._normalize_hex(raw)

    @staticmethod
    def _normalize_hex(hex_str):
        """Normalize a hex string to consistent format.

        Strips leading zeros after '0x' prefix for consistent matching
        between '0x01c16daf' (retval format) and '0x1c16daf' (args format).
        """
        if not hex_str.startswith("0x"):
            return hex_str
        # Parse as int, re-format without leading zeros
        try:
            val = int(hex_str, 16)
            return "0x{:x}".format(val)
        except ValueError:
            return hex_str


class SegmentResolver:
    """Track LoadSeg/NewLoadSeg return values and resolve segment
    pointers to filenames for RunCommand annotation.

    Maintains a bounded cache mapping hex segment values to filenames.
    Populated by tracking LoadSeg/NewLoadSeg events; consumed by
    RunCommand annotation.

    Cache semantics differ from HandleResolver: segments are not
    consumed on use because RunCommand does not free the segment
    (the caller calls UnloadSeg separately, which is not a traced
    function). Entries expire via FIFO eviction only.
    """

    def __init__(self, max_size=128):
        self._cache = {}       # hex_str -> filename
        self._order = []       # insertion order for FIFO eviction
        self._max_size = max_size

    def clear(self):
        """Clear all cached segments. Called at trace session start."""
        self._cache.clear()
        self._order.clear()

    def track(self, event):
        """Track LoadSeg/NewLoadSeg events, caching segment->filename.

        Args:
            event: Parsed event dict with func, retval, args, status.
        """
        func = event.get("func", "")
        if func not in ("LoadSeg", "NewLoadSeg"):
            return
        retval = event.get("retval", "")
        status = event.get("status", "")
        if status != "O":
            return
        if not retval.startswith("0x"):
            return

        # Extract filename from args: LoadSeg("filename")
        args = event.get("args", "")
        if not args.startswith('"'):
            return
        end = args.find('"', 1)
        if end < 0:
            return
        filename = args[1:end]
        if not filename:
            return

        key = self._normalize_hex(retval)

        # Evict oldest if at capacity.
        # O(n) pop(0) on _order is acceptable for max_size=128.
        if len(self._cache) >= self._max_size and \
                key not in self._cache:
            oldest = self._order.pop(0)
            self._cache.pop(oldest, None)

        self._cache[key] = filename
        if key not in self._order:
            self._order.append(key)

    def annotate(self, event):
        """Return filename for RunCommand's seg= argument, or None.

        Args:
            event: Parsed event dict for a RunCommand event.
        """
        func = event.get("func", "")
        if func != "RunCommand":
            return None
        args = event.get("args", "")
        # Extract seg=0x... from args
        if not args.startswith("seg=0x"):
            return None
        # Find end of hex value (next comma or end of string)
        end = args.find(",", 4)
        if end < 0:
            hex_str = args[4:]  # skip "seg=" prefix, keeping "0x..."
        else:
            hex_str = args[4:end]  # skip "seg=" prefix, keeping "0x..."
        key = self._normalize_hex(hex_str)
        return self._cache.get(key)

    @staticmethod
    def _normalize_hex(hex_str):
        """Normalize hex string by stripping leading zeros.

        '0x01c16daf' -> '0x1c16daf'
        '0x00001234' -> '0x1234'
        """
        if not hex_str.startswith("0x"):
            return hex_str
        stripped = hex_str[2:].lstrip("0") or "0"
        return "0x" + stripped


class TraceViewer:
    """Interactive trace event viewer.

    Manages the terminal UI, event consumption, filtering, and
    user input during a trace stream.
    """

    def __init__(self, conn, session, cw, mode="start", proc_id=None):
        self.conn = conn
        self.session = session        # RawTraceSession
        self.sock = session.sock      # raw socket for select()
        self.reader = session.reader  # TraceStreamReader
        self.cw = cw
        self.mode = mode        # "start" or "run"
        self.proc_id = proc_id

        # State
        self.running = True
        self.paused = False
        self.search_pattern = None
        self.stats_mode = False
        self.timestamp_mode = "absolute"  # absolute, relative, delta
        self.help_visible = False
        self.grid_visible = False
        self.errors_filter = False  # Toggle for ERRORS mode
        # Per-item noise suppression: items in this set are suppressed.
        # Default: all noise items suppressed (matches old
        # shell_noise_filter=True behavior). Individual items can be
        # enabled via the NOISE category in the toggle grid.
        self.noise_suppressed = set(_ALL_NOISE_NAMES)

        # Event tracking
        self.total_events = 0
        self.shown_events = 0
        self.error_count = 0
        self.start_time = None
        self.last_event_time = None

        # Statistics
        self.func_counts = {}    # "lib.func" -> count
        self.lib_counts = {}     # "lib" -> count
        self.proc_counts = {}    # "proc" -> count
        self.error_counts = {}   # "lib.func" -> error_count

        # Pause buffer
        self.pause_buffer = []
        self.pause_buffer_limit = 1000
        self.pause_scroll_pos = 0

        # Scrollback buffer: retains ALL received events for retroactive
        # filtering and scroll-back when paused. Populated by
        # _process_event_result() before client filters are applied.
        # [SF4 fix]: Uses deque(maxlen=N) for O(1) append with automatic
        # size limiting.
        self.scrollback_limit = 10000
        self.scrollback = deque(maxlen=self.scrollback_limit)
        self._scroll_snapshot = None  # [R3-SF7 fix]: frozen on pause
        self._scrollback_full = False  # [R3-SF10 fix]: ever reached maxlen

        # Discovered filter values (for toggle grid)
        self.discovered_libs = {}    # lib_name -> count
        self.discovered_funcs = {}   # {lib_name: {func_name: count}} (M6 fix)
        self.discovered_procs = {}   # proc_name -> count

        # Items explicitly disabled in the grid (blocklist).
        # Used by _passes_client_filter() for filtering AND by
        # _restore_grid_state() for grid state restoration.
        # None = no filter applied (grid never used, allow all).
        # Empty set = grid was used, nothing disabled (same as None
        # for filtering purposes). [SF8 fix]
        # Non-empty set = blocklist of disabled values.
        self.disabled_libs = None    # set[str] or None
        self.disabled_procs = None   # set[str] or None
        self.disabled_noise = None   # set[str] or None -- noise items disabled in grid

        # [R3-MF3 fix]: Per-library function disable state.
        # dict[str, set[str]] mapping lib name -> disabled func names,
        # or None if no function filters have been set.
        # Scoped per-library because the grid only shows one library's
        # functions at a time. See _save_func_state().
        self.disabled_funcs = None   # dict[str, set[str]] or None

        # [R6-SF3 fix]: Snapshot of disabled_funcs taken on grid open.
        # Restored on cancel so _save_func_state() navigation writes
        # don't leak into _passes_client_filter(). Cleared on apply.
        self._pre_grid_disabled_funcs = None
        self._pre_grid_noise_suppressed = None

        # Grid state
        self.grid = None             # ToggleGrid instance (when used)
        self.daemon_disabled_funcs = set()  # Daemon-disabled functions in "lib.func" format (Fix 3)
        self.user_enabled_funcs = set()  # Functions user explicitly enabled, "lib.func" format (Fix 3)

        # Output tier state
        from .trace_tiers import TIER_BASIC_LEVEL
        self.current_tier = TIER_BASIC_LEVEL  # Default: Basic
        self.manual_additions = set()  # func_name strings enabled outside tier
        self.manual_removals = set()   # func_name strings disabled within tier

        # Resize handling (C7 fix)
        self._resize_pending = False

        # Status bar dirty flag (C2 fix, R4): avoid redrawing
        # the status bar 10x/sec when nothing has changed.
        self._status_dirty = True
        self._last_status_time = 0.0  # monotonic time of last redraw

        # Highlight cursor (event detail view)
        self.highlight_pos = 0            # Index into combined event list

        # Detail view overlay
        self.detail_visible = False       # Detail overlay active
        self._detail_event = None         # Event dict being shown in detail
        self._detail_scroll_pos = 0       # Scroll position within detail content
        self._detail_lines = []           # Pre-rendered detail lines

        # Handle/lock path annotation (Feature B)
        self.handle_resolver = HandleResolver()

        # Segment/filename annotation (Fix 9)
        self.segment_resolver = SegmentResolver()

        # Trace session metadata from header comments
        self.eclock_freq = 0          # EClock frequency in Hz
        self.timestamp_precision = ""  # "microsecond" or ""

    def _prepopulate_from_status(self, status):
        """Pre-populate discovered_libs and discovered_funcs from TRACE STATUS.

        Ensures all patched functions (including daemon-disabled noise
        functions) appear in the toggle grid with count=0. Also tracks
        which functions are daemon-disabled so the grid can send
        ENABLE/DISABLE commands when toggling them.

        Args:
            status: Dict from conn.trace_status(), containing patch_list.
        """
        patch_list = status.get("patch_list", [])
        self.daemon_disabled_funcs = set()  # Track daemon-disabled funcs ("lib.func" format)

        for entry in patch_list:
            name = entry.get("name", "")
            enabled = entry.get("enabled", True)
            if "." not in name:
                continue
            lib, func = name.split(".", 1)

            # Initialize lib with count=0 if not yet seen
            if lib not in self.discovered_libs:
                self.discovered_libs[lib] = 0
            if lib not in self.discovered_funcs:
                self.discovered_funcs[lib] = {}
            if func not in self.discovered_funcs[lib]:
                self.discovered_funcs[lib][func] = 0

            if not enabled:
                # Store in "lib.func" format to match TRACE STATUS patch_N=<lib>.<func>
                # and prevent future name collisions across libraries.
                self.daemon_disabled_funcs.add("{}.{}".format(lib, func))

    def run(self):
        """Main entry point. Sets up terminal and runs event loop."""
        with TerminalState() as term:
            self.term = term
            # Install SIGWINCH handler
            old_sigwinch = signal.signal(
                signal.SIGWINCH, self._handle_sigwinch)
            try:
                self.layout = ColumnLayout(term.cols)
                term.clear_screen()       # Bug 20: wipe stale shell text
                term.setup_regions()
                self._draw_status_bar()
                self._draw_header()
                self._draw_hotkey_bar()
                # Apply initial tier if not Basic
                if self.current_tier > 1:
                    self._apply_initial_tier()
                self._event_loop()
            except KeyboardInterrupt:
                self._stop_trace()
            finally:
                signal.signal(signal.SIGWINCH, old_sigwinch)

    def _event_loop(self):
        """Select-based event loop: multiplex socket and stdin.

        The socket is ALWAYS in wait_fds (S8 fix). Events must
        always be consumed to prevent daemon-side backpressure,
        regardless of pause/grid state. Display and buffering
        decisions happen in _handle_socket_data().
        """
        while self.running:
            # Always include both stdin and socket (S8 fix)
            wait_fds = [sys.stdin.fileno(), self.sock]

            readable, _, _ = select.select(
                wait_fds, [], [], 0.1)

            if self.sock in readable:
                self._handle_socket_data()

            # Also drain any buffered data from previous recv()
            # that contained multiple events (S3: public method)
            while self.running and self.reader.has_buffered_data():
                result = self.reader.drain_buffered()
                if result is None:
                    break
                self._process_event_result(result)

            if sys.stdin.fileno() in readable:
                self._handle_keypress()

            # Handle deferred SIGWINCH resize (C7 fix)
            if self._resize_pending:
                self._resize_pending = False
                self._handle_resize()

            # C2 fix (R4): Only redraw status bar when state changed
            # or once per second (for elapsed time update). Avoids
            # 10x/sec redraws that cause flicker on slow SSH.
            if not self.grid_visible and not self.help_visible \
                    and not self.detail_visible:
                now = _time.monotonic()
                if self._status_dirty or \
                   (now - self._last_status_time) >= 1.0:
                    self._draw_status_bar()
                    self._status_dirty = False
                    self._last_status_time = now

    def _handle_socket_data(self):
        """Read and process available trace data from the socket.

        Uses the non-blocking TraceStreamReader. A single recv()
        may contain multiple events; has_buffered_data() handles
        this in the event loop.
        """
        result = self.reader.try_read_event()
        if result is None:
            return  # Incomplete data, wait for more
        self._process_event_result(result)

    def _process_event_result(self, result):
        """Process a result from TraceStreamReader.

        result is:
        - False: stream ended
        - dict with type="comment": comment event
        - dict with type="event": trace event
        """
        if result is False:
            # Stream ended (END received)
            self.running = False
            return

        event = result

        if event.get("type") == "comment":
            # Parse metadata BEFORE the pause check so eclock_freq /
            # timestamp_precision extraction works regardless of pause
            # state. Only the display part is deferred.
            self._parse_comment_metadata(event)
            self.scrollback.append(event)
            if len(self.scrollback) >= self.scrollback_limit:
                self._scrollback_full = True
            # While paused or overlay visible, buffer the comment for
            # later rendering via _scroll_pause_buffer(). Do NOT call
            # _display_comment() here -- it uses write_event() (scroll
            # region appending) which corrupts the pause view that uses
            # write_at() (absolute positioning).
            if self.paused or self.grid_visible or self.help_visible or self.detail_visible:
                if len(self.pause_buffer) < self.pause_buffer_limit:
                    self.pause_buffer.append(event)
                return
            # Live stream: display immediately
            self._display_comment(event)
            return

        # Update statistics
        self.total_events += 1
        self._status_dirty = True  # C2 fix: event count changed
        lib = event.get("lib", "")
        func = event.get("func", "")
        proc = event.get("task", "")
        lib_func = "{}.{}".format(lib, func)

        self.func_counts[lib_func] = \
            self.func_counts.get(lib_func, 0) + 1
        self.lib_counts[lib] = self.lib_counts.get(lib, 0) + 1
        self.proc_counts[proc] = self.proc_counts.get(proc, 0) + 1

        if event.get("status") == "E":
            self.error_count += 1
            self.error_counts[lib_func] = \
                self.error_counts.get(lib_func, 0) + 1

        # Track discovered values for toggle grid (M6 fix:
        # functions are per-library, not flat)
        self.discovered_libs[lib] = self.lib_counts[lib]
        if lib not in self.discovered_funcs:
            self.discovered_funcs[lib] = {}
        self.discovered_funcs[lib][func] = \
            self.discovered_funcs[lib].get(func, 0) + 1
        self.discovered_procs[proc] = self.proc_counts[proc]

        # Set start_time from first event
        if self.start_time is None:
            self.start_time = event.get("time", "")

        # Track Open/Lock for handle annotation (Feature B)
        self.handle_resolver.track(event)

        # Track LoadSeg/NewLoadSeg for segment annotation (Fix 9)
        self.segment_resolver.track(event)

        # Eagerly resolve handle annotation for Close/CurrentDir.
        # Must happen AFTER track() (which caches this event's Open/Lock)
        # and BEFORE the event is stored in scrollback. This ensures the
        # annotation reflects the cache state at event time, not render
        # time. Fixes wrong-path bug when handles are reused.
        annotation = self.handle_resolver.annotate(event, consume=True)
        if annotation is not None:
            event["_handle_annotation"] = annotation

        # Eagerly resolve segment annotation for RunCommand (Fix 9).
        seg_annotation = self.segment_resolver.annotate(event)
        if seg_annotation is not None:
            event["_segment_annotation"] = seg_annotation

        # Store ALL events in scrollback regardless of filter state.
        # This enables retroactive filter changes: the user can
        # capture events, pause, change filters, and see previously-
        # hidden events appear from the scrollback.
        self.scrollback.append(event)
        if len(self.scrollback) >= self.scrollback_limit:
            self._scrollback_full = True

        # Client-side filtering (toggle grid state)
        if not self._passes_client_filter(event):
            return

        # Client-side search filter
        if self.search_pattern:
            formatted = format_trace_event(event, self.cw)
            if self.search_pattern.lower() not in \
               formatted.lower():
                return

        # If paused, buffer the event instead of displaying
        if self.paused:
            if len(self.pause_buffer) < self.pause_buffer_limit:
                self.pause_buffer.append(event)
            self._draw_status_bar()
            return

        # Help/grid/detail mode: buffer events to prevent scroll region
        # corruption. Defense-in-depth: detail_visible implies paused
        # (caught above), but guard here for consistency with help/grid.
        if self.help_visible or self.grid_visible or self.detail_visible:
            if len(self.pause_buffer) < self.pause_buffer_limit:
                self.pause_buffer.append(event)
            return

        # Display the event
        self.shown_events += 1
        self._display_event(event)

        # S5 fix (R4): Update last_event_time unconditionally so
        # _elapsed_str() works in all timestamp modes, not just delta.
        # This is placed AFTER _display_event() so that delta mode's
        # _format_timestamp() (called from _display_event()) reads
        # the PREVIOUS event's time before we overwrite it here.
        self.last_event_time = event.get("time", "")

    def _handle_keypress(self):
        """Process a single keypress from stdin."""
        key = self.term.read_key()
        if key is None:
            return

        if self.detail_visible:
            if isinstance(key, tuple) and key[0] == "esc":
                seq = key[1]
                if seq == "":  # Bare Escape: dismiss detail
                    self._dismiss_detail()
                    return
                elif seq == "[A":  # Up
                    self._detail_scroll_pos = max(
                        0, self._detail_scroll_pos - 1)
                    self._render_detail()
                    return
                elif seq == "[B":  # Down
                    self._detail_scroll_pos = min(
                        self._detail_scroll_max(),
                        self._detail_scroll_pos + 1)
                    self._render_detail()
                    return
                elif seq == "[5~":  # PgUp
                    self._detail_scroll_pos = max(
                        0, self._detail_scroll_pos -
                        (self.term.rows - 4))
                    self._render_detail()
                    return
                elif seq == "[6~":  # PgDn
                    self._detail_scroll_pos = min(
                        self._detail_scroll_max(),
                        self._detail_scroll_pos +
                        (self.term.rows - 4))
                    self._render_detail()
                    return
            # Any non-navigation key: ignore (only Esc dismisses)
            return

        if self.help_visible:
            # Arrow/page keys scroll help; any other key dismisses
            if isinstance(key, tuple) and key[0] == "esc":
                seq = key[1]
                if seq == "[A":    # Up arrow
                    self._help_scroll_pos = max(
                        0, self._help_scroll_pos - 1)
                    self._render_help()
                    return
                elif seq == "[B":  # Down arrow
                    self._help_scroll_pos = min(
                        self._help_scroll_max(),
                        self._help_scroll_pos + 1)
                    self._render_help()
                    return
                elif seq == "[5~":  # Page Up
                    self._help_scroll_pos = max(
                        0, self._help_scroll_pos -
                        (self.term.rows - 4))
                    self._render_help()
                    return
                elif seq == "[6~":  # Page Down
                    self._help_scroll_pos = min(
                        self._help_scroll_max(),
                        self._help_scroll_pos +
                        (self.term.rows - 4))
                    self._render_help()
                    return
            # Any other key dismisses help and re-renders events
            self.help_visible = False
            self.term.clear_scroll_region()
            self.term.setup_regions()
            self._draw_hotkey_bar()
            self._draw_status_bar()
            if not self.paused:
                self.pause_buffer = []
                self.pause_scroll_pos = 0
                self._rerender_from_scrollback()
            else:
                self._scroll_snapshot = self._build_filtered_snapshot()
                self.pause_scroll_pos = len(self._scroll_snapshot) + \
                    len(self.pause_buffer)
                self._init_highlight_at_bottom()
                self._scroll_pause_buffer(0)
            return

        if self.grid_visible:
            self._handle_grid_key(key)
            return

        # Scroll-back: auto-pause on Up/PgUp, auto-unpause on
        # Down/PgDn at bottom. Highlight cursor moves with arrows.
        if isinstance(key, tuple) and key[0] == "esc":
            seq = key[1]
            if seq == "[A":    # Up arrow
                if not self.paused:
                    self._toggle_pause()
                    self._move_highlight(-1)
                else:
                    self._move_highlight(-1)
                return
            elif seq == "[5~":  # Page Up
                if not self.paused:
                    self._toggle_pause()
                    self._move_highlight(-(self.term.rows - 4))
                else:
                    self._move_highlight(-(self.term.rows - 4))
                return
            elif seq == "[B" and self.paused:  # Down arrow
                at_bottom = self._move_highlight(1)
                if at_bottom:
                    self._toggle_pause()
                return
            elif seq == "[6~" and self.paused:  # Page Down
                at_bottom = self._move_highlight(
                    self.term.rows - 4)
                if at_bottom:
                    self._toggle_pause()
                return

        if key == "q":
            self._stop_trace()
            self.running = False
        elif key == "p":
            self._toggle_pause()
        elif key == "s":
            self.stats_mode = not self.stats_mode
            self._status_dirty = True  # C2 fix
            self._draw_status_bar()
            self._draw_hotkey_bar()
        elif key == "t":
            self._cycle_timestamp()
            self._status_dirty = True  # C2 fix
        elif key == "/":
            self._enter_search_mode()
        elif key == "\t":  # Tab
            self._enter_toggle_grid()
        elif key == "?":
            self._show_help()
        elif key == "e":
            self._toggle_errors_filter()
        elif key == "S":
            self._save_scrollback()
        elif key == "c":
            if not self.paused:
                self._clear_events()
        elif key == "1":
            self._switch_tier(1)
        elif key == "2":
            self._switch_tier(2)
        elif key == "3":
            self._switch_tier(3)
        elif key == "\r" or key == "\n":
            if self.paused:
                self._open_detail_view()

    def _toggle_errors_filter(self):
        """Toggle ERRORS filter on/off (S7: replaces grid RETURN STATUS)."""
        self.errors_filter = not self.errors_filter
        self._status_dirty = True  # C2 fix
        # Send updated filter to daemon
        self._send_current_filter()
        self._draw_status_bar()
        self._draw_hotkey_bar()

    def _clear_events(self):
        """Clear all accumulated events and reset the display.

        Wipes the scrollback buffer, all counters, statistics, and
        discovered filter values. Does NOT clear disabled_* filter
        choices (those are the user's active filters) and does NOT
        send any protocol commands (the trace stream continues).
        """
        # Clear scrollback
        self.scrollback.clear()
        self._scrollback_full = False

        # Reset counters
        self.shown_events = 0
        self.total_events = 0
        self.error_count = 0
        self.last_event_time = None
        self.start_time = None

        # Clear statistics
        self.func_counts.clear()
        self.lib_counts.clear()
        self.proc_counts.clear()
        self.error_counts.clear()

        # Clear discovered filter data
        self.discovered_libs.clear()
        self.discovered_funcs.clear()
        self.discovered_procs.clear()

        # Clear handle resolution cache
        self.handle_resolver.clear()

        # Clear segment resolution cache (Fix 9)
        self.segment_resolver.clear()

        # Clear pause buffer (no stale events on next pause)
        self.pause_buffer.clear()
        self.pause_scroll_pos = 0

        # Redraw: wipe scroll region, update bars
        self.term.clear_scroll_region()
        self._status_dirty = True
        self._draw_status_bar()
        self._draw_hotkey_bar()

    def _save_scrollback(self):
        """Save filtered scrollback buffer to a timestamped log file."""
        # Filter through current client-side filters (disabled_libs,
        # disabled_funcs, disabled_procs, noise_suppressed, search).
        # The saved file should match what the user sees on screen.
        events = self._build_filtered_snapshot()
        if not events:
            self.term.write_hotkey_bar("Nothing to save")
            return

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = "atrace_{}.log".format(timestamp)

        lines = []
        prev_time = None
        for event in events:
            if event.get("type") == "comment":
                lines.append("# {}".format(event.get("text", "")))
                continue
            ev = self._annotated_event(event)
            if hasattr(self, 'layout'):
                time_str = self._format_timestamp_for_scroll(
                    ev, prev_time)
                formatted = self.layout.format_event(
                    ev, self.cw, time_str=time_str)
            else:
                formatted = format_trace_event(ev, self.cw)
            lines.append(strip_ansi(formatted))
            prev_time = ev.get("time", "")

        try:
            with open(filename, 'w') as f:
                f.write('\n'.join(lines))
                f.write('\n')
            if len(events) < len(self.scrollback):
                self.term.write_hotkey_bar(
                    "Saved {} of {} events to {}".format(
                        len(events), len(self.scrollback), filename))
            else:
                self.term.write_hotkey_bar(
                    "Saved {} events to {}".format(len(events), filename))
        except OSError as e:
            self.term.write_hotkey_bar("Save failed: {}".format(e))

    # ---- Display methods ----

    def _annotated_event(self, event):
        """Return event with handle/segment annotations applied to args.

        Prefers the eagerly-computed annotation stored at process time
        (Fix 1: correct for reused handles). Falls back to live cache
        lookup for events processed before this fix was deployed (e.g.,
        if scrollback contains events from a mixed session).

        Returns the original event if no annotation applies, or a
        shallow copy with modified args if annotation is available.
        Does not mutate the original event.
        """
        ev = event  # may be replaced with a copy

        # Handle annotation (Close/CurrentDir)
        annotation = event.get("_handle_annotation")
        if annotation is None:
            # Fallback: live cache lookup (legacy path)
            annotation = self.handle_resolver.annotate(event)
        if annotation is not None:
            ev = dict(event)
            ev["args"] = '{} "{}"'.format(event["args"], annotation)

        # Segment annotation (RunCommand) -- Fix 9
        seg_ann = event.get("_segment_annotation")
        if seg_ann is not None:
            if ev is event:
                ev = dict(event)
            args = ev.get("args", event.get("args", ""))
            ev["args"] = re.sub(
                r'(seg=0x[0-9a-fA-F]+)',
                r'\1 "' + seg_ann + '"',
                args)

        return ev

    def _display_event(self, event):
        """Format and display a single trace event in the scroll region.

        Pure formatting+output method. Scrollback storage is handled by
        _process_event_result() before this method is called.

        Uses ColumnLayout.format_event() with a pre-formatted timestamp
        when available, otherwise falls back to format_trace_event().
        """
        event = self._annotated_event(event)
        if hasattr(self, 'layout'):
            # Adaptive layout with pre-formatted timestamp
            time_str = self._format_timestamp(event)
            formatted = self.layout.format_event(
                event, self.cw, time_str=time_str)
        else:
            # Fallback: use existing format_trace_event()
            formatted = format_trace_event(event, self.cw)
        self.term.write_event(formatted)

    def _rerender_from_scrollback(self):
        """Re-render the scroll region from the scrollback buffer.

        Applies current client filters and search pattern to the
        full scrollback, displaying the most recent events that fit
        in the visible scroll region.

        Does not modify shown_events (lifetime counter).

        Called after:
        - Closing the filter grid (apply or cancel)
        - Unpausing
        - Changing the search pattern
        - Any filter change that should retroactively show/hide events
        """
        self.term.clear_scroll_region()
        visible_lines = self.term.rows - 4  # status + hotkey bars

        # Filter scrollback through current filters
        filtered = self._build_filtered_snapshot()

        # Display the last visible_lines events using write_at()
        # for absolute row positioning (same pattern as
        # _scroll_pause_buffer()).
        display_start = max(0, len(filtered) - visible_lines)
        display_events = filtered[display_start:]

        prev_time = None
        for idx, event in enumerate(display_events):
            row = 3 + idx  # scroll region starts at row 3
            if row >= self.term.rows - 1:
                break
            if event.get("type") == "comment":
                # Render comment using write_at() -- do NOT call
                # _display_comment() which uses write_event() (scroll
                # region appending, not absolute positioning).
                text = self.cw.warning(
                    "# {}".format(event.get("text", "")))
                self.term.write_at(row, text)
                # Do not update prev_time for comments
                continue
            event = self._annotated_event(event)
            if hasattr(self, 'layout'):
                time_str = self._format_timestamp_for_scroll(
                    event, prev_time)
                text = self.layout.format_event(
                    event, self.cw, time_str=time_str)
            else:
                text = format_trace_event(event, self.cw)
            self.term.write_at(row, text)
            prev_time = event.get("time", "")

        # Update last_event_time so delta timestamps for new live events
        # are relative to the last re-rendered event, providing visual
        # continuity after filter changes.
        if prev_time is not None:
            self.last_event_time = prev_time

    def _build_filtered_snapshot(self):
        """Build a list of scrollback events filtered through current
        client filters and search pattern.

        Used by _rerender_from_scrollback(), enter-pause snapshot
        building, grid-close-while-paused snapshot rebuilding, and
        search-while-paused snapshot rebuilding.
        """
        filtered = []
        for event in self.scrollback:
            if event.get("type") == "comment":
                filtered.append(event)
                continue
            if not self._passes_client_filter(event):
                continue
            if self.search_pattern:
                formatted = format_trace_event(event, self.cw)
                if self.search_pattern.lower() not in \
                   formatted.lower():
                    continue
            filtered.append(event)
        return filtered

    def _parse_comment_metadata(self, event):
        """Extract metadata from comment text (eclock_freq, etc.).

        Called unconditionally for all comments, even when buffered
        during pause. The display is separate.
        """
        text = event.get("text", "")
        if text.startswith("eclock_freq: "):
            try:
                freq_str = text.split(":")[1].strip().split()[0]
                self.eclock_freq = int(freq_str)
            except (ValueError, IndexError):
                pass
        elif text.startswith("timestamp_precision: "):
            self.timestamp_precision = text.split(":", 1)[1].strip()

    def _display_comment(self, event):
        """Display a comment event in the scroll region.

        Only called when the stream is live (not paused, no overlay).
        Metadata parsing is handled separately by
        _parse_comment_metadata() which runs unconditionally.
        """
        self._parse_comment_metadata(event)
        text = event.get("text", "")
        self.term.write_event(self.cw.warning("# {}".format(text)))

    # ---- Status and hotkey bars ----

    def _tier_label(self):
        """Build the tier label for the status bar.

        Returns a string like "[basic]", "[detail]",
        "[detail+PutMsg]", "[basic-OpenFont]",
        "[detail+AllocMem,PutMsg]", "[basic-OpenFont+AllocMem]".
        """
        from .trace_tiers import TIER_NAMES

        name = TIER_NAMES.get(self.current_tier, "?")

        parts = []
        if self.manual_removals:
            parts.append("-" + ",".join(sorted(self.manual_removals)))
        if self.manual_additions:
            parts.append("+" + ",".join(sorted(self.manual_additions)))

        if parts:
            return "[{}{}]".format(name, "".join(parts))
        return "[{}]".format(name)

    def _draw_status_bar(self):
        """Render the status bar (top line) based on current mode.

        Dispatches to the appropriate content builder based on state:
        - stats_mode: show per-function call counts
        - paused: show pause position and buffer info
        - default: show event counts, active filters, elapsed time
        """
        if self.detail_visible:
            seq = self._detail_event.get("seq", "?") \
                if self._detail_event else "?"
            text = "DETAIL | Event #{}".format(seq)
        elif self.stats_mode:
            text = self._build_stats_text()
        elif self.paused:
            combined = self._get_combined_events()
            combined_len = len(combined)
            if combined_len > 0:
                visible_lines = self.term.rows - 4
                new_count = max(0, combined_len - (
                    self.pause_scroll_pos + visible_lines))
                text = "PAUSED | event {}/{} | {} new".format(
                    self.highlight_pos + 1, combined_len, new_count)
                if (len(self.scrollback) >= self.scrollback_limit
                        and len(self.pause_buffer)
                        >= self.pause_buffer_limit):
                    text += " | buffer full"
            else:
                text = "PAUSED"
        else:
            # Default: event counts + active filters + elapsed
            shown_text = "{} events".format(self.total_events)
            if self.shown_events != self.total_events:
                shown_text = "{} events ({} shown)".format(
                    self.total_events, self.shown_events)
            if self.error_count > 0:
                shown_text += " {} errors".format(self.error_count)

            filter_parts = []
            if self.errors_filter:
                filter_parts.append("ERRORS")
            if self.noise_suppressed:
                filter_parts.append("noise")
            if self.search_pattern:
                filter_parts.append(
                    'search: "{}"'.format(self.search_pattern))

            elapsed = self._elapsed_str()
            tier_label = self._tier_label()
            parts = ["TRACE " + tier_label + ": " + shown_text]
            if filter_parts:
                parts.append(" | ".join(filter_parts))
            if self.timestamp_mode == "relative":
                parts.append("time:rel")
            elif self.timestamp_mode == "delta":
                parts.append("time:delta")
            parts.append(elapsed)
            text = " | ".join(parts)

        self.term.write_status_bar(text)

    def _draw_header(self):
        """Render the column header at line 2 (between status and scroll)."""
        if hasattr(self, 'layout'):
            header = self.layout.format_header(self.cw)
            self.term.write_at(2, header)

    def _draw_hotkey_bar(self):
        """Render the hotkey bar (bottom line).

        When help is visible, shows help-specific navigation hints
        instead of the main hotkeys. Otherwise delegates to
        _build_hotkey_bar() for content.
        """
        if self.detail_visible:
            bar = "  Esc to dismiss"
            self.term.write_hotkey_bar(
                bar + " " * max(0, self.term.cols - len(bar)))
            return
        if self.help_visible:
            bar = "  Up/Down  PgUp/PgDn  scroll  |  Any other key to dismiss"
            self.term.write_hotkey_bar(
                bar + " " * max(0, self.term.cols - len(bar)))
            return
        text = self._build_hotkey_bar()
        self.term.write_hotkey_bar(text)

    def _build_hotkey_bar(self):
        """Build hotkey bar text adapted to terminal width."""
        if self.grid_visible:
            from .trace_grid import GRID_FOOTER_TEXT
            return GRID_FOOTER_TEXT

        if self.paused:
            pause_text = "[p] RESUME"
        else:
            pause_text = "[p] pause"

        if self.stats_mode:
            stats_text = "[s] STATS"
        else:
            stats_text = "[s] stats"

        if self.errors_filter:
            errors_text = "[e] ERRORS"
        else:
            errors_text = "[e] errors"

        if self.timestamp_mode == "relative":
            time_text = "[t] RELATIVE"
        elif self.timestamp_mode == "delta":
            time_text = "[t] DELTA"
        else:
            time_text = "[t] time"

        full = ("  [Tab] filters  [/] search  {}  {}  "
                "{}  [c] clear  [S] save  [1/2/3] tier  {}  "
                "[?] help  [q] quit").format(
                    pause_text, stats_text, errors_text, time_text)

        if len(full) <= self.term.cols:
            return full

        # Abbreviated
        if self.timestamp_mode == "relative":
            time_short = "[t]REL"
        elif self.timestamp_mode == "delta":
            time_short = "[t]DELT"
        else:
            time_short = "[t]"

        short = "[Tab]filt [/]srch {} {} {} [c] [S] [123] {} [?] [q]".format(
            "[p]PAUS" if self.paused else "[p]",
            "[s]STAT" if self.stats_mode else "[s]",
            "[e]ERR" if self.errors_filter else "[e]",
            time_short)

        if len(short) <= self.term.cols:
            return short

        # Minimal
        return "[Tab] [/] [p] [s] [e] [c] [123] [t] [?] [q]"

    # ---- Filter methods ----

    def _stop_trace(self):
        """Send STOP to the daemon and drain the remaining stream.

        The socket is in non-blocking mode during the interactive loop.
        We temporarily switch to timeout mode (10s) for the drain,
        send STOP, and consume remaining DATA chunks using
        TraceStreamReader. Then restore non-blocking mode.

        The drain loop uses settimeout(10.0), NOT setblocking(True).
        This means recv() waits UP TO 10 seconds, then raises
        socket.timeout (a subclass of OSError). The outer except
        OSError handler catches this as the expected timeout exit
        path when the daemon is slow to respond.

        This is called from:
        - 'q' keypress (normal exit)
        - KeyboardInterrupt handler (Ctrl-C)
        """
        self.running = False
        try:
            # Switch to timeout mode for reliable STOP/drain
            self.sock.settimeout(10.0)

            # Send STOP command
            send_command(self.sock, "STOP")

            # Drain remaining events using the reader.
            # After STOP, the daemon sends any remaining DATA chunks,
            # then END + sentinel. We must consume all of them.
            #
            # Process buffered data first (from previous recv() calls
            # that may contain complete events). This avoids an
            # unnecessary recv() that could stall up to 10 seconds
            # when the END is already in the buffer.
            while True:
                # First, drain any buffered data without recv()
                while self.reader.has_buffered_data():
                    result = self.reader.drain_buffered()
                    if result is False:
                        return  # END received from buffer
                    if result is None:
                        break  # Incomplete, need more data
                    # Discard the event (we're shutting down)

                # Now try recv() for more data (timeout mode:
                # waits up to 10s, raises socket.timeout on expiry)
                try:
                    result = self.reader.try_read_event()
                except socket.timeout:
                    # Timeout expired -- daemon did not send END
                    # within 10 seconds. Best-effort drain complete.
                    break
                if result is False:
                    break  # END received, stream terminated
                if result is None:
                    # Incomplete data -- try again (timeout mode:
                    # next recv() will wait up to 10s)
                    continue
                # Discard the event (we're shutting down)
        except (OSError, ProtocolError):
            # Best-effort: if drain fails, the connection may be
            # broken. The caller will clean up. socket.timeout is
            # a subclass of OSError and is caught here too.
            pass
        finally:
            # Restore non-blocking for any remaining cleanup
            try:
                self.sock.setblocking(False)
            except OSError:
                pass

    def _passes_client_filter(self, event):
        """Check if an event passes client-side toggle grid filters.

        Uses blocklist semantics: items explicitly disabled in the
        grid are blocked. Items never seen in the grid (newly
        discovered after the last grid usage) pass through. [MF2 fix]

        Semantics:
        - disabled_X is None: no filter active, allow all.
        - disabled_X is a non-empty set/dict: block items in the set.

        None vs empty set [SF8 fix]: An empty set means the grid
        was used but nothing was disabled. Treated the same as None
        (allow all) because there is nothing to block.

        [R3-MF3 fix]: disabled_funcs is a dict[str, set[str]],
        keyed by library name. The event's library determines which
        set of disabled functions to check against.
        """
        # Comments always pass client filters
        if event.get("type") == "comment":
            return True

        if self.disabled_libs:
            lib = event.get("lib", "")
            if lib in self.disabled_libs:
                return False

        if self.disabled_funcs:
            # [R3-MF3 fix]: Look up disabled funcs for this event's
            # library. If the library has no entry, all its functions
            # pass (allow-unknown semantics).
            lib = event.get("lib", "")
            disabled_for_lib = self.disabled_funcs.get(lib, set())
            if disabled_for_lib:
                func = event.get("func", "")
                if func in disabled_for_lib:
                    return False

        if self.disabled_procs:
            proc = event.get("task", "")
            if proc in self.disabled_procs:
                return False

        # Shell noise filter: per-item suppression via noise_suppressed set.
        # Each noise item corresponds to a specific variable name or
        # FindVar type. Items in noise_suppressed are blocked.
        if self.noise_suppressed:
            func = event.get("func", "")
            if func in ("SetVar", "GetVar"):
                args = event.get("args", "")
                # Extract variable name from args format:
                # SetVar: "varname",LOCAL
                # GetVar: "varname",LOCAL
                if args.startswith('"'):
                    end_quote = args.find('"', 1)
                    if end_quote > 1:
                        varname = args[1:end_quote]
                        if varname in self.noise_suppressed:
                            return False
            if func == "FindVar":
                args = event.get("args", "")
                if ",LV_ALIAS" in args:
                    if "LV_ALIAS" in self.noise_suppressed:
                        return False

        return True

    def _switch_tier(self, new_level):
        """Switch to a different output tier.

        Computes the delta between current effective function set and
        the new clean tier, sends ENABLE=/DISABLE= via send_filter(),
        and updates state.

        Uses conn.send_filter(raw=...) instead of conn.trace_enable()
        / conn.trace_disable() because the socket is in non-blocking
        mode during streaming. send_filter() is fire-and-forget and
        handles non-blocking sockets correctly. trace_enable() uses
        _send_command() which calls read_response(), which would fail
        on a non-blocking socket (BlockingIOError or reading trace
        DATA chunks instead of the expected OK response).

        The FILTER command must include the current filter state
        (LIB=/FUNC=/PROC=/ERRORS) alongside ENABLE=/DISABLE= because
        parse_extended_filter() in the daemon resets all per-session
        filter state at the start of every FILTER command. Sending
        just FILTER ENABLE=... without the rest would clear any active
        LIB/FUNC/PROC filters.

        Args:
            new_level: Target tier level (1, 2, or 3).
        """
        from .trace_tiers import compute_tier_switch

        if new_level == self.current_tier and not self.manual_additions \
                and not self.manual_removals:
            return  # Already at this tier with no manual overrides

        to_enable, to_disable = compute_tier_switch(
            self.current_tier, new_level,
            self.manual_additions, self.manual_removals)

        # Build FILTER command with ENABLE=/DISABLE= alongside
        # the current filter state. This follows the same pattern
        # as _apply_grid_filters() which builds a complete filter
        # command including all current state.
        filter_parts = []

        # Reconstruct current filter state from grid (if used)
        if self.grid is not None:
            grid_cmd = self.grid.build_filter_command()
            if grid_cmd:
                filter_parts.append(grid_cmd)

        if to_enable:
            filter_parts.append(
                "ENABLE=" + ",".join(sorted(to_enable)))
        if to_disable:
            filter_parts.append(
                "DISABLE=" + ",".join(sorted(to_disable)))

        if self.errors_filter:
            filter_parts.append("ERRORS")

        if filter_parts:
            try:
                self.conn.send_filter(
                    raw=" ".join(filter_parts))
            except Exception:
                pass  # Best-effort (fire-and-forget)

        # Update state
        self.current_tier = new_level
        self.manual_additions = set()
        self.manual_removals = set()

        # Notify daemon of tier level for content-based filtering
        # (e.g., OpenLibrary v0 suppression at Basic tier).
        # Uses bare "TIER <n>" inline command, handled by
        # trace_handle_input() alongside STOP and FILTER.
        try:
            self.conn.send_inline("TIER {}".format(new_level))
        except Exception:
            pass  # Best-effort (fire-and-forget during streaming)

        # Update discovered info: mark newly disabled as daemon-disabled,
        # mark newly enabled as not daemon-disabled.
        for func in to_disable:
            for lib, funcs_dict in self.discovered_funcs.items():
                if func in funcs_dict:
                    self.daemon_disabled_funcs.add(
                        "{}.{}".format(lib, func))
                    break
        for func in to_enable:
            for lib, funcs_dict in self.discovered_funcs.items():
                if func in funcs_dict:
                    self.daemon_disabled_funcs.discard(
                        "{}.{}".format(lib, func))
                    break

        self._status_dirty = True
        self._draw_status_bar()
        self._draw_hotkey_bar()

    def _update_manual_overrides(self):
        """Update manual_additions/manual_removals from daemon state.

        Compares the effective daemon-side enabled/disabled state
        against the current tier's expected function set. Functions
        enabled outside the tier are manual additions; functions
        disabled within the tier are manual removals.

        Called after _apply_grid_filters() (Enter key in grid) to
        track when grid toggles create tier deviations. Also called
        after _switch_tier(), where it will find empty sets (correct
        -- a tier switch is a clean reset that clears overrides).
        """
        from .trace_tiers import functions_for_tier
        tier_funcs = functions_for_tier(self.current_tier)

        self.manual_additions = set()
        self.manual_removals = set()

        for lib_name, funcs_dict in self.discovered_funcs.items():
            for func_name in funcs_dict:
                qualified = "{}.{}".format(lib_name, func_name)
                in_tier = func_name in tier_funcs
                daemon_disabled = qualified in self.daemon_disabled_funcs
                if not daemon_disabled and not in_tier:
                    # Enabled but not in tier = manual addition
                    self.manual_additions.add(func_name)
                elif daemon_disabled and in_tier:
                    # Disabled but in tier = manual removal
                    self.manual_removals.add(func_name)

        self._status_dirty = True

    def _apply_initial_tier(self):
        """Send ENABLE commands for the initial tier if above Basic.

        Called once at startup when --detail or --verbose was specified.
        The loader already disabled non-Basic functions, so we only
        need to enable the functions in the target tier that are
        currently disabled.

        Uses conn.send_filter(raw=...) because the socket is already
        in non-blocking mode (set by trace_start_raw()). The FILTER
        command with ENABLE= is fire-and-forget: no response expected,
        and send_filter() handles non-blocking sockets correctly.

        IMPORTANT: The FILTER command resets per-session filter state
        (parse_extended_filter() clears LIB/FUNC/PROC/ERRORS at the
        start of every call). If the user specified initial filters
        (e.g. trace start --detail LIB=dos), those filters were set
        by TRACE START's parse_filters() call. We must reconstruct
        and include them in the FILTER command to avoid losing them.

        The initial filter kwargs are stored in self._initial_filters
        (set by the caller before run()). If no initial filters were
        specified, only ENABLE= is sent.
        """
        from .trace_tiers import functions_for_tier, TIER_BASIC

        target_funcs = functions_for_tier(self.current_tier)
        to_enable = target_funcs - TIER_BASIC  # Already enabled by loader

        if to_enable:
            parts = []
            # Reconstruct initial filters to prevent FILTER reset
            # from clearing them. self._initial_filters is set by
            # the caller (e.g., shell.py) before viewer.run().
            if hasattr(self, '_initial_filters'):
                f = self._initial_filters
                if f.get("lib"):
                    parts.append("LIB={}".format(f["lib"]))
                if f.get("func"):
                    parts.append("FUNC={}".format(f["func"]))
                if f.get("proc"):
                    parts.append("PROC={}".format(f["proc"]))
                if f.get("errors_only"):
                    parts.append("ERRORS")

            parts.append(
                "ENABLE=" + ",".join(sorted(to_enable)))

            try:
                self.conn.send_filter(raw=" ".join(parts))
            except Exception:
                pass  # Best-effort (fire-and-forget)

        # Notify daemon of tier level for content-based filtering
        # (e.g., OpenLibrary v0 suppression at Basic tier).
        # Only needed when starting above Basic (tier 1 is the daemon
        # default, so sending it would be redundant).
        if self.current_tier != 1:
            try:
                self.conn.send_inline(
                    "TIER {}".format(self.current_tier))
            except Exception:
                pass  # Best-effort (fire-and-forget during streaming)

        if to_enable:
            # Update daemon_disabled tracking
            for func in to_enable:
                for lib, funcs_dict in self.discovered_funcs.items():
                    if func in funcs_dict:
                        self.daemon_disabled_funcs.discard(
                            "{}.{}".format(lib, func))
                        break

    def _send_current_filter(self):
        """Send a FILTER command reflecting the current filter state.

        Combines the errors filter and any active grid filters into
        a single FILTER command. Called when the 'e' hotkey toggles
        ERRORS mode.

        Uses the same content-based approach as _apply_grid_filters()
        (S2 R5): only sends FILTER when there is actual filter content.
        When both the grid filter and errors are empty/off, nothing is
        sent, preserving initial trace start filters.

        Known limitation: The FILTER command replaces ALL server-side
        filter state. If the user started with `trace start LIB=dos`
        and presses `e` to enable ERRORS, the daemon receives
        `FILTER ERRORS` which replaces the LIB=dos filter. Pressing
        `e` again (ERRORS off) with no grid active sends nothing
        (preserving whatever the server has), but the LIB=dos is
        already gone from the first `e` press. A complete fix would
        require tracking and re-sending the initial filters, which is
        deferred to a future enhancement.
        """
        parts = []

        # Include grid filter if grid has been used
        if self.grid is not None:
            grid_cmd = self.grid.build_filter_command()
            if grid_cmd:
                parts.append(grid_cmd)

        # Append ERRORS if active
        if self.errors_filter:
            parts.append("ERRORS")

        if parts:
            self.conn.send_filter(raw=" ".join(parts))
        # else: no filter content -- preserve current server-side state.
        # Don't send bare FILTER (which would clear all filters).

    # ---- Interactive feature methods ----

    def _toggle_pause(self):
        """Toggle pause state."""
        if self.paused:
            # Unpause: re-render from scrollback with current filters
            self.paused = False
            self._scroll_snapshot = None
            self.pause_buffer = []
            self.pause_scroll_pos = 0
            self.highlight_pos = 0
            self._rerender_from_scrollback()
        else:
            self.paused = True
            self.pause_buffer = []
            # Build filtered snapshot from full scrollback
            self._scroll_snapshot = self._build_filtered_snapshot()
            visible_lines = self.term.rows - 4
            total = len(self._scroll_snapshot)
            self.pause_scroll_pos = max(0, total - visible_lines)
            self.highlight_pos = max(0, len(self._scroll_snapshot) - 1)
        self._status_dirty = True  # C2 fix
        self._draw_status_bar()
        self._draw_hotkey_bar()

    def _get_combined_events(self):
        """Build the combined event list (snapshot + filtered pause_buffer)."""
        snapshot = self._scroll_snapshot or []
        filtered_new = [
            e for e in self.pause_buffer
            if e.get("type") == "comment"
            or (self._passes_client_filter(e)
                and (not self.search_pattern
                     or self.search_pattern.lower()
                     in format_trace_event(e, self.cw).lower()))
        ]
        return snapshot + filtered_new

    def _init_highlight_at_bottom(self):
        """Set highlight_pos to the last visible event.

        Called when entering pause mode, and after any operation
        that resets pause_scroll_pos to the bottom (help dismiss,
        grid close, search exit).
        """
        combined = self._get_combined_events()
        visible_lines = self.term.rows - 4
        if combined:
            self.highlight_pos = min(
                len(combined) - 1,
                self.pause_scroll_pos + visible_lines - 1)
        else:
            self.highlight_pos = 0

    def _move_highlight(self, delta):
        """Move the highlight cursor by delta positions.

        Adjusts the viewport (pause_scroll_pos) if the highlight
        moves outside the visible window. Returns True if the
        highlight was already at the last event and delta > 0
        (signals auto-unpause).
        """
        combined = self._get_combined_events()
        if not combined:
            return True

        old_pos = self.highlight_pos
        new_pos = self.highlight_pos + delta
        new_pos = max(0, min(new_pos, len(combined) - 1))

        # Auto-unpause: highlight was at bottom and user pressed Down/PgDn
        if delta > 0 and old_pos >= len(combined) - 1:
            return True

        self.highlight_pos = new_pos

        # Adjust viewport to keep highlight visible
        visible_lines = self.term.rows - 4
        if self.highlight_pos < self.pause_scroll_pos:
            self.pause_scroll_pos = self.highlight_pos
        elif self.highlight_pos >= self.pause_scroll_pos + visible_lines:
            self.pause_scroll_pos = self.highlight_pos - visible_lines + 1

        max_pos = max(0, len(combined) - visible_lines)
        self.pause_scroll_pos = max(0, min(self.pause_scroll_pos, max_pos))

        self._scroll_pause_buffer(0)
        return False

    def _format_detail_status(self, status):
        """Format status code for detail view display."""
        if status == "O":
            return "OK"
        elif status == "E":
            return "Error"
        return status

    def _build_detail_lines(self, event):
        """Build detail view content lines for an event.

        Returns a list of strings for display in the detail overlay.
        Long field values are soft-wrapped at the terminal width.
        """
        ev = self._annotated_event(event)
        lines = []
        lines.append("")
        lines.append("  Event #{}".format(ev.get("seq", "?")))
        lines.append("  " + "\u2500" * min(34, self.term.cols - 4))
        lines.append("")

        fields = [
            ("Time", ev.get("time", "")),
            ("Function", "{}.{}".format(
                ev.get("lib", ""), ev.get("func", ""))),
            ("Task", ev.get("task", "")),
            ("Args", ev.get("args", "")),
            ("Result", ev.get("retval", "")),
            ("Status", self._format_detail_status(
                ev.get("status", "-"))),
        ]

        # "  " + 10-char label + " " = 13 chars before value
        indent = 13
        wrap_width = max(self.term.cols - indent, 20)

        for label, value in fields:
            if len(value) <= wrap_width:
                lines.append("  {:<10s} {}".format(label, value))
            else:
                lines.append("  {:<10s} {}".format(
                    label, value[:wrap_width]))
                remaining = value[wrap_width:]
                while remaining:
                    chunk = remaining[:wrap_width]
                    remaining = remaining[wrap_width:]
                    lines.append(" " * indent + chunk)

        lines.append("")
        return lines

    def _detail_scroll_max(self):
        """Return the maximum detail scroll position."""
        available = self.term.rows - 4
        if len(self._detail_lines) > available:
            available -= 1
        return max(0, len(self._detail_lines) - available)

    def _render_detail(self):
        """Render the detail overlay at the current scroll position."""
        # Clear the column header at row 2 (visually confusing behind
        # the detail overlay)
        self.term.write_at(2, " " * self.term.cols)
        self.term.clear_scroll_region()
        available = self.term.rows - 4
        needs_indicator = len(self._detail_lines) > available

        if needs_indicator:
            available -= 1

        start = self._detail_scroll_pos
        end = start + available
        visible = self._detail_lines[start:end]

        for i, line in enumerate(visible):
            row = 3 + i
            if row >= self.term.rows - 1:
                break
            self.term.write_at(row, line)

        if needs_indicator:
            indicator = "  [lines {}-{} of {}]".format(
                start + 1,
                min(start + available, len(self._detail_lines)),
                len(self._detail_lines))
            self.term.write_at(3 + available, indicator)

    def _open_detail_view(self):
        """Open the detail view overlay for the highlighted event."""
        combined = self._get_combined_events()
        if not combined:
            return
        idx = max(0, min(self.highlight_pos, len(combined) - 1))
        event = combined[idx]
        # Comments don't have func/args/retval fields -- skip detail view
        if event.get("type") == "comment":
            return
        self._detail_event = event
        self._detail_lines = self._build_detail_lines(event)
        self._detail_scroll_pos = 0
        self.detail_visible = True
        self._render_detail()
        self._draw_status_bar()
        self._draw_hotkey_bar()

    def _dismiss_detail(self):
        """Dismiss the detail overlay and restore the scrollback view."""
        self.detail_visible = False
        self._detail_event = None
        self._detail_lines = []
        self._detail_scroll_pos = 0
        self.term.clear_scroll_region()
        self.term.setup_regions()
        self._draw_header()  # Restore column header at row 2
        self._draw_hotkey_bar()
        self._draw_status_bar()
        # Re-render pause scrollback with highlight preserved
        self._scroll_pause_buffer(0)

    def _scroll_pause_buffer(self, delta):
        """Scroll through combined scrollback snapshot and pause buffer.

        [R3-SF7 fix]: Uses _scroll_snapshot (frozen on pause) +
        pause_buffer (live arrivals) to avoid mutation during render.
        [R3-MF1 fix]: Delta timestamps are computed sequentially
        across the visible window, not from stale last_event_time.

        Returns True if the scroll position is at the bottom after
        scrolling (used by auto-unpause logic).
        """
        combined = self._get_combined_events()
        if not combined:
            return True  # empty buffer counts as "at bottom"

        # Clamp highlight_pos to valid range (defense-in-depth)
        self.highlight_pos = max(
            0, min(self.highlight_pos, len(combined) - 1))

        visible_lines = self.term.rows - 4
        max_pos = max(0, len(combined) - visible_lines)

        self.pause_scroll_pos += delta
        self.pause_scroll_pos = max(0, min(self.pause_scroll_pos,
                                            max_pos))

        # [R3-SF10 fix]: Buffer-full indicator
        # [R6-C2 fix]: Removed hasattr guard -- _scrollback_full is
        # always initialized in __init__().
        buffer_truncated = self._scrollback_full

        # Re-render visible window
        self.term.clear_scroll_region()
        start = self.pause_scroll_pos
        end = min(start + visible_lines, len(combined))

        # [R3-MF1 fix]: For delta timestamps, compute deltas
        # sequentially across the visible window. The first visible
        # event's delta is relative to the event immediately before
        # it (or None if it's the first event in the combined list).
        prev_time = None
        if start > 0:
            prev_time = combined[start - 1].get("time", "")

        for i in range(start, end):
            event = combined[i]
            row = 3 + (i - start)

            # [R3-SF10 fix]: Show truncation notice at top.
            # This replaces the first event row when scrolled to
            # the very top, so one fewer event is visible. The user
            # can scroll down one line to see all events.
            if (buffer_truncated and i == start
                    and self.pause_scroll_pos == 0):
                self.term.write_at(
                    row, self.cw.dim(
                        "[buffer full -- oldest events truncated]"))
                continue

            if row >= self.term.rows - 1:
                break

            # Comment rendering: use write_at() with warning color.
            # Do NOT call _display_comment() (uses write_event()) or
            # _annotated_event() (accesses event["args"]).
            if event.get("type") == "comment":
                text = self.cw.warning(
                    "# {}".format(event.get("text", "")))
                # No highlight bar for comments (not selectable)
                self.term.write_at(row, text)
                # Do not update prev_time for comments
                continue

            event = self._annotated_event(event)
            if hasattr(self, 'layout'):
                time_str = self._format_timestamp_for_scroll(
                    event, prev_time)
                formatted = self.layout.format_event(
                    event, self.cw, time_str=time_str)
            else:
                formatted = format_trace_event(event, self.cw)

            # Highlight the selected row with reverse video
            if i == self.highlight_pos:
                vis_len = _visible_len(formatted)
                pad = max(0, self.term.cols - vis_len)
                # Re-apply reverse video after every RESET in the formatted string
                # so the highlight bar spans the full row width
                highlighted = formatted.replace("\033[0m", "\033[0m\033[7m")
                formatted = "\033[7m" + highlighted + " " * pad + "\033[0m"

            self.term.write_at(row, formatted)
            prev_time = event.get("time", "")

        self._draw_status_bar()
        return self.pause_scroll_pos >= max_pos

    def _enter_search_mode(self):
        """Replace hotkey bar with search input.

        Uses select() with timeout to multiplex stdin and socket,
        avoiding busy-wait (S3 fix).
        """
        self.term.write_hotkey_bar("Search: ")
        search_buf = []
        while True:
            # Wait for either stdin or socket data with timeout
            readable, _, _ = select.select(
                [self.term.stdin_fd, self.sock], [], [], 0.1)

            # Always consume socket data first
            if self.sock in readable:
                self._handle_socket_data()
            # Drain buffered data (S3: public method)
            while self.running and self.reader.has_buffered_data():
                result = self.reader.drain_buffered()
                if result is None:
                    break
                self._process_event_result(result)

            # C1 fix (R4): Exit search mode if the trace stream ended
            # (e.g., TRACE RUN process exited). Without this check,
            # the search loop would keep running until the user presses
            # Enter or Esc, even though there are no more events.
            if not self.running:
                break

            if self.term.stdin_fd not in readable:
                continue

            key = self.term.read_key()
            if key is None:
                continue
            if key == "\n" or key == "\r":
                break
            if key == "\x1b" or (isinstance(key, tuple)
                                 and key[0] == "esc"):
                search_buf = []
                break
            if key == "\x7f" or key == "\x08":  # backspace
                if search_buf:
                    search_buf.pop()
            elif isinstance(key, str) and key.isprintable():
                search_buf.append(key)
            # Re-render search bar
            text = "".join(search_buf)
            self.term.write_hotkey_bar("Search: " + text)

        pattern = "".join(search_buf)
        if pattern:
            self.search_pattern = pattern
        else:
            self.search_pattern = None
        self._draw_status_bar()
        self._draw_hotkey_bar()
        if self.paused:
            # Rebuild snapshot with updated search filter, re-render
            # in place so arrow-key scrolling remains consistent.
            self._scroll_snapshot = self._build_filtered_snapshot()
            visible_lines = self.term.rows - 4
            total = len(self._scroll_snapshot)
            self.pause_scroll_pos = max(0, total - visible_lines)
            self._init_highlight_at_bottom()
            self._scroll_pause_buffer(0)
        else:
            self._rerender_from_scrollback()

    def _build_stats_text(self):
        """Build statistics string for status bar."""
        # Sort by count descending, show top N that fit
        sorted_funcs = sorted(
            self.func_counts.items(), key=lambda x: -x[1])
        parts = []
        for name, count in sorted_funcs[:6]:
            parts.append("{}:{}".format(name, count))
        stats = " | ".join(parts)
        elapsed = self._elapsed_str()
        return "STATS: {} | {} events {} errors | {}".format(
            stats, self.total_events, self.error_count, elapsed)

    def _cycle_timestamp(self):
        """Cycle through timestamp display modes."""
        modes = ["absolute", "relative", "delta"]
        idx = modes.index(self.timestamp_mode)
        self.timestamp_mode = modes[(idx + 1) % len(modes)]
        self._draw_status_bar()
        if self.paused:
            self._scroll_pause_buffer(0)
        else:
            self._rerender_from_scrollback()

    def _format_timestamp(self, event):
        """Format the event timestamp based on the current mode.

        Called from _display_event() -> layout.format_event() chain.
        For delta mode, reads self.last_event_time which contains the
        PREVIOUS event's time (S5 fix: _process_event_result() updates
        last_event_time AFTER calling _display_event(), so the value
        read here is always the previous event's time).
        """
        raw_time = event.get("time", "")
        if self.timestamp_mode == "absolute":
            return raw_time[:12]
        elif self.timestamp_mode == "relative":
            if self.start_time is None:
                return "+0.000000"
            return self._time_diff(self.start_time, raw_time)
        elif self.timestamp_mode == "delta":
            if self.last_event_time is None:
                return "+0.000000"
            return self._time_diff(self.last_event_time, raw_time)
            # Note: last_event_time is updated to raw_time by
            # _process_event_result() after _display_event() returns.
        return raw_time

    def _format_timestamp_for_scroll(self, event, prev_time):
        """Format timestamp for scrollback rendering.

        Like _format_timestamp() but takes an explicit prev_time
        parameter instead of reading self.last_event_time. This
        allows delta timestamps to be computed sequentially across
        the visible scroll window. [R3-MF1 fix]

        Args:
            event: The event dict.
            prev_time: The previous event's raw time string, or
                None if this is the first event.
        """
        raw_time = event.get("time", "")
        if self.timestamp_mode == "absolute":
            return raw_time[:12]
        elif self.timestamp_mode == "relative":
            if self.start_time is None:
                return "+0.000000"
            return self._time_diff(self.start_time, raw_time)
        elif self.timestamp_mode == "delta":
            if prev_time is None:
                return "+0.000000"
            return self._time_diff(prev_time, raw_time)
        return raw_time

    def _show_help(self):
        """Show the help overlay in the scroll region."""
        self.help_visible = True
        self._help_lines = [
            "",
            "  atrace Interactive Trace Viewer",
            "  ================================",
            "",
            "  Hotkeys:",
            "    Tab     Open filter toggle grid",
            "    /       Search: filter display by text pattern",
            "    p       Pause/resume display (events still consumed)",
            "    s       Toggle statistics display in status bar",
            "    t       Cycle timestamp: absolute / relative / delta",
            "    e       Toggle ERRORS filter (show only errors)",
            "    c       Clear all events and reset counters",
            "    S       Save scrollback to log file",
            "    1/2/3   Switch output tier (basic/detail/verbose)",
            "    ?       Toggle this help screen",
            "    q       Stop trace and exit viewer",
            "",
            "  Scrollback:",
            "    Up/PgUp     Scroll up (auto-pauses if live)",
            "    Down/PgDn   Scroll down (resumes at bottom)",
            "    Enter       Show full event details (when paused)",
            "",
            "  While in filter grid:",
            "    Up/Down     Select item",
            "    Space       Toggle selected item",
            "    Left/Right  Switch category",
            "    A           Enable all in current category",
            "    N           Disable all in current category",
            "    Enter       Apply filters and return to stream",
            "    Esc         Cancel without applying",
            "",
            "  Process filtering is client-side only.",
            "",
            "  Output tiers:",
            "    1 = Basic:   Core diagnostics (57 functions)",
            "    2 = Detail:  Basic + resource lifecycle (70 functions)",
            "    3 = Verbose: Detail + high-volume I/O (73 functions)",
            "    Manual-tier functions (26) are never auto-enabled.",
            "    Use the toggle grid to enable them individually.",
        ]
        self._help_scroll_pos = 0
        self._render_help()
        self._draw_hotkey_bar()

    def _help_scroll_max(self):
        """Return the maximum help scroll position."""
        available = self.term.rows - 4
        # Reserve a row for the position indicator when help is truncated
        if len(self._help_lines) > available:
            available -= 1
        return max(0, len(self._help_lines) - available)

    def _render_help(self):
        """Render the help overlay at the current scroll position."""
        self.term.clear_scroll_region()
        available = self.term.rows - 4  # status + header at top, hotkey at bottom
        needs_indicator = len(self._help_lines) > available

        # Reserve a row for the position indicator when help is truncated
        if needs_indicator:
            available -= 1

        start = self._help_scroll_pos
        end = start + available
        visible = self._help_lines[start:end]

        for i, line in enumerate(visible):
            row = 3 + i
            if row >= self.term.rows - 1:
                break
            self.term.write_at(row, line)

        # Show position indicator on the reserved row
        if needs_indicator:
            indicator = "  [lines {}-{} of {}]".format(
                start + 1,
                min(start + available, len(self._help_lines)),
                len(self._help_lines))
            self.term.write_at(3 + available, indicator)

    def _restore_grid_state(self):
        """Restore toggle grid item states from saved disabled_* sets.

        Called after creating a new ToggleGrid to preserve filter
        choices across open/close cycles. Uses disabled_* (blocklist)
        instead of toggled_* (whitelist) so that items not yet seen
        in the grid (newly discovered) remain enabled by default.
        [MF2 fix]

        When disabled_X is None, the grid keeps its constructor
        defaults (including noise defaults). When disabled_X is a
        set/dict, items in the set are disabled; all others stay
        enabled.

        [R3-MF3 fix]: disabled_funcs is a dict[str, set[str]]
        keyed by library name. Only the current library's functions
        are restored here (matching grid.func_items). The full
        per-library state is persisted via _save_func_state() on
        library switches and grid close.
        """
        if self.disabled_libs is not None:
            for item in self.grid.lib_items:
                item["enabled"] = item["name"] not in self.disabled_libs

        # [R3-MF3 fix]: Restore function state for the current library.
        # [R5-SF2 fix]: Check `is not None` (identity), not truthiness.
        # An empty dict {} means "grid was used, nothing disabled" --
        # we must still iterate to CLEAR noise defaults set by the
        # constructor. Without this, opening the grid, enabling all
        # noise functions, closing, and reopening would silently
        # re-disable them.
        if self.disabled_funcs is not None:
            lib = self.grid.selected_lib
            disabled_for_lib = self.disabled_funcs.get(lib, set())
            # Even if disabled_for_lib is empty, iterate to set all
            # items enabled=True, overriding constructor noise defaults.
            # [R5-SF2 fix]: Removed `if disabled_for_lib:` guard.
            for item in self.grid.func_items:
                item["enabled"] = (
                    item["name"] not in disabled_for_lib)

        if self.disabled_procs is not None:
            for item in self.grid.proc_items:
                item["enabled"] = item["name"] not in self.disabled_procs

        # Restore noise item states from disabled_noise.
        # Unlike LIB/FUNC/PROC, noise items have no "never visited"
        # concept -- they always exist. When disabled_noise is not None,
        # restore the saved state. When None (first grid open), the
        # constructor defaults (all disabled) match noise_suppressed.
        if self.disabled_noise is not None:
            for item in self.grid.noise_items:
                item["enabled"] = item["name"] not in self.disabled_noise

        # Re-snapshot the initial filter so has_user_changes()
        # correctly detects changes from the RESTORED state, not
        # from the fresh-constructor state.
        self.grid._initial_filter = self.grid.build_filter_command()
        self.grid.user_interacted = False

    def _save_func_state(self):
        """Save the current grid's function disabled state for the
        current library into self.disabled_funcs[lib].

        Called before update_func_items() (library switch) and
        before grid close (apply/cancel) to persist per-library
        function choices. [R3-MF3 fix]
        """
        if self.grid is None:
            return
        lib = self.grid.selected_lib
        if lib is None:
            return
        if self.disabled_funcs is None:
            self.disabled_funcs = {}
        disabled = {i["name"] for i in self.grid.func_items
                    if not i["enabled"]}
        # Store the set for this library, even if empty.
        # An empty set means "user visited this library and
        # enabled everything" -- distinct from the library
        # having no entry (never visited). [R5-SF2 fix]
        self.disabled_funcs[lib] = disabled
        # Do NOT collapse empty dict to None. {} means "grid
        # was used, no functions disabled" which suppresses
        # noise defaults on grid reopen. None means "grid
        # never used, let noise defaults apply." [R5-SF2 fix]

    def _enter_toggle_grid(self):
        """Show the toggle grid."""
        # Determine which library's functions to show initially.
        # Use the most common library observed so far.
        initial_lib = None
        initial_funcs = {}
        if self.discovered_libs:
            initial_lib = max(self.discovered_libs,
                              key=self.discovered_libs.get)
            initial_funcs = self.discovered_funcs.get(initial_lib, {})

        from .trace_grid import ToggleGrid
        self.grid = ToggleGrid(
            self.discovered_libs, initial_funcs,
            self.discovered_procs, initial_lib=initial_lib,
            daemon_disabled_funcs=self.daemon_disabled_funcs,
            tier_level=self.current_tier)
        if initial_lib:
            # C5 fix (R4): Set focused_lib_index to match initial lib
            for i, item in enumerate(self.grid.lib_items):
                if item["name"] == initial_lib:
                    self.grid.focused_lib_index = i
                    self.grid.cursor_pos[0] = i
                    break

        # Snapshot disabled_funcs so cancel can restore.
        # _save_func_state() writes to disabled_funcs during navigation
        # and on close, which would leak cancelled changes into
        # _passes_client_filter().
        self._pre_grid_disabled_funcs = (
            copy.deepcopy(self.disabled_funcs)
            if self.disabled_funcs is not None else None)

        # Snapshot noise state for cancel restoration.
        self._pre_grid_noise_suppressed = set(self.noise_suppressed)

        self._restore_grid_state()
        self.grid_visible = True
        self.grid.render(self.term, self.cw)
        self._draw_hotkey_bar()

    def _handle_grid_key(self, key):
        """Handle keypress while toggle grid is visible."""
        if key == "\r" or key == "\n":  # Enter: apply and return
            self._save_func_state()  # [R3-MF3 fix]
            self._pre_grid_disabled_funcs = None  # [R6-SF3 fix]
            self._pre_grid_noise_suppressed = None
            self._apply_grid_filters()
            self._update_manual_overrides()

            # Update noise state unconditionally -- noise changes are
            # client-side only and not gated by has_user_changes().
            noise_enabled = self.grid.get_noise_state()
            self.noise_suppressed = _ALL_NOISE_NAMES - noise_enabled

            # Save noise grid state for restoration on next grid open.
            self.disabled_noise = {i["name"] for i in self.grid.noise_items
                                   if not i["enabled"]}

            self.grid_visible = False
            self.grid = None
            self.term.clear_scroll_region()
            self.term.setup_regions()
            self._draw_hotkey_bar()
            if not self.paused:
                self.pause_buffer = []
                self.pause_scroll_pos = 0
                self._rerender_from_scrollback()
            else:
                # Grid closed while paused: rebuild _scroll_snapshot
                # with new filter state so scroll-back view is consistent.
                # _scroll_pause_buffer() filters pause_buffer internally
                # and clamps pause_scroll_pos, so just set a large value
                # to scroll to the bottom.
                self._scroll_snapshot = self._build_filtered_snapshot()
                self.pause_scroll_pos = len(self._scroll_snapshot) + \
                    len(self.pause_buffer)
                self._init_highlight_at_bottom()
                self._scroll_pause_buffer(0)
            return

        if key in ("A", "a"):
            self.grid.all_on()
        elif key in ("N", "n"):
            self.grid.none()
        elif isinstance(key, tuple) and key[0] == "esc":
            seq = key[1]
            if seq == "":  # Bare Escape: cancel without applying
                self._save_func_state()  # [R3-MF3 fix]
                # [R6-SF3 fix]: Restore disabled_funcs to pre-grid
                # state. Navigation saves are for internal grid
                # consistency; cancel means "don't apply to live
                # filtering."
                self.disabled_funcs = self._pre_grid_disabled_funcs
                self._pre_grid_disabled_funcs = None
                # Restore noise state (cancel = no noise changes)
                self.noise_suppressed = self._pre_grid_noise_suppressed
                self._pre_grid_noise_suppressed = None
                self.grid_visible = False
                self.grid = None
                self.term.clear_scroll_region()
                self.term.setup_regions()
                self._draw_hotkey_bar()
                if not self.paused:
                    self.pause_buffer = []
                    self.pause_scroll_pos = 0
                    self._rerender_from_scrollback()
                else:
                    # Re-render pause scroll view (snapshot unchanged
                    # since Escape = no filter changes)
                    self._init_highlight_at_bottom()
                    self._scroll_pause_buffer(0)
                return
            elif seq == "[D":    # Left arrow
                self.grid.active_category = max(
                    0, self.grid.active_category - 1)
            elif seq == "[C":  # Right arrow
                # Sync focused_lib_index with LIBRARIES cursor
                self.grid.focused_lib_index = self.grid.cursor_pos[0]
                new_cat = min(
                    len(self.grid.categories) - 1,
                    self.grid.active_category + 1)
                self.grid.active_category = new_cat
                # If switching to FUNCTIONS, update with the
                # currently selected library's function list
                if self.grid.categories[new_cat] == "FUNCTIONS":
                    lib = self._get_selected_lib_name()
                    if lib and lib in self.discovered_funcs:
                        # [R3-MF3 fix]: Save current library's
                        # disabled funcs before switching.
                        self._save_func_state()
                        self.grid.update_func_items(
                            self.discovered_funcs[lib], lib)
                        # [R3-MF3 fix]: Restore new library's state.
                        # Only override daemon-disabled defaults when
                        # the library was previously visited (has an
                        # entry in disabled_funcs). Unvisited libraries
                        # keep their daemon-disabled defaults from
                        # update_func_items() -> _apply_noise_defaults().
                        if self.disabled_funcs is not None \
                                and lib in self.disabled_funcs:
                            disabled_for_lib = \
                                self.disabled_funcs[lib]
                            for item in self.grid.func_items:
                                item["enabled"] = (
                                    item["name"]
                                    not in disabled_for_lib)
                        self.grid.clamp_cursor(1)  # FUNCTIONS
            elif seq == "[A":  # Up arrow
                avail = self.grid._available_item_rows(self.term.rows)
                self.grid.move_cursor(-1, visible_rows=avail)
            elif seq == "[B":  # Down arrow
                avail = self.grid._available_item_rows(self.term.rows)
                self.grid.move_cursor(1, visible_rows=avail)
            elif seq == "[5~":  # Page Up
                avail = self.grid._available_item_rows(self.term.rows)
                self.grid.move_cursor(-avail, visible_rows=avail)
            elif seq == "[6~":  # Page Down
                avail = self.grid._available_item_rows(self.term.rows)
                self.grid.move_cursor(avail, visible_rows=avail)
        elif key == " ":  # Space: toggle at cursor
            if self.grid.categories[self.grid.active_category] \
                    == "LIBRARIES":
                self.grid.focused_lib_index = \
                    self.grid.cursor_pos[self.grid.active_category]
            self.grid.toggle_at_cursor()

        # Re-render
        self.grid.render(self.term, self.cw)

    def _apply_grid_filters(self):
        """Send FILTER command based on toggle grid state (M5 fix).

        Uses conn.send_filter(raw=...) instead of calling
        send_command() directly. This goes through the proper API
        and does not depend on conn._sock internals.

        Only send FILTER when the user has actually
        changed something from the grid's initial state. Opening and
        immediately closing the grid (with noise defaults) is a no-op.
        This prevents noise defaults from replacing initial TRACE START
        filters (e.g. LIB=dos).
        """
        # [R5-SF1 fix]: Compound guard. Proceed if the grid has user
        # changes (lib/proc toggles, current-library func toggles) OR
        # if disabled_funcs has per-library state from prior navigation
        # (captured by _save_func_state() during library switches).
        has_changes = self.grid.has_user_changes()
        has_func_state = self.disabled_funcs is not None

        # Server-side: send FILTER when grid changed or func state
        if has_changes or has_func_state:
            filter_cmd = self.grid.build_filter_command()

            # [R3-MF3 fix]: Build FUNC= filter from all libraries'
            # disabled functions, not just the grid's current
            # func_items. Uses lib.func format for daemon
            # disambiguation.
            if self.disabled_funcs:
                # Collect all disabled functions across all libraries,
                # using dotted lib.func format for daemon disambiguation.
                # disabled_funcs values are set[str] of bare names;
                # dotting is done here at send time.
                all_disabled_funcs = set()
                for lib_name, lib_funcs in self.disabled_funcs.items():
                    for fn in lib_funcs:
                        all_disabled_funcs.add(
                            "{}.{}".format(lib_name, fn))

                if all_disabled_funcs:
                    # Remove any FUNC= or -FUNC= the grid generated
                    parts = filter_cmd.split()
                    parts = [p for p in parts
                             if not p.startswith("FUNC=")
                             and not p.startswith("-FUNC=")]
                    # Add comprehensive -FUNC= blacklist
                    parts.append(
                        "-FUNC=" + ",".join(
                            sorted(all_disabled_funcs)))
                    filter_cmd = " ".join(parts)

            # Fix 3: Send ENABLE/DISABLE for daemon-disabled functions
            # that the user has toggled. Scans ALL libraries in
            # disabled_funcs (not just the current grid view) so that
            # toggles made in previously-viewed libraries are sent.
            # Uses user_enabled_funcs to track what the user has
            # explicitly enabled at the daemon level.
            enable_funcs = []
            disable_funcs = []

            if self.disabled_funcs:
                for lib_name, disabled_set in self.disabled_funcs.items():
                    funcs_for_lib = self.discovered_funcs.get(
                        lib_name, {})
                    for fname in funcs_for_lib:
                        qualified = "{}.{}".format(lib_name, fname)
                        is_disabled = fname in disabled_set
                        if not is_disabled and \
                                qualified in self.daemon_disabled_funcs:
                            # User wants it ON but daemon has it
                            # OFF -> ENABLE
                            enable_funcs.append(fname)
                        elif is_disabled and \
                                qualified in self.user_enabled_funcs:
                            # User previously enabled it, now wants
                            # it OFF -> DISABLE
                            disable_funcs.append(fname)

            if enable_funcs:
                filter_cmd += " ENABLE=" + ",".join(enable_funcs)
                # Update tracking (use "lib.func" format)
                for f in enable_funcs:
                    for lib_name in self.disabled_funcs:
                        qualified = "{}.{}".format(lib_name, f)
                        if qualified in self.daemon_disabled_funcs:
                            self.daemon_disabled_funcs.discard(
                                qualified)
                            self.user_enabled_funcs.add(qualified)

            if disable_funcs:
                filter_cmd += " DISABLE=" + ",".join(disable_funcs)
                # Update tracking (use "lib.func" format)
                for f in disable_funcs:
                    for lib_name in self.disabled_funcs:
                        qualified = "{}.{}".format(lib_name, f)
                        if qualified in self.user_enabled_funcs:
                            self.daemon_disabled_funcs.add(qualified)
                            self.user_enabled_funcs.discard(
                                qualified)

            if filter_cmd:
                if self.errors_filter:
                    filter_cmd += " ERRORS"
                self.conn.send_filter(raw=filter_cmd)
            elif self.errors_filter:
                self.conn.send_filter(raw="ERRORS")

        # Client-side: update disabled_* sets.
        # Skip only if BOTH conditions are false (no grid changes AND
        # no per-library function state).
        if not has_changes and not has_func_state:
            return

        # Track explicitly disabled items for allow-unknown semantics.
        # [MF2 fix]: disabled_* is the sole source of truth for both
        # filtering and grid state restoration.
        disabled_libs = {i["name"] for i in self.grid.lib_items
                         if not i["enabled"]}
        if not disabled_libs:
            self.disabled_libs = None  # nothing disabled = no filter
        else:
            self.disabled_libs = disabled_libs

        # [R3-MF3 fix]: Function state is per-library.
        # _save_func_state() was already called before this point
        # (in _handle_grid_key()), so disabled_funcs already contains
        # the current library's state. No additional work needed here.

        disabled_procs = {i["name"] for i in self.grid.proc_items
                          if not i["enabled"]}
        if not disabled_procs:
            self.disabled_procs = None
        else:
            self.disabled_procs = disabled_procs

    def _get_selected_lib_name(self):
        """Get the currently focused library name from the grid.

        C5 fix (R4): Uses the focused_lib_index cursor instead of
        picking the first enabled library. When the user arrows
        through the LIBRARIES column, focused_lib_index tracks which
        library they were looking at. This ensures the FUNCTIONS
        column shows functions for the library the user actually
        focused on, not an arbitrary one.
        """
        if not self.grid or not self.grid.lib_items:
            return None
        idx = self.grid.focused_lib_index
        if 0 <= idx < len(self.grid.lib_items):
            return self.grid.lib_items[idx]["name"]
        return self.grid.lib_items[0]["name"]

    # ---- Signal handlers ----

    def _handle_sigwinch(self, signum, frame):
        """Handle terminal resize signal.

        Sets a flag for deferred processing in the event loop.
        Does NOT do any I/O directly (signal safety).
        """
        self._resize_pending = True

    def _handle_resize(self):
        """Process a deferred SIGWINCH resize.

        Called from the event loop when _resize_pending is True.
        Safe to do I/O here (we are between select() iterations).

        Only recreate ColumnLayout if self.layout already exists.
        If layout has not been initialized, _display_event() uses the
        non-adaptive format_trace_event() fallback path. Creating a
        ColumnLayout prematurely would switch _display_event() to the
        adaptive path before required dependencies are available.
        """
        self.term._update_size()
        if self.term.rows < 5:
            # Terminal too short; reset DECSTBM to avoid invalid sequences
            self.term._write("\033[r")
            return
        if hasattr(self, 'layout'):
            self.layout = ColumnLayout(self.term.cols)
        self.term.setup_regions()
        self._draw_status_bar()
        if not self.detail_visible:
            self._draw_header()
        self._draw_hotkey_bar()
        if self.detail_visible:
            self._detail_lines = self._build_detail_lines(
                self._detail_event)
            self._detail_scroll_pos = 0
            self._render_detail()
        if self.grid_visible:
            self.grid.render(self.term, self.cw)

    # ---- Utility methods ----

    def _elapsed_str(self):
        """Format elapsed time since the first event as +M:SS.t.

        Returns a string like "+0:05.2" or "+1:23.4".
        If no events have been received, returns "+0:00.0".
        Uses microsecond precision internally for correct arithmetic.
        """
        if self.start_time is None or self.last_event_time is None:
            return "+0:00.0"
        us = TraceViewer._parse_time_us(self.last_event_time) - \
            TraceViewer._parse_time_us(self.start_time)
        if us < 0:
            us += 24 * 3600 * 1000000  # midnight wraparound
        total_secs = us // 1000000
        tenths = (us % 1000000) // 100000
        mins = total_secs // 60
        secs = total_secs % 60
        return "+{}:{:02d}.{}".format(mins, secs, tenths)

    @staticmethod
    def _parse_time_us(timestr):
        """Parse HH:MM:SS.ffffff to total microseconds.

        Handles variable-precision fractional seconds:
        - 6 digits (us): "12:34:56.123456" -> 123456 us
        - 3 digits (ms): "12:34:56.123" -> padded to 123000 us
        - No fraction: "12:34:56" -> 0 us
        """
        try:
            parts = timestr.split(":")
            h = int(parts[0])
            m = int(parts[1])
            sec_parts = parts[2].split(".")
            s = int(sec_parts[0])
            if len(sec_parts) > 1:
                frac = sec_parts[1].ljust(6, '0')[:6]
                us = int(frac)
            else:
                us = 0
            return ((h * 3600 + m * 60 + s) * 1000000) + us
        except (ValueError, IndexError):
            return 0

    @staticmethod
    def _parse_time(timestr):
        """Parse HH:MM:SS.fff... to total milliseconds (backward compat).

        Delegates to _parse_time_us() and truncates to milliseconds.
        """
        return TraceViewer._parse_time_us(timestr) // 1000

    @staticmethod
    def _time_diff(t1, t2):
        """Compute time difference as +S.uuuuuu string.

        Handles midnight wraparound (C4 fix): if the difference is
        negative, add 24 hours. Uses microsecond precision.
        """
        us1 = TraceViewer._parse_time_us(t1)
        us2 = TraceViewer._parse_time_us(t2)
        diff = us2 - us1
        if diff < 0:
            diff += 24 * 3600 * 1000000  # midnight wraparound
        secs = diff // 1000000
        micros = diff % 1000000
        return "+{}.{:06d}".format(secs, micros)
