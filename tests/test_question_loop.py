"""질문 루프 단위 테스트 (가짜 LLM·가짜 검색, API 키 불필요). 실행: python -m pytest tests"""

import numpy as np
import pytest

from rag.vector_db import VectorDB
from server.question_loop import CLOSING_MESSAGE, MAX_FOLLOWUPS, QuestionLoop


def make_docs(n, prefix="doc"):
    return [{"doc_id": f"{prefix}_{i}", "title": f"t{i}", "url": f"u{i}", "body": f"본문 {i}"} for i in range(n)]


class FakeLLM:
    """step 이름별로 미리 정한 응답을 차례로 돌려준다."""

    def __init__(self, tails=None, followups=None, targets=None, experience=None, empathy=""):
        self.calls = []
        self.experience = list(experience or [])     # step1 has_experience 응답 차례 (없으면 True)
        self.empathy = empathy
        self.tails = list(tails or [])
        self.followups = list(followups or [])
        self.targets = list(targets or [])

    def __call__(self, step, system, user, schema):
        self.calls.append((step, system, user))
        if step == "opening_from_memo":
            return {"question": "메모 속 그날 이야기를 더 들려주세요."}
        if step == "step1_query":
            has = self.experience.pop(0) if self.experience else True
            return {"has_experience": has, "query": "성수동 카페 방문 후기" if has else None}
        if step == "step3_aspects":
            ids = [line.split('"')[1] for line in user.splitlines() if line.startswith("<doc id=")]
            return {"aspects": [
                {"name": "분위기", "elements": [{"name": "조명", "doc_ids": ids[:2] + ["doc_없음"]},
                                               {"name": "음악", "doc_ids": ids[2:3]}]},
                {"name": "가격", "elements": [{"name": "메뉴별 가격대", "doc_ids": ids[3:4]}]},
            ]}
        if step == "step4_tail_question":
            q = self.tails.pop(0) if self.tails else None
            t = self.targets.pop(0) if self.targets else "분위기/조명"
            return {"empathy": self.empathy, "question": q, "target": t, "reason": "r"}
        if step == "step5_followup":
            q = self.followups.pop(0) if self.followups else None
            return {"need_followup": q is not None, "question": q, "reason": "r"}
        raise AssertionError(step)

    def steps(self):
        return [c[0] for c in self.calls]


class FakeSearch:
    def __init__(self, docs):
        self.docs = docs
        self.excludes = []

    def __call__(self, query, exclude, k):
        exclude = set(exclude)
        self.excludes.append(exclude)
        return [d for d in self.docs if d["doc_id"] not in exclude][:k]


def test_first_answer_runs_round_and_asks_tail_question():
    llm, search = FakeLLM(tails=["조명은 어떤 느낌이었나요?"]), FakeSearch(make_docs(15))
    loop = QuestionLoop(llm, search)
    s, opening = loop.start()
    reply = loop.handle_user_message(s, "성수동 카페에 다녀왔어요")

    assert reply == "조명은 어떤 느낌이었나요?"
    assert llm.steps() == ["step1_query", "step3_aspects", "step4_tail_question"]
    assert len(s.cached_docs) == 10 and len(s.used_doc_ids) == 10 and s.round == 1
    assert s.aspect_map["분위기"]["조명"] == ["doc_0", "doc_1"]          # 없는 doc_id는 버림
    assert s.asked_questions[-1].kind == "tail"
    assert [m.role for m in s.conversation] == ["assistant", "user", "assistant"]
    # Step 4에는 원문을 넣지 않는다
    assert "본문 0" not in llm.calls[-1][2]


def test_followups_capped_then_next_tail():
    llm = FakeLLM(tails=["Q1", "Q2"], followups=["F1", "F2", "F3"])
    loop = QuestionLoop(llm, FakeSearch(make_docs(15)))
    s, _ = loop.start()
    assert loop.handle_user_message(s, "카페 다녀옴") == "Q1"
    assert loop.handle_user_message(s, "좋았어요") == "F1"
    assert loop.handle_user_message(s, "음") == "F2"
    # 추가질문 2회 소진 → Step 5 없이 다음 꼬리질문
    assert loop.handle_user_message(s, "그냥요") == "Q2"
    assert llm.steps().count("step5_followup") == MAX_FOLLOWUPS
    assert [q.kind for q in s.asked_questions] == ["opening", "tail", "followup", "followup", "tail"]
    assert s.followup_count == 0


def test_step5_gets_reference_docs_for_target():
    llm = FakeLLM(tails=["Q1", "Q2"], followups=[None])
    loop = QuestionLoop(llm, FakeSearch(make_docs(15)))
    s, _ = loop.start()
    loop.handle_user_message(s, "카페")
    loop.handle_user_message(s, "답")
    step5_input = [c for c in llm.calls if c[0] == "step5_followup"][0][2]
    assert "본문 0" in step5_input and "본문 1" in step5_input and "본문 5" not in step5_input
    assert [d.doc_id for d in loop.reference_docs(s, "차별점")] == []
    assert {d.doc_id for d in loop.reference_docs(s, "분위기")} == {"doc_0", "doc_1", "doc_2"}


