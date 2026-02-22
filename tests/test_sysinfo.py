"""Phase 3 system information tests for amigactld.

These tests exercise the system query commands (SYSINFO, ASSIGNS, PORTS,
VOLUMES, TASKS) and the ASSIGN mutation command against a live amigactld
daemon.

The daemon must be running on the target machine before these tests are
executed.
"""

import pytest

from conftest import read_response, send_command
from amigactl import AmigaConnection, RemoteIOError, NotFoundError


# ---------------------------------------------------------------------------
# SYSINFO
# ---------------------------------------------------------------------------

class TestSysinfo:
    """Tests for the SYSINFO command."""

    def test_sysinfo_keys(self, raw_connection):
        """SYSINFO returns the expected set of key=value pairs.
        COMMANDS.md: 'The payload consists of key=value lines in a fixed
        order.'  At minimum: chip_free, fast_free, total_free,
        exec_version, kickstart, bsdsocket. chip_total and fast_total
        may be present on exec v39+ systems."""
        sock, _banner = raw_connection
        send_command(sock, "SYSINFO")
        status, payload = read_response(sock)
        assert status == "OK"
        assert len(payload) >= 6, (
            "Expected at least 6 payload lines, got {}".format(len(payload))
        )

        kv = {}
        for line in payload:
            key, _, value = line.partition("=")
            kv[key] = value

        # These keys are always present
        required_keys = [
            "chip_free", "fast_free", "total_free",
            "exec_version", "kickstart", "bsdsocket",
        ]
        for key in required_keys:
            assert key in kv, (
                "Required key {!r} missing from SYSINFO. Keys: {}".format(
                    key, sorted(kv.keys()))
            )

        # Verify key order (chip_total/fast_total may be absent on pre-v39)
        expected_order = [
            "chip_free", "fast_free", "total_free",
            "chip_total", "fast_total",
            "exec_version", "kickstart", "bsdsocket",
        ]
        actual_keys = [line.partition("=")[0] for line in payload]
        # Filter expected to only include keys actually present
        expected_present = [k for k in expected_order if k in actual_keys]
        assert actual_keys == expected_present, (
            "Keys must be in fixed order.\nExpected: {}\nActual: {}".format(
                expected_present, actual_keys)
        )

    def test_sysinfo_format(self, raw_connection):
        """SYSINFO memory values are numeric and version strings are
        non-empty.  COMMANDS.md: 'Memory values are decimal integers
        (bytes). Version strings are dot-separated.'"""
        sock, _banner = raw_connection
        send_command(sock, "SYSINFO")
        status, payload = read_response(sock)
        assert status == "OK"

        kv = {}
        for line in payload:
            key, _, value = line.partition("=")
            kv[key] = value

        # Memory values must be numeric
        memory_keys = ["chip_free", "fast_free", "total_free"]
        for key in memory_keys:
            assert key in kv
            assert kv[key].isdigit(), (
                "{} should be numeric, got: {!r}".format(key, kv[key])
            )

        # chip_total and fast_total, if present, must also be numeric
        for key in ["chip_total", "fast_total"]:
            if key in kv:
                assert kv[key].isdigit(), (
                    "{} should be numeric, got: {!r}".format(key, kv[key])
                )

        # Version strings must be non-empty
        assert kv["exec_version"], "exec_version should not be empty"
        assert kv["kickstart"], "kickstart should not be empty"
        assert kv["bsdsocket"], "bsdsocket should not be empty"

        # exec_version and bsdsocket should be dot-separated
        assert "." in kv["exec_version"], (
            "exec_version should be dot-separated, got: {!r}".format(
                kv["exec_version"])
        )
        assert "." in kv["bsdsocket"], (
            "bsdsocket should be dot-separated, got: {!r}".format(
                kv["bsdsocket"])
        )


# ---------------------------------------------------------------------------
# ASSIGNS
# ---------------------------------------------------------------------------

class TestAssigns:
    """Tests for the ASSIGNS command."""

    def test_assigns_has_sys_and_s(self, raw_connection):
        """ASSIGNS returns at least SYS: and S: assigns.
        COMMANDS.md: ASSIGNS lists all logical assigns.  SYS: and S: are
        standard AmigaOS assigns that always exist on a booted system."""
        sock, _banner = raw_connection
        send_command(sock, "ASSIGNS")
        status, payload = read_response(sock)
        assert status == "OK"
        assert len(payload) > 0, "Expected at least one assign"

        names = set()
        for line in payload:
            fields = line.split("\t")
            if len(fields) >= 1:
                names.add(fields[0].upper())

        assert "SYS:" in names, (
            "SYS: not found in assigns. Names: {}".format(sorted(names))
        )
        assert "S:" in names, (
            "S: not found in assigns. Names: {}".format(sorted(names))
        )

    def test_assigns_format(self, raw_connection):
        """ASSIGNS lines are tab-separated: name (with colon) and path.
        COMMANDS.md: 'Each payload line contains two tab-separated fields:
        name: (including trailing colon) and path.'"""
        sock, _banner = raw_connection
        send_command(sock, "ASSIGNS")
        status, payload = read_response(sock)
        assert status == "OK"
        assert len(payload) > 0

        for line in payload:
            fields = line.split("\t")
            assert len(fields) == 2, (
                "Expected 2 tab-separated fields, got {}: {!r}".format(
                    len(fields), line)
            )
            name, path = fields
            assert name.endswith(":"), (
                "Assign name should end with colon, got: {!r}".format(name)
            )
            assert len(path) > 0, (
                "Path should not be empty for assign: {!r}".format(name)
            )


