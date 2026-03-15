# Interactive Trace Viewer (TUI)

The interactive trace viewer provides a full-screen Terminal User Interface (TUI) for
monitoring atrace events in real time. It supports pause and scrollback,
per-library/function/process filtering through a toggle grid, text search,
event detail inspection, statistics, tier switching, and scrollback export.

The viewer is implemented entirely with ANSI escape sequences -- no curses
or other TUI library is required. It uses DECSTBM (DEC Set Top and Bottom
Margins) scroll regions for a fixed-header, fixed-footer layout with a
scrolling event area in between.

## Accessing the Viewer

The interactive viewer is available **only** through the `amigactl shell`
interactive mode. It is not available from the CLI `amigactl trace start`
command, which prints plain text to stdout.

To launch the viewer:

```
$ amigactl --host <ip> shell
amigactl> trace start
```

Optional arguments are the same as the CLI form:

```
amigactl> trace start --lib dos
amigactl> trace start --detail
amigactl> trace start --errors --lib exec
```

The viewer takes over the terminal until you press `q` to quit or the
stream ends (for `trace run`, when the traced process exits).

See [cli-reference.md](cli-reference.md) for full option syntax.

## Screen Layout

The viewer divides the terminal into four fixed regions:

```
+------------------------------------------------------+
| Line 1:  Status bar (fixed)                          |
| Line 2:  Column headers (fixed)                      |
|                                                      |
|  Lines 3 through rows-1:  Scroll region (events)     |
|                                                      |
| Line rows:  Hotkey bar (fixed)                       |
+------------------------------------------------------+
```

- **Status bar** (line 1): Event counts, elapsed time, current tier,
  active filter indicators, and pause state. Updated once per second or
  when state changes.

- **Column headers** (line 2): Rendered in dim text. Column names match
  the event fields: SEQ, TIME, FUNCTION, TASK, ARGS, RESULT.

- **Scroll region** (lines 3 through rows-1): Events scroll upward
  automatically in live mode. The terminal's DECSTBM mechanism handles
  scrolling within this region without disturbing the status bar or
  hotkey bar.

- **Hotkey bar** (bottom line): Context-sensitive key hints. Changes
  based on the current mode (live, paused, grid, help, detail).

### Adaptive Column Widths

Column widths adapt to the terminal width. The `ColumnLayout` class
computes widths at four breakpoints:

| Terminal Width | Timestamp | Function | Result | Task | Library Names |
|----------------|-----------|----------|--------|------|---------------|
| 120+ cols      | 12 chars  | 20 chars | 12 chars | 16 chars | Full (e.g., `dos.Open`) |
| 80-119 cols    | 12 chars  | 16 chars | 8 chars  | 14 chars | Full |
| 60-79 cols     | 8 chars   | 12 chars | 6 chars  | 10 chars | Abbreviated (e.g., `d.Open`) |
| <60 cols       | Hidden    | 10 chars | 4 chars  | 8 chars  | Abbreviated |

The SEQ column is fixed at 6 characters. The ARGS column receives all
remaining space (minimum 10 characters). Function names and task names
are truncated with a trailing `~` when they exceed their column width.
Args are truncated with `...`.

## Color Coding

Events are color-coded by library. Known libraries have fixed color
assignments:

| Library      | Color        |
|--------------|--------------|
| dos          | Cyan         |
| exec         | Yellow       |
| intuition    | Green        |
| graphics     | Blue         |
| bsdsocket    | Bold red     |
| icon         | Magenta      |
| workbench    | Bold cyan    |

Unknown libraries are auto-assigned from a rotating 10-color palette.

Return values are colored by status: green for success (`O`), red for
error (`E`), and uncolored for neutral. Sequence numbers are rendered
in dim text. Task names are green.

Color output respects the `NO_COLOR` environment variable and
`AMIGACTL_COLOR` setting. See [cli-reference.md](cli-reference.md) for
details.

## Status Bar

The status bar content changes based on the current mode:

**Live mode (default):**

```
TRACE [basic]: 42 events (38 shown) 3 errors | ERRORS | noise | time:rel | +0:05.2
```

Components:
- `TRACE`: Mode indicator.
- `[basic]`: Current tier. Shows manual overrides when present, e.g.,
  `[basic+AllocMem]` or `[detail-OpenFont]` or
  `[basic-OpenFont+AllocMem,PutMsg]`.