def test_null_tail_starts_new_round_excluding_used_docs():
    llm = FakeLLM(tails=[None, "새 라운드 질문"])
    search = FakeSearch(make_docs(25))
    loop = QuestionLoop(llm, search)
    s, _ = loop.start()
    assert loop.handle_user_message(s, "카페") == "새 라운드 질문"
    assert s.round == 2
    assert search.excludes[1] == {f"doc_{i}" for i in range(10)}
    assert [d.doc_id for d in s.cached_docs] == [f"doc_{i}" for i in range(10, 20)]
    assert len(s.used_doc_ids) == 20


def test_partial_then_empty_search_finishes():
    llm = FakeLLM(tails=[None, None])
    loop = QuestionLoop(llm, FakeSearch(make_docs(13)))
    s, _ = loop.start()
    assert loop.handle_user_message(s, "카페") == CLOSING_MESSAGE
    assert s.round == 2                            # 10개 → 3개 → 0개
    assert s.finished and s.finish_reason == "no_more_docs"


def test_max_rounds_finishes():
    llm = FakeLLM(tails=[None] * 5)
    loop = QuestionLoop(llm, FakeSearch(make_docs(100)))
    s, _ = loop.start()
    assert loop.handle_user_message(s, "카페") == CLOSING_MESSAGE
    assert s.round == 3 and s.finish_reason == "max_rounds"
    assert llm.steps().count("step1_query") == 3


@pytest.mark.parametrize("msg,stop", [("이제 그만 물어봐도 돼요", False), ("", True)])
def test_user_stop(msg, stop):
    llm = FakeLLM(tails=["Q1"])
    loop = QuestionLoop(llm, FakeSearch(make_docs(15)))
    s, _ = loop.start()
    loop.handle_user_message(s, "카페")
    n = len(llm.calls)
    assert loop.handle_user_message(s, msg, stop=stop) == CLOSING_MESSAGE
    assert s.finish_reason == "user_stop" and len(llm.calls) == n
    assert loop.handle_user_message(s, "더 얘기할래요") == CLOSING_MESSAGE    # 종료 후엔 추가 호출 없음
    assert len(llm.calls) == n


def test_vector_db_search_orders_and_excludes(tmp_path):
    docs = make_docs(3)
    emb = np.array([[1, 0], [0.6, 0.8], [0, 1]], dtype=np.float32)
    VectorDB(docs, emb).save(tmp_path)
    db = VectorDB.load(tmp_path)
    q = np.array([1, 0], dtype=np.float32)
    assert [d["doc_id"] for d in db.search(q, k=2)] == ["doc_0", "doc_1"]
    assert [d["doc_id"] for d in db.search(q, k=5, exclude={"doc_0"})] == ["doc_1", "doc_2"]


def test_split_chunks():
    from rag.embedder import split_chunks
    chunks = split_chunks("가" * 200 + ". " + "나" * 200 + ". 다.", max_chars=300)
    assert len(chunks) == 2 and all(len(c) <= 300 for c in chunks)


# ---------------------------------------------------------------- 넘어가기 / 메모 / 더 쓰러 가기
from server.question_loop import NO_EXPERIENCE_MESSAGE, OPENING_REASON, REOPEN_MESSAGE, fmt_conversation  # noqa: E402


def test_skip_records_target_and_moves_on():
    llm = FakeLLM(tails=["Q1", "Q2"], followups=["F1"], targets=["분위기/조명", "가격/메뉴별 가격대"])
    loop = QuestionLoop(llm, FakeSearch(make_docs(15)))
    s, _ = loop.start()
    assert loop.handle_user_message(s, "카페 다녀왔어요") == "Q1"
    n5 = llm.steps().count("step5_followup")
    assert loop.handle_user_message(s, "", skip=True) == "Q2"
    assert llm.steps().count("step5_followup") == n5          # 넘기면 추가질문 없이 다음 꼬리질문
    assert [q.target for q in s.skipped] == ["분위기/조명"]
    assert s.conversation[-2].kind == "skip"
    step4_input = [c for c in llm.calls if c[0] == "step4_tail_question"][-1][2]
    assert "분위기/조명" in step4_input.split("<skipped_questions>")[1]


def test_step4_choosing_skipped_target_is_retried():
    llm = FakeLLM(tails=["Q1", "같은 주제 또", "다른 질문"],
                  targets=["분위기/조명", "분위기/조명", "가격/메뉴별 가격대"])
    loop = QuestionLoop(llm, FakeSearch(make_docs(15)))
    s, _ = loop.start()
    loop.handle_user_message(s, "카페")
    assert loop.handle_user_message(s, "", skip=True) == "다른 질문"
    assert "같은 주제 또" not in [q.question for q in s.asked_questions]


def test_skip_opening_without_story():
    llm = FakeLLM()
    loop = QuestionLoop(llm, FakeSearch(make_docs(15)))
    s, _ = loop.start()
    assert loop.handle_user_message(s, "", skip=True) == NO_EXPERIENCE_MESSAGE
    assert llm.calls == [] and s.needs_topic


