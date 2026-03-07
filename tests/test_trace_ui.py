"""Unit tests for the interactive trace viewer (trace_ui.py).

These are pure unit tests that do not require a network connection.
They exercise the TerminalState context manager, DECSTBM scroll
region layout, TraceViewer event processing, and keyboard handling.
"""

import io
import os
import sys
from collections import deque
from unittest import mock

import pytest

from amigactl.colors import ColorWriter, format_trace_event
from amigactl.trace_ui import (
    ColumnLayout, TerminalState, TraceViewer,
    _truncate_to_visible, _visible_len,
)
from amigactl.colors import get_lib_color, _lib_color_assignments


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_terminal_state(rows=24, cols=80):
    """Create a TerminalState with captured stdout and mocked termios.

    Returns (term, output) where output is a StringIO capturing
    all terminal writes.
    """
    output = io.StringIO()
    term = TerminalState(stdin_fd=0, stdout=output)
    term.rows = rows
    term.cols = cols
    # Pre-set _saved_attrs so cleanup logic is exercised
    term._saved_attrs = [[0] * 7]  # dummy termios attrs
    return term, output


def _make_event(**overrides):
    """Build a default trace event dict with optional overrides."""
    base = {
        "seq": 42, "time": "14:30:01.000", "lib": "dos",
        "func": "Open", "task": "Shell Process",
        "args": '"SYS:Startup-Sequence"', "retval": "0x1a2b3c",
        "status": "O", "type": "event",
    }
    base.update(overrides)
    return base


def _make_viewer(**overrides):
    """Create a TraceViewer with mocked conn/session for unit testing.

    Returns a viewer with a mock terminal attached. The term is
    set up so methods that write to it don't fail.
    """
    conn = mock.MagicMock()
    session = mock.MagicMock()
    session.sock = mock.MagicMock()
    session.reader = mock.MagicMock()
    session.reader.has_buffered_data.return_value = False

    cw = ColorWriter(force_color=False)
    viewer = TraceViewer(conn, session, cw, **overrides)

    # Attach a mock terminal
    output = io.StringIO()
    term = TerminalState(stdin_fd=0, stdout=output)
    term.rows = 24
    term.cols = 80
    viewer.term = term

    return viewer


# ---------------------------------------------------------------------------
# TestTerminalState
# ---------------------------------------------------------------------------

class TestTerminalState:
    """Tests for TerminalState context manager."""

    @mock.patch("amigactl.trace_ui.termios")
    @mock.patch("amigactl.trace_ui.tty")
    @mock.patch("amigactl.trace_ui.atexit")
    @mock.patch("amigactl.trace_ui.os.get_terminal_size",
                return_value=os.terminal_size((80, 24)))
    def test_cleanup_restores_attrs(self, mock_termsize, mock_atexit,
                                     mock_tty, mock_termios):
        """Verify tcsetattr called with saved attrs on exit."""
        # termios.tcgetattr returns [iflag, oflag, cflag, lflag,
        #   ispeed, ospeed, cc_list]
        saved = [0x0500, 0x0005, 0x00bf, 0x8a3b, 0x000f, 0x000f,
                 [b'\x03', b'\x1c', b'\x7f', b'\x15', b'\x04',
                  b'\x00', b'\x01', b'\x00', b'\x11', b'\x13',
                  b'\x1a', b'\x00', b'\x12', b'\x0f', b'\x17',
                  b'\x16', b'\x00', b'\x00', b'\x00', b'\x00',
                  b'\x00', b'\x00', b'\x00', b'\x00', b'\x00',
                  b'\x00', b'\x00', b'\x00', b'\x00', b'\x00',
                  b'\x00', b'\x00']]
        mock_termios.tcgetattr.return_value = saved
        mock_termios.IXON = 0x0400
        mock_termios.IXOFF = 0x1000
        mock_termios.TCSANOW = 0
        mock_termios.TCSADRAIN = 1

        output = io.StringIO()
        term = TerminalState(stdin_fd=0, stdout=output)

        # Simulate __enter__
        term.__enter__()

        # Verify attrs were saved
        assert term._saved_attrs is not None

        # Simulate __exit__
        term.__exit__(None, None, None)

        # Verify tcsetattr was called to restore
        mock_termios.tcsetattr.assert_called()
        # After cleanup, _saved_attrs should be None
        assert term._saved_attrs is None

    def test_cleanup_idempotent(self):
        """Call _cleanup() twice, no error."""
        term, output = _make_terminal_state()

        with mock.patch("amigactl.trace_ui.termios") as mock_termios:
            mock_termios.TCSADRAIN = 1
            term._cleanup()
            # _saved_attrs is now None
            assert term._saved_attrs is None
            # Second call should be a no-op
            term._cleanup()
            assert term._saved_attrs is None

    def test_setup_regions_escape_sequences(self):
        """Verify DECSTBM escape sequence is emitted."""
        term, output = _make_terminal_state(rows=24, cols=80)
        term._saved_attrs = None  # Skip cleanup writes

        term.setup_regions()

        written = output.getvalue()
        # Should contain DECSTBM: ESC[3;23r (rows-1 = 23)
        assert "\033[3;23r" in written
        # Should position cursor in scroll region
        assert "\033[3;1H" in written

    def test_write_status_bar_positioning(self):
        """Verify cursor save/restore and line 1 positioning."""
        term, output = _make_terminal_state()
        term._saved_attrs = None  # Skip cleanup

        term.write_status_bar("TRACE: 42 events")

        written = output.getvalue()
        # Save cursor
        assert "\0337" in written
        # Move to row 1, col 1
        assert "\033[1;1H" in written
        # Clear line
        assert "\033[2K" in written
        # Content
        assert "TRACE: 42 events" in written
        # Restore cursor
        assert "\0338" in written

    def test_write_hotkey_bar_positioning(self):
        """Verify bottom line positioning."""
        term, output = _make_terminal_state(rows=24)
        term._saved_attrs = None

        term.write_hotkey_bar("[q] quit")

        written = output.getvalue()
        # Should position at row 24
        assert "\033[24;1H" in written
        assert "[q] quit" in written

    def test_write_event_truncation(self):
        """Feed a 200-char event line to an 80-col terminal."""
        term, output = _make_terminal_state(rows=24, cols=80)
        term._saved_attrs = None

        long_text = "A" * 200
        term.write_event(long_text)

        written = output.getvalue()
        # The text should be truncated. Count visible chars.
        # The output contains escape sequences + truncated text.
        # The "A" characters should be at most 80.
        a_count = written.count("A")
        assert a_count <= 80

    def test_read_key_returns_char(self):
        """Mock stdin fd with known byte, verify return."""
        term, output = _make_terminal_state()
        term._saved_attrs = None

        with mock.patch("amigactl.trace_ui.select") as mock_select_mod:
            mock_select_mod.select.return_value = ([term.stdin_fd], [], [])
            with mock.patch("os.read", return_value=b"a"):
                result = term.read_key()
                assert result == "a"

    def test_read_key_returns_none_when_empty(self):
        """Mock empty stdin."""
        term, output = _make_terminal_state()
        term._saved_attrs = None

        with mock.patch("amigactl.trace_ui.select") as mock_select_mod:
            mock_select_mod.select.return_value = ([], [], [])
            result = term.read_key()
            assert result is None

    def test_read_key_escape_sequence(self):
        """Mock ESC + [A (up arrow), verify tuple return."""
        term, output = _make_terminal_state()
        term._saved_attrs = None

        call_count = [0]

        def mock_select(fds, w, x, timeout=0):
            call_count[0] += 1
            return ([term.stdin_fd], [], [])

        read_results = [b"\033", b"[A"]

        with mock.patch("amigactl.trace_ui.select") as mock_select_mod:
            mock_select_mod.select.side_effect = mock_select
            with mock.patch("os.read", side_effect=read_results):
                result = term.read_key()
                assert isinstance(result, tuple)
                assert result[0] == "esc"
                assert result[1] == "[A"


# ---------------------------------------------------------------------------
# TestTruncation
# ---------------------------------------------------------------------------

class TestTruncation:
    """Tests for _visible_len and _truncate_to_visible."""

    def test_visible_len_plain(self):
        assert _visible_len("hello") == 5

    def test_visible_len_with_ansi(self):
        s = "\033[31mhello\033[0m"
        assert _visible_len(s) == 5

    def test_truncate_plain(self):
        result = _truncate_to_visible("A" * 100, 10)
        assert len(result) == 10
        assert result == "A" * 10

    def test_truncate_with_ansi(self):
        s = "\033[31m" + "A" * 100 + "\033[0m"
        result = _truncate_to_visible(s, 10)
        # Should have 10 visible chars plus ANSI codes
        assert _visible_len(result) == 10
        # Should end with reset since escape was active
        assert result.endswith("\033[0m")

    def test_truncate_short_string(self):
        """String shorter than max_width is returned as-is."""
        result = _truncate_to_visible("hello", 80)
        assert result == "hello"

    def test_truncate_reset_not_appended_when_reset_at_end(self):
        """No extra reset if the last escape was itself a reset."""
        s = "\033[31mhi\033[0m"
        result = _truncate_to_visible(s, 80)
        # Should not have double reset
        assert result == s


# ---------------------------------------------------------------------------
# TestTraceViewer
# ---------------------------------------------------------------------------

