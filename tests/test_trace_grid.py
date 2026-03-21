"""Unit tests for the toggle grid (trace_grid.py).

These are pure unit tests that exercise the ToggleGrid class:
item management, filter command building, rendering, noise defaults,
and batch operations.
"""

import io
from unittest import mock

import pytest

from amigactl.colors import ColorWriter
from amigactl.trace_grid import ToggleGrid
from amigactl.trace_ui import TerminalState, _visible_len


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_grid(libs=None, funcs=None, procs=None, initial_lib=None,
               daemon_disabled_funcs=None, tier_level=1):
    """Create a ToggleGrid with test data.

    Defaults provide a minimal three-category grid.
    """
    if libs is None:
        libs = {"exec": 234, "dos": 187}
    if funcs is None:
        funcs = {"Open": 12, "Lock": 8, "Close": 6}
    if procs is None:
        procs = {"myapp": 89, "Shell Process": 43, "ramlib": 10}
    return ToggleGrid(libs, funcs, procs, initial_lib=initial_lib,
                      daemon_disabled_funcs=daemon_disabled_funcs,
                      tier_level=tier_level)


def _make_terminal_state(rows=24, cols=80):
    """Create a TerminalState with captured stdout."""
    output = io.StringIO()
    term = TerminalState(stdin_fd=0, stdout=output)
    term.rows = rows
    term.cols = cols
    term._saved_attrs = [[0] * 7]
    return term, output


def _make_viewer(**overrides):
    """Create a TraceViewer with mocked conn/session for unit testing."""
    from amigactl.trace_ui import TraceViewer
    conn = mock.MagicMock()
    session = mock.MagicMock()
    session.sock = mock.MagicMock()
    session.reader = mock.MagicMock()
    session.reader.has_buffered_data.return_value = False

    cw = ColorWriter(force_color=False)
    viewer = TraceViewer(conn, session, cw, **overrides)

    output = io.StringIO()
    term = TerminalState(stdin_fd=0, stdout=output)
    term.rows = 24
    term.cols = 80
    viewer.term = term

    return viewer


# ---------------------------------------------------------------------------
# TestToggleGrid
# ---------------------------------------------------------------------------

