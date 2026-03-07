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
from typing import Callable, Dict, List, Optional, Tuple, Type

from .protocol import (
    BinaryTransferError, ENCODING, ProtocolError, ServerError,
    TraceStreamReader, _parse_trace_event,
    read_binary_response,
    read_exec_response, read_line, read_response, recv_exact, send_command,
    send_data_chunks,
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
    "ServerError",
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
# Raw trace session
# ---------------------------------------------------------------------------

class RawTraceSession:
    """Context manager for a raw trace session.

    Saves the socket timeout on entry and restores it on exit.
    Provides the socket and a TraceStreamReader for non-blocking reads.
    """

    def __init__(self, sock, old_timeout):
        self.sock = sock
        self.reader = TraceStreamReader(sock)
        self._old_timeout = old_timeout

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        try:
            self.sock.settimeout(self._old_timeout)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Blocking trace event reader
# ---------------------------------------------------------------------------

def read_one_trace_event(sock):
    """Read one trace event from the socket (BLOCKING).

    Returns an event dict on success, or None if END was received
    (stream terminated). Raises ProtocolError on framing errors.

    This uses the blocking read_line() and is intended for the
    callback-based trace_start() API, NOT the interactive viewer.
    """
    line = read_line(sock)
    if line.startswith("DATA "):
        try:
            chunk_len = int(line[5:])
        except ValueError:
            raise ProtocolError(
                "Invalid DATA chunk length: {!r}".format(line))
        chunk = recv_exact(sock, chunk_len)
        text = chunk.decode(ENCODING)
        if text.startswith("#"):
            return {
                "type": "comment",
                "text": text[2:] if len(text) > 2 else "",
            }
        return _parse_trace_event(text)
    elif line == "END":
        sentinel = read_line(sock)
        if sentinel != ".":
            raise ProtocolError(
                "Expected sentinel, got: {!r}".format(sentinel))
        return None
    elif line == "ERR" or line.startswith("ERR "):
        read_line(sock)  # sentinel
        return None
    else:
        raise ProtocolError(
            "Unexpected line during TRACE: {!r}".format(line))


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

    def __repr__(self) -> str:
        state = "connected" if self._sock is not None else "disconnected"
        return "AmigaConnection({!r}, port={}, {})".format(
            self.host, self.port, state)

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

    def reboot(self) -> str:
        """Send REBOOT CONFIRM and return the server's info string.

        Returns the info text from the OK response (e.g. "Rebooting").
        ColdReboot() may kill the TCP stack before the response arrives,
        so ConnectionResetError and BrokenPipeError are treated as
        success (the reboot happened).  Raises PermissionDeniedError
        (201) if remote reboot is not permitted, or CommandSyntaxError
        (100) on protocol issues.  The connection is closed after this
        call.
        """
        try:
            info, _payload = self._send_command("REBOOT CONFIRM")
        except (ProtocolError, OSError):
            # ColdReboot() killed the connection before the OK arrived
            info = "Rebooting"
        try:
            self._sock.close()
        except Exception:
            pass
        self._sock = None
        return info

    def uptime(self) -> int:
        """Send UPTIME and return daemon uptime in seconds.

        Returns a non-negative integer representing the number of seconds
        the daemon has been running.
        """
        _info, payload = self._send_command("UPTIME")
        result = {}
        for line in payload:
            key, _, value = line.partition("=")
            result[key] = value
        try:
            return int(result["seconds"])
        except (KeyError, ValueError):
            raise ProtocolError("UPTIME missing seconds field")

    # -- File operations ---------------------------------------------------

    def dir(self, path: str, recursive: bool = False) -> List[dict]:
        """List directory contents.

        Returns a list of dicts with keys: type (str, "FILE" or "DIR"),
        name (str), size (int), protection (8-digit hex str), datestamp
        (str, "YYYY-MM-DD HH:MM:SS" in local Amiga time).
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

        Returns a dict with keys: type, name, size (int), protection
        (8-digit hex str), datestamp (str, "YYYY-MM-DD HH:MM:SS" in local
        Amiga time), comment (str, may be empty).
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

    def read(self, path: str, offset: Optional[int] = None, length: Optional[int] = None) -> bytes:
        """Download a file (or partial file).

        Returns the file contents as bytes.

        offset: Start reading at this byte offset (default: 0).
        length: Read at most this many bytes (default: entire file).
        """
        if self._sock is None:
            raise ProtocolError("Not connected")
        cmd = "READ {}".format(path)
        if offset is not None:
            cmd += " OFFSET {}".format(offset)
        if length is not None:
            cmd += " LENGTH {}".format(length)
        send_command(self._sock, cmd)
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
            raise ProtocolError("WRITE OK line missing byte count")
        try:
            return int(stripped)
        except ValueError:
            raise ProtocolError(
                "WRITE OK line missing numeric size: {!r}".format(info))

    def append(self, path: str, data: bytes) -> int:
        """Append data to an existing file on the Amiga.

        data must be bytes. The file must already exist.
        Returns the number of bytes appended.
        """
        if self._sock is None:
            raise ProtocolError("Not connected")
        send_command(self._sock,
                     "APPEND {} {}".format(path, len(data)))

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
            raise ProtocolError("APPEND OK line missing byte count")
        try:
            return int(stripped)
        except ValueError:
            raise ProtocolError(
                "APPEND OK line missing numeric size: {!r}".format(info))

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

    def copy(self, src: str, dst: str, noclone: bool = False, noreplace: bool = False) -> None:
        """Copy a file on the Amiga.

        Copies src to dst. By default, preserves metadata (protection
        bits, datestamp, comment).

        noclone: If True, do not copy metadata.
        noreplace: If True, fail if dst already exists.
        """
        if self._sock is None:
            raise ProtocolError("Not connected")
        flags = ""
        if noclone:
            flags += " NOCLONE"
        if noreplace:
            flags += " NOREPLACE"
        msg = "COPY{}\n{}\n{}\n".format(flags, src, dst)
        self._sock.sendall(msg.encode(ENCODING))

        status, info, payload = read_response(self._sock)
        if status == "ERR":
            _raise_for_error(info)

    def makedir(self, path: str) -> None:
        """Create a directory."""
        self._send_command("MAKEDIR {}".format(path))

    def protect(self, path: str, value: Optional[str] = None) -> str:
        """Get or set AmigaOS protection bits.

        If value is None, returns current protection as an 8-digit hex string.
        If value is an 8-digit hex string, sets protection and returns the new
        value.

        AmigaOS protection bits have INVERTED semantics for bits 0-3:
        a SET bit means the operation is DENIED (opposite of Unix).

        Bit layout (32-bit hex, right to left):
            Bit 0: Delete   (set = delete denied)
            Bit 1: Execute  (set = execute denied)
            Bit 2: Write    (set = write denied)
            Bit 3: Read     (set = read denied)
            Bit 4: Archive  (set = archived)
            Bit 5: Pure     (set = re-entrant)
            Bit 6: Script   (set = script file)
            Bit 7: Hold     (set = hold in memory)

        Examples:
            "00000000" -- all operations allowed (default for new files)
            "0000000f" -- all RWED operations denied
            "00000001" -- delete denied, read/write/execute allowed
            "00000008" -- read denied, delete/write/execute allowed

        To make a file read-only (deny write and delete):
            protect(path, "00000005")  # bits 0 (delete) + 2 (write) set
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

    # -- Execution and process management ----------------------------------

    def execute(self, command: str,
                timeout: Optional[int] = None,
                cd: Optional[str] = None) -> Tuple[int, str]:
        """Execute a CLI command synchronously.

        Returns (rc, output) where rc is the AmigaOS return code (int)
        and output is the captured stdout decoded from ISO-8859-1.

        If timeout is specified, sets the socket timeout for this command
        (restores the original timeout afterward).

        If cd is specified, prepends CD=<path> to the command.
        """
        if self._sock is None:
            raise ProtocolError("Not connected")

        cmd = "EXEC "
        if cd is not None:
            cmd += "CD={} ".format(cd)
        cmd += command

        if timeout is not None:
            old_timeout = self._sock.gettimeout()
            self._sock.settimeout(timeout)
        try:
            send_command(self._sock, cmd)
            try:
                rc, data = read_exec_response(self._sock)
            except ServerError as e:
                _raise_for_error(e.err_info)
                raise  # unreachable
            except BinaryTransferError as e:
                _raise_for_error(e.err_info)
                raise  # unreachable
        finally:
            if timeout is not None:
                self._sock.settimeout(old_timeout)

        output = data.decode(ENCODING)
        return (rc, output)

    def execute_async(self, command: str,
                      cd: Optional[str] = None) -> int:
        """Launch a command asynchronously.

        Returns the daemon-assigned process ID (int).
        """
        cmd = "EXEC ASYNC "
        if cd is not None:
            cmd += "CD={} ".format(cd)
        cmd += command

        info, _payload = self._send_command(cmd)
        try:
            return int(info.strip())
        except ValueError:
            raise ProtocolError(
                "EXEC ASYNC OK line missing numeric ID: {!r}".format(info))

    def proclist(self) -> List[dict]:
        """List daemon-launched processes.

        Returns a list of dicts with keys: id (int), command (str),
        status (str), rc (int or None).
        """
        _info, payload = self._send_command("PROCLIST")
        entries = []
        for line in payload:
            parts = line.split("\t")
            if len(parts) != 4:
                raise ProtocolError(
                    "PROCLIST entry has {} fields, expected 4: {!r}".format(
                        len(parts), line))
            try:
                proc_id = int(parts[0])
            except ValueError:
                raise ProtocolError(
                    "PROCLIST entry has non-numeric id: {!r}".format(line))
            rc_str = parts[3]
            if rc_str == "-":
                rc = None
            else:
                try:
                    rc = int(rc_str)
                except ValueError:
                    raise ProtocolError(
                        "PROCLIST entry has non-numeric rc: {!r}".format(line))
            entries.append({
                "id": proc_id,
                "command": parts[1],
                "status": parts[2],
                "rc": rc,
            })
        return entries

    def procstat(self, proc_id: int) -> dict:
        """Get status of a specific tracked process.

        Returns a dict with keys: id (int), command (str),
        status (str), rc (int or None).
        """
        _info, payload = self._send_command("PROCSTAT {}".format(proc_id))
        result = {}  # type: dict
        for line in payload:
            key, _, value = line.partition("=")
            result[key] = value
        # Convert types
        if "id" in result:
            try:
                result["id"] = int(result["id"])
            except ValueError:
                raise ProtocolError(
                    "PROCSTAT id is non-numeric: {!r}".format(result["id"]))
        if "rc" in result:
            if result["rc"] == "-":
                result["rc"] = None
            else:
                try:
                    result["rc"] = int(result["rc"])
                except ValueError:
                    raise ProtocolError(
                        "PROCSTAT rc is non-numeric: {!r}".format(result["rc"]))
        return result

    def signal(self, proc_id: int, sig: str = "CTRL_C") -> None:
        """Send a break signal to a tracked process."""
        cmd = "SIGNAL {}".format(proc_id)
        if sig != "CTRL_C":
            cmd += " {}".format(sig)
        self._send_command(cmd)

    def kill(self, proc_id: int) -> None:
        """Force-terminate a tracked process."""
        self._send_command("KILL {}".format(proc_id))

    # -- System information ------------------------------------------------

    def sysinfo(self) -> dict:
        """Get system information.

        Returns a dict of key=value pairs. Memory values (chip_free,
        fast_free, total_free, chip_total, fast_total, chip_largest,
        fast_largest) are returned as int. Version strings
        (exec_version, kickstart, bsdsocket) are returned as str.
        """
        _info, payload = self._send_command("SYSINFO")
        result = {}
        for line in payload:
            key, _, value = line.partition("=")
            result[key] = value
        # Convert memory values to int
        _MEMORY_KEYS = {
            "chip_free", "fast_free", "total_free",
            "chip_total", "fast_total", "chip_largest", "fast_largest",
        }
        for key in _MEMORY_KEYS:
            if key in result:
                try:
                    result[key] = int(result[key])
                except ValueError:
                    raise ProtocolError(
                        "SYSINFO has non-numeric {}: {!r}".format(
                            key, result[key]))
        return result

    def libver(self, name: str) -> dict:
        """Get the version of an Amiga library or device.

        name: Library name (e.g. "exec.library", "timer.device").

        Returns a dict with keys:
            name: Library/device name (str).
            version: Version string "major.minor" (str).
        """
        info, payload = self._send_command(
            "LIBVER {}".format(name))
        result = {}
        for line in payload:
            key, _, value = line.partition("=")
            result[key] = value
        return result

    def env(self, name: str) -> str:
        """Get an AmigaOS environment variable.

        Returns the variable's value as a string.

        Raises NotFoundError if the variable does not exist.
        """
        info, payload = self._send_command(
            "ENV {}".format(name))
        result = {}
        for line in payload:
            key, _, value = line.partition("=")
            result[key] = value
        return result.get("value", "")

    def setenv(self, name: str, value: Optional[str] = None,
               volatile: bool = False) -> None:
        """Set or delete an AmigaOS environment variable.

        name: Variable name.
        value: Value to set. None to delete the variable.
        volatile: If False (default), the variable is saved to both
                  ENV: (current session) and ENVARC: (persists across
                  reboots -- the AmigaOS equivalent of writing to disk).
                  If True, the variable is set in ENV: only and will be
                  lost when the Amiga reboots.
        """
        if value is not None:
            if volatile:
                cmd = "SETENV VOLATILE {} {}".format(name, value)
            else:
                cmd = "SETENV {} {}".format(name, value)
        else:
            if volatile:
                cmd = "SETENV VOLATILE {}".format(name)
            else:
                cmd = "SETENV {}".format(name)
        self._send_command(cmd)

    def assigns(self) -> dict:
        """List logical assigns.

        Returns a dict mapping assign names (with trailing colon) to
        path strings.
        """
        _info, payload = self._send_command("ASSIGNS")
        result = {}
        for line in payload:
            parts = line.split("\t", 1)
            if len(parts) != 2:
                raise ProtocolError(
                    "ASSIGNS entry missing tab separator: {!r}".format(line))
            result[parts[0]] = parts[1]
        return result

    def assign(self, name: str, path: Optional[str] = None,
               mode: Optional[str] = None) -> None:
        """Create, replace, or remove a logical assign.

        name must include trailing colon (e.g., "TEST:").
        mode: None (lock-based, default), "late", or "add".
              Raises ValueError for any other value.
        path: target path. If None, removes the assign.
        """
        if mode is not None and mode not in ("late", "add"):
            raise ValueError(
                "mode must be None, 'late', or 'add', got: {!r}".format(mode))
        if path is not None:
            if mode == "late":
                self._send_command(
                    "ASSIGN LATE {} {}".format(name, path))
            elif mode == "add":
                self._send_command(
                    "ASSIGN ADD {} {}".format(name, path))
            else:
                self._send_command(
                    "ASSIGN {} {}".format(name, path))
        else:
            self._send_command("ASSIGN {}".format(name))

    def ports(self) -> List[str]:
        """List active Exec message ports.

        Returns a list of port name strings.
        """
        _info, payload = self._send_command("PORTS")
        return payload

    def volumes(self) -> List[dict]:
        """List mounted volumes.

        Returns a list of dicts with keys: name (str), used (int),
        free (int), capacity (int), blocksize (int).
        """
        _info, payload = self._send_command("VOLUMES")
        entries = []
        for line in payload:
            parts = line.split("\t")
            if len(parts) != 5:
                raise ProtocolError(
                    "VOLUMES entry has {} fields, expected 5: {!r}".format(
                        len(parts), line))
            try:
                entries.append({
                    "name": parts[0],
                    "used": int(parts[1]),
                    "free": int(parts[2]),
                    "capacity": int(parts[3]),
                    "blocksize": int(parts[4]),
                })
            except ValueError:
                raise ProtocolError(
                    "VOLUMES entry has non-numeric field: {!r}".format(line))
        return entries

    def tasks(self) -> List[dict]:
        """List running tasks/processes.

        Returns a list of dicts with keys: name (str), type (str),
        priority (int), state (str), stacksize (int).
        """
        _info, payload = self._send_command("TASKS")
        entries = []
        for line in payload:
            parts = line.split("\t")
            if len(parts) != 5:
                raise ProtocolError(
                    "TASKS entry has {} fields, expected 5: {!r}".format(
                        len(parts), line))
            try:
                entries.append({
                    "name": parts[0],
                    "type": parts[1],
                    "priority": int(parts[2]),
                    "state": parts[3],
                    "stacksize": int(parts[4]),
                })
            except ValueError:
                raise ProtocolError(
                    "TASKS entry has non-numeric field: {!r}".format(line))
        return entries

    def devices(self) -> List[dict]:
        """List Exec devices.

        Returns a list of dicts with keys:
            name: Device name (str).
            version: Version string "major.minor" (str).
        """
        _info, payload = self._send_command("DEVICES")
        entries = []
        for line in payload:
            parts = line.split("\t")
            if len(parts) != 2:
                raise ProtocolError(
                    "DEVICES entry has {} fields, expected 2: {!r}".format(
                        len(parts), line))
            entries.append({
                "name": parts[0],
                "version": parts[1],
            })
        return entries

    def capabilities(self) -> dict:
        """Get daemon capabilities and supported commands.

        Returns a dict with keys:
            version: Daemon version (str).
            protocol: Protocol version (str).
            max_clients: Maximum simultaneous clients (int).
            max_cmd_len: Maximum command line length (int).
            commands: Comma-separated list of supported commands (str).
        """
        _info, payload = self._send_command("CAPABILITIES")
        result = {}
        for line in payload:
            key, _, value = line.partition("=")
            result[key] = value
        for key in ("max_clients", "max_cmd_len"):
            if key in result:
                try:
                    result[key] = int(result[key])
                except ValueError:
                    raise ProtocolError(
                        "CAPABILITIES has non-numeric {}: {!r}".format(
                            key, result[key]))
        return result

    # -- File operations (continued) ---------------------------------------

    def setdate(self, path: str, datestamp: Optional[str] = None) -> str:
        """Set file/directory datestamp.

        datestamp is a string in YYYY-MM-DD HH:MM:SS format.
        If datestamp is None, the daemon uses the current system time.
        Returns the applied datestamp string.
        """
        if datestamp is not None:
            cmd = "SETDATE {} {}".format(path, datestamp)
        else:
            cmd = "SETDATE {}".format(path)
        info, payload = self._send_command(cmd)
        result = {}
        for line in payload:
            key, _, value = line.partition("=")
            result[key] = value
        return result.get("datestamp", "")

    def checksum(self, path: str) -> dict:
        """Compute CRC32 checksum of a remote file.

        Returns a dict with keys:
            crc32: 8-character lowercase hex string (e.g. "a1b2c3d4").
                   To convert to an integer: int(result["crc32"], 16).
                   Matches Python's zlib.crc32() & 0xFFFFFFFF.
            size: File size in bytes (int).
        """
        info, payload = self._send_command(
            "CHECKSUM {}".format(path))
        result = {}
        for line in payload:
            key, _, value = line.partition("=")
            result[key] = value
        if "size" in result:
            try:
                result["size"] = int(result["size"])
            except ValueError:
                raise ProtocolError(
                    "CHECKSUM has non-numeric size: {!r}".format(
                        result["size"]))
        return result

    def setcomment(self, path: str, comment: str) -> None:
        """Set the file comment on a remote file.

        path: Amiga file path.
        comment: Comment string (empty string to clear).
        """
        self._send_command("SETCOMMENT {}\t{}".format(path, comment))

    # -- ARexx and file streaming ------------------------------------------

    def arexx(self, port: str, command: str,
              timeout: int = 35) -> Tuple[int, str]:
        """Send an ARexx command to a named port.

        Returns (rc, result_string) where rc is the ARexx return code
        (0=success) and result_string is the RESULT string decoded from
        ISO-8859-1 (empty if the target returned no result).

        The daemon has a 30-second timeout for ARexx replies.  The
        default socket timeout of 35 seconds gives the daemon time to
        return ERR 400 on timeout before the client gives up.
        """
        if self._sock is None:
            raise ProtocolError("Not connected")

        cmd = "AREXX {} {}".format(port, command)

        old_timeout = self._sock.gettimeout()
        self._sock.settimeout(timeout)
        try:
            send_command(self._sock, cmd)
            try:
                rc, data = read_exec_response(self._sock)
            except ServerError as e:
                _raise_for_error(e.err_info)
                raise  # unreachable
            except BinaryTransferError as e:
                _raise_for_error(e.err_info)
                raise  # unreachable
        finally:
            self._sock.settimeout(old_timeout)

        result = data.decode(ENCODING)
        return (rc, result)

    def tail(self, path: str,
             callback: Callable[[bytes], None]) -> None:
        """Stream file appends to callback.

        Sends TAIL <path>, then blocks reading DATA chunks as the file
        grows on the Amiga.  Each chunk is passed to callback as bytes.
        Returns when the server sends END (after a STOP) or raises on
        ERR.

        Does NOT catch KeyboardInterrupt -- the caller should catch it
        and call stop_tail() to terminate the stream cleanly.
        """
        if self._sock is None:
            raise ProtocolError("Not connected")

        send_command(self._sock, "TAIL {}".format(path))

        # Read OK <current_size> or ERR
        status_line = read_line(self._sock)
        if status_line == "ERR" or status_line.startswith("ERR "):
            # Read sentinel and raise
            read_line(self._sock)  # sentinel
            _raise_for_error(status_line[4:])
        if not status_line.startswith("OK"):
            raise ProtocolError(
                "Expected OK, got: {!r}".format(status_line))

        # OK line is "OK <current_size>" -- we don't need the size
        # but it's available in status_line[3:].strip() if needed.

        old_timeout = self._sock.gettimeout()
        try:
            # Block indefinitely waiting for DATA chunks
            self._sock.settimeout(None)

            while True:
                line = read_line(self._sock)
                if line.startswith("DATA "):
                    try:
                        chunk_len = int(line[5:])
                    except ValueError:
                        raise ProtocolError(
                            "Invalid DATA chunk length: {!r}".format(line))
                    chunk = recv_exact(self._sock, chunk_len)
                    callback(chunk)
                elif line == "END":
                    # Stream complete -- read sentinel
                    sentinel = read_line(self._sock)
                    if sentinel != ".":
                        raise ProtocolError(
                            "Expected sentinel, got: {!r}".format(
                                sentinel))
                    return
                elif line == "ERR" or line.startswith("ERR "):
                    # Error during stream (e.g. file deleted)
                    sentinel = read_line(self._sock)
                    if sentinel != ".":
                        raise ProtocolError(
                            "Expected sentinel after ERR, got: {!r}"
                            .format(sentinel))
                    _raise_for_error(line[4:])
                else:
                    raise ProtocolError(
                        "Unexpected line during TAIL: {!r}".format(line))
        finally:
            self._sock.settimeout(old_timeout)

    def stop_tail(self) -> None:
        """Send STOP during an active TAIL stream and drain the response.

        Sends STOP to the server, then reads and discards any remaining
        DATA chunks until END + sentinel.  After this call, the
        connection is back in normal command mode.
        """
        if self._sock is None:
            raise ProtocolError("Not connected")

        send_command(self._sock, "STOP")

        # Drain remaining DATA chunks until END + sentinel
        old_timeout = self._sock.gettimeout()
        self._sock.settimeout(10)  # 10s timeout for drain
        try:
            while True:
                line = read_line(self._sock)
                if line.startswith("DATA "):
                    try:
                        chunk_len = int(line[5:])
                    except ValueError:
                        raise ProtocolError(
                            "Invalid DATA chunk length: {!r}".format(line))
                    recv_exact(self._sock, chunk_len)
                elif line == "END":
                    sentinel = read_line(self._sock)
                    if sentinel != ".":
                        raise ProtocolError(
                            "Expected sentinel, got: {!r}".format(sentinel))
                    return
                elif line == "ERR" or line.startswith("ERR "):
                    sentinel = read_line(self._sock)
                    if sentinel != ".":
                        raise ProtocolError(
                            "Expected sentinel after ERR, got: {!r}"
                            .format(sentinel))
                    # Stream ended with error -- still drained, return
                    return
                else:
                    raise ProtocolError(
                        "Unexpected line during STOP drain: {!r}".format(
                            line))
        finally:
            self._sock.settimeout(old_timeout)

    # -- Library call tracing (atrace) -------------------------------------

    def trace_status(self):
        # type: () -> dict
        """Query atrace status.

        Returns a dict with keys:
            loaded (bool), enabled (bool), patches (int),
            events_produced (int), events_consumed (int),
            events_dropped (int), buffer_capacity (int),
            buffer_used (int), filter_task (str or absent),
            noise_disabled (int or absent),
            anchor_version (int or absent),
            eclock_freq (int or absent).

        filter_task is a hex string like "0x0e300200" when a task
        filter is active (during TRACE RUN), or "0x00000000" when
        no filter is set.  Only present when atrace version >= 2.

        noise_disabled is the count of noise functions currently
        disabled.  Only present when atrace is loaded.

        anchor_version is the atrace kernel module version (e.g. 3
        for Phase 6+).  Only present when atrace is loaded.

        eclock_freq is the EClock frequency in Hz (e.g. 709379 for
        PAL).  Only present when anchor_version >= 3.

        Integer fields are only present when atrace is loaded.
        """
        info, payload = self._send_command("TRACE STATUS")

        result = {}  # type: dict
        for line in payload:
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip()

            if key == "loaded":
                result["loaded"] = val == "1"
            elif key == "enabled":
                result["enabled"] = val == "1"
            elif key in ("patches", "events_produced", "events_consumed",
                          "events_dropped", "buffer_capacity", "buffer_used"):
                try:
                    result[key] = int(val)
                except ValueError:
                    result[key] = 0
            elif key == "filter_task":
                result["filter_task"] = val  # hex string like "0x0e300200"
            elif key == "noise_disabled":
                try:
                    result["noise_disabled"] = int(val)
                except ValueError:
                    result["noise_disabled"] = 0
            elif key == "anchor_version":
                try:
                    result["anchor_version"] = int(val)
                except ValueError:
                    result["anchor_version"] = 0
            elif key == "eclock_freq":
                try:
                    result["eclock_freq"] = int(val)
                except ValueError:
                    result["eclock_freq"] = 0
            elif key.startswith("patch_"):
                # patch_0=exec.FindPort enabled=1
                if "patch_list" not in result:
                    result["patch_list"] = []
                parts = val.split()
                entry = {"name": parts[0]}
                for p in parts[1:]:
                    if p.startswith("enabled="):
                        entry["enabled"] = p.split("=")[1] == "1"
                result["patch_list"].append(entry)

        return result

    def trace_start(self, callback, lib=None, func=None, proc=None,
                    errors_only=False):
        # type: (Callable, Optional[str], Optional[str], Optional[str], bool) -> None
        """Start a trace event stream.

        callback(event_dict) is called for each trace event.
        event_dict has keys: type, raw, seq, time, lib, func, task,
        args, retval, status.

        Comment lines produce: type="comment", text=<text>.

        Does NOT catch KeyboardInterrupt -- the caller should catch it
        and call stop_trace() to terminate cleanly.
        """
        if self._sock is None:
            raise ProtocolError("Not connected")

        cmd = "TRACE START"
        if lib:
            cmd += " LIB={}".format(lib)
        if func:
            cmd += " FUNC={}".format(func)
        if proc:
            cmd += " PROC={}".format(proc)
        if errors_only:
            cmd += " ERRORS"

        send_command(self._sock, cmd)

        # Read OK or ERR (streaming response -- no sentinel after OK)
        status_line = read_line(self._sock)
        if status_line == "ERR" or status_line.startswith("ERR "):
            # Read sentinel and raise
            read_line(self._sock)  # sentinel
            _raise_for_error(status_line[4:])
        if not status_line.startswith("OK"):
            raise ProtocolError(
                "Expected OK, got: {!r}".format(status_line))

        old_timeout = self._sock.gettimeout()
        try:
            # Block indefinitely waiting for DATA chunks
            self._sock.settimeout(None)

            while True:
                line = read_line(self._sock)
                if line.startswith("DATA "):
                    try:
                        chunk_len = int(line[5:])
                    except ValueError:
                        raise ProtocolError(
                            "Invalid DATA chunk length: {!r}".format(line))
                    chunk = recv_exact(self._sock, chunk_len)
                    text = chunk.decode(ENCODING)
                    if text.startswith("#"):
                        callback({
                            "type": "comment",
                            "text": text[2:] if len(text) > 2 else "",
                        })
                    else:
                        event = _parse_trace_event(text)
                        callback(event)
                elif line == "END":
                    sentinel = read_line(self._sock)
                    if sentinel != ".":
                        raise ProtocolError(
                            "Expected sentinel, got: {!r}".format(
                                sentinel))
                    return
                elif line == "ERR" or line.startswith("ERR "):
                    sentinel = read_line(self._sock)
                    _raise_for_error(line[4:])
                else:
                    raise ProtocolError(
                        "Unexpected line during TRACE: {!r}".format(line))
        finally:
            self._sock.settimeout(old_timeout)

    def stop_trace(self):
        # type: () -> None
        """Send STOP during an active trace stream and drain remaining
        DATA chunks until END + sentinel.

        After this call, the connection is back in normal command mode.
        """
        if self._sock is None:
            raise ProtocolError("Not connected")

        send_command(self._sock, "STOP")

        # Drain remaining DATA chunks until END + sentinel
        old_timeout = self._sock.gettimeout()
        self._sock.settimeout(10)  # 10s timeout for drain
        try:
            while True:
                line = read_line(self._sock)
                if line.startswith("DATA "):
                    try:
                        chunk_len = int(line[5:])
                    except ValueError:
                        raise ProtocolError(
                            "Invalid DATA chunk length: {!r}".format(line))
                    recv_exact(self._sock, chunk_len)
                elif line == "END":
                    sentinel = read_line(self._sock)
                    if sentinel != ".":
                        raise ProtocolError(
                            "Expected sentinel, got: {!r}".format(
                                sentinel))
                    return
                elif line == "ERR" or line.startswith("ERR "):
                    sentinel = read_line(self._sock)
                    # Stream ended with error -- still drained, return
                    return
                else:
                    raise ProtocolError(
                        "Unexpected line during STOP drain: {!r}".format(
                            line))
        finally:
            self._sock.settimeout(old_timeout)

    def trace_run(self, command, callback, lib=None, func=None,
                  errors_only=False, cd=None):
        # type: (str, Callable, Optional[str], Optional[str], bool, Optional[str]) -> dict
        """Launch a program and trace its library calls.

        callback(event_dict) is called for each trace event.
        event_dict has the same format as trace_start().

        The stream auto-terminates when the process exits.  The final
        callback receives a comment event with text "PROCESS EXITED rc=N".

        Returns a dict with keys:
            proc_id (int) -- daemon-assigned process ID
            rc (int or None) -- process exit code (from the exit comment)

        Does NOT catch KeyboardInterrupt -- the caller should catch it
        and call stop_trace() to terminate cleanly.

        Args:
            command: AmigaOS command string to execute.
            callback: Function called with each event dict.
            lib: Optional library filter (e.g. "dos").
            func: Optional function filter (e.g. "Open").
            errors_only: If True, only show error returns.
            cd: Optional working directory for the command.
        """
        if self._sock is None:
            raise ProtocolError("Not connected")

        cmd = "TRACE RUN"
        if lib:
            cmd += " LIB={}".format(lib)
        if func:
            cmd += " FUNC={}".format(func)
        if errors_only:
            cmd += " ERRORS"
        if cd:
            cmd += " CD={}".format(cd)
        cmd += " -- {}".format(command)

        send_command(self._sock, cmd)

        # Read OK or ERR
        status_line = read_line(self._sock)
        if status_line == "ERR" or status_line.startswith("ERR "):
            read_line(self._sock)  # sentinel
            _raise_for_error(status_line[4:])
        if not status_line.startswith("OK"):
            raise ProtocolError(
                "Expected OK, got: {!r}".format(status_line))

        # Parse proc_id from OK line
        proc_id = None
        info = status_line[3:].strip()
        if info:
            try:
                proc_id = int(info)
            except ValueError:
                pass

        # Stream events (same loop as trace_start)
        rc = None
        old_timeout = self._sock.gettimeout()
        try:
            self._sock.settimeout(None)

            while True:
                line = read_line(self._sock)
                if line.startswith("DATA "):
                    try:
                        chunk_len = int(line[5:])
                    except ValueError:
                        raise ProtocolError(
                            "Invalid DATA chunk length: {!r}".format(line))
                    chunk = recv_exact(self._sock, chunk_len)
                    text = chunk.decode(ENCODING)
                    if text.startswith("#"):
                        comment_text = text[2:] if len(text) > 2 else ""
                        # Parse exit code from PROCESS EXITED comment
                        if comment_text.startswith("PROCESS EXITED rc="):
                            try:
                                rc = int(comment_text[18:])
                            except ValueError:
                                pass
                        callback({
                            "type": "comment",
                            "text": comment_text,
                        })
                    else:
                        event = _parse_trace_event(text)
                        callback(event)
                elif line == "END":
                    sentinel = read_line(self._sock)
                    if sentinel != ".":
                        raise ProtocolError(
                            "Expected sentinel, got: {!r}".format(
                                sentinel))
                    return {"proc_id": proc_id, "rc": rc}
                elif line == "ERR" or line.startswith("ERR "):
                    sentinel = read_line(self._sock)
                    _raise_for_error(line[4:])
                else:
                    raise ProtocolError(
                        "Unexpected line during TRACE RUN: {!r}".format(
                            line))
        finally:
            self._sock.settimeout(old_timeout)

    def trace_start_raw(self, lib=None, func=None, proc=None,
                        errors_only=False):
        # type: (Optional[str], Optional[str], Optional[str], bool) -> RawTraceSession
        """Start a trace stream and return a RawTraceSession.

        Unlike trace_start(), this does NOT enter a read loop. The
        caller uses the returned session's .sock for select() and
        .reader for non-blocking event parsing.

        Returns a RawTraceSession context manager. The caller should
        use it in a ``with`` block to ensure socket timeout is restored:

            with conn.trace_start_raw(lib="dos") as session:
                # session.sock for select()
                # session.reader.try_read_event() for events
                ...

        Raises AmigactlError or ProtocolError on failure.
        """
        if self._sock is None:
            raise ProtocolError("Not connected")

        cmd = "TRACE START"
        if lib:
            cmd += " LIB={}".format(lib)
        if func:
            cmd += " FUNC={}".format(func)
        if proc:
            cmd += " PROC={}".format(proc)
        if errors_only:
            cmd += " ERRORS"

        send_command(self._sock, cmd)

        # Read OK or ERR (uses blocking read_line for the handshake)
        status_line = read_line(self._sock)
        if status_line == "ERR" or status_line.startswith("ERR "):
            read_line(self._sock)  # sentinel
            _raise_for_error(status_line[4:])
        if not status_line.startswith("OK"):
            raise ProtocolError(
                "Expected OK, got: {!r}".format(status_line))

        # Save timeout before switching to non-blocking
        old_timeout = self._sock.gettimeout()

        # Set socket to non-blocking for the interactive loop
        self._sock.setblocking(False)

        return RawTraceSession(self._sock, old_timeout)

    def trace_run_raw(self, command, lib=None, func=None,
                      errors_only=False, cd=None):
        # type: (str, Optional[str], Optional[str], bool, Optional[str]) -> Tuple[RawTraceSession, Optional[int]]
        """Start a TRACE RUN stream and return (session, proc_id)."""
        if self._sock is None:
            raise ProtocolError("Not connected")

        cmd = "TRACE RUN"
        if lib:
            cmd += " LIB={}".format(lib)
        if func:
            cmd += " FUNC={}".format(func)
        if errors_only:
            cmd += " ERRORS"
        if cd:
            cmd += " CD={}".format(cd)
        cmd += " -- {}".format(command)

        send_command(self._sock, cmd)

        status_line = read_line(self._sock)
        if status_line == "ERR" or status_line.startswith("ERR "):
            read_line(self._sock)  # sentinel
            _raise_for_error(status_line[4:])
        if not status_line.startswith("OK"):
            raise ProtocolError(
                "Expected OK, got: {!r}".format(status_line))

        proc_id = None
        info = status_line[3:].strip()
        if info:
            try:
                proc_id = int(info)
            except ValueError:
                pass

        old_timeout = self._sock.gettimeout()
        self._sock.setblocking(False)

        return RawTraceSession(self._sock, old_timeout), proc_id

    def send_filter(self, lib=None, func=None, proc=None, raw=None):
        # type: (Optional[str], Optional[str], Optional[str], Optional[str]) -> None
        """Send a FILTER command during an active trace stream.

        Fire-and-forget: no response is expected. Call with no arguments
        to clear all filters.

        Handles non-blocking sockets: temporarily sets socket to blocking
        mode with a short timeout for the sendall() call, then restores
        non-blocking mode. This prevents BlockingIOError from sendall()
        on a non-blocking socket when the TCP send buffer is full.

        Args:
            lib: Library name filter (e.g. "dos").
            func: Function name filter (e.g. "Open").
            proc: Process name filter (e.g. "bbs").
            raw: Raw filter string (e.g. "LIB=dos,exec -FUNC=AllocMem").
                 When provided, lib/func/proc are ignored.
        """
        if self._sock is None:
            raise ProtocolError("Not connected")

        if raw is not None:
            cmd = "FILTER"
            if raw:
                cmd += " " + raw
        else:
            cmd = "FILTER"
            if lib:
                cmd += " LIB={}".format(lib)
            if func:
                cmd += " FUNC={}".format(func)
            if proc:
                cmd += " PROC={}".format(proc)

        # Temporarily set blocking with short timeout for sendall().
        # The socket may be in non-blocking mode (interactive viewer).
        was_blocking = self._sock.getblocking()
        try:
            if not was_blocking:
                self._sock.settimeout(2.0)
            send_command(self._sock, cmd)
        except (BlockingIOError, OSError):
            # Fire-and-forget: silently drop if send fails.
            # The daemon's filter state is unchanged; the user can
            # retry by pressing Tab again.
            pass
        finally:
            if not was_blocking:
                self._sock.setblocking(False)

    def trace_enable(self, funcs=None):
        # type: (Optional[List[str]]) -> None
        """Enable atrace globally, or enable specific functions.

        Args:
            funcs: Optional list of function names to enable.  If None,
                   toggles global_enable on.

        Raises AmigactlError if atrace is not loaded or a function
        name is not recognized.
        """
        cmd = "TRACE ENABLE"
        if funcs:
            cmd += " " + " ".join(funcs)
        self._send_command(cmd)

    def trace_disable(self, funcs=None):
        # type: (Optional[List[str]]) -> None
        """Disable atrace globally, or disable specific functions.

        Args:
            funcs: Optional list of function names to disable.  If None,
                   toggles global_enable off and drains buffer.

        Raises AmigactlError if atrace is not loaded or a function
        name is not recognized.
        """
        cmd = "TRACE DISABLE"
        if funcs:
            cmd += " " + " ".join(funcs)
        self._send_command(cmd)
