"""2차원 표(엑셀 시트 / PPT 표) 공용 diff 엔진.

절차
1. 헤더 행 탐지 (설정으로 강제 가능)
2. 열 정렬: 헤더 텍스트 유사도 + 열 내용(Jaccard) 유사도, 순서 보존 DP
3. 행 정렬: (a) ID 성격의 키 열이 있으면 키로 1:1 대응  (b) 없으면 행 내용 유사도 DP
4. 대응된 셀끼리 값 비교 → 셀 수정 / 추가·삭제 행과 열은 통째로 보고
5. 메모(코멘트)·서식 변경 보고
6. ``verify_grid`` 로 "원본 + 변경목록 = 수정본" 재구성 검증 (무결성)
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Callable, Optional

from openpyxl.utils import get_column_letter

from ..models import Anchor, Change, ChangeType, Location
from . import normalize as N
from .align import align_sequences, jaccard, longest_increasing_subsequence, quick_text_sim, text_sim
from .builder import ChangeBuilder

STYLE_FIELDS = ("bold", "italic", "underline", "font_color", "fill", "number_format")

_ID_HDR = re.compile(r"(?i)(\bid\b|id$|^id|\bno\.?$|^no\.?\b|번호|코드|\bcode\b|\bkey\b|식별|^#$)")
_ID_VAL = re.compile(r"^[A-Za-z가-힣]{0,12}[-_ ]?\d+(?:[-_.]\d+)*$")


@dataclass
class GridCell:
    canon: Optional[str]
    display: str = ""
    cached: Optional[str] = None
    comment: Optional[str] = None
    style: Optional[tuple] = None


@dataclass
class Grid:
    grid_id: str
    name: str
    cells: dict[tuple[int, int], GridCell] = field(default_factory=dict)
    is_excel: bool = True
    header_row: Optional[int] = None  # 강제 지정 (None = 자동 탐지)
    key_column: Optional[str] = None  # 강제 지정할 키 열 헤더명

    def value(self, r: int, c: int) -> Optional[str]:
        cell = self.cells.get((r, c))
        return cell.canon if cell else None

    def rows(self) -> list[int]:
        return sorted({r for (r, _), v in self.cells.items() if v.canon is not None})

    def cols(self) -> list[int]:
        return sorted({c for (_, c), v in self.cells.items() if v.canon is not None})

    def addr(self, r: int, c: int) -> str:
        return f"{get_column_letter(c)}{r}" if self.is_excel else f"R{r}C{c}"

    def col_name(self, c: int) -> str:
        return get_column_letter(c) if self.is_excel else f"{c}열"

    def row_cells(self, r: int) -> dict[int, str]:
        return {c: v.canon for (rr, c), v in self.cells.items() if rr == r and v.canon is not None}

    def all_canon(self) -> dict[tuple[int, int], str]:
        return {k: v.canon for k, v in self.cells.items() if v.canon is not None}


@dataclass
class GridAlignment:
    grid_id: str
    base_grid_id: str
    row_pairs: list[tuple[int, int]] = field(default_factory=list)
    col_pairs: list[tuple[int, int]] = field(default_factory=list)
    header_base: Optional[int] = None
    header_rev: Optional[int] = None
    key_column: Optional[str] = None
    row_method: dict[int, tuple[str, float]] = field(default_factory=dict)  # rev_row -> (method, score)
    moved_rows: list[tuple[int, int]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "grid_id": self.grid_id,
            "base_grid_id": self.base_grid_id,
            "header_base": self.header_base,
            "header_rev": self.header_rev,
            "key_column": self.key_column,
            "row_pairs": [list(p) for p in self.row_pairs],
            "col_pairs": [list(p) for p in self.col_pairs],
            "row_method": {str(k): list(v) for k, v in self.row_method.items()},
        }


LocFactory = Callable[..., Location]


# --------------------------------------------------------------------------- 탐지 유틸
def detect_header_row(grid: Grid) -> Optional[int]:
    if grid.header_row is not None:
        return grid.header_row
    rows = grid.rows()
    if len(rows) < 2:
        return None
    best, best_n = None, 0
    for r in rows[:10]:
        if r == rows[-1]:
            break
        vals = [v for v in grid.row_cells(r).values()]
        n = len(vals)
        if n < 2:
            continue
        n_str = sum(1 for v in vals if v.startswith("s:") and N.parse_number(N.body(v)) is None)
        if n_str / n < 0.6:
            continue
        if n > best_n:
            best, best_n = r, n
    return best


def _header_text(grid: Grid, hdr: Optional[int], c: int) -> Optional[str]:
    if hdr is None:
        return None
    v = grid.value(hdr, c)
    if v is None:
        return None
    return N.collapse_ws(N.display(v)) or None


# --------------------------------------------------------------------------- 메인
def diff_grids(
    base: Grid,
    rev: Grid,
    builder: ChangeBuilder,
    make_loc: LocFactory,
    *,
    context_anchors: list[Anchor] | None = None,
    compare_styles: bool = True,
) -> GridAlignment:
    ctx_anchors = list(context_anchors or [])
    al = GridAlignment(grid_id=rev.grid_id, base_grid_id=base.grid_id)

    b_rows_all, r_rows_all = base.rows(), rev.rows()
    hb, hr = detect_header_row(base), detect_header_row(rev)
    if (hb is None) != (hr is None):
        # 한쪽만 탐지되면 같은 행 번호가 다른 쪽에도 존재할 때만 공유
        if hb is not None and hb in r_rows_all:
            hr = hb
        elif hr is not None and hr in b_rows_all:
            hb = hr
        else:
            hb = hr = None
    al.header_base, al.header_rev = hb, hr

    def data_rows(rows: list[int], h: Optional[int]) -> list[int]:
        return [r for r in rows if h is None or r > h]

    def pre_rows(rows: list[int], h: Optional[int]) -> list[int]:
        return [r for r in rows if h is not None and r < h]

    b_data, r_data = data_rows(b_rows_all, hb), data_rows(r_rows_all, hr)

    # ---------------- 열 정렬
    b_cols, r_cols = base.cols(), rev.cols()
    hdr_b = {c: _header_text(base, hb, c) for c in b_cols}
    hdr_r = {c: _header_text(rev, hr, c) for c in r_cols}
    content_b = {c: Counter(v for r in b_data if (v := base.value(r, c)) is not None) for c in b_cols}
    content_r = {c: Counter(v for r in r_data if (v := rev.value(r, c)) is not None) for c in r_cols}

    def col_sim(i: int, j: int) -> float:
        cb, cr = b_cols[i], r_cols[j]
        ha, hbt = hdr_b[cb], hdr_r[cr]
        ca, cr_ = content_b[cb], content_r[cr]
        cs = jaccard(ca, cr_) if ca and cr_ else 0.0
        if ha and hbt:
            if ha == hbt:
                return 1.0
            return max(text_sim(ha, hbt), 0.85 * cs)
        return 0.9 * cs

    def col_key(grid_hdr: dict, content: dict, c: int):
        h = grid_hdr[c]
        if h:
            return ("h", h)
        return ("c", tuple(sorted(content[c].items())))

    col_al = align_sequences(
        len(b_cols),
        len(r_cols),
        col_sim,
        0.45,
        key_a=[col_key(hdr_b, content_b, c) for c in b_cols],
        key_b=[col_key(hdr_r, content_r, c) for c in r_cols],
    )
    col_pairs = [(b_cols[i], r_cols[j]) for i, j, _ in col_al if i is not None and j is not None]
    # 순서가 바뀐 열: 헤더가 정확히 같은 미대응 열끼리 연결
    moved_cols: list[tuple[int, int]] = []
    un_b = [b_cols[i] for i, j, _ in col_al if j is None]
    un_r = [r_cols[j] for i, j, _ in col_al if i is None]
    for cb in list(un_b):
        h = hdr_b.get(cb)
        if not h:
            continue
        for cr in un_r:
            if hdr_r.get(cr) == h:
                col_pairs.append((cb, cr))
                moved_cols.append((cb, cr))
                un_r.remove(cr)
                break
    col_pairs.sort(key=lambda p: p[1])
    al.col_pairs = col_pairs
    col_map = dict(col_pairs)
    col_map_rev = {r: b for b, r in col_pairs}

    # ---------------- 행 정렬
    row_pairs: list[tuple[int, int]] = []
    row_method: dict[int, tuple[str, float]] = {}

    def row_sim_fn(b_list: list[int], r_list: list[int]):
        def f(i: int, j: int) -> float:
            br, rr = b_list[i], r_list[j]
            eq, union = 0.0, 0
            for bc, rc in col_pairs:
                a = base.value(br, bc)
                b = rev.value(rr, rc)
                if a is None and b is None:
                    continue
                union += 1
                if a == b:
                    eq += 1
                elif a is not None and b is not None:
                    eq += 0.5 * quick_text_sim(a, b)
            return eq / union if union else 0.0

        return f

    def row_key(grid: Grid, r: int, side: str):
        return tuple(grid.value(r, bc if side == "b" else rc) for bc, rc in col_pairs)

    def dp_rows(b_list: list[int], r_list: list[int], method: str) -> tuple[list[int], list[int]]:
        res = align_sequences(
            len(b_list),
            len(r_list),
            row_sim_fn(b_list, r_list),
            0.5,
            key_a=[row_key(base, r, "b") for r in b_list],
            key_b=[row_key(rev, r, "r") for r in r_list],
        )
        lb, lr = [], []
        for i, j, s in res:
            if i is not None and j is not None:
                row_pairs.append((b_list[i], r_list[j]))
                row_method[r_list[j]] = ("exact" if s >= 0.9999 else method, s)
            elif i is not None:
                lb.append(b_list[i])
            else:
                lr.append(r_list[j])
        return lb, lr

    # 헤더 위쪽(제목/메타 행)
    left_b, left_r = dp_rows(pre_rows(b_rows_all, hb), pre_rows(r_rows_all, hr), "similarity")
    # 헤더 행
    if hb is not None and hr is not None:
        row_pairs.append((hb, hr))
        row_method[hr] = ("header", 1.0)

    key = _detect_key(base, rev, b_data, r_data, col_pairs, hdr_b)
    if key is not None:
        kb, kr = key
        al.key_column = hdr_b.get(kb) or base.col_name(kb)
        b_by_key = {base.value(r, kb): r for r in b_data if base.value(r, kb) is not None}
        r_by_key = {rev.value(r, kr): r for r in r_data if rev.value(r, kr) is not None}
        keyed = []
        for k, br in b_by_key.items():
            if k in r_by_key:
                keyed.append((br, r_by_key[k]))
                row_method[r_by_key[k]] = ("key", 1.0)
        row_pairs.extend(keyed)
        pb = {b for b, _ in keyed}
        pr = {r for _, r in keyed}
        lb2, lr2 = dp_rows([r for r in b_data if r not in pb], [r for r in r_data if r not in pr], "similarity")
        left_b += lb2
        left_r += lr2
    else:
        lb2, lr2 = dp_rows(b_data, r_data, "similarity")
        left_b += lb2
        left_r += lr2

    # 이동된 행(내용이 완전히 같은 미대응 행끼리)
    moved_rows: list[tuple[int, int]] = []
    if left_b and left_r:
        r_key_index = defaultdict(list)
        for rr in left_r:
            r_key_index[row_key(rev, rr, "r")].append(rr)
        for br in list(left_b):
            k = row_key(base, br, "b")
            if any(v is not None for v in k) and r_key_index.get(k):
                rr = r_key_index[k].pop(0)
                row_pairs.append((br, rr))
                row_method[rr] = ("moved", 1.0)
                moved_rows.append((br, rr))
                left_b.remove(br)
                left_r.remove(rr)

    row_pairs.sort(key=lambda p: p[1])
    # 키/이동 기반 대응에서 순서 바뀐 행 탐지 (LIS 밖에 있는 행)
    data_pairs = [p for p in row_pairs if (hb is None or p[0] > hb)]
    by_base = sorted(data_pairs, key=lambda p: p[0])
    lis = longest_increasing_subsequence([p[1] for p in by_base])
    moved_set = {by_base[i] for i in range(len(by_base)) if i not in lis}
    al.row_pairs = row_pairs
    al.row_method = row_method
    al.moved_rows = sorted(moved_set, key=lambda p: p[1])
    row_map = dict(row_pairs)
    paired_rev_rows = {r for _, r in row_pairs}

    # ---------------- 라벨 함수
    key_cols = key

    def row_label(grid: Grid, r: int, side: str) -> str:
        if r == (hb if side == "b" else hr):
            return "(헤더)"
        if key_cols is not None:
            v = grid.value(r, key_cols[0] if side == "b" else key_cols[1])
            if v is not None:
                return N.shorten(N.display(v), 40)
        rc = grid.row_cells(r)
        for c in sorted(rc):
            d = N.collapse_ws(N.display(rc[c]))
            if d:
                return N.shorten(d, 40)
        return f"{r}행"

    def col_label(grid: Grid, c: int, side: str) -> str:
        h = (hdr_b if side == "b" else hdr_r).get(c)
        return h if h else grid.col_name(c)

    def cell_anchors(c_label: str, before: Optional[str], after: Optional[str], header_present: bool) -> list[Anchor]:
        res = list(ctx_anchors)
        if header_present:
            res.append(Anchor(text=c_label, role="column"))
        for txt, role in ((before, "value"), (after, "new_value")):
            if txt and 2 <= len(txt) <= 80 and N.parse_number(txt) is None:
                res.append(Anchor(text=txt, role=role))
        return res

    gid = rev.grid_id

    # ---------------- 1) 열 구조 변경
    for cb in sorted(set(b_cols) - set(col_map)):
        cells = [[br, base.value(br, cb)] for br, _ in row_pairs if base.value(br, cb) is not None]
        h = hdr_b.get(cb)
        builder.add(
            ChangeType.COLUMN_REMOVED,
            make_loc(part="column", base_rc=(None, cb), rev_rc=None, row_label=None, col_label=col_label(base, cb, "b")),
            before=h or base.col_name(cb),
            format_only=False,
            detail={
                "grid_id": gid,
                "base_col": cb,
                "header": h,
                "cells": cells,
                "values": [{"row": row_label(base, br, "b"), "value": N.display(v)} for br, v in cells],
            },
            anchors=ctx_anchors + ([Anchor(text=h, role="column")] if h else []),
        )
    rev_headers_ordered = [(c, hdr_r.get(c)) for c in r_cols]
    for cr in sorted(set(r_cols) - set(col_map_rev)):
        cells = [[rr, rev.value(rr, cr)] for _, rr in row_pairs if rev.value(rr, cr) is not None]
        h = hdr_r.get(cr)
        left = next((hdr_r.get(c) for c in reversed(r_cols) if c < cr and c in col_map_rev and hdr_r.get(c)), None)
        right = next((hdr_r.get(c) for c in r_cols if c > cr and c in col_map_rev and hdr_r.get(c)), None)
        anchors = list(ctx_anchors)
        if h:
            anchors.append(Anchor(text=h, role="new_column"))
        if left:
            anchors.append(Anchor(text=left, role="neighbor_left"))
        if right:
            anchors.append(Anchor(text=right, role="neighbor_right"))
        builder.add(
            ChangeType.COLUMN_ADDED,
            make_loc(part="column", base_rc=None, rev_rc=(None, cr), row_label=None, col_label=col_label(rev, cr, "r")),
            after=h or rev.col_name(cr),
            detail={
                "grid_id": gid,
                "rev_col": cr,
                "header": h,
                "cells": cells,
                "values": [{"row": row_label(rev, rr, "r"), "value": N.display(v)} for rr, v in cells],
                "sibling_headers": [hdr_b[c] for c in b_cols if hdr_b.get(c)],
                "rev_headers": [h2 for _, h2 in rev_headers_ordered if h2],
            },
            anchors=anchors,
        )
    for cb, cr in moved_cols:
        h = hdr_b.get(cb)
        builder.add(
            ChangeType.COLUMN_MOVED,
            make_loc(part="column", base_rc=(None, cb), rev_rc=(None, cr), row_label=None, col_label=h or rev.col_name(cr)),
            before=base.col_name(cb),
            after=rev.col_name(cr),
            pairing="moved",
            detail={"grid_id": gid, "base_col": cb, "rev_col": cr, "header": h},
            anchors=ctx_anchors + ([Anchor(text=h, role="column")] if h else []),
        )

    # ---------------- 2) 대응 행의 셀 비교 + 3) 추가 행
    moved_lookup = {p[1]: p[0] for p in al.moved_rows}
    inv_rows = {r: b for b, r in row_pairs}
    for rr in sorted(set(r_rows_all) | paired_rev_rows):
        if rr in paired_rev_rows:
            br = inv_rows[rr]
            method, score = row_method.get(rr, ("similarity", 0.0))
            is_header = hb is not None and br == hb and rr == hr
            if rr in moved_lookup:
                builder.add(
                    ChangeType.ROW_MOVED,
                    make_loc(part="row", base_rc=(br, None), rev_rc=(rr, None), row_label=row_label(rev, rr, "r"), col_label=None),
                    before=f"{br}행",
                    after=f"{rr}행",
                    pairing=method,
                    pairing_score=score,
                    detail={"grid_id": gid, "base_row": br, "rev_row": rr},
                    anchors=ctx_anchors,
                )
            for bc, rc in col_pairs:
                a = base.value(br, bc)
                b = rev.value(rr, rc)
                kind = N.compare(a, b)
                if kind is None:
                    continue
                c_label = col_label(rev, rc, "r") if not is_header else (hdr_b.get(bc) or rev.col_name(rc))
                detail = {
                    "grid_id": gid,
                    "base_rc": [br, bc],
                    "rev_rc": [rr, rc],
                    "before_canon": a,
                    "after_canon": b,
                    "is_header": is_header,
                }
                if (a or "").startswith("f:") or (b or "").startswith("f:"):
                    detail["formula"] = True
                    if N.formula_shape(a) is not None and N.formula_shape(a) == N.formula_shape(b):
                        detail["formula_ref_shift"] = True
                na, nb = N.numeric_value(a), N.numeric_value(b)
                if na is not None and nb is not None:
                    detail["numeric_delta"] = nb - na
                    if na != 0:
                        detail["numeric_delta_pct"] = round((nb - na) / abs(na) * 100, 4)
                bdisp, adisp = N.display(a) or None, N.display(b) or None
                if is_header:
                    anchors = list(ctx_anchors)
                    if bdisp:
                        anchors.append(Anchor(text=N.collapse_ws(bdisp), role="column"))
                    if adisp:
                        anchors.append(Anchor(text=N.collapse_ws(adisp), role="new_column"))
                else:
                    anchors = cell_anchors(c_label, bdisp, adisp, hdr_r.get(rc) is not None or hdr_b.get(bc) is not None)
                builder.add(
                    ChangeType.CELL_MODIFIED,
                    make_loc(
                        part="header" if is_header else "cell",
                        base_rc=(br, bc),
                        rev_rc=(rr, rc),
                        row_label=row_label(rev, rr, "r"),
                        col_label=c_label,
                    ),
                    before=bdisp,
                    after=adisp,
                    format_only=(kind == "format"),
                    pairing=method,
                    pairing_score=score,
                    detail=detail,
                    anchors=anchors,
                )
        else:
            cells = sorted(rev.row_cells(rr).items())
            values = {col_label(rev, c, "r"): N.display(v) for c, v in cells}
            builder.add(
                ChangeType.ROW_ADDED,
                make_loc(part="row", base_rc=None, rev_rc=(rr, None), row_label=row_label(rev, rr, "r"), col_label=None),
                after=" | ".join(f"{k}: {N.shorten(v, 60)}" for k, v in values.items()),
                detail={"grid_id": gid, "rev_row": rr, "cells": [[c, v] for c, v in cells], "values": values},
                anchors=ctx_anchors,
            )

    # ---------------- 4) 삭제 행
    paired_base_rows = set(row_map)
    for br in b_rows_all:
        if br in paired_base_rows:
            continue
        cells = sorted(base.row_cells(br).items())
        values = {col_label(base, c, "b"): N.display(v) for c, v in cells}
        anchors = list(ctx_anchors)
        for _, v in cells[:4]:
            d = N.collapse_ws(N.display(v))
            if 2 <= len(d) <= 80 and N.parse_number(d) is None:
                anchors.append(Anchor(text=d, role="value"))
        builder.add(
            ChangeType.ROW_REMOVED,
            make_loc(part="row", base_rc=(br, None), rev_rc=None, row_label=row_label(base, br, "b"), col_label=None),
            before=" | ".join(f"{k}: {N.shorten(v, 60)}" for k, v in values.items()),
            detail={"grid_id": gid, "base_row": br, "cells": [[c, v] for c, v in cells], "values": values},
            anchors=anchors,
        )

    # ---------------- 5) 메모(코멘트)
    _diff_comments(base, rev, row_map, col_map, builder, make_loc, gid, ctx_anchors, row_label, col_label)

    # ---------------- 6) 서식
    if compare_styles:
        _diff_styles(base, rev, row_pairs, col_pairs, builder, make_loc, gid, ctx_anchors, col_label, row_label)

    return al


def _detect_key(base: Grid, rev: Grid, b_rows, r_rows, col_pairs, hdr_b) -> Optional[tuple[int, int]]:
    if len(b_rows) < 2 or len(r_rows) < 2:
        return None
    forced = base.key_column or rev.key_column
    best, best_score = None, -1.0
    for bc, rc in col_pairs:
        name = hdr_b.get(bc) or ""
        if forced:
            if N.collapse_ws(name) == N.collapse_ws(forced):
                return (bc, rc)
            continue
        bv = [base.value(r, bc) for r in b_rows]
        rv = [rev.value(r, rc) for r in r_rows]

        def uniq_ok(vals):
            ne = [v for v in vals if v is not None]
            return len(ne) >= 2 and len(ne) >= 0.8 * len(vals) and len(set(ne)) == len(ne)

        if not (uniq_ok(bv) and uniq_ok(rv)):
            continue
        bset = {v for v in bv if v is not None}
        rset = {v for v in rv if v is not None}
        overlap = len(bset & rset) / max(1, len(bset))
        if overlap < 0.5:
            continue
        hint = bool(_ID_HDR.search(name))
        idlike = sum(1 for v in bset if _ID_VAL.match(N.collapse_ws(N.display(v)))) / max(1, len(bset)) >= 0.9
        if not (hint or idlike):
            continue
        score = 2.0 * hint + 1.0 * idlike + overlap - 0.001 * bc
        if score > best_score:
            best, best_score = (bc, rc), score
    return best


def _diff_comments(base, rev, row_map, col_map, builder, make_loc, gid, ctx_anchors, row_label, col_label):
    inv_row = {r: b for b, r in row_map.items()}
    inv_col = {r: b for b, r in col_map.items()}
    seen_base = set()
    for (rr, rc), cell in sorted(rev.cells.items()):
        if not cell.comment:
            continue
        br, bc = inv_row.get(rr), inv_col.get(rc)
        bcell = base.cells.get((br, bc)) if br is not None and bc is not None else None
        if bcell is not None:
            seen_base.add((br, bc))
        bcomment = bcell.comment if bcell else None
        if bcomment == cell.comment:
            continue
        ct = ChangeType.COMMENT_MODIFIED if bcomment else ChangeType.COMMENT_ADDED
        builder.add(
            ct,
            make_loc(part="cell", base_rc=(br, bc) if bcell else None, rev_rc=(rr, rc), row_label=row_label(rev, rr, "r"), col_label=col_label(rev, rc, "r")),
            before=bcomment,
            after=cell.comment,
            detail={"grid_id": gid, "rev_rc": [rr, rc], "cell_value": cell.display},
            anchors=ctx_anchors + ([Anchor(text=col_label(rev, rc, "r"), role="column")]),
        )
    for (br, bc), cell in sorted(base.cells.items()):
        if not cell.comment or (br, bc) in seen_base:
            continue
        rr, rc = row_map.get(br), col_map.get(bc)
        rcell = rev.cells.get((rr, rc)) if rr is not None and rc is not None else None
        if rcell is not None and rcell.comment:
            continue
        builder.add(
            ChangeType.COMMENT_REMOVED,
            make_loc(part="cell", base_rc=(br, bc), rev_rc=(rr, rc) if rcell else None, row_label=row_label(base, br, "b"), col_label=col_label(base, bc, "b")),
            before=cell.comment,
            detail={"grid_id": gid, "base_rc": [br, bc]},
            anchors=ctx_anchors,
        )


def _diff_styles(base, rev, row_pairs, col_pairs, builder, make_loc, gid, ctx_anchors, col_label, row_label):
    groups: dict[tuple, list[tuple[int, int, int, int]]] = defaultdict(list)
    for br, rr in row_pairs:
        for bc, rc in col_pairs:
            a = base.cells.get((br, bc))
            b = rev.cells.get((rr, rc))
            if not a or not b or a.canon is None or b.canon is None:
                continue
            if a.style is None or b.style is None or a.style == b.style:
                continue
            delta = tuple(
                (STYLE_FIELDS[i], a.style[i], b.style[i]) for i in range(min(len(a.style), len(b.style))) if a.style[i] != b.style[i]
            )
            if delta:
                groups[(rc, delta)].append((br, bc, rr, rc))
    for (rc, delta), cells in sorted(groups.items(), key=lambda kv: (kv[0][0], str(kv[0][1]))):
        first = cells[0]
        desc = ", ".join(f"{f}: {x} → {y}" for f, x, y in delta)
        builder.add(
            ChangeType.STYLE_MODIFIED,
            make_loc(part="column", base_rc=(first[0], first[1]), rev_rc=(first[2], first[3]), row_label=row_label(rev, first[2], "r"), col_label=col_label(rev, rc, "r")),
            before=None,
            after=desc,
            format_only=True,
            detail={
                "grid_id": gid,
                "style_delta": [list(d) for d in delta],
                "cells": [rev.addr(c[2], c[3]) for c in cells],
                "count": len(cells),
            },
            anchors=ctx_anchors,
        )


# --------------------------------------------------------------------------- 무결성 검증
def verify_grid(base: Grid, rev: Grid, al: GridAlignment, changes: list[Change]) -> tuple[int, list[str]]:
    """보고된 alignment + 변경 목록만으로 원본에서 수정본을 재구성해 실제 수정본과 대조한다."""
    checks = 0
    failures: list[str] = []
    gid = al.grid_id
    row_map = dict(al.row_pairs)
    col_map = dict(al.col_pairs)
    prefix = f"[{gid}]"

    checks += 1
    if len(set(row_map.values())) != len(al.row_pairs) or len(row_map) != len(al.row_pairs):
        failures.append(f"{prefix} 행 대응이 1:1 이 아님")
    checks += 1
    if len(set(col_map.values())) != len(al.col_pairs) or len(col_map) != len(al.col_pairs):
        failures.append(f"{prefix} 열 대응이 1:1 이 아님")

    rec: dict[tuple[int, int], str] = {}
    for (r, c), v in base.all_canon().items():
        if r in row_map and c in col_map:
            rec[(row_map[r], col_map[c])] = v
    removed_rows: dict[int, dict] = {}
    removed_cols: dict[int, dict] = {}
    added_rows: set[int] = set()
    added_cols: set[int] = set()

    for ch in changes:
        d = ch.detail
        if d.get("grid_id") != gid:
            continue
        t = ch.change_type
        if t == ChangeType.CELL_MODIFIED:
            br, bc = d["base_rc"]
            rr, rc = d["rev_rc"]
            checks += 1
            if base.value(br, bc) != d.get("before_canon"):
                failures.append(f"{prefix} {ch.id}: 보고된 이전 값이 원본과 다름 ({base.addr(br, bc)})")
            if row_map.get(br) != rr or col_map.get(bc) != rc:
                failures.append(f"{prefix} {ch.id}: 셀 좌표가 대응표와 불일치")
            if d.get("after_canon") is None:
                rec.pop((rr, rc), None)
            else:
                rec[(rr, rc)] = d["after_canon"]
        elif t == ChangeType.ROW_ADDED:
            added_rows.add(d["rev_row"])
            for c, v in d["cells"]:
                rec[(d["rev_row"], c)] = v
        elif t == ChangeType.COLUMN_ADDED:
            added_cols.add(d["rev_col"])
            for r, v in d["cells"]:
                rec[(r, d["rev_col"])] = v
        elif t == ChangeType.ROW_REMOVED:
            removed_rows[d["base_row"]] = {c: v for c, v in d["cells"]}
        elif t == ChangeType.COLUMN_REMOVED:
            removed_cols[d["base_col"]] = {r: v for r, v in d["cells"]}

    actual = rev.all_canon()
    checks += 1
    if rec != actual:
        diffs = []
        for k in sorted(set(rec) | set(actual)):
            if rec.get(k) != actual.get(k):
                diffs.append(f"{rev.addr(*k)} 재구성={N.shorten(N.display(rec.get(k)), 30)!r} 실제={N.shorten(N.display(actual.get(k)), 30)!r}")
        failures.append(f"{prefix} 재구성 불일치 {len(diffs)}건: " + "; ".join(diffs[:8]))

    for r in base.rows():
        if r in row_map:
            continue
        checks += 1
        if r not in removed_rows:
            failures.append(f"{prefix} 원본 {r}행이 대응도 삭제 보고도 없음")
        elif removed_rows[r] != base.row_cells(r):
            failures.append(f"{prefix} 원본 {r}행 삭제 보고 내용 불일치")
    for c in base.cols():
        if c in col_map:
            continue
        checks += 1
        expected = {br: base.value(br, c) for br in row_map if base.value(br, c) is not None}
        if c not in removed_cols:
            failures.append(f"{prefix} 원본 {base.col_name(c)}열이 대응도 삭제 보고도 없음")
        elif removed_cols[c] != expected:
            failures.append(f"{prefix} 원본 {base.col_name(c)}열 삭제 보고 내용 불일치")
    paired_rev_rows = set(row_map.values())
    paired_rev_cols = set(col_map.values())
    for r in rev.rows():
        if r not in paired_rev_rows:
            checks += 1
            if r not in added_rows:
                failures.append(f"{prefix} 수정본 {r}행이 대응도 추가 보고도 없음")
    for c in rev.cols():
        if c not in paired_rev_cols:
            checks += 1
            if c not in added_cols:
                failures.append(f"{prefix} 수정본 {rev.col_name(c)}열이 대응도 추가 보고도 없음")
    return checks, failures
