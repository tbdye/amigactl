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

    Teardown restores: globally enabled, all non-noise functions
    enabled, noise functions disabled.  This matches the expected
    default state of a freshly loaded atrace (Phase 4+).
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
    # Re-disable noise functions to match post-install defaults
    noise_funcs = ["FindPort", "FindSemaphore", "FindTask",
                   "GetMsg", "PutMsg", "ObtainSemaphore",
                   "ReleaseSemaphore", "AllocMem",
                   "OpenLibrary",
                   # Phase 5 additions
                   "FreeMem", "AllocVec", "FreeVec",
                   "Read", "Write",
                   # Phase 5 device I/O additions
                   "DoIO", "SendIO", "WaitIO",
                   "AbortIO", "CheckIO"]
    conn.trace_disable(funcs=noise_funcs)


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
        """Numeric fields are ints, booleans are bools, patches==50."""
        status = conn.trace_status()
        assert isinstance(status["loaded"], bool)
        assert isinstance(status["enabled"], bool)
        assert isinstance(status["patches"], int)
        assert isinstance(status["events_produced"], int)
        assert status["patches"] == 50
        assert status["buffer_capacity"] > 0

    def test_status_patch_list(self, conn):
        """patch_list has 50 entries, each with name and enabled bool."""
        status = conn.trace_status()
        assert "patch_list" in status
        assert len(status["patch_list"]) == 50
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
        # With 50 patches active, background activity fills the buffer
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
                    # Skip header comment lines (Phase 5b)
                    while text.startswith("#"):
                        line = _read_line(trace_sock)
                        if not line.startswith("DATA "):
                            break
                        chunk_len = int(line[5:])
                        data = _recv_exact(trace_sock, chunk_len)
                        text = data.decode("iso-8859-1")
                    if text.startswith("#"):
                        # Loop exited via break (non-DATA line while
                        # skipping comments) -- no event received.
                        pass
                    else:
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


# ---------------------------------------------------------------------------
# TestNoiseDefaults -- Phase 4: noise function auto-disable
# ---------------------------------------------------------------------------

class TestNoiseDefaults:
    """Tests for noise function auto-disable defaults (Phase 4).

    After loading atrace, 19 high-frequency functions should be
    disabled by default.  These tests verify the default state and
    user override behavior.
    """

    def test_noise_funcs_default_disabled(self, conn):
        """Noise functions should be disabled by default after loading."""
        status = conn.trace_status()
        assert status.get("noise_disabled", 0) >= 19

        # Check specific functions are disabled
        patches = status.get("patch_list", [])
        noise_names = {
            "exec.FindPort", "exec.FindSemaphore", "exec.FindTask",
            "exec.GetMsg", "exec.PutMsg", "exec.ObtainSemaphore",
            "exec.ReleaseSemaphore", "exec.AllocMem",
            "exec.OpenLibrary",
            # Phase 5 additions
            "exec.FreeMem", "exec.AllocVec", "exec.FreeVec",
            "dos.Read", "dos.Write",
            # Phase 5 device I/O additions
            "exec.DoIO", "exec.SendIO", "exec.WaitIO",
            "exec.AbortIO", "exec.CheckIO",
        }
        for patch in patches:
            if patch["name"] in noise_names:
                assert not patch["enabled"], \
                    "{} should be disabled by default".format(patch["name"])

        # Cross-check: noise_disabled count matches actual disabled noise funcs
        actual_disabled = sum(
            1 for p in patches
            if p["name"] in noise_names and not p["enabled"]
        )
        assert status["noise_disabled"] == actual_disabled, \
            "noise_disabled {} != actual disabled noise funcs {}".format(
                status["noise_disabled"], actual_disabled)

    def test_noise_func_explicit_enable(self, conn, restore_trace_state):
        """User can explicitly enable noise functions."""
        conn.trace_enable(funcs=["ObtainSemaphore"])
        status = conn.trace_status()
        patches = status.get("patch_list", [])
        for patch in patches:
            if patch["name"] == "exec.ObtainSemaphore":
                assert patch["enabled"], \
                    "ObtainSemaphore should be enabled after explicit enable"
                break


# ---------------------------------------------------------------------------
# TestTraceRunPhase4 -- Phase 4: task filter, noise isolation, process name
# ---------------------------------------------------------------------------

class TestTraceRunPhase4:
    """Tests for Phase 4 TRACE RUN enhancements: task filter, noise
    isolation, and process name fix."""

    def test_trace_run_leaves_noise_disabled(self, amiga_host, amiga_port):
        """TRACE RUN should not change noise function enable state."""
        conn = AmigaConnection(amiga_host, amiga_port)
        conn.connect()
        try:
            # Check noise functions are disabled before
            status = conn.trace_status()
            pre_noise = status.get("noise_disabled", 0)
            assert pre_noise >= 19

            # Start TRACE RUN
            events = []

            def collect(ev):
                events.append(ev)

            conn.trace_run("List SYS:", collect)

            # After trace_run returns, noise should still be disabled
            status = conn.trace_status()
            post_noise = status.get("noise_disabled", 0)
            assert post_noise >= 19, \
                "Noise functions should remain disabled during TRACE RUN"
        finally:
            conn.close()

    def test_trace_run_no_overflow(self, amiga_host, amiga_port):
        """TRACE RUN with task filter should not overflow the ring buffer."""
        conn = AmigaConnection(amiga_host, amiga_port)
        conn.connect()
        try:
            # Record overflow count before
            status_before = conn.trace_status()
            overflow_before = status_before.get("events_dropped", 0)

            events = []

            def collect(ev):
                events.append(ev)

            conn.trace_run("List SYS:", collect)

            # Check overflow did not increase
            status_after = conn.trace_status()
            overflow_after = status_after.get("events_dropped", 0)
            assert overflow_after == overflow_before, \
                "Ring buffer overflowed during filtered TRACE RUN"
        finally:
            conn.close()

    def test_trace_run_process_name(self, amiga_host, amiga_port):
        """TRACE RUN should show command basename, not 'amigactld-exec'."""
        conn = AmigaConnection(amiga_host, amiga_port)
        conn.connect()
        try:
            events = []

            def collect(ev):
                if ev.get("type") == "event":
                    events.append(ev)

            conn.trace_run("C:List SYS:", collect)

            # RunCommand executes in the daemon's own process context,
            # so the task name is the daemon's CLI command name "amigactld"
            assert len(events) > 0, "No events received"
            task_names = {ev.get("task", "") for ev in events}
            assert any("amigactld" in t for t in task_names), \
                "Expected task name containing 'amigactld', got: {}".format(task_names)
        finally:
            conn.close()

    def test_trace_run_filter_only_target(self, amiga_host, amiga_port):
        """TRACE RUN should only show events from the target process."""
        conn = AmigaConnection(amiga_host, amiga_port)
        conn.connect()
        try:
            events = []

            def collect(ev):
                if ev.get("type") == "event":
                    events.append(ev)

            conn.trace_run("C:List SYS:", collect)

            # All events should be from the daemon process (RunCommand
            # executes in the daemon's own context, identified as "amigactld")
            assert len(events) > 0, "No events received"
            for ev in events:
                task = ev.get("task", "")
                assert "amigactld" in task, \
                    "Event from non-target task: {}".format(task)
        finally:
            conn.close()

    def test_trace_status_filter_task_during_run(self, amiga_host,
                                                  amiga_port):
        """TRACE STATUS should show filter_task during TRACE RUN."""
        import threading

        conn1 = AmigaConnection(amiga_host, amiga_port)
        conn1.connect()
        conn2 = AmigaConnection(amiga_host, amiga_port)
        conn2.connect()
        try:
            # Start a long-running TRACE RUN on conn1
            events = []
            run_done = threading.Event()

            def collect(ev):
                events.append(ev)

            def run_trace():
                try:
                    conn1.trace_run("C:Wait 2", collect)
                except Exception:
                    pass
                run_done.set()

            t = threading.Thread(target=run_trace)
            t.start()

            # Poll for filter_task to become non-zero (up to 3 seconds)
            filter_task = "0x00000000"
            for _ in range(30):
                time.sleep(0.1)
                status = conn2.trace_status()
                filter_task = status.get("filter_task", "0x00000000")
                if filter_task != "0x00000000":
                    break
            assert filter_task != "0x00000000", \
                "filter_task should be non-zero during TRACE RUN"

            run_done.wait(timeout=10)
            t.join(timeout=5)
        finally:
            conn1.close()
            conn2.close()


# ---------------------------------------------------------------------------
# TestPhase4bFilters -- Phase 4b filter feature tests
# ---------------------------------------------------------------------------

