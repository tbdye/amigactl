"""Integration tests for atrace test execution app.

These tests run the atrace_test binary on the Amiga via TRACE RUN and
validate that every traced function produces the correct wire format:
args, retval, and status fields.

The test app (C:atrace_test) calls each of the 50 traced functions
(except AddDosEntry) with distinctive, predictable inputs.  Multiple
fixtures run the app in different configurations:

  trace_events: Module-scoped.  Runs with default settings (noise
    functions disabled).  Captures the 31 non-noise functions.  Used by
    TestExecFunctions, TestDosFunctions,
    TestIntuitionFunctions, TestFieldInvariants, and
    TestPhase4bFeatures.

  noise_group1_events: Class-scoped.  Enables FindPort, FindSemaphore,
    FindTask only.  Used by TestExecNoiseGroup1.

  noise_group2_events: Class-scoped.  Enables GetMsg, PutMsg only.
    Used by TestExecNoiseGroup2.

  noise_group3_events: Class-scoped.  Enables ObtainSemaphore,
    ReleaseSemaphore, AllocMem only.  Used by TestExecNoiseGroup3.

  noise_group4_events: Class-scoped.  Enables OpenLibrary only.
    Used by TestExecNoiseGroup4.

  noise_group5_events: Class-scoped.  Enables FreeMem, AllocVec,
    FreeVec only.  Used by TestExecNoiseGroup5.

  noise_group6_events: Class-scoped.  Enables Read, Write only.
    Used by TestDosNoiseGroup6.

  noise_group7_events: Class-scoped.  Enables DoIO, SendIO, WaitIO,
    AbortIO, CheckIO only.  Used by TestExecDeviceIO.

The noise functions are split into seven groups (at most 5 stubs each)
to keep the background OS event rate low enough that the 8192-entry
ring buffer does not overflow during trace_run.

All tests in this module are skipped if the target does not have
atrace loaded.
"""

import re
import signal
import sys
from collections import Counter

import pytest

from amigactl import AmigaConnection
from amigactl.trace_ui import SegmentResolver


# ---------------------------------------------------------------------------
# Timeout helper
# ---------------------------------------------------------------------------

def _timeout_handler(signum, frame):
    raise TimeoutError("trace fixture timed out after 60 seconds")


# ---------------------------------------------------------------------------
# Module-level fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True, scope="module")
def require_atrace_for_app(request):
    """Skip all tests in this module if atrace is not loaded."""
    host = request.config.getoption("--host")
    port = request.config.getoption("--port")
    conn = AmigaConnection(host, port)
    conn.connect()
    try:
        status = conn.trace_status()
        if not status.get("loaded"):
            pytest.skip("atrace not loaded on target")
    except Exception:
        pytest.skip("atrace not available (connection or command failed)")
    finally:
        conn.close()


@pytest.fixture(scope="module")
def trace_events(request, require_atrace_for_app):
    """Run atrace_test via TRACE RUN, collect all events.

    Depends on require_atrace_for_app to ensure atrace is loaded before
    running the expensive test app.  Without this, a missing atrace
    installation would cause a confusing connection-level failure instead
    of a clean skip.

    Noise functions remain at their default (disabled) state.  Only the
    31 non-noise functions produce events in this fixture.  Noise
    function tests use the separate noise_group1-7_events fixtures.

    Uses signal.alarm() for a 60-second external timeout.  This is
    necessary because trace_run() calls settimeout(None) internally,
    making socket-level timeouts ineffective.  If the test app hangs or
    the daemon stalls, the SIGALRM will raise an exception and prevent
    the test suite from blocking indefinitely.

    Includes Bug #9 diagnostic instrumentation: logs daemon state before
    and after the TRACE RUN to stderr (prefix "Bug9 diag: ") for
    diagnosing event loss in combined suite runs.
    """
    host = request.config.getoption("--host")
    port = request.config.getoption("--port")
    conn = AmigaConnection(host, port)
    conn.connect()
    try:
        return _trace_events_inner(conn)
    finally:
        conn.close()


def _trace_events_inner(conn):
    """Guts of trace_events, factored out for clean conn.close() in finally."""
    # --- Bug9 diag: pre-run status ---
    noise_set = set(_NOISE_FUNCS)
    pre_status = conn.trace_status()

    print("Bug9 diag: === pre-run trace_status ===", file=sys.stderr)
    print("Bug9 diag: enabled={}".format(
        pre_status.get("enabled")), file=sys.stderr)
    for key in ("events_produced", "events_consumed", "events_dropped",
                "buffer_used", "buffer_capacity"):
        if key in pre_status:
            print("Bug9 diag: {}={}".format(key, pre_status[key]),
                  file=sys.stderr)
    if "filter_task" in pre_status:
        print("Bug9 diag: filter_task={}".format(
            pre_status["filter_task"]), file=sys.stderr)
    if "noise_disabled" in pre_status:
        print("Bug9 diag: noise_disabled={}".format(
            pre_status["noise_disabled"]), file=sys.stderr)

    # Log per-function state: report any non-noise functions that are
    # disabled or noise functions that are unexpectedly enabled.
    patch_list = pre_status.get("patch_list", [])
    disabled_non_noise = []
    enabled_noise = []
    for entry in patch_list:
        bare = entry.get("name", "").split(".", 1)[-1]
        is_enabled = entry.get("enabled", True)
        if bare in noise_set and is_enabled:
            enabled_noise.append(bare)
        elif bare not in noise_set and not is_enabled:
            disabled_non_noise.append(bare)
    if disabled_non_noise or enabled_noise:
        if disabled_non_noise:
            print("Bug9 diag: DISABLED non-noise functions: {}".format(
                ", ".join(disabled_non_noise)), file=sys.stderr)
        if enabled_noise:
            print("Bug9 diag: ENABLED noise functions: {}".format(
                ", ".join(enabled_noise)), file=sys.stderr)
    else:
        print("Bug9 diag: function state: all defaults", file=sys.stderr)

    # --- Bug9 diag: trace_run with try/except ---
    old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(60)
    try:
        events = []

        def collect(ev):
            events.append(ev)

        try:
            result = conn.trace_run("C:atrace_test", collect)
        except Exception as exc:
            print("Bug9 diag: trace_run() raised {}: {}".format(
                type(exc).__name__, exc), file=sys.stderr)
            raise
        assert result["rc"] == 0, (
            "atrace_test exited with rc={}".format(result["rc"]))
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)

    # --- Bug9 diag: post-run status ---
    post_status = conn.trace_status()

    print("Bug9 diag: === post-run trace_status ===", file=sys.stderr)
    for key in ("events_produced", "events_consumed", "events_dropped",
                "buffer_used"):
        if key in post_status:
            pre_val = pre_status.get(key, 0)
            post_val = post_status[key]
            print("Bug9 diag: {}={} (delta={})".format(
                key, post_val, post_val - pre_val), file=sys.stderr)

    # Log raw event counts
    event_entries = [e for e in events if e.get("type") == "event"]
    comment_entries = [e for e in events if e.get("type") != "event"]
    print("Bug9 diag: raw entries collected: {}".format(
        len(events)), file=sys.stderr)
    print("Bug9 diag: event entries: {}, comment entries: {}".format(
        len(event_entries), len(comment_entries)), file=sys.stderr)

    # Log any comment entries (may contain daemon-side messages)
    for ce in comment_entries:
        print("Bug9 diag: comment: {}".format(
            ce.get("text", ce.get("raw", "?"))), file=sys.stderr)

    # Log per-function event counts
    func_counts = Counter(e.get("func", "?") for e in event_entries)
    if func_counts:
        summary = ", ".join("{}={}".format(f, c)
                            for f, c in sorted(func_counts.items()))
        print("Bug9 diag: per-function counts: {}".format(
            summary), file=sys.stderr)

    # Log sequence number range (gaps indicate truncation)
    if event_entries:
        seqs = [e.get("seq", 0) for e in event_entries]
        print("Bug9 diag: seq range: first={}, last={}, count={}".format(
            min(seqs), max(seqs), len(seqs)), file=sys.stderr)

    return event_entries


# The 19 noise functions that are disabled by default due to high
# event volume from OS-internal activity.
_NOISE_FUNCS = [
    "FindPort", "FindSemaphore", "FindTask", "GetMsg", "PutMsg",
    "ObtainSemaphore", "ReleaseSemaphore", "AllocMem",
    "OpenLibrary",
    # Phase 5 additions
    "FreeMem", "AllocVec", "FreeVec",
    "Read", "Write",
    # Phase 5 device I/O additions
    "DoIO", "SendIO", "WaitIO", "AbortIO", "CheckIO",
]


_NOISE_GROUP1 = ["FindPort", "FindSemaphore", "FindTask"]
_NOISE_GROUP2 = ["GetMsg", "PutMsg"]
_NOISE_GROUP3 = ["ObtainSemaphore", "ReleaseSemaphore", "AllocMem"]
_NOISE_GROUP4 = ["OpenLibrary"]
_NOISE_GROUP5 = ["FreeMem", "AllocVec", "FreeVec"]
_NOISE_GROUP6 = ["Read", "Write"]
_NOISE_GROUP7 = ["DoIO", "SendIO", "WaitIO", "AbortIO", "CheckIO"]


