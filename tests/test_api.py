"""API 전체 흐름 (데모 모드: 가짜 LLM, 임시 DB)"""

from fastapi.testclient import TestClient

from server.app import app

client = TestClient(app)


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_signup_login_profile_memo_and_writing_flow():
    r = client.post("/api/auth/signup", json={"email": "a@b.co", "password": "password1", "blog_name": "내 블로그",
                                              "blog_url": "https://blog.naver.com/someone"})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    # 백그라운드 작업(문체 프로필)은 TestClient에서 응답 직후 실행됨
    profile = client.get("/api/profile", headers=auth(token)).json()["profile"]
    assert profile["status"] == "ready" and profile["examples"]

    assert client.post("/api/auth/signup", json={"email": "a@b.co", "password": "password1", "blog_name": "x",
                                                 "blog_url": "https://blog.naver.com/x"}).status_code == 409
    assert client.post("/api/auth/login", json={"email": "a@b.co", "password": "wrongpass"}).status_code == 401
    assert client.get("/api/me").status_code == 401

    memo = client.post("/api/memos", json={"text": "비 오는 날 간 카페"}, headers=auth(token)).json()["memo"]
    r = client.post("/api/sessions", json={"memo_id": memo["id"]}, headers=auth(token)).json()
    sid, state = r["state"]["session_id"], r["state"]
    assert state["conversation"][0]["kind"] == "memo" and r["reply"].startswith("메모에")

    r = client.post(f"/api/sessions/{sid}/messages", json={"message": "토요일 오후에 창가 자리에 앉았어요"},
                    headers=auth(token)).json()
    assert r["state"]["current_question"] and r["state"]["draft_stale"]

    st = client.post(f"/api/sessions/{sid}/draft", headers=auth(token)).json()["state"]
    assert st["article"]["blocks"] and not st["draft_stale"]

    # 사용자가 첫 문단을 고치면 잠기고, 다음 글 갱신에서도 그대로
    blocks = st["article"]["blocks"]
    blocks[0] = {**blocks[0], "text": "내가 직접 쓴 첫 문단", "locked": True}
    client.put(f"/api/sessions/{sid}/article", json={"title": "내 제목", "title_locked": True, "blocks": blocks},
               headers=auth(token))
    client.post(f"/api/sessions/{sid}/messages", json={"message": "조명이 노랗고 따뜻해서 오래 앉아 있었어요"},
                headers=auth(token))
    st = client.post(f"/api/sessions/{sid}/draft", headers=auth(token)).json()["state"]
    assert st["article"]["title"] == "내 제목"
    assert st["article"]["blocks"][0] == {**blocks[0]}

    # 넘어가기 → 그만 → 지표 → 더 쓰러 가기
    r = client.post(f"/api/sessions/{sid}/messages", json={"skip": True}, headers=auth(token)).json()
    assert r["state"]["conversation"][-2]["kind"] == "skip"
    # 반영 없이 넘어가기: 대화에는 남지만 글 재료로 세지 않음
    r = client.post(f"/api/sessions/{sid}/messages", json={"message": "음악은 기억이 잘 안 나요", "include": False},
                    headers=auth(token)).json()
    assert r["state"]["conversation"][-2]["include"] is False and not r["state"]["draft_stale"]
    r = client.post(f"/api/sessions/{sid}/messages", json={"stop": True}, headers=auth(token)).json()
    assert r["state"]["finished"] and r["state"]["needs_final"] and r["state"]["draft_stale"]
    # 질문이 끝나면 최종 완성 (한 번만)
    st = client.post(f"/api/sessions/{sid}/draft", headers=auth(token)).json()["state"]
    assert not st["needs_final"] and not st["draft_stale"]
    texts = [b["text"] for b in st["article"]["blocks"]]
    assert "다음에도 또 이런 경험을 글로 남겨 보고 싶어요." in texts
    assert not any("음악은 기억이" in x for x in texts)
    st = client.post(f"/api/sessions/{sid}/metrics", headers=auth(token)).json()["state"]
    assert st["metrics"] is not None and not st["metrics_stale"]
    r = client.post(f"/api/sessions/{sid}/continue", headers=auth(token)).json()
    assert not r["state"]["finished"] and r["reply"].startswith("좋아요")

    listed = client.get("/api/sessions", headers=auth(token)).json()["sessions"]
    assert listed[0]["id"] == sid and listed[0]["title"] == "내 제목"


def test_demo_login_and_other_users_data_is_hidden():
    t1 = client.post("/api/auth/demo").json()["token"]
    t2 = client.post("/api/auth/signup", json={"email": "c@d.co", "password": "password1", "blog_name": "b",
                                               "blog_url": "https://blog.naver.com/other"}).json()["token"]
    sid = client.post("/api/sessions", json={}, headers=auth(t1)).json()["state"]["session_id"]
    assert client.get(f"/api/sessions/{sid}", headers=auth(t2)).status_code == 404
    memo = client.post("/api/memos", json={"text": "비밀 메모"}, headers=auth(t1)).json()["memo"]
    assert client.post("/api/sessions", json={"memo_id": memo["id"]}, headers=auth(t2)).status_code == 404
