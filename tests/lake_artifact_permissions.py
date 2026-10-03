#!/usr/bin/env python3
"""Restored Lake products must be replaceable by the next build."""

import stat
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from tauceti_worker.agents import make_lake_build_outputs_writable


def check(name, condition):
    print(f"[{'OK ' if condition else 'BAD'}] {name}")
    return condition


with tempfile.TemporaryDirectory() as raw:
    root = Path(raw)
    build = root / ".lake" / "build" / "lib" / "lean"
    build.mkdir(parents=True)
    restored = build / "Example.ilean"
    restored.write_text("restored")
    restored.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    cache = root / "cache.ilean"
    cache.write_text("cached")
    cache.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    shared = build / "Shared.ilean"
    shared.hardlink_to(cache)
    writable = build / "AlreadyWritable.olean"
    writable.write_text("kept")
    link = build / "link.ilean"
    link.symlink_to(restored)

    changed = make_lake_build_outputs_writable(root)
    ok = [
        check("read-only restored output is repaired", bool(restored.stat().st_mode & stat.S_IWUSR)),
        check("read-only hardlink is detached and repaired", bool(shared.stat().st_mode & stat.S_IWUSR)),
        check("cache hardlink remains read-only", not bool(cache.stat().st_mode & stat.S_IWUSR)),
        check("cache hardlink is detached before repair", shared.stat().st_ino != cache.stat().st_ino),
        check("both read-only outputs are counted", changed == 2),
        check("existing writable output is unchanged", writable.read_text() == "kept"),
        check("symlink is not followed", link.is_symlink()),
    ]
    raise SystemExit(0 if all(ok) else 1)
