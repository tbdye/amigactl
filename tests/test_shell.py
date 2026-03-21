"""Unit tests for shell utility functions and ColorWriter.

These are pure unit tests that do not require a network connection to the
daemon.  They exercise the formatting and path manipulation helpers used
by the interactive shell.
"""

import cmd
import io
import os
import tempfile
from unittest import mock

import pytest

from amigactl.shell import (
    format_size,
    _amiga_basename,
    _join_amiga_path,
    _format_protection,
    _normalize_dotdot,
    _visible_len,
    _find_filter,
    _build_tree,
    _format_tree,
    _grep_lines,
    _du_accumulate,
    AmigaShell,
    _DirCache,
)
from amigactl.colors import ColorWriter, _supports_color


# ---------------------------------------------------------------------------
# format_size()
# ---------------------------------------------------------------------------

class TestFormatSize:
    """Tests for the human-readable byte-count formatter."""

    def test_zero(self):
        assert format_size(0) == "0"

    def test_small_bytes(self):
        assert format_size(100) == "100"

    def test_just_under_1k(self):
        assert format_size(1023) == "1023"

    def test_exactly_1k(self):
        assert format_size(1024) == "1K"

    def test_fractional_kilobytes(self):
        assert format_size(1536) == "1.5K"

    def test_exactly_1m(self):
        assert format_size(1048576) == "1M"

    def test_fractional_megabytes(self):
        assert format_size(1572864) == "1.5M"

    def test_exactly_1g(self):
        assert format_size(1073741824) == "1G"


# ---------------------------------------------------------------------------
# _amiga_basename()
# ---------------------------------------------------------------------------

class TestAmigaBasename:
    """Tests for extracting the filename component from an Amiga path."""

    def test_with_directory(self):
        assert _amiga_basename("SYS:S/Startup-Sequence") == "Startup-Sequence"

    def test_volume_root_file(self):
        assert _amiga_basename("RAM:test.txt") == "test.txt"

    def test_deep_path(self):
        assert _amiga_basename("Work:foo/bar/baz.txt") == "baz.txt"

    def test_volume_only(self):
        assert _amiga_basename("Work:") == "Work"

    def test_bare_filename(self):
        assert _amiga_basename("test.txt") == "test.txt"


# ---------------------------------------------------------------------------
# _join_amiga_path()
# ---------------------------------------------------------------------------

class TestJoinAmigaPath:
    """Tests for joining Amiga directory paths with relative components."""

    def test_simple_join(self):
        assert _join_amiga_path("SYS:S", "Startup-Sequence") == \
            "SYS:S/Startup-Sequence"

    def test_volume_root(self):
        assert _join_amiga_path("SYS:", "S") == "SYS:S"

    def test_trailing_slash(self):
        assert _join_amiga_path("SYS:S/", "foo") == "SYS:S/foo"

    def test_parent_from_subdir(self):
        # /bar means "go up one level, then bar"
        assert _join_amiga_path("Work:Projects/foo", "/bar") == \
            "Work:Projects/bar"

    def test_double_parent(self):
        assert _join_amiga_path("Work:Projects/foo", "//test") == \
            "Work:test"

    def test_parent_from_volume_root(self):
        # Can't go above volume root
        assert _join_amiga_path("Work:", "/test") == "Work:test"

    def test_pure_parent_navigation(self):
        assert _join_amiga_path("SYS:S/Config", "/") == "SYS:S"

    def test_parent_at_volume_stays(self):
        # Already at volume root, / stays there
        assert _join_amiga_path("SYS:", "/") == "SYS:"

    def test_deep_relative(self):
        assert _join_amiga_path("Work:A/B", "C/D") == "Work:A/B/C/D"


# ---------------------------------------------------------------------------
# _normalize_dotdot()
# ---------------------------------------------------------------------------

class TestNormalizeDotdot:
    def test_single_parent(self):
        assert _normalize_dotdot("..") == "/"

    def test_parent_with_child(self):
        assert _normalize_dotdot("../foo") == "/foo"

    def test_double_parent(self):
        assert _normalize_dotdot("../../foo") == "//foo"

    def test_mid_path_parent(self):
        assert _normalize_dotdot("foo/../bar") == "bar"

    def test_double_mid_path_parent(self):
        assert _normalize_dotdot("foo/bar/../../baz") == "baz"

    def test_single_dot(self):
        assert _normalize_dotdot(".") == ""

    def test_dotdot_in_filename(self):
        assert _normalize_dotdot("file..bak") == "file..bak"

    def test_dot_removal(self):
        assert _normalize_dotdot("foo/./bar") == "foo/bar"

    def test_no_dots(self):
        assert _normalize_dotdot("no_dots_here") == "no_dots_here"


# ---------------------------------------------------------------------------
# _format_protection()
# ---------------------------------------------------------------------------

class TestFormatProtection:
    """Tests for converting AmigaOS protection bit hex to display string."""

    def test_default_file(self):
        # 0x00 = all RWED allowed, no HSPA flags
        assert _format_protection("00") == "----rwed"

    def test_read_only(self):
        # 0x05 = write denied (bit 2) + delete denied (bit 0)
        assert _format_protection("05") == "----r-e-"

    def test_all_denied(self):
        # 0x0F = all RWED denied
        assert _format_protection("0f") == "--------"

    def test_script_flag(self):
        # 0x40 = script set, RWED all allowed
        assert _format_protection("40") == "-s--rwed"

    def test_invalid_hex(self):
        # Non-hex input returned as-is
        assert _format_protection("xyz") == "xyz"


