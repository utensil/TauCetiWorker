"""Durable, local ownership of PRs created by one maintenance Worker instance.

The file is intentionally not a general database: it contains one line of positive PR numbers,
sorted and comma-separated.  A missing or malformed file is *not* interpreted as "all PRs".  That
fail-closed rule is the boundary that keeps an ``owned`` Worker from tending another instance's PRs.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from .config import Config

_NUMBERS = re.compile(r"[1-9][0-9]*(?:,[1-9][0-9]*)*")


class OwnedPRStateError(ValueError):
    """The local ownership record is unreadable or cannot be updated."""


class OwnedPRs:
    """Read and atomically update the PR-number set for one Worker ID."""

    def __init__(self, cfg: Config, *, root: Path | None = None):
        configured = os.environ.get("TAUCETI_INSTANCES_DIR")
        base = root or (Path(configured).expanduser() if configured else cfg.data_home / ".tauceti" / "instances")
        self.path = base / cfg.wid / "owned-prs"

    def read(self) -> set[int] | None:
        """Return the set, or ``None`` for missing/corrupt state (the fail-closed result)."""
        try:
            text = self.path.read_text()
        except FileNotFoundError:
            return None
        except OSError:
            return None
        # Existing state files conventionally end in one newline; permit that single terminator but
        # reject embedded whitespace, extra lines, signs, zero, and empty comma fields.
        if text.endswith("\n"):
            text = text[:-1]
        if "\n" in text or "\r" in text:
            return None
        if not text:
            return set()
        if not _NUMBERS.fullmatch(text):
            return None
        values = [int(item) for item in text.split(",")]
        if len(values) != len(set(values)):
            return None
        return set(values)

    def add(self, number: int) -> set[int]:
        """Record a newly-created PR and return the resulting set."""
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            raise OwnedPRStateError(f"invalid PR number: {number!r}")
        current = self.read()
        # A missing record is the normal first-PR case. Corrupt existing state is not silently
        # overwritten: doing so could discard ownership and make future tending ambiguous.
        if current is None and self.path.exists():
            raise OwnedPRStateError(f"cannot parse ownership record {self.path}")
        values = set(current or ())
        values.add(number)
        self._write(values)
        return values

    def _write(self, values: set[int]) -> None:
        if any(isinstance(n, bool) or not isinstance(n, int) or n <= 0 for n in values):
            raise OwnedPRStateError("ownership record contains an invalid PR number")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = ",".join(str(n) for n in sorted(values)) + "\n"
        fd, raw = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        temp = Path(raw)
        try:
            with os.fdopen(fd, "w", encoding="ascii") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, self.path)
            try:
                dirfd = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(dirfd)
                finally:
                    os.close(dirfd)
            except OSError:
                # The replace is still atomic; directory fsync is best effort on filesystems that do
                # not expose a syncable directory handle (notably some macOS volumes).
                pass
        except OSError as exc:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
            raise OwnedPRStateError(f"cannot write ownership record {self.path}: {exc}") from exc
