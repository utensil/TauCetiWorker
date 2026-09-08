#!/usr/bin/env python3
"""Synthetic Git/process faults for maintenance preservation and admission; no network/provider."""

import json
import os
import signal
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
        def launch(*args, on_launch):
            on_launch()
            return agent(*args)

        with (
            patch.object(w, "prepare_checkout", return_value=True),
            patch.object(w, "_effective_authoring_profile", return_value=None),
            patch.object(w, "run_agent_host", side_effect=launch),
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

    def maintenance_cases(self):
        return (
            (w.do_fix, (f"fix-999-{self.base[:12]}",)),
            (w.do_fix_ci, (f"ci-999-{self.base[:12]}", "ci-pr-999")),
        )

    def test_unresolved_prior_author_never_spends_selected_candidate(self):
        active = w._active_resume_path(self.worker)
        w._write_resume_meta(
            active,
            {"pr": 998, "public_head": "b" * 40, "stage": "fix", "author_scope_pending": True},
        )
        for run, keys in self.maintenance_cases():
            with self.subTest(kind=run.__name__), patch.object(w, "run_agent_host") as author:
                for _ in range(3):
                    with self.assertRaises(NoProgress):
                        run(self.worker, self.sv, self.c, self.opts, False)
                author.assert_not_called()
                self.assertTrue(active.exists())
                self.assertEqual([self.worker.counters.read(key) for key in keys], [0] * len(keys))

    def test_preparation_and_restore_failures_never_spend_attempts(self):
        for run, keys in self.maintenance_cases():
            for fault in ("prepare", "restore", "checkout", "prompt"):
                with (
                    self.subTest(kind=run.__name__, fault=fault),
                    patch.object(w, "run_agent_host") as author,
                    patch.object(w, "report_failure"),
                    patch.object(w, "prepare_checkout", return_value=fault != "prepare"),
                    patch.object(
                        w,
                        "_restore_resume",
                        side_effect=NoProgress("restore failed") if fault == "restore" else None,
                        return_value=False,
                    ),
                    patch.object(w, "fill_prompt", side_effect=OSError("prompt failed") if fault == "prompt" else None),
                    patch.object(
                        w.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "checkout failed")
                    ),
                ):
                    if fault in ("restore", "prompt"):
                        with self.assertRaises((NoProgress, OSError)):
                            run(self.worker, self.sv, self.c, self.opts, False)
                    else:
                        self.assertEqual(run(self.worker, self.sv, self.c, self.opts, False), 1)
                    author.assert_not_called()
                    self.assertEqual([self.worker.counters.read(key) for key in keys], [0] * len(keys))

    def test_claim_refusal_never_spends_either_maintenance_budget(self):
        self.worker.claims.begin_branch_work = lambda *args: False
        for run, keys in self.maintenance_cases():
            with self.subTest(kind=run.__name__), patch.object(w, "run_agent_host") as author:
                self.assertIsNone(run(self.worker, self.sv, self.c, self.opts, False))
                author.assert_not_called()
                self.assertEqual([self.worker.counters.read(key) for key in keys], [0] * len(keys))

    def test_runner_setup_and_last_claim_refusal_do_not_debit_or_refund(self):
        from tauceti_worker import round as rnd

        for run, keys in self.maintenance_cases():
            for fault in ("argv", "log", "claim"):
                self.cfg.logdir = self.co.parent / f"logs-{run.__name__}-{fault}"
                for key in keys:
                    self.worker.counters.write(key, 2)  # a refusal must not refund an older attempt
                with (
                    self.subTest(kind=run.__name__, fault=fault),
                    patch.object(w, "prepare_checkout", return_value=True),
                    patch.object(w, "_restore_resume", return_value=True),
                    patch.object(w, "_effective_authoring_profile", return_value=NS(provider="codex")),
                    patch.object(a, "_authoring_profile", side_effect=lambda profile: profile),
                    patch.object(
                        a,
                        "host_agent_argv",
                        side_effect=OSError("argv failed")
                        if fault == "argv"
                        else lambda *_: ([sys.executable, "-c", "pass"], dict(os.environ)),
                    ),
                    patch.object(rnd, "check_claim_health", return_value=False),
                    patch.object(w, "_refund_infra_failure") as refund,
                ):
                    a._AGENT_QUIESCENT = True
                    if fault == "log":
                        self.cfg.logdir.parent.mkdir(parents=True, exist_ok=True)
                        self.cfg.logdir.write_text("not a directory")
                    try:
                        if fault in ("argv", "log"):
                            with self.assertRaises(OSError):
                                run(self.worker, self.sv, self.c, self.opts, False)
                        else:
                            self.assertEqual(run(self.worker, self.sv, self.c, self.opts, False), 75)
                        refund.assert_not_called()
                        self.assertEqual([self.worker.counters.read(key) for key in keys], [2] * len(keys))
                    finally:
                        if fault == "log":
                            self.cfg.logdir.unlink()

    def test_success_and_ambiguous_process_launch_spend_exactly_one(self):
        popen = subprocess.Popen

        def ambiguous(argv, **kwargs):
            if argv[:2] == [sys.executable, "-c"]:
                raise KeyboardInterrupt("process construction interrupted")
            return popen(argv, **kwargs)

        for run, keys in self.maintenance_cases():
            for interrupted in (False, True):
                for key in keys:
                    self.worker.counters.write(key, 0)
                with (
                    self.subTest(kind=run.__name__, interrupted=interrupted),
                    patch.object(w, "prepare_checkout", return_value=True),
                    patch.object(w, "_restore_resume", return_value=True),
                    patch.object(w, "_effective_authoring_profile", return_value=NS(provider="codex")),
                    patch.object(a, "_authoring_profile", side_effect=lambda profile: profile),
                    patch.object(
                        a, "host_agent_argv", side_effect=lambda *_: ([sys.executable, "-c", "pass"], dict(os.environ))
                    ),
                    patch.object(a.subprocess, "Popen", side_effect=ambiguous if interrupted else popen),
                ):
                    a._AGENT_QUIESCENT = True
                    try:
                        if interrupted:
                            with self.assertRaises(NoProgress):
                                run(self.worker, self.sv, self.c, self.opts, False)
                            self.assertTrue(w._active_resume_path(self.worker).exists())
                        else:
                            self.assertEqual(run(self.worker, self.sv, self.c, self.opts, False), 0)
                        self.assertEqual([self.worker.counters.read(key) for key in keys], [1] * len(keys))
                    finally:
                        # The injected constructor above never spawns; retire only this fixture gate.
                        a._AGENT_QUIESCENT = True
                        w._active_resume_path(self.worker).unlink(missing_ok=True)

    def test_charge_failure_does_not_enter_process_construction(self):
        def unavailable_counter():
            raise OSError("counter storage unavailable")

        a._AGENT_QUIESCENT = True
        with patch.object(a.subprocess, "Popen") as spawn, self.assertRaises(OSError):
            a.run_agent_proc(
                [sys.executable, "-c", "pass"],
                env=dict(os.environ),
                logdir=self.cfg.logdir,
                label="synthetic",
                provider="codex",
                on_launch=unavailable_counter,
            )
        spawn.assert_not_called()
        self.assertTrue(a.agent_quiescent())

    def test_capacity_refund_requires_debit_and_no_work(self):
        for run, keys in self.maintenance_cases():
            for worked in (False, True):
                for key in keys:
                    self.worker.counters.write(key, 0)

                def author(*args, on_launch, worked=worked):
                    on_launch()
                    if worked:
                        (self.co / "source").write_text("work before failure\n")
                    a._LAST_AGENT_FAILURE = None if worked else "Selected model is at capacity."
                    return 1

                with (
                    self.subTest(kind=run.__name__, worked=worked),
                    patch.object(w, "prepare_checkout", return_value=True),
                    patch.object(w, "_restore_resume", return_value=True),
                    patch.object(w, "_effective_authoring_profile", return_value=None),
                    patch.object(w, "run_agent_host", side_effect=author),
                ):
                    if worked:
                        self.assertEqual(run(self.worker, self.sv, self.c, self.opts, False), 1)
                    else:
                        with self.assertRaises(NoProgress):
                            run(self.worker, self.sv, self.c, self.opts, False)
                    self.assertEqual([self.worker.counters.read(key) for key in keys], [int(worked)] * len(keys))

    def run_counter_ci(self, bubble=False):
        with (
            patch.object(w, "prepare_checkout", return_value=True),
            patch.object(w, "_restore_resume", return_value=True),
            patch.object(w, "_effective_authoring_profile", return_value=NS(provider="codex")),
            patch.object(a, "_authoring_profile", side_effect=lambda profile: profile),
            patch.object(
                a, "host_agent_argv", side_effect=lambda *_: ([sys.executable, "-c", "pass"], dict(os.environ))
            ),
        ):
            a._AGENT_QUIESCENT = True
            return w.do_fix_ci(self.worker, self.sv, self.c, self.opts, bubble)

    def test_second_counter_directory_never_partially_charges(self):
        head_key, pr_key = self.maintenance_cases()[1][1]
        (self.cfg.state / pr_key).mkdir(parents=True)
        with patch.object(a.subprocess, "Popen", wraps=subprocess.Popen) as spawn:
            for _ in range(3):
                with self.assertRaises(OSError):
                    self.run_counter_ci()
        self.assertFalse(any(call.args[0][:2] == [sys.executable, "-c"] for call in spawn.call_args_list))
        self.assertFalse((self.cfg.state / head_key).exists())
        self.assertTrue((self.cfg.state / pr_key).is_dir())
        self.assertFalse(w._counter_debit_path(self.worker).exists())

    def test_second_counter_write_failure_restores_exact_values_or_absence(self):
        keys = self.maintenance_cases()[1][1]
        write = self.worker.counters.write
        for previous in (None, b" 002\n"):
            for wrote_second in (False, True):
                for key in keys:
                    path = self.cfg.state / key
                    path.parent.mkdir(parents=True, exist_ok=True)
                    if previous is None:
                        path.unlink(missing_ok=True)
                    else:
                        path.write_bytes(previous)

                def fail_second(name, value, wrote_second=wrote_second):
                    if name == keys[1]:
                        if wrote_second:
                            write(name, value)
                        raise OSError("second counter write failed")
                    write(name, value)

                with (
                    self.subTest(previous=previous, wrote_second=wrote_second),
                    patch.object(self.worker.counters, "write", side_effect=fail_second),
                    patch.object(a.subprocess, "Popen", wraps=subprocess.Popen) as spawn,
                ):
                    for _ in range(3):
                        with self.assertRaises(OSError):
                            self.run_counter_ci()
                        self.assertEqual([w._counter_bytes(self.cfg.state / key) for key in keys], [previous] * 2)
                        self.assertFalse(w._counter_debit_path(self.worker).exists())
                    self.assertFalse(any(call.args[0][:2] == [sys.executable, "-c"] for call in spawn.call_args_list))

    def test_unverified_counter_rollback_preserves_gates_and_blocks_retry(self):
        keys = self.maintenance_cases()[1][1]
        for key in keys:
            self.worker.counters.write(key, 2)
        write, write_bytes = self.worker.counters.write, Path.write_bytes

        def fail_second(name, value):
            if name == keys[1]:
                raise OSError("second counter write failed")
            write(name, value)

        def fail_rollback(path, data):
            if path == self.cfg.state / keys[0]:
                raise OSError("rollback storage failure")
            return write_bytes(path, data)

        with (
            patch.object(self.worker.counters, "write", side_effect=fail_second),
            patch.object(Path, "write_bytes", fail_rollback),
            self.assertRaises(NoProgress),
        ):
            self.run_counter_ci()
        self.assertEqual([self.worker.counters.read(key) for key in keys], [3, 2])
        self.assertTrue(w._active_resume_path(self.worker).exists())
        intent = w._counter_debit_path(self.worker)
        prior = intent.read_bytes()
        self.assertEqual(json.loads(prior)["previous"], {key: "2" for key in keys})
        for bubble in (False, True):
            with (
                self.subTest(bubble=bubble),
                patch.object(self.worker.claims, "begin_branch_work") as claim,
                patch.object(w, "prepare_checkout") as prepare,
                patch.object(w, "run_in_bubble") as author,
                self.assertRaises(NoProgress),
            ):
                w.do_fix_ci(self.worker, self.sv, self.c, self.opts, bubble)
            claim.assert_not_called()
            prepare.assert_not_called()
            author.assert_not_called()
        with self.assertRaises(NoProgress):
            w._recover_active_checkout(self.worker)
        with self.assertRaises(NoProgress):
            w._restore_resume(self.worker, self.c, self.pr)
        with patch.object(a, "sync_mathlib_pool") as sync:
            self.assertFalse(a.prepare_checkout(self.cfg))
            sync.assert_not_called()
        self.assertEqual(intent.read_bytes(), prior)

    def test_counter_rollback_never_overwrites_an_unexpected_value(self):
        keys = self.maintenance_cases()[1][1]
        for key in keys:
            self.worker.counters.write(key, 2)
        write = self.worker.counters.write

        def changed_counter(name, value):
            if name == keys[1]:
                (self.cfg.state / keys[0]).write_bytes(b"99")
                raise OSError("second write failed after an unexpected change")
            write(name, value)

        with patch.object(self.worker.counters, "write", side_effect=changed_counter), self.assertRaises(NoProgress):
            self.run_counter_ci()
        self.assertEqual((self.cfg.state / keys[0]).read_bytes(), b"99")
        self.assertTrue(w._counter_debit_path(self.worker).exists())
        self.assertTrue(w._active_resume_path(self.worker).exists())

    def test_invalid_counter_values_are_rejected_before_either_write(self):
        keys = self.maintenance_cases()[1][1]
        for invalid in (b"", b"-1", b"not a counter", b"1" * 129):
            path = self.cfg.state / keys[1]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(invalid)
            with self.subTest(invalid=invalid), patch.object(self.worker.counters, "write") as write:
                with self.assertRaises(OSError):
                    self.run_counter_ci()
                write.assert_not_called()
                self.assertFalse((self.cfg.state / keys[0]).exists())
                self.assertEqual(path.read_bytes(), invalid)

    def test_malformed_counter_intent_blocks_host_and_bubble_admission(self):
        intent = w._counter_debit_path(self.worker)
        intent.parent.mkdir(parents=True)
        for kind in ("malformed", "directory", "symlink"):
            if kind == "malformed":
                intent.write_bytes(b"not json")
            elif kind == "directory":
                intent.mkdir()
            else:
                intent.symlink_to(intent.parent / "missing")
            for bubble in (False, True):
                with (
                    self.subTest(kind=kind, bubble=bubble),
                    patch.object(self.worker.claims, "begin_branch_work") as claim,
                    self.assertRaises(NoProgress),
                ):
                    w.do_fix_ci(self.worker, self.sv, self.c, self.opts, bubble)
                claim.assert_not_called()
                self.assertFalse(a._checkout_preserved(self.cfg))
            if kind == "directory":
                intent.rmdir()
            else:
                intent.unlink()

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
                env={**os.environ, "TAUCETI_NATIVE_ROUND_PARENT": str(os.getpid())},
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

    def test_capture_interruptions_restore_bytes_and_keep_older_generations(self):
        prior_refs = {}
        for boundary in ("stash", "stash_ref", "metadata", "alias"):
            with self.subTest(boundary=boundary):
                staged = f"staged {boundary}\0\n".encode()
                worktree = f"unstaged {boundary}\0\n".encode()
                untracked = f"new {boundary}\0\n".encode()
                (self.co / "source").write_bytes(staged)
                self.git("add", "source")
                (self.co / "source").write_bytes(worktree)
                (self.co / "new-source").write_bytes(untracked)
                active = w._active_resume_path(self.worker)
                w._write_resume_meta(active, {"pr": 999, "public_head": self.base, "stage": "fix"})
                original_git, original_meta = w._resume_git, w._write_resume_meta

                def interrupt_git(worker, *args, original_git=original_git, boundary=boundary, **kwargs):
                    result = original_git(worker, *args, **kwargs)
                    if (
                        (boundary == "stash" and args[:2] == ("stash", "push"))
                        or (boundary == "stash_ref" and args[0] == "update-ref" and args[1].endswith("/stash_ref"))
                        or (boundary == "alias" and args[:2] == ("update-ref", w._resume_paths(self.worker, self.c)[1]))
                    ):
                        raise KeyboardInterrupt("injected crash after durable Git mutation")
                    return result

                def interrupt_meta(path, data, original_meta=original_meta, boundary=boundary):
                    original_meta(path, data)
                    if boundary == "metadata" and path == w._resume_paths(self.worker, self.c)[0]:
                        raise KeyboardInterrupt("injected crash after metadata replacement")

                with (
                    patch.object(w, "_resume_git", side_effect=interrupt_git),
                    patch.object(w, "_write_resume_meta", side_effect=interrupt_meta),
                ):
                    with self.assertRaises(KeyboardInterrupt):
                        w._checkpoint_resume(self.worker, self.c, "fix")
                self.assertTrue(w._capture_intent_path(self.worker).exists())
                self.assertTrue(active.exists())
                self.assertFalse(a._checkout_preserved(self.cfg))
                w._recover_active_checkout(self.worker)
                self.assertFalse(active.exists())
                self.assertFalse(w._capture_intent_path(self.worker).exists())
                self.assertTrue(w._restore_resume(self.worker, self.c, self.pr))
                self.assertEqual(
                    RUN(["git", "-C", str(self.co), "show", ":source"], capture_output=True).stdout, staged
                )
                self.assertEqual((self.co / "source").read_bytes(), worktree)
                self.assertEqual((self.co / "new-source").read_bytes(), untracked)
                for ref, oid in prior_refs.items():
                    self.assertEqual(self.git("rev-parse", ref), oid)
                meta = json.loads(w._resume_paths(self.worker, self.c)[0].read_text())
                prior_refs.update(
                    {meta[field]: self.git("rev-parse", meta[field]) for field in ("commit_ref", "stash_ref")}
                )

    def test_interrupted_capture_never_adopts_unrelated_stash(self):
        (self.co / "source").write_text("unassociated work\n")
        active = w._active_resume_path(self.worker)
        w._write_resume_meta(active, {"pr": 999, "public_head": self.base, "stage": "fix"})
        original = w._resume_git

        def before_stash(worker, *args, **kwargs):
            if args[:2] == ("stash", "push"):
                raise KeyboardInterrupt("interrupted before stash")
            return original(worker, *args, **kwargs)

        with patch.object(w, "_resume_git", side_effect=before_stash), self.assertRaises(KeyboardInterrupt):
            w._checkpoint_resume(self.worker, self.c, "fix")
        self.git("stash", "push", "-qm", "unrelated stash")
        with self.assertRaises(NoProgress):
            w._recover_active_checkout(self.worker)
        self.assertTrue(active.exists())
        self.assertTrue(w._capture_intent_path(self.worker).exists())
        with self.assertRaises(NoProgress):
            w._restore_resume(self.worker, self.c, self.pr)
        self.assertFalse(a._checkout_preserved(self.cfg))

    def test_interrupted_popen_never_captures_while_os_child_is_alive(self):
        ready = self.co / "spawn-ready"
        code = (
            "# tcwork-spawn-boundary\nfrom pathlib import Path; import time; "
            f"Path({str(self.co / 'source')!r}).write_text('edit before spawn returns\\n'); "
            f"Path({str(ready)!r}).touch(); time.sleep(60)"
        )
        popen = subprocess.Popen
        processes = []

        def interrupted_spawn(argv, *args, **kwargs):
            proc = popen(argv, *args, **kwargs)
            if any("tcwork-spawn-boundary" in str(arg) for arg in argv):
                processes.append(proc)
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists())
                raise KeyboardInterrupt("interrupted after OS launch, before Popen returns")
            return proc

        def author(*args):
            return a.run_agent_proc(
                [sys.executable, "-c", code],
                env=dict(os.environ),
                logdir=self.cfg.logdir,
                label="synthetic",
                provider="codex",
            )

        a._AGENT_QUIESCENT = True
        try:
            with (
                patch.object(w, "_restore_resume", return_value=True),
                patch.object(a.subprocess, "Popen", side_effect=interrupted_spawn),
                patch.object(w, "_checkpoint_resume", wraps=w._checkpoint_resume) as capture,
            ):
                with self.assertRaises(NoProgress):
                    self.run_fix(author)
            capture.assert_not_called()
            self.assertIsNone(processes[0].poll())
            self.assertFalse(a.agent_quiescent())
            self.assertTrue(w._active_resume_path(self.worker).exists())
            self.assertFalse(w._resume_paths(self.worker, self.c)[0].exists())
            self.assertEqual((self.co / "source").read_text(), "edit before spawn returns\n")
        finally:
            for proc in processes:
                a._stop_agent_group(
                    proc, os.getsid(proc.pid), os.getpgrp() if os.getsid(proc.pid) == os.getsid(0) else None
                )
                if proc.stdout:
                    proc.stdout.close()
            a._AGENT_QUIESCENT = True

    def test_native_interrupted_popen_retains_recovery_gate(self):
        # Run the same complete work-unit boundary in an explicitly supervised native session.
        proc = subprocess.Popen(
            [sys.executable, __file__, "RecoveryTests.test_interrupted_popen_never_captures_while_os_child_is_alive"],
            start_new_session=True,
            env={**os.environ, "TAUCETI_NATIVE_ROUND_PARENT": str(os.getpid())},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        output, _ = proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 0, output)

    def test_incomplete_direct_author_launch_blocks_recovery(self):
        active = w._active_resume_path(self.worker)
        w._write_resume_meta(
            active,
            {
                "pr": 999,
                "public_head": self.base,
                "stage": "fix",
                "author_scope_pending": True,
            },
        )
        with self.assertRaises(NoProgress):
            w._recover_active_checkout(self.worker)
        self.assertTrue(active.exists())

    def test_unreadable_process_inventory_never_certifies_quiescence(self):
        for output in ("", "not-a-process\n", "1 S\n"):
            with (
                self.subTest(output=output),
                patch.object(a.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, output, "")),
                self.assertRaises(NoProgress),
            ):
                a._author_groups(999999)

    def test_regrouped_writer_quiesces_before_return_in_direct_and_native_modes(self):
        from tauceti_worker import round as rnd

        unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        self.addCleanup(lambda: unrelated.poll() is None and unrelated.kill())
        self.addCleanup(lambda: unrelated.poll() is None and unrelated.terminate())
        for native in (False, True):
            with self.subTest(native=native):
                ready = self.co / f"ready-{native}"
                late = self.co / f"late-{native}"
                result = self.co / f"result-{native}"
                child = (
                    "import os,time; from pathlib import Path; os.setpgrp(); "
                    f"Path({str(ready)!r}).write_text(str(os.getpid())); "
                    f"time.sleep(2); Path({str(late)!r}).write_text('late write'); time.sleep(60)"
                )
                author = (
                    "import subprocess,sys,time; from pathlib import Path; "
                    f"subprocess.Popen([sys.executable,'-c',{child!r}],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
                    f"\nwhile not Path({str(ready)!r}).exists(): time.sleep(.01)"
                )
                caller = (
                    "import os,sys; from pathlib import Path; from tauceti_worker.agents import run_agent_proc,agent_quiescent; "
                    f"rc=run_agent_proc([sys.executable,'-c',{author!r}],env=dict(os.environ),"
                    f"logdir=Path({str(self.cfg.logdir)!r}),label='synthetic',provider='codex'); "
                    f"Path({str(result)!r}).write_text(str((rc,agent_quiescent())))"
                )
                env = dict(os.environ)
                if native:
                    env["TAUCETI_NATIVE_ROUND_PARENT"] = str(os.getpid())
                proc = subprocess.Popen([sys.executable, "-c", caller], start_new_session=native, env=env)
                self.assertEqual(proc.wait(timeout=10), 0)
                self.assertEqual(result.read_text(), "(0, True)")
                pid = int(ready.read_text())
                status = RUN(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True)
                self.assertTrue(status.returncode or status.stdout.strip().startswith("Z"))
                self.assertFalse(late.exists())
                self.assertIsNone(unrelated.poll())
                if native:
                    self.assertFalse(rnd._session_groups(proc.pid))
        unrelated.terminate()
        unrelated.wait(timeout=5)

    def test_control_group_allows_live_registered_heartbeat(self):
        from tauceti_worker import round as rnd

        if os.environ.get("TAUCETI_NATIVE_ROUND_PARENT") != str(os.getppid()) or os.getsid(0) != os.getpid():
            proc = subprocess.Popen(
                [sys.executable, __file__, "RecoveryTests.test_control_group_allows_live_registered_heartbeat"],
                start_new_session=True,
                env={**os.environ, "TAUCETI_NATIVE_ROUND_PARENT": str(os.getpid())},
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            output, _ = proc.communicate(timeout=15)
            self.assertEqual(proc.returncode, 0, output)
            return
        claims = rnd.Claims(self.cfg, NS(add_cleanup=lambda fn: None))
        with patch.object(
            rnd,
            "self_argv",
            side_effect=lambda *args: [
                sys.executable,
                "-c",
                "import os,sys; os.read(int(sys.argv[1]),1)",
                args[-1],
            ],
        ):
            claims.start_heartbeat("branch/fixture", "fixture/claims")
        try:
            self.assertEqual(os.getpgid(claims._hb.pid), os.getpgrp())
            self.assertEqual(
                a.run_agent_proc(
                    [sys.executable, "-c", "pass"],
                    env=dict(os.environ),
                    logdir=self.cfg.logdir,
                    label="synthetic",
                    provider="codex",
                ),
                0,
            )
            self.assertTrue(a.agent_quiescent())
            self.assertIsNone(claims._hb.poll())
            self.assertFalse(a._author_groups(os.getsid(0), os.getpgrp()))
        finally:
            claims.stop_heartbeat()

    def test_old_session_control_process_is_not_exempt_during_recovery(self):
        old_round = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        active = w._active_resume_path(self.worker)
        w._write_resume_meta(
            active,
            {
                "pr": 999,
                "public_head": self.base,
                "stage": "fix",
                "author_sid": old_round.pid,
                "author_excluded_pgid": old_round.pid,
            },
        )
        try:
            with self.assertRaises(NoProgress):
                w._recover_active_checkout(self.worker)
            self.assertIsNone(old_round.poll())
            self.assertTrue(active.exists())
        finally:
            old_round.terminate()
            old_round.wait(timeout=5)
        w._recover_active_checkout(self.worker)
        self.assertFalse(active.exists())

    def test_joined_control_group_blocks_fix_capture_until_outer_cleanup(self):
        from tauceti_worker import round as rnd

        ready = self.cfg.state.parent / "joined-ready"
        result = self.cfg.state.parent / "joined-result"
        writer = (
            "import os,time; from pathlib import Path; "
            f"Path({str(self.co / 'source')!r}).write_text('joined group edit\\n'); "
            f"Path({str(ready)!r}).write_text(str(os.getpid())); time.sleep(60)"
        )
        author = (
            "import os,subprocess,sys,time; from pathlib import Path; os.setpgid(0,os.getppid()); "
            f"subprocess.Popen([sys.executable,'-c',{writer!r}],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
            f"\nwhile not Path({str(ready)!r}).exists(): time.sleep(.01)"
        )
        caller = f"""
import json,os,sys
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
from tauceti_worker import agents as a, work_units as w
from tauceti_worker.config import NoProgress
from tauceti_worker.survey import Candidate,Counters
cfg=NS(checkout=Path({str(self.co)!r}),state=Path({str(self.cfg.state)!r}),logdir=Path({str(self.cfg.logdir)!r}))
c=Candidate(999,{self.base!r},'fixture')
pr=NS(number=999,head_owner='fixture',head_repo='fixture',head_ref='topic')
worker=NS(cfg=cfg,counters=Counters(cfg),rc=NS(),claims=NS(begin_branch_work=lambda *_:True),rs=NS(bust=lambda *_:None),gh=NS(pr_progress_state=lambda *_:{{'head':{self.base!r}}}))
def run(*args,on_launch):
 return a.run_agent_proc([sys.executable,'-c',{author!r}],env=dict(os.environ),logdir=cfg.logdir,label='synthetic',provider='codex',on_launch=on_launch)
blocked=False
with patch.object(w,'prepare_checkout',return_value=True),patch.object(w,'_restore_resume',return_value=True),patch.object(w,'_effective_authoring_profile',return_value=None),patch.object(w,'run_agent_host',side_effect=run),patch.object(w,'_checkpoint_resume',wraps=w._checkpoint_resume) as capture:
 try: w.do_fix(worker,NS(open_prs=[pr]),c,NS(agent_name='synthetic'),False)
 except NoProgress: blocked=True
 Path({str(result)!r}).write_text(json.dumps({{'blocked':blocked,'quiescent':a.agent_quiescent(),'active':w._active_resume_path(worker).exists(),'capture_called':capture.called,'checkpoint':w._resume_paths(worker,c)[0].exists()}}))
"""
        unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        try:

            def spawn(_):
                return subprocess.Popen(
                    [sys.executable, "-c", caller],
                    start_new_session=True,
                    env={**os.environ, "TAUCETI_NATIVE_ROUND_PARENT": str(os.getpid())},
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

            with patch.object(rnd, "spawn_round", side_effect=spawn):
                self.assertEqual(rnd.run_round_subprocess([], timeout=10), 0)
            self.assertEqual(
                json.loads(result.read_text()),
                {
                    "blocked": True,
                    "quiescent": False,
                    "active": True,
                    "capture_called": False,
                    "checkpoint": False,
                },
            )
            self.assertIsNone(unrelated.poll())
            writer_pid = int(ready.read_text())
            status = RUN(["ps", "-p", str(writer_pid), "-o", "stat="], capture_output=True, text=True)
            self.assertTrue(status.returncode or status.stdout.strip().startswith("Z"))
            w._recover_active_checkout(self.worker)
            self.assertFalse(w._active_resume_path(self.worker).exists())
            self.assertTrue(w._restore_resume(self.worker, self.c, self.pr))
            self.assertEqual((self.co / "source").read_text(), "joined group edit\n")
        finally:
            unrelated.terminate()
            unrelated.wait(timeout=5)

    def test_native_sigkill_reaps_regrouped_writer_before_recovery(self):
        from tauceti_worker import round as rnd

        active = w._active_resume_path(self.worker)
        ready = self.co / "ready"
        w._write_resume_meta(active, {"pr": 999, "public_head": self.base, "stage": "fix"})
        writer = (
            "import os,time; from pathlib import Path; os.setpgrp(); "
            f"Path({str(self.co / 'source')!r}).write_text('hard-killed edit\\n'); "
            f"Path({str(ready)!r}).write_text(str(os.getpid())); time.sleep(60)"
        )
        author = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{writer!r}]); time.sleep(60)"
        caller = (
            "import os,sys,time,signal,threading; from pathlib import Path; "
            "from tauceti_worker.agents import run_agent_proc; "
            "\ndef crash():\n"
            f" while not Path({str(ready)!r}).exists(): time.sleep(.01)\n"
            " os.kill(os.getpid(),signal.SIGKILL)\n"
            "threading.Thread(target=crash,daemon=True).start()\n"
            f"run_agent_proc([sys.executable,'-c',{author!r}],env=dict(os.environ),"
            f"logdir=Path({str(self.cfg.logdir)!r}),label='synthetic',provider='codex')"
        )

        def spawn(_):
            return subprocess.Popen(
                [sys.executable, "-c", caller],
                start_new_session=True,
                env={
                    **os.environ,
                    "TAUCETI_NATIVE_ROUND_PARENT": str(os.getpid()),
                    "TAUCETI_ACTIVE_CHECKOUT": str(active),
                },
            )

        with patch.object(rnd, "spawn_round", side_effect=spawn):
            self.assertEqual(rnd.run_round_subprocess([], timeout=10), -signal.SIGKILL)
        w._recover_active_checkout(self.worker)
        self.assertFalse(active.exists())
        self.assertTrue(w._restore_resume(self.worker, self.c, self.pr))
        self.assertEqual((self.co / "source").read_text(), "hard-killed edit\n")

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
