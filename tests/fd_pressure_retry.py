"""Regression checks for transient file-descriptor pressure in status writes.

On 2026-09-19/20 the host exhausted its system-wide file table (``Errno 23: Too many open files in
system``) and the manager's status writes were the first syscalls to fail, because they are the ones
that need a new descriptor (the sibling ``.json.lock`` and the atomic temp file). The writes must
retry briefly, stay bounded, and never be confused with a broken configuration.
"""

from __future__ import annotations

import contextlib
import errno
import io
import json
import tempfile
from pathlib import Path
from unittest import mock

from tauceti_worker import worker_manager as wm
from tauceti_worker.runtime_status import (
    atomic_json,
    fd_pressure_stats,
    read_json,
    retry_fd_pressure,
    update_status,
)
from tauceti_worker.worker_manager import WorkersError, WorkersTransientError, load_worker_specs

ENFILE = OSError(errno.ENFILE, "Too many open files in system")
EACCES = OSError(errno.EACCES, "Permission denied")


def retries() -> int:
    return int(fd_pressure_stats()["fd_pressure_retries"])


# 1. The atomic write retries a burst on temp-file creation and still lands a complete file.
with tempfile.TemporaryDirectory(prefix="fd-pressure-atomic-") as raw:
    target = Path(raw) / "worker.json"
    real_mkstemp = tempfile.mkstemp
    made: list[int] = []

    def flaky_mkstemp(*args, **kwargs):
        made.append(1)
        if len(made) <= 2:
            raise ENFILE
        return real_mkstemp(*args, **kwargs)

    before = retries()
    with mock.patch("tauceti_worker.runtime_status.tempfile.mkstemp", flaky_mkstemp):
        atomic_json(target, {"alive": True})
    assert json.loads(target.read_text()) == {"alive": True}, "retried write must be complete"
    assert len(made) == 3, f"expected 2 retries after 2 failures, got {len(made)} attempts"
    assert retries() - before == 2, "each absorbed failure must be counted"
    leftovers = [p.name for p in Path(raw).iterdir() if p.name.startswith(".worker.json.")]
    assert not leftovers, f"retries must not leave temp files behind: {leftovers}"

# 2. update_status retries the sibling lock open, which is the descriptor the incident log showed
#    failing ("spinrep-front-b.json.lock").
with tempfile.TemporaryDirectory(prefix="fd-pressure-lock-") as raw:
    target = Path(raw) / "spinrep-front-b.json"
    real_open = Path.open
    state = {"fails": 1}

    def flaky_open(self, *args, **kwargs):
        if self.name.endswith(".json.lock") and state["fails"]:
            state["fails"] -= 1
            raise ENFILE
        return real_open(self, *args, **kwargs)

    before = retries()
    with mock.patch.object(Path, "open", flaky_open):
        value = update_status(target, state="running")
    assert value["state"] == "running" and target.is_file(), "status write must survive the retry"
    assert state["fails"] == 0, "the lock open must have been attempted again"
    assert retries() - before == 1, "the retried lock open must be counted"

# 3. A non-transient error is never retried: a permission problem must fail on the first attempt.
attempts: list[int] = []


def denied(self, *args, **kwargs):
    attempts.append(1)
    raise EACCES


before = retries()
with mock.patch.object(Path, "open", denied):
    try:
        update_status(Path(tempfile.gettempdir()) / "fd-pressure-denied.json", state="x")
    except OSError as exc:
        assert exc.errno == errno.EACCES, exc
    else:  # pragma: no cover - the write must not silently succeed
        raise AssertionError("EACCES must propagate")
assert len(attempts) == 1, f"EACCES must fail fast, saw {len(attempts)} attempts"
assert retries() == before, "a non-transient error must not be counted as pressure"

# 4. Sustained pressure stays bounded: the original error surfaces instead of looping forever.
sustained: list[int] = []


def always_enfile(self, *args, **kwargs):
    sustained.append(1)
    raise ENFILE


with mock.patch.object(Path, "open", always_enfile):
    try:
        update_status(Path(tempfile.gettempdir()) / "fd-pressure-sustained.json", state="x")
    except OSError as exc:
        assert exc.errno == errno.ENFILE, exc
    else:  # pragma: no cover
        raise AssertionError("sustained ENFILE must surface")
