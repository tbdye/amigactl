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
import socket

import pytest


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
