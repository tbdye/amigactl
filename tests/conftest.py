"""Shared fixtures and helpers for amigactl integration tests.

These tests connect to a live amigactld daemon over TCP using raw sockets.
The daemon must be running on the target Amiga (or emulator) before tests
are executed.

Usage:
    pytest tests/ --host 192.168.6.200 --port 6800 -v

Host and port can also be set via AMIGACTL_HOST and AMIGACTL_PORT
environment variables.
"""

import os
import re
import socket
import sys

import pytest

# Add the client library to the path so tests can import amigactl
_client_dir = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "client",
)
if _client_dir not in sys.path:
    sys.path.insert(0, _client_dir)

from amigactl import AmigaConnection


# ---------------------------------------------------------------------------
# File transfer protocol helpers (binary data, WRITE handshake, RENAME)
# ---------------------------------------------------------------------------

def _recv_exact(sock, nbytes):
    """Receive exactly nbytes from sock, looping on partial recv."""
    buf = bytearray()
    while len(buf) < nbytes:
        chunk = sock.recv(nbytes - len(buf))
        if not chunk:
            raise ConnectionError(
                "EOF while reading {} bytes (got {})".format(nbytes, len(buf))
            )
        buf.extend(chunk)
    return bytes(buf)


def read_data_response(sock):
    """Read a binary data response: OK line, DATA/END chunks, sentinel.

    Returns (info_str, raw_bytes).

    The OK info field contains the declared size (e.g. "1234").
    raw_bytes is the concatenated content from all DATA chunks.

    Also handles ERR -> sentinel (returns (status_line, b"")).
    After the DATA/END loop, validates that total received bytes match
    the declared size.
    """
    status_line = _read_line(sock)
    if status_line.startswith("ERR "):
        sentinel = _read_line(sock)
        assert sentinel == "."
        return status_line, b""

    assert status_line.startswith("OK"), \
        "Expected OK or ERR, got: {!r}".format(status_line)
    info = status_line[3:].strip()

    data = bytearray()
    while True:
        line = _read_line(sock)
        if line == "END":
            break
        assert line.startswith("DATA "), \
            "Expected DATA or END, got: {!r}".format(line)
        chunk_len = int(line[5:])
        chunk = _recv_exact(sock, chunk_len)
        data.extend(chunk)

    # Read sentinel
    sentinel = _read_line(sock)
    assert sentinel == "."

    # Validate received size matches declared size
    declared_size = int(info)
    assert len(data) == declared_size, \
        "Size mismatch: OK declared {} bytes but received {}".format(
            declared_size, len(data))

    return info, bytes(data)


def send_write_data(sock, path, data):
    """Execute a complete WRITE handshake.

    data must be bytes. Sends WRITE command, reads READY, sends
    DATA/END chunks, reads final response.

    Returns (status_line, payload_lines) from the final response.
    Raises AssertionError if READY is not received.
    """
    send_command(sock, "WRITE {} {}".format(path, len(data)))

    # Read READY or ERR
    ready_line = _read_line(sock)
    if ready_line.startswith("ERR "):
        sentinel = _read_line(sock)
        assert sentinel == "."
        return ready_line, []

    assert ready_line == "READY", \
        "Expected READY, got: {!r}".format(ready_line)

    # Send data in chunks
    CHUNK_SIZE = 4096
    offset = 0
    while offset < len(data):
        chunk = data[offset:offset + CHUNK_SIZE]
        header = "DATA {}\n".format(len(chunk)).encode("iso-8859-1")
        sock.sendall(header + chunk)
        offset += len(chunk)

    # For 0-byte writes, no DATA chunks are sent
    sock.sendall(b"END\n")

    # Read final response
    return read_response(sock)


def send_rename(sock, old_path, new_path):
    """Send a RENAME command in three-line format and read the response.

    Returns (status_line, payload_lines) from read_response().
    """
    msg = "RENAME\n{}\n{}\n".format(old_path, new_path)
    sock.sendall(msg.encode("iso-8859-1"))
    return read_response(sock)