- Event count: `42 events` or `42 events (38 shown)` when client-side
  filters hide some events.
- Error count: `3 errors` (only shown when errors > 0).
- Filter indicators: `ERRORS` (errors-only filter active), `noise`
  (noise suppression active), `search: "pattern"` (search filter active).
- Timestamp mode: `time:rel` or `time:delta` (omitted for absolute mode).
- Elapsed time: `+M:SS.t` format, time between first and most recent event.

**Paused mode:**

```
PAUSED | event 37/42 | 5 new
```

Shows the highlight cursor position, total events in the combined view
(snapshot + new arrivals), and the count of combined events below the current viewport. Appends `| buffer full` when both the scrollback
(10,000 events) and pause buffer (1,000 events) have reached capacity.

**Statistics mode:**

```
STATS: dos.Open:42 | dos.Lock:31 | dos.Close:28 | exec.OpenLibrary:15 | dos.LoadSeg:8 | dos.GetVar:7 | 131 events 3 errors | +1:23.4
```

Shows the top 6 functions by call count (sorted descending), formatted
as `lib.func:count` separated by ` | `, followed by total event count,
error count, and elapsed time.

**Detail mode:**

```
DETAIL | Event #42
```

Shows the sequence number of the event being inspected.

## Hotkey Bar

The bottom-line hotkey bar is context-sensitive. Its content changes with
the current mode and adapts to terminal width, falling back to
abbreviated or minimal formats when the full text does not fit.

**Live/paused mode (full width):**

```
  [Tab] filters  [/] search  [p] pause  [s] stats  [e] errors  [c] clear  [S] save  [1/2/3] tier  [t] time  [?] help  [q] quit
```

Active toggles are shown in uppercase: `[p] RESUME`, `[s] STATS`,
`[e] ERRORS`, `[t] RELATIVE`, `[t] DELTA`.

**Grid mode:**

```
  Up/Dn: select  PgUp/PgDn: page  Space: toggle  [A]ll on  [N]one  Arrows: category  Enter: apply  Esc: cancel
```

**Help mode:**

```
  Up/Down  PgUp/PgDn  scroll  |  Any other key to dismiss
```

**Detail mode:**

```
  Esc to dismiss
```

## Keybindings

### Main Mode

These keybindings are active in live and paused modes (when no overlay
is visible):

