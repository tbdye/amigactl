#!/usr/bin/env python3
"""Capture atrace events during CNet:Control startup and compare to SnoopDOS reference.

Usage:
    python3 capture_cnet_trace.py

The script connects to the Amiga at 192.168.6.228, starts a trace, launches
CNet:Control, waits for the user to dismiss the nag screen, collects remaining
events, then compares the capture against the 26-entry SnoopDOS reference log.
"""

import sys
import threading
import time

sys.path.insert(0, '/home/thomas/ClaudeProjects/amigactl/client')

from amigactl import AmigaConnection

HOST = '192.168.6.228'

# ---------------------------------------------------------------------------
# SnoopDOS reference data
#
# Each entry is a dict with:
#   num      -- SnoopDOS line number (1-based)
#   process  -- process name fragment (case-insensitive match against task)
#   action   -- SnoopDOS action name
#   target   -- file/command target (empty string if none, case-insensitive match)
#   options  -- SnoopDOS options field (e.g. "Read", "Write", "Single", "")
#   result   -- "OK" or "Fail" (or "" for ChangeDir which has no result)
#
# atrace function mapping:
#   Open (Read)  -> dos.Open, args contain MODE_OLDFILE / "oldfile" / "1005"
#   Open (Write) -> dos.Open, args contain MODE_NEWFILE / "newfile" / "1006"
#   Execute      -> dos.Execute
#   Load         -> dos.LoadSeg or dos.NewLoadSeg
#   ChangeDir    -> dos.CurrentDir
# ---------------------------------------------------------------------------

SNOPDOS_REF = [
    # num  process              action     target                           options   result
    { 'num':  1, 'process': 'cnet:control', 'action': 'Open',    'target': '*',                        'options': 'Read',   'result': 'OK'   },
    { 'num':  2, 'process': 'cnet:control', 'action': 'Open',    'target': 'cnet:big_numbers',         'options': 'Read',   'result': 'OK'   },
    { 'num':  3, 'process': 'cnet:control', 'action': 'Open',    'target': 'cnet:bbsconfig3',          'options': 'Read',   'result': 'OK'   },
    { 'num':  4, 'process': 'cnet:control', 'action': 'Open',    'target': 'cnet:bbslicense',         'options': 'Read',   'result': 'OK'   },
    { 'num':  5, 'process': 'cnet:control', 'action': 'Open',    'target': 'cnet:bbslicense',         'options': 'Write',  'result': 'OK'   },
    { 'num':  6, 'process': 'cnet:control', 'action': 'Open',    'target': 'cnet:bbsmenu',            'options': 'Read',   'result': 'OK'   },
    { 'num':  7, 'process': 'cnet:control', 'action': 'Open',    'target': 'cnet:bbstext',            'options': 'Read',   'result': 'OK'   },
    { 'num':  8, 'process': 'cnet:control', 'action': 'Open',    'target': 'sysdata:bbs.sam',         'options': 'Read',   'result': 'OK'   },
    { 'num':  9, 'process': 'cnet:control', 'action': 'Open',    'target': 'sysdata:bbs.sdata',       'options': 'Read',   'result': 'OK'   },
    { 'num': 10, 'process': 'cnet:control', 'action': 'Open',    'target': 'sysdata:passwords',       'options': 'Read',   'result': 'Fail' },
    { 'num': 11, 'process': 'cnet:control', 'action': 'Open',    'target': 'cnet:bbsroom0',           'options': 'Read',   'result': 'OK'   },
    { 'num': 12, 'process': 'cnet:control', 'action': 'Open',    'target': 'sysdata:subboards3',      'options': 'Read',   'result': 'Fail' },
    { 'num': 13, 'process': 'cnet:control', 'action': 'Open',    'target': 'sysdata:bbs.ukeys3',      'options': 'Read',   'result': 'OK'   },
    { 'num': 14, 'process': 'cnet:control', 'action': 'Open',    'target': 'sysdata:bbs.uind1',       'options': 'Read',   'result': 'OK'   },
    { 'num': 15, 'process': 'cnet:control', 'action': 'Open',    'target': 'sysdata:bbs.uind2',       'options': 'Read',   'result': 'OK'   },
    { 'num': 16, 'process': 'cnet:control', 'action': 'Open',    'target': 'sysdata:log/calls',       'options': 'Read',   'result': 'OK'   },
    { 'num': 17, 'process': 'cnet:control', 'action': 'Open',    'target': 'sysdata:bbs.adata',       'options': 'Read',   'result': 'Fail' },
    { 'num': 18, 'process': 'cnet:control', 'action': 'Open',    'target': 'cnet:bbscontrol3',        'options': 'Read',   'result': 'OK'   },
    { 'num': 19, 'process': 'cnet:control', 'action': 'Execute', 'target': 'run >nil: cnet:bbs 0',    'options': 'Single', 'result': 'OK'   },
    { 'num': 20, 'process': 'cnet:bbs',     'action': 'Open',    'target': '*',                       'options': 'Read',   'result': 'OK'   },
    { 'num': 21, 'process': 'ramlib',       'action': 'Load',    'target': 'libs:traplist.library',   'options': '',       'result': 'Fail' },
    { 'num': 22, 'process': 'ramlib',       'action': 'Load',    'target': 'traplist.library',        'options': '',       'result': 'Fail' },
    { 'num': 23, 'process': 'cnet:bbs',     'action': 'Open',    'target': 'cnet:bbsport0',           'options': 'Read',   'result': 'OK'   },
    { 'num': 24, 'process': 'cnet:control', 'action': 'Open',    'target': 'cnet:bbsevents',          'options': 'Read',   'result': 'OK'   },
    { 'num': 25, 'process': 'cnet:bbs',     'action': 'ChangeDir','target': 'system:programs/cnet',   'options': '',       'result': ''     },
    { 'num': 26, 'process': 'cnet:bbs',     'action': 'Open',    'target': 'sysdata:log/port0',       'options': 'Read',   'result': 'Fail' },
]

