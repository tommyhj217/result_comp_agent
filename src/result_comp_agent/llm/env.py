""".env 기반 Azure OpenAI(GPT) 설정 로더.

필수 키 (.env 또는 환경변수)
  AZURE_OPENAI_ENDPOINT      https://<resource>.openai.azure.com  (cognitiveservices / services.ai.azure.com 도 가능)
  AZURE_OPENAI_API_KEY
  AZURE_OPENAI_DEPLOYMENT    Azure 포털의 GPT 배포 이름
  AZURE_OPENAI_API_VERSION   예: 2024-10-21, 2025-04-01-preview, v1

오타·별칭 허용: 흔한 변형 키(AZURE_OPEN_API_VERSION, AZUTE_OPEN_API_VERSION, AZURE_OPENAI_DEPLOYMENT_NAME 등)도 읽되
``rca check-env`` 에서 표준 키 이름을 쓰도록 경고한다.

우선순위: (기본) 실제 환경변수 > .env 파일.  ``override=True`` 이면 .env 가 우선.
.env 파일 탐색: 명시 경로 > RCA_ENV_FILE > 설정파일 폴더의 .env > 현재 폴더의 .env
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

# 표준 키 → 허용 별칭 (앞쪽이 우선)
KEYS: dict[str, list[str]] = {
    "AZURE_OPENAI_ENDPOINT": ["AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_BASE_URL", "AZURE_ENDPOINT", "AZURE_OPEN_API_ENDPOINT"],
    "AZURE_OPENAI_API_KEY": ["AZURE_OPENAI_API_KEY", "AZURE_OPENAI_KEY", "AZURE_API_KEY", "AZURE_OPEN_API_KEY"],
    "AZURE_OPENAI_DEPLOYMENT": [
        "AZURE_OPENAI_DEPLOYMENT", "AZURE_OPENAI_DEPLOYMENT_NAME", "AZURE_OPENAI_CHAT_DEPLOYMENT", "AZURE_OPENAI_DEPLOYMENT_MAIN",
        "AZURE_OPEN_API_DEPLOYMENT",
    ],
    "AZURE_OPENAI_API_VERSION": [
        "AZURE_OPENAI_API_VERSION", "AZURE_OPENAI_VERSION", "OPENAI_API_VERSION", "AZURE_OPEN_API_VERSION", "AZUTE_OPEN_API_VERSION",
        "AZURE_OPENAI_API_VER",
    ],
    # 선택
    "AZURE_OPENAI_DEPLOYMENT_FAST": ["AZURE_OPENAI_DEPLOYMENT_FAST", "AZURE_OPENAI_DEPLOYMENT_MINI"],
    "AZURE_OPENAI_USE_ENTRA_ID": ["AZURE_OPENAI_USE_ENTRA_ID"],
    "AZURE_OPENAI_TIMEOUT": ["AZURE_OPENAI_TIMEOUT", "AZURE_OPENAI_TIMEOUT_SECONDS"],
    "AZURE_OPENAI_REASONING_EFFORT": ["AZURE_OPENAI_REASONING_EFFORT"],
    "AZURE_OPENAI_TEMPERATURE": ["AZURE_OPENAI_TEMPERATURE"],
    "AZURE_OPENAI_SEED": ["AZURE_OPENAI_SEED"],
    "AZURE_OPENAI_MAX_OUTPUT_TOKENS": ["AZURE_OPENAI_MAX_OUTPUT_TOKENS", "AZURE_OPENAI_MAX_TOKENS"],
    "RCA_LLM_BUDGET_SECONDS": ["RCA_LLM_BUDGET_SECONDS"],
}
REQUIRED = ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_DEPLOYMENT", "AZURE_OPENAI_API_VERSION")
DEFAULT_API_VERSION = "2024-10-21"

_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_.\-]*)\s*[=:]\s*(.*)$")


def parse_dotenv(text: str) -> dict[str, str]:
    """간단·안전한 .env 파서 (따옴표, export, 주석, 이스케이프 지원 / 변수 치환은 하지 않음)."""
    out: dict[str, str] = {}
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    i = 0
    while i < len(lines):
        raw = lines[i]
        i += 1
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        m = _LINE_RE.match(raw)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if val[:1] in ('"', "'"):
            q = val[0]
            body = val[1:]
            # 여러 줄 따옴표 값
            while q not in body.replace("\\" + q, "") and i < len(lines):
                body += "\n" + lines[i]
                i += 1
            end = body.rfind(q)
            body = body[:end] if end >= 0 else body
            if q == '"':
                body = body.replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")
            val = body
        else:
            val = re.split(r"\s+#", val, maxsplit=1)[0].strip()
        out[key] = val
    return out


def find_env_file(explicit: Optional[str] = None, config_dir: Optional[str] = None) -> Optional[Path]:
    cands = []
    if explicit:
        cands.append(Path(explicit).expanduser())
    if os.environ.get("RCA_ENV_FILE"):
        cands.append(Path(os.environ["RCA_ENV_FILE"]).expanduser())
    if config_dir:
        cands.append(Path(config_dir) / ".env")
    cands.append(Path.cwd() / ".env")
    for c in cands:
        if c.is_file():
            return c.resolve()
    if explicit:
        raise FileNotFoundError(f".env 파일을 찾을 수 없습니다: {explicit}")
    return None


@dataclass
class ResolvedValue:
    key: str  # 표준 키
    value: Optional[str]
    source: str  # "env" | ".env" | "default" | "missing"
    alias: Optional[str] = None  # 실제로 읽은 키 이름 (표준 키와 다르면 경고)


@dataclass
class AzureEnv:
    values: dict[str, ResolvedValue] = field(default_factory=dict)
    env_file: Optional[str] = None
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    mode: str = "versioned"  # versioned | v1

    def get(self, key: str) -> Optional[str]:
        v = self.values.get(key)
        return v.value if v else None

    @property
    def complete(self) -> bool:
        return not self.errors

    def masked(self) -> list[dict]:
        rows = []
        for k, v in self.values.items():
            val = v.value
            if val and "KEY" in k:
                val = (val[:4] + "…" + val[-4:]) if len(val) > 10 else "****"
            rows.append({"key": k, "value": val, "source": v.source, "read_from": v.alias or k})
        return rows


def normalize_endpoint(endpoint: str) -> tuple[str, dict[str, str], list[str]]:
    """엔드포인트에 배포/버전이 포함된 전체 URL 을 붙여넣은 경우 분해한다.
    반환: (기본 엔드포인트, 추출값{deployment, api_version, mode}, 경고)"""
    warnings: list[str] = []
    extracted: dict[str, str] = {}
    ep = endpoint.strip().strip('"').strip("'")
    if not re.match(r"^https?://", ep):
        ep = "https://" + ep
        warnings.append("AZURE_OPENAI_ENDPOINT 에 스킴이 없어 https:// 를 붙였습니다")
    u = urlparse(ep)
    if u.scheme != "https":
        warnings.append("AZURE_OPENAI_ENDPOINT 가 https 가 아닙니다")
    path = u.path or ""
    qs = parse_qs(u.query or "")
    if "api-version" in qs:
        extracted["api_version"] = qs["api-version"][0]
    m = re.search(r"/openai/deployments/([^/]+)", path)
    if m:
        extracted["deployment"] = m.group(1)
    if re.search(r"/openai/v1/?", path):
        extracted["mode"] = "v1"
    base = f"{u.scheme}://{u.netloc}"
    if path not in ("", "/") and not (path.rstrip("/") == "/openai/v1"):
        warnings.append(f"엔드포인트에 경로가 포함되어 있어 기본 주소({base})만 사용합니다")
    host = u.netloc.lower()
    if host and not any(host.endswith(s) for s in (".openai.azure.com", ".cognitiveservices.azure.com", ".services.ai.azure.com", ".azure-api.net")):
        warnings.append(f"Azure OpenAI 도메인이 아닌 엔드포인트입니다: {host} (프록시/APIM 이면 무시)")
    return base, extracted, warnings


def resolve_azure_env(
    env_file: Optional[str] = None,
    config_dir: Optional[str] = None,
    override: bool = False,
    environ: Optional[dict[str, str]] = None,
    use_dotenv: bool = True,
) -> AzureEnv:
    environ = dict(os.environ if environ is None else environ)
    path = find_env_file(env_file, config_dir) if use_dotenv else None
    dot = parse_dotenv(path.read_text(encoding="utf-8-sig")) if path else {}
    res = AzureEnv(env_file=str(path) if path else None)

    def lookup(std: str) -> ResolvedValue:
        sources = [(dot, ".env"), (environ, "env")] if override else [(environ, "env"), (dot, ".env")]
        for src, name in sources:
            for alias in KEYS[std]:
                v = src.get(alias)
                if v is not None and str(v).strip() != "":
                    return ResolvedValue(std, str(v).strip(), name, alias if alias != std else None)
        return ResolvedValue(std, None, "missing")

    for std in KEYS:
        rv = lookup(std)
        res.values[std] = rv
        if rv.alias:
            res.warnings.append(f"'{rv.alias}' 키를 '{std}' 로 해석했습니다 — 표준 이름 '{std}' 사용을 권장합니다")

    ep = res.get("AZURE_OPENAI_ENDPOINT")
    if ep:
        base, extracted, w = normalize_endpoint(ep)
        res.values["AZURE_OPENAI_ENDPOINT"].value = base
        res.warnings += w
        if extracted.get("deployment") and not res.get("AZURE_OPENAI_DEPLOYMENT"):
            res.values["AZURE_OPENAI_DEPLOYMENT"] = ResolvedValue("AZURE_OPENAI_DEPLOYMENT", extracted["deployment"], "endpoint-url")
            res.warnings.append("배포 이름을 엔드포인트 URL 에서 추출했습니다")
        if extracted.get("api_version") and not res.get("AZURE_OPENAI_API_VERSION"):
            res.values["AZURE_OPENAI_API_VERSION"] = ResolvedValue("AZURE_OPENAI_API_VERSION", extracted["api_version"], "endpoint-url")
            res.warnings.append("api-version 을 엔드포인트 URL 에서 추출했습니다")
        if extracted.get("mode") == "v1":
            res.mode = "v1"
    ver = res.get("AZURE_OPENAI_API_VERSION")
    if not ver:
        res.values["AZURE_OPENAI_API_VERSION"] = ResolvedValue("AZURE_OPENAI_API_VERSION", DEFAULT_API_VERSION, "default")
        res.warnings.append(f"AZURE_OPENAI_API_VERSION 이 없어 기본값 {DEFAULT_API_VERSION} 을 사용합니다")
    elif ver.lower() in ("v1", "latest", "preview") or res.mode == "v1":
        res.mode = "v1"
    elif not re.match(r"^\d{4}-\d{2}-\d{2}(-preview)?$", ver):
        res.warnings.append(f"api-version 형식이 일반적이지 않습니다: {ver} (예: 2024-10-21, 2025-04-01-preview)")

    entra = (res.get("AZURE_OPENAI_USE_ENTRA_ID") or "").lower() in ("1", "true", "yes", "on")
    for k in REQUIRED:
        if k == "AZURE_OPENAI_API_KEY" and entra:
            continue
        if not res.get(k):
            res.errors.append(f"{k} 가 설정되지 않았습니다" + (f" (.env: {path})" if path else " (.env 파일 없음)"))
    return res
