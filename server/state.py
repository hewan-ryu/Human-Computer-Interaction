"""
작성 세션 하나의 상태: 질문 루프 + 실시간 글 + 지표. 개인 DB(writing_sessions)에 JSON으로 저장된다.
"""

from __future__ import annotations

import uuid
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field


def new_block_id() -> str:
    return "b_" + uuid.uuid4().hex[:10]


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str
    kind: Literal["normal", "memo", "skip"] = "normal"     # memo: 메모로 시작, skip: 넘어가기
    include: bool = True                     # 사용자 답변: False면 '반영 없이 넘어가기' (글에 쓰지 않음)
    empathy: str = ""                        # AI 질문: 질문 앞에 붙는 짧은 공감
    reason: Optional[str] = None             # AI 질문: + 버튼으로 보는 질문 이유


class AskedQuestion(BaseModel):
    question: str
    target: str                              # "차별점" 또는 "측면/하위요소"
    kind: Literal["tail", "followup", "opening"]
    reason: Optional[str] = None


class CachedDoc(BaseModel):
    doc_id: str
    title: str = ""
    url: str = ""
    body: str


# ---------------------------------------------------------------- 글 (왼쪽 편집기)
class Block(BaseModel):
    id: str = Field(default_factory=new_block_id)
    text: str
    locked: bool = False                     # 사용자가 직접 고친 문단 → AI가 다시 고치지 않음


class Article(BaseModel):
    title: str = ""
    title_locked: bool = False
    blocks: List[Block] = Field(default_factory=list)
    deleted_texts: List[str] = Field(default_factory=list)   # 사용자가 지운 문단 (다시 쓰지 않음)

    def plain_text(self) -> str:
        return "\n\n".join(b.text for b in self.blocks if b.text.strip())

    def text_hash(self) -> str:
        import hashlib
        return hashlib.sha1((self.title + "\n" + self.plain_text()).encode()).hexdigest()[:12]


class Metrics(BaseModel):
    coverage: Optional[int] = None           # 들어가야 할 내용 % (기준 항목이 없으면 None)
    distinctiveness: int = 0                 # 차별성 있는 내용 %
    must_have: List[str] = Field(default_factory=list)
    covered: List[str] = Field(default_factory=list)
    missing: List[str] = Field(default_factory=list)
    sentence_count: int = 0
    text_hash: str = ""                       # 계산할 때의 글 (바뀌면 다시 계산 필요)


# ---------------------------------------------------------------- 세션
class SessionState(BaseModel):
    session_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    memo_id: Optional[str] = None

    # ---- 질문 루프 (명세의 상태)
    conversation: List[Message] = Field(default_factory=list)
    cached_docs: List[CachedDoc] = Field(default_factory=list)       # 현재 라운드 검색 결과
    used_doc_ids: List[str] = Field(default_factory=list)            # 모든 라운드 누적
    aspect_map: Dict[str, Dict[str, List[str]]] = Field(default_factory=dict)
    asked_questions: List[AskedQuestion] = Field(default_factory=list)

    # ---- 추가 상태
    all_aspects: Dict[str, Dict[str, List[str]]] = Field(default_factory=dict)   # 모든 라운드 측면 누적 (지표용)
    skipped: List[AskedQuestion] = Field(default_factory=list)       # '이 질문은 넘어갈게요'
    priority_targets: List[str] = Field(default_factory=list)        # '더 쓰러 가기': 먼저 물을 부족한 항목
    round: int = 0
    round_limit: int = 3
    round_active: bool = False
    last_query: str = ""
    current_tail: Optional[AskedQuestion] = None
    followup_count: int = 0
    finished: bool = False
    finish_reason: Optional[Literal["no_more_docs", "max_rounds", "user_stop"]] = None
    needs_topic: bool = False                # 사용자가 쓸 경험이 없다고 함 → 주제 추천 버튼

    # ---- 글 / 지표
    article: Article = Field(default_factory=Article)
    drafted_turns: int = 0                   # 글에 반영된 사용자 재료(답변·메모) 수
    final_turns: int = -1                    # 최종 완성 글을 쓸 때의 재료 수 (-1: 아직 안 씀)
    metrics: Optional[Metrics] = None

    def material_count(self) -> int:
        """글의 재료가 되는 사용자 메시지 수 (반영하기로 한 답변 + 메모)."""
        return sum(1 for m in self.conversation if m.role == "user" and m.kind != "skip" and m.include)

    def needs_final(self) -> bool:
        """질문이 끝났는데 최종 완성 글을 아직 안 썼거나, 그 뒤로 재료가 늘었음."""
        return self.finished and self.material_count() > 0 and self.final_turns != self.material_count()

    def skipped_targets(self) -> set:
        return {q.target for q in self.skipped if q.target and q.target != "차별점"}

    def public_view(self) -> dict:
        """front에 돌려줄 상태 (참고 글 본문 제외)."""
        return {
            "session_id": self.session_id,
            "conversation": [m.model_dump() for m in self.conversation],
            "current_question": self.current_tail.model_dump() if self.current_tail else None,
            "round": self.round,
            "aspect_map": self.aspect_map,
            "references": [{"doc_id": d.doc_id, "title": d.title, "url": d.url} for d in self.cached_docs],
            "finished": self.finished,
            "finish_reason": self.finish_reason,
            "article": self.article.model_dump(),
            "draft_stale": self.drafted_turns < self.material_count() or self.needs_final(),
            "needs_final": self.needs_final(),
            "needs_topic": self.needs_topic,
            "metrics": self.metrics.model_dump() if self.metrics else None,
            "metrics_stale": bool(self.metrics) and self.metrics.text_hash != self.article.text_hash(),
        }
