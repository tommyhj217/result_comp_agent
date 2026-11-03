"""LLM 오류 분류 — 실패를 '무엇 때문에' 실패했는지 보고서에 정확히 표시하기 위함."""
from __future__ import annotations

from typing import Optional

KIND_LABELS = {
    "timeout": "시간 초과",
    "budget": "LLM 시간 예산 초과",
    "connection": "연결 실패",
    "auth": "인증/권한 오류",
    "not_found": "배포/엔드포인트 없음",
    "rate_limit": "요청 한도 초과",
    "bad_request": "잘못된 요청",
    "content_filter": "콘텐츠 필터 차단",
    "server": "Azure 서버 오류",
    "validation": "응답 형식 검증 실패",
    "refusal": "모델 응답 거부",
    "circuit_open": "선행 치명 오류로 호출 중단",
    "config": "설정 오류",
    "error": "알 수 없는 오류",
}

RETRYABLE = {"timeout", "connection", "rate_limit", "server", "error"}
FATAL = {"auth", "not_found", "config"}  # 이후 호출도 모두 실패하므로 회로 차단


class LLMError(RuntimeError):
    def __init__(self, message: str, kind: str = "error", purpose: str = "", status: Optional[int] = None):
        super().__init__(message)
        self.kind = kind
        self.purpose = purpose
        self.status = status

    @property
    def label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)


def error_info(e: BaseException) -> dict:
    """openai SDK 예외(또는 유사 객체)에서 상태코드/코드/파라미터/메시지 추출."""
    body = getattr(e, "body", None)
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        body = body["error"]
    code = getattr(e, "code", None) or (body.get("code") if isinstance(body, dict) else None)
    param = getattr(e, "param", None) or (body.get("param") if isinstance(body, dict) else None)
    msg = getattr(e, "message", None) or (body.get("message") if isinstance(body, dict) else None) or str(e)
    status = getattr(e, "status_code", None)
    if status is None:
        resp = getattr(e, "response", None)
        status = getattr(resp, "status_code", None)
    return {"status": status, "code": str(code) if code else None, "param": param, "message": str(msg)}


def classify(e: BaseException) -> str:
    name = type(e).__name__
    info = error_info(e)
    status = info["status"]
    code = (info["code"] or "").lower()
    msg = info["message"].lower()
    if "timeout" in name.lower() or isinstance(e, TimeoutError):
        return "timeout"
    if "contentfilter" in name.lower() or code == "content_filter" or "content management policy" in msg:
        return "content_filter"
    if "connection" in name.lower() or isinstance(e, ConnectionError):
        return "connection"
    if status == 401 or "authentication" in name.lower():
        return "auth"
    if status == 403 or "permissiondenied" in name.lower():
        return "auth"
    if status == 404 or "notfound" in name.lower() or code == "deploymentnotfound":
        return "not_found"
    if status == 429 or "ratelimit" in name.lower():
        return "rate_limit"
    if status in (400, 422) or "badrequest" in name.lower() or "unprocessable" in name.lower():
        return "bad_request"
    if isinstance(status, int) and status >= 500:
        return "server"
    return "error"


def retry_after_seconds(e: BaseException) -> Optional[float]:
    resp = getattr(e, "response", None)
    headers = getattr(resp, "headers", None)
    if not headers:
        return None
    for h in ("retry-after-ms", "retry-after"):
        v = headers.get(h) if hasattr(headers, "get") else None
        if v:
            try:
                return float(v) / (1000.0 if h.endswith("-ms") else 1.0)
            except ValueError:
                return None
    return None