@pytest.fixture(scope="class")
def noise_group1_events(request, require_atrace_for_app):
    """Run atrace_test with only Group 1 noise functions enabled."""
    host = request.config.getoption("--host")
    port = request.config.getoption("--port")
    conn = AmigaConnection(host, port)
    conn.connect()

    noise_funcs = _NOISE_GROUP1
    status = conn.trace_status()
    patch_list = status.get("patch_list", [])
    noise_set = set(_NOISE_FUNCS)   # all 19
    # All non-noise function names -- used for unconditional restore
    all_non_noise = []
    for entry in patch_list:
        bare = entry.get("name", "").split(".", 1)[-1]
        if bare not in noise_set:
            all_non_noise.append(bare)

    old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(60)
    try:
        # Disable ALL non-noise functions
        if all_non_noise:
            conn.trace_disable(all_non_noise)

        # Disable any noise functions NOT in this group
        other_noise = [f for f in _NOISE_FUNCS if f not in noise_funcs]
        conn.trace_disable(other_noise)

        # Enable ONLY this group's functions
        conn.trace_enable(noise_funcs)

        events = []
        def collect(ev):
            events.append(ev)
        result = conn.trace_run("C:atrace_test", collect)
        assert result["rc"] == 0, \
            "atrace_test exited with rc={}".format(result["rc"])
        return [e for e in events if e.get("type") == "event"]
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)
        # Restore: disable this group's noise, re-enable ALL non-noise
        try:
            conn.trace_disable(noise_funcs)
        except Exception:
            pass
        if all_non_noise:
            try:
                conn.trace_enable(all_non_noise)
            except Exception:
                pass
        conn.close()


@pytest.fixture(scope="class")
def noise_group2_events(request, require_atrace_for_app):
    """Run atrace_test with only Group 2 noise functions enabled."""
    host = request.config.getoption("--host")
    port = request.config.getoption("--port")
    conn = AmigaConnection(host, port)
    conn.connect()

    noise_funcs = _NOISE_GROUP2
    status = conn.trace_status()
    patch_list = status.get("patch_list", [])
    noise_set = set(_NOISE_FUNCS)
    all_non_noise = []
    for entry in patch_list:
        bare = entry.get("name", "").split(".", 1)[-1]
        if bare not in noise_set:
            all_non_noise.append(bare)

    old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(60)
    try:
        if all_non_noise:
            conn.trace_disable(all_non_noise)
        other_noise = [f for f in _NOISE_FUNCS if f not in noise_funcs]
        conn.trace_disable(other_noise)
        conn.trace_enable(noise_funcs)

        events = []
        def collect(ev):
            events.append(ev)
        result = conn.trace_run("C:atrace_test", collect)
        assert result["rc"] == 0, \
            "atrace_test exited with rc={}".format(result["rc"])
        return [e for e in events if e.get("type") == "event"]
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)
        try:
            conn.trace_disable(noise_funcs)
        except Exception:
            pass
        if all_non_noise:
            try:
                conn.trace_enable(all_non_noise)
            except Exception:
                pass
        conn.close()


@pytest.fixture(scope="class")
def noise_group3_events(request, require_atrace_for_app):
    """Run atrace_test with only Group 3 noise functions enabled."""
    host = request.config.getoption("--host")
    port = request.config.getoption("--port")
    conn = AmigaConnection(host, port)
    conn.connect()

    noise_funcs = _NOISE_GROUP3
    status = conn.trace_status()
    patch_list = status.get("patch_list", [])
    noise_set = set(_NOISE_FUNCS)
    all_non_noise = []
    for entry in patch_list:
        bare = entry.get("name", "").split(".", 1)[-1]
        if bare not in noise_set:
            all_non_noise.append(bare)

    old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(60)
    try:
        if all_non_noise:
            conn.trace_disable(all_non_noise)
        other_noise = [f for f in _NOISE_FUNCS if f not in noise_funcs]
        conn.trace_disable(other_noise)
        conn.trace_enable(noise_funcs)

        events = []
        def collect(ev):
            events.append(ev)
        result = conn.trace_run("C:atrace_test", collect)
        assert result["rc"] == 0, \
            "atrace_test exited with rc={}".format(result["rc"])
        return [e for e in events if e.get("type") == "event"]
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)
        try:
            conn.trace_disable(noise_funcs)
        except Exception:
            pass
        if all_non_noise:
            try:
                conn.trace_enable(all_non_noise)
            except Exception:
                pass
        conn.close()


@pytest.fixture(scope="class")
def noise_group4_events(request, require_atrace_for_app):
    """Run atrace_test with only Group 4 noise functions enabled."""
    host = request.config.getoption("--host")
    port = request.config.getoption("--port")
    conn = AmigaConnection(host, port)
    conn.connect()

    noise_funcs = _NOISE_GROUP4
    status = conn.trace_status()
    patch_list = status.get("patch_list", [])
    noise_set = set(_NOISE_FUNCS)
    all_non_noise = []
    for entry in patch_list:
        bare = entry.get("name", "").split(".", 1)[-1]
        if bare not in noise_set:
            all_non_noise.append(bare)

    old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(60)
    try:
        if all_non_noise:
            conn.trace_disable(all_non_noise)
        other_noise = [f for f in _NOISE_FUNCS if f not in noise_funcs]
        conn.trace_disable(other_noise)
        conn.trace_enable(noise_funcs)

        events = []
        def collect(ev):
            events.append(ev)
        result = conn.trace_run("C:atrace_test", collect)
        assert result["rc"] == 0, \
            "atrace_test exited with rc={}".format(result["rc"])
        return [e for e in events if e.get("type") == "event"]
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)
        try:
            conn.trace_disable(noise_funcs)
        except Exception:
            pass
        if all_non_noise:
            try:
                conn.trace_enable(all_non_noise)
            except Exception:
                pass
        conn.close()


@pytest.fixture(scope="class")
def noise_group5_events(request, require_atrace_for_app):
    """Run atrace_test with only Group 5 noise functions enabled."""
    host = request.config.getoption("--host")
    port = request.config.getoption("--port")
    conn = AmigaConnection(host, port)
    conn.connect()

    noise_funcs = _NOISE_GROUP5
    status = conn.trace_status()
    patch_list = status.get("patch_list", [])
    noise_set = set(_NOISE_FUNCS)
    all_non_noise = []
    for entry in patch_list:
        bare = entry.get("name", "").split(".", 1)[-1]
        if bare not in noise_set:
            all_non_noise.append(bare)

    old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(60)
    try:
        if all_non_noise:
            conn.trace_disable(all_non_noise)
        other_noise = [f for f in _NOISE_FUNCS if f not in noise_funcs]
        conn.trace_disable(other_noise)
        conn.trace_enable(noise_funcs)

        events = []
        def collect(ev):
            events.append(ev)
        result = conn.trace_run("C:atrace_test", collect)
        assert result["rc"] == 0, \
            "atrace_test exited with rc={}".format(result["rc"])
        return [e for e in events if e.get("type") == "event"]
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)
        try:
            conn.trace_disable(noise_funcs)
        except Exception:
            pass
        if all_non_noise:
            try:
                conn.trace_enable(all_non_noise)
            except Exception:
                pass
        conn.close()


@pytest.fixture(scope="class")
def noise_group6_events(request, require_atrace_for_app):
    """Run atrace_test with only Group 6 noise functions enabled."""
    host = request.config.getoption("--host")
    port = request.config.getoption("--port")
    conn = AmigaConnection(host, port)
    conn.connect()

    noise_funcs = _NOISE_GROUP6
    status = conn.trace_status()
    patch_list = status.get("patch_list", [])
    noise_set = set(_NOISE_FUNCS)
    all_non_noise = []
    for entry in patch_list:
        bare = entry.get("name", "").split(".", 1)[-1]
        if bare not in noise_set:
            all_non_noise.append(bare)

    old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(60)
    try:
        if all_non_noise:
            conn.trace_disable(all_non_noise)
        other_noise = [f for f in _NOISE_FUNCS if f not in noise_funcs]
        conn.trace_disable(other_noise)
        conn.trace_enable(noise_funcs)

        events = []
        def collect(ev):
            events.append(ev)
        result = conn.trace_run("C:atrace_test", collect)
        assert result["rc"] == 0, \
            "atrace_test exited with rc={}".format(result["rc"])
        return [e for e in events if e.get("type") == "event"]
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)
        try:
            conn.trace_disable(noise_funcs)
        except Exception:
            pass
        if all_non_noise:
            try:
                conn.trace_enable(all_non_noise)
            except Exception:
                pass
        conn.close()


@pytest.fixture(scope="class")
def noise_group7_events(request, require_atrace_for_app):
    """Run atrace_test with only Group 7 noise functions enabled."""
    host = request.config.getoption("--host")
    port = request.config.getoption("--port")
    conn = AmigaConnection(host, port)
    conn.connect()

    noise_funcs = _NOISE_GROUP7
    status = conn.trace_status()
    patch_list = status.get("patch_list", [])
    noise_set = set(_NOISE_FUNCS)
    all_non_noise = []
    for entry in patch_list:
        bare = entry.get("name", "").split(".", 1)[-1]
        if bare not in noise_set:
            all_non_noise.append(bare)

    old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(60)
    try:
        if all_non_noise:
            conn.trace_disable(all_non_noise)
        other_noise = [f for f in _NOISE_FUNCS if f not in noise_funcs]
        conn.trace_disable(other_noise)
        conn.trace_enable(noise_funcs)

        events = []
        def collect(ev):
            events.append(ev)
        result = conn.trace_run("C:atrace_test", collect)
        assert result["rc"] == 0, \
            "atrace_test exited with rc={}".format(result["rc"])
        return [e for e in events if e.get("type") == "event"]
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)
        try:
            conn.trace_disable(noise_funcs)
        except Exception:
            pass
        if all_non_noise:
            try:
                conn.trace_enable(all_non_noise)
            except Exception:
                pass
        conn.close()


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _find_events(events, func_name, args_contains=None):
    """Find events matching func name and optional args substring."""
    result = []
    for ev in events:
        if ev.get("func") != func_name:
            continue
        if args_contains is not None and args_contains not in ev.get("args", ""):
            continue
        result.append(ev)
    return result


