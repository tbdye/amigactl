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

from .protocol import ENCODING, ProtocolError, read_line, read_response, send_command


__all__ = [
    "AmigaConnection",
    "AmigactlError",
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
