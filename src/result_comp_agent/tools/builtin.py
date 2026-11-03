"""기본 GPT 도구 (모두 읽기 전용)."""
from __future__ import annotations

from ..diff import normalize as N
from ..documents import search_units, select_units
from . import DEFAULT_REGISTRY as R

S = {"type": "string"}
I = {"type": "integer"}


def _need_index(ctx):
    if ctx is None or ctx.index is None:
        raise RuntimeError("이 에이전트는 코드 인덱스가 없습니다 (repo 미설정 또는 로드 실패)")
    return ctx.index


@R.tool("search_code", "에이전트 코드(리터럴·프롬프트·설정·템플릿)에서 텍스트를 검색해 파일·줄·함수를 돌려준다. 컬럼명/섹션명/문구가 어디서 정의되는지 찾을 때 사용.",
        {"type": "object", "properties": {"query": S, "limit": I}, "required": ["query"]}, source="builtin")
def search_code(ctx, query: str, limit: int = 10):
    idx = _need_index(ctx)
    out = []
    for m in idx.find(query, substring=True, fuzzy=True, limit=max(1, min(int(limit or 10), 30))):
        out.append({"file": m.lit.file, "line": m.line, "symbol": m.lit.symbol, "kind": m.lit.kind, "method": m.method,
                    "score": m.score, "text": N.shorten(m.lit.value, 200), "template_location": m.lit.sub_location})
    return {"query": query, "results": out}


@R.tool("read_code", "코드 파일의 줄 범위를 줄 번호와 함께 읽는다 (최대 150줄). 인용(quote)은 여기서 본 그대로 사용할 것.",
        {"type": "object", "properties": {"file": S, "start_line": I, "end_line": I}, "required": ["file"]}, source="builtin")
def read_code(ctx, file: str, start_line: int = 1, end_line: int = 80):
    idx = _need_index(ctx)
    if file not in idx.files:
        cands = [f for f in idx.files if f.endswith(file) or file in f]
        if len(cands) == 1:
            file = cands[0]
        else:
            return {"error": f"파일 없음: {file}", "similar": cands[:10]}
    n = idx.line_count(file)
    a = max(1, int(start_line or 1))
    b = min(n, max(a, int(end_line or a + 79)), a + 149)
    return {"file": file, "total_lines": n, "start": a, "end": b,
            "code": "\n".join(f"{i:>5}| {idx.files[file][i - 1]}" for i in range(a, b + 1))}


@R.tool("list_code_files", "인덱싱된 에이전트 코드 파일 목록 (접두어로 필터).", {"type": "object", "properties": {"prefix": S}}, source="builtin")
def list_code_files(ctx, prefix: str = ""):
    idx = _need_index(ctx)
    files = [f for f in sorted(idx.files) if f.startswith(prefix or "")]
    return {"files": files[:200], "templates": sorted(idx.binary_files)[:50], "total": len(files)}


@R.tool("list_prompts", "에이전트 코드 안의 프롬프트(생성 지침) 목록과 위치.", {"type": "object", "properties": {}}, source="builtin")
def list_prompts(ctx):
    idx = _need_index(ctx)
    return {"prompts": [{"file": l.file, "line_start": l.line_start, "line_end": l.line_end, "symbol": l.symbol, "preview": N.shorten(l.value, 300)} for l in idx.prompts()]}


def _change_brief(c):
    return {"id": c.id, "type": c.change_type.value, "category": c.category, "location": c.location.path,
            "before": N.shorten(c.before, 200), "after": N.shorten(c.after, 300), "format_only": c.format_only}


@R.tool("get_change", "변경 1건의 전체 정보(위치·이전/이후 값·세부·코드 추적 결과).", {"type": "object", "properties": {"change_id": S}, "required": ["change_id"]}, source="builtin")
def get_change(ctx, change_id: str):
    c = ctx.changes.get(change_id)
    if c is None:
        return {"error": f"없는 change_id: {change_id}"}
    d = c.model_dump(mode="json", exclude={"detail"})
    d["detail"] = {k: v for k, v in c.detail.items() if k not in ("cells", "content")}
    if "cells" in c.detail:
        d["detail"]["cells_count"] = len(c.detail["cells"])
    for r in d.get("code_refs", []):
        r.pop("snippet", None)
    return d


