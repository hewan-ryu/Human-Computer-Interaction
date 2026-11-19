"""
RAG 기반 질문 루프

라운드: Step 1 쿼리 생성 → Step 2 검색 → Step 3 측면 추출 → Step 4 꼬리질문 ⇄ Step 5 추가질문
Step 4에서 더 물을 게 없으면 새 라운드. 종료: 새 글 없음 / 라운드 한도 / 사용자 중단.

추가 동작
- 메모로 시작: 메모 내용을 대화 맨 앞에 넣고, 메모를 바탕으로 첫 질문을 만든다.
- '이 질문은 넘어갈게요': 그 질문(target)을 skipped에 넣고 이 글에서 다시 묻지 않는다.
- '반영 없이 넘어가기'(include=False): 답변은 대화에 남기되 글에는 쓰지 않고, 추가질문 없이 다음 꼬리질문으로.
- 경험 없음: 첫 라운드의 Step 1이 has_experience=false를 주면 검색하지 않고 주제 추천 안내(needs_topic).
- AI 질문마다 짧은 공감(empathy)과 + 버튼으로 보는 질문 이유(reason)를 함께 저장한다.
- '더 쓰러 가기'(reopen): 끝난 세션을 다시 열고, 글에 빠진 항목(priority_targets)부터 묻는다.

LLM 호출·검색 함수는 생성자로 주입한다 (테스트에서 가짜로 교체 가능).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Callable, Iterable, Optional

from .state import AskedQuestion, CachedDoc, Message, SessionState

MAX_ROUNDS = 3
EXTRA_ROUNDS = 2              # '더 쓰러 가기' 한 번에 추가로 허용하는 라운드
DOCS_PER_ROUND = 10
MAX_FOLLOWUPS = 2

CLOSING_MESSAGE = "질문은 여기까지예요. 지금까지 들려주신 이야기로 글을 정리해 볼게요."
SKIP_MESSAGE = "(이 질문은 넘어갈게요)"
REOPEN_MESSAGE = "좋아요, 조금 더 이야기해 볼게요. 글에 아직 부족한 부분부터 여쭤볼게요."
NO_EXPERIENCE_MESSAGE = ("괜찮아요! 아직 떠오르는 경험이 없다면 주제를 추천받아 볼 수도 있어요. "
                         "생각나는 경험이 있으면 언제든 편하게 들려주세요.")
OPENING_REASON = "어떤 경험을 쓰고 싶으신지 알아야 거기에 꼭 맞는 질문을 드릴 수 있어서요."
NOT_INCLUDED_MARK = "(글에 반영 안 함)"
STOP_PHRASES = ("그만", "이제 됐", "됐어요", "충분해", "글 써줘", "글 써 줘", "글 써주세요", "글로 써줘")

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"

CompleteJSON = Callable[[str, str, str, dict], dict]          # (step, system, user, schema) -> dict
Search = Callable[[str, Iterable[str], int], list]            # (query, exclude, k) -> docs
GetDocs = Callable[[list], list]                              # (doc_ids) -> docs

# ---------------------------------------------------------------- JSON 스키마
_NULLABLE_STR = {"anyOf": [{"type": "string"}, {"type": "null"}]}


def _obj(props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


SCHEMA_MEMO_OPENING = _obj({"empathy": {"type": "string"}, "question": {"type": "string"},
                            "reason": {"type": "string"}})
SCHEMA_QUERY = _obj({"has_experience": {"type": "boolean"}, "query": _NULLABLE_STR})
SCHEMA_ASPECTS = _obj({"aspects": {"type": "array", "items": _obj({
    "name": {"type": "string"},
    "elements": {"type": "array", "items": _obj({
        "name": {"type": "string"},
        "doc_ids": {"type": "array", "items": {"type": "string"}},
    })},
})}})
SCHEMA_TAIL = _obj({"empathy": {"type": "string"}, "question": _NULLABLE_STR, "target": {"type": "string"},
                    "reason": {"type": "string"}})
SCHEMA_FOLLOWUP = _obj({"need_followup": {"type": "boolean"}, "empathy": {"type": "string"},
                        "question": _NULLABLE_STR, "reason": _NULLABLE_STR})


class _NoExperience(Exception):
    """첫 라운드 Step 1: 사용자가 아직 쓸 경험을 말하지 않음."""


def _load(name: str) -> str:
    return (PROMPTS / name).read_text(encoding="utf-8").strip()


def fmt_conversation(s: SessionState, for_draft: bool = False) -> str:
    """for_draft=True: 글 쓰기용. 넘긴 질문과 '반영 없이 넘어가기'한 답변(과 그 질문)은 뺀다."""
    msgs = s.conversation
    if for_draft:
        drop = set()
        for i, m in enumerate(msgs):
            if m.role == "user" and (m.kind == "skip" or not m.include):
                drop.add(i)
                if i > 0 and msgs[i - 1].role == "assistant":
                    drop.add(i - 1)
        msgs = [m for i, m in enumerate(msgs) if i not in drop]
    lines = []
    for m in msgs:
        if m.kind == "memo":
            lines.append(f"사용자(미리 적어 둔 메모): {m.content}")
        elif m.kind == "skip":
            lines.append("사용자: (이 질문은 넘어갔어요)")
        elif m.role == "user":
            lines.append(f"사용자: {m.content}" + ("" if m.include else f" {NOT_INCLUDED_MARK}"))
        else:
            lines.append(f"AI: {m.content}")
    return "\n".join(lines)


def reply_text(m: Message) -> str:
    return f"{m.empathy}\n\n{m.content}" if m.empathy else m.content


def _fmt_docs(docs) -> str:
    return "\n\n".join(f'<doc id="{d.doc_id}">\n제목: {d.title}\n{d.body}\n</doc>' for d in docs)


def wants_to_stop(message: str) -> bool:
    compact = message.replace(" ", "")
    return any(p.replace(" ", "") in compact for p in STOP_PHRASES)


def merge_aspects(into: dict, new: dict):
    for aspect, elements in new.items():
        target = into.setdefault(aspect, {})
        for element, ids in elements.items():
            cur = target.setdefault(element, [])
            cur.extend(i for i in ids if i not in cur)


class QuestionLoop:
    def __init__(self, complete_json: CompleteJSON, search: Search, get_docs: Optional[GetDocs] = None):
        self.llm = complete_json
        self.search = search
        self.get_docs = get_docs or (lambda ids: [])
        self.writing_ai_prompt = _load("writing_ai.md")

    # ================================================================ 공개 API
    def start(self, memo_text: Optional[str] = None, memo_id: Optional[str] = None) -> tuple:
        s = SessionState(memo_id=memo_id)
        if memo_text:
            s.conversation.append(Message(role="user", content=memo_text, kind="memo"))
            system = _load("question_loop/opening_from_memo.md") + "\n\n" + self.writing_ai_prompt
            r = self.llm("opening_from_memo", system, f"<memo>\n{memo_text}\n</memo>", SCHEMA_MEMO_OPENING)
            return s, self._ask(s, r["question"].strip(), "", "opening",
                                reason=(r.get("reason") or "").strip() or None,
                                empathy=(r.get("empathy") or "").strip())
        return s, self._ask(s, _load("opening.md"), "", "opening", reason=OPENING_REASON)

    def handle_user_message(self, s: SessionState, message: str, stop: bool = False, skip: bool = False,
                            include: bool = True) -> str:
        """사용자 메시지를 받아 다음 AI 메시지(질문 또는 종료 안내)를 돌려준다. s를 직접 갱신한다.
        include=False: '반영 없이 넘어가기' (글에 쓰지 않고, 추가질문 없이 다음 질문)."""
        if s.finished:
            return CLOSING_MESSAGE
        if skip:
            return self._skip(s)
        message = message.strip()
        if message:
            s.conversation.append(Message(role="user", content=message, include=include))
        if stop or wants_to_stop(message):
            return self._finish(s, "user_stop")
        if not message:
            return reply_text(s.conversation[-1]) if s.conversation else _load("opening.md")

        # 꼬리질문/추가질문에 대한 답이면 Step 5 (반영하지 않는 답이면 더 파고들지 않음)
        if include and s.current_tail is not None and s.followup_count < MAX_FOLLOWUPS:
            r = self.step5_followup(s, message)
            if r:
                s.followup_count += 1
                return self._ask(s, r["question"].strip(), s.current_tail.target, "followup",
                                 reason=(r.get("reason") or "").strip() or None,
                                 empathy=(r.get("empathy") or "").strip())

        return self._next_tail_question(s)

    def reopen(self, s: SessionState, missing: list) -> str:
        """'더 쓰러 가기': 같은 세션에서 대화를 이어가고, 부족한 항목부터 묻는다."""
        skipped = s.skipped_targets()
        s.finished, s.finish_reason = False, None
        s.round_limit = max(s.round_limit, s.round) + EXTRA_ROUNDS
        s.priority_targets = [m for m in missing if m not in skipped]
        s.conversation.append(Message(role="assistant", content=REOPEN_MESSAGE))
        if s.all_aspects:                    # 지난 라운드들의 측면 전체로 다시 시작
            s.aspect_map = copy.deepcopy(s.all_aspects)
            ids = [i for els in s.all_aspects.values() for doc_ids in els.values() for i in doc_ids]
            docs = self.get_docs(list(dict.fromkeys(ids)))
            if docs:
                s.cached_docs = [CachedDoc(**{k: d.get(k, "") for k in ("doc_id", "title", "url", "body")})
                                 for d in docs]
            s.round_active = True
        q = self._next_tail_question(s)
        return f"{REOPEN_MESSAGE}\n\n{q}" if q != CLOSING_MESSAGE else q

    def _no_experience(self, s: SessionState) -> str:
        s.needs_topic = True
        s.current_tail, s.round_active = None, False
        for m in s.conversation:             # "없어요" 같은 답은 글 재료가 아님
            if m.role == "user" and m.kind == "normal":
                m.include = False
        return self._ask(s, NO_EXPERIENCE_MESSAGE, "", "opening")

    # ================================================================ 루프 제어
    def _skip(self, s: SessionState) -> str:
        last = s.current_tail or (s.asked_questions[-1] if s.asked_questions else None)
        if last is not None:
            s.skipped.append(last)
        s.conversation.append(Message(role="user", content=SKIP_MESSAGE, kind="skip"))
        has_story = any(m.role == "user" and m.kind != "skip" for m in s.conversation)
        if not has_story:                    # 첫 질문을 넘겼고 아직 이야기가 하나도 없음
            return self._no_experience(s)
        return self._next_tail_question(s)

    def _next_tail_question(self, s: SessionState) -> str:
        s.current_tail, s.followup_count = None, 0
        while True:
            try:
                if not s.round_active and not self._start_round(s):
                    return CLOSING_MESSAGE
            except _NoExperience:
                return self._no_experience(s)
            result = self.step4_tail_question(s)
            if result.get("question"):
                q = self._ask(s, result["question"].strip(), result.get("target", ""), "tail",
                              reason=(result.get("reason") or "").strip() or None,
                              empathy=(result.get("empathy") or "").strip())
                s.current_tail = s.asked_questions[-1]
                s.priority_targets = [t for t in s.priority_targets if t != s.current_tail.target]
                return q
            s.round_active = False          # 이번 라운드에서 더 물을 게 없음 → 새 라운드
            s.priority_targets = []

    def _start_round(self, s: SessionState) -> bool:
        if s.round >= s.round_limit:
            self._finish(s, "max_rounds")
            return False
        has_experience, query = self.step1_query(s)
        if s.round == 0 and not has_experience:
            raise _NoExperience()
        s.needs_topic = False
        s.last_query = query or s.last_query or fmt_conversation(s)[-200:]
        if not self.step2_search(s, s.last_query):
            self._finish(s, "no_more_docs")
            return False
        s.round += 1
        s.aspect_map = self.step3_aspects(s)
        merge_aspects(s.all_aspects, s.aspect_map)
        s.round_active = True
        return True

    def _ask(self, s: SessionState, question: str, target: str, kind: str,
             reason: Optional[str] = None, empathy: str = "") -> str:
        s.asked_questions.append(AskedQuestion(question=question, target=target, kind=kind, reason=reason))
        msg = Message(role="assistant", content=question, empathy=empathy, reason=reason)
        s.conversation.append(msg)
        return reply_text(msg)

    def _finish(self, s: SessionState, reason: str) -> str:
        s.finished, s.finish_reason = True, reason
        s.current_tail, s.round_active = None, False
        s.conversation.append(Message(role="assistant", content=CLOSING_MESSAGE))
        return CLOSING_MESSAGE

    # ================================================================ Step 1~5
    def step1_query(self, s: SessionState) -> tuple:
        """(has_experience, query)"""
        user = f"<conversation>\n{fmt_conversation(s)}\n</conversation>"
        r = self.llm("step1_query", _load("question_loop/step1_query.md"), user, SCHEMA_QUERY)
        query = (r.get("query") or "").strip()
        return bool(r.get("has_experience", True)) and bool(query), query

    def step2_search(self, s: SessionState, query: str) -> bool:
        used = set(s.used_doc_ids)
        docs = [d for d in self.search(query, s.used_doc_ids, DOCS_PER_ROUND) if d["doc_id"] not in used]
        docs = docs[:DOCS_PER_ROUND]
        if not docs:
            return False
        s.cached_docs = [CachedDoc(**{k: d.get(k, "") for k in ("doc_id", "title", "url", "body")}) for d in docs]
        s.used_doc_ids.extend(d.doc_id for d in s.cached_docs)
        return True

    def step3_aspects(self, s: SessionState) -> dict:
        result = self.llm("step3_aspects", _load("question_loop/step3_aspects.md"),
                          f"<documents>\n{_fmt_docs(s.cached_docs)}\n</documents>", SCHEMA_ASPECTS)
        valid_ids = {d.doc_id for d in s.cached_docs}
        aspect_map: dict = {}
        for a in result.get("aspects", []):
            elements = aspect_map.setdefault(a["name"].strip(), {})
            for e in a.get("elements", []):
                ids = elements.setdefault(e["name"].strip(), [])
                ids.extend(i for i in e.get("doc_ids", []) if i in valid_ids and i not in ids)
        return aspect_map

    def step4_tail_question(self, s: SessionState) -> dict:
        names_only = {aspect: {e: len(ids) for e, ids in elements.items()}   # 요소별 참고 글 수
                      for aspect, elements in s.aspect_map.items()}
        asked = [{"question": q.question, "target": q.target} for q in s.asked_questions]
        skipped = [{"question": q.question, "target": q.target} for q in s.skipped]
        user = (
            f"<aspect_map>\n{json.dumps(names_only, ensure_ascii=False, indent=1)}\n</aspect_map>\n\n"
            f"<conversation>\n{fmt_conversation(s)}\n</conversation>\n\n"
            f"<asked_questions>\n{json.dumps(asked, ensure_ascii=False, indent=1)}\n</asked_questions>\n\n"
            f"<skipped_questions>\n{json.dumps(skipped, ensure_ascii=False, indent=1)}\n</skipped_questions>\n\n"
            f"<priority_targets>\n{json.dumps(s.priority_targets, ensure_ascii=False)}\n</priority_targets>"
        )
        system = _load("question_loop/step4_tail_question.md") + "\n\n" + self.writing_ai_prompt
        skipped_targets = s.skipped_targets()
        for _ in range(2):                   # 넘긴 주제를 또 고르면 한 번 더 시도, 그래도면 없음으로 처리
            result = self.llm("step4_tail_question", system, user, SCHEMA_TAIL)
            if not result.get("question") or result.get("target", "").strip() not in skipped_targets:
                return result
            user += f"\n\n(주의: '{result['target']}'는 사용자가 넘긴 주제입니다. 다른 것을 고르세요.)"
        return {"empathy": "", "question": None, "target": "", "reason": "넘긴 주제만 남음"}

    def step5_followup(self, s: SessionState, answer: str) -> Optional[dict]:
        tail = s.current_tail
        last_question = s.asked_questions[-1].question if s.asked_questions else tail.question
        refs_text = _fmt_docs(self.reference_docs(s, tail.target)) or "(없음)"
        user = (
            f"<tail_question>\n질문: {tail.question}\ntarget: {tail.target}\n</tail_question>\n\n"
            f"<last_question>\n{last_question}\n</last_question>\n\n"
            f"<answer>\n{answer}\n</answer>\n\n"
            f"<conversation>\n{fmt_conversation(s)}\n</conversation>\n\n"
            f"<references>\n{refs_text}\n</references>"
        )
        system = _load("question_loop/step5_followup.md") + "\n\n" + self.writing_ai_prompt
        result = self.llm("step5_followup", system, user, SCHEMA_FOLLOWUP)
        return result if result.get("need_followup") and result.get("question") else None

    @staticmethod
    def reference_docs(s: SessionState, target: str) -> list:
        """target("측면/하위요소" 또는 "측면")에 연결된 cached_docs. "차별점"이면 없음."""
        if not target or target.strip() == "차별점":
            return []
        aspect, _, element = (p.strip() for p in target.partition("/"))
        elements = s.aspect_map.get(aspect, {})
        ids = elements.get(element, []) if element else [i for v in elements.values() for i in v]
        wanted = set(ids)
        return [d for d in s.cached_docs if d.doc_id in wanted]
