"""
데모 모드 (환경변수 DEMO_MODE=1): API 키·벡터 DB·크롤링 없이 전체 흐름을 돌려 보기 위한 가짜 구현.

- fake_complete_json: Step별로 그럴듯한 고정 규칙 응답 (실제 LLM 아님)
- search / get_docs: 아래 SAMPLE_DOCS에서 단어가 겹치는 순으로 검색
- sample_posts: 문체 프로필용 예시 블로그 글

화면 동작 확인과 시연용이다. 질문·글 품질은 실제 LLM과 다르다.
"""

from __future__ import annotations

import json
import re

ASPECTS = {
    "분위기": ["조명", "음악", "좌석"],
    "메뉴": ["대표 메뉴", "맛"],
    "가격": ["메뉴별 가격대"],
    "서비스": ["직원 응대"],
    "위치": ["찾아가는 길", "주차"],
}

SAMPLE_DOCS = []
for i in range(30):
    a = list(ASPECTS)[i % len(ASPECTS)]
    words = " ".join(ASPECTS[a])
    SAMPLE_DOCS.append({
        "doc_id": f"demo_{i}", "title": f"동네 카페 후기 {i}", "url": f"https://blog.naver.com/demo/{1000 + i}",
        "body": f"카페에 다녀왔어요. {a} 이야기를 해 볼게요. {words}. 커피 디저트 조용한 창가 자리.",
    })

SAMPLE_POSTS = [
    {"log_no": "1", "title": "비 오는 날 동네 산책", "url": "https://blog.naver.com/demo/1", "published_at": "",
     "body": "오늘은 비가 와서 그냥 집에 있을까 하다가 ㅋㅋ 우산 들고 나갔어요☔\n\n"
             "골목마다 물웅덩이가 있어서 신발이 다 젖었는데 이상하게 기분은 좋더라구요.\n\n다음엔 장화 신고 가야지!"},
    {"log_no": "2", "title": "첫 베이킹 도전기", "url": "https://blog.naver.com/demo/2", "published_at": "",
     "body": "드디어 오븐을 샀어요!! 🎉\n\n첫 작품은 스콘인데... 모양은 좀 그렇지만 맛은 꽤 괜찮았어요 ㅎㅎ\n\n"
             "버터를 너무 오래 섞으면 안 된다는 걸 몸으로 배웠답니다."},
    {"log_no": "3", "title": "주말 전시 다녀옴", "url": "https://blog.naver.com/demo/3", "published_at": "",
     "body": "친구랑 사진전 보고 왔어요📷\n\n사람이 많아서 줄은 길었지만 기다린 보람이 있었어요.\n\n"
             "제일 좋았던 건 마지막 방에 있던 바다 사진! 한참 서 있었어요."},
]


def sample_posts(blog_url: str = "") -> list:
    return [dict(p) for p in SAMPLE_POSTS]


def search(query: str, exclude, k: int) -> list:
    words = set(re.findall(r"\w+", query))
    excluded = set(exclude)
    scored = sorted((d for d in SAMPLE_DOCS if d["doc_id"] not in excluded),
                    key=lambda d: -len(words & set(re.findall(r"\w+", d["body"]))))
    return [dict(d, score=0.0) for d in scored[:k]]


def get_docs(ids: list) -> list:
    wanted = set(ids)
    return [dict(d) for d in SAMPLE_DOCS if d["doc_id"] in wanted]


# ---------------------------------------------------------------- 가짜 LLM
def _between(text: str, tag: str) -> str:
    m = re.search(rf"<{tag}>\n?(.*?)\n?</{tag}>", text, re.S)
    return m.group(1) if m else ""


def _user_answers(conversation: str) -> list:
    out = []
    for line in conversation.splitlines():
        if line.startswith("사용자") and "넘어갔어요" not in line:
            out.append(line.split(":", 1)[1].replace("(글에 반영 안 함)", "").strip())
    return out


NO_EXPERIENCE_WORDS = ("없어", "없네", "모르겠", "생각이 안", "생각 안", "글쎄")


def _empathy(answer: str) -> str:
    if not answer:
        return ""
    snippet = answer if len(answer) <= 14 else answer[:14] + "…"
    return f"'{snippet}' 이야기 들으니 그때 장면이 그려져요!"


DEMO_TAIL_QUESTIONS = 4      # 데모에서 묻는 꼬리질문 수 (짧게)


