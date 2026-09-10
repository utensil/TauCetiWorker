#!/usr/bin/env python3
"""Model-free deadline, native-lock and escaped-child regressions."""

import fcntl
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tauceti_worker import round_activity as a
from tauceti_worker.round import RoundContext, run_round_subprocess
from tauceti_worker.runtime_status import atomic_json, read_json

CHILD = """
import json,os,signal,subprocess,sys,time
from pathlib import Path
from types import SimpleNamespace
from tauceti_worker.round import RoundContext
from tauceti_worker.round_activity import Activity
state=Path(sys.argv[1]);mode=sys.argv[2]
with RoundContext(SimpleNamespace(state=state,wid='test')):
    activity=Activity(os.getpid())
    if mode=='lost-claim':
        activity.observe(json.dumps({'type':'item.completed','item':{'type':'command_execution','command':'inspect','exit_code':0,'aggregated_output':'fresh'}}))
        # cmd_heartbeat uses this signal after renewal reports ownership loss.
        os.kill(os.getpid(),signal.SIGTERM)
    elif mode=='progress':
        for i in range(10):
            activity.observe(json.dumps({'type':'item.completed','item':{'type':'command_execution','command':'inspect '+str(i),'exit_code':0,'aggregated_output':str(i)}}))
            time.sleep(.07)
    elif mode=='quiet':
        time.sleep(10)
    elif mode=='delegated':
        from tauceti_worker.round_activity import observed_command
        command="import json,time;[(print(json.dumps({'type':'item.completed','item':{'type':'command_execution','command':'inspect '+str(i),'exit_code':0,'aggregated_output':str(i)}}),flush=True),time.sleep(.07)) for i in range(10)]"
        observed_command([sys.executable,'-c',command])
    elif mode in ('escaped','orphan'):
        p=subprocess.Popen([sys.executable,'-c','import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(10)'],start_new_session=True)
        Activity(p.pid)
        (state/'escaped-pid').write_text(str(p.pid))
        time.sleep(10 if mode=='orphan' else .1)
        os._exit(0)
"""


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self.path = self.state / "runtime.json"
        self.cfg = SimpleNamespace(state=self.state, checkout=self.state / "checkout", wid="test")
        self.env = patch.dict(
            os.environ,
            {
                a.TOKEN_ENV: "test",
                a.STATUS_ENV: str(self.path),
            },
        )
        self.env.start()
        self.poll = patch.object(a, "POLL_INTERVAL", 0.05)
        self.poll.start()
        atomic_json(
            self.path, {"round_work": {"token": "test", "agents": {}, "owned": {}, "progress": time.monotonic()}}
        )

    def tearDown(self):
        self.poll.stop()
        self.env.stop()
        self.tmp.cleanup()

    def test_events_ignore_duplicates_polling_errors_and_chatter(self):
        observer = a.Activity(os.getpid())
        event = {
            "type": "item.completed",
            "item": {"type": "command_execution", "command": "rg lemma", "exit_code": 0, "aggregated_output": "found"},
        }
        with patch.object(a.time, "monotonic", return_value=10):
            observer.observe(json.dumps(event))
        with patch.object(a.time, "monotonic", return_value=20):
            observer.observe(json.dumps(event))
            for raw in ["bad-json", '{"type":"error"}', '{"type":"turn.started"}', "[]"]:
                observer.observe(raw)
            for command in ["ps -ef", "tail build.log", "sleep 1", "date"]:
                event["item"]["command"] = command
                observer.observe(json.dumps(event))
            event["item"].update(command="curl service", exit_code=1, aggregated_output="changing network failure")
            observer.observe(json.dumps(event))
        work = read_json(self.path)["round_work"]
        self.assertEqual(work["progress"], 10)
        self.assertNotIn("rg lemma", self.path.read_text())
        self.assertNotIn("found", self.path.read_text())

    def test_registration_does_not_reset_idle_clock(self):
        before = read_json(self.path)["round_work"]["progress"]
        a.Activity(os.getpid())
        self.assertEqual(read_json(self.path)["round_work"]["progress"], before)

    def test_old_observer_cannot_refresh_new_round(self):
        observer = a.Activity(os.getpid())
        atomic_json(self.path, {"round_work": {"token": "replacement", "agents": {}}})
        with self.assertRaisesRegex(RuntimeError, "identity"):
            observer.observe(
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "file_change", "status": "completed", "changes": ["fixture"]},
                    }
                )
            )

    def run_round(self, mode, timeout=0.3):
        with (
            patch.object(a.Config, "resolve", return_value=self.cfg),
            patch("tauceti_worker.round.self_argv", return_value=[sys.executable, "-c", CHILD, str(self.state), mode]),
        ):
            return run_round_subprocess([], timeout)

    def test_live_work_extends_same_round(self):
        started = time.monotonic()
        self.assertEqual(self.run_round("progress"), 0)
        self.assertGreater(time.monotonic() - started, 0.7)
        self.assertFalse(read_json(self.path)["round_work"]["owned"])

    def test_quiet_work_has_bounded_grace(self):
        started = time.monotonic()
        self.assertEqual(self.run_round("quiet"), 124)
        elapsed = time.monotonic() - started
        self.assertGreater(elapsed, 0.3)
        self.assertLess(elapsed, 5)

    def test_synchronous_delegate_extends_waiting_parent(self):
        self.assertEqual(self.run_round("delegated"), 0)
        work = read_json(self.path)["round_work"]
        self.assertGreaterEqual(len(work["agents"]), 2)
        self.assertFalse(work["owned"])

    def test_lost_claim_stop_overrides_fresh_progress(self):
        self.assertEqual(self.run_round("lost-claim"), 143)
        self.assertFalse(read_json(self.path)["round_work"]["owned"])

    def test_operator_interrupt_cleans_up_before_unlock(self):
        sleep = time.sleep
        interrupted = False

        def stop_once(seconds):
            nonlocal interrupted
            if not interrupted:
                interrupted = True
                raise KeyboardInterrupt
            sleep(seconds)

        with patch.object(a.time, "sleep", side_effect=stop_once), self.assertRaises(KeyboardInterrupt):
            self.run_round("quiet")
        self.assertFalse(read_json(self.path)["round_work"]["owned"])
        with (self.state / "round.lock").open("a+") as peer:
            fcntl.flock(peer, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_supervisor_holds_lock_after_child_exit_until_escaped_cleanup(self):
        original = a.processes
        checked = []

        def sample():
            snapshot = original()
            pidfile = self.state / "escaped-pid"
            if pidfile.exists() and pidfile.read_text() in snapshot:
                with (self.state / "round.lock").open("a+") as peer:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(peer, fcntl.LOCK_EX | fcntl.LOCK_NB)
                checked.append(True)
            return snapshot

        with patch.object(a, "processes", side_effect=sample):
            self.assertEqual(self.run_round("escaped"), 0)
        self.assertTrue(checked)
        self.assertNotIn((self.state / "escaped-pid").read_text(), original())
        with (self.state / "round.lock").open("a+") as peer:
            fcntl.flock(peer, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_one_shot_refuses_recorded_survivor_without_inherited_fd(self):
        identity = a.processes()[str(os.getpid())]
        atomic_json(self.path, {"round_work": {"agents": {}, "owned": {str(os.getpid()): identity}}})
        with self.assertRaisesRegex(Exception, "still owns live work"), RoundContext(self.cfg):
            self.fail("a new mutator entered")

    def test_missing_snapshot_keeps_lock_until_cleanup_can_be_verified(self):
        original = a.processes
        failures = 0

        def unavailable():
            nonlocal failures
            if failures < 2:
                failures += 1
                with (self.state / "round.lock").open("a+") as peer:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(peer, fcntl.LOCK_EX | fcntl.LOCK_NB)
                raise ValueError("snapshot unavailable")
            return original()

        with patch.object(a, "processes", side_effect=unavailable), self.assertRaisesRegex(ValueError, "snapshot"):
            self.run_round("quiet")
        self.assertEqual(failures, 2)
        self.assertFalse(read_json(self.path)["round_work"]["owned"])

    def test_parent_death_retains_lock_then_refuses_recorded_orphan(self):
        driver = f"""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from tauceti_worker import round_activity as a
a.POLL_INTERVAL=.05
state=Path(sys.argv[1])
cfg=SimpleNamespace(state=state,checkout=state/'checkout',wid='test')
with patch.object(a.Config,'resolve',return_value=cfg), patch('tauceti_worker.round.self_argv',return_value=[sys.executable,'-c',{CHILD!r},str(state),'orphan']):
    a.supervise([],30)
"""
        supervisor = subprocess.Popen([sys.executable, "-c", driver, str(self.state)])
        owned = {}
        try:
            until = time.monotonic() + 5
            while time.monotonic() < until:
                owned = read_json(self.path).get("round_work", {}).get("owned", {})
                if (self.state / "escaped-pid").exists():
                    escaped = (self.state / "escaped-pid").read_text()
                    if escaped in owned:
                        break
                time.sleep(0.05)
            else:
                self.fail("supervisor did not record its escaped child")
            supervisor.kill()
            supervisor.wait()
            with (self.state / "round.lock").open("a+") as peer:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(peer, fcntl.LOCK_EX | fcntl.LOCK_NB)
            leader = next(identity for identity in owned.values() if identity["parent"] == supervisor.pid)
            os.kill(leader["pid"], signal.SIGTERM)
            until = time.monotonic() + 5
            while a.alive(leader, a.processes()) and time.monotonic() < until:
                time.sleep(0.05)
            self.assertFalse(a.alive(leader, a.processes()))
            with self.assertRaisesRegex(Exception, "still owns live work"), RoundContext(self.cfg):
                self.fail("a new mutator entered with an orphan alive")
        finally:
            if supervisor.poll() is None:
                supervisor.kill()
                supervisor.wait()
            for identity in owned.values():
                if a.alive(identity, a.processes()):
                    os.kill(identity["pid"], signal.SIGKILL)


if __name__ == "__main__":
    unittest.main()
