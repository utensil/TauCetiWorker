#!/usr/bin/env python3
"""Review allowlist semantics and CLI validation without GitHub or model access."""

import argparse
import os
import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"[{'OK ' if ok else 'XX '}] {name}: got {got!r} want {want!r}")


def pr(number, *labels, focuses=()):
    return tc.PRInfo(
        number=number,
        head_oid=f"head{number}",
        head_ref=f"branch-{number}",
        head_owner="someone",
        head_repo="TauCeti",
        is_draft=False,
        mergeable="MERGEABLE",
        author="someone",
        build_success=True,
        build_failed=False,
        labels=tuple(labels),
        target_focuses=tuple(focuses),
    )


def scoped(roadmaps=(), prs=()):
    sv = tc.Survey(worker_id="test")
    sv.open_prs = [
        pr(1, "roadmap/RepresentationTheory"),
        pr(2, "roadmap/ReductiveGroups"),
        pr(3, "roadmap/Unknown", focuses=("RepresentationTheory",)),
        pr(4, focuses=("RepresentationTheory",)),
        pr(5, "roadmap/none"),
        pr(6, "roadmap/RepresentationTheory", "roadmap/ReductiveGroups"),
        pr(7, "roadmap/unknown", focuses=("RepresentationTheory",)),
    ]
    sv.reviewable.actionable = [tc.Candidate(item.number, item.head_oid) for item in sv.open_prs]
    sv.needs_fix.actionable = [tc.Candidate(99, "fix-head")]
    tc.scope_review_candidates(sv, list(roadmaps), list(prs))
    return sv


check("no scope preserves upstream queue", [c.pr for c in scoped().reviewable.actionable], [1, 2, 3, 4, 5, 6, 7])
check(
    "roadmap scope admits labelled and marker-fallback PRs",
    [c.pr for c in scoped(["representationtheory"]).reviewable.actionable],
    [1, 4, 6],
)
check(
    "roadmap/Unknown fails closed despite marker",
    [c.pr for c in scoped(["RepresentationTheory"]).reviewable.actionable],
    [1, 4, 6],
)
check(
    "roadmap/unknown fails closed case-insensitively",
    [c.pr for c in scoped(["RepresentationTheory"]).reviewable.actionable],
    [1, 4, 6],
)
check("explicit PR list admits exact numbers", [c.pr for c in scoped(prs=[2, 3, 5]).reviewable.actionable], [2, 3, 5])
check(
    "roadmap and PR filters form a union",
    [c.pr for c in scoped(["RepresentationTheory"], [2, 5]).reviewable.actionable],
    [1, 2, 4, 5, 6],
)
sv = scoped(["RepresentationTheory"], [2])
check("excluded candidates are observable", [c.pr for c in sv.review_scope_excluded], [3, 5, 7])
check("another work stage is untouched", [c.pr for c in sv.needs_fix.actionable], [99])

poison_names = ("TAUCETI_REVIEW_ROADMAPS", "TAUCETI_REVIEW_PRS")
saved = {name: os.environ.get(name) for name in poison_names}
try:
    os.environ["TAUCETI_REVIEW_ROADMAPS"] = "HiddenRoadmap"
    os.environ["TAUCETI_REVIEW_PRS"] = "999"
    parser = argparse.ArgumentParser()
    tc.add_review_scope_flags(parser)
    args = parser.parse_args(
        [
            "--review-roadmap",
            "RepresentationTheory,ReductiveGroups",
            "--review-pr",
            "3809",
            "--review-pr",
            "3871,3827",
        ]
    )
    check("repeatable roadmap flag parses", args.review_roadmap, ["RepresentationTheory,ReductiveGroups"])
    check("repeatable PR flag parses", args.review_pr, ["3809", "3871,3827"])
    check(
        "CLI scope is normalized without Worker state",
        tc.parse_review_scope(args),
        (["ReductiveGroups", "RepresentationTheory"], [3809, 3827, 3871]),
    )
    check("ambient roadmap value is ignored and untouched", os.environ["TAUCETI_REVIEW_ROADMAPS"], "HiddenRoadmap")
    check("ambient PR value is ignored and untouched", os.environ["TAUCETI_REVIEW_PRS"], "999")
    check(
        "no CLI scope stays unscoped despite ambient poison",
        tc.parse_review_scope(SimpleNamespace(review_roadmap=None, review_pr=None)),
        ([], []),
    )
    check(
        "loop children receive scope only as explicit argv",
        tc.review_scope_tail(["ReductiveGroups", "RepresentationTheory"], [3809, 3827, 3871]),
        [
            "--review-roadmap",
            "ReductiveGroups,RepresentationTheory",
            "--review-pr",
            "3809,3827,3871",
        ],
    )

    try:
        tc.parse_review_scope(SimpleNamespace(review_roadmap=None, review_pr=["9,nope"]))
    except tc.Die:
        malformed_pr_rejected = True
    else:
        malformed_pr_rejected = False
    check("malformed PR CLI fails loudly", malformed_pr_rejected, True)

    try:
        tc.parse_review_scope(SimpleNamespace(review_roadmap=["Representation Theory"], review_pr=None))
    except tc.Die:
        malformed_area_rejected = True
    else:
        malformed_area_rejected = False
    check("malformed roadmap CLI fails loudly", malformed_area_rejected, True)

    for attr, flag in (("review_roadmap", "--review-roadmap"), ("review_pr", "--review-pr")):
        values = {"review_roadmap": None, "review_pr": None}
        values[attr] = [" , "]
        try:
            tc.parse_review_scope(SimpleNamespace(**values))
        except tc.Die:
            empty_rejected = True
        else:
            empty_rejected = False
        check(f"empty {flag} fails closed", empty_rejected, True)
finally:
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
raise SystemExit(bool(fails))
