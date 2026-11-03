"""단일 파일 HTML 대시보드 보고서.

안정성 설계
* 외부 리소스 없음 + CSP(Content-Security-Policy)로 외부 로드 차단
* 모든 데이터는 HTML 이스케이프, DOM id 는 해시 기반(특수문자 안전), 스크립트에 데이터를 넣지 않음(data-* 속성 + 이벤트 위임)
* 섹션별 예외 격리: 한 섹션 렌더링이 실패해도 나머지는 표시되고 해당 위치에 오류 카드가 나타남
* 긴 텍스트·대량 행은 잘라서 표시하고 생략 사실을 명시 (전체는 Excel/JSON)
"""
from __future__ import annotations

import hashlib
import html
import json
import traceback
from collections import Counter
from typing import Callable

from ..models import AgentAnalysis, AnalysisResult, CHANGE_TYPE_LABELS
from .common import (
    CATEGORY_LABEL, PRIORITY_LABEL, STATUS_LABEL, STATUS_TEXT, agent_counts, cat_label, change_brief, failure_kind_label, pct,
    ref_text, sorted_changes,
)

MAX_CHANGE_ROWS = 800
MAX_TEXT = 600

CSS = """
:root{--bg:#f7f8fa;--card:#fff;--fg:#1d2330;--muted:#5d6678;--line:#e3e6ec;--accent:#2457c5;--ok:#1f8a4c;--warn:#a86b0c;--bad:#c53030;--code:#f1f3f7;--badbg:#fdecec;--warnbg:#fff6e0;--okbg:#e8f6ee}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#12151b;--card:#1b2029;--fg:#e6e9ef;--muted:#9aa3b5;--line:#2c3340;--accent:#7aa2ff;--ok:#5cc28a;--warn:#e0b45a;--bad:#f07a7a;--code:#232a35;--badbg:#3a1f22;--warnbg:#3a3020;--okbg:#1d3326}}
:root[data-theme=dark]{--bg:#12151b;--card:#1b2029;--fg:#e6e9ef;--muted:#9aa3b5;--line:#2c3340;--accent:#7aa2ff;--ok:#5cc28a;--warn:#e0b45a;--bad:#f07a7a;--code:#232a35;--badbg:#3a1f22;--warnbg:#3a3020;--okbg:#1d3326}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.55 system-ui,-apple-system,"Segoe UI","Apple SD Gothic Neo","Malgun Gothic",sans-serif;overflow-wrap:break-word}
main{max-width:1200px;margin:0 auto;padding:24px 16px 64px}h1{font-size:22px;margin:0 0 4px}h2{font-size:18px;margin:32px 0 12px}h3{font-size:16px;margin:22px 0 8px}
.muted{color:var(--muted)}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}.kpi{font-size:24px;font-weight:650}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);font-size:13px}
th,td{padding:7px 9px;word-break:keep-all;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{background:var(--code);font-weight:600}
.tw{overflow-x:auto;border-radius:8px}td code{white-space:nowrap}.badge{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;font-weight:600;border:1px solid currentColor;white-space:nowrap}
.VERIFIED,.PASS,.SUCCESS{color:var(--ok)}.PARTIAL{color:var(--warn)}.REJECTED,.FAIL,.FAILED{color:var(--bad)}
.banner{border-radius:10px;padding:12px 14px;margin:14px 0;border:1px solid var(--line)}.banner.SUCCESS{background:var(--okbg)}.banner.PARTIAL{background:var(--warnbg)}.banner.FAILED{background:var(--badbg)}
pre{background:var(--code);border-radius:6px;padding:10px;overflow-x:auto;font-size:12px;margin:8px 0;white-space:pre}code{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px}
.rec{margin:10px 0}.rec h4{margin:0 0 6px;font-size:15px}.rec dl{display:grid;grid-template-columns:90px 1fr;gap:4px 10px;margin:6px 0}.rec dt{color:var(--muted)}.rec dd{margin:0;min-width:0}
details summary{cursor:pointer;color:var(--accent)}input[type=search]{width:100%;max-width:420px;padding:7px 10px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg);margin:6px 0}
.add{color:var(--ok)}.del{color:var(--bad)}.err{border-color:var(--bad);background:var(--badbg)}
@media (max-width:640px){.rec dl{grid-template-columns:1fr}.rec dt{margin-top:6px}}
@media print{input[type=search]{display:none}details{display:block}}
"""

