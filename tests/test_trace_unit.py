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
    AmigaConnection, CommandSyntaxError, InternalError, _parse_trace_event,
)
from amigactl.colors import ColorWriter, format_trace_event
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
    conn._banner = "AMIGACTL 0.7.0"
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
        text = '42\t14:30:01.000\texec.OpenLibrary\tShell Process\t"dos.library",0\t0x07a3b2c0'
        event = _parse_trace_event(text)
        assert event["seq"] == 42
        assert event["time"] == "14:30:01.000"
        assert event["lib"] == "exec"
        assert event["func"] == "OpenLibrary"
        assert event["task"] == "Shell Process"
        assert event["args"] == '"dos.library",0'
        assert event["retval"] == "0x07a3b2c0"
        assert event["type"] == "event"

    def test_minimal_event(self):
        event = _parse_trace_event("1\t12:00:00.000")
        assert event["seq"] == 1
        assert event["time"] == "12:00:00.000"
        assert event["lib"] == ""
        assert event["func"] == ""
        assert event["task"] == ""
        assert event["args"] == ""
        assert event["retval"] == ""

    def test_empty_string(self):
        event = _parse_trace_event("")
        assert event["seq"] == 0
        assert event["time"] == ""
        assert event["lib"] == ""
        assert event["func"] == ""
        assert event["task"] == ""
        assert event["args"] == ""
        assert event["retval"] == ""

    def test_lib_func_split(self):
        event = _parse_trace_event("1\t00:00\tdos.Open\ttask\targs\tret")
        assert event["lib"] == "dos"
        assert event["func"] == "Open"

    def test_no_dot_in_func(self):
        event = _parse_trace_event("1\t00:00\tSomeName\ttask\targs\tret")
        assert event["lib"] == ""
        assert event["func"] == "SomeName"

    def test_invalid_seq(self):
        event = _parse_trace_event("abc\t00:00\texec.Open\ttask\targs\tret")
        assert event["seq"] == 0

    def test_comment_not_parsed(self):
        # Comment detection happens in trace_start() before calling the
        # parser.  The parser never sees comment lines.  This verifies
        # that if a #-prefixed line were somehow parsed, it would produce
        # seq=0 (the "#" is not numeric).
        event = _parse_trace_event("# OVERFLOW 5 events dropped")
        assert event["seq"] == 0


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
            "type": "event",
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

    def test_error_retval_null(self):
        cw = ColorWriter(force_color=True)
        event = self._event(retval="NULL")
        result = format_trace_event(event, cw)
        assert "\033[31m" in result
        assert "NULL" in result

    def test_error_retval_minus1(self):
        cw = ColorWriter(force_color=True)
        event = self._event(retval="-1")
        result = format_trace_event(event, cw)
        assert "\033[31m" in result
        assert "-1" in result

    def test_error_retval_zero(self):
        cw = ColorWriter(force_color=True)
        event = self._event(retval="0")
        result = format_trace_event(event, cw)
        assert "\033[31m" in result
        assert "0" in result

    def test_success_retval(self):
        cw = ColorWriter(force_color=True)
        event = self._event(retval="0x07a3b2c0")
        result = format_trace_event(event, cw)
        # RED should not wrap a successful return value
        assert "\033[31m" not in result

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
            "loaded=1", "enabled=1", "patches=30",
            "events_produced=1000", "events_consumed=950",
            "events_dropped=50", "buffer_capacity=8192",
            "buffer_used=100",
        ])
        result = conn.trace_status()
        assert result["loaded"] is True
        assert result["enabled"] is True
        assert result["patches"] == 30
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


# ---------------------------------------------------------------------------
# TestShellDoTrace
# ---------------------------------------------------------------------------

class TestShellDoTrace:
    """Tests for do_trace in client/amigactl/shell.py."""

    def test_trace_status_loaded(self, capsys):
        shell = _make_shell()
        shell.conn.trace_status.return_value = {
            "loaded": True, "enabled": True, "patches": 30,
            "events_produced": 100, "events_consumed": 95,
            "events_dropped": 5, "buffer_capacity": 8192,
            "buffer_used": 10,
        }
        shell.do_trace("status")
        out = capsys.readouterr().out
        assert "atrace status:" in out
        assert "Enabled:" in out
        assert "yes" in out
        assert "Patches:" in out
        assert "30" in out
        assert "Events produced:" in out
        assert "100" in out

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

        conn.trace_run("CNet:bbs", callback,
                        lib="dos", func="Open", errors_only=True)

        mock_send.assert_called_once_with(
            conn._sock,
            "TRACE RUN LIB=dos FUNC=Open ERRORS -- CNet:bbs")

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
            b'\t"test.txt",MODE_OLDFILE\t0x03c1a0b8'
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
