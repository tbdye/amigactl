"""File operation tests for amigactld.

These tests exercise the file operation commands (DIR, STAT, READ, WRITE,
DELETE, RENAME, MAKEDIR, PROTECT) against a live amigactld daemon.

Tests that create files or directories use RAM: as the target volume.
Cleanup is handled by the cleanup_paths fixture.

The daemon must be running on the target machine before these tests are
executed.
"""

import re
import socket
import time
import zlib

import pytest

from conftest import (
    _read_line,
    _recv_exact,
    read_data_response,
    read_response,
    send_command,
    send_copy,
    send_append_data,
    send_raw_write_start,
    send_rename,
    send_write_data,
)


# ---------------------------------------------------------------------------
# DIR
# ---------------------------------------------------------------------------

class TestDir:
    """Tests for the DIR command."""

    def test_dir_system_directory(self, raw_connection):
        """DIR SYS:S returns OK with at least one payload line.
        COMMANDS.md: DIR lists the contents of a directory.  SYS:S is a
        standard AmigaOS directory that always contains files."""
        sock, _banner = raw_connection
        send_command(sock, "DIR SYS:S")
        status, payload = read_response(sock)
        assert status == "OK"
        assert len(payload) > 0, "SYS:S should contain at least one entry"

    def test_dir_nonexistent(self, raw_connection):
        """DIR on a nonexistent path returns ERR 200.
        COMMANDS.md: 'Path not found -> ERR 200 <dos error message>'."""
        sock, _banner = raw_connection
        send_command(sock, "DIR RAM:nonexistent_amigactl_test")
        status, payload = read_response(sock)
        assert status.startswith("ERR 200"), (
            "Expected ERR 200, got: {!r}".format(status)
        )
        assert payload == []

    def test_dir_on_file(self, raw_connection):
        """DIR on a file (not a directory) returns ERR 200.
        COMMANDS.md: 'Path is a file (not a directory) -> ERR 200 Not a
        directory'."""
        sock, _banner = raw_connection
        send_command(sock, "DIR SYS:S/Startup-Sequence")
        status, payload = read_response(sock)
        assert status.startswith("ERR 200"), (
            "Expected ERR 200, got: {!r}".format(status)
        )
        assert payload == []

    def test_dir_field_format(self, raw_connection):
        """Each DIR entry has 5 tab-separated fields.
        COMMANDS.md specifies: type (FILE/DIR), name, size (numeric),
        protection (8 hex digits), datestamp (YYYY-MM-DD HH:MM:SS)."""
        sock, _banner = raw_connection
        send_command(sock, "DIR SYS:S")
        status, payload = read_response(sock)
        assert status == "OK"
        assert len(payload) > 0

        datestamp_re = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        protection_re = re.compile(r"^[0-9a-f]{8}$")

        for line in payload:
            fields = line.split("\t")
            assert len(fields) == 5, (
                "Expected 5 tab-separated fields, got {}: {!r}".format(
                    len(fields), line)
            )
            entry_type, name, size, protection, datestamp = fields

            assert entry_type in ("FILE", "DIR"), (
                "Type must be FILE or DIR, got: {!r}".format(entry_type)
            )
            assert name, "Name must not be empty"
            assert size.isdigit(), (
                "Size must be numeric, got: {!r}".format(size)
            )
            assert protection_re.match(protection), (
                "Protection must be 8 hex digits, got: {!r}".format(protection)
            )
            assert datestamp_re.match(datestamp), (
                "Datestamp must match YYYY-MM-DD HH:MM:SS, got: {!r}".format(
                    datestamp)
            )

    def test_dir_empty_directory(self, raw_connection, cleanup_paths):
        """DIR on an empty directory returns OK with no payload lines.
        COMMANDS.md: 'An empty directory returns OK with no payload lines
        (just the sentinel).'"""
        sock, _banner = raw_connection
        path = "RAM:amigactl_test_empty_dir"
        send_command(sock, "MAKEDIR {}".format(path))
        status, payload = read_response(sock)
        assert status == "OK", (
            "MAKEDIR failed: {!r}".format(status)
        )
        cleanup_paths.add(path)

        send_command(sock, "DIR {}".format(path))
        status, payload = read_response(sock)
        assert status == "OK"
        assert payload == [], (
            "Empty directory should have no entries, got: {!r}".format(payload)
        )

    def test_dir_recursive(self, raw_connection):
        """DIR RECURSIVE on a directory with subdirectories includes entries
        with '/' in the name (relative paths from the base directory).
        COMMANDS.md: 'entries from subdirectories use relative paths from
        the base directory as the name field (e.g., S/Startup-Sequence)'.
        Uses SYS:S rather than SYS: to keep the listing small enough to
        avoid timeouts."""
        sock, _banner = raw_connection
        send_command(sock, "DIR SYS:S RECURSIVE")
        status, payload = read_response(sock)
        assert status == "OK"
        assert len(payload) > 0

        # At least one entry should have a "/" in the name field,
        # indicating it comes from a subdirectory.
        has_subdir_entry = False
        for line in payload:
            fields = line.split("\t")
            if len(fields) >= 2 and "/" in fields[1]:
                has_subdir_entry = True
                break
        assert has_subdir_entry, (
            "RECURSIVE listing of SYS:S should contain entries with '/' "
            "in the name (subdirectory paths)"
        )

    def test_dir_recursive_flat(self, raw_connection):
        """DIR RECURSIVE on a flat directory (no subdirectories) produces
        the same entry names as a non-recursive listing.  SYS:S typically
        contains only files."""
        sock, _banner = raw_connection

        # Non-recursive listing
        send_command(sock, "DIR SYS:S")
        status_nr, payload_nr = read_response(sock)
        assert status_nr == "OK"

        # Recursive listing
        send_command(sock, "DIR SYS:S RECURSIVE")
        status_r, payload_r = read_response(sock)
        assert status_r == "OK"

        # Extract names from both listings
        names_nr = set()
        for line in payload_nr:
            fields = line.split("\t")
            if len(fields) >= 2:
                names_nr.add(fields[1])

        names_r = set()
        for line in payload_r:
            fields = line.split("\t")
            if len(fields) >= 2:
                names_r.add(fields[1])

        assert names_nr.issubset(names_r), (
            "Non-recursive entries should be a subset of recursive entries."
            "\nMissing from recursive: {}".format(
                sorted(names_nr - names_r))
        )

    def test_dir_recursive_nonexistent(self, raw_connection):
        """DIR RECURSIVE on nonexistent path returns ERR 200.
        COMMANDS.md: 'Path not found -> ERR 200 <dos error message>'."""
        sock, _banner = raw_connection
        send_command(sock, "DIR RAM:nonexistent_amigactl_test RECURSIVE")
        status, payload = read_response(sock)
        assert status.startswith("ERR 200"), (
            "Expected ERR 200, got: {!r}".format(status)
        )
        assert payload == []

    def test_dir_missing_path(self, raw_connection):
        """DIR with no path argument returns ERR 100.
        COMMANDS.md: 'Missing path argument -> ERR 100 Missing path
        argument'."""
        sock, _banner = raw_connection
        send_command(sock, "DIR")
        status, payload = read_response(sock)
        assert status == "ERR 100 Missing path argument"
        assert payload == []


# ---------------------------------------------------------------------------
# STAT
# ---------------------------------------------------------------------------

class TestStat:
    """Tests for the STAT command."""

    def test_stat_file(self, raw_connection):
        """STAT on a known file returns OK with 6 key=value payload lines.
        COMMANDS.md: 'The payload consists of key=value lines in a fixed
        order' -- type, name, size, protection, datestamp, comment."""
        sock, _banner = raw_connection
        send_command(sock, "STAT SYS:S/Startup-Sequence")
        status, payload = read_response(sock)
        assert status == "OK"
        assert len(payload) == 6, (
            "Expected 6 payload lines, got {}".format(len(payload))
        )

        # Parse into dict for validation
        kv = {}
        for line in payload:
            key, _, value = line.partition("=")
            kv[key] = value

        assert kv["type"] == "file"
        assert kv["name"].lower() == "startup-sequence"
        assert kv["size"].isdigit()
        assert int(kv["size"]) > 0

    def test_stat_nonexistent(self, raw_connection):
        """STAT on a nonexistent path returns ERR 200.
        COMMANDS.md: 'Path not found -> ERR 200 <dos error message>'."""
        sock, _banner = raw_connection
        send_command(sock, "STAT RAM:nonexistent_amigactl_test")
        status, payload = read_response(sock)
        assert status.startswith("ERR 200"), (
            "Expected ERR 200, got: {!r}".format(status)
        )
        assert payload == []

    def test_stat_format(self, raw_connection):
        """STAT key=value lines are in fixed order with correct formats.
        COMMANDS.md specifies the order: type, name, size, protection,
        datestamp, comment.  Protection is 8 hex digits, datestamp matches
        YYYY-MM-DD HH:MM:SS."""
        sock, _banner = raw_connection
        send_command(sock, "STAT SYS:S/Startup-Sequence")
        status, payload = read_response(sock)
        assert status == "OK"
        assert len(payload) == 6

        # Verify key order
        expected_keys = ["type", "name", "size", "protection",
                         "datestamp", "comment"]
        actual_keys = [line.partition("=")[0] for line in payload]
        assert actual_keys == expected_keys, (
            "Keys must be in fixed order.\nExpected: {}\nActual: {}".format(
                expected_keys, actual_keys)
        )

        # Parse values
        kv = {}
        for line in payload:
            key, _, value = line.partition("=")
            kv[key] = value

        # Protection: 8 lowercase hex digits
        assert re.match(r"^[0-9a-f]{8}$", kv["protection"]), (
            "Protection must be 8 hex digits, got: {!r}".format(
                kv["protection"])
        )

        # Datestamp: YYYY-MM-DD HH:MM:SS
        assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$",
                        kv["datestamp"]), (
            "Datestamp must match YYYY-MM-DD HH:MM:SS, got: {!r}".format(
                kv["datestamp"])
        )

        # Size: numeric
        assert kv["size"].isdigit(), (
            "Size must be numeric, got: {!r}".format(kv["size"])
        )

    def test_stat_directory(self, raw_connection):
        """STAT on a directory returns type=dir.
        COMMANDS.md: 'type -> file or dir (lowercase)'."""
        sock, _banner = raw_connection
        send_command(sock, "STAT SYS:S")
        status, payload = read_response(sock)
        assert status == "OK"
        assert len(payload) == 6

        kv = {}
        for line in payload:
            key, _, value = line.partition("=")
            kv[key] = value

        assert kv["type"] == "dir"

    def test_stat_missing_path(self, raw_connection):
        """STAT with no path argument returns ERR 100.
        COMMANDS.md: 'Missing path argument -> ERR 100'."""
        sock, _banner = raw_connection
        send_command(sock, "STAT")
        status, payload = read_response(sock)
        assert status == "ERR 100 Missing path argument"
        assert payload == []


