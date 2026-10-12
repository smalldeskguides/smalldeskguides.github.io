from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from watch_pages import PAGES_WORKFLOW, assess, public_files, watch

SHA = "a" * 40
NOW = datetime(2026, 10, 12, 22, 0, tzinfo=timezone.utc)


def build(status="building", seconds=0):
    return {"commit": SHA, "status": status, "created_at": (NOW - timedelta(seconds=seconds)).isoformat(),
            "updated_at": NOW.isoformat(), "error": {"message": None}}


def run(status="in_progress", conclusion=None):
    return {"id": 123, "workflow_id": PAGES_WORKFLOW, "head_sha": SHA,
            "created_at": NOW.isoformat(), "run_attempt": 1, "status": status, "conclusion": conclusion}


class PagesEvidence(unittest.TestCase):
    def test_stuck_build_uses_occurrence_not_refreshed_updated_time(self):
        report = assess(SHA, SHA, build(seconds=601), [run()], NOW)
        self.assertEqual(report["state"], "stuck_build")
        self.assertFalse(report["verified"])

    def test_missing_pages_run_is_distinct_from_source_ci_success(self):
        source_run = run("completed", "success")
        source_run["workflow_id"] = 382570042
        report = assess(SHA, SHA, build(seconds=601), [source_run], NOW)
        self.assertEqual(report["state"], "missing_run")
        self.assertIsNone(report["pagesRunId"])

    def test_native_and_workflow_failures_are_terminal(self):
        failed = build("errored")
        failed["error"]["message"] = "build failed"
        self.assertEqual(assess(SHA, SHA, failed, [], NOW)["state"], "deployment_failed")
        for conclusion in ("failure", "cancelled", "timed_out", "action_required"):
            with self.subTest(conclusion=conclusion):
                self.assertEqual(assess(SHA, SHA, build(), [run("completed", conclusion)], NOW)["state"],
                                 "deployment_failed")

    def test_wrong_native_revision_never_passes(self):
        previous = build("built")
        previous["commit"] = "b" * 40
        self.assertEqual(assess(SHA, SHA, previous, [], NOW, expired=True)["state"], "stale_revision")

    def test_new_main_supersedes_old_watch_and_is_not_retryable(self):
        report = watch(SHA, {"index.html": "c" * 64}, once=True,
                       native=lambda *_: ("b" * 40, build(), []), utc=lambda: NOW, emit=lambda _: None)
        self.assertEqual(report["state"], "superseded")
        self.assertFalse(report["recovery"]["eligibleForReview"])
        self.assertFalse(report["automaticRetry"])

    def test_earlier_same_sha_success_cannot_certify_new_attempt(self):
        old = run("completed", "success")
        old["created_at"] = (NOW - timedelta(days=1)).isoformat()
        self.assertEqual(assess(SHA, SHA, build("built"), [old], NOW, expired=True)["state"], "missing_run")

    def test_malformed_or_future_occurrences_rejected(self):
        for value in ("invalid", NOW.replace(tzinfo=None).isoformat(), (NOW + timedelta(minutes=5)).isoformat()):
            invalid = build()
            invalid["created_at"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                assess(SHA, SHA, invalid, [run()], NOW)
        future = run("completed", "success")
        future["created_at"] = (NOW + timedelta(minutes=5)).isoformat()
        with self.assertRaises(ValueError): assess(SHA, SHA, build("built"), [future], NOW)

    def test_native_success_alone_requires_public_bytes(self):
        report = assess(SHA, SHA, build("built"), [run("completed", "success")], NOW)
        self.assertEqual(report["state"], "awaiting_served_bytes")
        self.assertFalse(report["verified"])

    def test_exact_serving_verifies_only_bound_revision(self):
        report = watch(SHA, {"index.html": "c" * 64}, once=True,
            native=lambda *_: (SHA, build("built"), [run("completed", "success")]),
            readback=lambda *_: [{"path": "index.html", "exactBytes": True}], utc=lambda: NOW, emit=lambda _: None)
        self.assertTrue(report["verified"])
        self.assertEqual(report["publicFileMatches"], 1)
        self.assertEqual(report["nativeMutations"], 0)

    def test_new_head_during_serving_readback_cannot_pass(self):
        replies = iter([(SHA, build("built"), [run("completed", "success")]),
                        ("b" * 40, build("built"), [run("completed", "success")])])
        report = watch(SHA, {"index.html": "c" * 64}, once=True, native=lambda *_: next(replies),
            readback=lambda *_: [{"path": "index.html", "exactBytes": True}], utc=lambda: NOW, emit=lambda _: None)
        self.assertEqual(report["state"], "superseded")
        self.assertFalse(report["verified"])

    def test_same_sha_receipt_uses_latest_observed_attempt(self):
        first = run("completed", "success")
        second = {**first, "id": 456, "run_attempt": 2}
        replies = iter([(SHA, build("built"), [first]), (SHA, build("built"), [second])])
        report = watch(SHA, {"index.html": "c" * 64}, once=True, native=lambda *_: next(replies),
            readback=lambda *_: [{"path": "index.html", "exactBytes": True}], utc=lambda: NOW, emit=lambda _: None)
        self.assertTrue(report["verified"])
        self.assertEqual(report["pagesRunId"], 456)

    def test_wrong_or_missing_public_bytes_are_stale_serving(self):
        for checks in ([], [{"path": "index.html", "exactBytes": False}],
                       [{"path": "other.html", "exactBytes": True}]):
            with self.subTest(checks=checks):
                report = watch(SHA, {"index.html": "c" * 64}, once=True,
                    native=lambda *_: (SHA, build("built"), [run("completed", "success")]),
                    readback=lambda *_: checks, utc=lambda: NOW, emit=lambda _: None)
                self.assertEqual(report["state"], "serving_stale")
                self.assertFalse(report["verified"])

    def test_deadline_is_bounded_and_performs_no_final_network_call(self):
        elapsed = [0]
        calls = []
        def sleep(seconds): elapsed[0] += seconds
        def native(*_): calls.append(elapsed[0]); return SHA, build(), [run()]
        report = watch(SHA, {"index.html": "c" * 64}, timeout=120, interval=60,
            native=native, clock=lambda: elapsed[0], sleep=sleep,
            utc=lambda: NOW + timedelta(seconds=elapsed[0]), emit=lambda _: None)
        self.assertEqual(elapsed[0], 120)
        self.assertEqual(calls, [0, 60])
        self.assertEqual(report["state"], "stuck_build")
        self.assertFalse(report["automaticRetry"])

    def test_read_errors_stop_without_retrying_or_claiming_success(self):
        calls = []
        def fail(*_): calls.append(1); raise PermissionError("denied")
        report = watch(SHA, {"index.html": "c" * 64}, native=fail, utc=lambda: NOW, emit=lambda _: None)
        self.assertEqual(len(calls), 1)
        self.assertEqual(report["state"], "observation_error")
        self.assertFalse(report["recovery"]["eligibleForReview"])

    def test_current_public_inventory_includes_all_five_notices(self):
        files = public_files(ROOT)
        self.assertEqual(len(files), 26)
        self.assertIn("assets/fonts/provenance.json", files)
        self.assertEqual(sum(name.startswith("assets/fonts/licenses/") for name in files), 4)


if __name__ == "__main__": unittest.main()