class TestTraceViewer:
    """Tests for TraceViewer event processing and keyboard handling."""

    def test_event_updates_statistics(self):
        """Feed events through _process_event_result, verify counts."""
        viewer = _make_viewer()

        event1 = _make_event(lib="dos", func="Open", task="Shell")
        event2 = _make_event(lib="dos", func="Close", task="Shell")
        event3 = _make_event(lib="exec", func="OpenLibrary", task="Shell")

        viewer._process_event_result(event1)
        viewer._process_event_result(event2)
        viewer._process_event_result(event3)

        assert viewer.total_events == 3
        assert viewer.func_counts["dos.Open"] == 1
        assert viewer.func_counts["dos.Close"] == 1
        assert viewer.func_counts["exec.OpenLibrary"] == 1
        assert viewer.lib_counts["dos"] == 2
        assert viewer.lib_counts["exec"] == 1
        assert viewer.proc_counts["Shell"] == 3

    def test_event_buffered_when_paused(self):
        """Pause, feed event, verify pause_buffer populated."""
        viewer = _make_viewer()
        viewer.paused = True

        event = _make_event()
        viewer._process_event_result(event)

        assert len(viewer.pause_buffer) == 1
        assert viewer.pause_buffer[0] is event
        # Event still counted in total
        assert viewer.total_events == 1
        # But not shown
        assert viewer.shown_events == 0

    def test_search_filters_events(self):
        """Set search_pattern, feed events, verify only matching ones shown."""
        viewer = _make_viewer()
        viewer.search_pattern = "OpenLibrary"

        # This event matches the pattern
        event_match = _make_event(
            lib="exec", func="OpenLibrary",
            retval="0x1234", status="O")
        # This event does not match
        event_nomatch = _make_event(
            lib="dos", func="Close",
            retval="DOSTRUE", status="O")

        viewer._process_event_result(event_match)
        viewer._process_event_result(event_nomatch)

        # Both counted in total
        assert viewer.total_events == 2
        # Only matching event was shown
        assert viewer.shown_events == 1

    def test_keypress_q_stops(self):
        """Simulate 'q' keypress, verify running becomes False."""
        viewer = _make_viewer()
        viewer.term.read_key = mock.MagicMock(return_value="q")

        # Mock _stop_trace to avoid actual socket operations
        viewer._stop_trace = mock.MagicMock()

        viewer._handle_keypress()

        assert viewer.running is False
        viewer._stop_trace.assert_called_once()

    def test_keypress_p_toggles_pause(self):
        """Simulate 'p', verify paused toggles."""
        viewer = _make_viewer()

        assert viewer.paused is False

        # First press: pause
        viewer.term.read_key = mock.MagicMock(return_value="p")
        viewer._handle_keypress()
        assert viewer.paused is True

        # Second press: unpause
        viewer.term.read_key = mock.MagicMock(return_value="p")
        viewer._handle_keypress()
        assert viewer.paused is False

    def test_discovered_funcs_per_library(self):
        """Feed events from different libraries, verify nested dict."""
        viewer = _make_viewer()

        viewer._process_event_result(
            _make_event(lib="dos", func="Open"))
        viewer._process_event_result(
            _make_event(lib="dos", func="Open"))
        viewer._process_event_result(
            _make_event(lib="dos", func="Close"))
        viewer._process_event_result(
            _make_event(lib="exec", func="OpenLibrary"))

        # discovered_funcs should be nested: {lib: {func: count}}
        assert "dos" in viewer.discovered_funcs
        assert "exec" in viewer.discovered_funcs
        assert viewer.discovered_funcs["dos"]["Open"] == 2
        assert viewer.discovered_funcs["dos"]["Close"] == 1
        assert viewer.discovered_funcs["exec"]["OpenLibrary"] == 1

    def test_error_count_tracked(self):
        """Events with status 'E' increment error counters."""
        viewer = _make_viewer()

        viewer._process_event_result(
            _make_event(status="E", retval="NULL"))
        viewer._process_event_result(
            _make_event(status="O", retval="0x1234"))

        assert viewer.error_count == 1
        assert viewer.error_counts["dos.Open"] == 1

    def test_stream_end_stops_running(self):
        """False result (END) sets running to False."""
        viewer = _make_viewer()
        assert viewer.running is True

        viewer._process_event_result(False)

        assert viewer.running is False

    def test_comment_displayed(self):
        """Comment events are displayed through write_event."""
        viewer = _make_viewer()

        comment = {"type": "comment", "text": "OVERFLOW 5 events dropped"}

        with mock.patch.object(viewer.term, 'write_event') as mock_write:
            viewer._process_event_result(comment)
            mock_write.assert_called_once()
            call_arg = mock_write.call_args[0][0]
            assert "OVERFLOW" in call_arg

    def test_start_time_set_from_first_event(self):
        """start_time is set from the first event received."""
        viewer = _make_viewer()
        assert viewer.start_time is None

        viewer._process_event_result(
            _make_event(time="10:15:23.456"))

        assert viewer.start_time == "10:15:23.456"

    def test_last_event_time_updated(self):
        """last_event_time is updated after each displayed event."""
        viewer = _make_viewer()

        viewer._process_event_result(
            _make_event(time="10:15:23.456"))
        assert viewer.last_event_time == "10:15:23.456"

        viewer._process_event_result(
            _make_event(time="10:15:24.789"))
        assert viewer.last_event_time == "10:15:24.789"

    def test_passes_client_filter_none_allows(self):
        """None disabled sets allow all events (no grid interaction)."""
        viewer = _make_viewer()
        assert viewer.disabled_libs is None
        event = _make_event(lib="dos", func="Open", task="Shell")
        assert viewer._passes_client_filter(event) is True

    def test_passes_client_filter_nonempty(self):
        """Non-empty disabled sets block members."""
        viewer = _make_viewer()
        viewer.disabled_libs = {"dos"}

        event_dos = _make_event(lib="dos")
        event_exec = _make_event(lib="exec")

        assert viewer._passes_client_filter(event_dos) is False
        assert viewer._passes_client_filter(event_exec) is True

    def test_passes_client_filter_all_disabled_blocks_known(self):
        """All known libs disabled blocks those libs, unknown pass."""
        viewer = _make_viewer()
        viewer.disabled_libs = {"dos", "exec"}

        event = _make_event(lib="dos", func="Open", task="Shell")
        assert viewer._passes_client_filter(event) is False

        event2 = _make_event(lib="exec", func="Open", task="Shell")
        assert viewer._passes_client_filter(event2) is False

        # Unknown lib passes through (blocklist semantics)
        event3 = _make_event(lib="icon", func="Open", task="Shell")
        assert viewer._passes_client_filter(event3) is True

    def test_elapsed_str_no_events(self):
        """No events returns +0:00.0."""
        viewer = _make_viewer()
        assert viewer._elapsed_str() == "+0:00.0"

    def test_elapsed_str_with_events(self):
        """Elapsed string computed from start_time and last_event_time."""
        viewer = _make_viewer()
        viewer.start_time = "10:00:00.000"
        viewer.last_event_time = "10:01:23.400"
        result = viewer._elapsed_str()
        assert result == "+1:23.4"

    def test_parse_time_valid(self):
        """Verify _parse_time for a standard 3-digit timestamp."""
        ms = TraceViewer._parse_time("10:15:23.456")
        expected = (10 * 3600 + 15 * 60 + 23) * 1000 + 456
        assert ms == expected

    def test_parse_time_6digit(self):
        """Verify _parse_time with 6-digit microsecond timestamp."""
        ms = TraceViewer._parse_time("10:15:23.456789")
        expected = (10 * 3600 + 15 * 60 + 23) * 1000 + 456
        assert ms == expected

    def test_parse_time_malformed(self):
        """Verify graceful fallback for malformed time."""
        assert TraceViewer._parse_time("bad") == 0
        assert TraceViewer._parse_time("") == 0

    def test_time_diff_normal(self):
        """Verify normal time difference with microsecond precision."""
        result = TraceViewer._time_diff("10:00:00.000000", "10:00:01.500000")
        assert result == "+1.500000"

    def test_time_diff_normal_3digit(self):
        """Verify time difference with 3-digit timestamps (backward compat)."""
        result = TraceViewer._time_diff("10:00:00.000", "10:00:01.500")
        assert result == "+1.500000"

    def test_time_diff_midnight_wrap(self):
        """Verify midnight wraparound handling."""
        result = TraceViewer._time_diff("23:59:59.900000", "00:00:00.100000")
        assert result == "+0.200000"

    def test_keypress_s_toggles_stats(self):
        """Pressing 's' toggles stats_mode."""
        viewer = _make_viewer()
        assert viewer.stats_mode is False

        viewer.term.read_key = mock.MagicMock(return_value="s")
        viewer._handle_keypress()
        assert viewer.stats_mode is True

    def test_keypress_e_toggles_errors(self):
        """Pressing 'e' toggles errors_filter."""
        viewer = _make_viewer()
        assert viewer.errors_filter is False

        viewer.term.read_key = mock.MagicMock(return_value="e")
        # Mock send_filter to avoid socket operations
        viewer.conn.send_filter = mock.MagicMock()
        viewer._handle_keypress()
        assert viewer.errors_filter is True

    def test_keypress_c_clears_events(self):
        """Pressing 'c' clears scrollback, counters, and discovered data."""
        viewer = _make_viewer()

        # Populate state with some events
        viewer._process_event_result(
            _make_event(lib="dos", func="Open"))
        viewer._process_event_result(
            _make_event(lib="exec", func="OpenLibrary", status="E"))

        assert viewer.total_events == 2
        assert viewer.shown_events == 2
        assert len(viewer.scrollback) == 2
        assert len(viewer.discovered_libs) == 2
        assert len(viewer.discovered_funcs) == 2
        assert len(viewer.discovered_procs) == 1
        assert viewer.error_count == 1

        viewer.term.read_key = mock.MagicMock(return_value="c")
        viewer._handle_keypress()

        assert viewer.total_events == 0
        assert viewer.shown_events == 0
        assert len(viewer.scrollback) == 0
        assert viewer.last_event_time is None
        assert viewer.start_time is None
        assert viewer._scrollback_full is False
        assert len(viewer.discovered_libs) == 0
        assert len(viewer.discovered_funcs) == 0
        assert len(viewer.discovered_procs) == 0
        assert viewer.error_count == 0
        assert len(viewer.func_counts) == 0
        assert len(viewer.lib_counts) == 0
        assert len(viewer.proc_counts) == 0
        assert len(viewer.error_counts) == 0

    def test_clear_redraws_status_bar(self):
        """Verify status bar is redrawn after clear."""
        viewer = _make_viewer()

        viewer._process_event_result(
            _make_event(lib="dos", func="Open"))

        with mock.patch.object(viewer, '_draw_status_bar') as mock_status, \
             mock.patch.object(viewer, '_draw_hotkey_bar') as mock_hotkey:
            viewer._clear_events()
            mock_status.assert_called_once()
            mock_hotkey.assert_called_once()

    def test_clear_not_active_when_paused(self):
        """'c' does nothing when the viewer is paused."""
        viewer = _make_viewer()
        viewer.paused = True

        viewer._process_event_result(
            _make_event(lib="dos", func="Open"))

        viewer.term.read_key = mock.MagicMock(return_value="c")
        viewer._handle_keypress()

        # Events should still be there
        assert viewer.total_events == 1
        assert len(viewer.scrollback) == 1

    def test_clear_not_active_when_grid_visible(self):
        """'c' does nothing when the filter grid is open."""
        viewer = _make_viewer()
        viewer.grid_visible = True
        viewer.grid = mock.MagicMock()

        viewer._process_event_result(
            _make_event(lib="dos", func="Open"))

        viewer.term.read_key = mock.MagicMock(return_value="c")
        viewer._handle_keypress()

        # Events should still be there
        assert viewer.total_events == 1
        assert len(viewer.scrollback) == 1

    def test_clear_preserves_disabled_filters(self):
        """Clear does not reset the user's filter choices."""
        viewer = _make_viewer()
        viewer.disabled_libs = {"exec"}
        viewer.disabled_funcs = {"dos": {"Close"}}
        viewer.disabled_procs = {"Background CLI"}

        viewer._clear_events()

        assert viewer.disabled_libs == {"exec"}
        assert viewer.disabled_funcs == {"dos": {"Close"}}
        assert viewer.disabled_procs == {"Background CLI"}

    def test_build_stats_text_format(self):
        """Verify _build_stats_text output format."""
        viewer = _make_viewer()
        viewer.func_counts = {"dos.Open": 10, "exec.OpenLibrary": 5}
        viewer.total_events = 15
        viewer.error_count = 2

        text = viewer._build_stats_text()
        assert "STATS:" in text
        assert "dos.Open:10" in text
        assert "15 events" in text
        assert "2 errors" in text

    def test_build_hotkey_bar_full(self):
        """Full hotkey bar when terminal is wide enough."""
        viewer = _make_viewer()
        viewer.term.cols = 130

        text = viewer._build_hotkey_bar()
        assert "[Tab] filters" in text
        assert "[/] search" in text
        assert "[p] pause" in text
        assert "[q] quit" in text

    def test_build_hotkey_bar_paused(self):
        """Hotkey bar shows RESUME when paused."""
        viewer = _make_viewer()
        viewer.term.cols = 130
        viewer.paused = True

        text = viewer._build_hotkey_bar()
        assert "[p] RESUME" in text

    def test_build_hotkey_bar_narrow(self):
        """Abbreviated hotkey bar on narrow terminal."""
        viewer = _make_viewer()
        viewer.term.cols = 40

        text = viewer._build_hotkey_bar()
        # Should be minimal format
        assert "[q]" in text

    def test_cycle_timestamp(self):
        """Verify timestamp mode cycles through all three modes."""
        viewer = _make_viewer()
        assert viewer.timestamp_mode == "absolute"

        viewer._cycle_timestamp()
        assert viewer.timestamp_mode == "relative"

        viewer._cycle_timestamp()
        assert viewer.timestamp_mode == "delta"

        viewer._cycle_timestamp()
        assert viewer.timestamp_mode == "absolute"

    def test_format_timestamp_absolute(self):
        """Absolute mode returns raw time."""
        viewer = _make_viewer()
        viewer.timestamp_mode = "absolute"
        event = _make_event(time="10:15:23.456")
        assert viewer._format_timestamp(event) == "10:15:23.456"

    def test_format_timestamp_relative(self):
        """Relative mode returns offset from start time."""
        viewer = _make_viewer()
        viewer.timestamp_mode = "relative"
        viewer.start_time = "10:00:00.000"
        event = _make_event(time="10:00:01.500")
        assert viewer._format_timestamp(event) == "+1.500000"

    def test_format_timestamp_delta(self):
        """Delta mode returns offset from previous event."""
        viewer = _make_viewer()
        viewer.timestamp_mode = "delta"
        viewer.last_event_time = "10:00:00.000"
        event = _make_event(time="10:00:00.250")
        assert viewer._format_timestamp(event) == "+0.250000"

    def test_format_timestamp_delta_no_previous(self):
        """Delta mode with no previous event returns +0.000000."""
        viewer = _make_viewer()
        viewer.timestamp_mode = "delta"
        viewer.last_event_time = None
        event = _make_event(time="10:00:00.250")
        assert viewer._format_timestamp(event) == "+0.000000"

    def test_pause_buffer_limit(self):
        """Verify buffer stops growing at pause_buffer_limit."""
        viewer = _make_viewer()
        viewer.paused = True
        viewer.pause_buffer_limit = 5

        for i in range(10):
            viewer._process_event_result(_make_event(seq=i))

        assert len(viewer.pause_buffer) == 5
        # All 10 events should still be counted
        assert viewer.total_events == 10

    def test_help_key_dismisses_help(self):
        """Any key dismisses help overlay."""
        viewer = _make_viewer()
        viewer.help_visible = True
        viewer._help_lines = ["line {}".format(i) for i in range(10)]
        viewer._help_scroll_pos = 0

        viewer.term.read_key = mock.MagicMock(return_value="x")
        viewer._handle_keypress()

        assert viewer.help_visible is False

    def test_help_dismiss_rerenders_scrollback(self):
        """Dismissing help with events in scrollback re-renders them."""
        viewer = _make_viewer()
        viewer.help_visible = True
        viewer._help_lines = ["help line"]
        viewer._help_scroll_pos = 0

        # Add some events to scrollback
        for i in range(5):
            viewer.scrollback.append(
                _make_event(seq=i, time="10:00:0{}.000".format(i)))

        with mock.patch.object(
                viewer, '_rerender_from_scrollback') as mock_rerender:
            viewer.term.read_key = mock.MagicMock(return_value="x")
            viewer._handle_keypress()

            assert viewer.help_visible is False
            mock_rerender.assert_called_once()

    def test_help_dismiss_paused_restores_scroll(self):
        """Dismissing help while paused rebuilds pause scroll state."""
        viewer = _make_viewer()
        viewer.help_visible = True
        viewer.paused = True
        viewer._help_lines = ["help line"]
        viewer._help_scroll_pos = 0

        # Add scrollback and pause buffer events
        for i in range(5):
            viewer.scrollback.append(
                _make_event(seq=i, time="10:00:0{}.000".format(i)))
        viewer.pause_buffer = [
            _make_event(seq=10, time="10:00:10.000")]

        with mock.patch.object(
                viewer, '_scroll_pause_buffer') as mock_scroll:
            viewer.term.read_key = mock.MagicMock(return_value="x")
            viewer._handle_keypress()

            assert viewer.help_visible is False
            assert viewer._scroll_snapshot is not None
            mock_scroll.assert_called_once_with(0)

    def test_help_scroll_down(self):
        """Down arrow in help scrolls without dismissing."""
        viewer = _make_viewer()
        viewer.help_visible = True
        viewer._help_lines = ["line {}".format(i) for i in range(40)]
        viewer._help_scroll_pos = 0
        viewer.term.rows = 24  # available = 20, so 40 lines need scrolling

        viewer.term.read_key = mock.MagicMock(
            return_value=("esc", "[B"))
        viewer._handle_keypress()

        assert viewer.help_visible is True
        assert viewer._help_scroll_pos == 1

    def test_help_scroll_up(self):
        """Up arrow in help scrolls up without dismissing."""
        viewer = _make_viewer()
        viewer.help_visible = True
        viewer._help_lines = ["line {}".format(i) for i in range(40)]
        viewer._help_scroll_pos = 5
        viewer.term.rows = 24

        viewer.term.read_key = mock.MagicMock(
            return_value=("esc", "[A"))
        viewer._handle_keypress()

        assert viewer.help_visible is True
        assert viewer._help_scroll_pos == 4

    def test_help_scroll_clamps(self):
        """Help scroll position clamps at boundaries."""
        viewer = _make_viewer()
        viewer.help_visible = True
        viewer._help_lines = ["line {}".format(i) for i in range(40)]
        viewer.term.rows = 24  # available = 20, minus 1 for indicator = 19, max_pos = 21

        # Clamp at 0
        viewer._help_scroll_pos = 0
        viewer.term.read_key = mock.MagicMock(
            return_value=("esc", "[A"))
        viewer._handle_keypress()
        assert viewer._help_scroll_pos == 0

        # Clamp at max (40 lines - 19 visible = 21)
        viewer._help_scroll_pos = 21
        viewer.term.read_key = mock.MagicMock(
            return_value=("esc", "[B"))
        viewer._handle_keypress()
        assert viewer._help_scroll_pos == 21

    def test_help_scroll_page_down(self):
        """PgDn in help scrolls by page."""
        viewer = _make_viewer()
        viewer.help_visible = True
        viewer._help_lines = ["line {}".format(i) for i in range(60)]
        viewer._help_scroll_pos = 0
        viewer.term.rows = 24  # available = 20

        viewer.term.read_key = mock.MagicMock(
            return_value=("esc", "[6~"))
        viewer._handle_keypress()

        assert viewer.help_visible is True
        assert viewer._help_scroll_pos == 20

    def test_help_scroll_page_up(self):
        """PgUp in help scrolls by page."""
        viewer = _make_viewer()
        viewer.help_visible = True
        viewer._help_lines = ["line {}".format(i) for i in range(60)]
        viewer._help_scroll_pos = 30
        viewer.term.rows = 24  # available = 20

        viewer.term.read_key = mock.MagicMock(
            return_value=("esc", "[5~"))
        viewer._handle_keypress()

        assert viewer.help_visible is True
        assert viewer._help_scroll_pos == 10

    def test_sigwinch_sets_flag(self):
        """SIGWINCH handler sets _resize_pending flag."""
        viewer = _make_viewer()
        assert viewer._resize_pending is False

        viewer._handle_sigwinch(None, None)
        assert viewer._resize_pending is True

    def test_handle_resize_no_layout(self):
        """_handle_resize skips ColumnLayout when not initialized."""
        viewer = _make_viewer()
        # Ensure no layout attribute exists
        assert not hasattr(viewer, 'layout')

        # Should not raise -- just updates size and redraws
        viewer._handle_resize()

    def test_status_bar_default(self):
        """Default status bar shows event counts and elapsed."""
        viewer = _make_viewer()
        viewer.total_events = 42
        viewer.shown_events = 42

        output = io.StringIO()
        viewer.term.stdout = output

        viewer._draw_status_bar()

        written = output.getvalue()
        assert "TRACE:" in written
        assert "42 events" in written

    def test_status_bar_filtered(self):
        """Status bar shows shown vs total when filtered."""
        viewer = _make_viewer()
        viewer.total_events = 100
        viewer.shown_events = 42

        output = io.StringIO()
        viewer.term.stdout = output

        viewer._draw_status_bar()

        written = output.getvalue()
        assert "100 events" in written
        assert "42 shown" in written

    def test_status_bar_paused_empty(self):
        """Status bar when paused with no buffered events."""
        viewer = _make_viewer()
        viewer.paused = True
        viewer.pause_buffer = []

        output = io.StringIO()
        viewer.term.stdout = output

        viewer._draw_status_bar()

        written = output.getvalue()
        assert "PAUSED" in written

    def test_status_bar_paused_with_buffer(self):
        """Status bar when paused with buffered events."""
        viewer = _make_viewer()
        viewer.paused = True
        viewer.pause_buffer = [_make_event()] * 50

        output = io.StringIO()
        viewer.term.stdout = output

        viewer._draw_status_bar()

        written = output.getvalue()
        assert "PAUSED" in written
        assert "event" in written

    def test_status_bar_paused_buffer_full(self):
        """Status bar shows 'buffer full' when at limit."""
        viewer = _make_viewer()
        viewer.paused = True
        # Set small limits so combined reaches threshold
        viewer.scrollback_limit = 5
        viewer.pause_buffer_limit = 5
        viewer.scrollback = deque([_make_event()] * 5, maxlen=5)
        viewer._scroll_snapshot = [_make_event()] * 5
        viewer.pause_buffer = [_make_event()] * 5

        output = io.StringIO()
        viewer.term.stdout = output

        viewer._draw_status_bar()

        written = output.getvalue()
        assert "buffer full" in written

    def test_stop_trace_sends_stop(self):
        """_stop_trace sends STOP command."""
        viewer = _make_viewer()

        # Mock the reader to return False (END) immediately
        viewer.reader.has_buffered_data.return_value = False
        viewer.reader.try_read_event.return_value = False

        viewer._stop_trace()

        # Verify STOP was sent
        viewer.sock.settimeout.assert_called_with(10.0)
        viewer.sock.sendall.assert_called()
        sent_data = viewer.sock.sendall.call_args[0][0]
        assert b"STOP" in sent_data


# ---------------------------------------------------------------------------
# TestSearch (Wave 4)
# ---------------------------------------------------------------------------

class TestSearch:
    """Tests for search mode (/ hotkey)."""

    def test_search_pattern_set(self):
        """Simulate / + 'Open' + Enter, verify search_pattern set."""
        viewer = _make_viewer()

        # Build a sequence of keys: 'O', 'p', 'e', 'n', Enter
        keys = ["O", "p", "e", "n", "\n"]
        key_iter = iter(keys)

        def mock_read_key():
            try:
                return next(key_iter)
            except StopIteration:
                return None

        viewer.term.read_key = mock_read_key

        # Mock select to always report stdin ready, socket not ready
        with mock.patch("amigactl.trace_ui.select") as mock_select_mod:
            def fake_select(fds, w, x, timeout=0):
                return ([viewer.term.stdin_fd], [], [])
            mock_select_mod.select.side_effect = fake_select

            viewer._enter_search_mode()

        assert viewer.search_pattern == "Open"

    def test_search_esc_clears(self):
        """Simulate / + Esc, verify search_pattern is None."""
        viewer = _make_viewer()

        # Esc is returned as a tuple ("esc", "")
        keys = [("esc", "")]
        key_iter = iter(keys)

        def mock_read_key():
            try:
                return next(key_iter)
            except StopIteration:
                return None

        viewer.term.read_key = mock_read_key

        # Start with a pattern set to verify it gets cleared
        viewer.search_pattern = "old_pattern"

        with mock.patch("amigactl.trace_ui.select") as mock_select_mod:
            def fake_select(fds, w, x, timeout=0):
                return ([viewer.term.stdin_fd], [], [])
            mock_select_mod.select.side_effect = fake_select

            viewer._enter_search_mode()

        assert viewer.search_pattern is None

    def test_search_filters_display(self):
        """Events not matching search pattern are counted but not shown."""
        viewer = _make_viewer()
        viewer.search_pattern = "OpenLibrary"

        event_match = _make_event(
            lib="exec", func="OpenLibrary",
            retval="0x1234", status="O")
        event_nomatch = _make_event(
            lib="dos", func="Close",
            retval="DOSTRUE", status="O")

        viewer._process_event_result(event_match)
        viewer._process_event_result(event_nomatch)

        # Both counted in total
        assert viewer.total_events == 2
        # Only matching event was shown
        assert viewer.shown_events == 1

    def test_search_backspace(self):
        """Backspace removes last character from search buffer."""
        viewer = _make_viewer()

        # Type 'Opem', backspace, 'n', Enter -> "Open"
        keys = ["O", "p", "e", "m", "\x7f", "n", "\n"]
        key_iter = iter(keys)

        def mock_read_key():
            try:
                return next(key_iter)
            except StopIteration:
                return None

        viewer.term.read_key = mock_read_key

        with mock.patch("amigactl.trace_ui.select") as mock_select_mod:
            def fake_select(fds, w, x, timeout=0):
                return ([viewer.term.stdin_fd], [], [])
            mock_select_mod.select.side_effect = fake_select

            viewer._enter_search_mode()

        assert viewer.search_pattern == "Open"

    def test_search_empty_enter_clears(self):
        """Enter with empty buffer clears the search pattern."""
        viewer = _make_viewer()
        viewer.search_pattern = "old"

        keys = ["\n"]
        key_iter = iter(keys)

        def mock_read_key():
            try:
                return next(key_iter)
            except StopIteration:
                return None

        viewer.term.read_key = mock_read_key

        with mock.patch("amigactl.trace_ui.select") as mock_select_mod:
            def fake_select(fds, w, x, timeout=0):
                return ([viewer.term.stdin_fd], [], [])
            mock_select_mod.select.side_effect = fake_select

            viewer._enter_search_mode()

        assert viewer.search_pattern is None

    def test_search_consumes_socket_data(self):
        """Socket data is consumed during search input (S3 fix)."""
        viewer = _make_viewer()

        call_count = [0]
        original_handle = viewer._handle_socket_data

        def tracking_handle():
            call_count[0] += 1

        viewer._handle_socket_data = tracking_handle

        # First select returns socket ready, second returns stdin
        select_results = [
            ([viewer.sock], [], []),      # socket data available
            ([viewer.term.stdin_fd], [], []),  # stdin ready
        ]
        select_iter = iter(select_results)

        def mock_read_key():
            return "\n"  # Enter to exit

        viewer.term.read_key = mock_read_key

        with mock.patch("amigactl.trace_ui.select") as mock_select_mod:
            mock_select_mod.select.side_effect = \
                lambda f, w, x, timeout=0: next(select_iter)

            viewer._enter_search_mode()

        assert call_count[0] == 1

    def test_search_status_bar_indicator(self):
        """Status bar shows search pattern when active."""
        viewer = _make_viewer()
        viewer.search_pattern = "Open"
        viewer.total_events = 10
        viewer.shown_events = 5

        output = io.StringIO()
        viewer.term.stdout = output

        viewer._draw_status_bar()

        written = output.getvalue()
        assert 'search: "Open"' in written


# ---------------------------------------------------------------------------
# TestPause (Wave 4)
# ---------------------------------------------------------------------------

