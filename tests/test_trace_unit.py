"""Unit tests for atrace Python-side logic.

These are pure unit tests that do not require a network connection to the
daemon.  They exercise event parsing, command string building, response
parsing, and shell/CLI formatting for the atrace library call tracing
feature.
"""

import cmd
from unittest import mock

import pytest

from amigactl import (
    AmigaConnection, CommandSyntaxError, InternalError, RawTraceSession,
    _parse_trace_event, read_one_trace_event,
)
from amigactl.colors import ColorWriter, TRACE_HEADER, format_trace_event
from amigactl.protocol import ENCODING, ProtocolError, TraceStreamReader
from amigactl.shell import AmigaShell, _DirCache


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _make_mock_conn():
    """Create an AmigaConnection with mocked internals for unit testing.

    For trace_enable/trace_disable/trace_status, mock _send_command.
    For trace_start (which uses protocol-level calls directly),
    mock at the amigactl module level instead -- see
    TestTraceCommandBuilding for that pattern.
    """
    conn = AmigaConnection.__new__(AmigaConnection)
    conn._sock = mock.MagicMock()
    conn._banner = "AMIGACTL 0.8.0"
    conn._send_command = mock.MagicMock(return_value=("", []))
    return conn


def _make_shell():
    """Create an AmigaShell with a mocked connection for unit testing."""
    shell = AmigaShell.__new__(AmigaShell)
    cmd.Cmd.__init__(shell)
    shell.host = "test"
    shell.port = 6800
    shell.timeout = 30
    shell.conn = mock.MagicMock()
    shell.cw = ColorWriter(force_color=False)
    shell.cwd = "SYS:"
    shell._dir_cache = _DirCache()
    shell._editor = None
    return shell


# ---------------------------------------------------------------------------
# TestParseTraceEvent
# ---------------------------------------------------------------------------

class TestParseTraceEvent:
    """Tests for _parse_trace_event() in client/amigactl/__init__.py."""

    def test_full_event(self):
        text = '42\t14:30:01.000\texec.OpenLibrary\tShell Process\t"dos.library",0\t0x07a3b2c0\tO'
        event = _parse_trace_event(text)
        assert event["seq"] == 42
        assert event["time"] == "14:30:01.000"
        assert event["lib"] == "exec"
        assert event["func"] == "OpenLibrary"
        assert event["task"] == "Shell Process"
        assert event["args"] == '"dos.library",0'
        assert event["retval"] == "0x07a3b2c0"
        assert event["status"] == "O"
        assert event["type"] == "event"

    def test_full_event_error_status(self):
        text = '43\t14:30:02.000\texec.OpenLibrary\tShell Process\t"bogus.library",0\tNULL\tE'
        event = _parse_trace_event(text)
        assert event["retval"] == "NULL"
        assert event["status"] == "E"

    def test_full_event_neutral_status(self):
        text = '44\t14:30:03.000\texec.PutMsg\tShell Process\t0x1234,0x5678\t(void)\t-'
        event = _parse_trace_event(text)
        assert event["retval"] == "(void)"
        assert event["status"] == "-"

    def test_minimal_event(self):
        event = _parse_trace_event("1\t12:00:00.000")
        assert event["seq"] == 1
        assert event["time"] == "12:00:00.000"
        assert event["lib"] == ""
        assert event["func"] == ""
        assert event["task"] == ""
        assert event["args"] == ""
        assert event["retval"] == ""
        assert event["status"] == "-"

    def test_empty_string(self):
        event = _parse_trace_event("")
        assert event["seq"] == 0
        assert event["time"] == ""
        assert event["lib"] == ""
        assert event["func"] == ""
        assert event["task"] == ""
        assert event["args"] == ""
        assert event["retval"] == ""
        assert event["status"] == "-"

    def test_lib_func_split(self):
        event = _parse_trace_event("1\t00:00\tdos.Open\ttask\targs\tret\tO")
        assert event["lib"] == "dos"
        assert event["func"] == "Open"
        assert event["status"] == "O"

    def test_no_dot_in_func(self):
        event = _parse_trace_event("1\t00:00\tSomeName\ttask\targs\tret\t-")
        assert event["lib"] == ""
        assert event["func"] == "SomeName"

    def test_invalid_seq(self):
        event = _parse_trace_event("abc\t00:00\texec.Open\ttask\targs\tret\tE")
        assert event["seq"] == 0

    def test_comment_not_parsed(self):
        # Comment detection happens in trace_start() before calling the
        # parser.  The parser never sees comment lines.  This verifies
        # that if a #-prefixed line were somehow parsed, it would produce
        # seq=0 (the "#" is not numeric).
        event = _parse_trace_event("# OVERFLOW 5 events dropped")
        assert event["seq"] == 0

    def test_six_field_backward_compat(self):
        """Old 6-field format (no status) defaults status to '-'."""
        text = '42\t14:30:01.000\texec.OpenLibrary\tShell Process\t"dos.library",0\t0x07a3b2c0'
        event = _parse_trace_event(text)
        assert event["retval"] == "0x07a3b2c0"
        assert event["status"] == "-"

    def test_microsecond_timestamp_preserved(self):
        """6-digit microsecond timestamps are preserved as-is in the time field."""
        text = '42\t14:30:01.123456\texec.OpenLibrary\tShell Process\t"dos.library",0\t0x07a3b2c0\tO'
        event = _parse_trace_event(text)
        assert event["time"] == "14:30:01.123456"

    def test_3digit_timestamp_preserved(self):
        """3-digit millisecond timestamps are preserved as-is in the time field."""
        text = '42\t14:30:01.123\texec.OpenLibrary\tShell Process\t"dos.library",0\t0x07a3b2c0\tO'
        event = _parse_trace_event(text)
        assert event["time"] == "14:30:01.123"


# ---------------------------------------------------------------------------
# TestFormatTraceEvent
# ---------------------------------------------------------------------------

class TestFormatTraceEvent:
    """Tests for format_trace_event() in client/amigactl/colors.py."""

    def _event(self, **overrides):
        """Build a default event dict with optional overrides."""
        base = {
            "seq": 42, "time": "14:30:01.000", "lib": "exec",
            "func": "OpenLibrary", "task": "Shell Process",
            "args": '"dos.library",0', "retval": "0x07a3b2c0",
            "status": "O", "type": "event",
        }
        base.update(overrides)
        return base

    def test_normal_event(self):
        cw = ColorWriter(force_color=False)
        event = self._event()
        result = format_trace_event(event, cw)
        assert "42" in result
        assert "14:30:01.000" in result
        assert "exec" in result
        assert "OpenLibrary" in result
        assert "Shell Process" in result
        assert '"dos.library",0' in result
        assert "0x07a3b2c0" in result

    def test_error_status_null(self):
        cw = ColorWriter(force_color=True)
        event = self._event(retval="NULL", status="E")
        result = format_trace_event(event, cw)
        assert "\033[31m" in result
        assert "NULL" in result

    def test_error_status_fail(self):
        cw = ColorWriter(force_color=True)
        event = self._event(retval="FAIL", status="E")
        result = format_trace_event(event, cw)
        assert "\033[31m" in result
        assert "FAIL" in result

    def test_error_status_minus1(self):
        cw = ColorWriter(force_color=True)
        event = self._event(retval="-1", status="E")
        result = format_trace_event(event, cw)
        assert "\033[31m" in result
        assert "-1" in result

    def test_success_status_pointer(self):
        cw = ColorWriter(force_color=True)
        event = self._event(retval="0x07a3b2c0", status="O")
        result = format_trace_event(event, cw)
        # GREEN should wrap a successful return value
        assert "\033[32m" in result
        # RED should not wrap a successful return value
        assert "\033[31m" not in result
        assert "0x07a3b2c0" in result

    def test_success_status_ok(self):
        cw = ColorWriter(force_color=True)
        event = self._event(retval="OK", status="O")
        result = format_trace_event(event, cw)
        assert "\033[32m" in result
        assert "\033[31m" not in result

    def test_neutral_status_void(self):
        cw = ColorWriter(force_color=True)
        event = self._event(retval="(void)", status="-")
        result = format_trace_event(event, cw)
        # RED and GREEN should not wrap the retval itself
        assert "\033[31m(void)" not in result
        assert "\033[32m(void)" not in result
        # The retval text should still appear
        assert "(void)" in result

    def test_neutral_status_no_color(self):
        """Neutral status produces plain text (no color wrapping)."""
        cw = ColorWriter(force_color=True)
        event = self._event(retval="(empty)", status="-")
        result = format_trace_event(event, cw)
        # The retval itself should appear but not be wrapped in color
        assert "(empty)" in result
        # Check that retval is NOT wrapped in red or green
        # (other parts of the line have colors for seq, lib, func, task)
        parts = result.rsplit("(empty)", 1)
        # The retval is the last field, so anything after it is empty
        assert "\033[31m(empty)" not in result
        assert "\033[32m(empty)" not in result

    def test_comment_event(self):
        cw = ColorWriter(force_color=False)
        event = {"type": "comment", "text": "OVERFLOW 5 events dropped"}
        result = format_trace_event(event, cw)
        assert result == "# OVERFLOW 5 events dropped"

    def test_color_disabled(self):
        cw = ColorWriter(force_color=False)
        event = self._event()
        result = format_trace_event(event, cw)
        assert "\033[" not in result

    def test_backward_compat_no_status(self):
        """Events without status field (old daemon) get no retval color."""
        cw = ColorWriter(force_color=True)
        event = self._event()
        del event["status"]  # simulate old daemon
        result = format_trace_event(event, cw)
        # Default status is '-' (neutral), so no color on retval
        assert "\033[31m" not in result or "0x07a3b2c0" not in result.split("\033[31m")[-1]
        # The retval should appear in the output
        assert "0x07a3b2c0" in result


