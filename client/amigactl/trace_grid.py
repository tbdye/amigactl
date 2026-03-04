"""Toggle grid for interactive trace filter selection.

Provides a visual, keyboard-driven filter interface showing all
observed libraries, functions, and processes with their event counts.
Items are toggled on/off with single keypresses (1-9, a-z).

Extracted from trace_ui.py for testability and code organization
(C2 fix from R1).
"""

from .trace_ui import _visible_len

# Hotkey bar text for when the grid is visible. Shared between
# the grid footer (rendered inside the scroll area) and the
# hotkey bar (fixed bottom line in trace_ui.py). [SF6 fix]
GRID_FOOTER_TEXT = ("  [A]ll on  [N]one  Arrows: switch category"
                    "  Enter: apply  Esc: cancel")


class ToggleGrid:
    """Filter toggle grid with auto-discovered values.

    Three categories: libraries, functions (scoped to selected
    library), processes.
    """

    def __init__(self, discovered_libs, discovered_funcs_for_lib,
                 discovered_procs, initial_lib=None):
        """Initialize the grid.

        Args:
            discovered_libs: {lib_name: count}
            discovered_funcs_for_lib: {func_name: count} for the
                currently selected library. Updated when the user
                switches libraries.
            discovered_procs: {proc_name: count}
            initial_lib: Name of the initially selected library
                (used to scope noise defaults correctly).
        """
        self.categories = ["LIBRARIES", "FUNCTIONS", "PROCESSES"]
        self.active_category = 0
        self.selected_lib = initial_lib
        self.focused_lib_index = 0  # C5 fix (R4): cursor in LIBRARIES

        # Build items for each category
        self.lib_items = self._build_items(discovered_libs)
        self.func_items = self._build_items(discovered_funcs_for_lib)
        self.proc_items = self._build_items(discovered_procs)

        # Default: noise functions start unchecked
        self._apply_noise_defaults()

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

    def toggle_item(self, key_char):
        """Toggle the item mapped to the given key."""
        items = self._active_items()
        keys = "123456789abcdefghijklmnopqrstuvwxyz"
        idx = keys.find(key_char.lower())
        if 0 <= idx < len(items):
            items[idx]["enabled"] = not items[idx]["enabled"]
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
            row = i + 2  # scroll region starts at row 2
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

    def _format_item(self, key_char, item, cw, width):
        """Format a single toggle item as a fixed-width string.

        Format: "[k] Name      123" or "[ ] Name      123"
        - Enabled items show the key character: [1], [a]
        - Disabled items show empty brackets: [ ]
        - Noise functions (when disabled) are rendered in dim color
        - Counts are right-aligned within the column width

        Args:
            key_char: The toggle key (e.g. "1", "a").
            item: Dict with "name", "count", "enabled" keys.
            cw: ColorWriter instance.
            width: Total column width including brackets and padding.
        """
        name = item["name"]
        count = str(item["count"])

        if item["enabled"]:
            bracket = "[{}]".format(key_char)
        else:
            bracket = "[ ]"

        # Name + count, right-aligned count
        # Available space: width - len("[k] ") - len(count) - 1 space
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

        # Color: disabled items are dim
        if not item["enabled"]:
            text = cw.dim(text)

        return text

    def _render_three_column(self, cols, cw):
        """Render grid in three side-by-side columns (120+ cols).

        Layout:
          LIBRARIES          FUNCTIONS (dos)     PROCESSES
          [1] exec     234   [1] Open       12   [1] bbs      89
          [2] dos      187   [2] Lock        8   [2] Shell    43

        The active category header is rendered in bold.
        """
        lines = []
        col_width = (cols - 4) // 3  # 2-char gap between columns
        keys = "123456789abcdefghijklmnopqrstuvwxyz"

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
        for row in range(min(max_items, len(keys))):
            parts = []
            for cat_idx, items in enumerate([
                    self.lib_items, self.func_items,
                    self.proc_items]):
                if row < len(items):
                    key = keys[row]
                    parts.append(self._format_item(
                        key, items[row], cw, col_width))
                else:
                    parts.append(" " * col_width)
            lines.append("  ".join(parts))

        # Footer
        lines.append("")
        lines.append(GRID_FOOTER_TEXT)

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
        keys = "123456789abcdefghijklmnopqrstuvwxyz"

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
        for row in range(min(max_items, len(keys))):
            parts = []
            if row < len(left_items):
                parts.append(self._format_item(
                    keys[row], left_items[row], cw, col_width))
            else:
                parts.append(" " * col_width)
            if row < len(right_items):
                parts.append(self._format_item(
                    keys[row], right_items[row], cw, col_width))
            else:
                parts.append("")
            lines.append("  ".join(parts))

        lines.append("")
        lines.append(GRID_FOOTER_TEXT)

        return lines

    def _render_stacked(self, cols, cw):
        """Render grid in a single stacked column (<80 cols).

        Shows only the active category, one item per line.
        Category name shown as header.
        """
        lines = []
        keys = "123456789abcdefghijklmnopqrstuvwxyz"

        cat = self.categories[self.active_category]
        label = cat
        if cat == "FUNCTIONS" and self.selected_lib:
            label = "FUNCTIONS ({})".format(self.selected_lib)
        lines.append(cw.reverse(label))
        lines.append("")

        items = self._active_items()
        for row in range(min(len(items), len(keys))):
            lines.append(self._format_item(
                keys[row], items[row], cw, cols - 2))

        lines.append("")
        lines.append(GRID_FOOTER_TEXT)

        return lines