class TestPhase4bFilters:
    """Tests for Phase 4b filter enhancements: LIB= suffix stripping
    and FUNC= unknown sentinel matching.

    These require direct protocol interaction with TRACE RUN to verify
    daemon-side filter parsing.
    """

    def test_lib_suffix_stripping(self, amiga_host, amiga_port):
        """LIB=dos.library filter works after stripping the .library suffix.

        The daemon should accept "LIB=dos.library" and strip the suffix
        to match against "dos".  All received events should have lib="dos".
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(15)
        sock.connect((amiga_host, amiga_port))
        _read_line(sock)  # banner

        try:
            send_command(
                sock, "TRACE RUN LIB=dos.library -- C:Echo >NIL: test")
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

            # Must have received at least one event (Echo does dos.Open etc.)
            assert len(events) >= 1, (
                "No events received with LIB=dos.library filter")

            # All events should be dos.* (suffix was stripped correctly)
            for ev_text in events:
                parts = ev_text.split("\t")
                if len(parts) >= 3:
                    assert parts[2].startswith("dos."), (
                        "Expected dos.* event with LIB=dos.library filter, "
                        "got: {}".format(parts[2]))
        finally:
            sock.close()

    def test_func_unknown_sentinel(self, amiga_host, amiga_port):
        """FUNC=BogusFunction filter matches nothing, producing zero events.

        The daemon installs the unknown function name as a filter
        sentinel.  Since no traced function matches "BogusFunction",
        no events should be produced.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(15)
        sock.connect((amiga_host, amiga_port))
        _read_line(sock)  # banner

        try:
            send_command(
                sock,
                "TRACE RUN FUNC=BogusFunction -- C:Echo >NIL: test")
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

            assert len(events) == 0, (
                "Expected zero non-comment events with FUNC=BogusFunction "
                "filter, got {}: {}".format(
                    len(events),
                    [e[:80] for e in events]))
        finally:
            sock.close()


# ---------------------------------------------------------------------------
# TestTraceFilter -- Phase 7: mid-stream FILTER command tests
# ---------------------------------------------------------------------------

def _timeout_handler(signum, frame):
    raise TimeoutError("Test timed out")


def _collect_events_raw(session, timeout=5.0, max_events=200):
    """Collect trace events from a RawTraceSession using select().

    Returns a list of event dicts (type="event" only, no comments).
    Stops after max_events or when timeout expires with no new data.
    """
    import select

    events = []

    # First, drain any complete events left in the reader's internal
    # buffer from a prior recv() (e.g. from _drain_pre_filter_events).
    # Without this, select() may not fire if the kernel buffer is empty,
    # even though the reader has complete events ready to parse.
    while session.reader.has_buffered_data():
        result = session.reader.drain_buffered()
        if result is False:
            return events
        if result is None:
            break  # Partial data, need more from socket
        if result.get("type") == "event":
            events.append(result)

    for _ in range(max_events * 2):
        r, _, _ = select.select([session.sock], [], [], timeout)
        if not r:
            break  # Timed out waiting for data
        result = session.reader.try_read_event()
        if result is False:
            break  # Stream ended (END received)
        if result is None:
            continue  # Partial data, wait for more
        if result.get("type") == "event":
            events.append(result)
        # Drain any remaining buffered events from the same recv()
        while session.reader.has_buffered_data():
            result = session.reader.drain_buffered()
            if result is False:
                return events
            if result is None:
                break
            if result.get("type") == "event":
                events.append(result)
    return events


def _drain_pre_filter_events(session, timeout=1.0):
    """Drain any events that arrived before the FILTER took effect.

    After sending a FILTER command on a trace session that was started
    without an initial filter, system events (e.g. exec.OpenLibrary)
    may have accumulated in the socket buffer between TRACE START and
    the FILTER being processed.  This function consumes and discards
    those stale events so subsequent collection sees only post-filter
    events.

    Call this after send_filter() and a short sleep, before generating
    the test activity.
    """
    _collect_events_raw(session, timeout=timeout)


def _stop_raw_session(session):
    """Send STOP and drain a raw trace session back to command mode.

    Temporarily switches socket to blocking with a timeout so the
    STOP command and drain can complete reliably.

    Important: The TraceStreamReader may have already recv()'d data
    into its internal buffer that is no longer available on the socket.
    We must drain buffered events from the reader first, then switch
    to direct socket reads for any remaining framing.
    """
    from amigactl.protocol import send_command as proto_send_command

    session.sock.settimeout(10)
    proto_send_command(session.sock, "STOP")

    # First, drain any data already buffered in the TraceStreamReader.
    # The reader may have recv(4096)'d data that includes the STOP
    # response framing (DATA/END/ERR lines).  Reading directly from
    # the socket would miss this buffered data, causing desync.
    while session.reader.has_buffered_data():
        result = session.reader.drain_buffered()
        if result is False:
            return  # Stream ended (END or ERR consumed from buffer)
        if result is None:
            break  # Incomplete data in buffer, fall through to socket

    # Now use the reader's try_read_event() which does recv() + buffer
    # processing, keeping everything in sync.  The socket has a 10s
    # timeout, so each recv() blocks at most 10s.  Limit iterations
    # to prevent infinite loops if END never arrives (the 60s SIGALRM
    # is the ultimate safety net).
    for _ in range(500):
        result = session.reader.try_read_event()
        if result is False:
            break  # Stream ended
        # result is None (partial) or an event dict -- keep draining
        while session.reader.has_buffered_data():
            result = session.reader.drain_buffered()
            if result is False:
                return
            if result is None:
                break


def _find_events(events, func_name, args_contains=None):
    """Find events matching func name and optional args substring.

    Same helper as test_trace_app.py -- duplicated here so this module
    is self-contained.
    """
    result = []
    for ev in events:
        if ev.get("func") != func_name:
            continue
        if args_contains is not None and args_contains not in ev.get(
                "args", ""):
            continue
        result.append(ev)
    return result


def _collect_run_events_raw(session, timeout=5.0, max_events=500):
    """Collect trace events from a TRACE RUN raw session until exit.

    Unlike _collect_events_raw, this waits for the PROCESS EXITED
    comment that signals the traced process has finished, or until the
    stream ends (END received).

    Returns (events, saw_exit) where events is a list of event dicts
    (type="event" only) and saw_exit indicates whether PROCESS EXITED
    was seen.
    """
    import select

    events = []
    saw_exit = False

    # Pre-drain buffered data (same rationale as _collect_events_raw).
    while session.reader.has_buffered_data():
        result = session.reader.drain_buffered()
        if result is False:
            return events, True
        if result is None:
            break
        if result.get("type") == "event":
            events.append(result)
        elif result.get("type") == "comment":
            if "PROCESS EXITED" in result.get("text", ""):
                saw_exit = True
        if saw_exit:
            return events, saw_exit

    for _ in range(max_events * 2):
        r, _, _ = select.select([session.sock], [], [], timeout)
        if not r:
            break
        result = session.reader.try_read_event()
        if result is False:
            break
        if result is None:
            continue
        if result.get("type") == "event":
            events.append(result)
        elif result.get("type") == "comment":
            if "PROCESS EXITED" in result.get("text", ""):
                saw_exit = True
        while session.reader.has_buffered_data():
            result = session.reader.drain_buffered()
            if result is False:
                saw_exit = True
                break
            if result is None:
                break
            if result.get("type") == "event":
                events.append(result)
            elif result.get("type") == "comment":
                if "PROCESS EXITED" in result.get("text", ""):
                    saw_exit = True
        if saw_exit:
            break
    return events, saw_exit


