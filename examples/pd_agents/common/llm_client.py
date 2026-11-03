"""제품개발 에이전트 공통 — Azure OpenAI(GPT) 호출 유틸.

.env (또는 환경변수)의 4개 키를 사용한다.
  AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY, AZURE_OPENAI_DEPLOYMENT, AZURE_OPENAI_API_VERSION
"""
from __future__ import annotations

import json
import os
from pathlib import Path

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "offline_samples.json"


def _load_dotenv() -> dict[str, str]:
    """현재 폴더 → 저장소 루트 순으로 .env 를 찾아 읽는다 (환경변수가 우선)."""
    for d in (Path.cwd(), Path(__file__).resolve().parent.parent):
        p = d / ".env"
        if p.is_file():
            out = {}
            for line in p.read_text(encoding="utf-8-sig").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.removeprefix("export ").split("=", 1)
                out[k.strip()] = v.split(" #")[0].strip().strip('"').strip("'")
            return out
    return {}


def _cfg(key: str, default: str | None = None) -> str | None:
    return os.environ.get(key) or _load_dotenv().get(key) or default


def llm_available() -> bool:
    if os.environ.get("PD_AGENTS_OFFLINE") == "1":
        return False
    return all(_cfg(k) for k in ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_DEPLOYMENT"))


def call_gpt(system_prompt: str, user_prompt: str, deployment: str | None = None) -> dict:
    from openai import AzureOpenAI

    client = AzureOpenAI(
        azure_endpoint=_cfg("AZURE_OPENAI_ENDPOINT"),
        api_key=_cfg("AZURE_OPENAI_API_KEY"),
        api_version=_cfg("AZURE_OPENAI_API_VERSION", "2024-10-21"),
        timeout=float(_cfg("AZURE_OPENAI_TIMEOUT", "120")),
    )
    resp = client.chat.completions.create(
        model=deployment or _cfg("AZURE_OPENAI_DEPLOYMENT"),
        messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
        response_format={"type": "json_object"},
    )
    return json.loads(resp.choices[0].message.content)


def offline_sample(agent: str) -> dict:
    """Azure 미설정 환경(데모/테스트)용 고정 응답. 실제 운영에서는 call_gpt 결과를 사용."""
    return json.loads(_FIXTURES.read_text(encoding="utf-8"))[agent]
