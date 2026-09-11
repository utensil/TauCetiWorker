"""Bounded, opt-in observation of a validation subprocess's actual output.

The existing round supervisor remains the owner. Only process identities and
progress times enter its status channel; output and command text never do.
"""

from __future__ import annotations

import argparse
import hashlib
import math
import os
import re
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path

from .round_activity import STATUS_ENV, TOKEN_ENV, alive, descendants, processes, update_work

ANSI = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]")


class Interrupted(BaseException):
    def __init__(self, signum):
        self.signum = signum


def run(argv: list[str], *, idle_seconds: float, max_seconds: float) -> int:
    """Observe fresh output lines, with both idle and absolute time limits.

    Callers must select real validation commands, not log followers or polls.
    Distinct output is evidence of activity, not evidence of a successful build.
    Repeated lines never renew progress; the absolute deadline bounds noisy work.
    """
    if not argv or any(not math.isfinite(n) or n <= 0 for n in (idle_seconds, max_seconds)):
        raise ValueError("command and finite positive deadlines required")
    token = os.environ.get(TOKEN_ENV)
    path = Path(os.environ[STATUS_ENV]) if token else None
    # Reject a stale invocation before it can touch the checkout.
    if path:
        update_work(path, token, lambda w: None)
    started = last_output = time.monotonic()
    seen = set()
    pending = b""
    owned = {}
    child = None
    handlers = {}

    def interrupted(signum, frame):
        raise Interrupted(signum)

    def sample():
        nonlocal owned
        current = processes()
        if child.poll() is None and str(child.pid) not in current:
            raise ValueError("live validation missing from process snapshot")
        for pid, identity in current.items():
            if identity["group"] == child.pid:
                owned.setdefault(pid, identity)
        owned = descendants(owned, current)
        if path:

            def register(work):
                # A large build can launch thousands of short-lived compilers.
                # Keep the live registrations, not a history of every compiler.
                work["agents"] = {pid: identity for pid, identity in work["agents"].items() if alive(identity, current)}
                work["agents"].update(owned)

            update_work(path, token, register)

    def observe(line):
        nonlocal last_output
        line = ANSI.sub(b"", line).strip()
        if not line:
            return
        digest = hashlib.sha256(line).digest()
        # Bounded memory; reaching the cap fails closed for further renewals.
        if digest in seen or len(seen) >= 65536:
            return
        seen.add(digest)
        last_output = time.monotonic()
        if path:
            update_work(path, token, lambda w: w.update(progress=max(w["progress"], last_output)))

    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            handlers[sig] = signal.signal(sig, interrupted)
        child = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True
        )
        assert child.stdout is not None
        os.set_blocking(child.stdout.fileno(), False)
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            sample()
            next_sample = time.monotonic() + 0.5
            eof = False
            while True:
                now = time.monotonic()
                if now >= next_sample:
                    sample()
                    next_sample = time.monotonic() + 0.5
                if child.poll() is not None and eof:
                    if pending:
                        observe(pending)
                    return child.returncode
                if now - started >= max_seconds or now - last_output >= idle_seconds:
                    print("validation deadline reached", file=sys.stderr, flush=True)
                    return 124
                for key, _ in selector.select(
                    min(0.1, max_seconds - (now - started), idle_seconds - (now - last_output))
                ):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        eof = True
                        continue
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.buffer.flush()
                    pending += chunk.replace(b"\r", b"\n")
                    lines = pending.split(b"\n")
                    pending = lines.pop()
                    for line in lines:
                        observe(line)
                    # An unterminated stream must not grow memory or renew activity.
                    pending = pending[-65536:]
    except Interrupted as exc:
        return 128 + exc.signum
    finally:
        # Ignore repeated termination while proving cleanup. The outer supervisor
        # retains its round lock and can still escalate to SIGKILL if needed.
        for sig in handlers:
            signal.signal(sig, signal.SIG_IGN)
        if child is not None:
            cleanup_started = time.monotonic()
            while True:
                try:
                    # Cleanup must still work after a round token changes.
                    current = processes()
                    for pid, identity in current.items():
                        if identity["group"] == child.pid:
                            owned.setdefault(pid, identity)
                    owned = descendants(owned, current)
                    if not owned:
                        break
                    sig = signal.SIGTERM if time.monotonic() - cleanup_started < 5 else signal.SIGKILL
                    for identity in owned.values():
                        if alive(identity, processes()):
                            try:
                                os.kill(identity["pid"], sig)
                            except ProcessLookupError:
                                pass
                    child.poll()
                except (OSError, ValueError, subprocess.SubprocessError):
                    # A failed snapshot cannot certify that checkout users exited.
                    pass
                time.sleep(0.05)
            child.wait()
            child.stdout.close()
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


def positive_seconds(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be finite and positive")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--idle-seconds", type=positive_seconds, required=True)
    parser.add_argument("--max-seconds", type=positive_seconds, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a command is required")
    rc = run(command, idle_seconds=args.idle_seconds, max_seconds=args.max_seconds)
    return 128 - rc if rc < 0 else rc


if __name__ == "__main__":
    sys.exit(main())
