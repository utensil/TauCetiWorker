"""Bounded descriptor retries recover without discarding existing status."""

import errno
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tauceti_worker import runtime_status as status
from tauceti_worker.worker_manager import load_worker_specs


class DescriptorRetries(unittest.TestCase):
    def test_retry_policy(self):
        for code in (errno.ENFILE, errno.EMFILE):
            with self.subTest(errno=code), mock.patch.object(status.time, "sleep") as sleep:
                failure = OSError(code, "injected shortage")
                op = mock.Mock(side_effect=[failure, failure, "recovered"])
                self.assertEqual(status.retry_fd_pressure(op), "recovered")
                self.assertEqual(op.call_count, 3)
                self.assertEqual(sleep.call_args_list, [mock.call(0.05), mock.call(0.1)])

                sleep.reset_mock()
                op = mock.Mock(side_effect=failure)
                with self.assertRaises(OSError) as caught:
                    status.retry_fd_pressure(op)
                self.assertIs(caught.exception, failure)
                self.assertEqual(op.call_count, 4)
                self.assertEqual(sleep.call_args_list, [mock.call(0.05), mock.call(0.1), mock.call(0.2)])

        with mock.patch.object(status.time, "sleep") as sleep:
            op = mock.Mock(side_effect=PermissionError(errno.EACCES, "denied"))
            with self.assertRaises(PermissionError):
                status.retry_fd_pressure(op)
            self.assertEqual(op.call_count, 1)
            sleep.assert_not_called()

    def test_io_recovers(self):
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw) / "status.json"
            config = Path(raw) / "workers.toml"
            config.write_text("version = 1\nworkers = []\n")
            cases = [
                (Path, "open", lambda: status.update_status(target, alive=True)["alive"], True),
                (tempfile, "mkstemp", lambda: status.atomic_json(target, {"alive": True}), None),
                (Path, "read_text", lambda: status.read_json(target, strict=True)["alive"], True),
                (Path, "open", lambda: load_worker_specs(config), []),
            ]
            for owner, name, operation, expected in cases:
                with self.subTest(call=name, expected=expected):
                    original = getattr(owner, name)
                    failed = False

                    def flaky(*args, original=original, **kwargs):
                        nonlocal failed
                        if not failed:
                            failed = True
                            raise OSError(errno.ENFILE, "injected shortage")
                        return original(*args, **kwargs)

                    with (
                        mock.patch.object(owner, name, autospec=True, side_effect=flaky),
                        mock.patch.object(status.time, "sleep") as sleep,
                    ):
                        result = operation()
                    self.assertTrue(failed)
                    sleep.assert_called_once_with(0.05)
                    self.assertEqual(result, expected)
                    self.assertTrue(status.read_json(target)["alive"])

    def test_failed_update_preserves_status(self):
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw) / "status.json"
            status.atomic_json(target, {"round_work": {"token": "owned"}})
            old = target.read_text()
            for owner, name in ((Path, "open"), (Path, "read_text"), (tempfile, "mkstemp")):
                with self.subTest(operation=name), mock.patch.object(status.time, "sleep"):
                    with mock.patch.object(owner, name, side_effect=OSError(errno.EMFILE, "exhausted")) as op:
                        with self.assertRaises(OSError):
                            status.update_status(target, alive=True)
                    self.assertEqual(op.call_count, 4)
                    self.assertEqual(target.read_text(), old)
                    self.assertEqual(list(Path(raw).glob(".status.json.*")), [])


if __name__ == "__main__":
    unittest.main()
