"""tauceti_worker.round — round lifecycle: the per-worker lock, signal/cleanup handling, the
branch-claim heartbeat, and the loop→child round spawn with process-group teardown."""

from __future__ import annotations

import atexit
import fcntl
import os
import signal
import subprocess
import sys
import time

from .config import Config, Die, log
from .constants import CLAIM_HEARTBEAT_S, CLAIM_TTL_S, ROUND_TIMEOUT
from .github import claims_repo
from .paths import CLAIM_SH, self_argv, self_env

CLAIM_COMMAND_TIMEOUT_S = 35
_CLAIM_OUTCOMES = {
    1: "other-owner",
    2: "command/transport-error",
    3: "absent",
    4: "malformed",
    5: "expired",
    6: "CAS-race",
}
_ACTIVE_CLAIMS: Claims | None = None


def check_claim_health() -> bool:
    """Agent runners poll this even while the provider is silent; no active lease means no guard."""
    return _ACTIVE_CLAIMS is None or _ACTIVE_CLAIMS.check_health()


# ============================================================================
# Round lifecycle — flock (one round per worker), signal handling, cleanup, and
# the loop→child spawn with process-group teardown.
#
# Python's default close_fds=True means children never inherit the round.lock fd,
# so the old shell worker's hand-managed `9>&-` fd-leak fix (commit 3e4828b) is automatic; we
# also mark the lock fd non-inheritable as belt-and-suspenders. Running each round
# as a child of the loop in its OWN session is what makes timeout teardown, the
# cleanup-on-exit, and a SIGKILL-self-cleaning bubble behave like the shell.
# ============================================================================


