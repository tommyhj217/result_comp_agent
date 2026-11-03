"""보고서 생성 (md / xlsx / json / html)."""
from __future__ import annotations

from pathlib import Path

from ..models import AnalysisResult


def write_reports(res: AnalysisResult, out_dir: str | Path, formats: list[str] | None = None) -> dict[str, str]:
    formats = [f.strip().lower() for f in (formats or ["md", "xlsx", "json", "html"]) if f.strip()]
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}
    if "json" in formats:
        p = out / "analysis.json"
        p.write_text(res.model_dump_json(indent=1), encoding="utf-8")
        paths["json"] = str(p)
    if "md" in formats:
        from .markdown_report import render_markdown

        p = out / "report.md"
        p.write_text(render_markdown(res), encoding="utf-8")
        paths["md"] = str(p)
    if "xlsx" in formats:
        from .excel_report import write_excel

        p = out / "report.xlsx"
        write_excel(res, p)
        paths["xlsx"] = str(p)
    if "html" in formats:
        from .html_report import render_html

        p = out / "report.html"
        p.write_text(render_html(res), encoding="utf-8")
        paths["html"] = str(p)
    return paths
