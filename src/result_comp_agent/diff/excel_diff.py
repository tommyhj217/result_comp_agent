"""통합문서(Excel) 단위 diff + 무결성 검증."""
from __future__ import annotations

from collections import Counter
from typing import Optional

from ..extractors.excel import WorkbookSnap
from ..models import Anchor, ArtifactType, ChangeType, IntegrityReport, Location
from . import normalize as N
from .align import jaccard
from .builder import ChangeBuilder
from .grid import Grid, GridAlignment, diff_grids, verify_grid


def _sheet_loc_factory(base_sheet: Grid, rev_sheet: Grid):
    def make_loc(*, part, base_rc, rev_rc, row_label, col_label) -> Location:
        rc = rev_rc if rev_rc is not None else base_rc
        grid = rev_sheet if rev_rc is not None else base_sheet
        cell = None
        if rc is not None and rc[0] is not None and rc[1] is not None:
            cell = grid.addr(rc[0], rc[1])
        elif rc is not None and rc[1] is not None:
            cell = grid.col_name(rc[1]) + ":" + grid.col_name(rc[1])
        elif rc is not None and rc[0] is not None:
            cell = f"{rc[0]}:{rc[0]}"
        base_cell = None
        if base_rc is not None and base_rc[0] is not None and base_rc[1] is not None:
            base_cell = base_sheet.addr(base_rc[0], base_rc[1])
        bits = [f"{grid.name}!{cell}" if cell else grid.name]
        if col_label or row_label:
            bits.append(f"[{col_label or '-'} / {row_label or '-'}]")
        return Location(
            artifact=ArtifactType.XLSX,
            container=rev_sheet.grid_id,
            path=" ".join(bits),
            part=part,
            sheet=grid.name,
            cell=cell,
            base_cell=base_cell,
            row_label=row_label,
            column_label=col_label,
        )

    return make_loc


def _sheet_signature(g: Grid) -> Counter:
    return Counter(g.all_canon().values())


def _headers_of(g: Grid) -> list[str]:
    from .grid import detect_header_row

    h = detect_header_row(g)
    if h is None:
        return []
    return [N.collapse_ws(N.display(v)) for c, v in sorted(g.row_cells(h).items())]


