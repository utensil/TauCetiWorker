#!/usr/bin/env python3
"""Local ownership record and owned-only survey boundary tests."""

import importlib
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tauceti_worker.owned_prs import OwnedPRs

survey_mod = importlib.import_module("tauceti_worker.survey")
work_units_mod = importlib.import_module("tauceti_worker.work_units")
manager_mod = importlib.import_module("tauceti_worker.worker_manager")


fails = 0


def check(name, value):
    global fails
    if not value:
        fails += 1
    print(f"[{'OK ' if value else 'BAD'}] {name}")


with TemporaryDirectory(prefix="owned-prs-") as raw:
    root = Path(raw)
    cfg = SimpleNamespace(wid="tcwork", data_home=root / "home")
    owned = OwnedPRs(cfg)
    check("missing record fails closed", owned.read() is None)
    check("first add creates one-line record", owned.add(12) == {12})
    check("record is sorted comma-separated", owned.path.read_text() == "12\n")
    check("duplicate add is idempotent", owned.add(12) == {12} and owned.path.read_text() == "12\n")
    check("second number is sorted", owned.add(3) == {3, 12} and owned.path.read_text() == "3,12\n")
    owned.path.write_text("3,3\n")
    check("duplicate on disk fails closed", owned.read() is None)
    owned.path.write_text("3, 12\n")
    check("whitespace corruption fails closed", owned.read() is None)
    owned.path.write_text("\n")
    check("empty line is a valid empty set", owned.read() == set())

    def pr(number):
        return {
            "number": number,
            "title": f"PR {number}",
            "body": "",
            "headRefOid": f"head-{number}",
            "headRefName": f"roadmap/item-{number}",
            "headRepositoryOwner": {"login": "alice"},
            "headRepository": {"name": "TauCeti"},
            "isDraft": False,
            "statusCheckRollup": [],
            "author": {"login": "alice"},
            "mergeable": "CONFLICTING" if number == 3 else "MERGEABLE",
            "labels": [],
        }

    owned.path.write_text("3\n")
    survey_mod.me = lambda: "alice"
    gh = SimpleNamespace(pr_list=lambda fields: [pr(3), pr(4)])
    counters = SimpleNamespace(read=lambda name: 0)
    sv = survey_mod.survey(cfg, gh, None, counters, deep=False, tend_scope="owned")
    check("owned survey tends only recorded PRs", [c.pr for c in sv.rebaseable.actionable] == [3])
    check("owned survey backpressure counts only owned PRs", sv.n_mine_open == 1)
    sv_cap = survey_mod.survey(cfg, gh, None, counters, deep=False, tend_scope="owned", max_open_prs=1)
    check("owned survey uses its worker-local cap", sv_cap.roadmap_backpressure)
    # A stale blocking scoreboard must not dispatch the review fixer while the current head's
    # authoritative build is still pending.  The fake review state raises if consulted, proving the
    # build gate runs before the stale metadata path.
    pending_raw = [
        {
            **pr(3),
            "statusCheckRollup": [],
        }
    ]
    old_due = survey_mod.progress_due
    old_pr_list = gh.pr_list
    survey_mod.progress_due = lambda *_args, **_kwargs: (False, "")
    gh.pr_list = lambda fields: pending_raw
    exploding_rs = SimpleNamespace(
        gh_meta=lambda _pr: (_ for _ in ()).throw(AssertionError("stale review metadata was consulted")),
        ledger_blocking=lambda *_args: (_ for _ in ()).throw(AssertionError("stale blocking state was consulted")),
    )
    try:
        sv_pending = survey_mod.survey(cfg, gh, exploding_rs, counters, deep=True, tend_scope="owned")
        check("pending build does not dispatch stale review fix", not sv_pending.needs_fix.actionable)
        check(
            "pending build is explained in fix diagnostics",
            any("authoritative build is not green" in why for _pr, why in sv_pending.fix_waiting),
        )
    finally:
        survey_mod.progress_due = old_due
        gh.pr_list = old_pr_list
    try:
        survey_mod.survey(cfg, gh, None, counters, deep=False, tend_scope="author", retry_exhausted_fixes=True)
    except ValueError as exc:
        check("exhausted-fix recovery rejects unscoped maintenance", "owned" in str(exc))
    else:
        check("exhausted-fix recovery rejects unscoped maintenance", False)

    # The same owned-only override must reopen exhausted red-CI work, including the lifetime PR cap.
    red = {**pr(3), "statusCheckRollup": [{"context": "build", "state": "FAILURE"}]}
    gh.pr_list = lambda fields: [red]
    exhausted = SimpleNamespace(
        read=lambda name: (
            survey_mod.MAX_CI_ATTEMPTS
            if name.startswith("ci-") and name != "ci-pr-3"
            else survey_mod.MAX_CI_PR_ATTEMPTS
            if name == "ci-pr-3"
            else 0
        )
    )
    sv_red = survey_mod.survey(
        cfg,
        gh,
        None,
        exhausted,
        deep=False,
        tend_scope="owned",
        retry_exhausted_fixes=True,
    )
    check("owned recovery override reopens exhausted fix-ci", [c.pr for c in sv_red.red_ci.actionable] == [3])
    check("owned recovery override marks fix-ci budget unlimited", sv_red.red_ci.actionable[0].budget == 0)

    receipt = root / "receipt"
    receipt.write_text("21\n")
    work_units_mod._register_owned_receipt(cfg, receipt)
    check("creation receipt is persisted as owned PR", OwnedPRs(cfg).read() == {3, 21})
    check("consumed receipt is removed", not receipt.exists())

spec = manager_mod.WorkerSpec.from_dict({"id": "tcwork", "tend_scope": "owned"}, 0)
check("manager persists owned scope", spec.as_dict().get("tend_scope") == "owned")
check("manager forwards owned scope", spec.work_argv()[-2:] == ["--tend-scope", "owned"])
scoped_cap = manager_mod.WorkerSpec.from_dict({"id": "tcwork", "tend_scope": "owned", "max_open_prs": 4}, 0)
check("manager persists per-worker cap", scoped_cap.as_dict().get("max_open_prs") == 4)
check("manager forwards per-worker cap", "--max-open-prs" in scoped_cap.work_argv())
check("other workers retain default cap", manager_mod.WorkerSpec(id="other").max_open_prs == 8)

print(f"owned_prs: {fails} failure(s)")
raise SystemExit(1 if fails else 0)
