"""규칙 기반 개선 권고 (LLM 없이도 동작하는 기준선).

패턴(반복 보완) → 에이전트 코드에서 고칠 위치와 방법. 모든 권고는 근거 change_id 와 코드 위치를 가진다.
"""
from __future__ import annotations

from typing import Optional

from ..diff import normalize as N
from ..models import Change, CodeRef, Pattern, Recommendation
from .classifier import CATEGORIES


def _priority(p: Pattern, n_pairs: int, structural: bool) -> str:
    if len(p.support_pairs) >= 2 and p.support_ratio >= 0.5:
        return "high"
    if p.support_ratio >= 0.34 and n_pairs >= 3:
        return "medium"
    if structural or len(p.change_ids) >= 3:
        return "medium"
    return "low"


def _pick_refs(p: Pattern, changes: dict[str, Change], prefer: tuple[str, ...]) -> list[CodeRef]:
    pool: dict[tuple, CodeRef] = {}
    for cid in p.change_ids:
        ch = changes.get(cid)
        if not ch:
            continue
        for r in ch.code_refs:
            k = (r.file, r.line_start, r.sub_location)
            if k not in pool or pool[k].score < r.score:
                pool[k] = r

    def rank(r: CodeRef):
        idx = [i for i, x in enumerate(prefer) if x in (r.kind, r.anchor_role)]
        return (min(idx) if idx else len(prefer), -r.score, r.file, r.line_start)

    return sorted(pool.values(), key=rank)[:4]


def _support_text(p: Pattern, n_pairs: int) -> str:
    return f"{len(p.support_pairs)}/{n_pairs}개 비교쌍에서 {len(p.change_ids)}건"


def _examples_text(p: Pattern, n: int = 2) -> str:
    ex = []
    for e in p.examples[:n]:
        if e.get("before") and e.get("after"):
            ex.append(f"「{N.shorten(e['before'], 60)}」→「{N.shorten(e['after'], 80)}」")
        elif e.get("after"):
            ex.append(f"「{N.shorten(e['after'], 90)}」")
        elif e.get("before"):
            ex.append(f"(삭제)「{N.shorten(e['before'], 80)}」")
    return " / ".join(ex)


def _hardcoded_values(p: Pattern, changes: dict[str, Change]) -> list[tuple[Change, CodeRef]]:
    """근거 변경의 '원본 값'이 코드에 문자열 그대로 있는 경우 (하드코딩된 기본값/고정 문구)."""
    out = []
    for cid in p.change_ids:
        ch = changes.get(cid)
        if not ch:
            continue
        for r in ch.code_refs:
            if r.anchor_role == "value" and r.match_method in ("exact", "normalized") and r.kind != "template":
                out.append((ch, r))
                break
    return out


