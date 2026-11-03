from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "examples"))


@pytest.fixture(scope="session")
def demo_ws(tmp_path_factory):
    """examples/build_demo.py 로 git 저장소 + 산출물 + 사용자 수정본 + config 를 만든 작업공간."""
    import build_demo

    ws = tmp_path_factory.mktemp("demo")
    cfg = build_demo.build(ws / "ws")
    return cfg


# --------------------------------------------------------------------------- 가짜 Azure 전송 계층
class _Completions:
    def __init__(self, handler):
        self.handler = handler
        self.calls: list[dict] = []

    def create(self, **kw):
        self.calls.append(kw)
        content = self.handler(kw)
        msg = SimpleNamespace(content=content, refusal=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20))


class FakeTransport:
    def __init__(self, handler):
        self.completions = _Completions(handler)
        self.chat = SimpleNamespace(completions=self.completions)


def schema_name(kw: dict) -> str:
    return kw["response_format"]["json_schema"]["name"]


def user_text(kw: dict) -> str:
    return [m for m in kw["messages"] if m["role"] == "user"][0]["content"]


@pytest.fixture
def fake_transport_cls():
    return FakeTransport


@pytest.fixture
def helpers():
    return SimpleNamespace(schema_name=schema_name, user_text=user_text, re=re, json=json)