# ---------------------------------------------------------------------------
# READ
# ---------------------------------------------------------------------------

class TestRead:
    """Tests for the READ command."""

    def test_read_known_file(self, raw_connection):
        """READ a known file returns data of the declared size.
        COMMANDS.md: 'The OK status line includes the total file size in
        bytes.'  SYS:S/Startup-Sequence exists on all AmigaOS systems."""
        sock, _banner = raw_connection
        send_command(sock, "READ SYS:S/Startup-Sequence")
        info, data = read_data_response(sock)
        declared_size = int(info)
        assert declared_size > 0, "Startup-Sequence should not be empty"
        assert len(data) == declared_size

    def test_read_nonexistent(self, raw_connection):
        """READ on a nonexistent file returns ERR 200.
        COMMANDS.md: 'File not found -> ERR 200 <dos error message>'."""
        sock, _banner = raw_connection
        send_command(sock, "READ RAM:nonexistent_amigactl_test")
        info, data = read_data_response(sock)
        assert info.startswith("ERR 200"), (
            "Expected ERR 200, got: {!r}".format(info)
        )
        assert data == b""

    def test_read_directory(self, raw_connection):
        """READ on a directory returns ERR 300.
        COMMANDS.md: 'Path is a directory -> ERR 300 Is a directory'."""
        sock, _banner = raw_connection
        send_command(sock, "READ SYS:S")
        info, data = read_data_response(sock)
        assert info.startswith("ERR 300"), (
            "Expected ERR 300, got: {!r}".format(info)
        )
        assert data == b""

    def test_read_empty_file(self, raw_connection, cleanup_paths):
        """READ a 0-byte file returns OK 0 with no DATA chunks.
        COMMANDS.md: 'A zero-length file produces: OK 0 / END / .'"""
        sock, _banner = raw_connection
        path = "RAM:amigactl_test_empty_read.txt"

        # Write a 0-byte file
        status, _payload = send_write_data(sock, path, b"")
        assert status.startswith("OK"), (
            "WRITE 0 bytes failed: {!r}".format(status)
        )
        cleanup_paths.add(path)

        # Read it back
        send_command(sock, "READ {}".format(path))
        info, data = read_data_response(sock)
        assert info == "0"
        assert data == b""

    def test_read_large_file(self, raw_connection, cleanup_paths):
        """READ a file larger than 4096 bytes returns correct data.
        The server should split the response into multiple DATA chunks
        (max 4096 bytes each).  Byte content is verified by comparison."""
        sock, _banner = raw_connection
        path = "RAM:amigactl_test_large_read.txt"
        content = bytes(range(256)) * 20  # 5120 bytes

        # Write the test file
        status, _payload = send_write_data(sock, path, content)
        assert status.startswith("OK"), (
            "WRITE failed: {!r}".format(status)
        )
        cleanup_paths.add(path)

        # Read it back and byte-compare
        send_command(sock, "READ {}".format(path))
        info, data = read_data_response(sock)
        assert int(info) == len(content)
        assert data == content

    def test_read_missing_path(self, raw_connection):
        """READ with no path argument returns ERR 100.
        COMMANDS.md: 'Missing path argument -> ERR 100'."""
        sock, _banner = raw_connection
        send_command(sock, "READ")
        status, payload = read_response(sock)
        assert status == "ERR 100 Missing path argument"
        assert payload == []


# ---------------------------------------------------------------------------
# WRITE
# ---------------------------------------------------------------------------

class TestWrite:
    """Tests for the WRITE command."""

    def test_write_new_file(self, raw_connection, cleanup_paths):
        """WRITE a new file to RAM:, READ back, and verify content matches.
        COMMANDS.md: 'Uploads a file to the Amiga.'"""
        sock, _banner = raw_connection
        path = "RAM:amigactl_test_write.txt"
        content = b"hello world"

        status, _payload = send_write_data(sock, path, content)
        assert status.startswith("OK"), (
            "WRITE failed: {!r}".format(status)
        )
        cleanup_paths.add(path)

        # Read back and verify
        send_command(sock, "READ {}".format(path))
        info, data = read_data_response(sock)
        assert data == content

    def test_write_overwrite(self, raw_connection, cleanup_paths):
        """WRITE over an existing file replaces its contents.
        COMMANDS.md: 'If the target already exists, it is deleted before
        the rename.'"""
        sock, _banner = raw_connection
        path = "RAM:amigactl_test_overwrite.txt"

        # Write original content
        status, _payload = send_write_data(sock, path, b"original")
        assert status.startswith("OK"), (
            "First WRITE failed: {!r}".format(status)
        )
        cleanup_paths.add(path)

        # Overwrite with new content
        status, _payload = send_write_data(sock, path, b"replaced")
        assert status.startswith("OK"), (
            "Second WRITE failed: {!r}".format(status)
        )

        # Read back and verify the overwrite took effect
        send_command(sock, "READ {}".format(path))
        info, data = read_data_response(sock)
        assert data == b"replaced"

    def test_write_nonexistent_volume(self, raw_connection):
        """WRITE to a nonexistent volume returns ERR (not READY).
        COMMANDS.md: the server validates before sending READY and returns
        ERR if it cannot open the temporary file."""
        sock, _banner = raw_connection
        status, _payload = send_write_data(
            sock, "NONEXISTENT:foo.txt", b"hello"
        )
        assert status.startswith("ERR"), (
            "Expected ERR for nonexistent volume, got: {!r}".format(status)
        )

    def test_write_zero_bytes(self, raw_connection, cleanup_paths):
        """WRITE a 0-byte file succeeds and READ returns empty content.
        COMMANDS.md: 'A zero-byte file sends no DATA chunks -- just END
        immediately after receiving READY.'"""
        sock, _banner = raw_connection
        path = "RAM:amigactl_test_zero.txt"

        status, _payload = send_write_data(sock, path, b"")
        assert status.startswith("OK"), (
            "WRITE 0 bytes failed: {!r}".format(status)
        )
        cleanup_paths.add(path)

        # Read back and verify empty
        send_command(sock, "READ {}".format(path))
        info, data = read_data_response(sock)
        assert info == "0"
        assert data == b""

    def test_write_large_file(self, raw_connection, cleanup_paths):
        """WRITE a file larger than 4096 bytes (multi-chunk) succeeds.
        The content is read back and byte-compared to verify correctness."""
        sock, _banner = raw_connection
        path = "RAM:amigactl_test_large_write.txt"
        content = bytes(range(256)) * 20  # 5120 bytes

        status, _payload = send_write_data(sock, path, content)
        assert status.startswith("OK"), (
            "WRITE failed: {!r}".format(status)
        )
        cleanup_paths.add(path)

        # Read back and byte-compare
        send_command(sock, "READ {}".format(path))
        info, data = read_data_response(sock)
        assert data == content

    def test_write_missing_args(self, raw_connection):
        """WRITE with no arguments returns ERR 100.
        COMMANDS.md: 'Missing arguments -> ERR 100'."""
        sock, _banner = raw_connection
        send_command(sock, "WRITE")
        status, payload = read_response(sock)
        assert status.startswith("ERR 100"), (
            "Expected ERR 100, got: {!r}".format(status)
        )
        assert payload == []

    def test_write_invalid_size(self, raw_connection):
        """WRITE with non-numeric size returns ERR 100.
        COMMANDS.md: 'Invalid size -> ERR 100 Invalid size'."""
        sock, _banner = raw_connection
        send_command(sock, "WRITE RAM:amigactl_test.txt notanumber")
        status, payload = read_response(sock)
        assert status.startswith("ERR 100"), (
            "Expected ERR 100, got: {!r}".format(status)
        )
        assert payload == []


# ---------------------------------------------------------------------------
# DELETE
# ---------------------------------------------------------------------------

class TestDelete:
    """Tests for the DELETE command."""

    def test_delete_file(self, raw_connection, cleanup_paths):
        """DELETE a file and verify it is gone via STAT.
        COMMANDS.md: DELETE deletes a file or an empty directory."""
        sock, _banner = raw_connection
        path = "RAM:amigactl_test_delete.txt"

        # Create the file
        status, _payload = send_write_data(sock, path, b"delete me")
        assert status.startswith("OK"), (
            "WRITE failed: {!r}".format(status)
        )
        # Register for cleanup (will silently fail if already deleted)
        cleanup_paths.add(path)

        # Delete it
        send_command(sock, "DELETE {}".format(path))
        status, payload = read_response(sock)
        assert status == "OK"
        assert payload == []

        # Confirm it is gone
        send_command(sock, "STAT {}".format(path))
        status, payload = read_response(sock)
        assert status.startswith("ERR 200"), (
            "Expected ERR 200 after DELETE, got: {!r}".format(status)
        )

    def test_delete_nonexistent(self, raw_connection):
        """DELETE on a nonexistent file returns ERR 200.
        COMMANDS.md: 'Path not found -> ERR 200 <dos error message>'."""
        sock, _banner = raw_connection
        send_command(sock, "DELETE RAM:nonexistent_amigactl_test")
        status, payload = read_response(sock)
        assert status.startswith("ERR 200"), (
            "Expected ERR 200, got: {!r}".format(status)
        )
        assert payload == []

    def test_delete_nonempty_dir(self, raw_connection, cleanup_paths):
        """DELETE on a non-empty directory returns ERR.
        COMMANDS.md: 'Directory not empty -> ERR 201 <dos error message>'."""
        sock, _banner = raw_connection
        dir_path = "RAM:amigactl_test_nonempty"
        file_path = "RAM:amigactl_test_nonempty/child.txt"

        # Create directory and a file inside it
        send_command(sock, "MAKEDIR {}".format(dir_path))
        status, _payload = read_response(sock)
        assert status == "OK", (
            "MAKEDIR failed: {!r}".format(status)
        )
        # Register in creation order: dir first, then file.
        # Cleanup reverses: deletes file, then dir.
        cleanup_paths.add(dir_path)

        status, _payload = send_write_data(sock, file_path, b"child")
        assert status.startswith("OK"), (
            "WRITE child failed: {!r}".format(status)
        )
        cleanup_paths.add(file_path)

        # Attempt to delete the non-empty directory
        send_command(sock, "DELETE {}".format(dir_path))
        status, payload = read_response(sock)
        assert status.startswith("ERR"), (
            "Expected ERR for non-empty directory, got: {!r}".format(status)
        )
        assert payload == []

    def test_delete_missing_path(self, raw_connection):
        """DELETE with no path argument returns ERR 100.
        COMMANDS.md: 'Missing path argument -> ERR 100'."""
        sock, _banner = raw_connection
        send_command(sock, "DELETE")
        status, payload = read_response(sock)
        assert status == "ERR 100 Missing path argument"
        assert payload == []


