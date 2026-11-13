#!/usr/bin/env python3
"""Restart the kiosk browser after an Adreno GPU fault.

On some boots the a306 GPU hits "CP opcode error" during WebKit's initial page render, recovers, and
leaves the browser's GL context wedged (screen frozen on whatever frame was up). Weston itself keeps
working; only the client with the faulted context is stuck.

Watching kmsg for the recovery line and killing thinksmart-kiosk.py makes thinksmart-kiosk-app launch it
again and it loads the page afresh. A fault that didn't freeze anything costs a page reload.
Started before the kiosk session, so a fault right after boot is seen. Runs as root (kernel log, pkill).
"""
import os
import re
import selectors
import signal
import subprocess
import sys
import time

FAULT = re.compile(r'hangcheck recover|gpu fault|opcode error|preemption timed out')
SETTLE = 5        # seconds: let the GPU recover and the burst of fault messages end
MIN_GAP = 60      # seconds between restarts, so a fault loop can't keep the screen restarting
KIOSK = '^/usr/bin/python3 /usr/local/bin/thinksmart-kiosk.py'
OVERLAY_FLAGS = ('/run/user/1000/uxplay-mirroring-active', '/run/user/1000/intercom-call-active')


def log(msg):
    print(msg, flush=True)


def main():
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    log('watching the kernel log for GPU faults')
    kmsg = subprocess.Popen(['journalctl', '-k', '-f', '-n0', '-o', 'cat'], stdout=subprocess.PIPE)
    os.set_blocking(kmsg.stdout.fileno(), False)
    sel = selectors.DefaultSelector()
    sel.register(kmsg.stdout, selectors.EVENT_READ)
    try:
        _watch(kmsg, sel)
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        sel.close()
        kmsg.terminate()
        try:
            kmsg.wait(timeout=2)
        except subprocess.TimeoutExpired:
            kmsg.kill()
            kmsg.wait()
        if kmsg.stdout:
            kmsg.stdout.close()


def _watch(kmsg, sel):
    last_restart = None
    fault_at = None
    deferred_logged = False
    pending = b''
    while True:
        timeout = None if fault_at is None else max(0, fault_at + SETTLE - time.monotonic())
        if sel.select(timeout):
            try:
                chunk = os.read(kmsg.stdout.fileno(), 65536)
            except BlockingIOError:
                continue
            if not chunk:
                sys.exit('journalctl ended')
            lines = (pending + chunk).split(b'\n')
            pending = lines.pop()
            # A pipe read may end in the middle of a keyword or UTF-8 character.
            # Bound malformed/unterminated lines while keeping a keyword-sized overlap.
            if len(pending) > 65536:
                lines.append(pending)
                pending = pending[-128:]
            for raw in lines:
                line = raw.decode('utf-8', errors='replace')
                if FAULT.search(line):
                    log(f'GPU fault: {line.strip()[:160]}')
                    if fault_at is None:
                        fault_at = time.monotonic()
        # Busy kernel output must not starve a pending recovery deadline.
        if fault_at is None or time.monotonic() < fault_at + SETTLE:
            continue
        if any(os.path.exists(p) for p in OVERLAY_FLAGS):
            if not deferred_logged:
                log('overlay active (call/mirroring), deferring kiosk browser restart')
                deferred_logged = True
            fault_at = time.monotonic()
            continue
        deferred_logged = False
        fault_at = None
        if last_restart is not None and time.monotonic() - last_restart < MIN_GAP:
            log('restarted less than a minute ago, leaving it')
            continue
        last_restart = time.monotonic()
        # Resume any SIGSTOP'd WebKit/kiosk processes first so SIGTERM takes effect immediately and leaves no orphans
        subprocess.run(['pkill', '-CONT', '-u', 'ubuntu', '-f', r'(^|/)WebKit(Web|Network)Process( |$)'], stderr=subprocess.DEVNULL)
        subprocess.run(['pkill', '-CONT', '-u', 'ubuntu', '-f', KIOSK], stderr=subprocess.DEVNULL)
        r = subprocess.run(['pkill', '-u', 'ubuntu', '-f', KIOSK])
        log('kiosk browser restarted' if r.returncode == 0 else 'kiosk browser was not running')


if __name__ == '__main__':
    main()