# ---------------------------------------------------------------------------
# TestTraceHeader
# ---------------------------------------------------------------------------

class TestTraceHeader:
    """Tests for the TRACE_HEADER constant in client/amigactl/colors.py."""

    def test_header_contains_column_names(self):
        assert "SEQ" in TRACE_HEADER
        assert "TIME" in TRACE_HEADER
        assert "FUNCTION" in TRACE_HEADER
        assert "TASK" in TRACE_HEADER
        assert "ARGS" in TRACE_HEADER
        assert "RESULT" in TRACE_HEADER

    def test_header_column_widths(self):
        """Verify the header matches the widened column format."""
        expected = "{:<10s} {:>13s}  {:<28s} {:<20s} {:<40s} {}".format(
            "SEQ", "TIME", "FUNCTION", "TASK", "ARGS", "RESULT")
        assert TRACE_HEADER == expected


# ---------------------------------------------------------------------------
# TestTraceCommandBuilding
# ---------------------------------------------------------------------------

class TestTraceCommandBuilding:
    """Tests verifying the client library builds correct command strings."""

    @mock.patch("amigactl.read_line")
    @mock.patch("amigactl.send_command")
    def test_trace_start_no_filters(self, mock_send, mock_readline):
        conn = _make_mock_conn()
        conn._sock.gettimeout.return_value = 10
        mock_readline.side_effect = ["OK", "END", "."]
        callback = mock.MagicMock()

        conn.trace_start(callback)

        mock_send.assert_called_once_with(conn._sock, "TRACE START")

    @mock.patch("amigactl.read_line")
    @mock.patch("amigactl.send_command")
    def test_trace_start_lib(self, mock_send, mock_readline):
        conn = _make_mock_conn()
        conn._sock.gettimeout.return_value = 10
        mock_readline.side_effect = ["OK", "END", "."]
        callback = mock.MagicMock()

        conn.trace_start(callback, lib="dos")

        mock_send.assert_called_once_with(conn._sock, "TRACE START LIB=dos")

    @mock.patch("amigactl.read_line")
    @mock.patch("amigactl.send_command")
    def test_trace_start_func(self, mock_send, mock_readline):
        conn = _make_mock_conn()
        conn._sock.gettimeout.return_value = 10
        mock_readline.side_effect = ["OK", "END", "."]
        callback = mock.MagicMock()

        conn.trace_start(callback, func="Open")

        mock_send.assert_called_once_with(
            conn._sock, "TRACE START FUNC=Open")

    @mock.patch("amigactl.read_line")
    @mock.patch("amigactl.send_command")
    def test_trace_start_proc(self, mock_send, mock_readline):
        conn = _make_mock_conn()
        conn._sock.gettimeout.return_value = 10
        mock_readline.side_effect = ["OK", "END", "."]
        callback = mock.MagicMock()

        conn.trace_start(callback, proc="Shell")

        mock_send.assert_called_once_with(
            conn._sock, "TRACE START PROC=Shell")

    @mock.patch("amigactl.read_line")
    @mock.patch("amigactl.send_command")
    def test_trace_start_errors(self, mock_send, mock_readline):
        conn = _make_mock_conn()
        conn._sock.gettimeout.return_value = 10
        mock_readline.side_effect = ["OK", "END", "."]
        callback = mock.MagicMock()

        conn.trace_start(callback, errors_only=True)

        mock_send.assert_called_once_with(
            conn._sock, "TRACE START ERRORS")

    @mock.patch("amigactl.read_line")
    @mock.patch("amigactl.send_command")
    def test_trace_start_combined(self, mock_send, mock_readline):
        conn = _make_mock_conn()
        conn._sock.gettimeout.return_value = 10
        mock_readline.side_effect = ["OK", "END", "."]
        callback = mock.MagicMock()

        conn.trace_start(
            callback, lib="dos", func="Open", proc="myapp",
            errors_only=True)

        mock_send.assert_called_once_with(
            conn._sock,
            "TRACE START LIB=dos FUNC=Open PROC=myapp ERRORS")

    @mock.patch("amigactl.read_line")
    @mock.patch("amigactl.send_command")
    def test_trace_start_error(self, mock_send, mock_readline):
        """trace_start raises on ERR response."""
        conn = _make_mock_conn()
        conn._sock.gettimeout.return_value = 10
        mock_readline.side_effect = ["ERR 500 atrace is not loaded", "."]
        with pytest.raises(InternalError, match="atrace is not loaded"):
            conn.trace_start(callback=mock.MagicMock())

    def test_trace_enable_global(self):
        conn = _make_mock_conn()
        conn.trace_enable()
        conn._send_command.assert_called_once_with("TRACE ENABLE")

    def test_trace_enable_funcs(self):
        conn = _make_mock_conn()
        conn.trace_enable(funcs=["Open", "Lock"])
        conn._send_command.assert_called_once_with(
            "TRACE ENABLE Open Lock")

    def test_trace_disable_global(self):
        conn = _make_mock_conn()
        conn.trace_disable()
        conn._send_command.assert_called_once_with("TRACE DISABLE")

    def test_trace_disable_funcs(self):
        conn = _make_mock_conn()
        conn.trace_disable(funcs=["GetMsg", "ObtainSemaphore"])
        conn._send_command.assert_called_once_with(
            "TRACE DISABLE GetMsg ObtainSemaphore")


# ---------------------------------------------------------------------------
# TestTraceStatusParsing
# ---------------------------------------------------------------------------