def send_copy(sock, src, dst, flags=""):
    """Send a COPY command in three-line format and read the response.

    flags is an optional string of space-separated keywords (e.g.
    "NOCLONE", "NOREPLACE", "NOCLONE NOREPLACE").

    Returns (status_line, payload_lines) from read_response().
    """
    verb = "COPY"
    if flags:
        verb += " " + flags
    msg = "{}\n{}\n{}\n".format(verb, src, dst)
    sock.sendall(msg.encode("iso-8859-1"))
    return read_response(sock)


def send_append_data(sock, path, data):
    """Execute a complete APPEND handshake.

    data must be bytes. Sends APPEND command, reads READY, sends
    DATA/END chunks, reads final response.

    Returns (status_line, payload_lines) from the final response.
    Raises AssertionError if READY is not received.
    """
    send_command(sock, "APPEND {} {}".format(path, len(data)))

    # Read READY or ERR
    ready_line = _read_line(sock)
    if ready_line.startswith("ERR "):
        sentinel = _read_line(sock)
        assert sentinel == "."
        return ready_line, []

    assert ready_line == "READY", \
        "Expected READY, got: {!r}".format(ready_line)

    # Send data in chunks
    CHUNK_SIZE = 4096
    offset = 0
    while offset < len(data):
        chunk = data[offset:offset + CHUNK_SIZE]
        header = "DATA {}\n".format(len(chunk)).encode("iso-8859-1")
        sock.sendall(header + chunk)
        offset += len(chunk)

    # For 0-byte appends, no DATA chunks are sent
    sock.sendall(b"END\n")

    # Read final response
    return read_response(sock)


def send_raw_write_start(sock, path, declared_size):
    """Send WRITE command and read READY handshake.

    Returns "READY" on success, or the ERR status line on failure.
    On ERR, also reads and discards the sentinel.

    After receiving "READY", the caller is responsible for sending
    DATA/END chunks (correct or malformed) and reading the final
    response.
    """
    send_command(sock, "WRITE {} {}".format(path, declared_size))
    ready_line = _read_line(sock)
    if ready_line.startswith("ERR "):
        sentinel = _read_line(sock)
        assert sentinel == "."
        return ready_line
    assert ready_line == "READY", \
        "Expected READY, got: {!r}".format(ready_line)
    return "READY"


def pre_clean(sock, path):
    """Clear protection and delete a file, ignoring errors.

    Removes stale files from previous interrupted test runs that may
    have protection bits set.
    """
    send_command(sock, "PROTECT {} 00000000".format(path))
    try:
        read_response(sock)
    except Exception:
        pass
    send_command(sock, "DELETE {}".format(path))
    try:
        read_response(sock)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Command-line options
# ---------------------------------------------------------------------------

def pytest_addoption(parser):
    parser.addoption(
        "--host",
        default=os.environ.get("AMIGACTL_HOST", "192.168.6.200"),
        help="IP address of the Amiga running amigactld "
             "(default: AMIGACTL_HOST env or 192.168.6.200)",
    )
    parser.addoption(
        "--port",
        type=int,
        default=int(os.environ.get("AMIGACTL_PORT", "6800")),
        help="TCP port of amigactld "
             "(default: AMIGACTL_PORT env or 6800)",
    )


# ---------------------------------------------------------------------------
# Simple value fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def amiga_host(request):
    """Return the configured daemon host address."""
    return request.config.getoption("--host")


@pytest.fixture
def amiga_port(request):
    """Return the configured daemon port number."""
    return request.config.getoption("--port")


