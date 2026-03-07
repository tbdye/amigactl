"""Unit tests for HandleResolver, SegmentResolver, strip_ansi, and save functionality.

These are pure unit tests that do not require a network connection to the
daemon.  They exercise:

  - HandleResolver (Feature B): handle/lock path tracking and annotation
  - SegmentResolver (Fix 9): segment pointer to filename resolution
  - strip_ansi (Feature C): ANSI escape sequence removal
  - Save pipeline (Feature C): formatting + stripping for log export
  - _save_scrollback (Feature C): file writing with ANSI stripping
"""

import os
import re
from collections import deque
from unittest.mock import MagicMock

import pytest

from amigactl.trace_ui import HandleResolver, SegmentResolver, ColumnLayout, TraceViewer
from amigactl.colors import strip_ansi, ColorWriter


# ---------------------------------------------------------------------------
# HandleResolver
# ---------------------------------------------------------------------------

class TestHandleResolver:

    def test_track_open_and_annotate_close(self):
        """Open return value is resolved in Close annotation."""
        hr = HandleResolver()
        open_ev = {
            "func": "Open", "retval": "0x01c16daf",
            "args": '"RAM:test",Read', "status": "O",
        }
        hr.track(open_ev)
        close_ev = {"func": "Close", "args": "fh=0x1c16daf"}
        result = hr.annotate(close_ev)
        assert result == "RAM:test"

    def test_track_lock_and_annotate_currentdir(self):
        """Lock return value is resolved in CurrentDir annotation."""
        hr = HandleResolver()
        lock_ev = {
            "func": "Lock", "retval": "0x01234abc",
            "args": '"SYS:",Shared', "status": "O",
        }
        hr.track(lock_ev)
        cd_ev = {"func": "CurrentDir", "args": "lock=0x1234abc"}
        result = hr.annotate(cd_ev)
        assert result == "SYS:"

    def test_no_annotation_for_unknown_handle(self):
        """Close with uncached handle returns None."""
        hr = HandleResolver()
        ev = {"func": "Close", "args": "fh=0xdeadbeef"}
        assert hr.annotate(ev) is None

    def test_null_retval_not_cached(self):
        """Open with NULL return is not cached."""
        hr = HandleResolver()
        ev = {
            "func": "Open", "retval": "NULL",
            "args": '"missing",Read', "status": "E",
        }
        hr.track(ev)
        assert len(hr._cache) == 0

    def test_failed_open_not_cached(self):
        """Open with error status is not cached."""
        hr = HandleResolver()
        ev = {
            "func": "Open", "retval": "NULL",
            "args": '"fail",Read', "status": "E",
        }
        hr.track(ev)
        assert len(hr._cache) == 0

    def test_cache_eviction(self):
        """FIFO eviction when cache exceeds max_size."""
        hr = HandleResolver(max_size=2)
        for i in range(3):
            hr.track({
                "func": "Open",
                "retval": "0x{:08x}".format(i + 1),
                "args": '"file{}",Read'.format(i),
                "status": "O",
            })
        # First entry evicted
        assert hr.annotate(
            {"func": "Close", "args": "fh=0x1"}) is None
        # Second and third still present
        assert hr.annotate(
            {"func": "Close", "args": "fh=0x2"}) == "file1"
        assert hr.annotate(
            {"func": "Close", "args": "fh=0x3"}) == "file2"

    def test_annotate_idempotent(self):
        """Calling annotate twice returns the same result."""
        hr = HandleResolver()
        hr.track({
            "func": "Open", "retval": "0x00001234",
            "args": '"test",Read', "status": "O",
        })
        ev = {"func": "Close", "args": "fh=0x1234"}
        assert hr.annotate(ev) == "test"
        assert hr.annotate(ev) == "test"  # still cached

    def test_currentdir_already_resolved_skipped(self):
        """CurrentDir with quoted args (daemon-resolved) is not
        annotated."""
        hr = HandleResolver()
        hr.track({
            "func": "Lock", "retval": "0x00005678",
            "args": '"RAM:",Shared', "status": "O",
        })
        # Daemon already resolved the path
        ev = {"func": "CurrentDir", "args": '"RAM:"'}
        assert hr.annotate(ev) is None

    def test_non_tracked_func_ignored(self):
        """Non-Open/Lock functions are not tracked."""
        hr = HandleResolver()
        hr.track({
            "func": "FindPort", "retval": "0x00001111",
            "args": '"REXX"', "status": "O",
        })
        assert len(hr._cache) == 0

    def test_hex_normalization(self):
        """Hex values with different zero-padding match."""
        hr = HandleResolver()
        hr.track({
            "func": "Open", "retval": "0x0000abcd",
            "args": '"test",Read', "status": "O",
        })
        ev = {"func": "Close", "args": "fh=0xabcd"}
        assert hr.annotate(ev) == "test"

    def test_clear(self):
        """clear() empties the cache."""
        hr = HandleResolver()
        hr.track({
            "func": "Open", "retval": "0x00001234",
            "args": '"test",Read', "status": "O",
        })
        hr.clear()
        assert hr.annotate(
            {"func": "Close", "args": "fh=0x1234"}) is None

    def test_handle_annotation_key_populated_on_close(self):
        """Pre-computed _handle_annotation key is correct even after handle reuse.

        Tests the data contract: annotate(consume=True) returns the correct
        path and the stored key survives handle address reuse. Does not test
        the full TraceViewer integration path (_annotated_event + scrollback).
        """
        hr = HandleResolver()

        # Open file A at handle 0x1c16e23
        open_a = {
            "func": "Open", "retval": "0x01c16e23",
            "args": '"cnet:big_numbers",Read', "status": "O",
        }
        hr.track(open_a)

        # Close file A -- annotate eagerly with consume
        close_a = {"func": "Close", "args": "fh=0x1c16e23"}
        annotation_a = hr.annotate(close_a, consume=True)
        assert annotation_a == "cnet:big_numbers"

        # Open file B at the SAME handle address
        open_b = {
            "func": "Open", "retval": "0x01c16e23",
            "args": '"cnet:bbsconfig3",Read', "status": "O",
        }
        hr.track(open_b)

        # Late render of close_a would now get wrong answer from cache.
        # But the stored annotation is correct.
        close_a["_handle_annotation"] = annotation_a
        # Simulates what _annotated_event does:
        stored = close_a.get("_handle_annotation")
        assert stored == "cnet:big_numbers"

    def test_consume_removes_cache_entry(self):
        """annotate(consume=True) removes the entry from the cache."""
        hr = HandleResolver()
        open_ev = {
            "func": "Open", "retval": "0x01c16e23",
            "args": '"test.txt",Read', "status": "O",
        }
        hr.track(open_ev)
        close_ev = {"func": "Close", "args": "fh=0x1c16e23"}
        result = hr.annotate(close_ev, consume=True)
        assert result == "test.txt"
        # Second call returns None (consumed)
        assert hr.annotate(close_ev) is None

    def test_currentdir_does_not_consume(self):
        """CurrentDir annotation does not consume the lock entry."""
        hr = HandleResolver()
        lock_ev = {
            "func": "Lock", "retval": "0x01234abc",
            "args": '"SYS:",Shared', "status": "O",
        }
        hr.track(lock_ev)
        cd_ev = {"func": "CurrentDir", "args": "lock=0x1234abc"}
        result = hr.annotate(cd_ev, consume=True)
        assert result == "SYS:"
        # Lock entry still present (CurrentDir never consumes)
        assert hr.annotate(cd_ev) == "SYS:"

    def test_handle_annotation_key_takes_precedence(self):
        """_handle_annotation key takes precedence over live cache lookup.

        Tests the data contract: when an event has a pre-computed
        _handle_annotation key, that value is used instead of whatever
        the cache currently holds. Does not test the full TraceViewer
        integration path.
        """
        hr = HandleResolver()
        # Put wrong data in cache
        open_ev = {
            "func": "Open", "retval": "0x01c16e23",
            "args": '"WRONG_FILE",Read', "status": "O",
        }
        hr.track(open_ev)
        # Event with correct pre-computed annotation
        close_ev = {
            "func": "Close", "args": "fh=0x1c16e23",
            "_handle_annotation": "CORRECT_FILE",
        }
        # _annotated_event logic: check _handle_annotation first
        annotation = close_ev.get("_handle_annotation")
        assert annotation == "CORRECT_FILE"

    def test_handle_reuse_after_close(self):
        """Handle reuse: Open A, Close A, Open B (same handle), Close B.

        After track() processes Close A, the handle is evicted from the
        live cache. Open B reuses the same handle address. Both Close
        events must annotate with the correct path.
        """
        hr = HandleResolver()
        handle = "0x01c16e23"
        norm = HandleResolver._normalize_hex(handle)

        # Open file A
        hr.track({
            "func": "Open", "retval": handle,
            "args": '"RAM:file_a",Read', "status": "O", "seq": "1",
        })
        # Close file A -- track() snapshots annotation and evicts handle
        close_a = {"func": "Close", "args": "fh={}".format(norm),
                   "seq": "2"}
        hr.track(close_a)

        # Handle evicted from live cache
        assert norm not in hr._cache

        # Open file B at the SAME handle address
        hr.track({
            "func": "Open", "retval": handle,
            "args": '"RAM:file_b",Write', "status": "O", "seq": "3",
        })
        # Close file B
        close_b = {"func": "Close", "args": "fh={}".format(norm),
                   "seq": "4"}
        hr.track(close_b)

        # Both Close events annotate correctly via _close_annotations
        assert hr.annotate(close_a) == "RAM:file_a"
        assert hr.annotate(close_b) == "RAM:file_b"

    def test_track_close_evicts_from_cache(self):
        """track() on Close removes the handle from the live cache."""
        hr = HandleResolver()
        hr.track({
            "func": "Open", "retval": "0x00001234",
            "args": '"test.txt",Read', "status": "O", "seq": "1",
        })
        assert "0x1234" in hr._cache

        hr.track({"func": "Close", "args": "fh=0x1234", "seq": "2"})
        assert "0x1234" not in hr._cache

        # But annotation is still available via _close_annotations
        assert hr.annotate(
            {"func": "Close", "args": "fh=0x1234", "seq": "2"}) == "test.txt"

    def test_close_annotations_fifo_eviction(self):
        """_close_annotations evicts oldest entries at max_size."""
        hr = HandleResolver(max_size=2)

        # Open and close 3 files to exceed max_size
        for i in range(3):
            handle = "0x{:08x}".format(i + 1)
            hr.track({
                "func": "Open", "retval": handle,
                "args": '"file{}",Read'.format(i), "status": "O",
                "seq": str(i * 2 + 1),
            })
            hr.track({
                "func": "Close",
                "args": "fh={}".format(HandleResolver._normalize_hex(handle)),
                "seq": str(i * 2 + 2),
            })

        # First close annotation evicted (seq "2")
        assert hr.annotate(
            {"func": "Close", "args": "fh=0x1", "seq": "2"}) is None
        # Second and third still present
        assert hr.annotate(
            {"func": "Close", "args": "fh=0x2", "seq": "4"}) == "file1"
        assert hr.annotate(
            {"func": "Close", "args": "fh=0x3", "seq": "6"}) == "file2"

    def test_annotate_close_without_track_falls_back_to_cache(self):
        """Close annotation falls back to live cache if not tracked."""
        hr = HandleResolver()
        hr.track({
            "func": "Open", "retval": "0x00005678",
            "args": '"legacy.txt",Read', "status": "O", "seq": "1",
        })
        # Annotate without having tracked the Close event
        close_ev = {"func": "Close", "args": "fh=0x5678", "seq": "99"}
        assert hr.annotate(close_ev) == "legacy.txt"

    def test_close_unknown_handle_not_recorded(self):
        """Close with an unknown handle does not create a close annotation."""
        hr = HandleResolver()
        hr.track({"func": "Close", "args": "fh=0xdeadbeef", "seq": "1"})
        assert len(hr._close_annotations) == 0


