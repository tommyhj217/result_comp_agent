"""Excel 보고서 — 필터/정렬 가능한 전체 근거 데이터."""
from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from ..models import AnalysisResult, CHANGE_TYPE_LABELS
from .common import CATEGORY_LABEL, PRIORITY_LABEL, STATUS_LABEL, STATUS_TEXT, agent_counts, cat_label, failure_kind_label, pct, ref_text

HEAD_FILL = PatternFill("solid", fgColor="1F4E78")
HEAD_FONT = Font(bold=True, color="FFFFFF")
STATUS_FILL = {
    "VERIFIED": PatternFill("solid", fgColor="E2EFDA"),
    "PARTIAL": PatternFill("solid", fgColor="FFF2CC"),
    "REJECTED": PatternFill("solid", fgColor="F8CBAD"),
}
_ILLEGAL = {i for i in range(32)} - {9, 10, 13}


def _clean(v):
    if isinstance(v, str):
        v = "".join(ch for ch in v if ord(ch) not in _ILLEGAL)
        if v.startswith("="):
            v = "'" + v
        return v[:32000]
    return v


def _sheet(wb: Workbook, title: str, headers: list[str], rows: list[list], widths: list[int] | None = None):
    ws = wb.create_sheet(title)
    ws.append(headers)
    for c in range(1, len(headers) + 1):
        cell = ws.cell(1, c)
        cell.fill, cell.font = HEAD_FILL, HEAD_FONT
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    for r in rows:
        ws.append([_clean(v) for v in r])
    for i, w in enumerate(widths or [], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    ws.freeze_panes = "A2"
    if rows:
        ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{len(rows) + 1}"
    return ws


def write_excel(res: AnalysisResult, path: str | Path) -> None:
    wb = Workbook()
    wb.remove(wb.active)

    rel = res.reliability
    summary_rows = []
    for a in res.agents:
        k = agent_counts(a)
        summary_rows.append([a.stage, a.name, a.agent_id, a.output_type, a.repo, a.ref, (a.commit or "")[:12], k["pairs"], k["content"], k["format"],
                             f"{k['integrity_pass']}/{k['pairs']}", pct(k["trace_ratio"]), k["verified"], k["partial"], k["rejected"], a.code_error or ""])
    ws = _sheet(wb, "요약", ["단계", "에이전트", "ID", "산출물", "저장소", "ref", "커밋", "비교쌍", "내용 변경", "서식 변경", "무결성 PASS",
                            "코드 추적률", "검증된 권고", "부분 검증", "기각", "코드 비고"], summary_rows,
                [14, 20, 16, 8, 30, 12, 14, 8, 10, 10, 12, 12, 10, 10, 8, 30])
    r0 = len(summary_rows) + 3
    llm = res.llm or {}
    info = [
        ("실행 결과", f"{STATUS_TEXT.get(res.status, res.status)} (종료코드 {res.exit_code})"),
        ("strict", f"{'사용' if (res.strict or {}).get('enabled') else '미사용'} · 위반: {', '.join((res.strict or {}).get('violated') or []) or '없음'}"),
        ("실패", ", ".join(f"{CATEGORY_LABEL.get(f.category, f.category)}:{failure_kind_label(f.kind)}" for f in res.failures[:20]) or "없음"),
        ("프로젝트", res.project), ("생성 시각", res.generated_at), ("도구 버전", res.tool_version),
        ("해석 엔진", f"GPT ({llm.get('deployment_main')}, API {llm.get('api_version')})" if llm.get("active") else "오프라인(규칙 기반)"),
        ("응답 모델", "; ".join(f"{k}: {', '.join(v)}" for k, v in (llm.get("served_models") or {}).items()) or "-"),
        ("변경 검출", rel.get("detection")),
        ("diff 무결성", f"{rel.get('integrity', {}).get('pass')}/{rel.get('integrity', {}).get('pairs')} PASS"),
        ("코드 추적률", pct(rel.get("trace", {}).get("ratio"))),
        ("규칙-LLM 분류 일치율", pct(rel.get("classification", {}).get("rule_llm_agreement"))),
        ("권고 검증", ", ".join(f"{STATUS_LABEL[k]} {v}" for k, v in rel.get("recommendations", {}).items())),
    ]
    for i, (k, v) in enumerate(info):
        ws.cell(r0 + i, 1, k).font = Font(bold=True)
        ws.cell(r0 + i, 2, _clean(str(v)))

    rows = []
    for a in res.agents:
        for comp in a.comparisons:
            for c in comp.changes:
                top = c.code_refs[0] if c.code_refs else None
                rows.append([
                    c.id, a.name, comp.pair_id, comp.revised_by, c.artifact, CHANGE_TYPE_LABELS.get(c.change_type.value, c.change_type.value),
                    c.location.path, c.location.sheet or (c.location.slide_index and f"슬라이드 {c.location.slide_index}") or "",
                    c.location.column_label, c.location.row_label, c.before, c.after, "Y" if c.format_only else "",
                    cat_label(c.category), c.category_rule, cat_label(c.llm_category) if c.llm_category else "",
                    "" if c.category_agreement is None else ("Y" if c.category_agreement else "N"), c.user_intent, c.agent_gap,
                    c.pairing, c.pairing_score, ref_text(top, with_method=False) if top else "추적 불가", top.match_method if top else "",
                    top.score if top else None, "; ".join(ref_text(r, False) for r in c.code_change_refs[:2]),
                ])
    _sheet(wb, "변경사항", ["변경 ID", "에이전트", "비교쌍", "수정자", "형식", "변경 유형", "위치", "시트/슬라이드", "열(헤더)", "행(키)", "이전", "이후",
                         "서식만", "보완 유형", "분류 규칙", "LLM 분류", "규칙-LLM 일치", "사용자 의도", "에이전트 갭", "대응 방식", "대응 점수",
                         "코드 위치", "코드 매칭", "코드 점수", "코드 변경 연관"], rows,
           [18, 16, 14, 10, 6, 12, 40, 14, 14, 14, 40, 40, 6, 14, 20, 14, 8, 30, 30, 10, 8, 40, 12, 8, 30])

    rows = []
    for a in res.agents:
        for p in a.patterns:
            rows.append([p.id, a.name, p.title, cat_label(p.category), p.change_type, f"{len(p.support_pairs)}/{len(a.comparisons)}",
                         p.support_ratio, len(p.change_ids), ", ".join(p.change_ids[:20]),
                         " / ".join(f"{e.get('before') or ''} → {e.get('after') or ''}" for e in p.examples[:2]),
                         "; ".join(f"{r.file}:{r.line_start} {r.matched_text[:60]}" for r in p.user_code_evidence)])
    _sheet(wb, "보완패턴", ["패턴 ID", "에이전트", "패턴", "보완 유형", "변경 유형", "지원", "지원 비율", "건수", "근거 변경 ID", "예시", "사용자 코드 근거"], rows,
           [14, 16, 40, 14, 14, 8, 8, 6, 40, 50, 40])

    rows = []
    fills = []
    for a in res.agents:
        for r in a.recommendations + a.rejected_recommendations:
            v = r.verification
            patches = "\n\n".join(e.patch for e in r.proposed_edits if e.verified and e.patch)
            rows.append([r.id, a.name, STATUS_LABEL[v.status], PRIORITY_LABEL[r.priority], r.confidence, "규칙" if r.source == "rule" else "GPT",
                         r.title, r.problem, r.recommendation, r.expected_effect, ", ".join(r.evidence_change_ids[:30]),
                         "\n".join(ref_text(x) for x in r.code_refs), patches, "\n".join(v.checks), "\n".join(v.problems),
                         f"{v.llm_cross_check or ''} {v.llm_cross_check_reason or ''}".strip(),
                         ", ".join(f"{k}={x}" for k, x in r.confidence_breakdown.items())])
            fills.append(STATUS_FILL.get(v.status))
    ws = _sheet(wb, "개선권고", ["권고 ID", "에이전트", "검증 상태", "우선순위", "신뢰도", "출처", "제목", "문제", "권고", "기대 효과", "근거 변경 ID",
                             "코드 위치", "수정 패치", "검증 통과", "검증 문제", "GPT 교차검증", "신뢰도 구성"], rows,
                [14, 16, 10, 8, 8, 8, 36, 50, 50, 30, 30, 40, 60, 40, 40, 30, 40])
    for i, f in enumerate(fills, start=2):
        if f is not None:
            ws.cell(i, 3).fill = f

    rows = []
    for a in res.agents:
        for comp in a.comparisons:
            for c in comp.changes:
                for rank, r in enumerate(c.code_refs, start=1):
                    rows.append([c.id, rank, r.file, r.line_start, r.line_end, r.sub_location, r.symbol, r.kind, r.match_method, r.anchor, r.anchor_role,
                                 r.score, r.matched_text[:300], r.note, r.commit[:12]])
    _sheet(wb, "코드추적", ["변경 ID", "순위", "파일", "시작 줄", "끝 줄", "템플릿 위치", "함수", "종류", "매칭 방식", "앵커", "앵커 역할", "점수", "매칭 텍스트", "비고", "커밋"],
           rows, [18, 6, 36, 8, 8, 16, 24, 12, 12, 20, 12, 8, 50, 30, 14])

    rows = []
    for a in res.agents:
        for d in a.code_diffs:
            if d.error:
                rows.append([a.name, d.label, d.base_ref, d.head_ref, "", "", "", f"오류: {d.error}", ""])
            for s in d.supplements:
                rows.append([a.name, d.label, d.base_ref, d.head_ref, s.file, s.hunk_new_start, s.symbol, f"[{s.kind}] {s.summary}", "Y" if s.verified else "N"])
            if not d.supplements and not d.error:
                for f in d.files:
                    rows.append([a.name, d.label, d.base_ref, d.head_ref, f.path, "", "", f"{f.status} +{f.added_lines}/-{f.removed_lines}", "Y"])
    _sheet(wb, "코드보완", ["에이전트", "비교", "기준 ref", "대상 ref", "파일", "hunk 시작", "함수", "요약", "검증"], rows, [16, 30, 14, 20, 36, 8, 24, 60, 6])

    rows = []
    for a in res.agents:
        for comp in a.comparisons:
            rows.append([comp.pair_id, a.name, comp.integrity.status, comp.integrity.checks, "\n".join(comp.integrity.failures[:20]),
                         comp.baseline_path, comp.baseline_sha256[:16], comp.revised_path, comp.revised_sha256[:16], comp.seconds, "\n".join(comp.notes)])
        for f in a.failed_comparisons:
            rows.append([f.pair_id, a.name, "비교 실패", 0, f"{failure_kind_label(f.kind)}: {f.message}", f.baseline_path, "", f.revised_path, "", f.seconds, ""])
    ws = _sheet(wb, "무결성검증", ["비교쌍", "에이전트", "상태", "검사 수", "실패 내역", "원본 파일", "원본 SHA256", "수정본 파일", "수정본 SHA256", "소요(초)", "비고"], rows,
                [16, 16, 10, 8, 50, 40, 18, 40, 18, 8, 40])
    for i, r in enumerate(rows, start=2):
        if r[2] != "PASS":
            ws.cell(i, 3).fill = STATUS_FILL["REJECTED"]

    rows = [[CATEGORY_LABEL.get(f.category, f.category), f.stage, failure_kind_label(f.kind), f.target, f.message, f.impact] for f in res.failures]
    _sheet(wb, "실패내역", ["범주", "단계", "종류", "대상", "내용", "영향"], rows, [14, 22, 18, 24, 70, 40])

    rows = [[a.name, t.get("step"), t.get("tool"), t.get("arguments"), t.get("result_chars"), t.get("error")] for a in res.agents for t in a.tool_trace]
    _sheet(wb, "GPT도구조회", ["에이전트", "단계", "도구", "인자", "결과 크기", "오류"], rows, [16, 6, 18, 60, 10, 14])
    wb.save(path)