# ---------------------------------------------------------------------------
# PORTS
# ---------------------------------------------------------------------------

class TestPorts:
    """Tests for the PORTS command."""

    def test_ports_returns_ports(self, raw_connection):
        """PORTS returns at least one port name.
        COMMANDS.md: PORTS lists all active Exec message ports.  A running
        AmigaOS system always has at least one port (e.g., REXX)."""
        sock, _banner = raw_connection
        send_command(sock, "PORTS")
        status, payload = read_response(sock)
        assert status == "OK"
        assert len(payload) > 0, "Expected at least one port"

    def test_ports_one_per_line(self, raw_connection):
        """Each PORTS payload line is a single port name (no tabs).
        COMMANDS.md: 'Each payload line contains a single port name.'"""
        sock, _banner = raw_connection
        send_command(sock, "PORTS")
        status, payload = read_response(sock)
        assert status == "OK"

        for line in payload:
            assert "\t" not in line, (
                "Port name should not contain tabs: {!r}".format(line)
            )
            assert len(line) > 0, "Port name should not be empty"


# ---------------------------------------------------------------------------
# VOLUMES
# ---------------------------------------------------------------------------

class TestVolumes:
    """Tests for the VOLUMES command."""

    def test_volumes_has_boot(self, raw_connection):
        """VOLUMES returns at least one volume (the boot volume).
        COMMANDS.md: 'Only mounted volumes are listed.'  A booted AmigaOS
        system always has at least the boot volume."""
        sock, _banner = raw_connection
        send_command(sock, "VOLUMES")
        status, payload = read_response(sock)
        assert status == "OK"
        assert len(payload) > 0, "Expected at least one volume"

    def test_volumes_format(self, raw_connection):
        """VOLUMES lines are tab-separated with numeric fields.
        COMMANDS.md: 'Each payload line contains five tab-separated fields:
        name, used, free, capacity, blocksize.'  Used, free, capacity,
        and blocksize must be numeric."""
        sock, _banner = raw_connection
        send_command(sock, "VOLUMES")
        status, payload = read_response(sock)
        assert status == "OK"
        assert len(payload) > 0

        for line in payload:
            fields = line.split("\t")
            assert len(fields) == 5, (
                "Expected 5 tab-separated fields, got {}: {!r}".format(
                    len(fields), line)
            )
            name, used, free, capacity, blocksize = fields

            assert len(name) > 0, "Volume name should not be empty"
            assert used.isdigit(), (
                "Used should be numeric, got: {!r}".format(used)
            )
            assert free.isdigit(), (
                "Free should be numeric, got: {!r}".format(free)
            )
            assert capacity.isdigit(), (
                "Capacity should be numeric, got: {!r}".format(capacity)
            )
            assert blocksize.isdigit(), (
                "Blocksize should be numeric, got: {!r}".format(blocksize)
            )
            assert int(blocksize) > 0, (
                "Blocksize should be positive, got: {!r}".format(blocksize)
            )


# ---------------------------------------------------------------------------
# TASKS
# ---------------------------------------------------------------------------