# ---------------------------------------------------------------------------
# Mode token sets used for Open direction detection.
# atrace formats the mode argument in various ways; we check for known tokens.
# MODE_OLDFILE = 1005, MODE_NEWFILE = 1006.
# ---------------------------------------------------------------------------
READ_TOKENS  = {'oldfile', '1005', 'mode_oldfile', 'read'}
WRITE_TOKENS = {'newfile', '1006', 'mode_newfile', 'write'}


def _args_lower(event):
    """Return the args field of an event, lower-cased."""
    return event.get('args', '').lower()


def _task_lower(event):
    """Return the task field of an event, lower-cased."""
    return event.get('task', '').lower()


def _func_lower(event):
    """Return the lib.func identifier, lower-cased."""
    lib = event.get('lib', '').lower()
    func = event.get('func', '').lower()
    if lib:
        return '{}.{}'.format(lib, func)
    return func


def _event_succeeded(event):
    """Return True if the atrace event status indicates success."""
    status = event.get('status', '').lower()
    retval = event.get('retval', '').lower()
    # status field: "OK", "ok", or "-" often accompanies a non-zero retval
    if status in ('ok',):
        return True
    if status in ('fail', 'err', 'error'):
        return False
    # Fall back to retval: "0" / "null" / "nil" / "" => fail for Open
    # For Open: non-zero BPTR is success; 0/NULL is fail.
    # For Execute: 0 return value is success (BOOL TRUE = -1 in AmigaOS, but
    # dos.Execute returns IoErr on failure; treat non-empty non-zero as success).
    # Use status field as authoritative when present.
    return status not in ('fail', 'err', 'error', '0', 'null', 'nil')


def _event_status_label(event):
    """Return 'OK' or 'Fail' based on the atrace event."""
    status = event.get('status', '').lower()
    if status == 'ok':
        return 'OK'
    if status in ('fail', 'err', 'error'):
        return 'Fail'
    # Unknown — report as-is
    return event.get('status', '?')