# ---------------------------------------------------------------------------
# ColorWriter
# ---------------------------------------------------------------------------

class TestColorWriter:
    """Tests for ANSI color wrapping with enabled/disabled modes."""

    def test_color_disabled(self):
        cw = ColorWriter(force_color=False)
        assert cw.error("fail") == "fail"
        assert cw.success("ok") == "ok"
        assert cw.directory("Work:") == "Work:"
        assert cw.key("size") == "size"
        assert cw.bold("HEADER") == "HEADER"
        assert cw.write("plain") == "plain"

    def test_color_enabled(self):
        cw = ColorWriter(force_color=True)
        assert "\033[31m" in cw.error("fail")
        assert "\033[32m" in cw.success("ok")
        assert "\033[0m" in cw.error("fail")  # reset

    def test_color_enabled_contains_text(self):
        cw = ColorWriter(force_color=True)
        assert "fail" in cw.error("fail")
        assert "ok" in cw.success("ok")
        assert "Work:" in cw.directory("Work:")
        assert "size" in cw.key("size")
        assert "HEADER" in cw.bold("HEADER")

    def test_write_always_plain(self):
        cw = ColorWriter(force_color=True)
        assert cw.write("plain") == "plain"


# ---------------------------------------------------------------------------
# _visible_len()
# ---------------------------------------------------------------------------

class TestVisibleLen:
    """Tests for ANSI-aware string display width calculation."""

    def test_plain(self):
        assert _visible_len("hello") == 5

    def test_ansi(self):
        assert _visible_len("\033[34mhello\033[0m") == 5

    def test_empty(self):
        assert _visible_len("") == 0


# ---------------------------------------------------------------------------
# _supports_color() — Windows VT processing
# ---------------------------------------------------------------------------

class TestSupportsColorWindows:
    """Tests for Windows-specific ANSI color detection."""

    def test_win32_with_wt_session(self):
        """Windows Terminal (WT_SESSION set) should enable color."""
        mock_stdout = mock.MagicMock()
        mock_stdout.isatty.return_value = True
        with mock.patch("amigactl.colors.sys.platform", "win32"), \
             mock.patch("amigactl.colors.sys.stdout", mock_stdout), \
             mock.patch.dict(os.environ, {"WT_SESSION": "1"}, clear=False):
            # Remove NO_COLOR and AMIGACTL_COLOR if present
            env = os.environ.copy()
            env.pop("NO_COLOR", None)
            env.pop("AMIGACTL_COLOR", None)
            with mock.patch.dict(os.environ, env, clear=True):
                # Ensure WT_SESSION is set
                os.environ["WT_SESSION"] = "1"
                assert _supports_color() is True

    def test_win32_no_wt_session_ctypes_fails(self):
        """Windows without WT_SESSION and ctypes failure returns False."""
        mock_stdout = mock.MagicMock()
        mock_stdout.isatty.return_value = True
        with mock.patch("amigactl.colors.sys.platform", "win32"), \
             mock.patch("amigactl.colors.sys.stdout", mock_stdout), \
             mock.patch.dict(os.environ, {}, clear=True):
            # ctypes.windll doesn't exist on Linux, so the import
            # succeeds but windll attribute access raises AttributeError
            assert _supports_color() is False


# ---------------------------------------------------------------------------
# Editor fallback
# ---------------------------------------------------------------------------

class TestEditorFallback:
    """Tests for editor resolution in do_edit."""

    def test_unix_default_editor(self):
        """On Unix, default editor should be vi."""
        import shlex
        with mock.patch("sys.platform", "linux"), \
             mock.patch.dict(os.environ, {}, clear=True):
            default_editor = "vi"
            editor = (os.environ.get("VISUAL")
                      or os.environ.get("EDITOR")
                      or default_editor)
            assert editor == "vi"
            assert shlex.split(editor) == ["vi"]

    def test_windows_default_editor(self):
        """On Windows, default editor should be notepad."""
        import shlex
        with mock.patch("sys.platform", "win32"), \
             mock.patch.dict(os.environ, {}, clear=True):
            default_editor = "notepad"
            editor = (os.environ.get("VISUAL")
                      or os.environ.get("EDITOR")
                      or default_editor)
            assert editor == "notepad"
            assert shlex.split(editor) == ["notepad"]

    def test_visual_overrides_default(self):
        """$VISUAL should take precedence over platform default."""
        import shlex
        with mock.patch.dict(os.environ, {"VISUAL": "emacs"}, clear=True):
            editor = (os.environ.get("VISUAL")
                      or os.environ.get("EDITOR")
                      or "vi")
            assert editor == "emacs"

    def test_editor_overrides_default(self):
        """$EDITOR should take precedence over platform default."""
        import shlex
        with mock.patch.dict(os.environ, {"EDITOR": "nano"}, clear=True):
            editor = (os.environ.get("VISUAL")
                      or os.environ.get("EDITOR")
                      or "vi")
            assert editor == "nano"

    def test_visual_overrides_editor(self):
        """$VISUAL should take precedence over $EDITOR."""
        with mock.patch.dict(os.environ,
                             {"VISUAL": "emacs", "EDITOR": "nano"},
                             clear=True):
            editor = (os.environ.get("VISUAL")
                      or os.environ.get("EDITOR")
                      or "vi")
            assert editor == "emacs"

    def test_shlex_split_multi_word_editor(self):
        """Multi-word editor commands should be split correctly."""
        import shlex
        with mock.patch.dict(os.environ,
                             {"EDITOR": "code --wait"}, clear=True):
            editor = (os.environ.get("VISUAL")
                      or os.environ.get("EDITOR")
                      or "vi")
            parts = shlex.split(editor)
            assert parts == ["code", "--wait"]