# ---------------------------------------------------------------------------
# RENAME
# ---------------------------------------------------------------------------

class TestRename:
    """Tests for the RENAME command."""

    def test_rename_file(self, raw_connection, cleanup_paths):
        """RENAME a file and verify the old name is gone and the new name
        exists.  COMMANDS.md: 'Renames or moves a file or directory.'"""
        sock, _banner = raw_connection
        old_path = "RAM:amigactl_test_rename_old.txt"
        new_path = "RAM:amigactl_test_rename_new.txt"

        # Create the file
        status, _payload = send_write_data(sock, old_path, b"rename me")
        assert status.startswith("OK"), (
            "WRITE failed: {!r}".format(status)
        )
        # Register both old and new for cleanup (old may already be gone
        # after rename; cleanup errors are silently ignored)
        cleanup_paths.add(old_path)
        cleanup_paths.add(new_path)

        # Rename
        status, payload = send_rename(sock, old_path, new_path)
        assert status == "OK"
        assert payload == []

        # Verify old is gone
        send_command(sock, "STAT {}".format(old_path))
        status, payload = read_response(sock)
        assert status.startswith("ERR 200"), (
            "Old path should not exist after rename: {!r}".format(status)
        )

        # Verify new exists
        send_command(sock, "STAT {}".format(new_path))
        status, payload = read_response(sock)
        assert status == "OK"

    def test_rename_nonexistent(self, raw_connection):
        """RENAME with a nonexistent source returns ERR 200.
        COMMANDS.md: 'Old path not found -> ERR 200 <dos error message>'."""
        sock, _banner = raw_connection
        status, payload = send_rename(
            sock,
            "RAM:nonexistent_amigactl_test",
            "RAM:nonexistent_amigactl_test_new",
        )
        assert status.startswith("ERR 200"), (
            "Expected ERR 200, got: {!r}".format(status)
        )
        assert payload == []

    def test_rename_wire_format(self, raw_connection, cleanup_paths):
        """RENAME sent as three separate sendall() calls (verb, old path,
        new path) with small delays between them succeeds.  This validates
        that the daemon correctly buffers and reassembles the three-line
        command even when lines arrive in separate TCP segments."""
        sock, _banner = raw_connection
        old_path = "RAM:amigactl_test_wire_old.txt"
        new_path = "RAM:amigactl_test_wire_new.txt"

        # Create the file
        status, _payload = send_write_data(sock, old_path, b"wire test")
        assert status.startswith("OK"), (
            "WRITE failed: {!r}".format(status)
        )
        cleanup_paths.add(old_path)
        cleanup_paths.add(new_path)

        # Send RENAME as three separate transmissions with delays
        sock.sendall(b"RENAME\n")
        time.sleep(0.05)
        sock.sendall("{}\n".format(old_path).encode("iso-8859-1"))
        time.sleep(0.05)
        sock.sendall("{}\n".format(new_path).encode("iso-8859-1"))

        status, payload = read_response(sock)
        assert status == "OK"
        assert payload == []

    def test_rename_disconnect_mid_command(self, raw_connection,
                                          cleanup_paths, amiga_host,
                                          amiga_port):
        """Disconnecting after sending RENAME + old_path (but not new_path)
        does not crash the daemon.  COMMANDS.md: 'If the client disconnects
        after sending the RENAME verb but before both path lines arrive,
        the server discards the partial command and closes the connection.'"""
        sock, _banner = raw_connection
        path = "RAM:amigactl_test_disconnect_rename.txt"

        # Create a test file
        status, _payload = send_write_data(sock, path, b"disconnect test")
        assert status.startswith("OK"), (
            "WRITE failed: {!r}".format(status)
        )
        cleanup_paths.add(path)

        # Open a fresh socket, send partial RENAME, then disconnect
        partial_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        partial_sock.settimeout(10)
        partial_sock.connect((amiga_host, amiga_port))
        _read_line(partial_sock)  # banner
        partial_sock.sendall(b"RENAME\n")
        partial_sock.sendall("{}\n".format(path).encode("iso-8859-1"))
        # Do NOT send the new_path line -- disconnect immediately
        partial_sock.close()

        # Give the daemon a moment to process the disconnect
        time.sleep(0.2)

        # Verify the daemon is still running by connecting and sending PING
        verify_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        verify_sock.settimeout(10)
        verify_sock.connect((amiga_host, amiga_port))
        _read_line(verify_sock)  # banner
        send_command(verify_sock, "PING")
        status, payload = read_response(verify_sock)
        verify_sock.close()
        assert status == "OK", (
            "Daemon not responding after mid-RENAME disconnect: {!r}".format(
                status)
        )

    def test_rename_args_on_verb_line(self, raw_connection):
        """RENAME with arguments on the verb line returns ERR 100.
        COMMANDS.md: 'Arguments on verb line -> ERR 100'."""
        sock, _banner = raw_connection
        send_command(sock, "RENAME RAM:old RAM:new")
        status, payload = read_response(sock)
        assert status.startswith("ERR 100"), (
            "Expected ERR 100, got: {!r}".format(status)
        )
        assert payload == []


# ---------------------------------------------------------------------------
# MAKEDIR
# ---------------------------------------------------------------------------

class TestMakedir:
    """Tests for the MAKEDIR command."""

    def test_makedir(self, raw_connection, cleanup_paths):
        """MAKEDIR creates a directory that appears in a DIR listing.
        COMMANDS.md: MAKEDIR creates a new directory."""
        sock, _banner = raw_connection
        path = "RAM:amigactl_test_mkdir"

        send_command(sock, "MAKEDIR {}".format(path))
        status, payload = read_response(sock)
        assert status == "OK"
        assert payload == []
        cleanup_paths.add(path)

        # Verify the directory appears in RAM: listing
        send_command(sock, "DIR RAM:")
        status, payload = read_response(sock)
        assert status == "OK"

        found = False
        for line in payload:
            fields = line.split("\t")
            if len(fields) >= 2 and fields[1] == "amigactl_test_mkdir":
                assert fields[0] == "DIR", (
                    "Entry type should be DIR, got: {!r}".format(fields[0])
                )
                found = True
                break
        assert found, (
            "amigactl_test_mkdir not found in DIR RAM: listing"
        )

    def test_makedir_exists(self, raw_connection, cleanup_paths):
        """MAKEDIR on an already-existing path returns ERR 202.
        COMMANDS.md: 'Already exists -> ERR 202 <dos error message>'."""
        sock, _banner = raw_connection
        path = "RAM:amigactl_test_mkdir_dup"

        # Create it first
        send_command(sock, "MAKEDIR {}".format(path))
        status, payload = read_response(sock)
        assert status == "OK"
        cleanup_paths.add(path)

        # Try to create it again
        send_command(sock, "MAKEDIR {}".format(path))
        status, payload = read_response(sock)
        assert status.startswith("ERR 202"), (
            "Expected ERR 202, got: {!r}".format(status)
        )
        assert payload == []

    def test_makedir_missing_path(self, raw_connection):
        """MAKEDIR with no path argument returns ERR 100.
        COMMANDS.md: 'Missing path argument -> ERR 100'."""
        sock, _banner = raw_connection
        send_command(sock, "MAKEDIR")
        status, payload = read_response(sock)
        assert status == "ERR 100 Missing path argument"
        assert payload == []


# ---------------------------------------------------------------------------
# PROTECT
# ---------------------------------------------------------------------------

class TestProtect:
    """Tests for the PROTECT command."""

    def test_protect_get(self, raw_connection):
        """PROTECT on a known file returns OK with a protection=<8hex>
        payload line.  COMMANDS.md: 'Both GET and SET return the same
        response format.'"""
        sock, _banner = raw_connection
        send_command(sock, "PROTECT SYS:S/Startup-Sequence")
        status, payload = read_response(sock)
        assert status == "OK"
        assert len(payload) == 1, (
            "Expected 1 payload line, got {}".format(len(payload))
        )
        assert payload[0].startswith("protection="), (
            "Payload must start with 'protection=', got: {!r}".format(
                payload[0])
        )
        hex_value = payload[0][len("protection="):]
        assert re.match(r"^[0-9a-f]{8}$", hex_value), (
            "Protection value must be 8 hex digits, got: {!r}".format(
                hex_value)
        )

    def test_protect_set_roundtrip(self, raw_connection, cleanup_paths):
        """PROTECT SET then GET round-trips the protection value.
        COMMANDS.md: 'SET echoes the newly applied protection value.'
        The test writes a file, saves its original protection, sets a new
        value, reads it back, and restores the original."""
        sock, _banner = raw_connection
        path = "RAM:amigactl_test_protect.txt"

        # Create a test file
        status, _payload = send_write_data(sock, path, b"protect test")
        assert status.startswith("OK"), (
            "WRITE failed: {!r}".format(status)
        )
        cleanup_paths.add(path)

        # GET original protection value
        send_command(sock, "PROTECT {}".format(path))
        status, payload = read_response(sock)
        assert status == "OK"
        original = payload[0][len("protection="):]

        # SET a known value
        send_command(sock, "PROTECT {} 0000000f".format(path))
        status, payload = read_response(sock)
        assert status == "OK"
        assert payload[0] == "protection=0000000f", (
            "SET response should echo new value, got: {!r}".format(payload[0])
        )

        # GET to verify round-trip
        send_command(sock, "PROTECT {}".format(path))
        status, payload = read_response(sock)
        assert status == "OK"
        assert payload[0] == "protection=0000000f", (
            "GET after SET should return 0000000f, got: {!r}".format(
                payload[0])
        )

        # Restore original protection value
        send_command(sock, "PROTECT {} {}".format(path, original))
        status, payload = read_response(sock)
        assert status == "OK"

    def test_protect_missing_path(self, raw_connection):
        """PROTECT with no path argument returns ERR 100.
        COMMANDS.md: 'Missing path argument -> ERR 100'."""
        sock, _banner = raw_connection
        send_command(sock, "PROTECT")
        status, payload = read_response(sock)
        assert status == "ERR 100 Missing path argument"
        assert payload == []

    def test_protect_nonexistent(self, raw_connection):
        """PROTECT on nonexistent path returns ERR 200.
        COMMANDS.md: 'Path not found -> ERR 200'."""
        sock, _banner = raw_connection
        send_command(sock, "PROTECT RAM:nonexistent_amigactl_test")
        status, payload = read_response(sock)
        assert status.startswith("ERR 200"), (
            "Expected ERR 200, got: {!r}".format(status)
        )
        assert payload == []


# ---------------------------------------------------------------------------
# SETDATE
# ---------------------------------------------------------------------------

