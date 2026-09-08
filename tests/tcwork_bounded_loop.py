#!/usr/bin/env python3
"""Bounded recovery uses the native paced loop and stops before another dispatch.

All provider/GitHub calls are stubbed. The SIGTERM case uses a real process group and
native round teardown; it does not launch an author, contact GitHub, or modify a PR.
"""

import contextlib
import io
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
import tauceti_worker.cli as cli
import tauceti_worker.loop as loop
import tauceti_worker.round as lifecycle
from tauceti_worker.config import Die
from tauceti_worker.constants import EX_NOPROGRESS
from tauceti_worker.quota import Provider


def options(**changes):
    return SimpleNamespace(**dict(dict(max_rounds=1, ignore_quota=False, quota_cmd=None, loop=True), **changes))


class BoundedLoopTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.choose = self.stack.enter_context(
            patch.object(loop, "choose_model", return_value=("codex", {"codex": Provider("codex", True, "codex")}))
        )
        self.budget = self.stack.enter_context(patch.object(loop, "github_budget", return_value={}))
        self.child = self.stack.enter_context(patch.object(loop, "run_round_subprocess", return_value=0))
        self.sleep = self.stack.enter_context(patch.object(loop.time, "sleep"))
        self.stack.enter_context(patch.object(loop, "report_runtime"))
        self.stack.enter_context(patch.object(loop, "runtime_snapshot", return_value={}))
        self.stack.enter_context(patch.object(loop, "log"))

    def run_loop(self, **changes):
        return loop.cmd_loop(options(**changes), SimpleNamespace(wid="bounded-test"), only=["fix"], agent="codex")

    def test_cap_counts_dispatches_and_returns_last_result_before_sleep(self):
        for statuses in ([0], [EX_NOPROGRESS], [124], [137], [1], [0, EX_NOPROGRESS, 0]):
            with self.subTest(statuses=statuses):
                self.child.reset_mock()
                self.choose.reset_mock()
                self.sleep.reset_mock()
                self.child.side_effect = statuses
                self.assertEqual(self.run_loop(max_rounds=len(statuses)), statuses[-1])
                self.assertEqual(self.child.call_count, len(statuses))
                self.assertEqual(self.choose.call_count, len(statuses))
                self.assertEqual(self.sleep.call_count, len(statuses) - 1)
                for call in self.choose.call_args_list:
                    self.assertEqual(call.kwargs, {"refresh": True, "renew": True})
                for call in self.child.call_args_list:
                    tail = call.args[0]
                    self.assertIn("--ignore-quota", tail)
                    self.assertNotIn("--max-rounds", tail)

    def test_blocked_quota_exits_without_sleep_or_dispatch_even_ignore_quota(self):
        self.choose.return_value = None, {"codex": Provider("codex", False, None, error="unavailable")}
        for ignore in (False, True):
            self.assertEqual(self.run_loop(ignore_quota=ignore), EX_NOPROGRESS)
        self.child.assert_not_called()
        self.sleep.assert_not_called()
        self.assertTrue(all(call.kwargs["refresh"] for call in self.choose.call_args_list))

    def test_low_or_unknown_github_budget_exits_without_dispatch(self):
        for budget in ({"core": (0, int(time.time()) + 3600)}, None):
            self.budget.return_value = budget
            self.assertEqual(self.run_loop(), EX_NOPROGRESS)
        self.child.assert_not_called()
        self.sleep.assert_not_called()

    def test_bounded_external_quota_hook_has_timeout(self):
        self.choose.side_effect = subprocess.TimeoutExpired("quota-hook", 1)
        self.assertEqual(self.run_loop(quota_cmd="quota-hook"), EX_NOPROGRESS)
        self.assertEqual(self.choose.call_args.kwargs["command_timeout"], loop.ROUND_TIMEOUT)
        self.child.assert_not_called()
        self.sleep.assert_not_called()

    def test_unbounded_loop_still_waits_and_runs_multiple_rounds(self):
        self.child.side_effect = [0, EX_NOPROGRESS, KeyboardInterrupt]
        self.assertEqual(self.run_loop(max_rounds=None), 130)
        self.assertEqual(self.child.call_count, 3)
        self.assertEqual(self.sleep.call_count, 2)

    def test_unbounded_blocked_quota_still_waits(self):
        self.choose.return_value = None, {"codex": Provider("codex", False, None, error="unavailable")}
        self.sleep.side_effect = KeyboardInterrupt
        self.assertEqual(self.run_loop(max_rounds=None), 130)
        self.sleep.assert_called_once()
        self.child.assert_not_called()

    def test_interrupt_restores_signal_handler(self):
        before = signal.getsignal(signal.SIGTERM)
        self.child.side_effect = loop._LoopTerminated
        self.assertEqual(self.run_loop(), 143)
        self.assertIs(signal.getsignal(signal.SIGTERM), before)

    def test_cli_rejects_invalid_cap_or_non_loop_before_side_effects(self):
        for value in (0, -1):
            with self.assertRaisesRegex(Die, "positive integer"):
                cli.cmd_work(options(max_rounds=value), only=[], agent="codex", one_round=False)
        for is_loop, one_round in ((False, False), (True, True)):
            with self.assertRaisesRegex(Die, "requires work --loop"):
                cli.cmd_work(options(loop=is_loop), only=[], agent="codex", one_round=one_round)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["work", "--loop", "--max-rounds", "1.5"])
        args = cli.build_parser().parse_args(["work", "--loop", "--max-rounds", "2"])
        self.assertEqual(args.max_rounds, 2)
        self.assertIsNone(cli.build_parser().parse_args(["work", "--loop"]).max_rounds)


