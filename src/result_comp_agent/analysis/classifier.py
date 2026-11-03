"""규칙 기반 '사용자 보완 유형' 분류기 (결정론적, 규칙 ID 기록).

LLM 을 쓰더라도 이 분류가 항상 기준선으로 남고, LLM 결과와의 일치 여부가 신뢰도에 반영된다.
"""
from __future__ import annotations

from ..diff import normalize as N
from ..diff.align import text_sim
from ..models import Change, ChangeType

CATEGORIES: dict[str, str] = {
    "CONTENT_ADDITION": "내용 추가",
    "CONTENT_REMOVAL": "내용 삭제",
    "FACT_CORRECTION": "값·사실 정정",
    "CONTENT_ENRICHMENT": "내용 보강·구체화",
    "CONTENT_REWRITE": "내용 재작성",
    "REWORDING": "표현 수정",
    "STRUCTURE_CHANGE": "구조 변경",
    "REVIEW_ANNOTATION": "검토 의견·메모",
    "FORMULA_CHANGE": "수식 변경",
    "VISUAL_CHANGE": "시각 자료 변경",
    "FORMAT_ONLY": "서식·표기 변경",
}

CATEGORY_GUIDE: dict[str, str] = {
    "CONTENT_ADDITION": "에이전트가 생성하지 않은 새 항목/문단/슬라이드/값을 사용자가 추가",
    "CONTENT_REMOVAL": "에이전트가 생성한 항목을 사용자가 삭제 (불필요/부정확)",
    "FACT_CORRECTION": "수치·날짜·등급·담당 등 값이 틀려서 정정",
    "CONTENT_ENRICHMENT": "기존 내용을 유지하며 근거·세부사항·조건을 덧붙여 구체화",
    "CONTENT_REWRITE": "같은 위치의 내용을 거의 새로 작성",
    "REWORDING": "의미는 유지하고 표현만 다듬음",
    "STRUCTURE_CHANGE": "열/시트/슬라이드 구성, 컬럼명, 순서 등 산출물 구조 변경",
    "REVIEW_ANNOTATION": "메모·발표자 노트 등 검토 의견 추가/수정",
    "FORMULA_CHANGE": "엑셀 수식 변경",
    "VISUAL_CHANGE": "이미지·차트 변경",
    "FORMAT_ONLY": "공백·숫자 표기·서식·위치 등 내용과 무관한 변경",
}

_STRUCT = {
    ChangeType.SHEET_ADDED, ChangeType.SHEET_REMOVED, ChangeType.SHEET_RENAMED, ChangeType.COLUMN_ADDED,
    ChangeType.COLUMN_REMOVED, ChangeType.COLUMN_MOVED, ChangeType.ROW_MOVED, ChangeType.SLIDE_MOVED,
}
_FORMAT = {ChangeType.STYLE_MODIFIED, ChangeType.SHAPE_GEOMETRY_CHANGED, ChangeType.LAYOUT_CHANGED}


def classify(ch: Change) -> tuple[str, str]:
    """(category, rule_id)"""
    ct = ch.change_type
    part = ch.location.part or ""
    if ch.format_only or ct in _FORMAT:
        return "FORMAT_ONLY", "R01_format_only"
    if ct in (ChangeType.COMMENT_ADDED, ChangeType.COMMENT_MODIFIED, ChangeType.COMMENT_REMOVED) or part == "notes":
        return "REVIEW_ANNOTATION", "R02_comment_or_notes"
    if ct in _STRUCT:
        return "STRUCTURE_CHANGE", "R03_structure"
    if ct == ChangeType.CELL_MODIFIED and ch.detail.get("is_header"):
        return "STRUCTURE_CHANGE", "R04_header_renamed"
    if part == "title" and ct == ChangeType.TEXT_MODIFIED:
        return "STRUCTURE_CHANGE", "R05_slide_title_changed"
    if ct in (ChangeType.IMAGE_REPLACED, ChangeType.CHART_MODIFIED):
        return "VISUAL_CHANGE", "R06_visual"
    if ct == ChangeType.SHAPE_ADDED:
        kind = (ch.detail.get("content") or {}).get("kind")
        if kind in ("picture", "chart"):
            return "VISUAL_CHANGE", "R06_visual_added"
        return "CONTENT_ADDITION", "R07_added"
    if ct in (ChangeType.SLIDE_ADDED, ChangeType.ROW_ADDED, ChangeType.TEXT_ADDED):
        return "CONTENT_ADDITION", "R07_added"
    if ct in (ChangeType.SLIDE_REMOVED, ChangeType.ROW_REMOVED, ChangeType.TEXT_REMOVED, ChangeType.SHAPE_REMOVED):
        return "CONTENT_REMOVAL", "R08_removed"
    if ct == ChangeType.CELL_MODIFIED and ch.detail.get("formula"):
        return "FORMULA_CHANGE", "R09_formula"
    if ct in (ChangeType.CELL_MODIFIED, ChangeType.TEXT_MODIFIED):
        b = N.collapse_ws(ch.before or "")
        a = N.collapse_ws(ch.after or "")
        if not b and a:
            return "CONTENT_ADDITION", "R10_filled_empty"
        if b and not a:
            return "CONTENT_REMOVAL", "R11_cleared"
        if ch.detail.get("numeric_delta") is not None:
            return "FACT_CORRECTION", "R12_numeric_value"
        if N.parse_date(b) and N.parse_date(a):
            return "FACT_CORRECTION", "R13_date_value"
        nb, na = N.numbers_in_text(b), N.numbers_in_text(a)
        if nb and nb != na and text_sim(N.strip_numbers(b), N.strip_numbers(a)) >= 0.7:
            return "FACT_CORRECTION", "R14_numbers_in_text"
        if len(b) <= 12 and len(a) <= 12 and "\n" not in a:
            return "FACT_CORRECTION", "R15_short_value"
        nb_, na_ = N.norm_for_match(b), N.norm_for_match(a)
        if nb_ and nb_ in na_ and len(a) >= 1.2 * len(b):
            return "CONTENT_ENRICHMENT", "R16_superset"
        sim = text_sim(b, a)
        if len(a) >= 1.5 * len(b) and sim >= 0.3:
            return "CONTENT_ENRICHMENT", "R17_much_longer"
        if sim >= 0.6:
            return "REWORDING", "R18_similar"
        return "CONTENT_REWRITE", "R19_low_similarity"
    return "CONTENT_REWRITE", "R99_fallback"


def classify_all(changes: list[Change]) -> None:
    for ch in changes:
        ch.category, ch.category_rule = classify(ch)
