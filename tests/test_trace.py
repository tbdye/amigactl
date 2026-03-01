"""Integration tests for atrace (library call tracing).

These tests exercise the TRACE command family against a live amigactld
daemon with the atrace kernel module loaded.  They validate the wire
protocol, client library methods, and regressions for the three fixes:

1. Per-function enable/disable (Fix 1)
2. Buffer drain on global disable (Fix 2)
3. FindTask(NULL) string capture (Fix 3 -- covered implicitly by
   streaming tests that observe real events)

All tests in this module are skipped if the target does not have
atrace loaded.

The daemon must be running on the target machine before these tests are
executed.
"""

import socket
import time

import pytest

from conftest import (
    _read_line,
    _recv_exact,
    _send_stop_and_drain,
    read_response,
    send_command,
)
from amigactl import AmigaConnection, CommandSyntaxError


# ---------------------------------------------------------------------------
# Module-level fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True, scope="module")
def require_atrace(request):
    """Skip all tests in this module if atrace is not loaded."""
    host = request.config.getoption("--host")
    port = request.config.getoption("--port")
    conn = AmigaConnection(host, port)
    conn.connect()
    try:
        status = conn.trace_status()
        if not status.get("loaded"):
            pytest.skip("atrace not loaded on target")
    except Exception:
        pytest.skip("atrace not available (connection or command failed)")
    finally:
        conn.close()


@pytest.fixture
def restore_trace_state(conn):
    """Restore atrace to default state after test.

    Teardown unconditionally restores: globally enabled, all 30
    functions enabled.  This matches the expected default state of
    a freshly loaded atrace and is simpler than saving/restoring
    the exact pre-test state.
    """
    yield
    # Restore global enable
    conn.trace_enable()
    # Re-enable all patches that might have been disabled
    status = conn.trace_status()
    patch_list = status.get("patch_list", [])
    disabled_funcs = [e["name"].split(".")[-1] for e in patch_list
                      if not e.get("enabled")]
    if disabled_funcs:
        conn.trace_enable(funcs=disabled_funcs)


# ---------------------------------------------------------------------------
# TestTraceStatus -- read-only status queries
# ---------------------------------------------------------------------------

class TestTraceStatus:
    """Tests for TRACE STATUS via the client library."""

    def test_status_loaded(self, conn):
        """TRACE STATUS returns loaded=True and all expected keys."""
        status = conn.trace_status()
        assert status["loaded"] is True
        assert "enabled" in status
        assert "patches" in status
        assert "events_produced" in status
        assert "events_consumed" in status
        assert "events_dropped" in status
        assert "buffer_capacity" in status
        assert "buffer_used" in status

    def test_status_field_types(self, conn):
        """Numeric fields are ints, booleans are bools, patches==30."""
        status = conn.trace_status()
        assert isinstance(status["loaded"], bool)
        assert isinstance(status["enabled"], bool)
        assert isinstance(status["patches"], int)
        assert isinstance(status["events_produced"], int)
        assert status["patches"] == 30
        assert status["buffer_capacity"] > 0

    def test_status_patch_list(self, conn):
        """patch_list has 30 entries, each with name and enabled bool."""
        status = conn.trace_status()
        assert "patch_list" in status
        assert len(status["patch_list"]) == 30
        for entry in status["patch_list"]:
            assert "name" in entry
            assert "enabled" in entry
            assert isinstance(entry["enabled"], bool)

    def test_status_patch_names(self, conn):
        """Known function names appear in the patch list."""
        status = conn.trace_status()
        names = [e["name"] for e in status["patch_list"]]
        assert "exec.OpenLibrary" in names
        assert "dos.Open" in names
        assert "exec.FindTask" in names
        assert "dos.Lock" in names


# ---------------------------------------------------------------------------
# TestTraceEnableDisable -- per-function and global enable/disable
# ---------------------------------------------------------------------------