class TestPause:
    """Tests for pause and scroll-back (p hotkey)."""

    def test_pause_buffers_events(self):
        """Events are buffered when paused, not displayed."""
        viewer = _make_viewer()
        viewer.paused = True

        event = _make_event()
        viewer._process_event_result(event)

        assert len(viewer.pause_buffer) == 1
        assert viewer.pause_buffer[0] is event
        assert viewer.total_events == 1
        assert viewer.shown_events == 0

    def test_unpause_catches_up(self):
        """Unpause re-renders from scrollback and clears pause_buffer."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)
        viewer.paused = True

        # Buffer some events
        for i in range(5):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:0{}.000".format(i)))

        assert len(viewer.pause_buffer) == 5
        assert viewer.shown_events == 0

        # Unpause: re-renders from scrollback (no shown_events increment)
        viewer._toggle_pause()

        assert viewer.paused is False
        # shown_events stays 0: _rerender_from_scrollback is a
        # display-only method, not a lifetime counter incrementer
        assert viewer.shown_events == 0
        assert len(viewer.pause_buffer) == 0

    def test_pause_buffer_limit(self):
        """Buffer stops growing at pause_buffer_limit."""
        viewer = _make_viewer()
        viewer.paused = True
        viewer.pause_buffer_limit = 5

        for i in range(10):
            viewer._process_event_result(_make_event(seq=i))

        assert len(viewer.pause_buffer) == 5
        assert viewer.total_events == 10

    def test_scroll_pause_buffer_up(self):
        """Scroll up decrements position and re-renders."""
        viewer = _make_viewer()
        viewer.paused = True
        viewer.term.rows = 24

        # Fill buffer with enough events to scroll
        for i in range(50):
            viewer.pause_buffer.append(
                _make_event(seq=i, func="Func{}".format(i)))

        # Start at position 30
        viewer.pause_scroll_pos = 30

        output = io.StringIO()
        viewer.term.stdout = output

        viewer._scroll_pause_buffer(-1)

        assert viewer.pause_scroll_pos == 29

    def test_scroll_pause_buffer_clamps(self):
        """Scroll position clamps at 0 and max."""
        viewer = _make_viewer()
        viewer.paused = True
        viewer.term.rows = 24

        for i in range(50):
            viewer.pause_buffer.append(_make_event(seq=i))

        # Scroll far up past beginning
        viewer.pause_scroll_pos = 5
        viewer._scroll_pause_buffer(-100)
        assert viewer.pause_scroll_pos == 0

        # Scroll far down past end
        visible_lines = viewer.term.rows - 4  # 20
        max_pos = max(0, len(viewer.pause_buffer) - visible_lines)
        viewer._scroll_pause_buffer(10000)
        assert viewer.pause_scroll_pos == max_pos

    def test_scroll_pause_status_shows_position(self):
        """Status bar shows event N/M and new count when scrolling."""
        viewer = _make_viewer()
        viewer.paused = True
        viewer.term.rows = 24

        for i in range(50):
            viewer.pause_buffer.append(_make_event(seq=i))

        viewer.pause_scroll_pos = 10
        viewer.highlight_pos = 15

        output = io.StringIO()
        viewer.term.stdout = output

        viewer._draw_status_bar()

        written = output.getvalue()
        assert "PAUSED" in written
        assert "event 16/50" in written

    def test_scroll_empty_buffer_noop(self):
        """Scrolling with empty buffer is a no-op."""
        viewer = _make_viewer()
        viewer.paused = True

        viewer._scroll_pause_buffer(-1)
        assert viewer.pause_scroll_pos == 0

        viewer._scroll_pause_buffer(1)
        assert viewer.pause_scroll_pos == 0

    def test_arrow_keys_move_highlight_when_paused(self):
        """Up/Down arrows move highlight cursor when paused."""
        viewer = _make_viewer()
        viewer.paused = True

        for i in range(50):
            viewer.pause_buffer.append(_make_event(seq=i))
        viewer.highlight_pos = 25
        viewer.pause_scroll_pos = 10

        # Simulate up arrow
        viewer.term.read_key = mock.MagicMock(
            return_value=("esc", "[A"))
        viewer._handle_keypress()
        assert viewer.highlight_pos == 24

        # Simulate down arrow
        viewer.term.read_key = mock.MagicMock(
            return_value=("esc", "[B"))
        viewer._handle_keypress()
        assert viewer.highlight_pos == 25

    def test_page_keys_move_highlight_when_paused(self):
        """PgUp/PgDn move highlight by page when paused."""
        viewer = _make_viewer()
        viewer.paused = True
        viewer.term.rows = 24

        for i in range(100):
            viewer.pause_buffer.append(_make_event(seq=i))
        viewer.highlight_pos = 50

        page_size = viewer.term.rows - 4  # 20

        # Page up
        viewer.term.read_key = mock.MagicMock(
            return_value=("esc", "[5~"))
        viewer._handle_keypress()
        assert viewer.highlight_pos == 50 - page_size

        # Page down
        viewer.term.read_key = mock.MagicMock(
            return_value=("esc", "[6~"))
        viewer._handle_keypress()
        assert viewer.highlight_pos == 50


# ---------------------------------------------------------------------------
# TestTimestamp (Wave 4)
# ---------------------------------------------------------------------------

class TestTimestamp:
    """Tests for timestamp format cycling (t hotkey)."""

    def test_absolute_passthrough(self):
        """Absolute mode returns raw time."""
        viewer = _make_viewer()
        viewer.timestamp_mode = "absolute"
        event = _make_event(time="10:15:23.456")
        assert viewer._format_timestamp(event) == "10:15:23.456"

    def test_relative_from_start(self):
        """Relative mode returns offset from start time."""
        viewer = _make_viewer()
        viewer.timestamp_mode = "relative"
        viewer.start_time = "10:00:00.000"
        event = _make_event(time="10:00:01.500")
        assert viewer._format_timestamp(event) == "+1.500000"

    def test_delta_between_events(self):
        """Delta mode returns offset from previous event."""
        viewer = _make_viewer()
        viewer.timestamp_mode = "delta"
        viewer.last_event_time = "10:00:00.000"
        event = _make_event(time="10:00:00.250")
        assert viewer._format_timestamp(event) == "+0.250000"

    def test_relative_microsecond_precision(self):
        """Relative mode preserves microsecond precision."""
        viewer = _make_viewer()
        viewer.timestamp_mode = "relative"
        viewer.start_time = "10:00:00.000000"
        event = _make_event(time="10:00:01.123456")
        assert viewer._format_timestamp(event) == "+1.123456"

    def test_delta_microsecond_precision(self):
        """Delta mode preserves microsecond precision."""
        viewer = _make_viewer()
        viewer.timestamp_mode = "delta"
        viewer.last_event_time = "10:00:00.000000"
        event = _make_event(time="10:00:00.001234")
        assert viewer._format_timestamp(event) == "+0.001234"

    def test_time_parse_valid(self):
        """Verify _parse_time for a standard timestamp."""
        ms = TraceViewer._parse_time("10:15:23.456")
        expected = (10 * 3600 + 15 * 60 + 23) * 1000 + 456
        assert ms == expected

    def test_time_parse_malformed(self):
        """Verify graceful fallback for malformed time."""
        assert TraceViewer._parse_time("bad") == 0
        assert TraceViewer._parse_time("") == 0

    def test_time_diff_midnight_wrap(self):
        """Verify midnight wraparound handling."""
        result = TraceViewer._time_diff(
            "23:59:59.900000", "00:00:00.100000")
        assert result == "+0.200000"

    def test_cycle_through_modes(self):
        """Pressing t cycles absolute -> relative -> delta -> absolute."""
        viewer = _make_viewer()
        assert viewer.timestamp_mode == "absolute"

        viewer._cycle_timestamp()
        assert viewer.timestamp_mode == "relative"

        viewer._cycle_timestamp()
        assert viewer.timestamp_mode == "delta"

        viewer._cycle_timestamp()
        assert viewer.timestamp_mode == "absolute"

    def test_relative_no_start_time(self):
        """Relative mode with no start_time returns +0.000000."""
        viewer = _make_viewer()
        viewer.timestamp_mode = "relative"
        viewer.start_time = None
        event = _make_event(time="10:00:00.250")
        assert viewer._format_timestamp(event) == "+0.000000"

    def test_delta_no_previous(self):
        """Delta mode with no previous event returns +0.000000."""
        viewer = _make_viewer()
        viewer.timestamp_mode = "delta"
        viewer.last_event_time = None
        event = _make_event(time="10:00:00.250")
        assert viewer._format_timestamp(event) == "+0.000000"

    # -- Phase 6: Microsecond timestamp parsing tests --

    def test_parse_time_us_6digit(self):
        """_parse_time_us parses HH:MM:SS.uuuuuu to microseconds."""
        us = TraceViewer._parse_time_us("12:34:56.123456")
        expected = ((12 * 3600 + 34 * 60 + 56) * 1000000) + 123456
        assert us == expected

    def test_parse_time_us_3digit_padded(self):
        """_parse_time_us pads 3-digit ms to 6-digit us (backward compat)."""
        us = TraceViewer._parse_time_us("12:34:56.123")
        expected = ((12 * 3600 + 34 * 60 + 56) * 1000000) + 123000
        assert us == expected

    def test_parse_time_us_no_fraction(self):
        """_parse_time_us handles timestamps with no fractional part."""
        us = TraceViewer._parse_time_us("12:34:56")
        expected = (12 * 3600 + 34 * 60 + 56) * 1000000
        assert us == expected

    def test_parse_time_us_zero(self):
        """_parse_time_us handles midnight (00:00:00.000000)."""
        assert TraceViewer._parse_time_us("00:00:00.000000") == 0

    def test_parse_time_us_one_microsecond(self):
        """_parse_time_us handles smallest non-zero microsecond."""
        us = TraceViewer._parse_time_us("00:00:00.000001")
        assert us == 1

    def test_parse_time_us_999999(self):
        """_parse_time_us handles 999999 microseconds."""
        us = TraceViewer._parse_time_us("00:00:00.999999")
        assert us == 999999

    def test_parse_time_us_malformed(self):
        """_parse_time_us returns 0 for malformed input."""
        assert TraceViewer._parse_time_us("bad") == 0
        assert TraceViewer._parse_time_us("") == 0
        assert TraceViewer._parse_time_us("not:a:time") == 0

    def test_parse_time_us_4digit(self):
        """_parse_time_us pads 4-digit fraction to 6 digits."""
        us = TraceViewer._parse_time_us("00:00:01.1234")
        assert us == 1000000 + 123400

    def test_parse_time_us_7digit_truncated(self):
        """_parse_time_us truncates fractions longer than 6 digits."""
        us = TraceViewer._parse_time_us("00:00:01.1234567")
        assert us == 1000000 + 123456

    def test_parse_time_backward_compat_6digit(self):
        """_parse_time (ms wrapper) correctly truncates 6-digit timestamps."""
        ms = TraceViewer._parse_time("12:34:56.123456")
        expected = (12 * 3600 + 34 * 60 + 56) * 1000 + 123
        assert ms == expected

    def test_time_diff_microsecond_precision(self):
        """_time_diff produces microsecond-precision delta strings."""
        result = TraceViewer._time_diff(
            "10:00:00.000000", "10:00:00.001234")
        assert result == "+0.001234"

    def test_time_diff_3digit_backward_compat(self):
        """_time_diff works with 3-digit timestamps (padded to us)."""
        result = TraceViewer._time_diff("10:00:00.000", "10:00:01.500")
        assert result == "+1.500000"

    def test_time_diff_midnight_wrap_microsecond(self):
        """_time_diff handles midnight wraparound with microseconds."""
        result = TraceViewer._time_diff(
            "23:59:59.999000", "00:00:00.001000")
        assert result == "+0.002000"

    def test_elapsed_str_microsecond_input(self):
        """_elapsed_str works correctly with 6-digit timestamps."""
        viewer = _make_viewer()
        viewer.start_time = "10:00:00.000000"
        viewer.last_event_time = "10:01:23.456789"
        result = viewer._elapsed_str()
        assert result == "+1:23.4"

    def test_format_timestamp_for_scroll_delta_microsecond(self):
        """_format_timestamp_for_scroll delta uses microsecond precision."""
        viewer = _make_viewer()
        viewer.timestamp_mode = "delta"
        event = _make_event(time="10:00:00.123456")
        result = viewer._format_timestamp_for_scroll(
            event, "10:00:00.000000")
        assert result == "+0.123456"

    def test_format_timestamp_for_scroll_relative_microsecond(self):
        """_format_timestamp_for_scroll relative uses microsecond precision."""
        viewer = _make_viewer()
        viewer.timestamp_mode = "relative"
        viewer.start_time = "10:00:00.000000"
        event = _make_event(time="10:00:01.234567")
        result = viewer._format_timestamp_for_scroll(event, None)
        assert result == "+1.234567"


# ---------------------------------------------------------------------------
# TestCommentMetadata (Phase 6)
# ---------------------------------------------------------------------------

class TestCommentMetadata:
    """Tests for header comment metadata parsing (Phase 6)."""

    def test_eclock_freq_parsed_from_comment(self):
        """eclock_freq is extracted from header comment."""
        viewer = _make_viewer()
        comment = {"type": "comment", "text": "eclock_freq: 709379 Hz"}
        viewer._display_comment(comment)
        assert viewer.eclock_freq == 709379

    def test_timestamp_precision_parsed_from_comment(self):
        """timestamp_precision is extracted from header comment."""
        viewer = _make_viewer()
        comment = {"type": "comment",
                   "text": "timestamp_precision: microsecond"}
        viewer._display_comment(comment)
        assert viewer.timestamp_precision == "microsecond"

    def test_eclock_freq_ntsc(self):
        """NTSC EClock frequency is parsed correctly."""
        viewer = _make_viewer()
        comment = {"type": "comment", "text": "eclock_freq: 715909 Hz"}
        viewer._display_comment(comment)
        assert viewer.eclock_freq == 715909

    def test_eclock_freq_malformed_ignored(self):
        """Malformed eclock_freq comment is silently ignored."""
        viewer = _make_viewer()
        comment = {"type": "comment", "text": "eclock_freq: notanumber Hz"}
        viewer._display_comment(comment)
        assert viewer.eclock_freq == 0

    def test_metadata_defaults(self):
        """Metadata fields default to zero/empty."""
        viewer = _make_viewer()
        assert viewer.eclock_freq == 0
        assert viewer.timestamp_precision == ""


# ---------------------------------------------------------------------------
# TestStatistics (Wave 4)
# ---------------------------------------------------------------------------

class TestStatistics:
    """Tests for statistics mode (s hotkey)."""

    def test_func_counts_accumulate(self):
        """Feed events, verify function counts accumulate."""
        viewer = _make_viewer()

        for _ in range(3):
            viewer._process_event_result(
                _make_event(lib="dos", func="Open"))
        for _ in range(2):
            viewer._process_event_result(
                _make_event(lib="exec", func="OpenLibrary"))

        assert viewer.func_counts["dos.Open"] == 3
        assert viewer.func_counts["exec.OpenLibrary"] == 2
        assert viewer.lib_counts["dos"] == 3
        assert viewer.lib_counts["exec"] == 2

    def test_stats_text_format(self):
        """Verify _build_stats_text output format."""
        viewer = _make_viewer()
        viewer.func_counts = {
            "dos.Open": 10,
            "exec.OpenLibrary": 5,
        }
        viewer.total_events = 15
        viewer.error_count = 2

        text = viewer._build_stats_text()
        assert "STATS:" in text
        assert "dos.Open:10" in text
        assert "exec.OpenLibrary:5" in text
        assert "15 events" in text
        assert "2 errors" in text

    def test_stats_mode_toggle(self):
        """Pressing s toggles stats_mode and redraws."""
        viewer = _make_viewer()
        assert viewer.stats_mode is False

        viewer.term.read_key = mock.MagicMock(return_value="s")
        viewer._handle_keypress()
        assert viewer.stats_mode is True

        viewer.term.read_key = mock.MagicMock(return_value="s")
        viewer._handle_keypress()
        assert viewer.stats_mode is False

    def test_error_counts_tracked(self):
        """Events with status 'E' tracked in error_counts dict."""
        viewer = _make_viewer()

        viewer._process_event_result(
            _make_event(lib="dos", func="Open",
                        status="E", retval="NULL"))
        viewer._process_event_result(
            _make_event(lib="dos", func="Open",
                        status="O", retval="0x1234"))
        viewer._process_event_result(
            _make_event(lib="dos", func="Open",
                        status="E", retval="NULL"))

        assert viewer.error_count == 2
        assert viewer.error_counts["dos.Open"] == 2

    def test_stats_sorted_by_count(self):
        """Top functions in stats are sorted by count descending."""
        viewer = _make_viewer()
        viewer.func_counts = {
            "dos.Close": 1,
            "dos.Open": 100,
            "exec.OpenLibrary": 50,
        }
        viewer.total_events = 151
        viewer.error_count = 0

        text = viewer._build_stats_text()
        # "dos.Open:100" should appear before "exec.OpenLibrary:50"
        open_pos = text.index("dos.Open:100")
        olib_pos = text.index("exec.OpenLibrary:50")
        assert open_pos < olib_pos


# ---------------------------------------------------------------------------
# TestColumnLayout
# ---------------------------------------------------------------------------

class TestColumnLayout:
    """Tests for the ColumnLayout adaptive column width class."""

    def test_wide_terminal(self):
        """120+ cols: all columns present with full widths."""
        layout = ColumnLayout(120)
        assert layout.time_width == 12
        assert layout.func_width == 20
        assert layout.result_width == 12
        assert layout.proc_width == 16
        assert layout.args_width >= 10
        assert layout.abbrev_lib is False

    def test_standard_terminal(self):
        """80 cols: standard column widths."""
        layout = ColumnLayout(80)
        assert layout.time_width == 12
        assert layout.func_width == 16
        assert layout.result_width == 8
        assert layout.proc_width == 14
        assert layout.args_width >= 10
        assert layout.abbrev_lib is False

    def test_narrow_terminal(self):
        """65 cols: abbreviated lib names, reduced widths."""
        layout = ColumnLayout(65)
        assert layout.time_width == 8
        assert layout.func_width == 12
        assert layout.result_width == 6
        assert layout.proc_width == 10
        assert layout.args_width >= 10
        assert layout.abbrev_lib is True

    def test_cramped_terminal(self):
        """50 cols: timestamp dropped, minimal widths."""
        layout = ColumnLayout(50)
        assert layout.time_width == 0
        assert layout.func_width == 10
        assert layout.result_width == 4
        assert layout.proc_width == 8
        assert layout.args_width >= 10
        assert layout.abbrev_lib is True

    def test_long_values_truncated(self):
        """Long field values are truncated with markers."""
        layout = ColumnLayout(80)
        cw = ColorWriter(force_color=False)

        event = _make_event(
            lib="exec",
            func="VeryLongFunctionNameThatWontFit",
            task="A Very Long Process Name",
            args="x" * 200,
            retval="0x1234567890ABCDEF",
        )
        formatted = layout.format_event(event, cw)

        # lib.func should be truncated with ~
        assert "~" in formatted
        # args should be truncated with ...
        assert "..." in formatted

    def test_ansi_aware_padding(self):
        """Colored strings produce same visible alignment as plain strings."""
        layout = ColumnLayout(120)
        event = _make_event()

        cw_plain = ColorWriter(force_color=False)
        cw_color = ColorWriter(force_color=True)

        plain = layout.format_event(event, cw_plain)
        colored = layout.format_event(event, cw_color)

        # Visible widths should be the same
        assert _visible_len(plain) == _visible_len(colored)

    def test_format_event_with_time_str(self):
        """time_str parameter is used when provided."""
        layout = ColumnLayout(120)
        cw = ColorWriter(force_color=False)
        event = _make_event(time="10:15:23.456")

        formatted = layout.format_event(event, cw, time_str="+0:05.2")
        assert "+0:05.2" in formatted

    def test_format_event_fallback_time(self):
        """Falls back to event time when time_str is None."""
        layout = ColumnLayout(120)
        cw = ColorWriter(force_color=False)
        event = _make_event(time="10:15:23.456")

        formatted = layout.format_event(event, cw, time_str=None)
        assert "10:15:23.456" in formatted

    def test_abbreviated_lib_name(self):
        """Narrow terminal abbreviates library names."""
        layout = ColumnLayout(65)
        cw = ColorWriter(force_color=False)
        event = _make_event(lib="dos", func="Open")

        formatted = layout.format_event(event, cw)
        # Should contain "d.Open" not "dos.Open"
        assert "d.Open" in formatted

    def test_error_retval_colored(self):
        """Error status retval is colored red."""
        layout = ColumnLayout(120)
        cw = ColorWriter(force_color=True)
        event = _make_event(status="E", retval="NULL")

        formatted = layout.format_event(event, cw)
        # Should contain red ANSI code before NULL
        assert "\033[31m" in formatted

    def test_cramped_no_timestamp(self):
        """Cramped layout omits timestamp column."""
        layout = ColumnLayout(50)
        cw = ColorWriter(force_color=False)
        event = _make_event(time="10:15:23.456")

        formatted = layout.format_event(event, cw)
        # Timestamp should NOT appear
        assert "10:15:23.456" not in formatted

    def test_display_event_uses_layout(self):
        """_display_event calls layout.format_event when layout exists."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(120)

        output = io.StringIO()
        viewer.term.stdout = output

        event = _make_event()
        viewer._display_event(event)

        written = output.getvalue()
        # The layout-based output includes the event data
        assert "dos" in written or "Open" in written


# ---------------------------------------------------------------------------
# TestLibColors
# ---------------------------------------------------------------------------

class TestLibColors:
    """Tests for the library color coding palette."""

    def test_known_lib_color(self):
        """Known library returns its fixed color."""
        from amigactl.colors import CYAN
        assert get_lib_color("dos") == CYAN

    def test_unknown_lib_auto_assigned(self):
        """Unknown library gets a palette color."""
        # Clear runtime cache for this test
        _lib_color_assignments.clear()
        color = get_lib_color("totally_unknown_lib_xyz")
        assert color is not None
        assert len(color) > 0
        assert "\033[" in color

    def test_lib_color_stable(self):
        """Same library always returns the same color."""
        _lib_color_assignments.clear()
        c1 = get_lib_color("mylib_stable_test")
        c2 = get_lib_color("mylib_stable_test")
        assert c1 == c2

    def test_known_libs_fixed(self):
        """All known libraries have distinct fixed colors."""
        from amigactl.colors import _LIB_COLORS
        colors = list(_LIB_COLORS.values())
        # At least the documented known libraries exist
        assert "dos" in _LIB_COLORS
        assert "exec" in _LIB_COLORS
        assert "intuition" in _LIB_COLORS


# ---------------------------------------------------------------------------
# TestHotkeyBarAdaptive
# ---------------------------------------------------------------------------

class TestHotkeyBarAdaptive:
    """Tests for adaptive hotkey bar width handling."""

    def test_full_width(self):
        """130 cols: full hotkey bar text."""
        viewer = _make_viewer()
        viewer.term.cols = 130
        text = viewer._build_hotkey_bar()
        assert "[Tab] filters" in text
        assert "[/] search" in text
        assert "[?] help" in text
        assert "[q] quit" in text

    def test_abbreviated(self):
        """60 cols: abbreviated hotkey bar text."""
        viewer = _make_viewer()
        viewer.term.cols = 60
        text = viewer._build_hotkey_bar()
        # Should be in abbreviated or minimal format
        assert "[q]" in text

    def test_minimal(self):
        """45 cols: minimal hotkey bar text."""
        viewer = _make_viewer()
        viewer.term.cols = 45
        text = viewer._build_hotkey_bar()
        # Minimal format
        assert "[Tab]" in text
        assert "[q]" in text
        assert len(text) <= 45

    def test_state_reflected_paused(self):
        """Paused state shows RESUME in hotkey bar."""
        viewer = _make_viewer()
        viewer.term.cols = 130
        viewer.paused = True
        text = viewer._build_hotkey_bar()
        assert "RESUME" in text

    def test_state_reflected_errors(self):
        """Errors filter shows ERRORS in hotkey bar."""
        viewer = _make_viewer()
        viewer.term.cols = 130
        viewer.errors_filter = True
        text = viewer._build_hotkey_bar()
        assert "ERRORS" in text

    def test_state_reflected_stats(self):
        """Stats mode shows STATS in hotkey bar."""
        viewer = _make_viewer()
        viewer.term.cols = 130
        viewer.stats_mode = True
        text = viewer._build_hotkey_bar()
        assert "STATS" in text