class TestTraceStatusParsing:
    """Tests for trace_status() response parsing."""

    def test_loaded_status(self):
        conn = _make_mock_conn()
        conn._send_command.return_value = ("", [
            "loaded=1", "enabled=1", "patches=50",
            "events_produced=1000", "events_consumed=950",
            "events_dropped=50", "buffer_capacity=8192",
            "buffer_used=100",
        ])
        result = conn.trace_status()
        assert result["loaded"] is True
        assert result["enabled"] is True
        assert result["patches"] == 50
        assert result["events_produced"] == 1000
        assert result["events_consumed"] == 950
        assert result["events_dropped"] == 50
        assert result["buffer_capacity"] == 8192
        assert result["buffer_used"] == 100

    def test_not_loaded_status(self):
        conn = _make_mock_conn()
        conn._send_command.return_value = ("", ["loaded=0"])
        result = conn.trace_status()
        assert result["loaded"] is False
        assert "enabled" not in result

    def test_not_loaded_no_patch_list(self):
        conn = _make_mock_conn()
        conn._send_command.return_value = ("", ["loaded=0"])
        result = conn.trace_status()
        assert result["loaded"] is False
        assert "patch_list" not in result

    def test_patch_list_parsing(self):
        conn = _make_mock_conn()
        conn._send_command.return_value = ("", [
            "loaded=1", "enabled=1", "patches=3",
            "events_produced=0", "events_consumed=0",
            "events_dropped=0", "buffer_capacity=8192",
            "buffer_used=0",
            "patch_0=exec.FindPort enabled=1",
            "patch_1=exec.FindTask enabled=0",
            "patch_2=dos.Open enabled=1",
        ])
        result = conn.trace_status()
        assert len(result["patch_list"]) == 3
        assert result["patch_list"][0] == {
            "name": "exec.FindPort", "enabled": True}
        assert result["patch_list"][1] == {
            "name": "exec.FindTask", "enabled": False}
        assert result["patch_list"][2] == {
            "name": "dos.Open", "enabled": True}

    def test_patch_disabled(self):
        conn = _make_mock_conn()
        conn._send_command.return_value = ("", [
            "loaded=1", "enabled=1", "patches=1",
            "events_produced=0", "events_consumed=0",
            "events_dropped=0", "buffer_capacity=8192",
            "buffer_used=0",
            "patch_0=exec.GetMsg enabled=0",
        ])
        result = conn.trace_status()
        assert result["patch_list"][0]["enabled"] is False

    def test_missing_fields_graceful(self):
        conn = _make_mock_conn()
        conn._send_command.return_value = ("", [
            "loaded=1", "enabled=1",
        ])
        result = conn.trace_status()
        assert result["loaded"] is True
        assert "patches" not in result
        assert "patch_list" not in result

    def test_noise_disabled_field(self):
        """trace_status() should return noise_disabled field."""
        conn = _make_mock_conn()
        conn._send_command.return_value = ("", [
            "loaded=1",
            "enabled=1",
            "patches=50",
            "noise_disabled=19",
        ])
        status = conn.trace_status()
        assert status["noise_disabled"] == 19

    def test_noise_disabled_invalid(self):
        """noise_disabled with non-integer value defaults to 0."""
        conn = _make_mock_conn()
        conn._send_command.return_value = ("", [
            "loaded=1",
            "noise_disabled=bogus",
        ])
        status = conn.trace_status()
        assert status["noise_disabled"] == 0

    def test_filter_task_field(self):
        """trace_status() should return filter_task field."""
        conn = _make_mock_conn()
        conn._send_command.return_value = ("", [
            "loaded=1",
            "filter_task=0x0e300200",
        ])
        status = conn.trace_status()
        assert status["filter_task"] == "0x0e300200"

    def test_filter_task_null(self):
        """filter_task 0x00000000 is returned as-is (not converted)."""
        conn = _make_mock_conn()
        conn._send_command.return_value = ("", [
            "loaded=1",
            "filter_task=0x00000000",
        ])
        status = conn.trace_status()
        assert status["filter_task"] == "0x00000000"

    def test_filter_task_absent(self):
        """filter_task is absent when not in response (version < 2)."""
        conn = _make_mock_conn()
        conn._send_command.return_value = ("", [
            "loaded=1",
            "enabled=1",
        ])
        status = conn.trace_status()
        assert "filter_task" not in status

    def test_noise_disabled_absent(self):
        """noise_disabled is absent when not in response."""
        conn = _make_mock_conn()
        conn._send_command.return_value = ("", [
            "loaded=1",
            "enabled=1",
        ])
        status = conn.trace_status()
        assert "noise_disabled" not in status


# ---------------------------------------------------------------------------
# TestProcessNameExtraction
# ---------------------------------------------------------------------------

class TestProcessNameExtraction:
    """Tests for command basename extraction logic.

    The actual extraction happens daemon-side in C. These tests
    document the expected behavior as specifications. Actual
    verification is via integration tests (test_trace.py).
    """

    def test_basename_volume_path(self):
        """Work:control -> control"""
        # Tested via integration test (test_trace_run_process_name)

    def test_basename_dir_path(self):
        """SYS:Utilities/MultiView -> MultiView"""
        # Tested via integration test (test_trace_run_process_name)

    def test_basename_simple(self):
        """List -> List"""
        # Tested via integration test (test_trace_run_process_name)

    def test_basename_with_args(self):
        """C:Dir SYS: -> Dir (first word only)"""
        # Tested via integration test (test_trace_run_process_name)

    def test_basename_leading_spaces(self):
        """  List   -> List"""
        # Tested via integration test (test_trace_run_process_name)


# ---------------------------------------------------------------------------
# TestShellDoTrace
# ---------------------------------------------------------------------------

class TestShellDoTrace:
    """Tests for do_trace in client/amigactl/shell.py."""

    def test_trace_status_loaded(self, capsys):
        shell = _make_shell()
        shell.conn.trace_status.return_value = {
            "loaded": True, "enabled": True, "patches": 50,
            "events_produced": 100, "events_consumed": 95,
            "events_dropped": 5, "buffer_capacity": 8192,
            "buffer_used": 10,
            "noise_disabled": 19,
            "filter_task": "0x00000000",
        }
        shell.do_trace("status")
        out = capsys.readouterr().out
        assert "atrace status:" in out
        assert "Enabled:" in out
        assert "yes" in out
        assert "Patches:" in out
        assert "50" in out
        assert "Events produced:" in out
        assert "100" in out
        assert "Noise disabled:" in out
        assert "19" in out
        # filter_task 0x00000000 should be hidden
        assert "Filter task:" not in out

    def test_trace_status_not_loaded(self, capsys):
        shell = _make_shell()
        shell.conn.trace_status.return_value = {"loaded": False}
        shell.do_trace("status")
        out = capsys.readouterr().out
        assert "atrace is not loaded" in out

    def test_trace_status_with_patches(self, capsys):
        shell = _make_shell()
        shell.conn.trace_status.return_value = {
            "loaded": True, "enabled": True, "patches": 2,
            "events_produced": 0, "events_consumed": 0,
            "events_dropped": 0, "buffer_capacity": 8192,
            "buffer_used": 0,
            "noise_disabled": 1,
            "filter_task": "0x00000000",
            "patch_list": [
                {"name": "exec.FindPort", "enabled": True},
                {"name": "exec.GetMsg", "enabled": False},
            ],
        }
        shell.do_trace("status")
        out = capsys.readouterr().out
        assert "Patch details:" in out
        assert "exec.FindPort" in out
        assert "enabled" in out
        assert "exec.GetMsg" in out
        assert "disabled" in out

    def test_trace_status_filter_task_shown(self, capsys):
        """filter_task should display when non-zero."""
        shell = _make_shell()
        shell.conn.trace_status.return_value = {
            "loaded": True, "enabled": True, "patches": 50,
            "events_produced": 50, "events_consumed": 50,
            "events_dropped": 0, "buffer_capacity": 8192,
            "buffer_used": 0,
            "noise_disabled": 0,
            "filter_task": "0x0e300200",
        }
        shell.do_trace("status")
        out = capsys.readouterr().out
        assert "Filter task:" in out
        assert "0x0e300200" in out

    def test_trace_enable_global(self, capsys):
        shell = _make_shell()
        shell.do_trace("enable")
        shell.conn.trace_enable.assert_called_once_with(funcs=None)
        out = capsys.readouterr().out
        assert "atrace tracing enabled" in out

    def test_trace_enable_funcs(self, capsys):
        shell = _make_shell()
        shell.do_trace("enable Open Lock")
        shell.conn.trace_enable.assert_called_once_with(
            funcs=["Open", "Lock"])
        out = capsys.readouterr().out
        assert "Enabled: Open, Lock" in out

    def test_trace_disable_global(self, capsys):
        shell = _make_shell()
        shell.do_trace("disable")
        shell.conn.trace_disable.assert_called_once_with(funcs=None)
        out = capsys.readouterr().out
        assert "atrace tracing disabled" in out

    def test_trace_disable_funcs(self, capsys):
        shell = _make_shell()
        shell.do_trace("disable GetMsg")
        shell.conn.trace_disable.assert_called_once_with(
            funcs=["GetMsg"])
        out = capsys.readouterr().out
        assert "Disabled: GetMsg" in out

    def test_trace_enable_error(self, capsys):
        shell = _make_shell()
        shell.conn.trace_enable.side_effect = CommandSyntaxError(
            "Unknown function: Bogus")
        shell.do_trace("enable Bogus")
        out = capsys.readouterr().out
        assert "Error:" in out
        assert "Unknown function: Bogus" in out

    def test_trace_stop_hint(self, capsys):
        shell = _make_shell()
        shell.do_trace("stop")
        out = capsys.readouterr().out
        assert "only valid during an active trace stream" in out

    def test_trace_no_args(self, capsys):
        shell = _make_shell()
        shell.do_trace("")
        out = capsys.readouterr().out
        assert "Usage:" in out


# ---------------------------------------------------------------------------
# TestTraceRunCommandBuilding
# ---------------------------------------------------------------------------

