"""Atomic structured status shared by managed worker processes and the local dashboard."""

from __future__ import annotations

import errno
import fcntl
import json
import os
import random
import re
import tempfile
import time
from pathlib import Path

STATUS_ENV = "TAUCETI_RUNTIME_STATUS"
_RICH_STYLE_RE = re.compile(r"\[(?:/?(?:bold|red|yellow|green|dim)(?: [^]]+)?|/)\]")

# ENFILE (the kernel's system-wide file table) and EMFILE (this process's table) are burst
# conditions on a loaded host: the manager's status writes are usually the first syscalls that
# need a *new* descriptor, so they fail while everything else still works. Retry them briefly.
_FD_PRESSURE_ERRNOS = frozenset({errno.ENFILE, errno.EMFILE})
_FD_PRESSURE_RETRIES = 0
_FD_PRESSURE_LAST: float | None = None


def is_fd_pressure(exc: BaseException) -> bool:
    """True when ``exc`` is the kernel momentarily running out of file descriptors."""
    return isinstance(exc, OSError) and exc.errno in _FD_PRESSURE_ERRNOS


def fd_pressure_stats() -> dict:
    """Retry counters, so a report can show real pressure instead of inferring a leak."""
    return {"fd_pressure_retries": _FD_PRESSURE_RETRIES, "fd_pressure_last": _FD_PRESSURE_LAST}


def retry_fd_pressure(op, *, attempts: int = 4, base: float = 0.05, cap: float = 0.4, label: str = ""):
    """Run ``op`` again when the kernel is momentarily out of descriptors.

    Bounded on purpose: ``attempts`` tries with exponential backoff plus jitter, then the original
    error is raised, so a *sustained* exhaustion still surfaces instead of being hidden. Any other
    error (including a real permission or config problem) propagates on the first attempt.
    """
    global _FD_PRESSURE_RETRIES, _FD_PRESSURE_LAST
    last_error: OSError | None = None
    for attempt in range(attempts):
        try:
            return op()
        except OSError as exc:
            if not is_fd_pressure(exc):
                raise
            last_error = exc
            if attempt == attempts - 1:
                break
            _FD_PRESSURE_RETRIES += 1
            _FD_PRESSURE_LAST = time.time()
            time.sleep(min(cap, base * 2**attempt) + random.uniform(0, base))
    assert last_error is not None  # only reachable after a caught OSError
    raise last_error


def read_json(path: Path, *, strict: bool = False) -> dict:
    try:
        value = json.loads(retry_fd_pressure(path.read_text, attempts=3, label="read_json"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, TypeError):
        if strict:
            raise
        return {}
    if strict and not isinstance(value, dict):
        raise ValueError("runtime status must be an object")
    return value if isinstance(value, dict) else {}


def atomic_json(path: Path, value: dict) -> None:
    # The temp file plus the replace are the two places a status write needs a new descriptor, so a
    # burst can fail either one; retry the whole sequence so a half-written temp is never renamed.
    retry_fd_pressure(lambda: _atomic_json_once(path, value), label="atomic_json")


def _atomic_json_once(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(raw)
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(value, out, indent=2, sort_keys=True)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def update_status(path: Path, **changes) -> dict:
    """Merge changes into a status file under a sibling flock and replace it atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    # Opening the sibling lock file is the first new descriptor a status write needs, and it is the
    # operation observed failing with ENFILE during the 2026-09-19/20 bursts; retry just that.
    lock = retry_fd_pressure(lambda: lock_path.open("a+"), label="status_lock")
    with lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # An unreadable existing record is unknown, not an empty record. Otherwise
        # one failed heartbeat read can erase the active round's ownership token.
        value = read_json(path, strict=True)
        value.update(changes)
        value["status_updated_at"] = time.time()
        atomic_json(path, value)
        return value


def report_runtime(state: str | None = None, **changes) -> None:
    """Best-effort status update from a worker or round child.

    Unmanaged workers have no ``TAUCETI_RUNTIME_STATUS`` and pay only the environment lookup.
    Status reporting must never turn useful work into a failed round.
    """
    raw = os.environ.get(STATUS_ENV)
    if not raw:
        return
    if state is not None:
        changes["state"] = state
    if isinstance(changes.get("detail"), str):
        changes["detail"] = _RICH_STYLE_RE.sub("", changes["detail"])
    changes["activity_at"] = time.time()
    try:
        update_status(Path(raw), **changes)
    except Exception:
        pass


def runtime_snapshot() -> dict:
    """Read this process tree's shared runtime status, if it is managed."""
    raw = os.environ.get(STATUS_ENV)
    return read_json(Path(raw)) if raw else {}


def report_failure(reason: str, *, code: int | None = None, log_file: Path | str | None = None) -> None:
    """Publish a concise, structured failure for the supervising loop and human status views."""
    clean = _RICH_STYLE_RE.sub("", str(reason)).strip()
    report_runtime(
        failure_reason=clean[-1000:] or "unknown failure",
        failure_code=code,
        failure_log=str(log_file) if log_file is not None else None,
    )
