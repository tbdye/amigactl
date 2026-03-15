"""Output tier definitions for atrace function tracing.

Three tiers organize 99 traced functions into progressive verbosity
levels. Tier membership is the authoritative source of truth for:
- Loader auto-disable decisions (atrace/main.c)
- Interactive viewer tier switching (trace_ui.py)
- CLI tier flags (--basic/--detail/--verbose)
- Toggle grid tier awareness (trace_grid.py)

Tier assignments are finalized after Phase 9b empirical testing.
Phase 9d audit moved idle-firing functions to Manual tier.
Phase 10 added 19 functions from icon, workbench, dos, intuition,
exec, and graphics libraries.
"""

# --- Tier level constants ---

TIER_BASIC_LEVEL = 1
TIER_DETAIL_LEVEL = 2
TIER_VERBOSE_LEVEL = 3

# --- Tier 1: Basic ---
# Functions where a single event provides immediate diagnostic value.
# SnoopDOS-equivalent + atrace extensions. DEFAULT on trace start.
TIER_BASIC = frozenset({
    # dos.library (19)
    "Open", "Close", "Lock", "DeleteFile", "Execute",
    "GetVar", "FindVar", "LoadSeg", "NewLoadSeg", "CreateDir",
    "MakeLink", "Rename", "RunCommand", "SetVar", "DeleteVar",
    "SystemTagList", "AddDosEntry", "CurrentDir",
    "SetProtection",
    # exec.library (5)
    "OpenDevice", "CloseDevice", "OpenLibrary",
    "OpenResource", "FindResident",
    # intuition.library (12)
    "OpenWindow", "CloseWindow", "OpenScreen", "CloseScreen",
    "ActivateWindow", "WindowToFront", "WindowToBack",
    "OpenWorkBench", "CloseWorkBench", "LockPubScreen",
    "OpenWindowTagList", "OpenScreenTagList",
    # bsdsocket.library (10)
    "socket", "bind", "listen", "accept", "connect", "shutdown",
    "CloseSocket", "setsockopt", "getsockopt", "IoctlSocket",
    # icon.library (5)
    "GetDiskObject", "PutDiskObject", "FreeDiskObject",
    "FindToolType", "MatchToolValue",
    # workbench.library (6)
    "AddAppIconA", "RemoveAppIcon", "AddAppWindowA",
    "RemoveAppWindow", "AddAppMenuItemA", "RemoveAppMenuItem",
})

# --- Tier 2: Detail ---
# Deeper debugging functions, noisy for casual use.
TIER_DETAIL = frozenset({
    # exec.library (5)
    "AllocSignal", "FreeSignal", "CreateMsgPort", "DeleteMsgPort",
    "CloseLibrary",
    # dos.library (4)
    "UnLock", "Examine", "Seek",
    "UnLoadSeg",
    # intuition.library (2)
    "ModifyIDCMP",
    "UnlockPubScreen",
    # bsdsocket.library (2)
    "sendto", "recvfrom",
})

# --- Tier 3: Verbose ---
# High-volume burst events tied to user actions.
TIER_VERBOSE = frozenset({
    # dos.library (1)
    "ExNext",
    # graphics.library (2)
    "OpenFont", "CloseFont",
})

# --- Manual ---
# Extreme event rate functions, only useful with task filtering.
# Never auto-enabled by any tier.
TIER_MANUAL = frozenset({
    # exec.library (21)
    "FindPort", "FindSemaphore", "FindTask",
    "PutMsg", "GetMsg", "ObtainSemaphore", "ReleaseSemaphore",
    "AllocMem", "FreeMem", "AllocVec", "FreeVec",
    "Wait", "Signal", "DoIO", "SendIO", "WaitIO", "AbortIO",
    "CheckIO", "ReplyMsg",
    "AddPort", "WaitPort",
    # dos.library (2)
    "Read", "Write",
    # bsdsocket.library (3)
    "send", "recv", "WaitSelect",
})

# --- Derived sets and validation ---

# Tier display names
TIER_NAMES = {
    TIER_BASIC_LEVEL: "basic",
    TIER_DETAIL_LEVEL: "detail",
    TIER_VERBOSE_LEVEL: "verbose",
}

# Total function count (sanity check)
_ALL_FUNCTIONS = TIER_BASIC | TIER_DETAIL | TIER_VERBOSE | TIER_MANUAL

