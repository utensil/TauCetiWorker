#!/usr/bin/env python3
"""Real local Git pushes exercise publication failures, destination binding and receipts."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from tauceti_worker import work_units as wu
from tauceti_worker.publication import bind_publication
from tauceti_worker.review_state import Meta
from tauceti_worker.survey import Counters, fix_disposition


class Publication(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="publication-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.fork = self.root / "fork.git"
        self.upstream = self.root / "upstream.git"
        self.git("init", "-q", "--bare", str(self.fork), cwd=self.root)
        self.git("init", "-q", "--bare", str(self.upstream), cwd=self.root)
        self.git("init", "-q", "-b", "topic", str(self.repo), cwd=self.root)
        self.git("config", "user.name", "Publication Test")
        self.git("config", "user.email", "test.invalid")
        (self.repo / "source.txt").write_text("base\n")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD")
        self.git("remote", "add", "origin", str(self.upstream))
        self.git("push", "-q", str(self.fork), "HEAD:topic")
        (self.repo / "source.txt").write_text("candidate\n")
        self.git("commit", "-qam", "candidate")
        self.head = self.git("rev-parse", "HEAD")
        self.checker = self.root / "check"
        self.checker.write_text("#!/bin/sh\nexit 0\n")
        self.checker.chmod(0o755)
        self.receipt = self.root / "published.txt"
        self.env = {
            **os.environ,
            "TAUCETI_PUSH_REMOTE": str(self.fork),
            "TAUCETI_PUSH_REF": "topic",
            "TAUCETI_PUSH_EXPECT": self.base,
            "TAUCETI_PRE_PUSH_CHECK": str(self.checker),
            "TAUCETI_CLAIM_KEY": "",
            "TAUCETI_PUBLISHED_HEAD_FILE": str(self.receipt),
        }
        with patch.dict(os.environ, self.env, clear=True):
            bind_publication(self.repo)

    def git(self, *args, cwd=None):
        return subprocess.run(
            ["git", *args], cwd=cwd or self.repo, capture_output=True, text=True, check=True
        ).stdout.strip()

    def push(self, **env):
        return subprocess.run(
            [str(REPO / "scripts/git-safe-push")],
            cwd=self.repo,
            env={**self.env, **env},
            capture_output=True,
            text=True,
        )

    def remote_head(self):
        return self.git("--git-dir", str(self.fork), "rev-parse", "topic")

    def test_bound_fork_survives_wrong_origin_export(self):
        result = self.push(TAUCETI_PUSH_REMOTE="origin")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.remote_head(), self.head)
        self.assertEqual(self.receipt.read_text().strip(), self.head)
        self.assertEqual(self.git("--git-dir", str(self.upstream), "for-each-ref", "refs/heads"), "")

    def test_false_green_output_cannot_override_failure(self):
        self.checker.write_text("#!/bin/sh\necho 'The module compiles'\nexit 1\n")
        result = self.push()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("candidate validation failed", result.stderr)
        self.assertEqual(self.remote_head(), self.base)
        self.assertFalse(self.receipt.exists())

    def test_candidate_mutation_during_check_blocks_push(self):
        self.checker.write_text("#!/bin/sh\necho changed >> source.txt\nexit 0\n")
        self.assertNotEqual(self.push().returncode, 0)
        self.assertEqual(self.remote_head(), self.base)
        self.assertFalse(self.receipt.exists())

    def test_new_commit_during_check_blocks_push(self):
        self.checker.write_text("#!/bin/sh\necho changed >> source.txt\ngit commit -qam changed\n")
        result = self.push()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("candidate changed during validation", result.stderr)
        self.assertEqual(self.remote_head(), self.base)

    def test_missing_check_blocks_push(self):
        self.checker.unlink()
        self.assertNotEqual(self.push().returncode, 0)
        self.assertEqual(self.remote_head(), self.base)

    def test_dirty_candidate_never_launches_check(self):
        self.checker.write_text(f"#!/bin/sh\ntouch '{self.root / 'ran'}'\n")
        (self.repo / "source.txt").write_text("unfinished\n")
        self.assertNotEqual(self.push().returncode, 0)
        self.assertFalse((self.root / "ran").exists())

    def test_branch_override_rejected(self):
        self.assertNotEqual(self.push(TAUCETI_PUSH_REF="wrong").returncode, 0)
        self.assertEqual(self.remote_head(), self.base)

    def test_stale_lease_preserves_peer(self):
        self.git("push", "-q", str(self.fork), "HEAD:topic")
        (self.repo / "source.txt").write_text("next\n")
        self.git("commit", "-qam", "next")
        result = self.push()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.remote_head(), self.head)
        self.assertFalse(self.receipt.exists())

    def test_unknown_remote_error_is_not_mislabeled_as_race(self):
        self.git("config", "--file", ".git/tauceti-publication", "publication.remote", str(self.root / "missing.git"))
        result = self.push()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("permission/transport failure is not a branch race", result.stderr)
        self.assertNotIn("moved since checkout", result.stderr)

    def test_worktrees_keep_separate_publication_policies(self):
        peer = self.root / "peer"
        self.git("worktree", "add", "--detach", str(peer), self.base)
        original = (self.repo / ".git/tauceti-publication").read_bytes()
        with patch.dict(os.environ, {**self.env, "TAUCETI_PUSH_REMOTE": str(self.upstream)}, clear=True):
            bind_publication(peer)
        self.assertEqual((self.repo / ".git/tauceti-publication").read_bytes(), original)
        self.assertEqual(self.push(TAUCETI_PUSH_REMOTE="origin").returncode, 0)
        self.assertEqual(self.remote_head(), self.head)

    def test_legacy_fix_debits_survive_head_change(self):
        counters = Counters(SimpleNamespace(state=self.root / "state"))
        counters.write("fix-77-aaaaaaaaaaaa", 3)
        counters.write("fix-77-bbbbbbbbbbbb", 2)
        counters.write("fix-771-cccccccccccc", 90)
        counters.write("fix-77-not-a-head", 90)
        self.assertEqual(counters.fix_pr_attempts(77), 5)
        meta = Meta({"head_sha": "new"}, "fresh")
        status, _ = fix_disposition(meta, "new", True, True, 0, per_pr=counters.fix_pr_attempts(77))
        self.assertEqual(status, "exhausted")
        self.assertEqual(fix_disposition(meta, "new", True, True, 0, per_pr=4)[0], "actionable")
        self.assertEqual(fix_disposition(meta, "new", True, True, 0, per_pr=5, pending_contest=True)[0], "waiting")

    def test_new_pr_requires_marker_and_published_head(self):
        worker = SimpleNamespace(gh=SimpleNamespace())
        candidate = wu.Candidate(0, "", "test")
        pre = {"prs": {1}, "published_head": self.head}
        worker.gh.pr_view = lambda *_: {"body": "<!--tauceti-target:v1 {}-->", "headRefOid": self.head}
        with patch.object(wu, "_open_pr_numbers", return_value={1, 2}):
            self.assertTrue(wu._progressed(worker, candidate, pre))
            worker.gh.pr_view = lambda *_: {"body": "<!--tauceti-target:v1 {}-->", "headRefOid": self.base}
            self.assertFalse(wu._progressed(worker, candidate, pre))
            worker.gh.pr_view = lambda *_: {"body": "no marker", "headRefOid": self.head}
            self.assertFalse(wu._progressed(worker, candidate, pre))
            worker.gh.pr_view = lambda *_: None
            self.assertFalse(wu._progressed(worker, candidate, pre))

    def test_progress_requires_this_rounds_remote_receipt(self):
        result = {"head": self.head, "ncomments": 100}
        worker = SimpleNamespace(
            cfg=SimpleNamespace(checkout=self.repo),
            gh=SimpleNamespace(pr_progress_state=lambda _: result),
        )
        candidate = wu.Candidate(77, self.base, "test")
        pre = {"head": self.base, "ncomments": 0, "published_head": ""}
        self.assertFalse(wu._progressed(worker, candidate, None))
        self.assertFalse(wu._progressed(worker, candidate, pre))
        pre["published_head"] = self.head
        self.assertTrue(wu._progressed(worker, candidate, pre))
        result["head"] = self.base  # comment growth alone
        self.assertFalse(wu._progressed(worker, candidate, pre))
        result["head"] = "c" * 40  # another worker's push
        self.assertFalse(wu._progressed(worker, candidate, pre))
        worker.gh.pr_progress_state = lambda _: None
        self.assertFalse(wu._progressed(worker, candidate, pre))


if __name__ == "__main__":
    unittest.main()