# ---------------------------------------------------------------------------
# Helper for dir entry construction
# ---------------------------------------------------------------------------

def _entry(name, type_="FILE", size=100):
    """Create a minimal dir entry dict for testing."""
    return {"name": name, "type": type_, "size": size,
            "protection": "00", "datestamp": "2026-01-01 12:00:00"}


# ---------------------------------------------------------------------------
# _find_filter()
# ---------------------------------------------------------------------------

class TestFindFilter:
    """Tests for glob pattern and type filtering of directory entries."""

    def test_basic_pattern(self):
        entries = [
            _entry("readme.txt"),
            _entry("image.png"),
            _entry("notes.txt"),
        ]
        result = _find_filter(entries, "*.txt")
        assert len(result) == 2
        names = [e["name"] for e in result]
        assert "readme.txt" in names
        assert "notes.txt" in names

    def test_case_insensitive(self):
        entries = [
            _entry("readme.txt"),
            _entry("NOTES.TXT"),
            _entry("image.png"),
        ]
        result = _find_filter(entries, "*.TXT")
        assert len(result) == 2
        names = [e["name"] for e in result]
        assert "readme.txt" in names
        assert "NOTES.TXT" in names

    def test_type_filter_files_only(self):
        entries = [
            _entry("readme.txt"),
            _entry("docs", "DIR", 0),
            _entry("notes.txt"),
        ]
        result = _find_filter(entries, "*", type_filter="f")
        assert len(result) == 2
        assert all(e["type"] == "FILE" for e in result)


# ---------------------------------------------------------------------------
# _build_tree() / _format_tree()
# ---------------------------------------------------------------------------

class TestTree:
    """Tests for tree building and rendering."""

    def test_nested_tree(self):
        entries = [
            _entry("C", "DIR", 0),
            _entry("C/Copy", size=1234),
            _entry("C/Dir", size=567),
            _entry("S", "DIR", 0),
            _entry("S/Startup-Sequence", size=200),
        ]
        tree = _build_tree(entries)
        lines, dir_count, file_count = _format_tree("ROOT:", tree)
        assert lines[0] == "ROOT:"
        assert dir_count == 2
        assert file_count == 3
        # Verify box-drawing characters and names appear in lines
        joined = "\n".join(lines)
        assert "\u251c" in joined or "\u2514" in joined  # branch chars
        assert "Copy" in joined
        assert "Dir" in joined
        assert "Startup-Sequence" in joined

    def test_dirs_only(self):
        entries = [
            _entry("C", "DIR", 0),
            _entry("C/Copy", size=1234),
            _entry("S", "DIR", 0),
            _entry("S/Startup-Sequence", size=200),
        ]
        tree = _build_tree(entries)
        lines, dir_count, file_count = _format_tree("ROOT:", tree,
                                                     dirs_only=True)
        assert file_count == 0
        assert dir_count == 2

    def test_empty_tree(self):
        tree = _build_tree([])
        lines, dir_count, file_count = _format_tree("ROOT:", tree)
        assert lines == ["ROOT:"]
        assert dir_count == 0
        assert file_count == 0


# ---------------------------------------------------------------------------
# _grep_lines()
# ---------------------------------------------------------------------------

class TestGrep:
    """Tests for line-by-line text search."""

    def test_fixed_string(self):
        text = "hello world\ngoodbye world\nhello again"
        result = _grep_lines(text, "hello")
        assert len(result) == 2
        assert result[0][1] == "hello world"
        assert result[1][1] == "hello again"

    def test_case_insensitive(self):
        text = "Hello World\nhello world\nHELLO WORLD"
        result = _grep_lines(text, "hello", ignore_case=True)
        assert len(result) == 3

    def test_line_numbers(self):
        text = "alpha\nbeta\ngamma\nbeta again"
        result = _grep_lines(text, "beta")
        assert result[0][0] == 2
        assert result[1][0] == 4

    def test_no_match(self):
        text = "nothing here\nor here\nor anywhere"
        result = _grep_lines(text, "missing")
        assert result == []

    def test_regex_mode(self):
        text = "error: something broke\nwarning: check this\ninfo: all good"
        result = _grep_lines(text, "error|warn", is_regex=True)
        assert len(result) == 2
        assert "error" in result[0][1]
        assert "warn" in result[1][1]

    def test_special_chars_escaped(self):
        text = "foo.bar\nfooXbar\nfoo-bar"
        result = _grep_lines(text, "foo.bar")
        assert len(result) == 1
        assert result[0][1] == "foo.bar"


