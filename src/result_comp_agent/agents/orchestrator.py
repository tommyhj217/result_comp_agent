"""Orchestrator — 하위 에이전트를 순서대로 실행해 AnalysisResult 를 만든다.

  DiffAgent(결정론)          문서 핸들러로 추출 → diff → 무결성 검증 (별도 프로세스·제한 시간)
  CodeAgent(결정론)          git 코드 로드 → 인덱스 → 변경-코드 추적 / 사용자 브랜치 diff / 버전 상관
  SupplementAnalystAgent     규칙 분류 + (GPT) 사용자 의도/에이전트 갭 해석
  Aggregator(결정론)         반복 패턴, 에이전트 간 공통 패턴
  ImprovementAdvisorAgent    규칙 권고 + (GPT + 도구 조회) 코드 개선안·수정 패치
  VerifierAgent              근거·코드·패치 검증 + (GPT 교차검증) + 신뢰도

실패 처리
  모든 실패는 StepFailure(범주·단계·종류·대상·메시지·영향)로 기록되어 보고서에 '실패'로 표시된다.
  범주: comparison(비교 실패/시간초과) · integrity(무결성 FAIL) · llm(GPT 시간초과/인증/배포없음…) · code(git/코드 로드)
        · report(보고서 작성) · config(플러그인 등)
  strict 모드: fail_on 범주의 실패가 있으면 실패 종료코드, fail_fast 면 첫 실패에서 중단(부분 보고서 작성).
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
from ..diff.compare import DEFAULT_TARGET, _resolve, compare_files_isolated
from ..documents import DocumentError
from ..documents.base import DOC_ERROR_LABELS
from ..llm.client import LLMClient
from ..llm.errors import KIND_LABELS
from ..models import AgentAnalysis, AnalysisResult, Change, ComparisonResult, FailedComparison, StepFailure
from ..plugins import load_plugins
from .code_agent import CodeAgent
from .code_supplement_agent import CodeSupplementAgent
from .improvement_agent import ImprovementAdvisorAgent
from .supplement_agent import SupplementAnalystAgent
from .verifier_agent import VerifierAgent

log = logging.getLogger(__name__)

# strict 종료코드 (여러 범주가 실패하면 SEVERITY 순서상 첫 범주의 코드)
EXIT_CODES = {"config": 7, "comparison": 3, "integrity": 2, "llm": 4, "code": 5, "report": 6}
SEVERITY = ["config", "comparison", "integrity", "llm", "code", "report"]
NONSTRICT_FAILED_EXIT = 1

LLM_STAGE_IMPACT = {
    "llm.supplement": "해당 변경은 규칙 기반 분류만 적용됨 (사용자 의도/에이전트 갭 해석 없음)",
    "llm.improvement": "GPT 코드 개선안 없이 규칙 기반 권고만 표시됨",
    "llm.improvement:tools": "GPT 도구 조회 없이 사전 수집 컨텍스트로 진행됨",
    "llm.crosscheck": "GPT 교차검증 없이 결정론적 검증만 적용됨",
    "llm.code_supplement": "사용자 코드 보완은 결정론적 요약만 표시됨",
    "llm.probe": "연결 점검 실패",
}


class StrictAbort(Exception):
    def __init__(self, failure: StepFailure):
        super().__init__(failure.message)
        self.failure = failure


class DiffAgent:
    def __init__(self, cfg: RunConfig, plugins: list[str], target: str = DEFAULT_TARGET):
        self.cfg = cfg
        self.plugins = plugins
        self.target = target

    def compare_one(self, agent: TargetAgent, p: PairSpec) -> ComparisonResult:
        kwargs = dict(
            baseline=self.cfg.resolve(p.baseline), revised=self.cfg.resolve(p.revised), pair_id=p.id or agent.id, agent_id=agent.id,
            revised_by=p.revised_by, baseline_ref=p.baseline_ref, revised_ref=p.revised_ref,
            compare_styles=self.cfg.diff.compare_styles, compare_geometry=self.cfg.diff.compare_geometry,
            header_rows=agent.header_rows, key_columns=agent.key_columns,
        )
        t = self.cfg.diff.timeout_seconds
        if t and t > 0 and self.cfg.diff.isolation == "process":
            return compare_files_isolated(t, self.target, self.plugins, **kwargs)
        return _resolve(self.target)(**kwargs)

    def run(self, agent: TargetAgent, pairs: list[PairSpec], on_failure: Callable[[StepFailure], None]) -> tuple[list[ComparisonResult], list[FailedComparison]]:
        comps: list[ComparisonResult] = []
        failed: list[FailedComparison] = []
        for p in pairs:
            try:
                comp = self.compare_one(agent, p)
            except DocumentError as e:
                kind, msg = e.kind, str(e)
            except Exception as e:  # noqa: BLE001
                kind, msg = "error", f"{type(e).__name__}: {e}"
            else:
                if agent.output_type not in ("auto", "") and comp.artifact != agent.output_type:
                    comp.notes.append(f"설정된 산출물 형식({agent.output_type})과 실제 형식({comp.artifact})이 다름")
                comps.append(comp)
                if comp.integrity.status != "PASS":
                    on_failure(StepFailure(
                        category="integrity", stage="diff.integrity", kind="integrity_fail", target=comp.pair_id,
                        message="; ".join(comp.integrity.failures[:3]), impact="이 비교쌍의 변경 목록은 완전성이 보장되지 않음 — 참고용",
                    ))
                continue
            failed.append(FailedComparison(
                pair_id=p.id or agent.id, agent_id=agent.id, baseline_path=p.baseline, revised_path=p.revised,
                revised_by=p.revised_by, kind=kind, message=msg,
            ))
            on_failure(StepFailure(
                category="comparison", stage="diff", kind=kind, target=p.id or agent.id,
                message=msg, impact="이 비교쌍은 분석에서 제외됨",
            ))
        return comps, failed


class Orchestrator:
    def __init__(self, cfg: RunConfig, llm: Optional[LLMClient] = None, progress: Optional[Callable[[str], None]] = None, diff_target: str = DEFAULT_TARGET):
        self.cfg = cfg
        self.llm = llm if (llm is not None and llm.active and cfg.llm.enabled is not False) else None
        self.progress = progress or (lambda m: log.info(m))
        self.run_failures: list[StepFailure] = []
        self.plugins, plugin_errors = load_plugins(cfg.plugins, cfg.base_dir)
        for e in plugin_errors:
            self.run_failures.append(StepFailure(category="config", stage="plugins", kind="plugin_error", target="plugins", message=e, impact="해당 플러그인 기능 미사용"))
        o = cfg.llm
        from ..tools import default_registry

        registry = default_registry()
        if o.tools is not None:
            registry = registry.subset(o.tools)
        self.diff_agent = DiffAgent(cfg, self.plugins, diff_target)
        self.code_agent = CodeAgent(cfg.resolve(cfg.cache_dir), CodeSupplementAgent(self.llm))
        self.supplement_agent = SupplementAnalystAgent(self.llm, o.supplement_batch_size, o.max_changes_per_agent, o.accept_reclassification_confidence)
        self.improvement_agent = ImprovementAdvisorAgent(
            self.llm, max_context_chars=o.max_context_chars, use_tools=o.use_tools, registry=registry,
            max_tool_steps=o.max_tool_steps, max_tool_calls=o.max_tool_calls,
        )
        self.verifier = VerifierAgent(self.llm, cross_check=o.cross_check)

    # ------------------------------------------------------------------ 실패 기록
    def _add(self, aa: Optional[AgentAnalysis], f: StepFailure) -> None:
        (aa.failures if aa is not None else self.run_failures).append(f)
        st = self.cfg.strict
        if st.enabled and st.fail_fast and f.category in st.fail_on:
            raise StrictAbort(f)

    def _collect_llm_failures(self, aa: AgentAnalysis, mark: int) -> int:
        if self.llm is None:
            return mark
        for ev in self.llm.failures[mark:]:
            purpose = ev.get("purpose") or ""
            head = purpose.split(":")[0] or "llm"
            stage = f"llm.{head}" + (":tools" if purpose.endswith(":tools") else "")
            self._add(aa, StepFailure(
                category="llm", stage=stage, kind=ev["kind"], target=aa.agent_id,
                message=f"{KIND_LABELS.get(ev['kind'], ev['kind'])}: {ev['message']}", impact=LLM_STAGE_IMPACT.get(stage, "GPT 결과 일부 누락"),
            ))
        return len(self.llm.failures)

    # ------------------------------------------------------------------ 에이전트 1개
    def run_agent(self, agent: TargetAgent, aa: AgentAnalysis) -> AgentAnalysis:
        label = f"{agent.name or agent.id} [{agent.stage}]"
        mark = len(self.llm.failures) if self.llm else 0
        pairs = self.cfg.pairs_for(agent.id)
        self.progress(f"[{agent.id}] 1/6 산출물 비교 ({len(pairs)}쌍)")
        comps, failed = self.diff_agent.run(agent, pairs, lambda f: self._add(aa, f))
        aa.comparisons, aa.failed_comparisons = comps, failed
        for c in comps:
            aa.llm_notes.extend(f"[{c.pair_id}] {n}" for n in c.notes)
        changes: dict[str, Change] = {c.id: c for comp in comps for c in comp.changes}

        self.progress(f"[{agent.id}] 2/6 에이전트 코드 로드·추적")
        repo, index, err, err_kind = self.code_agent.load(agent, self.cfg.resolve(agent.repo) if agent.repo else None)
        aa.code_error = err
        if err and err_kind:
            self._add(aa, StepFailure(category="code", stage="code.load", kind=err_kind, target=agent.id, message=err,
                                      impact="코드 추적·코드 개선안 없이 산출물 비교 결과만 표시됨"))
        if index is not None:
            aa.commit = index.commit
            aa.code_files_indexed = len(index.files) + len(index.binary_files)
            aa.trace_stats = self.code_agent.trace(index, comps)
            if index.errors:
                aa.llm_notes.extend(f"[인덱스] {e}" for e in index.errors[:10])
        if repo is not None:
            aa.code_diffs = self.code_agent.user_branches(repo, agent) + self.code_agent.correlate_versions(repo, agent, comps)
            for d in aa.code_diffs:
                if d.error:
                    self._add(aa, StepFailure(category="code", stage="code.branch", kind="git_error", target=f"{agent.id}:{d.label}",
                                              message=d.error, impact="해당 코드 비교 결과 없음"))
        mark = self._collect_llm_failures(aa, mark)

        self.progress(f"[{agent.id}] 3/6 사용자 보완 해석")
        sup_stats = self.supplement_agent.run(label, list(changes.values()), agent_id=agent.id)
        aa.trace_stats["supplement"] = sup_stats
        if sup_stats.get("truncated"):
            aa.llm_notes.append(f"GPT 해석 대상 {sup_stats['truncated']}건 초과분은 규칙 분류만 적용")
        mark = self._collect_llm_failures(aa, mark)

        self.progress(f"[{agent.id}] 4/6 반복 패턴 집계")
        aa.patterns = aggregate(agent.id, comps)
        if aa.code_diffs:
            aa.trace_stats["patterns_with_user_code"] = self.code_agent.attach_user_code_evidence(
                aa.patterns, [d for d in aa.code_diffs if "산출물 생성 코드" not in d.label]
            )

        self.progress(f"[{agent.id}] 5/6 개선 권고 도출")
        rule_recs = recommend(agent.id, aa.patterns, changes, len(comps))
        from ..tools.context import ToolContext

        tool_ctx = ToolContext(agent_id=agent.id, index=index, changes=changes, comparisons=comps, patterns=aa.patterns, code_diffs=aa.code_diffs)
        llm_recs = self.improvement_agent.run(
            agent_label=label, agent_id=agent.id, index=index, patterns=aa.patterns, rule_recs=rule_recs,
            changes=changes, n_pairs=len(comps), code_diffs=aa.code_diffs, notes=aa.llm_notes, tool_ctx=tool_ctx,
        )
        aa.tool_trace = list(self.improvement_agent.last_trace)
        mark = self._collect_llm_failures(aa, mark)

        self.progress(f"[{agent.id}] 6/6 근거 검증")
        kept, rejected = self.verifier.run(
            rule_recs + llm_recs, index=index, changes=changes, n_pairs=len(comps), patterns=aa.patterns,
            agent_label=label, notes=aa.llm_notes, agent_id=agent.id,
        )
        aa.recommendations, aa.rejected_recommendations = kept, rejected
        self._collect_llm_failures(aa, mark)
        return aa

    # ------------------------------------------------------------------ 전체
    def run(self) -> AnalysisResult:
        result = AnalysisResult(
            project=self.cfg.project,
            generated_at=dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            tool_version=__version__,
            plugins=self.plugins,
        )
        aborted: Optional[StepFailure] = None
        try:
            for f in list(self.run_failures):
                st = self.cfg.strict
                if st.enabled and st.fail_fast and f.category in st.fail_on:
                    raise StrictAbort(f)
            for agent in sorted(self.cfg.agents, key=lambda a: (a.stage, a.id)):
                aa = AgentAnalysis(agent_id=agent.id, name=agent.name or agent.id, stage=agent.stage, output_type=agent.output_type, repo=agent.repo, ref=agent.ref)
                result.agents.append(aa)
                self.run_agent(agent, aa)
        except StrictAbort as e:
            aborted = e.failure
            self.progress(f"strict fail_fast: '{e.failure.stage}' 실패로 분석을 중단합니다 — {e.failure.message[:120]}")
        result.cross_agent_patterns = cross_agent_patterns({a.agent_id: a.patterns for a in result.agents}, {a.agent_id: a.stage for a in result.agents})
        result.llm = self._llm_info()
        result.failures = list(self.run_failures) + [f for a in result.agents for f in a.failures]
        result.reliability = self._reliability(result)
        finalize_status(result, self.cfg, aborted)
        return result

    def _llm_info(self) -> dict:
        if self.llm is None:
            return {"active": False, "mode": "오프라인(규칙 기반) — LLM 미사용"}
        return self.llm.info()

    @staticmethod
    def _reliability(result: AnalysisResult) -> dict:
        comps = [c for a in result.agents for c in a.comparisons]
        failed_comps = [f for a in result.agents for f in a.failed_comparisons]
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
            "comparisons": {"configured": len(comps) + len(failed_comps), "succeeded": len(comps), "failed": len(failed_comps),
                            "failed_detail": [f"{f.pair_id}: {DOC_ERROR_LABELS.get(f.kind, f.kind)}" for f in failed_comps]},
            "changes": {
                "total": sum(len(c.changes) for c in comps),
                "content": sum(c.stats.get("content", 0) for c in comps),
                "format_only": sum(c.stats.get("format_only", 0) for c in comps),
                "low_confidence_pairing": low_pair,
            },
            "trace": {"content_changes": content, "traced": traced, "ratio": round(traced / content, 4) if content else None},
            "classification": {"llm_covered": covered, "rule_llm_agreement": round(agree_n / covered, 4) if covered else None},
            "recommendations": {k: status.get(k, 0) for k in ("VERIFIED", "PARTIAL", "UNVERIFIED", "REJECTED")},
            "failures_by_category": dict(Counter(f.category for f in result.failures)),
            "tool_calls": sum(len(a.tool_trace) for a in result.agents),
        }


def finalize_status(result: AnalysisResult, cfg: RunConfig, aborted: Optional[StepFailure] = None) -> None:
    """status / strict 판정 / 종료코드 계산 (보고서 작성 실패를 반영하려면 작성 후 다시 호출)."""
    if aborted is None and (result.strict or {}).get("aborted"):
        ab = result.strict["aborted"]
        aborted = next((f for f in result.failures if f.stage == ab.get("stage") and f.target == ab.get("target")), None) or StepFailure(
            category="comparison", stage=ab.get("stage") or "", kind="aborted", target=ab.get("target") or "", message=ab.get("message") or "")
    cats = {f.category for f in result.failures}
    rel = result.reliability.get("comparisons", {})
    nothing = rel.get("configured", 0) > 0 and rel.get("succeeded", 0) == 0
    if aborted is not None or nothing:
        result.status = "FAILED"
    elif result.failures:
        result.status = "PARTIAL"
    else:
        result.status = "SUCCESS"
    st = cfg.strict
    violated = [c for c in SEVERITY if c in cats and c in st.fail_on] if st.enabled else []
    if st.enabled and violated:
        code = EXIT_CODES[violated[0]]
    elif result.status == "FAILED":
        code = NONSTRICT_FAILED_EXIT if not st.enabled else EXIT_CODES.get(aborted.category if aborted else "comparison", 3)
    else:
        code = 0
    result.exit_code = code
    result.strict = {
        "enabled": st.enabled, "fail_on": list(st.fail_on), "fail_fast": st.fail_fast, "violated": violated,
        "aborted": {"stage": aborted.stage, "target": aborted.target, "message": aborted.message} if aborted else None,
        "exit_code": code,
    }
    result.reliability["failures_by_category"] = dict(Counter(f.category for f in result.failures))
