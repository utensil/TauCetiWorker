"""Opt-in round supervision using round.lock and the existing runtime status channel.

Only structural process identities and activity times are persisted. A heartbeat is
not progress. Missing activity permits timeout, never concurrent checkout reuse.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from .config import Config, Die, log
from .paths import self_argv, self_env
from .runtime_status import STATUS_ENV, atomic_json, read_json

TOKEN_ENV = "TAUCETI_ROUND_INSTANCE"
LOCK_ENV = "TAUCETI_ROUND_LOCK_FD"


def processes() -> dict[str, dict]:
    """Portable host snapshot without argv, environment, or other secret-bearing data.

    PID plus kernel-reported start time protects against ordinary PID reuse. Unknown
    snapshots raise; they never certify cleanup. Zombies no longer access a checkout.
    """
    result = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,pgid=,stat=,time=,lstart="],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    found = {}
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 10:
            raise ValueError("unrecognized process snapshot")
        pid, parent, group, state, cpu = fields[:5]
        if "Z" in state:
            continue
        days, sep, clock = cpu.partition("-")
        parts = (clock if sep else days).split(":")
        seconds = sum(float(part) * 60**i for i, part in enumerate(reversed(parts)))
        found[pid] = {
            "pid": int(pid),
            "parent": int(parent),
            "group": int(group),
            "birth": " ".join(fields[5:]),
            "cpu": seconds + (int(days) * 86400 if sep else 0),
        }
    if str(os.getpid()) not in found:
        raise ValueError("incomplete process snapshot")
    return found


def alive(identity: dict, snapshot: dict) -> bool:
    current = snapshot.get(str(identity.get("pid")), {})
    return bool(current) and current.get("birth") == identity.get("birth")


def update_work(path: Path, token: str, change) -> dict:
    """Use the runtime-status lock, rejecting writes from an earlier round."""
    with path.with_suffix(path.suffix + ".lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        status = read_json(path)
        work = status.get("round_work", {})
        if work.get("token") != token:
            raise RuntimeError("round activity identity changed")
        change(work)
        atomic_json(path, status)
        return work


class Activity:
    """Record observed Codex work, including a nested process launched by a wrapper.

    Construct immediately after Popen; feed each raw JSON line before rendering it.
    No-op outside supervised rounds. Duplicates, polling and retry chatter don't
    refresh progress. Unknown event formats receive quiet grace, not fake progress.
    """

    def __init__(self, pid: int):
        self.token = os.environ.get(TOKEN_ENV)
        self.path = Path(os.environ[STATUS_ENV]) if self.token else None
        self.seen: set[str] = set()
        self.pid = str(pid)
        if self.path:
            identity = processes().get(self.pid)
            if identity is None:
                # The subprocess may already have completed. It needs no renewal.
                self.path = None
                return
            now = time.monotonic()
            update_work(
                self.path,
                self.token,
                lambda w: w["agents"].update(
                    {
                        self.pid: {**identity, "started": now, "progress": None, "finished": False},
                    }
                ),
            )

    def observe(self, raw: str) -> None:
        if not self.path:
            return
        try:
            event = json.loads(raw)
        except ValueError:
            return
        if not isinstance(event, dict) or event.get("type") != "item.completed":
            return
        item = event.get("item")
        if not isinstance(item, dict):
            return
        kind = item.get("type")
        if kind == "command_execution":
            if item.get("exit_code") != 0 or item.get("status") not in (None, "completed"):
                return
            command = str(item.get("command", ""))
            # Polling or transport retries can produce forever-changing timestamps.
            if re.search(r"\b(sleep|tail|watch|ps|date|retry|kill|lsof)\b", command):
                return
            payload = [kind, command, item.get("exit_code"), item.get("aggregated_output")]
        elif kind == "file_change":
            if item.get("status") != "completed":
                return
            payload = [kind, item.get("status"), item.get("changes")]
        elif kind == "mcp_tool_call" and not item.get("error") and item.get("status") == "completed":
            if re.search(r"wait|poll|sleep|status", str(item.get("tool", "")), re.I):
                return
            payload = [kind, item.get("server"), item.get("tool"), item.get("arguments"), item.get("result")]
        else:
            return
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        if fingerprint in self.seen or len(self.seen) >= 4096:
            return
        self.seen.add(fingerprint)
        now = time.monotonic()
        update_work(self.path, self.token, lambda w: w["agents"][self.pid].update(progress=now))

    def finish(self) -> None:
        if self.path:
            update_work(self.path, self.token, lambda w: w["agents"][self.pid].update(finished=True))


def decision(work: dict, snapshot: dict, now: float, *, fresh: float, quiet: float) -> str:
    """Three outcomes, independent of lock ownership and cleanup authorization."""
    agents = [a for a in work.get("agents", {}).values() if not a.get("finished") and alive(a, snapshot)]
    if any(a.get("progress") is not None and 0 <= now - a["progress"] <= fresh for a in agents):
        return "progress"
    # A known pending process can be remote inference or an output-buffered build.
    # CPU activity alone is not allowed to reset this bounded quiet allowance.
    if any(0 <= now - (a.get("progress") if a.get("progress") is not None else a["started"]) <= quiet for a in agents):
        return "quiet-grace"
    return "expired"


def descendants(owned: dict, snapshot: dict) -> dict:
    known = {pid: value for pid, value in owned.items() if alive(value, snapshot)}
    while True:
        extra = {pid: value for pid, value in snapshot.items() if str(value["parent"]) in known and pid not in known}
        if not extra:
            return known
        known.update(extra)


def refuse_surviving_work(cfg: Config) -> None:
    """One-shot invocations obey the same previous-round admission check."""
    path = Path(os.environ.get(STATUS_ENV, cfg.state / "runtime.json"))
    previous = read_json(path).get("round_work", {})
    identities = [*previous.get("owned", {}).values(), *previous.get("agents", {}).values()]
    if identities:
        snapshot = processes()
        if any(alive(identity, snapshot) for identity in identities):
            raise Die("previous round still owns live work; refusing checkout reuse")


def supervise(argv: list[str], timeout: float) -> int:
    """Keep the same round lock through child exit and verified owned cleanup."""
    wid = argv[argv.index("--worker-id") + 1] if "--worker-id" in argv else None
    cfg = Config.resolve(wid)
    cfg.state.mkdir(parents=True, exist_ok=True)
    status_path = Path(os.environ.get(STATUS_ENV, cfg.state / "runtime.json"))
    status_path.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    poll = max(0.05, float(os.environ.get("TAUCETI_ROUND_ACTIVITY_POLL", "2")))
    fresh = float(os.environ.get("TAUCETI_ROUND_ACTIVITY_FRESH", "600"))
    quiet = float(os.environ.get("TAUCETI_ROUND_QUIET_GRACE", "1800"))
    extension = max(poll, float(os.environ.get("TAUCETI_ROUND_EXTENSION", "300")))
    term_grace = max(0, float(os.environ.get("TAUCETI_ROUND_TERM_GRACE", "5")))
    if not 0 <= fresh <= quiet:
        raise Die("round activity freshness must be nonnegative and no longer than quiet grace")
    with (cfg.state / "round.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Die(f"another round holds {cfg.state / 'round.lock'}") from None
        refuse_surviving_work(cfg)
        # Reuse the status writer's lock; unrelated manager heartbeat fields survive.
        from .runtime_status import update_status

        update_status(
            status_path, round_work={"token": token, "agents": {}, "owned": {}, "checkout": str(cfg.checkout)}
        )
        env = self_env({**os.environ, STATUS_ENV: str(status_path), TOKEN_ENV: token, LOCK_ENV: str(lock.fileno())})
        child = subprocess.Popen(self_argv("_round", *argv), start_new_session=True, pass_fds=(lock.fileno(),), env=env)
        owned = {}
        deadline = time.monotonic() + timeout
        rc = None

        def sample():
            nonlocal owned
            current = processes()
            if str(child.pid) not in current and child.poll() is None:
                raise ValueError("live round missing from process snapshot")
            if str(child.pid) in current and child.poll() is None:
                owned.setdefault(str(child.pid), current[str(child.pid)])
            # A fast-exiting leader may leave same-group children before the first
            # sample. This is the same owned PGID boundary used by legacy teardown.
            for pid, identity in current.items():
                if identity["group"] == child.pid:
                    owned.setdefault(pid, identity)
            work = read_json(status_path).get("round_work", {})
            if work.get("token") != token:
                raise RuntimeError("round supervision identity changed")
            for pid, identity in work.get("agents", {}).items():
                if alive(identity, current):
                    owned.setdefault(pid, identity)
            owned = descendants(owned, current)
            update_work(status_path, token, lambda w: w.update(owned=owned))
            return work, current

        try:
            while child.poll() is None:
                work, snapshot = sample()
                # A snapshot/status write can outlast a child's final output. Prefer
                # its real exit status to a timeout inferred from finished observers.
                if child.poll() is not None:
                    break
                now = time.monotonic()
                if now >= deadline:
                    reason = decision(work, snapshot, now, fresh=fresh, quiet=quiet)
                    if reason == "expired":
                        rc = 124
                        break
                    log(f"round deadline extended by {extension:g}s: {reason}")
                    deadline = now + extension
                time.sleep(poll)
            if rc is None:
                rc = child.wait()
        finally:
            # Capture children (including their new sessions) BEFORE sending any signal.
            # If observation fails, retain the lock and retry rather than certify cleanup.
            warned = False
            cleanup_started = time.monotonic()
            while True:
                try:
                    sample()
                    if not owned:
                        break
                    sig = signal.SIGTERM if time.monotonic() - cleanup_started < term_grace else signal.SIGKILL
                    for identity in owned.values():
                        # Recheck birth immediately before a targeted signal.
                        if alive(identity, processes()):
                            try:
                                os.kill(identity["pid"], sig)
                            except ProcessLookupError:
                                pass
                    child.poll()  # reap the leader; zombies aren't checkout users
                except (OSError, ValueError, subprocess.SubprocessError, RuntimeError):
                    if not warned:
                        log("round cleanup unverified; retaining checkout lock")
                        warned = True
                time.sleep(poll)
            child.wait()
        return rc


def observed_command(argv: list[str]) -> int:
    """Run a synchronous delegated Codex command with the native activity observer.

    Use `python -m tauceti_worker.round_activity -- COMMAND ...` from a wrapper
    inheriting the round's environment. Raw output remains on stdout so the caller
    can keep its existing log. No alternate scheduler or lease is introduced.
    """
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    activity = None
    assert proc.stdout is not None
    try:
        activity = Activity(proc.pid)
        for line in proc.stdout:
            activity.observe(line)
            sys.stdout.write(line)
            sys.stdout.flush()
        return proc.wait()
    finally:
        # The supervisor retains round.lock and accounts for owned descendants.
        # A wrapper exception must not abandon its direct child while returning.
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        proc.stdout.close()
        if activity is not None:
            activity.finish()


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["--"]:
        args = args[1:]
    if not args:
        raise SystemExit("usage: python -m tauceti_worker.round_activity -- COMMAND ...")
    raise SystemExit(observed_command(args))