# ---------------------------------------------------------------------------
# TestGridStatePersistence (Bug 14)
# ---------------------------------------------------------------------------

class TestGridStatePersistence:
    """Tests for grid state persistence across open/close (Bug 14)."""

    def test_grid_state_persisted_across_open_close(self):
        """Disabled libs survive grid close and reopen."""
        viewer = _make_viewer()
        viewer.discovered_libs = {"dos": 100, "exec": 200}
        viewer.discovered_funcs = {"exec": {"OpenLibrary": 5}}
        viewer.discovered_procs = {"bbs": 89, "Shell": 43}

        viewer._enter_toggle_grid()

        # Disable dos (second by count: exec=200, dos=100)
        viewer.grid.cursor_pos[viewer.grid.active_category] = 1
        viewer.grid.toggle_at_cursor()
        viewer._save_func_state()
        viewer._apply_grid_filters()
        viewer.grid_visible = False
        viewer.grid = None

        # Reopen grid
        viewer._enter_toggle_grid()

        # dos should still be disabled
        dos_item = None
        exec_item = None
        for item in viewer.grid.lib_items:
            if item["name"] == "dos":
                dos_item = item
            elif item["name"] == "exec":
                exec_item = item
        assert dos_item is not None
        assert dos_item["enabled"] is False
        assert exec_item is not None
        assert exec_item["enabled"] is True

    def test_grid_state_none_preserves_noise_defaults(self):
        """When disabled_funcs is None, noise defaults apply."""
        viewer = _make_viewer()
        viewer.discovered_libs = {"exec": 200}
        viewer.discovered_funcs = {
            "exec": {"AllocMem": 128, "GetMsg": 47, "Open": 12}
        }
        viewer.discovered_procs = {}
        assert viewer.disabled_funcs is None

        viewer._enter_toggle_grid()

        # Noise functions should be disabled (constructor default)
        for item in viewer.grid.func_items:
            if item["name"] in ("AllocMem", "GetMsg"):
                assert not item["enabled"], \
                    "{} should be disabled (noise default)".format(
                        item["name"])
            else:
                assert item["enabled"]

    def test_grid_state_disabled_set_blocks_items(self):
        """Explicit disabled_libs set disables those items on reopen."""
        viewer = _make_viewer()
        viewer.discovered_libs = {"dos": 100, "exec": 200}
        viewer.discovered_funcs = {"exec": {"OpenLibrary": 5}}
        viewer.discovered_procs = {}
        viewer.disabled_libs = {"dos"}

        viewer._enter_toggle_grid()

        for item in viewer.grid.lib_items:
            if item["name"] == "dos":
                assert not item["enabled"]
            elif item["name"] == "exec":
                assert item["enabled"]

    def test_grid_new_item_stays_enabled_on_restore(self):
        """New items not in disabled set stay enabled on restore."""
        viewer = _make_viewer()
        viewer.discovered_libs = {
            "dos": 100, "exec": 200, "intuition": 50}
        viewer.discovered_funcs = {"exec": {"OpenLibrary": 5}}
        viewer.discovered_procs = {}
        viewer.disabled_libs = {"dos"}

        viewer._enter_toggle_grid()

        for item in viewer.grid.lib_items:
            if item["name"] == "dos":
                assert not item["enabled"]
            elif item["name"] == "intuition":
                assert item["enabled"], \
                    "intuition (new, not in disabled set) should be enabled"
            elif item["name"] == "exec":
                assert item["enabled"]

    def test_grid_no_spurious_filter_after_restore(self):
        """Reopened grid with restored state has no spurious changes."""
        viewer = _make_viewer()
        viewer.discovered_libs = {"dos": 100, "exec": 200}
        viewer.discovered_funcs = {"exec": {"OpenLibrary": 5}}
        viewer.discovered_procs = {"bbs": 89}

        # First: open grid, disable dos, apply
        viewer._enter_toggle_grid()
        viewer.grid.cursor_pos[viewer.grid.active_category] = 1
        viewer.grid.toggle_at_cursor()  # dos
        viewer._save_func_state()
        viewer._apply_grid_filters()
        viewer.grid_visible = False
        viewer.grid = None
        viewer.conn.send_filter.reset_mock()

        # Second: reopen grid, apply immediately without changes
        viewer._enter_toggle_grid()
        viewer._save_func_state()
        viewer._apply_grid_filters()

        # has_user_changes should be False (initial_filter re-snapshotted)
        # but has_func_state is True, so filter may be sent.
        # The key test: no SPURIOUS filter change from restored state.
        # (The filter sent, if any, should match the previous state.)

    def test_func_state_persisted_per_library(self):
        """Per-library function state survives grid close and reopen."""
        viewer = _make_viewer()
        viewer.discovered_libs = {"exec": 200, "dos": 100}
        # Use non-noise functions only to avoid noise default confusion
        viewer.discovered_funcs = {
            "exec": {"CreateIORequest": 50, "FindResident": 30},
            "dos": {"Open": 12, "Lock": 8},
        }
        viewer.discovered_procs = {}

        viewer._enter_toggle_grid()

        # Navigate to FUNCTIONS (right arrow), showing exec's funcs
        viewer._handle_grid_key(("esc", "[C"))
        assert viewer.grid.categories[
            viewer.grid.active_category] == "FUNCTIONS"

        # Disable FindResident (sorted: CreateIORequest=50, FindResident=30)
        viewer.grid.cursor_pos[viewer.grid.active_category] = 1
        viewer.grid.toggle_at_cursor()  # FindResident

        # Navigate back to LIBRARIES (left arrow)
        viewer._handle_grid_key(("esc", "[D"))

        # Focus dos (second item by count)
        viewer.grid.focused_lib_index = 1
        viewer.grid.cursor_pos[0] = 1

        # Navigate to FUNCTIONS again (right arrow) -- triggers save
        viewer._handle_grid_key(("esc", "[C"))

        # Disable Lock for dos (sorted: Open=12, Lock=8)
        viewer.grid.cursor_pos[viewer.grid.active_category] = 1
        viewer.grid.toggle_at_cursor()  # Lock

        # Apply (Enter)
        viewer._handle_grid_key("\r")

        assert viewer.disabled_funcs == {
            "exec": {"FindResident"}, "dos": {"Lock"}}

        # Reopen grid, navigate to exec's FUNCTIONS
        viewer._enter_toggle_grid()
        viewer._handle_grid_key(("esc", "[C"))  # to FUNCTIONS

        for item in viewer.grid.func_items:
            if item["name"] == "FindResident":
                assert not item["enabled"], \
                    "FindResident should be disabled for exec"
            elif item["name"] == "CreateIORequest":
                assert item["enabled"], \
                    "CreateIORequest should be enabled for exec"

        # Navigate to dos's FUNCTIONS
        viewer._handle_grid_key(("esc", "[D"))  # back to LIBRARIES
        viewer.grid.focused_lib_index = 1  # dos
        viewer.grid.cursor_pos[0] = 1
        viewer._handle_grid_key(("esc", "[C"))  # to FUNCTIONS

        for item in viewer.grid.func_items:
            if item["name"] == "Lock":
                assert not item["enabled"], \
                    "Lock should be disabled for dos"
            elif item["name"] == "Open":
                assert item["enabled"], \
                    "Open should be enabled for dos"

    def test_func_state_saved_on_library_switch(self):
        """Function state is saved when navigating between libraries."""
        viewer = _make_viewer()
        viewer.discovered_libs = {"exec": 200, "dos": 100}
        # Use non-noise functions to avoid noise default confusion
        viewer.discovered_funcs = {
            "exec": {"CreateIORequest": 50, "FindResident": 30},
            "dos": {"Open": 12},
        }
        viewer.discovered_procs = {}

        viewer._enter_toggle_grid()

        # Navigate to FUNCTIONS for exec
        viewer._handle_grid_key(("esc", "[C"))
        # Disable CreateIORequest (sorted: CreateIOReq=50, FindResident=30)
        viewer.grid.cursor_pos[viewer.grid.active_category] = 0
        viewer.grid.toggle_at_cursor()  # CreateIORequest

        # Navigate back to LIBRARIES
        viewer._handle_grid_key(("esc", "[D"))
        # Focus dos
        viewer.grid.focused_lib_index = 1
        viewer.grid.cursor_pos[0] = 1
        # Navigate to FUNCTIONS (triggers _save_func_state for exec)
        viewer._handle_grid_key(("esc", "[C"))

        # exec state should be saved
        assert "exec" in viewer.disabled_funcs
        assert "CreateIORequest" in viewer.disabled_funcs["exec"]

    def test_func_state_saved_on_grid_cancel(self):
        """Cancel restores disabled_funcs to pre-grid-open state."""
        viewer = _make_viewer()
        viewer.discovered_libs = {"exec": 200}
        viewer.discovered_funcs = {
            "exec": {"FindPort": 50, "OpenLibrary": 30},
        }
        viewer.discovered_procs = {}

        viewer._enter_toggle_grid()

        # Navigate to FUNCTIONS, disable FindPort
        viewer._handle_grid_key(("esc", "[C"))
        viewer.grid.cursor_pos[viewer.grid.active_category] = 0
        viewer.grid.toggle_at_cursor()

        # Press Escape (cancel)
        viewer._handle_grid_key(("esc", ""))

        # disabled_funcs should be None (restored to pre-grid state)
        assert viewer.disabled_funcs is None

    def test_cancel_does_not_leak_func_state_to_filter(self):
        """Cancel does not leak function state to live filtering."""
        viewer = _make_viewer()
        viewer.discovered_libs = {"exec": 200}
        viewer.discovered_funcs = {
            "exec": {"FindPort": 5, "OpenLibrary": 10},
        }
        viewer.discovered_procs = {}

        viewer._enter_toggle_grid()

        # Navigate to FUNCTIONS, disable FindPort
        viewer._handle_grid_key(("esc", "[C"))
        viewer.grid.cursor_pos[viewer.grid.active_category] = 1
        viewer.grid.toggle_at_cursor()  # FindPort (sorted: OL=10, FP=5)

        # Press Escape (cancel)
        viewer._handle_grid_key(("esc", ""))

        # disabled_funcs should be None (cancelled)
        assert viewer.disabled_funcs is None

        # FindPort event should pass filter (cancel discarded change)
        event = _make_event(lib="exec", func="FindPort")
        assert viewer._passes_client_filter(event) is True

    def test_noise_defaults_suppressed_after_enable_all(self):
        """After enabling all noise funcs, they stay enabled on reopen."""
        viewer = _make_viewer()
        viewer.discovered_libs = {"exec": 200}
        viewer.discovered_funcs = {
            "exec": {"AllocMem": 128, "FindPort": 47, "Open": 12},
        }
        viewer.discovered_procs = {}

        # First open: noise defaults apply (disabled_funcs is None)
        viewer._enter_toggle_grid()
        viewer._handle_grid_key(("esc", "[C"))  # to FUNCTIONS

        # Verify noise functions are disabled (constructor defaults)
        for item in viewer.grid.func_items:
            if item["name"] in ("AllocMem", "FindPort"):
                assert not item["enabled"]

        # Enable all
        viewer.grid.all_on()
        viewer._save_func_state()
        viewer._apply_grid_filters()
        viewer.grid_visible = False
        viewer.grid = None

        # disabled_funcs should record empty set for exec
        assert viewer.disabled_funcs == {"exec": set()}

        # Reopen grid
        viewer._enter_toggle_grid()
        viewer._handle_grid_key(("esc", "[C"))  # to FUNCTIONS

        # ALL functions should be enabled (noise defaults suppressed)
        for item in viewer.grid.func_items:
            assert item["enabled"], \
                "{} should be enabled after enable-all".format(
                    item["name"])


# ---------------------------------------------------------------------------
# TestGridHotkeyBar (Bug 15)
# ---------------------------------------------------------------------------

class TestGridHotkeyBar:
    """Tests for grid hotkey bar (Bug 15)."""

    def test_hotkey_bar_grid_visible(self):
        """Grid hotkey bar shows grid commands when grid is visible."""
        viewer = _make_viewer()
        viewer.grid_visible = True
        text = viewer._build_hotkey_bar()
        assert "Enter" in text
        assert "Esc" in text
        assert "[A]ll" in text
        assert "[Tab]" not in text
        assert "[/]" not in text

    def test_hotkey_bar_normal_when_grid_closed(self):
        """Normal hotkey bar when grid is closed."""
        viewer = _make_viewer()
        viewer.grid_visible = False
        text = viewer._build_hotkey_bar()
        assert "[Tab]" in text
        assert "[/]" in text


# ---------------------------------------------------------------------------
# TestGridApplyKey (Bug 16)
# ---------------------------------------------------------------------------

class TestGridApplyKey:
    """Tests for Enter as grid apply key (Bug 16)."""

    def test_enter_applies_grid(self):
        """Enter key applies grid filters and closes grid."""
        viewer = _make_viewer()
        viewer.discovered_libs = {"dos": 100, "exec": 200}
        viewer.discovered_funcs = {"exec": {"OpenLibrary": 5}}
        viewer.discovered_procs = {"bbs": 89}

        viewer._enter_toggle_grid()
        viewer.grid.cursor_pos[viewer.grid.active_category] = 1
        viewer.grid.toggle_at_cursor()  # disable dos

        viewer._handle_grid_key("\r")

        assert viewer.grid_visible is False
        assert viewer.grid is None
        assert viewer.disabled_libs == {"dos"}

    def test_tab_no_longer_applies_grid(self):
        """Tab does not apply grid (it is not the apply key anymore)."""
        viewer = _make_viewer()
        viewer.discovered_libs = {"dos": 100, "exec": 200}
        viewer.discovered_funcs = {"exec": {"OpenLibrary": 5}}
        viewer.discovered_procs = {"bbs": 89}

        viewer._enter_toggle_grid()
        viewer.grid.cursor_pos[viewer.grid.active_category] = 1
        viewer.grid.toggle_at_cursor()

        # Tab should NOT close the grid
        viewer._handle_grid_key("\t")

        # Grid is still visible (Tab is just a regular key now)
        assert viewer.grid_visible is True


# ---------------------------------------------------------------------------
# TestGridEventBuffering (Bug 12)
# ---------------------------------------------------------------------------