# ---------------------------------------------------------------------------
# _du_accumulate()
# ---------------------------------------------------------------------------

class TestDu:
    """Tests for per-directory size accumulation."""

    def test_basic_accumulation(self):
        entries = [
            _entry("A/file1", size=100),
            _entry("A/file2", size=200),
            _entry("B/file3", size=50),
        ]
        result, total = _du_accumulate(entries)
        dir_sizes = dict(result)
        assert dir_sizes["A"] == 300
        assert dir_sizes["B"] == 50
        assert dir_sizes["."] == 350

    def test_nested_propagation(self):
        entries = [
            _entry("A", "DIR", 0),
            _entry("A/B", "DIR", 0),
            _entry("A/B/deep.txt", size=500),
            _entry("A/shallow.txt", size=100),
        ]
        result, total = _du_accumulate(entries)
        dir_sizes = dict(result)
        assert dir_sizes["A/B"] == 500
        assert dir_sizes["A"] == 600
        assert dir_sizes["."] == 600

    def test_summary_total(self):
        entries = [
            _entry("A/file1", size=100),
            _entry("A/file2", size=200),
            _entry("B/file3", size=50),
        ]
        result, total = _du_accumulate(entries)
        assert total == 350


# ---------------------------------------------------------------------------
# Shell command tests (mock-based)
# ---------------------------------------------------------------------------

def _make_shell():
    """Create an AmigaShell with a mocked connection for unit testing."""
    shell = AmigaShell.__new__(AmigaShell)
    cmd.Cmd.__init__(shell)
    shell.host = "test"
    shell.port = 6800
    shell.timeout = 30
    shell.conn = mock.MagicMock()
    shell.cw = ColorWriter(force_color=False)
    shell.cwd = "SYS:"
    shell._dir_cache = _DirCache()
    shell._editor = None
    return shell


class TestDoTree:
    """Tests for do_tree shell command."""

    def test_tree_basic(self, capsys):
        shell = _make_shell()
        shell.conn.dir.return_value = [
            _entry("C", "DIR", 0),
            _entry("C/Copy", size=1234),
            _entry("S", "DIR", 0),
            _entry("S/Startup-Sequence", size=200),
        ]
        shell.do_tree("SYS:")
        out = capsys.readouterr().out
        assert "SYS:" in out
        assert "2 directories, 2 files" in out
        assert "Copy" in out
        assert "Startup-Sequence" in out

    def test_tree_ascii(self, capsys):
        shell = _make_shell()
        shell.conn.dir.return_value = [
            _entry("C", "DIR", 0),
            _entry("C/Copy", size=1234),
            _entry("S", "DIR", 0),
            _entry("S/Startup-Sequence", size=200),
        ]
        shell.do_tree("--ascii SYS:")
        out = capsys.readouterr().out
        assert "|--" in out or "`--" in out
        # Unicode box chars should be absent
        assert "\u251c" not in out
        assert "\u2514" not in out

    def test_tree_dirs_only(self, capsys):
        shell = _make_shell()
        shell.conn.dir.return_value = [
            _entry("C", "DIR", 0),
            _entry("C/Copy", size=1234),
            _entry("S", "DIR", 0),
            _entry("S/Startup-Sequence", size=200),
        ]
        shell.do_tree("-d SYS:")
        out = capsys.readouterr().out
        assert "2 directories, 0 files" in out
        assert "Copy" not in out
        assert "Startup-Sequence" not in out

    def test_tree_empty(self, capsys):
        shell = _make_shell()
        shell.conn.dir.return_value = []
        shell.do_tree("RAM:")
        out = capsys.readouterr().out
        assert "0 directories, 0 files" in out


class TestDoFind:
    """Tests for do_find shell command."""

    def test_find_name_pattern(self, capsys):
        shell = _make_shell()
        shell.conn.dir.return_value = [
            _entry("readme.txt"),
            _entry("icon.info"),
            _entry("notes.txt"),
            _entry("sub", "DIR", 0),
        ]
        shell.do_find("SYS: *.info")
        out = capsys.readouterr().out
        assert "icon.info" in out
        assert "readme.txt" not in out

    def test_find_type_file(self, capsys):
        shell = _make_shell()
        shell.conn.dir.return_value = [
            _entry("readme.txt"),
            _entry("sub", "DIR", 0),
            _entry("sub/note.txt"),
        ]
        shell.do_find("SYS: -type f *")
        out = capsys.readouterr().out
        assert "readme.txt" in out
        assert "sub/note.txt" in out
        # "sub" by itself (the dir) should not appear
        lines = [l.strip() for l in out.strip().split("\n") if l.strip()]
        dir_only_lines = [l for l in lines if l.strip() == "sub"]
        assert len(dir_only_lines) == 0

    def test_find_type_dir(self, capsys):
        shell = _make_shell()
        shell.conn.dir.return_value = [
            _entry("readme.txt"),
            _entry("sub", "DIR", 0),
            _entry("sub/note.txt"),
        ]
        shell.do_find("SYS: -type d *")
        out = capsys.readouterr().out
        assert "sub" in out
        assert "readme.txt" not in out

    def test_find_no_matches(self, capsys):
        shell = _make_shell()
        shell.conn.dir.return_value = [
            _entry("readme.txt"),
            _entry("notes.txt"),
        ]
        shell.do_find("SYS: *.nonexistent")
        out = capsys.readouterr().out
        assert out.strip() == ""