| Key | Action |
|-----|--------|
| `q` | Stop the trace and exit the viewer. Sends STOP to the daemon, drains remaining events, and restores the terminal. |
| `p` | Toggle pause. When pausing, freezes the display and snapshots the scrollback for navigation. When resuming, replays buffered events and returns to live streaming. |
| `s` | Toggle statistics mode. Replaces the status bar content with per-function call counts. The scroll region is unaffected. |
| `t` | Cycle timestamp display: absolute -> relative -> delta. Absolute shows `HH:MM:SS.mmm` wall clock time. Relative shows `+S.uuuuuu` seconds since the first event. Delta shows `+S.uuuuuu` since the previous event. The scroll region re-renders to reflect the new mode. |
| `/` | Enter search mode. The hotkey bar becomes a text input. Type a search pattern (case-insensitive substring match against formatted event text), then press Enter to apply or Esc to cancel. While typing, events continue to be consumed from the socket. Backspace edits the pattern. |
| `Tab` | Open the function toggle grid (see [Toggle Grid](#toggle-grid) below). |
| `?` | Toggle the help overlay. Shows a scrollable help screen listing all keybindings. |
| `e` | Toggle the ERRORS filter. When active, only error events are shown (daemon-side filtering via the ERRORS keyword in the FILTER command). The status bar shows `ERRORS` and the hotkey bar shows `[e] ERRORS`. |
| `S` | Save the current filtered scrollback to a timestamped log file. The file is written to the current working directory as `atrace_YYYYMMDD_HHMMSS.log`. Only events matching the current client-side filters and search pattern are included. ANSI color codes are stripped. |
| `c` | Clear all events and reset counters (live mode only, ignored when paused). Wipes the scrollback buffer, all statistics, discovered filter values, and handle/segment caches. Does not send any protocol commands -- the trace stream continues. Does not clear the user's filter choices. |
| `1` | Switch to Basic tier (57 functions). |
| `2` | Switch to Detail tier (70 functions: Basic + 13). |
| `3` | Switch to Verbose tier (73 functions: Basic + Detail + 3). |
| `Enter` | Open the detail view for the highlighted event (paused mode only). |

### Scroll Navigation

| Key | Live Mode | Paused Mode |
|-----|-----------|-------------|
| `Up` | Auto-pauses, then moves highlight up one event. | Moves highlight up one event. |
| `Down` | No effect. | Moves highlight down one event. If the highlight is already at the last event, auto-resumes to live mode. |
| `PgUp` | Auto-pauses, then moves highlight up one page (terminal height minus 4 rows). | Moves highlight up one page. |
| `PgDn` | No effect. | Moves highlight down one page. If the highlight reaches the last event, auto-resumes to live mode. |

The viewport follows the highlight cursor: if the highlight moves
outside the visible window, `pause_scroll_pos` adjusts to keep it
in view.

### Help Overlay

| Key | Action |
|-----|--------|
| `Up` | Scroll help text up one line. |
| `Down` | Scroll help text down one line. |
| `PgUp` | Scroll help text up one page. |
| `PgDn` | Scroll help text down one page. |
| Any other key | Dismiss the help overlay and restore the event view. If paused, the scrollback view is rebuilt with the highlight at the bottom. If live, events resume streaming. |

When the help content exceeds the available scroll region height, a
position indicator is shown at the bottom: `[lines 1-20 of 34]`.

### Detail View

| Key | Action |
|-----|--------|
| `Esc` | Dismiss the detail view and return to the scrollback view with the highlight position preserved. |
| `Up` | Scroll detail content up one line. |
| `Down` | Scroll detail content down one line. |
| `PgUp` | Scroll detail content up one page. |
| `PgDn` | Scroll detail content down one page. |

All other keys are ignored while the detail view is visible (only Esc
dismisses it).

### Grid Mode

See [Toggle Grid Keybindings](#grid-keybindings) below.

## Pause and Scrollback

### How Pause Works

Pressing `p` toggles between live and paused modes. In live mode,
events scroll through the display as they arrive. In paused mode, the
display freezes and the user can navigate through the event history.

Events are **always consumed** from the network socket regardless of
pause state. The socket remains in the `select()` wait set at all times
to prevent daemon-side backpressure from backing up the TCP connection.

### Buffer Architecture

Two distinct buffers store events:

1. **Scrollback** (`deque`, maxlen=10,000): The persistent event history.
   Every event is appended here unconditionally as it arrives, before
   client-side filters are applied. This enables retroactive filtering:
   changing filters while paused reveals previously-hidden events from
   the scrollback. When the deque reaches capacity, the oldest events
   are silently discarded.

2. **Pause buffer** (`list`, max 1,000 entries): A temporary buffer for
   events that arrive while the display is paused (or while an overlay
   such as the help screen or toggle grid is visible). On resume, pause
   buffer events are replayed into the scroll region for visual
   continuity, then the buffer is cleared. The pause buffer only holds
   events that pass the current client-side filters and search pattern.
   Comment events (metadata lines beginning with `#`) are added to the
   pause buffer unconditionally, bypassing filter checks.

When paused, the viewer builds a **combined event list** from the
filtered scrollback snapshot (frozen at pause time) plus the filtered
pause buffer (live arrivals). Navigation keys move through this combined
list.

### Auto-Pause and Auto-Resume

- Pressing `Up` or `PgUp` in live mode automatically enters pause mode
  before moving the highlight. This allows immediate scroll-back
  navigation without a separate pause keystroke.

- Pressing `Down` or `PgDn` when the highlight is already at the last
  event in the combined list automatically resumes live mode.

### Buffer Full Indicator

When both buffers are at capacity (scrollback at 10,000 and pause buffer
at 1,000), the status bar appends `| buffer full`.

When scrolled to the very top of the scrollback while it has been
truncated, the first row displays a dim notice:
`[buffer full -- oldest events truncated]`. This replaces one event row;
scrolling down one line reveals all events.

## Search

Pressing `/` enters search mode. The hotkey bar transforms into a text
input prompt:

```
Search: dos.Open
```

Type a search pattern and press `Enter` to apply. The pattern is matched
as a case-insensitive substring against the formatted event text
(including all fields: sequence number, timestamp, function name, task,
args, and result).

- **Backspace** deletes the last character.
- **Esc** cancels the current search input and clears any previously
  active search pattern.
- **Enter** applies the pattern.

While typing, events continue to be consumed from the socket to prevent
backpressure.

After applying a search pattern:
- In live mode, the scroll region re-renders from the scrollback,
  showing only matching events.
- In paused mode, the filtered snapshot is rebuilt and the view
  scrolls to the bottom with the highlight at the last matching event.

The search filter indicator appears in the status bar as
`search: "pattern"`. An empty pattern (Enter with no text) clears the
search.

Events are stored in the scrollback regardless of search state,
so changing the search pattern retroactively reveals or hides events.

## Toggle Grid

Pressing `Tab` opens the function toggle grid, a multi-column filter
interface showing all observed libraries, functions, processes, and
noise filter items with their event counts.

### Categories

The grid has four categories, navigated with Left/Right arrow keys:

1. **LIBRARIES**: Every library observed during the trace session.
   Items are sorted by event count (descending). Each shows a checkbox
   and count: `[x] dos     234`. Disabling a library hides all its
   events.

2. **FUNCTIONS**: Scoped to the currently selected library. Shows
   per-function event counts. The header indicates the library and
   current tier: `FUNCTIONS (dos) [basic]`.
   - Functions outside the Basic tier are rendered in **dim text** to
     visually distinguish them from default-tier functions.
   - Functions disabled at the daemon level (e.g., by tier auto-disable)
     show `[D]` instead of `[ ]`.

3. **PROCESSES**: Every process name observed during the trace session.
   Process filtering is **client-side only** -- toggling processes does
   not send a FILTER command to the daemon.

4. **NOISE**: Nine client-side shell noise suppression items. These
   control whether shell initialization variables (SetVar/GetVar/FindVar
   events) are displayed. All are suppressed by default. The items are:
   `process`, `echo`, `debug`, `oldredirect`, `interactive`,
   `simpleshell`, `RC`, `Result2`, `LV_ALIAS`. Noise items show `-`
   instead of an event count because they are configuration toggles,
   not event sources.

### Responsive Layout

The grid layout adapts to terminal width:

| Terminal Width | Layout |
|----------------|--------|
| 120+ cols | Three side-by-side columns. When NOISE is the active category, the visible columns shift to LIBRARIES, PROCESSES, NOISE (LIBRARIES always anchored on the left). |
| 80-119 cols | Two columns. The active category and one adjacent category are shown. |
| <80 cols | Single stacked column showing only the active category. |

The active category header is rendered in **reverse video**. Each
category scrolls independently -- scroll position is preserved when
switching between categories with Left/Right arrows. When a category has
more items than fit in the viewport, scroll indicators appear:
`^ more up` and `v more down`.

### Library-Function Scoping

When navigating Right from LIBRARIES to FUNCTIONS, the function list
updates to show functions for the library that the cursor is on in
the LIBRARIES column. Switching libraries (by moving the cursor in
LIBRARIES and pressing Right) saves the current library's function
toggle state and restores the target library's state. Libraries not
yet visited in this grid session keep their daemon-derived defaults.

### <a name="grid-keybindings"></a>Grid Keybindings

| Key | Action |
|-----|--------|
| `Up` / `Down` | Move the cursor within the active category. The cursor is clamped to the item list bounds. |
| `PgUp` / `PgDn` | Move the cursor by one page within the active category. Page size equals the number of visible item rows. |
| `Left` / `Right` | Switch between categories (LIBRARIES -> FUNCTIONS -> PROCESSES -> NOISE). |
| `Space` | Toggle the item at the cursor position (enable/disable). |
| `A` or `a` | Enable all items in the active category. |
| `N` or `n` | Disable all items in the active category. |
| `Enter` | Apply filter changes and close the grid. Sends a FILTER command to the daemon (for library and function changes) and updates client-side filter state (for process and noise changes). |
| `Esc` | Cancel without applying. All changes made in the grid are discarded. The pre-grid filter state is fully restored, including per-library function states and noise suppression. |

### Filter Command Generation

When `Enter` is pressed, the grid generates an optimized FILTER command
using whichever form is shorter -- whitelist (`LIB=dos,exec`) or
blacklist (`-LIB=bsdsocket`). If functions were toggled that are
disabled at the daemon level (shown as `[D]`), ENABLE/DISABLE
directives are included to change the daemon-side patch state.

Opening and closing the grid without making any changes is a no-op --
no FILTER command is sent, preserving any initial filters from
`trace start`.

See [filtering.md](filtering.md) for details on daemon-side vs.
client-side filtering and the FILTER command syntax.

## Detail View

Pressing `Enter` while paused opens a detail view for the highlighted
event. The detail view is a scrollable overlay that replaces the scroll
region content and hides the column header.

The detail view shows every event field in an expanded, labeled format:

```
  Event #42
  ----------------------------------

  Time       10:30:15.123456
  Function   dos.Open
  Task       [3] Shell Process
  Args       "RAM:test.txt",MODE_OLDFILE
  Result     0x1c16daf
  Status     OK
```

Long field values (particularly Args) are soft-wrapped at the terminal
width with consistent indentation.

The status bar shows `DETAIL | Event #42` and the hotkey bar shows
`Esc to dismiss`.

Navigation within the detail view:

- `Up` / `Down`: Scroll one line.
- `PgUp` / `PgDn`: Scroll one page.
- `Esc`: Dismiss and return to the scrollback view.

When the detail content exceeds the visible area, a position indicator
appears: `[lines 1-20 of 24]`.

Comment events (lines beginning with `#`) do not have the standard
event fields and cannot be opened in the detail view.

## Timestamp Modes

Pressing `t` cycles through three timestamp display modes:

1. **Absolute** (default): Wall clock time from the Amiga's EClock,
   displayed as `HH:MM:SS.mmm` (millisecond precision) or
   `HH:MM:SS.uuuuuu` (microsecond precision, depending on daemon
   configuration). At narrow terminal widths (60-79 cols), only the
   sub-second portion `SS.mmm` is shown.

2. **Relative**: Time since the first event in the session, displayed as
   `+S.uuuuuu` (seconds and microseconds).

3. **Delta**: Time since the previous event, displayed as `+S.uuuuuu`.
   Useful for identifying slow operations or gaps between events.

Changing the timestamp mode re-renders the scroll region immediately.
The status bar shows `time:rel` or `time:delta` when not in absolute
mode.

Midnight wraparound is handled correctly: if a timestamp difference
would be negative, 24 hours is added.

## Tier Switching

Pressing `1`, `2`, or `3` switches the output tier, controlling which
functions are enabled at the daemon's patch level:

| Key | Tier | Functions Enabled |
|-----|------|-------------------|
| `1` | Basic (default) | 57 core diagnostic functions |
| `2` | Detail | Basic + 13 deeper debugging functions (70 total) |
| `3` | Verbose | Detail + 3 high-volume functions (73 total) |

Tier switching sends ENABLE/DISABLE directives to the daemon via the
FILTER command. The current filter state (library, function, process,
and errors filters) is preserved across the switch.

Manual overrides (individual function toggles made through the grid
that deviate from the tier's default set) are cleared on tier switch.
The status bar reflects the current tier and any manual overrides:
`[basic]`, `[detail+AllocMem]`, `[basic-OpenFont+AllocMem,PutMsg]`.

If the viewer is already at the requested tier with no manual overrides,
the key press is a no-op.

The daemon is also notified of the tier level via an inline `TIER <n>`
command, which enables tier-aware content filtering at the daemon level
(e.g., suppressing low-value events at the Basic tier).

See [output-tiers.md](output-tiers.md) for full tier membership lists
and the rationale behind tier assignments.

## Handle and Segment Resolution

The viewer maintains two client-side caches for annotating events with
additional context:

- **HandleResolver**: Tracks `Open` and `Lock` return values and maps
  them to file paths. When a `Close` event arrives, the args are
  annotated with the original file path. `CurrentDir` events are
  similarly annotated with the lock's path. Cache size is bounded at
  256 entries with FIFO eviction.

- **SegmentResolver**: Tracks `LoadSeg` and `NewLoadSeg` return values
  and maps segment pointers to filenames. When a `RunCommand` event
  arrives, the `seg=0x...` argument is annotated with the loaded
  filename. Cache size is bounded at 128 entries.

Annotations are computed eagerly at event arrival time and stored with
the event, so they remain correct even when handles are reused.

## Save Scrollback

Pressing `S` exports the current filtered scrollback to a log file in
the current working directory. The filename is
`atrace_YYYYMMDD_HHMMSS.log`.

Only events that match the current client-side filters (library,
function, process, noise) and search pattern are included. ANSI color
codes are stripped from the output. Comment events are preserved as
`# text` lines.

The hotkey bar briefly shows the result: `Saved 42 events to
atrace_20260315_143022.log` or `Saved 38 of 42 events to ...` when
filters excluded some events. If the save fails, the error is shown:
`Save failed: [Errno 13] Permission denied`.

## Clear Events

Pressing `c` in live mode (ignored when paused) resets the viewer to a
clean state:

- Clears the scrollback buffer and pause buffer.
- Resets all counters: total events, shown events, error count.
- Clears all statistics (per-function, per-library, per-process counts).
- Clears discovered filter data (the toggle grid will start empty).
- Clears handle and segment resolution caches.
- Redraws the screen with an empty scroll region.

The trace stream continues uninterrupted. The user's filter choices
(disabled libraries, functions, processes, noise settings) are
preserved. No protocol commands are sent.

## Errors Filter

Pressing `e` toggles the ERRORS filter. When active, only events with
error status (`E`) are transmitted by the daemon. This is a daemon-side
filter using the `ERRORS` keyword in the FILTER protocol command.

The status bar shows `ERRORS` and the hotkey bar shows `[e] ERRORS`
when the filter is active. The toggle sends a FILTER command to the
daemon that combines the errors state with any active grid filters.

See [filtering.md](filtering.md) for details on error classification
and the ERRORS filter.

## Terminal Resize

The viewer handles terminal resize (SIGWINCH) gracefully. The signal
handler sets a flag for deferred processing; the actual resize work
happens in the next event loop iteration (signal-safe).

On resize:
- Terminal dimensions are re-read.
- `ColumnLayout` is recreated for the new width.
- DECSTBM scroll regions are reconfigured.
- Status bar, column header, and hotkey bar are redrawn.
- If the detail view is visible, its content is re-wrapped and re-rendered.
- If the toggle grid is visible, it is re-rendered at the new dimensions.
- The help overlay is not re-rendered on resize. Dismiss and reopen it
  for correct layout after resizing the terminal.
- Terminals shorter than 5 rows skip DECSTBM setup to avoid invalid
  escape sequences.

## Noise Suppression

The viewer suppresses shell initialization noise by default. Nine noise
items control whether specific SetVar, GetVar, and FindVar events are
displayed. These variables are set and read during normal shell
operation and are rarely relevant to debugging:

| Noise Item | Suppressed Events |
|------------|-------------------|
| `process` | SetVar/GetVar for "process" |
| `echo` | SetVar/GetVar for "echo" |
| `debug` | SetVar/GetVar for "debug" |
| `oldredirect` | SetVar/GetVar for "oldredirect" |
| `interactive` | SetVar/GetVar for "interactive" |
| `simpleshell` | SetVar/GetVar for "simpleshell" |
| `RC` | SetVar/GetVar for "RC" (duplicates return value info) |
| `Result2` | SetVar/GetVar for "Result2" (duplicates IoErr info) |
| `LV_ALIAS` | FindVar with `,LV_ALIAS` in args |

All items are suppressed by default. Individual items can be enabled
through the NOISE category in the toggle grid. The status bar shows
`noise` when any suppression is active.

Noise filtering is entirely client-side -- it does not affect what the
daemon sends. Events suppressed by noise filters are still stored in
the scrollback and can be revealed by changing the noise settings.

## Related Documentation

- [output-tiers.md](output-tiers.md) -- Tier membership lists and
  switching behavior.
- [filtering.md](filtering.md) -- Daemon-side vs. client-side
  filtering, FILTER command syntax, filter presets.
- [cli-reference.md](cli-reference.md) -- CLI `trace start` and
  `trace run` options. The distinction between CLI plain-text output
  and the interactive TUI viewer.
- [reading-output.md](reading-output.md) -- Interpreting event fields,
  return values, status codes, and timestamps.
