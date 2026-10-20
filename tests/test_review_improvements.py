"""Observed false positives and a stale-explanation miss; no network/model use."""
import copy
import json
import tempfile
import unittest

from test_review import fixture, finish
from test_criteria import answer
import app
import criteria
import review


class ReviewImprovementTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = fixture()
        self.item = review.catalog(self.snapshot)[0]
        self.questions = self.snapshot['sheets']['5_문항']['rows']
        self.originals = self.snapshot['sheets']['6_지문원문']['rows']

    def findings(self):
        return review.rule_review(self.snapshot, self.item)

    def test_legacy_input_answers_are_cloze_not_missing_mc_choices(self):
        for q in self.questions[5:10]:
            q['보기'] = '입력형 (' + q['보기'] + ')'
        self.assertEqual(self.findings(), [])

    def test_empty_cloze_allowed_answers_has_specific_diagnosis(self):
        q=self.questions[5]
        q.update(유형='개념 빈칸', 보기='')
        found=[i for i in self.findings() if i['row']==q['_row']]
        self.assertEqual([i['title'] for i in found], ['빈칸 허용 답안 누락'])
        self.assertEqual(found[0]['field'], '보기')

    def test_empty_inline_prompt_does_not_exempt_real_missing_question(self):
        self.questions[5]['질문']=''
        self.questions[-1]['질문']=''
        missing=[i for i in self.findings() if i['title']=='문항 질문 누락']
        self.assertEqual([i['row'] for i in missing], [self.questions[-1]['_row']])

    def test_circled_number_contents_are_five_distinct_choices(self):
        self.questions[-1].update(보기='① ① ② ② ③ ③ ④ ④ ⑤ ⑤', 정답='③')
        self.assertEqual(self.findings(), [])

    def test_actual_duplicate_number_contents_remain_warning(self):
        self.questions[-1].update(보기='① ① ② ① ③ ③', 정답='①')
        self.assertTrue(any(i['title']=='같은 내용의 보기' for i in self.findings()))

    def test_actual_broken_choice_markers_still_fail(self):
        self.questions[-1].update(보기='① 하나 ① 둘', 정답='②')
        titles={i['title'] for i in self.findings()}
        self.assertIn('객관식 보기 누락·중복',titles)
        self.assertIn('정답 번호가 보기에 없습니다',titles)

    def test_core_marker_with_annotation_is_counted_but_missing_is_not(self):
        self.originals[1]['핵심 문장']='핵심 · ㉠'
        self.assertEqual(self.findings(), [])
        self.originals[1]['핵심 문장']='㉠'
        self.assertTrue(any(i['title']=='문단 역할·핵심 문장 지정 확인' for i in self.findings()))

    def test_old_explanation_quote_is_checked_against_current_choice(self):
        q=self.questions[-1]
        q.update(보기='① 실제로 달아 두었다 ② 바람을 보고 일을 헤아렸다',정답='②',
                 **{'겨냥 학습 요소·어휘':"1번이 ‘~했을 것이다’로 낮춰져 있다."})
        self.snapshot['sheets']['5_문항']['headers'].append('겨냥 학습 요소·어휘')
        issue=next(i for i in self.findings() if i['title']=='해설의 보기 인용 불일치')
        self.assertEqual(issue['row'],q['_row'])
        self.assertIn('실제로 달아 두었다',issue['evidence'])
        q['보기']='① 실제로 달아 두었을 것이다 ② 바람을 보고 일을 헤아렸다'
        q['겨냥 학습 요소·어휘']="1번이 ‘달아 두었을 것이다’로 낮춰져 있다."
        self.assertFalse(any(i['title']=='해설의 보기 인용 불일치' for i in self.findings()))

    def test_proposed_rewrite_is_not_treated_as_original_quote(self):
        q=self.questions[-1]
        q['겨냥 학습 요소·어휘']="1번을 ‘새로운 보기 문장’으로 고치면 쉽다."
        self.snapshot['sheets']['5_문항']['headers'].append('겨냥 학습 요소·어휘')
        self.assertEqual(self.findings(), [])

    def test_duplicate_cascades_group_without_losing_actual_source_or_errors(self):
        meta=copy.deepcopy(self.snapshot['sheets']['2_세트목록']['rows'][0]);meta['_row']=100
        meta['지문2 제목']='서로 다른 제목'
        self.snapshot['sheets']['2_세트목록']['rows'].append(meta)
        original=copy.deepcopy(self.originals[1]);original['_row']=101;self.originals.append(original)
        original['문장']='같은 번호에 연결된 다른 문장입니다.'
        q=copy.deepcopy(self.questions[-1]);q['_row']=102;self.questions.append(q)
        q['정답']='②'
        self.questions[0]['정답']='⑨'
        issues=self.findings()
        root=next(i for i in issues if i['title']=='세트ID가 중복되어 있습니다')
        self.assertEqual(root['row'],100)
        grouped_titles={x['title'] for x in root['related_findings']}
        self.assertIn('지문 문장 번호 중복',grouped_titles)
        self.assertIn('문항 번호 중복',grouped_titles)
        self.assertTrue(any(i['title']=='정답 번호가 보기에 없습니다' for i in issues))
        self.assertTrue(any(x['row']==101 for x in root['related_findings']))
        self.assertEqual(review.consolidate_findings(issues),issues)

    def test_ai_duplicate_links_group_but_real_redundancy_is_not_hidden(self):
        root={'set_id':self.item['id'],'kind':'rule','title':'세트ID가 중복되어 있습니다'}
        duplicate={'set_id':self.item['id'],'kind':'ai','title':'원문 중복','check':'연결·표시'}
        wording={'set_id':self.item['id'],'kind':'ai','title':'뜻의 중복','check':'겹말'}
        result=review.consolidate_findings([root,duplicate,wording])
        self.assertEqual(len(result),2)
        self.assertEqual(root['related_findings'],[duplicate])
        self.assertIn(wording,result)

    def test_prompt_provides_real_resolved_concept_rows_and_box_context(self):
        q=self.questions[5];q['정답 근거(지문 문장)']='문장0'
        self.questions[-1]['질문']='〈계획〉 ㄱ. 관찰하기'
        context=json.loads(review.ai_prompt(self.snapshot,self.item).split('[검토 데이터]\n')[1])
        info=next(x for x in context['interpretation'] if x['question_row']==q['_row'])
        self.assertEqual(info['normalized_refs'],['개념0'])
        self.assertEqual(info['resolved_source_rows'],[2])
        self.assertEqual(info['accepted_answers'],['가'])
        self.assertTrue(context['interpretation'][-1]['question_box_provided'])
        # Context is additive; every provided row survives unchanged.
        self.assertEqual(context['sheets'],criteria.inputs(self.snapshot,self.item['id'])[0])

    def test_missing_passage_never_gets_difficulty_from_ai_default(self):
        self.snapshot['sheets']['6_지문원문']['rows']=[]
        self.item.update(ai_status='complete',ai_difficulty='적합',ai_section_results=answer()['section_results'])
        self.assertIsNone(criteria.summary(self.snapshot,self.item,[])['difficulty'])

    def test_ai_helper_keeps_secondary_passage_zero_separate(self):
        extra=dict(self.originals[1],지문='지문2(나)',_row=100)
        self.originals.append(extra)
        self.questions[-1]['정답 근거(지문 문장)']='문장1, (나)문장0'
        context=json.loads(review.ai_prompt(self.snapshot,self.item).split('[검토 데이터]\n')[1])
        self.assertEqual(context['interpretation'][-1]['normalized_refs'],['문장1','(나)문장0'])
        self.assertEqual(context['interpretation'][-1]['resolved_source_rows'],[4,100])

    def test_whole_unprovided_passage_is_source_gap_not_many_false_answers(self):
        self.snapshot['sheets']['6_지문원문']['rows']=[]
        issues=self.findings()
        self.assertEqual(len(issues),2)
        self.assertEqual({i['severity'] for i in issues},{'needs_source'})

    def test_provided_but_empty_original_is_still_error(self):
        self.originals[1]['문장']=''
        self.assertTrue(any(i['title']=='원문 문장이 비어 있습니다' and i['severity']=='error' for i in self.findings()))

    def test_snapshot_hashes_reproduce_content_changes(self):
        tab=self.snapshot['sheets']['5_문항'];h=review.table_hash(tab)
        self.assertEqual(h,review.table_hash(copy.deepcopy(tab)))
        tab['rows'][0]['정답']='②'
        self.assertNotEqual(h,review.table_hash(tab))

    def test_dictionary_spacing_in_prose_and_correct_form_control(self):
        self.originals[1]['문장']='경사진 길을 뛰어 올랐다.'
        self.assertTrue(any(i['title']=='사전 등재 합성어 띄어쓰기' for i in self.findings()))
        self.originals[1]['문장']='경사진 길을 뛰어올랐다.'
        self.assertEqual(self.findings(),[])
        self.questions[0]['보기']='① 옛 사람 ② 지금 사람'
        self.assertFalse(any(i['title']=='사전 등재 합성어 띄어쓰기' for i in self.findings()))

    def test_different_day_pronoun_check_and_same_day_control(self):
        q=self.questions[-1]
        self.snapshot['sheets']['5_문항']['headers'].append('겨냥 학습 요소·어휘')
        q['겨냥 학습 요소·어휘']='물이 줄었다는 것과 그날 물을 보충했다는 것을 이어 본다.'
        self.originals[1]['문장']='나흘째에는 물이 줄었다.'
        self.originals[2]['문장']='닷새째 저녁에는 물을 보충했다.'
        self.assertTrue(any(i['title']=='해설의 그날과 근거 날짜 대조' for i in self.findings()))
        self.originals[2]['문장']='나흘째 저녁에는 물을 보충했다.'
        self.assertFalse(any(i['title']=='해설의 그날과 근거 날짜 대조' for i in self.findings()))

    def test_first_state_explanation_reference_must_be_in_evidence(self):
        q=self.questions[-1]
        self.snapshot['sheets']['5_문항']['headers'].append('겨냥 학습 요소·어휘')
        q.update(질문='처음과 달라진 생각은?', **{'겨냥 학습 요소·어휘':'처음 지루했다는 말(2)과 즐거워진 모습을 비교한다.','정답 근거(지문 문장)':'문장0·1'})
        self.assertTrue(any(i['title']=='처음과 변화 비교의 근거 누락' for i in self.findings()))
        q['정답 근거(지문 문장)']='문장0·1·2'
        self.assertFalse(any(i['title']=='처음과 변화 비교의 근거 누락' for i in self.findings()))

    def test_uncertain_redundancy_is_not_a_mandatory_error(self):
        result=answer();self.originals[1]['문장']='쉬엄쉬엄 천천히 걷는다.'
        result['issues']=[{'category':'4','severity':'error','check':'겹말','title':'반복 확인','sheet':'6_지문원문','row':3,'field':'문장','quote':self.originals[1]['문장'],'reason':'뜻이 겹친다.','suggestion':'줄인다.','evidence_cells':[]}]
        finding=review.validate_ai(result,self.snapshot,self.item)[0]
        self.assertEqual(finding['severity'],'warning')
        self.assertEqual(finding['ai_severity'],'error')
        self.originals[1]['문장']='미리 사전에 확인한다.'
        result['issues'][0]['quote']=self.originals[1]['문장']
        self.assertEqual(review.validate_ai(result,self.snapshot,self.item)[0]['severity'],'error')

    def test_word_presence_missing_mark_and_inflected_controls(self):
        vocab=self.snapshot['sheets']['3_어휘']
        vocab['headers'].append('앱: 지문1 등장')
        word=vocab['rows'][0]
        word.update(낱말='거르다', **{'앱: 지문1 등장':'O'})
        title='어휘의 지문1 등장 표시 대조'
        found=[i for i in self.findings() if i['title']==title]
        self.assertEqual(len(found),1)
        self.assertEqual(found[0]['severity'],'warning')
        for sentence in ['아침밥을 거르는 습관', '아침밥을 거른 학생', '끼니를 걸렀다.']:
            self.originals[0]['문장']='[가] [나] [다] [라] [마] '+sentence
            self.assertFalse(any(i['title']==title for i in self.findings()),sentence)
        word['낱말']='생생하다'
        self.originals[0]['문장']='[가] [나] [다] [라] [마] 생생한 표현'
        self.assertFalse(any(i['title']==title for i in self.findings()))

    def test_word_presence_unprovided_text_and_short_predicate_are_not_errors(self):
        vocab=self.snapshot['sheets']['3_어휘'];vocab['headers'].append('앱: 지문1 등장')
        vocab['rows'][0].update(낱말='보다', **{'앱: 지문1 등장':'O'})
        title='어휘의 지문1 등장 표시 대조'
        self.assertFalse(any(i['title']==title for i in self.findings()))
        vocab['rows'][0]['낱말']='거르다'
        self.snapshot['sheets']['6_지문원문']['rows']=self.originals[1:]
        self.assertFalse(any(i['title']==title for i in self.findings()))

    def replacement_fixture(self):
        vocab=self.snapshot['sheets']['3_어휘'];vocab['headers'].append('앱: 어휘 퀴즈')
        vocab['rows'][0].update(낱말='마련하다', **{'예문(단어 카드)':self.originals[1]['문장'],'앱: 어휘 퀴즈':'-'})
        self.originals[1]['핵심 문장']='핵심 · ⓐ'
        self.questions[0].update(유형='바꿔 쓰기', 질문='ⓐ와 바꿔 쓸 말은?', **{'문항 번호':'퀴즈1'})
        return vocab

    def test_unique_replacement_target_connection_and_correct_link(self):
        vocab=self.replacement_fixture();title='표시 낱말의 바꿔 쓰기 퀴즈 연결 확인'
        found=[i for i in self.findings() if i['title']==title]
        self.assertEqual(len(found),1)
        self.assertEqual(found[0]['severity'],'warning')
        vocab['rows'][0]['앱: 어휘 퀴즈']='1번, 3번'
        self.assertFalse(any(i['title']==title for i in self.findings()))

    def test_replacement_target_ambiguity_and_missing_marker_are_not_overflagged(self):
        vocab=self.replacement_fixture();title='표시 낱말의 바꿔 쓰기 퀴즈 연결 확인'
        vocab['rows'][1]['예문(단어 카드)']=self.originals[1]['문장']
        self.assertFalse(any(i['title']==title for i in self.findings()))
        vocab['rows'][1]['예문(단어 카드)']='다른 예문'
        self.originals[1]['핵심 문장']='핵심'
        self.assertFalse(any(i['title']==title for i in self.findings()))

    def ai_issue(self, title, field, **extra):
        return dict(kind='ai',severity='error',title=title,sheet='5_문항',
                    row=self.questions[0]['_row'],field=field,check='연결·표시',**extra)

    def test_internal_index_claim_is_warning_only_when_offset_matches(self):
        q=self.questions[0];q.update(정답='④', **{'겨냥 학습 요소·어휘':'두 방법을 쓴 것은 3번뿐이다.'})
        issue=self.ai_issue('정답 보기 번호 불일치','겨냥 학습 요소·어휘')
        result=review.assess_ai_findings(self.snapshot,[issue])[0]
        self.assertEqual(result['severity'],'warning')
        self.assertEqual(result['ai_severity'],'error')
        self.assertEqual(issue['severity'],'error')
        self.assertEqual(review.assess_ai_findings(self.snapshot,[result]),[result])
        q['정답']='⑤'
        self.assertEqual(review.assess_ai_findings(self.snapshot,[issue])[0]['severity'],'error')

    def test_editorial_label_claim_retained_and_true_missing_material_not_downgraded(self):
        q=self.questions[0];q['질문']='〈묶음〉 가,나. 〈보기〉처럼 나눈 기준은?'
        issue=self.ai_issue('자료 명칭 불일치','질문')
        self.assertEqual(review.assess_ai_findings(self.snapshot,[issue])[0]['severity'],'warning')
        q['질문']='제시되지 않은 자료는?'
        self.assertEqual(review.assess_ai_findings(self.snapshot,[issue])[0]['severity'],'error')

    def test_existing_evidence_addition_is_suggestion_but_empty_evidence_not_exempt(self):
        q=self.questions[0];issue=self.ai_issue('근거 문장 추가','정답 근거(지문 문장)')
        self.assertEqual(review.assess_ai_findings(self.snapshot,[issue])[0]['severity'],'warning')
        q['정답 근거(지문 문장)']='-'
        self.assertEqual(review.assess_ai_findings(self.snapshot,[issue])[0]['severity'],'error')
        issue['kind']='rule';q['정답 근거(지문 문장)']='문장0'
        self.assertEqual(review.assess_ai_findings(self.snapshot,[issue])[0]['severity'],'error')

    def test_quiz_scope_claim_is_policy_check_and_answer_error_is_retained(self):
        issue=self.ai_issue('퀴즈 연결 누락','앱: 어휘 퀴즈');issue['sheet']='3_어휘'
        self.assertEqual(review.assess_ai_findings(self.snapshot,[issue])[0]['severity'],'warning')
        issue=self.ai_issue('보기와 정답 모순','정답')
        self.assertEqual(review.assess_ai_findings(self.snapshot,[issue])[0]['severity'],'error')

    def test_goal_coverage_gap_checks_other_activities_but_explicit_contradiction_stays(self):
        issue=self.ai_issue('쓰기 활동 누락','소단원·학습목표');issue.update(sheet='2_세트목록',check='학습 목표')
        self.assertEqual(review.assess_ai_findings(self.snapshot,[issue])[0]['severity'],'warning')
        issue['title']='목표와 내용의 명시적 모순'
        self.assertEqual(review.assess_ai_findings(self.snapshot,[issue])[0]['severity'],'error')

    def test_normal_review_applies_calibration_and_each_new_run_calls_ai(self):
        calls=[]
        def ai(snapshot,item,cancel):
            calls.append(item['id'])
            issue=self.ai_issue('퀴즈 연결 누락','앱: 어휘 퀴즈')
            issue.update(set_id=item['id'],sheet='3_어휘',quote='-',reason='연결 범위 확인',suggestion='정책 확인',url=review.SHEET_URL)
            return {'issues':[issue],'summary':'검토 완료','difficulty':'적합','section_results':answer()['section_results']}
        with tempfile.TemporaryDirectory() as folder:
            manager=app.ReviewManager(folder,downloader=lambda *a:copy.deepcopy(self.snapshot),ai_reviewer=ai)
            ids=[]
            for _ in range(2):
                ids.append(manager.start({'grades':[3],'ai':True}))
                report=finish(manager)
                self.assertEqual(report['status'],'complete')
                self.assertEqual(report['engine_version'],review.ENGINE_VERSION)
                self.assertEqual(report['issues'][0]['severity'],'warning')
                self.assertIn('판정 유의',review.markdown_report(report))
                self.assertNotIn('ai_source_run_id',report)
            self.assertNotEqual(*ids)
            self.assertEqual(calls,[self.item['id'],self.item['id']])


if __name__=='__main__':unittest.main()