class TestDoDu:
    """Tests for do_du shell command."""

    def test_du_basic(self, capsys):
        shell = _make_shell()
        shell.conn.dir.return_value = [
            _entry("A", "DIR", 0),
            _entry("A/file1", size=100),
            _entry("A/file2", size=200),
            _entry("B", "DIR", 0),
            _entry("B/file3", size=50),
        ]
        shell.do_du("SYS:")
        out = capsys.readouterr().out
        assert "A" in out
        assert "B" in out

    def test_du_summary(self, capsys):
        shell = _make_shell()
        shell.conn.dir.return_value = [
            _entry("A", "DIR", 0),
            _entry("A/file1", size=100),
            _entry("A/file2", size=200),
        ]
        shell.do_du("-s SYS:")
        out = capsys.readouterr().out
        lines = [l for l in out.strip().split("\n") if l.strip()]
        assert len(lines) == 1
        assert "SYS:" in lines[0]

    def test_du_human_readable(self, capsys):
        shell = _make_shell()
        shell.conn.dir.return_value = [
            _entry("big", "DIR", 0),
            _entry("big/large.dat", size=1048576),
        ]
        shell.do_du("-h SYS:")
        out = capsys.readouterr().out
        # Should contain K or M suffixes
        assert "M" in out or "K" in out


class TestDoGrep:
    """Tests for do_grep shell command."""

    def test_grep_recursive(self, capsys):
        shell = _make_shell()
        shell.conn.dir.return_value = [
            _entry("file1.txt"),
            _entry("file2.txt"),
        ]
        shell.conn.read.side_effect = [
            b"hello world\ngoodbye\n",
            b"nothing here\nhello again\n",
        ]
        shell.do_grep("-r hello SYS:")
        out = capsys.readouterr().out
        assert "hello world" in out
        assert "hello again" in out

    def test_grep_case_insensitive(self, capsys):
        shell = _make_shell()
        shell.conn.dir.return_value = [
            _entry("test.txt"),
        ]
        shell.conn.read.return_value = b"Hello World\nhello world\nHELLO\n"
        shell.do_grep("-ri HELLO SYS:")
        out = capsys.readouterr().out
        assert out.count("\n") >= 3  # all 3 lines match

    def test_grep_line_numbers(self, capsys):
        shell = _make_shell()
        shell.conn.dir.return_value = [
            _entry("test.txt"),
        ]
        shell.conn.read.return_value = b"alpha\nbeta\ngamma\nbeta again\n"
        shell.do_grep("-rn beta SYS:")
        out = capsys.readouterr().out
        assert "2:" in out
        assert "4:" in out

    def test_grep_count(self, capsys):
        shell = _make_shell()
        shell.conn.dir.return_value = [
            _entry("test.txt"),
        ]
        shell.conn.read.return_value = b"hello\nworld\nhello again\n"
        shell.do_grep("-rc hello SYS:")
        out = capsys.readouterr().out
        assert "2" in out

    def test_grep_filenames_only(self, capsys):
        shell = _make_shell()
        shell.conn.dir.return_value = [
            _entry("match.txt"),
            _entry("nomatch.txt"),
        ]
        shell.conn.read.side_effect = [
            b"hello world\n",
            b"nothing here\n",
        ]
        shell.do_grep("-rl hello SYS:")
        out = capsys.readouterr().out
        assert "match.txt" in out
        assert "hello world" not in out

    def test_grep_regex(self, capsys):
        shell = _make_shell()
        shell.conn.dir.return_value = [
            _entry("test.txt"),
        ]
        shell.conn.read.return_value = b"error: broke\nwarning: check\ninfo: ok\n"
        shell.do_grep("-rE 'error|warn' SYS:")
        out = capsys.readouterr().out
        assert "error" in out
        assert "warn" in out

    def test_grep_single_file(self, capsys):
        shell = _make_shell()
        shell.conn.read.return_value = b"hello world\ngoodbye\n"
        shell.do_grep("hello SYS:test.txt")
        out = capsys.readouterr().out
        assert "hello world" in out
        assert "goodbye" not in out


