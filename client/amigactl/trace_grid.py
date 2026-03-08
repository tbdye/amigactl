"""Toggle grid for interactive trace filter selection.

Provides a visual, keyboard-driven filter interface showing all
observed libraries, functions, processes, and noise filter items
with their event counts. Items are selected with arrow keys and
toggled with Space.

Extracted from trace_ui.py for testability and code organization
(C2 fix from R1).
"""

from .trace_ui import _visible_len

# Noise filter items. Each corresponds to a specific suppression
# rule in _passes_client_filter(). The "name" keys match the
# variable names in _SHELL_INIT_VARS plus "LV_ALIAS" for the
# FindVar filter.
NOISE_ITEMS = [
    "process", "echo", "debug", "oldredirect",
    "interactive", "simpleshell",
    "RC", "Result2",
    "LV_ALIAS",
]

# Hotkey bar text for when the grid is visible. Shared between
# the grid footer (rendered inside the scroll area) and the
# hotkey bar (fixed bottom line in trace_ui.py). [SF6 fix]
GRID_FOOTER_TEXT = ("  Up/Dn: select  PgUp/PgDn: page  Space: toggle"
                    "  [A]ll on  [N]one  Arrows: category"
                    "  Enter: apply  Esc: cancel")


class ToggleGrid:
    """Filter toggle grid with auto-discovered values.

    Four categories: libraries, functions (scoped to selected
    library), processes, and noise (client-side shell noise filters).
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
        self.categories = ["LIBRARIES", "FUNCTIONS", "PROCESSES", "NOISE"]
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
        self.cursor_pos = {0: 0, 1: 0, 2: 0, 3: 0}

        # Per-category scroll offset: first visible item index.
        # Each category scrolls independently. Preserved across
        # category switches (Left/Right arrows).
        self.scroll_offset = {0: 0, 1: 0, 2: 0, 3: 0}

        # Build items for each category
        self.lib_items = self._build_items(discovered_libs)
        self.func_items = self._build_items(discovered_funcs_for_lib)
        self.proc_items = self._build_items(discovered_procs)

        # NOISE items use the same semantics as LIB/FUNC/PROC:
        # enabled=True means "show these events"
        # enabled=False means "suppress these events" (default for noise)
        # count=None because noise items are configuration toggles, not
        # event sources -- _format_item() renders None as "-"
        self.noise_items = [
            {"name": n, "count": None, "enabled": False}
            for n in NOISE_ITEMS
        ]

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
    # daemon/trace.c and atrace/main.c. (M3 fix)
    # Keyed by library name so noise defaults only apply to the
    # correct library (S2 fix, Wave 5 review).
    _NOISE_FUNCS_BY_LIB = {
        "exec": {
            "FindPort", "FindSemaphore", "FindTask", "GetMsg",
            "PutMsg", "ObtainSemaphore", "ReleaseSemaphore",
            "AllocMem", "OpenLibrary",
            # Phase 5 additions
            "FreeMem", "AllocVec", "FreeVec",
            # Phase 5 device I/O additions
            "DoIO", "SendIO", "WaitIO", "AbortIO", "CheckIO",
        },
        "dos": {
            # Phase 5 additions
            "Read", "Write",
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

    def _items_for_category(self, cat_idx):
        """Return the item list for a given category index."""
        cat_name = self.categories[cat_idx]
        if cat_name == "LIBRARIES":
            return self.lib_items
        elif cat_name == "FUNCTIONS":
            return self.func_items
        elif cat_name == "PROCESSES":
            return self.proc_items
        elif cat_name == "NOISE":
            return self.noise_items
        return []

    def clamp_cursor(self, cat_idx):
        """Clamp cursor_pos and scroll_offset for the given category.

        Must be called after any operation that may shrink the item
        list for a category (e.g., update_func_items() on library
        switch). Render methods and toggle_at_cursor() assume
        cursor_pos is pre-clamped.

        Note: scroll_offset is loosely clamped to [0, len(items)-1]
        because the true maximum depends on visible_rows, which is
        only known at render time. _column_lines() corrects the
        offset during rendering and writes the corrected value back
        to self.scroll_offset (SF2 fix). This loose clamp prevents
        wildly out-of-range values after item list shrinks.

        Args:
            cat_idx: Category index (0=LIBRARIES, 1=FUNCTIONS, 2=PROCESSES).
        """
        items = self._items_for_category(cat_idx)
        if not items:
            self.cursor_pos[cat_idx] = 0
            self.scroll_offset[cat_idx] = 0
        else:
            self.cursor_pos[cat_idx] = max(
                0, min(len(items) - 1, self.cursor_pos[cat_idx]))
            self.scroll_offset[cat_idx] = max(
                0, min(len(items) - 1, self.scroll_offset[cat_idx]))

    def move_cursor(self, delta, visible_rows=None):
        """Move cursor within the active category by delta rows.

        Clamps to [0, len(items)-1]. No-op if category is empty.
        After moving, adjusts scroll_offset to keep the cursor
        within the visible viewport.

        Args:
            delta: +1 for down, -1 for up, or larger for page moves.
            visible_rows: Number of visible item rows in the viewport.
                Used to auto-scroll. If None, no scroll adjustment
                (backward compat for tests that don't track viewport).
        """
        items = self._active_items()
        if not items:
            return
        cat = self.active_category
        pos = self.cursor_pos[cat]
        pos = max(0, min(len(items) - 1, pos + delta))
        self.cursor_pos[cat] = pos
        if visible_rows is not None and visible_rows > 0:
            self._ensure_cursor_visible(cat, visible_rows)

    def _ensure_cursor_visible(self, cat_idx, visible_rows):
        """Adjust scroll_offset so cursor_pos is within the viewport.

        Uses a conservative effective window size that accounts for
        scroll indicators. _column_lines() reserves up to 2 rows for
        up/down indicators, so the actual number of visible items can
        be as low as visible_rows - 2. Using ``max(1, visible_rows - 2)``
        as the effective size guarantees the cursor is always within the
        rendered item range. When fewer than 2 indicators are shown,
        the cursor simply stays closer to the viewport edges — a fine
        tradeoff vs. computing the exact indicator count (which depends
        on scroll position, creating a circular dependency).

        Args:
            cat_idx: Category index.
            visible_rows: Number of visible item rows (raw, before
                indicator overhead).
        """
        effective = max(1, visible_rows - 2)
        pos = self.cursor_pos[cat_idx]
        offset = self.scroll_offset[cat_idx]
        if pos < offset:
            self.scroll_offset[cat_idx] = pos
        elif pos >= offset + effective:
            self.scroll_offset[cat_idx] = pos - effective + 1

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
        return self._items_for_category(self.active_category)

    def has_user_changes(self):
        """Return True if the user changed anything from the initial state.

        S5 fix (Wave 5): When the grid is opened and closed without
        any user interaction, no FILTER should be sent. This compares
        the current filter output against the initial snapshot taken
        at construction time.

        Note: Noise items are client-side only and do not affect
        build_filter_command(). Toggling noise items alone will NOT
        cause this method to return True, which is intentional --
        noise changes do not require a FILTER command to the daemon.
        """
        if not self.user_interacted:
            return False
        return self.build_filter_command() != self._initial_filter

    def get_noise_state(self):
        """Return set of enabled noise item names.

        Enabled = "show this noise" (not suppressed).
        Items NOT in this set are suppressed.
        """
        return {i["name"] for i in self.noise_items if i["enabled"]}

    def build_filter_command(self):
        """Build a FILTER command string from current toggle state.

        Uses whitelist when fewer items are enabled than disabled,
        blacklist when fewer are disabled. Minimizes command length.

        Returns a raw filter string (without the "FILTER " prefix)
        suitable for passing to conn.send_filter(raw=...).

        Note (C5): Process filtering is client-side only. The PROC
        items affect _passes_client_filter() in TraceViewer, not
        the daemon FILTER command.

        FUNC= filters use library-scoped "lib.func" format for
        unambiguous daemon-side matching (Phase 7b, Feature 8b.1).
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

        # Functions -- use lib.func format for daemon disambiguation.
        # When no library context is available (selected_lib is None),
        # skip dotted FUNC=/--FUNC= clauses but still emit the __NONE__
        # sentinel to block all functions when all are disabled.
        lib = self.selected_lib or ""
        enabled_funcs = ["{}.{}".format(lib, i["name"])
                         for i in self.func_items if i["enabled"]]
        disabled_funcs = ["{}.{}".format(lib, i["name"])
                          for i in self.func_items if not i["enabled"]]
        if disabled_funcs and not enabled_funcs:
            # All disabled: empty whitelist blocks everything
            parts.append("FUNC=__NONE__")
        elif disabled_funcs and lib:
            # Only emit dotted FUNC=/-FUNC= when library context exists.
            # Without a library prefix, ".FuncName" would be sent, which
            # the daemon parses as lib="" and silently matches nothing.
            if len(disabled_funcs) <= len(enabled_funcs):
                parts.append(
                    "-FUNC=" + ",".join(disabled_funcs))
            else:
                parts.append(
                    "FUNC=" + ",".join(enabled_funcs))

        return " ".join(parts) if parts else ""

    def _available_item_rows(self, term_rows):
        """Compute the number of item rows that fit in the viewport.

        Layout math:
          Row 1:        status bar (fixed, outside scroll region)
          Row 2:        column headers (fixed, outside scroll region)
          Rows 3..R-1:  scroll region (R = term_rows)
          Row R:        hotkey bar (fixed, outside scroll region)

        Within the scroll region, the grid uses:
          Line 0 (row 3): category headers
          Line 1 (row 4): blank separator
          Lines 2..N:     item rows

        Maximum usable row is term_rows - 2 (must be < term_rows - 1).
        Total scroll lines = (term_rows - 2) - 3 + 1 = term_rows - 4.
        Subtract 2 for headers = term_rows - 6 item rows.

        Returns at least 1 to avoid degenerate zero-row viewports.
        """
        return max(1, term_rows - 6)

    def render(self, term, cw):
        """Render the grid into the terminal scroll region.

        Uses term.write_at() (public API) instead of _write()
        for proper line clearing and truncation (S2 fix).
        """
        term.clear_scroll_region()
        avail = self._available_item_rows(term.rows)
        lines = self._build_lines(term.cols, cw, avail)
        for i, line in enumerate(lines):
            row = i + 3  # scroll region starts at row 3
            if row >= term.rows - 1:
                break
            term.write_at(row, line)

    def _build_lines(self, cols, cw, max_visible_rows=None):
        """Build grid display lines.

        Args:
            cols: Terminal width.
            cw: ColorWriter instance.
            max_visible_rows: Maximum number of item rows to render.
                If None, renders all items (backward compat).
        """
        if cols >= 120:
            return self._render_three_column(cols, cw, max_visible_rows)
        elif cols >= 80:
            return self._render_two_column(cols, cw, max_visible_rows)
        else:
            return self._render_stacked(cols, cw, max_visible_rows)

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
        count = str(item["count"]) if item["count"] is not None else "-"

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

    def _scroll_indicator(self, direction, col_width, cw):
        """Build a scroll indicator line for hidden items.

        Args:
            direction: "up" or "down" -- which direction has hidden items.
            col_width: Width of the column.
            cw: ColorWriter instance.

        Returns:
            A dim-styled indicator string of the given column width.
        """
        if direction == "up":
            arrow = "^"
        else:
            arrow = "v"
        # Center the arrow with a visual hint
        text = "  {} more {}".format(arrow, direction)
        if len(text) > col_width:
            text = text[:col_width]
        else:
            text = text + " " * (col_width - len(text))
        return cw.dim(text)

    def _column_lines(self, items, cat_idx, col_width, cw,
                      max_visible_rows):
        """Build visible item lines for a single column with scrolling.

        Returns a list of formatted lines for the visible viewport
        of the given category. Includes scroll indicators when items
        are hidden above or below.

        Args:
            items: Full item list for this category.
            cat_idx: Category index (for cursor/scroll state).
            col_width: Column width for formatting.
            cw: ColorWriter instance.
            max_visible_rows: Maximum item rows to display. If None,
                shows all items (no scrolling).

        Returns:
            List of formatted line strings, length <= max_visible_rows.
        """
        if not items:
            return []

        total = len(items)
        offset = self.scroll_offset[cat_idx]

        if max_visible_rows is None or total <= max_visible_rows:
            # No scrolling needed -- render all items
            lines = []
            for row in range(total):
                hl = (cat_idx == self.active_category
                      and row == self.cursor_pos[cat_idx])
                lines.append(self._format_item(
                    items[row], cw, col_width, highlighted=hl))
            return lines

        # Scrolling needed. Determine how many indicator lines we need
        # and reduce item space accordingly.
        has_above = offset > 0
        has_below = offset + max_visible_rows < total

        # Reserve rows for indicators
        indicator_rows = (1 if has_above else 0) + (1 if has_below else 0)
        item_rows = max_visible_rows - indicator_rows

        # Edge case: if we now have fewer item rows and items extend
        # beyond, we may need to recalculate. Clamp to at least 1 row.
        if item_rows < 1:
            item_rows = 1
            indicator_rows = max_visible_rows - item_rows

        # Re-check after adjusting item_rows
        end = offset + item_rows
        if end > total:
            end = total
            offset = max(0, total - item_rows)

        has_above = offset > 0
        has_below = end < total

        # Recalculate if indicator situation changed
        needed_indicators = (1 if has_above else 0) + (1 if has_below else 0)
        if needed_indicators < indicator_rows:
            # Freed up a row -- give it to items
            item_rows = max_visible_rows - needed_indicators
            end = min(offset + item_rows, total)
            has_below = end < total

        # MF1 fix: enforce that total output (indicators + items) does
        # not exceed max_visible_rows. With tiny viewports (1-2 rows),
        # indicators could push the output over budget.
        budget = max_visible_rows
        if has_above:
            budget -= 1
        if has_below:
            budget -= 1
        if budget < 1:
            # No room for indicators -- show items only
            has_above = False
            has_below = False
            item_rows = max_visible_rows
            end = min(offset + item_rows, total)
        elif budget < item_rows:
            item_rows = budget
            end = min(offset + item_rows, total)

        # SF2 fix: sync stored scroll_offset with the corrected local
        # offset so the next cursor move uses the actually-displayed
        # viewport position (avoids jarring jump after terminal resize).
        self.scroll_offset[cat_idx] = offset

        lines = []
        if has_above:
            lines.append(self._scroll_indicator("up", col_width, cw))

        for row in range(offset, end):
            hl = (cat_idx == self.active_category
                  and row == self.cursor_pos[cat_idx])
            lines.append(self._format_item(
                items[row], cw, col_width, highlighted=hl))

        if has_below:
            lines.append(self._scroll_indicator("down", col_width, cw))

        return lines

    def _render_three_column(self, cols, cw, max_visible_rows=None):
        """Render grid in three side-by-side columns (120+ cols).

        Layout:
          LIBRARIES          FUNCTIONS (dos)     PROCESSES
          [x] exec     234   [x] Open       12   [x] bbs      89
          [ ] dos      187   [ ] Lock        8   [ ] Shell    43

        When NOISE is the active category (index 3), the visible
        columns shift to show LIBRARIES, PROCESSES, NOISE -- keeping
        LIBRARIES anchored in the left column for navigation consistency.

        The active category header is rendered in reverse video.
        """
        lines = []
        col_width = (cols - 4) // 3  # 2-char gap between columns

        # Map category index -> item list for all 4 categories
        all_items = [self.lib_items, self.func_items,
                     self.proc_items, self.noise_items]

        # Determine which three categories to show.
        # LIBRARIES is always anchored in the left column for
        # navigation consistency. When NOISE is active, the
        # right two columns shift to show PROCESSES + NOISE.
        if self.active_category <= 2:
            visible_cats = [0, 1, 2]
        else:
            visible_cats = [0, 2, 3]  # LIB, PROC, NOISE

        # Headers
        headers = []
        for cat_idx in visible_cats:
            label = self.categories[cat_idx]
            if label == "FUNCTIONS" and self.selected_lib:
                label = "FUNCTIONS ({})".format(self.selected_lib)
            if cat_idx == self.active_category:
                label = cw.reverse(label)
            headers.append(label + " " * max(
                0, col_width - _visible_len(label)))
        lines.append("  ".join(headers))
        lines.append("")  # blank line after headers

        # Build visible items for each column with independent scrolling
        col_lines = []
        for cat_idx in visible_cats:
            items = all_items[cat_idx]
            col_lines.append(self._column_lines(
                items, cat_idx, col_width, cw, max_visible_rows))

        # Pad columns to equal height and interleave
        max_rows = max((len(cl) for cl in col_lines), default=0)
        for row in range(max_rows):
            parts = []
            for col_data in col_lines:
                if row < len(col_data):
                    parts.append(col_data[row])
                else:
                    parts.append(" " * col_width)
            lines.append("  ".join(parts))

        return lines

    def _render_two_column(self, cols, cw, max_visible_rows=None):
        """Render grid in two columns (80-119 cols).

        Shows the active category and one adjacent category.
        If LIBRARIES is active, show LIBRARIES + FUNCTIONS.
        If FUNCTIONS is active, show LIBRARIES + FUNCTIONS.
        If PROCESSES is active, show FUNCTIONS + PROCESSES.
        If NOISE is active, show PROCESSES + NOISE.
        """
        lines = []
        col_width = (cols - 2) // 2

        # Determine which two categories to show.
        # Goal: always show the active category + one useful neighbor.
        all_items = [self.lib_items, self.func_items,
                     self.proc_items, self.noise_items]

        if self.active_category <= 1:
            # LIBRARIES or FUNCTIONS active: show LIB + FUNC
            left_idx, right_idx = 0, 1
        elif self.active_category == 2:
            # PROCESSES active: show FUNC + PROC
            left_idx, right_idx = 1, 2
        else:
            # NOISE active: show PROC + NOISE (the two rightmost)
            left_idx, right_idx = 2, 3

        left_items = all_items[left_idx]
        right_items = all_items[right_idx]

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

        # Build visible items for each column with independent scrolling
        left_lines = self._column_lines(
            left_items, left_idx, col_width, cw, max_visible_rows)
        right_lines = self._column_lines(
            right_items, right_idx, col_width, cw, max_visible_rows)

        max_rows = max(len(left_lines), len(right_lines))
        for row in range(max_rows):
            parts = []
            if row < len(left_lines):
                parts.append(left_lines[row])
            else:
                parts.append(" " * col_width)
            if row < len(right_lines):
                parts.append(right_lines[row])
            else:
                parts.append("")
            lines.append("  ".join(parts))

        return lines

    def _render_stacked(self, cols, cw, max_visible_rows=None):
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
        col_lines = self._column_lines(
            items, cat_idx, cols - 2, cw, max_visible_rows)
        lines.extend(col_lines)

        return lines