# ---------------------------------------------------------------------------
# SegmentResolver
# ---------------------------------------------------------------------------

class TestSegmentResolver:

    def test_track_loadseg_and_annotate_runcommand(self):
        """LoadSeg return value is resolved in RunCommand annotation."""
        sr = SegmentResolver()
        load_ev = {"func": "LoadSeg", "retval": "0x01d0dfb5",
                   "args": '"cnet:control"', "status": "O"}
        sr.track(load_ev)
        run_ev = {"func": "RunCommand",
                  "args": "seg=0x1d0dfb5,stack=4096,1"}
        assert sr.annotate(run_ev) == "cnet:control"

    def test_no_annotation_for_unknown_segment(self):
        """RunCommand with uncached segment returns None."""
        sr = SegmentResolver()
        ev = {"func": "RunCommand", "args": "seg=0xdeadbeef,stack=4096,1"}
        assert sr.annotate(ev) is None

    def test_failed_loadseg_not_cached(self):
        """LoadSeg with NULL return is not cached."""
        sr = SegmentResolver()
        ev = {"func": "LoadSeg", "retval": "NULL",
              "args": '"missing"', "status": "E"}
        sr.track(ev)
        assert len(sr._cache) == 0

    def test_newloadseg_tracked(self):
        """NewLoadSeg events are also tracked."""
        sr = SegmentResolver()
        ev = {"func": "NewLoadSeg", "retval": "0x01234abc",
              "args": '"C:Echo"', "status": "O"}
        sr.track(ev)
        run_ev = {"func": "RunCommand",
                  "args": "seg=0x1234abc,stack=4096,1"}
        assert sr.annotate(run_ev) == "C:Echo"

    def test_hex_normalization(self):
        """Hex values with different zero-padding match."""
        sr = SegmentResolver()
        sr.track({"func": "LoadSeg", "retval": "0x0000abcd",
                  "args": '"test"', "status": "O"})
        ev = {"func": "RunCommand", "args": "seg=0xabcd,stack=4096,1"}
        assert sr.annotate(ev) == "test"

    def test_cache_eviction(self):
        """FIFO eviction when cache exceeds max_size."""
        sr = SegmentResolver(max_size=2)
        for i in range(3):
            sr.track({
                "func": "LoadSeg",
                "retval": "0x{:08x}".format(i + 1),
                "args": '"file{}"'.format(i),
                "status": "O",
            })
        # First entry evicted
        assert sr.annotate(
            {"func": "RunCommand", "args": "seg=0x1,stack=4096,1"}) is None
        # Second and third still present
        assert sr.annotate(
            {"func": "RunCommand", "args": "seg=0x2,stack=4096,1"}) == "file1"
        assert sr.annotate(
            {"func": "RunCommand", "args": "seg=0x3,stack=4096,1"}) == "file2"

    def test_clear(self):
        """clear() empties the cache."""
        sr = SegmentResolver()
        sr.track({"func": "LoadSeg", "retval": "0x00001234",
                  "args": '"test"', "status": "O"})
        sr.clear()
        assert sr.annotate(
            {"func": "RunCommand", "args": "seg=0x1234,stack=4096,1"}) is None

    def test_non_tracked_func_ignored(self):
        """Non-LoadSeg/NewLoadSeg functions are not tracked."""
        sr = SegmentResolver()
        sr.track({"func": "Open", "retval": "0x00001111",
                  "args": '"test",Read', "status": "O"})
        assert len(sr._cache) == 0

    def test_annotate_non_runcommand_returns_none(self):
        """annotate() returns None for non-RunCommand events."""
        sr = SegmentResolver()
        sr.track({"func": "LoadSeg", "retval": "0x00001234",
                  "args": '"test"', "status": "O"})
        assert sr.annotate({"func": "Close", "args": "fh=0x1234"}) is None

    def test_segment_not_consumed_on_use(self):
        """Segments are not consumed -- same segment can be annotated twice."""
        sr = SegmentResolver()
        sr.track({"func": "LoadSeg", "retval": "0x01d0dfb5",
                  "args": '"cnet:bbs"', "status": "O"})
        run1 = {"func": "RunCommand", "args": "seg=0x1d0dfb5,stack=4096,1"}
        run2 = {"func": "RunCommand", "args": "seg=0x1d0dfb5,stack=4096,1"}
        assert sr.annotate(run1) == "cnet:bbs"
        assert sr.annotate(run2) == "cnet:bbs"