# ---------------------------------------------------------------------------
# Connection fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def raw_connection(amiga_host, amiga_port):
    """Open a TCP connection to amigactld and read the banner.

    Yields ``(sock, banner)`` where *sock* is the connected
    :class:`socket.socket` and *banner* is the decoded banner string
    (without the trailing newline).

    The socket timeout is set to 10 seconds so that tests do not hang
    indefinitely if the daemon has a bug and never sends a sentinel or
    closes the connection.

    The socket is closed automatically on teardown.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(10)
    sock.connect((amiga_host, amiga_port))
    banner = _read_line(sock)
    yield sock, banner
    sock.close()


# ---------------------------------------------------------------------------
# Cleanup fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def cleanup_paths(amiga_host, amiga_port):
    """Fixture that tracks created paths for cleanup.

    Usage: cleanup_paths.add("RAM:testfile.txt")

    On teardown, issues DELETE commands in reverse order via a fresh
    connection. Errors are silently ignored.
    """
    tracker = _CleanupTracker(amiga_host, amiga_port)
    yield tracker
    tracker.cleanup()


class _CleanupTracker:
    """Track files/directories created during a test for cleanup.

    All created paths must be individually registered. Registering only
    a parent directory is not sufficient -- DELETE cannot remove
    non-empty directories.
    """

    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.paths = []

    def add(self, path):
        """Register a path for cleanup on teardown.

        Register paths in creation order (parent directories before
        child files). Cleanup deletes in reverse order.
        """
        self.paths.append(path)

    def cleanup(self):
        if not self.paths:
            return
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(10)
            sock.connect((self.host, self.port))
            _read_line(sock)  # banner
            for path in reversed(self.paths):
                # Clear protection bits so delete-protected files can be removed
                send_command(sock, "PROTECT {} 00000000".format(path))
                try:
                    read_response(sock)
                except Exception:
                    pass
                send_command(sock, "DELETE {}".format(path))
                try:
                    read_response(sock)
                except Exception:
                    pass
            sock.close()
        except Exception:
            pass


class _EnvCleanupTracker:
    """Track environment variables created during a test for cleanup."""

    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.vars = []  # list of (name, volatile_bool) tuples

    def add(self, name, volatile=False):
        """Register a variable for cleanup on teardown."""
        self.vars.append((name, volatile))

    def cleanup(self):
        if not self.vars:
            return
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(10)
            sock.connect((self.host, self.port))
            _read_line(sock)  # banner
            for name, volatile in reversed(self.vars):
                if volatile:
                    send_command(sock, "SETENV VOLATILE {}".format(name))
                else:
                    send_command(sock, "SETENV {}".format(name))
                try:
                    read_response(sock)
                except Exception:
                    pass
            sock.close()
        except Exception:
            pass


@pytest.fixture
def cleanup_env(amiga_host, amiga_port):
    """Fixture that tracks env variables for cleanup.

    Usage: cleanup_env.add("TestVar")
            cleanup_env.add("TestVar", volatile=True)

    On teardown, sends SETENV (delete) commands via a fresh connection.
    """
    tracker = _EnvCleanupTracker(amiga_host, amiga_port)
    yield tracker
    tracker.cleanup()


# ---------------------------------------------------------------------------
# Protocol helpers
# ---------------------------------------------------------------------------

def send_command(sock, cmd):
    """Send a command string to the daemon.

    Appends the required ``\\n`` terminator and encodes as ISO-8859-1.
    """
    sock.sendall((cmd + "\n").encode("iso-8859-1"))


def read_response(sock):
    """Read a complete response from the daemon.

    Reads lines until the sentinel (``".\\n"``) is encountered, performs
    dot-unstuffing on payload lines, and returns a ``(status_line,
    payload_lines)`` tuple.

    *status_line* is a string like ``"OK"`` or ``"ERR 100 Unknown command"``
    (without trailing newline).

    *payload_lines* is a list of unstuffed strings (without trailing
    newlines).  For error responses and commands with no payload, this list
    is empty.
    """
    status_line = _read_line(sock)
    payload_lines = []
    while True:
        line = _read_line(sock)
        if line == ".":
            # Sentinel -- response is complete.
            break
        if line.startswith(".."):
            # Dot-unstuffing: remove the leading escape dot.
            line = line[1:]
        payload_lines.append(line)
    return status_line, payload_lines


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _read_line(sock):
    """Read a single line from *sock*, up to and including ``\\n``.

    Returns the line content as a decoded string with the trailing
    ``\\n`` (and any preceding ``\\r``) stripped.

    Raises :class:`ConnectionError` if EOF is received before a newline.
    """
    buf = bytearray()
    while True:
        byte = sock.recv(1)
        if not byte:
            if buf:
                raise ConnectionError(
                    "EOF before newline; partial data: {!r}".format(bytes(buf))
                )
            raise ConnectionError("EOF while reading line (no data received)")
        if byte == b"\n":
            break
        buf.extend(byte)
    # Strip a trailing \r for telnet compatibility (the daemon should not
    # send \r\n, but be robust).
    line = buf.decode("iso-8859-1")
    if line.endswith("\r"):
        line = line[:-1]
    return line


# ---------------------------------------------------------------------------
# EXEC protocol helpers (binary response)
# ---------------------------------------------------------------------------

def read_exec_response(sock):
    """Read an EXEC binary data response: OK rc=N, DATA/END chunks, sentinel.

    Returns (rc, raw_bytes).

    The OK info field contains ``rc=<N>`` where N is the command's return
    code. raw_bytes is the concatenated content from all DATA chunks.

    Also handles ERR -> sentinel (returns (status_line, b"")).
    Unlike ``read_data_response()``, this does NOT validate that the total
    received bytes match the info field -- EXEC's info field is ``rc=N``,
    not a byte count.
    """
    status_line = _read_line(sock)
    if status_line.startswith("ERR "):
        sentinel = _read_line(sock)
        assert sentinel == "."
        return status_line, b""

    assert status_line.startswith("OK"), \
        "Expected OK or ERR, got: {!r}".format(status_line)
    info = status_line[3:].strip()

    # Parse rc=N from info field
    match = re.match(r"^rc=(-?\d+)$", info)
    assert match, \
        "Expected rc=N in OK info field, got: {!r}".format(info)
    rc = int(match.group(1))

    data = bytearray()
    while True:
        line = _read_line(sock)
        if line == "END":
            break
        assert line.startswith("DATA "), \
            "Expected DATA or END, got: {!r}".format(line)
        chunk_len = int(line[5:])
        chunk = _recv_exact(sock, chunk_len)
        data.extend(chunk)

    # Read sentinel
    sentinel = _read_line(sock)
    assert sentinel == "."

    return rc, bytes(data)


# ---------------------------------------------------------------------------
# High-level connection fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def conn(amiga_host, amiga_port):
    """Provide an AmigaConnection instance for tests.

    The connection is opened before the test and closed afterward.
    Tests that need the high-level client API (e.g. conn.arexx(),
    conn.write()) use this fixture instead of raw_connection.
    """
    connection = AmigaConnection(amiga_host, amiga_port)
    connection.connect()
    yield connection
    connection.close()


# ---------------------------------------------------------------------------
# Port-checking helpers
# ---------------------------------------------------------------------------

def has_port(conn, port_name):
    """Check if a named Exec message port exists on the Amiga.

    Uses the PORTS command to query the live system.
    """
    ports = conn.ports()
    return port_name in ports


@pytest.fixture
def rexx_available(conn):
    """Skip test if the ARexx REXX port is not available."""
    if not has_port(conn, "REXX"):
        pytest.skip("ARexx REXX port not available")


# ---------------------------------------------------------------------------
# Session-scoped SHUTDOWN fixture
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def shutdown_daemon(request):
    """Send SHUTDOWN CONFIRM at end of test session for clean teardown.

    Only effective when ALLOW_REMOTE_SHUTDOWN YES is in daemon config.
    Failure is silently ignored (daemon may not support remote shutdown,
    or may have already exited).
    """
    yield
    host = request.config.getoption("--host")
    port = request.config.getoption("--port")
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect((host, port))
        _read_line(sock)  # banner
        send_command(sock, "SHUTDOWN CONFIRM")
        try:
            read_response(sock)
        except Exception:
            pass
        sock.close()
    except Exception:
        pass
