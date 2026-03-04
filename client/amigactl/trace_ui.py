"""Interactive trace event viewer using ANSI escape sequences.

Provides a terminal UI for streaming trace events from atrace.
Uses DECSTBM (DEC Set Top and Bottom Margins) scroll regions for
a three-region layout: status bar, scroll region, hotkey bar.

No external TUI libraries (curses, etc.) -- pure ANSI escape codes.
"""

import atexit
import copy
from collections import deque
import os
import re
import select
import signal
import socket
import sys
import time as _time
import tty

try:
    import termios
except ImportError:
    termios = None  # Windows -- not supported for interactive viewer

from .colors import RESET, format_trace_event, get_lib_color
from .protocol import ProtocolError, send_command


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

    def setup_regions(self):
        """Configure DECSTBM three-region layout.

        Line 1:          Status bar (fixed)
        Lines 2..rows-1: Scroll region (events)
        Line rows:        Hotkey bar (fixed)
        """
        # Set scroll region: rows 2 through rows-1
        self._write("\033[2;{}r".format(self.rows - 1))
        # Move cursor into scroll region
        self._write("\033[2;1H")

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
        for row in range(2, self.rows):
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
            parts.append(time_str.rjust(self.time_width))
        parts.append(_pad(lib_func_colored, self.func_width))
        parts.append(_pad(proc_colored, self.proc_width))
        parts.append(_pad(args, self.args_width))
        parts.append(retval_colored)

        return "  ".join(parts)


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

        # Scrollback buffer: retains displayed events for scroll-back
        # when paused. Populated by _display_event(). [SF4 fix]: Uses
        # deque(maxlen=N) for O(1) append with automatic size limiting.
        self.scrollback_limit = 1000
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

        # Grid state
        self.grid = None             # ToggleGrid instance (when used)

        # Resize handling (C7 fix)
        self._resize_pending = False

        # Status bar dirty flag (C2 fix, R4): avoid redrawing
        # the status bar 10x/sec when nothing has changed.
        self._status_dirty = True
        self._last_status_time = 0.0  # monotonic time of last redraw

    def run(self):
        """Main entry point. Sets up terminal and runs event loop."""
        with TerminalState() as term:
            self.term = term
            # Install SIGWINCH handler
            old_sigwinch = signal.signal(
                signal.SIGWINCH, self._handle_sigwinch)
            try:
                self.layout = ColumnLayout(term.cols)
                term.setup_regions()
                self._draw_status_bar()
                self._draw_hotkey_bar()
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
            if not self.grid_visible and not self.help_visible:
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
            # Comments always displayed (overflow warnings, etc.)
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

        # Grid mode: buffer events to prevent scroll region corruption
        if self.grid_visible:
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

        if self.help_visible:
            # Any key dismisses help
            self.help_visible = False
            self.term.clear_scroll_region()
            self.term.setup_regions()
            self._draw_hotkey_bar()
            return

        if self.grid_visible:
            self._handle_grid_key(key)
            return

        # Scroll-back when paused (S4)
        if self.paused and isinstance(key, tuple) and key[0] == "esc":
            seq = key[1]
            if seq == "[A":    # Up arrow
                self._scroll_pause_buffer(-1)
                return
            elif seq == "[B":  # Down arrow
                self._scroll_pause_buffer(1)
                return
            elif seq == "[5~":  # Page Up
                self._scroll_pause_buffer(-(self.term.rows - 3))
                return
            elif seq == "[6~":  # Page Down
                self._scroll_pause_buffer(self.term.rows - 3)
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

    def _toggle_errors_filter(self):
        """Toggle ERRORS filter on/off (S7: replaces grid RETURN STATUS)."""
        self.errors_filter = not self.errors_filter
        self._status_dirty = True  # C2 fix
        # Send updated filter to daemon
        self._send_current_filter()
        self._draw_status_bar()
        self._draw_hotkey_bar()

    # ---- Display methods ----

    def _display_event(self, event):
        """Format and display a single trace event in the scroll region.

        Before Wave 6 (adaptive layout), uses format_trace_event()
        from colors.py. After Wave 6, uses ColumnLayout.format_event()
        with a pre-formatted timestamp.

        The viewer formats the timestamp (which depends on viewer state:
        timestamp_mode, start_time, last_event_time) before passing to
        the layout engine. This avoids the layout engine needing access
        to viewer state (M2 fix).
        """
        # Retain event in scrollback for pause-time scrolling
        self.scrollback.append(event)
        # [R3-SF10 fix]: Track when scrollback reaches capacity
        if len(self.scrollback) >= self.scrollback_limit:
            self._scrollback_full = True

        if hasattr(self, 'layout'):
            # Wave 6: adaptive layout with pre-formatted timestamp
            time_str = self._format_timestamp(event)
            formatted = self.layout.format_event(
                event, self.cw, time_str=time_str)
        else:
            # Waves 3-5: use existing format_trace_event()
            formatted = format_trace_event(event, self.cw)
        self.term.write_event(formatted)

    def _display_comment(self, event):
        """Display a comment event (e.g. overflow warnings).

        Comments are always shown regardless of filters or pause
        state. They are formatted in warning color (yellow).
        """
        text = event.get("text", "")
        self.term.write_event(self.cw.warning("# {}".format(text)))

    # ---- Status and hotkey bars ----

    def _draw_status_bar(self):
        """Render the status bar (top line) based on current mode.

        Dispatches to the appropriate content builder based on state:
        - stats_mode: show per-function call counts
        - paused: show pause position and buffer info
        - default: show event counts, active filters, elapsed time
        """
        if self.stats_mode:
            text = self._build_stats_text()
        elif self.paused:
            snapshot_len = (len(self._scroll_snapshot)
                            if self._scroll_snapshot else 0)
            combined_len = snapshot_len + len(self.pause_buffer)
            if combined_len > 0:
                pos = self.pause_scroll_pos + 1
                visible_lines = self.term.rows - 3
                new_count = max(0, combined_len - (
                    self.pause_scroll_pos + visible_lines))
                text = "PAUSED | line {}/{} | {} new".format(
                    pos, combined_len, new_count)
                if combined_len >= (self.scrollback_limit +
                                    self.pause_buffer_limit):
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
            if self.search_pattern:
                filter_parts.append(
                    'search: "{}"'.format(self.search_pattern))

            elapsed = self._elapsed_str()
            parts = ["TRACE: " + shown_text]
            if filter_parts:
                parts.append(" | ".join(filter_parts))
            parts.append(elapsed)
            text = " | ".join(parts)

        self.term.write_status_bar(text)

    def _draw_hotkey_bar(self):
        """Render the hotkey bar (bottom line).

        Delegates to _build_hotkey_bar() for content, then writes
        to the terminal's fixed bottom line.
        """
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

        full = ("  [Tab] filters  [/] search  {}  {}  "
                "{}  [t] time  [?] help  [q] quit").format(
                    pause_text, stats_text, errors_text)

        if len(full) <= self.term.cols:
            return full

        # Abbreviated
        short = "[Tab]filt [/]srch {} {} {} [t] [?] [q]".format(
            "[p]PAUS" if self.paused else "[p]",
            "[s]STAT" if self.stats_mode else "[s]",
            "[e]ERR" if self.errors_filter else "[e]")

        if len(short) <= self.term.cols:
            return short

        # Minimal
        return "[Tab] [/] [p] [s] [e] [t] [?] [q]"

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

        return True

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
            # Unpause: catch up on buffered events
            self.paused = False
            self._scroll_snapshot = None  # [R3-SF7 fix]: clear snapshot
            buf = self.pause_buffer
            self.pause_buffer = []
            self.pause_scroll_pos = 0
            if len(buf) > 0:
                # Show catch-up indicator
                self.term.write_event(
                    self.cw.warning(
                        "# {} buffered events".format(len(buf))))
                last_replayed_time = None
                for event in buf:
                    # [R2-SF2 fix]: Re-filter through current filters.
                    # Filters may have changed while paused (user opened
                    # grid, changed filters, applied).
                    if not self._passes_client_filter(event):
                        continue
                    if self.search_pattern:
                        formatted = format_trace_event(event, self.cw)
                        if self.search_pattern.lower() not in \
                           formatted.lower():
                            continue
                    self.shown_events += 1
                    self._display_event(event)
                    last_replayed_time = event.get("time", "")
                # [R3-MF2 fix]: Update last_event_time after replay so
                # the next live event's delta is relative to the last
                # replayed event, not whatever stale value was before
                # the pause.
                if last_replayed_time is not None:
                    self.last_event_time = last_replayed_time
        else:
            self.paused = True
            self.pause_buffer = []
            # [R3-SF7 fix]: Snapshot scrollback for stable iteration
            self._scroll_snapshot = list(self.scrollback)
            # [R3-SF8 fix]: Position at bottom so user sees latest
            visible_lines = self.term.rows - 3
            total = len(self._scroll_snapshot)
            self.pause_scroll_pos = max(0, total - visible_lines)
        self._status_dirty = True  # C2 fix
        self._draw_status_bar()
        self._draw_hotkey_bar()

    def _scroll_pause_buffer(self, delta):
        """Scroll through combined scrollback snapshot and pause buffer.

        [R3-SF7 fix]: Uses _scroll_snapshot (frozen on pause) +
        pause_buffer (live arrivals) to avoid mutation during render.
        [R3-MF1 fix]: Delta timestamps are computed sequentially
        across the visible window, not from stale last_event_time.
        """
        snapshot = self._scroll_snapshot or []
        combined = snapshot + self.pause_buffer
        if not combined:
            return

        visible_lines = self.term.rows - 3
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
            row = 2 + (i - start)

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
            if hasattr(self, 'layout'):
                time_str = self._format_timestamp_for_scroll(
                    event, prev_time)
                formatted = self.layout.format_event(
                    event, self.cw, time_str=time_str)
            else:
                formatted = format_trace_event(event, self.cw)
            self.term.write_at(row, formatted)
            prev_time = event.get("time", "")

        self._draw_status_bar()

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
            return raw_time
        elif self.timestamp_mode == "relative":
            if self.start_time is None:
                return "+0.000"
            return self._time_diff(self.start_time, raw_time)
        elif self.timestamp_mode == "delta":
            if self.last_event_time is None:
                return "+0.000"
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
            return raw_time
        elif self.timestamp_mode == "relative":
            if self.start_time is None:
                return "+0.000"
            return self._time_diff(self.start_time, raw_time)
        elif self.timestamp_mode == "delta":
            if prev_time is None:
                return "+0.000"
            return self._time_diff(prev_time, raw_time)
        return raw_time

    def _show_help(self):
        """Show the help overlay in the scroll region."""
        self.help_visible = True
        self.term.clear_scroll_region()

        help_text = [
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
            "    ?       Toggle this help screen",
            "    q       Stop trace and exit viewer",
            "",
            "  While paused:",
            "    Up/Down     Scroll through buffered events",
            "    PgUp/PgDn   Scroll one page",
            "",
            "  While in filter grid:",
            "    1-9, a-z    Toggle individual items",
            "    A           Enable all in current category",
            "    N           Disable all in current category",
            "    Left/Right  Switch category",
            "    Enter       Apply filters and return to stream",
            "",
            "  Process filtering is client-side only (C5).",
            "  Press any key to dismiss this help.",
        ]

        # Render into scroll region area
        for i, line in enumerate(help_text):
            row = i + 2
            if row >= self.term.rows - 1:
                break
            self.term.write_at(row, line)

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
            self.discovered_procs, initial_lib=initial_lib)
        if initial_lib:
            # C5 fix (R4): Set focused_lib_index to match initial lib
            for i, item in enumerate(self.grid.lib_items):
                if item["name"] == initial_lib:
                    self.grid.focused_lib_index = i
                    break

        # Snapshot disabled_funcs so cancel can restore.
        # _save_func_state() writes to disabled_funcs during navigation
        # and on close, which would leak cancelled changes into
        # _passes_client_filter().
        self._pre_grid_disabled_funcs = (
            copy.deepcopy(self.disabled_funcs)
            if self.disabled_funcs is not None else None)

        self._restore_grid_state()
        self.grid_visible = True
        self.grid.render(self.term, self.cw)

    def _handle_grid_key(self, key):
        """Handle keypress while toggle grid is visible."""
        if key == "\r" or key == "\n":  # Enter: apply and return
            self._save_func_state()  # [R3-MF3 fix]
            self._pre_grid_disabled_funcs = None  # [R6-SF3 fix]
            self._apply_grid_filters()
            self.grid_visible = False
            self.grid = None
            self.term.clear_scroll_region()
            self.term.setup_regions()
            self._draw_hotkey_bar()
            # Replay events buffered while grid was visible
            # [SF7 fix]: Only replay if NOT paused.
            if not self.paused:
                buf = self.pause_buffer
                self.pause_buffer = []
                self.pause_scroll_pos = 0
                if buf:
                    last_replayed_time = None
                    for event in buf:
                        # [MF3 fix]: Re-filter events through
                        # current filters.
                        if not self._passes_client_filter(event):
                            continue
                        if self.search_pattern:
                            formatted = format_trace_event(
                                event, self.cw)
                            if self.search_pattern.lower() not in \
                               formatted.lower():
                                continue
                        self.shown_events += 1
                        self._display_event(event)
                        last_replayed_time = event.get("time", "")
                    # [R3-MF2 fix]: Update last_event_time after
                    # replay so the next live event's delta is
                    # relative to the last replayed event.
                    if last_replayed_time is not None:
                        self.last_event_time = last_replayed_time
            return

        if key == "A":
            self.grid.all_on()
        elif key == "N":
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
                self.grid_visible = False
                self.grid = None
                self.term.clear_scroll_region()
                self.term.setup_regions()
                self._draw_hotkey_bar()
                # Replay events buffered while grid was visible
                # [SF7 fix]: Only replay if NOT paused.
                if not self.paused:
                    buf = self.pause_buffer
                    self.pause_buffer = []
                    self.pause_scroll_pos = 0
                    if buf:
                        last_replayed_time = None
                        for event in buf:
                            if not self._passes_client_filter(event):
                                continue
                            if self.search_pattern:
                                formatted = format_trace_event(
                                    event, self.cw)
                                if self.search_pattern.lower() \
                                   not in formatted.lower():
                                    continue
                            self.shown_events += 1
                            self._display_event(event)
                            last_replayed_time = event.get(
                                "time", "")
                        if last_replayed_time is not None:
                            self.last_event_time = \
                                last_replayed_time
                return
            elif seq == "[D":    # Left arrow
                self.grid.active_category = max(
                    0, self.grid.active_category - 1)
            elif seq == "[C":  # Right arrow
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
                        if self.disabled_funcs is not None:
                            disabled_for_lib = \
                                self.disabled_funcs.get(
                                    lib, set())
                            for item in self.grid.func_items:
                                item["enabled"] = (
                                    item["name"]
                                    not in disabled_for_lib)
        elif isinstance(key, str) and len(key) == 1:
            # Check if toggling a library in LIBRARIES category
            if self.grid.categories[self.grid.active_category] \
                    == "LIBRARIES":
                # C5 fix (R4): Update focused_lib_index to the item
                # the user just interacted with, so FUNCTIONS column
                # shows that library's functions when they navigate
                # right.
                keys = "123456789abcdefghijklmnopqrstuvwxyz"
                idx = keys.find(key.lower())
                if 0 <= idx < len(self.grid.lib_items):
                    self.grid.focused_lib_index = idx
                self.grid.toggle_item(key)
            else:
                self.grid.toggle_item(key)

        # Re-render
        self.grid.render(self.term, self.cw)

    def _apply_grid_filters(self):
        """Send FILTER command based on toggle grid state (M5 fix).

        Uses conn.send_filter(raw=...) instead of calling
        send_command() directly. This goes through the proper API
        and does not depend on conn._sock internals.

        S5 fix (Wave 5): Only send FILTER when the user has actually
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
            # func_items.
            if self.disabled_funcs:
                # Collect all disabled functions across all libraries
                all_disabled_funcs = set()
                for lib_funcs in self.disabled_funcs.values():
                    all_disabled_funcs.update(lib_funcs)

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

        S3 fix (R4): Only recreate ColumnLayout if self.layout already
        exists. During Waves 3-5 (before Wave 6 initializes self.layout
        in __init__), self.layout does not exist and _display_event()
        uses the non-adaptive format_trace_event() path. Creating a
        ColumnLayout here during those waves would switch _display_event()
        to the adaptive path, which depends on Wave 6 code (e.g.,
        get_lib_color()) that has not been written yet.
        """
        self.term._update_size()
        if hasattr(self, 'layout'):
            self.layout = ColumnLayout(self.term.cols)
        self.term.setup_regions()
        self._draw_status_bar()
        self._draw_hotkey_bar()
        if self.grid_visible:
            self.grid.render(self.term, self.cw)

    # ---- Utility methods ----

    def _elapsed_str(self):
        """Format elapsed time since the first event as +M:SS.m.

        Returns a string like "+0:05.2" or "+1:23.4".
        If no events have been received, returns "+0:00.0".
        """
        if self.start_time is None or self.last_event_time is None:
            return "+0:00.0"
        ms = TraceViewer._parse_time(self.last_event_time) - \
            TraceViewer._parse_time(self.start_time)
        if ms < 0:
            ms += 24 * 3600 * 1000  # midnight wraparound
        total_secs = ms // 1000
        tenths = (ms % 1000) // 100
        mins = total_secs // 60
        secs = total_secs % 60
        return "+{}:{:02d}.{}".format(mins, secs, tenths)

    @staticmethod
    def _parse_time(timestr):
        """Parse HH:MM:SS.mmm to total milliseconds."""
        try:
            parts = timestr.split(":")
            h, m = int(parts[0]), int(parts[1])
            sec_parts = parts[2].split(".")
            s = int(sec_parts[0])
            ms = int(sec_parts[1]) if len(sec_parts) > 1 else 0
            return ((h * 3600 + m * 60 + s) * 1000) + ms
        except (ValueError, IndexError):
            return 0

    @staticmethod
    def _time_diff(t1, t2):
        """Compute time difference as +S.mmm string.

        Handles midnight wraparound (C4 fix): if the difference is
        negative, add 24 hours.
        """
        ms1 = TraceViewer._parse_time(t1)
        ms2 = TraceViewer._parse_time(t2)
        diff = ms2 - ms1
        if diff < 0:
            diff += 24 * 3600 * 1000  # midnight wraparound
        secs = diff // 1000
        millis = diff % 1000
        return "+{}.{:03d}".format(secs, millis)