# Regex for matching a hex pointer (8 hex digits)
_HEX_PTR = re.compile(r"^0x[0-9a-f]{8}$")


# ---------------------------------------------------------------------------
# TestExecFunctions
# ---------------------------------------------------------------------------

class TestExecFunctions:
    """Tests for the 4 non-noise exec.library traced functions."""

    def test_findresident(self, trace_events):
        """FindResident("dos.library") -- always present, status O."""
        matches = _find_events(trace_events, "FindResident", '"dos.library"')
        assert len(matches) >= 1, (
            "No FindResident('dos.library') event found in {} events".format(
                len(trace_events)))
        ev = matches[0]
        assert ev["lib"] == "exec"
        assert ev["status"] == "O", (
            "dos.library should be resident, got status={} retval={}".format(
                ev["status"], ev["retval"]))
        assert ev["retval"] == "OK", (
            "Expected retval 'OK', got: {}".format(ev["retval"]))

    def test_opendevice(self, trace_events):
        """OpenDevice("timer.device", unit=0) -- status O, retval OK."""
        matches = _find_events(trace_events, "OpenDevice", '"timer.device"')
        # Further filter for unit=0 (UNIT_MICROHZ)
        matches = [ev for ev in matches if ",unit=0" in ev.get("args", "")]
        assert len(matches) >= 1, (
            "No OpenDevice('timer.device', unit=0) event found in {} events"
            .format(len(trace_events)))
        ev = matches[0]
        assert ev["lib"] == "exec"
        assert ev["status"] == "O", (
            "OpenDevice should succeed, got status={} retval={}".format(
                ev["status"], ev["retval"]))
        assert ev["retval"] == "OK", (
            "Expected retval 'OK', got: {}".format(ev["retval"]))

    def test_openresource(self, trace_events):
        """OpenResource("FileSystem.resource") -- always present, status O."""
        matches = _find_events(
            trace_events, "OpenResource", '"FileSystem.resource"')
        assert len(matches) >= 1, (
            "No OpenResource('FileSystem.resource') event found in {} events"
            .format(len(trace_events)))
        ev = matches[0]
        assert ev["lib"] == "exec"
        assert ev["status"] == "O", (
            "OpenResource should succeed, got status={} retval={}".format(
                ev["status"], ev["retval"]))
        assert ev["retval"] == "OK", (
            "Expected retval 'OK', got: {}".format(ev["retval"]))


# ---------------------------------------------------------------------------
# TestExecNoiseGroup1 -- lookup functions (FindPort, FindSemaphore, FindTask)
# ---------------------------------------------------------------------------

class TestExecNoiseGroup1:
    """Lookup functions: FindPort, FindSemaphore, FindTask."""

    def test_findport(self, noise_group1_events):
        """FindPort("AMITCP") -- status/retval consistency."""
        matches = _find_events(noise_group1_events, "FindPort", '"AMITCP"')
        assert len(matches) >= 1, (
            "No FindPort('AMITCP') event found in {} events".format(
                len(noise_group1_events)))
        ev = matches[0]
        assert ev["lib"] == "exec"
        # AMITCP port may or may not exist; assert consistency
        if ev["status"] == "O":
            assert ev["retval"] == "OK", (
                "FindPort OK but retval not 'OK': {}".format(ev["retval"]))
        else:
            assert ev["status"] == "E"
            assert ev["retval"] == "NULL"

    def test_findsemaphore(self, noise_group1_events):
        """FindSemaphore("atrace_patches") -- atrace loaded, status O."""
        matches = _find_events(
            noise_group1_events, "FindSemaphore", '"atrace_patches"')
        assert len(matches) >= 1, (
            "No FindSemaphore('atrace_patches') event found in {} events"
            .format(len(noise_group1_events)))
        ev = matches[0]
        assert ev["lib"] == "exec"
        assert ev["status"] == "O", (
            "atrace_patches semaphore should exist, got status={} retval={}"
            .format(ev["status"], ev["retval"]))
        assert ev["retval"] == "OK", (
            "Expected retval 'OK', got: {}".format(ev["retval"]))

    def test_findtask_self(self, noise_group1_events):
        """FindTask(NULL) -- self-lookup, always succeeds."""
        matches = _find_events(noise_group1_events, "FindTask", "NULL (self)")
        assert len(matches) >= 1, (
            "No FindTask('NULL (self)') event found in {} events".format(
                len(noise_group1_events)))
        ev = matches[0]
        assert ev["lib"] == "exec"
        assert ev["status"] == "O", (
            "FindTask(NULL) should succeed, got status={} retval={}".format(
                ev["status"], ev["retval"]))
        assert ev["retval"] == "OK", (
            "Expected retval 'OK', got: {}".format(ev["retval"]))


# ---------------------------------------------------------------------------
# TestExecNoiseGroup2 -- message functions (GetMsg, PutMsg)
# ---------------------------------------------------------------------------

class TestExecNoiseGroup2:
    """Message functions: GetMsg, PutMsg."""

    def test_getmsg_empty(self, noise_group2_events):
        """GetMsg on empty port -- retval (empty), status -."""
        # Find GetMsg events returning (empty).  There may be many from
        # noise (OS internals), but at least one must exist from our test.
        matches = _find_events(noise_group2_events, "GetMsg")
        empty_matches = [ev for ev in matches
                         if ev.get("retval") == "(empty)"]
        assert len(empty_matches) >= 1, (
            "No GetMsg with retval='(empty)' found in {} GetMsg events"
            .format(len(matches)))
        ev = empty_matches[0]
        assert ev["lib"] == "exec"
        assert ev["status"] == "-", (
            "GetMsg(empty) should have status '-', got: {}".format(
                ev["status"]))

    def test_putmsg_getmsg_pair(self, noise_group2_events):
        """PutMsg then GetMsg -- correct retval/status and sequence."""
        # Find PutMsg events (void return)
        putmsg_events = _find_events(noise_group2_events, "PutMsg")
        assert len(putmsg_events) >= 1, (
            "No PutMsg events found in {} events".format(
                len(noise_group2_events)))

        # Find GetMsg events with non-empty return (message retrieved)
        getmsg_events = _find_events(noise_group2_events, "GetMsg")
        getmsg_ok = [ev for ev in getmsg_events
                     if ev.get("status") == "O"
                     and _HEX_PTR.match(ev.get("retval", ""))]
        assert len(getmsg_ok) >= 1, (
            "No GetMsg with status=O and hex retval found in {} GetMsg events"
            .format(len(getmsg_events)))

        # Check PutMsg has void return
        # Among all PutMsg events, find ones from our test by checking
        # for port=0x and msg=0x in args
        test_putmsgs = [ev for ev in putmsg_events
                        if "port=0x" in ev.get("args", "")
                        and "msg=0x" in ev.get("args", "")]
        assert len(test_putmsgs) >= 1, (
            "No PutMsg with port and msg args found")
        pm = test_putmsgs[0]
        assert pm["retval"] == "(void)", (
            "PutMsg retval should be '(void)', got: {}".format(pm["retval"]))
        assert pm["status"] == "-", (
            "PutMsg status should be '-', got: {}".format(pm["status"]))

        # Verify sequence: find a PutMsg/GetMsg pair sharing the same port
        # address where PutMsg comes first
        for pm_ev in test_putmsgs:
            # Extract port address from PutMsg args: "port=0x<addr>,msg=0x..."
            pm_args = pm_ev.get("args", "")
            port_match = re.search(r"port=(0x[0-9a-f]+)", pm_args)
            if not port_match:
                continue
            port_addr = port_match.group(1)
            # Find GetMsg events on the same port with status O
            paired_getmsgs = [
                ev for ev in getmsg_ok
                if "port={}".format(port_addr) in ev.get("args", "")
                and ev["seq"] > pm_ev["seq"]
            ]
            if paired_getmsgs:
                gm = paired_getmsgs[0]
                assert gm["status"] == "O"
                assert _HEX_PTR.match(gm["retval"])
                return  # Found valid pair
        # If we get here, we couldn't find a port-matched pair.
        # Fall back to sequence-only: any PutMsg before any GetMsg(O)
        assert test_putmsgs[0]["seq"] < getmsg_ok[0]["seq"], (
            "PutMsg (seq={}) should precede GetMsg (seq={})".format(
                test_putmsgs[0]["seq"], getmsg_ok[0]["seq"]))


# ---------------------------------------------------------------------------
# TestExecNoiseGroup3 -- semaphore + memory (ObtainSemaphore, ReleaseSemaphore, AllocMem)
# ---------------------------------------------------------------------------