class TestTraceRunCommandBuilding:
    """Tests verifying the client library builds correct TRACE RUN
    command strings and parses responses."""

    @mock.patch("amigactl.read_line")
    @mock.patch("amigactl.send_command")
    def test_trace_run_builds_command(self, mock_send, mock_readline):
        """trace_run() sends TRACE RUN -- <command> with no filters."""
        conn = _make_mock_conn()
        conn._sock.gettimeout.return_value = 10
        mock_readline.side_effect = ["OK 1", "END", "."]
        callback = mock.MagicMock()

        conn.trace_run("Echo hello", callback)

        mock_send.assert_called_once_with(
            conn._sock, "TRACE RUN -- Echo hello")

    @mock.patch("amigactl.read_line")
    @mock.patch("amigactl.send_command")
    def test_trace_run_with_lib(self, mock_send, mock_readline):
        """LIB= filter is placed before the -- separator."""
        conn = _make_mock_conn()
        conn._sock.gettimeout.return_value = 10
        mock_readline.side_effect = ["OK 2", "END", "."]
        callback = mock.MagicMock()

        conn.trace_run("List SYS:", callback, lib="dos")

        mock_send.assert_called_once_with(
            conn._sock, "TRACE RUN LIB=dos -- List SYS:")

    @mock.patch("amigactl.read_line")
    @mock.patch("amigactl.send_command")
    def test_trace_run_with_func(self, mock_send, mock_readline):
        """FUNC= filter is placed before the -- separator."""
        conn = _make_mock_conn()
        conn._sock.gettimeout.return_value = 10
        mock_readline.side_effect = ["OK 3", "END", "."]
        callback = mock.MagicMock()

        conn.trace_run("test", callback, func="Open")

        mock_send.assert_called_once_with(
            conn._sock, "TRACE RUN FUNC=Open -- test")

    @mock.patch("amigactl.read_line")
    @mock.patch("amigactl.send_command")
    def test_trace_run_with_errors(self, mock_send, mock_readline):
        """ERRORS flag is placed before the -- separator."""
        conn = _make_mock_conn()
        conn._sock.gettimeout.return_value = 10
        mock_readline.side_effect = ["OK 4", "END", "."]
        callback = mock.MagicMock()

        conn.trace_run("test", callback, errors_only=True)

        mock_send.assert_called_once_with(
            conn._sock, "TRACE RUN ERRORS -- test")

    @mock.patch("amigactl.read_line")
    @mock.patch("amigactl.send_command")
    def test_trace_run_cd_option(self, mock_send, mock_readline):
        """CD= option is placed before the -- separator."""
        conn = _make_mock_conn()
        conn._sock.gettimeout.return_value = 10
        mock_readline.side_effect = ["OK 5", "END", "."]
        callback = mock.MagicMock()

        conn.trace_run("myprog", callback, cd="Work:")

        mock_send.assert_called_once_with(
            conn._sock, "TRACE RUN CD=Work: -- myprog")

    @mock.patch("amigactl.read_line")
    @mock.patch("amigactl.send_command")
    def test_trace_run_combined(self, mock_send, mock_readline):
        """All filters are placed before the -- separator."""
        conn = _make_mock_conn()
        conn._sock.gettimeout.return_value = 10
        mock_readline.side_effect = ["OK 6", "END", "."]
        callback = mock.MagicMock()

        conn.trace_run("Work:myapp", callback,
                        lib="dos", func="Open", errors_only=True)

        mock_send.assert_called_once_with(
            conn._sock,
            "TRACE RUN LIB=dos FUNC=Open ERRORS -- Work:myapp")

    @mock.patch("amigactl.read_line")
    @mock.patch("amigactl.send_command")
    def test_trace_run_parses_proc_id(self, mock_send, mock_readline):
        """OK line proc_id is parsed correctly."""
        conn = _make_mock_conn()
        conn._sock.gettimeout.return_value = 10
        mock_readline.side_effect = ["OK 42", "END", "."]
        callback = mock.MagicMock()

        result = conn.trace_run("test", callback)

        assert result["proc_id"] == 42

    @mock.patch("amigactl.read_line")
    @mock.patch("amigactl.recv_exact")
    @mock.patch("amigactl.send_command")
    def test_trace_run_parses_exit_code(self, mock_send,
                                         mock_recv, mock_readline):
        """trace_run extracts rc from PROCESS EXITED comment."""
        conn = _make_mock_conn()
        conn._sock.gettimeout.return_value = 10
        mock_readline.side_effect = [
            "OK 1",
            "DATA 25",
            "END",
            ".",
        ]
        mock_recv.return_value = b"# PROCESS EXITED rc=5"
        callback = mock.MagicMock()

        result = conn.trace_run("test", callback)

        assert result["proc_id"] == 1
        assert result["rc"] == 5
        callback.assert_called_once()
        assert callback.call_args[0][0]["type"] == "comment"
        assert "PROCESS EXITED rc=5" in callback.call_args[0][0]["text"]

    @mock.patch("amigactl.read_line")
    @mock.patch("amigactl.send_command")
    def test_trace_run_handles_error(self, mock_send, mock_readline):
        """trace_run raises on ERR response."""
        conn = _make_mock_conn()
        conn._sock.gettimeout.return_value = 10
        mock_readline.side_effect = [
            "ERR 500 atrace not loaded", "."]
        with pytest.raises(InternalError, match="atrace not loaded"):
            conn.trace_run("test", callback=mock.MagicMock())

    @mock.patch("amigactl.read_line")
    @mock.patch("amigactl.recv_exact")
    @mock.patch("amigactl.send_command")
    def test_trace_run_callback(self, mock_send, mock_recv,
                                 mock_readline):
        """Callback receives event dicts for trace events."""
        conn = _make_mock_conn()
        conn._sock.gettimeout.return_value = 10
        event_line = (
            b"1\t14:30:01.000\tdos.Open\tamigactld-exec"
            b'\t"test.txt",MODE_OLDFILE\t0x03c1a0b8\tO'
        )
        mock_readline.side_effect = [
            "OK 1",
            "DATA {}".format(len(event_line)),
            "DATA 25",
            "END",
            ".",
        ]
        mock_recv.side_effect = [event_line, b"# PROCESS EXITED rc=0"]
        events = []
        callback = mock.MagicMock(side_effect=lambda e: events.append(e))

        result = conn.trace_run("test", callback)

        assert len(events) == 2
        assert events[0]["type"] == "event"
        assert events[0]["func"] == "Open"
        assert events[0]["lib"] == "dos"
        assert events[0]["status"] == "O"
        assert events[1]["type"] == "comment"
        assert "PROCESS EXITED rc=0" in events[1]["text"]
        assert result["rc"] == 0

    @mock.patch("amigactl.read_line")
    @mock.patch("amigactl.send_command")
    def test_trace_run_no_exit_code(self, mock_send, mock_readline):
        """rc is None when no PROCESS EXITED comment is seen (e.g., STOP)."""
        conn = _make_mock_conn()
        conn._sock.gettimeout.return_value = 10
        # Simulate immediate END (as after STOP) with no exit comment
        mock_readline.side_effect = ["OK 1", "END", "."]
        callback = mock.MagicMock()

        result = conn.trace_run("test", callback)

        assert result["proc_id"] == 1
        assert result["rc"] is None


# ---------------------------------------------------------------------------
# TestShellDoTraceRun
# ---------------------------------------------------------------------------

class TestShellDoTraceRun:
    """Tests for trace run in the shell."""

    def test_trace_run_no_separator(self, capsys):
        shell = _make_shell()
        shell.do_trace("run Echo hello")
        out = capsys.readouterr().out
        assert "-- separator" in out or "--" in out

    def test_trace_run_no_command(self, capsys):
        shell = _make_shell()
        shell.do_trace("run --")
        out = capsys.readouterr().out
        assert "Missing command" in out or "command" in out.lower()

    def test_trace_run_basic(self):
        shell = _make_shell()
        shell.conn.trace_run = mock.MagicMock(
            return_value={"proc_id": 1, "rc": 0})
        shell.do_trace("run -- Echo hello")
        shell.conn.trace_run.assert_called_once()
        call_args = shell.conn.trace_run.call_args
        assert call_args[0][0] == "Echo hello"


# ---------------------------------------------------------------------------
# TestTraceStreamReader
# ---------------------------------------------------------------------------

