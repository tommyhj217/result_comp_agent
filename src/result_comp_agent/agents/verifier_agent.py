"""VerifierAgent — 모든 권고(규칙/LLM)의 근거를 결정론적으로 검증하고 신뢰도를 산출한다.

검증 항목
1. 근거 change_id 실재 여부 (없는 ID 제거, 하나도 없으면 REJECTED)
2. 코드 위치: 파일 존재 · 줄 범위 · 인용문이 해당 위치에 실제로 존재 (다른 줄에 있으면 줄 번호 보정)
3. 근거-코드 연관성: 인용 코드가 근거 변경의 추적 위치 근처이거나 근거 앵커 텍스트를 포함
4. 수정안: 원본 스니펫이 파일에 정확히 1회 존재 + (Python) 적용 후 구문 검사 → 통과 시 unified diff 패치 생성
5. (선택) GPT 교차 검증(AZURE_OPENAI_DEPLOYMENT_FAST 또는 같은 배포): 증거가 권고를 뒷받침하는지 판정 → not_supported 인 LLM 권고는 기각

신뢰도 = 0.4·증거강도 + 0.3·코드근거 + 0.1·수정안유효성 + 0.2·일관성  (구성요소를 그대로 보고)
"""
from __future__ import annotations

import json
from typing import Optional

from ..code.indexer import CodeIndex
from ..diff import normalize as N
from ..llm.client import LLMClient, LLMError
from ..llm.prompts import CROSSCHECK_SYSTEM
from ..llm.schemas import CROSSCHECK_SCHEMA, CrossCheckM
from ..models import Change, CodeRef, Pattern, Recommendation, VerificationResult
from .grounding import check_edit, locate_quote


