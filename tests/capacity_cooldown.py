"""Regression checks for restart-safe dual-provider capacity back-off."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from tauceti_worker.loop import _capacity_pause, _clear_capacity_pause, _set_capacity_pause


with tempfile.TemporaryDirectory(prefix="capacity-cooldown-") as raw:
    cfg = SimpleNamespace(state=Path(raw))
    _set_capacity_pause(cfg, "provider model capacity unavailable")
    marker = Path(raw) / "provider-capacity-cooldown.json"
    assert marker.is_file(), "capacity failure must survive a worker restart"
    data = json.loads(marker.read_text(encoding="utf-8"))
    assert data["reason"] == "provider model capacity unavailable"
    remaining = _capacity_pause(cfg)
    assert remaining is not None and remaining[0] > 0
    _clear_capacity_pause(cfg)
    assert _capacity_pause(cfg) is None

print("capacity cooldown checks passed")
