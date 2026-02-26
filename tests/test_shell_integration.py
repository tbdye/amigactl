"""Shell command integration tests against a live amigactld daemon.

These tests exercise the full pipeline: shell command -> client library ->
daemon -> response parsing -> output formatting.  They require a running
daemon (same as other integration tests).

Usage:
    pytest tests/test_shell_integration.py --host 192.168.6.228 -v
"""

import cmd
import socket

import pytest

from amigactl.shell import AmigaShell, _DirCache
from amigactl.colors import ColorWriter
from conftest import (
    _read_line, pre_clean, send_command, send_write_data, read_response,
)

# Paths managed by the fixture (deepest first for cleanup).
# Must stay in sync with the explicit MAKEDIR/WRITE commands in setup.
_FIXTURE_PATHS = [
    "RAM:amigactl_rectest_shell/sub1/deep/file3.txt",
    "RAM:amigactl_rectest_shell/sub1/deep",
    "RAM:amigactl_rectest_shell/sub1/file2.txt",
    "RAM:amigactl_rectest_shell/sub1",
    "RAM:amigactl_rectest_shell/file1.txt",
    "RAM:amigactl_rectest_shell",
]


# ---------------------------------------------------------------------------
# Shell fixture backed by a live connection
# ---------------------------------------------------------------------------

@pytest.fixture
def shell(conn):
    """Create an AmigaShell backed by a live AmigaConnection."""
    s = AmigaShell.__new__(AmigaShell)
    cmd.Cmd.__init__(s)
    s.host = "test"
    s.port = 6800
    s.timeout = 30
    s.conn = conn
    s.cw = ColorWriter(force_color=False)
    s.cwd = None
    s._dir_cache = _DirCache()
    s._editor = None
    return s


# ---------------------------------------------------------------------------
# Module-scoped test data fixture
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def shell_fixture(request):
    """Create a known directory structure on RAM: once per module.

    Uses its own connection for setup and teardown to avoid depending
    on function-scoped fixtures.  The structure is read-only during
    tests, so sharing across all tests in the module is safe.

    Structure:
        RAM:amigactl_rectest_shell/
            file1.txt          ("top level file\\n")
            sub1/
                file2.txt      ("mid level file\\nhello world\\n")
                deep/
                    file3.txt  ("deep level file\\nhello deep\\n")
    """
    host = request.config.getoption("--host")
    port = request.config.getoption("--port")

    # --- Setup: open a connection and create the directory structure ---
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(10)
    sock.connect((host, port))
    _read_line(sock)  # banner

    try:
        # Pre-clean stale data from interrupted prior runs (deepest first)
        for path in _FIXTURE_PATHS:
            pre_clean(sock, path)

        # Create directories
        send_command(sock, "MAKEDIR RAM:amigactl_rectest_shell")
        status, _ = read_response(sock)
        assert status == "OK", "MAKEDIR amigactl_rectest_shell: {}".format(status)

        send_command(sock, "MAKEDIR RAM:amigactl_rectest_shell/sub1")
        status, _ = read_response(sock)
        assert status == "OK"

        send_command(sock, "MAKEDIR RAM:amigactl_rectest_shell/sub1/deep")
        status, _ = read_response(sock)
        assert status == "OK"

        # Create files
        status, _ = send_write_data(
            sock, "RAM:amigactl_rectest_shell/file1.txt",
            b"top level file\n")
        assert status.startswith("OK")

        status, _ = send_write_data(
            sock, "RAM:amigactl_rectest_shell/sub1/file2.txt",
            b"mid level file\nhello world\n")
        assert status.startswith("OK")

        status, _ = send_write_data(
            sock, "RAM:amigactl_rectest_shell/sub1/deep/file3.txt",
            b"deep level file\nhello deep\n")
        assert status.startswith("OK")
    except Exception:
        sock.close()
        raise

    sock.close()

    # --- Yield: all module tests run here ---
    yield

    # --- Teardown: PROTECT + DELETE in deepest-first order ---
    try:
        sock2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock2.settimeout(10)
        sock2.connect((host, port))
        _read_line(sock2)  # banner
        try:
            for path in _FIXTURE_PATHS:
                pre_clean(sock2, path)
        finally:
            sock2.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# tree
# ---------------------------------------------------------------------------

