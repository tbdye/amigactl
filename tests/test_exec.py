"""Phase 3 execution and process management tests for amigactld.

These tests exercise the EXEC (sync and async), PROCLIST, PROCSTAT,
SIGNAL, and KILL commands against a live amigactld daemon.

EXEC sync uses DATA/END binary framing (same as READ) with an ``rc=N``
info field instead of a byte count. The ``read_exec_response()`` helper
from conftest.py handles this format.

EXEC ASYNC launches processes tracked by the daemon. PROCLIST, PROCSTAT,
SIGNAL, and KILL operate on these tracked processes.

The daemon must be running on the target machine before these tests are
executed.
"""

import re
import socket
import time

import pytest

from conftest import (
    _read_line,
    read_exec_response,
    read_response,
    send_command,
)


# ---------------------------------------------------------------------------
# EXEC (Synchronous)
# ---------------------------------------------------------------------------

class TestExecSync:
    """Tests for the synchronous EXEC command."""

    def test_exec_simple(self, raw_connection):
        """EXEC echo hello returns OK rc=0 with output containing 'hello'.
        COMMANDS.md: 'The OK status line includes rc=<N> where N is the
        AmigaOS return code from the command.'"""
        sock, _banner = raw_connection
        send_command(sock, "EXEC echo hello")
        rc, data = read_exec_response(sock)
        assert rc == 0
        output = data.decode("iso-8859-1")
        assert "hello" in output

    def test_exec_multiline_output(self, raw_connection):
        """EXEC list SYS:S returns OK rc=0 with multi-line output.
        COMMANDS.md: captured output follows using DATA/END chunked binary
        framing."""
        sock, _banner = raw_connection
        send_command(sock, "EXEC list SYS:S")
        rc, data = read_exec_response(sock)
        assert rc == 0
        output = data.decode("iso-8859-1")
        lines = output.strip().splitlines()
        assert len(lines) > 1, (
            "Expected multiple lines of output from 'list SYS:S', "
            "got {}".format(len(lines))
        )

    def test_exec_nonzero_rc(self, raw_connection):
        """EXEC a command that returns a non-zero rc.
        COMMANDS.md: 'A command that runs but returns a non-zero return code
        is NOT an error from the daemon's perspective.'  Uses 'search'
        which returns rc=5 (WARN) when no match is found."""
        sock, _banner = raw_connection
        send_command(sock, "EXEC search SYS:S/Startup-Sequence amigactl_nonexistent_pattern_xyz")
        rc, data = read_exec_response(sock)
        assert rc != 0, (
            "Expected non-zero rc for search with no match, got {}".format(rc)
        )

    def test_exec_nonexistent_command(self, raw_connection):
        """EXEC with a nonexistent command returns OK with a high rc.
        COMMANDS.md: 'A command that does not exist does NOT produce an ERR
        response. AmigaOS returns a non-zero rc (typically 20).'"""
        sock, _banner = raw_connection
        send_command(sock, "EXEC nonexistent_amigactl_xyz")
        rc, data = read_exec_response(sock)
        assert rc > 0, (
            "Expected non-zero rc for nonexistent command, got {}".format(rc)
        )

    def test_exec_empty_output(self, raw_connection):
        """EXEC a command that produces no output still returns OK rc=0.
        COMMANDS.md: 'If the command produces no output, the response
        contains no DATA chunks.'"""
        sock, _banner = raw_connection
        # 'cd SYS:' changes the working directory and produces no output
        send_command(sock, "EXEC cd SYS:")
        rc, data = read_exec_response(sock)
        assert rc == 0
        assert data == b"" or data.strip() == b""

    def test_exec_serialization(self, amiga_host, amiga_port):
        """Two simultaneous EXEC commands complete sequentially.
        COMMANDS.md: 'This blocks the daemon's event loop -- all other
        clients are blocked until the command completes.'"""
        # Open two independent connections
        sock1 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock1.settimeout(15)
        sock1.connect((amiga_host, amiga_port))
        _read_line(sock1)  # banner

        sock2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock2.settimeout(15)
        sock2.connect((amiga_host, amiga_port))
        _read_line(sock2)  # banner

        try:
            # Send EXEC on sock1 first.  Brief sleep gives the daemon time
            # to read and begin executing 'wait 2' (which blocks the event
            # loop) before sock2's command arrives.  This is inherently
            # timing-dependent -- there is no daemon-side synchronization
            # primitive to confirm the first command has started processing.
            send_command(sock1, "EXEC wait 2")
            time.sleep(0.5)
            send_command(sock2, "EXEC echo done")

            # Read response from sock1 (wait 2 should finish first since
            # it was sent first and blocks the event loop)
            rc1, data1 = read_exec_response(sock1)
            assert rc1 == 0, (
                "First EXEC (wait 2) failed with rc={}".format(rc1)
            )

            # Read response from sock2 (echo done runs after wait 2)
            rc2, data2 = read_exec_response(sock2)
            assert rc2 == 0, (
                "Second EXEC (echo done) failed with rc={}".format(rc2)
            )
            output2 = data2.decode("iso-8859-1")
            assert "done" in output2
        finally:
            sock1.close()
            sock2.close()

    def test_exec_response_format(self, raw_connection):
        """EXEC response has OK rc=N status line, DATA chunks, END, and
        sentinel.  COMMANDS.md: 'The OK status line includes rc=<N>'."""
        sock, _banner = raw_connection
        send_command(sock, "EXEC echo format_test")

        # Read status line manually to verify format
        status_line = _read_line(sock)
        assert status_line.startswith("OK rc="), (
            "Expected 'OK rc=N', got: {!r}".format(status_line)
        )
        # Verify rc is a valid integer
        info = status_line[3:].strip()
        match = re.match(r"^rc=(-?\d+)$", info)
        assert match, (
            "Info field should match rc=N, got: {!r}".format(info)
        )

        # Read DATA/END chunks
        saw_data = False
        while True:
            line = _read_line(sock)
            if line == "END":
                break
            assert line.startswith("DATA "), (
                "Expected DATA or END, got: {!r}".format(line)
            )
            chunk_len = int(line[5:])
            buf = bytearray()
            while len(buf) < chunk_len:
                chunk = sock.recv(chunk_len - len(buf))
                assert chunk, "EOF during DATA chunk"
                buf.extend(chunk)
            saw_data = True

        # Read sentinel
        sentinel = _read_line(sock)
        assert sentinel == ".", (
            "Expected sentinel, got: {!r}".format(sentinel)
        )

    def test_exec_cd(self, raw_connection):
        """EXEC CD=SYS:S with 'list' lists the contents of SYS:S.
        COMMANDS.md: 'CD=<path> is an optional prefix that sets the working
        directory for the executed command.'"""
        sock, _banner = raw_connection
        send_command(sock, "EXEC CD=SYS:S list")
        rc, data = read_exec_response(sock)
        assert rc == 0
        output = data.decode("iso-8859-1")
        # SYS:S should contain Startup-Sequence
        assert len(output) > 0, "Expected non-empty listing from SYS:S"

    def test_exec_cd_nonexistent(self, raw_connection):
        """EXEC CD= with nonexistent path returns ERR 200.
        COMMANDS.md: 'CD= path not found -> ERR 200 Directory not found'."""
        sock, _banner = raw_connection
        send_command(sock, "EXEC CD=RAM:nonexistent_amigactl_test echo hello")
        status, payload = read_response(sock)
        assert status.startswith("ERR 200"), (
            "Expected ERR 200, got: {!r}".format(status)
        )
        assert payload == []

    def test_exec_cd_persistent(self, raw_connection):
        """EXEC CD= does not change the daemon's own working directory.
        COMMANDS.md: 'The daemon's own current directory is saved before
        the command and restored afterward.'"""
        sock, _banner = raw_connection

        # Run a baseline EXEC without CD= to capture the daemon's default
        # working directory.  AmigaOS 'cd' with no arguments prints the
        # current directory path to stdout.
        send_command(sock, "EXEC cd")
        rc1, data1 = read_exec_response(sock)
        assert rc1 == 0
        baseline = data1.decode("iso-8859-1").strip()

        # Run EXEC with CD=SYS:S to change the working directory
        send_command(sock, "EXEC CD=SYS:S cd")
        rc2, data2 = read_exec_response(sock)
        assert rc2 == 0

        # Run another EXEC without CD= -- should return the same baseline
        send_command(sock, "EXEC cd")
        rc3, data3 = read_exec_response(sock)
        assert rc3 == 0
        after = data3.decode("iso-8859-1").strip()

        assert baseline == after, (
            "Daemon's working directory changed after CD= EXEC.\n"
            "Baseline: {!r}\nAfter CD=: {!r}".format(baseline, after)
        )

    def test_exec_missing_command(self, raw_connection):
        """EXEC with no command text returns ERR 100.
        COMMANDS.md: 'Missing command -> ERR 100 Missing command'."""
        sock, _banner = raw_connection
        send_command(sock, "EXEC")
        status, payload = read_response(sock)
        assert status == "ERR 100 Missing command"
        assert payload == []


