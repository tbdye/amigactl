"""amigactl -- Python client library for amigactld.

Provides AmigaConnection for communicating with an amigactld daemon
running on an Amiga, plus an exception hierarchy mapping protocol error
codes to Python exceptions.

Usage::

    with AmigaConnection("192.168.6.200") as amiga:
        print(amiga.version())
        amiga.ping()
"""

import socket
from typing import Dict, List, Optional, Tuple, Type

from .protocol import (
    BinaryTransferError, ENCODING, ProtocolError, read_binary_response,
    read_line, read_response, recv_exact, send_command, send_data_chunks,
)


__all__ = [
    "AmigaConnection",
    "AmigactlError",
    "BinaryTransferError",
    "CommandSyntaxError",
    "NotFoundError",
    "PermissionDeniedError",
    "AlreadyExistsError",
    "RemoteIOError",
    "RemoteTimeoutError",
    "InternalError",
    "ProtocolError",
]


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------

class AmigactlError(Exception):
    """Base exception for amigactld error responses.

    Attributes:
        code: Numeric error code from the server (e.g. 100, 201).
        message: Human-readable error message from the server.
    """

    def __init__(self, code: int, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__("ERR {} {}".format(code, message))


class CommandSyntaxError(AmigactlError):
    """Error 100 -- malformed command, unknown command, or command too long."""

    def __init__(self, message: str) -> None:
        super().__init__(100, message)


class NotFoundError(AmigactlError):
    """Error 200 -- file, directory, path, or ARexx port not found."""

    def __init__(self, message: str) -> None:
        super().__init__(200, message)


class PermissionDeniedError(AmigactlError):
    """Error 201 -- operation not permitted."""

    def __init__(self, message: str) -> None:
        super().__init__(201, message)


class AlreadyExistsError(AmigactlError):
    """Error 202 -- target already exists."""

    def __init__(self, message: str) -> None:
        super().__init__(202, message)


class RemoteIOError(AmigactlError):
    """Error 300 -- filesystem I/O failure on the Amiga."""

    def __init__(self, message: str) -> None:
        super().__init__(300, message)


class RemoteTimeoutError(AmigactlError):
    """Error 400 -- operation timed out on the Amiga."""

    def __init__(self, message: str) -> None:
        super().__init__(400, message)


class InternalError(AmigactlError):
    """Error 500 -- unexpected daemon error."""

    def __init__(self, message: str) -> None:
        super().__init__(500, message)


# Map error codes to exception classes.  Unknown codes fall back to
# the base AmigactlError.
_ERROR_MAP = {
    100: CommandSyntaxError,
    200: NotFoundError,
    201: PermissionDeniedError,
    202: AlreadyExistsError,
    300: RemoteIOError,
    400: RemoteTimeoutError,
    500: InternalError,
}  # type: Dict[int, Type[AmigactlError]]


def _raise_for_error(info: str) -> None:
    """Parse an ERR info string and raise the appropriate exception.

    The info string has the form "<code> <message>" (e.g.,
    "100 Unknown command").  If the code is unrecognized, the base
    AmigactlError is raised.
    """
    parts = info.split(None, 1)
    if not parts:
        raise AmigactlError(0, info)

    try:
        code = int(parts[0])
    except ValueError:
        raise AmigactlError(0, info)

    message = parts[1] if len(parts) > 1 else ""
    exc_class = _ERROR_MAP.get(code)
    if exc_class is not None:
        raise exc_class(message)
    raise AmigactlError(code, message)


# ---------------------------------------------------------------------------
# Connection class
# ---------------------------------------------------------------------------

class AmigaConnection:
    """A connection to an amigactld daemon.

    Can be used as a context manager::

        with AmigaConnection("192.168.6.200") as amiga:
            print(amiga.version())

    Or managed manually::

        conn = AmigaConnection("192.168.6.200")
        conn.connect()
        try:
            print(conn.version())
        finally:
            conn.close()
    """

    def __init__(
        self,
        host: str,
        port: int = 6800,
        timeout: int = 30,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock = None  # type: Optional[socket.socket]
        self._banner = None  # type: Optional[str]

    # -- Context manager ---------------------------------------------------

    def __enter__(self) -> "AmigaConnection":
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:  # type: ignore
        self.close()
        return None

    # -- Connection lifecycle ----------------------------------------------

    def connect(self) -> None:
        """Open TCP connection, set timeout, read and validate banner."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect((self.host, self.port))
        except Exception:
            sock.close()
            raise
        self._sock = sock

        # Read banner line (single line, no sentinel)
        banner = read_line(self._sock)
        if not banner.startswith("AMIGACTL "):
            self._sock.close()
            self._sock = None
            raise ProtocolError(
                "Invalid banner: {!r}".format(banner)
            )
        self._banner = banner

    def close(self) -> None:
        """Send QUIT (best-effort) and close the socket."""
        if self._sock is None:
            return
        # Best-effort QUIT so the server can clean up the slot
        try:
            send_command(self._sock, "QUIT")
            read_response(self._sock)
        except Exception:
            pass
        try:
            self._sock.close()
        except Exception:
            pass
        self._sock = None

    @property
    def banner(self) -> Optional[str]:
        """The banner string received on connect, or None if not connected."""
        return self._banner

    # -- Internal helpers --------------------------------------------------

    def _send_command(self, cmd: str) -> Tuple[str, List[str]]:
        """Send a command and read the response.

        Returns (info, payload_lines) on OK.  Raises the appropriate
        AmigactlError subclass on ERR.  Raises ProtocolError on
        framing violations.
        """
        if self._sock is None:
            raise ProtocolError("Not connected")
        send_command(self._sock, cmd)
        status, info, payload = read_response(self._sock)
        if status == "ERR":
            _raise_for_error(info)
        return (info, payload)

    # -- Commands ----------------------------------------------------------

    def version(self) -> str:
        """Send VERSION and return the version string.

        Returns the first payload line (e.g. "amigactld 0.1.0").
        """
        _info, payload = self._send_command("VERSION")
        if not payload:
            raise ProtocolError("VERSION returned no payload")
        return payload[0]

    def ping(self) -> None:
        """Send PING and verify OK response."""
        self._send_command("PING")

    def quit(self) -> None:
        """Send QUIT and close the connection.

        The connection becomes unusable after this call.
        """
        self.close()

    def shutdown(self) -> str:
        """Send SHUTDOWN CONFIRM and return the server's info string.

        Returns the info text from the OK response (e.g. "Shutting down").
        Raises PermissionDeniedError (201) if remote shutdown is not
        permitted, or CommandSyntaxError (100) on protocol issues.
        """
        info, _payload = self._send_command("SHUTDOWN CONFIRM")
        # Connection will be closed by the server after this
        try:
            self._sock.close()
        except Exception:
            pass
        self._sock = None
        return info

    # -- File operations ---------------------------------------------------

    def dir(self, path: str, recursive: bool = False) -> List[dict]:
        """List directory contents.

        Returns a list of dicts with keys: type, name, size, protection,
        datestamp.
        """
        cmd = "DIR {}".format(path)
        if recursive:
            cmd += " RECURSIVE"
        info, payload = self._send_command(cmd)
        entries = []
        for line in payload:
            parts = line.split("\t")
            if len(parts) != 5:
                raise ProtocolError(
                    "DIR entry has {} fields, expected 5: {!r}".format(
                        len(parts), line))
            try:
                size = int(parts[2])
            except ValueError:
                raise ProtocolError(
                    "DIR entry has non-numeric size: {!r}".format(line))
            entries.append({
                "type": parts[0],
                "name": parts[1],
                "size": size,
                "protection": parts[3],
                "datestamp": parts[4],
            })
        return entries

    def stat(self, path: str) -> dict:
        """Get file/directory metadata.

        Returns a dict with keys: type, name, size, protection,
        datestamp, comment.
        """
        info, payload = self._send_command("STAT {}".format(path))
        result = {}
        for line in payload:
            key, _, value = line.partition("=")
            result[key] = value
        if "size" in result:
            try:
                result["size"] = int(result["size"])
            except ValueError:
                raise ProtocolError(
                    "STAT has non-numeric size: {!r}".format(result["size"]))
        return result

    def read(self, path: str) -> bytes:
        """Download a file.

        Returns the file contents as bytes.
        """
        if self._sock is None:
            raise ProtocolError("Not connected")
        send_command(self._sock, "READ {}".format(path))
        status_line = read_line(self._sock)
        if status_line == "ERR" or status_line.startswith("ERR "):
            # Read sentinel and raise
            read_line(self._sock)  # sentinel
            _raise_for_error(status_line[4:])
        if not status_line.startswith("OK"):
            raise ProtocolError(
                "Expected OK, got: {!r}".format(status_line))

        info = status_line[3:].strip()
        try:
            data = read_binary_response(self._sock)
        except BinaryTransferError as e:
            _raise_for_error(e.err_info)
            raise  # unreachable; _raise_for_error always raises

        try:
            declared_size = int(info)
        except ValueError:
            raise ProtocolError(
                "READ OK line missing numeric size: {!r}".format(info))
        if len(data) != declared_size:
            raise ProtocolError(
                "Size mismatch: server declared {} bytes but sent {}".format(
                    declared_size, len(data)))
        return data

    def write(self, path: str, data: bytes) -> int:
        """Upload a file.

        data must be bytes. The file is written atomically on the Amiga.
        Returns the number of bytes written.
        """
        if self._sock is None:
            raise ProtocolError("Not connected")
        send_command(self._sock,
                     "WRITE {} {}".format(path, len(data)))

        # Read READY or ERR
        line = read_line(self._sock)
        if line == "ERR" or line.startswith("ERR "):
            read_line(self._sock)  # sentinel
            _raise_for_error(line[4:])
        if line != "READY":
            raise ProtocolError(
                "Expected READY, got: {!r}".format(line))

        # Send DATA chunks + END
        send_data_chunks(self._sock, data)

        # Read final response
        status, info, payload = read_response(self._sock)
        if status == "ERR":
            _raise_for_error(info)
        stripped = info.strip()
        if not stripped:
            return 0
        try:
            return int(stripped)
        except ValueError:
            raise ProtocolError(
                "WRITE OK line missing numeric size: {!r}".format(info))

    def delete(self, path: str) -> None:
        """Delete a file or empty directory."""
        self._send_command("DELETE {}".format(path))

    def rename(self, old_path: str, new_path: str) -> None:
        """Rename/move a file or directory."""
        if self._sock is None:
            raise ProtocolError("Not connected")
        msg = "RENAME\n{}\n{}\n".format(old_path, new_path)
        self._sock.sendall(msg.encode(ENCODING))

        status, info, payload = read_response(self._sock)
        if status == "ERR":
            _raise_for_error(info)

    def makedir(self, path: str) -> None:
        """Create a directory."""
        self._send_command("MAKEDIR {}".format(path))

    def protect(self, path: str, value: Optional[str] = None) -> str:
        """Get or set protection bits.

        If value is None, returns current protection as hex string.
        If value is a hex string, sets protection and returns new value.
        """
        if value is not None:
            cmd = "PROTECT {} {}".format(path, value)
        else:
            cmd = "PROTECT {}".format(path)

        info, payload = self._send_command(cmd)
        result = {}
        for line in payload:
            key, _, val = line.partition("=")
            result[key] = val
        return result.get("protection", "")
