"""흐름형 문서(Word·Markdown·텍스트·PDF) 공용 diff + 무결성 검증.

문서를 순서 있는 블록(제목/문단/목록/코드/표) 목록으로 보고
1) 텍스트 계열 블록과 표를 순서 보존 DP 로 정렬 (정확히 같은 블록은 앵커)
2) 대응 블록: 문단 수정(단어 단위 diff·수치 변화) / 제목·스타일만 변경(format_only) / 표는 grid 엔진
3) 미대응: 문단·제목 추가/삭제, 표 추가/삭제
4) 메모(주석) 추가/삭제
5) 재구성 검증: 원본 + 변경목록 = 수정본 (블록 종류·수준·텍스트, 표 셀까지)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Optional

from ..models import Anchor, Change, ChangeType, IntegrityReport, Location
from . import normalize as N
from .align import align_sequences, text_sim
from .builder import ChangeBuilder
from .grid import Grid, diff_grids, verify_grid

TEXT_KINDS = ("heading", "para", "list", "code")


@dataclass
class Block:
    kind: str  # heading | para | list | code | table
    text: str = ""
    level: int = 0
    grid: Optional[Grid] = None
    page: Optional[int] = None
    style: Optional[str] = None

    @property
    def is_table(self) -> bool:
        return self.kind == "table"

    def table_text(self) -> str:
        if self.grid is None:
            return ""
        return "\n".join(c.display for _, c in sorted(self.grid.cells.items()) if c.canon is not None)

    def sig(self) -> tuple:
        if self.is_table:
            return ("table", tuple(sorted(self.grid.all_canon().items())) if self.grid else ())
        return (self.kind, self.level, self.text)


@dataclass
class DocComment:
    text: str
    author: str = ""
    block_index: Optional[int] = None  # 주석이 달린 블록


@dataclass
class FlowDoc:
    path: str
    sha256: str
    blocks: list[Block] = field(default_factory=list)
    comments: list[DocComment] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def sections(self) -> list[Optional[str]]:
        """각 블록이 속한 가장 가까운 앞쪽 제목."""
        out, cur = [], None
        for b in self.blocks:
            if b.kind == "heading":
                cur = N.collapse_ws(b.text)
            out.append(cur)
        return out


def _group(b: Block) -> str:
    return "table" if b.is_table else "text"


def _sim(a: Block, b: Block) -> float:
    if _group(a) != _group(b):
        return 0.0
    if a.is_table:
        return text_sim(a.table_text()[:3000], b.table_text()[:3000])
    s = text_sim(a.text, b.text)
    if a.kind != b.kind:
        s *= 0.85
    return s


def _word_diff(a: str, b: str) -> dict:
    ta, tb = a.split(), b.split()
    ins, dele = [], []
    for op, i1, i2, j1, j2 in SequenceMatcher(None, ta, tb, autojunk=False).get_opcodes():
        if op in ("replace", "delete"):
            dele.append(" ".join(ta[i1:i2]))
        if op in ("replace", "insert"):
            ins.append(" ".join(tb[j1:j2]))
    return {"inserted": ins[:20], "deleted": dele[:20]}


KIND_LABEL = {"heading": "제목", "para": "문단", "list": "목록", "code": "코드", "table": "표"}


def diff_flow(base: FlowDoc, rev: FlowDoc, builder: ChangeBuilder, artifact: str) -> tuple[dict, IntegrityReport]:
    B, R = base.blocks, rev.blocks
    sec_b, sec_r = base.sections(), rev.sections()
    al = align_sequences(
        len(B), len(R), lambda i, j: _sim(B[i], R[j]), 0.5,
        key_a=[b.sig() for b in B], key_b=[b.sig() for b in R],
    )
    pairs = [(i, j, s) for i, j, s in al if i is not None and j is not None]
    # 표는 내용이 많이 바뀌어도 같은 표일 수 있어 임계값을 낮춰 한 번 더 대응
    un_b = [i for i, j, _ in al if j is None and B[i].is_table]
    un_r = [j for i, j, _ in al if i is None and R[j].is_table]
    for i in list(un_b):
        best = max(((_sim(B[i], R[j]), j) for j in un_r), default=(0.0, None))
        if best[1] is not None and best[0] >= 0.3:
            pairs.append((i, best[1], best[0]))
            un_r.remove(best[1])
    pairs.sort(key=lambda p: p[1])
    paired_b = {p[0] for p in pairs}
    paired_r = {p[1] for p in pairs}
    base_headings = [N.collapse_ws(b.text) for b in B if b.kind == "heading"]

    def loc(idx: int, side: str, part: str, extra: str = "", cell=None, row_label=None, column_label=None) -> Location:
        blocks = R if side == "r" else B
        secs = sec_r if side == "r" else sec_b
        b = blocks[idx]
        sec = secs[idx]
        page = f"p.{b.page} > " if b.page else ""
        where = f"{page}{('[' + N.shorten(sec, 40) + '] ') if sec else ''}{KIND_LABEL.get(b.kind, b.kind)} {idx + 1}{extra}"
        return Location(
            artifact=artifact, container=f"flow/block[{idx}]" if side == "r" else f"flow/base_block[{idx}]",
            path=("(원본) " if side == "b" else "") + where, part=part, section=sec, page=b.page,
            cell=cell, row_label=row_label, column_label=column_label,
        )

    def sec_anchor(sec: Optional[str]) -> list[Anchor]:
        if sec and 2 <= len(sec) <= 80:
            return [Anchor(text=sec, role="section")]
        return []

    def short(text: str, role: str) -> list[Anchor]:
        t = N.collapse_ws(text or "")
        return [Anchor(text=t, role=role)] if 2 <= len(t) <= 80 and N.parse_number(t) is None else []

    jobs = []
    for i, j, s in pairs:
        a, b = B[i], R[j]
        if b.is_table:
            g_b, g_r = a.grid, b.grid
            g_r.grid_id = f"flow/table[{j}]"
            g_b.grid_id = f"flow/base_table[{i}]"
            sec = sec_r[j]

            def make_loc(*, part, base_rc, rev_rc, row_label, col_label, _j=j):
                rc = rev_rc if rev_rc is not None else base_rc
                cell = f"R{rc[0]}C{rc[1]}" if rc and rc[0] is not None and rc[1] is not None else (f"{rc[1]}열" if rc and rc[1] else (f"{rc[0]}행" if rc and rc[0] else None))
                return loc(_j, "r", f"table_{part}", f" {cell or ''} [{col_label or '-'} / {row_label or '-'}]", cell, row_label, col_label)

            tal = diff_grids(g_b, g_r, builder, make_loc, context_anchors=sec_anchor(sec), compare_styles=False)
            jobs.append(("grid", g_b, g_r, tal))
            continue
        if a.sig() == b.sig():
            continue
        is_heading = "heading" in (a.kind, b.kind)
        kind = N.compare(N.canon(a.text), N.canon(b.text))
        fmt = kind in (None, "format")
        detail = {
            "flow_base_idx": i, "flow_rev_idx": j, "before_text": a.text, "after_text": b.text,
            "before_kind": a.kind, "after_kind": b.kind, "before_level": a.level, "after_level": b.level,
        }
        if a.text != b.text:
            detail.update(_word_diff(a.text, b.text))
            nb, na = N.numbers_in_text(a.text), N.numbers_in_text(b.text)
            if nb != na:
                detail["numbers_before"], detail["numbers_after"] = nb, na
        if a.kind != b.kind or a.level != b.level:
            detail["style_change"] = f"{KIND_LABEL.get(a.kind, a.kind)}{a.level or ''} → {KIND_LABEL.get(b.kind, b.kind)}{b.level or ''}"
        anchors = sec_anchor(sec_r[j]) + (short(a.text, "section") + short(b.text, "new_section") if is_heading else short(a.text, "value") + short(b.text, "new_value"))
        builder.add(
            ChangeType.TEXT_MODIFIED, loc(j, "r", "heading" if is_heading else "text"),
            before=a.text, after=b.text, format_only=fmt, pairing="exact" if s >= 0.9999 else "similarity", pairing_score=s,
            detail=detail, anchors=anchors,
        )

    rev_paired_headings = [(j, N.collapse_ws(R[j].text)) for j in range(len(R)) if j in paired_r and R[j].kind == "heading"]
    for j, b in enumerate(R):
        if j in paired_r:
            continue
        if b.is_table:
            cells = sorted(b.grid.all_canon().items()) if b.grid else []
            builder.add(
                ChangeType.TABLE_ADDED, loc(j, "r", "table"), after=N.shorten(b.table_text(), 2000),
                detail={"flow_rev_idx": j, "cells": [[r, c, v] for (r, c), v in cells]}, anchors=sec_anchor(sec_r[j]),
            )
            continue
        anchors = sec_anchor(sec_r[j])
        detail: dict[str, Any] = {"flow_rev_idx": j, "text": b.text, "kind": b.kind, "level": b.level}
        if b.kind == "heading":
            left = next((t for k, t in reversed(rev_paired_headings) if k < j), None)
            right = next((t for k, t in rev_paired_headings if k > j), None)
            anchors = short(b.text, "new_section") + short(left or "", "neighbor_left") + short(right or "", "neighbor_right")
            detail["sibling_titles"] = base_headings
        else:
            anchors += short(b.text, "new_value")
        builder.add(ChangeType.TEXT_ADDED, loc(j, "r", "heading" if b.kind == "heading" else "text"), after=b.text, detail=detail, anchors=anchors)
    for i, a in enumerate(B):
        if i in paired_b:
            continue
        if a.is_table:
            cells = sorted(a.grid.all_canon().items()) if a.grid else []
            builder.add(
                ChangeType.TABLE_REMOVED, loc(i, "b", "table"), before=N.shorten(a.table_text(), 2000),
                detail={"flow_base_idx": i, "cells": [[r, c, v] for (r, c), v in cells]}, anchors=sec_anchor(sec_b[i]),
            )
            continue
        builder.add(
            ChangeType.TEXT_REMOVED, loc(i, "b", "heading" if a.kind == "heading" else "text"), before=a.text,
            detail={"flow_base_idx": i, "text": a.text, "kind": a.kind, "level": a.level},
            anchors=sec_anchor(sec_b[i]) + short(a.text, "section" if a.kind == "heading" else "value"),
        )

    # ---- 메모(주석)
    def ckey(c: DocComment, doc: FlowDoc) -> tuple:
        anchor = doc.blocks[c.block_index].text[:60] if c.block_index is not None and c.block_index < len(doc.blocks) else ""
        return (N.collapse_ws(c.text), N.collapse_ws(anchor))

    base_keys = {ckey(c, base) for c in base.comments}
    rev_keys = {ckey(c, rev) for c in rev.comments}
    for c in rev.comments:
        if ckey(c, rev) in base_keys:
            continue
        idx = c.block_index if c.block_index is not None and c.block_index < len(R) else None
        l = loc(idx, "r", "comment") if idx is not None else Location(artifact=artifact, container="flow/comments", path="(문서 메모)", part="comment")
        builder.add(ChangeType.COMMENT_ADDED, l, after=c.text, detail={"author": c.author, "anchor_text": R[idx].text[:200] if idx is not None else ""},
                    anchors=sec_anchor(sec_r[idx]) if idx is not None else [])
    for c in base.comments:
        if ckey(c, base) in rev_keys:
            continue
        idx = c.block_index if c.block_index is not None and c.block_index < len(B) else None
        l = loc(idx, "b", "comment") if idx is not None else Location(artifact=artifact, container="flow/comments", path="(문서 메모)", part="comment")
        builder.add(ChangeType.COMMENT_REMOVED, l, before=c.text, detail={"author": c.author}, anchors=[])

    integrity = _verify(base, rev, pairs, jobs, builder.changes)
    alignment = {"blocks": [{"base": i, "rev": j, "score": round(s, 4)} for i, j, s in pairs], "base_blocks": len(B), "rev_blocks": len(R), "meta": {"base": base.meta, "rev": rev.meta}}
    return alignment, integrity


def _verify(base: FlowDoc, rev: FlowDoc, pairs, jobs, changes: list[Change]) -> IntegrityReport:
    B, R = base.blocks, rev.blocks
    checks, failures = 0, []
    rec: dict[int, tuple] = {}
    for i, j, _ in pairs:
        if not R[j].is_table:
            rec[j] = (B[i].kind, B[i].level, B[i].text)
    removed, added_tables, removed_tables = set(), {}, {}
    for ch in changes:
        d = ch.detail
        if ch.change_type == ChangeType.TEXT_MODIFIED and "flow_rev_idx" in d:
            checks += 1
            if B[d["flow_base_idx"]].text != d["before_text"]:
                failures.append(f"{ch.id}: 보고된 이전 문단이 원본과 다름")
            rec[d["flow_rev_idx"]] = (d["after_kind"], d["after_level"], d["after_text"])
        elif ch.change_type == ChangeType.TEXT_ADDED and "flow_rev_idx" in d:
            rec[d["flow_rev_idx"]] = (d["kind"], d["level"], d["text"])
        elif ch.change_type == ChangeType.TEXT_REMOVED and "flow_base_idx" in d:
            checks += 1
            removed.add(d["flow_base_idx"])
            if B[d["flow_base_idx"]].text != d["text"]:
                failures.append(f"{ch.id}: 삭제 보고 문단이 원본과 다름")
        elif ch.change_type == ChangeType.TABLE_ADDED:
            added_tables[d["flow_rev_idx"]] = {(r, c): v for r, c, v in d["cells"]}
        elif ch.change_type == ChangeType.TABLE_REMOVED:
            removed_tables[d["flow_base_idx"]] = {(r, c): v for r, c, v in d["cells"]}
    checks += 1
    expected = {j: (b.kind, b.level, b.text) for j, b in enumerate(R) if not b.is_table}
    if rec != expected:
        bad = [j for j in sorted(set(rec) | set(expected)) if rec.get(j) != expected.get(j)]
        failures.append(f"문단 재구성 불일치 {len(bad)}건 (블록 {', '.join(str(x + 1) for x in bad[:8])})")
    paired_b = {i for i, _, _ in pairs}
    paired_r = {j for _, j, _ in pairs}
    for i, b in enumerate(B):
        if i in paired_b:
            continue
        checks += 1
        if b.is_table:
            if removed_tables.get(i) != (b.grid.all_canon() if b.grid else {}):
                failures.append(f"원본 표(블록 {i + 1}) 삭제 보고 누락/불일치")
        elif i not in removed:
            failures.append(f"원본 블록 {i + 1} 이 대응도 삭제 보고도 없음")
    for j, b in enumerate(R):
        if j in paired_r or not b.is_table:
            continue
        checks += 1
        if added_tables.get(j) != (b.grid.all_canon() if b.grid else {}):
            failures.append(f"수정본 표(블록 {j + 1}) 추가 보고 누락/불일치")
    for job in jobs:
        _, gb, gr, tal = job
        c, f = verify_grid(gb, gr, tal, changes)
        checks += c
        failures.extend(f)
    return IntegrityReport(status="PASS" if not failures else "FAIL", checks=checks, failures=failures)