class TestExecNoiseGroup3:
    """Semaphore + memory functions: ObtainSemaphore, ReleaseSemaphore, AllocMem."""

    def test_obtain_release_semaphore(self, noise_group3_events):
        """ObtainSemaphore + ReleaseSemaphore -- void, paired, ordered."""
        obtain_events = _find_events(noise_group3_events, "ObtainSemaphore")
        release_events = _find_events(noise_group3_events, "ReleaseSemaphore")
        assert len(obtain_events) >= 1, (
            "No ObtainSemaphore events found in {} events".format(
                len(noise_group3_events)))
        assert len(release_events) >= 1, (
            "No ReleaseSemaphore events found in {} events".format(
                len(noise_group3_events)))

        # Both should be void
        for ev in obtain_events:
            assert ev["retval"] == "(void)", (
                "ObtainSemaphore retval should be '(void)', got: {}".format(
                    ev["retval"]))
            assert ev["status"] == "-", (
                "ObtainSemaphore status should be '-', got: {}".format(
                    ev["status"]))
        for ev in release_events:
            assert ev["retval"] == "(void)", (
                "ReleaseSemaphore retval should be '(void)', got: {}".format(
                    ev["retval"]))
            assert ev["status"] == "-", (
                "ReleaseSemaphore status should be '-', got: {}".format(
                    ev["status"]))

        # Find a matched pair sharing the same sem address
        for obt in obtain_events:
            sem_match = re.search(r"sem=(0x[0-9a-f]+)", obt.get("args", ""))
            if not sem_match:
                continue
            sem_addr = sem_match.group(1)
            paired = [ev for ev in release_events
                      if "sem={}".format(sem_addr) in ev.get("args", "")
                      and ev["seq"] > obt["seq"]]
            if paired:
                # Found valid Obtain/Release pair on the same semaphore
                return

        # Fallback: at minimum, some Obtain precedes some Release
        assert obtain_events[0]["seq"] < release_events[0]["seq"], (
            "ObtainSemaphore (seq={}) should precede ReleaseSemaphore "
            "(seq={})".format(
                obtain_events[0]["seq"], release_events[0]["seq"]))

    def test_allocmem(self, noise_group3_events):
        """AllocMem(1234, MEMF_PUBLIC|MEMF_CLEAR) -- distinctive size."""
        matches = _find_events(noise_group3_events, "AllocMem", "1234,")
        assert len(matches) >= 1, (
            "No AllocMem with size 1234 found in {} events".format(
                len(noise_group3_events)))
        ev = matches[0]
        assert ev["lib"] == "exec"
        assert "MEMF_PUBLIC" in ev["args"], (
            "Expected MEMF_PUBLIC in args, got: {}".format(ev["args"]))
        assert "MEMF_CLEAR" in ev["args"], (
            "Expected MEMF_CLEAR in args, got: {}".format(ev["args"]))
        assert ev["status"] == "O", (
            "AllocMem should succeed, got status={} retval={}".format(
                ev["status"], ev["retval"]))
        assert _HEX_PTR.match(ev["retval"]), (
            "Expected hex pointer retval, got: {}".format(ev["retval"]))


# ---------------------------------------------------------------------------
# TestExecNoiseGroup4 -- library open (OpenLibrary)
# ---------------------------------------------------------------------------

class TestExecNoiseGroup4:
    """OpenLibrary (noise group 4)."""

    def test_openlibrary(self, noise_group4_events):
        """OpenLibrary("dos.library", v0) -- always present, status O."""
        matches = _find_events(
            noise_group4_events, "OpenLibrary", '"dos.library",v0')
        assert len(matches) >= 1, (
            "No OpenLibrary('dos.library',v0) event found in {} events"
            .format(len(noise_group4_events)))
        ev = matches[0]
        assert ev["status"] == "O", (
            "OpenLibrary should succeed, got status={} retval={}".format(
                ev["status"], ev["retval"]))
        assert _HEX_PTR.match(ev["retval"]), (
            "Expected hex pointer retval, got: {}".format(ev["retval"]))


# ---------------------------------------------------------------------------
# TestDosFunctions (Phase A: 8 core tests)
# ---------------------------------------------------------------------------

