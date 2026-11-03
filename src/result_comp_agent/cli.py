"""명령행 인터페이스.

  rca run -c config.yaml            설정 파일 기반 전체 분석 (산출물 비교 → 코드 추적 → 보완 분석 → 개선 권고 → 검증 → 보고서)
  rca diff 원본 수정본               단일 비교쌍 diff (+ 선택적으로 --repo 로 코드 추적)
  rca code-diff --repo R --base A --head B   에이전트 코드 버전/브랜치 비교
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import __version__


def _llm(args, cfg_llm=None):
    from .llm.client import LLMClient, LLMSettings

    overrides = {}
    if cfg_llm is not None:
        overrides = dict(
            deployment_main=cfg_llm.deployment_main, deployment_fast=cfg_llm.deployment_fast,
            reasoning_effort=cfg_llm.reasoning_effort, api_version=cfg_llm.api_version,
        )
    s = LLMSettings.from_env(**overrides)
    if getattr(args, "cache_dir", None):
        s.cache_dir = str(Path(args.cache_dir) / "llm")
    if getattr(args, "offline", False) or (cfg_llm is not None and cfg_llm.enabled is False):
        s.enabled = False
    if cfg_llm is not None and cfg_llm.enabled is True and not s.is_active:
        raise SystemExit("설정에서 llm.enabled=true 이지만 AZURE_OPENAI_ENDPOINT/인증 정보가 없습니다.")
    return LLMClient(s)


def _print_summary(res, paths: dict) -> None:
    rel = res.reliability
    ig = rel.get("integrity", {})
    tr = rel.get("trace", {})
    rc = rel.get("recommendations", {})
    print(f"\n== 분석 완료: {res.project}")
    print(f"   해석 엔진   : {'GPT-6 (Azure OpenAI)' if res.llm.get('active') else '오프라인(규칙 기반)'}")
    print(f"   diff 무결성 : {ig.get('pass')}/{ig.get('pairs')} PASS" + (f"  ⚠ {ig.get('fail')}" if ig.get("fail") else ""))
    ch = rel.get("changes", {})
    print(f"   검출 변경   : 내용 {ch.get('content')}건 / 서식·표기 {ch.get('format_only')}건")
    if tr.get("ratio") is not None:
        print(f"   코드 추적률 : {tr.get('traced')}/{tr.get('content_changes')} ({tr.get('ratio') * 100:.0f}%)")
    print(f"   개선 권고   : 검증됨 {rc.get('VERIFIED', 0)} / 부분 {rc.get('PARTIAL', 0)} / 기각 {rc.get('REJECTED', 0)}")
    for k, p in paths.items():
        print(f"   보고서({k:4}) : {p}")


def cmd_run(args) -> int:
    from .agents.orchestrator import Orchestrator
    from .config import RunConfig
    from .report import write_reports

    cfg = RunConfig.load(args.config)
    if args.cache_dir is None:
        args.cache_dir = cfg.resolve(cfg.cache_dir)
    llm = _llm(args, cfg.llm)
    orch = Orchestrator(cfg, llm, progress=lambda m: print("  ·", m, file=sys.stderr))
    res = orch.run()
    out = args.out or cfg.resolve(cfg.output_dir)
    formats = args.formats.split(",") if args.formats else cfg.report_formats
    paths = write_reports(res, out, formats)
    _print_summary(res, paths)
    if args.strict and res.reliability.get("integrity", {}).get("fail"):
        return 2
    return 0


def cmd_diff(args) -> int:
    from .agents.orchestrator import Orchestrator
    from .config import PairSpec, RunConfig, TargetAgent
    from .report import write_reports

    agent = TargetAgent(
        id=args.agent_id, name=args.agent_id, stage=args.stage or "", repo=args.repo, ref=args.ref,
        paths=[p for p in (args.paths or "").split(",") if p],
    )
    cfg = RunConfig(
        project=f"단일 비교: {Path(args.baseline).name} vs {Path(args.revised).name}",
        agents=[agent],
        comparisons=[PairSpec(agent=agent.id, baseline=str(Path(args.baseline).resolve()), revised=str(Path(args.revised).resolve()), revised_by=args.revised_by)],
        cache_dir=args.cache_dir or ".rca_cache",
    )
    if args.cache_dir is None:
        args.cache_dir = ".rca_cache"
    llm = _llm(args, cfg.llm)
    res = Orchestrator(cfg, llm, progress=lambda m: print("  ·", m, file=sys.stderr)).run()
    paths = write_reports(res, args.out, args.formats.split(",") if args.formats else None)
    _print_summary(res, paths)
    return 0


def cmd_code_diff(args) -> int:
    from .code.code_diff import build_code_diff
    from .code.git_source import GitRepo

    repo = GitRepo(args.repo, Path(args.cache_dir or ".rca_cache") / "repos")
    rep = build_code_diff(repo, "adhoc", f"{args.base}…{args.head}", args.base, args.head, [p for p in (args.paths or "").split(",") if p])
    if rep.error:
        print("오류:", rep.error, file=sys.stderr)
        return 1
    print(f"{rep.base_ref}({rep.base_commit[:10]}) → {rep.head_ref}({rep.head_commit[:10]}) 파일 {len(rep.files)}개")
    for f in rep.files:
        print(f"  {f.status:9} {f.path}  +{f.added_lines}/-{f.removed_lines}")
        for h in f.hunks:
            print(f"      @@ {h.new_start},{h.new_len} {h.symbol or ''}")
    if rep.literals_added:
        print("  추가된 문자열:", ", ".join(rep.literals_added[:20]))
    if rep.literals_removed:
        print("  삭제된 문자열:", ", ".join(rep.literals_removed[:20]))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rca", description="제품개발 에이전트 산출물(PPT/Excel) 비교·코드 추적·개선 권고 에이전트")
    p.add_argument("--version", action="version", version=f"rca {__version__}")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="설정 파일 기반 전체 분석")
    r.add_argument("-c", "--config", required=True)
    r.add_argument("-o", "--out")
    r.add_argument("--formats", help="md,xlsx,json,html 중 선택 (쉼표 구분)")
    r.add_argument("--offline", action="store_true", help="LLM 을 사용하지 않음 (규칙 기반)")
    r.add_argument("--cache-dir")
    r.add_argument("--strict", action="store_true", help="무결성 검증 실패 시 종료코드 2")
    r.set_defaults(func=cmd_run)

    d = sub.add_parser("diff", help="원본/수정본 단일 비교")
    d.add_argument("baseline")
    d.add_argument("revised")
    d.add_argument("-o", "--out", default="rca_output")
    d.add_argument("--agent-id", default="adhoc")
    d.add_argument("--stage")
    d.add_argument("--revised-by")
    d.add_argument("--repo", help="에이전트 코드 git URL 또는 로컬 경로")
    d.add_argument("--ref")
    d.add_argument("--paths", help="저장소 내 에이전트 코드 경로 (쉼표 구분)")
    d.add_argument("--formats")
    d.add_argument("--offline", action="store_true")
    d.add_argument("--cache-dir")
    d.set_defaults(func=cmd_diff)

    c = sub.add_parser("code-diff", help="에이전트 코드 ref 간 비교")
    c.add_argument("--repo", required=True)
    c.add_argument("--base", required=True)
    c.add_argument("--head", required=True)
    c.add_argument("--paths")
    c.add_argument("--cache-dir")
    c.set_defaults(func=cmd_code_diff)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
