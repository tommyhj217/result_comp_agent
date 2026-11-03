"""Excel(.xlsx/.xlsm) 스냅샷 추출.

* 수식 셀은 수식 문자열을 값으로 사용하고, 마지막 계산값(cached)은 참고용으로 별도 보관
* 셀 메모(코멘트), 서식 시그니처(굵게/기울임/밑줄/글자색/채우기/표시형식) 수집
"""
from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from openpyxl import load_workbook

from ..diff import normalize as N
from ..diff.grid import Grid, GridCell


@dataclass
class WorkbookSnap:
    path: str
    sha256: str
    sheets: list[Grid] = field(default_factory=list)
    props: dict[str, dict] = field(default_factory=dict)  # 시트명 → 속성(데이터 유효성/병합/조건부서식/표시상태/틀고정/필터)
    meta: dict = field(default_factory=dict)

    def sheet(self, name: str) -> Optional[Grid]:
        return next((g for g in self.sheets if g.name == name), None)


def file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _color(c) -> Optional[str]:
    if c is None:
        return None
    try:
        if c.type == "rgb" and isinstance(c.rgb, str):
            return c.rgb
        if c.type == "theme":
            return f"theme{c.theme}" + (f"{c.tint:+.2f}" if c.tint else "")
        if c.type == "indexed":
            return f"idx{c.indexed}"
    except Exception:  # pragma: no cover - 방어
        return None
    return None


def _style_sig(cell) -> tuple:
    f = cell.font
    fill = None
    try:
        if cell.fill is not None and cell.fill.fill_type == "solid":
            fill = _color(cell.fill.fgColor)
    except Exception:  # pragma: no cover
        fill = None
    return (
        bool(f.b) if f is not None else False,
        bool(f.i) if f is not None else False,
        str(f.u) if f is not None and f.u else None,
        _color(f.color) if f is not None else None,
        fill,
        cell.number_format or "General",
    )


def _display(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return N.num_str(v)
    if isinstance(v, dt.datetime):
        return v.date().isoformat() if v.time() == dt.time(0) else v.isoformat(sep=" ")
    if isinstance(v, (dt.date, dt.time)):
        return v.isoformat()
    return N.clean_text(str(v))


def extract_workbook(
    path: str | Path,
    header_rows: Optional[dict[str, int]] = None,
    key_columns: Optional[dict[str, str]] = None,
) -> WorkbookSnap:
    path = str(path)
    header_rows = header_rows or {}
    key_columns = key_columns or {}
    wb = load_workbook(path, data_only=False)
    try:
        wb_cached = load_workbook(path, data_only=True)
    except Exception:  # pragma: no cover
        wb_cached = None
    snap = WorkbookSnap(path=path, sha256=file_sha256(path))
    for ws in wb.worksheets:
        wsc = wb_cached[ws.title] if wb_cached is not None and ws.title in wb_cached.sheetnames else None
        grid = Grid(
            grid_id=f"sheet:{ws.title}",
            name=ws.title,
            is_excel=True,
            header_row=header_rows.get(ws.title, header_rows.get("*")),
            key_column=key_columns.get(ws.title, key_columns.get("*")),
        )
        for row in ws.iter_rows():
            for cell in row:
                v = cell.value
                comment = None
                if cell.comment is not None and cell.comment.text:
                    comment = N.clean_text(cell.comment.text).strip() or None
                if v is None and comment is None:
                    continue
                cached = None
                if cell.data_type == "f" and v is not None:
                    ftext = getattr(v, "text", None) or str(v)
                    if not ftext.startswith("="):
                        ftext = "=" + ftext
                    c = N.canon_formula(ftext)
                    disp = ftext
                    if wsc is not None:
                        cv = wsc.cell(row=cell.row, column=cell.column).value
                        cached = _display(cv) if cv is not None else None
                else:
                    c = N.canon(v)
                    disp = _display(v)
                grid.cells[(cell.row, cell.column)] = GridCell(
                    canon=c,
                    display=disp,
                    cached=cached,
                    comment=comment,
                    style=_style_sig(cell) if c is not None else None,
                )
        snap.sheets.append(grid)
        try:
            snap.props[ws.title] = sheet_props(ws, grid)
        except Exception as e:  # 속성 추출 실패는 값 비교에 영향 없음 → 기록만
            snap.props[ws.title] = {"_error": str(e)}
    return snap


def _cols_label(sqref, headers: dict[int, str]) -> tuple[str, ...]:
    """범위(sqref)를 '열 헤더 이름' 집합으로 변환 → 행 삽입/삭제에 영향받지 않는 비교 키."""
    from openpyxl.utils.cell import range_boundaries

    cols: set[int] = set()
    for part in str(sqref).split():
        try:
            c1, _, c2, _ = range_boundaries(part)
        except Exception:
            continue
        if c1 is None:
            continue
        cols.update(range(c1, (c2 or c1) + 1))
    from openpyxl.utils import get_column_letter

    return tuple(headers.get(c) or get_column_letter(c) for c in sorted(cols))


def sheet_props(ws, grid: Grid) -> dict:
    from ..diff.grid import detect_header_row

    hdr = detect_header_row(grid)
    headers = {c: N.collapse_ws(N.display(grid.value(hdr, c))) for c in grid.cols()} if hdr else {}
    headers = {c: h for c, h in headers.items() if h}
    dv = set()
    for v in getattr(ws.data_validations, "dataValidation", []) or []:
        dv.add(("data_validation", v.type or "", v.operator or "", str(v.formula1 or ""), str(v.formula2 or ""), _cols_label(v.sqref, headers)))
    cf = set()
    for rng in ws.conditional_formatting:
        cols = _cols_label(rng.sqref, headers)
        for rule in rng.rules:
            color = None
            try:
                if rule.dxf is not None and rule.dxf.fill is not None:
                    color = _color(rule.dxf.fill.fgColor) or _color(rule.dxf.fill.bgColor)
            except Exception:
                color = None
            cf.add(("conditional_format", rule.type or "", rule.operator or "", "|".join(rule.formula or []), color or "", cols))
    merged = set()
    for rng in ws.merged_cells.ranges:
        tl = grid.cells.get((rng.min_row, rng.min_col))
        merged.add(("merged", N.shorten(tl.display if tl else "", 40), rng.max_row - rng.min_row + 1, rng.max_col - rng.min_col + 1))
    af = ws.auto_filter.ref if ws.auto_filter is not None else None
    return {
        "data_validation": sorted(dv),
        "conditional_format": sorted(cf),
        "merged": sorted(merged),
        "state": ws.sheet_state,
        "freeze_panes": str(ws.freeze_panes) if ws.freeze_panes else None,
        "auto_filter": _cols_label(af, headers) if af else None,
    }