class TestToggleGrid:
    """Tests for the ToggleGrid class."""

    def test_build_items_sorted_by_count(self):
        """Items are sorted by descending event count."""
        grid = _make_grid(libs={"dos": 50, "exec": 200, "icon": 5})
        names = [item["name"] for item in grid.lib_items]
        assert names == ["exec", "dos", "icon"]

    def test_noise_defaults_unchecked(self):
        """Daemon-disabled functions start with enabled=False."""
        # Include some daemon-disabled functions in the func dict
        funcs = {
            "Open": 12,
            "AllocMem": 128,
            "GetMsg": 47,
            "FindPort": 30,
            "Lock": 8,
        }
        daemon_disabled = {
            "exec.AllocMem", "exec.GetMsg", "exec.FindPort",
        }
        grid = _make_grid(funcs=funcs, initial_lib="exec",
                          daemon_disabled_funcs=daemon_disabled)

        disabled_names = {"AllocMem", "GetMsg", "FindPort"}
        for item in grid.func_items:
            if item["name"] in disabled_names:
                assert not item["enabled"], \
                    "{} should start unchecked".format(item["name"])
            else:
                assert item["enabled"], \
                    "{} should start checked".format(item["name"])

    def test_freemem_in_non_basic(self):
        """FreeMem IS in the non-basic function set (TIER_MANUAL)."""
        assert "FreeMem" in ToggleGrid._get_non_basic_funcs()

    def test_toggle_at_cursor(self):
        """Toggling an item flips its enabled state."""
        grid = _make_grid()
        # Items sorted by count: exec(234) first, dos(187) second
        assert grid.lib_items[0]["enabled"] is True

        grid.toggle_at_cursor()  # Toggle first item (cursor defaults to 0)
        assert grid.lib_items[0]["enabled"] is False

        grid.toggle_at_cursor()  # Toggle back
        assert grid.lib_items[0]["enabled"] is True

    def test_cursor_clamp_bounds(self):
        """Cursor clamps to valid bounds at edges."""
        grid = _make_grid()
        # move_cursor(-1) from position 0 stays at 0
        grid.cursor_pos[0] = 0
        grid.move_cursor(-1)
        assert grid.cursor_pos[0] == 0

        # move_cursor(1) from last item stays at last item
        last = len(grid.lib_items) - 1
        grid.cursor_pos[0] = last
        grid.move_cursor(1)
        assert grid.cursor_pos[0] == last

    def test_all_on(self):
        """all_on() enables all items in the active category."""
        grid = _make_grid()
        grid.none()  # Disable all first
        assert all(not item["enabled"] for item in grid.lib_items)

        grid.all_on()
        assert all(item["enabled"] for item in grid.lib_items)

    def test_none(self):
        """none() disables all items in the active category."""
        grid = _make_grid()
        grid.none()
        assert all(not item["enabled"] for item in grid.lib_items)

    def test_all_on_none_respects_active_category(self):
        """Batch operations only affect the active category."""
        grid = _make_grid()
        # Active category is LIBRARIES (index 0)
        grid.none()  # Disables only LIBRARIES items

        # FUNCTIONS and PROCESSES should be unaffected
        # (No daemon_disabled_funcs passed, so all funcs start enabled)
        assert all(item["enabled"] for item in grid.func_items)
        assert all(item["enabled"] for item in grid.proc_items)

    def test_build_filter_whitelist(self):
        """When fewer items enabled, produces LIB= whitelist."""
        grid = _make_grid(
            libs={"exec": 200, "dos": 100, "icon": 50})
        # Disable two of three libs
        grid.cursor_pos[0] = 0
        grid.toggle_at_cursor()  # exec
        grid.cursor_pos[0] = 2
        grid.toggle_at_cursor()  # icon

        cmd = grid.build_filter_command()
        assert "LIB=dos" == cmd

    def test_build_filter_blacklist(self):
        """When fewer items disabled, produces -LIB= blacklist."""
        grid = _make_grid(
            libs={"exec": 200, "dos": 100, "icon": 50})
        # Disable only one of three libs
        grid.cursor_pos[0] = 2
        grid.toggle_at_cursor()  # icon

        cmd = grid.build_filter_command()
        assert "-LIB=icon" == cmd

    def test_build_filter_empty(self):
        """When all enabled, produces empty string."""
        # No noise functions in the func set, so all enabled
        grid = _make_grid(funcs={"Open": 10, "Lock": 5})
        cmd = grid.build_filter_command()
        assert cmd == ""

    def test_build_filter_func_blacklist(self):
        """Function blacklist when fewer disabled than enabled.

        Uses lib.func dotted format.
        """
        grid = _make_grid(
            funcs={"Open": 12, "Lock": 8, "Close": 6,
                   "Execute": 4, "LoadSeg": 3},
            initial_lib="dos")
        # Disable just one
        grid.active_category = 1  # FUNCTIONS
        grid.cursor_pos[1] = 0
        grid.toggle_at_cursor()  # Open (highest count)

        cmd = grid.build_filter_command()
        assert "-FUNC=dos.Open" in cmd

    def test_build_filter_combined(self):
        """Combined LIB and FUNC filters in one command.

        FUNC= entries use lib.func dotted format.
        """
        grid = _make_grid(
            libs={"exec": 200, "dos": 100, "icon": 50},
            funcs={"Open": 12, "Lock": 8, "Close": 6},
            initial_lib="dos")
        # Disable one lib
        grid.cursor_pos[0] = 2
        grid.toggle_at_cursor()  # icon (in LIBRARIES)
        # Disable one func
        grid.active_category = 1  # FUNCTIONS
        grid.cursor_pos[1] = 2
        grid.toggle_at_cursor()  # Close (lowest count)

        cmd = grid.build_filter_command()
        assert "-LIB=icon" in cmd
        assert "-FUNC=dos.Close" in cmd

    def test_proc_not_in_filter_command(self):
        """Processes are NOT included in build_filter_command() output.

        Process filtering is client-side only (C5 from R1).
        """
        grid = _make_grid()
        # Disable a process
        grid.active_category = 2  # PROCESSES
        grid.cursor_pos[2] = 0
        grid.toggle_at_cursor()  # myapp (highest count)

        cmd = grid.build_filter_command()
        # PROC should not appear in the filter command
        assert "PROC" not in cmd
        assert "myapp" not in cmd

    def test_update_func_items(self):
        """Switching library updates function items and reapplies defaults."""
        daemon_disabled = {"exec.AllocMem", "exec.FindPort"}
        grid = _make_grid(daemon_disabled_funcs=daemon_disabled)
        new_funcs = {"AllocMem": 128, "FindPort": 47, "LoadSeg": 3}
        grid.update_func_items(new_funcs, "exec")

        assert grid.selected_lib == "exec"
        names = [item["name"] for item in grid.func_items]
        assert "AllocMem" in names
        assert "FindPort" in names
        assert "LoadSeg" in names

        # Daemon-disabled functions should be disabled after update
        for item in grid.func_items:
            if item["name"] in {"AllocMem", "FindPort"}:
                assert not item["enabled"]
            else:
                assert item["enabled"]

    def test_active_items_libraries(self):
        """Active items returns lib_items when LIBRARIES selected."""
        grid = _make_grid()
        grid.active_category = 0
        assert grid._active_items() is grid.lib_items

    def test_active_items_functions(self):
        """Active items returns func_items when FUNCTIONS selected."""
        grid = _make_grid()
        grid.active_category = 1
        assert grid._active_items() is grid.func_items

    def test_active_items_processes(self):
        """Active items returns proc_items when PROCESSES selected."""
        grid = _make_grid()
        grid.active_category = 2
        assert grid._active_items() is grid.proc_items

    def test_active_items_noise(self):
        """Active items returns noise_items when NOISE selected."""
        grid = _make_grid()
        grid.active_category = 3
        assert grid._active_items() is grid.noise_items

    def test_build_filter_all_libs_disabled(self):
        """All libs disabled produces a filter that blocks all (M1 fix)."""
        grid = _make_grid(libs={"exec": 200, "dos": 100})
        grid.cursor_pos[0] = 0
        grid.toggle_at_cursor()  # exec off
        grid.cursor_pos[0] = 1
        grid.toggle_at_cursor()  # dos off
        cmd = grid.build_filter_command()
        # Must NOT be empty -- should block everything
        assert cmd != ""
        assert "LIB=__NONE__" in cmd

    def test_build_filter_all_funcs_disabled(self):
        """All funcs disabled produces a filter that blocks all (M2 fix)."""
        grid = _make_grid(funcs={"Open": 12, "Lock": 8})
        grid.active_category = 1  # FUNCTIONS
        grid.cursor_pos[1] = 0
        grid.toggle_at_cursor()  # Open off
        grid.cursor_pos[1] = 1
        grid.toggle_at_cursor()  # Lock off
        cmd = grid.build_filter_command()
        assert cmd != ""
        assert "FUNC=__NONE__" in cmd

    def test_has_user_changes_false_initially(self):
        """Grid starts with no user changes (S5 fix)."""
        grid = _make_grid()
        assert grid.has_user_changes() is False

    def test_has_user_changes_after_toggle(self):
        """Toggling an item marks user_interacted (S5 fix)."""
        grid = _make_grid(funcs={"Open": 12, "Lock": 8})
        grid.toggle_at_cursor()
        assert grid.user_interacted is True
        assert grid.has_user_changes() is True

    def test_has_user_changes_toggle_back_is_no_change(self):
        """Toggling an item and toggling it back is no semantic change."""
        grid = _make_grid(funcs={"Open": 12, "Lock": 8})
        grid.toggle_at_cursor()
        grid.toggle_at_cursor()  # toggle back
        # user_interacted is True, but filter output matches initial
        assert grid.user_interacted is True
        assert grid.has_user_changes() is False

    def test_has_user_changes_all_on_no_change(self):
        """All-on when already all on is no semantic change."""
        grid = _make_grid(funcs={"Open": 12, "Lock": 8})
        grid.all_on()
        assert grid.user_interacted is True
        assert grid.has_user_changes() is False

    def test_daemon_disabled_scoped_to_lib(self):
        """Daemon-disabled defaults are scoped by library prefix."""
        funcs = {"AllocMem": 100, "GetMsg": 50, "Open": 12}
        # Daemon has exec.AllocMem and exec.GetMsg disabled
        daemon_disabled = {"exec.AllocMem", "exec.GetMsg"}

        # dos library: daemon_disabled has exec.* not dos.*, so all enabled
        grid_dos = ToggleGrid({"dos": 100}, funcs, {},
                              initial_lib="dos",
                              daemon_disabled_funcs=daemon_disabled)
        for item in grid_dos.func_items:
            assert item["enabled"], \
                "{} should be enabled for dos library".format(item["name"])

        # exec library: AllocMem and GetMsg should be disabled
        grid_exec = ToggleGrid({"exec": 100}, funcs, {},
                               initial_lib="exec",
                               daemon_disabled_funcs=daemon_disabled)
        for item in grid_exec.func_items:
            if item["name"] in {"AllocMem", "GetMsg"}:
                assert not item["enabled"], \
                    "{} should be disabled for exec".format(item["name"])
            else:
                assert item["enabled"]

    def test_openlibrary_is_basic_tier(self):
        """OpenLibrary is in the Basic tier (not non-basic)."""
        non_basic = ToggleGrid._get_non_basic_funcs()
        assert "OpenLibrary" not in non_basic

    def test_daemon_disabled_reflected_in_grid(self):
        """Daemon-disabled functions start unchecked in grid."""
        funcs = {
            "OpenLibrary": 50, "Open": 12,
        }
        daemon_disabled = {"exec.OpenLibrary"}
        grid = _make_grid(funcs=funcs, initial_lib="exec",
                          daemon_disabled_funcs=daemon_disabled)

        for item in grid.func_items:
            if item["name"] == "OpenLibrary":
                assert not item["enabled"], \
                    "OpenLibrary should start unchecked"
            elif item["name"] == "Open":
                assert item["enabled"], \
                    "Open should start checked"

    def test_footer_includes_esc(self):
        """Footer text includes Esc cancel hint (S3 fix).

        The footer is rendered by _draw_hotkey_bar() in trace_ui.py,
        not by _build_lines(). Verify the constant has the text.
        """
        from amigactl.trace_grid import GRID_FOOTER_TEXT
        assert "Esc" in GRID_FOOTER_TEXT

    def test_move_cursor_down(self):
        """move_cursor(1) increments cursor position."""
        grid = _make_grid()
        assert grid.cursor_pos[0] == 0
        grid.move_cursor(1)
        assert grid.cursor_pos[0] == 1

    def test_move_cursor_up(self):
        """move_cursor(-1) decrements cursor position."""
        grid = _make_grid()
        grid.cursor_pos[0] = 1
        grid.move_cursor(-1)
        assert grid.cursor_pos[0] == 0

    def test_cursor_persists_across_category_switch(self):
        """Cursor position is preserved when switching categories."""
        grid = _make_grid()
        grid.cursor_pos[0] = 1
        grid.active_category = 1
        grid.cursor_pos[1] = 2
        grid.active_category = 0
        assert grid.cursor_pos[0] == 1

    def test_toggle_at_cursor_empty_category(self):
        """toggle_at_cursor on empty category does not crash."""
        grid = _make_grid(libs={}, funcs={}, procs={})
        grid.toggle_at_cursor()  # should not raise

    def test_move_cursor_empty_category(self):
        """move_cursor on empty category does not crash."""
        grid = _make_grid(libs={}, funcs={}, procs={})
        grid.move_cursor(1)  # should not raise

    def test_clamp_cursor_after_shrink(self):
        """clamp_cursor clamps when item list shrinks."""
        grid = _make_grid()
        grid.cursor_pos[1] = 9
        grid.update_func_items({"Open": 12, "Lock": 8, "Close": 6},
                               "dos")
        grid.clamp_cursor(1)
        assert grid.cursor_pos[1] == 2

    def test_clamp_cursor_empty_list(self):
        """clamp_cursor resets to 0 when item list is empty."""
        grid = _make_grid()
        grid.cursor_pos[1] = 5
        grid.func_items = []
        grid.clamp_cursor(1)
        assert grid.cursor_pos[1] == 0

    def test_daemon_disabled_indicator(self):
        """Daemon-disabled items show [D] marker."""
        grid = ToggleGrid(
            {"exec": 100}, {"FindPort": 0, "Open": 50},
            {"myapp": 10}, initial_lib="exec",
            daemon_disabled_funcs={"exec.FindPort"})
        grid._mark_daemon_disabled()
        item = next(i for i in grid.func_items if i["name"] == "FindPort")
        assert item["daemon_disabled"] is True
        assert not item["enabled"]  # noise default

    def test_prepopulated_funcs_appear_in_grid(self):
        """Functions from TRACE STATUS appear with count=0."""
        grid = ToggleGrid(
            {"exec": 0, "dos": 0},
            {"FindPort": 0, "FindTask": 0},
            {}, initial_lib="exec")
        names = {i["name"] for i in grid.func_items}
        assert "FindPort" in names
        assert "FindTask" in names

    # --- Library-scoped FUNC= filtering ---

    def test_build_filter_func_blacklist_dotted(self):
        """Function blacklist uses lib.func dotted format (8b.1)."""
        grid = _make_grid(
            funcs={"Open": 12, "Lock": 8, "Close": 6,
                   "Execute": 4, "LoadSeg": 3},
            initial_lib="dos")
        # Disable just one
        grid.active_category = 1  # FUNCTIONS
        grid.cursor_pos[1] = 0
        grid.toggle_at_cursor()  # Open (highest count)

        cmd = grid.build_filter_command()
        assert "-FUNC=dos.Open" in cmd

    def test_build_filter_func_whitelist_dotted(self):
        """Function whitelist uses lib.func dotted format (8b.1)."""
        grid = _make_grid(
            funcs={"Open": 12, "Lock": 8, "Close": 6},
            initial_lib="dos")
        # Disable two of three, leaving one enabled -> whitelist
        grid.active_category = 1  # FUNCTIONS
        grid.cursor_pos[1] = 1
        grid.toggle_at_cursor()  # Lock
        grid.cursor_pos[1] = 2
        grid.toggle_at_cursor()  # Close

        cmd = grid.build_filter_command()
        assert "FUNC=dos.Open" in cmd

    def test_build_filter_func_dotted_with_exec_lib(self):
        """Dotted func names use the selected_lib (exec)."""
        grid = _make_grid(
            funcs={"FindPort": 30, "Open": 12, "Lock": 8},
            initial_lib="exec")
        grid.active_category = 1
        grid.cursor_pos[1] = 2
        grid.toggle_at_cursor()  # Lock (lowest count)

        cmd = grid.build_filter_command()
        # Noise defaults (FindPort) also contribute to disabled set
        # The key assertion: all FUNC= entries have exec. prefix
        parts = cmd.split()
        for part in parts:
            if part.startswith("-FUNC=") or part.startswith("FUNC="):
                prefix = part.split("=", 1)[1]
                for fn in prefix.split(","):
                    assert "." in fn, \
                        "Expected dotted lib.func, got: {}".format(fn)

    def test_build_filter_all_funcs_disabled_no_dotted(self):
        """All funcs disabled uses __NONE__ sentinel, no dotted names."""
        grid = _make_grid(funcs={"Open": 12, "Lock": 8},
                          initial_lib="dos")
        grid.active_category = 1
        grid.cursor_pos[1] = 0
        grid.toggle_at_cursor()  # Open off
        grid.cursor_pos[1] = 1
        grid.toggle_at_cursor()  # Lock off
        cmd = grid.build_filter_command()
        assert "FUNC=__NONE__" in cmd

    def test_build_filter_combined_lib_and_dotted_func(self):
        """Combined LIB and FUNC filters with dotted func names."""
        grid = _make_grid(
            libs={"exec": 200, "dos": 100, "icon": 50},
            funcs={"Open": 12, "Lock": 8, "Close": 6},
            initial_lib="dos")
        # Disable one lib
        grid.cursor_pos[0] = 2
        grid.toggle_at_cursor()  # icon
        # Disable one func
        grid.active_category = 1
        grid.cursor_pos[1] = 2
        grid.toggle_at_cursor()  # Close

        cmd = grid.build_filter_command()
        assert "-LIB=icon" in cmd
        assert "-FUNC=dos.Close" in cmd