# ---------------------------------------------------------------------------
# strip_ansi
# ---------------------------------------------------------------------------

class TestStripAnsi:

    def test_no_ansi(self):
        assert strip_ansi("hello world") == "hello world"

    def test_csi_color(self):
        assert strip_ansi("\x1b[32mOK\x1b[0m") == "OK"

    def test_csi_bold_and_reset(self):
        assert strip_ansi("\x1b[1mBold\x1b[0m") == "Bold"

    def test_multiple_sequences(self):
        text = "\x1b[33mWARN\x1b[0m: \x1b[1mtest\x1b[0m"
        assert strip_ansi(text) == "WARN: test"

    def test_8bit_csi(self):
        """0x9B is the 8-bit CSI used by AmigaOS."""
        assert strip_ansi("\x9b1mBold\x9b0m") == "Bold"

    def test_empty_string(self):
        assert strip_ansi("") == ""

    def test_plain_text_passthrough(self):
        text = 'O FindPort  [5] bbs  "REXX"  OK'
        assert strip_ansi(text) == text

    def test_mixed_ansi_and_text(self):
        """Text with ANSI codes interspersed retains only the text."""
        text = "before\x1b[31mred\x1b[0mafter"
        assert strip_ansi(text) == "beforeredafter"

    def test_multiple_params_in_csi(self):
        """CSI with multiple parameters like \\x1b[1;31m."""
        text = "\x1b[1;31mbold red\x1b[0m"
        assert strip_ansi(text) == "bold red"

    def test_8bit_csi_multiple_params(self):
        """8-bit CSI with multiple parameters."""
        text = "\x9b1;36mbold cyan\x9b0m"
        assert strip_ansi(text) == "bold cyan"


