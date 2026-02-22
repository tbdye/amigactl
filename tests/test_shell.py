"""Unit tests for shell utility functions and ColorWriter.

These are pure unit tests that do not require a network connection to the
daemon.  They exercise the formatting and path manipulation helpers used
by the interactive shell.
"""

import os
from unittest import mock

import pytest

from amigactl.shell import (
    format_size,
    _amiga_basename,
    _join_amiga_path,
    _format_protection,
    _normalize_dotdot,
    _visible_len,
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
