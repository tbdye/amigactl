"""Phase 4 ARexx tests for amigactld.

These tests exercise the AREXX command against a live amigactld daemon.
ARexx commands are dispatched asynchronously (non-blocking) -- the daemon
sends the ARexx message to the target port and resumes servicing other
clients while waiting for the reply.

Tests that require the built-in ARexx interpreter (REXX port) use the
``rexx_available`` fixture to skip gracefully when ARexx is not running.

The daemon must be running on the target machine before these tests are
executed.
"""

import re
import socket
import time

import pytest

from conftest import (
    _read_line,
    _recv_exact,
    read_exec_response,
    read_response,
    send_command,
)


# ---------------------------------------------------------------------------
# AREXX via REXX port (requires ARexx interpreter)
# ---------------------------------------------------------------------------

class TestArexxRexx:
    """Tests for AREXX commands sent to the built-in REXX port."""

    def test_arexx_simple_expression(self, conn, rexx_available):
        """AREXX REXX 'return 42' returns rc=0 with result '42'.
        COMMANDS.md: 'The OK status line includes rc=<N> where N is the
        return code from the target port's reply.'"""
        rc, result = conn.arexx("REXX", "return 42")
        assert rc == 0, (
            "Expected rc=0, got rc={}".format(rc)
        )
        assert result == "42", (
            "Expected result '42', got: {!r}".format(result)
        )

    def test_arexx_arithmetic(self, conn, rexx_available):
        """AREXX REXX 'return 1+2' returns rc=0 with result '3'.
        COMMANDS.md example: arithmetic expressions are evaluated by the
        ARexx interpreter."""
        rc, result = conn.arexx("REXX", "return 1+2")
        assert rc == 0, (
            "Expected rc=0, got rc={}".format(rc)
        )
        assert result == "3", (
            "Expected result '3', got: {!r}".format(result)
        )

    def test_arexx_string_result(self, conn, rexx_available):
        """AREXX REXX with a string return value preserves the string.
        COMMANDS.md: 'The DATA body is the ARexx RESULT string returned
        by the target port.'"""
        rc, result = conn.arexx("REXX", 'return "hello world"')
        assert rc == 0, (
            "Expected rc=0, got rc={}".format(rc)
        )
        assert result == "hello world", (
            "Expected 'hello world', got: {!r}".format(result)
        )

    def test_arexx_no_result(self, conn, rexx_available):
        """AREXX REXX with a command that returns no value produces empty
        result.  COMMANDS.md: 'If no result string was set, no DATA
        chunks are sent (just END immediately after the OK line).'"""
        rc, result = conn.arexx("REXX", "nop")
        assert rc == 0, (
            "Expected rc=0, got rc={}".format(rc)
        )
        assert result == "", (
            "Expected empty result for 'nop', got: {!r}".format(result)
        )

    def test_arexx_error_rc(self, conn, rexx_available):
        """AREXX REXX with a syntax error returns non-zero rc and empty
        result.  COMMANDS.md: 'A non-zero rc from the target is NOT a
        daemon-level error.  The daemon returns OK rc=<N>.'  When rc != 0,
        rm_Result2 is a secondary error code, not a result string."""
        rc, result = conn.arexx("REXX", "x = (")
        assert rc > 0, (
            "Expected non-zero rc from syntax error 'x = (', "
            "got rc={}".format(rc)
        )
        assert result == "", (
            "Non-zero rc should have empty result, got: {!r}".format(result)
        )

    def test_arexx_nonblocking(self, rexx_available, amiga_host, amiga_port):
        """AREXX does not block the daemon's event loop.  While one client
        waits for a slow AREXX response, another client can send PING and
        get a response immediately.
        COMMANDS.md: 'The requesting client is suspended until the ARexx
        reply arrives or timeout.  Other clients can send commands normally.'

        Uses a pure ARexx busy loop (no external libraries needed) to
        keep the interpreter busy for a few seconds."""
        # Client A: send slow AREXX -- generous timeout for emulated 68k
        sock_a = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock_a.settimeout(60)
        sock_a.connect((amiga_host, amiga_port))
        _read_line(sock_a)  # banner

        # Client B: for PING
        sock_b = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock_b.settimeout(10)
        sock_b.connect((amiga_host, amiga_port))
        _read_line(sock_b)  # banner

        try:
            # Client A sends AREXX with a busy loop (~2-5s on emulated 68k).
            # Pure ARexx -- no rexxsupport.library dependency.
            send_command(
                sock_a,
                "AREXX REXX do i = 1 to 50000; nop; end; return 0",
            )

            # Brief pause to let the daemon dispatch the ARexx message
            time.sleep(0.3)

            # Client B sends PING -- should get OK back immediately
            t_start = time.monotonic()
            send_command(sock_b, "PING")
            status, payload = read_response(sock_b)
            t_elapsed = time.monotonic() - t_start

            assert status == "OK", (
                "Client B PING failed while AREXX pending: {!r}".format(
                    status)
            )
            assert payload == []

            # PING should complete in well under the loop's execution time
            assert t_elapsed < 1.5, (
                "PING took {:.2f}s -- daemon may be blocking on AREXX".format(
                    t_elapsed)
            )

            # Now read Client A's AREXX response
            rc, data = read_exec_response(sock_a)
            assert rc == 0, (
                "AREXX loop command failed with rc={}".format(rc)
            )
        finally:
            sock_a.close()
            sock_b.close()


