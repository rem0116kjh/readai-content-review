"""Versioned user review criteria and summaries; no network or model calls."""
import hashlib
import copy
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent
VERSION = "2026-10-20.5"
TEXT = (ROOT / "검수기준.md").read_text()
HASH = hashlib.sha256(TEXT.encode() + (ROOT / "검수_추가기준_원문.txt").read_bytes()).hexdigest()
TABS = ("2_세트목록", "3_어휘", "5_문항", "6_지문원문")
REVIEW_ORDER = ("2_세트목록", "3_어휘", "6_지문원문", "5_문항")
VERDICTS = {"맞음", "수정 필요", "확인 필요"}
DIFFICULTIES = {"적합", "다소 어려움", "다소 쉬움"}
CHECKS = {"오탈자", "문법·문장", "겹말", "학년·난이도", "어휘", "학습 목표", "문항·정답", "연결·표시", "공통 내용"}
EXCLUDED_FIELDS = {"검수자", "검수 메모", "JSON 파일", "상태", "검수(통과/수정)",
                   "주 성취기준", "부 성취기준", "교과서 근거", "교과서 본문 빈도"}

class PreparedSnapshot(dict):
    """Private, immutable-by-convention input for one run; never caches AI results."""
    def __init__(self, snapshot):
        super().__init__(copy.deepcopy(snapshot))
        self.rows_by_set = {tab: {} for tab in TABS}
        self.input_cache = {}
        for tab in TABS:
            for row in self.get("sheets", {}).get(tab, {}).get("rows", []):
                self.rows_by_set[tab].setdefault(row.get("세트ID"), []).append(row)


def prepare(snapshot):
    return snapshot if isinstance(snapshot, PreparedSnapshot) else PreparedSnapshot(snapshot)


def rows_for(snapshot, tab, sid):
    if isinstance(snapshot, PreparedSnapshot):
        return list(snapshot.rows_by_set[tab].get(sid, []))
    return [r for r in snapshot.get("sheets", {}).get(tab, {}).get("rows", []) if r.get("세트ID") == sid]


def info():
    return {"version": VERSION, "sha256": HASH, "text": TEXT}


def partial(snapshot, tab):
    sheet = snapshot.get("sheets", {}).get(tab)
    return bool(snapshot.get("partial") or not sheet or sheet.get("partial") or sheet.get("missing_columns"))


def clean(row):
    return {k: v for k, v in row.items() if k not in EXCLUDED_FIELDS}


def inputs(snapshot, sid):
    """Only current supplied data; contextual evidence is explicitly whitelisted."""
    if isinstance(snapshot, PreparedSnapshot):
        if sid not in snapshot.input_cache:
            snapshot.input_cache[sid] = _inputs(snapshot, sid)
        # Validators/callers cannot change the next pass's evidence.
        return copy.deepcopy(snapshot.input_cache[sid])
    return _inputs(snapshot, sid)


def _inputs(snapshot, sid):
    own = {t: [clean(r) for r in rows_for(snapshot, t, sid)] for t in TABS}
    words = {re.sub(r"\s", "", r.get("낱말", "")) for r in own["3_어휘"] if r.get("낱말")}
    concepts = {label for r in own["6_지문원문"] if r.get("지문", "").startswith("지문1")
                for label in re.findall(r"\[([^\[\]]+)\]", r.get("문장", ""))}
    topics = {re.sub(r"\s", "", r.get("지문1(개념문) 주제", "")) for r in own["2_세트목록"] if r.get("지문1(개념문) 주제")}
    matching_topics = {r.get("세트ID") for r in snapshot.get("sheets", {}).get("2_세트목록", {}).get("rows", [])
                       if re.sub(r"\s", "", r.get("지문1(개념문) 주제", "")) in topics}
    meta_fields = {"세트ID", "학년", "지문1(개념문) 주제", "지문2 제목", "소단원·학습목표", "_row"}
    word_fields = {"세트ID", "낱말", "품사", "뜻(교과서 문맥)", "예문(단어 카드)", "_row"}
    other_meta = [{k: v for k, v in r.items() if k in meta_fields} for r in snapshot.get("sheets", {}).get("2_세트목록", {}).get("rows", []) if r.get("세트ID") and r["세트ID"] != sid]
    related = {"2_세트목록": other_meta,
               "3_어휘": [{k: v for k, v in r.items() if k in word_fields}
                          for r in snapshot.get("sheets", {}).get("3_어휘", {}).get("rows", [])
                          if r.get("세트ID") and r["세트ID"] != sid and re.sub(r"\s", "", r.get("낱말", "")) in words],
               "6_지문원문": [clean(r) for r in snapshot.get("sheets", {}).get("6_지문원문", {}).get("rows", [])
                              if r.get("세트ID") and r["세트ID"] != sid and r.get("지문", "").startswith("지문1")
                              and (r["세트ID"] in matching_topics or concepts.intersection(re.findall(r"\[([^\[\]]+)\]", r.get("문장", ""))))]}
    return own, related


def summary(snapshot, item, issues):
    sid = item["id"]
    findings = [i for i in issues if i.get("set_id") == sid]
    vocab = rows_for(snapshot, "3_어휘", sid)
    provided = len(vocab)
    checked = sum(bool(r.get("낱말", "").strip()) for r in vocab)
    ai_done = item.get("ai_status") == "complete" and set(item.get("ai_section_results", {})) == set(TABS)
    sections = {}
    for tab in TABS:
        errors = [i for i in findings if i.get("sheet") == tab]
        if any(i["severity"] == "error" for i in errors):
            result = "수정 필요"
        elif errors or partial(snapshot, tab) or not ai_done:
            result = "확인 필요"
        else:
            result = item["ai_section_results"].get(tab, "확인 필요")
        sections[tab] = result
    verdict = "수정 필요" if "수정 필요" in sections.values() else "확인 필요" if "확인 필요" in sections.values() else "맞음"
    checks = {}
    for label in ("오탈자", "겹말", "문법·문장", "문항·정답"):
        count = sum(i.get("check") == label for i in findings)
        has_material = label != "문항·정답" or any(r.get("세트ID") == sid for r in snapshot.get("sheets", {}).get("5_문항", {}).get("rows", []))
        checks[label] = f"{count}건" if count else "없음" if ai_done and has_material else "미검토"
    return {"vocabulary_provided": provided, "vocabulary_checked": checked,
            "vocabulary_text": f"제공된 {provided}개 중 {checked}개 확인",
            "difficulty": item.get("ai_difficulty") if ai_done and any(r.get("세트ID") == sid and r.get("지문", "").startswith("지문2") for r in snapshot.get("sheets", {}).get("6_지문원문", {}).get("rows", [])) else None,
            "verdict": verdict, "sections": sections, "checks": checks,
            "partial": any(partial(snapshot, t) for t in TABS)}