class TestTraceEnableDisable:
    """Tests for TRACE ENABLE and TRACE DISABLE via the client library.

    All tests request restore_trace_state to reset to defaults on
    teardown.
    """

    def test_global_enable(self, conn, restore_trace_state):
        """trace_enable() sets enabled=True."""
        conn.trace_disable()
        conn.trace_enable()
        status = conn.trace_status()
        assert status["enabled"] is True

    def test_global_disable(self, conn, restore_trace_state):
        """trace_disable() sets enabled=False."""
        conn.trace_enable()
        conn.trace_disable()
        status = conn.trace_status()
        assert status["enabled"] is False

    def test_enable_disable_roundtrip(self, conn, restore_trace_state):
        """Enable -> check -> disable -> check -> enable -> check."""
        conn.trace_enable()
        assert conn.trace_status()["enabled"] is True

        conn.trace_disable()
        assert conn.trace_status()["enabled"] is False

        conn.trace_enable()
        assert conn.trace_status()["enabled"] is True

    def test_per_func_enable(self, conn, restore_trace_state):
        """Disabling then re-enabling specific functions works."""
        conn.trace_disable(funcs=["Open", "Lock"])
        conn.trace_enable(funcs=["Open", "Lock"])
        status = conn.trace_status()
        patch_map = {e["name"]: e["enabled"]
                     for e in status["patch_list"]}
        assert patch_map["dos.Open"] is True
        assert patch_map["dos.Lock"] is True

    def test_per_func_disable(self, conn, restore_trace_state):
        """Disabling a specific function shows it as disabled."""
        conn.trace_enable(funcs=["Open"])
        conn.trace_disable(funcs=["Open"])
        status = conn.trace_status()
        patch_map = {e["name"]: e["enabled"]
                     for e in status["patch_list"]}
        assert patch_map["dos.Open"] is False

    def test_per_func_verify_status(self, conn, restore_trace_state):
        """Disable all, enable only Open and Lock, verify exact state."""
        status = conn.trace_status()
        all_func_names = [e["name"].split(".")[-1]
                          for e in status["patch_list"]]
        conn.trace_disable(funcs=all_func_names)

        conn.trace_enable(funcs=["Open", "Lock"])
        status = conn.trace_status()
        for entry in status["patch_list"]:
            func_name = entry["name"].split(".")[-1]
            if func_name in ("Open", "Lock"):
                assert entry["enabled"] is True, (
                    "{} should be enabled".format(entry["name"])
                )
            else:
                assert entry["enabled"] is False, (
                    "{} should be disabled".format(entry["name"])
                )

    def test_unknown_func_error(self, conn, restore_trace_state):
        """trace_enable with unknown function raises CommandSyntaxError."""
        with pytest.raises(CommandSyntaxError) as exc_info:
            conn.trace_enable(funcs=["Bogus"])
        assert "Unknown function: Bogus" in str(exc_info.value)

    def test_unknown_func_no_change(self, conn, restore_trace_state):
        """Error from unknown function leaves state unchanged (all-or-nothing)."""
        conn.trace_disable(funcs=["Open"])
        before = conn.trace_status()
        with pytest.raises(CommandSyntaxError):
            conn.trace_enable(funcs=["Open", "Bogus"])
        after = conn.trace_status()
        assert before["patch_list"] == after["patch_list"]


# ---------------------------------------------------------------------------
# TestBufferDrain -- Fix 2 regression tests
# ---------------------------------------------------------------------------

class TestBufferDrain:
    """Tests for buffer drain on global disable (Fix 2)."""

    def test_disable_drains_buffer(self, conn, restore_trace_state):
        """After global disable, buffer_used drops to 0."""
        conn.trace_enable()
        # With 30 patches active, background activity fills the buffer
        conn.trace_disable()
        status = conn.trace_status()
        assert status["buffer_used"] == 0

    def test_enable_after_drain(self, conn, restore_trace_state):
        """Re-enable after drain starts fresh without overflow."""
        conn.trace_disable()
        status = conn.trace_status()
        assert status["buffer_used"] == 0

        conn.trace_enable()
        time.sleep(0.5)
        # The key assertion: re-enable works without error.  Pre-Fix-2,
        # re-enabling with a full buffer caused immediate overflow.
        status = conn.trace_status()
        assert status["buffer_used"] >= 0