class TestDosFunctions:
    """Tests for dos.library traced functions (Phase A subset)."""

    def test_open_read_success(self, trace_events):
        """Open("RAM:atrace_test_read", Read) -- file exists, status O."""
        matches = _find_events(
            trace_events, "Open", '"RAM:atrace_test_read",Read')
        assert len(matches) >= 1, (
            "No Open('RAM:atrace_test_read',Read) event found in {} events"
            .format(len(trace_events)))
        ev = matches[0]
        assert ev["lib"] == "dos"
        assert ev["status"] == "O", (
            "Open(Read) should succeed, got status={} retval={}".format(
                ev["status"], ev["retval"]))
        assert _HEX_PTR.match(ev["retval"]), (
            "Expected hex pointer retval, got: {}".format(ev["retval"]))

    def test_open_read_failure(self, trace_events):
        """Open("RAM:atrace_test_nofile", Read) -- no file, status E."""
        matches = _find_events(
            trace_events, "Open", '"RAM:atrace_test_nofile"')
        assert len(matches) >= 1, (
            "No Open('RAM:atrace_test_nofile') event found in {} events"
            .format(len(trace_events)))
        ev = matches[0]
        assert ev["lib"] == "dos"
        assert ev["retval"] == "NULL", (
            "Open(nonexistent) retval should be 'NULL', got: {}".format(
                ev["retval"]))
        assert ev["status"] == "E", (
            "Open(nonexistent) status should be 'E', got: {}".format(
                ev["status"]))

    def test_open_write_success(self, trace_events):
        """Open("RAM:atrace_test_write", Write) -- status O."""
        matches = _find_events(
            trace_events, "Open", '"RAM:atrace_test_write",Write')
        assert len(matches) >= 1, (
            "No Open('RAM:atrace_test_write',Write) event found in {} events"
            .format(len(trace_events)))
        ev = matches[0]
        assert ev["lib"] == "dos"
        assert ev["status"] == "O", (
            "Open(Write) should succeed, got status={} retval={}".format(
                ev["status"], ev["retval"]))
        assert _HEX_PTR.match(ev["retval"]), (
            "Expected hex pointer retval, got: {}".format(ev["retval"]))

    def test_close(self, trace_events):
        """Close(fh) -- after Open(Write), status O."""
        # Find the Open(Write) event for atrace_test_write to get its
        # sequence number, then find the next Close event after it.
        open_matches = _find_events(
            trace_events, "Open", '"RAM:atrace_test_write",Write')
        assert len(open_matches) >= 1, (
            "No Open('RAM:atrace_test_write',Write) found for Close test")
        open_seq = open_matches[0]["seq"]

        close_events = _find_events(trace_events, "Close")
        # Find the first Close after the Open(Write)
        subsequent = [ev for ev in close_events if ev["seq"] > open_seq]
        assert len(subsequent) >= 1, (
            "No Close event found after Open(Write) at seq={}; "
            "Close events: {}".format(
                open_seq, [(e["seq"], e["args"]) for e in close_events]))
        ev = subsequent[0]
        assert ev["lib"] == "dos"
        assert "fh=0x" in ev["args"], (
            "Close args should contain 'fh=0x', got: {}".format(ev["args"]))
        assert ev["retval"] == "OK", (
            "Close retval should be 'OK', got: {}".format(ev["retval"]))
        assert ev["status"] == "O", (
            "Close status should be 'O', got: {}".format(ev["status"]))

    def test_close_annotation(self, trace_events):
        """Close(fh) is annotated with the Open path by HandleResolver."""
        from amigactl.trace_ui import HandleResolver

        # Find Open("RAM:atrace_test_write",Write) and get its retval
        open_matches = _find_events(
            trace_events, "Open", '"RAM:atrace_test_write",Write')
        assert len(open_matches) >= 1
        open_retval = open_matches[0]["retval"]
        open_seq = open_matches[0]["seq"]

        # Build a HandleResolver, feed all events through it
        hr = HandleResolver()
        for ev in trace_events:
            hr.track(ev)

        # Find the Close that matches the Open's return handle
        close_events = _find_events(trace_events, "Close")
        norm_retval = HandleResolver._normalize_hex(open_retval)
        matching_close = [
            ev for ev in close_events
            if ev["seq"] > open_seq
            and "fh={}".format(norm_retval) in ev.get("args", "")]
        assert len(matching_close) >= 1
        annotation = hr.annotate(matching_close[0])
        assert annotation == "RAM:atrace_test_write", \
            "Expected 'RAM:atrace_test_write', got: {}".format(annotation)

    def test_lock(self, trace_events):
        """Lock("RAM:", Shared) -- status O."""
        matches = _find_events(trace_events, "Lock", '"RAM:",Shared')
        assert len(matches) >= 1, (
            "No Lock('RAM:',Shared) event found in {} events".format(
                len(trace_events)))
        # Filter for successful events -- internal DOS cleanup operations
        # (e.g. Lock during DeleteFile of nonexistent files) can produce
        # earlier Lock("RAM:",Shared) events with status E.
        ok_matches = [ev for ev in matches if ev["status"] == "O"]
        assert len(ok_matches) >= 1, (
            "No successful Lock('RAM:',Shared) event found. "
            "All Lock events: {}".format(
                [(e["seq"], e["retval"], e["status"]) for e in matches]))
        ev = ok_matches[0]
        assert ev["lib"] == "dos"
        assert _HEX_PTR.match(ev["retval"]), (
            "Expected hex pointer retval, got: {}".format(ev["retval"]))

    def test_deletefile(self, trace_events):
        """DeleteFile("RAM:atrace_test_delete") -- status O."""
        matches = _find_events(
            trace_events, "DeleteFile", '"RAM:atrace_test_delete"')
        assert len(matches) >= 1, (
            "No DeleteFile('RAM:atrace_test_delete') event found in "
            "{} events".format(len(trace_events)))
        # Filter for successful events -- cleanup_files() at startup
        # produces DeleteFile events that fail (files don't exist yet).
        ok_matches = [ev for ev in matches if ev["status"] == "O"]
        assert len(ok_matches) >= 1, (
            "No successful DeleteFile('RAM:atrace_test_delete') event found. "
            "All DeleteFile events: {}".format(
                [(e["seq"], e["retval"], e["status"]) for e in matches]))
        ev = ok_matches[0]
        assert ev["lib"] == "dos"
        assert ev["retval"] == "OK", (
            "DeleteFile retval should be 'OK', got: {}".format(ev["retval"]))

    def test_loadseg(self, trace_events):
        """LoadSeg('C:Echo') -- verify event format."""
        matches = _find_events(trace_events, "LoadSeg", '"C:Echo"')
        assert len(matches) >= 1, (
            "No LoadSeg('C:Echo') event found in {} events".format(
                len(trace_events)))
        ev = matches[0]
        assert ev["lib"] == "dos"
        # C:Echo may not exist on disk (shell builtin/resident)
        # Verify format: quoted path in args, consistent retval/status
        assert ev["args"].startswith('"'), (
            "LoadSeg args should start with quoted path: {}".format(ev["args"]))
        if ev["status"] == "O":
            assert _HEX_PTR.match(ev["retval"]), (
                "Successful LoadSeg should return hex BPTR: {}".format(ev["retval"]))
        else:
            assert ev["retval"] == "NULL", (
                "Failed LoadSeg should return NULL: {}".format(ev["retval"]))
            assert ev["status"] == "E"

    def test_newloadseg(self, trace_events):
        """NewLoadSeg('C:Echo') -- verify event format."""
        matches = _find_events(
            trace_events, "NewLoadSeg", '"C:Echo"')
        assert len(matches) >= 1, (
            "No NewLoadSeg('C:Echo') event found in {} events"
            .format(len(trace_events)))
        ev = matches[0]
        assert ev["lib"] == "dos"
        # C:Echo may not exist on disk (shell builtin/resident)
        # Verify format: quoted path in args, consistent retval/status
        assert ev["args"].startswith('"'), (
            "NewLoadSeg args should start with quoted path: {}".format(
                ev["args"]))
        if ev["status"] == "O":
            assert _HEX_PTR.match(ev["retval"]), (
                "Successful NewLoadSeg should return hex BPTR: {}".format(
                    ev["retval"]))
        else:
            assert ev["retval"] == "NULL", (
                "Failed NewLoadSeg should return NULL: {}".format(
                    ev["retval"]))
            assert ev["status"] == "E"

    def test_execute(self, trace_events):
        """Execute("Echo >NIL: atrace_exec") -- neutral status."""
        matches = _find_events(
            trace_events, "Execute", "atrace_exec")
        assert len(matches) >= 1, (
            "No Execute event with 'atrace_exec' found in {} events".format(
                len(trace_events)))
        ev = matches[0]
        assert ev["lib"] == "dos"
        # The command string should contain the echo command
        assert "Echo >NIL: atrace_exec" in ev["args"], (
            "Execute args should contain command string, got: {}".format(
                ev["args"]))
        # Execute uses RET_EXECUTE: neutral status '-', retval is
        # "OK" for DOSTRUE (-1) or "rc=N" for shell return codes.
        assert ev["status"] == "-", (
            "Execute should have neutral status, got status={} retval={}".format(
                ev["status"], ev["retval"]))
        assert ev["retval"] in ("OK", "rc=0"), (
            "Execute retval should be 'OK' or 'rc=0', got: {}".format(
                ev["retval"]))

    def test_getvar(self, trace_events):
        """GetVar("atrace_test_var") -- variable set in prior block, status O."""
        matches = _find_events(
            trace_events, "GetVar", '"atrace_test_var"')
        assert len(matches) >= 1, (
            "No GetVar('atrace_test_var') event found in {} events".format(
                len(trace_events)))
        ev = matches[0]
        assert ev["lib"] == "dos"
        assert ev["args"].startswith('"'), (
            "GetVar args should start with quoted name: {}".format(
                ev["args"]))
        # GetVar succeeds when the variable was set earlier (Block 21)
        assert ev["status"] == "O", (
            "GetVar should succeed (variable set in prior block), "
            "got status={} retval={}".format(
                ev["status"], ev["retval"]))
        # Retval is byte count (RET_LEN), should be a non-negative decimal
        assert ev["retval"].lstrip("-").isdigit(), (
            "GetVar retval should be a decimal number, got: {}".format(
                ev["retval"]))
        retval_int = int(ev["retval"])
        assert retval_int >= 0, (
            "GetVar retval should be >=0 byte count, got: {}".format(
                ev["retval"]))

    def test_findvar(self, trace_events):
        """FindVar("atrace_test_var", LV_VAR) -- variable exists, verify args."""
        matches = _find_events(
            trace_events, "FindVar", '"atrace_test_var"')
        assert len(matches) >= 1, (
            "No FindVar('atrace_test_var') event found in {} events".format(
                len(trace_events)))
        ev = matches[0]
        assert ev["lib"] == "dos"
        assert ev["args"].startswith('"'), (
            "FindVar args should start with quoted name: {}".format(
                ev["args"]))
        # FindVar args include the type (LV_VAR for type=0)
        assert "LV_VAR" in ev["args"], (
            "FindVar args should contain 'LV_VAR', got: {}".format(
                ev["args"]))
        # FindVar returns RET_PTR_OPAQUE: O with "OK" or E with NULL
        if ev["status"] == "O":
            assert ev["retval"] == "OK", (
                "Successful FindVar should return 'OK': {}".format(
                    ev["retval"]))
        else:
            assert ev["status"] == "E"
            assert ev["retval"] == "NULL", (
                "Failed FindVar should return NULL: {}".format(ev["retval"]))

    def test_setvar(self, trace_events):
        """SetVar("atrace_test_setvar") -- status O."""
        matches = _find_events(
            trace_events, "SetVar", '"atrace_test_setvar"')
        assert len(matches) >= 1, (
            "No SetVar('atrace_test_setvar') event found in {} events".format(
                len(trace_events)))
        ev = matches[0]
        assert ev["lib"] == "dos"
        assert ev["args"].startswith('"'), (
            "SetVar args should start with quoted name: {}".format(
                ev["args"]))
        assert ev["status"] == "O", (
            "SetVar should succeed, got status={} retval={}".format(
                ev["status"], ev["retval"]))
        assert ev["retval"] == "OK", (
            "SetVar retval should be 'OK', got: {}".format(ev["retval"]))

    def test_deletevar(self, trace_events):
        """DeleteVar("atrace_test_delvar") -- status O."""
        # Block 24: SetVar creates the variable, then DeleteVar removes it.
        # Filter for atrace_test_delvar specifically to avoid matching the
        # cleanup DeleteVar of atrace_test_setvar from Block 23.
        matches = _find_events(
            trace_events, "DeleteVar", '"atrace_test_delvar"')
        assert len(matches) >= 1, (
            "No DeleteVar('atrace_test_delvar') event found in {} events"
            .format(len(trace_events)))
        # Filter for successful deletion -- the variable was just created
        ok_matches = [ev for ev in matches if ev["status"] == "O"]
        assert len(ok_matches) >= 1, (
            "No successful DeleteVar('atrace_test_delvar') found. "
            "All DeleteVar events: {}".format(
                [(e["seq"], e["retval"], e["status"]) for e in matches]))
        ev = ok_matches[0]
        assert ev["lib"] == "dos"
        assert ev["retval"] == "OK", (
            "DeleteVar retval should be 'OK', got: {}".format(ev["retval"]))

    def test_createdir(self, trace_events):
        """CreateDir("RAM:atrace_test_dir") -- status O."""
        matches = _find_events(
            trace_events, "CreateDir", '"RAM:atrace_test_dir"')
        assert len(matches) >= 1, (
            "No CreateDir('RAM:atrace_test_dir') event found in {} events"
            .format(len(trace_events)))
        # Filter for successful events -- cleanup_files may have
        # DeleteFile'd the dir earlier, causing internal Lock events,
        # but CreateDir itself should only appear from Block 25.
        ok_matches = [ev for ev in matches if ev["status"] == "O"]
        assert len(ok_matches) >= 1, (
            "No successful CreateDir('RAM:atrace_test_dir') found. "
            "All CreateDir events: {}".format(
                [(e["seq"], e["retval"], e["status"]) for e in matches]))
        ev = ok_matches[0]
        assert ev["lib"] == "dos"
        assert _HEX_PTR.match(ev["retval"]), (
            "Successful CreateDir should return hex lock: {}".format(
                ev["retval"]))

    def test_makelink(self, trace_events):
        """MakeLink("RAM:atrace_test_link") -- may fail on FFS."""
        matches = _find_events(
            trace_events, "MakeLink", "atrace_test_link")
        assert len(matches) >= 1, (
            "No MakeLink event with 'atrace_test_link' found in {} events"
            .format(len(trace_events)))
        ev = matches[0]
        assert ev["lib"] == "dos"
        # MakeLink args contain the link name, dest pointer, and soft/hard
        assert "soft" in ev["args"], (
            "MakeLink args should contain 'soft' (LINK_SOFT), got: {}".format(
                ev["args"]))
        # MakeLink may fail on FFS (no soft link support).
        # Accept both success and failure, but verify consistency.
        if ev["status"] == "O":
            assert ev["retval"] == "OK", (
                "Successful MakeLink retval should be 'OK', got: {}".format(
                    ev["retval"]))
        else:
            assert ev["status"] == "E", (
                "MakeLink status should be 'O' or 'E', got: {}".format(
                    ev["status"]))
            assert ev["retval"] == "FAIL", (
                "Failed MakeLink retval should be 'FAIL', got: {}".format(
                    ev["retval"]))

    def test_rename(self, trace_events):
        """Rename("RAM:atrace_test_ren_old", ...) -- status O."""
        # "RAM:atrace_test_ren_old" is 23 chars and may be truncated by
        # the ring buffer's string_data field.  Use prefix matching.
        matches = _find_events(
            trace_events, "Rename", "RAM:atrace_test_ren")
        assert len(matches) >= 1, (
            "No Rename event with 'RAM:atrace_test_ren' found in {} events"
            .format(len(trace_events)))
        ev = matches[0]
        assert ev["lib"] == "dos"
        assert ev["args"].startswith('"'), (
            "Rename args should start with quoted path: {}".format(
                ev["args"]))
        # The args include old name and new=0x<ptr>
        assert "new=0x" in ev["args"], (
            "Rename args should contain 'new=0x' pointer, got: {}".format(
                ev["args"]))
        assert ev["status"] == "O", (
            "Rename should succeed, got status={} retval={}".format(
                ev["status"], ev["retval"]))
        assert ev["retval"] == "OK", (
            "Rename retval should be 'OK', got: {}".format(ev["retval"]))

    def test_runcommand(self, trace_events):
        """RunCommand(seg, stack=4096, ...) -- verify args format."""
        matches = _find_events(trace_events, "RunCommand", "stack=4096")
        if len(matches) == 0:
            pytest.skip("RunCommand not captured (C:Echo likely not on disk)")
        ev = matches[0]
        assert ev["lib"] == "dos"
        # Args format: seg=0x<ptr>,stack=4096,6
        assert "seg=0x" in ev["args"], (
            "RunCommand args should contain 'seg=0x', got: {}".format(
                ev["args"]))
        # RunCommand returns rc (RET_RC). C:Echo may be resident (not on
        # disk) so LoadSeg might fail and RunCommand would not be called.
        # If the event exists, verify retval format.
        assert ev["retval"].startswith("rc="), (
            "RunCommand retval should start with 'rc=', got: {}".format(
                ev["retval"]))
        # Accept any rc -- C:Echo with "hello\n" should return rc=0 but
        # the command may behave differently on different systems.
        if ev["status"] == "O":
            assert ev["retval"] == "rc=0", (
                "Successful RunCommand retval should be 'rc=0', got: {}"
                .format(ev["retval"]))
        else:
            assert ev["status"] == "E", (
                "RunCommand status should be 'O' or 'E', got: {}".format(
                    ev["status"]))

    def test_runcommand_segment_annotation(self, trace_events):
        """SegmentResolver correlates LoadSeg with RunCommand in live events."""
        # Feed all events through SegmentResolver to build cache, then
        # check if RunCommand events get annotated.
        sr = SegmentResolver()
        annotated_count = 0
        for ev in trace_events:
            sr.track(ev)
            if ev.get("func") == "RunCommand":
                filename = sr.annotate(ev)
                if filename is not None:
                    annotated_count += 1
                    # The annotation should be a non-empty string
                    assert len(filename) > 0, (
                        "SegmentResolver returned empty filename")
        # atrace_test calls RunCommand after LoadSeg("C:Echo").
        # If C:Echo is resident (no LoadSeg), skip gracefully.
        run_matches = _find_events(trace_events, "RunCommand", "stack=4096")
        load_matches = _find_events(trace_events, "LoadSeg", '"C:Echo"')
        if len(run_matches) > 0 and len(load_matches) > 0:
            # LoadSeg succeeded -- RunCommand should be annotated
            ok_loads = [e for e in load_matches if e.get("status") == "O"]
            if ok_loads:
                assert annotated_count > 0, (
                    "SegmentResolver failed to annotate RunCommand despite "
                    "successful LoadSeg('C:Echo') in event stream")

    def test_systemtaglist(self, trace_events):
        """SystemTagList("Echo >NIL: systest") -- verify args."""
        matches = _find_events(
            trace_events, "SystemTagList", "systest")
        assert len(matches) >= 1, (
            "No SystemTagList event with 'systest' found in {} events".format(
                len(trace_events)))
        ev = matches[0]
        assert ev["lib"] == "dos"
        assert "Echo >NIL: systest" in ev["args"], (
            "SystemTagList args should contain command string, got: {}".format(
                ev["args"]))
        # SystemTagList returns rc (RET_RC).
        assert ev["retval"].startswith("rc="), (
            "SystemTagList retval should start with 'rc=', got: {}".format(
                ev["retval"]))
        if ev["retval"] == "rc=0":
            assert ev["status"] == "O", (
                "SystemTagList rc=0 should have status 'O', got: {}".format(
                    ev["status"]))
        else:
            assert ev["status"] == "E", (
                "SystemTagList non-zero rc should have status 'E', got: {}".format(
                    ev["status"]))

    def test_currentdir(self, trace_events):
        """CurrentDir(lock) -- status always '-' (informational)."""
        matches = _find_events(trace_events, "CurrentDir")
        assert len(matches) >= 1, (
            "No CurrentDir events found in {} events".format(
                len(trace_events)))
        # Find an event with meaningful args (not from internal DOS ops).
        # Block 31 does Lock("RAM:") then CurrentDir(lock), so the args
        # should contain either a quoted path (from lock-to-path cache)
        # or a hex lock pointer.
        test_matches = [ev for ev in matches
                        if ev.get("args", "") != "lock=NULL"]
        assert len(test_matches) >= 1, (
            "No CurrentDir events with non-NULL lock found. "
            "All CurrentDir events: {}".format(
                [(e["seq"], e["args"]) for e in matches]))
        ev = test_matches[0]
        assert ev["lib"] == "dos"
        # CurrentDir args: either "RAM:" (from cache) or lock=0x<ptr>
        assert (ev["args"].startswith('"')
                or ev["args"].startswith("lock=0x")), (
            "CurrentDir args should be quoted path or lock=0x, got: {}"
            .format(ev["args"]))
        # CurrentDir always has status '-' (ERR_CHECK_VOID, RET_OLD_LOCK)
        assert ev["status"] == "-", (
            "CurrentDir status should be '-', got: {}".format(ev["status"]))
        # Retval is the old lock: (none), quoted path, or hex pointer
        assert (ev["retval"] == "(none)"
                or ev["retval"].startswith('"')
                or _HEX_PTR.match(ev["retval"])), (
            "CurrentDir retval should be '(none)', quoted path, or hex "
            "pointer, got: {}".format(ev["retval"]))


