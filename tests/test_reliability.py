"""Offline regression coverage for the desktop runner and API boundaries."""
import copy
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

import app
import criteria
import review
from test_review import fixture, finish


class InputTests(unittest.TestCase):
    def test_unavailable_ai_is_checked_once_and_keeps_rule_results(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(review, "codex_status", return_value={"ready": False, "message": "로그인 필요"}) as status, \
             patch.object(review, "ai_review_pass", side_effect=AssertionError("No repeated failing calls")):
            manager = app.ReviewManager(folder, downloader=lambda *a: fixture())
            manager.start({"ai": True})
            result = finish(manager)
            self.assertEqual(result["status"], "partial")
            self.assertEqual(result["sets"][0]["rule_status"], "complete")
            self.assertEqual(result["sets"][0]["ai_status"], "failed")
            self.assertEqual(status.call_count, 1)

    def test_preparation_preserves_inputs_prompts_rules_and_serialization(self):
        raw = fixture()
        prepared = criteria.prepare(raw)
        self.assertEqual(json.dumps(raw, ensure_ascii=False), json.dumps(prepared, ensure_ascii=False))
        for item in review.catalog(raw):
            self.assertEqual(review.rule_review(raw, item), review.rule_review(prepared, item))
            for direction in ("forward", "reverse"):
                self.assertEqual(review.ai_prompt(raw, item, direction), review.ai_prompt(prepared, item, direction))
            original = criteria.inputs(raw, item["id"])
            changed = criteria.inputs(prepared, item["id"])
            changed[0]["3_어휘"].clear()
            self.assertEqual(criteria.inputs(prepared, item["id"]), original)
        # A later run must see edits; it must not reuse the old run's context.
        raw["sheets"]["3_어휘"]["rows"][0]["낱말"] = "새 낱말"
        self.assertNotEqual(criteria.inputs(criteria.prepare(raw), item["id"]), original)
        self.assertEqual(criteria.inputs(prepared, item["id"]), original)

    def test_options_are_normalized_and_do_not_retain_caller_reference(self):
        options = {"grades": [6, 3, 3], "ai": False}
        result = app.validate_options(options)
        options["grades"].clear()
        self.assertEqual(result["grades"], [3, 6])
        self.assertIsNone(result["saved_source"])
        self.assertFalse(result["include_sample"])

    def test_invalid_options_fail_before_job_creation(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = app.ReviewManager(folder)
            for invalid in ([], {"grades": [True]}, {"grades": []}, {"ai": "yes"},
                            {"saved_source": []}, {"saved_source": "unknown"},
                            {"sheet_url": {}}, {"secret": "must-not-be-persisted"},
                            {"set_id": 7}):
                with self.subTest(invalid=invalid):
                    with self.assertRaises(ValueError):
                        manager.start(invalid)
                    self.assertIsNone(manager.current())
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_atomic_save_keeps_old_report_on_failure_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "result.json"
            app.atomic_write(path, "old")
            with patch.object(Path, "replace", side_effect=OSError("disk failure")):
                with self.assertRaises(OSError):
                    app.atomic_write(path, "new")
            self.assertEqual(path.read_text(), "old")
            self.assertEqual(list(Path(folder).iterdir()), [path])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_ui_projection_preserves_findings_and_export_history(self):
        report = {"id": "a"*24, "issues": [{"quote": "원문"}], "sets": [
            {"id": "3-1-1-01", "ai_passes": {
                "forward": {"status": "complete", "result": {"issues": ["원문"]}},
                "reverse": {"status": "running"}}}]}
        original = copy.deepcopy(report)
        visible = app.public_report(report)
        self.assertEqual(visible["issues"], report["issues"])
        self.assertEqual(visible["sets"][0]["ai_passes"]["forward"], {"status": "complete"})
        visible["issues"].clear()
        self.assertEqual(report, original)
        with tempfile.TemporaryDirectory() as folder:
            manager = app.ReviewManager(folder)
            manager.job = report
            with patch.object(review, "markdown_report", return_value="report"):
                manager.persist()
            saved = json.loads((Path(folder)/"reports"/f"{report['id']}.json").read_text())
            self.assertEqual(saved, original)

    def test_restored_interrupted_stage_does_not_claim_it_is_still_running(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"reports"/("a"*24 + ".json")
            source = {"status": "running", "sets": [{"ai_status": "running",
                      "ai_passes": {"forward": {"status": "complete"}, "reverse": {"status": "running"}}}]}
            app.atomic_write(path, json.dumps(source))
            result = app.ReviewManager(folder).latest()
            self.assertEqual(result["status"], "interrupted")
            self.assertEqual(result["sets"][0]["ai_passes"]["reverse"]["status"], "cancelled")
            self.assertEqual(json.loads(path.read_text()), source)

    def test_stale_cancel_cannot_stop_new_run(self):
        started = threading.Event()
        def downloader(progress, cancel, sid):
            started.set()
            cancel.wait(3)
            raise review.Cancelled()
        with tempfile.TemporaryDirectory() as folder:
            manager = app.ReviewManager(folder, downloader=downloader)
            current = manager.start({"ai": False})
            try:
                self.assertTrue(started.wait(2))
                with self.assertRaises(RuntimeError):
                    manager.cancel("0"*24)
                self.assertFalse(manager.cancel_event.is_set())
                self.assertEqual(manager.current()["status"], "running")
            finally:
                manager.cancel(current)
                self.assertEqual(finish(manager)["status"], "cancelled")


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = app.ReviewManager(self.tmp.name, downloader=lambda *a: fixture())
        self.server = app.make_server(0, self.manager)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.manager.cancel()
        if self.manager.worker:
            self.manager.worker.join(3)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        self.tmp.cleanup()

    def request(self, path, data=None, headers=None):
        all_headers = {"Content-Type": "application/json", "X-Review-Token": self.server.token}
        all_headers.update(headers or {})
        req = urllib.request.Request(self.base+path, data=data, headers=all_headers)
        try:
            response = urllib.request.urlopen(req, timeout=3)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, json.load(response)

    def test_get_cross_origin_and_dns_rebinding_are_rejected(self):
        for path in ("/api/bootstrap", "/api/status", "/api/catalog"):
            for headers in ({"Origin": "https://other.example"}, {"Sec-Fetch-Site": "cross-site"},
                            {"Host": "other.example"}):
                with self.subTest(path=path, headers=headers):
                    self.assertEqual(self.request(path, headers=headers)[0], 403)

    def test_invalid_json_and_nonascii_token_return_error_not_disconnect(self):
        self.assertEqual(self.request("/api/review", b"\xff")[0], 400)
        self.assertEqual(self.request("/api/review", b"{")[0], 400)
        self.assertEqual(self.request("/api/review", b"{}", {"X-Review-Token": "\xe9"})[0], 403)

    def test_saved_catalog_retains_snapshot_timestamp(self):
        snapshot = fixture()
        snapshot.update(fetched_at="2020-01-01T00:00:00+09:00", source_mode="snapshot")
        with patch.object(app, "read_saved_source", return_value=snapshot):
            code, result = self.request("/api/catalog?saved_source=example")
        self.assertEqual(code, 200)
        self.assertEqual(result["fetched_at"], snapshot["fetched_at"])
        self.assertEqual(result["source_mode"], "snapshot")

    def test_bootstrap_uses_current_job_and_does_not_serialize_pass_history(self):
        self.manager.job = {"id": "a"*24, "status": "running", "sets": [{"ai_passes": {
            "forward": {"status": "complete", "result": {"issues": ["large history"]}}}}]}
        with patch.object(review, "codex_status", return_value={"ready": True}), \
             patch.object(self.manager, "latest", side_effect=AssertionError("stale disk report")):
            code, result = self.request("/api/bootstrap")
        self.assertEqual(code, 200)
        self.assertEqual(result["last_report"]["status"], "running")
        self.assertNotIn("result", result["last_report"]["sets"][0]["ai_passes"]["forward"])
        self.assertIn("result", self.manager.current()["sets"][0]["ai_passes"]["forward"])

    def test_cancel_requires_explicit_job_id(self):
        self.assertEqual(self.request("/api/cancel", b"{}")[0], 400)
        self.assertEqual(self.request("/api/cancel", json.dumps({"id": "0"*24}).encode())[0], 409)

    def test_compact_poll_and_full_export_have_identical_findings(self):
        self.manager.start({"ai": False})
        report = finish(self.manager)
        self.manager.job["sets"][0]["ai_passes"] = {"forward": {"status": "complete", "result": {"raw": "kept"}}}
        self.manager.persist()
        _, visible = self.request("/api/status")
        _, full = self.request(f"/api/report/{report['id']}.json")
        self.assertEqual(visible["job"]["issues"], full["issues"])
        self.assertNotIn("result", visible["job"]["sets"][0]["ai_passes"]["forward"])
        self.assertEqual(full["sets"][0]["ai_passes"]["forward"]["result"], {"raw": "kept"})

    def test_request_concurrency_is_bounded(self):
        for _ in range(16):
            self.assertTrue(self.server.request_slots.acquire(blocking=False))
        try:
            with urllib.request.urlopen(self.base + "/api/health", timeout=3):
                self.fail("Expected bounded request rejection")
        except urllib.error.HTTPError as error:
            self.assertEqual(error.code, 503)
            error.close()
        finally:
            for _ in range(16):
                self.server.request_slots.release()
        self.assertEqual(self.request("/api/health")[0], 200)


if __name__ == "__main__":
    unittest.main()
