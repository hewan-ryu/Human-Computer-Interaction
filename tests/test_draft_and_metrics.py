"""실시간 글 병합(잠금 보장)과 지표 계산 테스트"""

from server.draft_writer import apply_user_edit, merge_draft
from server.metrics import compute_metrics, must_have_items, split_sentences
from server.state import Article, Block, SessionState


def art(*blocks, title="제목", title_locked=False):
    return Article(title=title, title_locked=title_locked,
                   blocks=[Block(id=i, text=t, locked=l) for i, t, l in blocks])


def test_locked_block_text_is_kept_even_if_llm_changes_it():
    cur = art(("a", "AI 문단", False), ("b", "내가 쓴 문단", True))
    out = {"title": "새 제목", "blocks": [{"id": "a", "text": "AI 고친 문단"}, {"id": "b", "text": "LLM이 바꿈"},
                                         {"id": "new", "text": "새 문단"}]}
    res = merge_draft(cur, out)
    assert [(b.text, b.locked) for b in res.blocks] == [("AI 고친 문단", False), ("내가 쓴 문단", True), ("새 문단", False)]
    assert res.blocks[0].id == "a" and res.blocks[2].id not in {"a", "b", "new"}
    assert res.title == "새 제목"


def test_missing_locked_block_is_reinserted_after_its_previous_block():
    cur = art(("a", "첫 문단", False), ("b", "잠긴 문단", True), ("c", "셋째", False))
    out = {"title": "", "blocks": [{"id": "a", "text": "첫 문단!"}, {"id": "c", "text": "셋째!"}]}
    res = merge_draft(cur, out)
    assert [b.id for b in res.blocks] == ["a", "b", "c"]
    assert res.title == "제목"                                  # 빈 제목이면 기존 유지


def test_locked_blocks_keep_original_relative_order():
    cur = art(("x", "잠금1", True), ("y", "잠금2", True))
    out = {"title": "", "blocks": [{"id": "y", "text": ""}, {"id": "new", "text": "중간"}, {"id": "x", "text": ""}]}
    res = merge_draft(cur, out)
    assert [b.text for b in res.blocks if b.locked] == ["잠금1", "잠금2"]


def test_title_lock_and_duplicate_ids():
    cur = art(("a", "문단", False), title="내 제목", title_locked=True)
    out = {"title": "AI 제목", "blocks": [{"id": "a", "text": "1"}, {"id": "a", "text": "2"}]}
    res = merge_draft(cur, out)
    assert res.title == "내 제목" and len({b.id for b in res.blocks}) == 2


def test_apply_user_edit_records_deleted_blocks():
    cur = art(("a", "지울 문단", False), ("b", "남길 문단", False))
    edited = Article(title="제목", blocks=[Block(id="b", text="남길 문단 고침", locked=True), Block(id="b", text="복제", locked=True)])
    res = apply_user_edit(cur, edited)
    assert res.deleted_texts == ["지울 문단"]
    assert res.blocks[0].locked and len({b.id for b in res.blocks}) == 2


def test_must_have_items_and_split():
    aspects = {"분위기": {"조명": ["d1", "d2", "d3"], "음악": ["d1"]}, "가격": {"가성비": ["d4", "d5"], "메뉴": ["d6"]}}
    assert must_have_items(aspects) == ["분위기/조명"]
    assert must_have_items({"가격": {"가성비": ["d4"], "메뉴": ["d5", "d6"]}}) == ["가격"]
    assert split_sentences("첫 문장이에요. 둘째 문장!\n\n셋째 문장~ 넷") == ["첫 문장이에요.", "둘째 문장!", "셋째 문장~"]


def test_compute_metrics_percentages():
    s = SessionState(all_aspects={"분위기": {"조명": ["d1", "d2", "d3"]}, "가격": {"가성비": ["d1", "d2", "d3"]}},
                     article=art(("a", "조명이 노랬어요. 창밖 고양이와 눈이 마주쳤어요. 좋았어요.", False)))

    def fake(step, system, user, schema):
        assert step == "metrics" and "2. 좋았어요." in user
        return {"covered": ["분위기/조명", "없는 항목"],
                "sentences": [{"index": 0, "label": "aspect"}, {"index": 1, "label": "distinct"},
                              {"index": 2, "label": "other"}, {"index": 9, "label": "distinct"}]}

    m = compute_metrics(s, fake)
    assert (m.coverage, m.distinctiveness) == (50, 33)
    assert m.missing == ["가격/가성비"] and m.text_hash == s.article.text_hash()