JS = """
document.addEventListener('input',function(ev){var t=ev.target;if(!t||!t.dataset||!t.dataset.filter)return;
var tb=document.getElementById(t.dataset.filter);if(!tb)return;var q=t.value.toLowerCase();
Array.prototype.forEach.call(tb.querySelectorAll('tbody tr'),function(tr){tr.style.display=tr.textContent.toLowerCase().indexOf(q)>=0?'':'none'})});
"""

CSP = "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:"


def _e(s, limit: int | None = None) -> str:
    t = "" if s is None else str(s)
    if limit and len(t) > limit:
        t = t[: limit - 1] + "…"
    return html.escape(t, quote=True)


def _id(prefix: str, raw: str) -> str:
    return f"{prefix}-{hashlib.sha1(str(raw).encode('utf-8', 'replace')).hexdigest()[:10]}"


def _diff_html(patch: str) -> str:
    out = []
    for ln in patch.splitlines()[:400]:
        cls = "add" if ln.startswith("+") and not ln.startswith("+++") else "del" if ln.startswith("-") and not ln.startswith("---") else ""
        out.append(f'<span class="{cls}">{_e(ln)}</span>' if cls else _e(ln))
    return "<pre><code>" + "\n".join(out) + "</code></pre>"


def _safe(title: str, fn: Callable[[], str]) -> str:
    try:
        return fn()
    except Exception as e:  # 섹션 단위 격리
        tb = traceback.format_exc(limit=3)
        return f"<div class='card err'><b>⚠ '{_e(title)}' 섹션을 표시하지 못했습니다</b><br><code>{_e(type(e).__name__)}: {_e(e, 300)}</code><details><summary>상세</summary><pre>{_e(tb, 3000)}</pre></details></div>"


def _status_banner(res: AnalysisResult) -> str:
    st = res.strict or {}
    cats = Counter(f.category for f in res.failures)
    parts = [f"<div class='banner {res.status}'><b class='{res.status}'>실행 결과: {_e(STATUS_TEXT.get(res.status, res.status))}</b>"]
    if cats:
        parts.append(" — 실패 " + ", ".join(f"{_e(CATEGORY_LABEL.get(c, c))} {n}건" for c, n in cats.items()))
    if st.get("enabled"):
        v = st.get("violated") or []
        parts.append(f"<br><span class='muted'>strict 모드 (실패 기준: {_e(', '.join(st.get('fail_on') or []))}{' · fail-fast' if st.get('fail_fast') else ''})"
                     f" → {'위반: ' + _e(', '.join(CATEGORY_LABEL.get(x, x) for x in v)) if v else '위반 없음'} · 종료코드 {res.exit_code}</span>")
    if st.get("aborted"):
        ab = st["aborted"]
        parts.append(f"<br><b class='FAILED'>첫 실패에서 분석 중단</b>: {_e(ab.get('stage'))} / {_e(ab.get('target'))} — {_e(ab.get('message'), 300)}")
    parts.append("</div>")
    return "".join(parts)


def _failures_table(res: AnalysisResult) -> str:
    if not res.failures:
        return "<p class='muted'>실패 없음</p>"
    rows = []
    for f in res.failures:
        rows.append(f"<tr><td>{_e(CATEGORY_LABEL.get(f.category, f.category))}</td><td><code>{_e(f.stage)}</code></td><td><b class='FAIL'>{_e(failure_kind_label(f.kind))}</b></td>"
                    f"<td><code>{_e(f.target, 80)}</code></td><td>{_e(f.message, 500)}</td><td>{_e(f.impact)}</td></tr>")
    return ("<div class='tw'><table><thead><tr><th>범주</th><th>단계</th><th>종류</th><th>대상</th><th>내용</th><th>영향</th></tr></thead><tbody>"
            + "".join(rows) + "</tbody></table></div>")


