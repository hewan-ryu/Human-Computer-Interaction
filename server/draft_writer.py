"""
실시간 글 갱신: 대화 + 현재 글(잠긴 문단 포함) + 문체 프로필 → 새 글

"사용자가 고친 부분은 절대 다시 수정하지 않는다"는 프롬프트에만 맡기지 않고
merge_draft()가 코드로 보장한다:
  - 잠긴 문단은 LLM 출력과 상관없이 원문 그대로 다시 넣는다.
  - LLM이 빠뜨린 잠긴 문단은 원래 앞 문단 뒤에 다시 끼워 넣는다.
  - 잠긴 문단끼리의 순서는 원래 순서를 지킨다.
  - 제목이 잠겨 있으면 제목도 그대로 둔다.
"""

from __future__ import annotations

import json
from typing import Callable, Optional

from .state import Article, Block, SessionState, new_block_id

SCHEMA_DRAFT = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "blocks": {"type": "array", "items": {
            "type": "object",
            "properties": {"id": {"type": "string"}, "text": {"type": "string"}},
            "required": ["id", "text"],
            "additionalProperties": False,
        }},
    },
    "required": ["title", "blocks"],
    "additionalProperties": False,
}


def style_section(profile: Optional[dict]) -> str:
    if not profile or not profile.get("features"):
        return "## 이 사용자의 문체\n(아직 문체 프로필이 없음: 담백하고 자연스러운 블로그 해요체로 쓴다)"
    f = profile["features"]
    lines = [
        "## 이 사용자의 문체",
        f"- 요약: {f.get('summary', '')}",
        f"- 말투: {f.get('tone', '')}",
        f"- 자주 쓰는 끝맺음: {', '.join(f.get('sentence_endings', []))}",
        f"- 문장 길이: {f.get('sentence_length', '')}",
        f"- 이모지·기호: {f.get('emoji_and_symbols', '')}",
        f"- 줄바꿈·형식: {f.get('formatting', '')}",
        f"- 전개 방식: {f.get('structure', '')}",
        f"- 버릇: {', '.join(f.get('habits', []))}",
        "",
        "## 이 사용자가 실제로 쓴 문단 예시 (문체만 참고, 내용은 가져오지 말 것)",
    ]
    for ex in profile.get("examples", []):
        lines.append(f'<example from="{ex.get("title", "")}">\n{ex["excerpt"]}\n</example>')
    return "\n".join(lines)


def merge_draft(current: Article, output: dict) -> Article:
    locked = {b.id: b for b in current.blocks if b.locked}
    existing = {b.id for b in current.blocks}
    result: list[Block] = []
    used: set = set()

    for item in output.get("blocks", []):
        bid, text = item.get("id", ""), (item.get("text") or "").strip()
        if bid in locked:
            if bid not in used:
                result.append(locked[bid].model_copy())
                used.add(bid)
        elif bid in existing and bid not in used:
            if text:
                result.append(Block(id=bid, text=text))
                used.add(bid)
        elif text:
            result.append(Block(id=new_block_id(), text=text))

    # 빠진 잠긴 문단 다시 넣기 (원래 바로 앞에 있던 문단 뒤)
    for idx, b in enumerate(current.blocks):
        if not b.locked or b.id in used:
            continue
        pos = 0
        for prev in reversed(current.blocks[:idx]):
            j = next((k for k, r in enumerate(result) if r.id == prev.id), None)
            if j is not None:
                pos = j + 1
                break
        result.insert(pos, b.model_copy())
        used.add(b.id)

    # 잠긴 문단끼리의 순서를 원래대로
    original_locked_order = [b.id for b in current.blocks if b.locked]
    slots = [k for k, r in enumerate(result) if r.locked]
    for k, bid in zip(slots, original_locked_order):
        result[k] = locked[bid].model_copy()

    title = current.title if current.title_locked else ((output.get("title") or "").strip() or current.title)
    return Article(title=title, title_locked=current.title_locked, blocks=result,
                   deleted_texts=list(current.deleted_texts))


def apply_user_edit(current: Article, edited: Article) -> Article:
    """front에서 사용자가 고친 글을 받는다. 사라진 문단은 '사용자가 지운 내용'으로 기록."""
    seen, blocks = set(), []
    for b in edited.blocks:
        bid = b.id if b.id and b.id not in seen else new_block_id()
        seen.add(bid)
        blocks.append(Block(id=bid, text=b.text, locked=b.locked))
    deleted = list(current.deleted_texts)
    for b in current.blocks:
        if b.id not in seen and b.text.strip() and b.text not in deleted:
            deleted.append(b.text)
    return Article(title=edited.title, title_locked=edited.title_locked, blocks=blocks, deleted_texts=deleted)


def write_draft(s: SessionState, profile: Optional[dict], complete_json: Callable, final: bool = False) -> dict:
    """LLM 출력(dict)을 돌려준다. 병합은 호출하는 쪽에서 최신 글 기준으로 merge_draft로 한다.
    final=True: 질문이 끝난 뒤 글 전체를 다듬는 최종 완성. '반영 없이 넘어가기'한 답변은 항상 뺀다."""
    from .question_loop import _load, fmt_conversation

    current = {
        "title": s.article.title,
        "title_locked": s.article.title_locked,
        "blocks": [{"id": b.id, "locked": b.locked, "text": b.text} for b in s.article.blocks],
    }
    user = (
        f"<conversation>\n{fmt_conversation(s, for_draft=True)}\n</conversation>\n\n"
        f"<current_article>\n{json.dumps(current, ensure_ascii=False, indent=1)}\n</current_article>\n\n"
        f"<deleted_by_user>\n{json.dumps(s.article.deleted_texts, ensure_ascii=False)}\n</deleted_by_user>"
    )
    system = _load("draft.md") + ("\n\n" + _load("draft_final.md") if final else "") + "\n\n" + style_section(profile)
    return complete_json("draft_final" if final else "draft", system, user, SCHEMA_DRAFT)