class TestTraceStreamReader:
    """Tests for TraceStreamReader in client/amigactl/protocol.py."""

    def _make_reader(self):
        """Create a TraceStreamReader with a mock socket."""
        sock = mock.MagicMock()
        reader = TraceStreamReader(sock)
        return reader, sock

    def test_reader_complete_event(self):
        """Feed a complete DATA <len> + payload, verify parsed event."""
        reader, sock = self._make_reader()
        payload = b"42\t14:30:01.000\texec.OpenLibrary\tShell\t\"dos\",0\t0x1234\tO"
        frame = "DATA {}\n".format(len(payload)).encode(ENCODING) + payload
        sock.recv.return_value = frame

        event = reader.try_read_event()

        assert event is not None
        assert event is not False
        assert event["type"] == "event"
        assert event["seq"] == 42
        assert event["func"] == "OpenLibrary"
        assert event["status"] == "O"

    def test_reader_partial_line(self):
        """Feed partial header, then rest. Verify None then event."""
        reader, sock = self._make_reader()
        payload = b"1\t12:00:00.000\tdos.Open\ttask\targs\tret\tO"
        full_frame = "DATA {}\n".format(len(payload)).encode(ENCODING) + payload

        # First recv: partial header "DAT"
        sock.recv.return_value = b"DAT"
        result = reader.try_read_event()
        assert result is None

        # Second recv: rest of frame
        sock.recv.return_value = full_frame[3:]
        result = reader.try_read_event()
        assert result is not None
        assert result["type"] == "event"
        assert result["func"] == "Open"

    def test_reader_partial_chunk(self):
        """Feed DATA header + partial payload, then rest."""
        reader, sock = self._make_reader()
        payload = b"1\t12:00:00.000\tdos.Open\ttask\targs\tret\tO"
        header = "DATA {}\n".format(len(payload)).encode(ENCODING)

        # First recv: header + first 10 bytes of payload
        sock.recv.return_value = header + payload[:10]
        result = reader.try_read_event()
        assert result is None

        # Second recv: remaining payload
        sock.recv.return_value = payload[10:]
        result = reader.try_read_event()
        assert result is not None
        assert result["type"] == "event"
        assert result["func"] == "Open"

    def test_reader_end_sentinel(self):
        """Feed END + sentinel, verify False returned."""
        reader, sock = self._make_reader()
        sock.recv.return_value = b"END\n.\n"

        result = reader.try_read_event()
        assert result is False

    def test_reader_end_split_sentinel(self):
        """Feed END alone, verify None. Then feed sentinel, verify False."""
        reader, sock = self._make_reader()

        # First recv: just END line
        sock.recv.return_value = b"END\n"
        result = reader.try_read_event()
        assert result is None

        # Second recv: sentinel
        sock.recv.return_value = b".\n"
        result = reader.try_read_event()
        assert result is False

    def test_reader_multiple_events(self):
        """Feed two complete events in one recv, verify both returned."""
        reader, sock = self._make_reader()
        payload1 = b"1\t12:00\tdos.Open\ttask\targs\tret\tO"
        payload2 = b"2\t12:01\tdos.Close\ttask\targs\tret\tO"
        frame1 = "DATA {}\n".format(len(payload1)).encode(ENCODING) + payload1
        frame2 = "DATA {}\n".format(len(payload2)).encode(ENCODING) + payload2
        sock.recv.return_value = frame1 + frame2

        event1 = reader.try_read_event()
        assert event1 is not None
        assert event1["func"] == "Open"

        assert reader.has_buffered_data()
        event2 = reader.drain_buffered()
        assert event2 is not None
        assert event2["func"] == "Close"

    def test_reader_comment(self):
        """Feed DATA with # prefix, verify comment dict."""
        reader, sock = self._make_reader()
        payload = b"# OVERFLOW 5 events dropped"
        frame = "DATA {}\n".format(len(payload)).encode(ENCODING) + payload
        sock.recv.return_value = frame

        result = reader.try_read_event()
        assert result is not None
        assert result["type"] == "comment"
        assert result["text"] == "OVERFLOW 5 events dropped"

    def test_reader_blocking_io_error(self):
        """BlockingIOError from recv returns None."""
        reader, sock = self._make_reader()
        sock.recv.side_effect = BlockingIOError()

        result = reader.try_read_event()
        assert result is None

    def test_reader_connection_closed(self):
        """Empty recv raises ProtocolError."""
        reader, sock = self._make_reader()
        sock.recv.return_value = b""

        with pytest.raises(ProtocolError, match="Connection closed"):
            reader.try_read_event()

    def test_reader_err_line(self):
        """ERR line + sentinel returns False."""
        reader, sock = self._make_reader()
        sock.recv.return_value = b"ERR 500 internal error\n.\n"

        result = reader.try_read_event()
        assert result is False

    def test_reader_err_split_sentinel(self):
        """ERR without sentinel, then sentinel arrives."""
        reader, sock = self._make_reader()

        sock.recv.return_value = b"ERR 500 error\n"
        result = reader.try_read_event()
        assert result is None

        sock.recv.return_value = b".\n"
        result = reader.try_read_event()
        assert result is False

    def test_reader_has_buffered_data_empty(self):
        """has_buffered_data() returns False on fresh reader."""
        reader, sock = self._make_reader()
        assert reader.has_buffered_data() is False

    def test_reader_invalid_data_length(self):
        """Invalid DATA length raises ProtocolError."""
        reader, sock = self._make_reader()
        sock.recv.return_value = b"DATA abc\n"

        with pytest.raises(ProtocolError, match="Invalid DATA length"):
            reader.try_read_event()

    def test_reader_unexpected_line(self):
        """Unexpected line raises ProtocolError."""
        reader, sock = self._make_reader()
        sock.recv.return_value = b"BOGUS\n"

        with pytest.raises(ProtocolError, match="Unexpected line"):
            reader.try_read_event()


# ---------------------------------------------------------------------------
# TestTraceStartRaw
# ---------------------------------------------------------------------------

class TestTraceStartRaw:
    """Tests for trace_start_raw() on AmigaConnection."""

    @mock.patch("amigactl.read_line")
    @mock.patch("amigactl.send_command")
    def test_trace_start_raw_sends_command(self, mock_send, mock_readline):
        """Verify correct command string is sent and OK is consumed."""
        conn = _make_mock_conn()
        conn._sock.gettimeout.return_value = 10
        mock_readline.return_value = "OK"

        session = conn.trace_start_raw(lib="dos", func="Open")

        mock_send.assert_called_once_with(
            conn._sock, "TRACE START LIB=dos FUNC=Open")
        assert isinstance(session, RawTraceSession)
        assert session.sock is conn._sock

    @mock.patch("amigactl.read_line")
    @mock.patch("amigactl.send_command")
    def test_trace_start_raw_raises_on_error(self, mock_send, mock_readline):
        """ERR response raises exception."""
        conn = _make_mock_conn()
        conn._sock.gettimeout.return_value = 10
        mock_readline.side_effect = ["ERR 500 atrace not loaded", "."]

        with pytest.raises(InternalError, match="atrace not loaded"):
            conn.trace_start_raw()

    @mock.patch("amigactl.read_line")
    @mock.patch("amigactl.send_command")
    def test_trace_start_raw_restores_timeout(self, mock_send, mock_readline):
        """Context manager restores original timeout on exit."""
        conn = _make_mock_conn()
        conn._sock.gettimeout.return_value = 10
        mock_readline.return_value = "OK"

        with conn.trace_start_raw() as session:
            # Socket should be non-blocking inside
            conn._sock.setblocking.assert_called_with(False)

        # After exiting, timeout should be restored
        conn._sock.settimeout.assert_called_with(10)

    @mock.patch("amigactl.read_line")
    @mock.patch("amigactl.send_command")
    def test_trace_start_raw_no_filters(self, mock_send, mock_readline):
        """Bare TRACE START with no filters."""
        conn = _make_mock_conn()
        conn._sock.gettimeout.return_value = 10
        mock_readline.return_value = "OK"

        conn.trace_start_raw()

        mock_send.assert_called_once_with(conn._sock, "TRACE START")

    @mock.patch("amigactl.read_line")
    @mock.patch("amigactl.send_command")
    def test_trace_start_raw_all_filters(self, mock_send, mock_readline):
        """TRACE START with all filters."""
        conn = _make_mock_conn()
        conn._sock.gettimeout.return_value = 10
        mock_readline.return_value = "OK"

        conn.trace_start_raw(lib="dos", func="Open", proc="myapp",
                             errors_only=True)

        mock_send.assert_called_once_with(
            conn._sock,
            "TRACE START LIB=dos FUNC=Open PROC=myapp ERRORS")

    @mock.patch("amigactl.read_line")
    @mock.patch("amigactl.send_command")
    def test_trace_start_raw_not_connected(self, mock_send, mock_readline):
        """Raises ProtocolError when not connected."""
        conn = _make_mock_conn()
        conn._sock = None

        with pytest.raises(ProtocolError, match="Not connected"):
            conn.trace_start_raw()


# ---------------------------------------------------------------------------
# TestTraceRunRaw
# ---------------------------------------------------------------------------