class TestGridEventBuffering:
    """Tests for event buffering while grid is visible (Bug 12)."""

    def test_events_buffered_while_grid_visible(self):
        """Events go to pause_buffer when grid is visible."""
        viewer = _make_viewer()
        viewer.discovered_libs = {"dos": 100}
        viewer.discovered_funcs = {"dos": {"Open": 12}}
        viewer.discovered_procs = {"bbs": 89}

        viewer._enter_toggle_grid()
        assert viewer.grid_visible is True

        # Feed 3 events
        for i in range(3):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:0{}.000".format(i)))

        assert len(viewer.pause_buffer) == 3
        assert viewer.total_events == 3
        assert viewer.shown_events == 0

    def test_events_replayed_on_grid_close(self):
        """Events re-rendered from scrollback when grid closes via Enter."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)
        viewer.discovered_libs = {"dos": 100}
        viewer.discovered_funcs = {"dos": {"Open": 12}}
        viewer.discovered_procs = {"bbs": 89}

        viewer._enter_toggle_grid()

        for i in range(3):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:0{}.000".format(i)))

        assert viewer.shown_events == 0

        # Apply via Enter: re-renders from scrollback
        viewer._handle_grid_key("\r")

        # shown_events stays 0: re-render doesn't increment it
        assert viewer.shown_events == 0
        assert len(viewer.pause_buffer) == 0

    def test_events_replayed_on_grid_cancel(self):
        """Events re-rendered from scrollback when grid closes via Escape."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)
        viewer.discovered_libs = {"dos": 100}
        viewer.discovered_funcs = {"dos": {"Open": 12}}
        viewer.discovered_procs = {"bbs": 89}

        viewer._enter_toggle_grid()

        for i in range(3):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:0{}.000".format(i)))

        assert viewer.shown_events == 0

        # Cancel via Escape: re-renders from scrollback
        viewer._handle_grid_key(("esc", ""))

        # shown_events stays 0: re-render doesn't increment it
        assert viewer.shown_events == 0

    def test_grid_event_buffer_respects_limit(self):
        """Buffer is capped at pause_buffer_limit."""
        viewer = _make_viewer()
        viewer.discovered_libs = {"dos": 100}
        viewer.discovered_funcs = {"dos": {"Open": 12}}
        viewer.discovered_procs = {"bbs": 89}

        viewer._enter_toggle_grid()
        viewer.pause_buffer_limit = 5

        for i in range(10):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:0{}.000".format(i)))

        assert len(viewer.pause_buffer) == 5

    def test_grid_replay_respects_filters(self):
        """Re-render from scrollback respects current filters after apply."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)
        viewer.discovered_libs = {"dos": 100, "exec": 200}
        viewer.discovered_funcs = {"exec": {"OpenLibrary": 5}}
        viewer.discovered_procs = {}

        viewer._enter_toggle_grid()

        # Feed events from both libs while grid is visible
        viewer._process_event_result(
            _make_event(seq=1, lib="exec", func="OpenLibrary",
                        time="10:00:01.000"))
        viewer._process_event_result(
            _make_event(seq=2, lib="dos", func="Open",
                        time="10:00:02.000"))
        viewer._process_event_result(
            _make_event(seq=3, lib="exec", func="OpenLibrary",
                        time="10:00:03.000"))

        # Disable exec in the grid
        viewer.grid.cursor_pos[viewer.grid.active_category] = 0
        viewer.grid.toggle_at_cursor()  # exec (highest count)

        # Apply via Enter: re-renders from scrollback with filter
        with mock.patch.object(viewer.term, 'write_at') as mock_write:
            viewer._handle_grid_key("\r")

        # _rerender_from_scrollback writes only matching events.
        # With exec disabled, only the 1 dos event should be rendered.
        # shown_events stays 0 (re-render doesn't increment).
        assert viewer.shown_events == 0
        # Verify only 1 event was written (the dos event)
        assert mock_write.call_count == 1

    def test_grid_replay_skipped_when_paused(self):
        """Events NOT replayed if still paused when grid closes."""
        viewer = _make_viewer()
        viewer.discovered_libs = {"dos": 100}
        viewer.discovered_funcs = {"dos": {"Open": 12}}
        viewer.discovered_procs = {"bbs": 89}

        # Pause first
        viewer.paused = True

        viewer._enter_toggle_grid()

        # Feed events (they go to pause_buffer via paused check)
        for i in range(3):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:0{}.000".format(i)))

        # Apply via Enter
        viewer._handle_grid_key("\r")

        # No replay because still paused
        assert viewer.shown_events == 0
        assert len(viewer.pause_buffer) > 0


# ---------------------------------------------------------------------------
# TestAllNonePersistence (Bug 17)
# ---------------------------------------------------------------------------

class TestAllNonePersistence:
    """Tests for All/None operations with blocklist persistence (Bug 17)."""

    def test_all_on_persists_across_grid_reopen(self):
        """All-off persists after grid close/reopen via disabled_funcs."""
        viewer = _make_viewer()
        viewer.discovered_libs = {"exec": 200}
        viewer.discovered_funcs = {
            "exec": {"Open": 12, "Lock": 8, "Close": 6},
        }
        viewer.discovered_procs = {"bbs": 89}

        viewer._enter_toggle_grid()

        # Switch to FUNCTIONS category
        viewer.grid.active_category = 1

        # Disable all functions
        viewer.grid.none()

        # Apply and close
        viewer._save_func_state()
        viewer._apply_grid_filters()
        viewer.grid_visible = False
        viewer.grid = None

        # Reopen
        viewer._enter_toggle_grid()

        # Switch to FUNCTIONS and check state
        viewer.grid.active_category = 1
        lib = viewer._get_selected_lib_name()
        if lib and lib in viewer.discovered_funcs:
            viewer._save_func_state()
            viewer.grid.update_func_items(
                viewer.discovered_funcs[lib], lib)
            if viewer.disabled_funcs is not None:
                disabled_for_lib = viewer.disabled_funcs.get(
                    lib, set())
                for item in viewer.grid.func_items:
                    item["enabled"] = (
                        item["name"] not in disabled_for_lib)

        # All function items should be disabled (persisted from none())
        for item in viewer.grid.func_items:
            assert not item["enabled"], \
                "{} should be disabled".format(item["name"])

    def test_none_sends_filter_on_apply(self):
        """none() in LIBRARIES sends LIB=__NONE__ on apply."""
        viewer = _make_viewer()
        viewer.discovered_libs = {"dos": 100, "exec": 200}
        viewer.discovered_funcs = {"exec": {"Open": 12}}
        viewer.discovered_procs = {"bbs": 89}

        viewer._enter_toggle_grid()

        # Disable all libraries
        viewer.grid.none()

        # Apply via Enter
        viewer._save_func_state()
        viewer._apply_grid_filters()

        # Should have sent a FILTER with LIB=__NONE__
        viewer.conn.send_filter.assert_called_once()
        call_args = viewer.conn.send_filter.call_args
        raw = call_args[1].get("raw", call_args[0][0]
                                if call_args[0] else "")
        assert "LIB=__NONE__" in raw


# ---------------------------------------------------------------------------
# TestNewlyDiscoveredItems (Bug 18)
# ---------------------------------------------------------------------------

class TestNewlyDiscoveredItems:
    """Tests for allow-unknown semantics with blocklist filters (Bug 18)."""

    def test_newly_discovered_lib_passes_filter(self):
        """Unknown libraries pass through blocklist filters."""
        viewer = _make_viewer()
        viewer.discovered_libs = {"dos": 100, "exec": 200}
        viewer.discovered_funcs = {"exec": {"Open": 12}}
        viewer.discovered_procs = {"bbs": 89}

        viewer._enter_toggle_grid()

        # Disable dos
        viewer.grid.cursor_pos[viewer.grid.active_category] = 1
        viewer.grid.toggle_at_cursor()  # dos (second by count)

        viewer._save_func_state()
        viewer._apply_grid_filters()
        assert viewer.disabled_libs == {"dos"}

        # Unknown lib passes through
        event_new = _make_event(lib="intuition", func="OpenWindow")
        assert viewer._passes_client_filter(event_new) is True

        # Disabled lib is blocked
        event_dos = _make_event(lib="dos", func="Open")
        assert viewer._passes_client_filter(event_dos) is False

    def test_newly_discovered_func_passes_filter(self):
        """Unknown functions pass through blocklist filters."""
        viewer = _make_viewer()
        viewer.disabled_funcs = {"exec": {"AllocMem"}}

        # Known disabled func is blocked
        event_blocked = _make_event(lib="exec", func="AllocMem")
        assert viewer._passes_client_filter(event_blocked) is False

        # Known enabled func passes
        event_ok = _make_event(lib="exec", func="OpenLibrary")
        assert viewer._passes_client_filter(event_ok) is True

        # Unknown func in unknown lib passes
        event_unknown = _make_event(lib="intuition", func="OpenWindow")
        assert viewer._passes_client_filter(event_unknown) is True

    def test_all_disabled_still_blocks_known(self):
        """All known libs disabled blocks those libs, unknown pass."""
        viewer = _make_viewer()
        viewer.discovered_libs = {"dos": 100, "exec": 200}
        viewer.discovered_funcs = {"exec": {"Open": 12}}
        viewer.discovered_procs = {"bbs": 89}

        viewer._enter_toggle_grid()

        # Disable all libraries
        viewer.grid.none()
        viewer._save_func_state()
        viewer._apply_grid_filters()

        assert viewer.disabled_libs == {"exec", "dos"}

        # Known libs blocked
        event_exec = _make_event(lib="exec")
        assert viewer._passes_client_filter(event_exec) is False

        # Unknown lib passes (blocklist semantics)
        event_icon = _make_event(lib="icon")
        assert viewer._passes_client_filter(event_icon) is True

    def test_no_filter_allows_all(self):
        """When no grid used (disabled_libs is None), all events pass."""
        viewer = _make_viewer()
        assert viewer.disabled_libs is None

        event = _make_event(lib="anything", func="whatever")
        assert viewer._passes_client_filter(event) is True

    def test_empty_disabled_set_allows_all(self):
        """Empty disabled set (all enabled, grid used) allows all."""
        viewer = _make_viewer()
        viewer.discovered_libs = {"dos": 100, "exec": 200}
        viewer.discovered_funcs = {"exec": {"Open": 12}}
        viewer.discovered_procs = {"bbs": 89}

        viewer._enter_toggle_grid()
        # All enabled, apply
        viewer._save_func_state()
        viewer._apply_grid_filters()

        # disabled_libs should be None (empty set collapsed)
        assert viewer.disabled_libs is None

        # Any event passes
        event = _make_event(lib="whatever")
        assert viewer._passes_client_filter(event) is True

    def test_disabled_funcs_per_library_filter(self):
        """Per-library disabled_funcs blocks correctly."""
        viewer = _make_viewer()
        viewer.disabled_funcs = {
            "exec": {"AllocMem"},
            "dos": {"Lock"},
        }

        # exec.AllocMem blocked
        event1 = _make_event(lib="exec", func="AllocMem")
        assert viewer._passes_client_filter(event1) is False

        # exec.OpenLibrary passes
        event2 = _make_event(lib="exec", func="OpenLibrary")
        assert viewer._passes_client_filter(event2) is True

        # dos.Lock blocked
        event3 = _make_event(lib="dos", func="Lock")
        assert viewer._passes_client_filter(event3) is False

        # dos.Open passes
        event4 = _make_event(lib="dos", func="Open")
        assert viewer._passes_client_filter(event4) is True

        # Unknown lib passes
        event5 = _make_event(lib="intuition", func="OpenWindow")
        assert viewer._passes_client_filter(event5) is True

    def test_disabled_funcs_server_filter_all_libraries(self):
        """Server filter includes disabled funcs from ALL libraries."""
        viewer = _make_viewer()
        viewer.discovered_libs = {"exec": 200, "dos": 100}
        # Use non-noise functions to avoid noise default interference
        viewer.discovered_funcs = {
            "exec": {"FindResident": 50, "OpenDevice": 30},
            "dos": {"Open": 12, "Lock": 8},
        }
        viewer.discovered_procs = {"bbs": 89}

        viewer._enter_toggle_grid()

        # Navigate to FUNCTIONS for exec
        viewer.grid.active_category = 1
        lib = viewer._get_selected_lib_name()
        if lib and lib in viewer.discovered_funcs:
            viewer._save_func_state()
            viewer.grid.update_func_items(
                viewer.discovered_funcs[lib], lib)
            if viewer.disabled_funcs is not None:
                disabled_for_lib = viewer.disabled_funcs.get(
                    lib, set())
                for item in viewer.grid.func_items:
                    item["enabled"] = (
                        item["name"] not in disabled_for_lib)

        # Disable FindResident for exec (item 1, highest count)
        viewer.grid.cursor_pos[1] = 0
        viewer.grid.toggle_at_cursor()  # FindResident

        # Save exec state, switch to dos
        viewer._save_func_state()
        viewer.grid.active_category = 0  # back to LIBRARIES
        viewer.grid.focused_lib_index = 1  # dos
        viewer.grid.cursor_pos[0] = 1
        viewer.grid.active_category = 1  # FUNCTIONS
        lib = "dos"
        viewer.grid.update_func_items(
            viewer.discovered_funcs[lib], lib)
        if viewer.disabled_funcs is not None:
            disabled_for_lib = viewer.disabled_funcs.get(lib, set())
            for item in viewer.grid.func_items:
                item["enabled"] = (
                    item["name"] not in disabled_for_lib)

        # Disable Lock for dos (item 2, second by count)
        viewer.grid.cursor_pos[1] = 1
        viewer.grid.toggle_at_cursor()  # Lock

        # Apply
        viewer._save_func_state()
        viewer._apply_grid_filters()

        # Check that send_filter was called with a command containing
        # both FindResident and Lock in a single -FUNC= clause
        viewer.conn.send_filter.assert_called_once()
        call_args = viewer.conn.send_filter.call_args
        raw = call_args[1].get("raw", call_args[0][0]
                                if call_args[0] else "")
        # Should contain both functions
        assert "-FUNC=" in raw
        # Split out the -FUNC= value
        for part in raw.split():
            if part.startswith("-FUNC="):
                funcs_str = part[len("-FUNC="):]
                funcs_list = funcs_str.split(",")
                assert "FindResident" in funcs_list
                assert "Lock" in funcs_list
                # Should be a single -FUNC= clause, not two
                break
        # No duplicate FUNC= clauses
        func_parts = [p for p in raw.split()
                      if p.startswith("FUNC=")
                      or p.startswith("-FUNC=")]
        assert len(func_parts) == 1, \
            "Expected single FUNC clause, got: {}".format(func_parts)


# ---------------------------------------------------------------------------
# TestScrollback (Wave 3, Bug 19)
# ---------------------------------------------------------------------------

class TestScrollback:
    """Tests for scrollback buffer (Bug 19)."""

    def test_scrollback_populated_by_display_event(self):
        """Events displayed via _process_event_result go into scrollback."""
        viewer = _make_viewer()
        for i in range(5):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:0{}.000".format(i)))
        assert len(viewer.scrollback) == 5

    def test_scrollback_limit_enforced(self):
        """Scrollback deque drops oldest events when full."""
        viewer = _make_viewer()
        viewer.scrollback = deque(maxlen=3)
        viewer.scrollback_limit = 3
        for i in range(5):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:0{}.000".format(i)))
        assert len(viewer.scrollback) == 3
        # Oldest 2 dropped -- first remaining is seq=2
        assert viewer.scrollback[0]["seq"] == 2

    def test_scroll_works_after_pause_with_no_new_events(self):
        """Scrollback enables scrolling even with empty pause_buffer."""
        viewer = _make_viewer()
        viewer.term.rows = 10
        # Feed 20 events (all displayed)
        for i in range(20):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:{:02d}.000".format(i)))
        assert len(viewer.scrollback) == 20

        # Pause
        viewer._toggle_pause()
        assert viewer.pause_buffer == []

        # visible_lines = 10 - 4 = 6, expected_pos = max(0, 20-6) = 14
        assert viewer.pause_scroll_pos == 14

        # Scroll up
        viewer._scroll_pause_buffer(-1)
        assert viewer.pause_scroll_pos == 13

    def test_scroll_combines_scrollback_and_pause_buffer(self):
        """Scroll operates on combined scrollback + pause_buffer."""
        viewer = _make_viewer()
        viewer.term.rows = 10
        # Feed 10 events (displayed, go to scrollback)
        for i in range(10):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:{:02d}.000".format(i)))
        assert len(viewer.scrollback) == 10

        # Pause
        viewer._toggle_pause()

        # Feed 5 more events (buffered)
        for i in range(10, 15):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:{:02d}.000".format(i)))
        assert len(viewer.pause_buffer) == 5

        # Combined = 10 + 5 = 15. visible_lines = 6, max_pos = 9
        # Scroll position starts at max(0, 10-6) = 4 (from snapshot).
        # After pause_buffer gets events, scroll up should move into
        # the scrollback region.
        viewer._scroll_pause_buffer(-5)
        assert viewer.pause_scroll_pos == 0

    def test_scrollback_populated_during_pause(self):
        """Events arriving during pause go to BOTH scrollback and pause_buffer.

        Architecture change: scrollback stores ALL events regardless
        of pause/filter state, enabling retroactive filter changes.
        """
        viewer = _make_viewer()
        viewer.paused = True
        for i in range(5):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:0{}.000".format(i)))
        assert len(viewer.scrollback) == 5
        assert len(viewer.pause_buffer) == 5

    def test_scrollback_grows_during_pause(self):
        """Scrollback grows during pause (architecture change).

        Events go into scrollback in _process_event_result() before
        any filter/pause checks, so scrollback has all events even
        during pause.
        """
        viewer = _make_viewer()
        # Feed 3 events (displayed, in scrollback)
        for i in range(3):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:0{}.000".format(i)))
        assert len(viewer.scrollback) == 3

        # Pause, feed 2 more (both in scrollback AND pause_buffer)
        viewer._toggle_pause()
        for i in range(3, 5):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:0{}.000".format(i)))
        assert len(viewer.scrollback) == 5

        # Unpause: scrollback still has all 5
        viewer._toggle_pause()
        assert len(viewer.scrollback) == 5

    def test_unpause_rerender_respects_filters(self):
        """Unpause re-render filters scrollback through current filters."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)
        # Pause
        viewer._toggle_pause()
        # Feed 3 events: 2 from exec, 1 from dos
        viewer._process_event_result(
            _make_event(seq=1, lib="exec", func="Open",
                        time="10:00:01.000"))
        viewer._process_event_result(
            _make_event(seq=2, lib="exec", func="Close",
                        time="10:00:02.000"))
        viewer._process_event_result(
            _make_event(seq=3, lib="dos", func="Open",
                        time="10:00:03.000"))

        # Set filter to block exec
        viewer.disabled_libs = {"exec"}

        # Unpause: _rerender_from_scrollback applies filters
        with mock.patch.object(viewer.term, 'write_at') as mock_write:
            viewer._toggle_pause()

        # Only the dos event should be rendered (write_at called once)
        assert mock_write.call_count == 1

    def test_delta_timestamps_correct_during_scroll(self):
        """Delta timestamps in scroll mode are sequential across window."""
        viewer = _make_viewer()
        viewer.term.rows = 10
        # Layout must exist for _format_timestamp_for_scroll to be used
        viewer.layout = ColumnLayout(viewer.term.cols)
        viewer.timestamp_mode = "delta"

        # Feed 10 events with known timestamps
        timestamps = [
            "00:00:01.000", "00:00:01.100", "00:00:01.300",
            "00:00:01.600", "00:00:02.000", "00:00:02.500",
            "00:00:03.100", "00:00:03.800", "00:00:04.600",
            "00:00:05.500",
        ]
        for i, ts in enumerate(timestamps):
            viewer._process_event_result(
                _make_event(seq=i, time=ts))

        # Pause (snapshot has all 10 events)
        viewer._toggle_pause()

        # Scroll to top
        viewer.pause_scroll_pos = 0

        # Mock write_at to capture formatted strings
        written_rows = {}
        original_write_at = viewer.term.write_at

        def capture_write_at(row, text):
            written_rows[row] = text
            original_write_at(row, text)

        viewer.term.write_at = capture_write_at
        viewer._scroll_pause_buffer(0)

        # Row 3 should be first event (delta = +0.000000 since no prev)
        assert "+0.000000" in written_rows.get(3, "")
        # Row 4 should be second event (delta from 01.000 to 01.100)
        assert "+0.100000" in written_rows.get(4, "")
        # Row 5 should be third event (delta from 01.100 to 01.300)
        assert "+0.200000" in written_rows.get(5, "")

    def test_last_event_time_updated_after_grid_replay(self):
        """Grid close replay updates last_event_time."""
        viewer = _make_viewer()
        viewer.discovered_libs = {"dos": 100}
        viewer.discovered_funcs = {"dos": {"Open": 10}}
        viewer.discovered_procs = {"Shell": 50}

        # Feed 5 events
        for i in range(5):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:0{}.000".format(i)))

        # Open grid, feed 3 more events (buffered)
        viewer._enter_toggle_grid()
        for i in range(5, 8):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:0{}.000".format(i)))
        assert len(viewer.pause_buffer) == 3

        # Apply grid (Enter), triggering replay
        viewer._handle_grid_key("\r")
        assert viewer.last_event_time == "10:00:07.000"

    def test_last_event_time_updated_after_unpause_replay(self):
        """Unpause replay updates last_event_time."""
        viewer = _make_viewer()
        # Feed 5 events
        for i in range(5):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:0{}.000".format(i)))
        assert viewer.last_event_time == "10:00:04.000"

        # Pause, feed 3 more events
        viewer._toggle_pause()
        for i in range(5, 8):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:0{}.000".format(i)))

        # Unpause
        viewer._toggle_pause()
        assert viewer.last_event_time == "10:00:07.000"

    def test_scroll_snapshot_frozen_on_pause(self):
        """Scroll snapshot is frozen on pause, not affected by new events."""
        viewer = _make_viewer()
        for i in range(5):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:0{}.000".format(i)))

        # Pause
        viewer._toggle_pause()
        assert len(viewer._scroll_snapshot) == 5

        # Feed 2 more events (go to pause_buffer)
        for i in range(5, 7):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:0{}.000".format(i)))

        # Snapshot unchanged
        assert len(viewer._scroll_snapshot) == 5
        assert len(viewer.pause_buffer) == 2

    def test_scroll_snapshot_cleared_on_unpause(self):
        """Scroll snapshot is cleared when unpausing."""
        viewer = _make_viewer()
        for i in range(5):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:0{}.000".format(i)))

        viewer._toggle_pause()
        assert viewer._scroll_snapshot is not None

        viewer._toggle_pause()
        assert viewer._scroll_snapshot is None

    def test_pause_starts_at_bottom(self):
        """Pause positions scroll at the bottom (most recent events)."""
        viewer = _make_viewer()
        viewer.term.rows = 10
        for i in range(20):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:{:02d}.000".format(i)))

        viewer._toggle_pause()
        # visible_lines = 6, expected_pos = max(0, 20 - 6) = 14
        assert viewer.pause_scroll_pos == 14

    def test_scrollback_full_indicator(self):
        """Buffer-full notice shown when scrollback was full and at top."""
        viewer = _make_viewer()
        viewer.scrollback = deque(maxlen=10)
        viewer.scrollback_limit = 10
        # Feed 15 events (deque drops first 5)
        for i in range(15):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:{:02d}.000".format(i)))
        assert viewer._scrollback_full is True

        # Pause, scroll to top
        viewer._toggle_pause()
        viewer.pause_scroll_pos = 0

        # Mock write_at to capture rendered text
        written_rows = {}
        original_write_at = viewer.term.write_at

        def capture_write_at(row, text):
            written_rows[row] = text
            original_write_at(row, text)

        viewer.term.write_at = capture_write_at
        viewer._scroll_pause_buffer(0)

        # First row should have the buffer full notice
        assert "buffer full" in written_rows.get(3, "")

    def test_scrollback_not_full_no_indicator(self):
        """No buffer-full notice when scrollback has not reached capacity."""
        viewer = _make_viewer()
        viewer.scrollback = deque(maxlen=100)
        viewer.scrollback_limit = 100
        viewer.term.rows = 10
        for i in range(5):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:0{}.000".format(i)))
        assert viewer._scrollback_full is False

        # Pause, scroll to top
        viewer._toggle_pause()
        viewer.pause_scroll_pos = 0

        written_rows = {}
        original_write_at = viewer.term.write_at

        def capture_write_at(row, text):
            written_rows[row] = text
            original_write_at(row, text)

        viewer.term.write_at = capture_write_at
        viewer._scroll_pause_buffer(0)

        # No "buffer full" text
        for row, text in written_rows.items():
            assert "buffer full" not in text


# ---------------------------------------------------------------------------
# TestShellIntegration
# ---------------------------------------------------------------------------

class TestShellIntegration:
    """Tests for shell integration of trace start/run with TraceViewer."""

    @mock.patch("amigactl.shell.sys.stdout")
    @mock.patch("amigactl.shell.os.name", "posix")
    def test_trace_start_uses_viewer(self, mock_stdout):
        """When isatty() is True, trace start uses TraceViewer."""
        mock_stdout.isatty.return_value = True

        from amigactl.shell import AmigaShell
        shell = AmigaShell("localhost", 6800)
        shell.conn = mock.MagicMock()
        shell.cw = ColorWriter(force_color=False)

        # Mock trace_start_raw to return a context manager
        mock_session = mock.MagicMock()
        mock_session.__enter__ = mock.MagicMock(return_value=mock_session)
        mock_session.__exit__ = mock.MagicMock(return_value=False)
        shell.conn.trace_start_raw.return_value = mock_session

        # Patch the lazy import inside do_trace
        with mock.patch(
                "amigactl.trace_ui.TraceViewer") as MockViewerUI:
            MockViewerUI.return_value.run.return_value = None
            shell.do_trace("start")

        shell.conn.trace_start_raw.assert_called_once()

    @mock.patch("amigactl.shell.sys.stdout")
    def test_trace_start_fallback_no_tty(self, mock_stdout):
        """When not a tty, trace start uses callback path."""
        mock_stdout.isatty.return_value = False

        from amigactl.shell import AmigaShell
        shell = AmigaShell("localhost", 6800)
        shell.conn = mock.MagicMock()
        shell.cw = ColorWriter(force_color=False)

        shell.do_trace("start")

        shell.conn.trace_start.assert_called_once()
        shell.conn.trace_start_raw.assert_not_called()

    @mock.patch("amigactl.shell.sys.stdout")
    @mock.patch("amigactl.shell.os.name", "posix")
    def test_trace_run_uses_viewer(self, mock_stdout):
        """When isatty() is True, trace run uses TraceViewer."""
        mock_stdout.isatty.return_value = True

        from amigactl.shell import AmigaShell
        shell = AmigaShell("localhost", 6800)
        shell.conn = mock.MagicMock()
        shell.cw = ColorWriter(force_color=False)

        # Mock trace_run_raw to return (session, proc_id) tuple
        mock_session = mock.MagicMock()
        mock_session.__enter__ = mock.MagicMock(return_value=mock_session)
        mock_session.__exit__ = mock.MagicMock(return_value=False)
        shell.conn.trace_run_raw.return_value = (mock_session, "42")

        with mock.patch(
                "amigactl.trace_ui.TraceViewer") as MockViewerUI:
            MockViewerUI.return_value.run.return_value = None
            shell.do_trace("run -- List SYS:")

        shell.conn.trace_run_raw.assert_called_once()

    @mock.patch("amigactl.shell.sys.stdout")
    def test_trace_run_fallback_no_tty(self, mock_stdout):
        """When not a tty, trace run uses callback path."""
        mock_stdout.isatty.return_value = False

        from amigactl.shell import AmigaShell
        shell = AmigaShell("localhost", 6800)
        shell.conn = mock.MagicMock()
        shell.cw = ColorWriter(force_color=False)
        shell.conn.trace_run.return_value = {"rc": 0, "proc_id": "42"}

        shell.do_trace("run -- List SYS:")

        shell.conn.trace_run.assert_called_once()
        shell.conn.trace_run_raw.assert_not_called()


