"""Orchestrator — 하위 에이전트를 순서대로 실행해 AnalysisResult 를 만든다.

  DiffAgent(결정론)          산출물 추출 → diff → 무결성 검증
  CodeAgent(결정론)          git 코드 로드 → 인덱스 → 변경-코드 추적 / 사용자 브랜치 diff / 버전 상관
  SupplementAnalystAgent     규칙 분류 + (GPT-6) 사용자 의도/에이전트 갭 해석
  Aggregator(결정론)         반복 보완 패턴, 에이전트 간 공통 패턴
  ImprovementAdvisorAgent    규칙 권고 + (GPT-6) 코드 개선안·수정 패치
  VerifierAgent              근거·코드·패치 검증 + (GPT-6-mini) 교차검증 + 신뢰도
  ReportAgent                Markdown / Excel / JSON / HTML 보고서
"""
from __future__ import annotations

import datetime as dt
import logging
from collections import Counter
from typing import Callable, Optional

from .. import __version__
from ..analysis.aggregator import aggregate, cross_agent_patterns
from ..analysis.recommender import recommend
from ..config import PairSpec, RunConfig, TargetAgent
from ..diff.compare import compare_files
from ..llm.client import LLMClient
from ..models import AgentAnalysis, AnalysisResult, Change, ComparisonResult
from .code_agent import CodeAgent
from .code_supplement_agent import CodeSupplementAgent
from .improvement_agent import ImprovementAdvisorAgent
from .supplement_agent import SupplementAnalystAgent
from .verifier_agent import VerifierAgent

log = logging.getLogger(__name__)


class DiffAgent:
    def __init__(self, cfg: RunConfig):
        self.cfg = cfg

    def run(self, agent: TargetAgent, pairs: list[PairSpec]) -> tuple[list[ComparisonResult], list[str]]:
        out, errors = [], []
        for p in pairs:
            try:
                comp = compare_files(
                    self.cfg.resolve(p.baseline),
                    self.cfg.resolve(p.revised),
                    pair_id=p.id or agent.id,
                    agent_id=agent.id,
                    revised_by=p.revised_by,
                    baseline_ref=p.baseline_ref,
                    revised_ref=p.revised_ref,
                    compare_styles=self.cfg.diff.compare_styles,
                    compare_geometry=self.cfg.diff.compare_geometry,
                    header_rows=agent.header_rows,
                    key_columns=agent.key_columns,
                )
                if agent.output_type != "auto" and comp.artifact.value != agent.output_type:
                    errors.append(f"{p.id}: 설정된 산출물 형식({agent.output_type})과 실제({comp.artifact.value})가 다름")
                out.append(comp)
            except Exception as e:
                errors.append(f"{p.id}: 비교 실패 — {e}")
        return out, errors


