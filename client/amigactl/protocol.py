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


class BinaryTransferError(ProtocolError):
    """Raised when ERR is received mid-stream during binary DATA/END transfer.

    Attributes:
        err_info: The ERR line content after "ERR " (e.g. "300 Read failed").
        partial_data: Bytes received before the error.
    """

    def __init__(self, err_info: str, partial_data: bytes) -> None:
        self.err_info = err_info
        self.partial_data = partial_data
        super().__init__(
            "Server error during binary transfer: ERR {}".format(err_info))


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


def recv_exact(sock: socket.socket, nbytes: int) -> bytes:
    """Receive exactly nbytes from sock.

    Raises ProtocolError on EOF or socket timeout.
    """
    buf = bytearray()
    while len(buf) < nbytes:
        try:
            chunk = sock.recv(nbytes - len(buf))
        except socket.timeout:
            raise ProtocolError(
                "Timed out reading {} bytes".format(nbytes))
        except OSError as e:
            raise ProtocolError("Socket error: {}".format(e))
        if not chunk:
            raise ProtocolError(
                "Connection closed after {}/{} bytes".format(
                    len(buf), nbytes))
        buf.extend(chunk)
    return bytes(buf)


def read_binary_response(sock: socket.socket) -> bytes:
    """Read DATA/END chunks + sentinel after an OK status line.

    Assumes the caller has already read and validated the OK status line.
    Reads DATA <len> / raw chunk pairs until END, then reads the
    sentinel line.

    Returns the concatenated bytes from all DATA chunks.

    If the server sends an ERR line mid-stream (e.g. I/O error during
    read), raises BinaryTransferError with the ERR info and any partial
    data received so far.

    Raises ProtocolError on framing errors (unexpected lines, EOF,
    missing sentinel).
    """
    data = bytearray()
    while True:
        line = read_line(sock)
        if line == "END":
            break
        if line == "ERR" or line.startswith("ERR "):
            err_info = line[4:]
            # Read sentinel after ERR
            sentinel = read_line(sock)
            if sentinel != ".":
                raise ProtocolError(
                    "Expected sentinel after ERR, got: {!r}".format(
                        sentinel))
            raise BinaryTransferError(err_info, bytes(data))
        if not line.startswith("DATA "):
            raise ProtocolError(
                "Expected DATA or END, got: {!r}".format(line))
        try:
            chunk_len = int(line[5:])
        except ValueError:
            raise ProtocolError(
                "Invalid DATA chunk length: {!r}".format(line))
        chunk = recv_exact(sock, chunk_len)
        data.extend(chunk)

    # Read sentinel
    sentinel = read_line(sock)
    if sentinel != ".":
        raise ProtocolError(
            "Expected sentinel, got: {!r}".format(sentinel))

    return bytes(data)


def send_data_chunks(sock: socket.socket, data: bytes, chunk_size: int = 4096) -> None:
    """Send data as DATA/END chunks to the server.

    data must be bytes. Sends DATA <len> header + raw bytes for each
    chunk, then sends END.
    """
    offset = 0
    while offset < len(data):
        chunk = data[offset:offset + chunk_size]
        header = "DATA {}\n".format(len(chunk)).encode(ENCODING)
        sock.sendall(header + chunk)
        offset += len(chunk)
    sock.sendall(b"END\n")