class TestTraceRunRaw:
    """Tests for trace_run_raw() on AmigaConnection."""

    @mock.patch("amigactl.read_line")
    @mock.patch("amigactl.send_command")
    def test_trace_run_raw_sends_command(self, mock_send, mock_readline):
        """Verify correct command string is sent."""
        conn = _make_mock_conn()
        conn._sock.gettimeout.return_value = 10
        mock_readline.return_value = "OK 42"

        session, proc_id = conn.trace_run_raw("Echo hello", lib="dos")

        mock_send.assert_called_once_with(
            conn._sock, "TRACE RUN LIB=dos -- Echo hello")
        assert proc_id == 42
        assert isinstance(session, RawTraceSession)

    @mock.patch("amigactl.read_line")
    @mock.patch("amigactl.send_command")
    def test_trace_run_raw_raises_on_error(self, mock_send, mock_readline):
        """ERR response raises exception."""
        conn = _make_mock_conn()
        conn._sock.gettimeout.return_value = 10
        mock_readline.side_effect = ["ERR 500 atrace not loaded", "."]

        with pytest.raises(InternalError, match="atrace not loaded"):
            conn.trace_run_raw("test")

    @mock.patch("amigactl.read_line")
    @mock.patch("amigactl.send_command")
    def test_trace_run_raw_all_options(self, mock_send, mock_readline):
        """All options are placed before the -- separator."""
        conn = _make_mock_conn()
        conn._sock.gettimeout.return_value = 10
        mock_readline.return_value = "OK 1"

        conn.trace_run_raw("test", lib="dos", func="Open",
                           errors_only=True, cd="Work:")

        mock_send.assert_called_once_with(
            conn._sock,
            "TRACE RUN LIB=dos FUNC=Open ERRORS CD=Work: -- test")

    @mock.patch("amigactl.read_line")
    @mock.patch("amigactl.send_command")
    def test_trace_run_raw_no_proc_id(self, mock_send, mock_readline):
        """OK with no proc_id returns None."""
        conn = _make_mock_conn()
        conn._sock.gettimeout.return_value = 10
        mock_readline.return_value = "OK"

        session, proc_id = conn.trace_run_raw("test")
        assert proc_id is None


# ---------------------------------------------------------------------------
# TestSendFilter
# ---------------------------------------------------------------------------

class TestSendFilter:
    """Tests for send_filter() on AmigaConnection."""

    @mock.patch("amigactl.send_command")
    def test_send_filter_builds_correct_command(self, mock_send):
        """Verify FILTER command with lib and func."""
        conn = _make_mock_conn()
        conn._sock.getblocking.return_value = True

        conn.send_filter(lib="dos", func="Open")

        mock_send.assert_called_once_with(
            conn._sock, "FILTER LIB=dos FUNC=Open")

    @mock.patch("amigactl.send_command")
    def test_send_filter_raw_string(self, mock_send):
        """Verify raw filter string is passed through."""
        conn = _make_mock_conn()
        conn._sock.getblocking.return_value = True

        conn.send_filter(raw="LIB=dos,exec")

        mock_send.assert_called_once_with(
            conn._sock, "FILTER LIB=dos,exec")

    @mock.patch("amigactl.send_command")
    def test_send_filter_no_args_clears(self, mock_send):
        """Bare FILTER (no args) clears all filters."""
        conn = _make_mock_conn()
        conn._sock.getblocking.return_value = True

        conn.send_filter()

        mock_send.assert_called_once_with(conn._sock, "FILTER")

    @mock.patch("amigactl.send_command")
    def test_send_filter_proc(self, mock_send):
        """Verify FILTER with proc."""
        conn = _make_mock_conn()
        conn._sock.getblocking.return_value = True

        conn.send_filter(proc="myapp")

        mock_send.assert_called_once_with(
            conn._sock, "FILTER PROC=myapp")

    @mock.patch("amigactl.send_command")
    def test_send_filter_nonblocking_socket(self, mock_send):
        """Non-blocking socket gets temporary timeout for send."""
        conn = _make_mock_conn()
        conn._sock.getblocking.return_value = False

        conn.send_filter(lib="dos")

        # Should have set timeout, then restored non-blocking
        conn._sock.settimeout.assert_called_with(2.0)
        conn._sock.setblocking.assert_called_with(False)

    @mock.patch("amigactl.send_command")
    def test_send_filter_not_connected(self, mock_send):
        """Raises ProtocolError when not connected."""
        conn = _make_mock_conn()
        conn._sock = None

        with pytest.raises(ProtocolError, match="Not connected"):
            conn.send_filter(lib="dos")

    @mock.patch("amigactl.send_command")
    def test_send_filter_raw_empty_string(self, mock_send):
        """raw="" sends bare FILTER (clears filters)."""
        conn = _make_mock_conn()
        conn._sock.getblocking.return_value = True

        conn.send_filter(raw="")

        mock_send.assert_called_once_with(conn._sock, "FILTER")

    @mock.patch("amigactl.send_command")
    def test_send_filter_send_failure_silent(self, mock_send):
        """OSError during send is silently swallowed."""
        conn = _make_mock_conn()
        conn._sock.getblocking.return_value = True
        mock_send.side_effect = OSError("broken pipe")

        # Should not raise
        conn.send_filter(lib="dos")

    # --- Library-scoped FUNC= filtering ---

    @mock.patch("amigactl.send_command")
    def test_send_filter_dotted_func(self, mock_send):
        """Verify FILTER command with dotted lib.func syntax (8b.1)."""
        conn = _make_mock_conn()
        conn._sock.getblocking.return_value = True

        conn.send_filter(func="dos.Open")

        mock_send.assert_called_once_with(
            conn._sock, "FILTER FUNC=dos.Open")

    @mock.patch("amigactl.send_command")
    def test_send_filter_raw_dotted_func(self, mock_send):
        """Verify raw filter with dotted lib.func names (8b.1)."""
        conn = _make_mock_conn()
        conn._sock.getblocking.return_value = True

        conn.send_filter(raw="-FUNC=exec.AllocMem,dos.Read")

        mock_send.assert_called_once_with(
            conn._sock, "FILTER -FUNC=exec.AllocMem,dos.Read")

    @mock.patch("amigactl.send_command")
    def test_send_filter_dotted_func_with_lib(self, mock_send):
        """Dotted func and lib filter combined (8b.1)."""
        conn = _make_mock_conn()
        conn._sock.getblocking.return_value = True

        conn.send_filter(lib="dos", func="dos.Open")

        mock_send.assert_called_once_with(
            conn._sock, "FILTER LIB=dos FUNC=dos.Open")


# ---------------------------------------------------------------------------
# TestReadOneTraceEvent
# ---------------------------------------------------------------------------

class TestReadOneTraceEvent:
    """Tests for read_one_trace_event() module-level function."""

    @mock.patch("amigactl.recv_exact")
    @mock.patch("amigactl.read_line")
    def test_data_event(self, mock_readline, mock_recv):
        """DATA chunk produces event dict."""
        payload = b"1\t12:00:00.000\tdos.Open\ttask\targs\tret\tO"
        mock_readline.return_value = "DATA {}".format(len(payload))
        mock_recv.return_value = payload
        sock = mock.MagicMock()

        event = read_one_trace_event(sock)

        assert event["type"] == "event"
        assert event["func"] == "Open"

    @mock.patch("amigactl.recv_exact")
    @mock.patch("amigactl.read_line")
    def test_comment_event(self, mock_readline, mock_recv):
        """DATA chunk with # prefix produces comment dict."""
        payload = b"# OVERFLOW 5 events dropped"
        mock_readline.return_value = "DATA {}".format(len(payload))
        mock_recv.return_value = payload
        sock = mock.MagicMock()

        event = read_one_trace_event(sock)

        assert event["type"] == "comment"
        assert event["text"] == "OVERFLOW 5 events dropped"

    @mock.patch("amigactl.read_line")
    def test_end_returns_none(self, mock_readline):
        """END + sentinel returns None."""
        mock_readline.side_effect = ["END", "."]
        sock = mock.MagicMock()

        result = read_one_trace_event(sock)
        assert result is None

    @mock.patch("amigactl.read_line")
    def test_err_returns_none(self, mock_readline):
        """ERR + sentinel returns None."""
        mock_readline.side_effect = ["ERR 500 internal error", "."]
        sock = mock.MagicMock()

        result = read_one_trace_event(sock)
        assert result is None

    @mock.patch("amigactl.read_line")
    def test_unexpected_line_raises(self, mock_readline):
        """Unexpected line raises ProtocolError."""
        mock_readline.return_value = "BOGUS"
        sock = mock.MagicMock()

        with pytest.raises(ProtocolError, match="Unexpected line"):
            read_one_trace_event(sock)

    @mock.patch("amigactl.read_line")
    def test_end_bad_sentinel_raises(self, mock_readline):
        """END with wrong sentinel raises ProtocolError."""
        mock_readline.side_effect = ["END", "WRONG"]
        sock = mock.MagicMock()

        with pytest.raises(ProtocolError, match="Expected sentinel"):
            read_one_trace_event(sock)


# ---------------------------------------------------------------------------
# TestExtendedEventFormats -- extended event format parsing
# ---------------------------------------------------------------------------