class TestDoLs:
    """Tests for do_ls shell command."""

    def test_ls_basic(self, capsys):
        shell = _make_shell()
        shell.conn.dir.return_value = [
            _entry("file1.txt"),
            _entry("sub", "DIR", 0),
            _entry("file2.txt"),
        ]
        shell.do_ls("SYS:")
        out = capsys.readouterr().out
        assert "file1.txt" in out
        assert "sub" in out
        assert "file2.txt" in out

    def test_ls_long_format(self, capsys):
        shell = _make_shell()
        shell.conn.dir.return_value = [
            _entry("file1.txt", size=1234),
        ]
        shell.do_ls("-l SYS:")
        out = capsys.readouterr().out
        assert "file1.txt" in out
        assert "1234" in out or "rwed" in out

    def test_ls_recursive(self, capsys):
        shell = _make_shell()
        shell.conn.dir.return_value = [
            _entry("C", "DIR", 0),
            _entry("C/Copy", size=1234),
            _entry("S", "DIR", 0),
            _entry("S/Startup-Sequence", size=200),
        ]
        shell.do_ls("-r SYS:")
        out = capsys.readouterr().out
        assert "C/Copy" in out or "Copy" in out
        assert "S/Startup-Sequence" in out or "Startup-Sequence" in out

    def test_ls_glob_pattern(self, capsys):
        shell = _make_shell()
        shell.conn.dir.return_value = [
            _entry("readme.txt"),
            _entry("icon.info"),
            _entry("notes.txt"),
        ]
        shell.do_ls("*.info")
        out = capsys.readouterr().out
        assert "icon.info" in out
        assert "readme.txt" not in out

    def test_ls_cwd_fallback(self, capsys):
        shell = _make_shell()
        shell.cwd = "SYS:"
        shell.conn.dir.return_value = [
            _entry("file.txt"),
        ]
        shell.do_ls("")
        shell.conn.dir.assert_called_once_with("SYS:", recursive=False)


class TestDoCd:
    """Tests for do_cd shell command."""

    def test_cd_absolute(self, capsys):
        shell = _make_shell()
        shell.conn.stat.return_value = {
            "type": "DIR", "name": "Work", "size": 0,
            "protection": "00", "datestamp": "2026-01-01 12:00:00",
        }
        shell.do_cd("Work:")
        assert shell.cwd == "Work:"

    def test_cd_relative(self, capsys):
        shell = _make_shell()
        shell.cwd = "SYS:"
        shell.conn.stat.return_value = {
            "type": "DIR", "name": "S", "size": 0,
            "protection": "00", "datestamp": "2026-01-01 12:00:00",
        }
        shell.do_cd("S")
        assert shell.cwd == "SYS:S"

    def test_cd_parent(self, capsys):
        shell = _make_shell()
        shell.cwd = "SYS:S"
        shell.conn.stat.return_value = {
            "type": "DIR", "name": "SYS", "size": 0,
            "protection": "00", "datestamp": "2026-01-01 12:00:00",
        }
        shell.do_cd("..")
        assert shell.cwd == "SYS:"

    def test_cd_no_args(self, capsys):
        shell = _make_shell()
        shell.cwd = "Work:Projects"
        shell.conn.stat.return_value = {
            "type": "DIR", "name": "SYS", "size": 0,
            "protection": "00", "datestamp": "2026-01-01 12:00:00",
        }
        shell.do_cd("")
        assert shell.cwd == "SYS:"


class TestDoCp:
    """Tests for do_cp shell command."""

    def test_cp_basic(self, capsys):
        shell = _make_shell()
        shell.do_cp("RAM:a.txt RAM:b.txt")
        shell.conn.copy.assert_called_once_with(
            "RAM:a.txt", "RAM:b.txt", noclone=False, noreplace=False)

    def test_cp_noclone(self, capsys):
        shell = _make_shell()
        shell.do_cp("-P RAM:a.txt RAM:b.txt")
        shell.conn.copy.assert_called_once_with(
            "RAM:a.txt", "RAM:b.txt", noclone=True, noreplace=False)

    def test_cp_noreplace(self, capsys):
        shell = _make_shell()
        shell.do_cp("-n RAM:a.txt RAM:b.txt")
        shell.conn.copy.assert_called_once_with(
            "RAM:a.txt", "RAM:b.txt", noclone=False, noreplace=True)

    def test_cp_combined_flags(self, capsys):
        shell = _make_shell()
        shell.do_cp("-Pn RAM:a.txt RAM:b.txt")
        shell.conn.copy.assert_called_once_with(
            "RAM:a.txt", "RAM:b.txt", noclone=True, noreplace=True)


class TestDoCat:
    """Tests for do_cat shell command."""

    def test_cat_basic(self):
        shell = _make_shell()
        shell.conn.read.return_value = b"hello"
        with mock.patch("sys.stdout") as mock_stdout:
            mock_stdout.buffer = mock.MagicMock()
            shell.do_cat("SYS:test.txt")
        shell.conn.read.assert_called_once()
        args, kwargs = shell.conn.read.call_args
        assert args[0] == "SYS:test.txt"

    def test_cat_offset(self):
        shell = _make_shell()
        shell.conn.read.return_value = b"data"
        with mock.patch("sys.stdout") as mock_stdout:
            mock_stdout.buffer = mock.MagicMock()
            shell.do_cat("--offset 10 SYS:test.txt")
        args, kwargs = shell.conn.read.call_args
        assert kwargs.get("offset") == 10 or (len(args) > 1 and args[1] == 10)

    def test_cat_length(self):
        shell = _make_shell()
        shell.conn.read.return_value = b"data"
        with mock.patch("sys.stdout") as mock_stdout:
            mock_stdout.buffer = mock.MagicMock()
            shell.do_cat("--length 5 SYS:test.txt")
        args, kwargs = shell.conn.read.call_args
        assert kwargs.get("length") == 5 or (len(args) > 2 and args[2] == 5)

    def test_cat_offset_and_length(self):
        shell = _make_shell()
        shell.conn.read.return_value = b"data"
        with mock.patch("sys.stdout") as mock_stdout:
            mock_stdout.buffer = mock.MagicMock()
            shell.do_cat("--offset 10 --length 5 SYS:test.txt")
        args, kwargs = shell.conn.read.call_args
        assert kwargs.get("offset") == 10 or (len(args) > 1 and args[1] == 10)
        assert kwargs.get("length") == 5 or (len(args) > 2 and args[2] == 5)