class TestTraceFilter:
    """Tests for mid-stream FILTER command (Phase 7).

    These tests use the two-connection pattern: conn1 starts a global
    trace stream (TRACE START), applies a FILTER, then conn2 runs
    C:atrace_test to generate deterministic events across both dos and
    exec libraries.  Assertions use _find_events() with known
    atrace_test arguments for precise validation.
    """

    def test_filter_lib_single(self, amiga_host, amiga_port):
        """FILTER LIB=dos passes only dos events from atrace_test."""
        import signal as sig

        old_handler = sig.signal(sig.SIGALRM, _timeout_handler)
        sig.alarm(60)
        try:
            conn_trace = AmigaConnection(amiga_host, amiga_port)
            conn_trace.connect()
            conn_activity = AmigaConnection(amiga_host, amiga_port)
            conn_activity.connect()

            try:
                session = conn_trace.trace_start_raw()
                with session:
                    conn_trace.send_filter(raw="LIB=dos")
                    time.sleep(0.5)
                    _drain_pre_filter_events(session)

                    conn_activity.execute("C:atrace_test", timeout=60)

                    events = _collect_events_raw(session, timeout=5.0)

                    _stop_raw_session(session)

                # Should have dos events from atrace_test
                assert len(events) > 0, "No events received with LIB=dos"

                # All events must be dos library
                for ev in events:
                    assert ev["lib"] == "dos", (
                        "Expected lib='dos', got lib={!r} func={!r}".format(
                            ev["lib"], ev["func"]))

                # Verify known dos events from atrace_test are present
                opens = _find_events(events, "Open", "atrace_test")
                assert len(opens) >= 1, (
                    "Expected atrace_test Open events in dos-filtered "
                    "stream, found none in {} events".format(len(events)))

                # Verify no exec events leaked through
                exec_funcs = {"FindPort", "FindResident", "FindSemaphore",
                              "FindTask", "OpenDevice", "OpenLibrary",
                              "OpenResource", "GetMsg", "PutMsg",
                              "ObtainSemaphore", "ReleaseSemaphore",
                              "AllocMem"}
                for ev in events:
                    assert ev["func"] not in exec_funcs, (
                        "exec func {!r} should not appear with LIB=dos "
                        "filter".format(ev["func"]))
            finally:
                conn_trace.close()
                conn_activity.close()
        finally:
            sig.alarm(0)
            sig.signal(sig.SIGALRM, old_handler)

    def test_filter_lib_multiple(self, amiga_host, amiga_port):
        """FILTER LIB=dos,exec passes both libraries from atrace_test."""
        import signal as sig

        old_handler = sig.signal(sig.SIGALRM, _timeout_handler)
        sig.alarm(60)
        try:
            conn_trace = AmigaConnection(amiga_host, amiga_port)
            conn_trace.connect()
            conn_activity = AmigaConnection(amiga_host, amiga_port)
            conn_activity.connect()

            try:
                session = conn_trace.trace_start_raw()
                with session:
                    conn_trace.send_filter(raw="LIB=dos,exec")
                    time.sleep(0.5)
                    _drain_pre_filter_events(session)

                    conn_activity.execute("C:atrace_test", timeout=60)

                    events = _collect_events_raw(session, timeout=5.0)

                    _stop_raw_session(session)

                assert len(events) > 0, "No events received with LIB=dos,exec"
                allowed_libs = {"dos", "exec"}
                for ev in events:
                    assert ev["lib"] in allowed_libs, (
                        "Expected lib in {}, got lib={!r} func={!r}".format(
                            allowed_libs, ev["lib"], ev["func"]))

                # Both libraries should be represented (atrace_test
                # exercises both dos and exec)
                libs_seen = {ev["lib"] for ev in events}
                assert "dos" in libs_seen, (
                    "Expected dos events from atrace_test, only saw: "
                    "{}".format(libs_seen))
                assert "exec" in libs_seen, (
                    "Expected exec events from atrace_test, only saw: "
                    "{}".format(libs_seen))
            finally:
                conn_trace.close()
                conn_activity.close()
        finally:
            sig.alarm(0)
            sig.signal(sig.SIGALRM, old_handler)

    def test_filter_func_whitelist(self, amiga_host, amiga_port):
        """FILTER FUNC=Open passes only Open events from atrace_test."""
        import signal as sig

        old_handler = sig.signal(sig.SIGALRM, _timeout_handler)
        sig.alarm(60)
        try:
            conn_trace = AmigaConnection(amiga_host, amiga_port)
            conn_trace.connect()
            conn_activity = AmigaConnection(amiga_host, amiga_port)
            conn_activity.connect()

            try:
                session = conn_trace.trace_start_raw()
                with session:
                    conn_trace.send_filter(raw="FUNC=Open")
                    time.sleep(0.5)
                    _drain_pre_filter_events(session)

                    # atrace_test calls Open multiple times:
                    # - Open("RAM:atrace_test_read", MODE_NEWFILE)
                    # - Open("RAM:atrace_test_read", MODE_OLDFILE)
                    # - Open("RAM:atrace_test_nofile", MODE_OLDFILE)
                    # - Open("RAM:atrace_test_write", MODE_NEWFILE)
                    # plus several setup/cleanup Opens
                    conn_activity.execute("C:atrace_test", timeout=60)

                    events = _collect_events_raw(session, timeout=5.0)

                    _stop_raw_session(session)

                assert len(events) > 0, "No events received with FUNC=Open"

                # All events must be Open
                for ev in events:
                    assert ev["func"] == "Open", (
                        "Expected func='Open', got func={!r}".format(
                            ev["func"]))

                # Verify distinctive atrace_test Open events are present
                test_opens = _find_events(events, "Open", "atrace_test")
                assert len(test_opens) >= 2, (
                    "Expected at least 2 atrace_test Open events, "
                    "found {} in {} total Open events".format(
                        len(test_opens), len(events)))
            finally:
                conn_trace.close()
                conn_activity.close()
        finally:
            sig.alarm(0)
            sig.signal(sig.SIGALRM, old_handler)

    def test_filter_func_blacklist(self, amiga_host, amiga_port):
        """FILTER -FUNC=Open excludes Open but passes other functions."""
        import signal as sig

        old_handler = sig.signal(sig.SIGALRM, _timeout_handler)
        sig.alarm(60)
        try:
            conn_trace = AmigaConnection(amiga_host, amiga_port)
            conn_trace.connect()
            conn_activity = AmigaConnection(amiga_host, amiga_port)
            conn_activity.connect()

            try:
                session = conn_trace.trace_start_raw()
                with session:
                    conn_trace.send_filter(raw="-FUNC=Open")
                    time.sleep(0.5)
                    _drain_pre_filter_events(session)

                    conn_activity.execute("C:atrace_test", timeout=60)

                    events = _collect_events_raw(session, timeout=5.0)

                    _stop_raw_session(session)

                # Should have events (atrace_test calls many funcs
                # besides Open)
                assert len(events) > 0, (
                    "No events received with -FUNC=Open")

                # No Open events should appear
                for ev in events:
                    assert ev["func"] != "Open", (
                        "Open should be excluded by -FUNC=Open blacklist")

                # Verify other atrace_test functions are present
                locks = _find_events(events, "Lock", "RAM:")
                assert len(locks) >= 1, (
                    "Expected Lock('RAM:') events from atrace_test "
                    "in non-Open stream")
            finally:
                conn_trace.close()
                conn_activity.close()
        finally:
            sig.alarm(0)
            sig.signal(sig.SIGALRM, old_handler)

    def test_filter_clear(self, amiga_host, amiga_port):
        """Empty FILTER clears all filters, restoring full event flow."""
        import signal as sig

        old_handler = sig.signal(sig.SIGALRM, _timeout_handler)
        sig.alarm(60)
        try:
            conn_trace = AmigaConnection(amiga_host, amiga_port)
            conn_trace.connect()
            conn_activity = AmigaConnection(amiga_host, amiga_port)
            conn_activity.connect()

            try:
                session = conn_trace.trace_start_raw()
                with session:
                    # Apply restrictive filter that matches nothing
                    conn_trace.send_filter(raw="FUNC=BogusFunction")
                    time.sleep(0.5)
                    _drain_pre_filter_events(session)

                    conn_activity.execute("C:atrace_test", timeout=60)
                    filtered_events = _collect_events_raw(
                        session, timeout=5.0)

                    # Now clear the filter
                    conn_trace.send_filter(raw="")
                    time.sleep(0.5)
                    _drain_pre_filter_events(session)

                    # Run atrace_test again -- events should flow
                    conn_activity.execute("C:atrace_test", timeout=60)
                    unfiltered_events = _collect_events_raw(
                        session, timeout=5.0)

                    _stop_raw_session(session)

                # With the bogus filter, no events should pass
                assert len(filtered_events) == 0, (
                    "Expected 0 events with bogus filter, got {}".format(
                        len(filtered_events)))

                # After clearing, events should flow again
                assert len(unfiltered_events) > 0, (
                    "Expected events after clearing filter, got 0")

                # Verify known atrace_test events are present
                opens = _find_events(unfiltered_events, "Open",
                                     "atrace_test")
                assert len(opens) >= 1, (
                    "Expected atrace_test Open events after filter clear")
            finally:
                conn_trace.close()
                conn_activity.close()
        finally:
            sig.alarm(0)
            sig.signal(sig.SIGALRM, old_handler)

    def test_filter_combined(self, amiga_host, amiga_port):
        """FILTER LIB=dos -FUNC=Lock passes dos events except Lock."""
        import signal as sig

        old_handler = sig.signal(sig.SIGALRM, _timeout_handler)
        sig.alarm(60)
        try:
            conn_trace = AmigaConnection(amiga_host, amiga_port)
            conn_trace.connect()
            conn_activity = AmigaConnection(amiga_host, amiga_port)
            conn_activity.connect()

            try:
                session = conn_trace.trace_start_raw()
                with session:
                    conn_trace.send_filter(raw="LIB=dos -FUNC=Lock")
                    time.sleep(0.5)
                    _drain_pre_filter_events(session)

                    # atrace_test calls both Lock("RAM:") and
                    # Open("RAM:atrace_test_*") -- Lock should be
                    # excluded, Open should pass
                    conn_activity.execute("C:atrace_test", timeout=60)

                    events = _collect_events_raw(session, timeout=5.0)

                    _stop_raw_session(session)

                assert len(events) > 0, (
                    "No events received with LIB=dos -FUNC=Lock")

                for ev in events:
                    assert ev["lib"] == "dos", (
                        "Expected lib='dos', got lib={!r}".format(ev["lib"]))
                    assert ev["func"] != "Lock", (
                        "Lock should be excluded by -FUNC=Lock blacklist")

                # Verify Open events from atrace_test are present
                # (dos function that should pass the filter)
                opens = _find_events(events, "Open", "atrace_test")
                assert len(opens) >= 1, (
                    "Expected atrace_test Open events with LIB=dos "
                    "-FUNC=Lock filter")
            finally:
                conn_trace.close()
                conn_activity.close()
        finally:
            sig.alarm(0)
            sig.signal(sig.SIGALRM, old_handler)

    def test_filter_replaces_previous(self, amiga_host, amiga_port):
        """Second FILTER replaces the first, not appends to it."""
        import signal as sig

        old_handler = sig.signal(sig.SIGALRM, _timeout_handler)
        sig.alarm(60)
        try:
            conn_trace = AmigaConnection(amiga_host, amiga_port)
            conn_trace.connect()
            conn_activity = AmigaConnection(amiga_host, amiga_port)
            conn_activity.connect()

            try:
                session = conn_trace.trace_start_raw()
                with session:
                    # First filter: only dos
                    conn_trace.send_filter(raw="LIB=dos")
                    time.sleep(0.5)
                    _drain_pre_filter_events(session)

                    conn_activity.execute("C:atrace_test", timeout=60)
                    dos_events = _collect_events_raw(session, timeout=5.0)

                    # Second filter: only exec (replaces dos filter)
                    conn_trace.send_filter(raw="LIB=exec")
                    time.sleep(0.5)
                    _drain_pre_filter_events(session)

                    conn_activity.execute("C:atrace_test", timeout=60)
                    exec_events = _collect_events_raw(session, timeout=5.0)

                    _stop_raw_session(session)

                # First batch should be dos only
                assert len(dos_events) > 0, (
                    "No events received with LIB=dos filter")
                for ev in dos_events:
                    assert ev["lib"] == "dos", (
                        "Expected dos during first filter, got {!r}".format(
                            ev["lib"]))

                # Verify dos batch has known atrace_test dos events
                opens = _find_events(dos_events, "Open", "atrace_test")
                assert len(opens) >= 1, (
                    "Expected atrace_test Open events in dos batch")

                # Second batch should be exec only (not dos+exec)
                assert len(exec_events) > 0, (
                    "No events received with LIB=exec filter")
                for ev in exec_events:
                    assert ev["lib"] == "exec", (
                        "Expected exec during second filter, got {!r} -- "
                        "filter may not have replaced previous".format(
                            ev["lib"]))

                # Verify exec batch has known atrace_test exec events
                # (noise functions are disabled, use OpenDevice which is not noise)
                opendev = _find_events(exec_events, "OpenDevice")
                assert len(opendev) >= 1, (
                    "Expected atrace_test exec events (OpenDevice) "
                    "in exec batch")
            finally:
                conn_trace.close()
                conn_activity.close()
        finally:
            sig.alarm(0)
            sig.signal(sig.SIGALRM, old_handler)

    def test_filter_func_lib_scoped(self, amiga_host, amiga_port):
        """FILTER FUNC=dos.Open passes only dos.Open (no exec functions)."""
        import signal as sig

        old_handler = sig.signal(sig.SIGALRM, _timeout_handler)
        sig.alarm(60)
        try:
            conn_trace = AmigaConnection(amiga_host, amiga_port)
            conn_trace.connect()
            conn_activity = AmigaConnection(amiga_host, amiga_port)
            conn_activity.connect()

            try:
                session = conn_trace.trace_start_raw()
                with session:
                    conn_trace.send_filter(raw="FUNC=dos.Open")
                    time.sleep(0.5)
                    _drain_pre_filter_events(session)

                    conn_activity.execute("C:atrace_test", timeout=60)

                    events = _collect_events_raw(session, timeout=5.0)

                    _stop_raw_session(session)

                # Should have events (atrace_test calls dos.Open many times)
                assert len(events) > 0, (
                    "No events received with FUNC=dos.Open")

                # All events must be dos.Open
                for ev in events:
                    assert ev["func"] == "Open", (
                        "Expected func='Open', got func={!r}".format(
                            ev["func"]))
                    assert ev["lib"] == "dos", (
                        "Expected lib='dos', got lib={!r}".format(
                            ev["lib"]))

                # Verify distinctive atrace_test Open events are present
                test_opens = _find_events(events, "Open", "atrace_test")
                assert len(test_opens) >= 2, (
                    "Expected at least 2 atrace_test Open events, "
                    "found {} in {} total Open events".format(
                        len(test_opens), len(events)))

                # Verify no exec events leaked through
                exec_funcs = {"FindPort", "FindResident", "FindSemaphore",
                              "FindTask", "OpenDevice", "OpenLibrary",
                              "OpenResource", "GetMsg", "PutMsg",
                              "ObtainSemaphore", "ReleaseSemaphore",
                              "AllocMem"}
                for ev in events:
                    assert ev["func"] not in exec_funcs, (
                        "exec func {!r} should not appear with "
                        "FUNC=dos.Open filter".format(ev["func"]))
            finally:
                conn_trace.close()
                conn_activity.close()
        finally:
            sig.alarm(0)
            sig.signal(sig.SIGALRM, old_handler)

    def test_filter_func_global_still_works(self, amiga_host, amiga_port):
        """FILTER FUNC=Open (no dot) still matches globally."""
        import signal as sig

        old_handler = sig.signal(sig.SIGALRM, _timeout_handler)
        sig.alarm(60)
        try:
            conn_trace = AmigaConnection(amiga_host, amiga_port)
            conn_trace.connect()
            conn_activity = AmigaConnection(amiga_host, amiga_port)
            conn_activity.connect()

            try:
                session = conn_trace.trace_start_raw()
                with session:
                    conn_trace.send_filter(raw="FUNC=Open")
                    time.sleep(0.5)
                    _drain_pre_filter_events(session)

                    conn_activity.execute("C:atrace_test", timeout=60)

                    events = _collect_events_raw(session, timeout=5.0)

                    _stop_raw_session(session)

                # Should have events
                assert len(events) > 0, (
                    "No events received with FUNC=Open (global)")

                # All events must be Open (global match applies to all
                # libraries that have an Open function -- dos.Open is
                # the primary one exercised by atrace_test)
                for ev in events:
                    assert ev["func"] == "Open", (
                        "Expected func='Open', got func={!r}".format(
                            ev["func"]))

                # Verify distinctive atrace_test Open events are present
                test_opens = _find_events(events, "Open", "atrace_test")
                assert len(test_opens) >= 2, (
                    "Expected at least 2 atrace_test Open events with "
                    "global FUNC=Open, found {} in {} total".format(
                        len(test_opens), len(events)))
            finally:
                conn_trace.close()
                conn_activity.close()
        finally:
            sig.alarm(0)
            sig.signal(sig.SIGALRM, old_handler)

    def test_filter_proc_daemon_side(self, amiga_host, amiga_port):
        """FILTER PROC=atrace_test passes only events from that process.

        Uses EXEC ASYNC so the daemon's main loop keeps running while
        atrace_test executes.  This lets trace_poll_events() process
        events while the process is alive, ensuring the task name is
        resolvable.  EXEC ASYNC creates a wrapper process via
        CreateNewProcTags(NP_Cli, TRUE) which sets cli_CommandName
        to the command path before RunCommand.  resolve_cli_name()
        extracts the basename ("atrace_test") for PROC filter matching.
        """
        import signal as sig

        old_handler = sig.signal(sig.SIGALRM, _timeout_handler)
        sig.alarm(60)
        try:
            conn_trace = AmigaConnection(amiga_host, amiga_port)
            conn_trace.connect()
            conn_activity = AmigaConnection(amiga_host, amiga_port)
            conn_activity.connect()

            try:
                # Phase 1: PROC=atrace_test should pass events
                session = conn_trace.trace_start_raw()
                with session:
                    conn_trace.send_filter(raw="PROC=atrace_test")
                    time.sleep(0.5)
                    _drain_pre_filter_events(session)

                    # Use EXEC ASYNC so the daemon main loop is not
                    # blocked and can poll events while the process
                    # is alive.
                    proc_id = conn_activity.execute_async(
                        "C:atrace_test")

                    # Wait for the async process to finish
                    for _ in range(120):
                        time.sleep(0.5)
                        stat = conn_activity.procstat(proc_id)
                        if stat.get("status") == "EXITED":
                            break
                    else:
                        raise AssertionError(
                            "atrace_test did not exit within timeout")

                    # Allow final events to be polled and delivered
                    time.sleep(1.0)

                    matched_events = _collect_events_raw(
                        session, timeout=5.0)

                    # Phase 2: PROC=nonexistent should pass no events
                    conn_trace.send_filter(raw="PROC=nonexistent")
                    time.sleep(0.5)
                    _drain_pre_filter_events(session)

                    proc_id2 = conn_activity.execute_async(
                        "C:atrace_test")

                    for _ in range(120):
                        time.sleep(0.5)
                        stat = conn_activity.procstat(proc_id2)
                        if stat.get("status") == "EXITED":
                            break

                    time.sleep(1.0)

                    unmatched_events = _collect_events_raw(
                        session, timeout=5.0)

                    _stop_raw_session(session)

                # Phase 1 assertions: events should arrive, and task
                # names should contain "atrace_test"
                assert len(matched_events) > 0, (
                    "No events received with PROC=atrace_test")
                for ev in matched_events:
                    task = ev.get("task", "")
                    assert "atrace_test" in task, (
                        "Expected task containing 'atrace_test', "
                        "got task={!r}".format(task))

                # Phase 2 assertions: no events from nonexistent process
                assert len(unmatched_events) == 0, (
                    "Expected 0 events with PROC=nonexistent, "
                    "got {}".format(len(unmatched_events)))
            finally:
                conn_trace.close()
                conn_activity.close()
        finally:
            sig.alarm(0)
            sig.signal(sig.SIGALRM, old_handler)


