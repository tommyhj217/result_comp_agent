"""Markdown 보고서 (사람이 읽는 요약 + 근거)."""
from __future__ import annotations

from collections import Counter

from ..models import AnalysisResult, CHANGE_TYPE_LABELS, Recommendation
import traceback

from .common import (
    CATEGORY_LABEL, PRIORITY_LABEL, STATUS_ICON, STATUS_LABEL, STATUS_TEXT, agent_counts, cat_label, change_brief, failure_kind_label,
    pct, ref_text, short, sorted_changes,
)


def _fence(text: str, lang: str = "") -> list[str]:
    """내용에 ``` 가 있어도 깨지지 않는 코드 펜스."""
    n = 3
    while "`" * n in (text or ""):
        n += 1
    return ["`" * n + lang, text or "", "`" * n]


def _esc(s) -> str:
    return str(s or "").replace("|", "\\|").replace("\n", " ⏎ ")


def _rec_md(r: Recommendation, changes_by_id: dict) -> list[str]:
    v = r.verification
    out = [
        f"#### {STATUS_ICON[v.status]} {r.title}",
        "",
        f"- **상태**: {STATUS_LABEL[v.status]} · **우선순위**: {PRIORITY_LABEL[r.priority]} · **신뢰도**: {r.confidence:.2f} · "
        f"**출처**: {'규칙 기반(결정론)' if r.source == 'rule' else 'GPT 분석'} · `{r.id}`",
        f"- **문제**: {r.problem}",
        f"- **권고**: {r.recommendation}",
    ]
    if r.expected_effect:
        out.append(f"- **기대 효과**: {r.expected_effect}")
    ev = r.evidence_change_ids
    ev_txt = ", ".join(f"`{i}`" for i in ev[:8]) + (f" 외 {len(ev) - 8}건" if len(ev) > 8 else "")
    out.append(f"- **근거 변경** ({len(ev)}건): {ev_txt}")
    for cid in ev[:2]:
        c = changes_by_id.get(cid)
        if c:
            out.append(f"  - `{cid}` {c.location.path}: {change_brief(c)}")
    if r.code_refs:
        out.append("- **코드 위치**:")
        for ref in r.code_refs:
            out.append(f"  - `{ref_text(ref)}`" + (f" — {ref.note}" if ref.note else ""))
        snip = r.code_refs[0].snippet
        if snip:
            out += [""] + _fence(snip, "python")
    for e in r.proposed_edits:
        if e.verified and e.patch:
            out += ["", f"- **수정 패치** ({e.file}) — {e.explanation or ''}", ""] + _fence(e.patch.rstrip(), "diff")
        else:
            out.append(f"- 수정안({e.file}) 미검증: {e.verification_note}")
    bd = ", ".join(f"{k}={v2}" for k, v2 in r.confidence_breakdown.items())
    out.append(f"- **검증 내역**: " + "; ".join(v.checks[:6]) + (f" / ⚠ " + "; ".join(v.problems[:4]) if v.problems else ""))
    if v.llm_cross_check:
        out.append(f"- **GPT 교차검증**: {v.llm_cross_check} — {v.llm_cross_check_reason}")
    out.append(f"- 신뢰도 구성: {bd}")
    out.append("")
    return out


def _safe(title: str, fn) -> list[str]:
    try:
        return fn()
    except Exception as e:  # 섹션 단위 격리
        return [f"> ⚠ '{title}' 섹션을 작성하지 못했습니다: `{type(e).__name__}: {str(e)[:200]}`", "", "<details><summary>상세</summary>", "",
                *_fence(traceback.format_exc(limit=3)), "", "</details>", ""]


def _status_md(res: AnalysisResult) -> list[str]:
    st = res.strict or {}
    icon = {"SUCCESS": "✅", "PARTIAL": "🟡", "FAILED": "⛔"}.get(res.status, "")
    cats = Counter(f.category for f in res.failures)
    line = f"> **실행 결과: {icon} {STATUS_TEXT.get(res.status, res.status)}**"
    if cats:
        line += " — 실패 " + ", ".join(f"{CATEGORY_LABEL.get(c, c)} {n}건" for c, n in cats.items())
    out = [line]
    if st.get("enabled"):
        v = st.get("violated") or []
        out.append(f"> strict 모드 (실패 기준: {', '.join(st.get('fail_on') or [])}{' · fail-fast' if st.get('fail_fast') else ''}) → "
                   + (f"위반: {', '.join(CATEGORY_LABEL.get(x, x) for x in v)}" if v else "위반 없음") + f" · 종료코드 {res.exit_code}")
    if st.get("aborted"):
        ab = st["aborted"]
        out.append(f"> **첫 실패에서 분석 중단**: {ab.get('stage')} / {ab.get('target')} — {short(ab.get('message'), 200)}")
    return out + [""]


