#!/usr/bin/env python3
"""Review allowlist semantics and CLI validation without GitHub or model access."""

import argparse
import importlib
import os
import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

survey_module = importlib.import_module("tauceti_worker.survey")

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"[{'OK ' if ok else 'XX '}] {name}: got {got!r} want {want!r}")


def pr(number, *labels, focuses=(), author="someone"):
    return tc.PRInfo(
        number=number,
        head_oid=f"head{number}",
        head_ref=f"branch-{number}",
        head_owner="someone",
        head_repo="TauCeti",
        is_draft=False,
        mergeable="MERGEABLE",
        author=author,
        build_success=True,
        build_failed=False,
        labels=tuple(labels),
        target_focuses=tuple(focuses),
    )


def scoped(roadmaps=(), prs=(), authors=()):
    sv = tc.Survey(worker_id="test")
    sv.open_prs = [
        pr(1, "roadmap/RepresentationTheory"),
        pr(2, "roadmap/ReductiveGroups"),
        pr(3, "roadmap/Unknown", focuses=("RepresentationTheory",)),
        pr(4, focuses=("RepresentationTheory",)),
        pr(5, "roadmap/none", author="Contributor-A"),
        pr(6, "roadmap/RepresentationTheory", "roadmap/ReductiveGroups"),
        pr(7, "roadmap/unknown", focuses=("RepresentationTheory",)),
    ]
    sv.reviewable.actionable = [tc.Candidate(item.number, item.head_oid) for item in sv.open_prs]
    sv.needs_fix.actionable = [tc.Candidate(99, "fix-head")]
    tc.scope_review_candidates(sv, list(roadmaps), list(prs), list(authors))
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
check(
    "author scope matches case-insensitively",
    [c.pr for c in scoped(authors=["contributor-a"]).reviewable.actionable],
    [5],
)
check(
    "roadmap, PR, and author filters form one union",
    [c.pr for c in scoped(["RepresentationTheory"], [2], ["contributor-a"]).reviewable.actionable],
    [1, 2, 4, 5, 6],
)
sv = scoped(["RepresentationTheory"], [2])
check("excluded candidates are observable", [c.pr for c in sv.review_scope_excluded], [3, 5, 7])
check("another work stage is untouched", [c.pr for c in sv.needs_fix.actionable], [99])

