#!/usr/bin/env python3
"""Offline lease/CAS and lifetime regressions, using real local bare repositories."""

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from tauceti_worker import round as rounds

REAL_GIT = shutil.which("git")
CLAIM = REPO / "scripts/claim.sh"
PUSH = REPO / "scripts/git-safe-push"
KEY = "branch/123"
REF = "refs/tauceti-claims/" + KEY


def run(*args, env=None, cwd=None, input=None):
    return subprocess.run(args, env=env, cwd=cwd, input=input, text=True, capture_output=True, timeout=40)


class LeaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.remote = self.base / "remote.git"
        self.env = {
            **os.environ,
            "CLAIM_REPO": "offline/fixture",
            "CLAIM_GITDIR": str(self.base / "scratch.git"),
            "TAUCETI_WORKER_ID": "fixture-owner",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"url.{self.remote}.insteadOf",
            "GIT_CONFIG_VALUE_0": "https://github.com/offline/fixture",
            "GIT_AUTHOR_NAME": "fixture",
            "GIT_AUTHOR_EMAIL": "fixture@invalid",
            "GIT_COMMITTER_NAME": "fixture",
            "GIT_COMMITTER_EMAIL": "fixture@invalid",
        }
        self.assertEqual(run(REAL_GIT, "init", "--bare", str(self.remote)).returncode, 0)

    def claim(self, command, expected=0, **env):
        result = run(str(CLAIM), command, KEY, env={**self.env, **env})
        self.assertEqual(result.returncode, expected, result.stderr)
        return result

    def git(self, *args, input=None):
        result = run(REAL_GIT, "-C", str(self.remote), *args, env=self.env, input=input)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def install(self, payload, update=True):
        tree = self.git("hash-object", "-t", "tree", "-w", "--stdin", input="")
        oid = self.git("commit-tree", tree, "-F", "-", input=payload)
        if update:
            self.git("update-ref", REF, oid)
        return oid

    def payload(self, **changes):
        return json.dumps(
            {
                "schema": "tauceti-claim/v1",
                "resource": KEY,
                "owner": "fixture-owner",
                "expires_at": int(time.time()) + 1500,
                **changes,
            }
        )

    def mock_git(self, body):
        bindir = self.base / "bin"
        bindir.mkdir(exist_ok=True)
        path = bindir / "git"
        path.write_text(f'#!/bin/bash\n{body}\nexec "{REAL_GIT}" "$@"\n')
        path.chmod(0o755)
        return {"PATH": f"{bindir}:{os.environ['PATH']}"}

    def test_roundtrip_actual_json(self):
        self.claim("acquire")
        js = json.loads(self.claim("read").stdout)
        self.assertEqual(js["owner"], "fixture-owner")
        self.assertEqual(js["resource"], KEY)
        self.assertGreater(js["expires_at"], time.time())
        self.claim("holds")
        self.claim("renew")
        self.claim("release")
        self.claim("holds", 3)
        self.claim("release")

    def test_absent_network_fetch_and_malformed_are_distinct(self):
        self.claim("holds", 3)
        failed = self.mock_git('[[ "$3" == ls-remote ]] && exit 2')
        self.assertIn("remote-query-failed", self.claim("acquire", 2, **failed).stderr)
        self.assertEqual(self.git("for-each-ref", "--format=%(refname)"), "")
        self.install("")
        failed = self.mock_git('[[ "$3" == fetch ]] && exit 2')
        self.assertIn("lease-fetch-failed", self.claim("holds", 2, **failed).stderr)
        self.assertIn("malformed-lease", self.claim("holds", 4).stderr)
        self.claim("acquire", 4)
        self.claim("renew", 4)
        self.claim("release", 4)

    def test_malformed_payload_variants_cannot_be_reclaimed(self):
        for payload in (
            self.payload() + "\n" + self.payload(),
            self.payload(resource="branch/other"),
            self.payload(expires_at="tomorrow"),
            self.payload(expires_at=1e50),
            "[]",
        ):
            oid = self.install(payload)
            self.claim("acquire", 4)
            self.assertEqual(self.git("rev-parse", REF), oid)

    def test_actual_helper_deadline_reaps_network_descendants(self):
        marker = self.base / "network-child"
        fakegit = self.mock_git(f'''if [[ "$3" == ls-remote ]]; then
 sleep 60 &
 echo $! > "{marker}"
 wait
fi''')
        started = time.monotonic()
        result = self.claim("acquire", 2, **fakegit)
        self.assertIn("command-timeout", result.stderr)
        self.assertLess(time.monotonic() - started, 35)
        self.assertFalse(alive(int(marker.read_text())), "timed-out network child survived")

    def test_other_owner_and_expiry(self):
        oid = self.install(self.payload(owner="peer"))
        self.claim("holds", 1)
        self.claim("acquire", 1)
        self.claim("renew", 1)
        self.claim("release", 1)
        self.assertEqual(self.git("rev-parse", REF), oid)
        self.install(self.payload(expires_at=1))
        self.claim("holds", 5)
        self.claim("renew")
        self.claim("holds")

    def test_same_owner_cas_race_rereads_once(self):
        self.claim("acquire")
        replacement = self.install(self.payload(expires_at=int(time.time()) + 2000), update=False)
        marker = self.base / "raced"
        failed = self.mock_git(
            f'''if [[ "$3" == push && ! -f "{marker}" ]]; then
 touch "{marker}"
 "{REAL_GIT}" -C "{self.remote}" update-ref "{REF}" "{replacement}"
 echo ' ! [rejected] (stale info)' >&2
 exit 1
fi'''
        )
        self.claim("renew", **failed)
        self.claim("holds")
        failed = self.mock_git('[[ "$3" == push ]] && { echo "[rejected] stale info" >&2; exit 1; }')
        self.claim("renew", 6, **failed)

    def test_push_unknown_and_missing_helper_fail_before_branch_write(self):
        marker = self.base / "pushed"
        fakegit = self.mock_git(f'touch "{marker}"; exit 0')
        env = {
            **self.env,
            **fakegit,
            "TAUCETI_CLAIM_KEY": KEY,
            "TAUCETI_PUSH_REF": "feature",
            "TAUCETI_CLAIM_SH": str(self.base / "missing"),
        }
        result = run(str(PUSH), env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("helper unavailable", result.stderr)
        self.assertFalse(marker.exists())
        helper = self.base / "helper"
        for code, reason in ((2, "unknown"), (3, "absent"), (4, "malformed"), (1, "other owner"), (6, "CAS race")):
            helper.write_text(f"#!/bin/sh\nexit {code}\n")
            helper.chmod(0o755)
            env["TAUCETI_CLAIM_SH"] = str(helper)
            result = run(str(PUSH), env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(reason, result.stderr)
            self.assertFalse(marker.exists())

    def test_branch_cas_still_refuses_remote_change(self):
        work = self.base / "work"
        self.assertEqual(run(REAL_GIT, "clone", str(self.remote), str(work)).returncode, 0)

        def commit(message):
            result = run(REAL_GIT, "-C", str(work), "commit", "--allow-empty", "-m", message, env=self.env)
            self.assertEqual(result.returncode, 0, result.stderr)
            return run(REAL_GIT, "-C", str(work), "rev-parse", "HEAD").stdout.strip()

        baseline = commit("base")
        run(REAL_GIT, "-C", str(work), "push", "origin", "HEAD:refs/heads/feature")
        candidate = commit("candidate")
        moved = self.install(self.payload(), update=False)
        self.git("update-ref", "refs/heads/feature", moved)
        result = run(
            str(PUSH),
            cwd=work,
            env={
                **self.env,
                "TAUCETI_PUSH_REF": "refs/heads/feature",
                "TAUCETI_PUSH_EXPECT": baseline,
                "TAUCETI_CLAIM_KEY": "",
            },
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.git("rev-parse", "refs/heads/feature"), moved)
        self.assertEqual(run(REAL_GIT, "-C", str(work), "rev-parse", "HEAD").stdout.strip(), candidate)


class LifetimeTests(unittest.TestCase):
    def test_health_reports_stopped_heartbeat_even_zero_exit(self):
        claims = rounds.Claims(SimpleNamespace(), SimpleNamespace())
        for code in (0, 1, 2, 3, 4, 6):
            claims._hb = SimpleNamespace(poll=lambda code=code: code)
            self.assertFalse(claims.check_health())
        claims._hb = SimpleNamespace(poll=lambda: None)
        self.assertTrue(claims.check_health())

    def heartbeat(self, helper, rfd):
        code = (
            "import sys;sys.path.insert(0,sys.argv[1]);from tauceti_worker import round as r;"
            "from types import SimpleNamespace;"
            "r.CLAIM_HEARTBEAT_S=.02;r.CLAIM_COMMAND_TIMEOUT_S=1.5;r.CLAIM_SH=sys.argv[2];"
            "sys.exit(r.cmd_heartbeat(SimpleNamespace(key='branch/test',ppipe=int(sys.argv[3]))))"
        )
        return subprocess.Popen([sys.executable, "-c", code, str(REPO), str(helper), str(rfd)], pass_fds=[rfd])

    def test_heartbeat_typed_failure_timeout_and_parent_eof(self):
        with tempfile.TemporaryDirectory() as tmp:
            helper = Path(tmp) / "helper"
            for code in (1, 2, 4, 6):
                helper.write_text(f"#!/bin/sh\nexit {code}\n")
                helper.chmod(0o755)
                rfd, wfd = os.pipe()
                proc = self.heartbeat(helper, rfd)
                os.close(rfd)
                try:
                    self.assertEqual(proc.wait(5), code)
                finally:
                    os.close(wfd)
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait()
            childfile = Path(tmp) / "child"
            helper.write_text(f'#!/bin/sh\nsleep 30 &\necho $! > "{childfile}"\nwait\n')
            for eof in (False, True):
                childfile.unlink(missing_ok=True)
                rfd, wfd = os.pipe()
                proc = self.heartbeat(helper, rfd)
                os.close(rfd)
                try:
                    deadline = time.monotonic() + 5
                    while not childfile.exists() and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertTrue(childfile.exists())
                    child = int(childfile.read_text())
                    if eof:
                        os.close(wfd)
                        wfd = None
                    self.assertEqual(proc.wait(5), 0 if eof else 2)
                    self.assertFalse(alive(child), "renewal descendant survived")
                finally:
                    if wfd is not None:
                        os.close(wfd)
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait()

    def test_session_sweep_reaches_separate_group_and_preserves_sibling(self):
        sibling = subprocess.Popen(["sleep", "30"], start_new_session=True)
        try:
            for mode in ("normal", "timeout", "sigkill", "sigterm"):
                with tempfile.TemporaryDirectory() as tmp:
                    childfile = Path(tmp) / "child"
                    code = (
                        "import subprocess,sys,time;from pathlib import Path;"
                        "p=subprocess.Popen(['sleep','30'],process_group=0);"
                        "Path(sys.argv[1]).write_text(str(p.pid));"
                        "time.sleep(0 if sys.argv[2]=='normal' else 30)"
                    )
                    leader = subprocess.Popen(
                        [sys.executable, "-c", code, str(childfile), mode], start_new_session=True
                    )
                    deadline = time.monotonic() + 5
                    while not childfile.exists() and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertTrue(childfile.exists())
                    child = int(childfile.read_text())
                    try:
                        if mode == "sigkill":
                            leader.kill()
                        elif mode == "sigterm":
                            leader.terminate()
                        with patch.object(rounds, "spawn_round", return_value=leader):
                            result = rounds.run_round_subprocess([], timeout=0.1 if mode == "timeout" else 5)
                        self.assertEqual(result, {"normal": 0, "timeout": 124, "sigkill": -9, "sigterm": -15}[mode])
                        self.assertFalse(alive(child), mode)
                        self.assertIsNone(sibling.poll(), "unrelated session was signalled")
                    finally:
                        if leader.poll() is None:
                            leader.kill()
                            leader.wait()
                        if alive(child):
                            os.kill(child, signal.SIGKILL)
        finally:
            sibling.terminate()
            sibling.wait()

    def test_failed_inventory_never_signals(self):
        with patch.object(rounds.subprocess, "run", side_effect=OSError), patch.object(rounds, "signal_group") as sig:
            with self.assertRaises(rounds.Die):
                rounds.reap_round_session(999999)
            sig.assert_not_called()

    def test_empty_inventory_never_claims_cleanup(self):
        with patch.object(rounds.subprocess, "run", return_value=SimpleNamespace(stdout="")):
            with self.assertRaises(rounds.Die):
                rounds.reap_round_session(999999)


def alive(pid):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        state = run("ps", "-p", str(pid), "-o", "stat=").stdout.strip()
        if not state or state.startswith("Z"):
            return False
        time.sleep(0.03)
    return True


if __name__ == "__main__":
    unittest.main()