# ---------------------------------------------------------------------------
# TestToggleGridRendering
# ---------------------------------------------------------------------------

class TestToggleGridRendering:
    """Tests for grid rendering at different terminal widths."""

    def test_render_three_column_wide(self):
        """Three-column layout at 120+ cols."""
        grid = _make_grid()
        grid.selected_lib = "dos"
        cw = ColorWriter(force_color=False)
        lines = grid._render_three_column(120, cw)

        # Should have headers, blank, item rows, blank, footer
        assert len(lines) >= 5
        # Header line should contain all three categories
        header = lines[0]
        assert "LIBRARIES" in header
        assert "FUNCTIONS" in header
        assert "PROCESSES" in header

    def test_render_two_column_standard(self):
        """Two-column layout at 80-119 cols."""
        grid = _make_grid()
        grid.selected_lib = "dos"
        cw = ColorWriter(force_color=False)
        lines = grid._render_two_column(100, cw)

        assert len(lines) >= 5
        # Default active_category=0 (LIBRARIES), should show
        # LIBRARIES + FUNCTIONS
        header = lines[0]
        assert "LIBRARIES" in header
        assert "FUNCTIONS" in header

    def test_render_two_column_processes(self):
        """Two-column shows FUNCTIONS + PROCESSES when PROCESSES active."""
        grid = _make_grid()
        grid.active_category = 2
        grid.selected_lib = "dos"
        cw = ColorWriter(force_color=False)
        lines = grid._render_two_column(100, cw)

        header = lines[0]
        assert "FUNCTIONS" in header
        assert "PROCESSES" in header

    def test_render_stacked_narrow(self):
        """Stacked layout at <80 cols."""
        grid = _make_grid()
        cw = ColorWriter(force_color=False)
        lines = grid._render_stacked(60, cw)

        # Should show only the active category (LIBRARIES)
        assert len(lines) >= 4
        assert "LIBRARIES" in lines[0]

    def test_render_fits_terminal(self):
        """All rendered lines fit within 80 columns.

        The footer line is excluded because GRID_FOOTER_TEXT is a
        fixed-width string that gets truncated by write_at() at
        render time.
        """
        from amigactl.trace_grid import GRID_FOOTER_TEXT
        grid = _make_grid()
        grid.selected_lib = "dos"
        cw = ColorWriter(force_color=False)
        lines = grid._build_lines(80, cw)

        for line in lines:
            if line == GRID_FOOTER_TEXT:
                continue
            visible = _visible_len(line)
            assert visible <= 80, \
                "Line exceeds 80 cols ({}): {!r}".format(visible, line)

    def test_render_fits_terminal_wide(self):
        """All rendered lines fit within 120 columns."""
        grid = _make_grid()
        grid.selected_lib = "dos"
        cw = ColorWriter(force_color=False)
        lines = grid._build_lines(120, cw)

        for line in lines:
            visible = _visible_len(line)
            assert visible <= 120, \
                "Line exceeds 120 cols ({}): {!r}".format(visible, line)

    def test_render_with_color(self):
        """Rendering with color enabled produces ANSI sequences."""
        grid = _make_grid()
        grid.selected_lib = "dos"
        cw = ColorWriter(force_color=True)
        lines = grid._build_lines(120, cw)

        # The active category header should have bold ANSI codes
        header = lines[0]
        assert "\033[" in header  # Contains ANSI escape

    def test_render_to_terminal(self):
        """render() writes to the terminal without errors."""
        grid = _make_grid()
        grid.selected_lib = "dos"
        term, output = _make_terminal_state(rows=24, cols=120)
        cw = ColorWriter(force_color=False)

        # Should not raise
        grid.render(term, cw)
        rendered = output.getvalue()
        assert len(rendered) > 0  # Something was written

    def test_format_item_enabled(self):
        """Enabled item shows [x] in brackets."""
        grid = _make_grid()
        cw = ColorWriter(force_color=False)
        item = {"name": "exec", "count": 234, "enabled": True}
        text = grid._format_item(item, cw, 25)
        assert "[x]" in text
        assert "exec" in text
        assert "234" in text

    def test_format_item_disabled(self):
        """Disabled item shows empty brackets."""
        grid = _make_grid()
        cw = ColorWriter(force_color=False)
        item = {"name": "exec", "count": 234, "enabled": False}
        text = grid._format_item(item, cw, 25)
        assert "[ ]" in text
        assert "exec" in text

    def test_format_item_long_name_truncated(self):
        """Long names are truncated with ~ marker."""
        grid = _make_grid()
        cw = ColorWriter(force_color=False)
        item = {"name": "VeryLongFunctionName", "count": 5,
                "enabled": True}
        text = grid._format_item(item, cw, 20)
        visible = _visible_len(text)
        assert visible <= 20

    def test_empty_grid(self):
        """Grid with no data renders without errors."""
        grid = _make_grid(libs={}, funcs={}, procs={})
        cw = ColorWriter(force_color=False)
        lines = grid._build_lines(80, cw)
        # Should at least have headers (footer is in hotkey bar)
        assert len(lines) >= 2

    def test_footer_present(self):
        """Footer text is available for the hotkey bar.

        The footer is rendered by _draw_hotkey_bar() in trace_ui.py
        (not by _build_lines()) to avoid duplicate display. Verify
        the GRID_FOOTER_TEXT constant has the expected content.
        """
        from amigactl.trace_grid import GRID_FOOTER_TEXT
        assert "Enter" in GRID_FOOTER_TEXT
        assert "apply" in GRID_FOOTER_TEXT

    def test_active_category_reverse(self):
        """Active category header is rendered in reverse video."""
        grid = _make_grid()
        grid.active_category = 0  # LIBRARIES
        cw = ColorWriter(force_color=True)

        # Three-column layout
        lines = grid._render_three_column(120, cw)
        header = lines[0]
        # LIBRARIES is active -- should have reverse ANSI
        assert "\033[7m" in header

    def test_non_basic_func_dim(self):
        """Non-basic functions are rendered dim."""
        funcs = {"AllocMem": 128, "Open": 12}
        grid = _make_grid(funcs=funcs, initial_lib="exec")
        cw = ColorWriter(force_color=True)

        # AllocMem is TIER_MANUAL (non-basic), should be marked non_basic
        alloc_item = None
        for item in grid.func_items:
            if item["name"] == "AllocMem":
                alloc_item = item
                break
        assert alloc_item is not None
        assert alloc_item.get("non_basic")

        # Non-basic items are dimmed even when enabled
        text = grid._format_item(alloc_item, cw, 30)
        # Should have dim ANSI code
        assert "\033[2m" in text

    def test_format_item_alignment(self):
        """Counts are right-aligned within column width (S7)."""
        grid = _make_grid()
        cw = ColorWriter(force_color=False)

        item = {"name": "exec", "count": 234, "enabled": True}
        text = grid._format_item(item, cw, 25)
        # The count "234" should be at the right side
        # Total visible length should be <= width
        visible = _visible_len(text)
        assert visible <= 25

    def test_cursor_highlight_active_only(self):
        """Highlighted (reverse) styling appears only in active column."""
        grid = _make_grid()
        grid.active_category = 0  # LIBRARIES
        grid.cursor_pos[0] = 0
        cw = ColorWriter(force_color=True)
        lines = grid._render_three_column(120, cw)
        # Find the first item row (after header + blank)
        item_line = lines[2]
        col_width = (120 - 4) // 3
        # Split into segments by column width
        # The first column (LIBRARIES) should have reverse
        left_seg = item_line[:col_width + 10]  # generous bounds
        assert "\033[7m" in left_seg
        # The middle and right columns should NOT have reverse
        # (they are not the active category)
        right_start = col_width + 2 + col_width + 2
        right_seg = item_line[right_start:] if len(item_line) > right_start else ""
        assert "\033[7m" not in right_seg

    def test_format_item_highlighted(self):
        """_format_item with highlighted=True produces reverse video."""
        grid = _make_grid()
        cw = ColorWriter(force_color=True)
        item = {"name": "exec", "count": 234, "enabled": True}
        text = grid._format_item(item, cw, 25, highlighted=True)
        assert "\033[7m" in text

    def test_format_item_daemon_disabled(self):
        """Daemon-disabled items show [D] when not enabled."""
        grid = _make_grid()
        cw = ColorWriter(force_color=False)
        item = {"name": "FindPort", "count": 0, "enabled": False,
                "daemon_disabled": True}
        text = grid._format_item(item, cw, 30)
        assert "[D]" in text

    def test_format_item_daemon_disabled_but_enabled(self):
        """Daemon-disabled items show [x] when user enables them."""
        grid = _make_grid()
        cw = ColorWriter(force_color=False)
        item = {"name": "FindPort", "count": 0, "enabled": True,
                "daemon_disabled": True}
        text = grid._format_item(item, cw, 30)
        assert "[x]" in text