class PromptTests(unittest.TestCase):
    def test_build_wait_and_preservation_contract(self):
        for name in ("fix", "fix-ci", "rebase", "bump", "roadmap"):
            prompt = (REPO / "prompts" / f"{name}.md").read_text()
            with self.subTest(prompt=name):
                self.assertIn("keep waiting on that same handle", prompt)
                self.assertIn("confirm it has exited before restarting", prompt)
                self.assertIn("Never launch a duplicate", prompt)
                self.assertIn("recovery checkpoint", prompt)
                self.assertNotIn("Pushing is the only thing", prompt)
                self.assertNotIn("rerun the command with a longer foreground timeout", prompt)

    def test_semantic_and_convergence_checks(self):
        fix = (REPO / "prompts" / "fix.md").read_text()
        roadmap = (REPO / "prompts" / "roadmap.md").read_text()
        for prompt in (fix, roadmap):
            self.assertIn("semantic witness", prompt)
            self.assertIn("correctness", prompt)
            self.assertIn("scope", prompt)
            self.assertIn("proof infrastructure", prompt)
        self.assertIn("unresolved findings from earlier", fix)
        self.assertIn("reviewer's disposition", fix)
        self.assertIn("identical rejected", fix)
        self.assertNotIn("~200–600 lines", roadmap)


def native_driver(started, cleaned):
    loop.choose_model = lambda *_a, **_k: ("codex", {})
    loop.github_budget = lambda: {}
    loop.report_runtime = lambda *_a, **_k: None
    lifecycle.spawn_round = lambda _tail: subprocess.Popen(
        [sys.executable, __file__, "--round-child", str(started), str(cleaned)], start_new_session=True
    )
    return loop.cmd_loop(options(), SimpleNamespace(wid="bounded-test"), only=["fix"], agent="codex")


def native_child(started, cleaned):
    def stop(_signum, _frame):
        cleaned.touch()
        raise SystemExit(143)

    signal.signal(signal.SIGTERM, stop)
    started.touch()
    while True:
        time.sleep(60)


class NativeSignalTests(unittest.TestCase):
    def test_bounded_driver_terminates_and_reaps_active_round(self):
        with tempfile.TemporaryDirectory(prefix="tcwork-bounded-") as tmp:
            started, cleaned = Path(tmp) / "started", Path(tmp) / "cleaned"
            process = subprocess.Popen([sys.executable, __file__, "--driver", str(started), str(cleaned)])
            try:
                deadline = time.monotonic() + 10
                while not started.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(started.exists(), "native child failed to start")
                process.terminate()
                self.assertEqual(process.wait(15), 143)
                self.assertTrue(cleaned.exists(), "native child did not receive teardown")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--round-child":
        native_child(Path(sys.argv[2]), Path(sys.argv[3]))
    elif len(sys.argv) > 1 and sys.argv[1] == "--driver":
        raise SystemExit(native_driver(Path(sys.argv[2]), Path(sys.argv[3])))
    else:
        unittest.main()
