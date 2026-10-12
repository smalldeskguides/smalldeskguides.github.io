from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from watch_pages import PAGES_WORKFLOW, SITE, REPO, assess, get_bytes, public_files, watch

SHA = "a" * 40
NOW = datetime(2026, 10, 12, 22, 0, tzinfo=timezone.utc)


def build(status="building", seconds=0):
    return {"commit": SHA, "status": status, "created_at": (NOW - timedelta(seconds=seconds)).isoformat(),
            "updated_at": NOW.isoformat(), "error": {"message": None}}


def run(status="in_progress", conclusion=None):
    return {"id": 123, "workflow_id": PAGES_WORKFLOW, "head_sha": SHA,
            "created_at": NOW.isoformat(), "run_attempt": 1, "status": status, "conclusion": conclusion}


def public_success():
    record = run("completed", "success")
    record["run_started_at"] = NOW.isoformat()
    record["deployment"] = {"id": 321, "sha": SHA, "environment": "github-pages",
        "created_at": NOW.isoformat(), "status": {"id": 789, "state": "success",
        "environment": "github-pages", "environment_url": SITE, "created_at": NOW.isoformat(),
        "log_url": f"https://github.com/{REPO}/actions/runs/123/job/456"}}
    record["deployJob"] = {"id": 456, "name": "deploy", "head_sha": SHA,
        "status": "completed", "conclusion": "success", "steps": [
        {"name": "Deploy to GitHub Pages", "status": "completed", "conclusion": "success"}]}
    return record


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

    def test_public_native_deployment_requires_bound_job_and_receipt(self):
        report = assess(SHA, SHA, None, [public_success()], NOW)
        self.assertEqual(report["state"], "awaiting_served_bytes")
        self.assertEqual(report["nativeEvidenceKind"], "public_pages_workflow_and_deployment")
        self.assertEqual(report["deploymentId"], 321)
        self.assertIsNone(report["buildStatus"])
        bare = run("completed", "success")
        self.assertEqual(assess(SHA, SHA, None, [bare], NOW, expired=True)["state"], "missing_deployment_receipt")

    def test_public_native_receipt_mismatches_cannot_verify(self):
        changes = [("sha", "b" * 40), ("environment", "other"),
                   ("created_at", (NOW - timedelta(days=1)).isoformat())]
        for key, value in changes:
            record = public_success(); record["deployment"][key] = value
            with self.subTest(key=key):
                self.assertNotEqual(assess(SHA, SHA, None, [record], NOW, expired=True)["state"], "awaiting_served_bytes")
        for key, value in [("log_url", f"https://github.com/{REPO}/actions/runs/999/job/456"),
                           ("environment_url", "https://example.com/"), ("environment", "other"),
                           ("created_at", (NOW + timedelta(days=1)).isoformat())]:
            record = public_success(); record["deployment"]["status"][key] = value
            with self.subTest(statusKey=key):
                self.assertNotEqual(assess(SHA, SHA, None, [record], NOW, expired=True)["state"], "awaiting_served_bytes")
        record = public_success(); record["deployJob"]["steps"][0]["conclusion"] = "failure"
        self.assertNotEqual(assess(SHA, SHA, None, [record], NOW, expired=True)["state"], "awaiting_served_bytes")

    def test_manual_retry_fence_excludes_prior_same_sha_success(self):
        record = public_success()
        later = NOW + timedelta(minutes=5)
        self.assertEqual(assess(SHA, SHA, None, [record], later, expired=True, not_before=later)["state"], "missing_run")
        record["run_started_at"] = later.isoformat(); record["run_attempt"] = 2
        # Even a fresh attempt cannot reuse an earlier deployment receipt.
        self.assertEqual(assess(SHA, SHA, None, [record], later, expired=True, not_before=later)["state"],
                         "missing_deployment_receipt")

    def test_native_deployment_identifiers_are_positive_integers(self):
        for kind, value in [("deployment", 0), ("deployment", -1), ("status", None),
                            ("job", 0), ("job", -1), ("job", "456"), ("job", True)]:
            record = public_success()
            target = record["deployment"] if kind == "deployment" else record["deployment"]["status"] if kind == "status" else record["deployJob"]
            target["id"] = value
            if kind == "job":
                record["deployment"]["status"]["log_url"] = f"https://github.com/{REPO}/actions/runs/123/job/{value}"
            with self.subTest(kind=kind, value=value), self.assertRaises(ValueError):
                assess(SHA, SHA, None, [record], NOW)

    def test_stale_serving_wait_has_one_native_observation_per_interval(self):
        elapsed = [0]
        calls = []
        def native(*_): calls.append(elapsed[0]); return SHA, None, [public_success()]
        def sleep(seconds): elapsed[0] += seconds
        report = watch(SHA, {"index.html": "c" * 64}, timeout=600, interval=60,
            native=native, readback=lambda *_: [{"path": "index.html", "exactBytes": False}],
            clock=lambda: elapsed[0], sleep=sleep, utc=lambda: NOW + timedelta(seconds=elapsed[0]),
            emit=lambda _: None)
        self.assertEqual(calls, list(range(0, 600, 60)))
        self.assertFalse(report["verified"])
        self.assertEqual(len(calls), 10)
        self.assertEqual(report["state"], "serving_stale")
        self.assertIn("WATCH_WINDOW_EXCEEDED", report["codes"])

    def test_deadline_exhausted_readback_cannot_skip_ownership_and_pass(self):
        elapsed = [0]
        calls = []
        def native(*_): calls.append(elapsed[0]); return SHA, build("built"), [run("completed", "success")]
        def readback(*_): elapsed[0] = 60; return [{"path": "index.html", "exactBytes": True}]
        report = watch(SHA, {"index.html": "c" * 64}, timeout=60, native=native, readback=readback,
                       clock=lambda: elapsed[0], utc=lambda: NOW, emit=lambda _: None)
        self.assertEqual(calls, [0])
        self.assertFalse(report["verified"])
        self.assertEqual(report["state"], "observation_error")

    def test_http_body_completing_after_deadline_is_rejected(self):
        elapsed = [0]
        class Response:
            status = 200
            url = SITE
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read1(self, _): elapsed[0] = 20; return b"late"
        class Opener:
            def open(self, request, **_):
                self.request = request
                return Response()
        opener = Opener()
        with patch("watch_pages.time.monotonic", side_effect=lambda: elapsed[0]), \
             patch("watch_pages.build_opener", return_value=opener), self.assertRaises(TimeoutError):
            get_bytes(SITE, 10)
        self.assertEqual(opener.request.get_method(), "GET")
        self.assertNotIn("Authorization", opener.request.headers)


if __name__ == "__main__": unittest.main()
