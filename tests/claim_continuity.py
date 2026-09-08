#!/usr/bin/env python3
"""Focused local fixtures for fail-closed claims and same-owner renewal continuity."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CLAIM = REPO / "scripts" / "claim.sh"
SAFE_PUSH = REPO / "scripts" / "git-safe-push"
PYTHON = sys.executable
REAL_GIT = shutil.which("git")
assert REAL_GIT


def check(name, fn):
    try:
        fn()
        print(f"[OK ] {name}")
        return 0
    except Exception as exc:
        print(f"[BAD] {name}: {exc}")
        return 1


def claim_env(remote: Path, gitdir: Path, owner="worker-a", **extra):
    url = "https://github.com/local/claims"
    return {
        **os.environ,
        "CLAIM_REPO": "local/claims",
        "CLAIM_GITDIR": str(gitdir),
        "TAUCETI_WORKER_ID": owner,
        "CLAIM_COMMAND_TIMEOUT": "3",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"url.file://{remote}.insteadOf",
        "GIT_CONFIG_VALUE_0": url,
        **extra,
    }


def call_claim(env, *args, timeout=8):
    return subprocess.run([CLAIM, *args], env=env, capture_output=True, text=True, timeout=timeout)


def write_lease(gitdir: Path, remote: Path, key: str, body: str):
    tree = subprocess.check_output(
        [REAL_GIT, "-C", gitdir, "hash-object", "-t", "tree", "-w", "/dev/null"], text=True
    ).strip()
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test.invalid",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test.invalid",
    }
    oid = subprocess.run(
        [REAL_GIT, "-C", gitdir, "commit-tree", tree, "-F", "-"],
        input=body,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    subprocess.run(
        [REAL_GIT, "-C", gitdir, "push", str(remote), f"{oid}:refs/tauceti-claims/{key}"],
        capture_output=True,
        check=True,
    )


def local_claim_states():
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        remote = tmp / "remote.git"
        gd_a = tmp / "a.git"
        gd_b = tmp / "b.git"
        subprocess.run([REAL_GIT, "init", "-q", "--bare", remote], check=True)
        a = claim_env(remote, gd_a)
        b = claim_env(remote, gd_b, owner="worker-b")

        assert call_claim(a, "read", "branch/1").stdout == ""
        assert call_claim(a, "holds", "branch/1").returncode == 1
        assert call_claim(a, "renew", "branch/1").returncode == 1
        started = time.monotonic()
        assert call_claim(a, "acquire", "branch/1", "30").returncode == 0
        lease = json.loads(call_claim(a, "read", "branch/1").stdout)
        assert lease["owner"] == "worker-a" and lease["resource"] == "branch/1"
        assert call_claim(a, "holds", "branch/1").returncode == 0
        assert call_claim(a, "renew", "branch/1", "30").returncode == 0
        assert time.monotonic() - started < 3, "successful commands waited for canceled watchdog sleeps"
        assert call_claim(b, "acquire", "branch/1", "30").returncode == 1
        assert call_claim(b, "holds", "branch/1").returncode == 1
        assert call_claim(b, "renew", "branch/1").returncode == 1
        assert call_claim(a, "release", "branch/1").returncode == 0
        assert call_claim(a, "read", "branch/1").stdout == ""

        write_lease(gd_a, remote, "branch/bad", "not-json")
        for command in ("read", "holds", "renew", "acquire", "release"):
            assert call_claim(a, command, "branch/bad").returncode == 2, command


def lookup_timeout_is_unknown():
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        remote = tmp / "remote.git"
        gitdir = tmp / "claims.git"
        bindir = tmp / "bin"
        subprocess.run([REAL_GIT, "init", "-q", "--bare", remote], check=True)
        subprocess.run([REAL_GIT, "init", "-q", "--bare", gitdir], check=True)
        bindir.mkdir()
        shim = bindir / "git"
        shim.write_text(
            f'#!/bin/sh\ncase " $* " in *" ls-remote "*) trap "" TERM; exec sleep 5;; esac\nexec "{REAL_GIT}" "$@"\n'
        )
        shim.chmod(0o755)
        env = claim_env(remote, gitdir, PATH=f"{bindir}:{os.environ['PATH']}", CLAIM_COMMAND_TIMEOUT="1")
        started = time.monotonic()
        result = call_claim(env, "read", "branch/timeout", timeout=4)
        assert result.returncode == 2 and time.monotonic() - started < 3


def same_owner_race_continues_current_build():
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        remote = tmp / "remote.git"
        bindir = tmp / "bin"
        barrier = tmp / "barrier"
        subprocess.run([REAL_GIT, "init", "-q", "--bare", remote], check=True)
        bindir.mkdir()
        barrier.mkdir()
        base = claim_env(remote, tmp / "initial.git", owner="same-owner")
        assert call_claim(base, "acquire", "branch/race", "30").returncode == 0
        shim = bindir / "git"
        shim.write_text(
            f'#!/bin/sh\ncase " $* " in *" push --force-with-lease="*) touch "$RACE_DIR/$RACE_ID"; '
            'while [ "$(find "$RACE_DIR" -type f | wc -l | tr -d " ")" -lt 2 ]; do sleep .01; done\n'
            f';; esac\nexec "{REAL_GIT}" "$@"\n'
        )
        shim.chmod(0o755)
        author = subprocess.Popen([PYTHON, "-c", "import time; time.sleep(20)"])
        try:
            racers = []
            for rid in ("one", "two"):
                env = claim_env(
                    remote,
                    tmp / f"{rid}.git",
                    owner="same-owner",
                    RACE_ID=rid,
                    RACE_DIR=str(barrier),
                    PATH=f"{bindir}:{os.environ['PATH']}",
                )
                racers.append(
                    subprocess.Popen(
                        [CLAIM, "renew", "branch/race", "30"],
                        env=env,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                )
            results = [p.communicate(timeout=8) + (p.returncode,) for p in racers]
            assert [r[2] for r in results] == [0, 0], results
            assert author.poll() is None, "recovered renewal race stopped the current build"
            assert call_claim(base, "holds", "branch/race").returncode == 0
        finally:
            author.terminate()
            author.wait(3)


def safe_push_refuses_uncertain_claims():
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        bindir = tmp / "bin"
        bindir.mkdir()
        pushed = tmp / "pushed"
        git = bindir / "git"
        git.write_text(f'#!/bin/sh\ntouch "{pushed}"\n')
        git.chmod(0o755)
        base = {
            **os.environ,
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "TAUCETI_CLAIM_KEY": "branch/1",
            "TAUCETI_PUSH_REF": "feature",
            "TAUCETI_PUSH_EXPECT": "abc",
        }
        missing = subprocess.run(
            [SAFE_PUSH], env={**base, "TAUCETI_CLAIM_SH": str(tmp / "missing")}, capture_output=True, text=True
        )
        assert missing.returncode == 1 and not pushed.exists() and "could not be verified" in missing.stderr
        helper = tmp / "claim"
        helper.write_text("#!/bin/sh\nexit 2\n")
        helper.chmod(0o755)
        unknown = subprocess.run(
            [SAFE_PUSH], env={**base, "TAUCETI_CLAIM_SH": str(helper)}, capture_output=True, text=True
        )
        assert unknown.returncode == 1 and not pushed.exists() and "could not be verified" in unknown.stderr
        assert "took over" not in unknown.stderr


PARENT_FIXTURE = r"""
import os, signal, subprocess, sys, time
from pathlib import Path
from types import SimpleNamespace
from tauceti_worker import round as r
r.CLAIM_HEARTBEAT_S = .05
r.CLAIM_CALL_TIMEOUT_S = .2
r.CLAIM_SH = os.environ["FIXTURE_HELPER"]
cfg = SimpleNamespace(state=Path(os.environ["FIXTURE_STATE"]), wid="fixture")
with r.RoundContext(cfg) as ctx:
    author = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
    Path(os.environ["AUTHOR_PID"]).write_text(str(author.pid))
    def stop_author():
        author.terminate()
        author.wait(3)
    ctx.add_cleanup(stop_author)
    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(write_fd)
        rc = r.cmd_heartbeat(SimpleNamespace(key="branch/fixture", ppipe=read_fd))
        os._exit(rc)
    os.close(read_fd)
    ctx.add_cleanup(lambda: os.close(write_fd))
    ctx.add_cleanup(lambda: os.waitpid(child, 0))
    while True: time.sleep(1)