class TestTreeIntegration:
    """Integration tests for do_tree against a live daemon."""

    def test_tree_subdir(self, shell, shell_fixture, capsys):
        """tree on a subdirectory shows the full nested structure."""
        shell.do_tree("RAM:amigactl_rectest_shell")
        out = capsys.readouterr().out
        assert "RAM:amigactl_rectest_shell" in out
        assert "file1.txt" in out
        assert "sub1" in out
        assert "file2.txt" in out
        assert "deep" in out
        assert "file3.txt" in out
        assert "2 directories, 3 files" in out

    def test_tree_volume_root(self, shell, shell_fixture, capsys):
        """tree on a volume root recurses into nested directories."""
        shell.do_tree("RAM:")
        out = capsys.readouterr().out
        assert "RAM:" in out.split("\n")[0]
        # Our test structure should be visible
        assert "amigactl_rectest_shell" in out
        assert "sub1" in out
        assert "file3.txt" in out

    def test_tree_dirs_only(self, shell, shell_fixture, capsys):
        """tree -d shows only directories, no files."""
        shell.do_tree("-d RAM:amigactl_rectest_shell")
        out = capsys.readouterr().out
        assert "sub1" in out
        assert "deep" in out
        assert "file1.txt" not in out
        assert "file2.txt" not in out
        assert "file3.txt" not in out
        assert "2 directories, 0 files" in out

    def test_tree_ascii(self, shell, shell_fixture, capsys):
        """tree --ascii uses ASCII box-drawing characters."""
        shell.do_tree("--ascii RAM:amigactl_rectest_shell")
        out = capsys.readouterr().out
        assert "|--" in out or "`--" in out
        assert "\u251c" not in out
        assert "\u2514" not in out


# ---------------------------------------------------------------------------
# find
# ---------------------------------------------------------------------------

class TestFindIntegration:
    """Integration tests for do_find against a live daemon."""

    def test_find_pattern(self, shell, shell_fixture, capsys):
        """find *.txt matches all text files recursively."""
        shell.do_find("RAM:amigactl_rectest_shell *.txt")
        out = capsys.readouterr().out
        assert "file1.txt" in out
        assert "sub1/file2.txt" in out
        assert "sub1/deep/file3.txt" in out

    def test_find_type_dir(self, shell, shell_fixture, capsys):
        """find -type d shows only directories."""
        shell.do_find("RAM:amigactl_rectest_shell -type d *")
        out = capsys.readouterr().out
        assert "sub1" in out
        # deep may appear as "sub1/deep"
        assert "deep" in out
        assert "file1.txt" not in out

    def test_find_type_file(self, shell, shell_fixture, capsys):
        """find -type f shows only files."""
        shell.do_find("RAM:amigactl_rectest_shell -type f *.txt")
        out = capsys.readouterr().out
        assert "file1.txt" in out
        assert "file2.txt" in out
        assert "file3.txt" in out
        # Directory names alone should not appear
        lines = out.strip().split("\n")
        dir_only = [l for l in lines if l.strip() in ("sub1", "deep")]
        assert len(dir_only) == 0

    def test_find_no_matches(self, shell, shell_fixture, capsys):
        """find with unmatched pattern produces no output."""
        shell.do_find("RAM:amigactl_rectest_shell *.nonexistent")
        out = capsys.readouterr().out
        assert out.strip() == ""

    def test_find_volume_root(self, shell, shell_fixture, capsys):
        """find from volume root descends into nested directories."""
        shell.do_find("RAM: *.txt")
        out = capsys.readouterr().out
        # Our deep file should be found via volume root recursion
        assert "file3.txt" in out


# ---------------------------------------------------------------------------
# du
# ---------------------------------------------------------------------------

class TestDuIntegration:
    """Integration tests for do_du against a live daemon."""

    def test_du_subdir(self, shell, shell_fixture, capsys):
        """du shows per-directory sizes."""
        shell.do_du("RAM:amigactl_rectest_shell")
        out = capsys.readouterr().out
        # Should have per-directory lines
        assert "sub1" in out or "deep" in out
        # Total line should include the path
        assert "amigactl_rectest_shell" in out

    def test_du_summary(self, shell, shell_fixture, capsys):
        """du -s shows only the total."""
        shell.do_du("-s RAM:amigactl_rectest_shell")
        out = capsys.readouterr().out
        lines = [l for l in out.strip().split("\n") if l.strip()]
        assert len(lines) == 1
        assert "amigactl_rectest_shell" in lines[0]

    def test_du_human_readable(self, shell, shell_fixture, capsys):
        """du -sh shows human-readable total."""
        shell.do_du("-sh RAM:amigactl_rectest_shell")
        out = capsys.readouterr().out
        assert "amigactl_rectest_shell" in out

    def test_du_volume_root(self, shell, shell_fixture, capsys):
        """du on volume root includes nested directory sizes."""
        shell.do_du("RAM:")
        out = capsys.readouterr().out
        # Should have multiple lines (per-directory breakdown)
        lines = [l for l in out.strip().split("\n") if l.strip()]
        assert len(lines) > 1


