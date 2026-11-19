"""
대화형 블로그 글쓰기 API

실행 (저장소 루트에서):
    pip install -r server/requirements.txt
    cp server/.env.example server/.env    # OPENAI_API_KEY, DEEPSEEK_API_KEY 입력
    python -m rag.build_index             # 공통 DB 벡터 인덱스 (최초 1회)
    uvicorn server.app:app --reload --port 8000

    키 없이 화면만 돌려 보려면:  DEMO_MODE=1 uvicorn server.app:app --port 8000

주요 API (로그인 후 Authorization: Bearer <token>)
    POST /api/auth/signup | login | demo | logout,  GET /api/me
    GET  /api/profile,  POST /api/profile/refresh            문체 프로필 ('RE')
    GET/POST /api/memos,  DELETE /api/memos/{id}
    GET/POST /api/sessions                                    작성 세션 목록 / 새 글 (memo_id로 메모에서 시작)
    GET  /api/sessions/{id}
    POST /api/sessions/{id}/messages   {message, stop, skip, include}  답변 → 다음 질문 (include=false: 반영 없이 넘어가기)
    POST /api/sessions/{id}/draft                             실시간 글 갱신 (질문이 끝났으면 최종 완성)
    PUT  /api/sessions/{id}/article                           사용자가 고친 글 저장 (잠금)
    POST /api/sessions/{id}/metrics                           지표 계산
    POST /api/sessions/{id}/continue                          '더 쓰러 가기'
"""

from __future__ import annotations

import os
import re
import secrets
import threading
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

load_dotenv(Path(__file__).resolve().parent / ".env")

from . import storage  # noqa: E402
from .draft_writer import apply_user_edit, merge_draft, write_draft  # noqa: E402
from .metrics import compute_metrics  # noqa: E402
from .question_loop import QuestionLoop  # noqa: E402
from .state import Article, Block, SessionState  # noqa: E402
from .style_profile import build_profile, fetch_latest_posts  # noqa: E402

DEMO_MODE = os.getenv("DEMO_MODE") == "1"

if DEMO_MODE:
    from . import demo
    complete_json, search, get_docs, fetch_posts = demo.fake_complete_json, demo.search, demo.get_docs, demo.sample_posts
else:
    from rag.retrieve import get_docs, search_similar

    from .llm import complete_json

    def search(q, exclude, k):
        return search_similar(q, exclude=exclude, k=k)

    fetch_posts = fetch_latest_posts

try:
    from openai import APIError as _APIError
except ImportError:                                  # 데모 모드에서 openai가 없어도 동작
    class _APIError(Exception):
        pass
from .llm import LLMError  # noqa: E402

DEMO_EMAIL = "demo@moa.local"

app = FastAPI()
app.add_middleware(CORSMiddleware,
                   allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
                   allow_methods=["*"], allow_headers=["*"])
storage.init_db()
loop = QuestionLoop(complete_json=complete_json, search=search, get_docs=get_docs)

_locks: dict = {}
_locks_guard = threading.Lock()


