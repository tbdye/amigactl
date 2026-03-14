"""Unit tests for the output tier definitions (trace_tiers.py).

Pure unit tests for tier membership invariants, cumulative tier
computation, tier switching deltas, function-to-tier lookups,
tier detection from enabled sets, and tier name formatting.
"""

import pytest

from amigactl.trace_tiers import (
    TIER_BASIC, TIER_BASIC_LEVEL,
    TIER_DETAIL, TIER_DETAIL_LEVEL,
    TIER_VERBOSE, TIER_VERBOSE_LEVEL,
    TIER_MANUAL, TIER_NAMES,
    _ALL_FUNCTIONS,
    compute_tier_switch,
    detect_tier,
    functions_for_tier,
    tier_for_function,
    tier_name,
)


# ---------------------------------------------------------------------------
# TestTierDefinitions
# ---------------------------------------------------------------------------

class TestTierDefinitions:
    """Verify tier membership invariants."""

    def test_all_80_functions_assigned(self):
        """Every function is in exactly one tier."""
        assert len(_ALL_FUNCTIONS) == 80

    def test_tiers_disjoint(self):
        """No function appears in multiple tiers."""
        assert not (TIER_BASIC & TIER_DETAIL)
        assert not (TIER_BASIC & TIER_VERBOSE)
        assert not (TIER_BASIC & TIER_MANUAL)
        assert not (TIER_DETAIL & TIER_VERBOSE)
        assert not (TIER_DETAIL & TIER_MANUAL)
        assert not (TIER_VERBOSE & TIER_MANUAL)

    def test_basic_count(self):
        assert len(TIER_BASIC) == 43

    def test_detail_count(self):
        assert len(TIER_DETAIL) == 11

    def test_verbose_count(self):
        assert len(TIER_VERBOSE) == 2

    def test_manual_count(self):
        assert len(TIER_MANUAL) == 24

    def test_union_equals_all(self):
        """The four tiers together contain all 80 functions."""
        union = TIER_BASIC | TIER_DETAIL | TIER_VERBOSE | TIER_MANUAL
        assert union == _ALL_FUNCTIONS

    def test_tiers_are_frozensets(self):
        """Tier sets are immutable frozensets."""
        assert isinstance(TIER_BASIC, frozenset)
        assert isinstance(TIER_DETAIL, frozenset)
        assert isinstance(TIER_VERBOSE, frozenset)
        assert isinstance(TIER_MANUAL, frozenset)

    def test_level_constants(self):
        """Tier level constants have correct values."""
        assert TIER_BASIC_LEVEL == 1
        assert TIER_DETAIL_LEVEL == 2
        assert TIER_VERBOSE_LEVEL == 3


# ---------------------------------------------------------------------------
# TestFunctionsForTier
# ---------------------------------------------------------------------------

class TestFunctionsForTier:
    """Tests for functions_for_tier() cumulative behavior."""

    def test_level_1_is_basic(self):
        """Level 1 returns exactly the Basic tier."""
        assert functions_for_tier(1) == TIER_BASIC

    def test_level_2_is_basic_plus_detail(self):
        """Level 2 returns Basic + Detail."""
        assert functions_for_tier(2) == TIER_BASIC | TIER_DETAIL

    def test_level_3_is_basic_plus_detail_plus_verbose(self):
        """Level 3 returns Basic + Detail + Verbose."""
        assert functions_for_tier(3) == (
            TIER_BASIC | TIER_DETAIL | TIER_VERBOSE)

    def test_cumulative_containment(self):
        """Each higher tier is a superset of the lower tiers."""
        t1 = functions_for_tier(1)
        t2 = functions_for_tier(2)
        t3 = functions_for_tier(3)
        assert t1 < t2
        assert t2 < t3

    def test_manual_never_in_tier(self):
        """Manual functions are never included in any tier."""
        for level in (1, 2, 3):
            assert not (functions_for_tier(level) & TIER_MANUAL)

    def test_returns_frozenset(self):
        """functions_for_tier() returns a frozenset."""
        result = functions_for_tier(1)
        assert isinstance(result, frozenset)