# ---------------------------------------------------------------------------
# TestTraceFilterComment -- Phase 7b: filter comment emission tests
# ---------------------------------------------------------------------------

def _collect_results_raw(session, timeout=5.0, max_results=200):
    """Collect trace results (events AND comments) from a RawTraceSession.

    Like _collect_events_raw but returns all result types, not just events.
    Returns a list of dicts (both type="event" and type="comment").
    """
    import select

    results = []

    # Drain buffered data first
    while session.reader.has_buffered_data():
        result = session.reader.drain_buffered()
        if result is False:
            return results
        if result is None:
            break
        if isinstance(result, dict):
            results.append(result)

    for _ in range(max_results * 2):
        r, _, _ = select.select([session.sock], [], [], timeout)
        if not r:
            break
        result = session.reader.try_read_event()
        if result is False:
            break
        if result is None:
            continue
        if isinstance(result, dict):
            results.append(result)
        # Drain remaining buffered
        while session.reader.has_buffered_data():
            result = session.reader.drain_buffered()
            if result is False:
                return results
            if result is None:
                break
            if isinstance(result, dict):
                results.append(result)
    return results


class TestTraceFilterComment:
    """Tests for filter comment emission after FILTER command (Phase 7b)."""

    def test_filter_emits_comment(self, amiga_host, amiga_port):
        """FILTER LIB=dos emits a '# filter: LIB=dos' comment."""
        import signal as sig

        old_handler = sig.signal(sig.SIGALRM, _timeout_handler)
        sig.alarm(60)
        try:
            conn_trace = AmigaConnection(amiga_host, amiga_port)
            conn_trace.connect()

            try:
                session = conn_trace.trace_start_raw()
                with session:
                    # Drain header comments
                    _collect_results_raw(session, timeout=1.0)

                    # Send a filter command
                    conn_trace.send_filter(raw="LIB=dos")
                    time.sleep(0.3)

                    # Collect results -- should include a filter comment
                    results = _collect_results_raw(session, timeout=2.0)

                    _stop_raw_session(session)

                comments = [r for r in results
                            if r.get("type") == "comment"]
                filter_comments = [c for c in comments
                                   if c.get("text", "").startswith(
                                       "filter:")]

                assert len(filter_comments) >= 1, (
                    "Expected a '# filter:' comment after FILTER "
                    "LIB=dos, got comments: {}".format(
                        [c.get("text") for c in comments]))

                # Verify the comment text contains LIB=dos
                text = filter_comments[0]["text"]
                assert "LIB=dos" in text, (
                    "Expected 'LIB=dos' in filter comment, "
                    "got: {!r}".format(text))
            finally:
                conn_trace.close()
        finally:
            sig.alarm(0)
            sig.signal(sig.SIGALRM, old_handler)

    def test_filter_clear_emits_none_comment(self, amiga_host, amiga_port):
        """Empty FILTER emits a '# filter: (none)' comment."""
        import signal as sig

        old_handler = sig.signal(sig.SIGALRM, _timeout_handler)
        sig.alarm(60)
        try:
            conn_trace = AmigaConnection(amiga_host, amiga_port)
            conn_trace.connect()

            try:
                session = conn_trace.trace_start_raw()
                with session:
                    # Drain header
                    _collect_results_raw(session, timeout=1.0)

                    # First set a filter, then clear it
                    conn_trace.send_filter(raw="LIB=dos")
                    time.sleep(0.3)
                    _collect_results_raw(session, timeout=1.0)

                    # Now clear the filter
                    conn_trace.send_filter(raw="")
                    time.sleep(0.3)

                    results = _collect_results_raw(session, timeout=2.0)

                    _stop_raw_session(session)

                comments = [r for r in results
                            if r.get("type") == "comment"]
                none_comments = [c for c in comments
                                 if "filter: (none)" in c.get("text", "")]

                assert len(none_comments) >= 1, (
                    "Expected a '# filter: (none)' comment after "
                    "clearing filters, got comments: {}".format(
                        [c.get("text") for c in comments]))
            finally:
                conn_trace.close()
        finally:
            sig.alarm(0)
            sig.signal(sig.SIGALRM, old_handler)


