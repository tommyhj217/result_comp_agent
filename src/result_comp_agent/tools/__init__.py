"""GPT 함수 호출(function calling)용 도구 레지스트리.

GPT 가 분석 중에 필요한 코드·문서·변경 정보를 '직접 조회'할 수 있게 하여, 미리 넣어준 컨텍스트 밖의 범위까지
근거를 확장한다. 모든 기본 도구는 읽기 전용이며 결과는 크기 제한(truncate)된다.

도구 추가 방법 (플러그인 모듈에서)::

    from result_comp_agent.tools import register_tool

    @register_tool("count_rows", "시트의 행 수를 센다", {"type": "object", "properties": {"pair_id": {"type": "string"}}, "required": ["pair_id"]})
    def count_rows(ctx, pair_id):
        ...
        return {"rows": 10}

설정 파일 ``plugins: [my_pkg.my_tools]`` 또는 환경변수 ``RCA_PLUGINS`` 로 모듈을 지정하면 실행 시 자동 등록된다.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

MAX_RESULT_CHARS = 8000


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    func: Callable[..., Any]
    source: str = "builtin"


class ToolRegistry:
    def __init__(self, max_result_chars: int = MAX_RESULT_CHARS):
        self._tools: dict[str, Tool] = {}
        self.max_result_chars = max_result_chars

    def register(self, tool: Tool, replace: bool = False) -> Tool:
        if not tool.name.replace("_", "").isalnum():
            raise ValueError(f"도구 이름은 영문/숫자/_ 만 허용: {tool.name}")
        if tool.name in self._tools and not replace:
            raise ValueError(f"이미 등록된 도구: {tool.name}")
        params = dict(tool.parameters or {"type": "object", "properties": {}})
        params.setdefault("type", "object")
        params.setdefault("properties", {})
        tool.parameters = params
        self._tools[tool.name] = tool
        return tool

    def register_function(self, name: str, description: str, parameters: dict, func: Callable[..., Any], source: str = "custom", replace: bool = False) -> Tool:
        return self.register(Tool(name, description, parameters, func, source), replace=replace)

    def tool(self, name: Optional[str] = None, description: str = "", parameters: Optional[dict] = None, source: str = "custom"):
        def deco(fn):
            self.register(Tool(name or fn.__name__, description or (fn.__doc__ or "").strip(), parameters or {}, fn, source), replace=True)
            return fn

        return deco

    def names(self) -> list[str]:
        return list(self._tools)

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def list(self) -> list[Tool]:
        return list(self._tools.values())

    def subset(self, names: list[str]) -> "ToolRegistry":
        r = ToolRegistry(self.max_result_chars)
        for n in names:
            if n in self._tools:
                r._tools[n] = self._tools[n]
        return r

    def copy(self) -> "ToolRegistry":
        return self.subset(self.names())

    def specs(self) -> list[dict]:
        return [
            {"type": "function", "function": {"name": t.name, "description": t.description[:1000], "parameters": t.parameters}}
            for t in self._tools.values()
        ]

    def call(self, name: str, arguments: Any, ctx: Any) -> tuple[str, Optional[str]]:
        """도구 실행. 반환: (JSON 문자열 결과, 오류 종류 또는 None). 예외는 모델에 오류 결과로 전달된다."""
        tool = self._tools.get(name)
        if tool is None:
            return self._dump({"error": f"알 수 없는 도구: {name}", "available": self.names()}), "unknown_tool"
        try:
            args = json.loads(arguments) if isinstance(arguments, str) and arguments.strip() else (arguments or {})
            if not isinstance(args, dict):
                raise ValueError("인자는 JSON 객체여야 합니다")
        except Exception as e:
            return self._dump({"error": f"인자 JSON 파싱 실패: {e}"}), "bad_arguments"
        props = tool.parameters.get("properties", {})
        missing = [p for p in tool.parameters.get("required", []) if p not in args]
        if missing:
            return self._dump({"error": f"필수 인자 누락: {missing}"}), "bad_arguments"
        clean = {k: v for k, v in args.items() if k in props} if props else {}
        try:
            result = tool.func(ctx, **clean)
        except Exception as e:  # 도구 오류는 모델에게 알려주고 계속
            return self._dump({"error": f"{type(e).__name__}: {e}"}), "tool_error"
        return self._dump(result), None

    def _dump(self, obj: Any) -> str:
        s = json.dumps(obj, ensure_ascii=False, default=str)
        if len(s) > self.max_result_chars:
            s = json.dumps(
                {"truncated": True, "total_chars": len(s), "note": "결과가 길어 앞부분만 표시합니다. 범위를 좁혀 다시 조회하세요.",
                 "partial": s[: self.max_result_chars - 200]},
                ensure_ascii=False,
            )
        return s


DEFAULT_REGISTRY = ToolRegistry()


def register_tool(name: str, description: str, parameters: Optional[dict] = None, source: str = "plugin"):
    """플러그인용 데코레이터 — 기본 레지스트리에 도구를 등록한다."""
    return DEFAULT_REGISTRY.tool(name, description, parameters, source)


def default_registry() -> ToolRegistry:
    from . import builtin  # noqa: F401  (기본 도구 등록)

    return DEFAULT_REGISTRY


__all__ = ["Tool", "ToolRegistry", "DEFAULT_REGISTRY", "register_tool", "default_registry"]