# ---------------------------------------------------------------------------
# TestFieldInvariants -- cross-cutting structural validation
# ---------------------------------------------------------------------------

class TestFieldInvariants:
    """Cross-cutting field validation tests on the full event stream."""

    def test_event_count_reasonable(self, trace_events):
        """Total event count is between 40 and 500.

        Upper bound accounts for the traced process's internal OS
        operations (OpenLibrary, LoadSeg, Lock, etc.) beyond the
        explicit atrace_test calls.
        """
        count = len(trace_events)
        assert 40 <= count <= 500, (
            "Expected 40-500 events, got {}".format(count))

    def test_all_events_have_func(self, trace_events):
        """Every event has a non-empty func field."""
        for ev in trace_events:
            assert ev.get("func"), (
                "Event seq={} has empty func: {}".format(
                    ev.get("seq"), ev.get("raw", "")))

    def test_all_events_have_status(self, trace_events):
        """Every event has status in (O, E, -)."""
        valid_statuses = {"O", "E", "-"}
        for ev in trace_events:
            assert ev.get("status") in valid_statuses, (
                "Event seq={} func={} has invalid status '{}': {}".format(
                    ev.get("seq"), ev.get("func"),
                    ev.get("status"), ev.get("raw", "")))

    def test_all_events_have_required_fields(self, trace_events):
        """Every event has all required fields (7 wire fields parsed into 8 dict keys)."""
        required_keys = ("seq", "time", "lib", "func", "task",
                         "args", "retval", "status")
        for ev in trace_events:
            for key in required_keys:
                assert ev.get(key) is not None, (
                    "Event seq={} missing field '{}': {}".format(
                        ev.get("seq"), key, ev.get("raw", "")))

    def test_lib_names_valid(self, trace_events):
        """All events have lib in {exec, dos, intuition}."""
        valid_libs = {"exec", "dos", "intuition"}
        bad = [ev for ev in trace_events
               if ev.get("lib") not in valid_libs]
        assert not bad, (
            "{} events have invalid lib: {}".format(
                len(bad),
                [(e.get("seq"), e.get("lib"), e.get("func")) for e in bad]))

    def test_open_quoted_filenames(self, trace_events):
        """All Open events have args starting with a double quote."""
        open_events = _find_events(trace_events, "Open")
        for ev in open_events:
            assert ev["args"].startswith('"'), (
                "Open event seq={} args don't start with quote: {}".format(
                    ev["seq"], ev["args"]))

    def test_success_open_has_hex_retval(self, trace_events):
        """All Open events with status O have a hex pointer retval."""
        open_events = _find_events(trace_events, "Open")
        ok_opens = [ev for ev in open_events if ev["status"] == "O"]
        for ev in ok_opens:
            assert _HEX_PTR.match(ev["retval"]), (
                "Open seq={} status=O but retval not hex: {}".format(
                    ev["seq"], ev["retval"]))

    def test_failure_open_has_null_retval(self, trace_events):
        """All Open events with status E have retval NULL."""
        open_events = _find_events(trace_events, "Open")
        err_opens = [ev for ev in open_events if ev["status"] == "E"]
        for ev in err_opens:
            assert ev["retval"] == "NULL", (
                "Open seq={} status=E but retval not NULL: {}".format(
                    ev["seq"], ev["retval"]))

    def test_currentdir_retval_is_old_lock(self, trace_events):
        """CurrentDir has retval that is never literally '(void)'.

        The noise void functions (PutMsg, ObtainSemaphore,
        ReleaseSemaphore) are validated in TestExecNoiseGroup2/3.
        This test checks the non-noise void-like function (CurrentDir)
        whose retval is the old lock, not "(void)".
        """
        # CurrentDir uses RET_OLD_LOCK, so retval is (none)/path/hex,
        # never "(void)".  Validate it's present and well-formed.
        matches = _find_events(trace_events, "CurrentDir")
        for ev in matches:
            assert ev["retval"] != "(void)", (
                "CurrentDir seq={} retval should not be '(void)': {}".format(
                    ev["seq"], ev["retval"]))

    def test_no_adddosentry_events(self, trace_events):
        """No AddDosEntry events appear (we don't call it).

        AddDosEntry is too dangerous for controlled testing -- it
        manipulates the system DOS list.  The test app skips it.
        """
        matches = _find_events(trace_events, "AddDosEntry")
        assert len(matches) == 0, (
            "Unexpected AddDosEntry events: {}".format(
                [(e["seq"], e["args"]) for e in matches]))