def recommend_for_pattern(p: Pattern, changes: dict[str, Change], n_pairs: int) -> Optional[Recommendation]:
    key = p.key
    kind = key.split("|", 1)[0]
    sup = _support_text(p, n_pairs)
    ex = _examples_text(p)
    first = changes.get(p.change_ids[0])
    loc_hint = p.location_hint
    title = problem = rec = ""
    structural = False
    prefer: tuple[str, ...] = ("literal",)

    if kind == "col_add":
        name = first.after if first else "?"
        vals = ", ".join(N.shorten(v.get("value"), 20) for v in (first.detail.get("values") or [])[1:4]) if first else ""
        title = f"{loc_hint} 출력 스키마에 '{name}' 열 추가"
        problem = f"사용자가 '{name}' 열을 직접 추가함 ({sup}). 입력 예: {vals}"
        rec = f"열 정의(컬럼 목록)에 '{name}'를 추가하고, 생성 프롬프트에 '{name}' 작성 기준(허용 값/판단 근거)을 명시하세요."
        structural, prefer = True, ("insertion_point",)
    elif kind == "col_del":
        name = first.before if first else "?"
        title = f"'{name}' 열 필요성 재검토"
        problem = f"사용자가 에이전트가 만든 '{name}' 열을 삭제함 ({sup})."
        rec = f"'{name}' 열이 실제 업무에 필요 없는지 확인 후 열 정의와 프롬프트에서 제거하거나 선택 옵션으로 전환하세요."
        structural, prefer = True, ("column",)
    elif kind == "hdr":
        b, a = (first.before, first.after) if first else ("?", "?")
        title = f"컬럼명 '{b}' → '{a}' 변경"
        problem = f"사용자가 컬럼명을 '{b}'에서 '{a}'로 바꿈 ({sup})."
        rec = f"코드의 컬럼명 리터럴 '{b}'를 '{a}'로 변경하고 프롬프트/후속 에이전트 참조도 함께 갱신하세요."
        structural, prefer = True, ("column",)
    elif kind == "slide_add":
        t = (first.detail.get("content") or {}).get("title") if first else "?"
        title = f"'{t}' 섹션(슬라이드) 생성 추가"
        problem = f"사용자가 '{t}' 슬라이드를 직접 추가함 ({sup}). 내용 예: {ex}"
        rec = f"섹션 목록에 '{t}'를 추가하고(삽입 위치 참조), 해당 섹션 생성 프롬프트를 작성하세요."
        structural, prefer = True, ("insertion_point",)
    elif kind == "section_add":
        t = first.after if first else "?"
        title = f"'{N.shorten(t, 40)}' 섹션 생성 추가"
        problem = f"사용자가 '{N.shorten(t, 40)}' 섹션을 직접 추가함 ({sup})."
        rec = f"문서 섹션 구성(목차/섹션 목록)에 '{N.shorten(t, 40)}'를 추가하고(삽입 위치 참조), 해당 섹션 작성 지침을 프롬프트에 넣으세요."
        structural, prefer = True, ("insertion_point", "prompt")
    elif kind == "section_del":
        t = first.before if first else "?"
        title = f"'{N.shorten(t, 40)}' 섹션 생성 여부 재검토"
        problem = f"사용자가 에이전트가 만든 '{N.shorten(t, 40)}' 섹션을 삭제함 ({sup})."
        rec = "해당 섹션을 섹션 목록·프롬프트에서 제거하거나 조건부 생성으로 바꾸세요."
        structural, prefer = True, ("section", "prompt")
    elif kind == "prop":
        prop = first.detail.get("property") if first else ""
        action = first.detail.get("action") if first else ""
        desc = (first.after or first.before or "") if first else ""
        if prop == "data_validation" and action == "added":
            title = f"{loc_hint} 입력 규칙(데이터 유효성) 코드화"
            problem = f"사용자가 입력 규칙을 직접 추가함 ({sup}): {desc}"
            rec = "생성 코드에서 openpyxl DataValidation 으로 같은 규칙(허용 값 목록·범위)을 적용하고, 프롬프트의 허용 값 정의와 일치시키세요."
            structural, prefer = True, ("column", "sheet")
        elif prop == "data_validation":
            title = f"{loc_hint} 입력 규칙 변경 반영"
            problem = f"사용자가 입력 규칙을 삭제/변경함 ({sup}): {desc}"
            rec = "생성 코드의 데이터 유효성 규칙을 사용자 수정에 맞게 조정하세요."
            structural, prefer = True, ("column", "sheet")
        elif len(p.support_pairs) >= 2:
            title = f"{loc_hint} 시트 서식/설정 코드화 ({prop})"
            problem = f"사용자가 반복적으로 같은 시트 설정을 적용함 ({sup}): {desc}"
            rec = "조건부 서식·틀 고정·필터·병합 등 사용자가 매번 하는 설정을 생성 코드에 추가하세요."
            prefer = ("sheet",)
        else:
            return None
    elif kind == "slide_del":
        title = f"{loc_hint} 생성 여부 재검토"
        problem = f"사용자가 에이전트가 만든 슬라이드를 삭제함 ({sup})."
        rec = "해당 섹션을 섹션 목록에서 제거하거나, 조건부 생성(입력 정보가 있을 때만)으로 바꾸세요."
        structural, prefer = True, ("section",)
    elif kind in ("slide_move", "row_moved"):
        title = f"{loc_hint} 순서 조정"
        problem = f"사용자가 순서를 변경함 ({sup})."
        rec = "섹션/항목 출력 순서 또는 정렬 기준을 사용자 수정 순서에 맞게 조정하세요."
        structural, prefer = True, ("section", "literal")
    elif kind == "row_added":
        title = f"{loc_hint} 항목 누락 보완 (생성 범위 확대)"
        problem = f"사용자가 행(항목)을 직접 추가함 ({sup}). 예: {ex}"
        rec = "프롬프트에 누락된 관점(예: 비기능/예외/보안 등 추가된 항목 유형)을 명시하고, 최소 생성 개수·체크리스트를 지정하세요."
        prefer = ("prompt", "text_line", "sheet")
    elif kind == "row_removed":
        title = f"{loc_hint} 불필요 항목 생성 억제"
        problem = f"사용자가 에이전트가 생성한 행을 삭제함 ({sup}). 예: {ex}"
        rec = "삭제된 항목 유형을 프롬프트의 제외 조건으로 명시하거나, 생성 후 필터링 규칙을 추가하세요."
        prefer = ("prompt", "text_line", "value")
    elif kind in ("cell", "text_modified", "text_removed") and _hardcoded_values(p, changes):
        hv = _hardcoded_values(p, changes)
        ch0, ref0 = hv[0]
        vals = ", ".join(sorted({f"「{N.shorten(c.before, 30)}」→「{N.shorten(c.after, 30) or '(삭제)'}」" for c, _ in hv}))
        title = f"하드코딩된 값 '{N.shorten(ch0.before, 30)}' 수정 ({loc_hint})"
        problem = f"코드에 고정된 값이 산출물에 그대로 들어갔고 사용자가 이를 수정함 ({sup}): {vals}"
        rec = (f"{ref0.file}:{ref0.line_start} 의 고정값을 사용자 수정 내용 기준으로 바꾸거나, "
               "입력(설정/매핑 테이블/LLM 생성)으로 결정되도록 변경하세요.")
        prefer = ("value",)
    elif kind == "cell":
        col = first.location.column_label if first else "?"
        cat = p.category
        if cat == "FACT_CORRECTION":
            title = f"'{col}' 값 정확도 개선"
            problem = f"사용자가 '{col}' 값을 정정함 ({sup}). 예: {ex}"
            rec = f"'{col}' 산정 기준을 프롬프트에 명확히 하고, 근거 데이터 연결 또는 생성 후 검증 규칙을 추가하세요."
        elif cat in ("CONTENT_ENRICHMENT", "CONTENT_REWRITE", "CONTENT_ADDITION"):
            title = f"'{col}' 작성 품질 개선 (구체화)"
            problem = f"사용자가 '{col}' 내용을 보강/재작성함 ({sup}). 예: {ex}"
            rec = f"프롬프트의 '{col}' 작성 지침에 사용자 보완 형태(세부 조건·수치·근거)를 요구사항으로 추가하고 좋은 예시를 포함하세요."
        elif cat == "CONTENT_REMOVAL":
            title = f"'{col}' 불필요 값 생성 억제"
            problem = f"사용자가 '{col}' 값을 비움 ({sup})."
            rec = f"'{col}' 값을 생성하지 않거나 확실할 때만 채우도록 지침을 바꾸세요."
        elif cat == "REWORDING":
            title = f"'{col}' 표현 가이드 보완"
            problem = f"사용자가 '{col}' 표현을 다듬음 ({sup}). 예: {ex}"
            rec = f"'{col}' 용어·문체 가이드(사내 표준 용어)를 프롬프트에 추가하세요."
        elif cat == "FORMULA_CHANGE":
            title = f"'{col}' 수식 수정"
            problem = f"사용자가 수식을 수정함 ({sup}). 예: {ex}"
            rec = "수식을 생성하는 코드를 사용자 수정 수식으로 교체하세요."
        else:
            return None
        prefer = ("prompt", "text_line", "column")
    elif kind in ("text_added", "text_modified", "text_removed", "shape_added", "shape_removed", "table_added", "table_removed"):
        cat = p.category
        title = f"{loc_hint} {CATEGORIES.get(cat, cat)} 반영"
        problem = f"사용자 보완 ({sup}). 예: {ex}"
        if cat == "FACT_CORRECTION":
            rec = "수치/사실 생성 시 출처 데이터 입력을 요구하거나, 생성 후 수치 검증 단계를 추가하세요."
        elif cat == "CONTENT_REMOVAL":
            rec = "삭제된 유형의 문장을 생성하지 않도록 섹션 프롬프트에 제외 조건을 추가하세요."
        else:
            rec = "섹션 프롬프트에 사용자가 추가/보강한 내용 유형(예: 출처, 수치 근거, 세부 항목)을 필수 요소로 명시하세요."
        prefer = ("prompt", "text_line", "section")
    elif kind == "notes":
        title = f"{loc_hint}: 검토 의견 확인"
        problem = f"사용자가 발표자 노트에 검토 의견/근거를 남김 ({sup}). 예: {ex}"
        rec = "노트 내용(근거·출처·후속 확인 사항)을 슬라이드 생성 시 함께 작성하도록 프롬프트에 포함하세요."
        prefer = ("prompt", "section")
    elif kind == "comment":
        title = f"{loc_hint}: 셀 메모 검토 의견 확인"
        problem = f"사용자가 셀 메모로 검토 의견을 남김 ({sup}). 예: {ex}"
        rec = "메모로 지적된 사항을 생성 지침/검증 체크리스트에 반영하세요."
        prefer = ("column", "prompt")
    elif kind == "style":
        if len(p.support_pairs) < 2 or p.support_ratio < 0.5:
            return None
        title = f"{loc_hint} 서식 규칙 코드화"
        problem = f"사용자가 반복적으로 같은 서식을 적용함 ({sup}). {first.after if first else ''}"
        rec = "조건부 서식 또는 생성 코드의 셀 스타일 지정으로 사용자 서식을 자동화하세요."
        prefer = ("column", "sheet")
    elif kind in ("sheet_added", "sheet_removed", "sheet_renamed"):
        title = f"{p.title}"
        problem = f"사용자가 시트 구성을 변경함 ({sup})."
        rec = "통합문서 생성 코드의 시트 구성을 사용자 수정에 맞게 조정하세요."
        structural, prefer = True, ("insertion_point", "sheet")
    else:
        if p.category == "FORMAT_ONLY":
            return None
        title = p.title
        problem = f"사용자 보완 ({sup}). 예: {ex}"
        rec = "해당 위치의 생성 로직/프롬프트를 사용자 수정 형태에 맞게 조정하세요."
    if p.category == "FORMAT_ONLY" and kind not in ("style", "prop"):
        return None
    return Recommendation(
        id="",
        agent_id=p.agent_id,
        title=title,
        problem=problem,
        recommendation=rec,
        category=p.category,
        priority=_priority(p, n_pairs, structural),
        source="rule",
        evidence_change_ids=list(p.change_ids),
        pattern_ids=[p.id],
        code_refs=_pick_refs(p, changes, prefer),
    )


def recommend(agent_id: str, patterns: list[Pattern], changes: dict[str, Change], n_pairs: int) -> list[Recommendation]:
    out = []
    for p in patterns:
        r = recommend_for_pattern(p, changes, n_pairs)
        if r is not None:
            out.append(r)
    for i, r in enumerate(out, start=1):
        r.id = f"{agent_id}:R{i:03d}"
    return out
