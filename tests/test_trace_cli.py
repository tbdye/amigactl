"""Tests for CLI trace subcommands in __main__.py.

These are unit tests that mock the AmigaConnection to verify that the
CLI trace subcommands parse arguments correctly, call the right client
methods, and format output properly.

The tests follow the same mock-based pattern used by TestShellDoTrace
and TestShellDoTraceRun in test_trace_unit.py.
"""

import argparse
import sys
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_conn():
    """Create a mock AmigaConnection for CLI testing."""
    conn = mock.MagicMock()
    conn.trace_status.return_value = {
        "loaded": True,
        "enabled": True,
        "patches": 99,
        "events_produced": 100,
        "events_consumed": 95,
        "events_dropped": 5,
        "buffer_capacity": 8192,
        "buffer_used": 10,
    }
    conn.trace_enable.return_value = None
    conn.trace_disable.return_value = None
    conn.trace_run.return_value = {
        "proc_id": 42,
        "rc": 0,
        "stats": {
            "total_events": 10,
            "by_function": {"Open": 5, "Close": 5},
            "errors": 0,
            "error_functions": {},
        },
    }
    return conn


def _make_args(**kwargs):
    """Build an argparse.Namespace with trace-relevant defaults."""
    defaults = {
        "trace_cmd": None,
        "lib": None,
        "func": None,
        "proc": None,
        "errors": False,
        "cd": None,
        "tier": None,
        "funcs": None,
        "cmd": [],
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _run_cmd_trace(conn, args):
    """Import and call cmd_trace from __main__.py."""
    from amigactl.__main__ import cmd_trace
    cmd_trace(conn, args)


# ---------------------------------------------------------------------------
# TestTraceStatusCLI
# ---------------------------------------------------------------------------

class TestTraceStatusCLI:
    """Test `amigactl trace status` CLI output."""

    def test_status_loaded_output(self, capsys):
        """trace status displays loaded state with patch count."""
        conn = _make_mock_conn()
        args = _make_args(trace_cmd="status")
        _run_cmd_trace(conn, args)
        out = capsys.readouterr().out
        assert "loaded=1" in out
        assert "enabled=1" in out
        assert "patches=99" in out

    def test_status_not_loaded_output(self, capsys):
        """trace status displays 'not loaded' when atrace is inactive."""
        conn = _make_mock_conn()
        conn.trace_status.return_value = {"loaded": False}
        args = _make_args(trace_cmd="status")
        _run_cmd_trace(conn, args)
        out = capsys.readouterr().out
        assert "not loaded" in out

    def test_status_shows_event_counts(self, capsys):
        """trace status includes event production/consumption counters."""
        conn = _make_mock_conn()
        args = _make_args(trace_cmd="status")
        _run_cmd_trace(conn, args)
        out = capsys.readouterr().out
        assert "events_produced=100" in out
        assert "events_consumed=95" in out
        assert "events_dropped=5" in out


# ---------------------------------------------------------------------------
# TestTraceEnableDisableCLI
# ---------------------------------------------------------------------------

class TestTraceEnableDisableCLI:
    """Test `amigactl trace enable/disable` CLI commands."""

    def test_enable_global(self, capsys):
        """trace enable with no args calls trace_enable() globally."""
        conn = _make_mock_conn()
        args = _make_args(trace_cmd="enable", funcs=[])
        _run_cmd_trace(conn, args)
        conn.trace_enable.assert_called_once_with(funcs=None)
        out = capsys.readouterr().out
        assert "enabled" in out.lower()

    def test_enable_funcs(self, capsys):
        """trace enable with func names calls trace_enable with func list."""
        conn = _make_mock_conn()
        args = _make_args(trace_cmd="enable", funcs=["Open", "Close"])
        _run_cmd_trace(conn, args)
        conn.trace_enable.assert_called_once_with(funcs=["Open", "Close"])
        out = capsys.readouterr().out
        assert "Open" in out
        assert "Close" in out

    def test_disable_global(self, capsys):
        """trace disable with no args calls trace_disable() globally."""
        conn = _make_mock_conn()
        args = _make_args(trace_cmd="disable", funcs=[])
        _run_cmd_trace(conn, args)
        conn.trace_disable.assert_called_once_with(funcs=None)
        out = capsys.readouterr().out
        assert "disabled" in out.lower()

    def test_disable_funcs(self, capsys):
        """trace disable with func names calls trace_disable with func list."""
        conn = _make_mock_conn()
        args = _make_args(trace_cmd="disable", funcs=["Open", "Close"])
        _run_cmd_trace(conn, args)
        conn.trace_disable.assert_called_once_with(funcs=["Open", "Close"])
        out = capsys.readouterr().out
        assert "Open" in out
        assert "Close" in out


# ---------------------------------------------------------------------------
# TestTraceRunCLI
# ---------------------------------------------------------------------------

class TestTraceRunCLI:
    """Test `amigactl trace run` CLI argument parsing."""

    def test_run_requires_command(self, capsys):
        """trace run with empty command shows usage error and exits."""
        conn = _make_mock_conn()
        args = _make_args(trace_cmd="run", cmd=[])
        with pytest.raises(SystemExit) as exc_info:
            _run_cmd_trace(conn, args)
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "Usage:" in err

    def test_run_strips_separator(self, capsys):
        """trace run with -- separator strips it before passing command."""
        conn = _make_mock_conn()
        args = _make_args(trace_cmd="run", cmd=["--", "C:atrace_test"])
        _run_cmd_trace(conn, args)
        # The command passed to trace_run should be "C:atrace_test"
        call_args = conn.trace_run.call_args
        assert call_args[0][0] == "C:atrace_test"

    def test_run_passes_lib_filter(self, capsys):
        """trace run --lib=dos passes lib filter to trace_run."""
        conn = _make_mock_conn()
        args = _make_args(trace_cmd="run", cmd=["C:test"], lib="dos")
        _run_cmd_trace(conn, args)
        call_kwargs = conn.trace_run.call_args[1]
        assert call_kwargs.get("lib") == "dos"

    def test_run_passes_func_filter(self, capsys):
        """trace run --func=Open passes func filter to trace_run."""
        conn = _make_mock_conn()
        args = _make_args(trace_cmd="run", cmd=["C:test"], func="Open")
        _run_cmd_trace(conn, args)
        call_kwargs = conn.trace_run.call_args[1]
        assert call_kwargs.get("func") == "Open"

    def test_run_passes_errors_flag(self, capsys):
        """trace run --errors passes errors_only=True to trace_run."""
        conn = _make_mock_conn()
        args = _make_args(trace_cmd="run", cmd=["C:test"], errors=True)
        _run_cmd_trace(conn, args)
        call_kwargs = conn.trace_run.call_args[1]
        assert call_kwargs.get("errors_only") is True

    def test_run_passes_cd(self, capsys):
        """trace run --cd=RAM: passes cd to trace_run."""
        conn = _make_mock_conn()
        args = _make_args(trace_cmd="run", cmd=["C:test"], cd="RAM:")
        _run_cmd_trace(conn, args)
        call_kwargs = conn.trace_run.call_args[1]
        assert call_kwargs.get("cd") == "RAM:"

    def test_run_nonzero_rc_exits(self, capsys):
        """trace run with non-zero rc exits with error code."""
        conn = _make_mock_conn()
        conn.trace_run.return_value = {
            "proc_id": 42, "rc": 20,
            "stats": {"total_events": 0, "by_function": {},
                      "errors": 0, "error_functions": {}},
        }
        args = _make_args(trace_cmd="run", cmd=["C:failing_cmd"])
        with pytest.raises(SystemExit) as exc_info:
            _run_cmd_trace(conn, args)
        assert exc_info.value.code == 20
        err = capsys.readouterr().err
        assert "rc=20" in err


# ---------------------------------------------------------------------------
# TestTraceStartCLI
# ---------------------------------------------------------------------------

class TestTraceStartCLI:
    """Test `amigactl trace start` CLI argument parsing."""

    def test_start_calls_trace_start(self, capsys):
        """trace start calls trace_start on the connection."""
        conn = _make_mock_conn()
        # trace_start blocks, so make it raise KeyboardInterrupt immediately
        conn.trace_start.side_effect = KeyboardInterrupt
        args = _make_args(trace_cmd="start")
        _run_cmd_trace(conn, args)
        conn.trace_start.assert_called_once()

    def test_start_passes_lib_filter(self, capsys):
        """trace start --lib=dos passes lib filter."""
        conn = _make_mock_conn()
        conn.trace_start.side_effect = KeyboardInterrupt
        args = _make_args(trace_cmd="start", lib="dos")
        _run_cmd_trace(conn, args)
        call_kwargs = conn.trace_start.call_args[1]
        assert call_kwargs.get("lib") == "dos"

    def test_start_passes_func_filter(self, capsys):
        """trace start --func=Open passes func filter."""
        conn = _make_mock_conn()
        conn.trace_start.side_effect = KeyboardInterrupt
        args = _make_args(trace_cmd="start", func="Open")
        _run_cmd_trace(conn, args)
        call_kwargs = conn.trace_start.call_args[1]
        assert call_kwargs.get("func") == "Open"

    def test_start_passes_proc_filter(self, capsys):
        """trace start --proc=myapp passes proc filter."""
        conn = _make_mock_conn()
        conn.trace_start.side_effect = KeyboardInterrupt
        args = _make_args(trace_cmd="start", proc="myapp")
        _run_cmd_trace(conn, args)
        call_kwargs = conn.trace_start.call_args[1]
        assert call_kwargs.get("proc") == "myapp"

    def test_start_passes_errors_flag(self, capsys):
        """trace start --errors passes errors_only=True."""
        conn = _make_mock_conn()
        conn.trace_start.side_effect = KeyboardInterrupt
        args = _make_args(trace_cmd="start", errors=True)
        _run_cmd_trace(conn, args)
        call_kwargs = conn.trace_start.call_args[1]
        assert call_kwargs.get("errors_only") is True

    def test_start_keyboard_interrupt_stops(self, capsys):
        """trace start handles KeyboardInterrupt by calling stop_trace."""
        conn = _make_mock_conn()
        conn.trace_start.side_effect = KeyboardInterrupt
        args = _make_args(trace_cmd="start")
        _run_cmd_trace(conn, args)
        conn.stop_trace.assert_called_once()


# ---------------------------------------------------------------------------
# TestTraceStopCLI
# ---------------------------------------------------------------------------

class TestTraceStopCLI:
    """Test `amigactl trace stop` CLI command."""

    def test_stop_shows_error(self, capsys):
        """trace stop outside a session shows usage message and exits."""
        conn = _make_mock_conn()
        args = _make_args(trace_cmd="stop")
        with pytest.raises(SystemExit) as exc_info:
            _run_cmd_trace(conn, args)
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "only valid during an active trace stream" in err


# ---------------------------------------------------------------------------
# TestTraceNoSubcommand
# ---------------------------------------------------------------------------

class TestTraceNoSubcommand:
    """Test `amigactl trace` with no subcommand."""

    def test_no_subcommand_shows_usage(self, capsys):
        """trace with no subcommand shows usage and exits."""
        conn = _make_mock_conn()
        args = _make_args(trace_cmd=None)
        with pytest.raises(SystemExit) as exc_info:
            _run_cmd_trace(conn, args)
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "Usage:" in err