assert len(sustained) == 4, f"expected the bounded attempt count, saw {len(sustained)}"

# 5. The retry helper exposes the same bound when called directly.
calls: list[int] = []


def op():
    calls.append(1)
    raise ENFILE


try:
    retry_fd_pressure(op, attempts=2)
except OSError as exc:
    assert exc.errno == errno.ENFILE, exc
else:  # pragma: no cover
    raise AssertionError("retry_fd_pressure must re-raise the original error")
assert len(calls) == 2, f"attempts must be honouring the bound, saw {len(calls)}"

# 6. read_json absorbs one burst but keeps its contract: non-strict tolerates, strict surfaces.
with tempfile.TemporaryDirectory(prefix="fd-pressure-read-") as raw:
    target = Path(raw) / "status.json"
    target.write_text('{"alive": true}')
    real_read_text = Path.read_text
    reads = {"n": 0}

    def flaky_read_text(self, *args, **kwargs):
        reads["n"] += 1
        if reads["n"] == 1:
            raise ENFILE
        return real_read_text(self, *args, **kwargs)

    with mock.patch.object(Path, "read_text", flaky_read_text):
        assert read_json(target) == {"alive": True}, "a transient burst must not lose the record"

    def always_fail(self, *args, **kwargs):
        raise ENFILE

    with mock.patch.object(Path, "read_text", always_fail):
        try:
            read_json(target, strict=True)
        except OSError as exc:
            assert exc.errno == errno.ENFILE, exc
        else:  # pragma: no cover
            raise AssertionError("strict reads must surface sustained pressure")
    with mock.patch.object(Path, "read_text", always_fail):
        assert read_json(target) == {}, "non-strict reads stay tolerant after the bound"

# 7. The config read must distinguish a momentary host condition from a broken workers.toml, so the
#    manager stops reporting "invalid configuration" for a kernel-level fd shortage.
with tempfile.TemporaryDirectory(prefix="fd-pressure-config-") as raw:
    config = Path(raw) / "workers.toml"
    config.write_text("version = 1\nworkers = []\n")
    assert load_worker_specs(config) == [], "the control case must load"

    def enfile_open(self, *args, **kwargs):
        raise ENFILE

    with mock.patch.object(Path, "open", enfile_open):
        try:
            load_worker_specs(config)
        except WorkersTransientError:
            pass
        except WorkersError as exc:
            raise AssertionError(f"fd pressure must be transient, not a config fault: {exc!r}") from None
        else:  # pragma: no cover
            raise AssertionError("an unreadable config must raise")

    broken = Path(raw) / "broken.toml"
    broken.write_text("version = 1\n[workers\n")
    try:
        load_worker_specs(broken)
    except WorkersTransientError as exc:
        raise AssertionError(f"a broken TOML must not be reported as transient: {exc!r}") from None
    except WorkersError:
        pass
    else:  # pragma: no cover
        raise AssertionError("a broken TOML must raise WorkersError")

# 8. Both descriptor-shortage errnos use the bounded retry policy.
for code in (errno.ENFILE, errno.EMFILE):
    failure = OSError(code, "injected descriptor pressure")
    operation = mock.Mock(side_effect=[failure, failure, "recovered"])
    with mock.patch("tauceti_worker.runtime_status.time.sleep") as sleep:
        assert retry_fd_pressure(operation) == "recovered"
    assert operation.call_count == 3 and sleep.call_count == 2

