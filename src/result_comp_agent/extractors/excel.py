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
    return snap
