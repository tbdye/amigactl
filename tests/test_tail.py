"""TAIL streaming tests for amigactld.

These tests exercise the TAIL command, which streams file appends to the
client until STOP is sent.  TAIL uses DATA/END framing with ongoing
streaming -- unlike READ or EXEC where the framing is bounded by a known
file size or command completion.

Most tests use two connections: one for the TAIL stream and one for
modifying the file (appending via EXEC, overwriting via WRITE, deleting
via DELETE).  The TAIL connection operates at the raw protocol level
because the client library's tail() method blocks.

Tests create files in RAM: to avoid disk I/O issues and register them
with cleanup_paths for automatic cleanup.

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
    send_write_data,
)

from amigactl import AmigaConnection


# ---------------------------------------------------------------------------
# Internal helpers for TAIL protocol interaction
# ---------------------------------------------------------------------------

def _start_tail(sock, path):
    """Send TAIL <path> and read the OK or ERR status line.

    Returns (ok, info) where ok is True if TAIL started successfully
    and info is the remainder of the status line (e.g. "1024" for
    "OK 1024", or "200 Object not found" for errors).

    On error, also reads and discards the sentinel.
    """
    send_command(sock, "TAIL {}".format(path))
    status_line = _read_line(sock)
    if status_line.startswith("OK"):
        info = status_line[3:].strip()
        return True, info
    elif status_line.startswith("ERR "):
        # Read sentinel
        sentinel = _read_line(sock)
        assert sentinel == ".", (
            "Expected sentinel after ERR, got: {!r}".format(sentinel)
        )
        return False, status_line
    else:
        raise AssertionError(
            "Expected OK or ERR, got: {!r}".format(status_line)
        )


def _read_tail_data(sock, timeout=5):
    """Read a single DATA chunk from a TAIL stream with timeout.

    Sets the socket timeout, reads one DATA <len> + <bytes> pair, and
    returns the raw bytes.  Raises socket.timeout if no data arrives
    within the timeout.  Raises AssertionError if the next line is not
    a DATA header.

    Does NOT restore the original socket timeout -- the caller is
    responsible for that.
    """
    old_timeout = sock.gettimeout()
    sock.settimeout(timeout)
    try:
        line = _read_line(sock)
        if line.startswith("DATA "):
            chunk_len = int(line[5:])
            data = _recv_exact(sock, chunk_len)
            return data
        elif line == "END":
            raise AssertionError("Received END instead of DATA chunk")
        elif line.startswith("ERR "):
            # Read sentinel
            sentinel = _read_line(sock)
            assert sentinel == "."
            raise AssertionError(
                "Received error instead of DATA: {}".format(line)
            )
        else:
            raise AssertionError(
                "Expected DATA, got: {!r}".format(line)
            )
    finally:
        sock.settimeout(old_timeout)


# ---------------------------------------------------------------------------
# TAIL basic operation
# ---------------------------------------------------------------------------

class TestTailBasic:
    """Tests for TAIL startup, STOP, and basic streaming."""

    def test_tail_existing_file(self, raw_connection, cleanup_paths):
        """TAIL on an existing file returns OK with the current file size.
        COMMANDS.md: 'The OK status line includes the file's current size
        in bytes at the time TAIL starts.'"""
        sock, _banner = raw_connection
        path = "RAM:amigactl_test_tail_exist.txt"
        content = b"initial content"

        # Create the file
        status, _payload = send_write_data(sock, path, content)
        assert status.startswith("OK"), (
            "WRITE failed: {!r}".format(status)
        )
        cleanup_paths.add(path)

        # Start TAIL
        ok, info = _start_tail(sock, path)
        assert ok, "TAIL failed to start: {}".format(info)

        # Verify the reported size matches
        reported_size = int(info)
        assert reported_size == len(content), (
            "Expected OK {}, got OK {}".format(len(content), reported_size)
        )

        # Clean stop
        _send_stop_and_drain(sock)

    def test_tail_stop_clean(self, raw_connection, cleanup_paths):
        """STOP terminates TAIL cleanly: END + sentinel, then connection
        returns to normal command processing.
        COMMANDS.md: 'After the sentinel, the connection returns to normal
        command processing.'"""
        sock, _banner = raw_connection
        path = "RAM:amigactl_test_tail_stop.txt"

        # Create file
        status, _payload = send_write_data(sock, path, b"stop test")
        assert status.startswith("OK")
        cleanup_paths.add(path)

        # Start TAIL
        ok, info = _start_tail(sock, path)
        assert ok

        # Send STOP immediately
        _send_stop_and_drain(sock)

        # Connection should be back to normal -- send PING
        send_command(sock, "PING")
        status, payload = read_response(sock)
        assert status == "OK", (
            "PING after STOP failed: {!r}".format(status)
        )
        assert payload == []

    def test_tail_empty_file(self, raw_connection, cleanup_paths):
        """TAIL on an empty file reports OK 0.
        COMMANDS.md: 'OK <current_size>' -- for an empty file this is
        'OK 0'."""
        sock, _banner = raw_connection
        path = "RAM:amigactl_test_tail_empty.txt"

        # Create empty file
        status, _payload = send_write_data(sock, path, b"")
        assert status.startswith("OK")
        cleanup_paths.add(path)

        # Start TAIL
        ok, info = _start_tail(sock, path)
        assert ok
        assert info == "0", (
            "Expected OK 0 for empty file, got OK {}".format(info)
        )

        # Clean stop
        _send_stop_and_drain(sock)

    def test_tail_receives_appended_data(self, amiga_host, amiga_port,
                                         cleanup_paths):
        """TAIL streams DATA chunks when new content is appended.
        COMMANDS.md: 'When new content is appended, it sends one or more
        DATA chunks containing the new bytes.'

        Uses a second connection to append data via EXEC while TAIL is
        active on the first connection."""
        path = "RAM:amigactl_test_tail_append.txt"

        # Primary connection: create the file
        conn = AmigaConnection(amiga_host, amiga_port)
        conn.connect()
        try:
            conn.write(path, b"line1\n")
            cleanup_paths.add(path)
        finally:
            conn.close()

        # TAIL connection (raw)
        tail_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        tail_sock.settimeout(10)
        tail_sock.connect((amiga_host, amiga_port))
        _read_line(tail_sock)  # banner

        # Modifier connection (for appending via EXEC)
        mod_conn = AmigaConnection(amiga_host, amiga_port)
        mod_conn.connect()

        try:
            # Start TAIL
            ok, info = _start_tail(tail_sock, path)
            assert ok, "TAIL failed: {}".format(info)

            # Append data via EXEC echo >>
            mod_conn.execute('echo "appended text" >>RAM:amigactl_test_tail_append.txt')

            # Wait for DATA chunk on tail connection (polling is ~1 second)
            data = _read_tail_data(tail_sock, timeout=10)
            assert len(data) > 0, "Expected non-empty DATA chunk"
            text = data.decode("iso-8859-1")
            assert "appended text" in text, (
                "Expected 'appended text' in DATA chunk, got: {!r}".format(
                    text)
            )

            # Clean stop
            _send_stop_and_drain(tail_sock)
        finally:
            tail_sock.close()
            mod_conn.close()