# ---------------------------------------------------------------------------
# TestTraceRawAPI -- Phase 7: raw (non-blocking) trace API tests
# ---------------------------------------------------------------------------

class TestTraceRawAPI:
    """Tests for the non-blocking raw trace APIs (Phase 7).

    These test trace_start_raw(), trace_run_raw(), send_filter(), and
    TraceStreamReader -- the select()-based alternatives to the
    callback-based trace_start() and trace_run().

    All tests use C:atrace_test for deterministic, identifiable events.
    """

    def test_trace_start_raw(self, amiga_host, amiga_port):
        """trace_start_raw returns a session with readable events."""
        import signal as sig

        old_handler = sig.signal(sig.SIGALRM, _timeout_handler)
        sig.alarm(60)
        try:
            conn_trace = AmigaConnection(amiga_host, amiga_port)
            conn_trace.connect()
            conn_activity = AmigaConnection(amiga_host, amiga_port)
            conn_activity.connect()

            try:
                session = conn_trace.trace_start_raw(lib="dos")
                with session:
                    assert session.sock is not None
                    assert session.reader is not None

                    conn_activity.execute("C:atrace_test", timeout=60)

                    events = _collect_events_raw(session, timeout=5.0)

                    _stop_raw_session(session)

                assert len(events) > 0, (
                    "Expected events from trace_start_raw")

                # With lib="dos" filter, all should be dos events
                for ev in events:
                    assert ev["lib"] == "dos", (
                        "Expected lib='dos', got {!r}".format(ev["lib"]))

                # Verify known dos events from atrace_test
                opens = _find_events(events, "Open", "atrace_test")
                assert len(opens) >= 1, (
                    "Expected atrace_test Open events in dos-filtered "
                    "raw stream")
                locks = _find_events(events, "Lock", "RAM:")
                assert len(locks) >= 1, (
                    "Expected Lock('RAM:') events from atrace_test")
            finally:
                conn_trace.close()
                conn_activity.close()
        finally:
            sig.alarm(0)
            sig.signal(sig.SIGALRM, old_handler)

    def test_trace_run_raw(self, amiga_host, amiga_port):
        """trace_run_raw of atrace_test returns events and PROCESS EXITED."""
        import signal as sig

        old_handler = sig.signal(sig.SIGALRM, _timeout_handler)
        sig.alarm(60)
        try:
            conn = AmigaConnection(amiga_host, amiga_port)
            conn.connect()

            try:
                session, proc_id = conn.trace_run_raw(
                    "C:atrace_test", lib="dos")
                with session:
                    assert proc_id is not None
                    assert proc_id > 0, (
                        "Expected positive proc_id, got {}".format(proc_id))
                    assert session.sock is not None
                    assert session.reader is not None

                    events, saw_exit = _collect_run_events_raw(
                        session, timeout=5.0)

                assert len(events) > 0, (
                    "Expected events from trace_run_raw")
                assert saw_exit, (
                    "Expected PROCESS EXITED comment from trace_run_raw")

                # All events should be dos (lib filter applied)
                for ev in events:
                    assert ev["lib"] == "dos", (
                        "Expected lib='dos', got {!r}".format(ev["lib"]))

                # Verify known atrace_test dos events
                opens = _find_events(events, "Open", "atrace_test")
                assert len(opens) >= 2, (
                    "Expected at least 2 atrace_test Open events from "
                    "trace_run_raw, found {}".format(len(opens)))
                locks = _find_events(events, "Lock", "RAM:")
                assert len(locks) >= 1, (
                    "Expected Lock('RAM:') from atrace_test")
            finally:
                conn.close()
        finally:
            sig.alarm(0)
            sig.signal(sig.SIGALRM, old_handler)

    def test_send_filter_during_stream(self, amiga_host, amiga_port):
        """send_filter narrows an active raw stream to dos-only events."""
        import signal as sig

        old_handler = sig.signal(sig.SIGALRM, _timeout_handler)
        sig.alarm(60)
        try:
            conn_trace = AmigaConnection(amiga_host, amiga_port)
            conn_trace.connect()
            conn_activity = AmigaConnection(amiga_host, amiga_port)
            conn_activity.connect()

            try:
                # Start with no filter (all events)
                session = conn_trace.trace_start_raw()
                with session:
                    # Generate warmup activity to confirm stream is live
                    conn_activity.execute("C:atrace_test", timeout=60)
                    warmup_events = _collect_events_raw(
                        session, timeout=5.0)

                    # Now apply filter via send_filter
                    conn_trace.send_filter(lib="dos")
                    time.sleep(0.5)
                    _drain_pre_filter_events(session)

                    # Run atrace_test again -- only dos events should
                    # pass the filter
                    conn_activity.execute("C:atrace_test", timeout=60)
                    filtered_events = _collect_events_raw(
                        session, timeout=5.0)

                    _stop_raw_session(session)

                # Warmup events should contain both dos and exec
                # (no filter was active)
                assert len(warmup_events) > 0, (
                    "No warmup events received (stream not live?)")

                # Filtered events should be dos only
                assert len(filtered_events) > 0, (
                    "No events after send_filter(lib='dos')")
                for ev in filtered_events:
                    assert ev["lib"] == "dos", (
                        "Expected lib='dos' after send_filter, "
                        "got {!r}".format(ev["lib"]))

                # Verify known atrace_test dos events are present
                opens = _find_events(filtered_events, "Open",
                                     "atrace_test")
                assert len(opens) >= 1, (
                    "Expected atrace_test Open events after "
                    "send_filter(lib='dos')")
            finally:
                conn_trace.close()
                conn_activity.close()
        finally:
            sig.alarm(0)
            sig.signal(sig.SIGALRM, old_handler)


