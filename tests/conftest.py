from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "examples"))
sys.path.insert(0, str(Path(__file__).resolve().parent))


@pytest.fixture(scope="session")
def demo_ws(tmp_path_factory):
    """examples/build_demo.py 로 git 저장소 + 산출물 + 사용자 수정본 + config 를 만든 작업공간."""
    import build_demo

    ws = tmp_path_factory.mktemp("demo")
    cfg = build_demo.build(ws / "ws")
    return cfg


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    """테스트가 실제 Azure 환경변수/.env 를 읽지 않도록 격리."""
    for k in list(__import__("os").environ):
        if k.startswith(("AZURE_OPENAI", "AZUTE_", "AZURE_OPEN_", "OPENAI_API_VERSION", "RCA_")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.chdir(tmp_path)


# --------------------------------------------------------------------------- 가짜 Azure 전송 계층
class FakeAPIError(Exception):
    """openai SDK 의 APIStatusError 와 같은 속성(status_code/param/code/message/body)을 가진 가짜 예외."""

    def __init__(self, status: int, message: str, param: str | None = None, code: str | None = None):
        super().__init__(message)
        self.status_code = status
        self.message = message
        self.param = param
        self.code = code
        self.body = {"message": message, "param": param, "code": code}


class APITimeoutError(Exception):
    pass


def tool_call(cid: str, name: str, args: dict):
    return SimpleNamespace(id=cid, type="function", function=SimpleNamespace(name=name, arguments=json.dumps(args, ensure_ascii=False)))


class _Completions:
    def __init__(self, handler, model):
        self.handler = handler
        self.model = model
        self.calls: list[dict] = []

    def create(self, **kw):
        self.calls.append(kw)
        out = self.handler(kw)
        if isinstance(out, BaseException):
            raise out
        finish = "stop"
        if isinstance(out, dict) and "tool_calls" in out:
            msg = SimpleNamespace(content=out.get("content"), refusal=None, tool_calls=out["tool_calls"])
            finish = "tool_calls"
        elif isinstance(out, dict) and "finish_reason" in out:
            msg = SimpleNamespace(content=out.get("content"), refusal=out.get("refusal"), tool_calls=None)
            finish = out["finish_reason"]
        else:
            msg = SimpleNamespace(content=out, refusal=None, tool_calls=None)
        return SimpleNamespace(model=self.model, choices=[SimpleNamespace(message=msg, finish_reason=finish)],
                               usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20))


class FakeTransport:
    def __init__(self, handler, model: str = "gpt-4o-2024-08-06"):
        self.completions = _Completions(handler, model)
        self.chat = SimpleNamespace(completions=self.completions)


def schema_name(kw: dict) -> str:
    rf = kw.get("response_format") or {}
    if rf.get("type") == "json_schema":
        return rf["json_schema"]["name"]
    return "<no-schema>"


def user_text(kw: dict) -> str:
    return "\n".join(m["content"] for m in kw["messages"] if m["role"] == "user" and isinstance(m.get("content"), str))


@pytest.fixture
def fake_transport_cls():
    return FakeTransport


@pytest.fixture
def helpers():
    return SimpleNamespace(schema_name=schema_name, user_text=user_text, re=re, json=json, tool_call=tool_call,
                           FakeAPIError=FakeAPIError, APITimeoutError=APITimeoutError)


def make_settings(tmp_path, **kw):
    from result_comp_agent.llm.client import LLMSettings

    base = dict(endpoint="https://unit.openai.azure.com", api_key="k", deployment_main="gpt-4o", api_version="2024-10-21",
                cache_dir=str(tmp_path / "llm"), retry_backoff=0)
    base.update(kw)
    return LLMSettings(**base)


@pytest.fixture
def settings_factory(tmp_path):
    return lambda **kw: make_settings(tmp_path, **kw)