# ---------------------------------------------------------------------------
# TAIL error handling
# ---------------------------------------------------------------------------

class TestTailErrors:
    """Tests for TAIL error conditions."""

    def test_tail_nonexistent_file(self, raw_connection):
        """TAIL on a nonexistent file returns ERR 200.
        COMMANDS.md: 'Path not found -> ERR 200 <dos error message>'."""
        sock, _banner = raw_connection
        send_command(sock, "TAIL RAM:nonexistent_amigactl_tail_test.txt")
        status, payload = read_response(sock)
        assert status.startswith("ERR 200"), (
            "Expected ERR 200, got: {!r}".format(status)
        )
        assert payload == []

    def test_tail_directory(self, raw_connection):
        """TAIL on a directory returns ERR 300.
        COMMANDS.md: 'Path is a directory -> ERR 300 TAIL requires a
        file, not a directory'."""
        sock, _banner = raw_connection
        send_command(sock, "TAIL RAM:")
        status, payload = read_response(sock)
        assert status.startswith("ERR 300"), (
            "Expected ERR 300, got: {!r}".format(status)
        )
        assert payload == []

    def test_tail_missing_path(self, raw_connection):
        """TAIL with no path argument returns ERR 100.
        COMMANDS.md: 'Missing path argument -> ERR 100 Missing path
        argument'."""
        sock, _banner = raw_connection
        send_command(sock, "TAIL")
        status, payload = read_response(sock)
        assert status.startswith("ERR 100"), (
            "Expected ERR 100, got: {!r}".format(status)
        )
        assert payload == []

    def test_tail_error_leaves_connection_usable(self, raw_connection):
        """Connection remains usable after a TAIL error.
        COMMANDS.md: 'These errors are returned synchronously.  The
        connection remains in normal command processing mode.'"""
        sock, _banner = raw_connection
        send_command(sock, "TAIL RAM:nonexistent_amigactl_tail_test.txt")
        status, payload = read_response(sock)
        assert status.startswith("ERR 200")

        # Connection should still work
        send_command(sock, "PING")
        status, payload = read_response(sock)
        assert status == "OK"
        assert payload == []