class TestSetdate:
    """Tests for the SETDATE command."""

    def test_setdate_roundtrip(self, raw_connection, cleanup_paths):
        """SETDATE on a file, then STAT to verify the datestamp changed.
        COMMANDS.md: 'The payload is a single key=value line echoing the
        applied datestamp.'"""
        sock, _banner = raw_connection
        path = "RAM:amigactl_test_setdate.txt"

        # Create a test file
        status, _payload = send_write_data(sock, path, b"setdate test")
        assert status.startswith("OK"), (
            "WRITE failed: {!r}".format(status)
        )
        cleanup_paths.add(path)

        # Set a known datestamp
        target_datestamp = "2024-06-15 14:30:00"
        send_command(sock, "SETDATE {} {}".format(path, target_datestamp))
        status, payload = read_response(sock)
        assert status == "OK"
        assert len(payload) == 1, (
            "Expected 1 payload line, got {}".format(len(payload))
        )
        assert payload[0].startswith("datestamp="), (
            "Payload must start with 'datestamp=', got: {!r}".format(
                payload[0])
        )
        applied_datestamp = payload[0][len("datestamp="):]
        assert applied_datestamp == target_datestamp, (
            "Applied datestamp should match target.\n"
            "Expected: {!r}\nActual: {!r}".format(
                target_datestamp, applied_datestamp)
        )

        # Verify via STAT
        send_command(sock, "STAT {}".format(path))
        status, payload = read_response(sock)
        assert status == "OK"
        kv = {}
        for line in payload:
            key, _, value = line.partition("=")
            kv[key] = value
        assert kv["datestamp"] == target_datestamp, (
            "STAT datestamp should match SETDATE target.\n"
            "Expected: {!r}\nActual: {!r}".format(
                target_datestamp, kv["datestamp"])
        )

    def test_setdate_nonexistent(self, raw_connection):
        """SETDATE on a nonexistent path returns ERR 200.
        COMMANDS.md: 'Path not found -> ERR 200 <dos error message>'."""
        sock, _banner = raw_connection
        send_command(sock, "SETDATE RAM:nonexistent_amigactl_test 2024-06-15 14:30:00")
        status, payload = read_response(sock)
        assert status.startswith("ERR 200"), (
            "Expected ERR 200, got: {!r}".format(status)
        )
        assert payload == []

    def test_setdate_invalid_format(self, raw_connection, cleanup_paths):
        """SETDATE with an invalid datestamp format returns ERR.
        The daemon falls back to treating the full args as the path
        (since the datestamp doesn't parse), so the concatenated path
        doesn't exist and SetFileDate fails."""
        sock, _banner = raw_connection
        path = "RAM:amigactl_test_setdate_fmt.txt"

        # Create a test file so the path exists
        status, _payload = send_write_data(sock, path, b"format test")
        assert status.startswith("OK"), (
            "WRITE failed: {!r}".format(status)
        )
        cleanup_paths.add(path)

        # Send an invalid datestamp (month 13 is out of range)
        send_command(sock, "SETDATE {} 2024-13-01 00:00:00".format(path))
        status, payload = read_response(sock)
        assert status.startswith("ERR"), (
            "Expected ERR, got: {!r}".format(status)
        )
        assert payload == []

    def test_setdate_malformed_format(self, raw_connection, cleanup_paths):
        """SETDATE with a structurally invalid datestamp returns ERR.
        The daemon falls back to treating the full args as the path
        (since the datestamp doesn't parse), so the concatenated path
        doesn't exist and SetFileDate fails."""
        sock, _banner = raw_connection
        path = "RAM:amigactl_test_setdate_mal.txt"

        status, _payload = send_write_data(sock, path, b"malformed test")
        assert status.startswith("OK"), (
            "WRITE failed: {!r}".format(status)
        )
        cleanup_paths.add(path)

        send_command(sock, "SETDATE {} not-a-datestamp".format(path))
        status, payload = read_response(sock)
        assert status.startswith("ERR"), (
            "Expected ERR, got: {!r}".format(status)
        )
        assert payload == []

    def test_setdate_write_then_set(self, raw_connection, cleanup_paths):
        """WRITE a file, SETDATE it, STAT to verify the datestamp matches.
        COMMANDS.md: 'SETDATE works on both files and directories.'"""
        sock, _banner = raw_connection
        path = "RAM:amigactl_test_setdate_ws.txt"

        # Write a file
        status, _payload = send_write_data(sock, path, b"write then set")
        assert status.startswith("OK"), (
            "WRITE failed: {!r}".format(status)
        )
        cleanup_paths.add(path)

        # Set a different datestamp
        target_datestamp = "2020-01-01 00:00:00"
        send_command(sock, "SETDATE {} {}".format(path, target_datestamp))
        status, payload = read_response(sock)
        assert status == "OK"
        assert len(payload) == 1
        applied = payload[0][len("datestamp="):]
        assert applied == target_datestamp

        # Verify via STAT
        send_command(sock, "STAT {}".format(path))
        status, payload = read_response(sock)
        assert status == "OK"
        kv = {}
        for line in payload:
            key, _, value = line.partition("=")
            kv[key] = value
        assert kv["datestamp"] == target_datestamp, (
            "STAT datestamp should match SETDATE target.\n"
            "Expected: {!r}\nActual: {!r}".format(
                target_datestamp, kv["datestamp"])
        )

    def test_setdate_current_time(self, raw_connection, cleanup_paths):
        """SETDATE with no datestamp uses current time.
        COMMANDS.md: 'When datestamp is omitted, the daemon uses the
        current Amiga system time.'"""
        sock, _banner = raw_connection
        path = "RAM:amigactl_test_setdate_now.txt"

        # Create a test file
        status, _payload = send_write_data(sock, path, b"test data")
        assert status.startswith("OK"), (
            "WRITE failed: {!r}".format(status)
        )
        cleanup_paths.add(path)

        # SETDATE with path only (no datestamp)
        send_command(sock, "SETDATE {}".format(path))
        status, payload = read_response(sock)
        assert status == "OK", (
            "Expected OK, got: {!r}".format(status)
        )
        assert len(payload) == 1, (
            "Expected 1 payload line, got {}".format(len(payload))
        )
        assert payload[0].startswith("datestamp="), (
            "Payload must start with 'datestamp=', got: {!r}".format(
                payload[0])
        )
        applied = payload[0][len("datestamp="):]
        # Verify format is YYYY-MM-DD HH:MM:SS
        assert len(applied) == 19, (
            "Datestamp must be 19 chars, got {}: {!r}".format(
                len(applied), applied)
        )
        assert applied[4] == "-"
        assert applied[7] == "-"
        assert applied[10] == " "

    def test_setdate_missing_args(self, raw_connection):
        """SETDATE with no arguments returns ERR 100.
        COMMANDS.md: 'Missing arguments -> ERR 100 Missing arguments'."""
        sock, _banner = raw_connection
        send_command(sock, "SETDATE")
        status, payload = read_response(sock)
        assert status.startswith("ERR 100"), (
            "Expected ERR 100, got: {!r}".format(status)
        )
        assert payload == []


# ---------------------------------------------------------------------------
# Partial READ
# ---------------------------------------------------------------------------

class TestPartialRead:
    """Tests for READ with OFFSET and LENGTH parameters."""

    def test_read_offset(self, raw_connection, cleanup_paths):
        """READ with OFFSET skips initial bytes."""
        sock, _banner = raw_connection
        content = bytes(range(100))
        path = "RAM:amigactl_test_partial.bin"
        cleanup_paths.add(path)
        status, _payload = send_write_data(sock, path, content)
        assert status.startswith("OK")

        send_command(sock, "READ {} OFFSET 50".format(path))
        info, data = read_data_response(sock)
        assert data == content[50:]

    def test_read_length(self, raw_connection, cleanup_paths):
        """READ with LENGTH limits returned bytes."""
        sock, _banner = raw_connection
        content = bytes(range(100))
        path = "RAM:amigactl_test_partial.bin"
        cleanup_paths.add(path)
        status, _payload = send_write_data(sock, path, content)
        assert status.startswith("OK")

        send_command(sock, "READ {} LENGTH 30".format(path))
        info, data = read_data_response(sock)
        assert data == content[:30]

    def test_read_offset_length(self, raw_connection, cleanup_paths):
        """READ with OFFSET and LENGTH returns the specified slice."""
        sock, _banner = raw_connection
        content = bytes(range(100))
        path = "RAM:amigactl_test_partial.bin"
        cleanup_paths.add(path)
        status, _payload = send_write_data(sock, path, content)
        assert status.startswith("OK")

        send_command(sock, "READ {} OFFSET 10 LENGTH 30".format(path))
        info, data = read_data_response(sock)
        assert data == content[10:40]

    def test_read_offset_past_eof(self, raw_connection, cleanup_paths):
        """READ with OFFSET past EOF returns 0 bytes."""
        sock, _banner = raw_connection
        content = bytes(range(100))
        path = "RAM:amigactl_test_partial.bin"
        cleanup_paths.add(path)
        status, _payload = send_write_data(sock, path, content)
        assert status.startswith("OK")

        send_command(sock, "READ {} OFFSET 200".format(path))
        info, data = read_data_response(sock)
        assert info == "0"
        assert data == b""

    def test_read_offset_length_past_eof(self, raw_connection, cleanup_paths):
        """READ with OFFSET+LENGTH extending past EOF returns available bytes."""
        sock, _banner = raw_connection
        content = bytes(range(100))
        path = "RAM:amigactl_test_partial.bin"
        cleanup_paths.add(path)
        status, _payload = send_write_data(sock, path, content)
        assert status.startswith("OK")

        send_command(sock, "READ {} OFFSET 90 LENGTH 20".format(path))
        info, data = read_data_response(sock)
        assert data == content[90:]

    def test_read_offset_zero(self, raw_connection, cleanup_paths):
        """READ with OFFSET 0 returns entire file."""
        sock, _banner = raw_connection
        content = bytes(range(100))
        path = "RAM:amigactl_test_partial.bin"
        cleanup_paths.add(path)
        status, _payload = send_write_data(sock, path, content)
        assert status.startswith("OK")

        send_command(sock, "READ {} OFFSET 0".format(path))
        info, data = read_data_response(sock)
        assert data == content

    def test_read_length_zero(self, raw_connection, cleanup_paths):
        """READ with LENGTH 0 returns 0 bytes."""
        sock, _banner = raw_connection
        content = bytes(range(100))
        path = "RAM:amigactl_test_partial.bin"
        cleanup_paths.add(path)
        status, _payload = send_write_data(sock, path, content)
        assert status.startswith("OK")

        send_command(sock, "READ {} LENGTH 0".format(path))
        info, data = read_data_response(sock)
        assert info == "0"
        assert data == b""

    def test_read_partial_via_client(self, conn, cleanup_paths):
        """READ with offset and length via client library."""
        content = bytes(range(100))
        path = "RAM:amigactl_test_partial.bin"
        cleanup_paths.add(path)
        conn.write(path, content)

        data = conn.read(path, offset=10, length=30)
        assert data == content[10:40]

    def test_read_invalid_offset(self, raw_connection, cleanup_paths):
        """READ with non-numeric OFFSET treats it as part of path (ERR 200)."""
        sock, _banner = raw_connection
        content = bytes(range(100))
        path = "RAM:amigactl_test_partial.bin"
        cleanup_paths.add(path)
        status, _payload = send_write_data(sock, path, content)
        assert status.startswith("OK")

        send_command(sock, "READ {} OFFSET notanumber".format(path))
        info, data = read_data_response(sock)
        assert info.startswith("ERR 200")