"""


def heartbeat_stops_only_native_parent():
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        for name, body in (("unknown", "exit 2"), ("hanging", "exec sleep 10")):
            helper = tmp / name
            helper.write_text(f"#!/bin/sh\n{body}\n")
            helper.chmod(0o755)
            author_pid = tmp / f"{name}.pid"
            env = {
                **os.environ,
                "PYTHONPATH": str(REPO),
                "FIXTURE_HELPER": str(helper),
                "FIXTURE_STATE": str(tmp / f"state-{name}"),
                "AUTHOR_PID": str(author_pid),
            }
            parent = subprocess.Popen(
                [PYTHON, "-c", PARENT_FIXTURE], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
            out, err = parent.communicate(timeout=5)
            assert parent.returncode == 143, (name, parent.returncode, out, err)
            pid = int(author_pid.read_text())
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                pass
            else:
                raise AssertionError(f"{name}: native cleanup left author {pid} alive")
            assert "ownership could not be verified" in err

        direct = subprocess.run(
            [
                PYTHON,
                "-c",
                "from types import SimpleNamespace; from tauceti_worker import round as r; "
                "r.CLAIM_HEARTBEAT_S=.01; r.CLAIM_CALL_TIMEOUT_S=.2; r.CLAIM_SH='" + str(helper) + "'; "
                "raise SystemExit(r.cmd_heartbeat(SimpleNamespace(key='direct', ppipe=None)))",
            ],
            env={**os.environ, "PYTHONPATH": str(REPO)},
            capture_output=True,
            text=True,
            timeout=3,
        )
        assert direct.returncode == 0 and "stopping heartbeat" in direct.stderr


fails = sum(
    check(name, case)
    for name, case in (
        ("local acquire/read/holds/renew/release and unknown states", local_claim_states),
        ("claim lookup timeout is bounded and unknown", lookup_timeout_is_unknown),
        ("same-owner renewal race preserves the current build", same_owner_race_continues_current_build),
        ("safe push refuses missing helpers and uncertain ownership", safe_push_refuses_uncertain_claims),
        ("heartbeat loss/timeout stops only its native parent", heartbeat_stops_only_native_parent),
    )
)
print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