def session_lock(session_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(session_id, threading.Lock())


# ================================================================ 인증
def current_user(authorization: str = Header(default="")) -> dict:
    token = authorization.removeprefix("Bearer ").strip()
    user = storage.user_for_token(token) if token else None
    if user is None:
        raise HTTPException(401, "로그인이 필요해요.")
    user["token"] = token
    return user


class SignupRequest(BaseModel):
    email: str
    password: str
    blog_name: str
    blog_url: str


class LoginRequest(BaseModel):
    email: str
    password: str


BLOG_URL = re.compile(r"^https://(m\.)?blog\.naver\.com/[A-Za-z0-9_-]+/?$")


def _auth_response(user: dict) -> dict:
    return {"token": storage.issue_token(user["id"]), "user": user, "profile": storage.latest_profile(user["id"])}


def _start_profile(user: dict, tasks: BackgroundTasks) -> dict:
    profile_id = storage.start_profile(user["id"])
    fetch = demo_posts if user.get("is_demo") else fetch_posts
    tasks.add_task(build_profile, user, profile_id, complete_json, fetch)
    return storage.latest_profile(user["id"])


def demo_posts(blog_url: str = "") -> list:
    from . import demo
    return demo.sample_posts()


@app.post("/api/auth/signup")
def signup(req: SignupRequest, tasks: BackgroundTasks):
    email = req.email.strip().lower()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise HTTPException(400, "이메일 형식을 확인해주세요.")
    if len(req.password) < 8:
        raise HTTPException(400, "비밀번호는 8자 이상이어야 해요.")
    if not req.blog_name.strip() or not BLOG_URL.match(req.blog_url.strip()):
        raise HTTPException(400, "블로그 이름과 네이버 블로그 메인화면 링크를 정확히 입력해주세요.")
    try:
        user = storage.create_user(email, req.password, req.blog_name.strip(), req.blog_url.strip())
    except storage.AuthError as e:
        raise HTTPException(409, str(e))
    res = _auth_response(user)
    res["profile"] = _start_profile(user, tasks)     # 가입하면 바로 문체 프로필 만들기 시작
    return res


@app.post("/api/auth/login")
def login(req: LoginRequest):
    try:
        user = storage.check_password(req.email.strip().lower(), req.password)
    except storage.AuthError as e:
        raise HTTPException(401, str(e))
    return _auth_response(user)


@app.post("/api/auth/demo")
def demo_login(tasks: BackgroundTasks):
    user = storage.find_user_by_email(DEMO_EMAIL)
    if user is None:
        user = storage.create_user(DEMO_EMAIL, secrets.token_urlsafe(16), "데모 블로그",
                                   "https://blog.naver.com/demo", is_demo=True)
    res = _auth_response(user)
    if res["profile"] is None:
        res["profile"] = _start_profile(user, tasks)
    return res


@app.post("/api/auth/logout")
def logout(user: dict = Depends(current_user)):
    storage.revoke_token(user["token"])
    return {"ok": True}


@app.get("/api/me")
def me(user: dict = Depends(current_user)):
    return {"user": {k: v for k, v in user.items() if k != "token"}, "profile": storage.latest_profile(user["id"]),
            "demo_mode": DEMO_MODE}


# ================================================================ 문체 프로필
@app.get("/api/profile")
def get_profile(user: dict = Depends(current_user)):
    return {"profile": storage.latest_profile(user["id"])}


@app.post("/api/profile/refresh")
def refresh_profile(tasks: BackgroundTasks, user: dict = Depends(current_user)):
    latest = storage.latest_profile(user["id"])
    if latest and latest["status"] == "building":
        return {"profile": latest}
    return {"profile": _start_profile(user, tasks)}


# ================================================================ 메모
class MemoRequest(BaseModel):
    text: str


@app.get("/api/memos")
def memos(user: dict = Depends(current_user)):
    return {"memos": storage.list_memos(user["id"])}


@app.post("/api/memos")
def add_memo(req: MemoRequest, user: dict = Depends(current_user)):
    text = req.text.strip()
    if not text:
        raise HTTPException(400, "메모 내용을 입력해주세요.")
    return {"memo": storage.add_memo(user["id"], text[:2000])}


@app.delete("/api/memos/{memo_id}")
def delete_memo(memo_id: str, user: dict = Depends(current_user)):
    storage.delete_memo(user["id"], memo_id)
    return {"ok": True}


# ================================================================ 작성 세션
def _load(user: dict, session_id: str) -> SessionState:
    raw = storage.load_session(user["id"], session_id)
    if raw is None:
        raise HTTPException(404, "글을 찾을 수 없어요.")
    return SessionState.model_validate_json(raw)


def _save(user: dict, s: SessionState):
    storage.save_session(user["id"], s.session_id, s.model_dump_json())


def _llm_errors(fn):
    """LLM·검색 실패를 HTTP 오류로. 실패하면 상태는 저장하지 않는다."""
    try:
        return fn()
    except (LLMError, _APIError) as e:
        raise HTTPException(502, f"AI 응답을 받지 못했어요. 잠시 후 다시 시도해 주세요. ({e})")
    except FileNotFoundError as e:                   # 공통 DB 벡터 인덱스가 아직 없음
        raise HTTPException(503, str(e))


class NewSessionRequest(BaseModel):
    memo_id: Optional[str] = None


class MessageRequest(BaseModel):
    message: str = ""
    stop: bool = False
    skip: bool = False
    include: bool = True


class BlockIn(BaseModel):
    id: str = ""
    text: str
    locked: bool = False


class ArticleIn(BaseModel):
    title: str = ""
    title_locked: bool = False
    blocks: List[BlockIn] = []


@app.get("/api/sessions")
def sessions(user: dict = Depends(current_user)):
    return {"sessions": storage.list_sessions(user["id"])}


@app.post("/api/sessions")
def new_session(req: NewSessionRequest, user: dict = Depends(current_user)):
    memo = storage.get_memo(user["id"], req.memo_id) if req.memo_id else None
    if req.memo_id and memo is None:
        raise HTTPException(404, "메모를 찾을 수 없어요.")
    s, reply = _llm_errors(lambda: loop.start(memo_text=memo["text"] if memo else None,
                                              memo_id=memo["id"] if memo else None))
    _save(user, s)
    return {"reply": reply, "state": s.public_view()}


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str, user: dict = Depends(current_user)):
    return {"state": _load(user, session_id).public_view()}