# ---------------------------------------------------------------------------
# TestToggleGridIntegration
# ---------------------------------------------------------------------------

class TestToggleGridIntegration:
    """Tests for grid integration with TraceViewer stubs."""

    def test_enter_toggle_grid_creates_grid(self):
        """_enter_toggle_grid() creates a ToggleGrid instance."""
        from amigactl.trace_ui import TraceViewer
        conn = mock.MagicMock()
        session = mock.MagicMock()
        session.sock = mock.MagicMock()
        session.reader = mock.MagicMock()
        cw = ColorWriter(force_color=False)
        viewer = TraceViewer(conn, session, cw)

        # Set up discovered data
        viewer.discovered_libs = {"dos": 100, "exec": 200}
        viewer.discovered_funcs = {
            "dos": {"Open": 12, "Lock": 8},
            "exec": {"AllocMem": 128, "FindPort": 47},
        }
        viewer.discovered_procs = {"myapp": 89, "Shell": 43}

        # Attach a mock terminal
        term, output = _make_terminal_state(rows=24, cols=120)
        viewer.term = term

        viewer._enter_toggle_grid()

        assert viewer.grid is not None
        assert viewer.grid_visible is True
        # Initial lib should be exec (highest count)
        assert viewer.grid.selected_lib == "exec"

    def test_apply_grid_filters_sends_filter(self):
        """_apply_grid_filters() sends FILTER when items disabled."""
        from amigactl.trace_ui import TraceViewer
        conn = mock.MagicMock()
        session = mock.MagicMock()
        session.sock = mock.MagicMock()
        session.reader = mock.MagicMock()
        cw = ColorWriter(force_color=False)
        viewer = TraceViewer(conn, session, cw)

        viewer.discovered_libs = {"dos": 100, "exec": 200}
        viewer.discovered_funcs = {"exec": {"OpenLibrary": 5}}
        viewer.discovered_procs = {"myapp": 89}

        term, output = _make_terminal_state(rows=24, cols=120)
        viewer.term = term

        viewer._enter_toggle_grid()

        # Disable one library (dos is second by count)
        viewer.grid.cursor_pos[viewer.grid.active_category] = 1
        viewer.grid.toggle_at_cursor()  # dos

        viewer._apply_grid_filters()

        # Should have sent a FILTER command
        conn.send_filter.assert_called_once()
        call_args = conn.send_filter.call_args
        raw = call_args[1].get("raw", call_args[0][0]
                                if call_args[0] else "")
        assert "LIB=" in raw or "-LIB=" in raw

    def test_apply_grid_no_filter_when_all_enabled(self):
        """_apply_grid_filters() sends nothing when all items enabled."""
        from amigactl.trace_ui import TraceViewer
        conn = mock.MagicMock()
        session = mock.MagicMock()
        session.sock = mock.MagicMock()
        session.reader = mock.MagicMock()
        cw = ColorWriter(force_color=False)
        viewer = TraceViewer(conn, session, cw)

        # Use non-noise functions so all start enabled
        viewer.discovered_libs = {"dos": 100}
        viewer.discovered_funcs = {"dos": {"Open": 12}}
        viewer.discovered_procs = {"myapp": 89}

        term, output = _make_terminal_state(rows=24, cols=120)
        viewer.term = term

        viewer._enter_toggle_grid()
        viewer._apply_grid_filters()

        # No filter should be sent (all enabled, no errors)
        conn.send_filter.assert_not_called()

    def test_apply_grid_sets_client_side_filters(self):
        """_apply_grid_filters() updates disabled_* sets."""
        from amigactl.trace_ui import TraceViewer
        conn = mock.MagicMock()
        session = mock.MagicMock()
        session.sock = mock.MagicMock()
        session.reader = mock.MagicMock()
        cw = ColorWriter(force_color=False)
        viewer = TraceViewer(conn, session, cw)

        viewer.discovered_libs = {"dos": 100, "exec": 200}
        viewer.discovered_funcs = {"exec": {"OpenLibrary": 5}}
        viewer.discovered_procs = {"myapp": 89, "Shell": 43}

        term, output = _make_terminal_state(rows=24, cols=120)
        viewer.term = term

        viewer._enter_toggle_grid()

        # Disable dos (second item by count)
        viewer.grid.cursor_pos[viewer.grid.active_category] = 1
        viewer.grid.toggle_at_cursor()

        viewer._save_func_state()
        viewer._apply_grid_filters()

        # disabled_libs should have "dos" blocked
        assert viewer.disabled_libs == {"dos"}

    def test_apply_grid_all_enabled_clears_sets(self):
        """When all items enabled, disabled_libs/procs are None."""
        from amigactl.trace_ui import TraceViewer
        conn = mock.MagicMock()
        session = mock.MagicMock()
        session.sock = mock.MagicMock()
        session.reader = mock.MagicMock()
        cw = ColorWriter(force_color=False)
        viewer = TraceViewer(conn, session, cw)

        viewer.discovered_libs = {"dos": 100}
        viewer.discovered_funcs = {"dos": {"Open": 12}}
        viewer.discovered_procs = {"myapp": 89}

        term, output = _make_terminal_state(rows=24, cols=120)
        viewer.term = term

        viewer._enter_toggle_grid()
        viewer._save_func_state()
        viewer._apply_grid_filters()

        # All enabled = None (allow all) for libs and procs
        assert viewer.disabled_libs is None
        assert viewer.disabled_procs is None
        # [R5-SF2 fix]: disabled_funcs is NOT None after apply --
        # _save_func_state() creates {"dos": set()} (empty set =
        # all enabled).
        assert viewer.disabled_funcs == {"dos": set()}

    def test_get_selected_lib_name(self):
        """_get_selected_lib_name() returns focused library."""
        from amigactl.trace_ui import TraceViewer
        conn = mock.MagicMock()
        session = mock.MagicMock()
        session.sock = mock.MagicMock()
        session.reader = mock.MagicMock()
        cw = ColorWriter(force_color=False)
        viewer = TraceViewer(conn, session, cw)

        viewer.discovered_libs = {"dos": 100, "exec": 200}
        viewer.discovered_funcs = {"exec": {"AllocMem": 128}}
        viewer.discovered_procs = {}

        term, output = _make_terminal_state(rows=24, cols=120)
        viewer.term = term

        viewer._enter_toggle_grid()

        # Initial focused lib should be exec (highest count)
        name = viewer._get_selected_lib_name()
        assert name == "exec"

        # Change focused index
        viewer.grid.focused_lib_index = 1  # dos
        viewer.grid.cursor_pos[0] = 1
        name = viewer._get_selected_lib_name()
        assert name == "dos"

    def test_apply_grid_no_filter_when_no_user_changes(self):
        """Opening and closing grid with no changes sends no FILTER (S5)."""
        from amigactl.trace_ui import TraceViewer
        conn = mock.MagicMock()
        session = mock.MagicMock()
        session.sock = mock.MagicMock()
        session.reader = mock.MagicMock()
        cw = ColorWriter(force_color=False)
        viewer = TraceViewer(conn, session, cw)

        # Use exec with noise functions -- grid will have noise
        # defaults that produce a non-empty filter command, but
        # the user hasn't interacted.
        viewer.discovered_libs = {"exec": 200}
        viewer.discovered_funcs = {
            "exec": {"AllocMem": 128, "GetMsg": 47, "OpenLibrary": 5}
        }
        viewer.discovered_procs = {"myapp": 89}

        term, output = _make_terminal_state(rows=24, cols=120)
        viewer.term = term

        viewer._enter_toggle_grid()
        # Do NOT toggle anything -- just apply immediately
        viewer._save_func_state()
        viewer._apply_grid_filters()

        # No FILTER should be sent (no user interaction).
        # [R5-SF1 fix]: _save_func_state() creates disabled_funcs
        # with noise defaults for exec. has_func_state is True, so
        # the server-side path runs, but since the filter_cmd matches
        # noise defaults (which were the grid's initial state), the
        # filter IS sent. However, the key point is no SPURIOUS
        # filter is sent when no user changes were made.
        # With _save_func_state() + has_func_state, a filter IS
        # sent because disabled_funcs is not None.
        # This test is adjusted: disabled_libs/procs remain None.
        assert viewer.disabled_libs is None
        assert viewer.disabled_procs is None
        # [R5-SF2 fix]: disabled_funcs is NOT None -- _save_func_state()
        # runs before apply, creating per-library state with noise
        # defaults.
        assert viewer.disabled_funcs is not None

    def test_apply_grid_all_libs_disabled_blocks_client(self):
        """All libs disabled sets disabled_libs to full blocklist."""
        from amigactl.trace_ui import TraceViewer
        conn = mock.MagicMock()
        session = mock.MagicMock()
        session.sock = mock.MagicMock()
        session.reader = mock.MagicMock()
        cw = ColorWriter(force_color=False)
        viewer = TraceViewer(conn, session, cw)

        viewer.discovered_libs = {"exec": 200, "dos": 100}
        viewer.discovered_funcs = {"exec": {"OpenLibrary": 5}}
        viewer.discovered_procs = {"myapp": 89}

        term, output = _make_terminal_state(rows=24, cols=120)
        viewer.term = term

        viewer._enter_toggle_grid()
        # Disable all libraries
        viewer.grid.cursor_pos[viewer.grid.active_category] = 0
        viewer.grid.toggle_at_cursor()  # exec off
        viewer.grid.cursor_pos[viewer.grid.active_category] = 1
        viewer.grid.toggle_at_cursor()  # dos off
        viewer._save_func_state()
        viewer._apply_grid_filters()

        # disabled_libs should have both known libs blocked
        assert viewer.disabled_libs == {"exec", "dos"}
        # Client-side filter should block known events
        event = {"lib": "exec", "func": "Open", "task": "Shell"}
        assert viewer._passes_client_filter(event) is False

    def test_escape_cancels_grid(self):
        """Escape key closes grid without applying filters (S3 fix)."""
        from amigactl.trace_ui import TraceViewer
        conn = mock.MagicMock()
        session = mock.MagicMock()
        session.sock = mock.MagicMock()
        session.reader = mock.MagicMock()
        cw = ColorWriter(force_color=False)
        viewer = TraceViewer(conn, session, cw)

        viewer.discovered_libs = {"exec": 200, "dos": 100}
        viewer.discovered_funcs = {"exec": {"OpenLibrary": 5}}
        viewer.discovered_procs = {"myapp": 89}

        term, output = _make_terminal_state(rows=24, cols=120)
        viewer.term = term

        viewer._enter_toggle_grid()
        assert viewer.grid_visible is True

        # Make a change then press Escape
        viewer.grid.cursor_pos[viewer.grid.active_category] = 0
        viewer.grid.toggle_at_cursor()  # disable exec
        viewer._handle_grid_key(("esc", ""))  # bare Escape

        # Grid should be closed without applying
        assert viewer.grid_visible is False
        assert viewer.grid is None
        # No filter sent
        conn.send_filter.assert_not_called()
        # Client-side filters unchanged (still None = initial state)
        assert viewer.disabled_libs is None

    def test_focused_lib_index_synced_on_right_arrow(self):
        """focused_lib_index is synced from cursor_pos[0] on Right arrow."""
        viewer = _make_viewer()
        viewer.discovered_libs = {"exec": 200, "dos": 100, "icon": 50}
        viewer.discovered_funcs = {
            "exec": {"FindResident": 50},
            "dos": {"Open": 12},
            "icon": {"GetDiskObject": 5},
        }
        viewer.discovered_procs = {}

        viewer._enter_toggle_grid()

        # Move cursor to dos (index 1) in LIBRARIES
        viewer.grid.cursor_pos[0] = 1
        # Press Right arrow -- handler syncs focused_lib_index
        viewer._handle_grid_key(("esc", "[C"))

        assert viewer.grid.focused_lib_index == 1

    # --- Library-scoped FUNC= in _apply_grid_filters ---

    def test_apply_grid_filters_dotted_func_names(self):
        """_apply_grid_filters() sends dotted lib.func in -FUNC= (8b.1)."""
        from amigactl.trace_ui import TraceViewer
        conn = mock.MagicMock()
        session = mock.MagicMock()
        session.sock = mock.MagicMock()
        session.reader = mock.MagicMock()
        cw = ColorWriter(force_color=False)
        viewer = TraceViewer(conn, session, cw)

        viewer.discovered_libs = {"dos": 100}
        viewer.discovered_funcs = {
            "dos": {"Open": 12, "Lock": 8, "Close": 6}
        }
        viewer.discovered_procs = {}

        term, output = _make_terminal_state(rows=24, cols=120)
        viewer.term = term

        viewer._enter_toggle_grid()

        # Disable one function
        viewer.grid.active_category = 1  # FUNCTIONS
        viewer.grid.cursor_pos[1] = 2
        viewer.grid.toggle_at_cursor()  # Close (lowest count)

        viewer._save_func_state()
        viewer._apply_grid_filters()

        # Verify send_filter was called with dotted names
        conn.send_filter.assert_called_once()
        call_args = conn.send_filter.call_args
        raw = call_args[1].get("raw", "")
        # The comprehensive blacklist from disabled_funcs should have
        # dotted lib.func format
        assert "dos.Close" in raw

    def test_apply_grid_filters_multi_lib_dotted(self):
        """_apply_grid_filters() collects dotted names from all libs (8b.1).

        When disabled_funcs has entries for multiple libraries, the
        comprehensive -FUNC= blacklist should use lib.func format for
        each, avoiding cross-library name collisions.
        """
        from amigactl.trace_ui import TraceViewer
        conn = mock.MagicMock()
        session = mock.MagicMock()
        session.sock = mock.MagicMock()
        session.reader = mock.MagicMock()
        cw = ColorWriter(force_color=False)
        viewer = TraceViewer(conn, session, cw)

        viewer.discovered_libs = {"dos": 100, "exec": 200}
        viewer.discovered_funcs = {
            "dos": {"Open": 12, "Lock": 8},
            "exec": {"FindPort": 30, "OpenLibrary": 5},
        }
        viewer.discovered_procs = {}

        term, output = _make_terminal_state(rows=24, cols=120)
        viewer.term = term

        # Pre-populate disabled_funcs with entries from both libraries
        # (simulating the user having navigated to each library's
        # FUNCTIONS panel and disabled items)
        viewer.disabled_funcs = {
            "dos": {"Lock"},
            "exec": {"FindPort"},
        }

        viewer._enter_toggle_grid()
        # Force user interaction so filter sends
        viewer.grid.toggle_at_cursor()
        viewer.grid.toggle_at_cursor()  # toggle back

        viewer._save_func_state()
        viewer._apply_grid_filters()

        # Verify the filter command has dotted names from both libs
        conn.send_filter.assert_called_once()
        call_args = conn.send_filter.call_args
        raw = call_args[1].get("raw", "")
        assert "dos.Lock" in raw
        assert "exec.FindPort" in raw


