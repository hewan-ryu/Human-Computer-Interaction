"""
문체 프로필: 사용자 블로그의 최신 글 N개 → 문체 특징 + few-shot 발췌

- 가입할 때와 사용자가 'RE'를 누를 때 백그라운드로 실행한다.
- 공통 DB 전처리(db/crawler/preprocess.py)는 이모지·ㅋㅋ·!! 를 지우는데, 그게 바로 문체라서
  내 글은 보이지 않는 문자만 지우는 가벼운 정리만 한다.
"""

from __future__ import annotations

import re
import time
from typing import Callable

from . import storage

POSTS_FOR_PROFILE = 10
MAX_EXAMPLES = 5

SCHEMA_PROFILE = {
    "type": "object",
    "properties": {
        "features": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "tone": {"type": "string"},
                "sentence_endings": {"type": "array", "items": {"type": "string"}},
                "sentence_length": {"type": "string"},
                "emoji_and_symbols": {"type": "string"},
                "formatting": {"type": "string"},
                "structure": {"type": "string"},
                "habits": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["summary", "tone", "sentence_endings", "sentence_length",
                         "emoji_and_symbols", "formatting", "structure", "habits"],
            "additionalProperties": False,
        },
        "examples": {"type": "array", "items": {
            "type": "object",
            "properties": {"post_index": {"type": "integer"}, "excerpt": {"type": "string"}},
            "required": ["post_index", "excerpt"],
            "additionalProperties": False,
        }},
    },
    "required": ["features", "examples"],
    "additionalProperties": False,
}


def blog_id_from_url(blog_url: str) -> str:
    m = re.match(r"https?://(?:m\.)?blog\.naver\.com/([A-Za-z0-9_-]+)/?$", blog_url.strip())
    if not m:
        raise ValueError(f"네이버 블로그 메인화면 주소가 아닙니다: {blog_url}")
    return m.group(1)


def light_clean(lines: list[str], title: str) -> str:
    """보이지 않는 문자만 정리. 제목 중복 줄 제거, 빈 줄 3개 이상은 1개로."""
    lines = [l.replace("​", "").rstrip() for l in lines]
    if lines and lines[0].strip() == title.strip():
        lines = lines[1:]
    text = "\n".join(lines).strip()
    return re.sub(r"\n{3,}", "\n\n", text)


def fetch_latest_posts(blog_url: str, n: int = POSTS_FOR_PROFILE, delay: float = 1.0) -> list[dict]:
    """사용자 블로그에서 최신 글 n개를 가져온다. [{log_no, title, body, url, published_at}]"""
    import requests

    from db.crawler.collect_urls import latest_urls
    from db.crawler.crawl_posts import crawl

    blog_id = blog_id_from_url(blog_url)
    session = requests.Session()
    posts = []
    for url in latest_urls(blog_id, n, session):
        try:
            log_no, d = crawl(url, session)
        except Exception as e:                       # 글 하나 실패해도 나머지는 계속
            print(f"[style_profile] 글 가져오기 실패 {url}: {e}")
            continue
        body = light_clean(d["body"], d["title"])
        if body:
            posts.append({"log_no": log_no, "title": d["title"], "body": body,
                          "url": d["url"], "published_at": d["published_at"]})
        time.sleep(delay)
    return posts


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def extract_profile(posts: list[dict], complete_json: Callable) -> tuple[dict, list[dict]]:
    """LLM으로 문체 특징과 발췌를 뽑는다. 원문에 없는 발췌는 버린다."""
    text = "\n\n".join(f'<post index="{i}">\n제목: {p["title"]}\n{p["body"]}\n</post>'
                       for i, p in enumerate(posts))
    system = _load_prompt()
    result = complete_json("style_profile", system, f"<posts>\n{text}\n</posts>", SCHEMA_PROFILE)

    examples, seen = [], set()
    for ex in result.get("examples", []):
        i, excerpt = ex.get("post_index"), ex.get("excerpt", "").strip()
        if not isinstance(i, int) or not 0 <= i < len(posts) or not excerpt:
            continue
        if _norm(excerpt) not in _norm(posts[i]["body"]) or _norm(excerpt) in seen:
            continue                                   # 원문 그대로가 아니면 few-shot으로 쓰지 않음
        seen.add(_norm(excerpt))
        examples.append({"title": posts[i]["title"], "excerpt": excerpt})
        if len(examples) == MAX_EXAMPLES:
            break
    if not examples:                                   # 발췌가 하나도 안 맞으면 글 앞부분으로 대신
        for p in posts[:3]:
            examples.append({"title": p["title"], "excerpt": p["body"].split("\n\n")[0][:600]})
    return result["features"], examples


def _load_prompt() -> str:
    from .question_loop import _load
    return _load("style_profile.md")


def build_profile(user: dict, profile_id: int, complete_json: Callable,
                  fetch_posts: Callable[[str], list[dict]] = fetch_latest_posts):
    """백그라운드 작업: 글 가져오기 → 개인 DB 저장 → 프로필 생성. 실패하면 failed로 기록."""
    try:
        posts = fetch_posts(user["blog_url"])
        if not posts:
            raise RuntimeError("블로그에서 글을 가져오지 못했어요. 블로그 주소와 공개 설정을 확인해 주세요.")
        storage.save_user_posts(user["id"], posts)
        features, examples = extract_profile(posts, complete_json)
        storage.finish_profile(profile_id, features, examples, len(posts))
    except Exception as e:
        storage.fail_profile(profile_id, str(e))
