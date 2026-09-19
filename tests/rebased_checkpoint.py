#!/usr/bin/env python3
"""Resume a real rebased repair, retaining exact checkpoint and public-CAS binding."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tauceti_worker import work_units as wu
from tauceti_worker.agents import continuation_checkout
from tauceti_worker.checkout_recovery import checkpoint_repair
from tauceti_worker.work_units import _restore_resume, _resume_metadata, _resume_paths

with tempfile.TemporaryDirectory(prefix="rebased-checkpoint-") as tmp:
    root = Path(tmp)
    co = root / "checkout"
    co.mkdir()

    def git(*args):
        return subprocess.check_output(["git", "-C", str(co), *args], text=True).strip()

    git("init", "-q", "-b", "main")
    git("config", "user.name", "Checkpoint Test")
    git("config", "user.email", "test.invalid")
    (co / "base").write_text("base\n")
    git("add", "base")
    git("commit", "-qm", "base")
    git("checkout", "-qb", "topic")
    (co / "proof").write_text("published\n")
    git("add", "proof")
    git("commit", "-qm", "published proof")
    public = git("rev-parse", "HEAD")
    git("checkout", "-q", "main")
    (co / "upstream").write_text("dependency update\n")
    git("add", "upstream")
    git("commit", "-qm", "upstream")
    git("update-ref", "refs/remotes/origin/main", "HEAD")
    git("checkout", "-q", "topic")
    git("rebase", "main")
    rebased = git("rev-parse", "HEAD")
    assert subprocess.run(["git", "-C", str(co), "merge-base", "--is-ancestor", public, rebased]).returncode == 1
    cfg = SimpleNamespace(checkout=co, state=root / "state")
    worker = SimpleNamespace(cfg=cfg)
    candidate = SimpleNamespace(pr=77, head=public)
    pr = SimpleNamespace(head_ref="topic")
    checkpoint_repair(cfg, 77, public, "fix")
    path, ref, _ = _resume_paths(worker, candidate)
    meta = _resume_metadata(worker, candidate)
    assert isinstance(meta, dict)
    assert continuation_checkout(cfg, "topic", public, saved_head=rebased) is None
    assert continuation_checkout(cfg, "topic", public, saved_head=rebased, checkpoint_verified=True) is True

    # Exercise actual fix admission: it must reuse the rewritten tree while the
    # publication compare-and-swap still expects the original remote public head.
    cfg.logdir = root / "logs"
    worker.claims = SimpleNamespace(begin_branch_work=lambda *args: True)
    worker.rc = SimpleNamespace()
    worker.rs = SimpleNamespace(bust=lambda pr: None)
    pr.number, pr.head_owner, pr.head_repo = 77, "test", "repo"
    with (
        patch.dict(os.environ),
        patch.object(wu, "_effective_authoring_profile", return_value=None),
        patch.object(wu, "run_agent_host", return_value=0) as author,
        patch.object(wu, "prepare_checkout") as prepare,
    ):
        result = wu._do_fixlike(
            worker,
            SimpleNamespace(open_prs=[pr]),
            candidate,
            SimpleNamespace(agent_name="codex"),
            False,
            prompt_file="fix.md",
            label="fix",
        )
        assert result == 0 and author.call_count == 1 and not prepare.called
        assert worker.rc.change_base_head == rebased
        assert os.environ["TAUCETI_PUSH_EXPECT"] == public

    # The saved index and untracked source must survive rebased checkpoint restoration.
    (co / "proof").write_text("repaired\n")
    git("add", "proof")
    (co / "consumer").write_text("untracked consumer\n")
    checkpoint_repair(cfg, 77, public, "fix")
    assert (
        continuation_checkout(cfg, "topic", public, saved_head=rebased, saved_payload=True, checkpoint_verified=True)
        is False
    )
    git("checkout", "-q", "main")
    assert _restore_resume(worker, candidate, pr) is True
    assert git("rev-parse", "HEAD") == rebased
    assert git("diff", "--cached", "--name-only") == "proof"
    assert (co / "proof").read_text() == "repaired\n"
    assert (co / "consumer").read_text() == "untracked consumer\n"
    assert _resume_metadata(worker, candidate)["stash_ref"] is None

    # An unrelated ref, substituted object, or changed public head never authorizes reuse.
    good = path.read_text()
    data = json.loads(good)
    data["commit_ref"] = "refs/heads/topic"
    path.write_text(json.dumps(data))
    assert _resume_metadata(worker, candidate) is None
    assert _restore_resume(worker, candidate, pr) is None
    path.write_text(good)
    git("update-ref", ref, public)
    assert _resume_metadata(worker, candidate) is None
    git("update-ref", ref, rebased)
    changed = SimpleNamespace(pr=77, head="a" * 40)
    assert _resume_metadata(worker, changed) is False
    assert git("rev-parse", "HEAD") == rebased
    assert (co / "proof").read_text() == "repaired\n"
print("PASS: rebased checkpoint restores exact objects/index without weakening public CAS binding")