def _llm_card(res: AnalysisResult) -> str:
    llm = res.llm or {}
    if not llm.get("active"):
        return f"<p class='muted'>GPT 미사용 — {_e(llm.get('mode') or '오프라인(규칙 기반)')}</p>"
    served = "; ".join(f"{k}: {', '.join(v)}" for k, v in (llm.get("served_models") or {}).items()) or "-"
    learned = []
    for dep, c in (llm.get("capabilities") or {}).items():
        for note in c.get("learned", []):
            learned.append(f"{dep}: {note}")
    rows = [
        ("엔드포인트", llm.get("endpoint")), ("배포(주/검증)", f"{llm.get('deployment_main')} / {llm.get('deployment_fast')}"),
        ("API 버전", f"{llm.get('api_version')} ({llm.get('mode')})"), ("응답 모델", served),
        (".env", llm.get("env_file") or "(환경변수)"), ("호출", f"{llm.get('calls')}회 · 캐시 {llm.get('cache_hits')} · 재시도 {llm.get('retries')} · 도구 호출 {llm.get('tool_calls')}"),
        ("시간 제한", f"호출당 {llm.get('timeout_seconds')}초 · 전체 예산 {llm.get('budget_seconds')}초"),
        ("실패", f"{llm.get('failures')}회" + (f" · 회로 차단: {llm.get('circuit_open')}" if llm.get("circuit_open") else "")),
        ("모델 적응", "; ".join(learned) or "없음 (모든 요청 파라미터 지원)"),
    ]
    return "<div class='tw'><table><tbody>" + "".join(f"<tr><th style='width:140px'>{_e(k)}</th><td>{_e(v, 800)}</td></tr>" for k, v in rows) + "</tbody></table></div>"


