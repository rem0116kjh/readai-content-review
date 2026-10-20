"""Meaningful corrupt-source, citation, job lifecycle and HTTP boundary tests."""
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import review
import criteria


def fixture():
    """Small, coherent independent set: zero-based references and five cloze labels."""
    sid = "3-1-1-01"
    meta = {"세트ID": sid, "학년": "3", "소단원·학습목표": "글의 중심 내용을 찾는다", "주 성취기준": "[4국02-02]",
            "지문1(개념문) 주제": "중심 내용", "지문2 제목": "학교의 나무", "_row": 2}
    words = [{"세트ID": sid, "번호": str(n), "낱말": f"낱말{n}", "품사": "명사", "뜻(교과서 문맥)": "정의", "예문(단어 카드)": "예문", "_row": n + 1} for n in range(1, 11)]
    originals = [{"세트ID": sid, "지문": "지문1(개념문)", "문장 번호": "개념0", "문장": "[가] [나] [다] [라] [마]", "문단·역할": "-", "핵심 문장": "-", "_row": 2}]
    originals += [{"세트ID": sid, "지문": "지문2", "문장 번호": f"문장{n}", "문장": f"나무가 자랍니다 {n}.", "문단·역할": f"{n + 1}문단", "핵심 문장": "핵심", "_row": n + 3} for n in range(3)]
    questions = []
    for stage, count in (("어휘", 5), ("지문1", 5), ("지문2", 7)):
        for n in range(count):
            questions.append({"세트ID": sid, "앱 단계": stage, "문항 번호": f"문항{n + 1}", "질문": "알맞은 것은?",
                              "보기": f"허용:{'가나다라마'[n]}" if stage == "지문1" else "① 하나 ② 둘 ③ 셋 ④ 넷 ⑤ 다섯",
                              "정답": "가나다라마"[n] if stage == "지문1" else "①", "정답 근거(지문 문장)": "개념0" if stage == "지문1" else "문장0·1",
                              "_row": len(questions) + 2})
    groups = {"2_세트목록": [meta], "3_어휘": words, "5_문항": questions, "6_지문원문": originals}
    return {"spreadsheetId": review.SHEET_ID, "fetched_at": review.now(), "sheets": {t: {"headers": list(rows[0]), "rows": rows} for t, rows in groups.items()}}


def finish(manager):
    manager.worker.join(timeout=5)
    if manager.worker.is_alive():
        raise AssertionError("Job did not finish")
    return manager.current()


class RuleTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = fixture()
        self.item = review.catalog(self.snapshot)[0]

    def findings(self):
        return review.rule_review(self.snapshot, self.item)

    def test_consistent_source_has_no_machine_errors_or_excluded_comparisons(self):
        issues = self.findings()
        self.assertEqual(issues, [])

    def test_excluded_tabs_are_not_part_of_input(self):
        self.assertEqual(set(review.TABS), {"2_세트목록", "3_어휘", "5_문항", "6_지문원문"})
        self.assertNotIn('"4_지문대응":', review.ai_prompt(self.snapshot, self.item))

    def test_duplicate_set_ids_preserve_actual_rows(self):
        duplicate = copy.deepcopy(self.snapshot["sheets"]["2_세트목록"]["rows"][0])
        duplicate["_row"] = 35
        duplicate["지문2 제목"] = "다른 지문"
        self.snapshot["sheets"]["2_세트목록"]["rows"].append(duplicate)
        self.assertEqual(review.catalog(self.snapshot)[0]["duplicate_rows"], [35])
        issue = next(i for i in self.findings() if "세트ID가 중복" in i["title"])
        self.assertEqual(issue["row"], 35)
        self.assertIn("range=A35", issue["url"])

    def test_compound_and_range_references(self):
        self.assertEqual(review.sentence_refs("문장7·8 및 문장10~12"), ["문장7", "문장8", "문장10", "문장11", "문장12"])
        self.assertEqual(review.sentence_refs("문장2, 문장4"), ["문장2", "문장4"])

    def test_dangling_reference_in_compound_expression(self):
        self.snapshot["sheets"]["5_문항"]["rows"][-1]["정답 근거(지문 문장)"] = "문장1·8"
        self.assertTrue(any("존재하지 않는 근거" in i["title"] and "문장8" in i["reason"] for i in self.findings()))

    def test_cloze_order_is_not_falsely_treated_as_wrong_answer(self):
        q = self.snapshot["sheets"]["5_문항"]["rows"]
        q[5:10] = list(reversed(q[5:10]))
        self.assertFalse(any("빈칸과 문항 정답 불일치" in i["title"] for i in self.findings()))

    def test_wrong_cloze_answer_and_accepted_answers_detected(self):
        self.snapshot["sheets"]["5_문항"]["rows"][5]["정답"] = "틀림"
        titles = [i["title"] for i in self.findings()]
        self.assertIn("빈칸 정답과 허용 답안 불일치", titles)
        self.snapshot["sheets"]["5_문항"]["rows"][5]["보기"] = "허용:틀림"
        self.assertTrue(any(i["title"] == "개념문 빈칸과 문항 정답 불일치" for i in self.findings()))

    def test_duplicate_options_and_invalid_answer(self):
        row = self.snapshot["sheets"]["5_문항"]["rows"][0]
        row.update(보기="① 하나 ② 하나 ③ 셋 ④ 넷 ⑤ 다섯", 정답="⑥")
        titles = [i["title"] for i in self.findings()]
        self.assertIn("같은 내용의 보기", titles)
        self.assertIn("정답 번호가 보기에 없습니다", titles)

    def test_variable_paragraph_count_allowed(self):
        originals = self.snapshot["sheets"]["6_지문원문"]["rows"]
        originals += [{"세트ID": self.item["id"], "지문": "지문2", "문장 번호": f"문장{n}", "문장": "추가 문장", "문단·역할": f"{n+1}문단", "핵심 문장": "핵심", "_row": n + 3} for n in (3, 4)]
        self.assertFalse(any(i["severity"] == "error" for i in self.findings()))

    def test_four_choices_are_valid(self):
        row = self.snapshot["sheets"]["5_문항"]["rows"][0]
        row.update(보기="① 하나 ② 둘 ③ 셋 ④ 넷", 정답="④", **{"응답 방식": "mc"})
        self.assertFalse(any(i["severity"] == "error" for i in self.findings()))

    def test_permitted_cloze_inflection_is_valid(self):
        self.snapshot["sheets"]["6_지문원문"]["rows"][0]["문장"] = "[가는] [나] [다] [라] [마]"
        self.snapshot["sheets"]["5_문항"]["rows"][5]["보기"] = "허용:가 / 가는"
        self.assertFalse(any(i["severity"] == "error" for i in self.findings()))

    def test_extra_concept_comprehension_question_is_allowed(self):
        q = {"세트ID": self.item["id"], "앱 단계": "지문1", "문항 번호": "이해1", "질문": "맞으면 O", "보기": "① 내용1 ② 내용2", "정답": "O X", "정답 근거(지문 문장)": "문장0", "응답 방식": "ox", "_row": 50}
        self.snapshot["sheets"]["5_문항"]["rows"].append(q)
        self.assertFalse(any(i["severity"] == "error" for i in self.findings()))

    def test_ox_answer_count_mismatch_detected(self):
        row = self.snapshot["sheets"]["5_문항"]["rows"][-1]
        row.update(보기="① 진술1 ② 진술2", 정답="O", **{"응답 방식": "ox"})
        self.assertTrue(any(i["title"] == "OX 답안 개수·형식 확인" for i in self.findings()))

    def test_two_step_answers_are_checked_within_their_own_step(self):
        row = self.snapshot["sheets"]["5_문항"]["rows"][-1]
        row.update(보기="1단계: ① 하나 ② 둘 / 2단계: ① 가 ② 나 ③ 다", 정답="1단계 ②, 2단계 ③", **{"응답 방식": "two_step"})
        self.assertFalse(any(i["severity"] == "error" for i in self.findings()))
        row["정답"] = "1단계 ③, 2단계 ③"
        self.assertTrue(any(i["title"] == "2단계 정답과 보기 연결 확인" for i in self.findings()))

    def test_match_and_order_unknown_targets_detected(self):
        row = self.snapshot["sheets"]["5_문항"]["rows"][-1]
        row.update(보기="왼쪽: 1. 이름 2. 이름 / 오른쪽: (가) 내용 (나) 내용", 정답="1-다, 2-나", **{"응답 방식": "match"})
        self.assertTrue(any(i["title"] == "짝짓기 정답에 없는 항목 참조" for i in self.findings()))
        row.update(보기="(가) 하나 (나) 둘 (다) 셋", 정답="(가)→(나)→(나)", **{"응답 방식": "order"})
        self.assertTrue(any(i["title"] == "순서 배열 답안의 항목 확인" for i in self.findings()))

    def test_secondary_passage_has_independent_sentence_numbering(self):
        original = {"세트ID": self.item["id"], "지문": "지문2(나)", "문장 번호": "문장0", "문장": "별도 지문입니다.", "문단·역할": "1문단", "핵심 문장": "핵심", "_row": 30}
        self.snapshot["sheets"]["6_지문원문"]["rows"].append(original)
        self.snapshot["sheets"]["5_문항"]["rows"][-1]["정답 근거(지문 문장)"] = "문장1, (나)문장0"
        self.assertFalse(any(i["severity"] == "error" for i in self.findings()))
        self.snapshot["sheets"]["5_문항"]["rows"][-1]["정답 근거(지문 문장)"] = "(나)문장9"
        self.assertTrue(any("(나)문장9" in i["reason"] for i in self.findings()))

    def test_grade_one_and_two_do_not_check_achievement_codes(self):
        for grade in (1, 2):
            self.item["grade"] = grade
            self.snapshot["sheets"]["2_세트목록"]["rows"][0]["주 성취기준"] = "잘못된 코드"
            self.assertEqual(self.findings(), [])

    def test_copy_links_do_not_point_back_to_original(self):
        self.snapshot["spreadsheetId"] = "copied-sheet-12345678901234567890"
        self.snapshot["sheets"]["5_문항"]["rows"][0]["정답"] = "⑥"
        self.assertIn("copied-sheet-12345678901234567890", self.findings()[0]["url"])
        self.assertNotIn(review.SHEET_ID, self.findings()[0]["url"])

    def test_source_url_cannot_be_used_for_arbitrary_network_requests(self):
        for address in ("file:///etc/passwd", "http://127.0.0.1/secrets", "https://evil.example/spreadsheets/d/12345678901234567890"):
            with self.assertRaises(ValueError):
                review.parse_sheet_id(address)
        self.assertEqual(review.parse_sheet_id(review.SHEET_URL), review.SHEET_ID)

    def test_csv_html_login_page_is_not_source_data(self):
        fake = io.BytesIO(b"<html>login</html>")
        fake.headers = {"Content-Type": "text/html"}
        with patch("urllib.request.urlopen", return_value=fake):
            with self.assertRaisesRegex(ValueError, "읽기 권한"):
                review.download_tab("2_세트목록")

    def test_source_row_numbers_keep_empty_rows(self):
        fake = io.BytesIO(('세트ID,학년,소단원·학습목표,주 성취기준,지문1(개념문) 주제,지문2 제목\n\n3-1-1-01,3,목표,코드,주제,제목\n').encode())
        fake.headers = {"Content-Type": "text/csv"}
        with patch("urllib.request.urlopen", return_value=fake):
            result = review.download_tab("2_세트목록")
        self.assertEqual(result["rows"][0]["_row"], 3)


