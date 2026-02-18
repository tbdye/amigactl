"""Wire protocol helpers for the amigactl client.

Handles line reading, response parsing, dot-unstuffing, and command
sending per the amigactl wire protocol specification (PROTOCOL.md).
All wire communication uses ISO-8859-1 encoding.
"""

import socket
from typing import List, Tuple

ENCODING = "iso-8859-1"


class ProtocolError(Exception):
    """Raised on wire protocol violations (unexpected EOF, malformed
    responses, timeouts)."""


def read_line(sock: socket.socket) -> str:
    """Read a single line from the socket, byte-by-byte until LF.

    Strips trailing CR LF or bare LF.  Raises ProtocolError on EOF
    (connection closed before LF) or socket timeout.
    """
    buf = bytearray()
    while True:
        try:
            b = sock.recv(1)
        except socket.timeout:
            raise ProtocolError("Timed out waiting for data from server")
        except OSError as e:
            raise ProtocolError("Socket error: {}".format(e))

        if not b:
            if buf:
                raise ProtocolError(
                    "Connection closed mid-line (partial data: {!r})".format(
                        bytes(buf)
                    )
                )
            raise ProtocolError("Connection closed by server")

        if b == b"\n":
            break
        buf.extend(b)

    # Strip trailing CR (telnet compatibility)
    line = buf.decode(ENCODING)
    if line.endswith("\r"):
        line = line[:-1]
    return line


def read_response(sock: socket.socket) -> Tuple[str, str, List[str]]:
    """Read a complete command response (status line + payload + sentinel).

    Returns (status, info, payload_lines) where:
      - status is "OK" or "ERR"
      - info is the remainder of the status line after the status word
        (empty string if none)
      - payload_lines is a list of dot-unstuffed payload lines (may be
        empty)

    Examples:
      VERSION -> ("OK", "", ["amigactld 0.1.0"])
      PING    -> ("OK", "", [])
      QUIT    -> ("OK", "Goodbye", [])
      error   -> ("ERR", "100 Unknown command", [])
    """
    status_line = read_line(sock)

    if status_line == "OK" or status_line.startswith("OK "):
        status = "OK"
        info = status_line[3:]  # empty if just "OK", rest after "OK "
    elif status_line == "ERR" or status_line.startswith("ERR "):
        status = "ERR"
        info = status_line[4:]  # rest after "ERR "
    else:
        raise ProtocolError(
            "Expected OK or ERR, got: {!r}".format(status_line)
        )

    # Read payload lines until sentinel
    payload_lines = []  # type: List[str]
    while True:
        line = read_line(sock)
        if line == ".":
            # Sentinel -- response complete
            break
        if line.startswith(".."):
            # Dot-unstuff: remove leading dot
            line = line[1:]
        payload_lines.append(line)

    return (status, info, payload_lines)


def send_command(sock: socket.socket, command: str) -> None:
    """Send a command line to the server.

    Appends LF and encodes as ISO-8859-1.
    """
    data = (command + "\n").encode(ENCODING)
    sock.sendall(data)
