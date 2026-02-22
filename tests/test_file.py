"""Phase 2 file operation tests for amigactld.

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

import pytest

from conftest import (
    _read_line,
    read_data_response,
    read_response,
    send_command,
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
