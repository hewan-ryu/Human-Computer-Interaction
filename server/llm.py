"""
LLM 호출 래퍼: 시스템 프롬프트 + 입력 → JSON(dict)

호출(step)마다 두 등급 중 하나의 모델을 쓴다. 둘 다 OpenAI 호환 Chat Completions API로 부른다.
  - writer: 사용자에게 보이는 문장 (질문·공감·질문 이유, 블로그 글, 문체 프로필)  기본 gpt-5.6-luna
  - fast:   화면에 안 보이는 내부 분석 (쿼리·경험 판단, 측면 추출, 지표 문장 판정) 기본 deepseek-v4-flash

설정 (server/.env)
  WRITER_MODEL / WRITER_API_KEY / WRITER_BASE_URL   (키 기본값: OPENAI_API_KEY, 주소 기본값: OpenAI)
  FAST_MODEL   / FAST_API_KEY   / FAST_BASE_URL     (키 기본값: DEEPSEEK_API_KEY, 주소 기본값: https://api.deepseek.com)
  WRITER_STRICT_SCHEMA=1 / FAST_STRICT_SCHEMA=0     1: response_format json_schema(strict)로 형식 강제
                                                    0: JSON 모드 + 스키마를 프롬프트에 넣고 코드로 검사
  LLM_ROUTE_<STEP>=writer|fast                      호출별 등급 바꾸기 (예: LLM_ROUTE_STEP5_FOLLOWUP=fast)

형식 검사에 실패하면 오류 내용을 알려 주고 한 번 더 요청한다.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Optional

MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "16000"))

# 호출별 기본 등급
ROUTES = {
    "step1_query": "fast",
    "step3_aspects": "fast",
    "metrics": "fast",
    "step4_tail_question": "writer",
    "step5_followup": "writer",
    "opening_from_memo": "writer",
    "draft": "writer",
    "draft_final": "writer",
    "style_profile": "writer",
}


class LLMError(RuntimeError):
    pass


@dataclass
class Tier:
    name: str
    model: str
    api_key: Optional[str]
    base_url: Optional[str]
    strict_schema: bool
    token_param: str            # OpenAI 최신 모델: max_completion_tokens, 그 밖의 호환 API: max_tokens


def _env(key: str, default: Optional[str] = None) -> Optional[str]:
    value = os.getenv(key, "").strip()
    return value or default


def tier_config(name: str) -> Tier:
    if name == "writer":
        return Tier("writer", _env("WRITER_MODEL", "gpt-5.6-luna"),
                    _env("WRITER_API_KEY", _env("OPENAI_API_KEY")), _env("WRITER_BASE_URL"),
                    _env("WRITER_STRICT_SCHEMA", "1") == "1",
                    _env("WRITER_TOKEN_PARAM", "max_completion_tokens"))
    if name == "fast":
        return Tier("fast", _env("FAST_MODEL", "deepseek-v4-flash"),
                    _env("FAST_API_KEY", _env("DEEPSEEK_API_KEY")), _env("FAST_BASE_URL", "https://api.deepseek.com"),
                    _env("FAST_STRICT_SCHEMA", "0") == "1",
                    _env("FAST_TOKEN_PARAM", "max_tokens"))
    raise ValueError(f"알 수 없는 모델 등급: {name}")


def route(step: str) -> str:
    tier = _env(f"LLM_ROUTE_{step.upper()}", ROUTES.get(step, "writer"))
    if tier not in ("writer", "fast"):
        raise ValueError(f"LLM_ROUTE_{step.upper()}는 writer 또는 fast여야 합니다: {tier}")
    return tier


# ---------------------------------------------------------------- 클라이언트
_clients: dict = {}


def _make_client(tier: Tier):
    if not tier.api_key:
        key_name = "WRITER_API_KEY 또는 OPENAI_API_KEY" if tier.name == "writer" else "FAST_API_KEY 또는 DEEPSEEK_API_KEY"
        raise LLMError(f"{tier.name} 모델의 API 키가 없습니다. server/.env에 {key_name}를 넣어 주세요.")
    from openai import OpenAI

    return OpenAI(api_key=tier.api_key, base_url=tier.base_url)


def _client(tier: Tier):
    key = (tier.name, tier.base_url, tier.api_key)
    if key not in _clients:
        _clients[key] = _make_client(tier)
    return _clients[key]


# ---------------------------------------------------------------- 스키마 검사
def schema_errors(value, schema: dict, path: str = "$") -> list:
    """이 프로젝트에서 쓰는 JSON 스키마 일부(object/array/string/integer/boolean/null/enum/anyOf)만 검사."""
    if "anyOf" in schema:
        if any(not schema_errors(value, s, path) for s in schema["anyOf"]):
            return []
        return [f"{path}: 허용된 형식 중 어느 것에도 맞지 않음"]
    kind = schema.get("type")
    checks = {
        "object": lambda v: isinstance(v, dict),
        "array": lambda v: isinstance(v, list),
        "string": lambda v: isinstance(v, str),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "boolean": lambda v: isinstance(v, bool),
        "null": lambda v: v is None,
    }
    if kind in checks and not checks[kind](value):
        return [f"{path}: {kind}이어야 함"]
    if "enum" in schema and value not in schema["enum"]:
        return [f"{path}: {schema['enum']} 중 하나여야 함"]
    errors = []
    if kind == "object":
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}.{key}: 빠짐")
        for key, sub in props.items():
            if key in value:
                errors += schema_errors(value[key], sub, f"{path}.{key}")
        if schema.get("additionalProperties") is False:
            errors += [f"{path}.{k}: 스키마에 없는 키" for k in value if k not in props]
    elif kind == "array" and "items" in schema:
        for i, item in enumerate(value):
            errors += schema_errors(item, schema["items"], f"{path}[{i}]")
    return errors


# ---------------------------------------------------------------- 호출
def _request(tier: Tier, step: str, messages: list, schema: dict) -> str:
    kwargs = {"model": tier.model, "messages": messages, tier.token_param: MAX_TOKENS}
    if tier.strict_schema:
        kwargs["response_format"] = {"type": "json_schema",
                                     "json_schema": {"name": step, "schema": schema, "strict": True}}
    else:
        kwargs["response_format"] = {"type": "json_object"}
    resp = _client(tier).chat.completions.create(**kwargs)
    choice = resp.choices[0]
    if getattr(choice.message, "refusal", None):
        raise LLMError(f"{step}: 모델이 응답을 거절했습니다 ({choice.message.refusal})")
    if choice.finish_reason == "length":
        raise LLMError(f"{step}: 응답이 최대 길이에서 잘렸습니다")
    if not choice.message.content:
        raise LLMError(f"{step}: 텍스트 응답이 없습니다")
    return choice.message.content


def _parse(text: str):
    text = text.strip()
    if text.startswith("```"):                      # 코드 블록으로 감싼 경우
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    return json.loads(text)


def complete_json(step: str, system: str, user: str, schema: dict) -> dict:
    """step은 호출 이름 (예: "step1_query"). 이름에 따라 writer/fast 모델을 고른다."""
    tier = tier_config(route(step))
    if not tier.strict_schema:
        system += ("\n\n## 출력 형식\n반드시 아래 JSON 스키마에 맞는 JSON 객체 하나만 출력한다. 다른 글은 쓰지 않는다.\n"
                   + json.dumps(schema, ensure_ascii=False))
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]

    problem = ""
    for _ in range(2):
        text = _request(tier, step, messages, schema)
        try:
            result = _parse(text)
            errors = schema_errors(result, schema)
        except json.JSONDecodeError as e:
            result, errors = None, [f"JSON이 아님 ({e})"]
        if not errors:
            return result
        problem = "; ".join(errors[:5])
        messages += [{"role": "assistant", "content": text},
                     {"role": "user", "content": f"출력 형식이 스키마와 맞지 않습니다: {problem}\n"
                                                 "스키마에 맞는 JSON 객체 하나만 다시 출력하세요."}]
    raise LLMError(f"{step}: {tier.model} 응답이 형식에 맞지 않습니다 ({problem})")
