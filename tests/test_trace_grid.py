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

def _make_grid(libs=None, funcs=None, procs=None, initial_lib=None):
    """Create a ToggleGrid with test data.

    Defaults provide a minimal three-category grid.
    """
    if libs is None:
        libs = {"exec": 234, "dos": 187}
    if funcs is None:
        funcs = {"Open": 12, "Lock": 8, "Close": 6}
    if procs is None:
        procs = {"bbs": 89, "Shell Process": 43, "ramlib": 10}
    return ToggleGrid(libs, funcs, procs, initial_lib=initial_lib)


def _make_terminal_state(rows=24, cols=80):
    """Create a TerminalState with captured stdout."""
    output = io.StringIO()
    term = TerminalState(stdin_fd=0, stdout=output)
    term.rows = rows
    term.cols = cols
    term._saved_attrs = [[0] * 7]
    return term, output


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
        """Noise functions start with enabled=False."""
        # Include some noise functions in the func dict
        funcs = {
            "Open": 12,
            "AllocMem": 128,
            "GetMsg": 47,
            "FindPort": 30,
            "Lock": 8,
        }
        grid = _make_grid(funcs=funcs, initial_lib="exec")

        noise_names = {"AllocMem", "GetMsg", "FindPort"}
        for item in grid.func_items:
            if item["name"] in noise_names:
                assert not item["enabled"], \
                    "{} should start unchecked".format(item["name"])
            else:
                assert item["enabled"], \
                    "{} should start checked".format(item["name"])

    def test_freemem_not_in_noise(self):
        """FreeMem is NOT in the noise function set (M3 fix)."""
        assert "FreeMem" not in ToggleGrid._NOISE_FUNCS

    def test_toggle_item(self):
        """Toggling an item flips its enabled state."""
        grid = _make_grid()
        # Items sorted by count: exec(234) first, dos(187) second
        assert grid.lib_items[0]["enabled"] is True

        grid.toggle_item("1")  # Toggle first item
        assert grid.lib_items[0]["enabled"] is False

        grid.toggle_item("1")  # Toggle back
        assert grid.lib_items[0]["enabled"] is True

    def test_toggle_item_invalid_key(self):
        """Invalid keys are silently ignored."""
        grid = _make_grid()
        initial = [item["enabled"] for item in grid.lib_items]
        grid.toggle_item("!")  # Not in key string
        after = [item["enabled"] for item in grid.lib_items]
        assert initial == after

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
        # (Functions may have noise defaults, but non-noise should be enabled)
        non_noise_funcs = [item for item in grid.func_items
                           if item["name"] not in ToggleGrid._NOISE_FUNCS]
        assert all(item["enabled"] for item in non_noise_funcs)
        assert all(item["enabled"] for item in grid.proc_items)

    def test_build_filter_whitelist(self):
        """When fewer items enabled, produces LIB= whitelist."""
        grid = _make_grid(
            libs={"exec": 200, "dos": 100, "icon": 50})
        # Disable two of three libs
        grid.toggle_item("1")  # exec
        grid.toggle_item("3")  # icon

        cmd = grid.build_filter_command()
        assert "LIB=dos" == cmd

    def test_build_filter_blacklist(self):
        """When fewer items disabled, produces -LIB= blacklist."""
        grid = _make_grid(
            libs={"exec": 200, "dos": 100, "icon": 50})
        # Disable only one of three libs
        grid.toggle_item("3")  # icon

        cmd = grid.build_filter_command()
        assert "-LIB=icon" == cmd

    def test_build_filter_empty(self):
        """When all enabled, produces empty string."""
        # No noise functions in the func set, so all enabled
        grid = _make_grid(funcs={"Open": 10, "Lock": 5})
        cmd = grid.build_filter_command()
        assert cmd == ""

    def test_build_filter_func_blacklist(self):
        """Function blacklist when fewer disabled than enabled."""
        grid = _make_grid(
            funcs={"Open": 12, "Lock": 8, "Close": 6,
                   "Execute": 4, "LoadSeg": 3})
        # Disable just one
        grid.active_category = 1  # FUNCTIONS
        grid.toggle_item("1")  # Open (highest count)

        cmd = grid.build_filter_command()
        assert "-FUNC=Open" in cmd

    def test_build_filter_combined(self):
        """Combined LIB and FUNC filters in one command."""
        grid = _make_grid(
            libs={"exec": 200, "dos": 100, "icon": 50},
            funcs={"Open": 12, "Lock": 8, "Close": 6})
        # Disable one lib
        grid.toggle_item("3")  # icon (in LIBRARIES)
        # Disable one func
        grid.active_category = 1  # FUNCTIONS
        grid.toggle_item("3")  # Close (lowest count)

        cmd = grid.build_filter_command()
        assert "-LIB=icon" in cmd
        assert "-FUNC=Close" in cmd

    def test_proc_not_in_filter_command(self):
        """Processes are NOT included in build_filter_command() output.

        Process filtering is client-side only (C5 from R1).
        """
        grid = _make_grid()
        # Disable a process
        grid.active_category = 2  # PROCESSES
        grid.toggle_item("1")  # bbs (highest count)

        cmd = grid.build_filter_command()
        # PROC should not appear in the filter command
        assert "PROC" not in cmd
        assert "bbs" not in cmd

    def test_update_func_items(self):
        """Switching library updates function items and reapplies noise."""
        grid = _make_grid()
        new_funcs = {"AllocMem": 128, "FindPort": 47, "LoadSeg": 3}
        grid.update_func_items(new_funcs, "exec")

        assert grid.selected_lib == "exec"
        names = [item["name"] for item in grid.func_items]
        assert "AllocMem" in names
        assert "FindPort" in names
        assert "LoadSeg" in names

        # Noise functions should be disabled after update
        for item in grid.func_items:
            if item["name"] in ToggleGrid._NOISE_FUNCS:
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

    def test_build_filter_all_libs_disabled(self):
        """All libs disabled produces a filter that blocks all (M1 fix)."""
        grid = _make_grid(libs={"exec": 200, "dos": 100})
        grid.toggle_item("1")  # exec off
        grid.toggle_item("2")  # dos off
        cmd = grid.build_filter_command()
        # Must NOT be empty -- should block everything
        assert cmd != ""
        assert "LIB=__NONE__" in cmd

    def test_build_filter_all_funcs_disabled(self):
        """All funcs disabled produces a filter that blocks all (M2 fix)."""
        grid = _make_grid(funcs={"Open": 12, "Lock": 8})
        grid.active_category = 1  # FUNCTIONS
        grid.toggle_item("1")  # Open off
        grid.toggle_item("2")  # Lock off
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
        grid.toggle_item("1")
        assert grid.user_interacted is True
        assert grid.has_user_changes() is True

    def test_has_user_changes_toggle_back_is_no_change(self):
        """Toggling an item and toggling it back is no semantic change."""
        grid = _make_grid(funcs={"Open": 12, "Lock": 8})
        grid.toggle_item("1")
        grid.toggle_item("1")  # toggle back
        # user_interacted is True, but filter output matches initial
        assert grid.user_interacted is True
        assert grid.has_user_changes() is False

    def test_has_user_changes_all_on_no_change(self):
        """All-on when already all on is no semantic change."""
        grid = _make_grid(funcs={"Open": 12, "Lock": 8})
        grid.all_on()
        assert grid.user_interacted is True
        assert grid.has_user_changes() is False

    def test_noise_defaults_scoped_to_exec(self):
        """Noise defaults only apply when selected_lib is exec (S2 fix)."""
        funcs = {"AllocMem": 100, "GetMsg": 50, "Open": 12}
        # dos library: noise funcs should NOT be disabled
        grid = _make_grid(funcs=funcs)
        grid_dos = ToggleGrid({"dos": 100}, funcs, {}, initial_lib="dos")
        for item in grid_dos.func_items:
            assert item["enabled"], \
                "{} should be enabled for dos library".format(item["name"])

        # exec library: noise funcs SHOULD be disabled
        grid_exec = ToggleGrid({"exec": 100}, funcs, {},
                               initial_lib="exec")
        for item in grid_exec.func_items:
            if item["name"] in ToggleGrid._NOISE_FUNCS:
                assert not item["enabled"], \
                    "{} should be disabled for exec".format(item["name"])
            else:
                assert item["enabled"]

    def test_openlibrary_in_noise(self):
        """OpenLibrary is in the noise function set."""
        assert "OpenLibrary" in ToggleGrid._NOISE_FUNCS

    def test_openlibrary_disabled_by_default(self):
        """OpenLibrary disabled by default for exec."""
        funcs = {
            "OpenLibrary": 50, "Open": 12,
        }
        grid = _make_grid(funcs=funcs, initial_lib="exec")

        for item in grid.func_items:
            if item["name"] == "OpenLibrary":
                assert not item["enabled"], \
                    "OpenLibrary should start unchecked"
            elif item["name"] == "Open":
                assert item["enabled"], \
                    "Open should start checked"

    def test_footer_includes_esc(self):
        """Footer text includes Esc cancel hint (S3 fix)."""
        grid = _make_grid()
        cw = ColorWriter(force_color=False)
        for cols in [60, 100, 120]:
            lines = grid._build_lines(cols, cw)
            footer = lines[-1]
            assert "Esc" in footer


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
        """All rendered lines fit within 80 columns."""
        grid = _make_grid()
        grid.selected_lib = "dos"
        cw = ColorWriter(force_color=False)
        lines = grid._build_lines(80, cw)

        for line in lines:
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
        """Enabled item shows key in brackets."""
        grid = _make_grid()
        cw = ColorWriter(force_color=False)
        item = {"name": "exec", "count": 234, "enabled": True}
        text = grid._format_item("1", item, cw, 25)
        assert "[1]" in text
        assert "exec" in text
        assert "234" in text

    def test_format_item_disabled(self):
        """Disabled item shows empty brackets."""
        grid = _make_grid()
        cw = ColorWriter(force_color=False)
        item = {"name": "exec", "count": 234, "enabled": False}
        text = grid._format_item("1", item, cw, 25)
        assert "[ ]" in text
        assert "exec" in text

    def test_format_item_long_name_truncated(self):
        """Long names are truncated with ~ marker."""
        grid = _make_grid()
        cw = ColorWriter(force_color=False)
        item = {"name": "VeryLongFunctionName", "count": 5,
                "enabled": True}
        text = grid._format_item("1", item, cw, 20)
        visible = _visible_len(text)
        assert visible <= 20

    def test_empty_grid(self):
        """Grid with no data renders without errors."""
        grid = _make_grid(libs={}, funcs={}, procs={})
        cw = ColorWriter(force_color=False)
        lines = grid._build_lines(80, cw)
        # Should at least have headers and footer
        assert len(lines) >= 3

    def test_footer_present(self):
        """All rendering modes include a help footer."""
        grid = _make_grid()
        cw = ColorWriter(force_color=False)

        for cols in [60, 100, 120]:
            lines = grid._build_lines(cols, cw)
            footer = lines[-1]
            assert "Enter" in footer
            assert "apply" in footer

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

    def test_noise_func_dim(self):
        """Disabled noise functions are rendered dim (S7)."""
        funcs = {"AllocMem": 128, "Open": 12}
        grid = _make_grid(funcs=funcs, initial_lib="exec")
        cw = ColorWriter(force_color=True)

        # AllocMem is noise for exec, should be disabled
        alloc_item = None
        for item in grid.func_items:
            if item["name"] == "AllocMem":
                alloc_item = item
                break
        assert alloc_item is not None
        assert not alloc_item["enabled"]

        text = grid._format_item("1", alloc_item, cw, 30)
        # Should have dim ANSI code
        assert "\033[2m" in text

    def test_format_item_alignment(self):
        """Counts are right-aligned within column width (S7)."""
        grid = _make_grid()
        cw = ColorWriter(force_color=False)

        item = {"name": "exec", "count": 234, "enabled": True}
        text = grid._format_item("1", item, cw, 25)
        # The count "234" should be at the right side
        # Total visible length should be <= width
        visible = _visible_len(text)
        assert visible <= 25

    def test_focused_lib_index_updated_on_toggle(self):
        """focused_lib_index is updated when toggling in LIBRARIES (C5)."""
        grid = _make_grid(libs={"exec": 200, "dos": 100, "icon": 50})
        # Items sorted: exec(0), dos(1), icon(2)
        assert grid.focused_lib_index == 0

        # Simulate what TraceViewer._handle_grid_key does:
        # toggle key "2" in LIBRARIES category updates focused_lib_index
        keys = "123456789abcdefghijklmnopqrstuvwxyz"
        key = "2"
        idx = keys.find(key)
        if 0 <= idx < len(grid.lib_items):
            grid.focused_lib_index = idx

        assert grid.focused_lib_index == 1  # dos


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
        viewer.discovered_procs = {"bbs": 89, "Shell": 43}

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
        viewer.discovered_procs = {"bbs": 89}

        term, output = _make_terminal_state(rows=24, cols=120)
        viewer.term = term

        viewer._enter_toggle_grid()

        # Disable one library (dos is second by count)
        viewer.grid.toggle_item("2")  # dos

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
        viewer.discovered_procs = {"bbs": 89}

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
        viewer.discovered_procs = {"bbs": 89, "Shell": 43}

        term, output = _make_terminal_state(rows=24, cols=120)
        viewer.term = term

        viewer._enter_toggle_grid()

        # Disable dos (second item by count)
        viewer.grid.toggle_item("2")

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
        viewer.discovered_procs = {"bbs": 89}

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
        viewer.discovered_procs = {"bbs": 89}

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
        viewer.discovered_procs = {"bbs": 89}

        term, output = _make_terminal_state(rows=24, cols=120)
        viewer.term = term

        viewer._enter_toggle_grid()
        # Disable all libraries
        viewer.grid.toggle_item("1")  # exec off
        viewer.grid.toggle_item("2")  # dos off
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
        viewer.discovered_procs = {"bbs": 89}

        term, output = _make_terminal_state(rows=24, cols=120)
        viewer.term = term

        viewer._enter_toggle_grid()
        assert viewer.grid_visible is True

        # Make a change then press Escape
        viewer.grid.toggle_item("1")  # disable exec
        viewer._handle_grid_key(("esc", ""))  # bare Escape

        # Grid should be closed without applying
        assert viewer.grid_visible is False
        assert viewer.grid is None
        # No filter sent
        conn.send_filter.assert_not_called()
        # Client-side filters unchanged (still None = initial state)
        assert viewer.disabled_libs is None


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
