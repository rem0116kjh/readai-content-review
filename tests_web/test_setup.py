import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from fastapi.testclient import TestClient

from openai_reviewer import OpenAIReviewer
from setup_env import ensure_env, load_env
import review
from test_review import fixture, finish
from test_web import FakeClient
import web_server


class EnvSetupTests(unittest.TestCase):
    def test_first_run_creates_private_file_and_keeps_existing_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            template = "OPENAI_API_KEY=\nOPENAI_MODEL=gpt-6.1-sol\n"
            (root / ".env.example").write_text(template)
            path, created = ensure_env(root)
            self.assertTrue(created)
            self.assertEqual(path.read_text(), template)
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            path.write_text("OPENAI_API_KEY=local-test-value\n")
            _, created = ensure_env(root)
            self.assertFalse(created)
            self.assertEqual(path.read_text(), "OPENAI_API_KEY=local-test-value\n")

    def test_missing_template_does_not_create_empty_env(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(FileNotFoundError):
                ensure_env(folder)
            self.assertFalse((Path(folder) / ".env").exists())

    def test_load_env_quotes_bom_comments_and_environment_precedence(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"OPENAI_MODEL": "environment-model"}, clear=True):
            path = Path(folder) / ".env"
            path.write_text('\ufeff# configuration\nexport OPENAI_API_KEY = "local#test"\n'
                            'OPENAI_MODEL=file-model # comment\n'
                            "READAI_ACCESS_PASSWORD='literal$(no-execution)#value'\n"
                            'UNRELATED_VARIABLE=ignored\n', encoding="utf-8")
            load_env(path)
            self.assertEqual(os.environ["OPENAI_API_KEY"], "local#test")
            self.assertEqual(os.environ["OPENAI_MODEL"], "environment-model")
            self.assertEqual(os.environ["READAI_ACCESS_PASSWORD"], "literal$(no-execution)#value")
            self.assertNotIn("UNRELATED_VARIABLE", os.environ)

    def test_env_to_web_review_two_passes_without_exposing_key(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            root = Path(folder)
            path = root / ".env"
            path.write_text("OPENAI_API_KEY=local-test-key\nOPENAI_MODEL=gpt-6.1-sol\n")
            load_env(path)
            calls = []
            settings = []
            def reviewer_factory(key, model):
                settings.append((key, model))
                return OpenAIReviewer(key, model, client_factory=lambda **kw: FakeClient(calls, **kw))
            with patch.object(web_server, "ROOT", root), patch.object(web_server, "OpenAIReviewer", reviewer_factory), \
                 patch.object(review, "download_snapshot", side_effect=lambda *args: fixture()):
                api = web_server.create_web_app()
                with TestClient(api, base_url="http://127.0.0.1:8770") as client:
                    bootstrap = client.get("/api/bootstrap").json()
                    self.assertTrue(bootstrap["ai"]["ready"])
                    self.assertEqual(settings, [("local-test-key", "gpt-6.1-sol")])
                    started = client.post("/api/review", json={"ai": True},
                                          headers={"X-Review-Token": bootstrap["token"]})
                    self.assertEqual(started.status_code, 202)
                    report = finish(api.state.manager)
                    self.assertEqual(report["status"], "complete")
                    self.assertEqual([c["model"] for c in calls], ["gpt-6.1-sol", "gpt-6.1-sol"])
                    self.assertNotIn("local-test-key", json.dumps(report))
                    self.assertNotIn("local-test-key", json.dumps(bootstrap))
                    self.assertEqual(client.get("/.env").status_code, 404)


if __name__ == "__main__":
    unittest.main()
