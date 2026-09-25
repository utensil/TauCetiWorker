"""Durable repair intent and checkout preservation across process termination.

The round owner calls recovery only after proving its descendants have exited.
Checkout preparation repeats it under the same lock, covering supervisor death.
"""

from __future__ import annotations

import json
import re
import subprocess
import time

from .config import log
from .runtime_status import atomic_json


def _git(cfg, *args):
    return subprocess.run(
        ["git", "-C", str(cfg.checkout), *args],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    ).stdout.strip()


def _save(cfg, prefix):
    head = _git(cfg, "rev-parse", "HEAD")
    dirty = bool(_git(cfg, "status", "--porcelain", "--untracked-files=all"))
    _git(cfg, "update-ref", prefix, head)
    stash_ref = None
    if dirty:
        _git(cfg, "stash", "push", "--include-untracked", "--quiet", "-m", "tauceti recovery")
        stash = _git(cfg, "rev-parse", "refs/stash")
        stash_ref = prefix + "-stash"
        _git(cfg, "update-ref", stash_ref, stash)
        if _git(cfg, "status", "--porcelain", "--untracked-files=all"):
            raise ValueError("checkout still dirty after preservation")
    return head, stash_ref


def checkpoint_repair(cfg, pr, public_head, label):
    """Keep the existing exact-public-head resume format, including index state."""
    if not isinstance(pr, int) or pr <= 0 or not re.fullmatch(r"[0-9a-f]{40}", public_head):
        raise ValueError("invalid repair recovery identity")
    head = _git(cfg, "rev-parse", "HEAD")
    directory = cfg.state / "resume"
    record = directory / f"{pr}-{public_head[:12]}.json"
    if not _git(cfg, "status", "--porcelain", "--untracked-files=all"):
        # Recovery may restart after a completed checkpoint but before its caller's
        # next write. Keep that checkpoint's dirty stash even on a private commit.
        if head == public_head or (record.exists() and json.loads(record.read_text())["candidate_head"] == head):
            return
    commit_ref = f"refs/tauceti-resume/{pr}/{public_head}"
    head, saved_stash = _save(cfg, commit_ref)
    stash_ref = f"refs/tauceti-resume-stash/{pr}/{public_head}" if saved_stash else None
    if saved_stash:
        _git(cfg, "update-ref", stash_ref, _git(cfg, "rev-parse", saved_stash))
        _git(cfg, "update-ref", "-d", saved_stash)
    directory.mkdir(parents=True, exist_ok=True)
    atomic_json(
        record,
        {
            "pr": pr,
            "public_head": public_head,
            "candidate_head": head,
            "commit_ref": commit_ref,
            "stash_ref": stash_ref,
            "stage": label,
            "created_at": int(time.time()),
        },
    )
    log(f"  {label} #{pr}: checkpointed local candidate @{head[:12]} for failed-round resume")


def arm_repair(cfg, pr, public_head, label, *, charged=None):
    cfg.state.mkdir(parents=True, exist_ok=True)
    atomic_json(
        cfg.state / "active-repair.json",
        {
            "pr": pr,
            "public_head": public_head,
            "label": label,
            "charged": charged or {},
        },
    )


def disarm_repair(cfg):
    (cfg.state / "active-repair.json").unlink(missing_ok=True)


def _refund_interruption(cfg, path, data, requested):
    """Replay exact counter targets after a verified operator stop, under round.lock."""
    from .constants import MAX_INFRA_REFUNDS
    from .survey import Counters

    counters = Counters(cfg)
    targets = data.get("interruption_refund")
    if targets is None:
        charged = data.get("charged") or {}
        resume = cfg.state / "resume" / f"{data['pr']}-{data['public_head'][:12]}.json"
        if not requested or not charged or not resume.exists():
            return
        if any(counters.read(key) != value or value <= 0 for key, value in charged.items()):
            log("interrupted repair: budget changed; retaining charges and saved candidate")
            return
        allowance = f"infra-{data['label']}-{data['pr']}"
        used = counters.read(allowance)
        if used >= MAX_INFRA_REFUNDS:
            log(f"interrupted repair: {MAX_INFRA_REFUNDS} infrastructure refunds spent; retaining charge")
            return
        targets = {key: value - 1 for key, value in charged.items()}
        targets[allowance] = used + 1
        # Persist the intended values before any write: a killed recovery must not refund twice.
        data["interruption_refund"] = targets
        atomic_json(path, data)
    for key, value in targets.items():
        counters.write(key, value)
    log(f"  {data['label']} #{data['pr']}: operator interruption; saved candidate and refunded attempt")


def recover_interrupted_repair(cfg, *, operator_interrupted=False):
    path = cfg.state / "active-repair.json"
    if not path.exists():
        return
    data = json.loads(path.read_text())
    checkpoint_repair(cfg, data["pr"], data["public_head"], data["label"])
    _refund_interruption(cfg, path, data, operator_interrupted)
    disarm_repair(cfg)


def preserve_before_checkout(cfg):
    """Never let a forced checkout erase an uncheckpointed interrupted candidate."""
    recover_interrupted_repair(cfg)
    if not _git(cfg, "status", "--porcelain", "--untracked-files=all"):
        return
    prefix = f"refs/tauceti-checkout-recovery/{time.time_ns()}"
    head, stash_ref = _save(cfg, prefix)
    directory = cfg.state / "checkout-recovery"
    directory.mkdir(parents=True, exist_ok=True)
    atomic_json(
        directory / (prefix.rsplit("/", 1)[1] + ".json"),
        {
            "head": head,
            "commit_ref": prefix,
            "stash_ref": stash_ref,
        },
    )
    log(f"checkout: preserved interrupted work in {prefix}; metadata at {directory}")
