"""여러 비교쌍(여러 사용자/프로젝트)에서 반복되는 보완 패턴 집계."""
from __future__ import annotations

from collections import defaultdict
from typing import Optional

from ..diff import normalize as N
from ..models import Anchor, ArtifactType, Change, ChangeType, ComparisonResult, Pattern
from .classifier import CATEGORIES


def _n(s: Optional[str]) -> str:
    return N.norm_for_match(s or "")


def pattern_key(ch: Change) -> tuple[str, str, str]:
    """(key, title, location_hint)"""
    ct = ch.change_type
    loc = ch.location
    cat = ch.category or "?"
    cat_label = CATEGORIES.get(cat, cat)
    if ch.change_type == ChangeType.SHEET_PROPERTY_CHANGED:
        from ..diff.excel_diff import PROP_LABEL

        scope = f"'{loc.sheet}' 시트"
        prop = ch.detail.get("property", "")
        action = {"added": "추가", "removed": "삭제", "changed": "변경"}.get(ch.detail.get("action"), "")
        desc = ch.after or ch.before or ""
        return (f"prop|{scope}|{prop}|{ch.detail.get('action')}|{_n(desc)}", f"{scope} {PROP_LABEL.get(prop, prop)} {action}: {N.shorten(desc, 60)}", scope)
    grid_like = ch.artifact in (ArtifactType.XLSX, ArtifactType.CSV) or (loc.part or "").startswith("table_")
    if grid_like:
        if ch.artifact in (ArtifactType.XLSX, ArtifactType.CSV):
            scope = f"'{loc.sheet}' 시트"
        elif ch.artifact == ArtifactType.PPTX:
            scope = f"슬라이드 '{loc.slide_title}' 표"
        else:
            scope = f"'{loc.section or '문서'}' 섹션 표"
        col = loc.column_label or "-"
        if ct == ChangeType.COLUMN_ADDED:
            return f"col_add|{scope}|{_n(ch.after)}", f"{scope}에 '{ch.after}' 열 추가", scope
        if ct == ChangeType.COLUMN_REMOVED:
            return f"col_del|{scope}|{_n(ch.before)}", f"{scope}에서 '{ch.before}' 열 삭제", scope
        if ct == ChangeType.CELL_MODIFIED and ch.detail.get("is_header"):
            return (f"hdr|{scope}|{_n(ch.before)}>{_n(ch.after)}", f"{scope} 컬럼명 '{ch.before}' → '{ch.after}'", scope)
        if ct == ChangeType.CELL_MODIFIED:
            return f"cell|{scope}|{_n(col)}|{cat}", f"{scope} '{col}' 열 — {cat_label}", f"{scope} / {col}"
        if ct in (ChangeType.ROW_ADDED, ChangeType.ROW_REMOVED, ChangeType.ROW_MOVED):
            lbl = {"row_added": "행 추가", "row_removed": "행 삭제", "row_moved": "행 순서 변경"}[ct.value]
            return f"{ct.value}|{scope}", f"{scope} {lbl}", scope
        if ct in (ChangeType.COMMENT_ADDED, ChangeType.COMMENT_MODIFIED, ChangeType.COMMENT_REMOVED):
            return f"comment|{scope}|{_n(col)}", f"{scope} '{col}' 셀 메모({ch.type_label})", f"{scope} / {col}"
        if ct == ChangeType.STYLE_MODIFIED:
            attrs = ",".join(sorted({d[0] for d in ch.detail.get("style_delta", [])}))
            return f"style|{scope}|{_n(col)}|{attrs}", f"{scope} '{col}' 서식 변경({attrs})", f"{scope} / {col}"
        if ct in (ChangeType.SHEET_ADDED, ChangeType.SHEET_REMOVED, ChangeType.SHEET_RENAMED):
            name = ch.after if ct != ChangeType.SHEET_REMOVED else ch.before
            return f"{ct.value}|{_n(name)}", f"시트 '{name}' {ch.type_label}", f"시트 {name}"
        return f"{ct.value}|{scope}|{_n(col)}", f"{scope} {ch.type_label}", scope
    # PPT / 흐름형 문서(Word·Markdown·텍스트·PDF)
    title = loc.slide_title or loc.section or (f"p.{loc.page}" if loc.page else "문서")
    if ct in (ChangeType.TEXT_ADDED, ChangeType.TEXT_REMOVED) and (loc.part or "") == "heading":
        name = ch.after if ct == ChangeType.TEXT_ADDED else ch.before
        k = "section_add" if ct == ChangeType.TEXT_ADDED else "section_del"
        return f"{k}|{_n(name)}", f"'{N.shorten(name, 40)}' 섹션 {'추가' if k == 'section_add' else '삭제'}", f"섹션 '{N.shorten(name, 40)}'"
    if ct in (ChangeType.COMMENT_ADDED, ChangeType.COMMENT_MODIFIED, ChangeType.COMMENT_REMOVED):
        return f"comment|{_n(title)}", f"'{title}' 메모({ch.type_label})", f"'{title}'"
    if ct == ChangeType.SLIDE_ADDED:
        t = (ch.detail.get("content") or {}).get("title") or title
        return f"slide_add|{_n(t)}", f"'{t}' 슬라이드 추가", f"슬라이드 '{t}'"
    if ct == ChangeType.SLIDE_REMOVED:
        return f"slide_del|{_n(title)}", f"'{title}' 슬라이드 삭제", f"슬라이드 '{title}'"
    if ct in (ChangeType.TABLE_ADDED, ChangeType.TABLE_REMOVED):
        return f"{ct.value}|{_n(title)}", f"'{title}' {ch.type_label}", f"'{title}'"
    if ct == ChangeType.SLIDE_MOVED:
        return f"slide_move|{_n(title)}", f"'{title}' 슬라이드 순서 변경", f"슬라이드 '{title}'"
    if (loc.part or "") == "notes":
        return f"notes|{_n(title)}", f"'{title}' 발표자 노트 {ch.type_label}", f"슬라이드 '{title}' 노트"
    hint = f"슬라이드 '{title}'" if ch.artifact == ArtifactType.PPTX else f"'{title}' 섹션"
    return f"{ct.value}|{_n(title)}|{loc.part}|{cat}", f"{hint} {ch.type_label} ({cat_label})", hint