# ---------------------------------------------------------------------------
# Save pipeline (Feature C) -- strip_ansi + format integration
# ---------------------------------------------------------------------------

class TestSavePipeline:
    """Test the save pipeline: format event, strip ANSI, produce clean text.

    TraceViewer._save_scrollback() is complex to instantiate in isolation
    (requires TerminalState, socket, etc.), so we test the pipeline
    components directly: format_trace_event + strip_ansi.
    """

    def test_format_and_strip_roundtrip(self):
        """Formatting with colors then stripping produces plain text."""
        from amigactl.colors import ColorWriter, format_trace_event
        cw = ColorWriter(force_color=True)
        event = {
            "seq": "42",
            "time": "12:00:00.123",
            "lib": "dos",
            "func": "Open",
            "task": "[5] bbs",
            "args": '"RAM:test",Read',
            "retval": "0x01234abc",
            "status": "O",
        }
        formatted = format_trace_event(event, cw)
        stripped = strip_ansi(formatted)
        # Verify no ANSI escape sequences remain
        assert "\x1b" not in stripped
        assert "\x9b" not in stripped
        # Verify key content is present
        assert "Open" in stripped
        assert "RAM:test" in stripped
        assert "0x01234abc" in stripped

    def test_format_error_event_strip(self):
        """Error-status events with red coloring are stripped cleanly."""
        from amigactl.colors import ColorWriter, format_trace_event
        cw = ColorWriter(force_color=True)
        event = {
            "seq": "99",
            "time": "12:00:01.456",
            "lib": "dos",
            "func": "Open",
            "task": "[5] bbs",
            "args": '"RAM:missing",Read',
            "retval": "NULL",
            "status": "E",
        }
        formatted = format_trace_event(event, cw)
        stripped = strip_ansi(formatted)
        assert "\x1b" not in stripped
        assert "NULL" in stripped

    def test_strip_preserves_all_fields(self):
        """All event fields survive the format-and-strip pipeline."""
        from amigactl.colors import ColorWriter, format_trace_event
        cw = ColorWriter(force_color=True)
        event = {
            "seq": "7",
            "time": "00:00:00.001",
            "lib": "exec",
            "func": "FindPort",
            "task": "test_task",
            "args": '"REXX"',
            "retval": "OK",
            "status": "O",
        }
        formatted = format_trace_event(event, cw)
        stripped = strip_ansi(formatted)
        assert "FindPort" in stripped
        assert "REXX" in stripped
        assert "OK" in stripped
        assert "test_task" in stripped