# ---------------------------------------------------------------------------
# TestComputeTierSwitch
# ---------------------------------------------------------------------------

class TestComputeTierSwitch:
    """Tests for compute_tier_switch()."""

    def test_basic_to_detail(self):
        """Switching 1->2 enables Detail functions."""
        to_enable, to_disable = compute_tier_switch(1, 2)
        assert to_enable == TIER_DETAIL
        assert to_disable == frozenset()

    def test_detail_to_basic(self):
        """Switching 2->1 disables Detail functions."""
        to_enable, to_disable = compute_tier_switch(2, 1)
        assert to_enable == frozenset()
        assert to_disable == TIER_DETAIL

    def test_basic_to_verbose(self):
        """Switching 1->3 enables Detail + Verbose."""
        to_enable, to_disable = compute_tier_switch(1, 3)
        assert to_enable == TIER_DETAIL | TIER_VERBOSE
        assert to_disable == frozenset()

    def test_verbose_to_basic(self):
        """Switching 3->1 disables Detail + Verbose."""
        to_enable, to_disable = compute_tier_switch(3, 1)
        assert to_enable == frozenset()
        assert to_disable == TIER_DETAIL | TIER_VERBOSE

    def test_detail_to_verbose(self):
        """Switching 2->3 enables Verbose functions."""
        to_enable, to_disable = compute_tier_switch(2, 3)
        assert to_enable == TIER_VERBOSE
        assert to_disable == frozenset()

    def test_verbose_to_detail(self):
        """Switching 3->2 disables Verbose functions."""
        to_enable, to_disable = compute_tier_switch(3, 2)
        assert to_enable == frozenset()
        assert to_disable == TIER_VERBOSE

    def test_same_tier_noop(self):
        """Switching to the same tier with no manual overrides
        produces empty lists."""
        to_enable, to_disable = compute_tier_switch(2, 2)
        assert to_enable == frozenset()
        assert to_disable == frozenset()

    def test_same_tier_noop_all_levels(self):
        """Same-tier switch is a noop for all levels."""
        for level in (1, 2, 3):
            to_enable, to_disable = compute_tier_switch(level, level)
            assert to_enable == frozenset()
            assert to_disable == frozenset()

    def test_manual_additions_cleared(self):
        """Manual additions are part of old_effective but not new."""
        # On Basic with AllocMem manually enabled, switch to Detail
        to_enable, to_disable = compute_tier_switch(
            1, 2, manual_additions={"AllocMem"})
        # AllocMem should be disabled (not in Detail tier)
        assert "AllocMem" in to_disable

    def test_manual_removals_cleared(self):
        """Manual removals are removed from old_effective."""
        # On Detail with Examine manually disabled, switch to Basic.
        # Examine is in Detail (not Basic), so it would normally be
        # disabled. But it was already manually removed from
        # old_effective, so it's not in to_disable.
        to_enable, to_disable = compute_tier_switch(
            2, 1, manual_removals={"Examine"})
        assert "Examine" not in to_disable

    def test_manual_addition_with_same_tier(self):
        """Switching same tier with manual addition disables the addition."""
        to_enable, to_disable = compute_tier_switch(
            1, 1, manual_additions={"AllocMem"})
        # Switching to same tier clears manual overrides, so AllocMem
        # (which is not in Basic) should be disabled.
        assert "AllocMem" in to_disable
        assert to_enable == frozenset()

    def test_manual_removal_with_same_tier(self):
        """Switching same tier with manual removal re-enables the removed func."""
        to_enable, to_disable = compute_tier_switch(
            1, 1, manual_removals={"Open"})
        # Open is in Basic, was manually removed. Switching to same
        # tier clears overrides, so Open gets re-enabled.
        assert "Open" in to_enable
        assert to_disable == frozenset()

    def test_no_manual_overrides(self):
        """Passing None for manual overrides works correctly."""
        to_enable, to_disable = compute_tier_switch(
            1, 2, manual_additions=None, manual_removals=None)
        assert to_enable == TIER_DETAIL
        assert to_disable == frozenset()


# ---------------------------------------------------------------------------
# TestTierForFunction
# ---------------------------------------------------------------------------

