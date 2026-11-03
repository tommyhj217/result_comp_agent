"""단일 파일 HTML 대시보드 보고서 (외부 리소스 없음, 라이트/다크 모드)."""
from __future__ import annotations

import html
import json
from collections import Counter

from ..models import AnalysisResult, CHANGE_TYPE_LABELS
from .common import PRIORITY_LABEL, STATUS_LABEL, agent_counts, cat_label, change_brief, pct, ref_text, sorted_changes

CSS = """
:root{--bg:#f7f8fa;--card:#fff;--fg:#1d2330;--muted:#5d6678;--line:#e3e6ec;--accent:#2457c5;--ok:#1f8a4c;--warn:#b7791f;--bad:#c53030;--code:#f1f3f7}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#12151b;--card:#1b2029;--fg:#e6e9ef;--muted:#9aa3b5;--line:#2c3340;--accent:#7aa2ff;--ok:#5cc28a;--warn:#e0b45a;--bad:#f07a7a;--code:#232a35}}
:root[data-theme=dark]{--bg:#12151b;--card:#1b2029;--fg:#e6e9ef;--muted:#9aa3b5;--line:#2c3340;--accent:#7aa2ff;--ok:#5cc28a;--warn:#e0b45a;--bad:#f07a7a;--code:#232a35}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.55 system-ui,-apple-system,"Segoe UI","Apple SD Gothic Neo","Malgun Gothic",sans-serif}
main{max-width:1200px;margin:0 auto;padding:24px 16px 64px}h1{font-size:22px;margin:0 0 4px}h2{font-size:18px;margin:32px 0 12px}h3{font-size:16px;margin:24px 0 8px}
.muted{color:var(--muted)}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}.kpi{font-size:24px;font-weight:650}.kpi small{font-size:12px;font-weight:400;color:var(--muted)}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:8px;overflow:hidden;font-size:13px}
th,td{padding:7px 9px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{background:var(--code);font-weight:600}
.tw{overflow-x:auto}.badge{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;font-weight:600;border:1px solid currentColor}
.VERIFIED{color:var(--ok)}.PARTIAL{color:var(--warn)}.REJECTED{color:var(--bad)}.PASS{color:var(--ok)}.FAIL{color:var(--bad)}
pre{background:var(--code);border-radius:6px;padding:10px;overflow-x:auto;font-size:12px;margin:8px 0}code{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px}
.rec{margin:10px 0}.rec h4{margin:0 0 6px;font-size:15px}.rec dl{display:grid;grid-template-columns:90px 1fr;gap:4px 10px;margin:6px 0}.rec dt{color:var(--muted)}.rec dd{margin:0}
details summary{cursor:pointer;color:var(--accent)}input[type=search]{width:100%;max-width:420px;padding:7px 10px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg);margin:6px 0}
.add{color:var(--ok)}.del{color:var(--bad)}
"""

JS = """
function filt(inp,tid){const q=inp.value.toLowerCase();document.querySelectorAll('#'+tid+' tbody tr').forEach(tr=>{tr.style.display=tr.textContent.toLowerCase().includes(q)?'':'none'})}
"""


