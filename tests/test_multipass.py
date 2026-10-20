"""Multi-pass correctness: complete coverage, no lost candidates, no false pass."""
import copy
import json
import tempfile
import threading
import unittest
from unittest.mock import patch

from test_review import fixture, finish
from test_criteria import answer
import app
import criteria
import review


class MultiPassTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = fixture()
        self.item = review.catalog(self.snapshot)[0]
        status = patch.object(review, "codex_status", return_value={"ready": True, "message": "offline test"})
        status.start()
        self.addCleanup(status.stop)

    def test_identical_repeated_rows_and_repeated_words_are_allowed(self):
        for table in self.snapshot["sheets"].values():
            table["rows"].append(dict(table["rows"][0], _row=999))
        self.assertEqual(review.rule_review(self.snapshot, self.item), [])
        words = self.snapshot["sheets"]["3_어휘"]["rows"]
        words[1]["낱말"] = words[0]["낱말"]
        self.assertEqual(review.rule_review(self.snapshot, self.item), [])
        words[1]["번호"] = words[0]["번호"]
        words[1]["뜻(교과서 문맥)"] = "서로 다른 뜻"
        findings = review.rule_review(self.snapshot, self.item)
        details = findings + [d for i in findings for d in i.get("related_findings", [])]
        self.assertTrue(any(i["title"] == "어휘 번호 중복" for i in details))

    def test_both_prompts_keep_all_original_rows_in_explicit_opposite_orders(self):
        for direction, order in (("forward", criteria.REVIEW_ORDER), ("reverse", tuple(reversed(criteria.REVIEW_ORDER)))):
            prompt = review.ai_prompt(self.snapshot, self.item, direction)
            text = prompt.split("[검토 데이터]\n")[1].split("\n[이전 검토 후보]")[0]
            data = json.loads(text)
            self.assertEqual(list(data["sheets"]), list(order))
            self.assertEqual(data["set_id"], self.item["id"])
            for tab in criteria.TABS:
                self.assertEqual(data["sheets"][tab], criteria.inputs(self.snapshot, self.item["id"])[0][tab])

    def test_identical_metadata_with_conflicting_children_is_one_group(self):
        meta = self.snapshot["sheets"]["2_세트목록"]["rows"]
        meta.append(dict(meta[0], _row=99))
        words = self.snapshot["sheets"]["3_어휘"]["rows"]
        words.append(dict(words[0], _row=100, 낱말="다른 말"))
        issues = review.rule_review(self.snapshot, self.item)
        root = next(i for i in issues if i["title"] == "같은 세트ID의 연결 번호 충돌")
        self.assertTrue(any(i["row"] == 100 for i in root["related_findings"]))
        self.assertEqual(review.consolidate_findings(issues), issues)

    def test_two_real_pass_invocations_and_reverse_receives_forward_candidates(self):
        candidate = {"sheet": "3_어휘", "row": 2, "field": "뜻(교과서 문맥)"}
        forward = {**answer(), "issues": [candidate]}
        reverse = {**answer(), "issues": []}
        calls = []

        def fake(snapshot, item, cancel, timeout, direction, prior):
            calls.append((direction, copy.deepcopy(prior)))
            return forward if direction == "forward" else reverse

        with patch.object(review, "ai_review_pass", side_effect=fake):
            result = review.ai_review(self.snapshot, self.item, threading.Event())
        self.assertEqual(calls, [("forward", []), ("reverse", [candidate])])
        self.assertEqual(result["issues"], [])
        self.assertEqual(result["passes"]["forward"]["issues"], [candidate])

    def test_missing_candidate_disposition_and_wrong_cell_are_rejected(self):
        prior = [{"sheet": "3_어휘", "row": 2, "field": "뜻(교과서 문맥)"}]
        with self.assertRaisesRegex(ValueError, "누락"):
            review.validate_rechecks({"rechecks": []}, prior, [])
        check = {"index": 0, "verdict": "keep", "reason": "근거 있음", "final_index": 0}
        with self.assertRaisesRegex(ValueError, "다른 셀"):
            review.validate_rechecks({"rechecks": [check]}, prior, [dict(prior[0], row=3)])
        findings = [{**prior[0], "severity": "error"}]
        check["verdict"] = "uncertain"
        review.validate_rechecks({"rechecks": [check]}, prior, findings)
        self.assertEqual(findings[0]["severity"], "warning")

    def test_reverse_failure_preserves_forward_pass_and_is_not_complete(self):
        def fake(snapshot, item, cancel, timeout, direction, prior):
            if direction == "reverse":
                raise ValueError("역순 실패")
            return answer()

        with tempfile.TemporaryDirectory() as folder, patch.object(review, "ai_review_pass", side_effect=fake):
            manager = app.ReviewManager(folder, downloader=lambda *a: self.snapshot)
            manager.start({"grades": [3], "ai": True})
            job = finish(manager)
        self.assertEqual(job["status"], "partial")
        item = job["sets"][0]
        self.assertEqual(item["ai_passes"]["forward"]["status"], "complete")
        self.assertEqual(item["ai_passes"]["reverse"]["status"], "failed")
        self.assertEqual(item["ai_status"], "failed")
        self.assertEqual(item["review_summary"]["verdict"], "확인 필요")

    def test_cancel_between_passes_does_not_start_reverse(self):
        event = threading.Event()
        def fake(*args):
            event.set()
            return answer()
        with patch.object(review, "ai_review_pass", side_effect=fake) as run:
            with self.assertRaises(review.Cancelled):
                review.ai_review(self.snapshot, self.item, event)
            self.assertEqual(run.call_count, 1)

    def test_ai_version_conflict_joins_existing_group_without_erasing_evidence(self):
        root = {"kind":"rule", "set_id":self.item["id"], "title":"같은 세트ID의 연결 번호 충돌"}
        finding = {"kind":"ai", "set_id":self.item["id"], "title":"문항·어휘 연결 버전 충돌",
                   "check":"연결·표시", "quote":"서로 다른 정답"}
        grouped = review.consolidate_findings([root, finding])
        self.assertEqual(len(grouped), 1)
        self.assertEqual(grouped[0]["related_findings"], [finding])
        self.assertEqual(review.consolidate_findings(grouped), grouped)

    def test_morphology_dependent_presence_remains_visible_as_warning(self):
        issue = {"kind":"ai", "severity":"error", "sheet":"3_어휘",
                 "field":"앱: 지문2 등장", "title":"등장 위치 불일치", "quote":"문장1"}
        result = review.assess_ai_findings(self.snapshot, [issue])[0]
        self.assertEqual(result["severity"], "warning")
        self.assertEqual(result["ai_severity"], "error")
        self.assertEqual(result["quote"], "문장1")
        self.assertEqual(issue["severity"], "error")


if __name__ == "__main__":
    unittest.main()
