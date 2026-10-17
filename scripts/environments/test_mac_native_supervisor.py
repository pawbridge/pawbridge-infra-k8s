import contextlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import mac_native_supervisor as supervisor


SCRIPT = Path(supervisor.__file__).resolve()


class SupervisorTests(unittest.TestCase):
    def run_child(self, code):
        return subprocess.run(
            [sys.executable, str(SCRIPT), sys.executable, "-c", code],
            capture_output=True, text=True, timeout=10,
        )

    def wait_for_file(self, path, process):
        deadline = time.monotonic() + 5
        while not path.exists():
            if process.poll() is not None or time.monotonic() >= deadline:
                self.fail("child did not reach the expected lifecycle state")
            time.sleep(0.02)

    def test_unexpected_child_exit_always_requests_restart(self):
        for child_code in (0, 7):
            with self.subTest(child_code=child_code):
                result = self.run_child(f"raise SystemExit({child_code})")
                self.assertEqual(1, result.returncode)
                events = [json.loads(line) for line in result.stdout.splitlines()]
                self.assertEqual(["started", "stopped"], [e["event"] for e in events])
                self.assertEqual(child_code, events[-1]["child_returncode"])
                self.assertFalse(events[-1]["planned_stop"])

    def test_repeated_parent_signal_waits_for_cleanup_and_forwards_term_once(self):
        for first_signal in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(first_signal=first_signal), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                code = """
import os, pathlib, signal, sys, time
root = pathlib.Path(sys.argv[1])
stopping = False
def stop(signum, frame):
    global stopping
    with (root / 'signals').open('a') as output:
        output.write(str(signum) + '\\n')
    stopping = True
signal.signal(signal.SIGTERM, stop)
(root / 'group').write_text(str(os.getpgrp()))
(root / 'ready').touch()
while not stopping:
    time.sleep(0.02)
(root / 'cleanup-started').touch()
time.sleep(0.6)
(root / 'cleanup-finished').touch()
raise SystemExit(7)
"""
                process = subprocess.Popen(
                    [sys.executable, str(SCRIPT), sys.executable, "-c", code, directory],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                try:
                    self.wait_for_file(root / "ready", process)
                    self.assertEqual(os.getpgrp(), int((root / "group").read_text()))
                    process.send_signal(first_signal)
                    self.wait_for_file(root / "cleanup-started", process)
                    process.send_signal(signal.SIGTERM)
                    time.sleep(0.15)
                    self.assertIsNone(process.poll(), "supervisor abandoned ongoing cleanup")
                    stdout, stderr = process.communicate(timeout=5)
                    self.assertEqual(0, process.returncode, stderr)
                    self.assertTrue((root / "cleanup-finished").exists())
                    self.assertEqual([str(int(signal.SIGTERM))], (root / "signals").read_text().splitlines())
                    stopped = json.loads(stdout.splitlines()[-1])
                    self.assertTrue(stopped["planned_stop"])
                    self.assertEqual(7, stopped["child_returncode"])
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.communicate(timeout=5)

    def test_spawn_failure_is_nonzero_without_command_leak(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "/no-such-pawbridge-executable", "private-argument"],
            capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(1, result.returncode)
        self.assertNotIn("private-argument", result.stdout + result.stderr)
        self.assertNotIn("no-such", result.stdout + result.stderr)
        self.assertIsNone(json.loads(result.stdout.splitlines()[-1])["child_pid"])

    def test_main_keeps_stop_handler_until_process_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wrapper = """
import pathlib, sys, time
import mac_native_supervisor
root = pathlib.Path(sys.argv[1])
child_code = "import pathlib, sys, time; pathlib.Path(sys.argv[1]).touch(); time.sleep(10)"
sys.argv = ['supervisor', sys.executable, '-c', child_code, str(root / 'ready')]
result = mac_native_supervisor.main()
(root / 'returned').touch()
# Widen the main-return-to-SystemExit boundary deterministically for the test.
time.sleep(0.4)
raise SystemExit(result)
"""
            process = subprocess.Popen(
                [sys.executable, "-c", wrapper, directory], cwd=SCRIPT.parent,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            try:
                self.wait_for_file(root / "ready", process)
                process.send_signal(signal.SIGTERM)
                self.wait_for_file(root / "returned", process)
                process.send_signal(signal.SIGTERM)
                process.send_signal(signal.SIGINT)
                _, stderr = process.communicate(timeout=5)
                self.assertEqual(0, process.returncode, stderr)
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.communicate(timeout=5)

    def test_empty_or_relative_command_is_rejected(self):
        for command in ([], ["python"], "python", [sys.executable, "bad\0argument"]):
            with self.subTest(command=command):
                self.assertEqual(2, supervisor.supervise(command))

    def test_stop_before_spawn_does_not_spawn_and_restores_handlers(self):
        originals = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
        real_signal = signal.signal

        def install(signum, handler):
            previous = real_signal(signum, handler)
            if signum == signal.SIGINT and callable(handler) and handler not in originals.values():
                handler(signal.SIGTERM, None)
            return previous

        with patch.object(supervisor.signal, "signal", side_effect=install), \
                patch.object(supervisor.subprocess, "Popen") as popen, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, supervisor.supervise([sys.executable]))
        popen.assert_not_called()
        for signum, handler in originals.items():
            self.assertIs(handler, signal.getsignal(signum))

    def test_stop_during_spawn_is_forwarded_after_spawn_returns(self):
        child = Mock(pid=123)
        child.poll.return_value = None
        child.wait.return_value = 0

        def spawn(*args, **kwargs):
            self.assertFalse(kwargs["start_new_session"])
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            return child

        with patch.object(supervisor.subprocess, "Popen", side_effect=spawn), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, supervisor.supervise([sys.executable]))
        child.terminate.assert_called_once_with()

    def test_late_signal_does_not_reclassify_observed_child_exit(self):
        child = Mock(pid=123)

        def poll():
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            return 0

        child.poll.side_effect = poll
        with patch.object(supervisor.subprocess, "Popen", return_value=child), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(1, supervisor.supervise([sys.executable]))
        child.terminate.assert_not_called()

    def test_unexpected_supervisor_error_terminates_and_reaps_child(self):
        child = Mock(pid=123)
        child.poll.side_effect = RuntimeError("simulated control failure")
        child.wait.return_value = 0
        with patch.object(supervisor.subprocess, "Popen", return_value=child), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(1, supervisor.supervise([sys.executable]))
        child.terminate.assert_called_once_with()
        child.wait.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
