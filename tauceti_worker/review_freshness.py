"""Model-free admission for an exact custom review-engine pin."""

from __future__ import annotations

import json
import os
import re
import subprocess
from urllib.parse import quote

from .config import NoProgress
from .constants import REVIEW
from .github import gh_run

SHA_RE = re.compile(r"[0-9a-f]{40}")
BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")


def _read(route: str) -> dict:
    try:
        result = gh_run(["gh", "api", route], max_wait=0)
        payload = json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        payload = None
    if not isinstance(payload, dict):
        raise NoProgress("review source freshness could not be verified; no review launched")
    return payload


def _head(repo: str, branch: str) -> str:
    head = _read(f"repos/{repo}/commits/{quote(branch, safe='')}").get("sha")
    if not isinstance(head, str) or not SHA_RE.fullmatch(head):
        raise NoProgress("review source freshness returned an invalid branch head; no review launched")
    return head


def verify_review_source(repo: str, ref: str) -> None:
    """Refuse custom-fork reviews unless the pin is current and contains upstream main."""
    if repo == REVIEW:
        return
    branch = os.environ.get("TAUCETI_REVIEW_ENGINE_BRANCH", "dev")
    if not BRANCH_RE.fullmatch(branch) or ".." in branch or "//" in branch:
        raise NoProgress("invalid review engine tracking branch; no review launched")
    if os.environ.get("TAUCETI_REVIEW_ENGINE_DIR") or os.environ.get("TAUCETI_REVIEW_DIR"):
        raise NoProgress("custom review source cannot use a local checkout override; no review launched")
    upstream = _head(REVIEW, "main")
    current = _head(repo, branch)
    if ref != current:
        raise NoProgress(
            f"review engine pin {ref[:12]} is not {repo}:{branch} at {current[:12]}; "
            "refresh the managed pin; no review launched"
        )
    comparison = _read(f"repos/{repo}/compare/{upstream}...{current}")
    merge_base = comparison.get("merge_base_commit")
    if (
        comparison.get("status") not in ("ahead", "identical")
        or type(comparison.get("behind_by")) is not int
        or comparison["behind_by"] != 0
        or not isinstance(merge_base, dict)
        or merge_base.get("sha") != upstream
    ):
        raise NoProgress(
            f"review fork {repo}:{branch} does not contain upstream main {upstream[:12]}; "
            "sync and verify the fork first; no review launched"
        )
    if _head(REVIEW, "main") != upstream or _head(repo, branch) != current:
        raise NoProgress("review source changed during freshness verification; no review launched")