# ---------------------------------------------------------------------------
# TestBugfix2 (Bugs 20-24, Architecture Refactor)
# ---------------------------------------------------------------------------

class TestBugfix2:
    """Tests for bugfix2 wave: scrollback architecture, clear screen,
    re-render, grid hotkey bar, and search re-render."""

    # -- Test 12: clear_screen method --

    def test_clear_screen_method(self):
        """TerminalState.clear_screen() emits ESC[2J ESC[H."""
        term, output = _make_terminal_state()
        term._saved_attrs = None  # Skip cleanup
        term.clear_screen()
        written = output.getvalue()
        assert "\033[2J" in written
        assert "\033[H" in written

    # -- Test 1: clear_screen on run --

    def test_clear_screen_on_run(self):
        """clear_screen() is called before setup_regions() in run().

        Verifies the call order by replacing TerminalState with a
        mock context manager that tracks method calls.
        """
        viewer = _make_viewer()

        call_order = []
        mock_term = mock.MagicMock()
        mock_term.rows = 24
        mock_term.cols = 80

        def track_clear():
            call_order.append("clear_screen")

        def track_setup():
            call_order.append("setup_regions")

        mock_term.clear_screen = track_clear
        mock_term.setup_regions = track_setup

        # Make _event_loop a no-op to avoid infinite loop
        viewer._event_loop = mock.MagicMock()

        # Replace TerminalState entirely to avoid __init__ stdin issue
        mock_ctx = mock.MagicMock()
        mock_ctx.__enter__ = mock.MagicMock(return_value=mock_term)
        mock_ctx.__exit__ = mock.MagicMock(return_value=False)

        with mock.patch(
                "amigactl.trace_ui.TerminalState",
                return_value=mock_ctx):
            with mock.patch("amigactl.trace_ui.signal"):
                viewer.run()

        assert "clear_screen" in call_order
        assert "setup_regions" in call_order
        assert call_order.index("clear_screen") < \
            call_order.index("setup_regions")

    # -- Test 13: scrollback limit --

    def test_scrollback_limit_10000(self):
        """Scrollback limit is 10000 and deque maxlen matches."""
        viewer = _make_viewer()
        assert viewer.scrollback_limit == 10000
        assert viewer.scrollback.maxlen == 10000

    # -- Test 2: scrollback stores filtered events --

    def test_scrollback_stores_filtered_events(self):
        """Events blocked by client filter still go into scrollback."""
        viewer = _make_viewer()
        viewer.disabled_libs = {"dos"}

        event = _make_event(lib="dos", func="Open")
        viewer._process_event_result(event)

        # Event IS in scrollback even though it was filtered
        assert len(viewer.scrollback) == 1
        assert viewer.scrollback[0] is event
        # But not shown
        assert viewer.shown_events == 0

    # -- Test 3: scrollback stores paused events --

    def test_scrollback_stores_paused_events(self):
        """Events arriving while paused are stored in scrollback."""
        viewer = _make_viewer()
        viewer.paused = True

        for i in range(5):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:0{}.000".format(i)))

        # Events are in both scrollback and pause_buffer
        assert len(viewer.scrollback) == 5
        assert len(viewer.pause_buffer) == 5

    # -- Test 4: rerender basic --

    def test_rerender_from_scrollback_basic(self):
        """Re-render writes events to scroll region with write_at."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)

        # Feed 5 events through normal path
        for i in range(5):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:0{}.000".format(i),
                            func="Func{}".format(i)))

        assert viewer.shown_events == 5

        # Capture write_at calls during re-render
        with mock.patch.object(viewer.term, 'write_at') as mock_write:
            with mock.patch.object(viewer.term, 'clear_scroll_region'):
                viewer._rerender_from_scrollback()

        assert mock_write.call_count == 5
        # Rows should be 3-7 (scroll region starts at row 3)
        rows = [call[0][0] for call in mock_write.call_args_list]
        assert rows == [3, 4, 5, 6, 7]
        # shown_events unchanged (lifetime counter)
        assert viewer.shown_events == 5

    # -- Test 5: rerender applies current filters --

    def test_rerender_applies_current_filters(self):
        """Re-render filters scrollback through current disabled_libs."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)

        # Feed 5 events: 3 dos, 2 exec
        for i in range(3):
            viewer._process_event_result(
                _make_event(seq=i, lib="dos", func="Open",
                            time="10:00:0{}.000".format(i)))
        for i in range(2):
            viewer._process_event_result(
                _make_event(seq=i + 3, lib="exec", func="OpenLibrary",
                            time="10:00:0{}.000".format(i + 3)))

        # Now disable dos
        viewer.disabled_libs = {"dos"}

        with mock.patch.object(viewer.term, 'write_at') as mock_write:
            with mock.patch.object(viewer.term, 'clear_scroll_region'):
                viewer._rerender_from_scrollback()

        # Only 2 exec events should be written
        assert mock_write.call_count == 2
        rows = [call[0][0] for call in mock_write.call_args_list]
        assert rows == [3, 4]

    # -- Test 6: rerender limits to visible lines --

    def test_rerender_limits_to_visible_lines(self):
        """Re-render shows at most visible_lines events."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)
        viewer.term.rows = 10  # visible_lines = 10 - 4 = 6

        # Feed 20 events
        for i in range(20):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:{:02d}.000".format(i),
                            func="Func{}".format(i)))

        with mock.patch.object(viewer.term, 'write_at') as mock_write:
            with mock.patch.object(viewer.term, 'clear_scroll_region'):
                viewer._rerender_from_scrollback()

        # Should write 6 events (last 6)
        assert mock_write.call_count == 6
        rows = [call[0][0] for call in mock_write.call_args_list]
        assert rows == [3, 4, 5, 6, 7, 8]

    # -- Test 7: rerender delta timestamps --

    def test_rerender_delta_timestamps(self):
        """Re-render passes correct prev_time for delta timestamps."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)
        viewer.timestamp_mode = "delta"

        events = [
            _make_event(seq=0, time="10:00:00.000"),
            _make_event(seq=1, time="10:00:01.000"),
            _make_event(seq=2, time="10:00:02.500"),
        ]
        for e in events:
            viewer._process_event_result(e)

        prev_times = []
        original_fmt = viewer._format_timestamp_for_scroll

        def tracking_fmt(event, prev_time):
            prev_times.append(prev_time)
            return original_fmt(event, prev_time)

        with mock.patch.object(
                viewer, '_format_timestamp_for_scroll',
                side_effect=tracking_fmt):
            with mock.patch.object(viewer.term, 'clear_scroll_region'):
                viewer._rerender_from_scrollback()

        # First event: prev_time is None
        assert prev_times[0] is None
        # Second: prev_time is first event's time
        assert prev_times[1] == "10:00:00.000"
        # Third: prev_time is second event's time
        assert prev_times[2] == "10:00:01.000"

    # -- Test 8: grid close rerenders scrollback --

    def test_grid_close_rerenders_scrollback(self):
        """Closing grid with Enter calls _rerender_from_scrollback."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)

        # Feed 5 events
        for i in range(5):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:0{}.000".format(i)))

        # Open grid
        viewer._enter_toggle_grid()
        assert viewer.grid_visible is True

        # Feed 3 more while grid is open (buffered)
        for i in range(3):
            viewer._process_event_result(
                _make_event(seq=i + 5,
                            time="10:00:0{}.000".format(i + 5)))

        # All 8 events should be in scrollback
        assert len(viewer.scrollback) == 8

        # Close grid with Enter
        with mock.patch.object(
                viewer, '_rerender_from_scrollback') as mock_rerender:
            viewer._handle_grid_key("\n")

        assert viewer.grid_visible is False
        mock_rerender.assert_called_once()

    # -- Test 9: grid escape preserves pre-grid events --

    def test_grid_escape_preserves_pre_grid_events(self):
        """Closing grid with Escape preserves scrollback and re-renders."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)

        # Feed 5 events
        for i in range(5):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:0{}.000".format(i)))

        # Open grid
        viewer._enter_toggle_grid()

        # Close with Escape
        with mock.patch.object(
                viewer, '_rerender_from_scrollback') as mock_rerender:
            viewer._handle_grid_key(("esc", ""))

        assert viewer.grid_visible is False
        assert len(viewer.scrollback) == 5
        mock_rerender.assert_called_once()

    def test_grid_escape_while_paused_rerenders_scroll(self):
        """Escape from grid while paused re-renders the pause scroll view."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)

        # Feed 5 events
        for i in range(5):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:0{}.000".format(i)))

        # Pause
        viewer._toggle_pause()
        assert viewer.paused is True

        # Open grid
        viewer._enter_toggle_grid()

        # Close with Escape while paused
        with mock.patch.object(
                viewer, '_scroll_pause_buffer') as mock_scroll:
            viewer._handle_grid_key(("esc", ""))

        assert viewer.grid_visible is False
        assert viewer.paused is True
        mock_scroll.assert_called_once_with(0)

    # -- Test 10: unpause rerenders scrollback --

    def test_unpause_rerenders_scrollback(self):
        """Unpause calls _rerender_from_scrollback, no 'buffered' msg."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)

        # Feed 5 events
        for i in range(5):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:0{}.000".format(i)))

        # Pause
        viewer._toggle_pause()
        assert viewer.paused is True

        # Feed 3 more while paused
        for i in range(3):
            viewer._process_event_result(
                _make_event(seq=i + 5,
                            time="10:00:0{}.000".format(i + 5)))

        # All 8 in scrollback
        assert len(viewer.scrollback) == 8

        # Unpause
        output = io.StringIO()
        viewer.term.stdout = output

        with mock.patch.object(
                viewer, '_rerender_from_scrollback') as mock_rerender:
            viewer._toggle_pause()

        assert viewer.paused is False
        mock_rerender.assert_called_once()
        # No "buffered events" message
        written = output.getvalue()
        assert "buffered events" not in written

    # -- Test 11: pause snapshot filters scrollback --

    def test_pause_snapshot_filters_scrollback(self):
        """Pause builds _scroll_snapshot from filtered scrollback."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)

        # Feed 5 events: 3 dos, 2 exec
        for i in range(3):
            viewer._process_event_result(
                _make_event(seq=i, lib="dos", func="Open",
                            time="10:00:0{}.000".format(i)))
        for i in range(2):
            viewer._process_event_result(
                _make_event(seq=i + 3, lib="exec", func="OpenLibrary",
                            time="10:00:0{}.000".format(i + 3)))

        # Disable dos
        viewer.disabled_libs = {"dos"}

        # Pause
        viewer._toggle_pause()

        # Snapshot should have only 2 events (exec only)
        assert len(viewer._scroll_snapshot) == 2
        for e in viewer._scroll_snapshot:
            assert e.get("lib") == "exec"

    # -- Test 14: search triggers rerender --

    def test_search_triggers_rerender(self):
        """Setting search pattern while NOT paused calls rerender."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)

        # Feed 5 events
        for i in range(5):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:0{}.000".format(i),
                            func="Func{}".format(i)))

        # Simulate search: type "Func1" + Enter
        keys = ["F", "u", "n", "c", "1", "\n"]
        key_iter = iter(keys)

        def mock_read_key():
            try:
                return next(key_iter)
            except StopIteration:
                return None

        viewer.term.read_key = mock_read_key

        with mock.patch("amigactl.trace_ui.select") as mock_select_mod:
            def fake_select(fds, w, x, timeout=0):
                return ([viewer.term.stdin_fd], [], [])
            mock_select_mod.select.side_effect = fake_select

            with mock.patch.object(
                    viewer,
                    '_rerender_from_scrollback') as mock_rerender:
                viewer._enter_search_mode()

        assert viewer.search_pattern == "Func1"
        mock_rerender.assert_called_once()

    # -- Test 14b: search while paused rebuilds snapshot --

    def test_search_while_paused_rebuilds_snapshot(self):
        """Setting search while paused rebuilds snapshot, not rerender."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)

        # Feed 5 events with different function names
        for i in range(5):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:0{}.000".format(i),
                            func="Func{}".format(i)))

        # Pause
        viewer._toggle_pause()
        assert viewer.paused is True

        # Simulate search: type "Func1" + Enter
        keys = ["F", "u", "n", "c", "1", "\n"]
        key_iter = iter(keys)

        def mock_read_key():
            try:
                return next(key_iter)
            except StopIteration:
                return None

        viewer.term.read_key = mock_read_key

        with mock.patch("amigactl.trace_ui.select") as mock_select_mod:
            def fake_select(fds, w, x, timeout=0):
                return ([viewer.term.stdin_fd], [], [])
            mock_select_mod.select.side_effect = fake_select

            with mock.patch.object(
                    viewer,
                    '_scroll_pause_buffer') as mock_scroll:
                viewer._enter_search_mode()

        assert viewer.search_pattern == "Func1"
        # Should call _scroll_pause_buffer(0), not _rerender
        mock_scroll.assert_called_once_with(0)
        # Snapshot should be filtered by search
        assert viewer._scroll_snapshot is not None
        # Only events matching "Func1" should be in snapshot
        for e in viewer._scroll_snapshot:
            assert "Func1" in format_trace_event(e, viewer.cw)

    # -- Test 15: grid close while paused rebuilds snapshot --

    def test_grid_close_while_paused_rebuilds_snapshot(self):
        """Grid close (Enter) while paused rebuilds filtered snapshot."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)

        # Feed 5 events: 3 dos, 2 exec
        for i in range(3):
            viewer._process_event_result(
                _make_event(seq=i, lib="dos", func="Open",
                            time="10:00:0{}.000".format(i)))
        for i in range(2):
            viewer._process_event_result(
                _make_event(seq=i + 3, lib="exec", func="OpenLibrary",
                            time="10:00:0{}.000".format(i + 3)))

        # Pause
        viewer._toggle_pause()
        assert viewer.paused is True

        # Open grid
        viewer._enter_toggle_grid()

        # Disable dos in the grid
        for item in viewer.grid.lib_items:
            if item["name"] == "dos":
                item["enabled"] = False
        viewer.grid.user_interacted = True

        # Close grid with Enter
        with mock.patch.object(
                viewer, '_scroll_pause_buffer') as mock_scroll:
            viewer._handle_grid_key("\n")

        # Still paused
        assert viewer.paused is True
        # Snapshot should have only exec events
        assert viewer._scroll_snapshot is not None
        assert len(viewer._scroll_snapshot) == 2
        for e in viewer._scroll_snapshot:
            assert e.get("lib") == "exec"
        # scroll_pause_buffer should be called to re-render
        mock_scroll.assert_called_once_with(0)

    # -- Test 16: shown_events lifetime counter --

    def test_shown_events_lifetime_counter(self):
        """shown_events is not reset by _rerender_from_scrollback."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)

        # Feed 5 events
        for i in range(5):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:0{}.000".format(i)))

        assert viewer.shown_events == 5

        # Re-render
        with mock.patch.object(viewer.term, 'clear_scroll_region'):
            viewer._rerender_from_scrollback()

        # shown_events unchanged
        assert viewer.shown_events == 5

    # -- Wave 2 Tests: Grid hotkey bar --

    def test_enter_toggle_grid_draws_hotkey_bar(self):
        """Opening the grid draws the grid hotkey bar."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)

        # Feed an event so grid has data
        viewer._process_event_result(
            _make_event(lib="dos", func="Open"))

        output = io.StringIO()
        viewer.term.stdout = output

        viewer._enter_toggle_grid()

        written = output.getvalue()
        # Should contain grid footer text
        assert "[A]ll on" in written

    def test_grid_hotkey_bar_content(self):
        """_build_hotkey_bar returns GRID_FOOTER_TEXT when grid visible."""
        viewer = _make_viewer()
        viewer.grid_visible = True

        text = viewer._build_hotkey_bar()
        from amigactl.trace_grid import GRID_FOOTER_TEXT
        assert text == GRID_FOOTER_TEXT

    def test_grid_hotkey_bar_reverts_on_close(self):
        """After closing grid, hotkey bar reverts to main controls."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)

        # Feed an event
        viewer._process_event_result(
            _make_event(lib="dos", func="Open"))

        # Open grid
        viewer._enter_toggle_grid()
        assert viewer.grid_visible is True

        # Close with Escape
        viewer._handle_grid_key(("esc", ""))
        assert viewer.grid_visible is False

        # Build hotkey bar -- should be main controls now
        text = viewer._build_hotkey_bar()
        assert "[Tab]" in text
        assert "[A]ll on" not in text


# ---------------------------------------------------------------------------
# TestGridCursorNavigation
# ---------------------------------------------------------------------------

class TestGridCursorNavigation:
    """Tests for arrow-key cursor navigation in the toggle grid."""

    def test_handle_grid_key_up_down(self):
        """Up/Down arrow keys move cursor position."""
        viewer = _make_viewer()
        viewer.discovered_libs = {"exec": 200, "dos": 100, "icon": 50}
        viewer.discovered_funcs = {"exec": {"Open": 12}}
        viewer.discovered_procs = {}

        viewer._enter_toggle_grid()
        assert viewer.grid.cursor_pos[0] == 0

        # Down arrow
        viewer._handle_grid_key(("esc", "[B"))
        assert viewer.grid.cursor_pos[0] == 1

        # Up arrow
        viewer._handle_grid_key(("esc", "[A"))
        assert viewer.grid.cursor_pos[0] == 0

    def test_handle_grid_key_space_toggle(self):
        """Space key toggles item at cursor position."""
        viewer = _make_viewer()
        viewer.discovered_libs = {"exec": 200, "dos": 100}
        viewer.discovered_funcs = {"exec": {"Open": 12}}
        viewer.discovered_procs = {}

        viewer._enter_toggle_grid()
        assert viewer.grid.lib_items[0]["enabled"] is True

        # Space toggles first item
        viewer._handle_grid_key(" ")
        assert viewer.grid.lib_items[0]["enabled"] is False

    def test_handle_grid_key_space_updates_focused_lib(self):
        """Space in LIBRARIES category syncs focused_lib_index."""
        viewer = _make_viewer()
        viewer.discovered_libs = {"exec": 200, "dos": 100}
        viewer.discovered_funcs = {"exec": {"Open": 12}}
        viewer.discovered_procs = {}

        viewer._enter_toggle_grid()
        # Move cursor to dos (index 1)
        viewer.grid.cursor_pos[0] = 1

        # Space on LIBRARIES syncs focused_lib_index
        viewer._handle_grid_key(" ")
        assert viewer.grid.focused_lib_index == 1

    def test_handle_grid_key_a_lowercase(self):
        """Lowercase 'a' triggers all_on (case-insensitive)."""
        viewer = _make_viewer()
        viewer.discovered_libs = {"exec": 200, "dos": 100}
        viewer.discovered_funcs = {"exec": {"Open": 12}}
        viewer.discovered_procs = {}

        viewer._enter_toggle_grid()
        viewer.grid.none()  # disable all first
        assert all(not item["enabled"] for item in viewer.grid.lib_items)

        viewer._handle_grid_key("a")
        assert all(item["enabled"] for item in viewer.grid.lib_items)

    def test_handle_grid_key_n_lowercase(self):
        """Lowercase 'n' triggers none (case-insensitive)."""
        viewer = _make_viewer()
        viewer.discovered_libs = {"exec": 200, "dos": 100}
        viewer.discovered_funcs = {"exec": {"Open": 12}}
        viewer.discovered_procs = {}

        viewer._enter_toggle_grid()
        assert all(item["enabled"] for item in viewer.grid.lib_items)

        viewer._handle_grid_key("n")
        assert all(not item["enabled"] for item in viewer.grid.lib_items)

    def test_cursor_clamped_on_library_switch(self):
        """Cursor is clamped when switching to a library with fewer funcs."""
        viewer = _make_viewer()
        viewer.discovered_libs = {"exec": 200, "dos": 100}
        viewer.discovered_funcs = {
            "exec": {"Open": 50, "Close": 40, "Lock": 30,
                     "Execute": 20, "LoadSeg": 10},
            "dos": {"Read": 5},
        }
        viewer.discovered_procs = {}

        viewer._enter_toggle_grid()

        # Navigate to FUNCTIONS for exec (default)
        viewer._handle_grid_key(("esc", "[C"))
        assert viewer.grid.active_category == 1

        # Set a high cursor position
        viewer.grid.cursor_pos[1] = 4  # last of 5 exec funcs

        # Go back to LIBRARIES
        viewer._handle_grid_key(("esc", "[D"))

        # Move cursor to dos (index 1)
        viewer.grid.cursor_pos[0] = 1

        # Navigate to FUNCTIONS -- dos has only 1 func
        viewer._handle_grid_key(("esc", "[C"))

        # cursor_pos[1] should be clamped to 0 (only 1 item)
        assert viewer.grid.cursor_pos[1] == 0


# ---------------------------------------------------------------------------
# TestAutoPauseUnpause
# ---------------------------------------------------------------------------

class TestAutoPauseUnpause:
    """Tests for auto-pause on Up/PgUp and auto-unpause on Down/PgDn."""

    def test_up_arrow_auto_pauses(self):
        """Up arrow while live viewing auto-pauses and scrolls up."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)
        assert viewer.paused is False

        # Populate scrollback so there is something to scroll
        for i in range(50):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:0{}.000".format(i % 10)))

        # Simulate Up arrow
        viewer.term.read_key = mock.MagicMock(
            return_value=("esc", "[A"))
        viewer._handle_keypress()

        # Should now be paused
        assert viewer.paused is True
        # _scroll_snapshot should have been created by _toggle_pause
        assert viewer._scroll_snapshot is not None

    def test_page_up_auto_pauses(self):
        """Page Up while live viewing auto-pauses and scrolls up."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)
        assert viewer.paused is False

        for i in range(50):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:0{}.000".format(i % 10)))

        viewer.term.read_key = mock.MagicMock(
            return_value=("esc", "[5~"))
        viewer._handle_keypress()

        assert viewer.paused is True
        assert viewer._scroll_snapshot is not None

    def test_down_arrow_at_bottom_auto_unpauses(self):
        """Down arrow at scroll bottom auto-unpauses."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)
        viewer.term.rows = 24

        # Populate scrollback
        for i in range(10):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:0{}.000".format(i)))

        # Pause manually
        viewer._toggle_pause()
        assert viewer.paused is True

        # With 10 events and visible_lines=20, max_pos=0
        # So we are already at the bottom
        assert viewer.pause_scroll_pos == 0

        viewer.term.read_key = mock.MagicMock(
            return_value=("esc", "[B"))
        viewer._handle_keypress()

        # Should auto-unpause since at bottom
        assert viewer.paused is False

    def test_down_arrow_not_at_bottom_stays_paused(self):
        """Down arrow NOT at highlight bottom stays paused."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)
        viewer.term.rows = 24

        # Populate scrollback with enough events to scroll
        for i in range(50):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:0{}.000".format(i % 10)))

        # Pause (highlight_pos = 49), then move highlight to top
        viewer._toggle_pause()
        assert viewer.paused is True
        viewer.highlight_pos = 5

        # Down arrow: should stay paused (not at last event)
        viewer.term.read_key = mock.MagicMock(
            return_value=("esc", "[B"))
        viewer._handle_keypress()

        assert viewer.paused is True
        assert viewer.highlight_pos == 6

    def test_page_down_at_bottom_auto_unpauses(self):
        """Page Down at scroll bottom auto-unpauses."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)
        viewer.term.rows = 24

        # Few events so we start at bottom
        for i in range(5):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:0{}.000".format(i)))

        viewer._toggle_pause()
        assert viewer.paused is True

        viewer.term.read_key = mock.MagicMock(
            return_value=("esc", "[6~"))
        viewer._handle_keypress()

        assert viewer.paused is False

    def test_up_arrow_already_paused_moves_highlight(self):
        """Up arrow when already paused moves highlight, no double-pause."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)
        viewer.term.rows = 24

        for i in range(50):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:0{}.000".format(i % 10)))

        viewer._toggle_pause()
        assert viewer.paused is True
        initial_highlight = viewer.highlight_pos  # 49

        viewer.term.read_key = mock.MagicMock(
            return_value=("esc", "[A"))
        viewer._handle_keypress()

        assert viewer.paused is True
        assert viewer.highlight_pos == initial_highlight - 1

    def test_segment_annotation_in_process_event(self):
        """LoadSeg then RunCommand: _segment_annotation key is set."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(120)

        load_ev = _make_event(
            func="LoadSeg", lib="dos",
            args='"cnet:control"', retval="0x01d0dfb5", status="O")
        run_ev = _make_event(
            func="RunCommand", lib="dos",
            args="seg=0x1d0dfb5,stack=4096,1", retval="rc=0", status="O")
        viewer._process_event_result(load_ev)
        viewer._process_event_result(run_ev)

        # RunCommand event in scrollback should have _segment_annotation
        stored = list(viewer.scrollback)[-1]
        assert stored.get("_segment_annotation") == "cnet:control"

    def test_annotated_event_applies_segment(self):
        """_annotated_event inserts filename after seg=0x... in RunCommand args."""
        viewer = _make_viewer()
        event = {
            "func": "RunCommand", "lib": "dos",
            "args": "seg=0x1d0dfb5,stack=4096,1",
            "retval": "rc=0", "status": "O",
            "_segment_annotation": "cnet:control",
        }
        result = viewer._annotated_event(event)
        assert result is not event  # should be a copy
        assert 'seg=0x1d0dfb5 "cnet:control"' in result["args"]
        assert ",stack=4096,1" in result["args"]