# ---------------------------------------------------------------------------
# TestPhase4bFeatures -- Phase 4b specific feature validation
# ---------------------------------------------------------------------------

class TestPhase4bFeatures:
    """Tests for Phase 4b features: CLI numbers, truncation indicator,
    lock cache for CurrentDir, and stale string_data exclusion.

    These validate daemon-side formatting enhancements introduced in
    Phase 4b using the same trace_events fixture (single TRACE RUN of
    C:atrace_test).
    """

    def test_cli_number_in_task_names(self, trace_events):
        """At least one event has a CLI number prefix in the task field.

        The test app runs as a CLI process via TRACE RUN, so the daemon
        should prefix its task name with the CLI number in square
        brackets, e.g. "[3] atrace_test".
        """
        cli_pattern = re.compile(r"\[\d+\] ")
        has_cli = any(cli_pattern.search(ev.get("task", ""))
                      for ev in trace_events)
        assert has_cli, (
            "No event has a CLI number prefix [N] in task field. "
            "Sample task names: {}".format(
                list({ev.get("task", "") for ev in trace_events[:10]})))

    def test_string_truncation_indicator(self, trace_events):
        """Rename event for a 23-char filename shows full string without truncation.

        The C test app calls Rename("RAM:atrace_test_ren_old", ...)
        where the old name is 23 characters.  With 59-char string_data
        capacity (Phase 5b event expansion to 128 bytes), this fits
        easily and no truncation occurs.
        """
        matches = _find_events(trace_events, "Rename", "RAM:atrace_test_ren")
        assert len(matches) >= 1, (
            "No Rename event with 'RAM:atrace_test_ren' found in "
            "{} events".format(len(trace_events)))
        ev = matches[0]
        assert "RAM:atrace_test_ren_old" in ev["args"], (
            "Rename args should contain full path with 59-char capture, "
            "got: {}".format(ev["args"]))
        assert "..." not in ev["args"], (
            "Rename args should not contain '...' truncation indicator "
            "with 59-char capture, got: {}".format(ev["args"]))

    def test_lock_cache_currentdir_path(self, trace_events):
        """CurrentDir event shows quoted path from lock cache, not raw hex.

        The C test app does Lock("RAM:") then Delay(1) then
        CurrentDir(lock).  The Delay gives the daemon time to poll and
        cache the lock-to-path mapping from the Lock event.  The
        subsequent CurrentDir should show a quoted path like "RAM:"
        rather than a raw lock=0x... hex pointer.
        """
        matches = _find_events(trace_events, "CurrentDir")
        # Filter for events with non-NULL args that start with a quote
        # (indicating the lock cache resolved the path).
        quoted = [ev for ev in matches
                  if ev.get("args", "").startswith('"')]
        assert len(quoted) >= 1, (
            "No CurrentDir event with quoted path args found. "
            "Lock cache may be broken. "
            "All CurrentDir args: {}".format(
                [(e["seq"], e["args"]) for e in matches]))

    def test_stale_string_data_excluded(self, trace_events):
        """Non-noise functions with has_string=0 do not show quoted args.

        Close has no string parameter.  Its args should never start with
        a double quote character, which would indicate stale string_data
        leaking from a previous ring buffer entry.

        The noise no-string functions (GetMsg, PutMsg, AllocMem,
        ObtainSemaphore, ReleaseSemaphore) are validated in the
        noise_group2/3_events fixtures where they actually appear.
        """
        # Close is the only non-noise function with has_string=0.
        no_string_funcs = ("Close",)
        for func_name in no_string_funcs:
            matches = _find_events(trace_events, func_name)
            for ev in matches:
                assert not ev["args"].startswith('"'), (
                    "{} seq={} args starts with '\"' -- possible stale "
                    "string_data leak: {}".format(
                        func_name, ev["seq"], ev["args"]))

    def test_save_scrollback(self, trace_events, tmp_path):
        """Save pipeline produces a valid log file from real daemon data.

        End-to-end test: feed real trace events through HandleResolver,
        format with annotation, strip ANSI, write to a file, and verify
        the output contains expected function names and paths with no
        ANSI escape sequences.
        """
        from amigactl.trace_ui import HandleResolver, ColumnLayout
        from amigactl.colors import ColorWriter, strip_ansi

        hr = HandleResolver()
        cw = ColorWriter(force_color=True)
        layout = ColumnLayout(120)

        lines = []
        prev_time = None
        for ev in trace_events:
            hr.track(ev)
            # Apply annotation (same logic as TraceViewer._annotated_event)
            annotation = hr.annotate(ev)
            if annotation is not None:
                ev = dict(ev)
                ev["args"] = '{} "{}"'.format(ev["args"], annotation)
            time_str = ev.get("time", "")
            formatted = layout.format_event(ev, cw, time_str=time_str)
            lines.append(strip_ansi(formatted))
            prev_time = ev.get("time", "")

        out_file = tmp_path / "atrace_test_save.log"
        out_file.write_text('\n'.join(lines) + '\n')

        content = out_file.read_text()

        # No ANSI escape sequences in the output
        assert "\x1b" not in content, (
            "ANSI escape found in saved output")
        assert "\x9b" not in content, (
            "8-bit CSI found in saved output")

        # Expected function names from atrace_test are present
        assert "Open" in content
        assert "Close" in content
        assert "Lock" in content

        # Expected test path from atrace_test
        assert "RAM:atrace_test" in content

        # Line count matches event count
        non_empty = [l for l in content.strip().split('\n') if l.strip()]
        assert len(non_empty) == len(trace_events), (
            "Expected {} lines, got {}".format(
                len(trace_events), len(non_empty)))

    def test_transient_process_has_task_name(self, trace_events):
        """Events from short-lived processes have non-empty task names."""
        # atrace_test calls Execute("run >NIL: C:Version")
        # The run shell is transient -- verify it has a task name
        execute_events = [e for e in trace_events
                          if e["func"] == "Execute"]
        for ev in execute_events:
            # The Execute call itself comes from atrace_test (not transient).
            # But events from the spawned shell (if any) should have names.
            assert ev["task"] != "", (
                "Execute event should have a task name: seq={}".format(
                    ev["seq"]))


# ---------------------------------------------------------------------------
# TestExecDeviceIO -- Phase 5: device I/O functions
# ---------------------------------------------------------------------------

class TestExecDeviceIO:
    """Tests for exec.library Device I/O functions (noise group 7)."""

    def test_doio(self, noise_group7_events):
        """DoIO on timer.device -- status O (success, retval=OK)."""
        matches = _find_events(noise_group7_events, "DoIO")
        assert len(matches) >= 1
        ev = matches[0]
        assert ev["lib"] == "exec"
        assert "io=0x" in ev["args"]
        assert ev["status"] == "O"
        assert ev["retval"] == "OK"

    def test_sendio(self, noise_group7_events):
        """SendIO -- void function, status '-'."""
        matches = _find_events(noise_group7_events, "SendIO")
        assert len(matches) >= 1
        ev = matches[0]
        assert ev["lib"] == "exec"
        assert "io=0x" in ev["args"]
        assert ev["status"] == "-"
        assert ev["retval"] == "(void)"

    def test_waitio(self, noise_group7_events):
        """WaitIO -- status O (success after SendIO completes)."""
        matches = _find_events(noise_group7_events, "WaitIO")
        assert len(matches) >= 1
        ev = matches[0]
        assert ev["lib"] == "exec"
        assert "io=0x" in ev["args"]
        assert ev["status"] == "O"
        assert ev["retval"] == "OK"

    def test_abortio(self, noise_group7_events):
        """AbortIO -- abort a pending timer request."""
        matches = _find_events(noise_group7_events, "AbortIO")
        assert len(matches) >= 1
        ev = matches[0]
        assert ev["lib"] == "exec"
        assert "io=0x" in ev["args"]
        # AbortIO may return 0 (success) or non-zero (already complete)
        # Don't assert status, just verify format

    def test_checkio(self, noise_group7_events):
        """CheckIO -- check pending request status."""
        matches = _find_events(noise_group7_events, "CheckIO")
        assert len(matches) >= 1
        ev = matches[0]
        assert ev["lib"] == "exec"
        assert "io=0x" in ev["args"]
        # CheckIO returns IORequest ptr or NULL, both valid


# ---------------------------------------------------------------------------
# TestExecNoiseGroup5 -- memory functions (FreeMem, AllocVec, FreeVec)
# ---------------------------------------------------------------------------

