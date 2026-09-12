#!/usr/bin/env python3
"""Full-survey regression: budget-deferred rubric slots must remain reviewable.

The review engine can stop for its daily budget before completing every rubric.
Once budget is available, an unchanged head must return to review unless an actual
author finding explains the deferred slots. Exercise both review and fix queues.
"""

import importlib
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from tauceti_worker.review_state import Meta, ReviewState

survey_module = importlib.import_module("tauceti_worker.survey")


class FakeRS(ReviewState):
    def __init__(self, states, runs):
        self.metadata = {"head_sha": "H", "states": states, "runs": runs}

    def gh_meta(self, _pr):
        return Meta(self.metadata, "fresh")

    def newest_contest_reply(self, _pr):
        return None

    def inflight_review(self, _pr, _head):
        return set()


raw_pr = {
    "number": 999,
    "title": "fixture",
    "body": "",
    "headRefOid": "H",
    "headRefName": "roadmap/fixture",
    "headRepositoryOwner": {"login": "fixture-author"},
    "headRepository": {"name": "TauCeti"},
    "isDraft": False,
    "statusCheckRollup": [{"__typename": "StatusContext", "context": "build", "state": "SUCCESS"}],
    "author": {"login": "fixture-author"},
    "mergeable": "MERGEABLE",
    "labels": [],
}

# (description, durable rubric states, latest verdicts, review candidates, fix candidates)
cases = [
    ("budget stop after approval", {"naming": "green", "reuse": "absent"}, ["approve"], [999], []),
    ("budget stop before any new call", {"naming": "stale", "reuse": "absent"}, [], [999], []),
    ("stale slot still needs review", {"naming": "green", "reuse": "stale"}, ["approve"], [999], []),
    ("retained error after partial approval", {"naming": "green", "reuse": "error"}, ["approve"], [999], []),
    ("finding beside execution error", {"naming": "blocking_request", "reuse": "error"}, ["error"], [999], [999]),
    ("intentional block halt", {"correctness": "blocking_block", "reuse": "absent"}, ["block"], [], [999]),
    ("retained finding with deferred slots", {"naming": "blocking_request", "reuse": "stale"}, [], [], [999]),
    ("unknown slot requires review", {"naming": "green", "reuse": "unknown"}, ["approve"], [999], []),
    ("finding cannot hide unknown slot", {"naming": "blocking_block", "reuse": "unknown"}, [], [999], [999]),
    ("all approved", {"naming": "green", "reuse": "green"}, ["approve"], [], []),
    ("durable green supersedes old error", {"naming": "green", "reuse": "green"}, ["error"], [], []),
    ("absent-only skeleton", {"naming": "absent", "reuse": "absent"}, [], [999], []),
    ("legacy error", {}, ["error"], [999], []),
    ("legacy finding", {}, ["request_changes"], [], [999]),
    ("legacy approval", {}, ["approve"], [], []),
]

fails = 0
with (
    tempfile.TemporaryDirectory(prefix="review-incomplete-") as directory,
    patch.object(survey_module, "me", return_value="fixture-author"),
    patch.object(survey_module, "progress_due", return_value=(False, "fixture not due")),
):
    cfg = SimpleNamespace(wid="fixture", state=Path(directory) / "state", store_dir=Path(directory) / "store")
    gh = SimpleNamespace(pr_list=lambda _fields: [raw_pr], fresh_claim_age=lambda _cid: None)
    counters = SimpleNamespace(read=lambda _key: 0, fix_pr_attempts=lambda _pr: 0)
    for name, states, verdicts, expected_review, expected_fix in cases:
        rs = FakeRS(states, [{"verdict": verdict} for verdict in verdicts])
        result = survey_module.survey(cfg, gh, rs, counters, deep=True, tend_scope="author")
        got = ([c.pr for c in result.reviewable.actionable], [c.pr for c in result.needs_fix.actionable])
        expected = (expected_review, expected_fix)
        ok = got == expected
        fails += not ok
        print(f"[{'OK ' if ok else 'XX '}] {name}: review/fix={got!r} want={expected!r}")

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
raise SystemExit(bool(fails))