def _failures_md(res: AnalysisResult) -> list[str]:
    if not res.failures:
        return []
    L = ["## 실패 내역", "", "| 범주 | 단계 | 종류 | 대상 | 내용 | 영향 |", "|---|---|---|---|---|---|"]
    for f in res.failures:
        L.append(f"| {CATEGORY_LABEL.get(f.category, f.category)} | `{f.stage}` | **{failure_kind_label(f.kind)}** | {_esc(f.target)} | {_esc(short(f.message, 200))} | {_esc(f.impact)} |")
    return L + [""]


def _llm_md(res: AnalysisResult) -> list[str]:
    llm = res.llm or {}
    if not llm.get("active"):
        return [f"- **GPT**: 미사용 — {llm.get('mode') or '오프라인(규칙 기반)'}"]
    served = "; ".join(f"{k}: {', '.join(v)}" for k, v in (llm.get("served_models") or {}).items()) or "-"
    learned = [f"{d}: {n}" for d, c in (llm.get("capabilities") or {}).items() for n in c.get("learned", [])]
    return [
        f"- **GPT**: 배포 `{llm.get('deployment_main')}` / 검증 `{llm.get('deployment_fast')}` · API `{llm.get('api_version')}` · 응답 모델 {served} · .env `{llm.get('env_file') or '(환경변수)'}`",
        f"- **LLM 사용**: 호출 {llm.get('calls')}회 · 캐시 {llm.get('cache_hits')}회 · 재시도 {llm.get('retries')}회 · 실패 {llm.get('failures')}회 · 도구 호출 {llm.get('tool_calls')}회 "
        f"(호출당 {llm.get('timeout_seconds')}초 제한, 전체 예산 {llm.get('budget_seconds')}초)" + (f" · ⛔ 회로 차단: {llm.get('circuit_open')}" if llm.get("circuit_open") else ""),
        f"- **모델 적응**: " + ("; ".join(learned) if learned else "없음 (요청 파라미터 모두 지원)"),
    ]


def render_markdown(res: AnalysisResult, max_changes_per_agent: int = 40) -> str:
    L: list[str] = []
    llm_mode = f"GPT ({(res.llm or {}).get('deployment_main')}, Azure OpenAI)" if (res.llm or {}).get("active") else "오프라인(규칙 기반)"
    L += [f"# {res.project}", "", f"> 생성: {res.generated_at} · 도구 v{res.tool_version} · 해석 엔진: {llm_mode}"
          + (f" · 플러그인: {', '.join(res.plugins)}" if res.plugins else ""), ">"]
    L += _safe("실행 결과", lambda: _status_md(res))
    L += _safe("실패 내역", lambda: _failures_md(res))

    L += ["## 1. 요약", "", "| 에이전트 | 단계 | 비교쌍 | 내용 변경 | 서식 변경 | 무결성 | 코드 추적률 | 권고(검증/부분) |", "|---|---|---|---|---|---|---|---|"]
    for a in res.agents:
        k = agent_counts(a)
        integ = f"{k['integrity_pass']}/{k['pairs']} PASS" if k["pairs"] else "-"
        L.append(f"| {a.name} | {a.stage} | {k['pairs']} | {k['content']} | {k['format']} | {integ} | {pct(k['trace_ratio'])} | {k['verified']}/{k['partial']} |")
    L.append("")

    rel = res.reliability
    L += ["## 2. 신뢰도 검증", ""]
    L.append(f"- **변경 검출 방식**: {rel.get('detection')}")
    cm = rel.get("comparisons", {})
    if cm:
        L.append(f"- **산출물 비교**: {cm.get('succeeded')}/{cm.get('configured')} 성공" + (f" — ⛔ 실패: {'; '.join(cm.get('failed_detail', []))}" if cm.get("failed") else ""))
    ig = rel.get("integrity", {})
    L.append(f"- **diff 무결성**: {ig.get('pass')}/{ig.get('pairs')} 비교쌍 PASS" + (" — 실패: " + "; ".join(ig.get("fail", [])) if ig.get("fail") else ""))
    ch = rel.get("changes", {})
    L.append(f"- **검출 변경**: 총 {ch.get('total')}건 (내용 {ch.get('content')} / 서식·표기 {ch.get('format_only')}) · 낮은 신뢰도 대응(유사도<0.7) {ch.get('low_confidence_pairing')}건")
    tr = rel.get("trace", {})
    L.append(f"- **코드 추적**: 내용 변경 {tr.get('content_changes')}건 중 {tr.get('traced')}건 코드 위치 확인 ({pct(tr.get('ratio'))}) — 확인 못한 변경은 '추적 불가'로 표시")
    cl = rel.get("classification", {})
    if cl.get("llm_covered"):
        L.append(f"- **보완 유형 분류**: 규칙 분류 + GPT 해석 {cl['llm_covered']}건, 규칙-LLM 일치율 {pct(cl.get('rule_llm_agreement'))}")
    rc = rel.get("recommendations", {})
    L.append(f"- **권고 검증**: 검증됨 {rc.get('VERIFIED', 0)} · 부분 검증 {rc.get('PARTIAL', 0)} · 기각 {rc.get('REJECTED', 0)}")
    L += _llm_md(res)
    L.append("")

    L += ["## 3. 에이전트별 분석", ""]
    for a in res.agents:
        L += _safe(f"에이전트 {a.agent_id}", lambda a=a: _agent_md(a, max_changes_per_agent))
    L += _safe("공통 보완 유형", lambda: _tail_md(res))
    return "\n".join(L)