class VerifierAgent:
    def __init__(self, llm: Optional[LLMClient] = None, cross_check: bool = True):
        self.llm = llm
        self.cross_check = cross_check
        self._agent_id: Optional[str] = None

    # ------------------------------------------------------------------ 코드 위치
    def _check_ref(self, ref: CodeRef, index: Optional[CodeIndex]) -> tuple[bool, str]:
        if index is None:
            return False, "코드 인덱스 없음"
        if not index.has_file(ref.file):
            return False, f"파일 없음: {ref.file}"
        if ref.file in index.binary_files:
            if ref.kind == "llm_location":
                st, _, _ = locate_quote(index, ref.file, ref.matched_text, 0, 0)
                return (st == "template"), f"템플릿 인용 {st}"
            return True, f"템플릿 {ref.sub_location or ''} 확인"
        n = index.line_count(ref.file)
        if ref.kind == "llm_location":
            st, s, e = locate_quote(index, ref.file, ref.matched_text, ref.line_start, ref.line_end)
            if st == "exact":
                ref.line_start, ref.line_end = s, e
                ref.score = 0.9
                ref.snippet = index.snippet(ref.file, s, e)
                ref.symbol = index.enclosing_symbol(ref.file, s)
                return True, f"{ref.file}:{s} 인용문 일치"
            if st == "relocated":
                note = f"{ref.file}: 인용문이 {ref.line_start}행이 아닌 {s}행에 있어 보정"
                ref.line_start, ref.line_end = s, e
                ref.score = 0.75
                ref.snippet = index.snippet(ref.file, s, e)
                ref.symbol = index.enclosing_symbol(ref.file, s)
                ref.note = (ref.note or "") + "줄 번호 보정됨"
                return True, note
            return False, f"{ref.file}:{ref.line_start} 인용문을 코드에서 찾을 수 없음({st})"
        if not (1 <= ref.line_start <= n and ref.line_start <= ref.line_end <= n):
            return False, f"{ref.file}: 줄 범위 {ref.line_start}-{ref.line_end} 가 파일 길이 {n} 을 벗어남"
        region = N.collapse_ws(" ".join(index.get_lines(ref.file, ref.line_start - 2, ref.line_end + 30)))
        if ref.kind == "insertion_point":
            return ('"' in region or "'" in region or ":" in region), f"{ref.file}:{ref.line_start} 삽입 위치 확인"
        probe = N.collapse_ws(ref.matched_text)[:30]
        if probe and probe in region:
            return True, f"{ref.file}:{ref.line_start} '{N.shorten(probe, 20)}' 확인"
        anchor = N.collapse_ws(ref.anchor)
        if anchor and anchor in region:
            return True, f"{ref.file}:{ref.line_start} 앵커 '{anchor}' 확인"
        return False, f"{ref.file}:{ref.line_start} 위치에서 매칭 텍스트를 확인할 수 없음"

    # ------------------------------------------------------------------ 단일 권고
    def _verify_one(self, r: Recommendation, index: Optional[CodeIndex], changes: dict[str, Change]) -> None:
        v = VerificationResult()
        valid = [i for i in r.evidence_change_ids if i in changes]
        invalid = [i for i in r.evidence_change_ids if i not in changes]
        if invalid:
            v.problems.append(f"존재하지 않는 근거 ID 제거: {invalid[:5]}")
        r.evidence_change_ids = valid
        if not valid:
            v.status = "REJECTED"
            v.problems.append("유효한 근거 변경이 없음")
            r.verification = v
            return
        v.checks.append(f"근거 변경 {len(valid)}건 실재 확인")

        good_refs = []
        for ref in r.code_refs:
            ok, msg = self._check_ref(ref, index)
            (v.checks if ok else v.problems).append(msg)
            if ok:
                good_refs.append(ref)
        r.code_refs = good_refs

        # 근거-코드 연관성
        related = False
        ev = [changes[i] for i in valid]
        ev_refs = [er for c in ev for er in c.code_refs]
        anchors = {a.text for c in ev for a in c.anchors if len(a.text) >= 2}
        for ref in good_refs:
            if any(er.file == ref.file and abs(er.line_start - ref.line_start) <= 15 for er in ev_refs):
                related = True
                break
            if index is not None and ref.line_start > 0:
                region = " ".join(index.get_lines(ref.file, ref.line_start - 3, ref.line_end + 3))
                if any(a in region for a in anchors):
                    related = True
                    break
            if ref.file in (index.binary_files if index else set()):
                related = True
        if good_refs:
            (v.checks if related else v.problems).append("근거 변경과 코드 위치의 연관성 " + ("확인" if related else "미확인"))

        for e in r.proposed_edits:
            if index is None:
                e.verified, e.verification_note = False, "코드 인덱스 없음"
                continue
            ok, note, patch = check_edit(index, e.file, e.original_snippet, e.replacement)
            e.verified, e.verification_note, e.patch = ok, note, patch
            (v.checks if ok else v.problems).append(f"수정안({e.file}): {note}")

        if not good_refs:
            v.status = "PARTIAL"
            v.problems.append("검증된 코드 위치 없음 (추적 불가 → 수동 확인 필요)")
        elif r.proposed_edits and not all(e.verified for e in r.proposed_edits):
            v.status = "PARTIAL"
        elif r.source == "llm" and not related:
            v.status = "PARTIAL"
        else:
            v.status = "VERIFIED"
        r.verification = v

    # ------------------------------------------------------------------ LLM 교차 검증
    def _cross_check(self, recs: list[Recommendation], changes: dict[str, Change], agent_label: str) -> Optional[str]:
        if self.llm is None or not self.llm.active or not self.cross_check or not recs:
            return None
        items = []
        for r in recs[:25]:
            ev = []
            for cid in r.evidence_change_ids[:6]:
                c = changes[cid]
                ev.append({"location": c.location.path, "type": c.type_label, "before": N.shorten(c.before, 160), "after": N.shorten(c.after, 200)})
            items.append({"rec_id": r.id, "title": r.title, "problem": r.problem, "recommendation": r.recommendation, "evidence": ev})
        ids = {r.id for r in recs[:25]}

        def feedback(m: CrossCheckM) -> list[str]:
            probs = [f"입력에 없는 rec_id: {x.rec_id}" for x in m.results if x.rec_id not in ids]
            missing = ids - {x.rec_id for x in m.results}
            if missing:
                probs.append(f"판정 누락 rec_id: {sorted(missing)[:10]}")
            return probs

        try:
            res = self.llm.structured(
                tier="fast", system=CROSSCHECK_SYSTEM,
                user=f"[대상 에이전트] {agent_label}\n" + json.dumps(items, ensure_ascii=False, indent=1),
                schema_name="cross_check", schema=CROSSCHECK_SCHEMA, validate=CrossCheckM.model_validate, feedback=feedback, purpose=f"crosscheck:{self._agent_id or agent_label}",
            )
        except LLMError as e:
            return f"교차 검증 실패: {str(e)[:200]}"
        by_id = {r.id: r for r in recs}
        for it in res.results:
            r = by_id.get(it.rec_id)
            if r is None or r.verification.llm_cross_check is not None:
                continue
            r.verification.llm_cross_check = it.verdict
            r.verification.llm_cross_check_reason = N.shorten(it.reason, 200)
            if r.source == "llm":
                if it.verdict == "not_supported":
                    r.verification.status = "REJECTED"
                    r.verification.problems.append(f"교차 검증: 증거가 뒷받침하지 않음 — {it.reason}")
                elif it.verdict == "partially_supported" and r.verification.status == "VERIFIED":
                    r.verification.status = "PARTIAL"
                    r.verification.problems.append(f"교차 검증: 부분적으로만 뒷받침 — {it.reason}")
        return None

    # ------------------------------------------------------------------ 신뢰도
    @staticmethod
    def _confidence(r: Recommendation, changes: dict[str, Change], n_pairs: int, patterns: dict[str, Pattern]) -> None:
        ev = [changes[i] for i in r.evidence_change_ids if i in changes]
        pairs = {c.pair_id for c in ev}
        support = len(pairs) / n_pairs if n_pairs else 0.0
        evidence = min(1.0, 0.7 * support + 0.3 * min(len(ev), 5) / 5)
        code = max((ref.score for ref in r.code_refs), default=0.0)
        if not r.proposed_edits:
            edits = 0.5
        else:
            edits = 1.0 if any(e.verified for e in r.proposed_edits) else 0.0
        cc = r.verification.llm_cross_check
        if r.source == "rule":
            consistency = 0.8 if cc in (None, "supported", "partially_supported") else 0.4
        else:
            consistency = {"supported": 1.0, "partially_supported": 0.6, "not_supported": 0.0}.get(cc or "", 0.5)
        if any(patterns.get(pid) and patterns[pid].user_code_evidence for pid in r.pattern_ids):
            consistency = min(1.0, consistency + 0.2)
        conf = 0.4 * evidence + 0.3 * code + 0.1 * edits + 0.2 * consistency
        if r.verification.status == "REJECTED":
            conf = min(conf, 0.2)
        r.confidence = round(conf, 3)
        r.confidence_breakdown = {
            "evidence(0.4)": round(evidence, 3),
            "code_grounding(0.3)": round(code, 3),
            "edit_validity(0.1)": round(edits, 3),
            "consistency(0.2)": round(consistency, 3),
            "support_ratio": round(support, 3),
        }

    def run(
        self,
        recs: list[Recommendation],
        *,
        index: Optional[CodeIndex],
        changes: dict[str, Change],
        n_pairs: int,
        patterns: list[Pattern],
        agent_label: str,
        notes: list[str],
        agent_id: Optional[str] = None,
    ) -> tuple[list[Recommendation], list[Recommendation]]:
        self._agent_id = agent_id
        for r in recs:
            self._verify_one(r, index, changes)
        live = [r for r in recs if r.verification.status != "REJECTED"]
        msg = self._cross_check(live, changes, agent_label)
        if msg:
            notes.append(msg)
        pmap = {p.id: p for p in patterns}
        for r in recs:
            self._confidence(r, changes, n_pairs, pmap)
        order = {"VERIFIED": 0, "PARTIAL": 1, "UNVERIFIED": 2, "REJECTED": 3}
        prio = {"high": 0, "medium": 1, "low": 2}
        kept = sorted(
            (r for r in recs if r.verification.status != "REJECTED"),
            key=lambda r: (order[r.verification.status], prio[r.priority], -r.confidence),
        )
        rejected = [r for r in recs if r.verification.status == "REJECTED"]
        return kept, rejected
