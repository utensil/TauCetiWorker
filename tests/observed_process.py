#!/usr/bin/env python3
"""Real subprocess output, bounded deadlines, status and cleanup regressions."""

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tauceti_worker.round_activity import STATUS_ENV, TOKEN_ENV, processes
from tauceti_worker.runtime_status import atomic_json, read_json


class ObservedProcessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "runtime.json"
        self.before = time.monotonic()
        atomic_json(self.path, {"round_work": {"token": "test", "agents": {}, "owned": {}, "progress": self.before}})
        self.env = {**os.environ, "PYTHONPATH": str(ROOT), TOKEN_ENV: "test", STATUS_ENV: str(self.path)}

    def tearDown(self):
        self.tmp.cleanup()

    def start(self, code, idle=0.6, maximum=4):
        return subprocess.Popen(
            [
                sys.executable,
                "-m",
                "tauceti_worker.observed_process",
                "--idle-seconds",
                str(idle),
                "--max-seconds",
                str(maximum),
                "--",
                sys.executable,
                "-c",
                code,
            ],
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def finish(self, proc, expected):
        stdout, stderr = proc.communicate(timeout=12)
        self.assertEqual(proc.returncode, expected, stderr.decode())
        return stdout

    def test_productive_single_command_renews_before_command_completion(self):
        status = read_json(self.path)
        caller = str(os.getpid())
        status["round_work"]["agents"] = {caller: processes()[caller], "0": {"pid": 0, "birth": "exited"}}
        atomic_json(self.path, status)
        p = self.start("import time; [(print('built module',i,flush=True),time.sleep(.15)) for i in range(12)]")
        time.sleep(0.9)
        self.assertIsNone(p.poll())
        work = read_json(self.path)["round_work"]
        self.assertGreater(work["progress"], self.before + 0.3)
        self.assertTrue(work["agents"])
        self.assertIn(caller, work["agents"])
        self.assertNotIn("0", work["agents"])
        self.assertIn(b"built module 11", self.finish(p, 0))
        self.assertNotIn("built module", self.path.read_text())

    def test_quiet_and_repeated_output_expire(self):
        for code in (
            "import time;time.sleep(10)",
            "import time;[(print('same',flush=True),time.sleep(.1)) for _ in range(100)]",
        ):
            with self.subTest(code=code):
                began = time.monotonic()
                self.finish(self.start(code), 124)
                self.assertLess(time.monotonic() - began, 3)

    def test_unique_noise_cannot_extend_absolute_deadline(self):
        began = time.monotonic()
        self.finish(
            self.start("import time;[(print(i,flush=True),time.sleep(.05)) for i in range(100)]", maximum=1), 124
        )
        self.assertLess(time.monotonic() - began, 3)

    def test_nonzero_exit_and_unterminated_output_preserved(self):
        output = self.finish(self.start("import sys;sys.stdout.write('failure detail');sys.exit(7)"), 7)
        self.assertEqual(output, b"failure detail")

    def test_stale_token_refuses_to_launch(self):
        self.env[TOKEN_ENV] = "old"
        marker = Path(self.tmp.name) / "launched"
        self.finish(self.start(f"from pathlib import Path;Path({str(marker)!r}).touch()"), 1)
        self.assertFalse(marker.exists())

    def test_interrupt_cleans_escaped_descendant(self):
        marker = Path(self.tmp.name) / "child"
        code = (
            "import subprocess,sys,time;from pathlib import Path;"
            "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],start_new_session=True);"
            f"Path({str(marker)!r}).write_text(str(p.pid));"
            "[(print(i,flush=True),time.sleep(.1)) for i in range(100)]"
        )
        p = self.start(code)
        try:
            deadline = time.monotonic() + 4
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(marker.exists())
            pid = marker.read_text()
            # Let the observer discover the child's separate session.
            time.sleep(0.8)
            p.send_signal(signal.SIGTERM)
            self.finish(p, 143)
            self.assertNotIn(pid, processes())
        finally:
            if p.poll() is None:
                p.kill()
                p.communicate()
            if marker.exists() and marker.read_text() in processes():
                os.kill(int(marker.read_text()), signal.SIGKILL)

    def test_round_change_cleans_running_child(self):
        p = self.start("import time;[(print(i,flush=True),time.sleep(.1)) for i in range(100)]")
        time.sleep(0.4)
        identities = read_json(self.path)["round_work"]["agents"]
        atomic_json(self.path, {"round_work": {"token": "replacement", "agents": {}, "progress": 123}})
        self.finish(p, 1)
        self.assertEqual(read_json(self.path)["round_work"]["progress"], 123)
        self.assertFalse(set(identities) & set(processes()))

    def test_invalid_deadlines_never_launch(self):
        for value in ("nan", "inf", "0", "-1"):
            self.finish(self.start("raise AssertionError('launched')", idle=value), 2)


if __name__ == "__main__":
    unittest.main()