# ---------------------------------------------------------------------------
# APPEND
# ---------------------------------------------------------------------------

class TestAppend:
    """Tests for the APPEND command."""

    def test_append_to_existing(self, raw_connection, cleanup_paths):
        """APPEND data to an existing file extends its content."""
        sock, _banner = raw_connection
        path = "RAM:amigactl_test_append.bin"
        cleanup_paths.add(path)
        status, _payload = send_write_data(sock, path, b"hello")
        assert status.startswith("OK")

        status, _payload = send_append_data(sock, path, b" world")
        assert status.startswith("OK")

        send_command(sock, "READ {}".format(path))
        info, data = read_data_response(sock)
        assert data == b"hello world"

    def test_append_to_nonexistent(self, raw_connection):
        """APPEND to a nonexistent file returns ERR 200."""
        sock, _banner = raw_connection
        status, _payload = send_append_data(
            sock, "RAM:nonexistent_amigactl_test_append", b"data"
        )
        assert status.startswith("ERR 200")

    def test_append_to_directory(self, raw_connection, cleanup_paths):
        """APPEND to a directory returns ERR 300."""
        sock, _banner = raw_connection
        path = "RAM:amigactl_test_append_dir"
        cleanup_paths.add(path)
        send_command(sock, "MAKEDIR {}".format(path))
        status, _payload = read_response(sock)
        assert status == "OK"

        status, _payload = send_append_data(sock, path, b"data")
        assert status.startswith("ERR 300")

    def test_append_zero_bytes(self, raw_connection, cleanup_paths):
        """APPEND zero bytes leaves the file unchanged."""
        sock, _banner = raw_connection
        path = "RAM:amigactl_test_append_zero.bin"
        cleanup_paths.add(path)
        status, _payload = send_write_data(sock, path, b"hello")
        assert status.startswith("OK")

        status, _payload = send_append_data(sock, path, b"")
        assert status.startswith("OK")

        send_command(sock, "READ {}".format(path))
        info, data = read_data_response(sock)
        assert data == b"hello"

    def test_append_multiple(self, raw_connection, cleanup_paths):
        """APPEND multiple times concatenates all data."""
        sock, _banner = raw_connection
        path = "RAM:amigactl_test_append_multi.bin"
        cleanup_paths.add(path)
        status, _payload = send_write_data(sock, path, b"A")
        assert status.startswith("OK")

        status, _payload = send_append_data(sock, path, b"B")
        assert status.startswith("OK")
        status, _payload = send_append_data(sock, path, b"C")
        assert status.startswith("OK")

        send_command(sock, "READ {}".format(path))
        info, data = read_data_response(sock)
        assert data == b"ABC"

    def test_append_missing_args(self, raw_connection):
        """APPEND with no arguments returns ERR 100."""
        sock, _banner = raw_connection
        send_command(sock, "APPEND")
        status, _payload = read_response(sock)
        assert status.startswith("ERR 100")

    def test_append_large(self, raw_connection, cleanup_paths):
        """APPEND a chunk larger than 4096 bytes succeeds."""
        sock, _banner = raw_connection
        path = "RAM:amigactl_test_append_large.bin"
        cleanup_paths.add(path)
        initial = b"\x00" * 1000
        append_data = b"\xff" * 5000
        status, _payload = send_write_data(sock, path, initial)
        assert status.startswith("OK")

        status, _payload = send_append_data(sock, path, append_data)
        assert status.startswith("OK")

        send_command(sock, "READ {}".format(path))
        info, data = read_data_response(sock)
        assert data == initial + append_data

    def test_append_via_client(self, conn, cleanup_paths):
        """APPEND via the client library."""
        path = "RAM:amigactl_test_append_client.bin"
        cleanup_paths.add(path)
        conn.write(path, b"hello")
        result = conn.append(path, b" world")
        assert result == len(b" world")
        data = conn.read(path)
        assert data == b"hello world"


# ---------------------------------------------------------------------------
# CHECKSUM
# ---------------------------------------------------------------------------

class TestChecksum:
    """Tests for the CHECKSUM command."""

    def test_checksum_known_content(self, raw_connection, cleanup_paths):
        """CHECKSUM returns correct CRC32 for known content."""
        sock, _banner = raw_connection
        content = b"The quick brown fox jumps over the lazy dog"
        path = "RAM:amigactl_test_checksum.bin"
        cleanup_paths.add(path)
        status, _payload = send_write_data(sock, path, content)
        assert status.startswith("OK")

        send_command(sock, "CHECKSUM {}".format(path))
        status, payload = read_response(sock)
        assert status == "OK"
        kv = {}
        for line in payload:
            key, _, value = line.partition("=")
            kv[key] = value

        expected_crc = "{:08x}".format(zlib.crc32(content) & 0xFFFFFFFF)
        assert kv["crc32"] == expected_crc, (
            "CRC32 mismatch: expected {}, got {}".format(
                expected_crc, kv["crc32"])
        )
        assert kv["size"] == str(len(content))

    def test_checksum_empty_file(self, raw_connection, cleanup_paths):
        """CHECKSUM of an empty file returns crc32=00000000, size=0."""
        sock, _banner = raw_connection
        path = "RAM:amigactl_test_checksum_empty.bin"
        cleanup_paths.add(path)
        status, _payload = send_write_data(sock, path, b"")
        assert status.startswith("OK")

        send_command(sock, "CHECKSUM {}".format(path))
        status, payload = read_response(sock)
        assert status == "OK"
        kv = {}
        for line in payload:
            key, _, value = line.partition("=")
            kv[key] = value

        assert kv["crc32"] == "00000000"
        assert kv["size"] == "0"

    def test_checksum_nonexistent(self, raw_connection):
        """CHECKSUM on a nonexistent file returns ERR 200."""
        sock, _banner = raw_connection
        send_command(sock, "CHECKSUM RAM:nonexistent_amigactl_test")
        status, _payload = read_response(sock)
        assert status.startswith("ERR 200")

    def test_checksum_directory(self, raw_connection):
        """CHECKSUM on a directory returns ERR 300."""
        sock, _banner = raw_connection
        send_command(sock, "CHECKSUM SYS:S")
        status, _payload = read_response(sock)
        assert status.startswith("ERR 300")

    def test_checksum_missing_path(self, raw_connection):
        """CHECKSUM with no path returns ERR 100."""
        sock, _banner = raw_connection
        send_command(sock, "CHECKSUM")
        status, _payload = read_response(sock)
        assert status.startswith("ERR 100")

    def test_checksum_format(self, raw_connection, cleanup_paths):
        """CHECKSUM response has correctly formatted crc32 and size fields."""
        sock, _banner = raw_connection
        content = b"format test data"
        path = "RAM:amigactl_test_checksum_fmt.bin"
        cleanup_paths.add(path)
        status, _payload = send_write_data(sock, path, content)
        assert status.startswith("OK")

        send_command(sock, "CHECKSUM {}".format(path))
        status, payload = read_response(sock)
        assert status == "OK"
        kv = {}
        for line in payload:
            key, _, value = line.partition("=")
            kv[key] = value

        assert re.match(r"^[0-9a-f]{8}$", kv["crc32"]), (
            "crc32 must be 8 hex digits, got: {!r}".format(kv["crc32"])
        )
        assert kv["size"].isdigit(), (
            "size must be numeric, got: {!r}".format(kv["size"])
        )

    def test_checksum_via_client(self, conn, cleanup_paths):
        """CHECKSUM via the client library."""
        content = b"client checksum test"
        path = "RAM:amigactl_test_checksum_client.bin"
        cleanup_paths.add(path)
        conn.write(path, content)

        result = conn.checksum(path)
        expected_crc = "{:08x}".format(zlib.crc32(content) & 0xFFFFFFFF)
        assert result["crc32"] == expected_crc
        assert result["size"] == len(content)


# ---------------------------------------------------------------------------
# COPY
# ---------------------------------------------------------------------------

