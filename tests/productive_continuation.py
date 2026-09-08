#!/usr/bin/env python3
"""A failed native author attempt continues its exact local candidate on the next admission."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

wu = tc.work_units
agents = tc.agents
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"[{'OK ' if ok else 'XX '}] {name}: got {got!r} want {want!r}")


def git(repo, *args, capture=False):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=not capture,
        capture_output=capture,
        text=True,
    )


class GitHub:
    def __init__(self, remote):
        self.remote = remote
        self.unknown = False

    def pr_progress_state(self, _pr):
        if self.unknown:
            return None
        p = subprocess.run(
            ["git", "--git-dir", str(self.remote), "rev-parse", "refs/heads/topic"],
            check=True,
            capture_output=True,
            text=True,
        )
        return {"head": p.stdout.strip(), "ncomments": 0}


class Kinds:
    def __init__(self, candidates):
        self.open_prs = []
        self._kinds = {name: SimpleNamespace(actionable=[]) for name in wu.AUTO_STAGES}
        self._kinds["fix"].actionable = candidates

    def kind(self, name):
        return self._kinds[name]


with tempfile.TemporaryDirectory(prefix="tauceti-continuation-") as td:
    root = Path(td)
    remote, seed, checkout = root / "remote.git", root / "seed", root / "checkout"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
    git(seed, "config", "user.name", "Continuation Test")
    git(seed, "config", "user.email", "test.invalid")
    (seed / ".gitignore").write_text("build/\n")
    (seed / "tracked.txt").write_text("base\n")
    git(seed, "add", ".gitignore", "tracked.txt")
    git(seed, "commit", "-qm", "public head")
    public = git(seed, "rev-parse", "HEAD", capture=True).stdout.strip()
    git(seed, "remote", "add", "origin", str(remote))
    git(seed, "push", "-q", "origin", "main", "HEAD:topic")
    subprocess.run(["git", "--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
    subprocess.run(["git", "clone", "-q", str(remote), str(checkout)], check=True)
    git(checkout, "config", "user.name", "Continuation Test")
    git(checkout, "config", "user.email", "test.invalid")

    # The ordinary first admission starts clean on main, so exercise prepare_checkout + PR checkout.
    bindir = root / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text("#!/bin/sh\nexec git checkout -q -B topic origin/topic\n")
    gh.chmod(0o755)
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{bindir}{os.pathsep}{old_path}"

    cfg = SimpleNamespace(checkout=checkout, state=root / "state", logdir=root / "logs", data_home=root / "home")
    github = GitHub(remote)
    counters = wu.Counters(SimpleNamespace(state=cfg.state))
    worker = SimpleNamespace(
        cfg=cfg,
        gh=github,
        rs=SimpleNamespace(bust=lambda _pr: None),
        counters=counters,
        rc=SimpleNamespace(change_base_head=None),
        claims=SimpleNamespace(begin_branch_work=lambda *_args: True),
    )
    pr = SimpleNamespace(number=77, head_oid=public, head_ref="topic", head_owner="test", head_repo="repo")
    candidate = wu.Candidate(77, public, "blocking review")
    survey = SimpleNamespace(open_prs=[pr])
    opts = wu.RoundOpts(
        only=["fix"],
        agent="codex",
        work_model="codex",
        sandbox_host=True,
        dry_run=False,
        authoring_profile=agents.AuthoringProfile("codex", "test", None, "test", "test"),
    )

    saved = {
        "sync": agents.sync_mathlib_pool,
        "agent": wu.run_agent_host,
        "which": wu.shutil.which,
        "runtime": wu.report_runtime,
        "failure": wu.report_failure,
    }
    calls = []

    def author(_cwd, _prompt, _profile, _logdir):
        calls.append("author")
        if len(calls) == 1:
            with (checkout / "tracked.txt").open("a") as f:
                f.write("staged\n")
            git(checkout, "add", "tracked.txt")
            with (checkout / "tracked.txt").open("a") as f:
                f.write("unstaged\n")
            (checkout / "new.txt").write_bytes(b"untracked bytes\x00\n")
            (checkout / "build").mkdir()
            (checkout / "build" / "artifact.bin").write_bytes(b"built-once\x00")
            return 1  # worked capacity remains charged; no checkpoint/stash is made here

        check(
            "second attempt sees staged bytes",
            git(checkout, "show", ":tracked.txt", capture=True).stdout,
            "base\nstaged\n",
        )
        check("second attempt sees unstaged bytes", (checkout / "tracked.txt").read_text(), "base\nstaged\nunstaged\n")
        check("second attempt sees untracked bytes", (checkout / "new.txt").read_bytes(), b"untracked bytes\x00\n")
        check(
            "second attempt reuses build artifact",
            (checkout / "build" / "artifact.bin").read_bytes(),
            b"built-once\x00",
        )
        git(checkout, "add", "tracked.txt", "new.txt")
        git(checkout, "commit", "-qm", "continue candidate")
        check("push CAS stays at admitted public head", os.environ.get("TAUCETI_PUSH_EXPECT"), public)
        git(checkout, "push", "-q", "origin", "HEAD:topic")
        return 0

    try:
        agents.sync_mathlib_pool = lambda _cfg: None
        wu.run_agent_host = author
        wu.shutil.which = lambda _name: "/synthetic/agent"
        wu.report_runtime = lambda *_args, **_kwargs: None
        wu.report_failure = lambda *_args, **_kwargs: None

        first = wu.dispatch("fix", worker, survey, candidate, opts)
        check("first worked attempt fails", first, 1)
        check("worked attempt remains charged", counters.read(f"fix-77-{public[:12]}"), 1)
        check(
            "first attempt leaves mixed index/worktree",
            git(checkout, "status", "--porcelain", capture=True).stdout.splitlines(),
            ["MM tracked.txt", "?? new.txt"],
        )
        check("ignored artifact remains", (checkout / "build" / "artifact.bin").read_bytes(), b"built-once\x00")

        # Existing recovery metadata can coexist with already materialized contents without replay.
        meta_path, commit_ref, _ = wu._resume_paths(worker, candidate)
        git(checkout, "update-ref", commit_ref, public)
        meta_path.parent.mkdir(parents=True)
        meta_path.write_text(
            json.dumps(
                {
                    "pr": 77,
                    "public_head": public,
                    "candidate_head": public,
                    "commit_ref": commit_ref,
                    "stash_ref": None,
                    "stage": "fix",
                }
            )
        )

        other = wu.Candidate(88, "b" * 40, "other")
        kinds = Kinds([other, candidate])
        kinds.open_prs = [pr]
        wu._prioritize_continuation(worker, kinds)
        check("same target is selected before another PR", kinds.kind("fix").actionable[0].pr, 77)

        second = wu.dispatch("fix", worker, survey, candidate, opts)
        check("second attempt publishes", second, 0)
        check("exactly two author launches", len(calls), 2)
        check("artifact was not rebuilt", (checkout / "build" / "artifact.bin").read_bytes(), b"built-once\x00")
        check("published checkout is clean", git(checkout, "status", "--porcelain", capture=True).stdout, "")
        check("proven recovery metadata cleared", meta_path.exists(), False)

        local = git(checkout, "rev-parse", "HEAD", capture=True).stdout.strip()
        fresh = wu.Candidate(77, local, "fresh public head")
        clean_kinds = Kinds([other, fresh])
        clean_kinds.open_prs = [SimpleNamespace(**{**vars(pr), "head_oid": local})]
        wu._prioritize_continuation(worker, clean_kinds)
        check("clean unchanged head does not monopolize queue", clean_kinds.kind("fix").actionable[0].pr, 88)

        fresh_path, fresh_ref, _ = wu._resume_paths(worker, fresh)
        git(checkout, "update-ref", fresh_ref, local)
        fresh_path.write_text(
            json.dumps(
                {
                    "pr": 77,
                    "public_head": local,
                    "candidate_head": local,
                    "commit_ref": fresh_ref,
                    "stash_ref": None,
                    "stage": "fix",
                }
            )
        )
        wu._clear_published_resume(worker, fresh, "topic")
        check("comment-only rc0 retains unchanged candidate", fresh_path.exists(), True)

        # rc0 without a push is not publication: dispatch raises and retains both recovery and HEAD.
        git(checkout, "update-ref", commit_ref, public)
        meta_path.write_text(
            json.dumps(
                {
                    "pr": 77,
                    "public_head": public,
                    "candidate_head": public,
                    "commit_ref": commit_ref,
                    "stash_ref": None,
                    "stage": "fix",
                }
            )
        )
        wu.run_agent_host = lambda *_args: 0
        refused = False
        try:
            wu.dispatch("fix", worker, survey, candidate, opts)
        except tc.config.NoProgress:
            refused = True
        check("rc0 refused publication is rejected", refused, True)
        check("rc0 refused publication retains recovery", meta_path.exists(), True)
        check(
            "rc0 refused publication retains candidate",
            git(checkout, "rev-parse", "HEAD", capture=True).stdout.strip(),
            local,
        )

        github.unknown = True
        wu._clear_published_resume(worker, candidate, "topic")
        check("unknown publication retains recovery", meta_path.exists(), True)

        for raw in ("[]", "null"):
            meta_path.write_text(raw)
            before_calls = len(calls)
            check(
                f"non-object recovery {raw} fails closed",
                wu._do_fixlike(worker, survey, candidate, opts, False, prompt_file="fix.md", label="fix"),
                1,
            )
            check(f"non-object recovery {raw} launches no agent", len(calls), before_calls)
            check(f"non-object recovery {raw} remains intact", meta_path.read_text(), raw)

        # The pre-existing checkpoint format still restores once, including its saved index.
        with (checkout / "tracked.txt").open("a") as f:
            f.write("legacy staged\n")
        git(checkout, "add", "tracked.txt")
        wu._checkpoint_resume(worker, candidate, "fix")
        check("legacy checkpoint has a saved payload", bool(json.loads(meta_path.read_text())["stash_ref"]), True)
        check("legacy restore preparation succeeds", agents.prepare_checkout(cfg), True)
        check("legacy saved candidate restores", wu._restore_resume(worker, candidate, pr), True)
        check(
            "legacy restore preserves index",
            git(checkout, "diff", "--cached", "--name-only", capture=True).stdout,
            "tracked.txt\n",
        )
        check("restored payload is marked materialized", json.loads(meta_path.read_text())["stash_ref"], None)
        git(checkout, "restore", "--staged", "--worktree", "tracked.txt")

        # Ordinary preparation keeps ignored artifacts, and refuses to rewind an ahead local main.
        git(checkout, "switch", "main")
        check("ordinary preparation succeeds", agents.prepare_checkout(cfg), True)
        check(
            "ordinary preparation retains ignored artifact",
            (checkout / "build" / "artifact.bin").read_bytes(),
            b"built-once\x00",
        )
        git(checkout, "switch", "topic")
        git(checkout, "branch", "-f", "main", "topic")
        main_head = git(checkout, "rev-parse", "HEAD", capture=True).stdout.strip()
        check(
            "ahead local main blocks destructive preparation",
            agents.continuation_checkout(cfg, "missing", "c" * 40),
            None,
        )
        check(
            "ahead local main remains referenced",
            git(checkout, "rev-parse", "main", capture=True).stdout.strip(),
            main_head,
        )

        # A dirty foreign branch is never reset merely because this PR is actionable again.
        git(checkout, "switch", "-qc", "other-local")
        (checkout / "foreign.txt").write_text("preserve me\n")
        before_calls = len(calls)
        unsafe = wu._do_fixlike(worker, survey, candidate, opts, False, prompt_file="fix.md", label="fix")
        check("unsafe other-branch work fails closed", unsafe, 1)
        check("unsafe path does not launch agent", len(calls), before_calls)
        check("unsafe other-branch bytes survive", (checkout / "foreign.txt").read_text(), "preserve me\n")
        check("unsafe path retains recovery", meta_path.exists(), True)
    finally:
        agents.sync_mathlib_pool = saved["sync"]
        wu.run_agent_host = saved["agent"]
        wu.shutil.which = saved["which"]
        wu.report_runtime = saved["runtime"]
        wu.report_failure = saved["failure"]
        os.environ["PATH"] = old_path
        os.environ.pop("TAUCETI_PUSH_EXPECT", None)

print("\nFAIL" if fails else "\nall productive-continuation checks passed")
sys.exit(1 if fails else 0)
