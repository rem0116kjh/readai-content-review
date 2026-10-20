"""Polling must skip unchanged payloads while never hiding new jobs/results."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.request
from unittest.mock import patch

from test_review import fixture, finish
import app
import criteria
import review


class PollTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.manager = app.ReviewManager(self.tmp.name, downloader=lambda *a: fixture())

    def run_rules(self):
        self.manager.start({"grades": [3], "ai": False})
        return finish(self.manager)

    def test_unchanged_poll_skips_copy_and_payload(self):
        job = self.run_rules()
        with patch.object(app.copy, "deepcopy", side_effect=AssertionError("Unchanged polling must not copy reports")):
            self.assertEqual(self.manager.poll(job["id"], job["revision"]), {"job": None, "unchanged": True})

    def test_changes_and_different_job_are_delivered(self):
        first = self.run_rules()
        self.manager.update(message="상태 변경")
        changed = self.manager.poll(first["id"], first["revision"])["job"]
        self.assertGreater(changed["revision"], first["revision"])
        self.assertEqual(changed["message"], "상태 변경")
        changed["sets"].clear()
        self.assertTrue(self.manager.current()["sets"])
        second = self.run_rules()
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(self.manager.poll(first["id"], second["revision"])["job"]["id"], second["id"])

    def test_each_new_click_downloads_again_and_rules_save_once(self):
        with patch.object(self.manager, "downloader", wraps=self.manager.downloader) as download, patch.object(self.manager, "persist", wraps=self.manager.persist) as save:
            self.run_rules()
            self.run_rules()
        self.assertEqual(download.call_count, 2)
        self.assertEqual(save.call_count, 2)

    def test_ai_progress_cancel_and_terminal_revision_are_delivered(self):
        entered, release = threading.Event(), threading.Event()
        def wait_ai(snapshot, item, cancel):
            entered.set()
            release.wait(3)
            raise review.Cancelled()
        self.manager.ai_reviewer = wait_ai
        self.manager.start({"grades": [3], "ai": True})
        try:
            self.assertTrue(entered.wait(3))
            running = self.manager.current()
            self.assertEqual(running["sets"][0]["ai_status"], "running")
            self.assertTrue(self.manager.poll(running["id"], running["revision"])["unchanged"])
            self.manager.cancel()
            cancelling = self.manager.poll(running["id"], running["revision"])["job"]
            self.assertEqual(cancelling["status"], "cancelling")
            release.set()
            final = finish(self.manager)
            self.assertGreater(final["revision"], cancelling["revision"])
            self.assertEqual(final["sets"][0]["ai_status"], "cancelled")
            stored = json.loads((Path(self.tmp.name)/"reports"/f"{final['id']}.json").read_text())
            self.assertEqual(stored, final)
        finally:
            release.set()
            self.manager.worker.join(5)

    def test_http_supports_old_clients_and_revision_cursor(self):
        job = self.run_rules()
        server = app.make_server(0, self.manager)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = f"http://127.0.0.1:{server.server_port}/api/status"
        try:
            with urllib.request.urlopen(base) as r:
                self.assertEqual(json.load(r)["job"]["id"], job["id"])
            with urllib.request.urlopen(f"{base}?after_id={job['id']}&after_revision={job['revision']}") as r:
                self.assertEqual(json.load(r), {"job": None, "unchanged": True})
            with urllib.request.urlopen(f"{base}?after_id={job['id']}&after_revision=invalid") as r:
                self.assertEqual(json.load(r)["job"]["id"], job["id"])
        finally:
            server.shutdown()
            server.server_close()
            worker.join(3)

    def test_compacted_prompt_keeps_exact_unicode_quotes_and_newlines(self):
        snapshot = fixture()
        row = snapshot["sheets"]["6_지문원문"]["rows"][1]
        row["문장"] = '“나무, 그늘”이라고 썼다.\n그대로 보존한다.'
        item = review.catalog(snapshot)[0]
        context = json.loads(review.ai_prompt(snapshot, item).split("[검토 데이터]\n", 1)[1])
        own, related = criteria.inputs(snapshot, item["id"])
        self.assertEqual(context["sheets"], own)
        self.assertEqual(context["related_sets"], related)
        self.assertEqual(context["sheets"]["6_지문원문"][1]["문장"], row["문장"])


if __name__ == "__main__":
    unittest.main()