class TestCopy:
    """Tests for the COPY command."""

    def test_copy_basic(self, raw_connection, cleanup_paths):
        """COPY duplicates a file with matching content."""
        sock, _banner = raw_connection
        content = b"copy me"
        src = "RAM:amigactl_test_copy_src.bin"
        dst = "RAM:amigactl_test_copy_dst.bin"
        cleanup_paths.add(src)
        cleanup_paths.add(dst)
        status, _payload = send_write_data(sock, src, content)
        assert status.startswith("OK")

        status, _payload = send_copy(sock, src, dst)
        assert status == "OK"

        send_command(sock, "READ {}".format(dst))
        info, data = read_data_response(sock)
        assert data == content

    def test_copy_preserves_metadata(self, raw_connection, cleanup_paths):
        """COPY preserves datestamp, protection, and comment by default."""
        sock, _banner = raw_connection
        content = b"metadata test"
        src = "RAM:amigactl_test_copy_meta_src.bin"
        dst = "RAM:amigactl_test_copy_meta_dst.bin"
        cleanup_paths.add(src)
        cleanup_paths.add(dst)
        status, _payload = send_write_data(sock, src, content)
        assert status.startswith("OK")

        # Set metadata on source
        send_command(sock, "SETDATE {} 2024-06-15 14:30:00".format(src))
        status, _payload = read_response(sock)
        assert status == "OK"
        send_command(sock, "PROTECT {} 00000007".format(src))
        status, _payload = read_response(sock)
        assert status == "OK"
        send_command(sock, "SETCOMMENT {}\ttest comment".format(src))
        status, _payload = read_response(sock)
        assert status == "OK"

        # Copy
        status, _payload = send_copy(sock, src, dst)
        assert status == "OK"

        # Verify metadata on destination
        send_command(sock, "STAT {}".format(dst))
        status, payload = read_response(sock)
        assert status == "OK"
        kv = {}
        for line in payload:
            key, _, value = line.partition("=")
            kv[key] = value
        assert kv["datestamp"] == "2024-06-15 14:30:00"
        assert kv["protection"] == "00000007"
        assert kv["comment"] == "test comment"

    def test_copy_noclone(self, raw_connection, cleanup_paths):
        """COPY NOCLONE does not preserve metadata."""
        sock, _banner = raw_connection
        content = b"noclone test"
        src = "RAM:amigactl_test_copy_noclone_src.bin"
        dst = "RAM:amigactl_test_copy_noclone_dst.bin"
        cleanup_paths.add(src)
        cleanup_paths.add(dst)
        status, _payload = send_write_data(sock, src, content)
        assert status.startswith("OK")

        # Set metadata on source
        send_command(sock, "SETDATE {} 2020-01-01 00:00:00".format(src))
        status, _payload = read_response(sock)
        assert status == "OK"
        send_command(sock, "PROTECT {} 00000007".format(src))
        status, _payload = read_response(sock)
        assert status == "OK"
        send_command(sock, "SETCOMMENT {}\tcloned comment".format(src))
        status, _payload = read_response(sock)
        assert status == "OK"

        # Copy with NOCLONE
        status, _payload = send_copy(sock, src, dst, flags="NOCLONE")
        assert status == "OK"

        # Verify metadata was NOT preserved
        send_command(sock, "STAT {}".format(dst))
        status, payload = read_response(sock)
        assert status == "OK"
        kv = {}
        for line in payload:
            key, _, value = line.partition("=")
            kv[key] = value
        assert kv["datestamp"] != "2020-01-01 00:00:00", (
            "NOCLONE should not preserve datestamp"
        )
        assert kv["protection"] == "00000000", (
            "NOCLONE should reset protection to default"
        )
        assert kv["comment"] == "", (
            "NOCLONE should not preserve comment"
        )

    def test_copy_noreplace_existing(self, raw_connection, cleanup_paths):
        """COPY NOREPLACE fails when destination already exists."""
        sock, _banner = raw_connection
        src = "RAM:amigactl_test_copy_norepl_src.bin"
        dst = "RAM:amigactl_test_copy_norepl_dst.bin"
        cleanup_paths.add(src)
        cleanup_paths.add(dst)
        status, _payload = send_write_data(sock, src, b"source")
        assert status.startswith("OK")
        status, _payload = send_write_data(sock, dst, b"existing")
        assert status.startswith("OK")

        status, _payload = send_copy(sock, src, dst, flags="NOREPLACE")
        assert status.startswith("ERR 202")

    def test_copy_noreplace_new(self, raw_connection, cleanup_paths):
        """COPY NOREPLACE succeeds when destination does not exist."""
        sock, _banner = raw_connection
        content = b"noreplace new"
        src = "RAM:amigactl_test_copy_nrn_src.bin"
        dst = "RAM:amigactl_test_copy_nrn_dst.bin"
        cleanup_paths.add(src)
        cleanup_paths.add(dst)
        status, _payload = send_write_data(sock, src, content)
        assert status.startswith("OK")

        status, _payload = send_copy(sock, src, dst, flags="NOREPLACE")
        assert status == "OK"

        send_command(sock, "READ {}".format(dst))
        info, data = read_data_response(sock)
        assert data == content

    def test_copy_noclone_noreplace(self, raw_connection, cleanup_paths):
        """COPY with both NOCLONE and NOREPLACE flags succeeds."""
        sock, _banner = raw_connection
        content = b"both flags"
        src = "RAM:amigactl_test_copy_both_src.bin"
        dst = "RAM:amigactl_test_copy_both_dst.bin"
        cleanup_paths.add(src)
        cleanup_paths.add(dst)
        status, _payload = send_write_data(sock, src, content)
        assert status.startswith("OK")

        status, _payload = send_copy(
            sock, src, dst, flags="NOCLONE NOREPLACE"
        )
        assert status == "OK"

        send_command(sock, "READ {}".format(dst))
        info, data = read_data_response(sock)
        assert data == content

    def test_copy_source_not_found(self, raw_connection):
        """COPY with nonexistent source returns ERR 200."""
        sock, _banner = raw_connection
        status, _payload = send_copy(
            sock,
            "RAM:nonexistent_amigactl_test_src",
            "RAM:amigactl_test_copy_nosrc_dst.bin",
        )
        assert status.startswith("ERR 200")

    def test_copy_same_file(self, raw_connection, cleanup_paths):
        """COPY a file to itself returns ERR 300."""
        sock, _banner = raw_connection
        path = "RAM:amigactl_test_copy_self.bin"
        cleanup_paths.add(path)
        status, _payload = send_write_data(sock, path, b"self copy")
        assert status.startswith("OK")

        status, _payload = send_copy(sock, path, path)
        assert status.startswith("ERR 300")

    def test_copy_source_is_directory(self, raw_connection):
        """COPY with a directory as source returns ERR 300."""
        sock, _banner = raw_connection
        status, _payload = send_copy(
            sock, "SYS:S", "RAM:amigactl_test_dircopy"
        )
        assert status.startswith("ERR 300")

    def test_copy_unknown_flag(self, raw_connection):
        """COPY with unknown flag returns ERR 100."""
        sock, _banner = raw_connection
        send_command(sock, "COPY BADFLAG")
        status, _payload = read_response(sock)
        assert status.startswith("ERR 100")

    def test_copy_missing_source(self, raw_connection):
        """COPY with empty source returns ERR 100."""
        sock, _banner = raw_connection
        status, _payload = send_copy(sock, "", "RAM:whatever")
        assert status.startswith("ERR 100")

    def test_copy_overwrite_existing(self, raw_connection, cleanup_paths):
        """COPY without NOREPLACE overwrites existing destination."""
        sock, _banner = raw_connection
        src = "RAM:amigactl_test_copy_ow_src.bin"
        dst = "RAM:amigactl_test_copy_ow_dst.bin"
        cleanup_paths.add(src)
        cleanup_paths.add(dst)
        status, _payload = send_write_data(sock, src, b"new content")
        assert status.startswith("OK")
        status, _payload = send_write_data(sock, dst, b"old content")
        assert status.startswith("OK")

        status, _payload = send_copy(sock, src, dst)
        assert status == "OK"

        send_command(sock, "READ {}".format(dst))
        info, data = read_data_response(sock)
        assert data == b"new content"

    def test_copy_large_file(self, raw_connection, cleanup_paths):
        """COPY a file larger than 4096 bytes succeeds."""
        sock, _banner = raw_connection
        content = bytes(range(256)) * 20  # 5120 bytes
        src = "RAM:amigactl_test_copy_large_src.bin"
        dst = "RAM:amigactl_test_copy_large_dst.bin"
        cleanup_paths.add(src)
        cleanup_paths.add(dst)
        status, _payload = send_write_data(sock, src, content)
        assert status.startswith("OK")

        status, _payload = send_copy(sock, src, dst)
        assert status == "OK"

        send_command(sock, "READ {}".format(dst))
        info, data = read_data_response(sock)
        assert data == content

    def test_copy_via_client(self, conn, cleanup_paths):
        """COPY via the client library."""
        content = b"client copy test"
        src = "RAM:amigactl_test_copy_cli_src.bin"
        dst = "RAM:amigactl_test_copy_cli_dst.bin"
        cleanup_paths.add(src)
        cleanup_paths.add(dst)
        conn.write(src, content)
        conn.copy(src, dst)
        data = conn.read(dst)
        assert data == content


# ---------------------------------------------------------------------------
# SETCOMMENT
# ---------------------------------------------------------------------------

class TestSetComment:
    """Tests for the SETCOMMENT command."""

    def test_setcomment_set(self, raw_connection, cleanup_paths):
        """SETCOMMENT sets a file comment visible via STAT."""
        sock, _banner = raw_connection
        path = "RAM:amigactl_test_setcomment.bin"
        cleanup_paths.add(path)
        status, _payload = send_write_data(sock, path, b"comment test")
        assert status.startswith("OK")

        send_command(sock, "SETCOMMENT {}\ttest comment".format(path))
        status, _payload = read_response(sock)
        assert status == "OK"

        send_command(sock, "STAT {}".format(path))
        status, payload = read_response(sock)
        assert status == "OK"
        kv = {}
        for line in payload:
            key, _, value = line.partition("=")
            kv[key] = value
        assert kv["comment"] == "test comment"

    def test_setcomment_clear(self, raw_connection, cleanup_paths):
        """SETCOMMENT with empty comment clears the comment."""
        sock, _banner = raw_connection
        path = "RAM:amigactl_test_setcomment_clr.bin"
        cleanup_paths.add(path)
        status, _payload = send_write_data(sock, path, b"clear test")
        assert status.startswith("OK")

        # Set a comment first
        send_command(sock, "SETCOMMENT {}\ttest comment".format(path))
        status, _payload = read_response(sock)
        assert status == "OK"

        # Verify comment was set
        send_command(sock, "STAT {}".format(path))
        status, payload = read_response(sock)
        assert status == "OK"
        kv = {}
        for line in payload:
            key, _, value = line.partition("=")
            kv[key] = value
        assert kv["comment"] == "test comment"

        # Clear the comment (empty string after tab)
        send_command(sock, "SETCOMMENT {}\t".format(path))
        status, _payload = read_response(sock)
        assert status == "OK"

        # Verify comment is cleared
        send_command(sock, "STAT {}".format(path))
        status, payload = read_response(sock)
        assert status == "OK"
        kv = {}
        for line in payload:
            key, _, value = line.partition("=")
            kv[key] = value
        assert kv["comment"] == ""

    def test_setcomment_nonexistent(self, raw_connection):
        """SETCOMMENT on a nonexistent file returns ERR 200."""
        sock, _banner = raw_connection
        send_command(sock,
                     "SETCOMMENT RAM:nonexistent_amigactl_test\tcomment")
        status, _payload = read_response(sock)
        assert status.startswith("ERR 200")

    def test_setcomment_missing_args(self, raw_connection):
        """SETCOMMENT with no arguments returns ERR 100."""
        sock, _banner = raw_connection
        send_command(sock, "SETCOMMENT")
        status, _payload = read_response(sock)
        assert status.startswith("ERR 100")

    def test_setcomment_missing_tab(self, raw_connection):
        """SETCOMMENT without tab separator returns ERR 100."""
        sock, _banner = raw_connection
        send_command(sock, "SETCOMMENT RAM:somefile.txt")
        status, _payload = read_response(sock)
        assert status.startswith("ERR 100")

    def test_setcomment_missing_path(self, raw_connection):
        """SETCOMMENT with tab but no path returns ERR 100."""
        sock, _banner = raw_connection
        send_command(sock, "SETCOMMENT \t")
        status, _payload = read_response(sock)
        assert status.startswith("ERR 100")

    def test_setcomment_via_client(self, conn, cleanup_paths):
        """SETCOMMENT via the client library."""
        path = "RAM:amigactl_test_setcomment_cli.bin"
        cleanup_paths.add(path)
        conn.write(path, b"client comment test")
        conn.setcomment(path, "client comment")

        info = conn.stat(path)
        assert info["comment"] == "client comment"


# ---------------------------------------------------------------------------
# WRITE robustness
# ---------------------------------------------------------------------------

