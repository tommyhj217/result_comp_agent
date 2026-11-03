"""Azure OpenAI (GPT-6 시리즈) 클라이언트.

* 배포(deployment) 2단 구성: main(gpt-6, 해석/권고) · fast(gpt-6-mini, 교차검증 등)
* Structured Outputs(json_schema, strict) 로 응답 형식을 강제하고, 파싱/검증 실패 시 오류를 알려주며 재시도
* 동일 입력 → 디스크 캐시 재사용 (재현성 확보, 비용 절감). 캐시 키 = 배포명+프롬프트+스키마 해시
* 인증: API Key 또는 Entra ID(azure-identity), 엔드포인트 방식: 버전 지정(api-version) 또는 v1 엔드포인트
* 엔드포인트가 설정되지 않으면 비활성(오프라인) — 파이프라인은 규칙 기반 결과만으로 완주

환경 변수
  AZURE_OPENAI_ENDPOINT        https://<resource>.openai.azure.com
  AZURE_OPENAI_API_KEY         (Entra ID 사용 시 생략)
  AZURE_OPENAI_USE_ENTRA_ID    true/false
  AZURE_OPENAI_API_VERSION     기본 2025-04-01-preview (v1 이면 v1 엔드포인트 사용)
  AZURE_OPENAI_DEPLOYMENT_MAIN 기본 gpt-6
  AZURE_OPENAI_DEPLOYMENT_FAST 기본 gpt-6-mini
  AZURE_OPENAI_REASONING_EFFORT 기본 medium (빈 값이면 미전송)
  AZURE_OPENAI_TEMPERATURE     기본 미전송 (추론 모델은 temperature 를 지원하지 않을 수 있음)
  AZURE_OPENAI_SEED            기본 42
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, TypeVar

log = logging.getLogger(__name__)
T = TypeVar("T")


class LLMError(RuntimeError):
    pass


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


@dataclass
class LLMSettings:
    endpoint: Optional[str] = None
    api_key: Optional[str] = None
    use_entra_id: bool = False
    api_version: str = "2025-04-01-preview"
    deployment_main: str = "gpt-6"
    deployment_fast: str = "gpt-6-mini"
    reasoning_effort: Optional[str] = "medium"
    temperature: Optional[float] = None
    seed: Optional[int] = 42
    max_completion_tokens: int = 16000
    timeout: float = 300.0
    max_attempts: int = 3
    retry_backoff: float = 2.0  # 호출 실패 시 대기(초) = backoff ** attempt (최대 20초), 0 이면 대기 없음
    cache_dir: Optional[str] = ".rca_cache/llm"
    enabled: Optional[bool] = None  # None = 엔드포인트가 있으면 사용

    @classmethod
    def from_env(cls, **overrides: Any) -> "LLMSettings":
        temp = os.environ.get("AZURE_OPENAI_TEMPERATURE")
        seed = os.environ.get("AZURE_OPENAI_SEED", "42")
        effort = os.environ.get("AZURE_OPENAI_REASONING_EFFORT", "medium")
        s = cls(
            endpoint=os.environ.get("AZURE_OPENAI_ENDPOINT") or None,
            api_key=os.environ.get("AZURE_OPENAI_API_KEY") or None,
            use_entra_id=_env_bool("AZURE_OPENAI_USE_ENTRA_ID"),
            api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2025-04-01-preview"),
            deployment_main=os.environ.get("AZURE_OPENAI_DEPLOYMENT_MAIN", "gpt-6"),
            deployment_fast=os.environ.get("AZURE_OPENAI_DEPLOYMENT_FAST", "gpt-6-mini"),
            reasoning_effort=effort or None,
            temperature=float(temp) if temp not in (None, "") else None,
            seed=int(seed) if seed not in (None, "") else None,
        )
        for k, v in overrides.items():
            if v is not None and hasattr(s, k):
                setattr(s, k, v)
        return s

    @property
    def is_active(self) -> bool:
        if self.enabled is False:
            return False
        has_cred = bool(self.api_key) or self.use_entra_id
        return bool(self.endpoint) and has_cred

    def public(self) -> dict[str, Any]:
        return {
            "active": self.is_active,
            "endpoint": self.endpoint,
            "auth": "entra_id" if self.use_entra_id else ("api_key" if self.api_key else None),
            "api_version": self.api_version,
            "deployment_main": self.deployment_main,
            "deployment_fast": self.deployment_fast,
            "reasoning_effort": self.reasoning_effort,
            "temperature": self.temperature,
            "seed": self.seed,
        }


@dataclass
class LLMUsage:
    calls: int = 0
    cache_hits: int = 0
    retries: int = 0
    failures: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    log: list[dict] = field(default_factory=list)


class LLMClient:
    """``transport`` 를 주입하면(테스트) 실제 Azure 호출 대신 사용한다.
    transport 는 ``.chat.completions.create(**kwargs)`` 를 제공해야 한다."""

    def __init__(self, settings: LLMSettings, transport: Any = None):
        self.settings = settings
        self._transport = transport
        self.usage = LLMUsage()

    @property
    def active(self) -> bool:
        return self._transport is not None or self.settings.is_active

    def _client(self):
        if self._transport is not None:
            return self._transport
        s = self.settings
        if not s.is_active:
            raise LLMError("Azure OpenAI 설정이 없습니다 (AZURE_OPENAI_ENDPOINT / 인증 정보)")
        from openai import AzureOpenAI, OpenAI

        token_provider: Optional[Callable[[], str]] = None
        if s.use_entra_id:
            try:
                from azure.identity import DefaultAzureCredential, get_bearer_token_provider
            except ImportError as e:  # pragma: no cover
                raise LLMError("Entra ID 인증에는 azure-identity 패키지가 필요합니다: pip install azure-identity") from e
            token_provider = get_bearer_token_provider(DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default")
        if s.api_version.lower() == "v1":
            base_url = s.endpoint.rstrip("/") + "/openai/v1/"
            self._transport = OpenAI(base_url=base_url, api_key=token_provider or s.api_key, timeout=s.timeout, max_retries=2)
        else:
            kw: dict[str, Any] = dict(azure_endpoint=s.endpoint, api_version=s.api_version, timeout=s.timeout, max_retries=2)
            if token_provider:
                kw["azure_ad_token_provider"] = token_provider
            else:
                kw["api_key"] = s.api_key
            self._transport = AzureOpenAI(**kw)
        return self._transport

    def deployment(self, tier: str) -> str:
        return self.settings.deployment_fast if tier == "fast" else self.settings.deployment_main

    def _cache_path(self, key: str) -> Optional[Path]:
        if not self.settings.cache_dir:
            return None
        return Path(self.settings.cache_dir) / f"{key}.json"

    def structured(
        self,
        *,
        tier: str,
        system: str,
        user: str,
        schema_name: str,
        schema: dict,
        validate: Callable[[dict], T],
        feedback: Optional[Callable[[T], list[str]]] = None,
        purpose: str = "",
    ) -> T:
        """JSON 스키마를 강제한 호출.

        * ``validate``: dict → 검증된 객체. 실패(예외) 시 오류를 알려주고 재요청 — 끝까지 실패하면 LLMError.
        * ``feedback``: 검증된 객체의 '근거 문제' 목록(없는 ID·코드에 없는 인용 등). 문제가 있으면 목록을 알려주고
          재작성을 요청하되, 마지막 시도에서는 받아들인다(문제 항목은 이후 VerifierAgent 가 개별 기각).
        """
        model = self.deployment(tier)
        key = hashlib.sha256(
            json.dumps([model, system, user, schema_name, schema, self.settings.reasoning_effort], ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        cpath = self._cache_path(key)
        if cpath is not None and cpath.exists():
            try:
                data = json.loads(cpath.read_text(encoding="utf-8"))
                obj = validate(data["response"])
                self.usage.cache_hits += 1
                self.usage.log.append({"purpose": purpose, "model": model, "cache": True})
                return obj
            except Exception:
                log.warning("LLM 캐시 손상 → 재호출: %s", cpath)

        client = self._client()
        messages: list[dict[str, str]] = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        params: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "response_format": {"type": "json_schema", "json_schema": {"name": schema_name, "schema": schema, "strict": True}},
            "max_completion_tokens": self.settings.max_completion_tokens,
        }
        if self.settings.reasoning_effort:
            params["reasoning_effort"] = self.settings.reasoning_effort
        if self.settings.temperature is not None:
            params["temperature"] = self.settings.temperature
        if self.settings.seed is not None:
            params["seed"] = self.settings.seed

        last_err: Optional[Exception] = None
        for attempt in range(1, self.settings.max_attempts + 1):
            self.usage.calls += 1
            t0 = time.time()
            try:
                resp = client.chat.completions.create(**params)
            except Exception as e:
                last_err = e
                self.usage.retries += 1
                log.warning("LLM 호출 실패(%s/%s): %s", attempt, self.settings.max_attempts, e)
                if self.settings.retry_backoff > 0:
                    time.sleep(min(self.settings.retry_backoff ** attempt, 20))
                continue
            usage = getattr(resp, "usage", None)
            if usage is not None:
                self.usage.prompt_tokens += int(getattr(usage, "prompt_tokens", 0) or 0)
                self.usage.completion_tokens += int(getattr(usage, "completion_tokens", 0) or 0)
            msg = resp.choices[0].message
            refusal = getattr(msg, "refusal", None)
            content = msg.content or ""
            if refusal:
                last_err = LLMError(f"모델이 응답을 거부함: {refusal}")
                break
            try:
                data = json.loads(content)
                obj = validate(data)
            except Exception as e:
                last_err = e
                self.usage.retries += 1
                messages.append({"role": "assistant", "content": content[:20000]})
                messages.append(
                    {
                        "role": "user",
                        "content": f"직전 응답이 검증에 실패했습니다: {str(e)[:1500]}\n규칙을 지켜 같은 스키마의 JSON 만 다시 출력하세요.",
                    }
                )
                continue
            problems = feedback(obj) if feedback is not None else []
            if problems and attempt < self.settings.max_attempts:
                self.usage.retries += 1
                messages.append({"role": "assistant", "content": content[:20000]})
                messages.append(
                    {
                        "role": "user",
                        "content": "직전 응답에서 근거 검증에 실패한 항목이 있습니다:\n- " + "\n- ".join(problems[:20])
                        + "\n해당 항목을 입력에 실제로 있는 change_id·코드 인용으로 고치거나 삭제하고, 같은 스키마의 JSON 전체를 다시 출력하세요.",
                    }
                )
                continue
            self.usage.log.append(
                {"purpose": purpose, "model": model, "attempt": attempt, "seconds": round(time.time() - t0, 2), "unresolved_problems": len(problems)}
            )
            if cpath is not None:
                cpath.parent.mkdir(parents=True, exist_ok=True)
                cpath.write_text(json.dumps({"model": model, "purpose": purpose, "response": data}, ensure_ascii=False, indent=1), encoding="utf-8")
            return obj
        self.usage.failures += 1
        raise LLMError(f"LLM 구조화 응답 실패({purpose}): {last_err}")