def match_ref_entry(ref, events):
    """Find the best-matching atrace event for a SnoopDOS reference entry.

    Returns (event, reason) where event is the matching event dict or None,
    and reason is a short explanation string for diagnostics.
    """
    action   = ref['action']
    process  = ref['process'].lower()   # e.g. 'cnet:control', 'ramlib'
    target   = ref['target'].lower()    # e.g. 'cnet:big_numbers', '*'
    options  = ref['options'].lower()   # 'read', 'write', 'single', ''
    expected = ref['result']            # 'OK', 'Fail', ''

    candidates = []

    for ev in events:
        if ev.get('type') != 'event':
            continue

        task = _task_lower(ev)
        func = _func_lower(ev)
        args = _args_lower(ev)

        # --- Process match ---
        # SnoopDOS shows names like "cnet:control", "cnet:bbs", "ramlib".
        # atrace task field may be "cnet:control" or a longer string like
        # "cnet:control [7]" or just the basename.  We require the process
        # fragment to appear somewhere in the task field.
        if process not in task:
            continue

        # --- Function / action match ---
        if action == 'Open':
            if 'open' not in func:
                continue
            # Direction check via args
            if options == 'read':
                tokens = set(args.replace(',', ' ').replace('(', ' ')
                             .replace(')', ' ').split())
                if not (tokens & READ_TOKENS):
                    # No read token found — still include as weak candidate
                    pass
            elif options == 'write':
                tokens = set(args.replace(',', ' ').replace('(', ' ')
                             .replace(')', ' ').split())
                if not (tokens & WRITE_TOKENS):
                    pass

        elif action == 'Execute':
            if 'execute' not in func:
                continue

        elif action == 'Load':
            if 'loadseg' not in func and 'load' not in func:
                continue

        elif action == 'ChangeDir':
            if 'currentdir' not in func and 'changedir' not in func and 'cd' not in func:
                continue

        # --- Target match ---
        if target == '*':
            # Wildcard: matches any args (including empty)
            target_ok = True
        else:
            # The target path should appear somewhere in args.
            # Strip leading/trailing whitespace, compare case-insensitively.
            # For partial paths (e.g. 'traplist.library') accept substring match.
            target_ok = target in args

        if not target_ok:
            continue

        candidates.append(ev)

    if not candidates:
        return None, 'no matching event found'

    # If multiple candidates, prefer the one whose result matches expected.
    if expected:
        for ev in candidates:
            label = _event_status_label(ev)
            if label == expected:
                return ev, 'matched (result={})'.format(label)

    # Return first candidate even if result differs.
    ev = candidates[0]
    label = _event_status_label(ev)
    return ev, 'matched but result={} (expected {})'.format(label, expected)


def print_event(ev, prefix=''):
    """Print a single atrace event in a readable format."""
    if ev.get('type') == 'comment':
        print('{}  # {}'.format(prefix, ev.get('text', '')))
        return
    seq    = ev.get('seq', '')
    time_  = ev.get('time', '')
    lib    = ev.get('lib', '')
    func   = ev.get('func', '')
    task   = ev.get('task', '')
    args   = ev.get('args', '')
    retval = ev.get('retval', '')
    status = ev.get('status', '')
    lf = '{}.{}'.format(lib, func) if lib else func
    print('{}{:>5}  {:>10}  {:28s}  {:22s}  {}  {} -> {}'.format(
        prefix, seq, time_, lf, task[:22], args[:50], retval, status))


