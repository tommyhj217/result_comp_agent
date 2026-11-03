"""Azure GPT 클라이언트: 모델 특성 자동 적응, 오류 분류, 재시도, 회로 차단, 시간 예산, 도구 세션."""
from __future__ import annotations

import json
import time

import pytest

from result_comp_agent.llm.client import Capabilities, LLMClient, LLMError, extract_json
from result_comp_agent.llm.schemas import SUPPLEMENT_SCHEMA, SupplementM
from result_comp_agent.tools import ToolRegistry

OK = '{"items": [{"change_id": "ok", "category": "REWORDING", "user_intent": "x", "agent_gap": "y", "confidence": 0.5}]}'


def _call(llm, **kw):
    return llm.structured(tier="main", system="s JSON", user="u", schema_name="supplement_analysis", schema=SUPPLEMENT_SCHEMA,
                          validate=SupplementM.model_validate, **kw)


def test_adapts_to_unsupported_params_and_persists(helpers, fake_transport_cls, settings_factory):
    """reasoning_effort·json_schema·max_completion_tokens 를 지원하지 않는 GPT 배포에 자동 적응하고, 학습 결과를 저장."""
    def h(kw):
        if "reasoning_effort" in kw:
            return helpers.FakeAPIError(400, "Unrecognized request argument supplied: reasoning_effort", param="reasoning_effort")
        if kw.get("response_format", {}).get("type") == "json_schema":
            return helpers.FakeAPIError(400, "Invalid parameter: 'response_format' of type 'json_schema' is not supported with this model.", param="response_format")
        if "max_completion_tokens" in kw:
            return helpers.FakeAPIError(400, "Unsupported parameter: 'max_completion_tokens' is not supported with this model.")
        assert kw["response_format"] == {"type": "json_object"} and "max_tokens" in kw
        assert "JSON 스키마" in kw["messages"][0]["content"]  # 스키마 지시가 프롬프트로 이동
        return "```json\n" + OK + "\n```"

    t = fake_transport_cls(h)
    llm = LLMClient(settings_factory(reasoning_effort="medium"), transport=t)
    m = _call(llm, use_cache=False)
    assert m.items[0].change_id == "ok"
    caps = llm.caps["gpt-4o"]
    assert (caps.reasoning_effort, caps.response_format, caps.token_param) == (False, "json_object", "max_tokens")
    assert len(caps.learned) == 3 and llm.usage.adaptations == 3
    # 새 클라이언트는 저장된 능력 정보로 바로 성공 (실패 호출 없음)
    n = len(t.completions.calls)
    llm2 = LLMClient(settings_factory(reasoning_effort="medium"), transport=t)
    _call(llm2, use_cache=False)
    assert len(t.completions.calls) == n + 1


def test_transient_errors_retry_then_succeed(helpers, fake_transport_cls, settings_factory):
    seq = iter([helpers.APITimeoutError("timed out"), helpers.FakeAPIError(429, "Rate limit"), helpers.FakeAPIError(503, "busy"), OK])
    t = fake_transport_cls(lambda kw: next(seq))
    llm = LLMClient(settings_factory(max_attempts=4), transport=t)
    assert _call(llm, use_cache=False).items[0].change_id == "ok"
    assert llm.usage.retries == 3 and not llm.failures


def test_timeout_failure_is_recorded(helpers, fake_transport_cls, settings_factory):
    t = fake_transport_cls(lambda kw: helpers.APITimeoutError("timed out"))
    llm = LLMClient(settings_factory(max_attempts=2, timeout=7), transport=t)
    with pytest.raises(LLMError) as ei:
        _call(llm, purpose="supplement:x", use_cache=False)
    assert ei.value.kind == "timeout" and "7초 제한" in str(ei.value)
    assert llm.failures[0]["kind"] == "timeout" and llm.failures[0]["purpose"] == "supplement:x"
    assert t.completions.calls[0]["timeout"] == 7


def test_auth_error_opens_circuit(helpers, fake_transport_cls, settings_factory):
    t = fake_transport_cls(lambda kw: helpers.FakeAPIError(401, "Access denied due to invalid subscription key."))
    llm = LLMClient(settings_factory(), transport=t)
    with pytest.raises(LLMError) as e1:
        _call(llm, use_cache=False)
    assert e1.value.kind == "auth" and "AZURE_OPENAI_API_KEY" in str(e1.value)
    with pytest.raises(LLMError) as e2:
        _call(llm, use_cache=False)
    assert e2.value.kind == "circuit_open" and len(t.completions.calls) == 1