def _agent_md(a, max_changes_per_agent: int) -> list[str]:
    L: list[str] = []
    changes = [c for comp in a.comparisons for c in comp.changes]
    by_id = {c.id: c for c in changes}
    L += [f"### {a.name} ({a.stage})", ""]
    code_line = f"- 코드: `{a.repo or '-'}` @ `{a.ref or 'HEAD'}`" + (f" (커밋 `{a.commit[:12]}`, 파일 {a.code_files_indexed}개 인덱싱)" if a.commit else "")
    L.append(code_line)
    if a.code_error:
        L.append(f"- ⚠ {a.code_error}")
    L.append("")
    L += ["#### 산출물 차이", "", "| 비교쌍 | 수정자 | 내용/서식 | 주요 변경 유형 | 무결성 |", "|---|---|---|---|---|"]
    for comp in a.comparisons:
        types = Counter(CHANGE_TYPE_LABELS.get(c.change_type.value, c.change_type.value) for c in comp.changes if not c.format_only)
        tops = ", ".join(f"{t} {n}" for t, n in types.most_common(4))
        integ = "✅ PASS" if comp.integrity.status == "PASS" else "❌ FAIL"
        L.append(f"| `{comp.pair_id}` | {comp.revised_by or '-'} | {comp.stats.get('content')}/{comp.stats.get('format_only')} | {tops} | {integ} ({comp.integrity.checks} 검사) |")
    for f in a.failed_comparisons:
        L.append(f"| `{f.pair_id}` | {f.revised_by or '-'} | - | ⛔ **비교 실패: {failure_kind_label(f.kind)}** — {_esc(short(f.message, 160))} | - |")
    L.append("")
    notes = [f"`{c.pair_id}` {n}" for c in a.comparisons for n in c.notes]
    if notes:
        L += [f"- ℹ {n}" for n in notes] + [""]
    if a.failures:
        L += ["**이 에이전트의 실패**", ""] + [f"- ⛔ {failure_kind_label(f.kind)} [`{f.stage}`] {_esc(f.target)} — {short(f.message, 200)} ({f.impact})" for f in a.failures] + [""]
    cats = Counter(c.category for c in changes if not c.format_only)
    if cats:
        L.append("**보완 유형 분포**: " + ", ".join(f"{cat_label(k)} {v}" for k, v in cats.most_common()))
        L.append("")
    pats = [p for p in a.patterns if p.category != "FORMAT_ONLY"]
    if pats:
        L += ["#### 사용자 보완 패턴 (반복 순)", "", "| 패턴 | 유형 | 지원 비교쌍 | 건수 | 예시 | 사용자 코드 근거 |", "|---|---|---|---|---|---|"]
        for p in pats[:20]:
            ex = p.examples[0] if p.examples else {}
            exs = f"「{short(ex.get('before'), 30)}」→「{short(ex.get('after'), 40)}」" if ex.get("before") and ex.get("after") else short(ex.get("after") or ex.get("before"), 60)
            uce = "; ".join(f"{r.file}:{r.line_start}" for r in p.user_code_evidence[:2]) or "-"
            L.append(f"| {_esc(p.title)} | {cat_label(p.category)} | {len(p.support_pairs)}/{len(a.comparisons)} | {len(p.change_ids)} | {_esc(exs)} | {uce} |")
        L.append("")
    L += ["#### 에이전트 코드 개선 권고", ""]
    if not a.recommendations:
        L += ["(권고 없음)", ""]
    for r in a.recommendations:
        L += _rec_md(r, by_id)
    user_diffs = [d for d in a.code_diffs if "산출물 생성 코드" not in d.label]
    if user_diffs:
        L += ["#### 사용자 코드 보완 (브랜치 비교)", ""]
        for d in user_diffs:
            if d.error:
                L.append(f"- {d.label}: ⚠ {d.error}")
                continue
            L.append(f"- **{d.label}** (`{d.base_ref}` → `{d.head_ref}`, 파일 {len(d.files)}개)")
            for s in d.supplements[:15]:
                L.append(f"  - `{s.file}:{s.hunk_new_start}` {s.symbol or ''} [{s.kind}] {s.summary}" + ("" if s.verified else " (미검증)"))
        L.append("")
    ver_diffs = [d for d in a.code_diffs if "산출물 생성 코드" in d.label]
    for d in ver_diffs:
        L.append(f"- 코드 버전 차이 `{d.base_ref}`→`{d.head_ref}`: 파일 {len(d.files)}개 변경")
    L += ["#### 주요 변경 상세", "", "| ID | 유형 | 위치 | 변경 | 보완 유형 | 사용자 의도 | 코드 위치 |", "|---|---|---|---|---|---|---|"]
    content = [c for c in sorted_changes(changes) if not c.format_only]
    for c in content[:max_changes_per_agent]:
        ref = ref_text(c.code_refs[0]) if c.code_refs else "추적 불가"
        L.append(f"| `{c.id}` | {c.type_label} | {_esc(short(c.location.path, 60))} | {_esc(change_brief(c))} | {cat_label(c.category)} | {_esc(short(c.user_intent, 40)) or '-'} | {_esc(ref)} |")
    if len(content) > max_changes_per_agent:
        L.append(f"\n전체 {len(content)}건 중 {max_changes_per_agent}건 표시 — 전체 목록은 Excel 보고서 '변경사항' 시트 참조")
    if a.tool_trace:
        L += ["", f"<details><summary>GPT 도구 조회 기록 ({len(a.tool_trace)}회)</summary>", ""]
        L += [f"- {t.get('step')}. `{t.get('tool')}` {short(t.get('arguments'), 120)} → {t.get('result_chars')}자" + (f" (오류: {t.get('error')})" if t.get("error") else "") for t in a.tool_trace[:60]]
        L += ["", "</details>"]
    if a.llm_notes:
        L += ["", "<details><summary>비고</summary>", ""] + [f"- {n}" for n in a.llm_notes] + ["", "</details>"]
    L.append("")
    return L


def _tail_md(res: AnalysisResult) -> list[str]:
    L: list[str] = []
    if res.cross_agent_patterns:
        L += ["## 4. 여러 에이전트 공통 보완 유형 (공통 모듈 개선 후보)", "", "| 보완 유형 | 에이전트별 건수 | 합계 |", "|---|---|---|"]
        for cp in res.cross_agent_patterns:
            L.append(f"| {cp['label']} | " + ", ".join(f"{k} {v}" for k, v in cp["agents"].items()) + f" | {cp['total']} |")
        L.append("")
    rej = [r for a in res.agents for r in a.rejected_recommendations]
    if rej:
        L += ["## 5. 부록: 기각된 권고", ""]
        for r in rej:
            L.append(f"- `{r.id}` {r.title} — " + "; ".join(r.verification.problems[:3]))
        L.append("")
    return L