poison_names = ("TAUCETI_REVIEW_ROADMAPS", "TAUCETI_REVIEW_PRS", "TAUCETI_REVIEW_AUTHORS")
saved = {name: os.environ.get(name) for name in poison_names}
try:
    os.environ["TAUCETI_REVIEW_ROADMAPS"] = "HiddenRoadmap"
    os.environ["TAUCETI_REVIEW_PRS"] = "999"
    os.environ["TAUCETI_REVIEW_AUTHORS"] = "hidden-contributor"
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
            "--review-author",
            "Contributor-A,contributor-b",
            "--review-author",
            "CONTRIBUTOR-A",
        ]
    )
    check("repeatable roadmap flag parses", args.review_roadmap, ["RepresentationTheory,ReductiveGroups"])
    check("repeatable PR flag parses", args.review_pr, ["3809", "3871,3827"])
    check("repeatable author flag parses", args.review_author, ["Contributor-A,contributor-b", "CONTRIBUTOR-A"])
    check(
        "CLI scope is normalized without Worker state",
        tc.parse_review_scope(args),
        (["ReductiveGroups", "RepresentationTheory"], [3809, 3827, 3871], ["contributor-a", "contributor-b"]),
    )
    check("ambient roadmap value is ignored and untouched", os.environ["TAUCETI_REVIEW_ROADMAPS"], "HiddenRoadmap")
    check("ambient PR value is ignored and untouched", os.environ["TAUCETI_REVIEW_PRS"], "999")
    check("ambient author value is ignored and untouched", os.environ["TAUCETI_REVIEW_AUTHORS"], "hidden-contributor")
    check(
        "no CLI scope stays unscoped despite ambient poison",
        tc.parse_review_scope(SimpleNamespace(review_roadmap=None, review_pr=None, review_author=None)),
        ([], [], []),
    )
    check(
        "loop children receive scope only as explicit argv",
        tc.review_scope_tail(
            ["ReductiveGroups", "RepresentationTheory"],
            [3809, 3827, 3871],
            ["contributor-a", "contributor-b"],
        ),
        [
            "--review-roadmap",
            "ReductiveGroups,RepresentationTheory",
            "--review-pr",
            "3809,3827,3871",
            "--review-author",
            "contributor-a,contributor-b",
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

    try:
        tc.parse_review_scope(SimpleNamespace(review_author=["bad--login"]))
    except tc.Die:
        malformed_author_rejected = True
    else:
        malformed_author_rejected = False
    check("malformed author CLI fails loudly", malformed_author_rejected, True)

    for attr, flag in (
        ("review_roadmap", "--review-roadmap"),
        ("review_pr", "--review-pr"),
        ("review_author", "--review-author"),
    ):
        values = {"review_roadmap": None, "review_pr": None, "review_author": None}
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


def raw_pr(number, *, state="OPEN", labels=(), body="", green=True, author="someone"):
    return {
        "number": number,
        "title": f"PR {number}",
        "body": body,
        "headRefOid": f"head{number}",
        "headRefName": f"branch-{number}",
        "headRepositoryOwner": {"login": "someone"},
        "headRepository": {"name": "TauCeti"},
        "isDraft": False,
        "statusCheckRollup": ([{"context": "build", "state": "SUCCESS"}] if green else []),
        "author": {"login": author, "is_bot": False},
        "mergeable": "MERGEABLE",
        "labels": [{"name": label} for label in labels],
        "state": state,
    }


class FakeCounters:
    def read(self, _name):
        return 0


class FakeGH:
    def __init__(self, *, index=(), views=None, full=()):
        self.index = list(index)
        self.views = dict(views or {})
        self.full = list(full)
        self.list_calls = []
        self.view_calls = []

    def pr_list(self, fields):
        self.list_calls.append(tuple(fields))
        if tuple(fields) in (
            tc.PR_SCOPE_INDEX_FIELDS,
            tc.PR_AUTHOR_SCOPE_INDEX_FIELDS,
            tc.PR_SCOPE_UNION_INDEX_FIELDS,
        ):
            return self.index
        return self.full

    def pr_view_required(self, number, fields):
        self.view_calls.append((number, tuple(fields)))
        value = self.views[number]
        if isinstance(value, Exception):
            raise value
        return value


old_me = survey_module.me
survey_module.me = lambda: "me"
try:
    gh = FakeGH(views={2: raw_pr(2), 3: raw_pr(3, state="MERGED")})
    sv = survey_module.survey(
        SimpleNamespace(wid="test"),
        gh,
        None,
        FakeCounters(),
        deep=False,
        review_scope_prs=[3, 2],
        scoped_review_only=True,
    )
    check("explicit-only scope does not enumerate the repository", gh.list_calls, [])
    check("explicit-only scope views deterministic exact numbers", [n for n, _ in gh.view_calls], [2, 3])
    check("explicit-only scope keeps only open PRs", [p.number for p in sv.open_prs], [2])
    check("explicit-only strategy is observable", sv.review_query_strategy, "explicit-pr")

    marker = '<!--tauceti-target:v1 {"focus":"RepresentationTheory"}-->'
    index = [
        raw_pr(1, labels=("roadmap/RepresentationTheory",)),
        raw_pr(2, body=marker),
        raw_pr(3, labels=("roadmap/Unknown",), body=marker),
        raw_pr(4, labels=("roadmap/ReductiveGroups",)),
    ]
    views = {1: index[0], 2: index[1], 5: raw_pr(5)}
    gh = FakeGH(index=index, views=views)
    sv = survey_module.survey(
        SimpleNamespace(wid="test"),
        gh,
        None,
        FakeCounters(),
        deep=False,
        review_scope_roadmaps=["RepresentationTheory"],
        review_scope_prs=[5],
        scoped_review_only=True,
    )
    check("roadmap scope uses only the lightweight index", gh.list_calls, [tc.PR_SCOPE_INDEX_FIELDS])
    check("roadmap union hydrates only matches and explicit PRs", [n for n, _ in gh.view_calls], [1, 2, 5])
    check("roadmap union candidate set", [c.pr for c in sv.reviewable.actionable], [1, 2, 5])
    check("roadmap union strategy is observable", sv.review_query_strategy, "scope-union")

    index = [raw_pr(6, author="Contributor-A"), raw_pr(7, author="someone-else")]
    gh = FakeGH(index=index, views={6: index[0]})
    sv = survey_module.survey(
        SimpleNamespace(wid="test"),
        gh,
        None,
        FakeCounters(),
        deep=False,
        review_scope_authors=["contributor-a"],
        scoped_review_only=True,
    )
    check("author scope uses only number and author", gh.list_calls, [tc.PR_AUTHOR_SCOPE_INDEX_FIELDS])
    check("author scope hydrates only matching authors", [n for n, _ in gh.view_calls], [6])
    check("author scope candidate set", [c.pr for c in sv.reviewable.actionable], [6])
    check("author scope strategy is observable", sv.review_query_strategy, "scope-union")

    index = [raw_pr(8, author="Contributor-A")]
    gh = FakeGH(index=index, views={8: raw_pr(8, author="someone-else")})
    sv = survey_module.survey(
        SimpleNamespace(wid="test"),
        gh,
        None,
        FakeCounters(),
        deep=False,
        review_scope_authors=["contributor-a"],
        scoped_review_only=True,
    )
    check("hydrated author is rechecked before selection", [c.pr for c in sv.reviewable.actionable], [])

    index = [
        raw_pr(10, labels=("roadmap/RepresentationTheory",)),
        raw_pr(11, author="Contributor-A"),
        raw_pr(12),
    ]
    gh = FakeGH(index=index, views={10: index[0], 11: index[1], 13: raw_pr(13)})
    sv = survey_module.survey(
        SimpleNamespace(wid="test"),
        gh,
        None,
        FakeCounters(),
        deep=False,
        review_scope_roadmaps=["RepresentationTheory"],
        review_scope_prs=[13],
        review_scope_authors=["contributor-a"],
        scoped_review_only=True,
    )
    check("three-way scope hydrates the exact union", [n for n, _ in gh.view_calls], [10, 11, 13])
    check("three-way scope uses the combined lightweight index", gh.list_calls, [tc.PR_SCOPE_UNION_INDEX_FIELDS])
    check("three-way scope candidate set", [c.pr for c in sv.reviewable.actionable], [10, 11, 13])

    gh = FakeGH(views={9: tc.GitHubError("gh pr view #9 failed: unexpected EOF")})
    sv = survey_module.survey(
        SimpleNamespace(wid="test"),
        gh,
        None,
        FakeCounters(),
        deep=False,
        review_scope_prs=[9],
        scoped_review_only=True,
    )
    check("scoped view failure fails the survey closed", sv.github_failed, True)
    check("scoped view failure preserves diagnostic", sv.errors, ["gh pr view #9 failed: unexpected EOF"])

    full = [raw_pr(1), raw_pr(2)]
    gh = FakeGH(full=full)
    sv = survey_module.survey(
        SimpleNamespace(wid="test"),
        gh,
        None,
        FakeCounters(),
        deep=False,
        review_scope_prs=[2],
        scoped_review_only=False,
    )
    check("non-review-only scope preserves full upstream query", gh.list_calls, [tc.PR_QUERY_FIELDS])
    check("non-review-only scope still filters review selection", [c.pr for c in sv.reviewable.actionable], [2])
finally:
    survey_module.me = old_me

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
raise SystemExit(bool(fails))