def main():
    events = []
    lock = threading.Lock()
    trace_done = threading.Event()

    def on_event(ev):
        with lock:
            events.append(ev)
        # Print live so the user can see activity.
        if ev.get('type') == 'event':
            lib  = ev.get('lib', '')
            func = ev.get('func', '')
            task = ev.get('task', '')
            args = ev.get('args', '')
            status = ev.get('status', '')
            lf = '{}.{}'.format(lib, func) if lib else func
            print('  [{:>4}] {:24s}  {:20s}  {}  {}'.format(
                ev.get('seq', ''), lf, task[:20], args[:60], status))

    print('Connecting to {} for trace...'.format(HOST))
    trace_conn = AmigaConnection(HOST)
    trace_conn.connect()

    print('Disabling noisy functions (OpenLibrary, Close)...')
    trace_conn.trace_disable(funcs=['OpenLibrary', 'Close'])

    # Start trace in a background thread so we can issue commands on a second
    # connection while trace_start() blocks.
    trace_ex = [None]  # container for thread exception

    def trace_thread():
        try:
            trace_conn.trace_start(on_event)
        except Exception as e:
            trace_ex[0] = e
        finally:
            trace_done.set()

    t = threading.Thread(target=trace_thread, name='trace-reader', daemon=True)
    t.start()

    # Give the trace thread a moment to enter its read loop.
    time.sleep(0.5)

    print('\nConnecting to {} for control commands...'.format(HOST))
    ctrl_conn = AmigaConnection(HOST)
    ctrl_conn.connect()

    print('Launching CNet:Control...')
    try:
        pid = ctrl_conn.execute_async('CNet:Control')
        print('CNet:Control started (pid={}).'.format(pid))
    except Exception as e:
        print('ERROR launching CNet:Control: {}'.format(e))
        # Stop trace and bail out.
        try:
            trace_conn.stop_trace()
        except Exception:
            pass
        ctrl_conn.close()
        trace_conn.close()
        sys.exit(1)

    print()
    print('CNet:Control started. Please click OK on the nag screen when it appears.')
    try:
        input('Press Enter here once you have dismissed the nag screen... ')
    except EOFError:
        pass

    print('Waiting 5 more seconds for remaining events...')
    time.sleep(5)

    print('Stopping trace...')
    try:
        trace_conn.stop_trace()
    except Exception as e:
        print('Warning: stop_trace error: {}'.format(e))

    # Wait for the trace thread to finish (it will exit once END is received).
    t.join(timeout=15)
    if t.is_alive():
        print('Warning: trace thread did not exit within 15s.')

    ctrl_conn.close()
    trace_conn.close()

    if trace_ex[0] is not None:
        print('Warning: trace thread raised: {}'.format(trace_ex[0]))

    # -----------------------------------------------------------------------
    # Print all captured events
    # -----------------------------------------------------------------------
    print()
    print('=' * 72)
    print('CAPTURED EVENTS ({} total)'.format(len(events)))
    print('=' * 72)
    print('{:>5}  {:>10}  {:28s}  {:22s}  {}'.format(
        'seq', 'time', 'lib.func', 'task', 'args  retval  status'))
    print('-' * 72)
    for ev in events:
        print_event(ev)

    # Filter to only trace events (skip comment lines) for comparison.
    trace_events = [ev for ev in events if ev.get('type') == 'event']

    # -----------------------------------------------------------------------
    # Compare against SnoopDOS reference
    # -----------------------------------------------------------------------
    print()
    print('=' * 72)
    print('COMPARISON AGAINST SNOPDOS REFERENCE (26 entries)')
    print('=' * 72)
    print('{:>4}  {:14s}  {:8s}  {:32s}  {:7s}  {}'.format(
        '#', 'process', 'action', 'target', 'result', 'verdict'))
    print('-' * 72)

    matched   = 0
    missing   = 0
    wrong_res = 0

    for ref in SNOPDOS_REF:
        ev, reason = match_ref_entry(ref, trace_events)
        num     = ref['num']
        process = ref['process']
        action  = ref['action']
        target  = ref['target']
        expected_result = ref['result']

        if ev is None:
            verdict = 'MISSING'
            missing += 1
        else:
            label = _event_status_label(ev)
            if expected_result == '' or label == expected_result:
                verdict = 'MATCH'
                matched += 1
            else:
                verdict = 'MATCH(result mismatch: got {}, expected {})'.format(
                    label, expected_result)
                wrong_res += 1
                matched += 1  # still structurally matched

        print('{:>4}  {:14s}  {:8s}  {:32s}  {:7s}  {}'.format(
            num,
            process[:14],
            action[:8],
            target[:32],
            expected_result if expected_result else '(none)',
            verdict,
        ))

        if ev is not None:
            # Print the matching event for inspection.
            lib  = ev.get('lib', '')
            func = ev.get('func', '')
            lf   = '{}.{}'.format(lib, func) if lib else func
            args = ev.get('args', '')
            stat = ev.get('status', '')
            seq  = ev.get('seq', '')
            task = ev.get('task', '')
            print('      -> [{:>4}] {}.{}  task={}  args={}  status={}'.format(
                seq, lib, func, task, args[:60], stat))

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------
    print()
    print('=' * 72)
    print('SUMMARY')
    print('=' * 72)
    total = len(SNOPDOS_REF)
    print('  Reference entries : {}'.format(total))
    print('  Matched           : {}'.format(matched))
    print('  Missing           : {}'.format(missing))
    print('  Result mismatches : {}'.format(wrong_res))
    print('  Total events cap. : {}'.format(len(trace_events)))
    if missing == 0 and wrong_res == 0:
        print()
        print('  PERFECT MATCH -- all 26 SnoopDOS entries found in atrace output.')
    elif missing == 0:
        print()
        print('  All calls structurally matched; {} result mismatch(es).'.format(wrong_res))
    else:
        print()
        print('  {} of {} reference entries were NOT captured.'.format(missing, total))


if __name__ == '__main__':
    main()