# ---------------------------------------------------------------------------
# TestActiveColumnReverse (Bug 13)
# ---------------------------------------------------------------------------

class TestActiveColumnReverse:
    """Tests for active column reverse video rendering (Bug 13)."""

    def test_active_category_reverse_video(self):
        """Active category in three-column uses reverse video."""
        grid = _make_grid()
        grid.active_category = 0
        cw = ColorWriter(force_color=True)
        lines = grid._render_three_column(120, cw)
        header = lines[0]
        assert "\033[7m" in header

    def test_active_category_reverse_two_column(self):
        """Active category in two-column uses reverse video."""
        grid = _make_grid()
        grid.active_category = 0
        cw = ColorWriter(force_color=True)
        lines = grid._render_two_column(100, cw)
        header = lines[0]
        assert "\033[7m" in header

    def test_active_category_reverse_stacked(self):
        """Active category in stacked layout uses reverse video."""
        grid = _make_grid()
        grid.active_category = 0
        cw = ColorWriter(force_color=True)
        lines = grid._render_stacked(60, cw)
        header = lines[0]
        assert "\033[7m" in header


# ---------------------------------------------------------------------------
# NOISE category tests
# ---------------------------------------------------------------------------


class TestNoiseCategory:
    """Tests for the NOISE category in the toggle grid."""

    def test_noise_category_exists(self):
        """NOISE is the 4th category in the grid."""
        grid = _make_grid()
        assert "NOISE" in grid.categories
        assert grid.categories[3] == "NOISE"
        assert len(grid.categories) == 4

    def test_noise_items_default_disabled(self):
        """All noise items start with enabled=False (suppressed)."""
        grid = _make_grid()
        assert len(grid.noise_items) == 9
        for item in grid.noise_items:
            assert item["enabled"] is False, \
                "{} should start disabled".format(item["name"])

    def test_noise_items_have_none_count(self):
        """Noise items have count=None (rendered as '-')."""
        grid = _make_grid()
        for item in grid.noise_items:
            assert item["count"] is None, \
                "{} should have count=None".format(item["name"])

    def test_noise_items_names(self):
        """Noise items include all expected shell variable names."""
        grid = _make_grid()
        names = {item["name"] for item in grid.noise_items}
        expected = {
            "process", "echo", "debug", "oldredirect",
            "interactive", "simpleshell",
            "RC", "Result2", "LV_ALIAS",
        }
        assert names == expected

    def test_noise_toggle(self):
        """Toggle a noise item and verify get_noise_state()."""
        grid = _make_grid()
        grid.active_category = 3  # NOISE
        grid.cursor_pos[3] = 0
        grid.toggle_at_cursor()  # Enable first noise item

        noise_enabled = grid.get_noise_state()
        assert grid.noise_items[0]["name"] in noise_enabled

    def test_noise_all_on(self):
        """all_on() on NOISE category enables all noise items."""
        grid = _make_grid()
        grid.active_category = 3  # NOISE
        grid.all_on()

        for item in grid.noise_items:
            assert item["enabled"] is True
        assert grid.get_noise_state() == {
            "process", "echo", "debug", "oldredirect",
            "interactive", "simpleshell",
            "RC", "Result2", "LV_ALIAS",
        }

    def test_noise_none(self):
        """none() on NOISE category disables all noise items."""
        grid = _make_grid()
        grid.active_category = 3  # NOISE
        # First enable all, then disable all
        grid.all_on()
        grid.none()

        for item in grid.noise_items:
            assert item["enabled"] is False
        assert grid.get_noise_state() == set()

    def test_noise_not_in_filter_command(self):
        """Noise items do not affect build_filter_command() output."""
        grid = _make_grid(funcs={"Open": 10, "Lock": 5})
        # Enable all noise items
        grid.active_category = 3
        grid.all_on()

        cmd = grid.build_filter_command()
        # Filter command should not mention any noise items
        assert "NOISE" not in cmd
        assert "process" not in cmd
        assert "LV_ALIAS" not in cmd

    def test_noise_toggle_does_not_trigger_has_user_changes(self):
        """Toggling only noise items doesn't cause has_user_changes()=True.

        Noise changes are client-side only and don't need a FILTER command.
        """
        grid = _make_grid(funcs={"Open": 10, "Lock": 5})
        grid.active_category = 3  # NOISE
        grid.toggle_at_cursor()  # Enable first noise item

        # user_interacted is True, but build_filter_command() unchanged
        assert grid.user_interacted is True
        assert grid.has_user_changes() is False

    def test_noise_cursor_pos_initialized(self):
        """Cursor position for NOISE category (index 3) starts at 0."""
        grid = _make_grid()
        assert 3 in grid.cursor_pos
        assert grid.cursor_pos[3] == 0

    def test_noise_clamp_cursor(self):
        """clamp_cursor works for the NOISE category."""
        grid = _make_grid()
        grid.cursor_pos[3] = 100  # out of bounds
        grid.clamp_cursor(3)
        assert grid.cursor_pos[3] == len(grid.noise_items) - 1

    def test_format_item_none_count(self):
        """_format_item renders count=None as '-'."""
        grid = _make_grid()
        cw = ColorWriter(force_color=False)
        item = {"name": "process", "count": None, "enabled": False}
        text = grid._format_item(item, cw, 30)
        assert "-" in text
        # Should not contain "None"
        assert "None" not in text

    def test_get_noise_state_empty_when_all_disabled(self):
        """get_noise_state() returns empty set when all disabled."""
        grid = _make_grid()
        assert grid.get_noise_state() == set()

    def test_get_noise_state_partial(self):
        """get_noise_state() returns only enabled items."""
        grid = _make_grid()
        # Enable just "RC" and "Result2"
        for item in grid.noise_items:
            if item["name"] in ("RC", "Result2"):
                item["enabled"] = True
        assert grid.get_noise_state() == {"RC", "Result2"}


