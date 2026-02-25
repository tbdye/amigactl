"""System information tests for amigactld.

These tests exercise the system query commands (SYSINFO, ASSIGNS, PORTS,
VOLUMES, TASKS) and the ASSIGN mutation command against a live amigactld
daemon.

The daemon must be running on the target machine before these tests are
executed.
"""

import re

import pytest

from conftest import read_response, send_command
from amigactl import (
    AmigaConnection, RemoteIOError, NotFoundError,
    CommandSyntaxError, AlreadyExistsError,
)


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
            "chip_largest", "fast_largest",
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

    def test_sysinfo_chip_largest_numeric(self, raw_connection):
        """SYSINFO chip_largest is present and numeric."""
        sock, _banner = raw_connection
        send_command(sock, "SYSINFO")
        status, payload = read_response(sock)
        assert status == "OK"

        kv = {}
        for line in payload:
            key, _, value = line.partition("=")
            kv[key] = value

        assert "chip_largest" in kv, (
            "chip_largest missing from SYSINFO. Keys: {}".format(
                sorted(kv.keys()))
        )
        assert kv["chip_largest"].isdigit(), (
            "chip_largest should be numeric, got: {!r}".format(
                kv["chip_largest"])
        )

    def test_sysinfo_fast_largest_numeric(self, raw_connection):
        """SYSINFO fast_largest is present and numeric."""
        sock, _banner = raw_connection
        send_command(sock, "SYSINFO")
        status, payload = read_response(sock)
        assert status == "OK"

        kv = {}
        for line in payload:
            key, _, value = line.partition("=")
            kv[key] = value

        assert "fast_largest" in kv, (
            "fast_largest missing from SYSINFO. Keys: {}".format(
                sorted(kv.keys()))
        )
        assert kv["fast_largest"].isdigit(), (
            "fast_largest should be numeric, got: {!r}".format(
                kv["fast_largest"])
        )

    def test_sysinfo_chip_largest_bounded(self, raw_connection):
        """SYSINFO chip_largest <= chip_free."""
        sock, _banner = raw_connection
        send_command(sock, "SYSINFO")
        status, payload = read_response(sock)
        assert status == "OK"

        kv = {}
        for line in payload:
            key, _, value = line.partition("=")
            kv[key] = value

        assert int(kv["chip_largest"]) <= int(kv["chip_free"]), (
            "chip_largest ({}) should be <= chip_free ({})".format(
                kv["chip_largest"], kv["chip_free"])
        )

    def test_sysinfo_fast_largest_bounded(self, raw_connection):
        """SYSINFO fast_largest <= fast_free."""
        sock, _banner = raw_connection
        send_command(sock, "SYSINFO")
        status, payload = read_response(sock)
        assert status == "OK"

        kv = {}
        for line in payload:
            key, _, value = line.partition("=")
            kv[key] = value

        assert int(kv["fast_largest"]) <= int(kv["fast_free"]), (
            "fast_largest ({}) should be <= fast_free ({})".format(
                kv["fast_largest"], kv["fast_free"])
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


# ---------------------------------------------------------------------------
# LIBVER
# ---------------------------------------------------------------------------

class TestLibver:
    """Tests for the LIBVER command."""

    def test_libver_exec(self, raw_connection):
        """LIBVER exec.library returns name and version."""
        sock, _banner = raw_connection
        send_command(sock, "LIBVER exec.library")
        status, payload = read_response(sock)
        assert status == "OK"

        kv = {}
        for line in payload:
            key, _, value = line.partition("=")
            kv[key] = value

        assert kv.get("name") == "exec.library", (
            "Expected name=exec.library, got: {!r}".format(kv.get("name"))
        )
        assert re.match(r"\d+\.\d+$", kv.get("version", "")), (
            "version should match N.N, got: {!r}".format(kv.get("version"))
        )

    def test_libver_dos(self, raw_connection):
        """LIBVER dos.library returns name and version."""
        sock, _banner = raw_connection
        send_command(sock, "LIBVER dos.library")
        status, payload = read_response(sock)
        assert status == "OK"

        kv = {}
        for line in payload:
            key, _, value = line.partition("=")
            kv[key] = value

        assert kv.get("name") == "dos.library", (
            "Expected name=dos.library, got: {!r}".format(kv.get("name"))
        )
        assert re.match(r"\d+\.\d+$", kv.get("version", "")), (
            "version should match N.N, got: {!r}".format(kv.get("version"))
        )

    def test_libver_device(self, raw_connection):
        """LIBVER timer.device returns name and version."""
        sock, _banner = raw_connection
        send_command(sock, "LIBVER timer.device")
        status, payload = read_response(sock)
        assert status == "OK"

        kv = {}
        for line in payload:
            key, _, value = line.partition("=")
            kv[key] = value

        assert kv.get("name") == "timer.device", (
            "Expected name=timer.device, got: {!r}".format(kv.get("name"))
        )
        assert re.match(r"\d+\.\d+$", kv.get("version", "")), (
            "version should match N.N, got: {!r}".format(kv.get("version"))
        )

    def test_libver_not_found(self, raw_connection):
        """LIBVER nonexistent.library returns ERR 200."""
        sock, _banner = raw_connection
        send_command(sock, "LIBVER nonexistent.library")
        status, payload = read_response(sock)
        assert status.startswith("ERR 200"), (
            "Expected ERR 200, got: {!r}".format(status)
        )

    def test_libver_missing_name(self, raw_connection):
        """LIBVER with no arguments returns ERR 100."""
        sock, _banner = raw_connection
        send_command(sock, "LIBVER")
        status, payload = read_response(sock)
        assert status.startswith("ERR 100"), (
            "Expected ERR 100, got: {!r}".format(status)
        )

    def test_libver_via_client(self, conn):
        """Client libver() returns dict with name and version keys."""
        result = conn.libver("exec.library")
        assert "name" in result, (
            "name missing from libver result. Keys: {}".format(
                sorted(result.keys()))
        )
        assert "version" in result, (
            "version missing from libver result. Keys: {}".format(
                sorted(result.keys()))
        )


# ---------------------------------------------------------------------------
# ENV / SETENV
# ---------------------------------------------------------------------------

class TestEnv:
    """Tests for the ENV and SETENV commands."""

    def test_env_set_and_read(self, raw_connection, cleanup_env):
        """SETENV creates a variable, ENV reads it back."""
        sock, _banner = raw_connection
        cleanup_env.add("amigactl_test_env")

        send_command(sock, "SETENV amigactl_test_env testvalue123")
        status, payload = read_response(sock)
        assert status == "OK"

        send_command(sock, "ENV amigactl_test_env")
        status, payload = read_response(sock)
        assert status == "OK"

        kv = {}
        for line in payload:
            key, _, value = line.partition("=")
            kv[key] = value

        assert kv.get("value") == "testvalue123", (
            "Expected value=testvalue123, got: {!r}".format(kv.get("value"))
        )

    def test_env_not_found(self, raw_connection):
        """ENV for a nonexistent variable returns ERR 200."""
        sock, _banner = raw_connection
        send_command(sock, "ENV nonexistent_amigactl_test_xyz")
        status, payload = read_response(sock)
        assert status.startswith("ERR 200"), (
            "Expected ERR 200, got: {!r}".format(status)
        )

    def test_env_missing_name(self, raw_connection):
        """ENV with no arguments returns ERR 100."""
        sock, _banner = raw_connection
        send_command(sock, "ENV")
        status, payload = read_response(sock)
        assert status.startswith("ERR 100"), (
            "Expected ERR 100, got: {!r}".format(status)
        )

    def test_setenv_delete(self, raw_connection, cleanup_env):
        """SETENV with name only deletes the variable."""
        sock, _banner = raw_connection
        cleanup_env.add("amigactl_test_del_raw")

        # Create the variable
        send_command(sock, "SETENV amigactl_test_del_raw deletetest")
        status, payload = read_response(sock)
        assert status == "OK"

        # Verify it exists
        send_command(sock, "ENV amigactl_test_del_raw")
        status, payload = read_response(sock)
        assert status == "OK"

        # Delete it (name only, no value)
        send_command(sock, "SETENV amigactl_test_del_raw")
        status, payload = read_response(sock)
        assert status == "OK"

        # Verify it is gone
        send_command(sock, "ENV amigactl_test_del_raw")
        status, payload = read_response(sock)
        assert status.startswith("ERR 200"), (
            "Expected ERR 200 after delete, got: {!r}".format(status)
        )

    def test_setenv_volatile(self, raw_connection, cleanup_env):
        """SETENV VOLATILE creates a volatile variable readable by ENV."""
        sock, _banner = raw_connection
        cleanup_env.add("amigactl_test_vol", volatile=True)

        send_command(sock, "SETENV VOLATILE amigactl_test_vol volval")
        status, payload = read_response(sock)
        assert status == "OK"

        send_command(sock, "ENV amigactl_test_vol")
        status, payload = read_response(sock)
        assert status == "OK"

        kv = {}
        for line in payload:
            key, _, value = line.partition("=")
            kv[key] = value

        assert kv.get("value") == "volval", (
            "Expected value=volval, got: {!r}".format(kv.get("value"))
        )

    def test_setenv_volatile_keyword_alone(self, raw_connection):
        """SETENV VOLATILE with no name or value returns ERR 100."""
        sock, _banner = raw_connection
        send_command(sock, "SETENV VOLATILE")
        status, payload = read_response(sock)
        assert status.startswith("ERR 100"), (
            "Expected ERR 100, got: {!r}".format(status)
        )

    def test_setenv_missing_name(self, raw_connection):
        """SETENV with no arguments returns ERR 100."""
        sock, _banner = raw_connection
        send_command(sock, "SETENV")
        status, payload = read_response(sock)
        assert status.startswith("ERR 100"), (
            "Expected ERR 100, got: {!r}".format(status)
        )

    def test_setenv_value_with_spaces(self, raw_connection, cleanup_env):
        """SETENV preserves spaces in the value."""
        sock, _banner = raw_connection
        cleanup_env.add("amigactl_test_spaces")

        send_command(sock, "SETENV amigactl_test_spaces hello world")
        status, payload = read_response(sock)
        assert status == "OK"

        send_command(sock, "ENV amigactl_test_spaces")
        status, payload = read_response(sock)
        assert status == "OK"

        kv = {}
        for line in payload:
            key, _, value = line.partition("=")
            kv[key] = value

        assert kv.get("value") == "hello world", (
            "Expected value='hello world', got: {!r}".format(kv.get("value"))
        )

    def test_env_via_client(self, conn, cleanup_env):
        """Client setenv()/env() round-trip works."""
        cleanup_env.add("amigactl_test_client")

        conn.setenv("amigactl_test_client", "clientval")
        result = conn.env("amigactl_test_client")
        assert result == "clientval", (
            "Expected 'clientval', got: {!r}".format(result)
        )

    def test_setenv_delete_via_client(self, conn, cleanup_env):
        """Client setenv() with no value deletes, env() raises NotFoundError."""
        cleanup_env.add("amigactl_test_del")

        conn.setenv("amigactl_test_del", "todelete")
        # Verify it exists
        result = conn.env("amigactl_test_del")
        assert result == "todelete"

        # Delete it
        conn.setenv("amigactl_test_del")

        # Verify it is gone
        with pytest.raises(NotFoundError):
            conn.env("amigactl_test_del")


# ---------------------------------------------------------------------------
# DEVICES
# ---------------------------------------------------------------------------

class TestDevices:
    """Tests for the DEVICES command."""

    def test_devices_returns_entries(self, raw_connection):
        """DEVICES returns at least one entry."""
        sock, _banner = raw_connection
        send_command(sock, "DEVICES")
        status, payload = read_response(sock)
        assert status == "OK"
        assert len(payload) > 0, "Expected at least one device entry"

    def test_devices_format(self, raw_connection):
        """DEVICES lines are tab-separated with version containing a dot."""
        sock, _banner = raw_connection
        send_command(sock, "DEVICES")
        status, payload = read_response(sock)
        assert status == "OK"
        assert len(payload) > 0

        for line in payload:
            fields = line.split("\t")
            assert len(fields) == 2, (
                "Expected 2 tab-separated fields, got {}: {!r}".format(
                    len(fields), line)
            )
            name, version = fields
            assert len(name) > 0, "Device name should not be empty"
            assert "." in version, (
                "Version should contain a dot, got: {!r}".format(version)
            )

    def test_devices_has_timer(self, raw_connection):
        """DEVICES includes timer.device."""
        sock, _banner = raw_connection
        send_command(sock, "DEVICES")
        status, payload = read_response(sock)
        assert status == "OK"

        names = []
        for line in payload:
            fields = line.split("\t")
            if len(fields) >= 1:
                names.append(fields[0])

        assert "timer.device" in names, (
            "timer.device not found in devices. Names: {}".format(
                sorted(names))
        )

    def test_devices_via_client(self, conn):
        """Client devices() returns list of dicts with name and version."""
        result = conn.devices()
        assert len(result) > 0, "Expected at least one device"
        for entry in result:
            assert "name" in entry, (
                "name missing from device entry. Keys: {}".format(
                    sorted(entry.keys()))
            )
            assert "version" in entry, (
                "version missing from device entry. Keys: {}".format(
                    sorted(entry.keys()))
            )


# ---------------------------------------------------------------------------
# CAPABILITIES
# ---------------------------------------------------------------------------

class TestCapabilities:
    """Tests for the CAPABILITIES command."""

    def test_capabilities_fields(self, raw_connection):
        """CAPABILITIES returns all expected fields."""
        sock, _banner = raw_connection
        send_command(sock, "CAPABILITIES")
        status, payload = read_response(sock)
        assert status == "OK"

        kv = {}
        for line in payload:
            key, _, value = line.partition("=")
            kv[key] = value

        required_keys = [
            "version", "protocol", "max_clients", "max_cmd_len", "commands",
        ]
        for key in required_keys:
            assert key in kv, (
                "Required key {!r} missing from CAPABILITIES. Keys: {}".format(
                    key, sorted(kv.keys()))
            )

    def test_capabilities_version_format(self, raw_connection):
        """CAPABILITIES version matches X.Y.Z and agrees with banner."""
        sock, banner = raw_connection
        send_command(sock, "CAPABILITIES")
        status, payload = read_response(sock)
        assert status == "OK"

        kv = {}
        for line in payload:
            key, _, value = line.partition("=")
            kv[key] = value

        assert re.match(r"\d+\.\d+\.\d+$", kv["version"]), (
            "version should match X.Y.Z, got: {!r}".format(kv["version"])
        )

        # Banner format is "AMIGACTL X.Y.Z"
        banner_version = banner.split()[1]
        assert kv["version"] == banner_version, (
            "CAPABILITIES version ({}) should match banner version ({})".format(
                kv["version"], banner_version)
        )

    def test_capabilities_protocol_format(self, raw_connection):
        """CAPABILITIES protocol matches X.Y format."""
        sock, _banner = raw_connection
        send_command(sock, "CAPABILITIES")
        status, payload = read_response(sock)
        assert status == "OK"

        kv = {}
        for line in payload:
            key, _, value = line.partition("=")
            kv[key] = value

        assert re.match(r"\d+\.\d+$", kv["protocol"]), (
            "protocol should match X.Y, got: {!r}".format(kv["protocol"])
        )

    def test_capabilities_commands_sorted(self, raw_connection):
        """CAPABILITIES commands are sorted and include expected entries."""
        sock, _banner = raw_connection
        send_command(sock, "CAPABILITIES")
        status, payload = read_response(sock)
        assert status == "OK"

        kv = {}
        for line in payload:
            key, _, value = line.partition("=")
            kv[key] = value

        commands = [c.strip() for c in kv["commands"].split(",")]
        assert commands == sorted(commands), (
            "Commands should be sorted. Got: {}".format(commands)
        )

        expected_commands = [
            "COPY", "APPEND", "CHECKSUM", "SETCOMMENT",
            "LIBVER", "ENV", "SETENV", "DEVICES", "CAPABILITIES",
        ]
        for cmd in expected_commands:
            assert cmd in commands, (
                "{} not found in commands list: {}".format(cmd, commands)
            )

    def test_capabilities_via_client(self, conn):
        """Client capabilities() returns dict with all expected keys."""
        result = conn.capabilities()
        required_keys = [
            "version", "protocol", "max_clients", "max_cmd_len", "commands",
        ]
        for key in required_keys:
            assert key in result, (
                "Required key {!r} missing from capabilities. Keys: {}".format(
                    key, sorted(result.keys()))
            )
        assert "COPY" in result["commands"], (
            "COPY not found in commands: {!r}".format(result["commands"])
        )
        assert isinstance(result["max_clients"], int), (
            "max_clients should be int, got: {!r}".format(
                type(result["max_clients"]))
        )


# ---------------------------------------------------------------------------
# CAPABILITIES (numeric validation)
# ---------------------------------------------------------------------------

class TestCapabilitiesNumeric:
    """Tests for CAPABILITIES numeric field validation."""

    def test_capabilities_max_clients_numeric(self, raw_connection):
        """CAPABILITIES max_clients is present and numeric with value 8."""
        sock, _banner = raw_connection
        send_command(sock, "CAPABILITIES")
        status, payload = read_response(sock)
        assert status == "OK"

        kv = {}
        for line in payload:
            key, _, value = line.partition("=")
            kv[key] = value

        assert "max_clients" in kv, (
            "max_clients missing from CAPABILITIES. Keys: {}".format(
                sorted(kv.keys()))
        )
        assert kv["max_clients"].isdigit(), (
            "max_clients should be numeric, got: {!r}".format(
                kv["max_clients"])
        )
        assert int(kv["max_clients"]) == 8, (
            "max_clients should be 8, got: {}".format(kv["max_clients"])
        )

    def test_capabilities_max_cmd_len_numeric(self, raw_connection):
        """CAPABILITIES max_cmd_len is present and numeric with value 4096."""
        sock, _banner = raw_connection
        send_command(sock, "CAPABILITIES")
        status, payload = read_response(sock)
        assert status == "OK"

        kv = {}
        for line in payload:
            key, _, value = line.partition("=")
            kv[key] = value

        assert "max_cmd_len" in kv, (
            "max_cmd_len missing from CAPABILITIES. Keys: {}".format(
                sorted(kv.keys()))
        )
        assert kv["max_cmd_len"].isdigit(), (
            "max_cmd_len should be numeric, got: {!r}".format(
                kv["max_cmd_len"])
        )
        assert int(kv["max_cmd_len"]) == 4096, (
            "max_cmd_len should be 4096, got: {}".format(kv["max_cmd_len"])
        )


# ---------------------------------------------------------------------------
# ASSIGN mode validation (client-side)
# ---------------------------------------------------------------------------

class TestAssignModeValidation:
    """Tests for assign() mode parameter validation."""

    def test_assign_invalid_mode(self, conn):
        """assign() with invalid mode raises ValueError."""
        with pytest.raises(ValueError):
            conn.assign("TEST:", "RAM:", mode="invalid")


# ---------------------------------------------------------------------------
# AmigaConnection repr
# ---------------------------------------------------------------------------

class TestClientRepr:
    """Tests for AmigaConnection.__repr__."""

    def test_connection_repr(self, conn):
        """repr(conn) contains host and 'connected'."""
        r = repr(conn)
        assert "connected" in r, (
            "repr should contain 'connected', got: {!r}".format(r)
        )


# ---------------------------------------------------------------------------
# ENV return type validation
# ---------------------------------------------------------------------------

class TestEnvReturnType:
    """Tests for env() return type after API change."""

    def test_env_returns_string(self, conn, cleanup_env):
        """env() returns str, not dict."""
        cleanup_env.add("amigactl_test_env_type")

        conn.setenv("amigactl_test_env_type", "testval")
        result = conn.env("amigactl_test_env_type")
        assert isinstance(result, str), (
            "env() should return str, got: {!r}".format(type(result))
        )
        assert result == "testval", (
            "Expected 'testval', got: {!r}".format(result)
        )


# ---------------------------------------------------------------------------
# SYSINFO numeric conversion validation
# ---------------------------------------------------------------------------

class TestSysinfoNumericConversion:
    """Tests for sysinfo() memory value int conversion."""

    def test_sysinfo_memory_values_are_int(self, conn):
        """sysinfo() returns int for memory keys."""
        result = conn.sysinfo()
        for key in ("chip_free", "fast_free", "total_free"):
            assert isinstance(result[key], int), (
                "{} should be int, got: {!r}".format(key, type(result[key]))
            )


# ---------------------------------------------------------------------------
# CAPABILITIES numeric conversion validation
# ---------------------------------------------------------------------------

class TestCapabilitiesNumericConversion:
    """Tests for capabilities() numeric type conversion."""

    def test_capabilities_numeric_types(self, conn):
        """capabilities() returns int for max_clients and max_cmd_len."""
        result = conn.capabilities()
        assert isinstance(result["max_clients"], int), (
            "max_clients should be int, got: {!r}".format(
                type(result["max_clients"]))
        )
        assert isinstance(result["max_cmd_len"], int), (
            "max_cmd_len should be int, got: {!r}".format(
                type(result["max_cmd_len"]))
        )
