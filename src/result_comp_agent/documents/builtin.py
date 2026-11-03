"""기본 제공 문서 핸들러."""
from __future__ import annotations

from ..diff import normalize as N
from ..diff.builder import ChangeBuilder
from . import register_handler
from .base import CompareOptions, CompareOutput, DocUnit, DocumentHandler, FlowDocHandler


class XlsxHandler(DocumentHandler):
    name = "excel"
    artifact = "xlsx"
    extensions = (".xlsx", ".xlsm")
    description = "Excel 통합문서: 시트·행·열·셀 값·수식·메모·서식 + 데이터 유효성·조건부 서식·병합·틀고정·필터·표시상태"
    requires = ("openpyxl",)

    def compare(self, baseline: str, revised: str, builder: ChangeBuilder, opts: CompareOptions) -> CompareOutput:
        from ..diff.excel_diff import diff_workbooks
        from ..extractors.excel import extract_workbook

        b = extract_workbook(baseline, opts.header_rows, opts.key_columns)
        r = extract_workbook(revised, opts.header_rows, opts.key_columns)
        alignment, integrity = diff_workbooks(b, r, builder, compare_styles=opts.compare_styles)
        notes = [f"{s}: 속성 추출 실패 {p['_error']}" for s, p in {**b.props, **r.props}.items() if "_error" in p]
        return CompareOutput(alignment, integrity, b.sha256, r.sha256, notes)

    def outline(self, path: str) -> list[DocUnit]:
        from ..extractors.excel import extract_workbook

        snap = extract_workbook(path)
        out = []
        for g in snap.sheets:
            for r in g.rows():
                cells = sorted(g.row_cells(r).items())
                out.append(DocUnit(f"{g.name}!{r}", " | ".join(f"{g.addr(r, c)}={N.display(v)}" for c, v in cells), "row"))
            props = snap.props.get(g.name) or {}
            desc = []
            for k in ("data_validation", "conditional_format", "merged"):
                if props.get(k):
                    desc.append(f"{k}: {len(props[k])}건 {props[k][:5]}")
            for k in ("state", "freeze_panes", "auto_filter"):
                if props.get(k):
                    desc.append(f"{k}: {props[k]}")
            if desc:
                out.append(DocUnit(f"{g.name}!속성", " / ".join(desc), "properties"))
        return out


class PptxHandler(DocumentHandler):
    name = "powerpoint"
    artifact = "pptx"
    extensions = (".pptx",)
    description = "PowerPoint: 슬라이드·도형·문단·표·차트·이미지·SmartArt 텍스트·발표자 노트"
    requires = ("pptx",)

    def compare(self, baseline: str, revised: str, builder: ChangeBuilder, opts: CompareOptions) -> CompareOutput:
        from ..diff.pptx_diff import diff_presentations
        from ..extractors.pptx import extract_presentation

        b, r = extract_presentation(baseline), extract_presentation(revised)
        alignment, integrity = diff_presentations(b, r, builder, compare_geometry=opts.compare_geometry)
        return CompareOutput(alignment, integrity, b.sha256, r.sha256, [])

    def outline(self, path: str) -> list[DocUnit]:
        from ..extractors.pptx import extract_presentation

        snap = extract_presentation(path)
        out = []
        for s in snap.slides:
            out.append(DocUnit(f"slide {s.index}", f"[{s.layout}] {s.title}", "slide"))
            for sh in s.shapes:
                if sh.kind == "table" and sh.table is not None:
                    rows = {}
                    for (r, c), v in sorted(sh.table.cells.items()):
                        rows.setdefault(r, []).append(v.display)
                    text = "\n".join(f"R{r}: " + " | ".join(vals) for r, vals in rows.items())
                else:
                    text = sh.text or f"[{sh.kind}]"
                out.append(DocUnit(f"slide {s.index} / {sh.name}", text, sh.kind))
            if s.notes:
                out.append(DocUnit(f"slide {s.index} / notes", "\n".join(p for p, _ in s.notes), "notes"))
        return out


class DocxHandler(FlowDocHandler):
    name = "word"
    artifact = "docx"
    extensions = (".docx",)
    description = "Word 문서: 제목(섹션)·문단·목록·표·메모, 변경 내용 추적은 '모두 승인' 기준"
    requires = ("docx",)
    install_hint = "pip install python-docx"

    def extract(self, path: str):
        from ..extractors.flow import extract_docx

        return extract_docx(path)


class MarkdownHandler(FlowDocHandler):
    name = "markdown"
    artifact = "text"
    extensions = (".md", ".markdown")
    description = "Markdown: 제목·문단·목록·코드 블록·표"

    def extract(self, path: str):
        from ..extractors.flow import extract_markdown

        return extract_markdown(path)


class TextHandler(FlowDocHandler):
    name = "text"
    artifact = "text"
    extensions = (".txt", ".log")
    description = "텍스트: 줄 단위 (UTF-8/CP949 자동 판별)"

    def extract(self, path: str):
        from ..extractors.flow import extract_text

        return extract_text(path)


class PdfHandler(FlowDocHandler):
    name = "pdf"
    artifact = "pdf"
    extensions = (".pdf",)
    description = "PDF: 페이지별 텍스트 줄 (텍스트 레이어 기준)"
    requires = ("pypdf",)
    install_hint = "pip install pypdf"
    reliability_note = "PDF 는 추출된 텍스트 기준 비교입니다 (서식·배치·이미지 미비교, 줄 나눔은 PDF 생성 방식에 따라 달라질 수 있음)"

    def extract(self, path: str):
        from ..extractors.flow import extract_pdf

        return extract_pdf(path)


class CsvHandler(DocumentHandler):
    name = "csv"
    artifact = "csv"
    extensions = (".csv", ".tsv")
    description = "CSV/TSV: 표(행·열·셀) — 인코딩(UTF-8/CP949)·구분자 자동 판별"

    def compare(self, baseline: str, revised: str, builder: ChangeBuilder, opts: CompareOptions) -> CompareOutput:
        from ..diff.excel_diff import diff_workbooks
        from ..extractors.flow import extract_csv

        b, r = extract_csv(baseline), extract_csv(revised)
        for snap in (b, r):
            for g in snap.sheets:
                g.header_row = opts.header_rows.get("*", g.header_row)
                g.key_column = opts.key_columns.get("*", g.key_column)
        alignment, integrity = diff_workbooks(b, r, builder, compare_styles=False)
        notes = []
        if b.meta.get("encoding") != r.meta.get("encoding"):
            notes.append(f"인코딩이 다름: 원본 {b.meta.get('encoding')} / 수정본 {r.meta.get('encoding')} (값은 디코딩 후 비교)")
        return CompareOutput(alignment, integrity, b.sha256, r.sha256, notes)

    def outline(self, path: str) -> list[DocUnit]:
        from ..extractors.flow import extract_csv

        g = extract_csv(path).sheets[0]
        return [DocUnit(f"row {r}", " | ".join(N.display(v) for _, v in sorted(g.row_cells(r).items())), "row") for r in g.rows()]


for _h in (XlsxHandler(), PptxHandler(), DocxHandler(), CsvHandler(), MarkdownHandler(), TextHandler(), PdfHandler()):
    register_handler(_h)