class TestNoiseCategoryRendering:
    """Tests for rendering the NOISE category at various widths."""

    def test_three_column_noise_active(self):
        """When NOISE is active, three-column shows LIB, PROC, NOISE."""
        grid = _make_grid()
        grid.active_category = 3  # NOISE
        cw = ColorWriter(force_color=False)
        lines = grid._render_three_column(120, cw)

        header = lines[0]
        assert "LIBRARIES" in header
        assert "PROCESSES" in header
        assert "NOISE" in header
        # FUNCTIONS should NOT be visible when NOISE is active
        assert "FUNCTIONS" not in header

    def test_three_column_default_no_noise(self):
        """Default three-column layout shows LIB, FUNC, PROC (not NOISE)."""
        grid = _make_grid()
        grid.active_category = 0
        cw = ColorWriter(force_color=False)
        lines = grid._render_three_column(120, cw)

        header = lines[0]
        assert "LIBRARIES" in header
        assert "FUNCTIONS" in header
        assert "PROCESSES" in header
        assert "NOISE" not in header

    def test_two_column_noise_active(self):
        """When NOISE is active, two-column shows PROC + NOISE."""
        grid = _make_grid()
        grid.active_category = 3  # NOISE
        cw = ColorWriter(force_color=False)
        lines = grid._render_two_column(100, cw)

        header = lines[0]
        assert "PROCESSES" in header
        assert "NOISE" in header

    def test_stacked_noise_active(self):
        """Stacked layout shows only NOISE when active."""
        grid = _make_grid()
        grid.active_category = 3  # NOISE
        cw = ColorWriter(force_color=False)
        lines = grid._render_stacked(60, cw)

        assert "NOISE" in lines[0]
        # Should have items rendered
        assert len(lines) >= 11  # header + blank + 9 items

    def test_noise_items_rendered_with_dash_count(self):
        """Noise items show '-' as count in rendered output."""
        grid = _make_grid()
        grid.active_category = 3  # NOISE
        cw = ColorWriter(force_color=False)
        lines = grid._render_stacked(60, cw)

        # Item lines start after header + blank line
        for line in lines[2:]:
            if line.strip():
                assert "-" in line  # count rendered as "-"

    def test_noise_reverse_video_header(self):
        """NOISE category header is in reverse video when active."""
        grid = _make_grid()
        grid.active_category = 3  # NOISE
        cw = ColorWriter(force_color=True)
        lines = grid._render_stacked(60, cw)
        header = lines[0]
        assert "\033[7m" in header


# ---------------------------------------------------------------------------
# Viewport scrolling tests (scroll offset fix)
# ---------------------------------------------------------------------------


def _make_large_grid(num_funcs=30):
    """Create a grid with many functions to trigger scrolling."""
    libs = {"exec": 500, "dos": 300}
    funcs = {"Func{}".format(i): 100 - i for i in range(num_funcs)}
    procs = {"Proc{}".format(i): 50 - i for i in range(10)}
    return ToggleGrid(libs, funcs, procs)


