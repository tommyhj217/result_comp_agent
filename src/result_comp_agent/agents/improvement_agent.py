"""ImprovementAdvisorAgent — 에이전트 코드 개선안 도출.

* 규칙 기반 권고(항상)는 orchestrator 가 analysis.recommender 로 생성
* GPT 에는 '패턴(증거) + 규칙 권고 + 실제 코드 스니펫(줄 번호 포함) + 사용자 코드 보완'을 제공하고
  구체적 코드 위치·수정안을 요청한다.
* 응답 검증 루프: 존재하지 않는 change_id, 코드에 없는 인용문이 많으면 그 목록을 알려주고 재작성 요청
"""
from __future__ import annotations

import json
from collections import defaultdict
from typing import Optional

from ..code.indexer import CodeIndex
from ..diff import normalize as N
from ..llm.client import LLMClient, LLMError
from ..llm.prompts import IMPROVEMENT_SYSTEM
from ..llm.schemas import IMPROVEMENT_SCHEMA, ImprovementM
from ..models import Change, CodeDiffReport, CodeRef, Pattern, ProposedEdit, Recommendation
from .grounding import locate_quote


def build_code_context(
    index: CodeIndex, recs: list[Recommendation], patterns: list[Pattern], changes: dict[str, Change], max_chars: int
) -> str:
    regions: dict[str, list[list[int]]] = defaultdict(list)

    def add(file: str, s: int, e: int, pad: int = 12) -> None:
        if file in index.files and s > 0:
            regions[file].append([max(1, s - pad), min(index.line_count(file), e + pad)])

    for r in recs:
        for ref in r.code_refs:
            add(ref.file, ref.line_start, ref.line_end)
    for p in patterns[:20]:
        for cid in p.change_ids[:3]:
            ch = changes.get(cid)
            if ch:
                for ref in ch.code_refs[:2]:
                    add(ref.file, ref.line_start, ref.line_end, pad=8)
    for lit in index.prompts():
        if lit.kind == "prompt":
            add(lit.file, lit.line_start, lit.line_end, pad=2)
        else:
            add(lit.file, 1, index.line_count(lit.file), pad=0)
    if not regions:
        for f in list(index.files)[:10]:
            add(f, 1, min(index.line_count(f), 200), pad=0)
    out, size = [], 0
    for file in sorted(regions):
        spans = sorted(regions[file])
        merged: list[list[int]] = []
        for s, e in spans:
            if merged and s <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])
        for s, e in merged:
            block = f"### FILE: {file} (줄 {s}-{e})\n" + "\n".join(f"{i:>5}| {index.files[file][i - 1]}" for i in range(s, e + 1))
            if size + len(block) > max_chars:
                out.append(f"### (컨텍스트 한도로 {file} {s}-{e} 생략)")
                continue
            out.append(block)
            size += len(block)
    return "\n\n".join(out)


TOOLS_HINT = """

[도구 사용]
- 제공된 스니펫만으로 부족하면 도구로 직접 조회하십시오: search_code(문구 위치 찾기), read_code(줄 번호 포함 원문),
  list_prompts, get_change/list_changes(변경 상세), read_document/search_documents(원본·수정본 문서), get_pattern, get_code_diff.
- 코드 인용(quote)과 수정안 original_snippet 은 read_code 로 확인한 원문을 그대로 복사하십시오.
- 조사가 끝나면 도구 호출 없이 답하십시오 (최종 JSON 은 다음 단계에서 요청합니다)."""