# ---------------------------------------------------------------------------
# AREXX error handling
# ---------------------------------------------------------------------------

class TestArexxErrors:
    """Tests for AREXX error conditions and edge cases."""

    def test_arexx_port_not_found(self, raw_connection):
        """AREXX to a nonexistent port returns ERR 200.
        COMMANDS.md: 'Target port not found -> ERR 200 ARexx port not
        found'."""
        sock, _banner = raw_connection
        send_command(sock, "AREXX NONEXISTENT_PORT_12345 test command")
        status, payload = read_response(sock)
        assert status.startswith("ERR 200"), (
            "Expected ERR 200, got: {!r}".format(status)
        )
        assert payload == []

    def test_arexx_missing_all_args(self, raw_connection):
        """AREXX with no arguments returns ERR 100.
        COMMANDS.md: 'Missing port name or command -> ERR 100 Usage: AREXX
        <port> <command>'."""
        sock, _banner = raw_connection
        send_command(sock, "AREXX")
        status, payload = read_response(sock)
        assert status.startswith("ERR 100"), (
            "Expected ERR 100, got: {!r}".format(status)
        )
        assert payload == []

    def test_arexx_missing_command(self, raw_connection):
        """AREXX with port name but no command returns ERR 100.
        COMMANDS.md: 'AREXX REXX (port name with no command text) returns
        ERR 100.'"""
        sock, _banner = raw_connection
        send_command(sock, "AREXX REXX")
        status, payload = read_response(sock)
        assert status.startswith("ERR 100"), (
            "Expected ERR 100, got: {!r}".format(status)
        )
        assert payload == []

    def test_arexx_response_format(self, rexx_available, raw_connection):
        """AREXX response uses DATA/END framing identical to EXEC.
        COMMANDS.md: the response format is 'OK rc=<N> / DATA <len> /
        <bytes> / END / .'"""
        sock, _banner = raw_connection
        send_command(sock, "AREXX REXX return 99")

        # Read status line manually to verify format
        status_line = _read_line(sock)
        assert status_line.startswith("OK rc="), (
            "Expected 'OK rc=N', got: {!r}".format(status_line)
        )
        info = status_line[3:].strip()
        match = re.match(r"^rc=(-?\d+)$", info)
        assert match, (
            "Info field should match rc=N, got: {!r}".format(info)
        )

        # Read DATA/END chunks
        while True:
            line = _read_line(sock)
            if line == "END":
                break
            assert line.startswith("DATA "), (
                "Expected DATA or END, got: {!r}".format(line)
            )
            chunk_len = int(line[5:])
            _recv_exact(sock, chunk_len)

        # Read sentinel
        sentinel = _read_line(sock)
        assert sentinel == ".", (
            "Expected sentinel, got: {!r}".format(sentinel)
        )

    def test_arexx_connection_alive_after_error(self, raw_connection):
        """Connection remains usable after an AREXX error.
        COMMANDS.md: error codes 500 and 200 are returned synchronously,
        and the connection continues."""
        sock, _banner = raw_connection
        send_command(sock, "AREXX NONEXISTENT_PORT_12345 test")
        status, payload = read_response(sock)
        assert status.startswith("ERR 200")
        assert payload == []

        # Connection should still work
        send_command(sock, "PING")
        status, payload = read_response(sock)
        assert status == "OK"
        assert payload == []