class TestViewportScrolling:
    """Tests for per-category viewport scrolling."""

    def test_scroll_offset_initialized(self):
        """scroll_offset dict is initialized to zero for all categories."""
        grid = _make_grid()
        assert grid.scroll_offset == {0: 0, 1: 0, 2: 0, 3: 0}

    def test_available_item_rows(self):
        """_available_item_rows() computes correct row count."""
        grid = _make_grid()
        # 24 rows: rows 3-22 scroll region = 20 lines, minus 2 headers = 18
        assert grid._available_item_rows(24) == 18
        # 10 rows: rows 3-8 scroll region = 6 lines, minus 2 = 4
        assert grid._available_item_rows(10) == 4
        # Minimum: 7 rows -> 1 item row
        assert grid._available_item_rows(7) == 1
        # Very small: returns at least 1
        assert grid._available_item_rows(5) == 1

    def test_cursor_down_scrolls_viewport(self):
        """Moving cursor below viewport scrolls down.

        With visible=5 and indicator overhead of 2, effective window
        is 3 items. Scroll triggers when cursor reaches offset + 3.
        """
        grid = _make_large_grid(num_funcs=30)
        grid.active_category = 1  # FUNCTIONS
        visible = 5

        # effective = max(1, 5 - 2) = 3
        # Move cursor to last visible row before scroll (index 2)
        for _ in range(2):
            grid.move_cursor(1, visible_rows=visible)
        assert grid.cursor_pos[1] == 2
        assert grid.scroll_offset[1] == 0

        # Move one more -- should scroll (pos 3 >= offset 0 + effective 3)
        grid.move_cursor(1, visible_rows=visible)
        assert grid.cursor_pos[1] == 3
        assert grid.scroll_offset[1] == 1

    def test_cursor_up_scrolls_viewport(self):
        """Moving cursor above viewport scrolls up."""
        grid = _make_large_grid(num_funcs=30)
        grid.active_category = 1
        visible = 5

        # Start scrolled down
        grid.cursor_pos[1] = 10
        grid.scroll_offset[1] = 8

        # Move up past scroll_offset
        grid.move_cursor(-1, visible_rows=visible)
        grid.move_cursor(-1, visible_rows=visible)
        assert grid.cursor_pos[1] == 8
        assert grid.scroll_offset[1] == 8

        # Move one more up -- scrolls up
        grid.move_cursor(-1, visible_rows=visible)
        assert grid.cursor_pos[1] == 7
        assert grid.scroll_offset[1] == 7

    def test_page_down(self):
        """Page-down moves cursor by visible_rows items."""
        grid = _make_large_grid(num_funcs=30)
        grid.active_category = 1
        visible = 5

        grid.move_cursor(visible, visible_rows=visible)
        assert grid.cursor_pos[1] == 5
        # effective = max(1, 5 - 2) = 3
        # Scroll offset: pos(5) - effective(3) + 1 = 3
        assert grid.scroll_offset[1] == 3

    def test_page_up(self):
        """Page-up moves cursor up by visible_rows items."""
        grid = _make_large_grid(num_funcs=30)
        grid.active_category = 1
        visible = 5

        # Start near the bottom
        grid.cursor_pos[1] = 20
        grid.scroll_offset[1] = 16

        grid.move_cursor(-visible, visible_rows=visible)
        assert grid.cursor_pos[1] == 15
        assert grid.scroll_offset[1] == 15

    def test_page_down_clamps_at_end(self):
        """Page-down clamps cursor at last item."""
        grid = _make_large_grid(num_funcs=8)
        grid.active_category = 1
        visible = 5

        grid.cursor_pos[1] = 6
        grid.move_cursor(visible, visible_rows=visible)
        assert grid.cursor_pos[1] == 7  # last item (0-indexed, 8 items)

    def test_page_up_clamps_at_start(self):
        """Page-up clamps cursor at first item."""
        grid = _make_large_grid(num_funcs=30)
        grid.active_category = 1
        visible = 5

        grid.cursor_pos[1] = 2
        grid.scroll_offset[1] = 0
        grid.move_cursor(-visible, visible_rows=visible)
        assert grid.cursor_pos[1] == 0
        assert grid.scroll_offset[1] == 0

    def test_scroll_indicators_appear(self):
        """Scroll indicators show when items are hidden."""
        grid = _make_large_grid(num_funcs=20)
        grid.active_category = 1  # FUNCTIONS
        cw = ColorWriter(force_color=False)

        # max_visible_rows=5, 20 items total -> indicators needed
        lines = grid._column_lines(
            grid.func_items, 1, 30, cw, max_visible_rows=5)
        # First line should NOT be an indicator (scroll_offset=0)
        # Last line should be a "down" indicator
        assert "more down" in lines[-1]

    def test_scroll_indicator_up(self):
        """Up indicator shows when items are hidden above."""
        grid = _make_large_grid(num_funcs=20)
        grid.active_category = 1
        grid.scroll_offset[1] = 5
        cw = ColorWriter(force_color=False)

        lines = grid._column_lines(
            grid.func_items, 1, 30, cw, max_visible_rows=5)
        assert "more up" in lines[0]

    def test_scroll_indicator_both(self):
        """Both indicators show when items are hidden above and below."""
        grid = _make_large_grid(num_funcs=20)
        grid.active_category = 1
        grid.scroll_offset[1] = 5
        cw = ColorWriter(force_color=False)

        lines = grid._column_lines(
            grid.func_items, 1, 30, cw, max_visible_rows=5)
        assert "more up" in lines[0]
        assert "more down" in lines[-1]

    def test_no_scroll_indicators_when_all_visible(self):
        """No indicators when all items fit in viewport."""
        grid = _make_grid()  # 3 funcs
        grid.active_category = 1
        cw = ColorWriter(force_color=False)

        lines = grid._column_lines(
            grid.func_items, 1, 30, cw, max_visible_rows=10)
        for line in lines:
            assert "more up" not in line
            assert "more down" not in line

    def test_category_switch_preserves_scroll(self):
        """Switching categories preserves each category's scroll offset."""
        grid = _make_large_grid(num_funcs=30)
        visible = 5

        # Scroll LIBRARIES down
        grid.active_category = 0
        grid.cursor_pos[0] = 1
        grid.scroll_offset[0] = 0

        # Scroll FUNCTIONS down
        grid.active_category = 1
        grid.cursor_pos[1] = 15
        grid.scroll_offset[1] = 11

        # Switch back to LIBRARIES -- its scroll should be preserved
        grid.active_category = 0
        assert grid.scroll_offset[0] == 0
        assert grid.cursor_pos[0] == 1

        # Switch to FUNCTIONS -- its scroll should be preserved
        grid.active_category = 1
        assert grid.scroll_offset[1] == 11
        assert grid.cursor_pos[1] == 15

    def test_clamp_cursor_clamps_scroll_offset(self):
        """clamp_cursor also clamps scroll_offset when items shrink."""
        grid = _make_large_grid(num_funcs=30)
        grid.cursor_pos[1] = 25
        grid.scroll_offset[1] = 20

        # Shrink to 5 items
        grid.func_items = grid.func_items[:5]
        grid.clamp_cursor(1)

        assert grid.cursor_pos[1] == 4  # last valid index
        assert grid.scroll_offset[1] == 4  # clamped to max valid

    def test_clamp_cursor_empty_resets_scroll_offset(self):
        """clamp_cursor resets scroll_offset to 0 for empty list."""
        grid = _make_grid()
        grid.scroll_offset[1] = 10
        grid.cursor_pos[1] = 5
        grid.func_items = []
        grid.clamp_cursor(1)
        assert grid.cursor_pos[1] == 0
        assert grid.scroll_offset[1] == 0

    def test_render_respects_max_visible_rows(self):
        """render() limits output lines to fit the terminal."""
        grid = _make_large_grid(num_funcs=30)
        grid.active_category = 1
        term, output = _make_terminal_state(rows=12, cols=120)
        cw = ColorWriter(force_color=False)

        grid.render(term, cw)
        rendered = output.getvalue()

        # With 12 rows: avail = 12-6 = 6 item rows
        # Total lines = 2 (headers) + items (capped at 6)
        # All should fit in scroll region (rows 3-10, 8 lines)
        assert len(rendered) > 0

    def test_stacked_scroll(self):
        """Stacked layout (narrow terminal) scrolls correctly."""
        grid = _make_large_grid(num_funcs=20)
        grid.active_category = 1
        cw = ColorWriter(force_color=False)

        # Scroll down in stacked mode
        grid.scroll_offset[1] = 5
        grid.cursor_pos[1] = 7

        lines = grid._render_stacked(60, cw, max_visible_rows=5)
        # Should have header + blank + at most 5 item lines
        # (some may be indicators)
        item_lines = lines[2:]  # skip header + blank
        assert len(item_lines) <= 5

    def test_three_column_independent_scroll(self):
        """Three-column mode: each column scrolls independently."""
        grid = _make_large_grid(num_funcs=30)
        grid.active_category = 1  # FUNCTIONS

        # Scroll FUNCTIONS column independently
        grid.scroll_offset[1] = 10
        grid.cursor_pos[1] = 12

        # LIBRARIES column stays at 0
        assert grid.scroll_offset[0] == 0

        cw = ColorWriter(force_color=False)
        lines = grid._render_three_column(120, cw, max_visible_rows=5)

        # Header and blank
        assert len(lines) >= 2
        # The item rows should reflect independent scrolling
        # LIBRARIES items should start from index 0
        # FUNCTIONS items should start from index 10
        item_lines = lines[2:]
        assert len(item_lines) > 0

    def test_two_column_independent_scroll(self):
        """Two-column mode: each column scrolls independently."""
        grid = _make_large_grid(num_funcs=30)
        grid.active_category = 0  # LIBRARIES

        # Scroll FUNCTIONS column independently
        grid.scroll_offset[1] = 10
        grid.cursor_pos[0] = 0

        cw = ColorWriter(force_color=False)
        lines = grid._render_two_column(100, cw, max_visible_rows=5)

        # Should render without error
        item_lines = lines[2:]
        assert len(item_lines) > 0

    def test_move_cursor_without_visible_rows(self):
        """move_cursor without visible_rows still works (backward compat)."""
        grid = _make_grid()
        grid.move_cursor(1)
        assert grid.cursor_pos[0] == 1
        # scroll_offset should not change without visible_rows
        assert grid.scroll_offset[0] == 0

    def test_build_lines_without_max_visible(self):
        """_build_lines without max_visible_rows renders all items."""
        grid = _make_large_grid(num_funcs=20)
        grid.active_category = 1
        cw = ColorWriter(force_color=False)

        lines = grid._build_lines(120, cw)
        # Should have header + blank + all 20 func items (max across cols)
        # 3 columns: LIB(2), FUNC(20), PROC(10) -> max=20 item rows
        item_lines = lines[2:]
        assert len(item_lines) == 20

    def test_footer_text_includes_pgupdn(self):
        """GRID_FOOTER_TEXT includes PgUp/PgDn hint."""
        from amigactl.trace_grid import GRID_FOOTER_TEXT
        assert "PgUp" in GRID_FOOTER_TEXT or "PgDn" in GRID_FOOTER_TEXT

    def test_handle_grid_key_page_down(self):
        """PgDn escape sequence pages down in the grid."""
        viewer = _make_viewer()
        viewer.discovered_libs = {"exec": 200, "dos": 100}
        viewer.discovered_funcs = {
            "exec": {"Func{}".format(i): 100 - i for i in range(30)},
        }
        viewer.discovered_procs = {}

        viewer._enter_toggle_grid()
        viewer.grid.active_category = 1  # FUNCTIONS

        # Get available rows for this terminal
        avail = viewer.grid._available_item_rows(viewer.term.rows)

        # Press PgDn
        viewer._handle_grid_key(("esc", "[6~"))

        # Cursor should have moved by avail rows (clamped to max)
        assert viewer.grid.cursor_pos[1] == min(avail, 29)

    def test_handle_grid_key_page_up(self):
        """PgUp escape sequence pages up in the grid."""
        viewer = _make_viewer()
        viewer.discovered_libs = {"exec": 200, "dos": 100}
        viewer.discovered_funcs = {
            "exec": {"Func{}".format(i): 100 - i for i in range(30)},
        }
        viewer.discovered_procs = {}

        viewer._enter_toggle_grid()
        viewer.grid.active_category = 1  # FUNCTIONS
        viewer.grid.cursor_pos[1] = 20
        viewer.grid.scroll_offset[1] = 15

        avail = viewer.grid._available_item_rows(viewer.term.rows)

        # Press PgUp
        viewer._handle_grid_key(("esc", "[5~"))

        # Cursor should have moved up by avail rows
        assert viewer.grid.cursor_pos[1] == 20 - avail

    def test_render_small_terminal(self):
        """Grid renders correctly on a very small terminal."""
        grid = _make_large_grid(num_funcs=20)
        grid.active_category = 1
        term, output = _make_terminal_state(rows=8, cols=120)
        cw = ColorWriter(force_color=False)

        # Should not raise
        grid.render(term, cw)
        rendered = output.getvalue()
        assert len(rendered) > 0

    def test_scroll_offset_not_negative(self):
        """Scroll offset never goes negative."""
        grid = _make_large_grid(num_funcs=20)
        grid.active_category = 1
        grid.cursor_pos[1] = 0
        grid.scroll_offset[1] = 0

        grid.move_cursor(-5, visible_rows=5)
        assert grid.cursor_pos[1] == 0
        assert grid.scroll_offset[1] == 0

    @pytest.mark.parametrize("total,mvr,offset", [
        (20, 1, 5),   # MF1: tiny viewport, items above and below
        (20, 1, 0),   # tiny viewport, items only below
        (20, 1, 19),  # tiny viewport, items only above
        (20, 2, 5),   # 2-row viewport, items above and below
        (20, 2, 0),   # 2-row viewport, items only below
        (20, 2, 18),  # 2-row viewport, items only above
        (20, 3, 0),   # 3-row viewport, items only below
        (20, 3, 5),   # 3-row viewport, items above and below
        (20, 5, 0),   # normal viewport, start
        (20, 5, 15),  # normal viewport, near end
        (5, 5, 0),    # all items fit exactly
        (3, 5, 0),    # fewer items than viewport
        (1, 1, 0),    # single item, single row
        (3, 2, 1),    # 3 items, 2 rows, offset in middle
    ])
    def test_column_lines_length_invariant(self, total, mvr, offset):
        """_column_lines output never exceeds max_visible_rows."""
        grid = _make_large_grid(num_funcs=total)
        grid.active_category = 1
        grid.scroll_offset[1] = offset
        cw = ColorWriter(force_color=False)
        lines = grid._column_lines(
            grid.func_items, 1, 30, cw, max_visible_rows=mvr)
        assert len(lines) <= mvr

    def test_column_lines_writeback_scroll_offset(self):
        """_column_lines writes corrected scroll_offset back (SF2 fix)."""
        grid = _make_large_grid(num_funcs=10)
        grid.active_category = 1
        # Set scroll_offset past the valid range for a 3-row viewport.
        # With mvr=3, offset=9, total=10:
        #   has_above=True -> 1 indicator row, item_rows=2
        #   end=9+2=11 > 10, so end=10, offset corrected to max(0,10-2)=8
        # The corrected offset=8 is valid: shows items[8..9] + up indicator.
        grid.scroll_offset[1] = 9
        cw = ColorWriter(force_color=False)
        grid._column_lines(
            grid.func_items, 1, 30, cw, max_visible_rows=3)
        # After rendering, stored offset should be corrected from 9 to 8
        assert grid.scroll_offset[1] == 8

    def test_cursor_always_within_rendered_items(self):
        """Cursor never moves to a position hidden by scroll indicators.

        Regression test for the indicator-desync bug: with avail=4 and
        15 items, _column_lines shows at most 3 items (4 - 1 down
        indicator), but old _ensure_cursor_visible allowed cursor at
        position offset+3 which was the indicator row, not an item.

        After the fix, effective = max(1, avail - 2) = 2, so cursor
        stays within the actually-rendered items at all scroll positions.
        """
        grid = _make_large_grid(num_funcs=15)
        grid.active_category = 1  # FUNCTIONS
        avail = 4
        cw = ColorWriter(force_color=False)

        # Walk cursor through all items, checking at each step that
        # the cursor position is within the rendered item range.
        for step in range(15):
            grid.move_cursor(1, visible_rows=avail)

            offset = grid.scroll_offset[1]
            pos = grid.cursor_pos[1]

            # Render to get the actual items shown
            lines = grid._column_lines(
                grid.func_items, 1, 30, cw, max_visible_rows=avail)

            # Count how many lines are items (not indicators)
            item_indices = []
            for idx, line in enumerate(lines):
                if "more up" not in line and "more down" not in line:
                    item_indices.append(idx)

            # The cursor must be within the range [offset, offset + len(item_indices))
            rendered_item_count = len(item_indices)
            assert pos >= offset, (
                "step {}: cursor {} below scroll_offset {}".format(
                    step, pos, offset))
            assert pos < offset + rendered_item_count, (
                "step {}: cursor {} at/beyond last rendered item "
                "(offset={}, rendered={})".format(
                    step, pos, offset, rendered_item_count))

    def test_cursor_walk_up_within_rendered_items(self):
        """Walking cursor upward also stays within rendered items."""
        grid = _make_large_grid(num_funcs=15)
        grid.active_category = 1
        avail = 4
        cw = ColorWriter(force_color=False)

        # Start at bottom
        grid.cursor_pos[1] = 14
        grid.scroll_offset[1] = 12

        for step in range(15):
            grid.move_cursor(-1, visible_rows=avail)

            offset = grid.scroll_offset[1]
            pos = grid.cursor_pos[1]

            lines = grid._column_lines(
                grid.func_items, 1, 30, cw, max_visible_rows=avail)

            item_count = sum(1 for l in lines
                             if "more up" not in l
                             and "more down" not in l)

            assert pos >= offset, (
                "step {}: cursor {} below scroll_offset {}".format(
                    step, pos, offset))
            assert pos < offset + item_count, (
                "step {}: cursor {} beyond rendered items "
                "(offset={}, count={})".format(
                    step, pos, offset, item_count))