class RoundContext:
    """Holds the per-worker round lock for the life of a round, runs cleanup on any exit, and routes
    SIGTERM→143 / SIGINT→130 through that cleanup (so a loop-sent SIGTERM still releases the lease)."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._fd: int | None = None
        self._cleanups: list = []
        self._done = False
        # The checkout baseline established by the selected work unit.  This is deliberately set
        # after any target-branch checkout, not when the round starts: the shared host checkout may
        # still be on another PR from the preceding round.
        self.change_base_head: str | None = None

    def __enter__(self) -> RoundContext:
        self.cfg.state.mkdir(parents=True, exist_ok=True)
        path = self.cfg.state / "round.lock"
        fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o644)
        os.set_inheritable(fd, False)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise Die(
                f"another round for worker '{self.cfg.wid}' holds {path} — one round per worker at a time"
            ) from None
        self._fd = fd
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
        signal.signal(signal.SIGINT, lambda *_: sys.exit(130))
        atexit.register(self._cleanup)
        return self

    def add_cleanup(self, fn) -> None:
        self._cleanups.append(fn)

    def _cleanup(self) -> None:
        if self._done:
            return
        self._done = True
        for fn in reversed(self._cleanups):  # LIFO: stop heartbeat, pop bubble, release claim
            try:
                fn()
            except Exception as e:
                log(f"cleanup step failed: {e}")
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

    def __exit__(self, *exc) -> bool:
        self._cleanup()
        return False


def spawn_round(argv_tail: list[str]) -> subprocess.Popen:
    """Spawn one round as a child in its OWN session (so the loop can kill the whole group). Invokes
    the current interpreter directly on this file (NOT via the uv shebang) to avoid a uv wrapper
    process between the loop and the round — sys.executable is already the uv-resolved interpreter."""
    cmd = self_argv("_round", *argv_tail)
    return subprocess.Popen(cmd, start_new_session=True, env=self_env())


def signal_group(pgid: int, sig: int) -> str:
    """Send `sig` to process group `pgid`. Returns "sent", "gone", or "denied"; never raises.

    "gone" is ESRCH: no such process group, so there is nothing to clean up.

    "denied" is EPERM: the group exists but killpg found no member it could signal. On Darwin that
    is what a group reports once its survivors are all zombies — XNU's killpg1 skips SZOMB members
    while iterating an explicit process group and then returns EPERM having signalled none — which
    is the state a just-killed leader is in until its parent wait()s it, and is why the round sweep
    blew up on macOS. It is NOT only that. A live member whose real or saved uid no longer matches
    ours (an agent running `sudo -u other`), a MAC policy denial, or the accepted pgid-reuse race
    landing on somebody else's group all report the same thing, and killpg cannot tell them apart.

    So "denied" must not be read as "clean". Callers stop, because a signal that could not be
    delivered will not be delivered by repeating it, but they say so rather than claiming success.
    """
    try:
        os.killpg(pgid, sig)
        return "sent"
    except ProcessLookupError:
        return "gone"
    except PermissionError:
        return "denied"


def kill_round_group(p: subprocess.Popen, term_grace: int = 30) -> None:
    """SIGTERM the round's process group, give it term_grace to clean up, then SIGKILL — the
    `timeout --kill-after=30s` teardown, but reaching the whole group (agent + build daemons)."""
    try:
        pgid = os.getpgid(p.pid)
    except ProcessLookupError:
        return
    sent = signal_group(pgid, signal.SIGTERM)
    if sent == "gone":
        return
    if sent == "denied":
        # Nothing in the group can be signalled, so neither the grace wait nor SIGKILL will achieve
        # anything, and p.wait() below would block for as long as the leader happens to live.
        log(f"WARNING: round group {pgid} refused SIGTERM; leaving teardown to the operator")
        return
    try:
        p.wait(term_grace)
    except subprocess.TimeoutExpired:
        if signal_group(pgid, signal.SIGKILL) == "denied" and p.poll() is None:
            # The leader is still running and we could not signal its group. Nothing here can force
            # it down, and p.wait() below would block forever, so report and leave it to the operator.
            log(f"WARNING: round {p.pid} is still alive but its process group refused SIGKILL; not waiting")
            return
        p.wait()


def reap_round_group(pgid: int, term_grace: float = 2.0) -> None:
    """Sweep any processes still alive in a finished round's process group — the background poll-loops a
    tool-using agent leaves behind. Claude Code's Bash tool, waiting on a backgrounded `lake build`,
    synthesizes `until ... do sleep; done` (and, on a job-control quirk in non-interactive bash, a
    `until ! kill -0 %1; do :; done` that busy-spins a whole core). When the agent exits 0 those loops
    have no parent left and reparent to init, surviving forever. The round runs in its OWN session
    (spawn_round's start_new_session ⇒ the group id equals the leader pid), so signalling `pgid` reaches
    only the round's descendants — never the loop driver or the user's shell. Idempotent: a no-op when
    the group is already empty (the clean, common case) or already torn down by kill_round_group."""
    sent = signal_group(pgid, signal.SIGTERM)
    if sent == "gone":
        return  # group empty — round left nothing behind
    if sent == "denied":
        log(f"round group {pgid}: nothing signalable left to sweep (likely already-exited stragglers)")
        return
    deadline = time.monotonic() + term_grace
    while time.monotonic() < deadline:
        probe = signal_group(pgid, 0)
        if probe == "gone":
            return  # stragglers died on SIGTERM
        if probe == "denied":
            log(f"round group {pgid}: survivors are no longer signalable; leaving them")
            return
        time.sleep(0.05)
    if signal_group(pgid, signal.SIGKILL) == "denied":
        log(f"WARNING: round group {pgid} ignored SIGTERM and refused SIGKILL; stragglers may survive")


def _session_groups(session_id: int) -> set[int]:
    """Enumerate PID/state only, then ask the kernel for session/group identity (portable on Darwin)."""
    try:
        result = subprocess.run(["ps", "-axo", "pid=,stat="], capture_output=True, text=True, timeout=5, check=True)
        rows = [line.split() for line in result.stdout.splitlines()]
        pids = [int(row[0]) for row in rows if len(row) == 2 and not row[1].startswith("Z")]
        if any(len(row) != 2 for row in rows) or os.getpid() not in pids:
            raise ValueError("invalid or incomplete process inventory")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise Die("cannot enumerate round session safely; operator cleanup required") from exc
    groups = set()
    for pid in pids:
        try:
            if os.getsid(pid) == session_id:
                group = os.getpgid(pid)
                if os.getsid(pid) == session_id:
                    groups.add(group)
        except ProcessLookupError:
            continue
        except PermissionError as exc:
            raise Die("cannot verify process session; operator cleanup required") from exc
    return groups


def reap_round_session(session_id: int, term_grace: float = 2.0) -> None:
    """Sweep dedicated agent groups that remain in this round's original session.

    Agent groups may outlive their leader or the round (including SIGKILL). Kernel session identity
    avoids a registration-file race and never selects the loop or an unrelated direct agent session.
    """
    if session_id == os.getsid(0):
        raise Die("refusing to sweep the caller's own session")
    deadline = time.monotonic() + term_grace
    kill_deadline = deadline + 1.0
    while True:
        groups = _session_groups(session_id)
        if not groups:
            return
        sig = signal.SIGTERM if time.monotonic() < deadline else signal.SIGKILL
        for group in groups:
            if signal_group(group, sig) == "denied":
                raise Die(f"round session {session_id} group {group} cannot be reaped; operator cleanup required")
        if time.monotonic() >= kill_deadline:
            if _session_groups(session_id):
                raise Die(
                    f"round session {session_id} still has live processes after SIGKILL; operator cleanup required"
                )
            return
        time.sleep(0.05)


class Claims:
    """[COOP] branch claims + the [HARD] push-arbiter env. Mutating tasks take a branch/<pr> claim and
    heartbeat it (dedup only; git-safe-push's branch CAS is the real guarantee). The heartbeat is a
    detached child that dies with the parent via an inherited pipe (EOF when the parent goes, even on
    SIGKILL), never runs the round's cleanup, and never holds the round.lock fd (pass_fds keeps only
    the pipe; the lock fd is non-inheritable + closed by close_fds)."""

    def __init__(self, cfg: Config, ctx: RoundContext):
        self.cfg = cfg
        self.ctx = ctx
        self.held: tuple[str, str] | None = None
        self._hb: subprocess.Popen | None = None
        self._hb_wfd: int | None = None

    def begin_branch_work(self, pr: int, head: str, refname: str, owner: str, repo: str) -> bool:
        """Take the branch claim and set the push-arbiter env. Returns False unless ownership is
        positively established. Unknown ownership must not admit expensive branch-authoring work.

        The claim goes to the worker's claim namespace, NOT to the PR's head repository: `branch/<pr>`
        is keyed on the canonical PR number, so two workers contend for it wherever they are, whereas a
        claim in someone else's fork is one only its owner can push. `owner`/`repo` still name the head
        repo, because that is where the push arbiter's branch CAS runs."""
        key = f"branch/{pr}"
        claim_repo = claims_repo()
        claim_env = {**os.environ, "CLAIM_REPO": claim_repo}
        try:
            rc = subprocess.run(
                [CLAIM_SH, "acquire", key, str(CLAIM_TTL_S)],
                capture_output=True,
                env=claim_env,
                timeout=CLAIM_COMMAND_TIMEOUT_S,
            ).returncode
        except (OSError, subprocess.TimeoutExpired):
            rc = 2
        if rc != 0:
            reason = _CLAIM_OUTCOMES.get(rc, "helper-error")
            log(f"branch #{pr} claim unavailable ({reason}, rc={rc}) — deferring authoring")
            return False
        os.environ["TAUCETI_PUSH_REF"] = refname
        os.environ["TAUCETI_PUSH_EXPECT"] = head
        os.environ["TAUCETI_PUSH_REMOTE"] = f"https://github.com/{owner}/{repo}"
        os.environ["TAUCETI_CLAIM_SH"] = CLAIM_SH
        self.held = (key, claim_repo)
        os.environ["TAUCETI_CLAIM_REPO"] = claim_repo
        os.environ["TAUCETI_CLAIM_KEY"] = key
        self.ctx.add_cleanup(self.release)
        try:
            self.start_heartbeat(key, claim_repo)
        except OSError:
            log(f"branch #{pr}: heartbeat failed to start — deferring authoring")
            self.release()
            return False
        return True

    def start_heartbeat(self, key: str, claim_repo: str) -> None:
        rfd, wfd = os.pipe()
        os.set_inheritable(rfd, True)
        cmd = self_argv("_heartbeat", key, "--ppipe", str(rfd))
        env = self_env(
            {
                **os.environ,
                "CLAIM_REPO": claim_repo,
                "TAUCETI_CLAIM_SH": CLAIM_SH,
                "CLAIM_TTL": str(CLAIM_TTL_S),
            }
        )
        try:
            self._hb = subprocess.Popen(cmd, pass_fds=[rfd], env=env)
        except BaseException:
            os.close(rfd)
            os.close(wfd)
            raise
        global _ACTIVE_CLAIMS
        _ACTIVE_CLAIMS = self
        self._health_reported = False
        os.close(rfd)  # parent keeps only the write end; its closure (or death) is the EOF signal
        self._hb_wfd = wfd
        self.ctx.add_cleanup(self.stop_heartbeat)

    def check_health(self) -> bool:
        rc = self._hb.poll() if self._hb is not None else 2
        if rc is None:
            return True
        if not getattr(self, "_health_reported", False):
            reason = _CLAIM_OUTCOMES.get(rc, "heartbeat-exited")
            log(f"branch claim heartbeat unavailable ({reason}, rc={rc}) — stop and preserve candidate")
            self._health_reported = True
        return False

    def stop_heartbeat(self) -> None:
        global _ACTIVE_CLAIMS
        if _ACTIVE_CLAIMS is self:
            _ACTIVE_CLAIMS = None
        if self._hb_wfd is not None:
            try:
                os.close(self._hb_wfd)  # EOF → the heartbeat child exits on its own
            except OSError:
                pass
            self._hb_wfd = None
        if self._hb is not None:
            try:
                self._hb.terminate()
                self._hb.wait(5)
            except subprocess.TimeoutExpired:
                self._hb.kill()
                self._hb.wait(5)
            except OSError:
                pass
            self._hb = None

    def release(self) -> None:
        if self.held:
            key, claim_repo = self.held
            try:
                result = subprocess.run(
                    [CLAIM_SH, "release", key],
                    capture_output=True,
                    env={**os.environ, "CLAIM_REPO": claim_repo},
                    timeout=CLAIM_COMMAND_TIMEOUT_S,
                )
                if result.returncode:
                    log(f"claim release unavailable (rc={result.returncode}); leaving lease to expire")
            except (OSError, subprocess.TimeoutExpired):
                log("claim release failed/timed out; leaving lease to expire")
            self.held = None
            os.environ.pop("TAUCETI_CLAIM_KEY", None)
            os.environ.pop("TAUCETI_CLAIM_REPO", None)


def cmd_heartbeat(args) -> int:
    """Return the typed renewal failure to the parent; bound renewal and stop on parent pipe EOF."""
    import select

    def parent_gone(delay: float) -> bool:
        if args.ppipe is None:
            time.sleep(delay)
            return False
        try:
            ready, _, _ = select.select([args.ppipe], [], [], delay)
            return bool(ready) and os.read(args.ppipe, 1) == b""
        except OSError:
            return True

    # A parent-requested shutdown must unwind the active renewal child too.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    while not parent_gone(CLAIM_HEARTBEAT_S):
        try:
            proc = subprocess.Popen(
                [CLAIM_SH, "renew", args.key],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            return 2
        try:
            deadline = time.monotonic() + CLAIM_COMMAND_TIMEOUT_S
            while proc.poll() is None:
                if parent_gone(0.1):
                    return 0
                if time.monotonic() >= deadline:
                    return 2
            if proc.returncode != 0:
                return proc.returncode if proc.returncode > 0 else 2
        finally:
            if proc.poll() is None:
                signal_group(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(2)
                except subprocess.TimeoutExpired:
                    signal_group(proc.pid, signal.SIGKILL)
                    proc.wait(2)
            reap_round_group(proc.pid, term_grace=0.1)
    return 0


def run_round_subprocess(argv_tail: list[str], timeout: int = ROUND_TIMEOUT) -> int:
    """Run one round as a child under a hard timeout; tear down the group on expiry. Used by the loop.
    Maps a timed-out round to rc 124, a SIGKILL-after-grace to 137 (matching the shell's `timeout`)."""
    p = spawn_round(argv_tail)
    pgid = p.pid  # spawn_round's start_new_session ⇒ the round leads its own group; pgid == leader pid
    try:
        return p.wait(timeout)
    except subprocess.TimeoutExpired:
        log(f"round timed out after {timeout}s — tearing down")
        kill_round_group(p)
        rc = p.returncode
        return 137 if rc is not None and rc < 0 and -rc == signal.SIGKILL else 124
    except KeyboardInterrupt:
        kill_round_group(p)
        raise
    finally:
        # Even a round that exits 0 can leave the agent's backgrounded build-waiters alive; the timeout
        # path's kill_round_group never runs for it. Sweep the group on EVERY exit so a leaked poll-loop
        # lives at most one round, not forever (a no-op once kill_round_group already cleared the group).
        # Unlike kill_round_group (which signals while the leader PID is still live), p.wait() has already
        # reaped the leader here, so the group is held open only by stragglers. The lone wrong-kill window
        # — the freed leader PID being reused AND the reuser making itself a group leader before this line
        # — is microseconds wide and needs a deliberate setsid; we accept it. (One-shot `tauceti work`
        # runs the round in-process, not through here, so it is not swept; only the unbounded --loop leak
        # is operationally damaging, so that scope gap is acceptable.)
        reap_round_group(pgid)
        reap_round_session(pgid)
