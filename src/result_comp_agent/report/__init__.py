"""보고서 생성 (md / xlsx / json / html).

안정성
* 각 형식은 독립적으로 작성 — 한 형식이 실패해도 나머지는 작성되고 실패는 결과(failures)에 기록
* 임시 파일에 쓴 뒤 교체(atomic) → 중간에 실패해도 깨진 보고서가 남지 않음
* 문자 인코딩 오류(고립 서로게이트 등)는 치환해서 기록
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Callable

from ..models import AnalysisResult, StepFailure


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", errors="replace", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_with(path: Path, writer: Callable[[Path], None]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=path.suffix, dir=str(path.parent))
    os.close(fd)
    try:
        writer(Path(tmp))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_reports(res: AnalysisResult, out_dir: str | Path, formats: list[str] | None = None) -> tuple[dict[str, str], list[StepFailure]]:
    """반환: (작성된 경로, 보고서 작성 실패 목록). JSON 은 다른 형식의 실패까지 반영하도록 마지막에 쓴다."""
    formats = [f.strip().lower() for f in (formats or ["md", "xlsx", "json", "html"]) if f.strip()]
    out = Path(out_dir)
    paths: dict[str, str] = {}
    errors: list[StepFailure] = []

    def attempt(fmt: str, fn: Callable[[], Path]) -> None:
        try:
            paths[fmt] = str(fn())
        except Exception as e:  # noqa: BLE001
            errors.append(StepFailure(category="report", stage=f"report.{fmt}", kind="write_error", target=str(out),
                                      message=f"{type(e).__name__}: {e}", impact=f"{fmt} 보고서가 작성되지 않음"))

    if "md" in formats:
        def _md() -> Path:
            from .markdown_report import render_markdown

            p = out / "report.md"
            atomic_write_text(p, render_markdown(res))
            return p

        attempt("md", _md)
    if "xlsx" in formats:
        def _xlsx() -> Path:
            from .excel_report import write_excel

            p = out / "report.xlsx"
            atomic_write_with(p, lambda t: write_excel(res, t))
            return p

        attempt("xlsx", _xlsx)
    if "html" in formats:
        def _html() -> Path:
            from .html_report import render_html

            p = out / "report.html"
            atomic_write_text(p, render_html(res))
            return p

        attempt("html", _html)
    if "json" in formats:
        def _json() -> Path:
            p = out / "analysis.json"
            snapshot = res.model_copy(update={"failures": list(res.failures) + errors})
            atomic_write_text(p, snapshot.model_dump_json(indent=1))
            return p

        attempt("json", _json)
    return paths, errors