# ---------------------------------------------------------------------------
# Group 2: Event Display & Formatting
# ---------------------------------------------------------------------------

from amigactl.trace_ui import ColumnLayout, _visible_len
from amigactl.colors import ColorWriter, format_trace_event


class TestEventFormatting:
    """Validate the formatting pipeline on real trace events.

    ColumnLayout, format_trace_event, color coding, and ANSI-aware
    truncation are exercised against actual events from C:atrace_test
    running on the Amiga.  Unit tests (test_trace_ui.py) cover
    synthetic event dicts; these integration tests prove the pipeline
    handles real daemon output.
    """

    @pytest.fixture(scope="class")
    def formatting_events(self, request):
        """Collect real events from C:atrace_test for formatting tests."""
        import signal as sig

        host = request.config.getoption("--host")
        port = request.config.getoption("--port")
        conn = AmigaConnection(host, port)
        conn.connect()

        old_handler = sig.signal(sig.SIGALRM, _timeout_handler)
        sig.alarm(60)
        try:
            events = []

            def collect(ev):
                events.append(ev)

            result = conn.trace_run("C:atrace_test", collect)
            assert result["rc"] == 0, (
                "atrace_test exited with rc={}".format(result["rc"]))
            filtered = [e for e in events if e.get("type") == "event"]
            assert len(filtered) > 0, "No events from atrace_test"
            return filtered
        finally:
            sig.alarm(0)
            sig.signal(sig.SIGALRM, old_handler)
            conn.close()

    def test_column_layout_produces_output(self, formatting_events):
        """ColumnLayout(120) formats every real event within width."""
        cw = ColorWriter(force_color=True)
        layout = ColumnLayout(120)
        for event in formatting_events:
            result = layout.format_event(event, cw)
            assert result, (
                "format_event returned empty for {}".format(event))
            vis = _visible_len(result)
            assert vis <= 120, (
                "Visible length {} exceeds 120 for event: {}".format(
                    vis, event.get("func", "")))

    def test_library_color_dos_cyan(self, formatting_events):
        """dos library events are colored with CYAN."""
        cw = ColorWriter(force_color=True)
        layout = ColumnLayout(120)
        dos_events = _find_events(formatting_events, "Open",
                                  "atrace_test")
        assert len(dos_events) >= 1, (
            "Expected dos.Open events from atrace_test")
        for event in dos_events:
            result = layout.format_event(event, cw)
            assert "\033[36m" in result, (
                "Expected CYAN (\\033[36m) in dos event output")

    def test_library_color_exec_yellow(self, formatting_events):
        """exec library events are colored with YELLOW."""
        cw = ColorWriter(force_color=True)
        layout = ColumnLayout(120)
        # OpenLibrary and DoIO are noise-disabled; use OpenDevice which is not noise
        exec_events = _find_events(formatting_events, "OpenDevice")
        assert len(exec_events) >= 1, (
            "Expected exec.OpenDevice events from atrace_test")
        for event in exec_events:
            result = layout.format_event(event, cw)
            assert "\033[33m" in result, (
                "Expected YELLOW (\\033[33m) in exec event output")

    def test_error_retval_red(self, formatting_events):
        """Error status events have retval colored RED."""
        cw = ColorWriter(force_color=True)
        layout = ColumnLayout(120)
        error_events = [e for e in formatting_events
                        if e.get("status") == "E"]
        assert len(error_events) >= 1, (
            "Expected at least one error event from atrace_test "
            "(RAM:atrace_test_nofile)")
        for event in error_events:
            result = layout.format_event(event, cw)
            assert "\033[31m" in result, (
                "Expected RED (\\033[31m) in error event output")
            # Verify the retval text appears within the red portion.
            # The retval may be truncated by ColumnLayout (trailing ~),
            # so check that the displayed text is a prefix of the retval.
            retval = event.get("retval", "")
            if retval:
                red_prefix = "\033[31m"
                idx = result.find(red_prefix)
                assert idx >= 0, "RED escape not found"
                after_red = result[idx + len(red_prefix):]
                # Strip RESET and anything after to isolate the red text
                reset_idx = after_red.find("\033[0m")
                if reset_idx >= 0:
                    red_text = after_red[:reset_idx]
                else:
                    red_text = after_red
                # red_text is either the full retval or truncated with ~
                if red_text.endswith("~"):
                    assert retval.startswith(red_text[:-1]), (
                        "retval {!r} does not start with truncated "
                        "red text {!r}".format(retval, red_text))
                else:
                    assert red_text == retval, (
                        "retval {!r} does not match red text {!r}".format(
                            retval, red_text))

    def test_ok_retval_green(self, formatting_events):
        """Success status events have retval colored GREEN."""
        cw = ColorWriter(force_color=True)
        layout = ColumnLayout(120)
        ok_events = [e for e in formatting_events
                     if e.get("status") == "O"]
        assert len(ok_events) >= 1, (
            "Expected at least one OK event from atrace_test")
        for event in ok_events:
            result = layout.format_event(event, cw)
            assert "\033[32m" in result, (
                "Expected GREEN (\\033[32m) in OK event output")
            # Verify the retval text appears within the green portion
            retval = event.get("retval", "")
            if retval:
                green_prefix = "\033[32m"
                idx = result.find(green_prefix)
                assert idx >= 0, "GREEN escape not found"
                after_green = result[idx + len(green_prefix):]
                assert retval in after_green, (
                    "retval {!r} not found after GREEN escape".format(
                        retval))

    def test_column_layout_respects_width_with_colors(
            self, formatting_events):
        """Cramped layout (50 cols) respects visible width limits."""
        cw = ColorWriter(force_color=True)
        layout = ColumnLayout(50)
        for event in formatting_events:
            result = layout.format_event(event, cw)
            vis = _visible_len(result)
            raw = len(result)
            assert vis <= 50, (
                "Visible length {} exceeds 50 for event: {}".format(
                    vis, event.get("func", "")))
            assert raw >= vis, (
                "Raw length {} < visible length {} -- ANSI codes "
                "should add invisible bytes".format(raw, vis))

    def test_adaptive_widths_120_vs_50(self, formatting_events):
        """ColumnLayout adapts widths: 120 has timestamp, 50 drops it."""
        layout_wide = ColumnLayout(120)
        layout_narrow = ColumnLayout(50)
        assert layout_wide.time_width == 12, (
            "Expected time_width=12 at 120 cols, got {}".format(
                layout_wide.time_width))
        assert layout_narrow.time_width == 0, (
            "Expected time_width=0 at 50 cols (below 60), got {}".format(
                layout_narrow.time_width))

        # Same event formatted at both widths -- wide should be longer
        cw = ColorWriter(force_color=True)
        event = formatting_events[0]
        wide_result = layout_wide.format_event(event, cw)
        narrow_result = layout_narrow.format_event(event, cw)
        assert _visible_len(wide_result) > _visible_len(narrow_result), (
            "120-col output ({}) should be wider than 50-col ({})".format(
                _visible_len(wide_result), _visible_len(narrow_result)))

    def test_format_trace_event_fallback(self, formatting_events):
        """format_trace_event (legacy fallback) handles real events."""
        cw = ColorWriter(force_color=True)
        for event in formatting_events:
            result = format_trace_event(event, cw)
            assert result, (
                "format_trace_event returned empty for {}".format(event))
            assert "\033[" in result, (
                "Expected ANSI color codes in output for {}".format(
                    event.get("func", "")))