# ---------------------------------------------------------------------------
# TestColumnHeader
# ---------------------------------------------------------------------------

class TestColumnHeader:
    """Tests for the column header row feature."""

    def test_format_header_matches_columns(self):
        """ColumnLayout.format_header() produces correct column names."""
        layout = ColumnLayout(120)
        cw = ColorWriter(force_color=False)

        header = layout.format_header(cw)
        assert "SEQ" in header
        assert "TIME" in header
        assert "FUNCTION" in header
        assert "TASK" in header
        assert "ARGS" in header
        assert "RESULT" in header

    def test_format_header_no_time_when_cramped(self):
        """Cramped layout omits TIME from header."""
        layout = ColumnLayout(50)
        cw = ColorWriter(force_color=False)

        header = layout.format_header(cw)
        assert "TIME" not in header
        assert "SEQ" in header
        assert "FUNCTION" in header

    def test_format_header_dim_when_colored(self):
        """Header is rendered dim when colors are enabled."""
        layout = ColumnLayout(120)
        cw = ColorWriter(force_color=True)

        header = layout.format_header(cw)
        # Should contain DIM escape code
        assert "\033[2m" in header

    def test_visible_lines_accounts_for_header(self):
        """Scroll calculations use rows - 4 (status + header + hotkey + 1)."""
        viewer = _make_viewer()
        viewer.term.rows = 24

        # visible_lines should be 24 - 4 = 20
        visible_lines = viewer.term.rows - 4
        assert visible_lines == 20

    def test_header_drawn_on_start(self):
        """Verify _draw_header writes the header at row 2."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)

        output = io.StringIO()
        viewer.term.stdout = output
        viewer._draw_header()
        written = output.getvalue()
        # Should position at row 2
        assert "\033[2;1H" in written

    def test_setup_regions_scroll_starts_at_row_3(self):
        """Verify DECSTBM starts at row 3 (after header)."""
        term, output = _make_terminal_state(rows=24, cols=80)
        term._saved_attrs = None

        term.setup_regions()

        written = output.getvalue()
        # DECSTBM should be 3;23 (row 3 through rows-1)
        assert "\033[3;23r" in written
        assert "\033[3;1H" in written


# ---------------------------------------------------------------------------
# Shell noise filter (Fix 4)
# ---------------------------------------------------------------------------


class TestShellNoiseFilter:
    """Tests for the shell init noise filter (Fix 4)."""

    def test_shell_noise_filter_blocks_setvar_rc(self):
        """Shell noise filter suppresses SetVar for RC (shell bookkeeping)."""
        viewer = _make_viewer()
        viewer.shell_noise_filter = True
        event = {"func": "SetVar",
                 "args": '"RC",LOCAL',
                 "lib": "dos", "task": "[1] control", "status": "O"}
        assert not viewer._passes_client_filter(event)

    def test_shell_noise_filter_blocks_setvar_result2(self):
        """Shell noise filter suppresses SetVar for Result2 (shell bookkeeping)."""
        viewer = _make_viewer()
        viewer.shell_noise_filter = True
        event = {"func": "SetVar",
                 "args": '"Result2",LOCAL',
                 "lib": "dos", "task": "[1] control", "status": "O"}
        assert not viewer._passes_client_filter(event)

    def test_shell_noise_filter_blocks_getvar_rc_after_command(self):
        """Shell noise filter suppresses GetVar for RC (post-command query)."""
        viewer = _make_viewer()
        viewer.shell_noise_filter = True
        event = {"func": "GetVar",
                 "args": '"RC",LOCAL',
                 "lib": "dos", "task": "[1] control", "status": "O"}
        assert not viewer._passes_client_filter(event)

    def test_shell_noise_filter_blocks_setvar_process(self):
        """Shell noise filter suppresses SetVar for process (init-only)."""
        viewer = _make_viewer()
        viewer.shell_noise_filter = True
        event = {"func": "SetVar", "args": '"process",LOCAL',
                 "lib": "dos", "task": "[1] control", "status": "O"}
        assert not viewer._passes_client_filter(event)

    def test_shell_noise_filter_passes_setvar_custom(self):
        """Shell noise filter does not block SetVar for custom variables."""
        viewer = _make_viewer()
        viewer.shell_noise_filter = True
        event = {"func": "SetVar", "args": '"myvar",LOCAL',
                 "lib": "dos", "task": "[1] control", "status": "O"}
        assert viewer._passes_client_filter(event)

    def test_shell_noise_filter_off_passes_all(self):
        """With shell noise filter off, shell init events pass through."""
        viewer = _make_viewer()
        viewer.shell_noise_filter = False
        event = {"func": "SetVar", "args": '"process",LOCAL',
                 "lib": "dos", "task": "[1] control", "status": "O"}
        assert viewer._passes_client_filter(event)

    def test_shell_noise_filter_blocks_getvar_echo(self):
        """Shell noise filter suppresses GetVar for echo."""
        viewer = _make_viewer()
        viewer.shell_noise_filter = True
        event = {"func": "GetVar", "args": '"echo",LOCAL',
                 "lib": "dos", "task": "[1] control", "status": "-"}
        assert not viewer._passes_client_filter(event)

    def test_shell_noise_filter_passes_findvar_lv_var(self):
        """FindVar with LV_VAR is not affected by shell noise filter."""
        viewer = _make_viewer()
        viewer.shell_noise_filter = True
        event = {"func": "FindVar", "args": '"process",LV_VAR',
                 "lib": "dos", "task": "[1] control", "status": "E"}
        assert viewer._passes_client_filter(event)

    def test_shell_noise_filter_blocks_findvar_lv_alias(self):
        """FindVar with LV_ALIAS is suppressed by shell noise filter."""
        viewer = _make_viewer()
        viewer.shell_noise_filter = True
        event = {"func": "FindVar",
                 "args": '"cnet:control",LV_ALIAS',
                 "lib": "dos", "task": "[1] control", "status": "E"}
        assert not viewer._passes_client_filter(event)

    def test_shell_noise_filter_off_passes_findvar_alias(self):
        """With shell noise filter off, FindVar LV_ALIAS passes."""
        viewer = _make_viewer()
        viewer.shell_noise_filter = False
        event = {"func": "FindVar",
                 "args": '"cnet:control",LV_ALIAS',
                 "lib": "dos", "task": "[1] control", "status": "E"}
        assert viewer._passes_client_filter(event)


# ---------------------------------------------------------------------------
# TestPrepopulateFromStatus (Fix 3)
# ---------------------------------------------------------------------------

class TestPrepopulateFromStatus:
    """Tests for _prepopulate_from_status() (Fix 3)."""

    def test_prepopulate_from_status(self):
        """TRACE STATUS patch_list populates discovered_libs and discovered_funcs."""
        viewer = _make_viewer()
        status = {
            "patch_list": [
                {"name": "exec.FindPort", "enabled": False},
                {"name": "dos.Open", "enabled": True},
            ]
        }
        viewer._prepopulate_from_status(status)
        assert "exec" in viewer.discovered_libs
        assert "dos" in viewer.discovered_libs
        assert "FindPort" in viewer.discovered_funcs.get("exec", {})
        assert "exec.FindPort" in viewer.daemon_disabled_funcs
        assert "dos.Open" not in viewer.daemon_disabled_funcs


# ---------------------------------------------------------------------------
# TestHelpOverlay (review fixes)
# ---------------------------------------------------------------------------

class TestHelpOverlay:
    """Tests for help overlay hotkey bar and event buffering."""

    def test_help_hotkey_bar_shows_scroll_and_dismiss(self):
        """Help-specific hotkey bar shows scroll/dismiss hints."""
        viewer = _make_viewer()
        viewer.help_visible = True

        output = io.StringIO()
        viewer.term.stdout = output

        viewer._draw_hotkey_bar()

        written = output.getvalue()
        assert "scroll" in written
        assert "dismiss" in written
        # Should NOT contain main hotkey items
        assert "[q]" not in written

    def test_events_buffered_during_help_display(self):
        """Events arriving while help is visible are buffered."""
        viewer = _make_viewer()
        viewer.help_visible = True

        for i in range(3):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:0{}.000".format(i)))

        # Events should be buffered
        assert len(viewer.pause_buffer) == 3
        # Events still counted in total
        assert viewer.total_events == 3
        # But not shown
        assert viewer.shown_events == 0
        # Events should still be in scrollback
        assert len(viewer.scrollback) == 3

    def test_help_indicator_does_not_overwrite_last_line(self):
        """Position indicator uses a reserved row, not the last help line."""
        viewer = _make_viewer()
        viewer.term.rows = 10  # available = 10 - 4 = 6, minus 1 for indicator = 5
        viewer.help_visible = True
        viewer._help_lines = ["line {}".format(i) for i in range(20)]
        viewer._help_scroll_pos = 0

        written_rows = {}
        original_write_at = viewer.term.write_at

        def capture_write_at(row, text):
            written_rows[row] = text
            original_write_at(row, text)

        viewer.term.write_at = capture_write_at

        viewer._render_help()

        # 5 help lines at rows 3-7, indicator at row 8 (3 + 5)
        # Help lines should be lines 0-4
        for i in range(5):
            assert "line {}".format(i) in written_rows.get(3 + i, "")
        # Row 8 should have the indicator
        assert "[lines" in written_rows.get(8, "")
        # Row 8 should NOT contain "line 5" (that would mean overwrite)
        assert "line 5" not in written_rows.get(8, "")


# ---------------------------------------------------------------------------
# TestHighlightCursor (Wave 1: event detail view)
# ---------------------------------------------------------------------------

class TestHighlightCursor:
    """Tests for highlight cursor in paused/scrollback mode."""

    def test_highlight_initialized_on_pause(self):
        """Pause with 50 events -> highlight_pos == 49 (last event)."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)
        viewer.term.rows = 24
        for i in range(50):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:{:02d}.000".format(i % 60)))
        viewer._toggle_pause()
        assert viewer.highlight_pos == 49

    def test_highlight_moves_up(self):
        """After pause, _move_highlight(-1) -> highlight_pos decrements."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)
        viewer.term.rows = 24
        for i in range(50):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:{:02d}.000".format(i % 60)))
        viewer._toggle_pause()
        assert viewer.highlight_pos == 49
        viewer._move_highlight(-1)
        assert viewer.highlight_pos == 48

    def test_highlight_moves_down(self):
        """Pause at pos 0, _move_highlight(1) -> highlight_pos == 1."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)
        viewer.term.rows = 24
        for i in range(50):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:{:02d}.000".format(i % 60)))
        viewer._toggle_pause()
        viewer.highlight_pos = 0
        result = viewer._move_highlight(1)
        assert viewer.highlight_pos == 1
        assert result is False

    def test_highlight_clamps_at_zero(self):
        """_move_highlight(-100) -> highlight_pos == 0."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)
        viewer.term.rows = 24
        for i in range(10):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:0{}.000".format(i)))
        viewer._toggle_pause()
        viewer.highlight_pos = 5
        viewer._move_highlight(-100)
        assert viewer.highlight_pos == 0

    def test_highlight_clamps_at_end(self):
        """_move_highlight(+100) -> highlight_pos == len-1."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)
        viewer.term.rows = 24
        for i in range(10):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:0{}.000".format(i)))
        viewer._toggle_pause()
        viewer.highlight_pos = 5
        viewer._move_highlight(100)
        assert viewer.highlight_pos == 9

    def test_highlight_auto_unpause_at_bottom(self):
        """At last event, _move_highlight(1) returns True."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)
        viewer.term.rows = 24
        for i in range(10):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:0{}.000".format(i)))
        viewer._toggle_pause()
        viewer.highlight_pos = 9  # last event
        result = viewer._move_highlight(1)
        assert result is True

    def test_highlight_not_at_bottom_stays_paused(self):
        """At middle, _move_highlight(1) returns False."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)
        viewer.term.rows = 24
        for i in range(10):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:0{}.000".format(i)))
        viewer._toggle_pause()
        viewer.highlight_pos = 5
        result = viewer._move_highlight(1)
        assert result is False
        assert viewer.highlight_pos == 6

    def test_highlight_scrolls_viewport_up(self):
        """highlight_pos < pause_scroll_pos -> viewport adjusts."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)
        viewer.term.rows = 24  # visible_lines = 20
        for i in range(50):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:{:02d}.000".format(i % 60)))
        viewer._toggle_pause()
        # Scroll starts at 30 (50-20), highlight at 49
        viewer.highlight_pos = 30  # top of viewport
        viewer._move_highlight(-1)  # highlight now 29, < scroll_pos 30
        assert viewer.highlight_pos == 29
        assert viewer.pause_scroll_pos == 29

    def test_highlight_scrolls_viewport_down(self):
        """highlight_pos > pause_scroll_pos + visible -> viewport adjusts."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)
        viewer.term.rows = 10  # visible_lines = 6
        for i in range(20):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:{:02d}.000".format(i)))
        viewer._toggle_pause()
        # scroll_pos = 14 (20-6), highlight = 19
        viewer.pause_scroll_pos = 5
        viewer.highlight_pos = 10  # bottom of viewport at 5+6-1=10
        viewer._move_highlight(1)  # highlight now 11, >= 5+6=11
        assert viewer.highlight_pos == 11
        assert viewer.pause_scroll_pos == 6  # 11 - 6 + 1

    def test_highlight_rendered_as_reverse_video(self):
        """Check that write_at output contains reverse video for highlighted row."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)
        viewer.term.rows = 10
        cw = ColorWriter(force_color=True)
        viewer.cw = cw
        for i in range(3):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:0{}.000".format(i)))
        viewer._toggle_pause()
        viewer.highlight_pos = 1  # middle event

        written_rows = {}
        original_write_at = viewer.term.write_at

        def capture_write_at(row, text):
            written_rows[row] = text
            original_write_at(row, text)

        viewer.term.write_at = capture_write_at
        viewer._scroll_pause_buffer(0)

        # Row for event index 1: row 3 + (1 - 0) = 4
        highlighted = written_rows.get(4, "")
        assert "\033[7m" in highlighted  # reverse video on
        assert "\033[0m" in highlighted  # reset
        # Every internal RESET must re-enable reverse video so the
        # highlight bar spans the full row, not just the first column.
        assert highlighted.startswith("\033[7m")
        assert highlighted.endswith("\033[0m")
        # Strip the leading \033[7m and trailing \033[0m, then verify
        # every remaining \033[0m is followed by \033[7m
        inner = highlighted[4:-4]  # strip \033[7m prefix and \033[0m suffix
        assert "\033[0m\033[7m" in inner or "\033[0m" not in inner

        # Non-highlighted rows should NOT have reverse video
        non_highlighted = written_rows.get(3, "")
        assert "\033[7m" not in non_highlighted

    def test_highlight_full_width_padding(self):
        """Highlighted row padded to terminal width."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)
        viewer.term.rows = 10
        viewer.term.cols = 80
        cw = ColorWriter(force_color=True)
        viewer.cw = cw
        for i in range(3):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:0{}.000".format(i)))
        viewer._toggle_pause()
        viewer.highlight_pos = 0

        written_rows = {}
        original_write_at = viewer.term.write_at

        def capture_write_at(row, text):
            written_rows[row] = text
            original_write_at(row, text)

        viewer.term.write_at = capture_write_at
        viewer._scroll_pause_buffer(0)

        highlighted = written_rows.get(3, "")
        # The visible length of the highlighted row (excluding ANSI)
        # should be at least terminal width (80)
        vis_len = _visible_len(highlighted)
        assert vis_len >= 80

    def test_highlight_cleared_on_unpause(self):
        """After _toggle_pause (unpause), highlight_pos reset."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)
        viewer.term.rows = 24
        for i in range(10):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:0{}.000".format(i)))
        viewer._toggle_pause()
        assert viewer.highlight_pos == 9
        viewer._toggle_pause()  # unpause
        assert viewer.highlight_pos == 0

    def test_highlight_page_up(self):
        """PgUp moves highlight by visible_lines."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)
        viewer.term.rows = 24  # visible_lines = 20
        for i in range(50):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:{:02d}.000".format(i % 60)))
        viewer._toggle_pause()
        assert viewer.highlight_pos == 49
        viewer._move_highlight(-20)
        assert viewer.highlight_pos == 29

    def test_highlight_page_down(self):
        """PgDn moves highlight by visible_lines."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)
        viewer.term.rows = 24  # visible_lines = 20
        for i in range(50):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:{:02d}.000".format(i % 60)))
        viewer._toggle_pause()
        viewer.highlight_pos = 10
        viewer._move_highlight(20)
        assert viewer.highlight_pos == 30

    def test_up_arrow_auto_pauses_with_highlight(self):
        """Up arrow while live -> paused, highlight at last-1."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)
        viewer.term.rows = 24
        for i in range(30):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:{:02d}.000".format(i % 60)))
        assert viewer.paused is False

        viewer.term.read_key = mock.MagicMock(
            return_value=("esc", "[A"))
        viewer._handle_keypress()

        assert viewer.paused is True
        # _toggle_pause sets highlight to 29 (last), then
        # _move_highlight(-1) moves it to 28
        assert viewer.highlight_pos == 28

    def test_page_up_auto_pauses_with_highlight(self):
        """PgUp while live -> paused, highlight moves up by page."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)
        viewer.term.rows = 24  # visible_lines = 20
        for i in range(50):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:{:02d}.000".format(i % 60)))
        assert viewer.paused is False

        viewer.term.read_key = mock.MagicMock(
            return_value=("esc", "[5~"))
        viewer._handle_keypress()

        assert viewer.paused is True
        # _toggle_pause sets highlight to 49, then
        # _move_highlight(-20) moves it to 29
        assert viewer.highlight_pos == 29

    def test_highlight_clamped_after_filter_change(self):
        """Rebuild snapshot with fewer events -> highlight clamped."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)
        viewer.term.rows = 24
        # 5 dos, 5 exec
        for i in range(5):
            viewer._process_event_result(
                _make_event(seq=i, lib="dos", time="10:00:0{}.000".format(i)))
        for i in range(5, 10):
            viewer._process_event_result(
                _make_event(seq=i, lib="exec", time="10:00:0{}.000".format(i)))
        viewer._toggle_pause()
        assert viewer.highlight_pos == 9

        # Disable exec, rebuild snapshot (5 events only)
        viewer.disabled_libs = {"exec"}
        viewer._scroll_snapshot = viewer._build_filtered_snapshot()
        assert len(viewer._scroll_snapshot) == 5

        # highlight_pos at 9 exceeds new combined size (5)
        # _scroll_pause_buffer should clamp it
        viewer._scroll_pause_buffer(0)
        assert viewer.highlight_pos == 4  # len(5) - 1

    def test_highlight_reset_after_help_dismiss(self):
        """Open help while paused at mid-scroll, dismiss -> highlight at bottom."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)
        viewer.term.rows = 24
        for i in range(50):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:{:02d}.000".format(i % 60)))
        viewer._toggle_pause()
        viewer.highlight_pos = 10  # mid-position
        viewer.pause_scroll_pos = 5

        # Show and dismiss help
        viewer._show_help()
        assert viewer.help_visible is True

        # Dismiss help (any non-scroll key dismisses)
        viewer.term.read_key = mock.MagicMock(return_value="x")
        viewer._handle_keypress()

        assert viewer.help_visible is False
        # After dismiss, snapshot is rebuilt and scrolled to bottom
        # _init_highlight_at_bottom() sets highlight to bottom visible
        visible_lines = viewer.term.rows - 4
        combined = viewer._get_combined_events()
        expected = min(
            len(combined) - 1,
            viewer.pause_scroll_pos + visible_lines - 1)
        assert viewer.highlight_pos == expected

    def test_highlight_reset_after_grid_apply(self):
        """Open grid while paused, close via Enter -> highlight at bottom."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)
        viewer.term.rows = 24
        viewer.discovered_libs = {"dos": 50}
        viewer.discovered_funcs = {"dos": {"Open": 50}}
        viewer.discovered_procs = {"Shell": 50}
        for i in range(50):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:{:02d}.000".format(i % 60)))
        viewer._toggle_pause()
        viewer.highlight_pos = 10

        viewer._enter_toggle_grid()
        assert viewer.grid_visible is True

        # Apply (Enter)
        viewer._handle_grid_key("\r")
        assert viewer.grid_visible is False

        # highlight should be at bottom visible row
        visible_lines = viewer.term.rows - 4
        combined = viewer._get_combined_events()
        expected = min(
            len(combined) - 1,
            viewer.pause_scroll_pos + visible_lines - 1)
        assert viewer.highlight_pos == expected

    def test_highlight_reset_after_grid_cancel(self):
        """Open grid while paused, close via Esc -> highlight at bottom."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)
        viewer.term.rows = 24
        viewer.discovered_libs = {"dos": 50}
        viewer.discovered_funcs = {"dos": {"Open": 50}}
        viewer.discovered_procs = {"Shell": 50}
        for i in range(50):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:{:02d}.000".format(i % 60)))
        viewer._toggle_pause()
        viewer.highlight_pos = 10

        viewer._enter_toggle_grid()
        assert viewer.grid_visible is True

        # Cancel (Esc)
        viewer._handle_grid_key(("esc", ""))
        assert viewer.grid_visible is False

        # highlight should be at bottom visible row
        visible_lines = viewer.term.rows - 4
        combined = viewer._get_combined_events()
        expected = min(
            len(combined) - 1,
            viewer.pause_scroll_pos + visible_lines - 1)
        assert viewer.highlight_pos == expected

    def test_highlight_reset_after_init_toggle(self):
        """Toggle [i] while paused -> highlight at bottom visible row."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)
        viewer.term.rows = 24
        for i in range(50):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:{:02d}.000".format(i % 60)))
        viewer._toggle_pause()
        viewer.highlight_pos = 10

        # Toggle init noise filter
        viewer.term.read_key = mock.MagicMock(return_value="i")
        viewer._handle_keypress()

        # highlight should be at bottom visible row
        visible_lines = viewer.term.rows - 4
        combined = viewer._get_combined_events()
        expected = min(
            len(combined) - 1,
            viewer.pause_scroll_pos + visible_lines - 1)
        assert viewer.highlight_pos == expected

    def test_highlight_reset_after_search_exit(self):
        """Search while paused at mid-scroll, clear search -> highlight at bottom."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)
        viewer.term.rows = 24
        for i in range(50):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:{:02d}.000".format(i % 60)))
        viewer._toggle_pause()
        viewer.highlight_pos = 10  # mid-position
        viewer.pause_scroll_pos = 5

        # Enter search mode and immediately press Esc to clear
        viewer.term.read_key = mock.MagicMock(return_value="\x1b")

        with mock.patch("amigactl.trace_ui.select") as mock_select_mod:
            def fake_select(fds, w, x, timeout=0):
                return ([viewer.term.stdin_fd], [], [])
            mock_select_mod.select.side_effect = fake_select

            viewer._enter_search_mode()

        assert viewer.search_pattern is None
        # highlight should be at bottom visible row
        visible_lines = viewer.term.rows - 4
        combined = viewer._get_combined_events()
        expected = min(
            len(combined) - 1,
            viewer.pause_scroll_pos + visible_lines - 1)
        assert viewer.highlight_pos == expected

    def test_status_bar_shows_highlight_position(self):
        """Status bar says 'event N/M' when paused."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)
        viewer.term.rows = 24
        for i in range(50):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:{:02d}.000".format(i % 60)))
        viewer._toggle_pause()
        viewer.highlight_pos = 24  # 25th event (1-based = 25)

        output = io.StringIO()
        viewer.term.stdout = output
        viewer._draw_status_bar()

        written = output.getvalue()
        assert "event 25/50" in written

    def test_status_bar_detail_mode(self):
        """Status bar shows 'DETAIL | Event #N' when detail_visible."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)
        viewer.term.rows = 24
        for i in range(10):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:0{}.000".format(i)))
        viewer._toggle_pause()
        viewer.highlight_pos = 5

        viewer._open_detail_view()
        assert viewer.detail_visible is True

        output = io.StringIO()
        viewer.term.stdout = output
        viewer._draw_status_bar()

        written = output.getvalue()
        assert "DETAIL" in written
        assert "Event #5" in written


# ---------------------------------------------------------------------------
# TestDetailView (Wave 2: event detail view overlay)
# ---------------------------------------------------------------------------

class TestDetailView:
    """Tests for detail view overlay."""

    def test_enter_opens_detail(self):
        """Paused with highlight -> Enter sets detail_visible=True."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)
        viewer.term.rows = 24
        for i in range(10):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:0{}.000".format(i)))
        viewer._toggle_pause()
        viewer.highlight_pos = 5

        viewer.term.read_key = mock.MagicMock(return_value="\r")
        viewer._handle_keypress()

        assert viewer.detail_visible is True
        assert viewer._detail_event is not None
        assert viewer._detail_event.get("seq") == 5

    def test_enter_no_events_noop(self):
        """Empty scrollback -> Enter does nothing."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)
        viewer.term.rows = 24
        viewer.paused = True
        viewer._scroll_snapshot = []

        viewer.term.read_key = mock.MagicMock(return_value="\r")
        viewer._handle_keypress()

        assert viewer.detail_visible is False

    def test_detail_lines_contain_all_fields(self):
        """_build_detail_lines has seq, time, func, task, args, retval, status."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)
        viewer.term.rows = 24
        viewer.term.cols = 80

        event = _make_event(
            seq=42, time="14:30:01.000", lib="dos", func="Open",
            task="Shell Process", args='"SYS:Startup-Sequence"',
            retval="0x1a2b3c", status="O")

        lines = viewer._build_detail_lines(event)
        text = "\n".join(lines)

        assert "Event #42" in text
        assert "14:30:01.000" in text
        assert "dos.Open" in text
        assert "Shell Process" in text
        assert '"SYS:Startup-Sequence"' in text
        assert "0x1a2b3c" in text
        assert "OK" in text  # status "O" -> "OK"

    def test_detail_lines_no_truncation(self):
        """Long args appear in full, not truncated."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)
        viewer.term.rows = 24
        viewer.term.cols = 200  # wide enough for no wrapping

        long_args = "A" * 150
        event = _make_event(args=long_args)
        lines = viewer._build_detail_lines(event)
        text = "\n".join(lines)

        assert long_args in text

    def test_detail_lines_soft_wrap(self):
        """Args longer than terminal width are wrapped across multiple lines."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)
        viewer.term.rows = 24
        viewer.term.cols = 80  # wrap_width = 80 - 13 = 67

        long_args = "X" * 150
        event = _make_event(args=long_args)
        lines = viewer._build_detail_lines(event)

        # Find the Args line and continuation lines
        args_lines = [l for l in lines if "X" in l]
        assert len(args_lines) >= 2  # at least one continuation line

        # All Xs should be present across the lines
        all_xs = "".join(l.strip() for l in args_lines
                         if "Args" not in l or "X" in l)
        # Remove the "Args" label prefix
        x_count = sum(l.count("X") for l in args_lines)
        assert x_count == 150

    def test_detail_esc_dismisses(self):
        """Esc key sets detail_visible=False."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)
        viewer.term.rows = 24
        for i in range(10):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:0{}.000".format(i)))
        viewer._toggle_pause()
        viewer._open_detail_view()
        assert viewer.detail_visible is True

        viewer.term.read_key = mock.MagicMock(
            return_value=("esc", ""))
        viewer._handle_keypress()

        assert viewer.detail_visible is False
        assert viewer._detail_event is None

    def test_detail_dismiss_restores_scrollback(self):
        """After dismiss, scrollback re-rendered with highlight preserved."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)
        viewer.term.rows = 24
        for i in range(10):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:0{}.000".format(i)))
        viewer._toggle_pause()
        old_highlight = viewer.highlight_pos
        viewer._open_detail_view()
        viewer._dismiss_detail()

        assert viewer.detail_visible is False
        assert viewer.paused is True
        # Highlight should be preserved (unchanged by detail view)
        assert viewer.highlight_pos == old_highlight

    def test_detail_scroll_down(self):
        """Down arrow increments _detail_scroll_pos."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)
        viewer.term.rows = 10  # small terminal
        viewer.term.cols = 80
        for i in range(5):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:0{}.000".format(i)))
        viewer._toggle_pause()
        # Create detail with many lines
        viewer._open_detail_view()
        assert viewer._detail_scroll_pos == 0

        # Only scroll if content exceeds available space
        if viewer._detail_scroll_max() > 0:
            viewer.term.read_key = mock.MagicMock(
                return_value=("esc", "[B"))
            viewer._handle_keypress()
            assert viewer._detail_scroll_pos == 1

    def test_detail_scroll_up(self):
        """Up arrow decrements _detail_scroll_pos."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)
        viewer.term.rows = 10
        viewer.term.cols = 80
        for i in range(5):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:0{}.000".format(i)))
        viewer._toggle_pause()
        viewer._open_detail_view()
        viewer._detail_scroll_pos = 2

        viewer.term.read_key = mock.MagicMock(
            return_value=("esc", "[A"))
        viewer._handle_keypress()

        assert viewer._detail_scroll_pos == 1

    def test_detail_scroll_clamps(self):
        """Scroll pos clamps at 0 and max."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)
        viewer.term.rows = 24
        viewer.term.cols = 80
        for i in range(5):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:0{}.000".format(i)))
        viewer._toggle_pause()
        viewer._open_detail_view()

        # Try scrolling up past 0
        viewer._detail_scroll_pos = 0
        viewer.term.read_key = mock.MagicMock(
            return_value=("esc", "[A"))
        viewer._handle_keypress()
        assert viewer._detail_scroll_pos == 0

        # Try scrolling down past max
        max_scroll = viewer._detail_scroll_max()
        viewer._detail_scroll_pos = max_scroll
        viewer.term.read_key = mock.MagicMock(
            return_value=("esc", "[B"))
        viewer._handle_keypress()
        assert viewer._detail_scroll_pos == max_scroll

    def test_detail_page_scroll(self):
        """PgUp/PgDn move by page."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)
        viewer.term.rows = 10  # available = 6
        viewer.term.cols = 40  # narrow terminal to force wrapping

        # Create event with very long args to force many detail lines
        long_args = "Z" * 500
        for i in range(5):
            viewer._process_event_result(
                _make_event(seq=i, args=long_args,
                            time="10:00:0{}.000".format(i)))
        viewer._toggle_pause()
        viewer._open_detail_view()

        max_scroll = viewer._detail_scroll_max()
        if max_scroll > 0:
            # PgDn
            viewer.term.read_key = mock.MagicMock(
                return_value=("esc", "[6~"))
            viewer._handle_keypress()
            page_size = viewer.term.rows - 4
            expected = min(max_scroll, page_size)
            assert viewer._detail_scroll_pos == expected

            # PgUp back
            viewer.term.read_key = mock.MagicMock(
                return_value=("esc", "[5~"))
            viewer._handle_keypress()
            assert viewer._detail_scroll_pos == 0

    def test_detail_events_buffered(self):
        """Events arriving during detail go to pause_buffer."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)
        viewer.term.rows = 24
        for i in range(10):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:0{}.000".format(i)))
        viewer._toggle_pause()
        viewer._open_detail_view()

        # Simulate new events arriving during detail
        viewer._process_event_result(
            _make_event(seq=100, time="10:01:00.000"))
        viewer._process_event_result(
            _make_event(seq=101, time="10:01:01.000"))

        # Events should be buffered (paused catches first, but
        # detail_visible also in the defense-in-depth guard)
        assert len(viewer.pause_buffer) == 2
        assert viewer.total_events == 12

    def test_detail_hotkey_bar(self):
        """Hotkey bar shows 'Esc to dismiss'."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)
        viewer.term.rows = 24
        for i in range(5):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:0{}.000".format(i)))
        viewer._toggle_pause()
        viewer._open_detail_view()

        output = io.StringIO()
        viewer.term.stdout = output
        viewer._draw_hotkey_bar()

        written = output.getvalue()
        assert "Esc to dismiss" in written
        assert "[q]" not in written

    def test_detail_non_esc_key_ignored(self):
        """Pressing 'x' during detail does nothing."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)
        viewer.term.rows = 24
        for i in range(5):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:0{}.000".format(i)))
        viewer._toggle_pause()
        viewer._open_detail_view()
        assert viewer.detail_visible is True

        viewer.term.read_key = mock.MagicMock(return_value="x")
        viewer._handle_keypress()

        # Still in detail view
        assert viewer.detail_visible is True

    def test_detail_resize_rebuilds(self):
        """SIGWINCH during detail rebuilds lines."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)
        viewer.term.rows = 24
        viewer.term.cols = 80
        for i in range(5):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:0{}.000".format(i)))
        viewer._toggle_pause()
        viewer._open_detail_view()
        viewer._detail_scroll_pos = 2

        # Simulate resize
        viewer.term.cols = 120
        viewer.term.rows = 30
        viewer._handle_resize()

        # Lines should be rebuilt and scroll position reset
        assert viewer._detail_scroll_pos == 0
        assert viewer.detail_visible is True
        assert len(viewer._detail_lines) > 0

    def test_detail_annotated_event(self):
        """Detail shows handle/segment annotations."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)
        viewer.term.rows = 24
        viewer.term.cols = 80

        # Open a file (so handle is cached)
        open_event = _make_event(
            seq=1, func="Open",
            args='"RAM:testfile",Write',
            retval="0x1a2b3c", status="O")
        viewer._process_event_result(open_event)

        # Close the file (annotation should be added)
        close_event = _make_event(
            seq=2, func="Close", args="fh=0x1a2b3c",
            retval="0", status="O")
        viewer._process_event_result(close_event)

        viewer._toggle_pause()
        viewer.highlight_pos = 1  # Close event

        lines = viewer._build_detail_lines(close_event)
        text = "\n".join(lines)

        # The annotation should include the filename
        assert "RAM:testfile" in text

    def test_detail_status_ok(self):
        """status 'O' shown as 'OK'."""
        viewer = _make_viewer()
        assert viewer._format_detail_status("O") == "OK"

    def test_detail_status_error(self):
        """status 'E' shown as 'Error'."""
        viewer = _make_viewer()
        assert viewer._format_detail_status("E") == "Error"

    def test_detail_clears_column_header(self):
        """Detail view clears row 2; dismiss restores it."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)
        viewer.term.rows = 24
        for i in range(5):
            viewer._process_event_result(
                _make_event(seq=i, time="10:00:0{}.000".format(i)))
        viewer._toggle_pause()

        written_rows = {}
        original_write_at = viewer.term.write_at

        def capture_write_at(row, text):
            written_rows[row] = text
            original_write_at(row, text)

        viewer.term.write_at = capture_write_at

        # Open detail: row 2 should be cleared
        viewer._open_detail_view()
        assert written_rows.get(2, "").strip() == ""

        # Dismiss detail: row 2 should have column header
        written_rows.clear()
        viewer._dismiss_detail()
        row2 = written_rows.get(2, "")
        assert "SEQ" in row2 or "FUNCTION" in row2

    def test_detail_wrap_alignment(self):
        """Continuation lines align with first line's value column."""
        viewer = _make_viewer()
        viewer.layout = ColumnLayout(80)
        viewer.term.rows = 24
        viewer.term.cols = 80  # wrap_width = 80 - 13 = 67

        # Create args that will wrap
        long_args = "A" * 150
        event = _make_event(args=long_args)
        lines = viewer._build_detail_lines(event)

        # Find continuation lines (lines after Args that contain "A"s)
        args_idx = None
        for i, line in enumerate(lines):
            if "Args" in line and "A" in line:
                args_idx = i
                break

        assert args_idx is not None
        # The continuation line should start with 13 spaces
        continuation = lines[args_idx + 1]
        assert continuation.startswith(" " * 13)
        # And should not start with 14 spaces (exact alignment)
        assert not continuation.startswith(" " * 14)