def _e(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _diff_html(patch: str) -> str:
    out = []
    for ln in patch.splitlines():
        cls = "add" if ln.startswith("+") and not ln.startswith("+++") else "del" if ln.startswith("-") and not ln.startswith("---") else ""
        out.append(f'<span class="{cls}">{_e(ln)}</span>' if cls else _e(ln))
    return "<pre><code>" + "\n".join(out) + "</code></pre>"


def render_html(res: AnalysisResult) -> str:
    rel = res.reliability
    ig = rel.get("integrity", {})
    tr = rel.get("trace", {})
    rc = rel.get("recommendations", {})
    ch = rel.get("changes", {})
    llm_mode = "GPT-6 (Azure OpenAI)" if res.llm.get("active") else "오프라인(규칙 기반)"
    P: list[str] = []
    P.append(f"<h1>{_e(res.project)}</h1><div class='muted'>생성 {_e(res.generated_at)} · v{_e(res.tool_version)} · 해석 엔진 {_e(llm_mode)}</div>")
    P.append("<h2>신뢰도 요약</h2><div class='grid'>")
    integ_cls = "PASS" if not ig.get("fail") else "FAIL"
    for title, val, sub, cls in [
        ("diff 무결성", f"{ig.get('pass')}/{ig.get('pairs')}", "원본+변경=수정본 재구성", integ_cls),
        ("검출 변경", f"{ch.get('content')}", f"내용 · 서식 {ch.get('format_only')}건 별도", ""),
        ("코드 추적률", pct(tr.get("ratio")), f"{tr.get('traced')}/{tr.get('content_changes')} 내용 변경", ""),
        ("검증된 권고", f"{rc.get('VERIFIED', 0)}", f"부분 {rc.get('PARTIAL', 0)} · 기각 {rc.get('REJECTED', 0)}", "VERIFIED"),
    ]:
        P.append(f"<div class='card'><div class='muted'>{_e(title)}</div><div class='kpi {cls}'>{_e(val)}</div><div class='muted'>{_e(sub)}</div></div>")
    P.append("</div>")
    P.append(f"<p class='muted'>변경 검출은 {_e(rel.get('detection'))}. LLM 은 검출된 변경의 해석·개선안 작성에만 쓰이며, 모든 권고는 근거 변경 ID·코드 인용·패치 적용 가능성을 결정론적으로 검증합니다.</p>")

    P.append("<h2>에이전트 요약</h2><div class='tw'><table><thead><tr><th>단계</th><th>에이전트</th><th>비교쌍</th><th>내용 변경</th><th>무결성</th><th>코드 추적률</th><th>검증/부분 권고</th></tr></thead><tbody>")
    for a in res.agents:
        k = agent_counts(a)
        P.append(f"<tr><td>{_e(a.stage)}</td><td><a href='#ag-{_e(a.agent_id)}'>{_e(a.name)}</a></td><td>{k['pairs']}</td><td>{k['content']}</td>"
                 f"<td class='{'PASS' if k['integrity_pass'] == k['pairs'] else 'FAIL'}'>{k['integrity_pass']}/{k['pairs']}</td><td>{pct(k['trace_ratio'])}</td><td>{k['verified']}/{k['partial']}</td></tr>")
    P.append("</tbody></table></div>")

    for a in res.agents:
        changes = [c for comp in a.comparisons for c in comp.changes]
        by_id = {c.id: c for c in changes}
        P.append(f"<h2 id='ag-{_e(a.agent_id)}'>{_e(a.name)} <span class='muted'>({_e(a.stage)})</span></h2>")
        P.append(f"<div class='muted'>코드 {_e(a.repo or '-')} @ {_e(a.ref or 'HEAD')} {('· 커밋 ' + _e(a.commit[:12])) if a.commit else ''} {('· ⚠ ' + _e(a.code_error)) if a.code_error else ''}</div>")
        cats = Counter(c.category for c in changes if not c.format_only)
        if cats:
            P.append("<p>" + " · ".join(f"<b>{_e(cat_label(k))}</b> {v}" for k, v in cats.most_common()) + "</p>")
        P.append("<h3>개선 권고</h3>")
        if not a.recommendations:
            P.append("<p class='muted'>권고 없음</p>")
        for r in a.recommendations:
            v = r.verification
            P.append(f"<div class='card rec'><h4><span class='badge {v.status}'>{_e(STATUS_LABEL[v.status])}</span> {_e(r.title)}</h4>")
            P.append(f"<div class='muted'>우선순위 {_e(PRIORITY_LABEL[r.priority])} · 신뢰도 {r.confidence:.2f} · {'규칙 기반' if r.source == 'rule' else 'GPT-6'} · {_e(r.id)}</div><dl>")
            P.append(f"<dt>문제</dt><dd>{_e(r.problem)}</dd><dt>권고</dt><dd>{_e(r.recommendation)}</dd>")
            if r.expected_effect:
                P.append(f"<dt>기대 효과</dt><dd>{_e(r.expected_effect)}</dd>")
            evs = []
            for cid in r.evidence_change_ids[:4]:
                c = by_id.get(cid)
                if c:
                    evs.append(f"<code>{_e(cid)}</code> {_e(c.location.path)} — {_e(change_brief(c))}")
            more = f" 외 {len(r.evidence_change_ids) - 4}건" if len(r.evidence_change_ids) > 4 else ""
            P.append(f"<dt>근거</dt><dd>{'<br>'.join(evs)}{more}</dd>")
            if r.code_refs:
                P.append("<dt>코드 위치</dt><dd>" + "<br>".join(f"<code>{_e(ref_text(x))}</code> {_e(x.note or '')}" for x in r.code_refs) + "</dd>")
            P.append(f"<dt>검증</dt><dd>{_e('; '.join(v.checks[:5]))}{(' / ⚠ ' + _e('; '.join(v.problems[:3]))) if v.problems else ''}"
                     f"{('<br>GPT 교차검증: ' + _e(v.llm_cross_check) + ' — ' + _e(v.llm_cross_check_reason)) if v.llm_cross_check else ''}</dd></dl>")
            if r.code_refs and r.code_refs[0].snippet:
                P.append(f"<details><summary>코드 스니펫</summary><pre><code>{_e(r.code_refs[0].snippet)}</code></pre></details>")
            for e in r.proposed_edits:
                if e.verified and e.patch:
                    P.append(f"<details open><summary>수정 패치 ({_e(e.file)})</summary>{_diff_html(e.patch)}</details>")
            P.append("</div>")
        pats = [p for p in a.patterns if p.category != "FORMAT_ONLY"]
        if pats:
            P.append("<h3>사용자 보완 패턴</h3><div class='tw'><table><thead><tr><th>패턴</th><th>유형</th><th>지원</th><th>건수</th><th>사용자 코드 근거</th></tr></thead><tbody>")
            for p in pats[:30]:
                uce = "<br>".join(_e(f"{r.file}:{r.line_start}") for r in p.user_code_evidence[:2]) or "-"
                P.append(f"<tr><td>{_e(p.title)}</td><td>{_e(cat_label(p.category))}</td><td>{len(p.support_pairs)}/{len(a.comparisons)}</td><td>{len(p.change_ids)}</td><td>{uce}</td></tr>")
            P.append("</tbody></table></div>")
        tid = f"t-{a.agent_id}"
        P.append(f"<h3>변경 목록</h3><input type='search' placeholder='검색 (위치, 내용, 유형…)' oninput=\"filt(this,'{_e(tid)}')\">")
        P.append(f"<div class='tw'><table id='{_e(tid)}'><thead><tr><th>ID</th><th>유형</th><th>위치</th><th>변경</th><th>보완 유형</th><th>코드 위치</th></tr></thead><tbody>")
        for c in sorted_changes(changes)[:500]:
            ref = ref_text(c.code_refs[0]) if c.code_refs else "추적 불가"
            fmt = " <span class='muted'>(서식)</span>" if c.format_only else ""
            P.append(f"<tr><td><code>{_e(c.id)}</code></td><td>{_e(CHANGE_TYPE_LABELS.get(c.change_type.value))}{fmt}</td><td>{_e(c.location.path)}</td>"
                     f"<td>{_e(change_brief(c))}</td><td>{_e(cat_label(c.category))}</td><td><code>{_e(ref)}</code></td></tr>")
        P.append("</tbody></table></div>")
        for d in a.code_diffs:
            if d.supplements:
                P.append(f"<h3>사용자 코드 보완 — {_e(d.label)}</h3><ul>")
                for s in d.supplements[:20]:
                    P.append(f"<li><code>{_e(s.file)}:{s.hunk_new_start}</code> {_e(s.symbol or '')} [{_e(s.kind)}] {_e(s.summary)}</li>")
                P.append("</ul>")
    if res.cross_agent_patterns:
        P.append("<h2>공통 보완 유형</h2><div class='tw'><table><thead><tr><th>유형</th><th>에이전트별</th><th>합계</th></tr></thead><tbody>")
        for cp in res.cross_agent_patterns:
            P.append(f"<tr><td>{_e(cp['label'])}</td><td>{_e(json.dumps(cp['agents'], ensure_ascii=False))}</td><td>{cp['total']}</td></tr>")
        P.append("</tbody></table></div>")
    return (
        "<!doctype html><html lang='ko'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>산출물 비교 분석</title><style>{CSS}</style></head><body><main>" + "".join(P) + f"</main><script>{JS}</script></body></html>"
    )
