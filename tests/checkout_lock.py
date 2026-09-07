#!/usr/bin/env python3
"""Stale checkout-lock recovery is conservative and recoverable."""

import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker.agents as agents


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        git_dir = Path(tmp) / ".git"
        git_dir.mkdir()
        lock = git_dir / "index.lock"
        lock.write_bytes(b"")
        old = time.time() - 600
        import os

        os.utime(lock, (old, old))

        original = agents.subprocess.run

        def no_holder(cmd, **kwargs):
            if cmd[:3] == ["lsof", "-t", "--"]:
                return subprocess.CompletedProcess(cmd, 1, "", "")
            return original(cmd, **kwargs)

        agents.subprocess.run = no_holder
        try:
            stale = agents._quarantine_stale_index_lock(Path(tmp), min_age_s=300)
        finally:
            agents.subprocess.run = original
        assert stale is not None and stale.name.startswith("index.lock.stale-")
        assert not lock.exists() and stale.exists()

        # A young lock is never touched, even when lsof would report no holder.
        lock.write_bytes(b"")
        untouched = agents._quarantine_stale_index_lock(Path(tmp), min_age_s=300)
        assert untouched is None and lock.exists()
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
