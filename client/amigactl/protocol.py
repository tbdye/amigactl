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


class ServerError(ProtocolError):
    """Raised when the server returns an ERR status line.

    Attributes:
        err_info: The ERR line content after "ERR " (e.g. "200 Not found").
    """

    def __init__(self, err_info: str) -> None:
        self.err_info = err_info
        super().__init__("ERR {}".format(err_info))


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
      VERSION -> ("OK", "", ["amigactld 0.8.0"])
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


def _read_data_chunks(sock: socket.socket) -> bytes:
    """Read DATA/END chunks from the socket.

    Reads DATA <len> / raw-chunk pairs until END.  If the server sends
    an ERR line mid-stream, raises BinaryTransferError with any partial
    data.

    Returns the concatenated bytes.  The caller is responsible for
    reading the sentinel line that follows END.
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
    return bytes(data)


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
    data = _read_data_chunks(sock)

    # Read sentinel
    sentinel = read_line(sock)
    if sentinel != ".":
        raise ProtocolError(
            "Expected sentinel, got: {!r}".format(sentinel))

    return data


def read_exec_response(sock: socket.socket) -> "Tuple[int, bytes]":
    """Read a full EXEC response: status line + DATA/END chunks + sentinel.

    Reads the OK rc=N status line (or ERR), the binary DATA/END body, and
    the sentinel.  Unlike read_binary_response, this function reads the
    status line itself and does not validate data length against a declared
    file size (EXEC's info field is rc=N, not a byte count).

    Returns (rc, data) where rc is the integer return code and data is
    the raw binary output.

    Raises ProtocolError on framing violations or if the response is ERR.
    """
    status_line = read_line(sock)

    if status_line == "ERR" or status_line.startswith("ERR "):
        # Read sentinel and return the error info for the caller to handle
        sentinel = read_line(sock)
        if sentinel != ".":
            raise ProtocolError(
                "Expected sentinel after ERR, got: {!r}".format(sentinel))
        err_info = status_line[4:]
        raise ServerError(err_info)

    if not status_line.startswith("OK"):
        raise ProtocolError(
            "Expected OK, got: {!r}".format(status_line))

    info = status_line[3:].strip()

    # Parse rc=N from info field
    if not info.startswith("rc="):
        raise ProtocolError(
            "EXEC OK line missing rc= field: {!r}".format(info))
    try:
        rc = int(info[3:])
    except ValueError:
        raise ProtocolError(
            "EXEC OK line has non-numeric rc: {!r}".format(info))

    data = _read_data_chunks(sock)

    # Read sentinel
    sentinel = read_line(sock)
    if sentinel != ".":
        raise ProtocolError(
            "Expected sentinel, got: {!r}".format(sentinel))

    return (rc, data)


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


def _parse_trace_event(text):
    # type: (str) -> dict
    """Parse a tab-separated trace event line into a dict.

    All keys are initialized to defaults so callers can access any key
    without checking for existence, even if the event line is malformed.

    Guaranteed fields (stable API contract for agent consumers):

    ========  ====  ===========  ==========================================
    Field     Type  Default      Description
    ========  ====  ===========  ==========================================
    type      str   ``"event"``  Always ``"event"`` (comments: ``"comment"``)
    raw       str   (input)      Raw event line for debugging
    seq       int   ``0``        Sequence number (0 if unparseable)
    time      str   ``""``       Timestamp string (empty if missing)
    lib       str   ``""``       Library short name (empty if missing)
    func      str   ``""``       Function name (empty if missing)
    task      str   ``""``       Task identifier ``[pid] name``
    args      str   ``""``       Formatted arguments (empty if missing)
    retval    str   ``""``       Return value string (empty if missing)
    status    str   ``"-"``      ``"O"`` ok, ``"E"`` error, ``"-"`` neutral
    ========  ====  ===========  ==========================================
    """
    parts = text.split("\t")
    event = {
        "raw": text, "type": "event",
        "seq": 0, "time": "", "lib": "", "func": "",
        "task": "", "args": "", "retval": "", "status": "-",
    }
    if len(parts) >= 1:
        try:
            event["seq"] = int(parts[0])
        except ValueError:
            event["seq"] = 0
    if len(parts) >= 2:
        event["time"] = parts[1]
    if len(parts) >= 3:
        lib_func = parts[2]
        dot = lib_func.find(".")
        if dot >= 0:
            event["lib"] = lib_func[:dot]
            event["func"] = lib_func[dot + 1:]
        else:
            event["lib"] = ""
            event["func"] = lib_func
    if len(parts) >= 4:
        event["task"] = parts[3]
    if len(parts) >= 5:
        event["args"] = parts[4]
    if len(parts) >= 6:
        event["retval"] = parts[5]
    if len(parts) >= 7:
        event["status"] = parts[6]
    return event


class TraceStreamReader:
    """Non-blocking, stateful line reader for trace event streams.

    Designed for use with select(). The caller sets the socket to
    non-blocking mode and calls try_read_event() when select()
    indicates readability. The reader buffers partial data internally
    and returns complete events when available.

    States:
    - READING_HEADER: Accumulating bytes for the next line
      (DATA <len>, END, ERR, or comment)
    - READING_CHUNK: After seeing DATA <len>, accumulating the
      binary payload

    Assumption: The daemon sends each DATA <len>\\n<payload> pair
    atomically (single send_trace_data_chunk() call). This means
    an ERR line can never appear mid-chunk -- the reader will
    never see partial DATA followed by ERR bytes that would be
    consumed as chunk data. If the connection drops mid-chunk,
    the next recv() returns empty bytes, which is caught as a
    ConnectionError.
    """

    def __init__(self, sock):
        self._sock = sock
        self._buf = bytearray()
        self._state = "header"     # "header" or "chunk"
        self._chunk_remaining = 0
        self._chunk_data = bytearray()

    def try_read_event(self):
        """Try to read one complete trace event.

        Returns:
        - dict: A complete parsed event (type="event" or type="comment")
        - None: Incomplete data, call again when select() fires
        - False: Stream ended (END received and sentinel consumed)

        Raises ProtocolError on framing errors.
        Raises ConnectionError on socket close.
        """
        # Read available data into buffer (non-blocking)
        try:
            data = self._sock.recv(4096)
        except BlockingIOError:
            return None  # No data available
        except socket.timeout:
            return None  # Timeout during drain, no data yet
        except OSError as e:
            raise ProtocolError("Socket error: {}".format(e))

        if not data:
            raise ProtocolError("Connection closed by server")

        self._buf.extend(data)

        # Process buffered data
        return self.drain_buffered()

    def drain_buffered(self):
        """Process buffered data without calling recv().

        Intended for use after try_read_event() when
        has_buffered_data() returns True -- multiple events may
        have arrived in a single recv() call.

        Returns the same values as try_read_event():
        - dict: A complete parsed event
        - None: Incomplete data in buffer
        - False: Stream ended
        """
        while True:
            if self._state == "header":
                # Look for a complete line (terminated by LF)
                idx = self._buf.find(b"\n")
                if idx < 0:
                    return None  # Incomplete line

                line_bytes = bytes(self._buf[:idx])
                del self._buf[:idx + 1]

                line = line_bytes.decode(ENCODING)
                if line.endswith("\r"):
                    line = line[:-1]

                if line.startswith("DATA "):
                    try:
                        self._chunk_remaining = int(line[5:])
                    except ValueError:
                        raise ProtocolError(
                            "Invalid DATA length: {!r}".format(line))
                    self._chunk_data = bytearray()
                    self._state = "chunk"
                    # Fall through to chunk processing below
                elif line == "END":
                    # Consume sentinel line
                    sentinel_idx = self._buf.find(b"\n")
                    if sentinel_idx < 0:
                        # Sentinel not yet received -- wait.
                        self._state = "sentinel"
                        return self._try_sentinel()
                    sentinel = bytes(self._buf[:sentinel_idx])
                    del self._buf[:sentinel_idx + 1]
                    sentinel_str = sentinel.decode(ENCODING).rstrip("\r")
                    if sentinel_str != ".":
                        raise ProtocolError(
                            "Expected sentinel, got: {!r}".format(
                                sentinel_str))
                    return False  # Stream ended
                elif line == "ERR" or line.startswith("ERR "):
                    # Consume sentinel after ERR
                    sentinel_idx = self._buf.find(b"\n")
                    if sentinel_idx < 0:
                        self._state = "err_sentinel"
                        return self._try_err_sentinel()
                    del self._buf[:sentinel_idx + 1]
                    return False  # Stream ended with error
                else:
                    raise ProtocolError(
                        "Unexpected line during TRACE: {!r}".format(
                            line))

            elif self._state == "chunk":
                if len(self._buf) < self._chunk_remaining:
                    return None  # Incomplete chunk

                chunk = bytes(self._buf[:self._chunk_remaining])
                del self._buf[:self._chunk_remaining]
                self._state = "header"

                text = chunk.decode(ENCODING)
                if text.startswith("#"):
                    return {
                        "type": "comment",
                        "text": text[2:] if len(text) > 2 else "",
                    }
                return _parse_trace_event(text)

            elif self._state == "sentinel":
                return self._try_sentinel()

            elif self._state == "err_sentinel":
                return self._try_err_sentinel()

            else:
                raise ProtocolError(
                    "Invalid reader state: {}".format(self._state))

    def _try_sentinel(self):
        """Try to consume the sentinel line after END."""
        idx = self._buf.find(b"\n")
        if idx < 0:
            return None
        sentinel = bytes(self._buf[:idx]).decode(ENCODING).rstrip("\r")
        del self._buf[:idx + 1]
        if sentinel != ".":
            raise ProtocolError(
                "Expected sentinel, got: {!r}".format(sentinel))
        self._state = "header"
        return False

    def _try_err_sentinel(self):
        """Try to consume the sentinel line after ERR."""
        idx = self._buf.find(b"\n")
        if idx < 0:
            return None
        del self._buf[:idx + 1]
        self._state = "header"
        return False

    def has_buffered_data(self):
        """Return True if there is unprocessed data in the buffer.

        The event loop should call drain_buffered() again (without
        recv) when this returns True, because multiple events may
        have arrived in a single recv() call.
        """
        return len(self._buf) > 0
