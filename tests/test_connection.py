"""Connection lifecycle tests for amigactld.

These tests exercise the daemon's connection handling, banner, and the four
lifecycle commands (VERSION, PING, QUIT, SHUTDOWN) using raw TCP sockets.
They validate behavior against the specs in docs/COMMANDS.md and
docs/PROTOCOL.md.

All tests use the ``raw_connection`` fixture from conftest.py, which opens
a socket, sets a 10-second timeout, and reads the banner.  Protocol helpers
``send_command`` and ``read_response`` are imported from conftest.

The daemon must be running on the target machine before these tests are
executed.
"""

import re
import socket

import pytest

from conftest import read_response, send_command


# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------

class TestBanner:
    """Tests for the connection banner sent immediately on connect."""

    def test_banner_format(self, raw_connection):
        """The banner must match 'AMIGACTL <version>' where version is a
        dotted numeric string (e.g. 0.1.0).  COMMANDS.md specifies the
        format as 'AMIGACTL <version>' and notes that the version matches
        the daemon version."""
        _sock, banner = raw_connection
        assert re.match(r"^AMIGACTL \d+\.\d+\.\d+$", banner), (
            "Banner does not match expected format: {!r}".format(banner)
        )


# ---------------------------------------------------------------------------
# PING
# ---------------------------------------------------------------------------

class TestPing:
    """Tests for the PING command."""

    def test_ping(self, raw_connection):
        """PING returns 'OK' with no payload and no info text.
        COMMANDS.md: Response is 'OK\\n.\\n'."""
        sock, _banner = raw_connection
        send_command(sock, "PING")
        status, payload = read_response(sock)
        assert status == "OK"
        assert payload == []

    def test_case_insensitive(self, raw_connection):
        """Commands are case-insensitive.  'ping' in lowercase must produce
        the same response as 'PING'.  COMMANDS.md: 'Commands are
        case-insensitive.'"""
        sock, _banner = raw_connection
        send_command(sock, "ping")
        status, payload = read_response(sock)
        assert status == "OK"
        assert payload == []

    def test_ping_trailing_text_ignored(self, raw_connection):
        """Extra text after the PING verb is ignored.  The daemon parses
        the verb and discards the rest of the line."""
        sock, _banner = raw_connection
        send_command(sock, "PING extra")
        status, payload = read_response(sock)
        assert status == "OK"
        assert payload == []

    def test_crlf_line_endings(self, raw_connection):
        """CR LF line endings are accepted (telnet compatibility).  The
        daemon strips the trailing CR before processing the command."""
        sock, _banner = raw_connection
        sock.sendall(b"PING\r\n")
        status, payload = read_response(sock)
        assert status == "OK"
        assert payload == []


# ---------------------------------------------------------------------------
# VERSION
# ---------------------------------------------------------------------------

class TestVersion:
    """Tests for the VERSION command."""

    def test_version(self, raw_connection):
        """VERSION returns 'OK' with a single payload line containing
        'amigactld <version>'.  COMMANDS.md: 'The payload is a single line
        containing the daemon identifier and version in the format
        amigactld <version>.'"""
        sock, _banner = raw_connection
        send_command(sock, "VERSION")
        status, payload = read_response(sock)
        assert status == "OK"
        assert len(payload) == 1, (
            "Expected exactly one payload line, got {}".format(len(payload))
        )
        assert re.match(r"^amigactld \d+\.\d+\.\d+$", payload[0]), (
            "Version payload does not match expected format: {!r}".format(
                payload[0]
            )
        )

    def test_version_trailing_text_ignored(self, raw_connection):
        """Extra text after the VERSION verb is ignored.  The daemon
        parses the verb and discards the rest of the line."""
        sock, _banner = raw_connection
        send_command(sock, "VERSION extra stuff")
        status, payload = read_response(sock)
        assert status == "OK"
        assert len(payload) == 1, (
            "Expected exactly one payload line, got {}".format(len(payload))
        )
        assert re.match(r"^amigactld \d+\.\d+\.\d+$", payload[0]), (
            "Version payload does not match expected format: {!r}".format(
                payload[0]
            )
        )


# ---------------------------------------------------------------------------
# QUIT
# ---------------------------------------------------------------------------

