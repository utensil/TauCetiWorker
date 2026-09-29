#!/usr/bin/env python3
"""Cross-head repair-loop guard tests."""

import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tauceti_worker.review_state import Meta
from tauceti_worker.survey import Counters, blocking_rubric_key, fix_blocker_streak, fix_disposition


def check(name, value):
    print(f"[{'OK ' if value else 'BAD'}] {name}")
    return value


fails = 0
meta = Meta(
    {
        "head_sha": "a" * 40,
        "states": {"correctness": "blocking_block", "reuse": "green", "scope": "stale"},
    },
    "fresh",
)
fails += not check("blocking key keeps only unresolved rubrics", blocking_rubric_key(meta) == "correctness")
fails += not check(
    "stale review slots do not become blockers",
    blocking_rubric_key(Meta({"head_sha": "a" * 40, "states": {"correctness": "stale"}}, "fresh")) == "",
)

with TemporaryDirectory(prefix="review-blocker-") as raw:
    counters = Counters(SimpleNamespace(state=Path(raw)))
    fails += not check(
        "first blocking head starts a streak",
        fix_blocker_streak(counters, 9536, "correctness", "a" * 40, meta) == 1,
    )
    counters.write_fix_blocker(9536, {"head": "a" * 40, "key": "correctness", "streak": 1})
    meta_b = Meta({"head_sha": "b" * 40, "states": {"correctness": "blocking_block"}}, "fresh")
    fails += not check(
        "same blocker on a new head advances the streak",
        fix_blocker_streak(counters, 9536, "correctness", "b" * 40, meta_b) == 2,
    )
    fails += not check(
        "third unchanged head is suppressed",
        fix_disposition(meta_b, "b" * 40, True, True, 0, blocker_streak=3)[0] == "exhausted",
    )
    fails += not check(
        "a changed blocker resets the streak",
        fix_blocker_streak(counters, 9536, "api-design", "b" * 40, Meta({"head_sha": "b" * 40}, "fresh")) == 1,
    )

sys.exit(1 if fails else 0)
