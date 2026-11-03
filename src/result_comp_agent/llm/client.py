"""Azure OpenAI GPT 클라이언트 (.env 기반, 배포된 GPT 모델 특성에 자동 적응).

설정: .env / 환경변수의 4개 키
  AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY, AZURE_OPENAI_DEPLOYMENT, AZURE_OPENAI_API_VERSION
  (선택) AZURE_OPENAI_DEPLOYMENT_FAST(교차검증용, 없으면 같은 배포), AZURE_OPENAI_TIMEOUT(호출당 초),
         RCA_LLM_BUDGET_SECONDS(전체 LLM 시간 예산), AZURE_OPENAI_REASONING_EFFORT, AZURE_OPENAI_TEMPERATURE,
         AZURE_OPENAI_SEED, AZURE_OPENAI_MAX_OUTPUT_TOKENS, AZURE_OPENAI_USE_ENTRA_ID

GPT 모델별 차이에 대한 자동 적응 (배포별로 학습해 캐시에 저장)
  * reasoning_effort / temperature / seed 를 지원하지 않는 모델 → 400 응답의 param 을 보고 해당 파라미터 제외
  * max_completion_tokens ↔ max_tokens (모델/ api-version 에 따라) 자동 전환
  * Structured Outputs(json_schema) 미지원 → json_object → 프롬프트 지시 + 로컬 스키마 검증으로 단계적 하향
  * 함수 호출(tools) 미지원 → 도구 없이 사전 수집 컨텍스트로 진행

실패 처리
  * 오류 종류 분류: 시간초과/연결/인증/배포없음/한도초과/잘못된요청/콘텐츠필터/서버/검증/예산초과
  * 일시적 오류(시간초과·연결·429·5xx)만 백오프 재시도 (Retry-After 준수)
  * 인증 오류·배포 없음은 회로 차단 → 이후 호출 즉시 실패 (불필요한 대기 없음)
  * 모든 실패는 ``failures`` 에 기록되어 보고서에 '실패'로 표시
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, TypeVar

from .env import AzureEnv, resolve_azure_env
from .errors import FATAL, KIND_LABELS, RETRYABLE, LLMError, classify, error_info, retry_after_seconds

log = logging.getLogger(__name__)
T = TypeVar("T")

__all__ = ["LLMClient", "LLMSettings", "LLMError", "Capabilities", "extract_json"]


def _f(v: Optional[str]) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


@dataclass
class LLMSettings:
    endpoint: Optional[str] = None
    api_key: Optional[str] = None
    deployment_main: Optional[str] = None
    deployment_fast: Optional[str] = None  # None → deployment_main
    api_version: str = "2024-10-21"
    mode: str = "versioned"  # versioned | v1
    use_entra_id: bool = False
    reasoning_effort: Optional[str] = None  # 추론형 GPT 에서만 의미, 미지원 모델이면 자동 제외
    temperature: Optional[float] = None
    seed: Optional[int] = 42
    max_output_tokens: int = 8000
    timeout: float = 120.0  # 호출 1회 제한 시간(초)
    budget_seconds: float = 1800.0  # 실행 전체 LLM 시간 예산(초), 0 이면 무제한
    max_attempts: int = 3  # 일시적 오류 재시도 / 검증 재요청 횟수
    retry_backoff: float = 2.0
    cache_dir: Optional[str] = ".rca_cache/llm"
    enabled: Optional[bool] = None
    env_report: Optional[AzureEnv] = field(default=None, repr=False)

    @classmethod
    def from_env(
        cls,
        env_file: Optional[str] = None,
        config_dir: Optional[str] = None,
        environ: Optional[dict[str, str]] = None,
        override: bool = False,
        use_dotenv: bool = True,
        **overrides: Any,
    ) -> "LLMSettings":
        env = resolve_azure_env(env_file, config_dir, override=override, environ=environ, use_dotenv=use_dotenv)
        g = env.get
        s = cls(
            endpoint=g("AZURE_OPENAI_ENDPOINT"),
            api_key=g("AZURE_OPENAI_API_KEY"),
            deployment_main=g("AZURE_OPENAI_DEPLOYMENT"),
            deployment_fast=g("AZURE_OPENAI_DEPLOYMENT_FAST"),
            api_version=g("AZURE_OPENAI_API_VERSION") or "2024-10-21",
            mode=env.mode,
            use_entra_id=(g("AZURE_OPENAI_USE_ENTRA_ID") or "").lower() in ("1", "true", "yes", "on"),
            reasoning_effort=g("AZURE_OPENAI_REASONING_EFFORT") or None,
            temperature=_f(g("AZURE_OPENAI_TEMPERATURE")),
            seed=int(g("AZURE_OPENAI_SEED")) if (g("AZURE_OPENAI_SEED") or "").lstrip("-").isdigit() else 42,
            max_output_tokens=int(g("AZURE_OPENAI_MAX_OUTPUT_TOKENS") or 8000),
            timeout=_f(g("AZURE_OPENAI_TIMEOUT")) or 120.0,
            budget_seconds=_f(g("RCA_LLM_BUDGET_SECONDS")) if g("RCA_LLM_BUDGET_SECONDS") else 1800.0,
            env_report=env,
        )
        for k, v in overrides.items():
            if v is not None and hasattr(s, k):
                setattr(s, k, v)
        return s

    @property
    def fast(self) -> Optional[str]:
        return self.deployment_fast or self.deployment_main

    @property
    def is_active(self) -> bool:
        if self.enabled is False:
            return False
        has_cred = bool(self.api_key) or self.use_entra_id
        return bool(self.endpoint) and has_cred and bool(self.deployment_main)

    def missing(self) -> list[str]:
        out = []
        if not self.endpoint:
            out.append("AZURE_OPENAI_ENDPOINT")
        if not (self.api_key or self.use_entra_id):
            out.append("AZURE_OPENAI_API_KEY")
        if not self.deployment_main:
            out.append("AZURE_OPENAI_DEPLOYMENT")
        return out

    def public(self) -> dict[str, Any]:
        return {
            "active": self.is_active,
            "endpoint": self.endpoint,
            "auth": "entra_id" if self.use_entra_id else ("api_key" if self.api_key else None),
            "api_version": self.api_version,
            "mode": self.mode,
            "deployment_main": self.deployment_main,
            "deployment_fast": self.fast,
            "reasoning_effort": self.reasoning_effort,
            "temperature": self.temperature,
            "seed": self.seed,
            "timeout_seconds": self.timeout,
            "budget_seconds": self.budget_seconds,
            "env_file": self.env_report.env_file if self.env_report else None,
        }


@dataclass
class Capabilities:
    """배포(GPT 모델)별로 학습한 요청 파라미터 지원 여부."""

    reasoning_effort: bool = True
    temperature: bool = True
    seed: bool = True
    token_param: str = "max_completion_tokens"  # max_completion_tokens | max_tokens | none
    response_format: str = "json_schema"  # json_schema | json_object | none
    tools: bool = True
    learned: list[str] = field(default_factory=list)


@dataclass
class LLMUsage:
    calls: int = 0
    cache_hits: int = 0
    retries: int = 0
    failures: int = 0
    adaptations: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tool_calls: int = 0
    log: list[dict] = field(default_factory=list)


@dataclass
class ToolSession:
    used: bool
    transcript: str
    trace: list[dict]
    note: str = ""


_QUOTED_PARAM = re.compile(r"'([a-z_][a-z0-9_.]*)'", re.I)
_ADAPTABLE = ("reasoning_effort", "temperature", "seed", "max_completion_tokens", "max_tokens", "response_format", "tools", "tool_choice")


def extract_json(content: str) -> Any:
    """응답에서 JSON 추출 (코드펜스/앞뒤 설명문이 붙은 경우 포함)."""
    s = (content or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z0-9]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        a, b = s.find("{"), s.rfind("}")
        if a >= 0 and b > a:
            return json.loads(s[a : b + 1])
        raise


class LLMClient:
    """``transport`` 를 주입하면(테스트) 실제 Azure 호출 대신 사용한다.
    transport 는 ``.chat.completions.create(**kwargs)`` 를 제공해야 한다."""

    def __init__(self, settings: LLMSettings, transport: Any = None):
        self.settings = settings
        self._transport = transport
        self.usage = LLMUsage()
        self.caps: dict[str, Capabilities] = {}
        self.broken: Optional[LLMError] = None
        self.failures: list[dict] = []
        self.served_models: dict[str, set] = {}
        self._t0: Optional[float] = None
        self._load_caps()

    # ------------------------------------------------------------------ 기본
    @property
    def active(self) -> bool:
        if self.settings.enabled is False:
            return False
        return self._transport is not None or self.settings.is_active

    def deployment(self, tier: str) -> str:
        d = self.settings.fast if tier == "fast" else self.settings.deployment_main
        if not d:
            raise LLMError("AZURE_OPENAI_DEPLOYMENT 가 설정되지 않았습니다", "config")
        return d

    def _client(self):
        if self._transport is not None:
            return self._transport
        s = self.settings
        if not s.is_active:
            raise LLMError("Azure OpenAI 설정 누락: " + ", ".join(s.missing()), "config")
        from openai import AzureOpenAI, OpenAI

        token_provider: Optional[Callable[[], str]] = None
        if s.use_entra_id:
            try:
                from azure.identity import DefaultAzureCredential, get_bearer_token_provider
            except ImportError as e:  # pragma: no cover
                raise LLMError("Entra ID 인증에는 azure-identity 패키지가 필요합니다: pip install azure-identity", "config") from e
            token_provider = get_bearer_token_provider(DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default")
        if s.mode == "v1" or s.api_version.lower() == "v1":
            base_url = s.endpoint.rstrip("/") + "/openai/v1/"
            self._transport = OpenAI(base_url=base_url, api_key=token_provider or s.api_key, timeout=s.timeout, max_retries=0)
        else:
            kw: dict[str, Any] = dict(azure_endpoint=s.endpoint, api_version=s.api_version, timeout=s.timeout, max_retries=0)
            if token_provider:
                kw["azure_ad_token_provider"] = token_provider
            else:
                kw["api_key"] = s.api_key
            self._transport = AzureOpenAI(**kw)
        return self._transport

    # ------------------------------------------------------------------ 능력(capability) 캐시
    def _caps_file(self) -> Optional[Path]:
        return Path(self.settings.cache_dir) / "capabilities.json" if self.settings.cache_dir else None

    def _caps_key(self, deployment: str) -> str:
        return f"{self.settings.endpoint}|{deployment}|{self.settings.api_version}"

    def _load_caps(self) -> None:
        p = self._caps_file()
        if p is None or not p.exists():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            for k, v in data.items():
                if k.startswith(f"{self.settings.endpoint}|"):
                    dep = k.split("|")[1]
                    if k == self._caps_key(dep):
                        self.caps[dep] = Capabilities(**v)
        except Exception:
            log.warning("capabilities 캐시를 읽지 못함: %s", p)

    def _save_caps(self) -> None:
        p = self._caps_file()
        if p is None:
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        except Exception:
            data = {}
        for dep, c in self.caps.items():
            data[self._caps_key(dep)] = asdict(c)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    def caps_for(self, deployment: str) -> Capabilities:
        if deployment not in self.caps:
            self.caps[deployment] = Capabilities()
        return self.caps[deployment]

    @staticmethod
    def _offending_param(info: dict, params: dict) -> Optional[str]:
        p = (info.get("param") or "").split(".")[0].split("[")[0]
        if p in _ADAPTABLE:
            return p
        msg = info.get("message") or ""
        for m in _QUOTED_PARAM.finditer(msg):
            name = m.group(1).split(".")[0]
            if name in _ADAPTABLE and name in params:
                return name
        low = msg.lower()
        for name in _ADAPTABLE:
            if name in params and name in low:
                if name == "max_tokens" and "max_completion_tokens" in params:
                    continue
                return name
        if "json_schema" in low and "response_format" in params:
            return "response_format"
        return None

    def _adapt(self, caps: Capabilities, info: dict, params: dict) -> Optional[str]:
        name = self._offending_param(info, params)
        if name == "reasoning_effort" and caps.reasoning_effort:
            caps.reasoning_effort = False
            return "reasoning_effort 미지원 → 제외"
        if name == "temperature" and caps.temperature:
            caps.temperature = False
            return "temperature 미지원 → 제외"
        if name == "seed" and caps.seed:
            caps.seed = False
            return "seed 미지원 → 제외"
        if name == "max_completion_tokens" and caps.token_param == "max_completion_tokens":
            caps.token_param = "max_tokens"
            return "max_completion_tokens 미지원 → max_tokens 사용"
        if name == "max_tokens" and caps.token_param == "max_tokens":
            caps.token_param = "max_completion_tokens"
            return "max_tokens 미지원 → max_completion_tokens 사용"
        if name == "response_format":
            if caps.response_format == "json_schema":
                caps.response_format = "json_object"
                return "json_schema(Structured Outputs) 미지원 → json_object + 로컬 스키마 검증"
            if caps.response_format == "json_object":
                caps.response_format = "none"
                return "response_format 미지원 → 프롬프트 지시 + 로컬 스키마 검증"
        if name in ("tools", "tool_choice") and caps.tools:
            caps.tools = False
            return "함수 호출(tools) 미지원 → 사전 수집 컨텍스트만 사용"
        return None

    # ------------------------------------------------------------------ 시간 예산 / 실패 기록
    def _remaining(self) -> Optional[float]:
        b = self.settings.budget_seconds
        if not b or b <= 0:
            return None
        if self._t0 is None:
            self._t0 = time.monotonic()
        return b - (time.monotonic() - self._t0)

    def _record_failure(self, e: LLMError) -> None:
        self.usage.failures += 1
        self.failures.append({"purpose": e.purpose, "kind": e.kind, "label": KIND_LABELS.get(e.kind, e.kind), "message": str(e)[:500]})

    # ------------------------------------------------------------------ 저수준 호출
    def _params(self, caps: Capabilities, model: str, messages: list[dict], schema_name: Optional[str], schema: Optional[dict], tools: Optional[list] = None) -> dict:
        s = self.settings
        msgs = [dict(m) for m in messages]
        params: dict[str, Any] = {"model": model}
        if caps.token_param != "none" and s.max_output_tokens:
            params[caps.token_param] = s.max_output_tokens
        if s.reasoning_effort and caps.reasoning_effort:
            params["reasoning_effort"] = s.reasoning_effort
        if s.temperature is not None and caps.temperature:
            params["temperature"] = s.temperature
        if s.seed is not None and caps.seed:
            params["seed"] = s.seed
        if schema is not None:
            if caps.response_format == "json_schema":
                params["response_format"] = {"type": "json_schema", "json_schema": {"name": schema_name, "schema": schema, "strict": True}}
            else:
                if caps.response_format == "json_object":
                    params["response_format"] = {"type": "json_object"}
                instr = (
                    "\n\n[출력 형식] 반드시 아래 JSON 스키마를 만족하는 JSON 객체 하나만 출력하십시오. 설명문·코드펜스 금지.\n"
                    + json.dumps(schema, ensure_ascii=False)
                )
                msgs[0] = {**msgs[0], "content": msgs[0]["content"] + instr}
        if tools and caps.tools:
            params["tools"] = tools
            params["tool_choice"] = "auto"
        params["messages"] = msgs
        return params

    def _invoke(self, build: Callable[[Capabilities], dict], deployment: str, purpose: str):
        caps = self.caps_for(deployment)
        client = self._client()
        attempts = adaptations = 0
        while True:
            if self.broken is not None:
                raise LLMError(f"선행 치명 오류로 호출 중단: {self.broken}", "circuit_open", purpose)
            rem = self._remaining()
            if rem is not None and rem <= 0:
                raise LLMError(f"LLM 시간 예산({self.settings.budget_seconds:.0f}초) 소진", "budget", purpose)
            params = build(caps)
            timeout = self.settings.timeout if rem is None else max(1.0, min(self.settings.timeout, rem))
            self.usage.calls += 1
            t0 = time.monotonic()
            try:
                resp = client.chat.completions.create(**params, timeout=timeout)
            except Exception as e:
                kind = classify(e)
                info = error_info(e)
                if kind == "bad_request":
                    note = self._adapt(caps, info, params)
                    if note and adaptations < 8:
                        adaptations += 1
                        self.usage.adaptations += 1
                        caps.learned.append(note)
                        self._save_caps()
                        log.info("[%s] %s", deployment, note)
                        continue
                    raise LLMError(f"잘못된 요청(400): {info['message'][:400]}", "bad_request", purpose, info["status"])
                if kind in FATAL:
                    err = LLMError(
                        f"{KIND_LABELS[kind]}: {info['message'][:300]}"
                        + (" — AZURE_OPENAI_DEPLOYMENT(배포 이름)·AZURE_OPENAI_API_VERSION 을 확인하세요" if kind == "not_found" else "")
                        + (" — AZURE_OPENAI_API_KEY 를 확인하세요" if kind == "auth" else ""),
                        kind, purpose, info["status"],
                    )
                    self.broken = err
                    raise err
                if kind == "content_filter":
                    raise LLMError(f"Azure 콘텐츠 필터가 요청을 차단: {info['message'][:300]}", kind, purpose, info["status"])
                attempts += 1
                if kind in RETRYABLE and attempts < self.settings.max_attempts:
                    self.usage.retries += 1
                    wait = retry_after_seconds(e)
                    if wait is None and self.settings.retry_backoff > 0:
                        wait = min(self.settings.retry_backoff ** attempts, 30)
                    if wait:
                        rem2 = self._remaining()
                        if rem2 is not None:
                            wait = max(0.0, min(wait, rem2 - 1))
                        time.sleep(wait)
                    log.warning("[%s] %s (%s/%s) 재시도", purpose, KIND_LABELS.get(kind, kind), attempts, self.settings.max_attempts)
                    continue
                suffix = f" ({timeout:.0f}초 제한, {attempts}회 시도)" if kind == "timeout" else f" ({attempts}회 시도)"
                raise LLMError(f"{KIND_LABELS.get(kind, kind)}: {info['message'][:300]}{suffix}", kind, purpose, info["status"])
            usage = getattr(resp, "usage", None)
            if usage is not None:
                self.usage.prompt_tokens += int(getattr(usage, "prompt_tokens", 0) or 0)
                self.usage.completion_tokens += int(getattr(usage, "completion_tokens", 0) or 0)
            served = getattr(resp, "model", None)
            if served:
                self.served_models.setdefault(deployment, set()).add(str(served))
            self.usage.log.append({"purpose": purpose, "deployment": deployment, "seconds": round(time.monotonic() - t0, 2)})
            return resp

    # ------------------------------------------------------------------ 구조화 출력
    def _cache_path(self, key: str) -> Optional[Path]:
        if not self.settings.cache_dir:
            return None
        return Path(self.settings.cache_dir) / f"{key}.json"

    def _cache_key(self, *parts: Any) -> str:
        return hashlib.sha256(json.dumps([*parts, self.settings.reasoning_effort, self.settings.endpoint], ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()

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
        extra_context: Optional[str] = None,
        use_cache: bool = True,
    ) -> T:
        """JSON 스키마를 강제한 호출.

        * ``validate``: dict → 검증된 객체. 실패(예외) 시 오류를 알려주고 재요청 — 끝까지 실패하면 LLMError(validation).
        * ``feedback``: 검증된 객체의 '근거 문제' 목록(없는 ID·코드에 없는 인용 등). 문제가 있으면 목록을 알려주고
          재작성을 요청하되, 마지막 시도에서는 받아들인다(문제 항목은 이후 VerifierAgent 가 개별 기각).
        """
        try:
            obj, _ = self._structured(tier, system, user, schema_name, schema, validate, feedback, purpose, extra_context, use_cache)
            return obj
        except LLMError as e:
            e.purpose = e.purpose or purpose
            self._record_failure(e)
            raise

    def _structured(self, tier, system, user, schema_name, schema, validate, feedback, purpose, extra_context, use_cache):
        model = self.deployment(tier)
        key = self._cache_key(model, system, user, extra_context, schema_name, schema)
        cpath = self._cache_path(key) if use_cache else None
        if cpath is not None and cpath.exists():
            try:
                data = json.loads(cpath.read_text(encoding="utf-8"))
                obj = validate(data["response"])
                self.usage.cache_hits += 1
                self.usage.log.append({"purpose": purpose, "deployment": model, "cache": True})
                return obj, data["response"]
            except Exception:
                log.warning("LLM 캐시 손상 → 재호출: %s", cpath)
        messages: list[dict] = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        if extra_context:
            messages.append({"role": "user", "content": extra_context})
        last_err: Optional[str] = None
        for attempt in range(1, self.settings.max_attempts + 1):
            resp = self._invoke(lambda caps: self._params(caps, model, messages, schema_name, schema), model, purpose)
            choice = resp.choices[0]
            msg = choice.message
            finish = getattr(choice, "finish_reason", None)
            if getattr(msg, "refusal", None):
                raise LLMError(f"모델이 응답을 거부함: {msg.refusal}", "refusal", purpose)
            if finish == "content_filter":
                raise LLMError("Azure 콘텐츠 필터로 응답이 차단됨", "content_filter", purpose)
            content = msg.content or ""
            try:
                if finish == "length":
                    raise ValueError("응답이 최대 출력 토큰 제한으로 잘렸습니다. 항목 수를 줄이고 더 간결하게 작성하세요")
                data = extract_json(content)
                obj = validate(data)
            except Exception as e:
                last_err = str(e)
                self.usage.retries += 1
                messages.append({"role": "assistant", "content": content[:20000]})
                messages.append({"role": "user", "content": f"직전 응답이 검증에 실패했습니다: {str(e)[:1500]}\n규칙을 지켜 같은 스키마의 JSON 만 다시 출력하세요."})
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
            if self.usage.log:
                self.usage.log[-1].update({"attempt": attempt, "unresolved_problems": len(problems)})
            if cpath is not None:
                cpath.parent.mkdir(parents=True, exist_ok=True)
                cpath.write_text(json.dumps({"deployment": model, "purpose": purpose, "response": data}, ensure_ascii=False, indent=1), encoding="utf-8")
            return obj, data
        raise LLMError(f"응답 형식 검증 실패({self.settings.max_attempts}회): {last_err}", "validation", purpose)

    # ------------------------------------------------------------------ 함수 호출(도구) 세션
    def tool_session(self, *, tier: str, system: str, user: str, registry, ctx, max_steps: int = 6, max_tool_calls: int = 20, purpose: str = "") -> ToolSession:
        """GPT 가 도구(read-only)를 호출해 코드·문서·변경을 추가 조회하는 세션.
        결과는 최종 구조화 호출에 '도구 조회 기록'(텍스트)으로 전달된다 → 모델/버전에 무관하게 안정적."""
        model = self.deployment(tier)
        caps = self.caps_for(model)
        if not caps.tools:
            return ToolSession(False, "", [], "배포가 함수 호출을 지원하지 않아 도구 없이 진행")
        specs = registry.specs()
        messages: list[dict] = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        trace: list[dict] = []
        n_calls = 0
        try:
            for step in range(1, max_steps + 1):
                resp = self._invoke(lambda c: self._params(c, model, messages, None, None, tools=specs), model, purpose)
                if not caps.tools:
                    return ToolSession(False, "", trace, "배포가 함수 호출을 지원하지 않아 도구 없이 진행")
                msg = resp.choices[0].message
                calls = list(getattr(msg, "tool_calls", None) or [])
                if not calls:
                    break
                messages.append(
                    {
                        "role": "assistant",
                        "content": msg.content or None,  # 도구 호출만 있는 응답은 null 이 표준
                        "tool_calls": [{"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments}} for c in calls],
                    }
                )
                for c in calls:
                    n_calls += 1
                    if n_calls > max_tool_calls:
                        result, err = json.dumps({"error": f"도구 호출 한도({max_tool_calls}) 초과"}, ensure_ascii=False), "limit"
                    else:
                        result, err = registry.call(c.function.name, c.function.arguments, ctx)
                    self.usage.tool_calls += 1
                    messages.append({"role": "tool", "tool_call_id": c.id, "content": result})
                    trace.append({"step": step, "tool": c.function.name, "arguments": (c.function.arguments or "")[:500], "result_chars": len(result), "error": err})
        except LLMError as e:
            e.purpose = e.purpose or purpose
            self._record_failure(e)
            if e.kind in FATAL or e.kind in ("budget", "circuit_open"):
                raise
            return ToolSession(bool(trace), self._transcript(trace, messages), trace, f"도구 세션 중단: {e.label}")
        return ToolSession(bool(trace), self._transcript(trace, messages), trace)

    @staticmethod
    def _transcript(trace: list[dict], messages: list[dict], max_chars: int = 40000) -> str:
        if not trace:
            return ""
        parts = []
        calls = {}
        for m in messages:
            if m.get("role") == "assistant" and m.get("tool_calls"):
                for tc in m["tool_calls"]:
                    calls[tc["id"]] = tc["function"]
            elif m.get("role") == "tool":
                fn = calls.get(m["tool_call_id"], {})
                parts.append(f"### {fn.get('name')}({fn.get('arguments')})\n{m['content']}")
        out = "[GPT 도구 조회 기록 — 아래 결과는 실제 코드/문서/변경 데이터에서 조회한 것]\n\n" + "\n\n".join(parts)
        return out if len(out) <= max_chars else out[: max_chars - 40] + "\n…(조회 기록 일부 생략)"

    def structured_with_tools(
        self, *, tier, system, user, schema_name, schema, validate, feedback=None, purpose="", registry=None, ctx=None, max_steps: int = 6,
        max_tool_calls: int = 20,
    ) -> tuple[Any, list[dict], str]:
        """도구 조회 → 구조화 출력. 반환: (객체, 도구 trace, 비고). 결과 전체를 캐시해 재현성 확보."""
        model = self.deployment(tier)
        tool_names = [t["function"]["name"] for t in registry.specs()] if registry else []
        key = self._cache_key("tools-v1", model, system, user, schema_name, schema, tool_names)
        cpath = self._cache_path(key)
        if cpath is not None and cpath.exists():
            try:
                data = json.loads(cpath.read_text(encoding="utf-8"))
                obj = validate(data["response"])
                self.usage.cache_hits += 1
                return obj, data.get("trace", []), "캐시"
            except Exception:
                log.warning("LLM 캐시 손상 → 재호출: %s", cpath)
        note = ""
        session = ToolSession(False, "", [])
        if registry is not None:
            session = self.tool_session(
                tier=tier, system=system, user=user, registry=registry, ctx=ctx, max_steps=max_steps, max_tool_calls=max_tool_calls, purpose=purpose + ":tools"
            )
            note = session.note
        try:
            obj, data = self._structured(tier, system, user, schema_name, schema, validate, feedback, purpose, session.transcript or None, False)
        except LLMError as e:
            e.purpose = e.purpose or purpose
            self._record_failure(e)
            raise
        if cpath is not None:
            cpath.parent.mkdir(parents=True, exist_ok=True)
            cpath.write_text(json.dumps({"deployment": model, "purpose": purpose, "response": data, "trace": session.trace}, ensure_ascii=False, indent=1), encoding="utf-8")
        return obj, session.trace, note

    # ------------------------------------------------------------------ 진단
    def probe(self, tools: bool = True) -> dict:
        """배포 연결·인증·응답 형식·도구 호출 지원을 실제로 확인 (rca check-env --probe)."""
        out: dict[str, Any] = {"ok": False}
        schema = {"type": "object", "additionalProperties": False, "required": ["ok", "message"], "properties": {"ok": {"type": "boolean"}, "message": {"type": "string"}}}
        t0 = time.monotonic()
        try:
            obj = self.structured(
                tier="main", system="연결 점검입니다. JSON 으로 답하세요.", user='{"ok": true, "message": "pong"} 을 그대로 출력하세요.',
                schema_name="probe", schema=schema, validate=lambda d: d, purpose="probe", use_cache=False,
            )
            out.update(ok=bool(obj.get("ok", True)), seconds=round(time.monotonic() - t0, 2))
        except LLMError as e:
            out.update(error_kind=e.kind, error_label=e.label, error=str(e), seconds=round(time.monotonic() - t0, 2))
            return out
        if tools:
            try:
                from ..tools import ToolRegistry

                reg = ToolRegistry()
                reg.register_function("echo", "입력 문자열을 그대로 돌려줍니다.", {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}, lambda ctx, text: {"echo": text})
                s = self.tool_session(tier="main", system="도구 점검입니다.", user="echo 도구를 text='ping' 으로 한 번 호출한 뒤 끝내세요.", registry=reg, ctx=None, max_steps=2, purpose="probe:tools")
                out["tools"] = "supported" if s.used else ("unsupported" if not self.caps_for(self.deployment("main")).tools else "not_called")
            except LLMError as e:
                out["tools"] = f"error: {e.label}"
        dep = self.deployment("main")
        out["served_models"] = sorted(self.served_models.get(dep, []))
        out["capabilities"] = asdict(self.caps_for(dep))
        return out

    def info(self) -> dict:
        d = dict(self.settings.public())
        u = self.usage
        d.update(
            {
                "active": self.active,
                "calls": u.calls, "cache_hits": u.cache_hits, "retries": u.retries, "failures": u.failures,
                "adaptations": u.adaptations, "tool_calls": u.tool_calls,
                "prompt_tokens": u.prompt_tokens, "completion_tokens": u.completion_tokens,
                "served_models": {k: sorted(v) for k, v in self.served_models.items()},
                "capabilities": {k: asdict(v) for k, v in self.caps.items()},
                "failure_events": self.failures[:50],
                "circuit_open": str(self.broken) if self.broken else None,
            }
        )
        return d