# ---------------------------------------------------------------------------
# EXEC ASYNC and Process Management
# ---------------------------------------------------------------------------

class TestExecAsync:
    """Tests for EXEC ASYNC and process management commands."""

    def test_exec_async_launch(self, raw_connection):
        """EXEC ASYNC launches a process and returns a numeric ID.
        COMMANDS.md: 'The OK status line includes the daemon-assigned
        process ID (a monotonically incrementing integer).'"""
        sock, _banner = raw_connection
        send_command(sock, "EXEC ASYNC echo done")
        status, payload = read_response(sock)
        assert status.startswith("OK"), (
            "Expected OK, got: {!r}".format(status)
        )
        # Extract process ID from OK line
        proc_id = status[3:].strip()
        assert proc_id.isdigit(), (
            "Expected numeric process ID, got: {!r}".format(proc_id)
        )
        assert int(proc_id) >= 1
        assert payload == []

    def test_proclist_shows_process(self, raw_connection):
        """After EXEC ASYNC, PROCLIST includes the launched process.
        COMMANDS.md: PROCLIST lists all daemon-launched asynchronous
        processes."""
        sock, _banner = raw_connection

        # Launch a long-running process so it is in RUNNING state
        send_command(sock, "EXEC ASYNC wait 5")
        status, _payload = read_response(sock)
        assert status.startswith("OK"), (
            "EXEC ASYNC failed: {!r}".format(status)
        )
        proc_id = status[3:].strip()

        # Give a moment for the process to register
        time.sleep(0.5)

        # PROCLIST should include the process
        send_command(sock, "PROCLIST")
        status, payload = read_response(sock)
        assert status == "OK"

        found = False
        for line in payload:
            fields = line.split("\t")
            if len(fields) >= 1 and fields[0] == proc_id:
                found = True
                assert len(fields) == 4, (
                    "Expected 4 fields, got {}".format(len(fields))
                )
                assert "wait" in fields[1], (
                    "Command should contain 'wait', got: {!r}".format(
                        fields[1])
                )
                assert fields[2] == "RUNNING", (
                    "Expected RUNNING status, got: {!r}".format(fields[2])
                )
                assert fields[3] == "-", (
                    "Expected '-' rc for RUNNING, got: {!r}".format(
                        fields[3])
                )
                break
        assert found, (
            "Process ID {} not found in PROCLIST. Payload: {!r}".format(
                proc_id, payload)
        )

        # Clean up: signal the wait process
        send_command(sock, "SIGNAL {}".format(proc_id))
        read_response(sock)

    def test_procstat_valid(self, raw_connection):
        """PROCSTAT for a valid process ID returns key=value pairs.
        COMMANDS.md: 'The payload consists of key=value lines in a fixed
        order: id, command, status, rc.'"""
        sock, _banner = raw_connection

        # Launch a process
        send_command(sock, "EXEC ASYNC wait 5")
        status, _payload = read_response(sock)
        assert status.startswith("OK")
        proc_id = status[3:].strip()

        time.sleep(0.5)

        # PROCSTAT should return key=value pairs
        send_command(sock, "PROCSTAT {}".format(proc_id))
        status, payload = read_response(sock)
        assert status == "OK"
        assert len(payload) == 4, (
            "Expected 4 payload lines, got {}".format(len(payload))
        )

        # Parse into dict
        kv = {}
        for line in payload:
            key, _, value = line.partition("=")
            kv[key] = value

        expected_keys = ["id", "command", "status", "rc"]
        actual_keys = [line.partition("=")[0] for line in payload]
        assert actual_keys == expected_keys, (
            "Keys must be in fixed order.\nExpected: {}\nActual: {}".format(
                expected_keys, actual_keys)
        )

        assert kv["id"] == proc_id
        assert kv["status"] in ("RUNNING", "EXITED")

        # Clean up
        send_command(sock, "SIGNAL {}".format(proc_id))
        read_response(sock)

    def test_procstat_invalid(self, raw_connection):
        """PROCSTAT for an invalid ID returns ERR 200.
        COMMANDS.md: 'Process not found -> ERR 200 Process not found'."""
        sock, _banner = raw_connection
        send_command(sock, "PROCSTAT 99999")
        status, payload = read_response(sock)
        assert status.startswith("ERR 200"), (
            "Expected ERR 200, got: {!r}".format(status)
        )
        assert payload == []

    def test_procstat_missing_id(self, raw_connection):
        """PROCSTAT with no ID returns ERR 100.
        COMMANDS.md: 'Missing process ID -> ERR 100 Missing process ID'."""
        sock, _banner = raw_connection
        send_command(sock, "PROCSTAT")
        status, payload = read_response(sock)
        assert status == "ERR 100 Missing process ID"
        assert payload == []

    def test_procstat_nonnumeric_id(self, raw_connection):
        """PROCSTAT with non-numeric ID returns ERR 100.
        COMMANDS.md: 'Invalid process ID (non-numeric) -> ERR 100'."""
        sock, _banner = raw_connection
        send_command(sock, "PROCSTAT abc")
        status, payload = read_response(sock)
        assert status == "ERR 100 Invalid process ID"
        assert payload == []

    def test_signal_running_process(self, raw_connection):
        """SIGNAL sends CTRL_C to a running process, causing it to exit.
        COMMANDS.md: 'Sends an AmigaOS break signal to a daemon-launched
        asynchronous process.'"""
        sock, _banner = raw_connection

        # Launch a long-running process
        send_command(sock, "EXEC ASYNC wait 30")
        status, _payload = read_response(sock)
        assert status.startswith("OK")
        proc_id = status[3:].strip()

        time.sleep(0.5)

        # Verify it is running
        send_command(sock, "PROCSTAT {}".format(proc_id))
        status, payload = read_response(sock)
        assert status == "OK"
        kv = {}
        for line in payload:
            key, _, value = line.partition("=")
            kv[key] = value
        assert kv["status"] == "RUNNING", (
            "Expected RUNNING, got: {!r}".format(kv["status"])
        )

        # Signal it
        send_command(sock, "SIGNAL {}".format(proc_id))
        status, payload = read_response(sock)
        assert status == "OK"
        assert payload == []

        # Wait for the process to exit and poll PROCSTAT
        for _attempt in range(10):
            time.sleep(0.5)
            send_command(sock, "PROCSTAT {}".format(proc_id))
            status, payload = read_response(sock)
            assert status == "OK"
            kv = {}
            for line in payload:
                key, _, value = line.partition("=")
                kv[key] = value
            if kv["status"] == "EXITED":
                break
        else:
            pytest.fail(
                "Process {} did not exit within 5 seconds after SIGNAL".format(
                    proc_id)
            )

    def test_signal_not_running(self, raw_connection):
        """SIGNAL to an EXITED process returns ERR 200.
        COMMANDS.md: 'Process not running -> ERR 200 Process not running'."""
        sock, _banner = raw_connection

        # Launch a quick process and wait for it to exit
        send_command(sock, "EXEC ASYNC echo done")
        status, _payload = read_response(sock)
        assert status.startswith("OK")
        proc_id = status[3:].strip()

        # Poll until the process exits
        for _attempt in range(10):
            time.sleep(0.5)
            send_command(sock, "PROCSTAT {}".format(proc_id))
            status, payload = read_response(sock)
            if status == "OK":
                kv = {}
                for line in payload:
                    key, _, value = line.partition("=")
                    kv[key] = value
                if kv.get("status") == "EXITED":
                    break
        else:
            pytest.fail(
                "Process {} did not exit within 5 seconds".format(proc_id))

        # Signal the exited process
        send_command(sock, "SIGNAL {}".format(proc_id))
        status, payload = read_response(sock)
        assert status.startswith("ERR 200"), (
            "Expected ERR 200 for EXITED process, got: {!r}".format(status)
        )
        assert payload == []

    def test_signal_invalid_id(self, raw_connection):
        """SIGNAL to a nonexistent ID returns ERR 200.
        COMMANDS.md: 'Process not found -> ERR 200 Process not found'."""
        sock, _banner = raw_connection
        send_command(sock, "SIGNAL 99999")
        status, payload = read_response(sock)
        assert status.startswith("ERR 200"), (
            "Expected ERR 200, got: {!r}".format(status)
        )
        assert payload == []

    def test_signal_missing_id(self, raw_connection):
        """SIGNAL with no ID returns ERR 100.
        COMMANDS.md: 'Missing process ID -> ERR 100 Missing process ID'."""
        sock, _banner = raw_connection
        send_command(sock, "SIGNAL")
        status, payload = read_response(sock)
        assert status == "ERR 100 Missing process ID"
        assert payload == []

    def test_signal_nonnumeric_id(self, raw_connection):
        """SIGNAL with non-numeric ID returns ERR 100.
        COMMANDS.md: 'Invalid process ID (non-numeric) -> ERR 100'."""
        sock, _banner = raw_connection
        send_command(sock, "SIGNAL abc")
        status, payload = read_response(sock)
        assert status == "ERR 100 Invalid process ID"
        assert payload == []

    def test_signal_invalid_name(self, raw_connection):
        """SIGNAL with invalid signal name returns ERR 100.
        COMMANDS.md: 'Invalid signal name -> ERR 100 Invalid signal name'.
        Error checking order: process ID is validated first, then status,
        then signal name.  Uses a running process to reach the signal
        name validation."""
        sock, _banner = raw_connection

        # Launch a long-running process
        send_command(sock, "EXEC ASYNC wait 10")
        status, _payload = read_response(sock)
        assert status.startswith("OK")
        proc_id = status[3:].strip()

        time.sleep(0.5)

        # Send invalid signal name
        send_command(sock, "SIGNAL {} HUP".format(proc_id))
        status, payload = read_response(sock)
        assert status == "ERR 100 Invalid signal name"
        assert payload == []

        # Clean up
        send_command(sock, "SIGNAL {}".format(proc_id))
        read_response(sock)

    def test_kill_not_permitted(self, raw_connection):
        """KILL when ALLOW_REMOTE_SHUTDOWN is NO returns ERR 201.
        COMMANDS.md: 'Remote kill not permitted -> ERR 201'."""
        sock, _banner = raw_connection
        send_command(sock, "KILL 1")
        status, payload = read_response(sock)
        assert status == "ERR 201 Remote kill not permitted"
        assert payload == []

    def test_kill_missing_id(self, raw_connection):
        """KILL with no ID still returns ERR 201 when remote kill is disabled.
        COMMANDS.md error checking order: 'permission is validated first'."""
        sock, _banner = raw_connection
        send_command(sock, "KILL")
        status, payload = read_response(sock)
        assert status == "ERR 201 Remote kill not permitted"
        assert payload == []

    @pytest.mark.skip(
        reason="Manual test: requires ALLOW_REMOTE_SHUTDOWN YES"
    )
    def test_kill_actual(self):
        """KILL a running process when ALLOW_REMOTE_SHUTDOWN is YES.

        Manual test instructions:
        1. Set ALLOW_REMOTE_SHUTDOWN YES in S:amigactld.conf.
        2. EXEC ASYNC a 'wait 60' command.
        3. KILL the process ID.
        4. Verify PROCSTAT shows status=EXITED with rc=-1.
        5. Verify daemon stability (no crash) by sending PING.
        """

    def test_process_shows_rc(self, raw_connection):
        """After a process exits, PROCSTAT shows status=EXITED and a
        numeric rc.  COMMANDS.md: 'rc -> Return code (integer) when EXITED;
        - when RUNNING'."""
        sock, _banner = raw_connection

        # Launch a process that exits quickly
        send_command(sock, "EXEC ASYNC echo done")
        status, _payload = read_response(sock)
        assert status.startswith("OK")
        proc_id = status[3:].strip()

        # Poll until the process exits
        for _attempt in range(10):
            time.sleep(0.5)
            send_command(sock, "PROCSTAT {}".format(proc_id))
            status, payload = read_response(sock)
            if status == "OK":
                kv = {}
                for line in payload:
                    key, _, value = line.partition("=")
                    kv[key] = value
                if kv.get("status") == "EXITED":
                    break
        else:
            pytest.fail(
                "Process {} did not exit within 5 seconds".format(proc_id))

        # PROCSTAT should show EXITED with a numeric rc
        send_command(sock, "PROCSTAT {}".format(proc_id))
        status, payload = read_response(sock)
        assert status == "OK"
        kv = {}
        for line in payload:
            key, _, value = line.partition("=")
            kv[key] = value
        assert kv["status"] == "EXITED", (
            "Expected EXITED, got: {!r}".format(kv["status"])
        )
        # rc should be a number (not "-")
        assert kv["rc"] != "-", (
            "Expected numeric rc for EXITED process, got: {!r}".format(
                kv["rc"])
        )
        assert kv["rc"].lstrip("-").isdigit(), (
            "rc should be a valid integer, got: {!r}".format(kv["rc"])
        )

    def test_exec_async_missing_command(self, raw_connection):
        """EXEC ASYNC with no command text returns ERR 100.
        COMMANDS.md: 'Missing command -> ERR 100 Missing command'."""
        sock, _banner = raw_connection
        send_command(sock, "EXEC ASYNC")
        status, payload = read_response(sock)
        assert status == "ERR 100 Missing command"
        assert payload == []

    def test_exec_async_cd(self, raw_connection):
        """EXEC ASYNC CD=SYS:S launches a process in the specified
        directory.  COMMANDS.md: 'CD=<path> follows the same parsing rules
        as synchronous EXEC.'"""
        sock, _banner = raw_connection
        send_command(sock, "EXEC ASYNC CD=SYS:S echo done")
        status, payload = read_response(sock)
        assert status.startswith("OK"), (
            "Expected OK, got: {!r}".format(status)
        )
        proc_id = status[3:].strip()
        assert proc_id.isdigit(), (
            "Expected numeric process ID, got: {!r}".format(proc_id)
        )
        assert payload == []

    def test_proclist_format(self, raw_connection):
        """PROCLIST payload lines have 4 tab-separated fields.
        COMMANDS.md: 'Each payload line contains four tab-separated fields:
        id, command, status, rc.'

        Note: this test may see entries from earlier tests in the session.
        It validates format regardless of whether entries are present."""
        sock, _banner = raw_connection
        send_command(sock, "PROCLIST")
        status, payload = read_response(sock)
        assert status == "OK"
        # Each payload line, if any, should have tab-separated fields
        for line in payload:
            fields = line.split("\t")
            assert len(fields) == 4, (
                "Expected 4 tab-separated fields, got {}: {!r}".format(
                    len(fields), line)
            )

    @pytest.mark.skip(
        reason="Manual test: requires 16 concurrent long-running processes"
    )
    def test_exec_async_table_full(self):
        """EXEC ASYNC when all 16 process table slots are RUNNING.

        Manual test instructions:
        1. Launch 16 'wait 60' commands via EXEC ASYNC.
        2. Before any complete, send another EXEC ASYNC.
        3. Verify ERR 500 response ('Process table full').
        """
