"""비교쌍 하나(원본 산출물 vs 수정본)를 비교하는 진입점."""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Optional

from ..models import ArtifactType, ComparisonResult
from .builder import ChangeBuilder

XLSX_EXT = {".xlsx", ".xlsm"}
PPTX_EXT = {".pptx"}


def detect_artifact(path: str | Path) -> ArtifactType:
    ext = Path(path).suffix.lower()
    if ext in XLSX_EXT:
        return ArtifactType.XLSX
    if ext in PPTX_EXT:
        return ArtifactType.PPTX
    raise ValueError(f"지원하지 않는 산출물 형식: {path} (지원: .xlsx/.xlsm/.pptx)")


def compare_files(
    baseline: str | Path,
    revised: str | Path,
    *,
    pair_id: str,
    agent_id: str,
    revised_by: Optional[str] = None,
    baseline_ref: Optional[str] = None,
    revised_ref: Optional[str] = None,
    compare_styles: bool = True,
    compare_geometry: bool = True,
    header_rows: Optional[dict[str, int]] = None,
    key_columns: Optional[dict[str, str]] = None,
) -> ComparisonResult:
    art = detect_artifact(baseline)
    if detect_artifact(revised) != art:
        raise ValueError(f"원본과 수정본 형식이 다릅니다: {baseline} / {revised}")
    builder = ChangeBuilder(pair_id, agent_id, art)
    if art == ArtifactType.XLSX:
        from ..extractors.excel import extract_workbook
        from .excel_diff import diff_workbooks

        b = extract_workbook(baseline, header_rows, key_columns)
        r = extract_workbook(revised, header_rows, key_columns)
        alignment, integrity = diff_workbooks(b, r, builder, compare_styles=compare_styles)
        b_sha, r_sha = b.sha256, r.sha256
    else:
        from ..extractors.pptx import extract_presentation
        from .pptx_diff import diff_presentations

        b = extract_presentation(baseline)
        r = extract_presentation(revised)
        alignment, integrity = diff_presentations(b, r, builder, compare_geometry=compare_geometry)
        b_sha, r_sha = b.sha256, r.sha256

    changes = builder.changes
    stats = {
        "total": len(changes),
        "content": sum(1 for c in changes if not c.format_only),
        "format_only": sum(1 for c in changes if c.format_only),
        "by_type": dict(Counter(c.change_type.value for c in changes)),
        "low_confidence_pairing": sum(1 for c in changes if c.pairing == "similarity" and c.pairing_score < 0.7),
    }
    return ComparisonResult(
        pair_id=pair_id,
        agent_id=agent_id,
        artifact=art,
        baseline_path=str(baseline),
        revised_path=str(revised),
        baseline_sha256=b_sha,
        revised_sha256=r_sha,
        revised_by=revised_by,
        baseline_ref=baseline_ref,
        revised_ref=revised_ref,
        changes=changes,
        alignment=alignment,
        integrity=integrity,
        stats=stats,
    )
