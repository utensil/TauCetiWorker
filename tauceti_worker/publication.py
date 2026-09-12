"""Bind host publication to the destination and optional check selected by the worker.

This is an accidental-misconfiguration guard for cooperating agents, not a sandbox
against an agent that deliberately rewrites its Git configuration or bypasses wrappers.
Project validation belongs in the operator-supplied executable, outside this module.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from .config import Die


def bind_publication(cwd: Path) -> None:
    remote = os.environ.get("TAUCETI_PUSH_REMOTE", "")
    if not remote:
        return  # e.g. read-only review or progress reporting
    checker = os.environ.get("TAUCETI_PRE_PUSH_CHECK", "")
    if checker and (not Path(checker).is_absolute() or not os.access(checker, os.X_OK)):
        raise Die("configured pre-push check must be an absolute executable path")
    location = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-path", "tauceti-publication"],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if location.returncode or not location.stdout.strip():
        raise Die("could not locate checkout publication policy; agent not launched")
    policy = Path(location.stdout.strip())
    # A worktree's git-dir is private to it; --local config would instead affect
    # every worktree sharing the common repository. Replace the whole policy.
    fd, name = tempfile.mkstemp(prefix=".publication-", dir=policy.parent)
    os.close(fd)
    pending = Path(name)
    try:
        values = {
            "remote": remote,
            "ref": os.environ.get("TAUCETI_PUSH_REF", ""),
            "expect": os.environ.get("TAUCETI_PUSH_EXPECT", ""),
            "check": checker,
        }
        for key, value in values.items():
            result = subprocess.run(
                ["git", "config", "--file", str(pending), f"publication.{key}", value],
                cwd=cwd,
                capture_output=True,
                timeout=30,
            )
            if result.returncode:
                raise Die("could not bind publication policy in the selected checkout; agent not launched")
        os.replace(pending, policy)
    finally:
        pending.unlink(missing_ok=True)