def _agent_section(a: AgentAnalysis) -> str:
    P: list[str] = []
    changes = [c for comp in a.comparisons for c in comp.changes]
    by_id = {c.id: c for c in changes}
    P.append(f"<h2 id='{_id('ag', a.agent_id)}'>{_e(a.name)} <span class='muted'>({_e(a.stage)})</span></h2>")
    P.append(f"<div class='muted'>코드 {_e(a.repo or '-')} @ {_e(a.ref or 'HEAD')} {('· 커밋 ' + _e(a.commit[:12])) if a.commit else ''}</div>")
    if a.failures:
        P.append("<div class='card err'><b>이 에이전트의 실패</b><ul>" + "".join(
            f"<li><b>{_e(failure_kind_label(f.kind))}</b> [{_e(f.stage)}] {_e(f.target)} — {_e(f.message, 300)} <span class='muted'>({_e(f.impact)})</span></li>" for f in a.failures
        ) + "</ul></div>")

    def comps_table() -> str:
        rows = []
        for comp in a.comparisons:
            types = Counter(CHANGE_TYPE_LABELS.get(c.change_type.value, c.change_type.value) for c in comp.changes if not c.format_only)
            notes = "<br>".join(_e(n, 200) for n in comp.notes)
            rows.append(f"<tr><td><code>{_e(comp.pair_id)}</code></td><td>{_e(comp.revised_by or '-')}</td><td>{_e(comp.artifact)}</td>"
                        f"<td>{comp.stats.get('content', 0)}/{comp.stats.get('format_only', 0)}</td><td>{_e(', '.join(f'{t} {n}' for t, n in types.most_common(4)))}</td>"
                        f"<td class='{comp.integrity.status}'>{_e(comp.integrity.status)} ({comp.integrity.checks})</td><td>{_e(comp.seconds)}초</td><td>{notes}</td></tr>")
        for f in a.failed_comparisons:
            rows.append(f"<tr><td><code>{_e(f.pair_id)}</code></td><td>{_e(f.revised_by or '-')}</td><td>-</td><td colspan='2' class='FAIL'><b>비교 실패: {_e(failure_kind_label(f.kind))}</b> — {_e(f.message, 300)}</td>"
                        f"<td class='FAIL'>-</td><td>-</td><td>{_e(f.baseline_path, 120)} ↔ {_e(f.revised_path, 120)}</td></tr>")
        if not rows:
            return "<p class='muted'>비교쌍 없음</p>"
        return ("<div class='tw'><table><thead><tr><th>비교쌍</th><th>수정자</th><th>형식</th><th>내용/서식</th><th>주요 변경</th><th>무결성</th><th>소요</th><th>비고</th></tr></thead><tbody>"
                + "".join(rows) + "</tbody></table></div>")

    P.append("<h3>산출물 비교</h3>" + _safe("산출물 비교", comps_table))
    cats = Counter(c.category for c in changes if not c.format_only)
    if cats:
        P.append("<p>" + " · ".join(f"<b>{_e(cat_label(k))}</b> {v}" for k, v in cats.most_common()) + "</p>")

    def recs() -> str:
        if not a.recommendations:
            return "<p class='muted'>권고 없음</p>"
        out = []
        for r in a.recommendations:
            v = r.verification
            out.append(f"<div class='card rec'><h4><span class='badge {v.status}'>{_e(STATUS_LABEL.get(v.status, v.status))}</span> {_e(r.title)}</h4>")
            out.append(f"<div class='muted'>우선순위 {_e(PRIORITY_LABEL.get(r.priority, r.priority))} · 신뢰도 {r.confidence:.2f} · {'규칙 기반' if r.source == 'rule' else 'GPT'} · {_e(r.id)}</div><dl>")
            out.append(f"<dt>문제</dt><dd>{_e(r.problem, 1200)}</dd><dt>권고</dt><dd>{_e(r.recommendation, 1200)}</dd>")
            if r.expected_effect:
                out.append(f"<dt>기대 효과</dt><dd>{_e(r.expected_effect, 500)}</dd>")
            evs = [f"<code>{_e(cid)}</code> {_e(by_id[cid].location.path, 160)} — {_e(change_brief(by_id[cid]), 240)}" for cid in r.evidence_change_ids[:4] if cid in by_id]
            more = f" 외 {len(r.evidence_change_ids) - 4}건" if len(r.evidence_change_ids) > 4 else ""
            out.append(f"<dt>근거</dt><dd>{'<br>'.join(evs)}{_e(more)}</dd>")
            if r.code_refs:
                out.append("<dt>코드 위치</dt><dd>" + "<br>".join(f"<code>{_e(ref_text(x))}</code> {_e(x.note or '')}" for x in r.code_refs) + "</dd>")
            cc = f"<br>GPT 교차검증: {_e(v.llm_cross_check)} — {_e(v.llm_cross_check_reason)}" if v.llm_cross_check else ""
            probs = (" / ⚠ " + _e("; ".join(v.problems[:3]), 600)) if v.problems else ""
            out.append(f"<dt>검증</dt><dd>{_e('; '.join(v.checks[:5]), 800)}{probs}{cc}</dd></dl>")
            if r.code_refs and r.code_refs[0].snippet:
                out.append(f"<details><summary>코드 스니펫</summary><pre><code>{_e(r.code_refs[0].snippet, 6000)}</code></pre></details>")
            for ed in r.proposed_edits:
                if ed.verified and ed.patch:
                    out.append(f"<details open><summary>수정 패치 ({_e(ed.file)})</summary>{_diff_html(ed.patch)}</details>")
                elif ed.file:
                    out.append(f"<div class='muted'>수정안({_e(ed.file)}) 미검증: {_e(ed.verification_note, 300)}</div>")
            out.append("</div>")
        return "".join(out)

    P.append("<h3>개선 권고</h3>" + _safe("개선 권고", recs))

    def patterns() -> str:
        pats = [p for p in a.patterns if p.category != "FORMAT_ONLY"]
        if not pats:
            return ""
        rows = "".join(
            f"<tr><td>{_e(p.title, 200)}</td><td>{_e(cat_label(p.category))}</td><td>{len(p.support_pairs)}/{len(a.comparisons)}</td><td>{len(p.change_ids)}</td>"
            f"<td>{'<br>'.join(_e(f'{r.file}:{r.line_start}') for r in p.user_code_evidence[:2]) or '-'}</td></tr>" for p in pats[:40]
        )
        return "<h3>사용자 보완 패턴</h3><div class='tw'><table><thead><tr><th>패턴</th><th>유형</th><th>지원</th><th>건수</th><th>사용자 코드 근거</th></tr></thead><tbody>" + rows + "</tbody></table></div>"

    P.append(_safe("사용자 보완 패턴", patterns))

    def change_list() -> str:
        tid = _id("t", a.agent_id)
        ordered = sorted_changes(changes)
        rows = []
        for c in ordered[:MAX_CHANGE_ROWS]:
            ref = ref_text(c.code_refs[0]) if c.code_refs else "추적 불가"
            fmt = " <span class='muted'>(서식)</span>" if c.format_only else ""
            rows.append(f"<tr><td><code>{_e(c.id)}</code></td><td>{_e(CHANGE_TYPE_LABELS.get(c.change_type.value, c.change_type.value))}{fmt}</td>"
                        f"<td>{_e(c.location.path, 200)}</td><td>{_e(change_brief(c), MAX_TEXT)}</td><td>{_e(cat_label(c.category))}</td><td><code>{_e(ref, 200)}</code></td></tr>")
        note = f"<p class='muted'>전체 {len(ordered)}건 중 {MAX_CHANGE_ROWS}건 표시 — 전체 목록은 report.xlsx '변경사항' 시트</p>" if len(ordered) > MAX_CHANGE_ROWS else ""
        return (f"<h3>변경 목록</h3><input type='search' placeholder='검색 (위치, 내용, 유형…)' data-filter='{tid}' aria-label='변경 목록 검색'>"
                f"<div class='tw'><table id='{tid}'><thead><tr><th>ID</th><th>유형</th><th>위치</th><th>변경</th><th>보완 유형</th><th>코드 위치</th></tr></thead><tbody>"
                + "".join(rows) + "</tbody></table></div>" + note)

    P.append(_safe("변경 목록", change_list))

    def extras() -> str:
        out = []
        for d in a.code_diffs:
            if d.supplements:
                out.append(f"<h3>사용자 코드 보완 — {_e(d.label)}</h3><ul>" + "".join(
                    f"<li><code>{_e(s.file)}:{s.hunk_new_start}</code> {_e(s.symbol or '')} [{_e(s.kind)}] {_e(s.summary, 300)}{'' if s.verified else ' (미검증)'}</li>" for s in d.supplements[:30]
                ) + "</ul>")
        if a.tool_trace:
            out.append("<details><summary>GPT 도구 조회 기록 (" + str(len(a.tool_trace)) + "회)</summary><div class='tw'><table><thead><tr><th>단계</th><th>도구</th><th>인자</th><th>결과 크기</th><th>오류</th></tr></thead><tbody>"
                       + "".join(f"<tr><td>{_e(t.get('step'))}</td><td><code>{_e(t.get('tool'))}</code></td><td><code>{_e(t.get('arguments'), 300)}</code></td><td>{_e(t.get('result_chars'))}</td><td>{_e(t.get('error') or '')}</td></tr>" for t in a.tool_trace[:100])
                       + "</tbody></table></div></details>")
        if a.llm_notes:
            out.append("<details><summary>비고</summary><ul>" + "".join(f"<li>{_e(n, 400)}</li>" for n in a.llm_notes[:100]) + "</ul></details>")
        return "".join(out)

    P.append(_safe("부가 정보", extras))
    return "".join(P)


