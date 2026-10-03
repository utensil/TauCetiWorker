#!/usr/bin/env python3
"""Host authoring rounds must restore TauCeti's public Lake cache first."""

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from tauceti_worker import agents

with tempfile.TemporaryDirectory() as raw:
    root = Path(raw)
    scratch = root / "scratch"
    scratch.mkdir()
    calls = []
    original = agents.subprocess.run

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0)

    agents.subprocess.run = fake_run
    try:
        env = agents.prepare_host_lake_cache(root, scratch)
    finally:
        agents.subprocess.run = original

    config = (scratch / "lake-cache.toml").read_text()
    assert 'cache.defaultService = "tauceti-public"' in config
    assert agents.TAUCETI_CACHE_ARTIFACT_URL in config
    assert env == {
        "LAKE_CONFIG": str(scratch / "lake-cache.toml"),
        "LAKE_ARTIFACT_CACHE": "true",
        "LAKE_RESTORE_ARTIFACTS": "true",
    }
    assert calls and calls[0][0] == ["lake", "cache", "get", "--service", "tauceti-public", "--repo", agents.TAUCETI]
    assert calls[0][1]["cwd"] == root
    assert calls[0][1]["timeout"] == 600
