"""CodeAgent — git 에서 에이전트 코드를 가져와 인덱싱하고, 산출물 변경을 코드에 추적한다.

* 기준 ref 코드 인덱싱 → 변경별 코드 위치 추적(CodeTracer)
* 사용자 보완 브랜치(code_compare) diff → 사용자가 코드에 직접 보완한 사항
* 비교쌍의 baseline_ref/revised_ref 가 다르면 → 산출물 변경 중 '코드 변경으로 설명되는 것' 연결
* 보완 패턴의 앵커가 사용자 브랜치의 추가 코드에 있으면 → 패턴에 '사용자 코드 근거' 첨부
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..code.code_diff import build_code_diff, correlate_changes, find_in_code_diff
from ..code.git_source import GitError, GitRepo
from ..code.indexer import CodeIndex
from ..code.tracer import CodeTracer
from ..config import TargetAgent
from ..models import ComparisonResult, CodeDiffReport, Pattern
from .code_supplement_agent import CodeSupplementAgent


class CodeAgent:
    def __init__(self, cache_dir: str | Path, supplement_agent: Optional[CodeSupplementAgent] = None):
        self.cache_dir = Path(cache_dir) / "repos"
        self.supplement_agent = supplement_agent

    def load(self, agent: TargetAgent, repo_src: Optional[str]) -> tuple[Optional[GitRepo], Optional[CodeIndex], Optional[str], Optional[str]]:
        """반환: (repo, index, 메시지, 실패종류). repo 미설정은 실패가 아니므로 종류 None."""
        if not repo_src:
            return None, None, "repo 미설정 — 코드 추적 생략", None
        try:
            repo = GitRepo(repo_src, self.cache_dir)
        except Exception as e:
            return None, None, f"저장소 초기화 실패: {e}", "git_error"
        try:
            commit = repo.resolve(agent.ref)
        except GitError as e:
            msg = str(e)
            kind = "ref_not_found" if "ref 를 찾을 수 없음" in msg else ("git_clone" if "clone" in msg else "git_error")
            return None, None, f"코드 로드 실패: {msg}", kind
        except Exception as e:
            return None, None, f"코드 로드 실패: {e}", "git_error"
        try:
            index = CodeIndex.build(repo, commit, agent.paths, agent.id)
        except Exception as e:
            return repo, None, f"코드 인덱싱 실패: {e}", "index_error"
        if not index.files and not index.binary_files:
            return repo, index, f"지정 경로 {agent.paths} 에서 인덱싱할 코드 파일을 찾지 못함", "no_files"
        return repo, index, None, None

    @staticmethod
    def trace(index: CodeIndex, comps: list[ComparisonResult]) -> dict:
        tracer = CodeTracer(index)
        total = {"content_changes": 0, "traced": 0, "top_methods": {}}
        for comp in comps:
            st = tracer.trace_all(comp.changes)
            comp.stats["trace"] = st
            total["content_changes"] += st["content_changes"]
            total["traced"] += st["traced"]
            for k, v in st["top_methods"].items():
                total["top_methods"][k] = total["top_methods"].get(k, 0) + v
        n = total["content_changes"]
        total["ratio"] = round(total["traced"] / n, 4) if n else None
        return total

    def user_branches(self, repo: GitRepo, agent: TargetAgent) -> list[CodeDiffReport]:
        out = []
        for spec in agent.code_compare:
            label = spec.label or f"{spec.base}…{spec.head}"
            rep = build_code_diff(repo, agent.id, label, spec.base, spec.head, agent.paths)
            if self.supplement_agent is not None and not rep.error:
                rep.supplements = self.supplement_agent.run(rep)
            out.append(rep)
        return out

    @staticmethod
    def correlate_versions(repo: GitRepo, agent: TargetAgent, comps: list[ComparisonResult]) -> list[CodeDiffReport]:
        out = []
        for comp in comps:
            if not comp.baseline_ref or not comp.revised_ref:
                continue
            rep = build_code_diff(repo, agent.id, f"{comp.pair_id} 산출물 생성 코드 버전 차이", comp.baseline_ref, comp.revised_ref, agent.paths)
            comp.stats["explained_by_code_change"] = correlate_changes(comp.changes, rep)
            out.append(rep)
        return out

    @staticmethod
    def attach_user_code_evidence(patterns: list[Pattern], reports: list[CodeDiffReport]) -> int:
        n = 0
        for p in patterns:
            refs = []
            for rep in reports:
                if rep.error:
                    continue
                for a in p.anchors:
                    if a.role in ("new_column", "new_section", "column", "section", "sheet"):
                        refs += [r for r in find_in_code_diff(rep, a.text, a.role) if "추가" in (r.note or "")]
            if refs:
                uniq = {}
                for r in refs:
                    uniq.setdefault((r.file, r.line_start), r)
                p.user_code_evidence = list(uniq.values())[:4]
                n += 1
        return n