class TestQuit:
    """Tests for the QUIT command."""

    def test_quit(self, raw_connection):
        """QUIT returns 'OK Goodbye' with no payload, then the server
        closes the connection (recv returns EOF).  COMMANDS.md: 'After
        sending the sentinel, the server closes the client's TCP
        connection.'"""
        sock, _banner = raw_connection
        send_command(sock, "QUIT")
        status, payload = read_response(sock)
        assert status == "OK Goodbye"
        assert payload == []
        # The server should have closed the connection.  recv() must
        # return empty bytes (EOF).
        remaining = sock.recv(1024)
        assert remaining == b"", (
            "Expected EOF after QUIT, got: {!r}".format(remaining)
        )


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class TestErrors:
    """Tests for error responses (unknown commands, oversized input)."""

    def test_empty_line_ignored(self, raw_connection):
        """An empty line (bare LF) produces no response.  The daemon
        silently discards it and processes the next command normally."""
        sock, _banner = raw_connection
        # Send an empty line followed by PING.
        sock.sendall(b"\n")
        send_command(sock, "PING")
        status, payload = read_response(sock)
        assert status == "OK"
        assert payload == []

    def test_unknown_command(self, raw_connection):
        """An unrecognized command verb returns 'ERR 100 Unknown command'.
        COMMANDS.md: 'Any command verb that the server does not recognize
        produces a syntax error.'"""
        sock, _banner = raw_connection
        send_command(sock, "FOOBAR")
        status, payload = read_response(sock)
        assert status == "ERR 100 Unknown command"
        assert payload == []

    def test_oversized_command(self, raw_connection):
        """Sending >4096 bytes without a newline triggers 'ERR 100 Command
        too long'.  After the error, the connection must remain usable.
        COMMANDS.md: 'The connection is NOT closed.  The client can recover
        by ensuring its next transmission after the error includes a
        newline.'

        The test sends 5000 bytes of padding (no newline), reads the error,
        then sends a terminating newline (to end discard mode) followed by
        a PING to prove the connection is still alive."""
        sock, _banner = raw_connection

        # Send more than 4096 bytes without a newline.  The daemon should
        # detect the overflow when its buffer fills and send the error.
        overflow_data = ("A" * 5000).encode("iso-8859-1")
        sock.sendall(overflow_data)

        # Read the error response.
        status, payload = read_response(sock)
        assert status == "ERR 100 Command too long"
        assert payload == []

        # The daemon is now in discard mode.  Send a newline to terminate
        # the oversized "command", which ends discard mode.
        sock.sendall(b"\n")

        # Verify the connection is still usable by sending PING.
        send_command(sock, "PING")
        status, payload = read_response(sock)
        assert status == "OK"
        assert payload == []


# ---------------------------------------------------------------------------
# Multiple clients
# ---------------------------------------------------------------------------

class TestMultipleClients:
    """Tests for concurrent client support."""

    def test_multiple_clients(self, amiga_host, amiga_port):
        """The daemon must handle multiple simultaneous connections.  Open
        three connections, send PING on each, and verify all respond.
        PROTOCOL.md: 'The daemon accepts up to 8 simultaneous clients.'"""
        sockets = []
        try:
            for _ in range(3):
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(10)
                s.connect((amiga_host, amiga_port))
                # Read and discard the banner.
                _read_banner(s)
                sockets.append(s)

            # Send PING on each socket and verify responses.
            for i, s in enumerate(sockets):
                send_command(s, "PING")
                status, payload = read_response(s)
                assert status == "OK", (
                    "Client {} did not get OK: {!r}".format(i, status)
                )
                assert payload == [], (
                    "Client {} got unexpected payload: {!r}".format(i, payload)
                )
        finally:
            for s in sockets:
                s.close()


# ---------------------------------------------------------------------------
# SHUTDOWN
# ---------------------------------------------------------------------------

class TestShutdown:
    """Tests for the SHUTDOWN command."""

    def test_shutdown_not_permitted(self, raw_connection):
        """With default configuration (ALLOW_REMOTE_SHUTDOWN not set),
        'SHUTDOWN CONFIRM' returns 'ERR 201 Remote shutdown not permitted'.
        COMMANDS.md: the error table shows code 201 for this condition."""
        sock, _banner = raw_connection
        send_command(sock, "SHUTDOWN CONFIRM")
        status, payload = read_response(sock)
        assert status == "ERR 201 Remote shutdown not permitted"
        assert payload == []

    def test_shutdown_missing_confirm(self, raw_connection):
        """'SHUTDOWN' without the CONFIRM keyword returns 'ERR 100
        SHUTDOWN requires CONFIRM keyword'.  COMMANDS.md: 'Error checking
        order: the CONFIRM keyword is validated first.'"""
        sock, _banner = raw_connection
        send_command(sock, "SHUTDOWN")
        status, payload = read_response(sock)
        assert status == "ERR 100 SHUTDOWN requires CONFIRM keyword"
        assert payload == []

    def test_shutdown_wrong_keyword(self, raw_connection):
        """'SHUTDOWN NOW' (wrong keyword) returns the same error as a
        missing keyword -- the daemon expects exactly 'CONFIRM'."""
        sock, _banner = raw_connection
        send_command(sock, "SHUTDOWN NOW")
        status, payload = read_response(sock)
        assert status.startswith("ERR 100"), (
            "Expected ERR 100, got: {!r}".format(status)
        )
        assert "SHUTDOWN requires CONFIRM keyword" in status
        assert payload == []