class TestWriteRobustness:
    """Tests for malformed WRITE handshakes and size mismatches."""

    def test_write_malformed_data_header_alpha(self, raw_connection,
                                                cleanup_paths,
                                                amiga_host, amiga_port):
        """Send DATA abc after READY. Daemon should disconnect."""
        sock, _banner = raw_connection
        path = "RAM:amigactl_test_malformed_alpha.bin"
        cleanup_paths.add(path)
        cleanup_paths.add(path + ".amigactld.tmp")

        result = send_raw_write_start(sock, path, 10)
        assert result == "READY"

        # Send malformed DATA header
        sock.sendall(b"DATA abc\n")

        # Daemon should close the connection
        try:
            data = sock.recv(1024)
            # Empty recv means EOF (connection closed)
            assert data == b"", (
                "Expected EOF after malformed DATA, got: {!r}".format(data)
            )
        except (ConnectionResetError, ConnectionError, OSError):
            pass  # Also acceptable -- connection reset

        # Verify daemon is still alive via new connection
        verify = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        verify.settimeout(5)
        try:
            verify.connect((amiga_host, amiga_port))
            _read_line(verify)  # banner
            send_command(verify, "PING")
            status, payload = read_response(verify)
            assert status == "OK"
        finally:
            verify.close()

    def test_write_malformed_data_header_negative(self, raw_connection,
                                                   cleanup_paths,
                                                   amiga_host, amiga_port):
        """Send DATA -1 after READY. Daemon should disconnect."""
        sock, _banner = raw_connection
        path = "RAM:amigactl_test_malformed_neg.bin"
        cleanup_paths.add(path)
        cleanup_paths.add(path + ".amigactld.tmp")

        result = send_raw_write_start(sock, path, 10)
        assert result == "READY"

        sock.sendall(b"DATA -1\n")

        try:
            data = sock.recv(1024)
            assert data == b"", (
                "Expected EOF after malformed DATA, got: {!r}".format(data)
            )
        except (ConnectionResetError, ConnectionError, OSError):
            pass

        verify = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        verify.settimeout(5)
        try:
            verify.connect((amiga_host, amiga_port))
            _read_line(verify)  # banner
            send_command(verify, "PING")
            status, payload = read_response(verify)
            assert status == "OK"
        finally:
            verify.close()

    def test_write_malformed_data_header_huge(self, raw_connection,
                                               cleanup_paths,
                                               amiga_host, amiga_port):
        """Send DATA 99999 after READY (exceeds chunk limit). Daemon should disconnect."""
        sock, _banner = raw_connection
        path = "RAM:amigactl_test_malformed_huge.bin"
        cleanup_paths.add(path)
        cleanup_paths.add(path + ".amigactld.tmp")

        result = send_raw_write_start(sock, path, 10)
        assert result == "READY"

        # Send oversized chunk header + some padding bytes
        sock.sendall(b"DATA 99999\n" + b"x" * 10)

        try:
            data = sock.recv(1024)
            assert data == b"", (
                "Expected EOF after oversized DATA, got: {!r}".format(data)
            )
        except (ConnectionResetError, ConnectionError, OSError):
            pass

        verify = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        verify.settimeout(5)
        try:
            verify.connect((amiga_host, amiga_port))
            _read_line(verify)  # banner
            send_command(verify, "PING")
            status, payload = read_response(verify)
            assert status == "OK"
        finally:
            verify.close()

    def test_write_size_mismatch_over(self, raw_connection, cleanup_paths,
                                       amiga_host, amiga_port):
        """Declare size 10, send 20 bytes. Daemon returns ERR 300."""
        sock, _banner = raw_connection
        path = "RAM:amigactl_test_mismatch_over.bin"
        cleanup_paths.add(path)
        cleanup_paths.add(path + ".amigactld.tmp")

        result = send_raw_write_start(sock, path, 10)
        assert result == "READY"

        # Send 20 bytes in a valid DATA chunk, then END
        sock.sendall(b"DATA 20\n" + b"x" * 20)
        sock.sendall(b"END\n")

        status, payload = read_response(sock)
        assert status.startswith("ERR 300"), (
            "Expected ERR 300 for size mismatch, got: {!r}".format(status)
        )

        # Verify daemon alive
        verify = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        verify.settimeout(5)
        try:
            verify.connect((amiga_host, amiga_port))
            _read_line(verify)  # banner
            send_command(verify, "PING")
            vs, _ = read_response(verify)
            assert vs == "OK"
        finally:
            verify.close()

    def test_write_size_mismatch_under(self, raw_connection, cleanup_paths,
                                        amiga_host, amiga_port):
        """Declare size 10, send only 5 bytes. Daemon returns ERR 300."""
        sock, _banner = raw_connection
        path = "RAM:amigactl_test_mismatch_under.bin"
        cleanup_paths.add(path)
        cleanup_paths.add(path + ".amigactld.tmp")

        result = send_raw_write_start(sock, path, 10)
        assert result == "READY"

        # Send only 5 bytes, then END
        sock.sendall(b"DATA 5\n" + b"x" * 5)
        sock.sendall(b"END\n")

        status, payload = read_response(sock)
        assert status.startswith("ERR 300"), (
            "Expected ERR 300 for size mismatch, got: {!r}".format(status)
        )

        # Verify daemon alive
        verify = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        verify.settimeout(5)
        try:
            verify.connect((amiga_host, amiga_port))
            _read_line(verify)  # banner
            send_command(verify, "PING")
            vs, _ = read_response(verify)
            assert vs == "OK"
        finally:
            verify.close()


# ---------------------------------------------------------------------------
# Mid-transfer disconnect
# ---------------------------------------------------------------------------

class TestMidTransferDisconnect:
    """Tests for client disconnect during file transfer."""

    def test_write_disconnect_mid_transfer(self, amiga_host, amiga_port,
                                            cleanup_paths):
        """Start WRITE, send partial DATA, disconnect. Verify daemon alive
        and no temp file left."""
        path = "RAM:amigactl_test_disconnect.bin"
        cleanup_paths.add(path)
        cleanup_paths.add(path + ".amigactld.tmp")

        # Open a socket and start a WRITE handshake
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(10)
        sock.connect((amiga_host, amiga_port))
        _read_line(sock)  # banner

        result = send_raw_write_start(sock, path, 100)
        assert result == "READY"

        # Send partial data and disconnect
        sock.sendall(b"DATA 50\n" + b"x" * 50)
        sock.close()

        # Wait for daemon to process the disconnect
        time.sleep(1)

        # Verify daemon is alive
        verify = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        verify.settimeout(5)
        try:
            verify.connect((amiga_host, amiga_port))
            _read_line(verify)  # banner

            send_command(verify, "PING")
            status, _ = read_response(verify)
            assert status == "OK"

            # Verify temp file was cleaned up
            send_command(verify, "STAT {}".format(path + ".amigactld.tmp"))
            status, _ = read_response(verify)
            assert status.startswith("ERR 200"), (
                "Temp file should have been cleaned up, got: {!r}".format(
                    status)
            )
        finally:
            verify.close()


# ---------------------------------------------------------------------------
# Delete-protected file
# ---------------------------------------------------------------------------

class TestDeleteProtected:
    """Tests for deleting files with protection bits."""

    def test_delete_protected_file(self, raw_connection, cleanup_paths):
        """WRITE file, set delete-protect, DELETE fails with ERR 201.
        Restore protection, DELETE succeeds."""
        sock, _banner = raw_connection
        path = "RAM:amigactl_test_delprot.bin"
        cleanup_paths.add(path)

        # Create file
        status, _ = send_write_data(sock, path, b"protected content")
        assert status.startswith("OK"), (
            "WRITE failed: {!r}".format(status)
        )

        # Set delete-protect (bit 0)
        send_command(sock, "PROTECT {} 00000001".format(path))
        status, _ = read_response(sock)
        assert status == "OK"

        # DELETE should fail
        send_command(sock, "DELETE {}".format(path))
        status, _ = read_response(sock)
        assert status.startswith("ERR 201"), (
            "Expected ERR 201 for delete-protected file, got: {!r}".format(
                status)
        )

        # Restore protection
        send_command(sock, "PROTECT {} 00000000".format(path))
        status, _ = read_response(sock)
        assert status == "OK"

        # DELETE should succeed now
        send_command(sock, "DELETE {}".format(path))
        status, _ = read_response(sock)
        assert status == "OK"


# ---------------------------------------------------------------------------
# Protected access
# ---------------------------------------------------------------------------

class TestProtectedAccess:
    """Tests for read and write protected files."""

    def test_read_protected_file(self, raw_connection, cleanup_paths):
        """WRITE file, set read-protect, READ returns ERR 201."""
        sock, _banner = raw_connection
        path = "RAM:amigactl_test_readprot.bin"
        cleanup_paths.add(path)

        # Create file
        status, _ = send_write_data(sock, path, b"read protected")
        assert status.startswith("OK")

        # Set read-protect (bit 3)
        send_command(sock, "PROTECT {} 00000008".format(path))
        status, _ = read_response(sock)
        assert status == "OK"

        # READ should fail
        send_command(sock, "READ {}".format(path))
        info, data = read_data_response(sock)
        assert info.startswith("ERR 201"), (
            "Expected ERR 201 for read-protected file, got: {!r}".format(
                info)
        )

        # Restore protection
        send_command(sock, "PROTECT {} 00000000".format(path))
        status, _ = read_response(sock)
        assert status == "OK"

    def test_write_protected_file(self, raw_connection, cleanup_paths):
        """WRITE file, set write-protect, second WRITE returns ERR 201."""
        sock, _banner = raw_connection
        path = "RAM:amigactl_test_writeprot.bin"
        cleanup_paths.add(path)

        # Create file
        status, _ = send_write_data(sock, path, b"write protected")
        assert status.startswith("OK")

        # Set write-protect (bit 2)
        send_command(sock, "PROTECT {} 00000004".format(path))
        status, _ = read_response(sock)
        assert status == "OK"

        # Second WRITE should fail
        status, _ = send_write_data(sock, path, b"overwrite attempt")
        assert status.startswith("ERR 201"), (
            "Expected ERR 201 for write-protected file, got: {!r}".format(
                status)
        )

        # Restore protection
        send_command(sock, "PROTECT {} 00000000".format(path))
        status, _ = read_response(sock)
        assert status == "OK"


# ---------------------------------------------------------------------------
# Path length
# ---------------------------------------------------------------------------

class TestWritePathLength:
    """Tests for WRITE path length limits."""

    def test_write_path_too_long(self, raw_connection):
        """WRITE with path exceeding 497 chars returns ERR 300."""
        sock, _banner = raw_connection
        # 501 chars total (RAM: + 497 a's), over the 497-char path limit
        long_path = "RAM:" + "a" * 497
        send_command(sock, "WRITE {} 5".format(long_path))
        status, payload = read_response(sock)
        assert status.startswith("ERR 300"), (
            "Expected ERR 300 for path too long, got: {!r}".format(status)
        )


