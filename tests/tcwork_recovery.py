#!/usr/bin/env python3
"""Synthetic Git/process faults for maintenance preservation and admission; no network/provider."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tauceti_worker import agents as a
from tauceti_worker import work_units as w
from tauceti_worker.config import NoProgress
from tauceti_worker.survey import Candidate, Counters

RUN = subprocess.run


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="tcwork-recovery-test-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.co = root / "checkout"
        self.co.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Synthetic")
        self.git("config", "user.email", "synthetic@invalid")
        (self.co / "source").write_bytes(b"base\n")
        self.git("add", "source")
        self.git("commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/remotes/origin/main", self.base)
        self.c = Candidate(999, self.base, "synthetic")
        self.cfg = NS(checkout=self.co, state=root / "state", logdir=root / "logs")
        self.worker = NS(
            cfg=self.cfg,
            counters=Counters(self.cfg),
            rc=NS(),
            claims=NS(begin_branch_work=lambda *args: True),
            rs=NS(bust=lambda n: None),
            gh=NS(pr_progress_state=lambda n: {"head": self.base, "ncomments": 0}),
        )
        self.pr = NS(number=999, head_owner="synthetic", head_repo="repo", head_ref="topic")
        self.sv = NS(open_prs=[self.pr])
        self.opts = NS(agent_name="synthetic")

    def git(self, *args):
        return RUN(["git", "-C", str(self.co), *args], check=True, capture_output=True, text=True).stdout.strip()

    def candidate(self):
        (self.co / "source").write_bytes(b"candidate\n")
        self.git("add", "source")
        self.git("commit", "-qm", "candidate")
        return self.git("rev-parse", "HEAD")

    def run_fix(self, agent):
        with (
            patch.object(w, "prepare_checkout", return_value=True),
            patch.object(w, "_effective_authoring_profile", return_value=None),
            patch.object(w, "run_agent_host", side_effect=agent),
        ):
            # Existing checkpoint avoids gh checkout; all agent behavior is synthetic.
            return w.do_fix(self.worker, self.sv, self.c, self.opts, False)

    def test_failed_stash_blocks_prepare_without_success_metadata(self):
        (self.co / "source").write_bytes(b"UNSAVED\n")

        def fail(args, **kw):
            if "stash" in args and "push" in args:
                return subprocess.CompletedProcess(args, 1, "", "injected failure")
            return RUN(args, **kw)

        with patch.object(w.subprocess, "run", side_effect=fail):
            with self.assertRaises(NoProgress):
                w._checkpoint_resume(self.worker, self.c, "fix")
        self.assertFalse(w._resume_paths(self.worker, self.c)[0].exists())
        with patch.object(a, "sync_mathlib_pool") as sync:
            self.assertFalse(a.prepare_checkout(self.cfg))
            sync.assert_not_called()
        self.assertEqual((self.co / "source").read_bytes(), b"UNSAVED\n")

    def test_failed_status_blocks_checkpoint(self):
        self.candidate()

        def fail(args, **kw):
            if "status" in args:
                return subprocess.CompletedProcess(args, 1, "", "injected failure")
            return RUN(args, **kw)

        with patch.object(w.subprocess, "run", side_effect=fail):
            with self.assertRaises(NoProgress):
                w._checkpoint_resume(self.worker, self.c, "fix")
        self.assertFalse(w._resume_paths(self.worker, self.c)[0].exists())

    def test_interrupted_index_worktree_untracked_restore_exactly(self):
        candidate = self.candidate()
        (self.co / "source").write_bytes(b"staged\x00bytes\n")
        self.git("add", "source")
        (self.co / "source").write_bytes(b"unstaged\x00bytes\n")
        (self.co / "new-source").write_bytes(b"untracked\x00bytes\n")
        w._write_resume_meta(w._active_resume_path(self.worker), {"pr": 999, "public_head": self.base, "stage": "fix"})
        w._recover_active_checkout(self.worker)
        self.assertTrue(a._checkout_preserved(self.cfg))
        self.git("checkout", "-q", "-f", "-B", "main", self.base)
        self.assertTrue(w._restore_resume(self.worker, self.c, self.pr))
        self.assertEqual(self.git("rev-parse", "HEAD"), candidate)
        self.assertEqual(
            RUN(["git", "-C", str(self.co), "show", ":source"], capture_output=True).stdout, b"staged\x00bytes\n"
        )
        self.assertEqual((self.co / "source").read_bytes(), b"unstaged\x00bytes\n")
        self.assertEqual((self.co / "new-source").read_bytes(), b"untracked\x00bytes\n")

    def test_unpublished_normal_exit_retains_candidate(self):
        candidate = self.candidate()
        w._checkpoint_resume(self.worker, self.c, "fix")
        self.assertEqual(self.run_fix(lambda *args: 0), 0)
        self.assertTrue(w._resume_paths(self.worker, self.c)[0].exists())
        self.assertFalse(w._progressed(self.worker, self.c, {"head": self.base, "ncomments": 0}))
        self.assertEqual(self.git("rev-parse", "HEAD"), candidate)

    def test_worked_capacity_preserves_without_refund(self):
        self.candidate()
        w._checkpoint_resume(self.worker, self.c, "fix")

        def agent(*args):
            (self.co / "source").write_text("work before capacity\n")
            a._LAST_AGENT_FAILURE = None  # structured work means no infrastructure refund
            return 1

        self.assertEqual(self.run_fix(agent), 1)
        self.assertEqual(self.worker.counters.read(f"fix-999-{self.base[:12]}"), 1)
        meta = json.loads(w._resume_paths(self.worker, self.c)[0].read_text())
        self.assertTrue(meta["stash_ref"])
        w._restore_resume(self.worker, self.c, self.pr)
        self.assertEqual((self.co / "source").read_text(), "work before capacity\n")

    def test_capacity_classification_remains_narrow(self):
        terminal = {"type": "turn.failed", "error": {"message": "Selected model is at capacity."}}
        work = {
            "type": "item.completed",
            "item": {
                "id": "x",
                "type": "command_execution",
                "command": "synthetic edit",
                "aggregated_output": "done",
                "exit_code": 0,
                "status": "completed",
            },
        }
        for worked in (False, True):
            payload = [work, terminal] if worked else [terminal]
            code = "import json,sys\nfor x in " + repr(payload) + ": print(json.dumps(x))\nsys.exit(1)"
            with patch.object(a, "report_failure"):
                self.assertEqual(
                    a.run_agent_proc(
                        [sys.executable, "-c", code],
                        env=dict(os.environ),
                        logdir=self.cfg.logdir,
                        label="synthetic",
                        provider="codex",
                    ),
                    1,
                )
            reason = a.take_last_agent_infra_failure()
            self.assertEqual(bool(reason), not worked)

    def test_comment_and_unknown_do_not_certify_progress_or_clear(self):
        self.candidate()
        w._checkpoint_resume(self.worker, self.c, "fix")
        self.worker.gh.pr_progress_state = lambda n: {"head": self.base, "ncomments": 1}
        self.assertFalse(w._progressed(self.worker, self.c, {"head": self.base, "ncomments": 0}))
        w._clear_resume(self.worker, self.c)
        self.assertTrue(w._resume_paths(self.worker, self.c)[0].exists())
        self.worker.gh.pr_progress_state = lambda n: None
        self.assertIsNone(w._progressed(self.worker, self.c, {"head": self.base, "ncomments": 0}))
        w._clear_resume(self.worker, self.c)
        self.assertTrue(w._resume_paths(self.worker, self.c)[0].exists())

    def test_foreign_public_head_preserves_and_defers(self):
        self.candidate()
        w._checkpoint_resume(self.worker, self.c, "fix")
        self.worker.gh.pr_progress_state = lambda n: {"head": "f" * 40, "ncomments": 0}
        w._clear_resume(self.worker, self.c)
        self.assertTrue(w._resume_paths(self.worker, self.c)[0].exists())
        with self.assertRaises(NoProgress):
            w._restore_resume(self.worker, Candidate(999, "f" * 40, "moved"), self.pr)

    def test_only_exact_own_clean_publication_clears(self):
        candidate = self.candidate()
        w._checkpoint_resume(self.worker, self.c, "fix")
        self.worker.gh.pr_progress_state = lambda n: {"head": candidate, "ncomments": 0}
        (self.co / "source").write_text("later edit\n")
        w._clear_resume(self.worker, self.c)
        self.assertTrue(w._resume_paths(self.worker, self.c)[0].exists())
        self.git("checkout", "--", "source")
        w._clear_resume(self.worker, self.c)
        self.assertFalse(w._resume_paths(self.worker, self.c)[0].exists())
        self.assertTrue(a._checkout_preserved(self.cfg))

    def test_stashed_later_work_survives_published_commit(self):
        candidate = self.candidate()
        (self.co / "source").write_text("unpublished after push\n")
        w._checkpoint_resume(self.worker, self.c, "fix")
        self.assertFalse(self.git("status", "--porcelain"))
        self.worker.gh.pr_progress_state = lambda n: {"head": candidate, "ncomments": 0}
        w._clear_resume(self.worker, self.c)
        self.assertTrue(w._resume_paths(self.worker, self.c)[0].exists())
        w._restore_resume(self.worker, self.c, self.pr)
        self.assertEqual((self.co / "source").read_text(), "unpublished after push\n")

    def test_foreign_head_does_not_certify_this_author_progress(self):
        self.worker.rc.author_pr = self.c.pr
        self.worker.rc.author_head = self.candidate()
        self.worker.gh.pr_progress_state = lambda n: {"head": "f" * 40, "ncomments": 2}
        self.assertFalse(w._progressed(self.worker, self.c, {"head": self.base, "ncomments": 0}))

    def test_refused_claim_and_deleted_head_use_no_attempt(self):
        self.worker.claims.begin_branch_work = lambda *args: False
        self.assertIsNone(w.do_fix(self.worker, self.sv, self.c, self.opts, False))
        self.assertEqual(self.worker.counters.read(f"fix-999-{self.base[:12]}"), 0)
        self.pr.head_owner = ""
        self.assertIsNone(w.do_fix_ci(self.worker, self.sv, self.c, self.opts, False))
        self.assertEqual(self.worker.counters.read("ci-pr-999"), 0)

    def test_unattributed_unpublished_commit_blocks_prepare(self):
        self.candidate()
        with patch.object(a, "sync_mathlib_pool") as sync:
            self.assertFalse(a.prepare_checkout(self.cfg))
            sync.assert_not_called()

    def test_agent_leader_exit_reaps_pipe_holding_descendant(self):
        pidfile = self.co / "child.pid"
        script = (
            "import subprocess,sys\n"
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
            f"open({str(pidfile)!r},'w').write(str(p.pid))\n"
        )
        started = time.monotonic()
        with patch.object(a, "report_failure"):
            self.assertEqual(
                a.run_agent_proc(
                    [sys.executable, "-c", script],
                    env=dict(os.environ),
                    logdir=self.cfg.logdir,
                    label="synthetic",
                    provider="codex",
                ),
                0,
            )
        self.assertLess(time.monotonic() - started, 10)
        child = int(pidfile.read_text())
        status = RUN(["ps", "-p", str(child), "-o", "stat="], capture_output=True, text=True)
        self.assertTrue(status.returncode or status.stdout.strip().startswith("Z"))
        self.assertTrue(a.agent_quiescent())

    def test_silent_claim_failure_stops_author_and_preserves_edits(self):
        from tauceti_worker import round as rnd

        self.candidate()
        w._checkpoint_resume(self.worker, self.c, "fix")

        def healthy():
            return not (self.co / "ready").exists()

        code = (
            "import pathlib,time\n"
            f"p=pathlib.Path({str(self.co)!r})\n"
            "(p/'source').write_text('claim-interrupted edit\\n')\n"
            "(p/'ready').write_text('ready')\n"
            "time.sleep(60)\n"
        )

        def agent(*args):
            return a.run_agent_proc(
                [sys.executable, "-c", code],
                env=dict(os.environ),
                logdir=self.cfg.logdir,
                label="synthetic",
                provider="codex",
            )

        with patch.object(rnd, "check_claim_health", side_effect=healthy), patch.object(a, "report_failure"):
            self.assertEqual(self.run_fix(agent), 75)
        self.assertTrue(a.agent_quiescent())
        w._restore_resume(self.worker, self.c, self.pr)
        self.assertEqual((self.co / "source").read_text(), "claim-interrupted edit\n")

    def test_live_or_reused_group_id_blocks_capture_without_signalling(self):
        (self.co / "source").write_text("still owned\n")
        w._write_resume_meta(
            w._active_resume_path(self.worker),
            {
                "pr": 999,
                "public_head": self.base,
                "stage": "fix",
                "author_pgid": os.getpgrp(),
            },
        )
        with patch.object(w.os, "killpg", wraps=os.killpg) as probe:
            with self.assertRaises(NoProgress):
                w._recover_active_checkout(self.worker)
        probe.assert_called_once_with(os.getpgrp(), 0)
        self.assertEqual((self.co / "source").read_text(), "still owned\n")
        self.assertTrue(w._active_resume_path(self.worker).exists())

    def test_native_timeout_reaps_writer_then_recovers_checkpoint(self):
        from tauceti_worker import round as rnd

        active = w._active_resume_path(self.worker)
        w._write_resume_meta(active, {"pr": 999, "public_head": self.base, "stage": "fix"})
        # Real author edits, then sleeps. Kill its worker through the native bounded supervisor;
        # recovery must run only after that supervisor has stopped the separate author group.
        author = (
            "from pathlib import Path; import time; "
            f"Path({str(self.co / 'source')!r}).write_text('timed-out edit\\n'); time.sleep(60)"
        )
        child = (
            "import os,sys; from pathlib import Path; from tauceti_worker.agents import run_agent_proc; "
            f"os.environ['TAUCETI_ACTIVE_CHECKOUT']={str(active)!r}; "
            f"run_agent_proc([sys.executable,'-c',{author!r}],env=dict(os.environ),"
            f"logdir=Path({str(self.cfg.logdir)!r}),label='synthetic',provider='codex')"
        )

        def spawn(_):
            return subprocess.Popen(
                [sys.executable, "-c", child],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        with patch.object(rnd, "spawn_round", side_effect=spawn):
            self.assertEqual(rnd.run_round_subprocess([], timeout=1), 124)
        self.assertEqual((self.co / "source").read_text(), "timed-out edit\n")
        w._recover_active_checkout(self.worker)
        self.assertFalse(active.exists())
        self.assertTrue(w._resume_paths(self.worker, self.c)[0].exists())
        w._restore_resume(self.worker, self.c, self.pr)
        self.assertEqual((self.co / "source").read_text(), "timed-out edit\n")

    def test_failed_restore_never_resets_or_falls_back(self):
        self.candidate()
        (self.co / "source").write_text("preserve this\n")
        w._checkpoint_resume(self.worker, self.c, "fix")

        def fail(args, **kw):
            if "stash" in args and "apply" in args:
                return subprocess.CompletedProcess(args, 1, "", "injected apply failure")
            self.assertNotIn("reset", args)
            self.assertNotEqual(args[:3], ["gh", "pr", "checkout"])
            return RUN(args, **kw)

        with patch.object(w.subprocess, "run", side_effect=fail):
            with self.assertRaises(NoProgress):
                w._restore_resume(self.worker, self.c, self.pr)
        self.assertTrue(w._resume_paths(self.worker, self.c)[0].exists())

    def test_prior_pr_checkpoint_not_overwritten_on_switch(self):
        candidate = self.candidate()
        w._write_resume_meta(w._active_resume_path(self.worker), {"pr": 999, "public_head": self.base, "stage": "fix"})
        w._recover_active_checkout(self.worker)
        self.git("checkout", "-q", "-f", "-B", "main", self.base)
        (self.co / "source").write_text("other PR\n")
        other = Candidate(1000, self.base, "other")
        w._checkpoint_resume(self.worker, other, "fix")
        self.assertEqual(self.git("rev-parse", w._resume_paths(self.worker, self.c)[1]), candidate)
        self.assertTrue(w._resume_paths(self.worker, other)[0].exists())


if __name__ == "__main__":
    unittest.main()
