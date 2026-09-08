#!/usr/bin/env python3
"""A newly archived error-bearing round is incomplete even when the engine exits zero."""

import json
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tauceti_worker as tc
from tauceti_worker import work_units as wu
from tauceti_worker.review_diagnostics import public_review_failure, read_review_failure, read_review_round

HEAD = "a" * 40


class Counters:
    def __init__(self):
        self.values = {"review-err-42": 2}

    def read(self, key):
        return self.values.get(key, 0)

    def write(self, key, value):
        self.values[key] = value

    def incr(self, key):
        self.write(key, self.read(key) + 1)


def round_record(number=1, head=HEAD, states=None):
    return {
        "round": number,
        "head_sha": head,
        "ts": f"2026-09-08T00:00:0{number}Z",
        "states": states if states is not None else {"reuse": "error", "naming": "green"},
        "ignored_private_field": "must-not-reach-diagnostics",
    }


def exercise(*, before=None, after=None, code=0, sync_code=0, bubble=False, contest=False):
    with tempfile.TemporaryDirectory() as temporary:
        state = Path(temporary)
        store = state / "store"
        store.mkdir()
        ledger = store / "ledger.json"

        def write_record(record):
            ledger.write_text(json.dumps({"prs": {"42": {"rounds": [record]}}}))

        if before is not None:
            write_record(before)

        def run(*args, **kwargs):
            if after is not None:
                write_record(after)
            return code

        busted = []
        released = []
        worker = types.SimpleNamespace(
            cfg=types.SimpleNamespace(state=state, store_dir=store, logdir=state, wid="test"),
            counters=Counters(),
            rs=types.SimpleNamespace(review_rounds=lambda *args: 0, bust=busted.append),
            gh=types.SimpleNamespace(
                add_reaction=lambda reply: True, remove_reaction=lambda reply: released.append(reply) or True
            ),
        )
        candidate = types.SimpleNamespace(
            pr=42,
            head=HEAD,
            contest="naming" if contest else None,
            contest_reply_id=123 if contest else None,
        )
        with (
            patch.object(wu, "run_to_logfile", side_effect=run),
            patch.object(wu, "review_in_bubble", side_effect=run),
            patch.object(wu, "_sync_review_outbox", return_value=sync_code) as sync,
            patch.object(wu, "me", return_value="tester"),
            patch.object(wu, "warn_red"),
            patch.object(wu, "runtime_snapshot", return_value={}),
            patch.object(wu, "report_failure") as reported,
        ):
            outcome = None
            try:
                result = wu.do_review(worker, None, candidate, types.SimpleNamespace(work_model="codex"), bubble)
            except tc.NoProgress as error:
                outcome = str(error)
                result = None
            return {
                "outcome": outcome,
                "result": result,
                "errors": worker.counters.read("review-err-42"),
                "contests": worker.counters.read("review-contest-42"),
                "syncs": sync.call_count,
                "failure": read_review_failure(state, 42),
                "busted": busted,
                "released": released,
                "reported": reported.call_args,
            }


for bubble in (False, True):
    observed = exercise(after=round_record(), bubble=bubble, contest=True)
    assert observed["outcome"] and "incomplete" in observed["outcome"], observed
    assert observed["errors"] == 2, observed
    assert observed["contests"] == 0, observed
    assert observed["syncs"] == 1 and observed["busted"] == [42], observed
    assert observed["released"] == [123], observed
    assert observed["reported"], observed
    assert "review-incomplete" in public_review_failure(observed["failure"]), observed
    assert "must-not-reach-diagnostics" not in json.dumps(observed["failure"]), observed

for record in (
    round_record(states={"reuse": "green"}),
    round_record(states={"correctness": "blocking_block", "reuse": "absent"}),
    round_record(head="b" * 40),
):
    observed = exercise(after=record)
    assert observed["result"] == 0 and observed["errors"] == 0, observed

unchanged = round_record()
observed = exercise(before=unchanged, after=unchanged)
assert observed["result"] == 0 and not observed["failure"], observed
observed = exercise(before=unchanged, after=round_record(2))
assert observed["outcome"] and observed["errors"] == 2, observed
observed = exercise(before=round_record(2), after=unchanged)
assert observed["result"] == 0 and not observed["failure"], observed
observed = exercise(after=round_record(), sync_code=1)
assert "publish failed" in observed["outcome"] and observed["failure"], observed
assert observed["errors"] == 2, observed
observed = exercise(after=round_record(), code=1)
assert observed["result"] == 1 and observed["errors"] == 3, observed
observed = exercise()
assert observed["result"] == 0, observed

with tempfile.TemporaryDirectory() as temporary:
    store = Path(temporary)
    assert read_review_round(store, 42) is None
    for raw in ("not-json", "null", "[]", '{"prs": []}', '{"prs": {"42": {"rounds": [null]}}}'):
        (store / "ledger.json").write_text(raw)
        assert read_review_round(store, 42) is None
    for field, value in (("round", True), ("round", 0), ("states", []), ("ts", None), ("head_sha", None)):
        record = round_record()
        record[field] = value
        (store / "ledger.json").write_text(json.dumps({"prs": {"42": {"rounds": [record]}}}))
        assert read_review_round(store, 42) is None

print("PASS: incomplete rounds, partial results, sync, claims, provenance and legacy paths")
