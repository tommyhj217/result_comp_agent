"""GPT 도구가 조회하는 분석 컨텍스트 (에이전트 1개 단위)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from ..code.indexer import CodeIndex
from ..models import Change, CodeDiffReport, ComparisonResult, Pattern


@dataclass
class ToolContext:
    agent_id: str
    index: Optional[CodeIndex] = None
    changes: dict[str, Change] = field(default_factory=dict)
    comparisons: list[ComparisonResult] = field(default_factory=list)
    patterns: list[Pattern] = field(default_factory=list)
    code_diffs: list[CodeDiffReport] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)  # 플러그인 도구용 확장 공간
    _outlines: dict[tuple[str, str], list] = field(default_factory=dict)

    def comparison(self, pair_id: str) -> Optional[ComparisonResult]:
        return next((c for c in self.comparisons if c.pair_id == pair_id), None)

    def outline(self, pair_id: str, side: str):
        """비교쌍 문서(원본/수정본)의 조회 단위 목록 (캐시)."""
        from ..documents import handler_for

        comp = self.comparison(pair_id)
        if comp is None:
            raise KeyError(f"비교쌍 없음: {pair_id} (가능: {[c.pair_id for c in self.comparisons]})")
        side = "baseline" if side.lower().startswith(("b", "o", "원")) else "revised"
        key = (pair_id, side)
        if key not in self._outlines:
            path = comp.baseline_path if side == "baseline" else comp.revised_path
            self._outlines[key] = handler_for(path).outline(path)
        return self._outlines[key]