class TestTasks:
    """Tests for the TASKS command."""

    def test_tasks_has_daemon(self, raw_connection):
        """TASKS returns at least one task entry.
        COMMANDS.md: 'The currently executing task (the daemon itself) is
        listed with state run.'  A running system always has tasks."""
        sock, _banner = raw_connection
        send_command(sock, "TASKS")
        status, payload = read_response(sock)
        assert status == "OK"
        assert len(payload) > 0, "Expected at least one task entry"

    def test_tasks_format(self, raw_connection):
        """TASKS lines are tab-separated with correct field types.
        COMMANDS.md: 'Each payload line contains five tab-separated fields:
        name, type (TASK/PROCESS), priority (signed integer), state
        (run/ready/wait), stacksize (numeric).'"""
        sock, _banner = raw_connection
        send_command(sock, "TASKS")
        status, payload = read_response(sock)
        assert status == "OK"
        assert len(payload) > 0

        for line in payload:
            fields = line.split("\t")
            assert len(fields) == 5, (
                "Expected 5 tab-separated fields, got {}: {!r}".format(
                    len(fields), line)
            )
            name, task_type, priority, state, stacksize = fields

            assert len(name) > 0, "Task name should not be empty"
            assert task_type in ("TASK", "PROCESS"), (
                "Type must be TASK or PROCESS, got: {!r}".format(task_type)
            )

            # Priority is a signed integer
            try:
                int(priority)
            except ValueError:
                pytest.fail(
                    "Priority should be an integer, got: {!r}".format(
                        priority)
                )

            assert state in ("run", "ready", "wait"), (
                "State must be run/ready/wait, got: {!r}".format(state)
            )
            assert stacksize.isdigit(), (
                "Stacksize should be numeric, got: {!r}".format(stacksize)
            )
            assert int(stacksize) > 0, (
                "Stacksize should be positive, got: {!r}".format(stacksize)
            )


# ---------------------------------------------------------------------------
# ASSIGN (mutation command)
# ---------------------------------------------------------------------------

class TestAssign:
    """Tests for the ASSIGN command (create/modify/remove assigns)."""

    def _cleanup_assign(self, conn, name):
        """Best-effort removal of a test assign."""
        try:
            conn.assign(name)
        except Exception:
            pass

    def test_assign_create(self, conn):
        """ASSIGN NAME: PATH creates a new assign visible in ASSIGNS."""
        try:
            conn.assign("TEST:", "RAM:")
            assigns = conn.assigns()
            assert "TEST:" in assigns, (
                "TEST: not found after ASSIGN. Keys: {}".format(
                    sorted(assigns.keys()))
            )
        finally:
            self._cleanup_assign(conn, "TEST:")

    def test_assign_remove(self, conn):
        """ASSIGN NAME: (no path) removes an existing assign."""
        try:
            conn.assign("TEST:", "RAM:")
            assigns = conn.assigns()
            assert "TEST:" in assigns

            conn.assign("TEST:")
            assigns = conn.assigns()
            assert "TEST:" not in assigns, (
                "TEST: still present after removal. Keys: {}".format(
                    sorted(assigns.keys()))
            )
        finally:
            self._cleanup_assign(conn, "TEST:")

    def test_assign_late(self, conn):
        """ASSIGN LATE NAME: PATH creates a late-binding assign."""
        try:
            conn.assign("TEST:", "RAM:", mode="late")
            assigns = conn.assigns()
            assert "TEST:" in assigns, (
                "TEST: not found after ASSIGN LATE. Keys: {}".format(
                    sorted(assigns.keys()))
            )
        finally:
            self._cleanup_assign(conn, "TEST:")

    def test_assign_add(self, conn):
        """ASSIGN ADD NAME: PATH adds to a multi-directory assign."""
        try:
            # First create the base assign
            conn.assign("TEST:", "RAM:")
            assigns = conn.assigns()
            assert "TEST:" in assigns

            # Add a second directory
            conn.assign("TEST:", "SYS:", mode="add")
            assigns = conn.assigns()
            assert "TEST:" in assigns
            # Multi-dir assigns are semicolon-separated
            test_path = assigns["TEST:"]
            assert ";" in test_path, (
                "Expected multi-dir assign with semicolons, got: {!r}".format(
                    test_path)
            )
        finally:
            self._cleanup_assign(conn, "TEST:")

    def test_assign_bad_path(self, conn):
        """ASSIGN NAME: BADPATH returns an error for nonexistent path."""
        with pytest.raises(Exception) as exc_info:
            conn.assign("TEST:", "RAM:NoSuchDirForAssignTest12345")
        # Should be an IO error (Lock failed) or NotFound
        assert exc_info.value.code in (200, 300), (
            "Expected error code 200 or 300, got: {}".format(
                exc_info.value.code)
        )

    def test_assign_syntax(self, conn):
        """ASSIGN with missing args or no colon returns ERR 100."""
        # Missing arguments entirely
        with pytest.raises(Exception) as exc_info:
            conn.assign("")
        assert exc_info.value.code == 100

        # Name without colon
        with pytest.raises(Exception) as exc_info:
            conn.assign("NOCOLON")
        assert exc_info.value.code == 100

    def test_assign_late_no_lock(self, conn):
        """ASSIGN LATE with nonexistent path succeeds (no lock at creation)."""
        try:
            # Late-bind does not verify the path exists
            conn.assign("TEST:", "RAM:NoSuchDirForAssignTest12345",
                        mode="late")
            assigns = conn.assigns()
            assert "TEST:" in assigns, (
                "TEST: not found after ASSIGN LATE with bad path. "
                "Keys: {}".format(sorted(assigns.keys()))
            )
        finally:
            self._cleanup_assign(conn, "TEST:")