class ImprovementAdvisorAgent:
    def __init__(
        self, llm: Optional[LLMClient], max_patterns: int = 25, max_context_chars: int = 60000,
        use_tools: bool = True, registry=None, max_tool_steps: int = 6, max_tool_calls: int = 20,
    ):
        self.llm = llm
        self.max_patterns = max_patterns
        self.max_context_chars = max_context_chars
        self.use_tools = use_tools
        self.registry = registry
        self.max_tool_steps = max_tool_steps
        self.max_tool_calls = max_tool_calls
        self.last_trace: list[dict] = []

    def run(
        self,
        *,
        agent_label: str,
        agent_id: str,
        index: Optional[CodeIndex],
        patterns: list[Pattern],
        rule_recs: list[Recommendation],
        changes: dict[str, Change],
        n_pairs: int,
        code_diffs: list[CodeDiffReport],
        notes: list[str],
        tool_ctx=None,
    ) -> list[Recommendation]:
        self.last_trace = []
        if self.llm is None or not self.llm.active:
            return []
        if index is None:
            notes.append("코드 인덱스가 없어 LLM 코드 개선안을 생략함")
            return []
        pats = [p for p in patterns if p.category != "FORMAT_ONLY" or p.support_ratio >= 0.5][: self.max_patterns]
        if not pats:
            return []
        pat_json = [
            {
                "pattern_id": p.id,
                "title": p.title,
                "category": p.category,
                "support": f"{len(p.support_pairs)}/{n_pairs} 비교쌍, {len(p.change_ids)}건",
                "change_ids": p.change_ids[:8],
                "examples": p.examples[:2],
                "user_code_evidence": [f"{r.file}:{r.line_start} {r.matched_text[:80]}" for r in p.user_code_evidence[:2]],
            }
            for p in pats
        ]
        rule_json = [
            {
                "id": r.id,
                "title": r.title,
                "recommendation": r.recommendation,
                "code_refs": [f"{c.file}:{c.line_start}-{c.line_end} ({c.kind}/{c.match_method}) {c.note or ''}".strip() for c in r.code_refs],
            }
            for r in rule_recs[:30]
        ]
        code_ctx = build_code_context(index, rule_recs, pats, changes, self.max_context_chars)
        user_sup = []
        for cd in code_diffs:
            for s in cd.supplements[:15]:
                user_sup.append(f"[{cd.label}] {s.file}:{s.hunk_new_start} ({s.kind}) {s.summary}")
        user = (
            f"[대상 에이전트] {agent_label} (커밋 {index.commit[:12]})\n\n"
            f"[사용자 보완 패턴]\n{json.dumps(pat_json, ensure_ascii=False, indent=1)}\n\n"
            f"[규칙 기반 권고(참고)]\n{json.dumps(rule_json, ensure_ascii=False, indent=1)}\n\n"
            f"[사용자가 코드에 직접 보완한 사항]\n" + ("\n".join(user_sup) or "(없음)") + "\n\n"
            f"[코드 스니펫]\n{code_ctx}"
        )
        known_ids = set(changes)

        def feedback(m: ImprovementM) -> list[str]:
            problems = []
            for i, r in enumerate(m.recommendations):
                bad_ids = [x for x in r.evidence_change_ids if x not in known_ids]
                if bad_ids or not r.evidence_change_ids:
                    problems.append(f"권고#{i + 1} '{r.title[:30]}': 입력에 없는 evidence_change_ids {bad_ids[:5] or '(비어 있음)'}")
                for loc in r.code_locations:
                    st, _, _ = locate_quote(index, loc.file, loc.quote, loc.line_start, loc.line_end)
                    if st in ("no_file", "not_found", "empty"):
                        problems.append(f"권고#{i + 1}: {loc.file}:{loc.line_start} 인용문을 코드에서 찾을 수 없음({st})")
            return problems

        use_tools = self.use_tools and self.registry is not None and tool_ctx is not None and self.registry.names()
        try:
            if use_tools:
                res, trace, note = self.llm.structured_with_tools(
                    tier="main", system=IMPROVEMENT_SYSTEM + TOOLS_HINT, user=user, schema_name="agent_improvements",
                    schema=IMPROVEMENT_SCHEMA, validate=ImprovementM.model_validate, feedback=feedback, purpose=f"improvement:{agent_id}",
                    registry=self.registry, ctx=tool_ctx, max_steps=self.max_tool_steps, max_tool_calls=self.max_tool_calls,
                )
                self.last_trace = trace
                if note and note != "캐시":
                    notes.append(note)
            else:
                res = self.llm.structured(
                    tier="main", system=IMPROVEMENT_SYSTEM, user=user, schema_name="agent_improvements",
                    schema=IMPROVEMENT_SCHEMA, validate=ImprovementM.model_validate, feedback=feedback, purpose=f"improvement:{agent_id}",
                )
        except LLMError as e:
            notes.append(f"GPT 개선안 생성 실패({e.label}): {str(e)[:300]}")
            return []
        out = []
        for i, r in enumerate(res.recommendations, start=1):
            refs = [
                CodeRef(
                    agent_id=agent_id, commit=index.commit, file=loc.file, line_start=loc.line_start, line_end=max(loc.line_start, loc.line_end),
                    symbol=index.enclosing_symbol(loc.file, loc.line_start), kind="llm_location", match_method="llm_quote",
                    anchor="", anchor_role="llm", matched_text=loc.quote[:500], score=0.0,
                )
                for loc in r.code_locations
            ]
            out.append(
                Recommendation(
                    id=f"{agent_id}:L{i:03d}",
                    agent_id=agent_id,
                    title=N.shorten(r.title, 120),
                    problem=r.problem,
                    recommendation=r.recommendation,
                    category=r.category,
                    priority=r.priority,
                    source="llm",
                    evidence_change_ids=list(dict.fromkeys(r.evidence_change_ids)),
                    code_refs=refs,
                    proposed_edits=[ProposedEdit(file=e.file, original_snippet=e.original_snippet, replacement=e.replacement, explanation=e.explanation) for e in r.proposed_edits],
                    expected_effect=r.expected_effect,
                    based_on=[b for b in r.based_on if any(b == rr.id for rr in rule_recs)],
                )
            )
        return out
