"""
개인 DB (SQLite): 사용자, 로그인 토큰, 문체 프로필, 내 블로그 글, 메모, 작성 세션

파일 위치는 환경변수 APP_DB로 바꿀 수 있다 (기본 server/data/app.db).
요청마다 새 연결을 열어 쓴다 (FastAPI가 요청을 여러 스레드에서 처리하기 때문).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(os.getenv("APP_DB", Path(__file__).resolve().parent / "data" / "app.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
    blog_name TEXT NOT NULL,
    blog_url TEXT NOT NULL,
    is_demo INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_tokens (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS style_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    status TEXT NOT NULL,              -- building | ready | failed
    error TEXT,
    features TEXT,                     -- JSON: 문체 특징
    examples TEXT,                     -- JSON: few-shot용 문단 발췌
    post_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS user_posts (
    user_id INTEGER NOT NULL REFERENCES users(id),
    log_no TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    url TEXT NOT NULL,
    published_at TEXT,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (user_id, log_no)
);
CREATE TABLE IF NOT EXISTS memos (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS writing_sessions (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    state TEXT NOT NULL,               -- JSON: SessionState (질문 루프 + 글)
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with connect() as c:
        c.execute("PRAGMA journal_mode = WAL")
        c.executescript(SCHEMA)


# ---------------------------------------------------------------- 사용자 / 로그인
def _hash_password(password: str, salt: str) -> str:
    return hashlib.scrypt(password.encode(), salt=salt.encode(), n=2**14, r=8, p=1).hex()


class AuthError(Exception):
    pass


def create_user(email: str, password: str, blog_name: str, blog_url: str, is_demo: bool = False) -> dict:
    salt = secrets.token_hex(16)
    try:
        with connect() as c:
            cur = c.execute(
                "INSERT INTO users (email, password_hash, salt, blog_name, blog_url, is_demo, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (email, _hash_password(password, salt), salt, blog_name, blog_url, int(is_demo), now()))
            return get_user(cur.lastrowid, c)
    except sqlite3.IntegrityError:
        raise AuthError("이미 가입된 이메일입니다. 로그인해 주세요.")


def check_password(email: str, password: str) -> dict:
    with connect() as c:
        row = c.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if row is None or not hmac.compare_digest(_hash_password(password, row["salt"]), row["password_hash"]):
        raise AuthError("이메일 또는 비밀번호를 확인해주세요.")
    return _user_dict(row)


def find_user_by_email(email: str) -> dict | None:
    with connect() as c:
        row = c.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    return _user_dict(row) if row else None


def get_user(user_id: int, conn=None) -> dict | None:
    if conn is not None:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    else:
        with connect() as c:
            row = c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _user_dict(row) if row else None


def _user_dict(row) -> dict:
    return {"id": row["id"], "email": row["email"], "blog_name": row["blog_name"],
            "blog_url": row["blog_url"], "is_demo": bool(row["is_demo"])}


def issue_token(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    with connect() as c:
        c.execute("INSERT INTO auth_tokens VALUES (?, ?, ?)", (token, user_id, now()))
    return token


def user_for_token(token: str) -> dict | None:
    with connect() as c:
        row = c.execute("SELECT u.* FROM auth_tokens t JOIN users u ON u.id = t.user_id WHERE t.token = ?",
                        (token,)).fetchone()
    return _user_dict(row) if row else None


def revoke_token(token: str):
    with connect() as c:
        c.execute("DELETE FROM auth_tokens WHERE token = ?", (token,))


# ---------------------------------------------------------------- 문체 프로필 / 내 글
def start_profile(user_id: int) -> int:
    with connect() as c:
        cur = c.execute("INSERT INTO style_profiles (user_id, status, created_at) VALUES (?, 'building', ?)",
                        (user_id, now()))
        return cur.lastrowid


def finish_profile(profile_id: int, features: dict, examples: list, post_count: int):
    with connect() as c:
        c.execute("UPDATE style_profiles SET status='ready', features=?, examples=?, post_count=? WHERE id=?",
                  (json.dumps(features, ensure_ascii=False), json.dumps(examples, ensure_ascii=False),
                   post_count, profile_id))


def fail_profile(profile_id: int, error: str):
    with connect() as c:
        c.execute("UPDATE style_profiles SET status='failed', error=? WHERE id=?", (error, profile_id))


def latest_profile(user_id: int, ready_only: bool = False) -> dict | None:
    sql = "SELECT * FROM style_profiles WHERE user_id = ?" + (" AND status = 'ready'" if ready_only else "")
    with connect() as c:
        row = c.execute(sql + " ORDER BY id DESC LIMIT 1", (user_id,)).fetchone()
    if row is None:
        return None
    return {"id": row["id"], "status": row["status"], "error": row["error"],
            "features": json.loads(row["features"]) if row["features"] else None,
            "examples": json.loads(row["examples"]) if row["examples"] else [],
            "post_count": row["post_count"], "created_at": row["created_at"]}


def save_user_posts(user_id: int, posts: list[dict]):
    with connect() as c:
        for p in posts:
            c.execute("INSERT OR REPLACE INTO user_posts VALUES (?, ?, ?, ?, ?, ?, ?)",
                      (user_id, p["log_no"], p["title"], p["body"], p["url"], p.get("published_at"), now()))


# ---------------------------------------------------------------- 메모
def list_memos(user_id: int) -> list[dict]:
    with connect() as c:
        rows = c.execute("SELECT * FROM memos WHERE user_id = ? ORDER BY created_at DESC, rowid DESC",
                         (user_id,)).fetchall()
    return [{"id": r["id"], "text": r["text"], "created_at": r["created_at"]} for r in rows]


def add_memo(user_id: int, text: str) -> dict:
    memo = {"id": uuid.uuid4().hex, "text": text, "created_at": now()}
    with connect() as c:
        c.execute("INSERT INTO memos VALUES (?, ?, ?, ?)", (memo["id"], user_id, text, memo["created_at"]))
    return memo


def get_memo(user_id: int, memo_id: str) -> dict | None:
    with connect() as c:
        r = c.execute("SELECT * FROM memos WHERE id = ? AND user_id = ?", (memo_id, user_id)).fetchone()
    return {"id": r["id"], "text": r["text"], "created_at": r["created_at"]} if r else None


def delete_memo(user_id: int, memo_id: str):
    with connect() as c:
        c.execute("DELETE FROM memos WHERE id = ? AND user_id = ?", (memo_id, user_id))


# ---------------------------------------------------------------- 작성 세션
def save_session(user_id: int, session_id: str, state_json: str):
    t = now()
    with connect() as c:
        c.execute("INSERT INTO writing_sessions VALUES (?, ?, ?, ?, ?)"
                  " ON CONFLICT(id) DO UPDATE SET state = excluded.state, updated_at = excluded.updated_at",
                  (session_id, user_id, state_json, t, t))


def load_session(user_id: int, session_id: str) -> str | None:
    with connect() as c:
        r = c.execute("SELECT state FROM writing_sessions WHERE id = ? AND user_id = ?",
                      (session_id, user_id)).fetchone()
    return r["state"] if r else None


def list_sessions(user_id: int) -> list[dict]:
    with connect() as c:
        rows = c.execute("SELECT id, state, created_at, updated_at FROM writing_sessions WHERE user_id = ?"
                         " ORDER BY updated_at DESC, rowid DESC", (user_id,)).fetchall()
    out = []
    for r in rows:
        article = json.loads(r["state"]).get("article", {})
        out.append({"id": r["id"], "title": article.get("title", ""), "created_at": r["created_at"],
                    "updated_at": r["updated_at"]})
    return out
