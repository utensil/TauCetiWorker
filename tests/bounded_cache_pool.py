#!/usr/bin/env python3
"""Optional cache exchange must not hold authoring behind an unbounded filesystem scan."""

import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tauceti_worker import agents, build_caches


class BoundedCachePool(unittest.TestCase):
    def test_real_exchange_keeps_complete_files_and_existing_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            private, pool = Path(tmp) / "private", Path(tmp) / "pool"
            private.mkdir()
            pool.mkdir()
            (private / "one.ltar").write_bytes(b"complete")
            (private / "partial.part").write_bytes(b"unfinished")
            (pool / "two.ltar").write_bytes(b"other")
            (private / "same.ltar").write_bytes(b"private")
            (pool / "same.ltar").write_bytes(b"pool")
            self.assertEqual(build_caches.sync_pool_bounded(private, pool), (1, 1))
            self.assertTrue(os.path.samefile(private / "one.ltar", pool / "one.ltar"))
            self.assertEqual((private / "same.ltar").read_bytes(), b"private")
            self.assertEqual((pool / "same.ltar").read_bytes(), b"pool")
            self.assertFalse((pool / "partial.part").exists())

    def test_timeout_reaps_child_and_preserves_completed_atomic_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            private, pool = Path(tmp) / "private", Path(tmp) / "pool"
            private.mkdir()
            source = private / "one.ltar"
            source.write_bytes(b"complete")
            source.chmod(0o444)
            pidfile = Path(tmp) / "child.pid"
            real_run = subprocess.run

            def stall_after_link(command, **kwargs):
                command = list(command)
                command[2] = (
                    "import os, time\n"
                    "from pathlib import Path\n"
                    "from tauceti_worker import build_caches as c\n"
                    "original_link = c.os.link\n"
                    "def slow_link(source, target):\n"
                    "    original_link(source, target)\n"
                    f"    Path({str(pidfile)!r}).write_text(str(os.getpid()))\n"
                    "    time.sleep(30)\n"
                    "c.os.link = slow_link\n" + command[2]
                )
                return real_run(command, **kwargs)

            with patch.object(build_caches.subprocess, "run", side_effect=stall_after_link):
                with self.assertRaisesRegex(TimeoutError, "private caches retained"):
                    build_caches.sync_pool_bounded(private, pool, timeout_s=2)
            pid = int(pidfile.read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
            self.assertTrue(os.path.samefile(source, pool / "one.ltar"))
            self.assertEqual(source.read_bytes(), b"complete")
            self.assertEqual(source.stat().st_mode & 0o777, 0o444)
            self.assertEqual(build_caches.sync_pool_bounded(private, pool), (0, 0))

    def test_native_callers_continue_after_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = types.SimpleNamespace(data_home=root / "home", state=root / "state")
            marker = config.state / "cache" / "lake-cache-toolchain.json"
            marker.parent.mkdir(parents=True)
            marker.write_text(json.dumps({"sha256": "a" * 64}))
            env = {
                "MATHLIB_CACHE_DIR": str(root / "private-mathlib"),
                "TAUCETI_MATHLIB_POOL": str(root / "mathlib-pool"),
                "LAKE_CACHE_DIR": str(root / "private-lake"),
                "TAUCETI_LAKE_POOL": str(root / "lake-pool"),
            }
            with (
                patch.dict(os.environ, env),
                patch.object(build_caches, "sync_pool_bounded", side_effect=TimeoutError("test timeout")) as exchange,
                patch.object(agents, "log") as log,
            ):
                agents.sync_mathlib_pool(config)
                agents.sync_lake_pool(config)
            self.assertEqual(exchange.call_count, 2)
            self.assertTrue(exchange.call_args.kwargs["lake"])
            self.assertEqual(log.call_count, 2)
            self.assertTrue(all("skipped" in call.args[0] for call in log.call_args_list))


if __name__ == "__main__":
    unittest.main()
