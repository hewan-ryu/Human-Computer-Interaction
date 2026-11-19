"""LLM 호출 래퍼: 호출별 모델 배분, 형식 검사·재요청 (가짜 클라이언트, 네트워크 없음)"""

import json
from types import SimpleNamespace

import pytest

from server import llm

SCHEMA = {"type": "object", "properties": {"question": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                                           "n": {"type": "integer"}},
          "required": ["question", "n"], "additionalProperties": False}


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        content = self.replies.pop(0)
        return SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(content=content, refusal=None))])


@pytest.fixture
def fake(monkeypatch):
    clients = {}

    def install(replies):
        client = FakeClient(replies)
        monkeypatch.setattr(llm, "_make_client", lambda tier: clients.setdefault(tier.name, client))
        llm._clients.clear()
        return client
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    yield install
    llm._clients.clear()


def test_routes_user_facing_to_writer_and_internal_to_fast():
    assert llm.route("step4_tail_question") == "writer"
    assert llm.route("draft_final") == "writer"
    assert llm.route("step1_query") == "fast"
    assert llm.route("metrics") == "fast"
    assert llm.tier_config("writer").model == "gpt-5.6-luna"
    assert llm.tier_config("fast").model == "deepseek-v4-flash"


def test_route_override(monkeypatch):
    monkeypatch.setenv("LLM_ROUTE_STEP5_FOLLOWUP", "fast")
    assert llm.route("step5_followup") == "fast"


def test_writer_uses_strict_schema(fake):
    client = fake([json.dumps({"question": "Q", "n": 1})])
    assert llm.complete_json("step4_tail_question", "sys", "user", SCHEMA) == {"question": "Q", "n": 1}
    call = client.calls[0]
    assert call["model"] == "gpt-5.6-luna" and call["response_format"]["type"] == "json_schema"
    assert "max_completion_tokens" in call


def test_fast_uses_json_mode_and_retries_bad_output(fake):
    client = fake(['```json\n{"question": "Q"}\n```', '{"question": null, "n": 2}'])
    assert llm.complete_json("step1_query", "sys", "user", SCHEMA) == {"question": None, "n": 2}
    first, second = client.calls
    assert first["model"] == "deepseek-v4-flash" and first["response_format"] == {"type": "json_object"}
    assert "JSON 스키마" in first["messages"][0]["content"] and "max_tokens" in first
    assert "$.n: 빠짐" in second["messages"][-1]["content"]


def test_gives_up_after_two_bad_outputs(fake):
    fake(["not json", '{"question": 3, "n": 1}'])
    with pytest.raises(llm.LLMError):
        llm.complete_json("metrics", "sys", "user", SCHEMA)


def test_missing_key_is_clear_error(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("FAST_API_KEY", raising=False)
    llm._clients.clear()
    with pytest.raises(llm.LLMError, match="DEEPSEEK_API_KEY"):
        llm._make_client(llm.tier_config("fast"))


def test_schema_errors():
    assert llm.schema_errors({"question": "a", "n": 1}, SCHEMA) == []
    assert llm.schema_errors({"question": "a", "n": True}, SCHEMA) == ["$.n: integer이어야 함"]
    assert "$.x: 스키마에 없는 키" in llm.schema_errors({"question": "a", "n": 1, "x": 0}, SCHEMA)
