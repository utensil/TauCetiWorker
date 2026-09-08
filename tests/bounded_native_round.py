#!/usr/bin/env python3
"""A finite native loop dispatches only its requested rounds and never waits after the last one."""

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import tauceti_worker as tc

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    print(f"[{'OK ' if ok else 'XX '}] {name}: got={got!r} want={want!r}")
    fails += not ok


parser = tc.cli.build_parser()
parsed = parser.parse_args(["work", "--loop", "--max-rounds", "1"])
check("positive --max-rounds parses", parsed.max_rounds, 1)
for bad in ("0", "-1", "not-an-int"):
    try:
        parser.parse_args(["work", "--loop", "--max-rounds", bad])
        rejected = False
    except SystemExit:
        rejected = True
    check(f"--max-rounds rejects {bad}", rejected, True)

saved_pace = tc.cli.resolve_pace
pace_calls = []
tc.cli.resolve_pace = lambda *_args: pace_calls.append(True)
try:
    try:
        tc.cli.main(["work", "--max-rounds", "1"])
        requires_loop = False
    except SystemExit:
        requires_loop = True
finally:
    tc.cli.resolve_pace = saved_pace
check("--max-rounds requires native work --loop", requires_loop, True)
check("invalid use is rejected before setup", pace_calls, [])

saved_budget = tc.loop.github_budget
saved_round = tc.loop.run_round_subprocess
saved_sleep = tc.loop.time.sleep
saved_report = tc.loop.report_runtime
saved_choose = tc.loop.choose_model
saved_run = tc.loop.subprocess.run

dispatches = []
sleeps = []
tc.loop.report_runtime = lambda *_args, **_kwargs: None
tc.loop.time.sleep = lambda seconds: sleeps.append(seconds)
tc.loop.github_budget = lambda: {}


def loop_args(**changes):
    values = dict(max_rounds=1, ignore_quota=False, bubble=False, quota_cmd=None, source=None)
    values.update(changes)
    return SimpleNamespace(**values)


try:
    for child_rc in (0, tc.EX_NOPROGRESS, 1):
        dispatches.clear()
        sleeps.clear()
        tc.loop.run_round_subprocess = lambda tail, rc=child_rc: dispatches.append(tail) or rc
        rc = tc.loop.cmd_loop(loop_args(), SimpleNamespace(wid="test"), only=["review"], agent="deepseek")
        check(f"child rc={child_rc} is returned", rc, child_rc)
        check(f"child rc={child_rc} dispatches exactly once", len(dispatches), 1)
        check(f"child rc={child_rc} returns before final sleep", sleeps, [])

    tc.loop.run_round_subprocess = lambda tail: dispatches.append(tail) or 0
    for budget, label in ((None, "unknown"), ({"core": (0, 0)}, "blocked")):
        dispatches.clear()
        sleeps.clear()
        tc.loop.github_budget = lambda value=budget: value
        rc = tc.loop.cmd_loop(loop_args(), SimpleNamespace(wid="test"), only=["review"], agent="deepseek")
        check(f"{label} GitHub preflight returns no-progress", rc, tc.EX_NOPROGRESS)
        check(f"{label} GitHub preflight dispatches nothing", dispatches, [])
        check(f"{label} GitHub preflight does not wait", sleeps, [])

    tc.loop.github_budget = lambda: {}
    tc.loop.choose_model = lambda *_args, **_kwargs: (
        None,
        {"codex": tc.Provider("codex", False, None, error="unavailable")},
    )
    dispatches.clear()
    sleeps.clear()
    rc = tc.loop.cmd_loop(loop_args(), SimpleNamespace(wid="test"), only=["review"], agent="codex")
    check("blocked provider preflight returns no-progress", rc, tc.EX_NOPROGRESS)
    check("blocked provider preflight dispatches nothing", dispatches, [])
    check("blocked provider preflight does not wait", sleeps, [])

    seen_timeout = []

    def timeout_run(*_args, **kwargs):
        seen_timeout.append(kwargs.get("timeout"))
        raise subprocess.TimeoutExpired("quota", kwargs.get("timeout"))

    tc.loop.choose_model = saved_choose
    tc.loop.subprocess.run = timeout_run
    rc = tc.loop.cmd_loop(
        loop_args(quota_cmd="quota-check"),
        SimpleNamespace(wid="test"),
        only=["review"],
        agent="codex",
    )
    check("finite quota command uses the poll bound", seen_timeout, [tc.loop.POLL])
    check("timed-out quota command returns no-progress", rc, tc.EX_NOPROGRESS)

    tc.loop.subprocess.run = saved_run
    tc.loop.github_budget = lambda: {}
    tc.loop.run_round_subprocess = lambda tail: dispatches.append(tail) or 0
    tc.loop.time.sleep = lambda seconds: (_ for _ in ()).throw(KeyboardInterrupt)
    dispatches.clear()
    rc = tc.loop.cmd_loop(loop_args(max_rounds=None), SimpleNamespace(wid="test"), only=["review"], agent="deepseek")
    check("unbounded loop still settles after a child", rc, 130)
    check("unbounded loop still dispatched before settling", len(dispatches), 1)
finally:
    tc.loop.github_budget = saved_budget
    tc.loop.run_round_subprocess = saved_round
    tc.loop.time.sleep = saved_sleep
    tc.loop.report_runtime = saved_report
    tc.loop.choose_model = saved_choose
    tc.loop.subprocess.run = saved_run

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
raise SystemExit(bool(fails))
