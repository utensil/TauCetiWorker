#!/usr/bin/env python3
"""Real-process recovery after supervisor timeout, plus fail-closed checkout fixtures."""

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from tauceti_worker import agents
from tauceti_worker import checkout_recovery as cr
from tauceti_worker import round as rounds
from tauceti_worker import round_activity as ra
from tauceti_worker.constants import MAX_INFRA_REFUNDS
from tauceti_worker.survey import Counters


class Recovery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        co = self.root / "checkout"
        subprocess.run(["git", "init", "-q", "-b", "main", str(co)], check=True)
        self.cfg = SimpleNamespace(checkout=co, state=self.root / "state", wid="fixture")
        self.cfg.state.mkdir()
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test.invalid")
        (co / "tracked").write_text("base\n")
        self.git("add", "tracked")
        self.git("commit", "-qm", "base")
        self.head = self.git("rev-parse", "HEAD")

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.cfg.checkout), *args], text=True).strip()

    def dirty(self):
        (self.cfg.checkout / "tracked").write_text("base\nstaged\n")
        self.git("add", "tracked")
        (self.cfg.checkout / "tracked").write_text("base\nstaged\nunstaged\n")
        (self.cfg.checkout / "new.lean").write_bytes(b"-- recover me\n")

    def assert_saved(self):
        meta = json.loads((self.cfg.state / "resume" / f"77-{self.head[:12]}.json").read_text())
        self.assertFalse(self.git("status", "--porcelain"))
        self.assertFalse((self.cfg.state / "active-repair.json").exists())
        self.git("stash", "apply", "--index", meta["stash_ref"])
        self.assertEqual(self.git("show", ":tracked"), "base\nstaged")
        self.assertEqual((self.cfg.checkout / "tracked").read_text(), "base\nstaged\nunstaged\n")
        self.assertEqual((self.cfg.checkout / "new.lean").read_bytes(), b"-- recover me\n")

    def repair_argv(self):
        child = """import os,sys,time
from pathlib import Path
from types import SimpleNamespace
from tauceti_worker.checkout_recovery import arm_repair
from tauceti_worker.survey import Counters
co,state,head=sys.argv[1:]
cfg=SimpleNamespace(checkout=Path(co),state=Path(state))
key=f'fix-77-{head[:12]}'
Counters(cfg).write(key,3)
arm_repair(cfg,77,head,'fix',charged={key:3})
import subprocess
(cfg.checkout/'tracked').write_text('base\\nstaged\\n')
subprocess.run(['git','-C',co,'add','tracked'],check=True)
(cfg.checkout/'tracked').write_text('base\\nstaged\\nunstaged\\n')
(cfg.checkout/'new.lean').write_bytes(b'-- recover me\\n')
time.sleep(20)
"""
        return [sys.executable, "-c", child, str(self.cfg.checkout), str(self.cfg.state), self.head]

    def test_supervisor_timeout_preserves_after_writers_exit(self):
        with (
            patch.object(ra.Config, "resolve", return_value=self.cfg),
            patch.object(rounds, "self_argv", return_value=self.repair_argv()),
            patch.object(ra, "POLL_INTERVAL", 0.03),
        ):
            self.assertEqual(ra.supervise([], 0.5), 124)
        self.assertEqual(Counters(self.cfg).read(f"fix-77-{self.head[:12]}"), 3)
        self.assert_saved()

    def test_abrupt_supervisor_death_recovers_before_checkout(self):
        cr.arm_repair(self.cfg, 77, self.head, "fix")
        self.dirty()
        cr.preserve_before_checkout(self.cfg)
        self.assert_saved()

    def arm_charged(self, count=3):
        self.counter = Counters(self.cfg)
        self.key = f"fix-77-{self.head[:12]}"
        self.counter.write(self.key, count)
        cr.arm_repair(self.cfg, 77, self.head, "fix", charged={self.key: count})
        self.dirty()

    def test_operator_stop_refunds_only_after_preserving_candidate(self):
        self.arm_charged()
        cr.recover_interrupted_repair(self.cfg, operator_interrupted=True)
        self.assertEqual(self.counter.read(self.key), 2)
        self.assertEqual(self.counter.read("infra-fix-77"), 1)
        self.assert_saved()

    def test_unknown_death_retains_attempt(self):
        self.arm_charged()
        cr.recover_interrupted_repair(self.cfg)
        self.assertEqual(self.counter.read(self.key), 3)
        self.assert_saved()

    def test_interruption_refunds_are_bounded(self):
        self.arm_charged()
        self.counter.write("infra-fix-77", MAX_INFRA_REFUNDS)
        cr.recover_interrupted_repair(self.cfg, operator_interrupted=True)
        self.assertEqual(self.counter.read(self.key), 3)
        self.assert_saved()

    def test_refund_replay_finishes_before_next_survey_without_double_credit(self):
        # A private commit plus dirty work must retain the original stash on replay.
        (self.cfg.checkout / "committed").write_text("private candidate\n")
        self.git("add", "committed")
        self.git("commit", "-qm", "private candidate")
        self.arm_charged()
        write = Counters.write

        def fail_after_decrement(counters, name, value):
            if name == "infra-fix-77":
                raise OSError("interrupted counter write")
            write(counters, name, value)

        with patch.object(Counters, "write", fail_after_decrement):
            with self.assertRaises(OSError):
                cr.recover_interrupted_repair(self.cfg, operator_interrupted=True)
        self.assertEqual(self.counter.read(self.key), 2)
        # The next native admission replays durable targets before it can survey or charge again.
        with rounds.RoundContext(self.cfg):
            self.assertEqual(self.counter.read(self.key), 2)
            self.assertEqual(self.counter.read("infra-fix-77"), 1)
            self.counter.incr(self.key)
        self.assertEqual(self.counter.read(self.key), 3)
        self.assert_saved()

    def test_crash_before_refund_journal_keeps_private_candidate_stash(self):
        (self.cfg.checkout / "committed").write_text("private candidate\n")
        self.git("add", "committed")
        self.git("commit", "-qm", "private candidate")
        self.arm_charged()
        atomic_json = cr.atomic_json

        def fail_refund_journal(path, data):
            if "interruption_refund" in data:
                raise OSError("interrupted before refund journal")
            atomic_json(path, data)

        with patch.object(cr, "atomic_json", fail_refund_journal):
            with self.assertRaises(OSError):
                cr.recover_interrupted_repair(self.cfg, operator_interrupted=True)
        cr.recover_interrupted_repair(self.cfg)
        self.assertEqual(self.counter.read(self.key), 3)
        self.assert_saved()

    def test_supervisor_operator_interrupt_reaches_refund_path(self):
        sleep = time.sleep
        interrupted = False

        def interrupt_once(delay):
            nonlocal interrupted
            if not interrupted and delay == ra.POLL_INTERVAL and (self.cfg.checkout / "new.lean").exists():
                interrupted = True
                raise KeyboardInterrupt
            sleep(delay)

        with (
            patch.object(ra.Config, "resolve", return_value=self.cfg),
            patch.object(rounds, "self_argv", return_value=self.repair_argv()),
            patch.object(ra, "POLL_INTERVAL", 0.03),
            patch.object(ra.time, "sleep", side_effect=interrupt_once),
        ):
            with self.assertRaises(KeyboardInterrupt):
                ra.supervise([], 30)
        self.assertEqual(Counters(self.cfg).read(f"fix-77-{self.head[:12]}"), 2)
        self.assert_saved()

    def test_unbound_dirty_checkout_gets_durable_snapshot(self):
        self.dirty()
        cr.preserve_before_checkout(self.cfg)
        meta = json.loads(next((self.cfg.state / "checkout-recovery").glob("*.json")).read_text())
        self.assertEqual(self.git("rev-parse", meta["commit_ref"]), self.head)
        self.git("stash", "apply", "--index", meta["stash_ref"])
        self.assertTrue((self.cfg.checkout / "new.lean").exists())

    def test_failed_snapshot_refuses_destructive_checkout(self):
        self.dirty()
        with (
            patch.object(agents, "sync_mathlib_pool"),
            patch.object(cr, "_save", side_effect=ValueError("disk unavailable")),
        ):
            self.assertFalse(agents.prepare_checkout(self.cfg))
        self.assertEqual(self.git("rev-parse", "HEAD"), self.head)
        self.assertTrue((self.cfg.checkout / "new.lean").exists())
        self.assertIn("unstaged", (self.cfg.checkout / "tracked").read_text())

    def test_renewal_retries_only_fresh_verified_owner(self):
        for outcomes, expected, calls in [
            ([2, 0, 0], 0, ["renew", "holds", "renew"]),
            ([2, 2], 2, ["renew", "holds"]),
            ([2, 1], 1, ["renew", "holds"]),
            ([1], 1, ["renew"]),
            ([2, 0, 2], 2, ["renew", "holds", "renew"]),
        ]:
            with patch.object(
                rounds.subprocess, "run", side_effect=[SimpleNamespace(returncode=x) for x in outcomes]
            ) as run:
                self.assertEqual(rounds.renew_heartbeat("branch/77"), expected)
                self.assertEqual([c.args[0][1] for c in run.call_args_list], calls)


if __name__ == "__main__":
    unittest.main()
