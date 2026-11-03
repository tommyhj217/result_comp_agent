"""명령행 인터페이스.

  rca run -c config.yaml          설정 파일 기반 전체 분석
  rca diff 원본 수정본              단일 비교쌍 분석 (선택: --repo 로 코드 추적)
  rca check-env [--probe]          .env 의 Azure OpenAI(GPT) 설정 점검 (+ 실제 연결·모델 기능 확인)
  rca inspect 파일                  문서를 어떻게 읽는지(추출 단위) 확인 — 지원 범위 점검용
  rca tools                        사용 가능한 문서 형식 핸들러와 GPT 도구 목록
  rca code-diff --repo R --base A --head B   에이전트 코드 버전/브랜치 비교

종료코드: 0 성공 · 1 분석 실패(산출물 비교 전부 실패 등) · strict 모드 위반 시
  2 무결성 실패 · 3 비교 실패/시간초과 · 4 GPT 실패/시간초과 · 5 코드 로드 실패 · 6 보고서 작성 실패 · 7 설정/플러그인 오류
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Optional

from . import __version__

CATS = ["comparison", "integrity", "llm", "code", "report", "config"]


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def _llm_settings(args, cfg):
    from .llm.client import LLMSettings

    o = cfg.llm
    overrides = dict(
        deployment_main=o.deployment_main, deployment_fast=o.deployment_fast, reasoning_effort=o.reasoning_effort,
        api_version=o.api_version, timeout=getattr(args, "llm_timeout", None) or o.timeout_seconds,
        budget_seconds=getattr(args, "llm_budget", None) if getattr(args, "llm_budget", None) is not None else o.budget_seconds,
    )
    env_file = getattr(args, "env_file", None) or (cfg.resolve(cfg.env_file) if cfg.env_file else None)
    s = LLMSettings.from_env(env_file=env_file, config_dir=cfg.base_dir, override=getattr(args, "env_override", False), **overrides)
    s.cache_dir = str(Path(cfg.resolve(cfg.cache_dir)) / "llm")
    if getattr(args, "offline", False) or o.enabled is False:
        s.enabled = False
    return s


def _apply_cli_overrides(args, cfg) -> None:
    if getattr(args, "strict", False):
        cfg.strict.enabled = True
    if getattr(args, "strict_on", None):
        cfg.strict.enabled = True
        cfg.strict.fail_on = [c.strip() for c in args.strict_on.split(",") if c.strip() in CATS]
    if getattr(args, "fail_fast", False):
        cfg.strict.enabled = True
        cfg.strict.fail_fast = True
    if getattr(args, "timeout", None) is not None:
        cfg.diff.timeout_seconds = args.timeout
    if getattr(args, "no_tools", False):
        cfg.llm.use_tools = False
    if getattr(args, "plugin", None):
        cfg.plugins = list(cfg.plugins) + list(args.plugin)


def _execute(args, cfg, out_dir: str, formats: Optional[list[str]]) -> int:
    from .agents.orchestrator import Orchestrator, finalize_status
    from .llm.client import LLMClient
    from .models import StepFailure
    from .report import write_reports

    _apply_cli_overrides(args, cfg)
    try:
        settings = _llm_settings(args, cfg)
    except FileNotFoundError as e:
        _err(f"설정 오류: {e}")
        return 7
    pre_failures: list[StepFailure] = []
    if cfg.llm.enabled is True and not settings.is_active:
        env = settings.env_report
        msg = "llm.enabled=true 이지만 Azure OpenAI 설정이 불완전합니다: " + ", ".join(settings.missing()) + (f" (.env: {env.env_file})" if env and env.env_file else " (.env 파일 없음)")
        if cfg.strict.enabled and "config" in cfg.strict.fail_on:
            _err("설정 오류: " + msg)
            return 7
        pre_failures.append(StepFailure(category="config", stage="llm.config", kind="config", target="AZURE_OPENAI_*", message=msg, impact="GPT 없이 규칙 기반으로 진행"))
    llm = LLMClient(settings)
    if settings.is_active:
        _err(f"  · GPT 사용: 배포 '{settings.deployment_main}' @ {settings.endpoint} (API {settings.api_version}, .env: {settings.env_report.env_file if settings.env_report else '-'})")
        for w in (settings.env_report.warnings if settings.env_report else []):
            _err(f"    ⚠ {w}")
    else:
        _err("  · GPT 미사용 (오프라인/설정 없음) — 규칙 기반으로 분석합니다")
    orch = Orchestrator(cfg, llm, progress=lambda m: _err("  · " + m))
    orch.run_failures = pre_failures + orch.run_failures
    res = orch.run()
    paths, report_errors = write_reports(res, out_dir, formats)
    if report_errors:
        res.failures.extend(report_errors)
        finalize_status(res, cfg)
    _print_summary(res, paths)
    return res.exit_code


def _print_summary(res, paths: dict) -> None:
    from .report.common import CATEGORY_LABEL, STATUS_TEXT, failure_kind_label

    rel = res.reliability
    ig = rel.get("integrity", {})
    tr = rel.get("trace", {})
    rc = rel.get("recommendations", {})
    cm = rel.get("comparisons", {})
    ch = rel.get("changes", {})
    print(f"\n== {res.project}: {STATUS_TEXT.get(res.status, res.status)} (종료코드 {res.exit_code})")
    print(f"   해석 엔진   : {'GPT ' + str(res.llm.get('deployment_main')) if res.llm.get('active') else '오프라인(규칙 기반)'}")
    print(f"   산출물 비교 : {cm.get('succeeded', 0)}/{cm.get('configured', 0)} 성공" + (f"  ⛔ 실패 {cm.get('failed')}건" if cm.get("failed") else ""))
    print(f"   diff 무결성 : {ig.get('pass')}/{ig.get('pairs')} PASS" + (f"  ⚠ {ig.get('fail')}" if ig.get("fail") else ""))
    print(f"   검출 변경   : 내용 {ch.get('content')}건 / 서식·표기 {ch.get('format_only')}건")
    if tr.get("ratio") is not None:
        print(f"   코드 추적률 : {tr.get('traced')}/{tr.get('content_changes')} ({tr.get('ratio') * 100:.0f}%)")
    print(f"   개선 권고   : 검증됨 {rc.get('VERIFIED', 0)} / 부분 {rc.get('PARTIAL', 0)} / 기각 {rc.get('REJECTED', 0)}")
    if res.failures:
        print(f"   실패 {len(res.failures)}건:")
        for f in res.failures[:15]:
            print(f"     ⛔ [{CATEGORY_LABEL.get(f.category, f.category)}] {failure_kind_label(f.kind)} · {f.target} — {f.message[:140]}")
        if len(res.failures) > 15:
            print(f"     … 외 {len(res.failures) - 15}건 (보고서 '실패 내역' 참조)")
    st = res.strict or {}
    if st.get("enabled"):
        print(f"   strict      : 위반 {st.get('violated') or '없음'}" + (f" · 중단: {st['aborted']['stage']}" if st.get("aborted") else ""))
    for k, p in paths.items():
        print(f"   보고서({k:4}) : {p}")


# ------------------------------------------------------------------ 명령
def cmd_run(args) -> int:
    from .config import RunConfig

    try:
        cfg = RunConfig.load(args.config)
    except Exception as e:
        _err(f"설정 파일 오류: {e}")
        return 7
    out = args.out or cfg.resolve(cfg.output_dir)
    return _execute(args, cfg, out, args.formats.split(",") if args.formats else cfg.report_formats)


def cmd_diff(args) -> int:
    from .config import PairSpec, RunConfig, TargetAgent

    agent = TargetAgent(id=args.agent_id, name=args.agent_id, stage=args.stage or "", repo=args.repo, ref=args.ref,
                        paths=[p for p in (args.paths or "").split(",") if p])
    cfg = RunConfig(
        project=f"단일 비교: {Path(args.baseline).name} vs {Path(args.revised).name}",
        agents=[agent],
        comparisons=[PairSpec(agent=agent.id, baseline=str(Path(args.baseline).resolve()), revised=str(Path(args.revised).resolve()), revised_by=args.revised_by)],
        cache_dir=args.cache_dir or ".rca_cache",
        base_dir=str(Path.cwd()),
    )
    return _execute(args, cfg, args.out, args.formats.split(",") if args.formats else None)


def cmd_check_env(args) -> int:
    from .llm.client import LLMClient, LLMSettings

    try:
        s = LLMSettings.from_env(env_file=args.env_file, override=args.env_override)
    except FileNotFoundError as e:
        _err(str(e))
        return 7
    env = s.env_report
    print(f".env 파일: {env.env_file or '(없음 — 환경변수만 사용)'}")
    print(f"엔드포인트 방식: {'v1' if s.mode == 'v1' else 'api-version 지정'}")
    for row in env.masked():
        if row["source"] == "missing" and row["key"] not in ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_DEPLOYMENT", "AZURE_OPENAI_API_VERSION"):
            continue
        src = {"env": "환경변수", ".env": ".env", "default": "기본값", "missing": "없음", "endpoint-url": "엔드포인트 URL"}.get(row["source"], row["source"])
        alias = f" (키 '{row['read_from']}')" if row["read_from"] != row["key"] else ""
        print(f"  {row['key']:32} = {row['value'] if row['value'] is not None else '-':40} [{src}]{alias}")
    for w in env.warnings:
        print(f"  ⚠ {w}")
    for e in env.errors:
        print(f"  ⛔ {e}")
    if env.errors and not s.use_entra_id:
        return 7
    if not args.probe:
        print("설정 확인 완료 (실제 호출 점검은 --probe)")
        return 0
    s.cache_dir = None
    s.max_attempts = 1
    if args.timeout:
        s.timeout = args.timeout
    llm = LLMClient(s)
    print(f"\n배포 '{s.deployment_main}' 점검 중…")
    r = llm.probe(tools=not args.no_tools)
    if not r.get("ok"):
        print(f"  ⛔ 실패: {r.get('error_label')} — {r.get('error')}")
        return 4
    print(f"  ✅ 응답 성공 ({r.get('seconds')}초) · 응답 모델: {', '.join(r.get('served_models') or ['-'])}")
    caps = r.get("capabilities", {})
    print(f"  응답 형식: {caps.get('response_format')} · 토큰 파라미터: {caps.get('token_param')} · 함수 호출: {r.get('tools', '미점검')}")
    for note in caps.get("learned", []):
        print(f"  · 자동 적응: {note}")
    return 0


def cmd_inspect(args) -> int:
    from .documents import DocumentError, handler_for

    try:
        h = handler_for(args.file)
        ok, why = h.available()
        if not ok:
            _err(f"{h.name}: {why}")
            return 3
        units = h.outline(args.file)
    except DocumentError as e:
        _err(f"{e.kind}: {e}")
        return 3
    except Exception as e:
        _err(f"문서를 읽지 못했습니다: {type(e).__name__}: {e}")
        return 3
    if args.json:
        print(json.dumps([u.__dict__ for u in units[: args.limit]], ensure_ascii=False, indent=1))
    else:
        print(f"[{h.name}] {args.file} — 조회 단위 {len(units)}개")
        for u in units[: args.limit]:
            print(f"  {u.locator:28} ({u.kind}) {u.text[:120].replace(chr(10), ' ⏎ ')}")
        if len(units) > args.limit:
            print(f"  … 외 {len(units) - args.limit}개 (--limit 로 조정)")
    return 0


def cmd_tools(args) -> int:
    from .config import RunConfig
    from .documents import list_handlers
    from .plugins import load_plugins
    from .tools import default_registry

    plugins = []
    if args.config:
        cfg = RunConfig.load(args.config)
        plugins, errs = load_plugins(cfg.plugins, cfg.base_dir)
        for e in errs:
            _err(f"⚠ {e}")
    else:
        plugins, errs = load_plugins([])
        for e in errs:
            _err(f"⚠ {e}")
    print("문서 형식 핸들러:")
    for h in list_handlers():
        ok, why = h.available()
        print(f"  {'✅' if ok else '⛔'} {h.name:11} {', '.join(h.extensions):18} {h.description}" + ("" if ok else f"  ({why})"))
    print("\nGPT 도구 (함수 호출):")
    for t in default_registry().list():
        print(f"  · {t.name:18} [{t.source}] {t.description[:90]}")
    if plugins:
        print(f"\n로드된 플러그인: {', '.join(plugins)}")
    return 0


def cmd_code_diff(args) -> int:
    from .code.code_diff import build_code_diff
    from .code.git_source import GitRepo

    repo = GitRepo(args.repo, Path(args.cache_dir or ".rca_cache") / "repos")
    rep = build_code_diff(repo, "adhoc", f"{args.base}…{args.head}", args.base, args.head, [p for p in (args.paths or "").split(",") if p])
    if rep.error:
        _err("오류: " + rep.error)
        return 5
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


def _common_run_opts(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("GPT(Azure OpenAI)")
    g.add_argument("--env-file", help="Azure 설정 .env 경로 (기본: 설정 파일 폴더 → 현재 폴더의 .env)")
    g.add_argument("--env-override", action="store_true", help=".env 값이 환경변수보다 우선")
    g.add_argument("--offline", action="store_true", help="GPT 를 사용하지 않음 (규칙 기반)")
    g.add_argument("--llm-timeout", type=float, help="GPT 호출 1회 제한 시간(초)")
    g.add_argument("--llm-budget", type=float, help="전체 GPT 시간 예산(초), 0=무제한")
    g.add_argument("--no-tools", action="store_true", help="GPT 함수 호출(도구 조회) 끄기")
    s = p.add_argument_group("실패/strict")
    s.add_argument("--timeout", type=float, help="비교쌍 1개 제한 시간(초), 0=제한 없음")
    s.add_argument("--strict", action="store_true", help="실패가 있으면 실패 종료코드 반환")
    s.add_argument("--strict-on", help="strict 판정 범주 (쉼표): " + ",".join(CATS))
    s.add_argument("--fail-fast", action="store_true", help="첫 실패에서 중단 (strict 포함)")
    p.add_argument("--plugin", action="append", help="문서 핸들러/GPT 도구 플러그인 모듈 또는 .py (여러 번 지정 가능)")
    p.add_argument("--formats", help="md,xlsx,json,html 중 선택 (쉼표 구분)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rca", description="제품개발 에이전트 산출물 비교·코드 추적·개선 권고 에이전트")
    p.add_argument("--version", action="version", version=f"rca {__version__}")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="설정 파일 기반 전체 분석")
    r.add_argument("-c", "--config", required=True)
    r.add_argument("-o", "--out")
    _common_run_opts(r)
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
    d.add_argument("--cache-dir")
    _common_run_opts(d)
    d.set_defaults(func=cmd_diff)

    e = sub.add_parser("check-env", help=".env 의 Azure OpenAI(GPT) 설정 점검")
    e.add_argument("--env-file")
    e.add_argument("--env-override", action="store_true")
    e.add_argument("--probe", action="store_true", help="실제로 배포를 호출해 연결·응답 형식·함수 호출 지원 확인")
    e.add_argument("--no-tools", action="store_true", help="probe 시 함수 호출 점검 생략")
    e.add_argument("--timeout", type=float, help="probe 호출 제한 시간(초)")
    e.set_defaults(func=cmd_check_env)

    i = sub.add_parser("inspect", help="문서 추출 결과(조회 단위) 확인")
    i.add_argument("file")
    i.add_argument("--limit", type=int, default=60)
    i.add_argument("--json", action="store_true")
    i.set_defaults(func=cmd_inspect)

    t = sub.add_parser("tools", help="문서 형식 핸들러·GPT 도구 목록")
    t.add_argument("-c", "--config", help="플러그인을 포함해 보려면 설정 파일 지정")
    t.set_defaults(func=cmd_tools)

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