@R.tool("list_changes", "변경 목록을 조건으로 조회 (category: CONTENT_ADDITION 등, change_type: cell_modified 등, text: 위치/내용 포함 문자열, pair_id).",
        {"type": "object", "properties": {"category": S, "change_type": S, "text": S, "pair_id": S, "include_format_only": {"type": "boolean"}, "limit": I}}, source="builtin")
def list_changes(ctx, category: str = "", change_type: str = "", text: str = "", pair_id: str = "", include_format_only: bool = False, limit: int = 30):
    out = []
    q = (text or "").casefold()
    for c in ctx.changes.values():
        if c.format_only and not include_format_only:
            continue
        if category and c.category != category:
            continue
        if change_type and c.change_type.value != change_type:
            continue
        if pair_id and c.pair_id != pair_id:
            continue
        if q and q not in f"{c.location.path} {c.before or ''} {c.after or ''}".casefold():
            continue
        out.append(_change_brief(c))
    lim = max(1, min(int(limit or 30), 100))
    return {"total": len(out), "changes": out[:lim]}


@R.tool("get_pattern", "반복 보완 패턴 1건 (지원 비교쌍·근거 변경·사용자 코드 근거).", {"type": "object", "properties": {"pattern_id": S}, "required": ["pattern_id"]}, source="builtin")
def get_pattern(ctx, pattern_id: str):
    p = next((x for x in ctx.patterns if x.id == pattern_id), None)
    if p is None:
        return {"error": f"없는 pattern_id: {pattern_id}", "available": [x.id for x in ctx.patterns[:30]]}
    return p.model_dump(mode="json")


@R.tool("read_document", "비교쌍 문서의 원본(baseline) 또는 수정본(revised) 내용을 위치로 읽는다. locator 예: 'Sheet1!3:10'(행), 'slide 3', '¶5', 섹션 제목 일부. 비우면 처음부터.",
        {"type": "object", "properties": {"pair_id": S, "side": {"type": "string", "enum": ["baseline", "revised"]}, "locator": S, "limit": I}, "required": ["pair_id", "side"]}, source="builtin")
def read_document(ctx, pair_id: str, side: str, locator: str = "", limit: int = 30):
    units = ctx.outline(pair_id, side)
    sel = select_units(units, locator, max(1, min(int(limit or 30), 80)))
    return {"pair_id": pair_id, "side": side, "total_units": len(units), "units": [{"locator": u.locator, "kind": u.kind, "text": N.shorten(u.text, 600)} for u in sel]}


@R.tool("search_documents", "이 에이전트의 모든 비교쌍 문서에서 텍스트를 검색 (side: baseline|revised|both).",
        {"type": "object", "properties": {"query": S, "side": {"type": "string", "enum": ["baseline", "revised", "both"]}, "limit": I}, "required": ["query"]}, source="builtin")
def search_documents(ctx, query: str, side: str = "both", limit: int = 20):
    sides = ["baseline", "revised"] if side == "both" else [side]
    out = []
    for comp in ctx.comparisons:
        for sd in sides:
            for hit in search_units(ctx.outline(comp.pair_id, sd), query, limit):
                out.append({"pair_id": comp.pair_id, "side": sd, **hit})
    return {"query": query, "results": out[: max(1, min(int(limit or 20), 60))], "total": len(out)}


@R.tool("get_code_diff", "사용자 보완 브랜치 등 코드 버전 비교 결과(hunk 의 추가/삭제 줄). label·file 로 필터.",
        {"type": "object", "properties": {"label": S, "file": S}}, source="builtin")
def get_code_diff(ctx, label: str = "", file: str = ""):
    out = []
    for d in ctx.code_diffs:
        if label and label not in d.label:
            continue
        files = []
        for f in d.files:
            if file and file not in f.path:
                continue
            files.append({"path": f.path, "status": f.status, "hunks": [
                {"new_start": h.new_start, "symbol": h.symbol, "added": [t for _, t in h.added][:40], "removed": [t for _, t in h.removed][:40]} for h in f.hunks[:20]]})
        out.append({"label": d.label, "base": d.base_ref, "head": d.head_ref, "error": d.error, "files": files})
    return {"diffs": out}