def test_start_from_memo():
    llm = FakeLLM(tails=["Q1"])
    loop = QuestionLoop(llm, FakeSearch(make_docs(15)))
    s, first = loop.start(memo_text="성수 카페 창가 자리, 비 오는 날", memo_id="m1")
    assert first == "메모 속 그날 이야기를 더 들려주세요."
    assert s.memo_id == "m1" and s.conversation[0].kind == "memo"
    loop.handle_user_message(s, "토요일 오후였어요")
    query_input = [c for c in llm.calls if c[0] == "step1_query"][0][2]
    assert "성수 카페 창가 자리" in query_input                 # 메모가 검색 쿼리 재료에 들어감


def test_reopen_asks_missing_items_first():
    llm = FakeLLM(tails=[None, None, None, "부족한 것 질문"], targets=["", "", "", "가격/메뉴별 가격대"])
    docs = make_docs(30)
    loop = QuestionLoop(llm, FakeSearch(docs), get_docs=lambda ids: [d for d in docs if d["doc_id"] in set(ids)])
    s, _ = loop.start()
    assert loop.handle_user_message(s, "카페") == CLOSING_MESSAGE
    assert s.finished and s.round == 3
    s.skipped.append(s.asked_questions[0].model_copy(update={"target": "분위기/음악"}))
    reply = loop.reopen(s, ["가격/메뉴별 가격대", "분위기/음악"])
    assert reply == f"{REOPEN_MESSAGE}\n\n부족한 것 질문"
    assert not s.finished and s.round_limit == 5
    step4_input = [c for c in llm.calls if c[0] == "step4_tail_question"][-1][2]
    assert '["가격/메뉴별 가격대"]' in step4_input                # 넘긴 항목은 우선순위에서 제외
    assert {d.doc_id for d in s.cached_docs} >= {"doc_0", "doc_1"}  # 지난 라운드 참고 글 다시 불러옴
    assert s.priority_targets == []                              # 물어본 항목은 우선순위에서 빠짐


# ---------------------------------------------------------------- 작성 AI 호출 규칙
def test_opening_question_and_reason():
    loop = QuestionLoop(FakeLLM(), FakeSearch(make_docs(15)))
    s, first = loop.start()
    assert first == "최근에 글로 남기고 싶었던 경험이 있나요? 있다면 자세히 작성해주세요."
    assert s.conversation[0].reason == OPENING_REASON


def test_no_experience_shows_topic_then_recovers():
    llm = FakeLLM(tails=["Q1"], experience=[False, True])
    search = FakeSearch(make_docs(15))
    loop = QuestionLoop(llm, search)
    s, _ = loop.start()
    assert loop.handle_user_message(s, "딱히 생각이 안 나요") == NO_EXPERIENCE_MESSAGE
    assert s.needs_topic and s.round == 0 and search.excludes == []     # 검색하지 않음
    assert s.material_count() == 0                                      # "생각이 안 나요"는 글 재료가 아님
    assert loop.handle_user_message(s, "어제 성수 카페에 갔어요") == "Q1"
    assert not s.needs_topic and s.round == 1


def test_empathy_and_reason_attached_to_question():
    llm = FakeLLM(tails=["조명은 어땠어요?"], empathy="창가 자리였군요!")
    loop = QuestionLoop(llm, FakeSearch(make_docs(15)))
    s, _ = loop.start()
    reply = loop.handle_user_message(s, "성수 카페 창가 자리에 앉았어요")
    assert reply == "창가 자리였군요!\n\n조명은 어땠어요?"
    last = s.conversation[-1]
    assert last.content == "조명은 어땠어요?" and last.empathy == "창가 자리였군요!" and last.reason == "r"
    assert s.asked_questions[-1].reason == "r"
    # Step 4에는 요소별 참고 글 수가 들어감
    step4_input = [c for c in llm.calls if c[0] == "step4_tail_question"][-1][2]
    assert '"조명": 2' in step4_input


def test_not_included_answer_skips_followup_and_draft():
    llm = FakeLLM(tails=["Q1", "Q2"], followups=["F1"])
    loop = QuestionLoop(llm, FakeSearch(make_docs(15)))
    s, _ = loop.start()
    loop.handle_user_message(s, "성수 카페에 다녀왔어요")
    n5 = llm.steps().count("step5_followup")
    assert loop.handle_user_message(s, "조명은 그냥 그랬어요", include=False) == "Q2"
    assert llm.steps().count("step5_followup") == n5          # 추가질문 없이 다음 꼬리질문
    assert s.material_count() == 1
    loop_text = fmt_conversation(s)
    draft_text = fmt_conversation(s, for_draft=True)
    assert "조명은 그냥 그랬어요 (글에 반영 안 함)" in loop_text   # 질문 루프는 알고 있음 (다시 묻지 않게)
    assert "조명은 그냥 그랬어요" not in draft_text and "Q1" not in draft_text
    assert "성수 카페에 다녀왔어요" in draft_text
