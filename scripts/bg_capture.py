#!/usr/bin/env python3
"""Background CNet:Control trace capture.

Runs for ~45 seconds total:
- Disables noise, starts trace
- Launches CNet:Control
- Waits 10s for pre-nag events
- Prints reminder about nag screen
- Waits 30s more for post-nag events
- Stops trace, writes results to /tmp/cnet_trace_results.txt
"""
import sys
import time
import threading

sys.path.insert(0, '/home/thomas/ClaudeProjects/amigactl/client')
from amigactl import AmigaConnection
from amigactl.protocol import send_command

HOST = '192.168.6.228'
OUTPUT = '/tmp/cnet_trace_results.txt'

events = []
lock = threading.Lock()
trace_error = [None]

def on_event(ev):
    with lock:
        events.append(ev)

print('Connecting for trace...')
trace_conn = AmigaConnection(HOST)
trace_conn.connect()

print('Disabling noise (OpenLibrary, Close, AllocMem)...')
trace_conn.trace_disable(funcs=['OpenLibrary', 'Close', 'AllocMem'])

def trace_thread():
    try:
        trace_conn.trace_start(on_event)
    except Exception as e:
        trace_error[0] = e

t = threading.Thread(target=trace_thread, daemon=True)
t.start()
time.sleep(0.5)

print('Connecting for control...')
ctrl_conn = AmigaConnection(HOST)
ctrl_conn.connect()

print('Launching CNet:Control...')
try:
    pid = ctrl_conn.execute_async('CNet:Control')
    print(f'CNet:Control started (pid={pid})')
except Exception as e:
    print(f'ERROR: {e}')
    send_command(trace_conn._sock, 'STOP')
    t.join(timeout=5)
    sys.exit(1)

print('Waiting 10s for pre-nag events...')
time.sleep(10)

with lock:
    pre_nag = len([e for e in events if e.get('type') == 'event'])
print(f'Pre-nag: {pre_nag} events captured so far')
print('*** USER: Please click OK on the CNet nag screen now ***')

print('Waiting 30s for post-nag events...')
time.sleep(30)

with lock:
    post_nag = len([e for e in events if e.get('type') == 'event'])
print(f'Post-nag: {post_nag} events total')

print('Stopping trace...')
send_command(trace_conn._sock, 'STOP')
t.join(timeout=15)

# Write results
with open(OUTPUT, 'w') as f:
    with lock:
        evt_list = [e for e in events if e.get('type') == 'event']
        f.write(f'Total events: {len(evt_list)}\n')
        f.write('=' * 100 + '\n')
        for ev in evt_list:
            lib = ev.get('lib', '')
            func = ev.get('func', '')
            task = ev.get('task', '')
            args = ev.get('args', '')
            retval = ev.get('retval', '')
            status = ev.get('status', '')
            seq = ev.get('seq', '')
            time_s = ev.get('time', '')
            lf = f'{lib}.{func}' if lib else func
            f.write(f'[{seq:>4}] {time_s:>12} {lf:28s} {task:24s} {args:60s} {retval:20s} {status}\n')

print(f'Results written to {OUTPUT}')

ctrl_conn.close()
trace_conn.close()
if trace_error[0]:
    print(f'Trace error: {trace_error[0]}')
print('Done.')
