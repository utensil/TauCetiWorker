#!/usr/bin/env python3
"""ledger_blocking must key on the durable per-rubric `states`, not just the latest round's `runs`.

A reply/partial round re-runs only some rubrics, so `runs` can show an approve while another rubric is
still blocking in `states` (the #229 stranding bug). The fix stage's eligibility reads this, and it
must agree with CI's close (which also reads `states`). Dependency-free.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc


class FakeRS:
    """Just enough of ReviewState to call the real ledger_blocking with a crafted meta."""

    def __init__(self, data):
        self._data = data

    def gh_meta(self, pr):
        return tc.Meta(self._data, "fresh")


def blocking(data, head="H"):
    return tc.ReviewState.ledger_blocking(FakeRS(data), 1, head)


HEAD = "H"
cases = [
    (
        "errors and deferred rubrics are not author findings",
        {"head_sha": HEAD, "states": {"naming": "green", "reuse": "error", "scope": "absent"}},
        False,
    ),
    (
        "a real finding remains actionable beside an execution error",
        {"head_sha": HEAD, "states": {"reuse": "error", "naming": "blocking_request"}},
        True,
    ),
    (
        "an early correctness halt remains actionable",
        {"head_sha": HEAD, "states": {"correctness": "blocking_block", "reuse": "absent"}},
        True,
    ),
    # (name, meta, expected)
    (
        "states blocking while runs approve (the #229 bug)",
        {
            "head_sha": HEAD,
            "runs": [{"rubric": "naming", "verdict": "approve"}],
            "states": {"naming": "green", "documentation": "blocking_request"},
        },
        True,
    ),
    (
        "states all green/stale → not blocking",
        {
            "head_sha": HEAD,
            "runs": [{"rubric": "x", "verdict": "request_changes"}],
            "states": {"a": "green", "b": "stale"},
        },
        False,
    ),
    ("head mismatch → not blocking", {"head_sha": "OTHER", "states": {"a": "blocking_request"}}, False),
    (
        "no states map → fall back to runs (blocking)",
        {"head_sha": HEAD, "runs": [{"rubric": "x", "verdict": "request_changes"}]},
        True,
    ),
    (
        "no states map → fall back to runs (clean)",
        {"head_sha": HEAD, "runs": [{"rubric": "x", "verdict": "approve"}]},
        False,
    ),
]

fails = 0
for name, meta, expected in cases:
    got = blocking(meta)
    ok = got == expected
    print(f"[{'OK ' if ok else 'XX '}] {name}: got={got} want={expected}")
    fails += not ok

clean_cases = [
    (
        "a successful partial round cannot hide a retained error",
        {
            "head_sha": HEAD,
            "states": {"reuse": "error", "naming": "green"},
            "runs": [{"rubric": "naming", "verdict": "approve"}],
        },
        "",
    ),
    (
        "durable repaired state supersedes an old error run",
        {"head_sha": HEAD, "states": {"reuse": "green"}, "runs": [{"verdict": "error"}]},
        HEAD,
    ),
    (
        "an intentional blocking halt is reviewed, not an error retry",
        {"head_sha": HEAD, "states": {"correctness": "blocking_block", "reuse": "absent"}},
        HEAD,
    ),
    ("absent-only skeleton is not reviewed", {"head_sha": HEAD, "states": {"reuse": "absent"}}, ""),
    ("legacy successful run still works", {"head_sha": HEAD, "runs": [{"verdict": "approve"}]}, HEAD),
    ("legacy error still needs review", {"head_sha": HEAD, "runs": [{"verdict": "error"}]}, ""),
    ("empty metadata is not reviewed", {}, ""),
]
for name, metadata, expected in clean_cases:
    got = tc.ReviewState.ledger_clean_head(FakeRS(metadata), 1)
    ok = got == expected
    print(f"[{'OK ' if ok else 'XX '}] {name}: got={got!r} want={expected!r}")
    fails += not ok

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