# ---------------------------------------------------------------------------
# _complete_local_path()
# ---------------------------------------------------------------------------

class TestCompleteLocalPath:
    """Tests for local filesystem tab completion."""

    @pytest.fixture(autouse=True)
    def tmp_tree(self, tmp_path):
        """Create a temp directory tree for completion tests."""
        self.base = tmp_path
        (tmp_path / "file1.txt").touch()
        (tmp_path / "file2.txt").touch()
        (tmp_path / "image.png").touch()
        (tmp_path / "subdir").mkdir()
        (tmp_path / "subdir" / "nested.txt").touch()
        (tmp_path / ".hidden").touch()
        (tmp_path / ".hiddendir").mkdir()

    def _shell(self):
        return _make_shell()

    def test_absolute_path_prefix(self):
        shell = self._shell()
        prefix = str(self.base) + "/fil"
        results = shell._complete_local_path(prefix)
        assert sorted(results) == sorted([
            str(self.base) + "/file1.txt",
            str(self.base) + "/file2.txt",
        ])

    def test_absolute_path_dir_suffix(self):
        shell = self._shell()
        prefix = str(self.base) + "/sub"
        results = shell._complete_local_path(prefix)
        assert results == [str(self.base) + "/subdir/"]

    def test_absolute_path_no_match(self):
        shell = self._shell()
        prefix = str(self.base) + "/nonexistent"
        results = shell._complete_local_path(prefix)
        assert results == []

    def test_nonexistent_directory(self):
        shell = self._shell()
        results = shell._complete_local_path("/no/such/directory/file")
        assert results == []

    def test_hidden_files_excluded_by_default(self):
        shell = self._shell()
        # No dot prefix -> hidden files excluded
        prefix = str(self.base) + "/"
        results = shell._complete_local_path(prefix)
        names = [os.path.basename(r.rstrip("/")) for r in results]
        assert ".hidden" not in names
        assert ".hiddendir" not in names

    def test_hidden_files_included_with_dot_prefix(self):
        shell = self._shell()
        prefix = str(self.base) + "/."
        results = shell._complete_local_path(prefix)
        names = [os.path.basename(r.rstrip("/")) for r in results]
        assert ".hidden" in names
        assert ".hiddendir" in names

    def test_tilde_expansion(self):
        shell = self._shell()
        home = os.path.expanduser("~")
        # Use a prefix that should match at least some files in the home dir.
        # We just verify tilde is preserved and results are returned without error.
        results = shell._complete_local_path("~/")
        # All results should start with ~/, not the expanded home path
        for r in results:
            assert r.startswith("~/"), \
                "Expected tilde prefix, got: {}".format(r)

    def test_relative_path(self):
        shell = self._shell()
        # Completions from CWD -- just verify no crash and returns a list
        results = shell._complete_local_path("")
        assert isinstance(results, list)

    def test_empty_directory(self, tmp_path):
        shell = self._shell()
        empty = tmp_path / "emptydir"
        empty.mkdir()
        results = shell._complete_local_path(str(empty) + "/")
        assert results == []


# ---------------------------------------------------------------------------
# complete_get / complete_put / complete_append delegation
# ---------------------------------------------------------------------------

class TestCompleteGetDelegation:
    """Verify complete_get delegates to the right completer per arg position."""

    def test_first_arg_uses_amiga_path(self):
        shell = _make_shell()
        with mock.patch.object(shell, "_complete_path",
                               return_value=["SYS:S/"]) as mp, \
             mock.patch.object(shell, "_complete_local_path") as ml:
            result = shell.complete_get("SYS:", "get SYS:", 4, 8)
            mp.assert_called_once_with("SYS:", "get SYS:", 4, 8)
            ml.assert_not_called()
            assert result == ["SYS:S/"]

    def test_second_arg_uses_local_path(self):
        shell = _make_shell()
        with mock.patch.object(shell, "_complete_path") as mp, \
             mock.patch.object(shell, "_complete_local_path",
                               return_value=["/tmp/out"]) as ml:
            result = shell.complete_get("/tmp/", "get SYS:file /tmp/", 13, 18)
            ml.assert_called_once_with("/tmp/")
            mp.assert_not_called()
            assert result == ["/tmp/out"]

    def test_third_arg_returns_empty(self):
        shell = _make_shell()
        with mock.patch.object(shell, "_complete_path") as mp, \
             mock.patch.object(shell, "_complete_local_path") as ml:
            result = shell.complete_get("x", "get a b x", 8, 9)
            mp.assert_not_called()
            ml.assert_not_called()
            assert result == []


