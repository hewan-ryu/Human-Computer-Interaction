"""
글 지표

- 들어가야 할 내용 %: 공통 DB에서 여러 글(MUST_MIN_DOCS개 이상)이 다룬 항목 중, 내 글이 다룬 비율
- 차별성 있는 내용 %: 내 글의 문장 중, 공통 측면에 없는 '나만의 구체적 경험·감정' 문장의 비율

LLM은 항목별·문장별 판정만 하고, 퍼센트는 코드에서 계산한다.
"""

from __future__ import annotations

import json
import re
from typing import Callable

from .state import Metrics, SessionState

MUST_MIN_DOCS = 3
MIN_SENTENCE_CHARS = 4

SCHEMA_METRICS = {
    "type": "object",
    "properties": {
        "covered": {"type": "array", "items": {"type": "string"}},
        "sentences": {"type": "array", "items": {
            "type": "object",
            "properties": {"index": {"type": "integer"},
                           "label": {"type": "string", "enum": ["distinct", "aspect", "other"]}},
            "required": ["index", "label"],
            "additionalProperties": False,
        }},
    },
    "required": ["covered", "sentences"],
    "additionalProperties": False,
}


def must_have_items(all_aspects: dict, min_docs: int = MUST_MIN_DOCS) -> list:
    """여러 글에 등장한 하위 요소 → 없으면 여러 글에 등장한 측면."""
    items = [f"{a}/{e}" for a, els in all_aspects.items() for e, ids in els.items() if len(set(ids)) >= min_docs]
    if items:
        return items
    return [a for a, els in all_aspects.items() if len({i for ids in els.values() for i in ids}) >= min_docs]


def split_sentences(text: str) -> list:
    parts = re.split(r"(?<=[.!?。~])\s+|\n+", text)
    return [p.strip() for p in parts if len(p.strip()) >= MIN_SENTENCE_CHARS]


def compute_metrics(s: SessionState, complete_json: Callable) -> Metrics:
    from .question_loop import _load, fmt_conversation

    sentences = split_sentences(s.article.plain_text())
    must = must_have_items(s.all_aspects)
    if not sentences:
        return Metrics(coverage=0 if must else None, distinctiveness=0, must_have=must, missing=must,
                       text_hash=s.article.text_hash())

    user = (
        f"<must_have>\n{json.dumps(must, ensure_ascii=False)}\n</must_have>\n\n"
        f"<common_aspects>\n{json.dumps({a: list(e) for a, e in s.all_aspects.items()}, ensure_ascii=False)}"
        f"\n</common_aspects>\n\n"
        f"<conversation>\n{fmt_conversation(s, for_draft=True)}\n</conversation>\n\n"
        "<sentences>\n" + "\n".join(f"{i}. {t}" for i, t in enumerate(sentences)) + "\n</sentences>"
    )
    result = complete_json("metrics", _load("metrics.md"), user, SCHEMA_METRICS)

    covered = [m for m in must if m in set(result.get("covered", []))]
    labels = {r["index"]: r["label"] for r in result.get("sentences", []) if 0 <= r.get("index", -1) < len(sentences)}
    distinct = sum(1 for i in range(len(sentences)) if labels.get(i) == "distinct")
    return Metrics(
        coverage=round(100 * len(covered) / len(must)) if must else None,
        distinctiveness=round(100 * distinct / len(sentences)),
        must_have=must, covered=covered, missing=[m for m in must if m not in covered],
        sentence_count=len(sentences), text_hash=s.article.text_hash(),
    )