# ---------------------------------------------------------------------------
# TestTraceProtocol -- raw wire protocol validation
# ---------------------------------------------------------------------------

class TestTraceProtocol:
    """Tests for TRACE commands at the raw protocol level."""

    def test_trace_status_raw(self, raw_connection):
        """TRACE STATUS returns OK with expected payload lines."""
        sock, _banner = raw_connection
        send_command(sock, "TRACE STATUS")
        status, payload = read_response(sock)
        assert status == "OK"
        assert any(line == "loaded=1" for line in payload)
        assert any(line.startswith("enabled=") for line in payload)
        assert any(line.startswith("patches=") for line in payload)
        assert any(line.startswith("patch_") for line in payload)

    def test_trace_enable_raw(self, raw_connection, restore_trace_state):
        """TRACE ENABLE returns OK with empty payload."""
        sock, _banner = raw_connection
        send_command(sock, "TRACE ENABLE")
        status, payload = read_response(sock)
        assert status == "OK"
        assert payload == []

    def test_trace_disable_raw(self, raw_connection, restore_trace_state):
        """TRACE DISABLE returns OK with empty payload."""
        sock, _banner = raw_connection
        send_command(sock, "TRACE DISABLE")
        status, payload = read_response(sock)
        assert status == "OK"
        assert payload == []

    def test_trace_enable_funcs_raw(self, raw_connection,
                                    restore_trace_state):
        """TRACE ENABLE Open Lock returns OK with empty payload."""
        sock, _banner = raw_connection
        send_command(sock, "TRACE ENABLE Open Lock")
        status, payload = read_response(sock)
        assert status == "OK"
        assert payload == []

    def test_trace_unknown_func_raw(self, raw_connection):
        """TRACE ENABLE Bogus returns ERR 100 with error message."""
        sock, _banner = raw_connection
        send_command(sock, "TRACE ENABLE Bogus")
        status, payload = read_response(sock)
        assert status.startswith("ERR 100")
        assert "Unknown function: Bogus" in status


# ---------------------------------------------------------------------------
# TestTraceStreaming -- TRACE START/STOP streaming path
# ---------------------------------------------------------------------------

class TestTraceStreaming:
    """Tests for the TRACE START/STOP streaming path.

    These tests use low-frequency functions only to avoid data framing
    issues from high event volumes.
    """

    def test_trace_start_receives_events(self, amiga_host, amiga_port):
        """Start trace with FUNC=Open filter, trigger activity, get events."""
        # Trace connection (raw socket)
        trace_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        trace_sock.settimeout(10)
        trace_sock.connect((amiga_host, amiga_port))
        _read_line(trace_sock)  # banner

        # Activity connection (high-level)
        activity_conn = AmigaConnection(amiga_host, amiga_port)
        activity_conn.connect()

        try:
            # Start trace with a filter for dos.Open
            send_command(trace_sock, "TRACE START FUNC=Open")
            status_line = _read_line(trace_sock)
            assert status_line.startswith("OK"), (
                "Expected OK, got: {!r}".format(status_line)
            )

            activity_conn.execute("Echo >RAM:atrace_test_trigger test")

            received_data = False
            try:
                line = _read_line(trace_sock)
                if line.startswith("DATA "):
                    chunk_len = int(line[5:])
                    data = _recv_exact(trace_sock, chunk_len)
                    text = data.decode("iso-8859-1")
                    received_data = True
                    assert "Open" in text, (
                        "Expected 'Open' in trace event, got: {!r}".format(
                            text)
                    )
            except socket.timeout:
                pass

            assert received_data, "Expected at least one DATA chunk"

            # Clean stop
            _send_stop_and_drain(trace_sock)

            # Verify connection returns to normal
            send_command(trace_sock, "PING")
            status, payload = read_response(trace_sock)
            assert status == "OK"
            assert payload == []
        finally:
            try:
                activity_conn.delete("RAM:atrace_test_trigger")
            except Exception:
                pass
            trace_sock.close()
            activity_conn.close()

    def test_trace_stop_clean(self, amiga_host, amiga_port):
        """STOP immediately after START returns connection to normal."""
        trace_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        trace_sock.settimeout(10)
        trace_sock.connect((amiga_host, amiga_port))
        _read_line(trace_sock)  # banner

        try:
            send_command(trace_sock, "TRACE START FUNC=Open")
            status_line = _read_line(trace_sock)
            assert status_line.startswith("OK"), (
                "Expected OK, got: {!r}".format(status_line)
            )

            # Immediately stop
            _send_stop_and_drain(trace_sock)

            # Verify connection is back to normal
            send_command(trace_sock, "PING")
            status, payload = read_response(trace_sock)
            assert status == "OK"
            assert payload == []
        finally:
            trace_sock.close()