class TestExtendedEventFormats:
    """Tests for parsing extended event formats.

    These verify that the Python client correctly parses events produced
    by the daemon for extended functions: device I/O, memory, intuition,
    and dos Read/Write.  The daemon does all formatting; the client just
    parses tab-delimited fields.
    """

    def test_intuition_lib_event(self):
        """Events with lib='intuition' parse correctly."""
        text = '100\t14:30:01.000\tintuition.OpenWindow\tShell Process\tnw=0x00234560\t0x00345678\tO'
        event = _parse_trace_event(text)
        assert event["lib"] == "intuition"
        assert event["func"] == "OpenWindow"
        assert event["args"] == "nw=0x00234560"
        assert event["retval"] == "0x00345678"
        assert event["status"] == "O"

    def test_intuition_void_function(self):
        """Intuition void functions (CloseWindow, etc.) parse correctly."""
        text = '101\t14:30:02.000\tintuition.CloseWindow\tShell Process\twin=0x00345678\t(void)\t-'
        event = _parse_trace_event(text)
        assert event["lib"] == "intuition"
        assert event["func"] == "CloseWindow"
        assert event["retval"] == "(void)"
        assert event["status"] == "-"

    def test_intuition_idcmp_flags_in_args(self):
        """ModifyIDCMP event with IDCMP flag names in args parses correctly."""
        text = '102\t14:30:03.000\tintuition.ModifyIDCMP\tShell Process\twin=0x00345678,CLOSEWINDOW|REFRESHWINDOW\t(void)\t-'
        event = _parse_trace_event(text)
        assert event["func"] == "ModifyIDCMP"
        assert "CLOSEWINDOW" in event["args"]
        assert "REFRESHWINDOW" in event["args"]
        assert event["status"] == "-"

    def test_device_io_ok(self):
        """DoIO event with status O and retval OK."""
        text = '103\t14:30:04.000\texec.DoIO\tShell Process\tio=0x00456789\tOK\tO'
        event = _parse_trace_event(text)
        assert event["func"] == "DoIO"
        assert event["retval"] == "OK"
        assert event["status"] == "O"
        assert "io=0x" in event["args"]

    def test_device_io_void(self):
        """SendIO event with void return."""
        text = '104\t14:30:05.000\texec.SendIO\tShell Process\tio=0x00456789\t(void)\t-'
        event = _parse_trace_event(text)
        assert event["func"] == "SendIO"
        assert event["retval"] == "(void)"
        assert event["status"] == "-"

    def test_freemem_void(self):
        """FreeMem event with void return and size in args."""
        text = '105\t14:30:06.000\texec.FreeMem\tShell Process\t0x00567890,2345\t(void)\t-'
        event = _parse_trace_event(text)
        assert event["func"] == "FreeMem"
        assert "2345" in event["args"]
        assert event["retval"] == "(void)"
        assert event["status"] == "-"

    def test_allocvec_success(self):
        """AllocVec event with successful allocation."""
        text = '106\t14:30:07.000\texec.AllocVec\tShell Process\t3456,MEMF_PUBLIC|MEMF_CLEAR\t0x00678901\tO'
        event = _parse_trace_event(text)
        assert event["func"] == "AllocVec"
        assert "MEMF_PUBLIC" in event["args"]
        assert "MEMF_CLEAR" in event["args"]
        assert event["status"] == "O"

    def test_freevec_void(self):
        """FreeVec event with void return."""
        text = '107\t14:30:08.000\texec.FreeVec\tShell Process\t0x00678901\t(void)\t-'
        event = _parse_trace_event(text)
        assert event["func"] == "FreeVec"
        assert event["retval"] == "(void)"
        assert event["status"] == "-"

    def test_ret_io_len_success(self):
        """Read/Write event with RET_IO_LEN: positive byte count, status O."""
        text = '108\t14:30:09.000\tdos.Write\tShell Process\tfh=0x00789012,buf=0x00890123,len=42\t42\tO'
        event = _parse_trace_event(text)
        assert event["func"] == "Write"
        assert event["retval"] == "42"
        assert event["status"] == "O"
        assert "len=42" in event["args"]

    def test_ret_io_len_zero(self):
        """Read event with RET_IO_LEN: 0 bytes (EOF), status O."""
        text = '109\t14:30:10.000\tdos.Read\tShell Process\tfh=0x00789012,buf=0x00890123,len=42\t0\tO'
        event = _parse_trace_event(text)
        assert event["func"] == "Read"
        assert event["retval"] == "0"
        assert event["status"] == "O"

    def test_ret_io_len_error(self):
        """Read event with RET_IO_LEN: -1 (error), status E."""
        text = '110\t14:30:11.000\tdos.Read\tShell Process\tfh=0x00789012,buf=0x00890123,len=42\t-1\tE'
        event = _parse_trace_event(text)
        assert event["func"] == "Read"
        assert event["retval"] == "-1"
        assert event["status"] == "E"

    def test_err_check_neg1_error(self):
        """ERR_CHECK_NEG1: retval -1 produces status E (daemon-side)."""
        # Simulate what the daemon would emit for Read() returning -1
        text = '111\t14:30:12.000\tdos.Write\tShell Process\tfh=0x00789012,buf=0x00890123,len=100\t-1\tE'
        event = _parse_trace_event(text)
        assert event["retval"] == "-1"
        assert event["status"] == "E"

    def test_err_check_neg1_success(self):
        """ERR_CHECK_NEG1: retval 0 produces status O (daemon-side)."""
        text = '112\t14:30:13.000\tdos.Read\tShell Process\tfh=0x00789012,buf=0x00890123,len=100\t0\tO'
        event = _parse_trace_event(text)
        assert event["retval"] == "0"
        assert event["status"] == "O"


class TestExtendedFormatTraceEvent:
    """Tests for format_trace_event() with extended event types."""

    def _event(self, **overrides):
        """Build a default event dict with optional overrides."""
        base = {
            "seq": 42, "time": "14:30:01.000", "lib": "exec",
            "func": "OpenLibrary", "task": "Shell Process",
            "args": '"dos.library",0', "retval": "0x07a3b2c0",
            "status": "O", "type": "event",
        }
        base.update(overrides)
        return base

    def test_intuition_event_format(self):
        """Intuition library events format correctly."""
        cw = ColorWriter(force_color=True)
        event = self._event(
            lib="intuition", func="OpenWindow",
            args="nw=0x00234560", retval="0x00345678", status="O")
        result = format_trace_event(event, cw)
        assert "intuition" in result
        assert "OpenWindow" in result
        assert "nw=0x00234560" in result

    def test_device_io_event_format(self):
        """Device I/O events format correctly."""
        cw = ColorWriter(force_color=True)
        event = self._event(
            func="DoIO", args="io=0x00456789",
            retval="OK", status="O")
        result = format_trace_event(event, cw)
        assert "DoIO" in result
        assert "io=0x00456789" in result
        assert "OK" in result

    def test_io_len_error_has_red(self):
        """RET_IO_LEN error (-1) is formatted in red."""
        cw = ColorWriter(force_color=True)
        event = self._event(
            lib="dos", func="Read",
            args="fh=0x00789012,buf=0x00890123,len=42",
            retval="-1", status="E")
        result = format_trace_event(event, cw)
        assert "\033[31m" in result
        assert "-1" in result

    def test_io_len_success_has_green(self):
        """RET_IO_LEN success (42) is formatted in green."""
        cw = ColorWriter(force_color=True)
        event = self._event(
            lib="dos", func="Write",
            args="fh=0x00789012,buf=0x00890123,len=42",
            retval="42", status="O")
        result = format_trace_event(event, cw)
        assert "\033[32m" in result
        assert "42" in result

    def test_idcmp_flags_in_formatted_output(self):
        """IDCMP flag names appear in formatted output."""
        cw = ColorWriter(force_color=False)
        event = self._event(
            lib="intuition", func="ModifyIDCMP",
            args="win=0x00345678,CLOSEWINDOW|REFRESHWINDOW",
            retval="(void)", status="-")
        result = format_trace_event(event, cw)
        assert "CLOSEWINDOW" in result
        assert "REFRESHWINDOW" in result
        assert "ModifyIDCMP" in result

    def test_memory_void_format(self):
        """Memory void functions format correctly."""
        cw = ColorWriter(force_color=False)
        event = self._event(
            func="FreeMem", args="0x00567890,2345",
            retval="(void)", status="-")
        result = format_trace_event(event, cw)
        assert "FreeMem" in result
        assert "2345" in result
        assert "(void)" in result


# ---------------------------------------------------------------------------
# TestHeaderComments -- header comment parsing
# ---------------------------------------------------------------------------