def fake_complete_json(step: str, system: str, user: str, schema: dict) -> dict:
    if step == "opening_from_memo":
        return {"empathy": "메모에 남겨 두실 만큼 기억에 남는 경험이었나 봐요.",
                "question": "메모에 적어 두신 이야기, 어디에서 있었던 일인지 조금 더 들려주시겠어요?",
                "reason": "메모에 장소가 자세히 나와 있지 않아서, 글의 배경부터 잡아 보려고요."}

    if step == "step1_query":
        answers = _user_answers(_between(user, "conversation"))
        real = [a for a in answers if not any(w in a for w in NO_EXPERIENCE_WORDS)]
        if not real:
            return {"has_experience": False, "query": None}
        return {"has_experience": True, "query": " ".join(real)[-80:]}

    if step == "step3_aspects":
        ids = re.findall(r'<doc id="([^"]+)">', user)
        aspects = []
        for n, (a, els) in enumerate(ASPECTS.items()):
            aspects.append({"name": a, "elements": [
                {"name": e, "doc_ids": ids[(n + j) % len(ids):(n + j) % len(ids) + 3]} for j, e in enumerate(els)]})
        return {"aspects": aspects}

    if step == "step4_tail_question":
        aspect_map = json.loads(_between(user, "aspect_map") or "{}")     # {측면: {요소: 참고 글 수}}
        conv_lines = _between(user, "conversation").splitlines()
        last = conv_lines[-1] if conv_lines else ""
        empathy = "" if (not last.startswith("사용자") or "넘어갔어요" in last) else _empathy(_user_answers(last)[0])
        asked = {q["target"] for q in json.loads(_between(user, "asked_questions") or "[]")}
        skipped = {q["target"] for q in json.loads(_between(user, "skipped_questions") or "[]")}
        priority = json.loads(_between(user, "priority_targets") or "[]")
        candidates = priority + [f"{a}/{e}" for a, els in aspect_map.items() for e in els][:DEMO_TAIL_QUESTIONS]
        for t in candidates:
            if t not in asked and t not in skipped:
                aspect, _, element = t.partition("/")
                count = aspect_map.get(aspect, {}).get(element, 0) if element else 0
                lead = f"다른 분들 후기에서 '{element}' 이야기가 많던데, " if count >= 3 else ""
                return {"empathy": empathy,
                        "question": f"{lead}직접 가 보셨을 때 '{element or aspect}'은(는) 어땠어요?",
                        "target": t,
                        "reason": f"아직 '{element or aspect}' 이야기는 안 나와서요. "
                                  "직접 느낀 점을 적으면 독자가 장면을 더 잘 떠올릴 수 있을 것 같아요."}
        return {"empathy": empathy, "question": None, "target": "", "reason": "데모: 더 물을 항목 없음"}

    if step == "step5_followup":
        answer = _between(user, "answer").strip()
        if len(answer) < 15:
            return {"need_followup": True, "empathy": _empathy(answer),
                    "question": "그때 어떤 기분이 들었는지 조금 더 들려주실 수 있을까요?",
                    "reason": "답변이 짧아서, 그때의 감정을 더하면 글이 훨씬 생생해질 것 같아요."}
        return {"need_followup": False, "empathy": "", "question": None, "reason": None}

    if step in ("draft", "draft_final"):
        article = json.loads(_between(user, "current_article") or "{}")
        answers = _user_answers(_between(user, "conversation"))
        locked = [b for b in article.get("blocks", []) if b["locked"]]
        locked_texts = " ".join(b["text"] for b in locked)
        free_ids = [b["id"] for b in article.get("blocks", []) if not b["locked"]]
        blocks = [{"id": b["id"], "text": b["text"]} for b in locked]
        for n, ans in enumerate(a for a in answers if a not in locked_texts):
            text = ans if ans.endswith((".", "!", "?", "요")) else ans + "."
            blocks.append({"id": free_ids[n] if n < len(free_ids) else "new", "text": text})
        if step == "draft_final" and answers:
            blocks.append({"id": "new", "text": "다음에도 또 이런 경험을 글로 남겨 보고 싶어요."})
        title = (answers[0][:18] + " 이야기") if answers else ""
        return {"title": title, "blocks": blocks}

    if step == "metrics":
        must = json.loads(_between(user, "must_have") or "[]")
        sentences = [re.sub(r"^\d+\.\s*", "", l) for l in _between(user, "sentences").splitlines() if l.strip()]
        text = " ".join(sentences)
        elements = {e for els in ASPECTS.values() for e in els}
        labels = []
        for i, s in enumerate(sentences):
            label = "aspect" if any(e in s for e in elements) else ("distinct" if len(s) >= 15 else "other")
            labels.append({"index": i, "label": label})
        return {"covered": [m for m in must if m.split("/")[-1] in text], "sentences": labels}

    if step == "style_profile":
        posts = re.findall(r'<post index="(\d+)">\n제목: [^\n]*\n(.*?)\n</post>', user, re.S)
        return {
            "features": {
                "summary": "친구에게 이야기하듯 가볍고 밝은 해요체. 짧은 문단과 이모지를 자주 쓴다.",
                "tone": "친근한 해요체", "sentence_endings": ["~했어요", "~더라구요", "~답니다"],
                "sentence_length": "짧은 문장 위주", "emoji_and_symbols": "문단마다 이모지 하나, ㅋㅋ·ㅎㅎ 사용",
                "formatting": "한두 문장마다 줄바꿈", "structure": "상황 → 느낀 점 → 다음 다짐",
                "habits": ["마지막에 다음 계획을 한 줄로 남김"],
            },
            "examples": [{"post_index": int(i), "excerpt": body.split("\n\n")[0]} for i, body in posts[:3]],
        }

    raise ValueError(f"demo: 알 수 없는 step {step}")