def _commit_loop(user: dict, working: SessionState) -> SessionState:
    """질문 루프 결과를 저장. 그사이 사용자가 고친 글·지표는 최신 것을 유지한다."""
    with session_lock(working.session_id):
        latest = _load(user, working.session_id)
        working.article, working.drafted_turns, working.metrics = latest.article, latest.drafted_turns, latest.metrics
        working.final_turns = latest.final_turns
        _save(user, working)
    return working


@app.post("/api/sessions/{session_id}/messages")
def send_message(session_id: str, req: MessageRequest, user: dict = Depends(current_user)):
    working = _load(user, session_id)
    reply = _llm_errors(lambda: loop.handle_user_message(working, req.message, stop=req.stop,
                                                               skip=req.skip, include=req.include))
    return {"reply": reply, "state": _commit_loop(user, working).public_view()}


@app.post("/api/sessions/{session_id}/continue")
def continue_writing(session_id: str, user: dict = Depends(current_user)):
    working = _load(user, session_id)
    if not working.finished:
        raise HTTPException(409, "아직 대화가 진행 중이에요.")
    if working.metrics is None or working.metrics.text_hash != working.article.text_hash():
        working.metrics = _llm_errors(lambda: compute_metrics(working, complete_json))
    reply = _llm_errors(lambda: loop.reopen(working, list(working.metrics.missing)))
    with session_lock(session_id):
        latest = _load(user, session_id)
        working.article, working.drafted_turns, working.final_turns = (latest.article, latest.drafted_turns,
                                                                       latest.final_turns)
        _save(user, working)
    return {"reply": reply, "state": working.public_view()}


@app.put("/api/sessions/{session_id}/article")
def edit_article(session_id: str, req: ArticleIn, user: dict = Depends(current_user)):
    edited = Article(title=req.title, title_locked=req.title_locked,
                     blocks=[Block(id=b.id, text=b.text, locked=b.locked) if b.id else
                             Block(text=b.text, locked=b.locked) for b in req.blocks])
    with session_lock(session_id):
        s = _load(user, session_id)
        s.article = apply_user_edit(s.article, edited)
        _save(user, s)
    return {"state": s.public_view()}


@app.post("/api/sessions/{session_id}/draft")
def update_draft(session_id: str, user: dict = Depends(current_user)):
    snapshot = _load(user, session_id)
    if snapshot.material_count() == 0:
        return {"state": snapshot.public_view()}     # 아직 쓸 재료가 없음
    final = snapshot.needs_final()                    # 질문이 끝났으면 글 전체를 다듬는 최종 완성
    profile = storage.latest_profile(user["id"], ready_only=True)
    output = _llm_errors(lambda: write_draft(snapshot, profile, complete_json, final=final))
    with session_lock(session_id):
        latest = _load(user, session_id)             # LLM이 쓰는 동안 사용자가 고친 문단도 잠금 유지
        latest.article = merge_draft(latest.article, output)
        latest.drafted_turns = max(latest.drafted_turns, snapshot.material_count())
        if final:
            latest.final_turns = snapshot.material_count()
        _save(user, latest)
    return {"state": latest.public_view()}


@app.post("/api/sessions/{session_id}/metrics")
def update_metrics(session_id: str, user: dict = Depends(current_user)):
    snapshot = _load(user, session_id)
    metrics = _llm_errors(lambda: compute_metrics(snapshot, complete_json))
    with session_lock(session_id):
        latest = _load(user, session_id)
        latest.metrics = metrics
        _save(user, latest)
    return {"state": latest.public_view()}


@app.get("/healthz")
def healthz():
    return {"ok": True, "demo_mode": DEMO_MODE}
