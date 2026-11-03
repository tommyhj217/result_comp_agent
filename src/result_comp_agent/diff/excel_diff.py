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


PROP_LABEL = {
    "data_validation": "데이터 유효성(입력 규칙)",
    "conditional_format": "조건부 서식",
    "merged": "병합 셀",
    "state": "시트 표시 상태",
    "freeze_panes": "틀 고정",
    "auto_filter": "자동 필터",
}


def _describe_prop(kind: str, item) -> str:
    if kind == "data_validation":
        _, typ, op, f1, f2, cols = item
        rule = f1 + (f" ~ {f2}" if f2 else "")
        return f"[{typ}{' ' + op if op else ''}] {rule} → 열 {', '.join(cols) or '-'}"
    if kind == "conditional_format":
        _, typ, op, formula, color, cols = item
        return f"[{typ}{' ' + op if op else ''}] {formula}{' 채우기 ' + color if color else ''} → 열 {', '.join(cols) or '-'}"
    if kind == "merged":
        _, text, nr, nc = item
        return f"'{text}' ({nr}행×{nc}열)"
    return str(item)


def diff_sheet_props(bp: dict, rp: dict, sheet: Grid, builder: ChangeBuilder) -> int:
    """값과 별개인 시트 속성 비교. 행 삽입에 흔들리지 않도록 범위를 '열 헤더 이름'으로 비교한다."""
    if not bp or not rp or "_error" in bp or "_error" in rp:
        return 0
    n = 0
    for key in ("data_validation", "conditional_format", "merged"):
        B = {tuple(x) for x in bp.get(key, [])}
        R = {tuple(x) for x in rp.get(key, [])}
        for action, items in (("added", sorted(R - B)), ("removed", sorted(B - R))):
            for item in items:
                desc = _describe_prop(key, item)
                cols = list(item[-1]) if key in ("data_validation", "conditional_format") else []
                builder.add(
                    ChangeType.SHEET_PROPERTY_CHANGED,
                    Location(artifact=ArtifactType.XLSX, container=sheet.grid_id, path=f"{sheet.name} · {PROP_LABEL[key]} {'추가' if action == 'added' else '삭제'}",
                             part="sheet_property", sheet=sheet.name, column_label=", ".join(cols) or None),
                    before=desc if action == "removed" else None,
                    after=desc if action == "added" else None,
                    format_only=(key != "data_validation"),
                    detail={"property": key, "action": action, "value": list(item)},
                    anchors=[Anchor(text=sheet.name, role="sheet")] + [Anchor(text=c, role="column") for c in cols[:3]],
                )
                n += 1
    for key in ("state", "freeze_panes", "auto_filter"):
        a, b = bp.get(key), rp.get(key)
        if a != b:
            fa = ", ".join(a) if isinstance(a, (list, tuple)) else a
            fb = ", ".join(b) if isinstance(b, (list, tuple)) else b
            builder.add(
                ChangeType.SHEET_PROPERTY_CHANGED,
                Location(artifact=ArtifactType.XLSX, container=sheet.grid_id, path=f"{sheet.name} · {PROP_LABEL[key]} 변경", part="sheet_property", sheet=sheet.name),
                before=str(fa) if fa is not None else None, after=str(fb) if fb is not None else None, format_only=True,
                detail={"property": key, "action": "changed", "before": a, "after": b},
                anchors=[Anchor(text=sheet.name, role="sheet")],
            )
            n += 1
    return n


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
        diff_sheet_props(base.props.get(b.name, {}), rev.props.get(r.name, {}), r, builder)
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