def render_html(res: AnalysisResult) -> str:
    rel = res.reliability or {}
    ig = rel.get("integrity", {})
    tr = rel.get("trace", {})
    rc = rel.get("recommendations", {})
    ch = rel.get("changes", {})
    cmp_ = rel.get("comparisons", {})
    llm_mode = f"GPT ({(res.llm or {}).get('deployment_main')})" if (res.llm or {}).get("active") else "오프라인(규칙 기반)"
    P: list[str] = []
    P.append(f"<h1>{_e(res.project)}</h1><div class='muted'>생성 {_e(res.generated_at)} · v{_e(res.tool_version)} · 해석 엔진 {_e(llm_mode)}"
             + (f" · 플러그인 {_e(', '.join(res.plugins))}" if res.plugins else "") + "</div>")
    P.append(_safe("실행 결과", lambda: _status_banner(res)))

    def kpis() -> str:
        integ_cls = "PASS" if not ig.get("fail") else "FAIL"
        cmp_cls = "PASS" if not cmp_.get("failed") else "FAIL"
        cards = [
            ("산출물 비교", f"{cmp_.get('succeeded', 0)}/{cmp_.get('configured', 0)}", f"실패 {cmp_.get('failed', 0)}건", cmp_cls),
            ("diff 무결성", f"{ig.get('pass', 0)}/{ig.get('pairs', 0)}", "원본+변경=수정본 재구성", integ_cls),
            ("검출 변경", f"{ch.get('content', 0)}", f"내용 · 서식 {ch.get('format_only', 0)}건 별도", ""),
            ("코드 추적률", pct(tr.get("ratio")), f"{tr.get('traced', 0)}/{tr.get('content_changes', 0)} 내용 변경", ""),
            ("검증된 권고", f"{rc.get('VERIFIED', 0)}", f"부분 {rc.get('PARTIAL', 0)} · 기각 {rc.get('REJECTED', 0)}", "VERIFIED"),
        ]
        return "<h2>신뢰도 요약</h2><div class='grid'>" + "".join(
            f"<div class='card'><div class='muted'>{_e(t)}</div><div class='kpi {c}'>{_e(v)}</div><div class='muted'>{_e(s)}</div></div>" for t, v, s, c in cards
        ) + (f"</div><p class='muted'>변경 검출은 {_e(rel.get('detection'))}. GPT 는 검출된 변경의 해석·개선안 작성에만 쓰이며, 모든 권고는 근거 변경 ID·코드 인용·패치 적용 가능성을 결정론적으로 검증합니다.</p>")

    P.append(_safe("신뢰도 요약", kpis))
    if res.failures:
        P.append("<h2>실패 내역</h2>" + _safe("실패 내역", lambda: _failures_table(res)))

    def agents_summary() -> str:
        rows = []
        for a in res.agents:
            k = agent_counts(a)
            nf = len(a.failed_comparisons)
            rows.append(f"<tr><td>{_e(a.stage)}</td><td><a href='#{_id('ag', a.agent_id)}'>{_e(a.name)}</a></td><td>{k['pairs']}{f' <b class=FAIL>(+실패 {nf})</b>' if nf else ''}</td><td>{k['content']}</td>"
                        f"<td class='{'PASS' if k['integrity_pass'] == k['pairs'] else 'FAIL'}'>{k['integrity_pass']}/{k['pairs']}</td><td>{pct(k['trace_ratio'])}</td><td>{k['verified']}/{k['partial']}</td>"
                        f"<td class='{'FAIL' if a.failures else 'PASS'}'>{len(a.failures)}</td></tr>")
        return ("<h2>에이전트 요약</h2><div class='tw'><table><thead><tr><th>단계</th><th>에이전트</th><th>비교쌍</th><th>내용 변경</th><th>무결성</th><th>코드 추적률</th><th>검증/부분 권고</th><th>실패</th></tr></thead><tbody>"
                + "".join(rows) + "</tbody></table></div>")

    P.append(_safe("에이전트 요약", agents_summary))
    P.append("<h2>GPT(Azure OpenAI) 실행 정보</h2>" + _safe("GPT 실행 정보", lambda: _llm_card(res)))
    for a in res.agents:
        P.append(_safe(f"에이전트 {a.agent_id}", lambda a=a: _agent_section(a)))

    def common() -> str:
        if not res.cross_agent_patterns:
            return ""
        return ("<h2>공통 보완 유형</h2><div class='tw'><table><thead><tr><th>유형</th><th>에이전트별</th><th>합계</th></tr></thead><tbody>"
                + "".join(f"<tr><td>{_e(cp['label'])}</td><td>{_e(json.dumps(cp['agents'], ensure_ascii=False))}</td><td>{_e(cp['total'])}</td></tr>" for cp in res.cross_agent_patterns)
                + "</tbody></table></div>")

    P.append(_safe("공통 보완 유형", common))
    return (
        "<!doctype html><html lang='ko'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<meta http-equiv='Content-Security-Policy' content=\"{CSP}\"><title>산출물 비교 분석</title><style>{CSS}</style></head>"
        f"<body><main>{''.join(P)}</main><script>{JS}</script></body></html>"
    )