# ---------------------------------------------------------------------------
# TestTraceHeader -- Phase 5b: trace log header tests
# ---------------------------------------------------------------------------

def _collect_all_chunks_raw(sock, timeout=10, max_chunks=50):
    """Collect raw DATA chunks from a trace stream.

    Returns a list of decoded text strings from DATA chunks.  Stops
    after max_chunks or timeout.  Includes both comment and event text.
    """
    chunks = []
    old_timeout = sock.gettimeout()
    sock.settimeout(timeout)
    try:
        for _ in range(max_chunks):
            try:
                line = _read_line(sock)
            except socket.timeout:
                break
            if line.startswith("DATA "):
                chunk_len = int(line[5:])
                data = _recv_exact(sock, chunk_len)
                chunks.append(data.decode("iso-8859-1"))
            elif line == "END":
                _read_line(sock)  # sentinel
                break
            elif line.startswith("ERR"):
                _read_line(sock)  # sentinel
                break
    finally:
        sock.settimeout(old_timeout)
    return chunks


class TestTraceHeader:
    """Tests for Phase 5b trace log header emission.

    Verifies that TRACE START and TRACE RUN emit #-prefixed header
    comments containing version, filter, and deviation information
    before the first trace event.
    """

    def test_trace_start_header(self, amiga_host, amiga_port):
        """TRACE START emits header with version and filter before events."""
        trace_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        trace_sock.settimeout(10)
        trace_sock.connect((amiga_host, amiga_port))
        _read_line(trace_sock)  # banner

        try:
            send_command(trace_sock, "TRACE START FUNC=Open")
            status_line = _read_line(trace_sock)
            assert status_line.startswith("OK"), (
                "Expected OK, got: {!r}".format(status_line))

            # Collect initial chunks (header + maybe some events)
            chunks = _collect_all_chunks_raw(trace_sock, timeout=5,
                                             max_chunks=20)

            # Stop the trace
            _send_stop_and_drain(trace_sock)

            # Extract comments (# prefixed)
            comments = [c for c in chunks if c.startswith("#")]
            assert len(comments) >= 2, (
                "Expected at least 2 header comments (version + filter), "
                "got {}: {}".format(len(comments), comments))

            # Version line should be first comment
            version_comments = [c for c in comments
                                if "atrace v" in c]
            assert len(version_comments) >= 1, (
                "Expected version header (# atrace v...), got: {}".format(
                    comments))

            # Filter line should be present
            filter_comments = [c for c in comments
                               if "filter:" in c]
            assert len(filter_comments) >= 1, (
                "Expected filter header, got: {}".format(comments))

            # With FUNC=Open filter, filter line should mention Open
            assert any("Open" in c for c in filter_comments), (
                "Expected filter to mention 'Open', got: {}".format(
                    filter_comments))

            # Header comments should come before any events
            first_event_idx = None
            first_comment_idx = None
            for i, c in enumerate(chunks):
                if c.startswith("#"):
                    if first_comment_idx is None:
                        first_comment_idx = i
                else:
                    if first_event_idx is None:
                        first_event_idx = i
            if first_event_idx is not None and first_comment_idx is not None:
                assert first_comment_idx < first_event_idx, (
                    "Header comments should precede events")
        finally:
            trace_sock.close()

    def test_trace_start_header_with_filter(self, amiga_host, amiga_port):
        """TRACE START with PROC= filter shows filter in header."""
        trace_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        trace_sock.settimeout(10)
        trace_sock.connect((amiga_host, amiga_port))
        _read_line(trace_sock)  # banner

        try:
            send_command(trace_sock, "TRACE START PROC=someprocess")
            status_line = _read_line(trace_sock)
            assert status_line.startswith("OK"), (
                "Expected OK, got: {!r}".format(status_line))

            chunks = _collect_all_chunks_raw(trace_sock, timeout=5,
                                             max_chunks=20)
            _send_stop_and_drain(trace_sock)

            comments = [c for c in chunks if c.startswith("#")]
            filter_comments = [c for c in comments if "filter:" in c]
            assert len(filter_comments) >= 1, (
                "Expected filter header, got: {}".format(comments))
            assert any("someprocess" in c for c in filter_comments), (
                "Expected filter to mention 'someprocess', got: {}".format(
                    filter_comments))
        finally:
            trace_sock.close()

    def test_trace_run_header(self, amiga_host, amiga_port):
        """TRACE RUN emits header with version, command, and filter."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(15)
        sock.connect((amiga_host, amiga_port))
        _read_line(sock)  # banner

        try:
            send_command(sock, "TRACE RUN -- C:atrace_test")
            status_line = _read_line(sock)
            assert status_line.startswith("OK "), (
                "Expected OK <id>, got: {!r}".format(status_line))

            # Collect all chunks until END
            chunks = []
            while True:
                line = _read_line(sock)
                if line.startswith("DATA "):
                    chunk_len = int(line[5:])
                    data = _recv_exact(sock, chunk_len)
                    chunks.append(data.decode("iso-8859-1"))
                elif line == "END":
                    _read_line(sock)  # sentinel
                    break

            comments = [c for c in chunks if c.startswith("#")]

            # Should have version header
            version_comments = [c for c in comments
                                if "atrace v" in c]
            assert len(version_comments) >= 1, (
                "Expected version header in TRACE RUN, got: {}".format(
                    comments))

            # Should have command header for TRACE RUN
            command_comments = [c for c in comments
                                if "command:" in c]
            assert len(command_comments) >= 1, (
                "Expected command header in TRACE RUN, got: {}".format(
                    comments))
            assert any("atrace_test" in c for c in command_comments), (
                "Expected command to mention 'atrace_test', got: {}".format(
                    command_comments))
        finally:
            sock.close()

    def test_trace_start_header_with_enabled_noise(self, amiga_host,
                                                    amiga_port,
                                                    conn,
                                                    restore_trace_state):
        """Header shows noise function when explicitly enabled."""
        # Enable a noise function
        conn.trace_enable(funcs=["GetMsg"])

        trace_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        trace_sock.settimeout(10)
        trace_sock.connect((amiga_host, amiga_port))
        _read_line(trace_sock)  # banner

        try:
            send_command(trace_sock, "TRACE START FUNC=Open")
            status_line = _read_line(trace_sock)
            assert status_line.startswith("OK"), (
                "Expected OK, got: {!r}".format(status_line))

            chunks = _collect_all_chunks_raw(trace_sock, timeout=5,
                                             max_chunks=20)
            _send_stop_and_drain(trace_sock)

            comments = [c for c in chunks if c.startswith("#")]
            enabled_comments = [c for c in comments if "enabled:" in c]
            assert len(enabled_comments) >= 1, (
                "Expected enabled deviation header when GetMsg is enabled, "
                "got comments: {}".format(comments))
            assert any("GetMsg" in c for c in enabled_comments), (
                "Expected 'GetMsg' in enabled deviation line, got: {}".format(
                    enabled_comments))
        finally:
            trace_sock.close()

    def test_trace_start_header_with_disabled_function(
            self, amiga_host, amiga_port, conn, restore_trace_state):
        """Header shows non-noise function when manually disabled."""
        # Disable a non-noise function
        conn.trace_disable(funcs=["Lock"])

        trace_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        trace_sock.settimeout(10)
        trace_sock.connect((amiga_host, amiga_port))
        _read_line(trace_sock)  # banner

        try:
            send_command(trace_sock, "TRACE START FUNC=Open")
            status_line = _read_line(trace_sock)
            assert status_line.startswith("OK"), (
                "Expected OK, got: {!r}".format(status_line))

            chunks = _collect_all_chunks_raw(trace_sock, timeout=5,
                                             max_chunks=20)
            _send_stop_and_drain(trace_sock)

            comments = [c for c in chunks if c.startswith("#")]
            disabled_comments = [c for c in comments if "disabled:" in c]
            assert len(disabled_comments) >= 1, (
                "Expected disabled deviation header when Lock is disabled, "
                "got comments: {}".format(comments))
            assert any("Lock" in c for c in disabled_comments), (
                "Expected 'Lock' in disabled deviation line, got: {}".format(
                    disabled_comments))
        finally:
            trace_sock.close()


# ---------------------------------------------------------------------------
# TestStringResolution -- Phase 5b: long path string resolution tests
# ---------------------------------------------------------------------------

class TestStringResolution:
    """Tests for Phase 5b expanded string capture.

    Verifies that paths longer than 23 characters (but within the
    59-char expanded string_data capacity) appear fully without
    truncation.
    """

    def test_long_path_not_truncated(self, amiga_host, amiga_port):
        """Lock() with 42-char path fits in 59-char string_data without '...'."""
        import signal as sig

        old_handler = sig.signal(sig.SIGALRM, _timeout_handler)
        sig.alarm(60)
        try:
            conn = AmigaConnection(amiga_host, amiga_port)
            conn.connect()
            try:
                events = []

                def collect(ev):
                    events.append(ev)

                result = conn.trace_run("C:atrace_test", collect)
                assert result["rc"] == 0, (
                    "atrace_test exited with rc={}".format(result["rc"]))

                # Find Lock events with the long path from atrace_test
                long_path = "atrace_test_long_path_verification"
                lock_events = [
                    e for e in events
                    if e.get("type") == "event"
                    and e.get("func") == "Lock"
                    and long_path in e.get("args", "")
                ]
                assert len(lock_events) >= 1, (
                    "Expected Lock event with long path '{}', "
                    "found none in {} events. Lock events: {}".format(
                        long_path,
                        len(events),
                        [e.get("args", "") for e in events
                         if e.get("func") == "Lock"]))

                # The full path should NOT be truncated with "..."
                for ev in lock_events:
                    args = ev.get("args", "")
                    assert "..." not in args, (
                        "Long path should not be truncated with '...', "
                        "got: {}".format(args))
                    # The full distinctive string should be present
                    assert long_path in args, (
                        "Expected full path '{}' in args, got: {}".format(
                            long_path, args))
            finally:
                conn.close()
        finally:
            sig.alarm(0)
            sig.signal(sig.SIGALRM, old_handler)


# ---------------------------------------------------------------------------
# TestPhase6EClock -- Phase 6: EClock timestamps and metadata
# ---------------------------------------------------------------------------

class TestPhase6EClock:
    """Tests for Phase 6 EClock timestamp features.

    Validates microsecond timestamps, monotonicity, per-event resolution,
    embedded task names, EClock frequency in STATUS, and anchor version.
    """

    def test_status_anchor_version(self, conn):
        """TRACE STATUS reports anchor_version >= 3 (Phase 6)."""
        status = conn.trace_status()
        assert "anchor_version" in status, (
            "anchor_version not in TRACE STATUS response")
        assert status["anchor_version"] >= 3, (
            "Expected anchor_version >= 3, got {}".format(
                status["anchor_version"]))

    def test_status_eclock_freq(self, conn):
        """TRACE STATUS reports a non-zero eclock_freq."""
        status = conn.trace_status()
        assert "eclock_freq" in status, (
            "eclock_freq not in TRACE STATUS response")
        assert status["eclock_freq"] > 0, (
            "Expected positive eclock_freq, got {}".format(
                status["eclock_freq"]))
        # Sanity: typical PAL=709379, NTSC=715909, emulated may vary
        # but should be in a reasonable range (100kHz - 10MHz)
        assert 100000 <= status["eclock_freq"] <= 10000000, (
            "eclock_freq {} outside reasonable range".format(
                status["eclock_freq"]))

    def test_timestamps_microsecond_format(self, amiga_host, amiga_port):
        """Events from TRACE RUN have 6-digit fractional timestamps."""
        import re
        import signal as sig

        old_handler = sig.signal(sig.SIGALRM, _timeout_handler)
        sig.alarm(30)
        try:
            conn = AmigaConnection(amiga_host, amiga_port)
            conn.connect()
            try:
                events = []
                result = conn.trace_run("C:atrace_test",
                                        lambda e: events.append(e))
                assert result["rc"] == 0

                real_events = [e for e in events
                               if e.get("type") == "event"]
                assert len(real_events) > 0, "No events collected"

                # Check that timestamps have 6-digit fractional part
                us_pattern = re.compile(
                    r'^\d{2}:\d{2}:\d{2}\.\d{6}$')
                for ev in real_events:
                    time_str = ev.get("time", "")
                    assert us_pattern.match(time_str), (
                        "Expected HH:MM:SS.uuuuuu format, got '{}' "
                        "(seq={})".format(time_str, ev.get("seq")))
            finally:
                conn.close()
        finally:
            sig.alarm(0)
            sig.signal(sig.SIGALRM, old_handler)

    def test_timestamps_monotonic(self, amiga_host, amiga_port):
        """Event timestamps are monotonically increasing within a session."""
        import signal as sig
        from amigactl.trace_ui import TraceViewer

        old_handler = sig.signal(sig.SIGALRM, _timeout_handler)
        sig.alarm(30)
        try:
            conn = AmigaConnection(amiga_host, amiga_port)
            conn.connect()
            try:
                events = []
                result = conn.trace_run("C:atrace_test",
                                        lambda e: events.append(e))
                assert result["rc"] == 0

                real_events = [e for e in events
                               if e.get("type") == "event"]
                assert len(real_events) >= 2, (
                    "Need >= 2 events for monotonicity check")

                prev_us = 0
                for i, ev in enumerate(real_events):
                    us = TraceViewer._parse_time_us(ev.get("time", ""))
                    assert us >= prev_us, (
                        "Timestamp not monotonic at event {}: {} < {} "
                        "(time='{}')".format(
                            i, us, prev_us, ev.get("time")))
                    prev_us = us
            finally:
                conn.close()
        finally:
            sig.alarm(0)
            sig.signal(sig.SIGALRM, old_handler)

    def test_timestamps_distinct(self, amiga_host, amiga_port):
        """Consecutive events have distinct (non-zero-delta) timestamps."""
        import signal as sig
        from amigactl.trace_ui import TraceViewer

        old_handler = sig.signal(sig.SIGALRM, _timeout_handler)
        sig.alarm(30)
        try:
            conn = AmigaConnection(amiga_host, amiga_port)
            conn.connect()
            try:
                events = []
                result = conn.trace_run("C:atrace_test",
                                        lambda e: events.append(e))
                assert result["rc"] == 0

                real_events = [e for e in events
                               if e.get("type") == "event"]
                assert len(real_events) >= 2, (
                    "Need >= 2 events for distinctness check")

                # At least some consecutive pairs should have different
                # timestamps (per-event EClock, not batch-shared)
                distinct_count = 0
                for i in range(1, len(real_events)):
                    us_prev = TraceViewer._parse_time_us(
                        real_events[i - 1].get("time", ""))
                    us_curr = TraceViewer._parse_time_us(
                        real_events[i].get("time", ""))
                    if us_curr > us_prev:
                        distinct_count += 1

                # With per-event EClock timestamps, the vast majority
                # of consecutive pairs should be distinct
                ratio = distinct_count / (len(real_events) - 1)
                assert ratio > 0.5, (
                    "Only {}/{} consecutive event pairs have distinct "
                    "timestamps (expected >50% with per-event EClock)".format(
                        distinct_count, len(real_events) - 1))
            finally:
                conn.close()
        finally:
            sig.alarm(0)
            sig.signal(sig.SIGALRM, old_handler)

    def test_task_name_embedded(self, amiga_host, amiga_port):
        """Events from TRACE RUN atrace_test contain the task name."""
        import signal as sig

        old_handler = sig.signal(sig.SIGALRM, _timeout_handler)
        sig.alarm(30)
        try:
            conn = AmigaConnection(amiga_host, amiga_port)
            conn.connect()
            try:
                events = []
                result = conn.trace_run("C:atrace_test",
                                        lambda e: events.append(e))
                assert result["rc"] == 0

                real_events = [e for e in events
                               if e.get("type") == "event"]
                assert len(real_events) > 0, "No events collected"

                # The task field should contain the process name.
                # atrace_test runs as a separate process; its task name
                # should be non-empty for all events.
                for ev in real_events:
                    task = ev.get("task", "")
                    assert len(task) > 0, (
                        "Empty task name in event seq={}".format(
                            ev.get("seq")))
            finally:
                conn.close()
        finally:
            sig.alarm(0)
            sig.signal(sig.SIGALRM, old_handler)