# ---------------------------------------------------------------------------
# REBOOT
# ---------------------------------------------------------------------------

class TestReboot:
    """Tests for the REBOOT command."""

    def test_reboot_missing_confirm(self, raw_connection):
        """REBOOT without CONFIRM returns ERR 100."""
        sock, _banner = raw_connection
        send_command(sock, "REBOOT")
        status, payload = read_response(sock)
        assert status == "ERR 100 REBOOT requires CONFIRM keyword"
        assert payload == []

    def test_reboot_wrong_keyword(self, raw_connection):
        """REBOOT NOW returns ERR 100."""
        sock, _banner = raw_connection
        send_command(sock, "REBOOT NOW")
        status, payload = read_response(sock)
        assert status.startswith("ERR 100"), (
            "Expected ERR 100, got: {!r}".format(status)
        )
        assert "REBOOT requires CONFIRM keyword" in status
        assert payload == []


# ---------------------------------------------------------------------------
# UPTIME
# ---------------------------------------------------------------------------

class TestUptime:
    """Tests for the UPTIME command."""

    def test_uptime_response_format(self, raw_connection):
        """UPTIME returns OK with seconds=N payload."""
        sock, _banner = raw_connection
        send_command(sock, "UPTIME")
        status, payload = read_response(sock)
        assert status == "OK"
        assert len(payload) == 1
        assert payload[0].startswith("seconds=")
        seconds = int(payload[0].split("=")[1])
        assert seconds >= 0

    def test_uptime_increases(self, raw_connection):
        """UPTIME seconds value should increase over time."""
        import time
        sock, _banner = raw_connection
        send_command(sock, "UPTIME")
        status1, payload1 = read_response(sock)
        s1 = int(payload1[0].split("=")[1])
        time.sleep(2)
        send_command(sock, "UPTIME")
        status2, payload2 = read_response(sock)
        s2 = int(payload2[0].split("=")[1])
        assert s2 >= s1 + 1  # at least 1 second passed


# ---------------------------------------------------------------------------
# Manual / skipped tests
# ---------------------------------------------------------------------------

class TestManual:
    """Tests that require special configuration or manual intervention."""

    @pytest.mark.skip(
        reason="Manual test: requires non-allowed IP source"
    )
    def test_acl_rejection(self):
        """Start the daemon with a restrictive ALLOW list that does not
        include this host's IP.  Connect and verify the daemon closes the
        connection immediately without sending a banner."""

    @pytest.mark.skip(
        reason="Manual test: Ctrl-C daemon"
    )
    def test_ctrl_c_shutdown(self):
        """With active client connections, send Ctrl-C to the daemon
        process on the Amiga.  Verify that all connections are closed and
        the daemon exits cleanly."""

    @pytest.mark.skip(
        reason="Manual test: requires ALLOW_REMOTE_SHUTDOWN YES"
    )
    def test_shutdown_permitted(self):
        """Start the daemon with ALLOW_REMOTE_SHUTDOWN YES in
        S:amigactld.conf.  Send 'SHUTDOWN CONFIRM' and verify the response
        is 'OK Shutting down', followed by the daemon closing all
        connections and exiting."""

    @pytest.mark.skip(
        reason="Manual test: requires ALLOW_REMOTE_REBOOT NO"
    )
    def test_reboot_not_permitted(self):
        """Start the daemon with ALLOW_REMOTE_REBOOT NO (or absent) in
        S:amigactld.conf.  Send 'REBOOT CONFIRM' and verify the response
        is 'ERR 201 Remote reboot not permitted'."""

    @pytest.mark.skip(
        reason="Manual test: requires ALLOW_REMOTE_REBOOT YES"
    )
    def test_reboot_permitted(self):
        """Start the daemon with ALLOW_REMOTE_REBOOT YES in
        S:amigactld.conf.  Send 'REBOOT CONFIRM' and verify the response
        is 'OK Rebooting', followed by the system rebooting."""


# ---------------------------------------------------------------------------
# Internal helpers (test-module-local)
# ---------------------------------------------------------------------------

def _read_banner(sock):
    """Read and return the banner line from a freshly connected socket.

    This is a minimal duplicate of the banner-reading logic in the
    ``raw_connection`` fixture, used by tests that manage their own
    sockets (e.g. ``test_multiple_clients``).
    """
    buf = bytearray()
    while True:
        byte = sock.recv(1)
        if not byte:
            raise ConnectionError("EOF while reading banner")
        if byte == b"\n":
            break
        buf.extend(byte)
    return buf.decode("iso-8859-1").rstrip("\r")