# Module-level assertions: catch tier definition errors at import time.
assert len(_ALL_FUNCTIONS) == 99, \
    "Tier sets contain {} functions, expected 99".format(
        len(_ALL_FUNCTIONS))
assert not (TIER_BASIC & TIER_DETAIL), "Basic/Detail overlap"
assert not (TIER_BASIC & TIER_VERBOSE), "Basic/Verbose overlap"
assert not (TIER_BASIC & TIER_MANUAL), "Basic/Manual overlap"
assert not (TIER_DETAIL & TIER_VERBOSE), "Detail/Verbose overlap"
assert not (TIER_DETAIL & TIER_MANUAL), "Detail/Manual overlap"
assert not (TIER_VERBOSE & TIER_MANUAL), "Verbose/Manual overlap"


def functions_for_tier(level):
    """Return the cumulative function set for a tier level.

    Tiers are cumulative:
    - Level 1 (Basic): TIER_BASIC
    - Level 2 (Detail): TIER_BASIC | TIER_DETAIL
    - Level 3 (Verbose): TIER_BASIC | TIER_DETAIL | TIER_VERBOSE

    Manual functions are NEVER included in any tier.

    Args:
        level: Tier level (1, 2, or 3).

    Returns:
        frozenset of function names included in the tier.
    """
    funcs = set(TIER_BASIC)
    if level >= TIER_DETAIL_LEVEL:
        funcs |= TIER_DETAIL
    if level >= TIER_VERBOSE_LEVEL:
        funcs |= TIER_VERBOSE
    return frozenset(funcs)


def compute_tier_switch(old_level, new_level,
                        manual_additions=None, manual_removals=None):
    """Compute ENABLE/DISABLE function lists for a tier switch.

    Computes the delta between the old effective function set (tier +
    manual overrides) and the new clean tier set. Manual overrides are
    cleared on tier switch.

    Args:
        old_level: Current tier level (1, 2, or 3).
        new_level: Target tier level (1, 2, or 3).
        manual_additions: set of function names manually enabled
            outside the current tier. Cleared on switch.
        manual_removals: set of function names manually disabled
            within the current tier. Cleared on switch.

    Returns:
        (to_enable, to_disable) -- two sets of function name strings.
    """
    old_funcs = set(functions_for_tier(old_level))
    if manual_additions:
        old_funcs |= manual_additions
    if manual_removals:
        old_funcs -= manual_removals

    new_funcs = functions_for_tier(new_level)

    to_enable = new_funcs - old_funcs
    to_disable = old_funcs - new_funcs

    return (to_enable, to_disable)


def tier_for_function(func_name):
    """Return the tier level for a function, or None for manual.

    Returns the tier where the function is defined (not cumulative).
    - 1 for Basic, 2 for Detail, 3 for Verbose, None for Manual.

    Args:
        func_name: Function name string.

    Returns:
        int (1, 2, 3) or None.
    """
    if func_name in TIER_BASIC:
        return TIER_BASIC_LEVEL
    if func_name in TIER_DETAIL:
        return TIER_DETAIL_LEVEL
    if func_name in TIER_VERBOSE:
        return TIER_VERBOSE_LEVEL
    if func_name in TIER_MANUAL:
        return None
    return None  # Unknown function


def tier_name(level):
    """Return the display name for a tier level.

    Args:
        level: Tier level (1, 2, or 3).

    Returns:
        Display name string ("basic", "detail", or "verbose"),
        or "tier-{}" for unknown levels.
    """
    return TIER_NAMES.get(level, "tier-{}".format(level))


def detect_tier(enabled_set):
    """Detect which tier the enabled function set matches.

    Checks whether the enabled set exactly matches a cumulative tier.
    Returns the highest matching tier level, or None if the set does
    not match any clean tier.

    A set matches a tier if it equals that tier's cumulative function
    set exactly (no manual additions or removals).

    Args:
        enabled_set: set or frozenset of function name strings.

    Returns:
        int (1, 2, or 3) if the set matches a tier, or None.
    """
    enabled = frozenset(enabled_set)
    # Check from highest tier down so we return the exact match
    for level in (TIER_VERBOSE_LEVEL, TIER_DETAIL_LEVEL, TIER_BASIC_LEVEL):
        if enabled == functions_for_tier(level):
            return level
    return None