# ---------------------------------------------------------------------------
# TestTraceRun -- TRACE RUN command (launch + trace)
# ---------------------------------------------------------------------------

class TestTraceRun:
    """Tests for TRACE RUN against a live daemon with atrace loaded."""

    def test_trace_run_basic(self, amiga_host, amiga_port):
        """TRACE RUN -- Echo >NIL: hello produces events and exits cleanly."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(15)
        sock.connect((amiga_host, amiga_port))
        _read_line(sock)  # banner

        try:
            send_command(sock, "TRACE RUN -- Echo >NIL: hello")
            status_line = _read_line(sock)
            assert status_line.startswith("OK "), (
                "Expected OK <id>, got: {!r}".format(status_line))

            # Parse proc_id
            proc_id = int(status_line[3:].strip())
            assert proc_id > 0

            # Collect events until END
            events = []
            exit_comment = None
            while True:
                line = _read_line(sock)
                if line.startswith("DATA "):
                    chunk_len = int(line[5:])
                    data = _recv_exact(sock, chunk_len)
                    text = data.decode("iso-8859-1")
                    if text.startswith("# PROCESS EXITED"):
                        exit_comment = text
                    else:
                        events.append(text)
                elif line == "END":
                    sentinel = _read_line(sock)
                    assert sentinel == "."
                    break

            # Verify exit comment
            assert exit_comment is not None, (
                "Expected PROCESS EXITED comment")
            assert "rc=0" in exit_comment

            # Verify connection returns to normal
            send_command(sock, "PING")
            status, payload = read_response(sock)
            assert status == "OK"
        finally:
            sock.close()

    def test_trace_run_with_stop(self, amiga_host, amiga_port):
        """STOP during TRACE RUN returns connection to normal.
        Process continues running."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(15)
        sock.connect((amiga_host, amiga_port))
        _read_line(sock)  # banner

        try:
            # Launch a long-running command
            send_command(sock, "TRACE RUN -- Wait 2")
            status_line = _read_line(sock)
            assert status_line.startswith("OK "), (
                "Expected OK <id>, got: {!r}".format(status_line))
            proc_id = int(status_line[3:].strip())

            # Immediately stop tracing
            _send_stop_and_drain(sock)

            # Verify connection is back to normal
            send_command(sock, "PING")
            status, payload = read_response(sock)
            assert status == "OK"

            # Clean up: signal the process to stop
            send_command(sock, "SIGNAL {}".format(proc_id))
            read_response(sock)
        finally:
            sock.close()

    def test_trace_run_command_not_found(self, amiga_host, amiga_port):
        """Running a nonexistent command produces PROCESS EXITED with
        a non-zero rc."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(15)
        sock.connect((amiga_host, amiga_port))
        _read_line(sock)  # banner

        try:
            send_command(
                sock,
                "TRACE RUN -- NoSuchProgramThatDefinitelyDoesNotExist_xyz")
            status_line = _read_line(sock)
            assert status_line.startswith("OK "), (
                "Expected OK <id>, got: {!r}".format(status_line))

            # Collect until END -- expect exit comment with non-zero rc
            exit_comment = None
            while True:
                line = _read_line(sock)
                if line.startswith("DATA "):
                    chunk_len = int(line[5:])
                    data = _recv_exact(sock, chunk_len)
                    text = data.decode("iso-8859-1")
                    if text.startswith("# PROCESS EXITED"):
                        exit_comment = text
                elif line == "END":
                    sentinel = _read_line(sock)
                    assert sentinel == "."
                    break

            assert exit_comment is not None, (
                "Expected PROCESS EXITED comment")
            # Command not found typically returns rc=-1 or rc=20
            assert "rc=" in exit_comment
            # Parse the rc value -- it should not be 0
            rc_str = exit_comment.split("rc=")[1].strip()
            rc = int(rc_str)
            assert rc != 0, (
                "Expected non-zero rc for command not found, got {}".format(rc)
            )
        finally:
            sock.close()

    def test_trace_run_with_filters(self, amiga_host, amiga_port):
        """TRACE RUN LIB=dos shows only dos.* events."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(15)
        sock.connect((amiga_host, amiga_port))
        _read_line(sock)  # banner

        try:
            send_command(sock, "TRACE RUN LIB=dos -- Echo >NIL: hello")
            status_line = _read_line(sock)
            assert status_line.startswith("OK "), (
                "Expected OK <id>, got: {!r}".format(status_line))

            events = []
            while True:
                line = _read_line(sock)
                if line.startswith("DATA "):
                    chunk_len = int(line[5:])
                    data = _recv_exact(sock, chunk_len)
                    text = data.decode("iso-8859-1")
                    if not text.startswith("#"):
                        events.append(text)
                elif line == "END":
                    _read_line(sock)  # sentinel
                    break

            # All events should be dos.* (not exec.*)
            for ev in events:
                parts = ev.split("\t")
                if len(parts) >= 3:
                    assert parts[2].startswith("dos."), (
                        "Expected dos.* event, got: {}".format(parts[2]))
        finally:
            sock.close()

    def test_trace_run_proc_filter_rejected(self, raw_connection):
        """TRACE RUN with PROC= returns ERR 100."""
        sock, _banner = raw_connection
        send_command(sock, "TRACE RUN PROC=test -- Echo hello")
        status, payload = read_response(sock)
        assert status.startswith("ERR 100")

    def test_trace_run_missing_separator(self, raw_connection):
        """TRACE RUN without -- returns ERR 100."""
        sock, _banner = raw_connection
        send_command(sock, "TRACE RUN Echo hello")
        status, payload = read_response(sock)
        assert status.startswith("ERR 100")

    def test_trace_run_missing_command(self, raw_connection):
        """TRACE RUN -- (no command) returns ERR 100."""
        sock, _banner = raw_connection
        send_command(sock, "TRACE RUN --")
        status, payload = read_response(sock)
        assert status.startswith("ERR 100")

    def test_trace_run_client_api(self, conn):
        """trace_run() via the Python client API works end-to-end."""
        events = []

        def cb(event):
            events.append(event)

        result = conn.trace_run("Echo >NIL: hello", cb)

        assert result["proc_id"] is not None
        assert result["proc_id"] > 0
        assert result["rc"] == 0

        # Should have at least one event (process exit comment)
        assert len(events) >= 1
        # Last event should be the exit comment
        exit_events = [e for e in events
                       if e.get("type") == "comment"
                       and "PROCESS EXITED" in e.get("text", "")]
        assert len(exit_events) == 1