# ---------------------------------------------------------------------------
# Dot-stuffing
# ---------------------------------------------------------------------------

class TestDotStuffing:
    """Tests for dot-stuffing in file names."""

    def test_dir_dot_stuffed_entry(self, raw_connection, cleanup_paths):
        """Create file named .dotfile, DIR the parent, verify entry
        appears correctly after dot-unstuffing."""
        sock, _banner = raw_connection
        dir_path = "RAM:amigactl_test_dotdir"
        file_path = dir_path + "/.dotfile"
        cleanup_paths.add(dir_path)
        cleanup_paths.add(file_path)

        # Create directory
        send_command(sock, "MAKEDIR {}".format(dir_path))
        status, _ = read_response(sock)
        assert status == "OK", (
            "MAKEDIR failed: {!r}".format(status)
        )

        # Create .dotfile
        status, _ = send_write_data(sock, file_path, b"dot content")
        assert status.startswith("OK")

        # DIR the parent
        send_command(sock, "DIR {}".format(dir_path))
        status, payload = read_response(sock)
        assert status == "OK"

        # Find .dotfile in entries
        found = False
        for line in payload:
            fields = line.split("\t")
            if len(fields) >= 2 and fields[1] == ".dotfile":
                found = True
                break
        assert found, (
            ".dotfile not found in DIR output. Payload: {!r}".format(payload)
        )

    def test_stat_dot_stuffed_name(self, raw_connection, cleanup_paths):
        """STAT a file named .dotfile, verify name survives dot-unstuffing.
        The name= payload line starts with a dot, so the daemon must
        dot-stuff it (send ..dotfile) and read_response() unstuffs it."""
        sock, _banner = raw_connection
        path = "RAM:.dotfile"
        cleanup_paths.add(path)

        # Create file
        status, _ = send_write_data(sock, path, b"dot stat content")
        assert status.startswith("OK")

        # STAT the file
        send_command(sock, "STAT {}".format(path))
        status, payload = read_response(sock)
        assert status == "OK"

        kv = {}
        for line in payload:
            key, _, value = line.partition("=")
            kv[key] = value

        assert kv["name"] == ".dotfile", (
            "Expected name='.dotfile', got: {!r}".format(kv.get("name"))
        )


# ---------------------------------------------------------------------------
# SETDATE on directory
# ---------------------------------------------------------------------------

class TestSetdateDirectory:
    """Tests for SETDATE on directories."""

    def test_setdate_directory(self, raw_connection, cleanup_paths):
        """MAKEDIR, SETDATE, STAT to verify datestamp on a directory."""
        sock, _banner = raw_connection
        path = "RAM:amigactl_test_setdate_dir"
        cleanup_paths.add(path)

        # Create directory
        send_command(sock, "MAKEDIR {}".format(path))
        status, _ = read_response(sock)
        assert status == "OK"

        # Set datestamp
        send_command(sock, "SETDATE {} 2023-03-15 10:00:00".format(path))
        status, _ = read_response(sock)
        assert status == "OK"

        # Verify via STAT
        send_command(sock, "STAT {}".format(path))
        status, payload = read_response(sock)
        assert status == "OK"

        kv = {}
        for line in payload:
            key, _, value = line.partition("=")
            kv[key] = value

        assert kv["datestamp"] == "2023-03-15 10:00:00", (
            "Expected datestamp='2023-03-15 10:00:00', got: {!r}".format(
                kv.get("datestamp"))
        )


# ---------------------------------------------------------------------------
# COPY disconnect
# ---------------------------------------------------------------------------

class TestCopyDisconnect:
    """Tests for client disconnect during COPY command."""

    def test_copy_disconnect_mid_command(self, raw_connection, cleanup_paths,
                                         amiga_host, amiga_port):
        """Create source file, then on a separate socket send partial
        COPY (verb + source but no dest), disconnect. Verify daemon alive."""
        sock, _banner = raw_connection
        src_path = "RAM:amigactl_test_copydisconnect.bin"
        cleanup_paths.add(src_path)

        # Create source file via raw_connection
        status, _ = send_write_data(sock, src_path, b"copy disconnect test")
        assert status.startswith("OK")

        # Open a separate socket and send partial COPY
        partial = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        partial.settimeout(5)
        partial.connect((amiga_host, amiga_port))
        _read_line(partial)  # banner

        # Send COPY verb + source but no destination
        partial.sendall(b"COPY\n")
        partial.sendall(src_path.encode("iso-8859-1") + b"\n")
        partial.close()

        # Wait for daemon to process
        time.sleep(0.2)

        # Verify daemon is alive via the original connection
        send_command(sock, "PING")
        status, _ = read_response(sock)
        assert status == "OK"


# ---------------------------------------------------------------------------
# APPEND invalid size
# ---------------------------------------------------------------------------

class TestAppendInvalidSize:
    """Tests for APPEND with invalid size parameter."""

    def test_append_invalid_size(self, raw_connection):
        """APPEND path notanumber returns ERR 100."""
        sock, _banner = raw_connection
        send_command(sock, "APPEND RAM:somefile notanumber")
        status, payload = read_response(sock)
        assert status.startswith("ERR 100"), (
            "Expected ERR 100 for invalid size, got: {!r}".format(status)
        )


# ---------------------------------------------------------------------------
# COPY wire format
# ---------------------------------------------------------------------------

class TestCopyWireFormat:
    """Tests for COPY three-line wire format with delays."""

    def test_copy_wire_format_segmented(self, raw_connection, cleanup_paths):
        """COPY sent as three separate sendall() calls with small delays."""
        sock, _banner = raw_connection
        src = "RAM:amigactl_test_copywire_src.bin"
        dst = "RAM:amigactl_test_copywire_dst.bin"
        cleanup_paths.add(src)
        cleanup_paths.add(dst)

        content = b"copy wire format test content"

        # Create source file
        status, _ = send_write_data(sock, src, content)
        assert status.startswith("OK")

        # Send COPY in three segments with delays
        sock.sendall(b"COPY\n")
        time.sleep(0.05)
        sock.sendall(src.encode("iso-8859-1") + b"\n")
        time.sleep(0.05)
        sock.sendall(dst.encode("iso-8859-1") + b"\n")

        status, payload = read_response(sock)
        assert status == "OK", (
            "COPY failed: {!r}".format(status)
        )

        # Verify destination content matches source
        send_command(sock, "READ {}".format(dst))
        info, data = read_data_response(sock)
        assert data == content


# ---------------------------------------------------------------------------
# MAKEDIR parent
# ---------------------------------------------------------------------------

class TestMakedirParent:
    """Tests for MAKEDIR with nonexistent parent."""

    def test_makedir_nonexistent_parent(self, raw_connection):
        """MAKEDIR with nonexistent parent returns ERR 200."""
        sock, _banner = raw_connection
        send_command(sock, "MAKEDIR RAM:nonexistent_amigactl_test/child")
        status, payload = read_response(sock)
        assert status.startswith("ERR 200"), (
            "Expected ERR 200 for nonexistent parent, got: {!r}".format(
                status)
        )


# ---------------------------------------------------------------------------
# SETCOMMENT max length
# ---------------------------------------------------------------------------

class TestSetcommentMaxLength:
    """Tests for SETCOMMENT maximum comment length."""

    def test_setcomment_max_length(self, raw_connection, cleanup_paths):
        """SETCOMMENT with 79-char comment succeeds (AmigaOS limit)."""
        sock, _banner = raw_connection
        path = "RAM:amigactl_test_maxcomment.bin"
        cleanup_paths.add(path)

        # Create file
        status, _ = send_write_data(sock, path, b"comment test")
        assert status.startswith("OK")

        # Set 79-character comment
        comment = "A" * 79
        send_command(sock, "SETCOMMENT {}\t{}".format(path, comment))
        status, _ = read_response(sock)
        assert status == "OK", (
            "SETCOMMENT 79 chars failed: {!r}".format(status)
        )

        # Verify via STAT
        send_command(sock, "STAT {}".format(path))
        status, payload = read_response(sock)
        assert status == "OK"

        kv = {}
        for line in payload:
            key, _, value = line.partition("=")
            kv[key] = value

        assert kv["comment"] == comment, (
            "Expected 79-char comment, got {} chars: {!r}".format(
                len(kv["comment"]), kv["comment"])
        )


# ---------------------------------------------------------------------------
# ISO-8859-1
# ---------------------------------------------------------------------------

class TestIso8859:
    """Tests for ISO-8859-1 character handling in content and metadata."""

    def test_write_read_iso8859_content(self, raw_connection, cleanup_paths):
        """Write and read back content containing ISO-8859-1 characters
        (bytes 0x80-0xFF)."""
        sock, _banner = raw_connection
        path = "RAM:amigactl_test_iso_content.bin"
        cleanup_paths.add(path)

        content = bytes(range(0x80, 0x100))  # 128 bytes

        status, _ = send_write_data(sock, path, content)
        assert status.startswith("OK")

        send_command(sock, "READ {}".format(path))
        info, data = read_data_response(sock)
        assert data == content, (
            "ISO-8859-1 round-trip failed: {} bytes written, {} read".format(
                len(content), len(data))
        )

    def test_setcomment_iso8859(self, raw_connection, cleanup_paths):
        """Set a file comment containing ISO-8859-1 characters."""
        sock, _banner = raw_connection
        path = "RAM:amigactl_test_iso_comment.bin"
        cleanup_paths.add(path)

        # Create file
        status, _ = send_write_data(sock, path, b"iso comment test")
        assert status.startswith("OK")

        # Set comment with accented characters
        comment = "Pr\xfcfung \xe4\xf6\xfc"
        send_command(sock, "SETCOMMENT {}\t{}".format(path, comment))
        status, _ = read_response(sock)
        assert status == "OK"

        # Verify via STAT
        send_command(sock, "STAT {}".format(path))
        status, payload = read_response(sock)
        assert status == "OK"

        kv = {}
        for line in payload:
            key, _, value = line.partition("=")
            kv[key] = value

        assert kv["comment"] == comment, (
            "Expected comment {!r}, got: {!r}".format(comment, kv["comment"])
        )

    def test_env_iso8859_value(self, raw_connection, cleanup_env):
        """SETENV/ENV round-trip with ISO-8859-1 value."""
        sock, _banner = raw_connection
        cleanup_env.add("amigactl_test_iso")

        value = "W\xf6rter"
        send_command(sock, "SETENV amigactl_test_iso {}".format(value))
        status, _ = read_response(sock)
        assert status == "OK"

        send_command(sock, "ENV amigactl_test_iso")
        status, payload = read_response(sock)
        assert status == "OK"

        kv = {}
        for line in payload:
            key, _, val = line.partition("=")
            kv[key] = val

        assert kv.get("value") == value, (
            "Expected {!r}, got: {!r}".format(value, kv.get("value"))
        )
