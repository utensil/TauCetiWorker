#!/usr/bin/env python3
"""A real managed child survives failed status writes and still stops on request."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from tauceti_worker import worker_manager as wm


def wait_for(predicate):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.05)
    raise AssertionError("condition did not become true")


with tempfile.TemporaryDirectory(prefix="tc-pressure-", dir="/tmp") as raw:
    root = Path(raw)
    state, runtime = root / "state", root / "run"
    fault, attempts, pid_file = root / "fault", root / "attempts", root / "child"
    fault.touch()
    harness = root / "runner.py"
    harness.write_text("""import argparse, errno, sys
from pathlib import Path
from tauceti_worker import worker_manager as wm
root = Path(sys.argv[1])
original = wm.update_status

def injected(path, **changes):
    if "managed" not in changes and (root / "fault").exists():
        with (root / "attempts").open("a") as out:
            out.write("failed\\n")
        raise OSError(errno.ENFILE, "injected host resource pressure")
    return original(path, **changes)

wm.update_status = injected
spec = wm.WorkerSpec(id="pressure", restart="never")
sys.exit(wm.cmd_managed_runner(argparse.Namespace(spec=wm._encode_spec(spec),
    state_dir=str(root / "state"), runtime_dir=str(root / "run"))))
""")
    env = dict(os.environ, PYTHONPATH=str(REPO))
    env["TAUCETI_MANAGER_TEST_COMMAND"] = shlex.join(
        [
            sys.executable,
            "-c",
            "import os,pathlib,sys; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
            "os.read(int(os.environ['TAUCETI_PARENT_PIPE_FD']), 1)",
            str(pid_file),
        ]
    )
    with (root / "runner.log").open("w") as output:
        runner = subprocess.Popen(
            [sys.executable, str(harness), str(root)], env=env, stdout=output, stderr=subprocess.STDOUT
        )
        child_pid = None
        try:
            wait_for(lambda: pid_file.exists() and pid_file.read_text())
            child_pid = int(pid_file.read_text())
            wait_for(lambda: attempts.exists() and len(attempts.read_text().splitlines()) >= 3)
            assert runner.poll() is None, "status failure terminated the wrapper"
            os.kill(child_pid, 0)
            before = json.loads((state / "pressure.json").read_text())["heartbeat_at"]
            fault.unlink()

            def heartbeat_recovered():
                snapshot = wm.read_json(state / "pressure.json")
                return snapshot if snapshot.get("heartbeat_at", 0) > before else None

            recovered = wait_for(heartbeat_recovered)
            assert recovered["child_pid"] == child_pid
            assert recovered["process_group"] == child_pid
            fault.touch()  # Even terminal-status failure must not prevent requested shutdown.
            assert wm._stop_runner("pressure", runtime)
            assert runner.wait(timeout=15) == 0
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                pass
            else:
                raise AssertionError("stop left the child alive")
            log = (root / "runner.log").read_text()
            assert "status write unavailable" in log and "status writes recovered" in log
        finally:
            if runner.poll() is None:
                runner.terminate()
                runner.wait(timeout=15)
    print("worker manager resource pressure: OK")
