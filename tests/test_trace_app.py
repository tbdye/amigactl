"""Integration tests for atrace test execution app.

These tests run the atrace_test binary on the Amiga via TRACE RUN and
validate that every traced function produces the correct wire format:
args, retval, and status fields.

The test app (C:atrace_test) calls each of the 30 traced functions
(except AddDosEntry) with distinctive, predictable inputs.  A single
module-scoped fixture runs the app once and all test methods share the
resulting event list.

All tests in this module are skipped if the target does not have
atrace loaded.
"""

import re
import signal

import pytest

from amigactl import AmigaConnection


# ---------------------------------------------------------------------------
# Timeout helper
# ---------------------------------------------------------------------------

def _timeout_handler(signum, frame):
    raise TimeoutError("trace_events fixture timed out after 60 seconds")


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

    Uses signal.alarm() for a 60-second external timeout.  This is
    necessary because trace_run() calls settimeout(None) internally,
    making socket-level timeouts ineffective.  If the test app hangs or
    the daemon stalls, the SIGALRM will raise an exception and prevent
    the test suite from blocking indefinitely.
    """
    host = request.config.getoption("--host")
    port = request.config.getoption("--port")
    conn = AmigaConnection(host, port)
    conn.connect()

    old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(60)
    try:
        events = []

        def collect(ev):
            events.append(ev)

        result = conn.trace_run("C:atrace_test", collect)
        assert result["rc"] == 0, (
            "atrace_test exited with rc={}".format(result["rc"]))
        # Filter to only "event" type (exclude comments)
        return [e for e in events if e.get("type") == "event"]
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)
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
    """Tests for all 12 exec.library traced functions."""

    def test_findport(self, trace_events):
        """FindPort("AMITCP") -- status/retval consistency."""
        matches = _find_events(trace_events, "FindPort", '"AMITCP"')
        assert len(matches) >= 1, (
            "No FindPort('AMITCP') event found in {} events".format(
                len(trace_events)))
        ev = matches[0]
        assert ev["lib"] == "exec"
        # AMITCP port may or may not exist; assert consistency
        if ev["status"] == "O":
            assert _HEX_PTR.match(ev["retval"]), (
                "FindPort OK but retval not hex: {}".format(ev["retval"]))
        else:
            assert ev["status"] == "E"
            assert ev["retval"] == "NULL"

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
        assert _HEX_PTR.match(ev["retval"]), (
            "Expected hex pointer retval, got: {}".format(ev["retval"]))

    def test_findsemaphore(self, trace_events):
        """FindSemaphore("atrace_patches") -- atrace loaded, status O."""
        matches = _find_events(
            trace_events, "FindSemaphore", '"atrace_patches"')
        assert len(matches) >= 1, (
            "No FindSemaphore('atrace_patches') event found in {} events"
            .format(len(trace_events)))
        ev = matches[0]
        assert ev["lib"] == "exec"
        assert ev["status"] == "O", (
            "atrace_patches semaphore should exist, got status={} retval={}"
            .format(ev["status"], ev["retval"]))
        assert _HEX_PTR.match(ev["retval"]), (
            "Expected hex pointer retval, got: {}".format(ev["retval"]))

    def test_findtask_self(self, trace_events):
        """FindTask(NULL) -- self-lookup, always succeeds."""
        matches = _find_events(trace_events, "FindTask", "NULL (self)")
        assert len(matches) >= 1, (
            "No FindTask('NULL (self)') event found in {} events".format(
                len(trace_events)))
        ev = matches[0]
        assert ev["lib"] == "exec"
        assert ev["status"] == "O", (
            "FindTask(NULL) should succeed, got status={} retval={}".format(
                ev["status"], ev["retval"]))
        assert _HEX_PTR.match(ev["retval"]), (
            "Expected hex pointer retval, got: {}".format(ev["retval"]))

    def test_opendevice(self, trace_events):
        """OpenDevice("timer.device", unit=0) -- status O, retval OK."""
        matches = _find_events(trace_events, "OpenDevice", '"timer.device"')
        # Further filter for unit=0 (UNIT_MICROHZ)
        matches = [ev for ev in matches if "unit=0," in ev.get("args", "")]
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

    def test_openlibrary(self, trace_events):
        """OpenLibrary("dos.library", v0) -- always present, status O."""
        matches = _find_events(
            trace_events, "OpenLibrary", '"dos.library",v0')
        assert len(matches) >= 1, (
            "No OpenLibrary('dos.library',v0) event found in {} events"
            .format(len(trace_events)))
        ev = matches[0]
        assert ev["lib"] == "exec"
        assert ev["status"] == "O", (
            "OpenLibrary should succeed, got status={} retval={}".format(
                ev["status"], ev["retval"]))
        assert _HEX_PTR.match(ev["retval"]), (
            "Expected hex pointer retval, got: {}".format(ev["retval"]))

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
        assert _HEX_PTR.match(ev["retval"]), (
            "Expected hex pointer retval, got: {}".format(ev["retval"]))

    def test_getmsg_empty(self, trace_events):
        """GetMsg on empty port -- retval (empty), status -."""
        # Find GetMsg events returning (empty).  There may be many from
        # noise (OS internals), but at least one must exist from our test.
        matches = _find_events(trace_events, "GetMsg")
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

    def test_putmsg_getmsg_pair(self, trace_events):
        """PutMsg then GetMsg -- correct retval/status and sequence."""
        # Find PutMsg events (void return)
        putmsg_events = _find_events(trace_events, "PutMsg")
        assert len(putmsg_events) >= 1, (
            "No PutMsg events found in {} events".format(len(trace_events)))

        # Find GetMsg events with non-empty return (message retrieved)
        getmsg_events = _find_events(trace_events, "GetMsg")
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

    def test_obtain_release_semaphore(self, trace_events):
        """ObtainSemaphore + ReleaseSemaphore -- void, paired, ordered."""
        obtain_events = _find_events(trace_events, "ObtainSemaphore")
        release_events = _find_events(trace_events, "ReleaseSemaphore")
        assert len(obtain_events) >= 1, (
            "No ObtainSemaphore events found in {} events".format(
                len(trace_events)))
        assert len(release_events) >= 1, (
            "No ReleaseSemaphore events found in {} events".format(
                len(trace_events)))

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

    def test_allocmem(self, trace_events):
        """AllocMem(1234, MEMF_PUBLIC|MEMF_CLEAR) -- distinctive size."""
        matches = _find_events(trace_events, "AllocMem", "1234,")
        assert len(matches) >= 1, (
            "No AllocMem with size 1234 found in {} events".format(
                len(trace_events)))
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
        """NewLoadSeg('C:Echo', tags=0x0) -- verify event format."""
        matches = _find_events(
            trace_events, "NewLoadSeg", '"C:Echo",tags=0x0')
        assert len(matches) >= 1, (
            "No NewLoadSeg('C:Echo',tags=0x0) event found in {} events"
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
        """Execute("Echo >NIL: atrace_exec", in=NULL, out=NULL) -- status O."""
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
        # Both input and output handles are NULL in the C test
        assert "in=NULL" in ev["args"], (
            "Execute args should contain 'in=NULL', got: {}".format(
                ev["args"]))
        assert "out=NULL" in ev["args"], (
            "Execute args should contain 'out=NULL', got: {}".format(
                ev["args"]))
        # Execute returns DOSTRUE if the shell started successfully,
        # regardless of whether the command itself exists or succeeds.
        assert ev["status"] == "O", (
            "Execute should succeed, got status={} retval={}".format(
                ev["status"], ev["retval"]))
        assert ev["retval"] == "OK", (
            "Execute retval should be 'OK', got: {}".format(ev["retval"]))

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
        # FindVar returns a pointer (RET_PTR): O with hex or E with NULL
        if ev["status"] == "O":
            assert _HEX_PTR.match(ev["retval"]), (
                "Successful FindVar should return hex pointer: {}".format(
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
        # Args format: seg=0x<ptr>,stack=4096,params=0x<ptr>,6
        assert "seg=0x" in ev["args"], (
            "RunCommand args should contain 'seg=0x', got: {}".format(
                ev["args"]))
        assert "params=0x" in ev["args"], (
            "RunCommand args should contain 'params=0x', got: {}".format(
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

    def test_systemtaglist(self, trace_events):
        """SystemTagList("Echo >NIL: systest", tags=...) -- verify args."""
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
        assert "tags=0x" in ev["args"], (
            "SystemTagList args should contain 'tags=0x', got: {}".format(
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

        Upper bound accounts for noise functions auto-enabled during
        TRACE RUN, which generate many additional events from internal
        OS operations (filesystem handler PutMsg/GetMsg, semaphore
        operations, memory allocations).
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
        """All events have lib in {exec, dos}."""
        valid_libs = {"exec", "dos"}
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

    def test_void_functions_have_void_retval(self, trace_events):
        """PutMsg, ObtainSemaphore, ReleaseSemaphore have retval (void)."""
        void_funcs = ("PutMsg", "ObtainSemaphore", "ReleaseSemaphore")
        for func_name in void_funcs:
            matches = _find_events(trace_events, func_name)
            for ev in matches:
                assert ev["retval"] == "(void)", (
                    "{} seq={} retval should be '(void)', got: {}".format(
                        func_name, ev["seq"], ev["retval"]))

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
        """Rename event for the 23-char filename shows truncation indicator.

        The C test app calls Rename("RAM:atrace_test_ren_old", ...)
        where the old name is 23 characters -- the maximum string_data
        capacity.  The daemon should detect the truncation and append
        "..." to the formatted args.
        """
        matches = _find_events(trace_events, "Rename", "RAM:atrace_test_ren")
        assert len(matches) >= 1, (
            "No Rename event with 'RAM:atrace_test_ren' found in "
            "{} events".format(len(trace_events)))
        ev = matches[0]
        assert "..." in ev["args"], (
            "Rename args should contain '...' truncation indicator, "
            "got: {}".format(ev["args"]))

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
        """Functions with has_string=0 do not show quoted strings in args.

        GetMsg, PutMsg, AllocMem, Close, ObtainSemaphore, and
        ReleaseSemaphore have no string parameter.  Their args should
        never start with a double quote character, which would indicate
        stale string_data leaking from a previous ring buffer entry.
        """
        no_string_funcs = (
            "GetMsg", "PutMsg", "AllocMem", "Close",
            "ObtainSemaphore", "ReleaseSemaphore",
        )
        for func_name in no_string_funcs:
            matches = _find_events(trace_events, func_name)
            for ev in matches:
                assert not ev["args"].startswith('"'), (
                    "{} seq={} args starts with '\"' -- possible stale "
                    "string_data leak: {}".format(
                        func_name, ev["seq"], ev["args"]))
