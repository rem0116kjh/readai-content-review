"""User policy regressions: partial inputs, conservative decisions, cross-set proof."""
import copy
import io
import json
import unittest
from unittest.mock import patch

from test_review import fixture, finish
import app
import criteria
import review


def answer():
    return {"summary": "제공 자료를 확인했습니다.", "coverage": ["2", "3", "4", "6"],
            "difficulty": "적합", "section_results": {t: "맞음" for t in criteria.TABS}, "issues": []}


class CriteriaTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = fixture()
        self.item = review.catalog(self.snapshot)[0]

    def test_eight_and_sixteen_words_are_counted_without_inventing_a_total(self):
        source = self.snapshot["sheets"]["3_어휘"]["rows"]
        for n in (8, 16):
            rows = [dict(source[0], 번호=str(i), 낱말=f"낱말{i}", _row=i+1) for i in range(1, n+1)]
            self.snapshot["sheets"]["3_어휘"]["rows"] = rows
            issues = review.rule_review(self.snapshot, self.item)
            self.assertEqual(issues, [])
            result = criteria.summary(self.snapshot, self.item, issues)
            self.assertEqual(result["vocabulary_text"], f"제공된 {n}개 중 {n}개 확인")
            self.assertIsNone(result["difficulty"])

    def test_normal_repeated_examples_are_not_flagged(self):
        self.assertEqual({r["예문(단어 카드)"] for r in self.snapshot["sheets"]["3_어휘"]["rows"]}, {"예문"})
        self.assertEqual(review.rule_review(self.snapshot, self.item), [])

    def test_excluded_codes_and_textbook_claims_are_not_ai_input(self):
        meta = self.snapshot["sheets"]["2_세트목록"]["rows"][0]
        meta.update({"주 성취기준": "THIS_CODE_MUST_NOT_APPEAR", "부 성취기준": "SECONDARY_HIDDEN", "교과서 근거": "TEXTBOOK_CLAIM_HIDDEN", "검수 메모": "PAST_REVIEW_HIDDEN"})
        prompt = review.ai_prompt(self.snapshot, self.item)
        for value in ("THIS_CODE_MUST_NOT_APPEAR", "SECONDARY_HIDDEN", "TEXTBOOK_CLAIM_HIDDEN", "PAST_REVIEW_HIDDEN"):
            self.assertNotIn(value, prompt)
        self.assertIn("글의 중심 내용을 찾는다", prompt)
        self.assertIn("겹말", prompt)
        meta["주 성취기준"] = ""
        self.assertEqual(review.rule_review(self.snapshot, self.item), [])

    def test_partial_columns_do_not_create_missing_answer_options_or_goals(self):
        for tab, field in (("2_세트목록", "소단원·학습목표"), ("5_문항", "보기")):
            table = self.snapshot["sheets"][tab]
            table["headers"].remove(field)
            for row in table["rows"]:
                row.pop(field, None)
            table.update(partial=True, missing_columns=[field])
        self.assertEqual(review.rule_review(self.snapshot, self.item), [])
        self.assertEqual(criteria.summary(self.snapshot, self.item, [])["verdict"], "확인 필요")

    def test_partial_original_does_not_assert_nonexistent_source(self):
        table = self.snapshot["sheets"]["6_지문원문"]
        table.update(partial=True, rows=table["rows"][:2])
        q = self.snapshot["sheets"]["5_문항"]["rows"][-1]
        q["정답 근거(지문 문장)"] = "문장99"
        issues = review.rule_review(self.snapshot, self.item)
        self.assertFalse(any(i["severity"] == "error" for i in issues))
        self.assertTrue(any(i["title"] == "근거 문장 확인 필요" for i in issues))

    def test_present_but_empty_provided_cell_is_still_an_error(self):
        self.snapshot["partial"] = True
        self.snapshot["sheets"]["3_어휘"]["rows"][0]["뜻(교과서 문맥)"] = ""
        issues = review.rule_review(self.snapshot, self.item)
        self.assertTrue(any(i["title"] == "어휘 뜻(교과서 문맥) 누락" for i in issues))

    def test_csv_partial_table_and_quoted_commas_preserve_word_count(self):
        raw = '세트ID,번호,낱말,뜻(교과서 문맥)\n3-1-1-01,1,나무,"뿌리, 줄기와 잎이 있는 식물"\n'
        fake = io.BytesIO(raw.encode())
        fake.headers = {"Content-Type": "text/csv"}
        with patch("urllib.request.urlopen", return_value=fake):
            table = review.download_tab("3_어휘")
        self.assertTrue(table["partial"])
        self.assertEqual(len(table["rows"]), 1)
        self.assertEqual(table["rows"][0]["뜻(교과서 문맥)"], "뿌리, 줄기와 잎이 있는 식물")

    def test_rules_only_never_claims_semantic_pass_or_no_typos(self):
        self.item.update(rule_status="complete", ai_status="skipped")
        result = criteria.summary(self.snapshot, self.item, [])
        self.assertEqual(result["verdict"], "확인 필요")
        self.assertEqual(set(result["checks"].values()), {"미검토"})

    def test_completed_ai_exposes_only_three_verdicts_and_three_difficulties(self):
        self.item.update(ai_status="complete", ai_section_results=answer()["section_results"], ai_difficulty="적합")
        result = criteria.summary(self.snapshot, self.item, [])
        self.assertEqual(result["verdict"], "맞음")
        self.assertEqual(set(result["checks"].values()), {"없음"})
        self.assertEqual(result["difficulty"], "적합")
        issue = {"set_id": self.item["id"], "sheet": "3_어휘", "severity": "error", "check": "겹말"}
        result = criteria.summary(self.snapshot, self.item, [issue])
        self.assertEqual(result["verdict"], "수정 필요")
        self.assertEqual(result["checks"]["겹말"], "1건")
        self.assertEqual(result["sections"]["3_어휘"], "수정 필요")

    def test_ai_rejects_numeric_or_unknown_difficulty_and_missing_section(self):
        for value in (5, "보통", "80점"):
            result = answer()
            result["difficulty"] = value
            with self.assertRaisesRegex(ValueError, "난이도"):
                review.validate_ai(result, self.snapshot, self.item)
        result = answer()
        del result["section_results"]["3_어휘"]
        with self.assertRaisesRegex(ValueError, "판정"):
            review.validate_ai(result, self.snapshot, self.item)

    def test_no_provided_questions_never_claims_answer_review_pass(self):
        self.snapshot["sheets"]["5_문항"].update(rows=[], partial=True)
        self.item.update(ai_status="complete", ai_section_results=answer()["section_results"], ai_difficulty="적합")
        result = criteria.summary(self.snapshot, self.item, [])
        self.assertEqual(result["sections"]["5_문항"], "확인 필요")
        self.assertEqual(result["checks"]["문항·정답"], "미검토")

    def test_related_evidence_is_limited_to_supplied_common_content(self):
        word = copy.deepcopy(self.snapshot["sheets"]["3_어휘"]["rows"][0])
        word.update(세트ID="4-1-1-01", _row=100)
        unrelated = dict(word, 낱말="다른 단어", _row=101)
        self.snapshot["sheets"]["3_어휘"]["rows"] += [word, unrelated]
        own, related = criteria.inputs(self.snapshot, self.item["id"])
        self.assertEqual([r["_row"] for r in related["3_어휘"]], [100])
        result = answer()
        result["issues"] = [{"category": "3", "check": "공통 내용", "severity": "warning", "title": "공통 뜻 확인", "sheet": "3_어휘", "row": 2, "field": "뜻(교과서 문맥)", "quote": "정의", "reason": "원문 대조", "suggestion": "문맥 확인", "evidence_cells": [{"sheet": "3_어휘", "row": 100, "field": "뜻(교과서 문맥)", "quote": "정의"}]}]
        findings = review.validate_ai(result, self.snapshot, self.item)
        self.assertEqual(findings[0]["related_set_ids"], ["4-1-1-01"])
        self.assertIn("4-1-1-01", findings[0]["evidence"])
        # Another set can be evidence, never the current finding's anchor.
        result["issues"][0]["row"] = 100
        with self.assertRaises(ValueError):
            review.validate_ai(result, self.snapshot, self.item)
        result["issues"][0]["row"] = 2
        result["issues"][0]["evidence_cells"][0]["row"] = 101
        with self.assertRaises(ValueError):
            review.validate_ai(result, self.snapshot, self.item)
        result["issues"][0]["evidence_cells"] = []
        with self.assertRaisesRegex(ValueError, "다른 세트"):
            review.validate_ai(result, self.snapshot, self.item)

    def test_shared_topic_concepts_are_available_even_without_same_cloze_label(self):
        other = dict(self.snapshot["sheets"]["2_세트목록"]["rows"][0], 세트ID="4-1-1-01", _row=100)
        self.snapshot["sheets"]["2_세트목록"]["rows"].append(other)
        concept = dict(self.snapshot["sheets"]["6_지문원문"]["rows"][0], 세트ID=other["세트ID"], 문장="다른 설명입니다.", _row=200)
        self.snapshot["sheets"]["6_지문원문"]["rows"].append(concept)
        self.assertEqual(criteria.inputs(self.snapshot, self.item["id"])[1]["6_지문원문"], [concept])

    def test_version_and_policy_are_saved_in_job_and_both_exports(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            manager = app.ReviewManager(folder, downloader=lambda *a: self.snapshot,
                                        ai_reviewer=lambda *a: {**answer(), "issues": []})
            job_id = manager.start({"grades": [3], "ai": True})
            job = finish(manager)
            self.assertEqual(job["criteria_sha256"], criteria.HASH)
            self.assertEqual(job["sets"][0]["review_summary"]["verdict"], "맞음")
            md = (Path(folder)/"reports"/f"{job_id}.md").read_text()
            self.assertIn(criteria.VERSION, md)
            self.assertIn("제공된 10개 중 10개 확인", md)
            self.assertIn("난이도: 적합", md)
            legacy = copy.deepcopy(job)
            legacy.pop("criteria_sha256")
            legacy.pop("criteria_version")
            self.assertIn("이전 기준 결과", review.markdown_report(legacy))


if __name__ == "__main__":
    unittest.main()