class TestHeaderComments:
    """Unit tests for header comment parsing.

    Verifies that TraceStreamReader correctly parses #-prefixed DATA
    chunks as type="comment" events with the expected text content.
    These are pure unit tests -- no daemon connection needed.
    """

    def _feed_chunk(self, text):
        """Feed a text string as a DATA chunk to a TraceStreamReader.

        Simulates a daemon sending a single DATA chunk containing the
        given text.  Returns the parsed event dict.
        """
        data = text.encode(ENCODING)
        header = "DATA {}\n".format(len(data)).encode(ENCODING)
        reader = TraceStreamReader(mock.MagicMock())
        reader._buf = bytearray(header + data)
        result = reader.drain_buffered()
        return result

    def test_header_comment_version(self):
        """Version/timestamp header line is parsed as type='comment'."""
        result = self._feed_chunk("# atrace v2, 2026-03-06 19:33:38")
        assert result is not None
        assert result["type"] == "comment"
        assert result["text"] == "atrace v2, 2026-03-06 19:33:38"

    def test_header_comment_filter_none(self):
        """Filter header with tier-only is parsed as type='comment'."""
        result = self._feed_chunk("# filter: tier=basic")
        assert result is not None
        assert result["type"] == "comment"
        assert result["text"] == "filter: tier=basic"

    def test_header_comment_with_filter(self):
        """Filter header with tier and PROC= filter is parsed correctly."""
        result = self._feed_chunk("# filter: tier=basic, PROC=DirectoryOpus")
        assert result is not None
        assert result["type"] == "comment"
        assert result["text"] == "filter: tier=basic, PROC=DirectoryOpus"

    def test_header_comment_enabled_deviation(self):
        """Enabled deviation header is parsed correctly."""
        result = self._feed_chunk(
            "# enabled: GetMsg (normally noise-disabled)")
        assert result is not None
        assert result["type"] == "comment"
        assert "enabled: GetMsg" in result["text"]

    def test_header_comment_disabled_deviation(self):
        """Disabled deviation header is parsed correctly."""
        result = self._feed_chunk(
            "# disabled: Lock (manually disabled)")
        assert result is not None
        assert result["type"] == "comment"
        assert "disabled: Lock" in result["text"]

    def test_header_comment_command(self):
        """Command header from TRACE RUN is parsed correctly."""
        result = self._feed_chunk("# command: C:atrace_test")
        assert result is not None
        assert result["type"] == "comment"
        assert result["text"] == "command: C:atrace_test"

    def test_header_comment_empty_hash(self):
        """Bare '#' line produces empty text."""
        result = self._feed_chunk("#")
        assert result is not None
        assert result["type"] == "comment"
        assert result["text"] == ""

    def test_header_comment_hash_space(self):
        """'# ' (hash + space, no content) produces empty text."""
        result = self._feed_chunk("# ")
        assert result is not None
        assert result["type"] == "comment"
        assert result["text"] == ""


# ---------------------------------------------------------------------------
# TestTraceRunStats -- stats accumulation and preset tests
# ---------------------------------------------------------------------------

class TestTraceRunStats:
    """Tests for trace_run stats accumulation and filter presets."""

    def test_filter_preset_file_io(self):
        """file-io preset expands to correct filter parameters."""
        from amigactl import FILTER_PRESETS
        p = FILTER_PRESETS["file-io"]
        assert p["lib"] == "dos"
        assert "Open" in p["func_list"]
        assert "Close" in p["func_list"]
        assert "SetProtection" in p["func_list"]

    def test_filter_preset_lib_load(self):
        """lib-load preset includes library management functions."""
        from amigactl import FILTER_PRESETS
        p = FILTER_PRESETS["lib-load"]
        assert "OpenLibrary" in p["func_list"]
        assert "CloseLibrary" in p["func_list"]

    def test_filter_preset_network(self):
        """network preset filters by bsdsocket library."""
        from amigactl import FILTER_PRESETS
        p = FILTER_PRESETS["network"]
        assert p["lib"] == "bsdsocket"

    def test_filter_preset_ipc(self):
        """ipc preset includes message port functions."""
        from amigactl import FILTER_PRESETS
        p = FILTER_PRESETS["ipc"]
        assert "FindPort" in p["func_list"]
        assert "GetMsg" in p["func_list"]
        assert "PutMsg" in p["func_list"]
        assert "AddPort" in p["func_list"]

    def test_filter_preset_errors_only(self):
        """errors-only preset sets errors_only flag."""
        from amigactl import FILTER_PRESETS
        p = FILTER_PRESETS["errors-only"]
        assert p["errors_only"] is True

    def test_filter_preset_memory(self):
        """memory preset includes memory allocation functions."""
        from amigactl import FILTER_PRESETS
        p = FILTER_PRESETS["memory"]
        assert "AllocMem" in p["func_list"]
        assert "FreeMem" in p["func_list"]
        assert "AllocVec" in p["func_list"]
        assert "FreeVec" in p["func_list"]

    def test_filter_preset_window(self):
        """window preset includes window and screen functions."""
        from amigactl import FILTER_PRESETS
        p = FILTER_PRESETS["window"]
        assert "OpenWindow" in p["func_list"]
        assert "CloseWindow" in p["func_list"]
        assert "OpenWindowTagList" in p["func_list"]
        assert "OpenScreenTagList" in p["func_list"]

    def test_filter_preset_icon(self):
        """icon preset filters by icon library."""
        from amigactl import FILTER_PRESETS
        p = FILTER_PRESETS["icon"]
        assert p["lib"] == "icon"

    def test_all_presets_have_valid_keys(self):
        """All presets contain only recognized keys."""
        from amigactl import FILTER_PRESETS
        valid_keys = {"lib", "func_list", "errors_only"}
        for name, preset in FILTER_PRESETS.items():
            for key in preset:
                assert key in valid_keys, (
                    "Preset {!r} has unknown key {!r}".format(name, key))

    def test_unknown_preset_raises_in_trace_run(self):
        """Unknown preset name raises ValueError in trace_run."""
        conn = _make_mock_conn()
        conn._sock = mock.MagicMock()
        with pytest.raises(ValueError, match="Unknown preset"):
            conn.trace_run("C:test", lambda ev: None, preset="nonexistent")

    def test_unknown_preset_raises_in_trace_start(self):
        """Unknown preset name raises ValueError in trace_start."""
        conn = _make_mock_conn()
        conn._sock = mock.MagicMock()
        with pytest.raises(ValueError, match="Unknown preset"):
            conn.trace_start(lambda ev: None, preset="nonexistent")

    def test_trace_run_stats_structure(self):
        """trace_run returns result dict with stats sub-dict."""
        # This test verifies the return structure using a mock that
        # simulates the server side: OK, DATA events, END, sentinel.
        conn = _make_mock_conn()
        sock = conn._sock

        # Simulate server responses: OK line, then 2 DATA events, END, sentinel
        event_text_1 = '1\t12:00:00.001\tdos.Open\t[5] test\t"RAM:file",Read\t0x01234abc\tO'
        event_text_2 = '2\t12:00:00.002\tdos.Open\t[5] test\t"RAM:missing",Read\tNULL\tE'
        exit_text = "# PROCESS EXITED rc=0"

        lines = [
            "OK 42",
            "DATA {}".format(len(event_text_1.encode("iso-8859-1"))),
            event_text_1,
            "DATA {}".format(len(event_text_2.encode("iso-8859-1"))),
            event_text_2,
            "DATA {}".format(len(exit_text.encode("iso-8859-1"))),
            exit_text,
            "END",
            ".",
        ]

        # Build the byte buffer the mock socket will return
        buf = bytearray()
        for i, line in enumerate(lines):
            if i in (2, 4, 6):
                # These are raw DATA chunk payloads (no newline)
                buf.extend(line.encode("iso-8859-1"))
            else:
                buf.extend((line + "\n").encode("iso-8859-1"))

        # Create a position tracker for recv
        pos = [0]
        raw_buf = bytes(buf)

        def mock_recv(n):
            if pos[0] >= len(raw_buf):
                return b""
            chunk = raw_buf[pos[0]:pos[0] + n]
            pos[0] += len(chunk)
            return chunk

        sock.recv = mock.MagicMock(side_effect=mock_recv)
        sock.sendall = mock.MagicMock()
        sock.gettimeout = mock.MagicMock(return_value=10)
        sock.settimeout = mock.MagicMock()

        events_received = []
        result = conn.trace_run("C:test", lambda ev: events_received.append(ev))

        assert result["proc_id"] == 42
        assert result["rc"] == 0
        assert "stats" in result
        stats = result["stats"]
        assert stats["total_events"] == 2
        assert stats["by_function"]["Open"] == 2
        assert stats["errors"] == 1
        assert stats["error_functions"]["Open"] == 1