# 9. A failure after the temp file is written must preserve the old record and clean up the temp.
with tempfile.TemporaryDirectory(prefix="fd-pressure-replace-") as raw:
    target = Path(raw) / "worker.json"
    atomic_json(target, {"round_work": {"token": "owned"}})
    old = target.read_text()
    real_replace = wm.os.replace
    replacements: list[Path] = []

    def flaky_replace(src, dst):
        replacements.append(Path(src))
        assert target.read_text() == old, "a failed replace must leave the previous record intact"
        if len(replacements) <= 2:
            raise ENFILE
        return real_replace(src, dst)

    with (
        mock.patch("tauceti_worker.runtime_status.os.replace", flaky_replace),
        mock.patch("tauceti_worker.runtime_status.time.sleep"),
    ):
        atomic_json(target, {"alive": True})
    assert read_json(target) == {"alive": True}
    assert len(set(replacements)) == 3 and all(not p.exists() for p in replacements)

    old = target.read_text()
    with (
        mock.patch("tauceti_worker.runtime_status.os.replace", side_effect=ENFILE),
        mock.patch("tauceti_worker.runtime_status.time.sleep"),
    ):
        try:
            atomic_json(target, {"alive": False})
        except OSError as exc:
            assert exc.errno == errno.ENFILE
        else:
            raise AssertionError("sustained replace failure must surface")
    assert target.read_text() == old
    assert list(Path(raw).iterdir()) == [target], "failed attempts must leave no temp files"

# 10. An unreadable status must not erase existing ownership when update_status merges changes.
with tempfile.TemporaryDirectory(prefix="fd-pressure-preserve-") as raw:
    target = Path(raw) / "worker.json"
    atomic_json(target, {"round_work": {"token": "owned"}})
    old = target.read_text()
    with (
        mock.patch.object(Path, "read_text", side_effect=ENFILE),
        mock.patch("tauceti_worker.runtime_status.time.sleep"),
    ):
        try:
            update_status(target, alive=True)
        except OSError as exc:
            assert exc.errno == errno.ENFILE
        else:
            raise AssertionError("an unreadable ownership record must not be replaced")
    assert target.read_text() == old

# 11. Failed retry batches must not be reported as recovered/absorbed; unchanged counters are silent.
output = io.StringIO()
with mock.patch.object(wm, "_fd_pressure_logged", 0), contextlib.redirect_stderr(output):
    wm.publish_fd_pressure()
    wm.publish_fd_pressure()
assert len(output.getvalue().splitlines()) == 1
assert "retries=" in output.getvalue() and "absorbed" not in output.getvalue()

# 12. Exercise the actual manager loop through startup pressure, recovery, pressure with a last-good
#     generation, invalid config, renewed pressure, and recovery. Only the I/O boundaries are mocked.
with tempfile.TemporaryDirectory(prefix="fd-pressure-manager-", dir="/tmp") as raw:
    root = Path(raw)
    spec = wm.WorkerSpec(id="retained")
    (root / "state").mkdir()
    (root / "state" / "retained.json").write_text("{}")
    replies: list[dict] = []
    server = mock.Mock()
    output = io.StringIO()

    def serve(server, handle):
        reply = handle({"action": "ping"})
        assert handle({"action": "apply"}) == reply
        replies.append(reply)
        if len(replies) == 6:
            handle({"action": "shutdown"})

    with (
        mock.patch.object(wm, "workers_runtime_dir", return_value=root / "run"),
        mock.patch.object(wm, "workers_state_dir", return_value=root / "state"),
        mock.patch.object(wm, "_bind_socket", return_value=server),
        mock.patch.object(wm.select, "select", return_value=([server], [], [])),
        mock.patch.object(wm, "_serve_one", side_effect=serve),
        mock.patch.object(
            wm,
            "load_worker_specs",
            side_effect=[
                WorkersTransientError("pressure"),
                [spec],
                WorkersTransientError("pressure"),
                WorkersError("broken config"),
                WorkersTransientError("pressure"),
                [spec],
            ],
        ),
        mock.patch.object(wm, "runner_status", return_value={"alive": True, "spec_hash": spec.fingerprint()}) as status,
        mock.patch.object(wm, "_launch_runner") as launch,
        mock.patch.object(wm, "_stop_runner") as stop,
        contextlib.redirect_stderr(output),
    ):
        assert wm.run_manager(root / "workers.toml", interval=0.1) == 0
    assert [reply["transient_error"] for reply in replies] == ["pressure", None, "pressure", None, "pressure", None]
    assert [reply["error"] for reply in replies] == [None, None, None, "broken config", "broken config", None]
    assert output.getvalue().count("transient host condition") == 3, (
        "new pressure after a config fault must be reported"
    )
    assert [call.args[0] for call in status.call_args_list] == ["retained"] * 5
    launch.assert_not_called()
    stop.assert_not_called()

print("fd pressure retry checks passed")