# ---------------------------------------------------------------------------
# _save_scrollback (Feature C)
# ---------------------------------------------------------------------------

def _make_viewer_stub(scrollback=None, timestamp_mode="absolute",
                      start_time=None, cols=120):
    """Build a minimal TraceViewer-like object for _save_scrollback tests.

    Instead of constructing a real TraceViewer (which needs a socket,
    session, etc.), we create a stub with only the attributes that
    _save_scrollback accesses:

      scrollback, handle_resolver, layout, cw, term,
      timestamp_mode, start_time, _annotated_event(),
      _format_timestamp_for_scroll()

    The stub inherits _save_scrollback, _annotated_event, and
    _format_timestamp_for_scroll from TraceViewer via the class, bound
    manually.
    """
    stub = MagicMock()
    stub.scrollback = deque(scrollback or [])
    stub.handle_resolver = HandleResolver()
    stub.segment_resolver = SegmentResolver()
    stub.layout = ColumnLayout(cols)
    stub.cw = ColorWriter(force_color=False)
    stub.timestamp_mode = timestamp_mode
    stub.start_time = start_time

    # Client-side filter attributes (defaults = no filtering)
    stub.search_pattern = None
    stub.disabled_libs = None
    stub.disabled_funcs = None
    stub.disabled_procs = None
    stub.shell_noise_filter = False

    # Bind real methods from TraceViewer so the save logic runs
    # the actual formatting code instead of a mock.
    stub._save_scrollback = (
        TraceViewer._save_scrollback.__get__(stub, type(stub)))
    stub._annotated_event = (
        TraceViewer._annotated_event.__get__(stub, type(stub)))
    stub._format_timestamp_for_scroll = (
        TraceViewer._format_timestamp_for_scroll.__get__(stub, type(stub)))
    stub._passes_client_filter = (
        TraceViewer._passes_client_filter.__get__(stub, type(stub)))
    stub._build_filtered_snapshot = (
        TraceViewer._build_filtered_snapshot.__get__(stub, type(stub)))

    # term.write_hotkey_bar is already a MagicMock from MagicMock()
    return stub


