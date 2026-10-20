import asyncio
import copy
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"tests"))
from fastapi.testclient import TestClient
import httpx
from openai import APIStatusError

import app
import review
from openai_reviewer import OpenAIReviewer
from web_server import create_web_app
from test_review import fixture, finish
from test_criteria import answer


class FakeClient:
    def __init__(self, calls, result=None, fail=None, **kwargs):
        self.calls = calls
        self.result = result or answer()
        self.fail = fail
        self.responses = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def create(self, **payload):
        self.calls.append(payload)
        if self.fail:
            raise self.fail
        result = dict(self.result)
        if payload["text"]["format"]["schema"].get("properties", {}).get("rechecks"):
            result["rechecks"] = []
        return SimpleNamespace(status="completed", output_text=json.dumps(result), id="response-test",
                               usage=SimpleNamespace(input_tokens=100, output_tokens=50, total_tokens=150))


class OpenAITests(unittest.TestCase):
    def setUp(self):
        self.snapshot = fixture()
        self.item = review.catalog(self.snapshot)[0]
        self.calls = []

    def adapter(self, **kwargs):
        return OpenAIReviewer("test-only-secret", client_factory=lambda **settings: FakeClient(self.calls, **settings, **kwargs))

    def test_two_passes_schema_validation_and_usage_without_secret_leak(self):
        result = self.adapter()(self.snapshot, self.item, threading.Event())
        self.assertEqual(len(self.calls), 2)
        self.assertIn("rechecks", self.calls[1]["text"]["format"]["schema"]["properties"])
        for call in self.calls:
            self.assertFalse(call["store"])
            self.assertTrue(call["text"]["format"]["strict"])
            self.assertNotIn("tools", call)
        self.assertEqual(result["passes"]["forward"]["usage"]["total_tokens"], 150)
        self.assertNotIn("test-only-secret", json.dumps(result))

    def test_missing_key_never_calls_provider(self):
        reviewer = OpenAIReviewer()
        self.assertFalse(reviewer.status()["ready"])
        with self.assertRaisesRegex(ValueError, "API 키"):
            reviewer(self.snapshot, self.item, threading.Event())

    def test_provider_errors_are_sanitized(self):
        for code in (400, 401, 403, 429, 500):
            fail = APIStatusError("private-account-body", response=httpx.Response(code, request=httpx.Request("POST", "https://api.openai.com/v1/responses")), body={"secret": "private-account-body"})
            with self.subTest(code=code), self.assertRaises(ValueError) as error:
                self.adapter(fail=fail)(self.snapshot, self.item, threading.Event())
            self.assertNotIn("private-account-body", str(error.exception))

    def test_unverified_quotes_are_rejected(self):
        result = answer()
        result["issues"] = [{"sheet": "3_어휘", "row": 2, "field": "낱말", "quote": "없는 원문"}]
        with self.assertRaises(ValueError):
            self.adapter(result=result)(self.snapshot, self.item, threading.Event())

    def test_cancellation_aborts_waiting_client_task(self):
        cancelled = []
        class Waiting(FakeClient):
            async def create(self, **payload):
                try:
                    await asyncio.sleep(30)
                finally:
                    cancelled.append(True)
        event = threading.Event()
        timer = threading.Timer(.05, event.set)
        timer.start()
        try:
            adapter = OpenAIReviewer("test-only", client_factory=lambda **kw: Waiting([], **kw))
            with self.assertRaises(review.Cancelled):
                adapter(self.snapshot, self.item, event)
        finally:
            timer.join()
        self.assertEqual(cancelled, [True])

    def test_incomplete_response_is_not_success(self):
        class Incomplete(FakeClient):
            async def create(self, **payload):
                return SimpleNamespace(status="incomplete", output_text="")
        adapter = OpenAIReviewer("test-only", client_factory=lambda **kw: Incomplete([], **kw))
        with self.assertRaisesRegex(ValueError, "미완료"):
            adapter(self.snapshot, self.item, threading.Event())

    def test_account_failure_stops_other_sets_and_preserves_all_rules(self):
        snapshot = copy.deepcopy(self.snapshot)
        for table in snapshot["sheets"].values():
            extra = copy.deepcopy(table["rows"])
            for row in extra:
                row["세트ID"] = "3-1-1-02"
                row["_row"] += 100
            table["rows"].extend(extra)
        for code in (401, 403, 429):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as folder:
                calls = []
                failure = APIStatusError("private-body", response=httpx.Response(
                    code, request=httpx.Request("POST", "https://api.openai.com/v1/responses")), body={})
                adapter = OpenAIReviewer("test-only", client_factory=lambda **kw: FakeClient(calls, fail=failure, **kw))
                manager = app.ReviewManager(folder, downloader=lambda *args: snapshot, ai_reviewer=adapter,
                                            ai_status=adapter.status, ai_progress=True)
                manager.start({"ai": True})
                result = finish(manager)
                self.assertEqual(len(calls), 1)
                self.assertEqual(result["status"], "partial")
                self.assertEqual(result["total_sets"], 2)
                self.assertTrue(all(s["rule_status"] == "complete" for s in result["sets"]))
                self.assertTrue(all(s["ai_status"] == "failed" for s in result["sets"]))
                self.assertEqual(result["sets"][0]["ai_passes"]["forward"]["status"], "failed")
                self.assertNotIn("ai_passes", result["sets"][1])
                self.assertIn("실행 안 함", result["sets"][1]["ai_error"])
                self.assertEqual(result, json.loads((Path(folder)/"reports"/(result["id"]+".json")).read_text()))


