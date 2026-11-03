"""제품개발 에이전트 공통 — Azure OpenAI(GPT-6) 호출 유틸."""
from __future__ import annotations

import json
import os
from pathlib import Path

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "offline_samples.json"


def llm_available() -> bool:
    if os.environ.get("PD_AGENTS_OFFLINE") == "1":
        return False
    return bool(os.environ.get("AZURE_OPENAI_ENDPOINT") and os.environ.get("AZURE_OPENAI_API_KEY"))


def call_gpt(system_prompt: str, user_prompt: str, deployment: str | None = None) -> dict:
    from openai import AzureOpenAI

    client = AzureOpenAI(
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2025-04-01-preview"),
    )
    resp = client.chat.completions.create(
        model=deployment or os.environ.get("AZURE_OPENAI_DEPLOYMENT_MAIN", "gpt-6"),
        messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
        response_format={"type": "json_object"},
    )
    return json.loads(resp.choices[0].message.content)


def offline_sample(agent: str) -> dict:
    """Azure 미설정 환경(데모/테스트)용 고정 응답. 실제 운영에서는 call_gpt 결과를 사용."""
    return json.loads(_FIXTURES.read_text(encoding="utf-8"))[agent]