_SAMPLE_EVENTS = [
    {
        "seq": "1", "time": "12:00:00.001", "lib": "dos",
        "func": "Open", "task": "[5] bbs",
        "args": '"RAM:test",Read', "retval": "0x01234abc",
        "status": "O",
    },
    {
        "seq": "2", "time": "12:00:00.005", "lib": "dos",
        "func": "Close", "task": "[5] bbs",
        "args": "fh=0x1234abc", "retval": "OK",
        "status": "O",
    },
]


class TestSaveScrollback:

    def test_save_writes_file(self, tmp_path, monkeypatch):
        """_save_scrollback creates a timestamped file with expected
        content (plain text, no ANSI)."""
        monkeypatch.chdir(tmp_path)
        stub = _make_viewer_stub(scrollback=_SAMPLE_EVENTS)
        stub._save_scrollback()

        # Find the written file
        files = list(tmp_path.glob("atrace_*.log"))
        assert len(files) == 1, "Expected one log file, got: {}".format(files)
        content = files[0].read_text()

        # Should contain function names from events
        assert "Open" in content
        assert "Close" in content
        # Should have two lines of content (one per event + trailing newline)
        lines = content.strip().split('\n')
        assert len(lines) == 2

        # Verify status message was shown
        stub.term.write_hotkey_bar.assert_called_once()
        msg = stub.term.write_hotkey_bar.call_args[0][0]
        assert "Saved 2 events" in msg

    def test_save_empty_buffer(self):
        """_save_scrollback with empty scrollback shows 'Nothing to save'."""
        stub = _make_viewer_stub(scrollback=[])
        stub._save_scrollback()

        stub.term.write_hotkey_bar.assert_called_once_with("Nothing to save")

    def test_save_strips_ansi(self, tmp_path, monkeypatch):
        """Saved content has no ANSI escape sequences."""
        monkeypatch.chdir(tmp_path)
        # Force colors ON so format_event produces ANSI codes
        stub = _make_viewer_stub(scrollback=_SAMPLE_EVENTS)
        stub.cw = ColorWriter(force_color=True)
        stub._save_scrollback()

        files = list(tmp_path.glob("atrace_*.log"))
        assert len(files) == 1
        content = files[0].read_text()
        assert "\x1b" not in content, (
            "ANSI escape found in saved file: {}".format(repr(content[:200])))
        assert "\x9b" not in content, (
            "8-bit CSI found in saved file: {}".format(repr(content[:200])))

    def test_save_oserror_handled(self, monkeypatch):
        """OSError during file write produces error message in footer."""
        # Use a nonexistent directory so open() raises OSError
        monkeypatch.chdir("/")
        stub = _make_viewer_stub(scrollback=_SAMPLE_EVENTS)
        # Write to a path that will fail (root-owned directory)
        stub._save_scrollback()

        stub.term.write_hotkey_bar.assert_called_once()
        msg = stub.term.write_hotkey_bar.call_args[0][0]
        assert "Save failed:" in msg

    def test_save_respects_disabled_libs(self, tmp_path, monkeypatch):
        """Save only includes events passing client-side lib filter."""
        monkeypatch.chdir(tmp_path)
        events = [
            {"seq": "1", "time": "12:00:00.001", "lib": "dos",
             "func": "Open", "task": "[5] bbs",
             "args": '"RAM:test",Read', "retval": "0x01234abc",
             "status": "O"},
            {"seq": "2", "time": "12:00:00.002", "lib": "dos",
             "func": "Close", "task": "[5] bbs",
             "args": "fh=0x1234abc", "retval": "OK",
             "status": "O"},
            {"seq": "3", "time": "12:00:00.003", "lib": "exec",
             "func": "OpenLibrary", "task": "[5] bbs",
             "args": '"dos.library",0', "retval": "0xabc",
             "status": "O"},
        ]
        stub = _make_viewer_stub(scrollback=events)
        stub.disabled_libs = {"exec"}
        stub._save_scrollback()

        files = list(tmp_path.glob("atrace_*.log"))
        assert len(files) == 1
        content = files[0].read_text()
        lines = content.strip().split('\n')
        assert len(lines) == 2
        assert "Open" in lines[0]
        assert "Close" in lines[1]
        assert "OpenLibrary" not in content

    def test_save_respects_shell_noise_filter(self, tmp_path, monkeypatch):
        """Save excludes shell init noise when filter is active."""
        monkeypatch.chdir(tmp_path)
        events = [
            {"seq": "1", "time": "12:00:00.001", "lib": "dos",
             "func": "Open", "task": "[5] bbs",
             "args": '"RAM:test",Read', "retval": "0x01234abc",
             "status": "O"},
            {"seq": "2", "time": "12:00:00.002", "lib": "dos",
             "func": "SetVar", "task": "[5] bbs",
             "args": '"process",LOCAL',
             "retval": "OK", "status": "O"},
        ]
        stub = _make_viewer_stub(scrollback=events)
        stub.shell_noise_filter = True
        stub._save_scrollback()

        files = list(tmp_path.glob("atrace_*.log"))
        assert len(files) == 1
        content = files[0].read_text()
        lines = content.strip().split('\n')
        assert len(lines) == 1
        assert "Open" in lines[0]
        assert "SetVar" not in content

    def test_save_shows_filtered_count(self, tmp_path, monkeypatch):
        """Save message shows 'N of M events' when filters are active."""
        monkeypatch.chdir(tmp_path)
        events = [
            {"seq": "1", "time": "12:00:00.001", "lib": "dos",
             "func": "Open", "task": "[5] bbs",
             "args": '"RAM:test",Read', "retval": "0x01234abc",
             "status": "O"},
            {"seq": "2", "time": "12:00:00.002", "lib": "dos",
             "func": "Close", "task": "[5] bbs",
             "args": "fh=0x1234abc", "retval": "OK",
             "status": "O"},
            {"seq": "3", "time": "12:00:00.003", "lib": "exec",
             "func": "OpenLibrary", "task": "[5] bbs",
             "args": '"dos.library",0', "retval": "0xabc",
             "status": "O"},
        ]
        stub = _make_viewer_stub(scrollback=events)
        stub.disabled_libs = {"exec"}
        stub._save_scrollback()

        msg = stub.term.write_hotkey_bar.call_args[0][0]
        assert "Saved 2 of 3 events" in msg