class Orchestrator:
    def __init__(self, cfg: RunConfig, llm: Optional[LLMClient] = None, progress: Optional[Callable[[str], None]] = None):
        self.cfg = cfg
        self.llm = llm if (llm is not None and llm.active and cfg.llm.enabled is not False) else None
        self.progress = progress or (lambda m: log.info(m))
        o = cfg.llm
        self.diff_agent = DiffAgent(cfg)
        self.code_agent = CodeAgent(cfg.resolve(cfg.cache_dir), CodeSupplementAgent(self.llm))
        self.supplement_agent = SupplementAnalystAgent(self.llm, o.supplement_batch_size, o.max_changes_per_agent, o.accept_reclassification_confidence)
        self.improvement_agent = ImprovementAdvisorAgent(self.llm, max_context_chars=o.max_context_chars)
        self.verifier = VerifierAgent(self.llm, cross_check=o.cross_check)

    def run_agent(self, agent: TargetAgent) -> AgentAnalysis:
        label = f"{agent.name or agent.id} [{agent.stage}]"
        aa = AgentAnalysis(agent_id=agent.id, name=agent.name or agent.id, stage=agent.stage, output_type=agent.output_type, repo=agent.repo, ref=agent.ref)
        pairs = self.cfg.pairs_for(agent.id)
        self.progress(f"[{agent.id}] 1/6 산출물 비교 ({len(pairs)}쌍)")
        comps, errors = self.diff_agent.run(agent, pairs)
        aa.comparisons = comps
        aa.llm_notes.extend(errors)
        changes: dict[str, Change] = {c.id: c for comp in comps for c in comp.changes}

        self.progress(f"[{agent.id}] 2/6 에이전트 코드 로드·추적")
        repo, index, err = self.code_agent.load(agent, self.cfg.resolve(agent.repo) if agent.repo else None)
        aa.code_error = err
        if index is not None:
            aa.commit = index.commit
            aa.code_files_indexed = len(index.files) + len(index.binary_files)
            aa.trace_stats = self.code_agent.trace(index, comps)
            if index.errors:
                aa.llm_notes.extend(f"[인덱스] {e}" for e in index.errors[:10])
        if repo is not None:
            aa.code_diffs = self.code_agent.user_branches(repo, agent) + self.code_agent.correlate_versions(repo, agent, comps)

        self.progress(f"[{agent.id}] 3/6 사용자 보완 해석")
        sup_stats = self.supplement_agent.run(label, list(changes.values()))
        aa.trace_stats["supplement"] = sup_stats
        for e in sup_stats.get("errors", []):
            aa.llm_notes.append(f"보완 해석 LLM 오류: {e}")
        if sup_stats.get("truncated"):
            aa.llm_notes.append(f"LLM 해석 대상 {sup_stats['truncated']}건 초과분은 규칙 분류만 적용")

        self.progress(f"[{agent.id}] 4/6 반복 패턴 집계")
        aa.patterns = aggregate(agent.id, comps)
        if aa.code_diffs:
            aa.trace_stats["patterns_with_user_code"] = self.code_agent.attach_user_code_evidence(
                aa.patterns, [d for d in aa.code_diffs if "산출물 생성 코드" not in d.label]
            )

        self.progress(f"[{agent.id}] 5/6 개선 권고 도출")
        rule_recs = recommend(agent.id, aa.patterns, changes, len(comps))
        llm_recs = self.improvement_agent.run(
            agent_label=label, agent_id=agent.id, index=index, patterns=aa.patterns, rule_recs=rule_recs,
            changes=changes, n_pairs=len(comps), code_diffs=aa.code_diffs, notes=aa.llm_notes,
        )

        self.progress(f"[{agent.id}] 6/6 근거 검증")
        kept, rejected = self.verifier.run(
            rule_recs + llm_recs, index=index, changes=changes, n_pairs=len(comps), patterns=aa.patterns, agent_label=label, notes=aa.llm_notes,
        )
        aa.recommendations, aa.rejected_recommendations = kept, rejected
        return aa

    def run(self) -> AnalysisResult:
        result = AnalysisResult(
            project=self.cfg.project,
            generated_at=dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            tool_version=__version__,
        )
        for agent in sorted(self.cfg.agents, key=lambda a: (a.stage, a.id)):
            result.agents.append(self.run_agent(agent))
        result.cross_agent_patterns = cross_agent_patterns({a.agent_id: a.patterns for a in result.agents}, {a.agent_id: a.stage for a in result.agents})
        result.llm = self._llm_info()
        result.reliability = self._reliability(result)
        return result

    def _llm_info(self) -> dict:
        if self.llm is None:
            return {"active": False, "mode": "오프라인(규칙 기반) — LLM 미사용"}
        u = self.llm.usage
        info = dict(self.llm.settings.public())
        info.update(
            {"active": True, "calls": u.calls, "cache_hits": u.cache_hits, "retries": u.retries, "failures": u.failures,
             "prompt_tokens": u.prompt_tokens, "completion_tokens": u.completion_tokens}
        )
        return info

    @staticmethod
    def _reliability(result: AnalysisResult) -> dict:
        comps = [c for a in result.agents for c in a.comparisons]
        fails = [f"{c.pair_id}: {'; '.join(c.integrity.failures[:3])}" for c in comps if c.integrity.status != "PASS"]
        content = sum(a.trace_stats.get("content_changes", 0) for a in result.agents)
        traced = sum(a.trace_stats.get("traced", 0) for a in result.agents)
        status = Counter(r.verification.status for a in result.agents for r in a.recommendations + a.rejected_recommendations)
        sup = [a.trace_stats.get("supplement", {}) for a in result.agents]
        covered = sum(s.get("covered", 0) for s in sup)
        agree_n = sum((s.get("agreement") or 0) * s.get("covered", 0) for s in sup)
        low_pair = sum(c.stats.get("low_confidence_pairing", 0) for c in comps)
        return {
            "detection": "결정론적 diff 엔진 (LLM 미사용) — 원본+변경목록=수정본 재구성으로 무결성 검증",
            "integrity": {"pairs": len(comps), "pass": len(comps) - len(fails), "fail": fails},
            "changes": {
                "total": sum(len(c.changes) for c in comps),
                "content": sum(c.stats.get("content", 0) for c in comps),
                "format_only": sum(c.stats.get("format_only", 0) for c in comps),
                "low_confidence_pairing": low_pair,
            },
            "trace": {"content_changes": content, "traced": traced, "ratio": round(traced / content, 4) if content else None},
            "classification": {"llm_covered": covered, "rule_llm_agreement": round(agree_n / covered, 4) if covered else None},
            "recommendations": {k: status.get(k, 0) for k in ("VERIFIED", "PARTIAL", "UNVERIFIED", "REJECTED")},
        }