def test_budget_exhaustion(helpers, fake_transport_cls, settings_factory):
    def h(kw):
        time.sleep(0.05)
        return OK

    llm = LLMClient(settings_factory(budget_seconds=0.03), transport=fake_transport_cls(h))
    _call(llm, use_cache=False)
    with pytest.raises(LLMError) as e:
        _call(llm, use_cache=False)
    assert e.value.kind == "budget"


def test_validation_feedback_loop_and_length(helpers, fake_transport_cls, settings_factory):
    replies = iter([{"content": '{"items": [', "finish_reason": "length"}, "not json", OK])
    t = fake_transport_cls(lambda kw: next(replies))
    llm = LLMClient(settings_factory(max_attempts=3), transport=t)
    assert _call(llm, use_cache=False).items[0].change_id == "ok"
    last_user = t.completions.calls[2]["messages"][-1]["content"]
    assert "검증에 실패" in last_user
    assert "잘렸" in t.completions.calls[1]["messages"][-1]["content"]


def test_feedback_retries_then_accepts_last_attempt(fake_transport_cls, settings_factory):
    t = fake_transport_cls(lambda kw: OK)
    llm = LLMClient(settings_factory(max_attempts=3), transport=t)
    obj = _call(llm, feedback=lambda o: ["없는 ID"], use_cache=False)
    assert obj.items and len(t.completions.calls) == 3 and llm.usage.retries == 2
    assert "근거 검증에 실패한 항목" in t.completions.calls[1]["messages"][-1]["content"]


def test_refusal_and_content_filter(helpers, fake_transport_cls, settings_factory):
    llm = LLMClient(settings_factory(), transport=fake_transport_cls(lambda kw: {"content": None, "refusal": "할 수 없습니다", "finish_reason": "stop"}))
    with pytest.raises(LLMError) as e:
        _call(llm, use_cache=False)
    assert e.value.kind == "refusal"
    llm2 = LLMClient(settings_factory(), transport=fake_transport_cls(lambda kw: helpers.FakeAPIError(400, "The response was filtered due to the prompt triggering Azure OpenAI's content management policy.", code="content_filter")))
    with pytest.raises(LLMError) as e2:
        _call(llm2, use_cache=False)
    assert e2.value.kind == "content_filter"


def test_extract_json_variants():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('결과입니다: {"a": [1, 2]} 끝') == {"a": [1, 2]}
    with pytest.raises(json.JSONDecodeError):
        extract_json("없음")


def test_tool_session_and_unsupported_tools(helpers, fake_transport_cls, settings_factory):
    reg = ToolRegistry()
    reg.register_function("add", "더하기", {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}}, "required": ["a", "b"]},
                          lambda ctx, a, b: {"sum": a + b})

    def h(kw):
        if not any(m.get("role") == "tool" for m in kw["messages"]):
            return {"tool_calls": [helpers.tool_call("t1", "add", {"a": 2, "b": 3}), helpers.tool_call("t2", "nope", {})]}
        return "끝"

    llm = LLMClient(settings_factory(), transport=fake_transport_cls(h))
    s = llm.tool_session(tier="main", system="s", user="u", registry=reg, ctx=None)
    assert s.used and [t["tool"] for t in s.trace] == ["add", "nope"]
    assert s.trace[1]["error"] == "unknown_tool" and '{"sum": 5}' in s.transcript

    t2 = fake_transport_cls(lambda kw: helpers.FakeAPIError(400, "Unrecognized request argument supplied: tools", param="tools") if "tools" in kw else "x")
    llm2 = LLMClient(settings_factory(), transport=t2)
    s2 = llm2.tool_session(tier="main", system="s", user="u", registry=reg, ctx=None)
    assert not s2.used and llm2.caps["gpt-4o"].tools is False


def test_probe(helpers, fake_transport_cls, settings_factory):
    def h(kw):
        if "tools" in kw:
            if any(m.get("role") == "tool" for m in kw["messages"]):
                return "done"
            return {"tool_calls": [helpers.tool_call("e1", "echo", {"text": "ping"})]}
        return '{"ok": true, "message": "pong"}'

    llm = LLMClient(settings_factory(cache_dir=None), transport=fake_transport_cls(h))
    r = llm.probe()
    assert r["ok"] and r["tools"] == "supported" and r["served_models"] == ["gpt-4o-2024-08-06"]
    assert isinstance(Capabilities(**r["capabilities"]), Capabilities)