class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.calls = []
        self.adapter = OpenAIReviewer("test-only-secret", client_factory=lambda **kw: FakeClient(self.calls, **kw))
        self.manager = app.ReviewManager(self.tmp.name, downloader=lambda *a: fixture(), ai_reviewer=self.adapter,
                                         ai_status=self.adapter.status, ai_progress=True)
        self.web = create_web_app(self.manager, self.adapter)
        self.client = self.enterContext(TestClient(self.web, base_url="http://127.0.0.1:8770"))
        self.token = self.client.get("/api/bootstrap").json()["token"]

    def post(self, path, data, headers=None):
        return self.client.post(path, json=data, headers={"Origin": "http://127.0.0.1:8770", "X-Review-Token": self.token, **(headers or {})})

    def test_end_to_end_rules_and_ai_and_full_download(self):
        for ai in (False, True):
            response = self.post("/api/review", {"grades": [3], "ai": ai})
            self.assertEqual(response.status_code, 202)
            result = finish(self.manager)
            self.assertEqual(result["status"], "complete")
            saved = self.client.get(f"/api/report/{result['id']}.json").json()
            self.assertEqual(saved, result)
            self.assertNotIn("test-only-secret", json.dumps(saved))
            if ai:
                self.assertEqual(saved["sets"][0]["ai_passes"]["reverse"]["status"], "complete")
                visible = self.client.get("/api/status").json()["job"]
                self.assertNotIn("result", visible["sets"][0]["ai_passes"]["forward"])

    def test_boundary_csrf_origin_and_url_scope(self):
        self.assertEqual(self.client.post("/api/review", json={"ai": False}).status_code, 403)
        self.assertEqual(self.post("/api/review", {}, {"Origin": "https://outside.example"}).status_code, 403)
        self.assertEqual(self.client.get("/api/bootstrap", headers={"Host": "outside.example"}).status_code, 403)
        self.assertEqual(self.client.get("/api/bootstrap", headers={"Sec-Fetch-Site": "cross-site"}).status_code, 403)
        self.assertEqual(self.post("/api/review", {"sheet_url": "file:///secret", "ai": False}).status_code, 400)
        self.assertEqual(self.client.get("/api/report/..%2F.env").status_code, 404)

    def test_bad_json_and_oversize_never_create_job(self):
        headers = {"Content-Type": "application/json", "X-Review-Token": self.token}
        for content, code in ((b"{", 400), (b"\xff", 400), (b"x"*16385, 413), (b"[]", 400)):
            self.assertEqual(self.client.post("/api/review", content=content, headers=headers).status_code, code)
        self.assertIsNone(self.manager.current())

    def test_concurrency_and_stale_cancel(self):
        entered = threading.Event()
        def wait(*args, **kwargs):
            entered.set()
            args[2].wait(3)
            raise review.Cancelled()
        self.manager.ai_reviewer = wait
        first = self.post("/api/review", {"ai": True}).json()["id"]
        self.assertTrue(entered.wait(2))
        self.assertEqual(self.post("/api/review", {"ai": False}).status_code, 409)
        self.assertEqual(self.post("/api/cancel", {"id": "0"*24}).status_code, 409)
        self.assertFalse(self.manager.cancel_event.is_set())
        self.assertEqual(self.post("/api/cancel", {"id": first}).status_code, 200)
        self.assertEqual(finish(self.manager)["status"], "cancelled")

    def test_key_configuration_is_never_exposed(self):
        data = self.client.get("/api/bootstrap").json()
        self.assertEqual(data["ai"]["provider"], "openai")
        self.assertNotIn("test-only-secret", json.dumps(data))
        page = self.client.get("/").text
        self.assertNotIn("현재 Codex 사용량", page)
        self.assertIn("OpenAI API 사용량", page)
        self.assertEqual(self.client.get("/.env").status_code, 404)

    def test_public_origin_is_explicitly_not_a_supported_deployment(self):
        for origin in ("https://public.example", "http://0.0.0.0:8770", "http://127.0.0.1:8770/path"):
            with self.assertRaises(ValueError):
                create_web_app(origin=origin)


class LoginTests(unittest.TestCase):
    def test_login_logout_and_cookie_boundaries(self):
        with tempfile.TemporaryDirectory() as folder:
            web = create_web_app(app.ReviewManager(folder), password="local-test-password")
            with TestClient(web, base_url="http://127.0.0.1:8770") as client:
                self.assertEqual(client.get("/api/bootstrap").status_code, 401)
                self.assertEqual(client.get("/api/report/"+"a"*24+".json").status_code, 401)
                self.assertEqual(client.post("/api/login", json={"password": "wrong"}).status_code, 401)
                response = client.post("/api/login", json={"password": "local-test-password"})
                self.assertEqual(response.status_code, 200)
                self.assertIn("HttpOnly", response.headers["set-cookie"])
                self.assertIn("SameSite=strict", response.headers["set-cookie"])
                token = client.get("/api/bootstrap").json()["token"]
                self.assertEqual(client.post("/api/logout", json={}, headers={"X-Review-Token": token}).status_code, 200)
                self.assertEqual(client.get("/api/bootstrap").status_code, 401)

    def test_login_attempts_are_limited(self):
        with tempfile.TemporaryDirectory() as folder:
            web = create_web_app(app.ReviewManager(folder), password="local-test-password")
            with TestClient(web, base_url="http://127.0.0.1:8770") as client:
                for _ in range(5):
                    self.assertEqual(client.post("/api/login", json={"password": "wrong"}).status_code, 401)
                self.assertEqual(client.post("/api/login", json={"password": "wrong"}).status_code, 429)


if __name__ == "__main__":
    unittest.main()
