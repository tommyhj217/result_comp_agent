"""보고서 공용 라벨/포맷 함수."""
from __future__ import annotations

from collections import Counter

from ..analysis.classifier import CATEGORIES
from ..diff import normalize as N
from ..models import AgentAnalysis, Change, CodeRef, Recommendation

STATUS_LABEL = {"VERIFIED": "검증됨", "PARTIAL": "부분 검증", "UNVERIFIED": "미검증", "REJECTED": "기각"}
STATUS_ICON = {"VERIFIED": "✅", "PARTIAL": "🟡", "UNVERIFIED": "⚪", "REJECTED": "⛔"}
PRIORITY_LABEL = {"high": "높음", "medium": "보통", "low": "낮음"}
METHOD_LABEL = {
    "exact": "정확 일치",
    "normalized": "정규화 일치",
    "substring": "부분 문자열",
    "fuzzy": "유사 일치",
    "neighbor": "이웃 항목 추론",
    "container": "형제 항목 컨테이너 추론",
    "diff": "코드 diff",
    "llm_quote": "LLM 인용(검증됨)",
}


def cat_label(c: str | None) -> str:
    return CATEGORIES.get(c or "", c or "")


def ref_text(r: CodeRef, with_method: bool = True) -> str:
    loc = f"{r.file}" + (f":{r.line_start}" if r.line_start else "") + (f"-{r.line_end}" if r.line_end and r.line_end != r.line_start else "")
    if r.sub_location:
        loc += f" [{r.sub_location}]"
    if r.symbol:
        loc += f" ({r.symbol})"
    if with_method:
        loc += f" · {METHOD_LABEL.get(r.match_method, r.match_method)} {r.score:.2f}"
    return loc


def agent_counts(a: AgentAnalysis) -> dict:
    changes = [c for comp in a.comparisons for c in comp.changes]
    st = Counter(r.verification.status for r in a.recommendations)
    return {
        "pairs": len(a.comparisons),
        "content": sum(1 for c in changes if not c.format_only),
        "format": sum(1 for c in changes if c.format_only),
        "integrity_pass": sum(1 for c in a.comparisons if c.integrity.status == "PASS"),
        "trace_ratio": a.trace_stats.get("ratio"),
        "verified": st.get("VERIFIED", 0),
        "partial": st.get("PARTIAL", 0),
        "rejected": len(a.rejected_recommendations),
    }


def pct(x) -> str:
    return "-" if x is None else f"{x * 100:.0f}%"


def short(s, n=80) -> str:
    return N.shorten(s, n)


def change_brief(c: Change) -> str:
    if c.before and c.after:
        return f"「{short(c.before, 50)}」→「{short(c.after, 60)}」"
    if c.after:
        return f"(추가)「{short(c.after, 80)}」"
    if c.before:
        return f"(삭제)「{short(c.before, 80)}」"
    return ""


def sorted_changes(changes: list[Change]) -> list[Change]:
    order = {"STRUCTURE_CHANGE": 0, "CONTENT_ADDITION": 1, "FACT_CORRECTION": 2, "CONTENT_ENRICHMENT": 3, "CONTENT_REWRITE": 4,
             "CONTENT_REMOVAL": 5, "REVIEW_ANNOTATION": 6, "REWORDING": 7, "FORMULA_CHANGE": 8, "VISUAL_CHANGE": 9, "FORMAT_ONLY": 10}
    return sorted(changes, key=lambda c: (c.format_only, order.get(c.category or "", 11), c.id))


def all_recs(a: AgentAnalysis) -> list[Recommendation]:
    return a.recommendations + a.rejected_recommendations


# ------------------------------------------------------------------ 실패 표시용 라벨
def failure_kind_label(kind: str) -> str:
    from ..documents.base import DOC_ERROR_LABELS
    from ..llm.errors import KIND_LABELS

    extra = {
        "integrity_fail": "무결성 검증 실패",
        "git_error": "git 오류",
        "git_clone": "저장소 클론 실패",
        "ref_not_found": "브랜치/커밋 없음",
        "no_files": "코드 파일 없음",
        "index_error": "코드 인덱싱 실패",
        "plugin_error": "플러그인 로드 실패",
        "write_error": "보고서 작성 실패",
    }
    return extra.get(kind) or DOC_ERROR_LABELS.get(kind) or KIND_LABELS.get(kind) or kind


CATEGORY_LABEL = {
    "comparison": "산출물 비교",
    "integrity": "무결성 검증",
    "llm": "GPT(Azure OpenAI)",
    "code": "에이전트 코드",
    "report": "보고서",
    "config": "설정/플러그인",
}
STATUS_TEXT = {"SUCCESS": "성공", "PARTIAL": "부분 실패", "FAILED": "실패"}