def aggregate(agent_id: str, comparisons: list[ComparisonResult]) -> list[Pattern]:
    n_pairs = len(comparisons)
    groups: dict[str, list[Change]] = defaultdict(list)
    meta: dict[str, tuple[str, str]] = {}
    for comp in comparisons:
        for ch in comp.changes:
            key, title, hint = pattern_key(ch)
            groups[key].append(ch)
            meta.setdefault(key, (title, hint))
    patterns: list[Pattern] = []
    for key, chs in groups.items():
        pairs = sorted({c.pair_id for c in chs})
        cats = defaultdict(int)
        for c in chs:
            cats[c.category or "?"] += 1
        cat = max(cats.items(), key=lambda kv: kv[1])[0]
        anchors: list[Anchor] = []
        seen = set()
        for c in chs:
            for a in c.anchors:
                k = (a.text, a.role)
                if k not in seen and a.role not in ("value", "new_value"):
                    seen.add(k)
                    anchors.append(a)
        examples = []
        for c in chs[:3]:
            examples.append({"change_id": c.id, "path": c.location.path, "before": N.shorten(c.before, 200), "after": N.shorten(c.after, 300)})
        title, hint = meta[key]
        patterns.append(
            Pattern(
                id="",
                agent_id=agent_id,
                key=key,
                title=title,
                change_type=chs[0].change_type.value,
                category=cat,
                location_hint=hint,
                support_pairs=pairs,
                support_ratio=round(len(pairs) / n_pairs, 4) if n_pairs else 0.0,
                change_ids=[c.id for c in chs],
                examples=examples,
                anchors=anchors[:8],
            )
        )
    patterns.sort(key=lambda p: (p.category == "FORMAT_ONLY", -p.support_ratio, -len(p.change_ids), p.key))
    for i, p in enumerate(patterns, start=1):
        p.id = f"{agent_id}:P{i:03d}"
    return patterns


def cross_agent_patterns(agent_patterns: dict[str, list[Pattern]], agent_stage: dict[str, str]) -> list[dict]:
    """여러 에이전트에 공통으로 나타나는 보완 유형 (공통 모듈/공통 프롬프트 개선 후보)."""
    by_cat: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for aid, pats in agent_patterns.items():
        for p in pats:
            if p.category == "FORMAT_ONLY":
                continue
            by_cat[p.category][aid] += len(p.change_ids)
    out = []
    for cat, per_agent in by_cat.items():
        if len(per_agent) >= 2:
            out.append(
                {
                    "category": cat,
                    "label": CATEGORIES.get(cat, cat),
                    "agents": {a: n for a, n in sorted(per_agent.items(), key=lambda kv: agent_stage.get(kv[0], kv[0]))},
                    "total": sum(per_agent.values()),
                }
            )
    out.sort(key=lambda d: (-len(d["agents"]), -d["total"]))
    return out