# ---------------------------------------------------------------------------
# TAIL file lifecycle events
# ---------------------------------------------------------------------------

class TestTailFileEvents:
    """Tests for TAIL behavior when the file is modified or deleted."""

    def test_tail_file_deleted(self, amiga_host, amiga_port, cleanup_paths):
        """TAIL sends ERR 300 when the file is deleted during streaming.
        COMMANDS.md: 'If the file is deleted or becomes inaccessible:
        ERR 300 File no longer accessible / .'"""
        path = "RAM:amigactl_test_tail_delete.txt"

        # Create the file
        setup_conn = AmigaConnection(amiga_host, amiga_port)
        setup_conn.connect()
        try:
            setup_conn.write(path, b"delete me during tail")
            cleanup_paths.add(path)
        finally:
            setup_conn.close()

        # TAIL connection
        tail_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        tail_sock.settimeout(10)
        tail_sock.connect((amiga_host, amiga_port))
        _read_line(tail_sock)  # banner

        # Delete connection
        del_conn = AmigaConnection(amiga_host, amiga_port)
        del_conn.connect()

        try:
            # Start TAIL
            ok, info = _start_tail(tail_sock, path)
            assert ok, "TAIL failed: {}".format(info)

            # Delete the file
            del_conn.delete(path)

            # Wait for ERR 300 on the TAIL connection (next poll cycle)
            tail_sock.settimeout(10)
            line = _read_line(tail_sock)
            assert line.startswith("ERR 300"), (
                "Expected ERR 300 after file deletion, got: {!r}".format(line)
            )

            # Read sentinel
            sentinel = _read_line(tail_sock)
            assert sentinel == ".", (
                "Expected sentinel after ERR, got: {!r}".format(sentinel)
            )

            # Connection should return to normal mode
            send_command(tail_sock, "PING")
            status, payload = read_response(tail_sock)
            assert status == "OK", (
                "PING after file deletion failed: {!r}".format(status)
            )
        finally:
            tail_sock.close()
            del_conn.close()

    def test_tail_truncation_detection(self, amiga_host, amiga_port,
                                       cleanup_paths):
        """TAIL detects file truncation and resets position.
        COMMANDS.md: 'If the file size decreases, the daemon resets its
        read position to the new file end.  No error is generated.
        Subsequent growth is streamed from the new end.'

        Overwrites a file with smaller content via WRITE, then appends
        via EXEC.  The streamed data should be from the new append, not
        stale data from the original file."""
        path = "RAM:amigactl_test_tail_trunc.txt"

        # Create a file with known content
        setup_conn = AmigaConnection(amiga_host, amiga_port)
        setup_conn.connect()
        try:
            setup_conn.write(path, b"1234567890")
            cleanup_paths.add(path)
        finally:
            setup_conn.close()

        # TAIL connection
        tail_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        tail_sock.settimeout(10)
        tail_sock.connect((amiga_host, amiga_port))
        _read_line(tail_sock)  # banner

        # Modifier connection
        mod_conn = AmigaConnection(amiga_host, amiga_port)
        mod_conn.connect()

        try:
            # Start TAIL
            ok, info = _start_tail(tail_sock, path)
            assert ok
            assert info == "10"  # original 10 bytes

            # Overwrite with smaller content (truncation)
            mod_conn.write(path, b"AB")

            # Wait for one poll cycle to detect truncation
            time.sleep(2)

            # Append new data after truncation
            mod_conn.execute(
                'echo "CD" >>RAM:amigactl_test_tail_trunc.txt'
            )

            # Read the DATA chunk -- should contain the appended data
            data = _read_tail_data(tail_sock, timeout=10)
            text = data.decode("iso-8859-1")
            assert "CD" in text, (
                "Expected appended data 'CD' after truncation, got: {!r}"
                .format(text)
            )

            # Should NOT contain stale data from the original file
            assert "1234567890" not in text, (
                "Received stale data from pre-truncation file: {!r}".format(
                    text)
            )

            # Clean stop
            _send_stop_and_drain(tail_sock)
        finally:
            tail_sock.close()
            mod_conn.close()


