#!/usr/bin/env python3
"""Keep launchd restart decisions separate from a native child's exit code.

A planned stop returns zero only to suppress a launchd restart. It does not
certify application cleanup; shutdown logs and process exit must be checked.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from collections.abc import Sequence


def _lifecycle(event: str, child: subprocess.Popen | None, planned: bool,
               child_returncode: int | None) -> None:
    try:
        print(json.dumps({
            "event": event,
            "pid": os.getpid(),
            "child_pid": child.pid if child is not None else None,
            "planned_stop": planned,
            "child_returncode": child_returncode,
        }, sort_keys=True), flush=True)
    except (OSError, ValueError):
        # A closed log destination must not leave the model process orphaned.
        pass


def supervise(command: Sequence[str], *, restore_handlers: bool = True) -> int:
    """Run one child in launchd's process group without forced termination."""
    if (not command or isinstance(command, (str, bytes))
            or not all(isinstance(part, str) and "\0" not in part for part in command)
            or not os.path.isabs(command[0])):
        return 2

    stop_requested = False
    planned_stop = False
    term_sent = False
    child = None
    child_returncode = None
    started = False
    result = 1
    previous_handlers = {}

    def request_stop(_signum, _frame):
        nonlocal stop_requested
        # Popen, kill and wait belong in the main control flow, never here.
        stop_requested = True

    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, request_stop)

        if stop_requested:
            planned_stop = True
            result = 0
        else:
            try:
                # Keep the child in launchd's group so a supervisor crash does
                # not deliberately detach the child from launchd's cleanup.
                child = subprocess.Popen(list(command), start_new_session=False)
            except OSError:
                planned_stop = stop_requested
                result = 0 if planned_stop else 1

            if child is not None:
                _lifecycle("started", child, False, None)
                started = True
                while True:
                    child_returncode = child.poll()
                    if child_returncode is not None:
                        break
                    if stop_requested and not term_sent:
                        planned_stop = True
                        term_sent = True
                        try:
                            child.terminate()
                        except ProcessLookupError:
                            pass
                    try:
                        # This timeout only rechecks stop requests. It never
                        # limits how long application cleanup may take.
                        child_returncode = child.wait(timeout=0.2)
                        break
                    except subprocess.TimeoutExpired:
                        continue
                # Do not consult stop_requested after observing child exit:
                # a late signal must not disguise an unexpected child exit.
                result = 0 if planned_stop else 1
    except Exception:
        # Unexpected supervisor errors must not intentionally abandon a child.
        result = 1
    finally:
        try:
            if child is not None and child_returncode is None:
                if not term_sent:
                    term_sent = True
                    try:
                        child.terminate()
                    except ProcessLookupError:
                        pass
                child_returncode = child.wait()
        finally:
            if not started:
                _lifecycle("started", child, planned_stop, None)
            _lifecycle("stopped", child, planned_stop, child_returncode)
            if restore_handlers:
                for signum, previous in previous_handlers.items():
                    signal.signal(signum, previous)
    return result


def main() -> int:
    # A second stop signal in the return-to-SystemExit gap must not replace a
    # planned zero exit with SIGTERM and make launchd restart the service.
    return supervise(sys.argv[1:], restore_handlers=False)


if __name__ == "__main__":
    raise SystemExit(main())