class AITests(unittest.TestCase):
    def setUp(self):
        self.snapshot = fixture()
        self.item = review.catalog(self.snapshot)[0]
        self.result = {"summary": "확인", "coverage": ["2", "3", "4", "6"], "difficulty": "적합", "section_results": {t: "맞음" for t in criteria.TABS}, "issues": [{"check": "문법·문장", "category": "3", "severity": "warning", "title": "표현 확인", "sheet": "6_지문원문", "row": 3, "field": "문장", "quote": "나무가 자랍니다", "reason": "이유", "suggestion": "제안", "evidence_cells": []}]}

    def test_ai_citation_is_bound_to_actual_source(self):
        result = review.validate_ai(self.result, self.snapshot, self.item)
        self.assertEqual(result[0]["kind"], "ai")
        self.assertIn("gid=6&range=D3", result[0]["url"])

    def test_hallucinated_quote_or_cross_set_row_rejected(self):
        for key, value in (("quote", "존재하지 않는 인용"), ("row", 999), ("field", "없는 열"), ("sheet", "4_지문대응")):
            with self.subTest(key=key):
                bad = copy.deepcopy(self.result)
                bad["issues"][0][key] = value
                with self.assertRaises(ValueError):
                    review.validate_ai(bad, self.snapshot, self.item)

    def test_incomplete_ai_coverage_is_not_complete(self):
        self.result["coverage"] = ["2", "3"]
        with self.assertRaisesRegex(ValueError, "범위"):
            review.validate_ai(self.result, self.snapshot, self.item)


class JobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.snapshot = fixture()
        self.manager = app.ReviewManager(self.tmp.name, downloader=lambda *a: copy.deepcopy(self.snapshot))

    def test_rules_only_and_unique_report_persistence(self):
        first = self.manager.start({"grades": [3], "ai": False})
        job = finish(self.manager)
        self.assertEqual(job["status"], "complete")
        self.assertEqual(job["sets"][0]["ai_status"], "skipped")
        second = self.manager.start({"grades": [3], "ai": False})
        finish(self.manager)
        self.assertNotEqual(first, second)
        self.assertEqual(len(list((Path(self.tmp.name)/"reports").glob("*.json"))), 2)

    def test_ai_failure_preserves_machine_results_and_is_partial(self):
        def fail(*args):
            raise ValueError("검토 실패")
        self.snapshot["sheets"]["5_문항"]["rows"][0]["정답"] = "⑥"
        self.manager.ai_reviewer = fail
        self.manager.start({"grades": [3], "ai": True})
        job = finish(self.manager)
        self.assertEqual(job["status"], "partial")
        self.assertEqual(job["sets"][0]["rule_status"], "complete")
        self.assertEqual(job["sets"][0]["ai_status"], "failed")
        self.assertTrue(job["issues"])

    def test_cancel_and_no_concurrent_run(self):
        started = threading.Event()
        def wait_ai(snapshot, item, cancel):
            started.set()
            cancel.wait(3)
            raise review.Cancelled()
        self.snapshot["sheets"]["5_문항"]["rows"][0]["정답"] = "⑥"
        self.manager.ai_reviewer = wait_ai
        self.manager.start({"grades": [3], "ai": True})
        self.assertTrue(started.wait(3))
        with self.assertRaises(RuntimeError):
            self.manager.start({"grades": [3], "ai": False})
        self.manager.cancel()
        job = finish(self.manager)
        self.assertEqual(job["status"], "cancelled")
        self.assertEqual(job["sets"][0]["ai_status"], "cancelled")
        self.assertTrue(job["issues"])

    def test_download_failure_never_reuses_old_data(self):
        self.manager.start({"grades": [3], "ai": False})
        finish(self.manager)
        def fail(*a):
            raise ValueError("시트 읽기 실패")
        self.manager.downloader = fail
        self.manager.start({"grades": [3], "ai": False})
        job = finish(self.manager)
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["sets"], [])
        self.assertEqual(job["issues"], [])

    def test_selected_grade_and_sample_filters(self):
        self.manager.start({"grades": [4], "ai": False})
        job = finish(self.manager)
        self.assertEqual(job["status"], "complete")
        self.assertEqual(job["sets"], [])
        self.assertEqual(job["empty_grades"], [4])
        meta = self.snapshot["sheets"]["2_세트목록"]["rows"][0]
        meta["세트ID"] = "3-견본"
        self.manager.start({"grades": [3], "ai": False})
        self.assertEqual(finish(self.manager)["sets"], [])

    def test_grade_one_can_run_without_changing_data(self):
        self.snapshot["sheets"]["2_세트목록"]["rows"][0]["학년"] = "1"
        self.snapshot["sheets"]["2_세트목록"]["rows"][0]["주 성취기준"] = "[2국02-04]"
        self.manager.start({"grades": [1, 2, 3, 4, 5, 6], "ai": False})
        job = finish(self.manager)
        self.assertEqual(job["status"], "complete")
        self.assertEqual(job["sets"][0]["grade"], 1)

    def test_interrupted_report_restored_as_partial(self):
        p = Path(self.tmp.name)/"reports"/"a.json"
        app.atomic_write(p, json.dumps({"status": "running"}))
        self.assertEqual(self.manager.latest()["status"], "interrupted")


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        manager = app.ReviewManager(self.tmp.name, downloader=lambda *a: fixture())
        self.server = app.make_server(0, manager)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.manager.cancel()
        if self.server.manager.worker:
            self.server.manager.worker.join(3)
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def test_cross_origin_and_missing_token_rejected(self):
        for headers in ({"Content-Type": "application/json"}, {"Content-Type": "application/json", "X-Review-Token": self.server.token, "Origin": "https://evil.example"}):
            req = urllib.request.Request(self.base+"/api/review", data=b'{"ai":false}', headers=headers)
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(req)
            self.assertEqual(raised.exception.code, 403)
            raised.exception.close()

    def test_authorized_button_request_runs_and_exports(self):
        headers = {"Content-Type": "application/json", "X-Review-Token": self.server.token, "Origin": self.base}
        req = urllib.request.Request(self.base+"/api/review", data=b'{"grades":[3],"ai":false}', headers=headers)
        with urllib.request.urlopen(req) as r:
            job_id = json.load(r)["id"]
        job = finish(self.server.manager)
        self.assertEqual(job["status"], "complete")
        with urllib.request.urlopen(self.base+f"/api/report/{job_id}.md") as r:
            self.assertIn("리드AI 콘텐츠 검토", r.read().decode())

    def test_path_traversal_not_served(self):
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(self.base+"/../review.py")
        self.assertEqual(raised.exception.code, 404)
        raised.exception.close()


if __name__ == "__main__":
    unittest.main()