# ---------------------------------------------------------------------------
# grep
# ---------------------------------------------------------------------------

class TestGrepIntegration:
    """Integration tests for do_grep against a live daemon."""

    def test_grep_recursive_subdir(self, shell, shell_fixture, capsys):
        """grep -r finds matches in nested files."""
        shell.do_grep("-r hello RAM:amigactl_rectest_shell")
        out = capsys.readouterr().out
        assert "hello world" in out
        assert "hello deep" in out

    def test_grep_recursive_volume_root(self, shell, shell_fixture, capsys):
        """grep -r from volume root reads files in nested directories."""
        shell.do_grep("-r 'deep level' RAM:")
        out = capsys.readouterr().out
        assert "deep level" in out

    def test_grep_single_file(self, shell, shell_fixture, capsys):
        """grep on a single file finds matching lines."""
        shell.do_grep("hello RAM:amigactl_rectest_shell/sub1/file2.txt")
        out = capsys.readouterr().out
        assert "hello world" in out

    def test_grep_line_numbers(self, shell, shell_fixture, capsys):
        """grep -n shows line numbers."""
        shell.do_grep("-n hello RAM:amigactl_rectest_shell/sub1/file2.txt")
        out = capsys.readouterr().out
        assert "2:" in out  # "hello world" is line 2

    def test_grep_count(self, shell, shell_fixture, capsys):
        """grep -c shows match count."""
        shell.do_grep("-c hello RAM:amigactl_rectest_shell/sub1/file2.txt")
        out = capsys.readouterr().out
        assert "1" in out

    def test_grep_filenames_only(self, shell, shell_fixture, capsys):
        """grep -rl shows only filenames with matches."""
        shell.do_grep("-rl hello RAM:amigactl_rectest_shell")
        out = capsys.readouterr().out
        assert "file2.txt" in out
        assert "file3.txt" in out
        # Line content should not appear
        assert "hello world" not in out

    def test_grep_no_match(self, shell, shell_fixture, capsys):
        """grep with unmatched pattern produces no output."""
        shell.do_grep("-r nonexistent_xyz RAM:amigactl_rectest_shell")
        out = capsys.readouterr().out
        assert out.strip() == ""


# ---------------------------------------------------------------------------
# ls
# ---------------------------------------------------------------------------

class TestLsIntegration:
    """Integration tests for do_ls against a live daemon."""

    def test_ls_subdir(self, shell, shell_fixture, capsys):
        """ls shows directory contents."""
        shell.do_ls("RAM:amigactl_rectest_shell")
        out = capsys.readouterr().out
        assert "file1.txt" in out
        assert "sub1" in out

    def test_ls_long_format(self, shell, shell_fixture, capsys):
        """ls -l shows detailed listing with protection bits."""
        shell.do_ls("-l RAM:amigactl_rectest_shell")
        out = capsys.readouterr().out
        assert "file1.txt" in out
        assert "rwed" in out or "----" in out

    def test_ls_recursive_subdir(self, shell, shell_fixture, capsys):
        """ls -r shows nested entries."""
        shell.do_ls("-r RAM:amigactl_rectest_shell")
        out = capsys.readouterr().out
        assert "file2.txt" in out
        assert "file3.txt" in out

    def test_ls_recursive_volume_root(self, shell, shell_fixture, capsys):
        """ls -r from volume root shows deeply nested entries."""
        shell.do_ls("-r RAM:")
        out = capsys.readouterr().out
        # Our deep entries should appear
        assert "file3.txt" in out or "deep" in out

    def test_ls_after_cd_volume_root(self, shell, shell_fixture, capsys):
        """ls after cd to volume root resolves relative paths correctly."""
        shell.do_cd("RAM:")
        assert shell.cwd == "RAM:"
        shell.do_ls("amigactl_rectest_shell")
        out = capsys.readouterr().out
        assert "file1.txt" in out
        assert "sub1" in out