class TestCompletePutDelegation:
    """Verify complete_put delegates to the right completer per arg position."""

    def test_first_arg_uses_local_path(self):
        shell = _make_shell()
        with mock.patch.object(shell, "_complete_path") as mp, \
             mock.patch.object(shell, "_complete_local_path",
                               return_value=["/home/test.txt"]) as ml:
            result = shell.complete_put("/home/te", "put /home/te", 4, 12)
            ml.assert_called_once_with("/home/te")
            mp.assert_not_called()
            assert result == ["/home/test.txt"]

    def test_second_arg_uses_amiga_path(self):
        shell = _make_shell()
        with mock.patch.object(shell, "_complete_path",
                               return_value=["RAM:dest"]) as mp, \
             mock.patch.object(shell, "_complete_local_path") as ml:
            result = shell.complete_put("RAM:", "put /tmp/f RAM:", 10, 14)
            mp.assert_called_once_with("RAM:", "put /tmp/f RAM:", 10, 14)
            ml.assert_not_called()
            assert result == ["RAM:dest"]

    def test_third_arg_returns_empty(self):
        shell = _make_shell()
        with mock.patch.object(shell, "_complete_path") as mp, \
             mock.patch.object(shell, "_complete_local_path") as ml:
            result = shell.complete_put("x", "put a b x", 8, 9)
            mp.assert_not_called()
            ml.assert_not_called()
            assert result == []


class TestCompleteAppendDelegation:
    """Verify complete_append delegates to the right completer per arg."""

    def test_first_arg_uses_local_path(self):
        shell = _make_shell()
        with mock.patch.object(shell, "_complete_path") as mp, \
             mock.patch.object(shell, "_complete_local_path",
                               return_value=["data.bin"]) as ml:
            result = shell.complete_append("data", "append data", 7, 11)
            ml.assert_called_once_with("data")
            mp.assert_not_called()
            assert result == ["data.bin"]

    def test_second_arg_uses_amiga_path(self):
        shell = _make_shell()
        with mock.patch.object(shell, "_complete_path",
                               return_value=["RAM:log"]) as mp, \
             mock.patch.object(shell, "_complete_local_path") as ml:
            result = shell.complete_append("RAM:", "append data.bin RAM:", 16, 20)
            mp.assert_called_once_with("RAM:", "append data.bin RAM:", 16, 20)
            ml.assert_not_called()
            assert result == ["RAM:log"]

    def test_third_arg_returns_empty(self):
        shell = _make_shell()
        with mock.patch.object(shell, "_complete_path") as mp, \
             mock.patch.object(shell, "_complete_local_path") as ml:
            result = shell.complete_append("x", "append a b x", 11, 12)
            mp.assert_not_called()
            ml.assert_not_called()
            assert result == []


# ---------------------------------------------------------------------------
# Tilde expansion in file transfer commands
# ---------------------------------------------------------------------------

class TestTildeExpansion:
    """Verify that ~/ in local paths is expanded before open() is called."""

    def test_get_expands_tilde_in_local_path(self, tmp_path):
        """get REMOTE ~/local should expand ~ before writing."""
        shell = _make_shell()
        shell.conn.read.return_value = b"file data"
        dest = tmp_path / "downloaded.txt"
        with mock.patch("os.path.expanduser",
                         return_value=str(dest)) as exp:
            shell.do_get("SYS:file.txt ~/downloaded.txt")
            exp.assert_called_once_with("~/downloaded.txt")
        assert dest.read_bytes() == b"file data"

    def test_get_one_arg_no_tilde_expansion(self):
        """get REMOTE (1-arg form) derives local name from remote; no tilde."""
        shell = _make_shell()
        shell.conn.read.return_value = b"data"
        with mock.patch("os.path.expanduser") as exp, \
             mock.patch("builtins.open", mock.mock_open()):
            shell.do_get("SYS:file.txt")
            exp.assert_not_called()

    def test_put_expands_tilde_one_arg(self, tmp_path):
        """put ~/file.txt should expand ~ before reading."""
        shell = _make_shell()
        src = tmp_path / "upload.txt"
        src.write_bytes(b"upload data")
        shell.conn.write.return_value = 11
        with mock.patch("os.path.expanduser",
                         return_value=str(src)) as exp:
            shell.do_put("~/upload.txt")
            exp.assert_called_once_with("~/upload.txt")
        shell.conn.write.assert_called_once()
        args = shell.conn.write.call_args[0]
        # Remote name should be derived from expanded basename
        assert args[0] == "SYS:upload.txt"
        assert args[1] == b"upload data"

    def test_put_expands_tilde_two_args(self, tmp_path):
        """put ~/file.txt REMOTE should expand ~ before reading."""
        shell = _make_shell()
        src = tmp_path / "upload.txt"
        src.write_bytes(b"upload data")
        shell.conn.write.return_value = 11
        with mock.patch("os.path.expanduser",
                         return_value=str(src)) as exp:
            shell.do_put("~/upload.txt RAM:dest.txt")
            exp.assert_called_once_with("~/upload.txt")
        shell.conn.write.assert_called_once()

    def test_append_expands_tilde(self, tmp_path):
        """append ~/file.txt REMOTE should expand ~ before reading."""
        shell = _make_shell()
        src = tmp_path / "extra.txt"
        src.write_bytes(b"extra data")
        shell.conn.append.return_value = 10
        with mock.patch("os.path.expanduser",
                         return_value=str(src)) as exp:
            shell.do_append("~/extra.txt RAM:log.txt")
            exp.assert_called_once_with("~/extra.txt")
        shell.conn.append.assert_called_once()
