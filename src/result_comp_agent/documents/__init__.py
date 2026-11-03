"""문서 형식 핸들러 레지스트리 — 비교 가능한 문서 범위를 확장하는 지점.

기본 제공: Excel(.xlsx/.xlsm) · PowerPoint(.pptx) · Word(.docx) · CSV/TSV · Markdown(.md) · 텍스트(.txt) · PDF(.pdf)

새 형식 추가 (플러그인 모듈에서)::

    from result_comp_agent.documents import DocumentHandler, register_handler

    class HwpxHandler(DocumentHandler):
        name, artifact, extensions = "hwpx", "hwpx", (".hwpx",)
        def compare(self, baseline, revised, builder, opts): ...   # CompareOutput 반환 (무결성 검증 포함 권장)
        def outline(self, path): ...                                # DocUnit 목록 (GPT 도구·inspect 용)

    register_handler(HwpxHandler())

흐름형 문서라면 ``FlowDocHandler`` 를 상속해 ``extract(path) -> FlowDoc`` 만 구현하면
diff·무결성 검증·outline 이 자동으로 제공된다.
"""
from __future__ import annotations

import importlib
from pathlib import Path
from typing import Optional

from .base import CompareOptions, CompareOutput, DocUnit, DocumentError, DocumentHandler, FlowDocHandler, select_units, search_units

_REGISTRY: dict[str, DocumentHandler] = {}
_BUILTINS_LOADED = False


def register_handler(handler: DocumentHandler, replace: bool = True) -> DocumentHandler:
    if not handler.name or not handler.extensions:
        raise ValueError("핸들러에는 name 과 extensions 가 필요합니다")
    if handler.name in _REGISTRY and not replace:
        raise ValueError(f"이미 등록된 핸들러: {handler.name}")
    _REGISTRY[handler.name] = handler
    return handler


def _ensure_builtins() -> None:
    global _BUILTINS_LOADED
    if not _BUILTINS_LOADED:
        _BUILTINS_LOADED = True
        importlib.import_module(".builtin", __name__)


def list_handlers() -> list[DocumentHandler]:
    _ensure_builtins()
    return list(_REGISTRY.values())


def supported_extensions() -> list[str]:
    return sorted({e for h in list_handlers() for e in h.extensions})


def handler_for(path: str | Path) -> DocumentHandler:
    _ensure_builtins()
    ext = Path(path).suffix.lower()
    # 나중에 등록된 핸들러(플러그인)가 우선
    for h in reversed(list(_REGISTRY.values())):
        if ext in h.extensions:
            return h
    raise DocumentError("unsupported", f"지원하지 않는 문서 형식 '{ext or Path(path).name}'. 지원 형식: {', '.join(supported_extensions())}")


def get_handler(name: str) -> Optional[DocumentHandler]:
    _ensure_builtins()
    return _REGISTRY.get(name)


__all__ = [
    "CompareOptions", "CompareOutput", "DocUnit", "DocumentError", "DocumentHandler", "FlowDocHandler",
    "register_handler", "list_handlers", "handler_for", "get_handler", "supported_extensions", "select_units", "search_units",
]