def diff_workbooks(
    base: WorkbookSnap,
    rev: WorkbookSnap,
    builder: ChangeBuilder,
    *,
    compare_styles: bool = True,
) -> tuple[dict, IntegrityReport]:
    pairs: list[tuple[Grid, Grid, str]] = []
    rev_by_name = {g.name: g for g in rev.sheets}
    used_rev: set[str] = set()
    un_base: list[Grid] = []
    for g in base.sheets:
        if g.name in rev_by_name:
            pairs.append((g, rev_by_name[g.name], "name"))
            used_rev.add(g.name)
        else:
            un_base.append(g)
    un_rev = [g for g in rev.sheets if g.name not in used_rev]
    # 이름이 바뀐 시트: 내용 유사도로 대응 (탐욕적, 임계값 0.5)
    cands = []
    for b in un_base:
        sb = _sheet_signature(b)
        for r in un_rev:
            cands.append((jaccard(sb, _sheet_signature(r)), b.name, r.name))
    cands.sort(reverse=True)
    renamed_b, renamed_r = set(), set()
    for s, bn, rn in cands:
        if s < 0.5 or bn in renamed_b or rn in renamed_r:
            continue
        renamed_b.add(bn)
        renamed_r.add(rn)
        pairs.append((base.sheet(bn), rev.sheet(rn), f"content:{s:.2f}"))

    alignments: list[GridAlignment] = []
    sheet_pairs_out = []
    for b, r, how in pairs:
        loc = _sheet_loc_factory(b, r)
        if b.name != r.name:
            builder.add(
                ChangeType.SHEET_RENAMED,
                Location(artifact=ArtifactType.XLSX, container=r.grid_id, path=f"시트 '{b.name}' → '{r.name}'", part="sheet", sheet=r.name),
                before=b.name,
                after=r.name,
                pairing="similarity",
                pairing_score=float(how.split(":")[1]),
                detail={"base_sheet": b.name, "rev_sheet": r.name},
                anchors=[Anchor(text=b.name, role="sheet"), Anchor(text=r.name, role="new_value")],
            )
        al = diff_grids(b, r, builder, loc, context_anchors=[Anchor(text=r.name, role="sheet")], compare_styles=compare_styles)
        alignments.append(al)
        sheet_pairs_out.append({"base": b.name, "rev": r.name, "how": how, "grid": al.to_dict()})

    rev_names_order = [g.name for g in rev.sheets]
    base_names_order = [g.name for g in base.sheets]
    for r in rev.sheets:
        if r.name in used_rev or r.name in renamed_r:
            continue
        idx = rev_names_order.index(r.name)
        left = next((n for n in reversed(rev_names_order[:idx]) if n in used_rev or n in renamed_r), None)
        right = next((n for n in rev_names_order[idx + 1 :] if n in used_rev or n in renamed_r), None)
        cells = sorted(r.all_canon().items())
        anchors = [Anchor(text=r.name, role="new_value")]
        if left:
            anchors.append(Anchor(text=left, role="neighbor_left"))
        if right:
            anchors.append(Anchor(text=right, role="neighbor_right"))
        builder.add(
            ChangeType.SHEET_ADDED,
            Location(artifact=ArtifactType.XLSX, container=r.grid_id, path=f"시트 '{r.name}' 추가", part="sheet", sheet=r.name),
            after=r.name,
            detail={
                "sheet": r.name,
                "cells": [[k[0], k[1], v] for k, v in cells],
                "n_cells": len(cells),
                "headers": _headers_of(r),
                "preview": [N.shorten(N.display(v), 40) for _, v in cells[:12]],
                "sibling_sheets": base_names_order,
            },
            anchors=anchors,
        )
    for b in base.sheets:
        if b.name in rev_by_name or b.name in renamed_b:
            continue
        cells = sorted(b.all_canon().items())
        builder.add(
            ChangeType.SHEET_REMOVED,
            Location(artifact=ArtifactType.XLSX, container=b.grid_id, path=f"시트 '{b.name}' 삭제", part="sheet", sheet=b.name),
            before=b.name,
            detail={
                "sheet": b.name,
                "cells": [[k[0], k[1], v] for k, v in cells],
                "n_cells": len(cells),
                "headers": _headers_of(b),
            },
            anchors=[Anchor(text=b.name, role="sheet")],
        )

    # ---------------- 무결성 검증
    checks, failures = 0, []
    paired_base = {b.name for b, _, _ in pairs}
    paired_rev = {r.name for _, r, _ in pairs}
    added = {c.detail["sheet"]: c for c in builder.changes if c.change_type == ChangeType.SHEET_ADDED}
    removed = {c.detail["sheet"]: c for c in builder.changes if c.change_type == ChangeType.SHEET_REMOVED}
    for r in rev.sheets:
        checks += 1
        if r.name in paired_rev:
            continue
        ch = added.get(r.name)
        if ch is None:
            failures.append(f"수정본 시트 '{r.name}' 이 대응도 추가 보고도 없음")
        elif {(a, b): v for a, b, v in ch.detail["cells"]} != r.all_canon():
            failures.append(f"시트 추가 보고 '{r.name}' 내용 불일치")
    for b in base.sheets:
        checks += 1
        if b.name in paired_base:
            continue
        ch = removed.get(b.name)
        if ch is None:
            failures.append(f"원본 시트 '{b.name}' 이 대응도 삭제 보고도 없음")
        elif {(a, c): v for a, c, v in ch.detail["cells"]} != b.all_canon():
            failures.append(f"시트 삭제 보고 '{b.name}' 내용 불일치")
    for (b, r, _), al in zip(pairs, alignments):
        c, f = verify_grid(b, r, al, builder.changes)
        checks += c
        failures.extend(f)

    integrity = IntegrityReport(status="PASS" if not failures else "FAIL", checks=checks, failures=failures)
    return {"sheets": sheet_pairs_out}, integrity