# ---------------------------------------------------------------------------
# TAIL non-blocking verification
# ---------------------------------------------------------------------------

class TestTailNonBlocking:
    """Tests verifying TAIL does not block other clients."""

    def test_tail_nonblocking(self, amiga_host, amiga_port, cleanup_paths):
        """While TAIL is active on one connection, another connection can
        send PING and get OK back.
        COMMANDS.md: TAIL is an ongoing stream that does not block the
        event loop for other clients."""
        path = "RAM:amigactl_test_tail_nonblock.txt"

        # Create file
        setup_conn = AmigaConnection(amiga_host, amiga_port)
        setup_conn.connect()
        try:
            setup_conn.write(path, b"nonblock test")
            cleanup_paths.add(path)
        finally:
            setup_conn.close()

        # TAIL connection
        tail_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        tail_sock.settimeout(10)
        tail_sock.connect((amiga_host, amiga_port))
        _read_line(tail_sock)  # banner

        # PING connection
        ping_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        ping_sock.settimeout(10)
        ping_sock.connect((amiga_host, amiga_port))
        _read_line(ping_sock)  # banner

        try:
            # Start TAIL
            ok, info = _start_tail(tail_sock, path)
            assert ok

            # Let one poll cycle pass
            time.sleep(1.5)

            # PING on the other connection should work immediately
            send_command(ping_sock, "PING")
            status, payload = read_response(ping_sock)
            assert status == "OK", (
                "PING failed while TAIL active: {!r}".format(status)
            )
            assert payload == []

            # Clean stop on TAIL connection
            _send_stop_and_drain(tail_sock)
        finally:
            tail_sock.close()
            ping_sock.close()


# ---------------------------------------------------------------------------
# STOP outside TAIL
# ---------------------------------------------------------------------------

class TestStopOutsideTail:
    """Tests for the STOP command outside of an active TAIL stream."""

    def test_stop_outside_tail(self, raw_connection):
        """STOP outside TAIL returns ERR 100 Unknown command.
        COMMANDS.md: 'STOP is not recognized as a command outside of an
        active TAIL stream.'"""
        sock, _banner = raw_connection
        send_command(sock, "STOP")
        status, payload = read_response(sock)
        assert status == "ERR 100 Unknown command", (
            "Expected ERR 100 Unknown command, got: {!r}".format(status)
        )
        assert payload == []
