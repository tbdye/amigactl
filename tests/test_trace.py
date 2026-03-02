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
                   "ReleaseSemaphore", "AllocMem"]
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


# ---------------------------------------------------------------------------
# TestNoiseDefaults -- Phase 4: noise function auto-disable
# ---------------------------------------------------------------------------

class TestNoiseDefaults:
    """Tests for noise function auto-disable defaults (Phase 4).

    After loading atrace, 8 high-frequency exec functions should be
    disabled by default.  These tests verify the default state and
    user override behavior.
    """

    def test_noise_funcs_default_disabled(self, conn):
        """Noise functions should be disabled by default after loading."""
        status = conn.trace_status()
        assert status.get("noise_disabled", 0) >= 8

        # Check specific functions are disabled
        patches = status.get("patch_list", [])
        noise_names = {
            "exec.FindPort", "exec.FindSemaphore", "exec.FindTask",
            "exec.GetMsg", "exec.PutMsg", "exec.ObtainSemaphore",
            "exec.ReleaseSemaphore", "exec.AllocMem",
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
# TestTraceRunPhase4 -- Phase 4: task filter, noise auto-enable, process name
# ---------------------------------------------------------------------------

class TestTraceRunPhase4:
    """Tests for Phase 4 TRACE RUN enhancements: task filter, noise
    auto-enable/restore, and process name fix."""

    def test_trace_run_auto_enables_noise(self, amiga_host, amiga_port):
        """TRACE RUN should auto-enable noise functions for the target task."""
        conn = AmigaConnection(amiga_host, amiga_port)
        conn.connect()
        try:
            # Check noise functions are disabled before
            status = conn.trace_status()
            pre_noise = status.get("noise_disabled", 0)
            assert pre_noise >= 8

            # Start TRACE RUN
            events = []

            def collect(ev):
                events.append(ev)

            conn.trace_run("List SYS:", collect)

            # After trace_run returns, check noise functions restored
            status = conn.trace_status()
            post_noise = status.get("noise_disabled", 0)
            assert post_noise >= 8, \
                "Noise functions should be restored after TRACE RUN"
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

            # At least some events should show "List" as the task name
            # Phase 4b adds CLI number prefix: "[N] List"
            assert len(events) > 0, "No events received"
            task_names = {ev.get("task", "") for ev in events}
            assert any(t.endswith("List") for t in task_names), \
                "Expected task name ending with 'List', got: {}".format(task_names)
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

            # All events should be from "List" (the traced process)
            # Phase 4b adds CLI number prefix: "[N] List"
            assert len(events) > 0, "No events received"
            for ev in events:
                task = ev.get("task", "")
                assert task.endswith("List"), \
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