class TestTierForFunction:
    """Tests for tier_for_function()."""

    def test_basic_function(self):
        """Basic-tier function returns 1."""
        assert tier_for_function("Open") == 1
        assert tier_for_function("OpenLibrary") == 1
        assert tier_for_function("socket") == 1

    def test_detail_function(self):
        """Detail-tier function returns 2."""
        assert tier_for_function("Examine") == 2
        assert tier_for_function("CloseLibrary") == 2
        assert tier_for_function("sendto") == 2

    def test_verbose_function(self):
        """Verbose-tier function returns 3."""
        assert tier_for_function("ExNext") == 3

    def test_manual_function(self):
        """Manual-tier function returns None."""
        assert tier_for_function("AllocMem") is None
        assert tier_for_function("FreeMem") is None
        assert tier_for_function("Wait") is None
        assert tier_for_function("Read") is None
        assert tier_for_function("ReplyMsg") is None
        assert tier_for_function("send") is None
        assert tier_for_function("recv") is None
        assert tier_for_function("WaitSelect") is None

    def test_unknown_function(self):
        """Unknown function returns None."""
        assert tier_for_function("NotARealFunction") is None

    def test_all_basic_return_1(self):
        """Every function in TIER_BASIC returns 1."""
        for func in TIER_BASIC:
            assert tier_for_function(func) == 1, \
                "{} should be tier 1".format(func)

    def test_all_detail_return_2(self):
        """Every function in TIER_DETAIL returns 2."""
        for func in TIER_DETAIL:
            assert tier_for_function(func) == 2, \
                "{} should be tier 2".format(func)

    def test_all_verbose_return_3(self):
        """Every function in TIER_VERBOSE returns 3."""
        for func in TIER_VERBOSE:
            assert tier_for_function(func) == 3, \
                "{} should be tier 3".format(func)

    def test_all_manual_return_none(self):
        """Every function in TIER_MANUAL returns None."""
        for func in TIER_MANUAL:
            assert tier_for_function(func) is None, \
                "{} should be None".format(func)


# ---------------------------------------------------------------------------
# TestDetectTier
# ---------------------------------------------------------------------------

class TestDetectTier:
    """Tests for detect_tier()."""

    def test_exact_basic(self):
        """Exact Basic set detects as tier 1."""
        assert detect_tier(TIER_BASIC) == 1

    def test_exact_detail(self):
        """Exact Detail cumulative set detects as tier 2."""
        assert detect_tier(TIER_BASIC | TIER_DETAIL) == 2

    def test_exact_verbose(self):
        """Exact Verbose cumulative set detects as tier 3."""
        assert detect_tier(TIER_BASIC | TIER_DETAIL | TIER_VERBOSE) == 3

    def test_basic_plus_manual_no_match(self):
        """Basic + a manual function does not match any tier."""
        assert detect_tier(TIER_BASIC | {"AllocMem"}) is None

    def test_basic_minus_one_no_match(self):
        """Basic minus one function does not match any tier."""
        subset = TIER_BASIC - {"Open"}
        assert detect_tier(subset) is None

    def test_empty_no_match(self):
        """Empty set does not match any tier."""
        assert detect_tier(frozenset()) is None

    def test_all_functions_no_match(self):
        """All 80 functions do not match any tier (includes Manual)."""
        assert detect_tier(_ALL_FUNCTIONS) is None

    def test_accepts_regular_set(self):
        """detect_tier() accepts a regular set, not just frozenset."""
        assert detect_tier(set(TIER_BASIC)) == 1


# ---------------------------------------------------------------------------
# TestTierName
# ---------------------------------------------------------------------------

class TestTierName:
    """Tests for tier_name()."""

    def test_basic(self):
        assert tier_name(1) == "basic"

    def test_detail(self):
        assert tier_name(2) == "detail"

    def test_verbose(self):
        assert tier_name(3) == "verbose"

    def test_unknown_level(self):
        """Unknown level returns formatted fallback."""
        assert tier_name(99) == "tier-99"

    def test_tier_names_dict(self):
        """TIER_NAMES dict matches tier_name() output."""
        for level, name in TIER_NAMES.items():
            assert tier_name(level) == name
