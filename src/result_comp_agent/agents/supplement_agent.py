"""SupplementAnalystAgent — 사용자 보완 사항 해석.

1) 규칙 기반 분류(항상 수행, 결정론적)
2) GPT-6 로 각 변경의 '사용자 의도'와 '에이전트가 놓친 점'을 해석 (선택)
   - 응답의 change_id 가 입력 배치에 없으면 거부 후 재요청, 20% 이상 누락이어도 재요청
   - 재분류는 내용 계열 유형끼리, 신뢰도 임계값 이상일 때만 허용
     (서식/구조/메모 같은 '사실' 분류는 LLM 이 바꿀 수 없음)
"""
from __future__ import annotations

import json
from typing import Optional

from ..analysis.classifier import classify_all
from ..diff import normalize as N
from ..llm.client import LLMClient, LLMError
from ..llm.prompts import SUPPLEMENT_SYSTEM
from ..llm.schemas import SUPPLEMENT_SCHEMA, SupplementM
from ..models import Change

CONTENT_CATS = {"CONTENT_ADDITION", "CONTENT_REMOVAL", "FACT_CORRECTION", "CONTENT_ENRICHMENT", "CONTENT_REWRITE", "REWORDING"}


class SupplementAnalystAgent:
    def __init__(self, llm: Optional[LLMClient], batch_size: int = 40, max_changes: int = 300, accept_conf: float = 0.7):
        self.llm = llm
        self.batch_size = batch_size
        self.max_changes = max_changes
        self.accept_conf = accept_conf

    @staticmethod
    def _payload(c: Change) -> dict:
        d = {
            "change_id": c.id,
            "type": c.type_label,
            "location": c.location.path,
            "before": N.shorten(c.before, 400) or None,
            "after": N.shorten(c.after, 400) or None,
            "rule_category": c.category,
        }
        for k in ("numeric_delta", "numeric_delta_pct", "numbers_before", "numbers_after", "inserted", "deleted"):
            if k in c.detail:
                d[k] = c.detail[k]
        return d

    def run(self, agent_label: str, changes: list[Change]) -> dict:
        classify_all(changes)
        stats = {"llm": False, "targets": 0, "covered": 0, "agreement": None, "reclassified": 0, "truncated": 0, "errors": []}
        if self.llm is None or not self.llm.active:
            return stats
        stats["llm"] = True
        targets = [c for c in changes if not c.format_only and c.category != "FORMAT_ONLY"]
        stats["targets"] = len(targets)
        if len(targets) > self.max_changes:
            stats["truncated"] = len(targets) - self.max_changes
            targets = targets[: self.max_changes]
        by_id = {c.id: c for c in targets}
        agree = total = 0
        for k in range(0, len(targets), self.batch_size):
            batch = targets[k : k + self.batch_size]
            ids = {c.id for c in batch}
            user = f"[대상 에이전트] {agent_label}\n[변경 목록 JSON]\n" + json.dumps([self._payload(c) for c in batch], ensure_ascii=False, indent=1)

            def feedback(m: SupplementM, ids=ids) -> list[str]:
                got = [it.change_id for it in m.items]
                problems = [f"입력에 없는 change_id 사용: {x}" for x in got if x not in ids][:10]
                missing = ids - set(got)
                if missing:
                    problems.append(f"누락된 change_id {len(missing)}건: {sorted(missing)[:10]}")
                return problems

            try:
                res = self.llm.structured(
                    tier="main", system=SUPPLEMENT_SYSTEM, user=user, schema_name="supplement_analysis",
                    schema=SUPPLEMENT_SCHEMA, validate=SupplementM.model_validate, feedback=feedback, purpose=f"supplement:{agent_label}",
                )
            except LLMError as e:
                stats["errors"].append(str(e)[:300])
                continue
            seen = set()
            for it in res.items:
                if it.change_id in seen or it.change_id not in by_id or it.change_id not in ids:
                    continue  # 입력에 없는 ID 는 무시 (해당 변경은 규칙 분류 유지)
                seen.add(it.change_id)
                ch = by_id[it.change_id]
                ch.llm_category = it.category
                ch.user_intent = it.user_intent.strip()[:200]
                ch.agent_gap = it.agent_gap.strip()[:200]
                ch.category_agreement = it.category == ch.category
                total += 1
                agree += int(ch.category_agreement)
                if (
                    not ch.category_agreement
                    and ch.category in CONTENT_CATS
                    and it.category in CONTENT_CATS
                    and it.confidence >= self.accept_conf
                ):
                    ch.category_rule = f"{ch.category_rule}→LLM({ch.category})"
                    ch.category = it.category
                    stats["reclassified"] += 1
        stats["covered"] = total
        stats["agreement"] = round(agree / total, 4) if total else None
        return stats
