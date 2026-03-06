"""Toggle grid for interactive trace filter selection.

Provides a visual, keyboard-driven filter interface showing all
observed libraries, functions, and processes with their event counts.
Items are selected with arrow keys and toggled with Space.

Extracted from trace_ui.py for testability and code organization
(C2 fix from R1).
"""

from .trace_ui import _visible_len

# Hotkey bar text for when the grid is visible. Shared between
# the grid footer (rendered inside the scroll area) and the
# hotkey bar (fixed bottom line in trace_ui.py). [SF6 fix]
GRID_FOOTER_TEXT = ("  Up/Down: select  Space: toggle  [A]ll on  [N]one"
                    "  Arrows: category  Enter: apply  Esc: cancel")


class ToggleGrid:
    """Filter toggle grid with auto-discovered values.

    Three categories: libraries, functions (scoped to selected
    library), processes.
    """

    def __init__(self, discovered_libs, discovered_funcs_for_lib,
                 discovered_procs, initial_lib=None,
                 daemon_disabled_funcs=None):
        """Initialize the grid.

        Args:
            discovered_libs: {lib_name: count}
            discovered_funcs_for_lib: {func_name: count} for the
                currently selected library. Updated when the user
                switches libraries.
            discovered_procs: {proc_name: count}
            initial_lib: Name of the initially selected library
                (used to scope noise defaults correctly).
            daemon_disabled_funcs: set of "lib.func" strings for
                functions disabled at the daemon level (noise functions).
        """
        self.categories = ["LIBRARIES", "FUNCTIONS", "PROCESSES"]
        self.active_category = 0
        self.selected_lib = initial_lib
        self.focused_lib_index = 0  # C5 fix (R4): cursor in LIBRARIES
        self._daemon_disabled_funcs = daemon_disabled_funcs or set()

        # Per-category cursor position (persisted across category switches).
        # Invariant: focused_lib_index == cursor_pos[0] at all points
        # where focused_lib_index is consumed (_get_selected_lib_name,
        # update_func_items). Both are set together in _enter_toggle_grid()
        # and synced in _handle_grid_key() (Right-arrow and Space handlers).
        # move_cursor() on category 0 updates cursor_pos[0] but not
        # focused_lib_index; the sync happens on the next Right-arrow or
        # Space before any consumer reads focused_lib_index.
        self.cursor_pos = {0: 0, 1: 0, 2: 0}

        # Build items for each category
        self.lib_items = self._build_items(discovered_libs)
        self.func_items = self._build_items(discovered_funcs_for_lib)
        self.proc_items = self._build_items(discovered_procs)

        # Default: noise functions start unchecked
        self._apply_noise_defaults()

        # Mark daemon-disabled items (Fix 3)
        self._mark_daemon_disabled()

        # S5 fix (Wave 5): Snapshot the initial filter output so we
        # can detect whether the user actually changed anything.
        # Opening and closing the grid with no changes should be a
        # no-op (no FILTER sent to daemon).
        self._initial_filter = self.build_filter_command()
        self.user_interacted = False

    # Noise functions per library, matching noise_func_names[] in
    # daemon/trace.c line 143. FreeMem is NOT in this list
    # (it is not a traced function). (M3 fix)
    # Keyed by library name so noise defaults only apply to the
    # correct library (S2 fix, Wave 5 review).
    _NOISE_FUNCS_BY_LIB = {
        "exec": {
            "FindPort", "FindSemaphore", "FindTask", "GetMsg",
            "PutMsg", "ObtainSemaphore", "ReleaseSemaphore",
            "AllocMem", "OpenLibrary",
        },
    }

    # Flat set for _format_item() dim styling (all libs combined)
    _NOISE_FUNCS = {
        fn for fns in _NOISE_FUNCS_BY_LIB.values() for fn in fns
    }

    def _build_items(self, count_dict):
        """Build toggle items from a name->count dict."""
        items = []
        for name, count in sorted(count_dict.items(),
                                   key=lambda x: -x[1]):
            items.append({
                "name": name,
                "count": count,
                "enabled": True,
            })
        return items

    def _apply_noise_defaults(self):
        """Noise functions start unchecked in the grid.

        Only applies to functions belonging to a library with
        defined noise functions (S2 fix, Wave 5 review).
        """
        noise_for_lib = self._NOISE_FUNCS_BY_LIB.get(
            self.selected_lib, set())
        for item in self.func_items:
            if item["name"] in noise_for_lib:
                item["enabled"] = False

    def _mark_daemon_disabled(self):
        """Mark function items that are daemon-disabled.

        daemon_disabled_funcs uses "lib.func" format. Items are matched
        against the currently selected library.
        """
        lib = self.selected_lib or ""
        for item in self.func_items:
            qualified = "{}.{}".format(lib, item["name"])
            item["daemon_disabled"] = (
                qualified in self._daemon_disabled_funcs)

    def update_func_items(self, discovered_funcs_for_lib, lib_name):
        """Update function items when the user switches libraries.

        Args:
            discovered_funcs_for_lib: {func_name: count} for the
                newly selected library.
            lib_name: Name of the selected library (for display).
        """
        self.selected_lib = lib_name
        self.func_items = self._build_items(discovered_funcs_for_lib)
        self._apply_noise_defaults()
        self._mark_daemon_disabled()

    def clamp_cursor(self, cat_idx):
        """Clamp cursor_pos for the given category to valid bounds.

        Must be called after any operation that may shrink the item
        list for a category (e.g., update_func_items() on library
        switch). Render methods and toggle_at_cursor() assume
        cursor_pos is pre-clamped.

        Args:
            cat_idx: Category index (0=LIBRARIES, 1=FUNCTIONS, 2=PROCESSES).
        """
        cat_name = self.categories[cat_idx]
        if cat_name == "LIBRARIES":
            items = self.lib_items
        elif cat_name == "FUNCTIONS":
            items = self.func_items
        elif cat_name == "PROCESSES":
            items = self.proc_items
        else:
            return
        if not items:
            self.cursor_pos[cat_idx] = 0
        else:
            self.cursor_pos[cat_idx] = max(
                0, min(len(items) - 1, self.cursor_pos[cat_idx]))

    def move_cursor(self, delta):
        """Move cursor within the active category by delta rows.

        Clamps to [0, len(items)-1]. No-op if category is empty.

        Args:
            delta: +1 for down, -1 for up.
        """
        items = self._active_items()
        if not items:
            return
        pos = self.cursor_pos[self.active_category]
        pos = max(0, min(len(items) - 1, pos + delta))
        self.cursor_pos[self.active_category] = pos

    def toggle_at_cursor(self):
        """Toggle the item at the current cursor position."""
        items = self._active_items()
        pos = self.cursor_pos[self.active_category]
        if 0 <= pos < len(items):
            items[pos]["enabled"] = not items[pos]["enabled"]
            self.user_interacted = True

    def all_on(self):
        """Enable all items in the active category."""
        for item in self._active_items():
            item["enabled"] = True
        self.user_interacted = True

    def none(self):
        """Disable all items in the active category."""
        for item in self._active_items():
            item["enabled"] = False
        self.user_interacted = True

    def _active_items(self):
        """Return the item list for the active category."""
        cat = self.categories[self.active_category]
        if cat == "LIBRARIES":
            return self.lib_items
        elif cat == "FUNCTIONS":
            return self.func_items
        elif cat == "PROCESSES":
            return self.proc_items
        return []

    def has_user_changes(self):
        """Return True if the user changed anything from the initial state.

        S5 fix (Wave 5): When the grid is opened and closed without
        any user interaction, no FILTER should be sent. This compares
        the current filter output against the initial snapshot taken
        at construction time.
        """
        if not self.user_interacted:
            return False
        return self.build_filter_command() != self._initial_filter

    def build_filter_command(self):
        """Build a FILTER command string from current toggle state.

        Uses whitelist when fewer items are enabled than disabled,
        blacklist when fewer are disabled. Minimizes command length.

        Returns a raw filter string (without the "FILTER " prefix)
        suitable for passing to conn.send_filter(raw=...).

        Note (C5): Process filtering is client-side only. The PROC
        items affect _passes_client_filter() in TraceViewer, not
        the daemon FILTER command.

        Known limitation (S6 fix, R4): FUNC= filters match by
        function name globally, not scoped to the library shown in
        the grid. The grid shows functions for the selected library,
        but the daemon's FUNC= filter applies to ALL libraries. For
        the current 30-function set there are no cross-library name
        collisions, so this is functionally correct.
        """
        parts = []

        # Libraries
        enabled_libs = [i["name"] for i in self.lib_items
                        if i["enabled"]]
        disabled_libs = [i["name"] for i in self.lib_items
                         if not i["enabled"]]
        if disabled_libs and not enabled_libs:
            # All disabled: empty whitelist blocks everything
            parts.append("LIB=__NONE__")
        elif disabled_libs:
            if len(disabled_libs) <= len(enabled_libs):
                parts.append(
                    "-LIB=" + ",".join(disabled_libs))
            else:
                parts.append(
                    "LIB=" + ",".join(enabled_libs))

        # Functions
        enabled_funcs = [i["name"] for i in self.func_items
                         if i["enabled"]]
        disabled_funcs = [i["name"] for i in self.func_items
                          if not i["enabled"]]
        if disabled_funcs and not enabled_funcs:
            # All disabled: empty whitelist blocks everything
            parts.append("FUNC=__NONE__")
        elif disabled_funcs:
            if len(disabled_funcs) <= len(enabled_funcs):
                parts.append(
                    "-FUNC=" + ",".join(disabled_funcs))
            else:
                parts.append(
                    "FUNC=" + ",".join(enabled_funcs))

        return " ".join(parts) if parts else ""

    def render(self, term, cw):
        """Render the grid into the terminal scroll region.

        Uses term.write_at() (public API) instead of _write()
        for proper line clearing and truncation (S2 fix).
        """
        term.clear_scroll_region()
        lines = self._build_lines(term.cols, cw)
        for i, line in enumerate(lines):
            row = i + 3  # scroll region starts at row 3
            if row >= term.rows - 1:
                break
            term.write_at(row, line)

    def _build_lines(self, cols, cw):
        """Build grid display lines."""
        if cols >= 120:
            return self._render_three_column(cols, cw)
        elif cols >= 80:
            return self._render_two_column(cols, cw)
        else:
            return self._render_stacked(cols, cw)

    def _format_item(self, item, cw, width, highlighted=False):
        """Format a single toggle item as a fixed-width string.

        Format: "[x] Name      123" or "[ ] Name      123"
        - Enabled items show [x], disabled show [ ]
        - highlighted=True renders in reverse video (cursor position)
        - Disabled non-highlighted items render dim

        Args:
            item: Dict with "name", "count", "enabled" keys.
            cw: ColorWriter instance.
            width: Total column width including brackets and padding.
            highlighted: Whether this item is at the cursor position
                in the active category.
        """
        name = item["name"]
        count = str(item["count"])

        if item.get("daemon_disabled") and not item["enabled"]:
            bracket = "[D]"  # Daemon-disabled
        else:
            bracket = "[x]" if item["enabled"] else "[ ]"

        # Name + count, right-aligned count
        # Available space: width - len("[x] ") - len(count) - 1 space
        name_width = width - 5 - len(count)
        if name_width < 1:
            name_width = 1
        if len(name) > name_width:
            name = name[:name_width - 1] + "~"
        padded_name = name + " " * max(0, name_width - len(name))

        text = "{} {} {}".format(bracket, padded_name, count)

        # Clamp to column width (large counts can overflow)
        if len(text) > width:
            text = text[:width]

        # Color: highlighted = reverse, disabled = dim
        if highlighted:
            text = cw.reverse(text)
        elif not item["enabled"]:
            text = cw.dim(text)

        return text

    def _render_three_column(self, cols, cw):
        """Render grid in three side-by-side columns (120+ cols).

        Layout:
          LIBRARIES          FUNCTIONS (dos)     PROCESSES
          [x] exec     234   [x] Open       12   [x] bbs      89
          [ ] dos      187   [ ] Lock        8   [ ] Shell    43

        The active category header is rendered in reverse video.
        """
        lines = []
        col_width = (cols - 4) // 3  # 2-char gap between columns

        # Headers
        headers = []
        for i, cat in enumerate(self.categories):
            label = cat
            if cat == "FUNCTIONS" and self.selected_lib:
                label = "FUNCTIONS ({})".format(self.selected_lib)
            if i == self.active_category:
                label = cw.reverse(label)
            headers.append(label + " " * max(
                0, col_width - _visible_len(label)))
        lines.append("  ".join(headers))
        lines.append("")  # blank line after headers

        # Item rows -- iterate to the max item count
        max_items = max(len(self.lib_items),
                        len(self.func_items),
                        len(self.proc_items))
        for row in range(max_items):
            parts = []
            for cat_idx, items in enumerate([
                    self.lib_items, self.func_items,
                    self.proc_items]):
                if row < len(items):
                    hl = (cat_idx == self.active_category
                          and row == self.cursor_pos[cat_idx])
                    parts.append(self._format_item(
                        items[row], cw, col_width, highlighted=hl))
                else:
                    parts.append(" " * col_width)
            lines.append("  ".join(parts))

        return lines

    def _render_two_column(self, cols, cw):
        """Render grid in two columns (80-119 cols).

        Shows the active category and one adjacent category.
        If LIBRARIES is active, show LIBRARIES + FUNCTIONS.
        If FUNCTIONS is active, show LIBRARIES + FUNCTIONS.
        If PROCESSES is active, show FUNCTIONS + PROCESSES.
        """
        lines = []
        col_width = (cols - 2) // 2

        # Determine which two categories to show
        if self.active_category <= 1:
            left_idx, right_idx = 0, 1
            left_items = self.lib_items
            right_items = self.func_items
        else:
            left_idx, right_idx = 1, 2
            left_items = self.func_items
            right_items = self.proc_items

        # Headers
        left_label = self.categories[left_idx]
        right_label = self.categories[right_idx]
        if left_label == "FUNCTIONS" and self.selected_lib:
            left_label = "FUNCTIONS ({})".format(self.selected_lib)
        if right_label == "FUNCTIONS" and self.selected_lib:
            right_label = "FUNCTIONS ({})".format(self.selected_lib)

        if left_idx == self.active_category:
            left_label = cw.reverse(left_label)
        if right_idx == self.active_category:
            right_label = cw.reverse(right_label)

        left_hdr = left_label + " " * max(
            0, col_width - _visible_len(left_label))
        right_hdr = right_label
        lines.append("  ".join([left_hdr, right_hdr]))
        lines.append("")

        max_items = max(len(left_items), len(right_items))
        for row in range(max_items):
            parts = []
            if row < len(left_items):
                hl = (left_idx == self.active_category
                      and row == self.cursor_pos[left_idx])
                parts.append(self._format_item(
                    left_items[row], cw, col_width, highlighted=hl))
            else:
                parts.append(" " * col_width)
            if row < len(right_items):
                hl = (right_idx == self.active_category
                      and row == self.cursor_pos[right_idx])
                parts.append(self._format_item(
                    right_items[row], cw, col_width, highlighted=hl))
            else:
                parts.append("")
            lines.append("  ".join(parts))

        return lines

    def _render_stacked(self, cols, cw):
        """Render grid in a single stacked column (<80 cols).

        Shows only the active category, one item per line.
        Category name shown as header.
        """
        lines = []

        cat = self.categories[self.active_category]
        label = cat
        if cat == "FUNCTIONS" and self.selected_lib:
            label = "FUNCTIONS ({})".format(self.selected_lib)
        lines.append(cw.reverse(label))
        lines.append("")

        items = self._active_items()
        cat_idx = self.active_category
        for row in range(len(items)):
            hl = (row == self.cursor_pos[cat_idx])
            lines.append(self._format_item(
                items[row], cw, cols - 2, highlighted=hl))

        return lines
