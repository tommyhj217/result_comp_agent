"""변경(Change) → 에이전트 코드 위치(CodeRef) 추적.

각 변경이 가진 앵커(시트명/컬럼명/슬라이드 제목/값)를 코드 인덱스에서 찾는다.
추가된 구조(열/슬라이드/시트)는 '기존 형제 항목들이 정의된 컨테이너'를 찾아 삽입 위치를 추론한다.
모든 결과에 매칭 방식과 점수가 붙으며, 임계값 미만은 버린다(추적 불가로 정직하게 보고).
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Optional

from ..models import Anchor, Change, ChangeType, CodeRef
from .indexer import CodeIndex, Match


@dataclass(frozen=True)
class RoleCfg:
    substring: bool
    fuzzy: bool
    weight: float


ROLE_CFG: dict[str, RoleCfg] = {
    "column": RoleCfg(True, True, 1.0),
    "sheet": RoleCfg(False, True, 0.9),
    "section": RoleCfg(True, True, 0.95),
    "new_column": RoleCfg(True, True, 0.9),
    "new_section": RoleCfg(True, True, 0.85),
    "value": RoleCfg(False, False, 1.0),
    "new_value": RoleCfg(False, False, 0.8),
}

ROLE_LABEL = {
    "column": "컬럼명",
    "sheet": "시트명",
    "section": "슬라이드/섹션 제목",
    "new_column": "신규 컬럼명",
    "new_section": "신규 섹션 제목",
    "value": "원본 값(하드코딩 여부)",
    "new_value": "수정 값",
    "insertion": "삽입 위치 추론",
    "prompt_context": "같은 모듈 프롬프트",
}

MIN_SCORE = 0.45
TRACED_SCORE = 0.6

# 같은 점수일 때 우선순위: 하드코딩 값 > 삽입 위치 > 컬럼/섹션 정의 > 시트 > 신규 값
ROLE_PRIORITY = {"value": 0, "insertion": 1, "column": 2, "new_column": 3, "section": 3, "new_section": 4, "sheet": 5, "new_value": 6, "prompt_context": 7}
KIND_GROUP = {"literal": "code", "fstring": "code", "prompt": "prompt", "docstring": "prompt", "text_line": "text", "template": "template"}
STRUCTURAL_TYPES = {
    ChangeType.COLUMN_ADDED, ChangeType.COLUMN_REMOVED, ChangeType.COLUMN_MOVED, ChangeType.SHEET_ADDED, ChangeType.SHEET_REMOVED,
    ChangeType.SHEET_RENAMED, ChangeType.SLIDE_MOVED, ChangeType.ROW_MOVED, ChangeType.LAYOUT_CHANGED, ChangeType.STYLE_MODIFIED,
    ChangeType.SHAPE_GEOMETRY_CHANGED,
}


class CodeTracer:
    def __init__(self, index: CodeIndex, max_refs: int = 5, min_score: float = MIN_SCORE):
        self.index = index
        self.max_refs = max_refs
        self.min_score = min_score

    def _ref(self, m: Match, anchor: Anchor, score: float) -> CodeRef:
        lit = m.lit
        line = m.line if m.line else lit.line_start
        end = line if m.method == "substring" else max(line, lit.line_end if lit.line_end - lit.line_start < 8 else line)
        snippet = self.index.snippet(lit.file, line, end) if lit.line_start else f"[템플릿] {lit.sub_location}: {lit.value[:200]}"
        return CodeRef(
            agent_id=self.index.agent_id,
            commit=self.index.commit,
            file=lit.file,
            line_start=line,
            line_end=end,
            symbol=lit.symbol,
            kind=lit.kind,
            match_method=m.method,
            anchor=anchor.text,
            anchor_role=anchor.role,
            matched_text=lit.value[:300],
            score=round(score, 4),
            snippet=snippet,
            sub_location=lit.sub_location,
        )

    def insertion_point(self, siblings: list[str], left: Optional[str], right: Optional[str], new_text: str) -> Optional[CodeRef]:
        idx = self.index
        cont = idx.find_schema_container(siblings) if siblings else None
        if cont and cont["coverage"] >= 0.5:
            lits = cont["literals"]
            L = lits.get(left) if left else None
            R = lits.get(right) if right else None
            if L and R:
                a, b = sorted((L.line_end, R.line_start))
                note = f"'{left}' 와 '{right}' 사이에 '{new_text}' 추가"
            elif L:
                a = b = L.line_end
                note = f"'{left}' 다음에 '{new_text}' 추가"
            elif R:
                a = b = R.line_start
                note = f"'{right}' 앞에 '{new_text}' 추가"
            else:
                a, b = cont["line_start"], cont["line_end"]
                note = f"목록에 '{new_text}' 추가"
            any_lit = next(iter(lits.values()))
            score = 0.55 + 0.4 * cont["coverage"]
            return CodeRef(
                agent_id=idx.agent_id,
                commit=idx.commit,
                file=cont["file"],
                line_start=a,
                line_end=b,
                symbol=any_lit.symbol,
                kind="insertion_point",
                match_method="container",
                anchor=new_text,
                anchor_role="insertion",
                matched_text=f"기존 항목 {len(cont['matched'])}/{len(siblings)}개가 정의된 위치: " + ", ".join(cont["matched"][:8]),
                score=round(score, 4),
                snippet=idx.snippet(cont["file"], cont["line_start"], cont["line_end"]) if any_lit.line_start else f"[템플릿] {any_lit.sub_location}",
                sub_location=any_lit.sub_location,
                note=note,
            )
        # 컨테이너를 못 찾으면 좌/우 이웃만으로 추론
        Lm = idx.find(left, substring=False, fuzzy=False, limit=10) if left else []
        Rm = idx.find(right, substring=False, fuzzy=False, limit=10) if right else []
        best = None
        for lm in Lm:
            for rm in Rm:
                same_cont = lm.lit.container and lm.lit.container == rm.lit.container
                near = lm.lit.file == rm.lit.file and abs(lm.line - rm.line) <= 6
                if same_cont or near:
                    sc = 0.85 if same_cont else 0.75
                    if best is None or sc > best[0]:
                        best = (sc, lm, rm)
        if best:
            sc, lm, rm = best
            a, b = sorted((lm.line, rm.line))
            return CodeRef(
                agent_id=idx.agent_id, commit=idx.commit, file=lm.lit.file, line_start=a, line_end=b, symbol=lm.lit.symbol,
                kind="insertion_point", match_method="neighbor", anchor=new_text, anchor_role="insertion",
                matched_text=f"{left} / {right}", score=sc, snippet=idx.snippet(lm.lit.file, a, b), sub_location=lm.lit.sub_location,
                note=f"'{left}' 와 '{right}' 사이에 '{new_text}' 추가",
            )
        single = (Lm[:1] and ("다음", left, Lm[0])) or (Rm[:1] and ("앞", right, Rm[0])) or None
        if single:
            where, name, m = single
            return CodeRef(
                agent_id=idx.agent_id, commit=idx.commit, file=m.lit.file, line_start=m.line, line_end=m.line, symbol=m.lit.symbol,
                kind="insertion_point", match_method="neighbor", anchor=new_text, anchor_role="insertion",
                matched_text=m.lit.value[:200], score=0.55, snippet=idx.snippet(m.lit.file, m.line, m.line), sub_location=m.lit.sub_location,
                note=f"'{name}' {where}에 '{new_text}' 추가 (한쪽 이웃만 확인됨)",
            )
        return None

    def _same_module_prompts(self, refs: list[CodeRef]) -> list[CodeRef]:
        """가장 강한 코드 위치와 같은 파일에 있는 프롬프트(생성 지침) — 내용 품질 개선의 수정 지점 후보."""
        if not refs:
            return []
        top = refs[0]
        out = []
        for lit in self.index.literals:
            if lit.kind == "prompt" and lit.file == top.file and not any(r.file == lit.file and r.line_start >= lit.line_start and r.line_start <= lit.line_end for r in refs):
                out.append(
                    CodeRef(
                        agent_id=self.index.agent_id, commit=self.index.commit, file=lit.file, line_start=lit.line_start,
                        line_end=lit.line_end, symbol=lit.symbol, kind="prompt", match_method="same_module",
                        anchor=top.anchor, anchor_role="prompt_context", matched_text=lit.value[:300], score=0.55,
                        snippet=self.index.snippet(lit.file, lit.line_start, min(lit.line_end, lit.line_start + 12), context=0),
                        note="같은 모듈의 생성 프롬프트 (내용 품질 개선 시 수정 지점 후보)",
                    )
                )
        return out[:1]

    def trace(self, change: Change) -> list[CodeRef]:
        refs: list[CodeRef] = []
        for a in change.anchors:
            cfg = ROLE_CFG.get(a.role)
            if cfg is None:
                continue
            # 앵커마다 '종류별 최고 매칭'만 채택 → 정의 리터럴·프롬프트 언급·템플릿이 고르게 남도록
            best_by_kind: dict[str, Match] = {}
            for m in self.index.find(a.text, substring=cfg.substring, fuzzy=cfg.fuzzy, limit=12):
                group = KIND_GROUP.get(m.lit.kind, m.lit.kind)
                if group not in best_by_kind or best_by_kind[group].score < m.score:
                    best_by_kind[group] = m
            for m in best_by_kind.values():
                refs.append(self._ref(m, a, m.score * cfg.weight))
        ct = change.change_type
        if ct in (ChangeType.COLUMN_ADDED, ChangeType.SLIDE_ADDED, ChangeType.SHEET_ADDED):
            d = change.detail
            siblings = d.get("sibling_headers") or d.get("sibling_titles") or d.get("sibling_sheets") or []
            left = next((a.text for a in change.anchors if a.role == "neighbor_left"), None)
            right = next((a.text for a in change.anchors if a.role == "neighbor_right"), None)
            new_text = change.after or ""
            if ct == ChangeType.SLIDE_ADDED:
                new_text = (d.get("content") or {}).get("title") or new_text
            ip = self.insertion_point(siblings, left, right, (new_text or "")[:60])
            if ip:
                refs.append(ip)
        best: dict[tuple, CodeRef] = {}
        for r in refs:
            k = (r.file, r.line_start, r.sub_location)
            if k not in best or (best[k].score, -ROLE_PRIORITY.get(best[k].anchor_role, 9)) < (r.score, -ROLE_PRIORITY.get(r.anchor_role, 9)):
                best[k] = r
        out = sorted(
            (r for r in best.values() if r.score >= self.min_score),
            key=lambda r: (-round(r.score, 2), ROLE_PRIORITY.get(r.anchor_role, 9), r.file, r.line_start),
        )[: self.max_refs]
        if ct not in STRUCTURAL_TYPES and not change.format_only:
            out += self._same_module_prompts(out)
        return out

    def trace_all(self, changes: list[Change]) -> dict:
        n = traced = 0
        methods: Counter = Counter()
        for ch in changes:
            ch.code_refs = self.trace(ch)
            if ch.format_only:
                continue
            n += 1
            if any(r.score >= TRACED_SCORE for r in ch.code_refs):
                traced += 1
                methods[ch.code_refs[0].match_method] += 1
        return {"content_changes": n, "traced": traced, "ratio": round(traced / n, 4) if n else None, "top_methods": dict(methods)}