class TestExecNoiseGroup5:
    """Memory management: FreeMem, AllocVec, FreeVec."""

    def test_freemem(self, noise_group5_events):
        """FreeMem(ptr, 2345) -- distinctive size from test app."""
        matches = _find_events(noise_group5_events, "FreeMem", "2345")
        assert len(matches) >= 1
        ev = matches[0]
        assert ev["lib"] == "exec"
        assert "0x" in ev["args"]   # pointer
        assert "2345" in ev["args"]  # size
        assert ev["status"] == "-"
        assert ev["retval"] == "(void)"

    def test_allocvec(self, noise_group5_events):
        """AllocVec(3456, MEMF_PUBLIC|MEMF_CLEAR) -- distinctive size."""
        matches = _find_events(noise_group5_events, "AllocVec", "3456,")
        assert len(matches) >= 1
        ev = matches[0]
        assert ev["lib"] == "exec"
        assert "MEMF_PUBLIC" in ev["args"]
        assert "MEMF_CLEAR" in ev["args"]
        assert ev["status"] == "O"
        assert _HEX_PTR.match(ev["retval"])

    def test_freevec(self, noise_group5_events):
        """FreeVec(ptr) -- void function."""
        matches = _find_events(noise_group5_events, "FreeVec")
        assert len(matches) >= 1
        ev = matches[0]
        assert ev["lib"] == "exec"
        assert "0x" in ev["args"]
        assert ev["status"] == "-"
        assert ev["retval"] == "(void)"


# ---------------------------------------------------------------------------
# TestIntuitionFunctions -- Phase 5: intuition.library
# ---------------------------------------------------------------------------

class TestIntuitionFunctions:
    """Tests for intuition.library traced functions."""

    def test_openwindow(self, trace_events):
        """OpenWindow -- returns window pointer, status O."""
        matches = _find_events(trace_events, "OpenWindow")
        assert len(matches) >= 1
        ev = matches[0]
        assert ev["lib"] == "intuition"
        assert "nw=0x" in ev["args"]
        assert ev["status"] == "O"
        assert _HEX_PTR.match(ev["retval"])

    def test_closewindow(self, trace_events):
        """CloseWindow -- void function, status '-'."""
        matches = _find_events(trace_events, "CloseWindow")
        assert len(matches) >= 1
        ev = matches[0]
        assert ev["lib"] == "intuition"
        assert "win=0x" in ev["args"]
        assert ev["status"] == "-"
        assert ev["retval"] == "(void)"

    def test_openscreen(self, trace_events):
        """OpenScreen -- returns screen pointer, status O."""
        matches = _find_events(trace_events, "OpenScreen")
        assert len(matches) >= 1
        ev = matches[0]
        assert ev["lib"] == "intuition"
        assert "ns=0x" in ev["args"]
        assert ev["status"] == "O"
        assert _HEX_PTR.match(ev["retval"])

    def test_closescreen(self, trace_events):
        """CloseScreen -- void function."""
        matches = _find_events(trace_events, "CloseScreen")
        assert len(matches) >= 1
        ev = matches[0]
        assert ev["lib"] == "intuition"
        assert "scr=0x" in ev["args"]
        assert ev["status"] == "-"
        assert ev["retval"] == "(void)"

    def test_activatewindow(self, trace_events):
        """ActivateWindow -- void function."""
        matches = _find_events(trace_events, "ActivateWindow")
        assert len(matches) >= 1
        ev = matches[0]
        assert ev["lib"] == "intuition"
        assert "win=0x" in ev["args"]
        assert ev["status"] == "-"
        assert ev["retval"] == "(void)"

    def test_windowtofront(self, trace_events):
        """WindowToFront -- void function."""
        matches = _find_events(trace_events, "WindowToFront")
        assert len(matches) >= 1
        ev = matches[0]
        assert ev["lib"] == "intuition"
        assert "win=0x" in ev["args"]
        assert ev["status"] == "-"
        assert ev["retval"] == "(void)"

    def test_windowtoback(self, trace_events):
        """WindowToBack -- void function."""
        matches = _find_events(trace_events, "WindowToBack")
        assert len(matches) >= 1
        ev = matches[0]
        assert ev["lib"] == "intuition"
        assert "win=0x" in ev["args"]
        assert ev["status"] == "-"
        assert ev["retval"] == "(void)"

    def test_modifyidcmp(self, trace_events):
        """ModifyIDCMP -- flags contain CLOSEWINDOW."""
        matches = _find_events(trace_events, "ModifyIDCMP")
        assert len(matches) >= 1
        # Filter for our specific CLOSEWINDOW call (Intuition itself calls
        # ModifyIDCMP with flags=0 during window lifecycle)
        cw_matches = [ev for ev in matches if "CLOSEWINDOW" in ev["args"]]
        assert len(cw_matches) >= 1, (
            "Expected ModifyIDCMP with CLOSEWINDOW flag from atrace_test")
        ev = cw_matches[0]
        assert ev["lib"] == "intuition"
        assert "win=0x" in ev["args"]
        assert ev["status"] == "-"
        assert ev["retval"] == "(void)"


# ---------------------------------------------------------------------------
# TestDosNoiseGroup6 -- Read/Write (noise group 6)
# ---------------------------------------------------------------------------

class TestDosNoiseGroup6:
    """dos.library Read/Write (noise group 6)."""

    def test_write(self, noise_group6_events):
        """Write(fh, buf, 42) -- distinctive length from test app."""
        matches = _find_events(noise_group6_events, "Write", "len=42")
        assert len(matches) >= 1
        ev = matches[0]
        assert ev["lib"] == "dos"
        assert "fh=0x" in ev["args"]
        assert "len=42" in ev["args"]
        # Write should return 42 (bytes written)
        assert ev["status"] == "O"
        assert ev["retval"] == "42"

    def test_read(self, noise_group6_events):
        """Read(fh, buf, 42) -- read back the 42 bytes we wrote."""
        matches = _find_events(noise_group6_events, "Read", "len=42")
        assert len(matches) >= 1
        ev = matches[0]
        assert ev["lib"] == "dos"
        assert "fh=0x" in ev["args"]
        assert "len=42" in ev["args"]
        # Read should return 42 (bytes read)
        assert ev["status"] == "O"
        assert ev["retval"] == "42"


# ---------------------------------------------------------------------------
# TestPhase5PatchCount -- verify patch count increased to 50
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("require_atrace_for_app")
class TestPhase5PatchCount:
    """Verify patch count increased to 50 after Phase 5."""

    def test_status_patch_count(self, request):
        """TRACE STATUS reports 50 patches."""
        host = request.config.getoption("--host")
        port = request.config.getoption("--port")
        conn = AmigaConnection(host, port)
        conn.connect()
        try:
            status = conn.trace_status()
            assert status["patches"] == 50, (
                "Expected 50 patches, got: {}".format(status["patches"]))
        finally:
            conn.close()

    def test_status_intuition_patches(self, request):
        """TRACE STATUS lists intuition.library patches."""
        host = request.config.getoption("--host")
        port = request.config.getoption("--port")
        conn = AmigaConnection(host, port)
        conn.connect()
        try:
            status = conn.trace_status()
            patch_list = status.get("patch_list", [])
            intuition_patches = [
                e for e in patch_list
                if e.get("name", "").startswith("intuition.")]
            assert len(intuition_patches) == 10, (
                "Expected 10 intuition patches, got: {}".format(
                    len(intuition_patches)))
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# TestPhase6Timestamps -- Phase 6: EClock timestamps in atrace_test events
# ---------------------------------------------------------------------------

class TestPhase6Timestamps:
    """Tests for Phase 6 EClock timestamp features using atrace_test events.

    Uses the module-scoped trace_events fixture to validate that events
    from atrace_test carry microsecond-precision timestamps.
    """

    def test_all_events_have_6digit_timestamps(self, trace_events):
        """Every event has a timestamp in HH:MM:SS.uuuuuu format."""
        events = trace_events
        real_events = [e for e in events if e.get("type") == "event"]
        assert len(real_events) > 0, "No events to check"

        import re
        us_pattern = re.compile(r'^\d{2}:\d{2}:\d{2}\.\d{6}$')
        for ev in real_events:
            time_str = ev.get("time", "")
            assert us_pattern.match(time_str), (
                "Expected HH:MM:SS.uuuuuu, got '{}' (seq={}, func={})".format(
                    time_str, ev.get("seq"), ev.get("func")))

    def test_timestamps_monotonic(self, trace_events):
        """Event timestamps are monotonically non-decreasing."""
        from amigactl.trace_ui import TraceViewer

        events = trace_events
        real_events = [e for e in events if e.get("type") == "event"]
        assert len(real_events) >= 2

        prev_us = 0
        for i, ev in enumerate(real_events):
            us = TraceViewer._parse_time_us(ev.get("time", ""))
            assert us >= prev_us, (
                "Timestamp not monotonic at event {}: {} < {} "
                "(func={}, time='{}')".format(
                    i, us, prev_us, ev.get("func"), ev.get("time")))
            prev_us = us

    def test_timestamps_have_nonzero_deltas(self, trace_events):
        """At least some consecutive events have distinct timestamps."""
        from amigactl.trace_ui import TraceViewer

        events = trace_events
        real_events = [e for e in events if e.get("type") == "event"]
        assert len(real_events) >= 2

        distinct_count = 0
        for i in range(1, len(real_events)):
            us_prev = TraceViewer._parse_time_us(
                real_events[i - 1].get("time", ""))
            us_curr = TraceViewer._parse_time_us(
                real_events[i].get("time", ""))
            if us_curr > us_prev:
                distinct_count += 1

        # Per-event EClock: most pairs should have distinct timestamps
        ratio = distinct_count / (len(real_events) - 1)
        assert ratio > 0.5, (
            "Only {}/{} pairs have distinct timestamps".format(
                distinct_count, len(real_events) - 1))

    def test_task_name_present(self, trace_events):
        """All events have a non-empty task name."""
        events = trace_events
        real_events = [e for e in events if e.get("type") == "event"]
        assert len(real_events) > 0

        for ev in real_events:
            task = ev.get("task", "")
            assert len(task) > 0, (
                "Empty task name in event seq={}, func={}".format(
                    ev.get("seq"), ev.get("func")))