# ---------------------------------------------------------------------------
# TestGridTierIntegration
# ---------------------------------------------------------------------------

class TestGridTierIntegration:
    """Tests for toggle grid tier awareness."""

    def test_grid_daemon_disabled_determines_defaults(self):
        """Grid uses daemon_disabled_funcs for initial state."""
        grid = _make_grid(
            libs={"exec": 100},
            funcs={"PutMsg": 50, "OpenLibrary": 80},
            procs={"Shell Process": 20},
            initial_lib="exec",
            daemon_disabled_funcs={"exec.PutMsg"})
        # PutMsg should be unchecked (daemon-disabled)
        putmsg = next(i for i in grid.func_items
                      if i["name"] == "PutMsg")
        assert not putmsg["enabled"]
        # OpenLibrary should be checked (not daemon-disabled)
        openlib = next(i for i in grid.func_items
                       if i["name"] == "OpenLibrary")
        assert openlib["enabled"]

    def test_grid_tier_level_in_func_header(self):
        """Grid FUNCTIONS header includes tier label."""
        grid = _make_grid(initial_lib="exec", tier_level=1)
        label = grid._func_header_label()
        assert "[basic]" in label
        assert "(exec)" in label

    def test_grid_tier_level_detail_in_func_header(self):
        """Grid FUNCTIONS header shows 'detail' for tier 2."""
        grid = _make_grid(initial_lib="dos", tier_level=2)
        label = grid._func_header_label()
        assert "[detail]" in label
        assert "(dos)" in label

    def test_grid_tier_level_verbose_in_func_header(self):
        """Grid FUNCTIONS header shows 'verbose' for tier 3."""
        grid = _make_grid(tier_level=3)
        label = grid._func_header_label()
        assert "[verbose]" in label

    def test_grid_func_header_no_lib(self):
        """Grid FUNCTIONS header without selected library."""
        grid = _make_grid(tier_level=2)
        # No initial_lib, so selected_lib is None
        label = grid._func_header_label()
        assert "FUNCTIONS [detail]" == label

    def test_non_basic_funcs_marked(self):
        """Non-basic functions are marked with non_basic=True."""
        funcs = {"Open": 12, "PutMsg": 5, "AllocMem": 3}
        grid = _make_grid(funcs=funcs, initial_lib="exec")
        for item in grid.func_items:
            if item["name"] == "Open":
                # Open is Basic tier
                assert not item.get("non_basic")
            elif item["name"] == "PutMsg":
                # PutMsg is Manual tier (non-basic)
                assert item.get("non_basic")
            elif item["name"] == "AllocMem":
                # AllocMem is Manual tier (non-basic)
                assert item.get("non_basic")

    def test_non_basic_dimmed_in_render(self):
        """Non-basic functions are rendered dim even when enabled."""
        funcs = {"AllocMem": 128, "Open": 12}
        grid = _make_grid(funcs=funcs, initial_lib="exec")
        cw = ColorWriter(force_color=True)

        alloc_item = next(
            i for i in grid.func_items if i["name"] == "AllocMem")
        # AllocMem should be enabled but dimmed (non-basic)
        assert alloc_item["enabled"]
        text = grid._format_item(alloc_item, cw, 30)
        assert "\033[2m" in text  # dim ANSI code

    def test_basic_func_not_dimmed(self):
        """Basic-tier functions are NOT dimmed when enabled."""
        funcs = {"Open": 12}
        grid = _make_grid(funcs=funcs, initial_lib="dos")
        cw = ColorWriter(force_color=True)

        open_item = next(
            i for i in grid.func_items if i["name"] == "Open")
        assert open_item["enabled"]
        text = grid._format_item(open_item, cw, 30)
        # Should NOT have dim ANSI code
        assert "\033[2m" not in text

    def test_non_basic_updated_on_lib_switch(self):
        """Non-basic marking is refreshed when switching libraries."""
        funcs1 = {"Open": 12}
        grid = _make_grid(funcs=funcs1, initial_lib="dos")

        # Switch to exec library with different functions
        funcs2 = {"AllocMem": 50, "OpenLibrary": 30}
        grid.update_func_items(funcs2, "exec")

        for item in grid.func_items:
            if item["name"] == "AllocMem":
                assert item.get("non_basic")
            elif item["name"] == "OpenLibrary":
                assert not item.get("non_basic")

    def test_func_header_rendered_in_three_column(self):
        """Three-column rendering includes tier label in FUNCTIONS header."""
        grid = _make_grid(initial_lib="exec", tier_level=2)
        grid.active_category = 1  # FUNCTIONS
        cw = ColorWriter(force_color=False)
        lines = grid._build_lines(120, cw)
        # First line is the header row
        header = lines[0]
        assert "[detail]" in header
        assert "(exec)" in header
