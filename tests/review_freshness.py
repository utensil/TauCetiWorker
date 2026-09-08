#!/usr/bin/env python3
"""Custom review forks must be current before either execution path has side effects."""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tauceti_worker import review_freshness, work_units
from tauceti_worker.config import NoProgress

FORK = "example/TauCetiReview"
UPSTREAM = "a" * 40
CURRENT = "b" * 40


def response(payload, returncode=0):
    return subprocess.CompletedProcess([], returncode, json.dumps(payload), "")


def passing():
    return [
        response({"sha": UPSTREAM}),
        response({"sha": CURRENT}),
        response({"status": "ahead", "behind_by": 0, "merge_base_commit": {"sha": UPSTREAM}}),
        response({"sha": UPSTREAM}),
        response({"sha": CURRENT}),
    ]


class ReviewFreshnessTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def check_blocked(self, replies, ref=CURRENT):
        with patch.object(review_freshness, "gh_run", side_effect=replies):
            with self.assertRaises(NoProgress):
                review_freshness.verify_review_source(FORK, ref)

    def test_current_fork_passes_with_exact_comparison(self):
        with patch.object(review_freshness, "gh_run", side_effect=passing()) as run:
            review_freshness.verify_review_source(FORK, CURRENT)
        self.assertEqual(run.call_count, 5)
        self.assertEqual(run.call_args_list[2].args[0][-1], f"repos/{FORK}/compare/{UPSTREAM}...{CURRENT}")
        self.assertTrue(all(call.kwargs["max_wait"] == 0 for call in run.call_args_list))

    def test_identical_upstream_passes(self):
        replies = passing()
        replies[2] = response({"status": "identical", "behind_by": 0, "merge_base_commit": {"sha": UPSTREAM}})
        with patch.object(review_freshness, "gh_run", side_effect=replies):
            review_freshness.verify_review_source(FORK, CURRENT)

    def test_stale_pin_is_refused_even_when_fork_is_synced(self):
        self.check_blocked(passing(), "c" * 40)

    def test_unsynced_fork_and_invalid_comparisons_are_refused(self):
        comparisons = [
            {"status": "diverged", "behind_by": 4, "merge_base_commit": {"sha": "c" * 40}},
            {"status": "behind", "behind_by": 1, "merge_base_commit": {"sha": CURRENT}},
            {"status": "ahead", "behind_by": 0, "merge_base_commit": {"sha": "c" * 40}},
            {"status": "ahead", "behind_by": False, "merge_base_commit": {"sha": UPSTREAM}},
            {"status": "ahead", "behind_by": 0, "merge_base_commit": []},
            {"status": [], "behind_by": 0, "merge_base_commit": {"sha": UPSTREAM}},
            {},
        ]
        for comparison in comparisons:
            with self.subTest(comparison=comparison):
                replies = passing()
                replies[2] = response(comparison)
                self.check_blocked(replies)

    def test_failed_reads_and_malformed_heads_fail_closed(self):
        failures = [
            response({}, 1),
            response([]),
            response({"sha": "main"}),
            response({"sha": None}),
            subprocess.CompletedProcess([], 0, "not json", ""),
            OSError("transport"),
            subprocess.TimeoutExpired("gh", 30),
        ]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                self.check_blocked([failure])

    def test_failed_final_read_and_branch_advances_fail_closed(self):
        for index in (3, 4):
            for result in (response({"sha": "d" * 40}), response({}, 1)):
                replies = passing()
                replies[index] = result
                self.check_blocked(replies)

    def test_tracking_branch_is_explicit_and_url_encoded(self):
        with patch.dict(os.environ, {"TAUCETI_REVIEW_ENGINE_BRANCH": "release/dev"}):
            with patch.object(review_freshness, "gh_run", side_effect=passing()) as run:
                review_freshness.verify_review_source(FORK, CURRENT)
        self.assertEqual(run.call_args_list[1].args[0][-1], f"repos/{FORK}/commits/release%2Fdev")

    def test_invalid_branch_and_local_overrides_do_not_reach_network(self):
        settings = [
            {"TAUCETI_REVIEW_ENGINE_BRANCH": "../main"},
            {"TAUCETI_REVIEW_ENGINE_BRANCH": ""},
            {"TAUCETI_REVIEW_ENGINE_DIR": "/tmp/engine"},
            {"TAUCETI_REVIEW_DIR": "/tmp/engine"},
        ]
        for setting in settings:
            with patch.dict(os.environ, setting), patch.object(review_freshness, "gh_run") as run:
                with self.assertRaises(NoProgress):
                    review_freshness.verify_review_source(FORK, CURRENT)
                run.assert_not_called()

    def test_default_upstream_flow_is_unchanged(self):
        with patch.object(review_freshness, "gh_run") as run:
            review_freshness.verify_review_source(review_freshness.REVIEW, "")
        run.assert_not_called()

    def test_guard_precedes_all_side_effects_on_both_paths(self):
        for bubble in (False, True):
            worker = Mock()
            with (
                patch.object(work_units, "_review_engine_source", return_value=(FORK, CURRENT)),
                patch.object(review_freshness, "gh_run", return_value=response({}, 1)),
                patch.object(work_units, "run_to_logfile") as host,
                patch.object(work_units, "review_in_bubble") as sandbox,
                patch.object(work_units, "_sync_review_outbox") as archive,
            ):
                with self.assertRaises(NoProgress):
                    work_units.do_review(worker, Mock(), Mock(), Mock(), bubble)
            self.assertEqual(worker.mock_calls, [])
            host.assert_not_called()
            sandbox.assert_not_called()
            archive.assert_not_called()


if __name__ == "__main__":
    unittest.main()
